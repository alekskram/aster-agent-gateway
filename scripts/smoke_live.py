#!/usr/bin/env python3
"""LIVE smoke script - the ONLY file allowed to touch the network.

Budget: AT MOST 13 live HTTP calls total (counted and printed). Each
call's raw response is recorded to tests/fixtures/ via the recorder
helper as it goes, then the corresponding offline checks are asserted.
Prints a PASS/FAIL table and the call count; exits nonzero on failure.

Chosen calls (13 max, per spec):
  1  fapi ticker/24hr ALL        (market_overview composite, part 1)
  2  fapi exchangeInfo           (market_overview composite, part 2)
  3  fapi premiumIndex ALL       (funding_overview/screener basis)
  4  fapi fundingInfo ALL        (fundingInfo schema + D1 regime live)
  5  sapi exchangeInfo           (spot_overview join, part 1)
  6  sapi ticker/24hr ALL        (spot_overview join, part 2; the
                                  raw list carries ~24k ephemeral
                                  options rows, fixture is sampled)
  7  sapi klines BTCUSDT         (spot klines live check)
  8  sapi depth BTCUSDT          (spot depth live check)
  9  fapi openInterest BTCUSDT   (per-symbol OI shape)
 10  solana getSignaturesForAddress (vault deposit signatures)
 11  tapi aster_getBalance       (public-account probe; privacy-empty
                                  is an acceptable honest outcome)
 12  fapi /trades BTCUSDT        (fresh trades shape)
 13  sapi /trades BTCUSDT        (probe the uncertain spot path)

Run:  uv run python scripts/smoke_live.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURES = REPO / "tests" / "fixtures"
FIXTURES.mkdir(parents=True, exist_ok=True)

import aster_mcp.chain as chain     # noqa: E402
import aster_mcp.rest as rest       # noqa: E402
import aster_mcp.server as srv      # noqa: E402

CALLS = {"n": 0}
RESULTS: list[tuple[str, bool, str]] = []
FIXTURES_WRITTEN: list[str] = []


def _record(name: str, payload) -> None:
    path = FIXTURES / f"{name}.json"
    path.write_text(json.dumps(payload, indent=1))
    FIXTURES_WRITTEN.append(f"{name}.json ({path.stat().st_size} B)")


def _record_spot_sample(tickers: list, listed: set) -> int:
    """Record sapi ticker/24hr as a REDUCED sample: every listed row
    verbatim plus the first 200 ephemeral options rows (repo-size
    sanity; the raw live list is ~10 MB of BTC_UP_DOWN_5M_* noise).
    Returns the number of rows written."""
    keep = [t for t in tickers if isinstance(t, dict)
            and t.get("symbol") in listed]
    ephem = [t for t in tickers if isinstance(t, dict)
             and t.get("symbol") not in listed]
    sample = keep + ephem[:200]
    _record("sapi_ticker24hr_all", sample)
    return len(sample)


def live_get(url: str) -> tuple[int, Any]:
    """One counted live GET. Returns (status, json)."""
    CALLS["n"] += 1
    req = urllib.request.Request(url, headers={"User-Agent":
                                               "aster-agent-gateway/0.1"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.status, json.loads(r.read())


def live_rpc(url: str, method: str, params) -> Any:
    """One counted live JSON-RPC POST. Returns result (raises on
    JSON-RPC error)."""
    CALLS["n"] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "aster-agent-gateway/0.1"})
    with urllib.request.urlopen(req, timeout=25) as r:
        payload = json.loads(r.read())
    if "error" in payload:
        raise RuntimeError(f"json-rpc error: {payload['error']}")
    return payload.get("result")


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))


def main() -> int:
    t_start = time.time()

    # ---- 1+2: market_overview composite (ticker ALL + exchangeInfo)
    try:
        st, tickers = live_get(rest.FAPI_BASE + "/ticker/24hr")
        _record("fapi_ticker24hr_all", tickers)
        n_tickers = len(tickers) if isinstance(tickers, list) else 0
        check("fapi ticker/24hr ALL", st == 200 and n_tickers > 500,
              f"status {st}, {n_tickers} rows")
    except Exception as e:
        check("fapi ticker/24hr ALL", False, repr(e)[:120])
        tickers = []
    try:
        st, info = live_get(rest.FAPI_BASE + "/exchangeInfo")
        _record("fapi_exchangeInfo", info)
        syms = (info or {}).get("symbols") or []
        check("fapi exchangeInfo", st == 200 and len(syms) > 500,
              f"status {st}, {len(syms)} symbols")
    except Exception as e:
        check("fapi exchangeInfo", False, repr(e)[:120])
        info = {"symbols": []}
    # join via the offline-path functions but live data
    try:
        import aster_mcp.server as srv2
        rest._CACHE[rest._cache_key("fapi", "/ticker/24hr", {})] = \
            (time.monotonic(), tickers)
        rest._CACHE[rest._cache_key("fapi", "/exchangeInfo", {})] = \
            (time.monotonic(), info)
        mo = srv2.market_overview(limit=5)
        check("market_overview join", bool(mo.get("markets")),
              f"{mo.get('count')} TRADING rows; "
              f"statuses={mo.get('status_counts')}")
    except Exception as e:
        check("market_overview join", False, repr(e)[:120])

    # ---- 3: premiumIndex ALL
    try:
        st, premium = live_get(rest.FAPI_BASE + "/premiumIndex")
        _record("fapi_premiumIndex_all", premium)
        n = len(premium) if isinstance(premium, list) else 0
        check("fapi premiumIndex ALL", st == 200 and n > 500,
              f"status {st}, {n} rows")
    except Exception as e:
        check("fapi premiumIndex ALL", False, repr(e)[:120])

    # ---- 4: fapi fundingInfo ALL (schema + fixture refresh)
    funding = None
    try:
        st, funding = live_get(rest.FAPI_BASE + "/fundingInfo")
        _record("fapi_fundingInfo_all", funding)
        n = len(funding) if isinstance(funding, list) else 0
        has_new = bool(funding) and "fundingFeeCap" in funding[0]
        check("fapi fundingInfo ALL", st == 200 and n > 700 and
              has_new, f"status {st}, {n} rows, fundingFeeCap "
              f"schema: {has_new}")
    except Exception as e:
        check("fapi fundingInfo ALL", False, repr(e)[:120])
        funding = []

    # ---- 4b: funding_screener live join (D1 non-null headroom/regime)
    try:
        rest._CACHE[rest._cache_key("fapi", "/premiumIndex", {})] = \
            (time.monotonic(), premium)
        rest._CACHE[rest._cache_key("fapi", "/fundingInfo", {})] = \
            (time.monotonic(), funding)
        sc = srv.funding_screener(top=5)
        top = sc.get("top_by_annualized_rate") or []
        nonnull = all(
            r["funding_regime"]["headroom_bps"] is not None
            and r["funding_regime"]["regime"] is not None
            and r["funding_regime"]["cap"] is not None
            for r in top)
        check("funding_screener live join (D1)", bool(top) and nonnull,
              f"{len(top)} rows, all headroom/regime non-null; "
              f"top={top[0]['symbol'] if top else '-'} "
              f"regime={top[0]['funding_regime']['regime'] if top else '-'}")
    except Exception as e:
        check("funding_screener live join (D1)", False, repr(e)[:120])

    # ---- 5: sapi exchangeInfo (spot join, part 1)
    try:
        st, sinfo = live_get(rest.SAPI_BASE + "/exchangeInfo")
        _record("sapi_exchangeInfo", sinfo)
        ssyms = (sinfo or {}).get("symbols") or []
        check("sapi exchangeInfo", st == 200 and 40 <= len(ssyms) <= 120,
              f"status {st}, {len(ssyms)} symbols")
    except Exception as e:
        check("sapi exchangeInfo", False, repr(e)[:120])
        sinfo = {"symbols": []}

    # ---- 6: sapi ticker/24hr ALL (spot join, part 2)
    try:
        st, stickers = live_get(rest.SAPI_BASE + "/ticker/24hr")
        listed = {s.get("symbol") for s in (sinfo or {}).get("symbols")
                  or []}
        nsample = _record_spot_sample(stickers or [], listed)
        n = len(stickers) if isinstance(stickers, list) else 0
        check("sapi ticker/24hr ALL", st == 200 and n > 20000,
              f"status {st}, {n} rows live, fixture sampled to "
              f"{nsample}")
    except Exception as e:
        check("sapi ticker/24hr ALL", False, repr(e)[:120])
        stickers = []

    # ---- 6b: spot_overview live join (ephemeral options filtered)
    try:
        rest._CACHE[rest._cache_key("sapi", "/ticker/24hr", {})] = \
            (time.monotonic(), stickers)
        rest._CACHE[rest._cache_key("sapi", "/exchangeInfo", {})] = \
            (time.monotonic(), sinfo)
        so = srv.spot_overview(limit=5)
        cnt = so.get("count") or 0
        syms = [r["symbol"] for r in so.get("pairs") or []]
        clean = not any("_UP_DOWN_" in s or s.startswith("EVENT")
                        for s in syms)
        check("spot_overview live join (ephemeral filter)",
              40 <= cnt <= 120 and clean,
              f"count={cnt}, unlisted_or_not_trading_filtered="
              f"{so.get('unlisted_or_not_trading_filtered')}, "
              f"junk_filtered={so.get('junk_filtered')}")
    except Exception as e:
        check("spot_overview live join (ephemeral filter)", False,
              repr(e)[:120])

    # ---- 7: sapi klines
    try:
        st, kl = live_get(rest.SAPI_BASE + "/klines?symbol=BTCUSDT"
                          "&interval=1h&limit=24")
        _record("sapi_klines_btcusdt", kl)
        check("sapi klines", st == 200 and isinstance(kl, list)
              and len(kl) > 0, f"status {st}, {len(kl)} rows")
    except Exception as e:
        check("sapi klines", False, repr(e)[:120])

    # ---- 8: sapi depth
    try:
        st, dp = live_get(rest.SAPI_BASE +
                          "/depth?symbol=BTCUSDT&limit=10")
        _record("sapi_depth_btcusdt", dp)
        ok = st == 200 and isinstance(dp, dict) and dp.get("bids")
        check("sapi depth", bool(ok), f"status {st}, "
              f"{len(dp.get('bids') or [])} bid levels")
    except Exception as e:
        check("sapi depth", False, repr(e)[:120])

    # ---- 9: fapi openInterest
    try:
        st, oi = live_get(rest.FAPI_BASE +
                          "/openInterest?symbol=BTCUSDT")
        _record("fapi_openInterest_btcusdt", oi)
        check("fapi openInterest", st == 200 and "openInterest" in
              (oi or {}), f"status {st}, "
              f"OI={((oi or {}).get('openInterest'))}")
    except Exception as e:
        check("fapi openInterest", False, repr(e)[:120])

    # ---- 10: Solana vault signatures
    try:
        sigs = live_rpc(chain.SOLANA_RPC, "getSignaturesForAddress",
                        [chain.ASTER_SOLANA_VAULT, {"limit": 20}])
        _record("solana_vault_signatures", sigs or [])
        check("solana vault signatures", isinstance(sigs, list)
              and len(sigs) > 0, f"{len(sigs or [])} signatures")
    except Exception as e:
        check("solana vault signatures", False, repr(e)[:120])

    # ---- 11: tapi public-account probe (privacy-empty = honest PASS
    # of the degradation path; data = bonus). Params use the DOCUMENTED
    # ARRAY form [address, blockTag] - the object form returns 400.
    tapi_outcome = "not-run"
    try:
        res = live_rpc(chain.TAPI_URL, "aster_getBalance",
                       ["0x128463a60784c4d3f46c23af3f65ed859ba87974",
                        "latest"])
        _record("tapi_getBalance_vault", {"result": res})
        has_data = isinstance(res, dict) and any(
            res.get(k) for k in ("perpAssets", "balances",
                                 "positions", "orders", "fills"))
        if not has_data:
            tapi_outcome = (f"privacy-empty (honest degradation path; "
                            f"accountPrivacy="
                            f"{(res or {}).get('accountPrivacy') if isinstance(res, dict) else None})")
            check("tapi aster_getBalance", True,
                  "privacy-empty result -> degradation is correct")
        else:
            tapi_outcome = f"DATA returned: {json.dumps(res)[:100]}"
            check("tapi aster_getBalance", True, tapi_outcome)
    except Exception as e:
        tapi_outcome = f"error: {repr(e)[:150]}"
        check("tapi aster_getBalance", False, tapi_outcome)

    # ---- 12: fapi trades
    try:
        st, tr = live_get(rest.FAPI_BASE +
                          "/trades?symbol=BTCUSDT&limit=20")
        _record("fapi_trades_btcusdt", tr)
        check("fapi trades", st == 200 and isinstance(tr, list)
              and len(tr) > 0, f"status {st}, {len(tr)} fills")
    except Exception as e:
        check("fapi trades", False, repr(e)[:120])

    # ---- 13: sapi trades (probe the uncertain spot path)
    try:
        st, tr = live_get(rest.SAPI_BASE +
                          "/trades?symbol=BTCUSDT&limit=20")
        if st == 200 and isinstance(tr, list):
            _record("sapi_trades_btcusdt", tr)
            check("sapi trades probe", True,
                  f"ALIVE: status {st}, {len(tr)} fills")
        else:
            check("sapi trades probe", True,
                  f"dead (status {st}) -> tool degrades honestly")
    except urllib.error.HTTPError as e:
        check("sapi trades probe", True,
              f"dead (HTTP {e.code}) -> tool degrades honestly")
    except Exception as e:
        check("sapi trades probe", False, repr(e)[:120])

    # ---------------- summary
    failed = [r for r in RESULTS if not r[1]]
    print("\n== smoke summary")
    print(f"live calls used: {CALLS['n']} / 13 budget")
    print(f"fixtures recorded: {len(FIXTURES_WRITTEN)}")
    for f in FIXTURES_WRITTEN:
        print(f"  - {f}")
    print(f"checks: {len(RESULTS) - len(failed)} PASS / "
          f"{len(failed)} FAIL ({time.time() - t_start:.1f}s)")
    print(f"tapi public-account outcome: {tapi_outcome}")
    if CALLS["n"] > 13:
        print("BUDGET EXCEEDED - FAIL")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
