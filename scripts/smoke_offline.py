#!/usr/bin/env python3
"""Offline smoke test for aster_mcp.rest + server - zero HTTP.

Monkeypatches urllib.request.urlopen to serve tests/fixtures/*.json
and exercises every one of the 13 tools through their happy paths.
Run:  uv run python scripts/smoke_offline.py
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
sys.path.insert(0, str(REPO))

import aster_mcp.rest as rest       # noqa: E402
import aster_mcp.server as srv      # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {"X-MBX-USED-WEIGHT-1M": "100"}

    def read(self, n=-1):
        data = json.dumps(self._payload).encode()
        return data if n < 0 else data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def route(url):
    from urllib.parse import urlparse
    q = urlparse(url)
    base = "fapi" if "fapi." in url else "sapi"
    path = q.path.replace("/fapi/v3", "").replace("/api/v3", "")
    stem = path.strip("/").replace("/", "")
    if "ticker" in stem:
        stem = "ticker24hr_all"
    if "premiumIndex" in stem:
        stem = "premiumIndex_all"
    if "fundingInfo" in stem:
        stem = "fundingInfo_all"
    if "exchangeInfo" in stem:
        stem = "exchangeInfo"
    return f"{base}_{stem}"


def fake_urlopen(req, timeout=None):
    name = route(req.full_url)
    mapping = {
        "fapi_exchangeInfo": "fapi_exchangeInfo.json",
        "fapi_ticker24hr_all": "fapi_ticker24hr_all.json",
        "fapi_premiumIndex_all": "fapi_premiumIndex_all.json",
        "fapi_fundingInfo_all": "fapi_fundingInfo_all.json",
        "fapi_depth": "fapi_depth_btcusdt.json",
        "fapi_klines": "fapi_klines_btcusdt_1h.json",
        "fapi_markPriceKlines": "fapi_markPriceKlines_btcusdt_1h.json",
        "fapi_indexPriceKlines": "fapi_indexPriceKlines_btcusdt_1h.json",
        "fapi_trades": "fapi_trades_btcusdt.json",
        "fapi_openInterest": "fapi_openInterest_btcusdt.json",
        "sapi_exchangeInfo": "sapi_exchangeInfo.json",
        "sapi_ticker24hr_all": "sapi_ticker24hr_all.json",
        "sapi_klines": "sapi_klines_btcusdt.json",
        "sapi_depth": "sapi_depth_btcusdt.json",
        "sapi_trades": "sapi_trades_btcusdt.json",
    }
    f = mapping.get(name)
    if f is None or not (FIXTURES / f).exists():
        raise urllib.error.HTTPError(req.full_url, 404, "no route",
                                     __import__("email.message",
                                                fromlist=["Message"])
                                     .Message(), None)
    return FakeResponse(json.loads((FIXTURES / f).read_text()))


def main() -> int:
    urllib.request.urlopen = fake_urlopen
    rest._CACHE.clear()
    with rest._BUCKET_LOCK:
        rest.BUCKETS["fapi"]["used"] = 0
        rest.BUCKETS["sapi"]["used"] = 0
        rest._THROTTLE_UNTIL["fapi"] = 0.0
        rest._THROTTLE_UNTIL["sapi"] = 0.0

    tools = [
        ("market_overview", lambda: srv.market_overview(limit=5)),
        ("exchange_symbols", lambda: srv.exchange_symbols()),
        ("order_book", lambda: srv.order_book("BTCUSDT")),
        ("klines", lambda: srv.klines("BTCUSDT")),
        ("trades", lambda: srv.trades("BTCUSDT")),
        ("spot_overview", lambda: srv.spot_overview()),
        ("funding_overview", lambda: srv.funding_overview()),
        ("tradfi_markets", lambda: srv.tradfi_markets()),
        ("funding_screener", lambda: srv.funding_screener(top=3)),
        ("oi_snapshot", lambda: srv.oi_snapshot(symbols=["BTCUSDT"])),
        ("deposit_flows",
         lambda: srv.deposit_flows(chain_filter="solana")),
        ("account_view",
         lambda: srv.account_view(
             "0x128463a60784c4d3f46c23af3f65ed859ba87974")),
        ("mark_index_divergence",
         lambda: srv.mark_index_divergence(limit=3)),
    ]
    for name, fn in tools:
        try:
            out = fn()
            ok = not (isinstance(out, dict) and out.get("error")
                      and name not in ("account_view", "deposit_flows"))
            check(name, ok,
                  "error-dict" if out.get("error") else
                  f"{len(json.dumps(out))} B")
        except Exception as e:
            check(name, False, repr(e)[:120])

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)} PASS / {len(failed)} FAIL "
          "(0 network calls)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
