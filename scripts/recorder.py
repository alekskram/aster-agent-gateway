#!/usr/bin/env python3
"""Live-response recorder: fetches each fapi/sapi/on-chain surface once
and saves raw JSON to tests/fixtures/ for the offline suite.

NOT run by CI or the test suite; run manually when fixtures need
refreshing. Budget: ~13 requests total (well inside 2400/min fapi,
6000/min sapi, 10/min solana). smoke_live.py is the canonical capture
path (10-call budget); this recorder exists for periodic refreshes:

    python scripts/recorder.py --out tests/fixtures
    python scripts/recorder.py --out tests/fixtures --only fapi_exchangeInfo
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import aster_mcp.chain as chain     # noqa: E402
import aster_mcp.rest as rest       # noqa: E402

# (fixture name, surface, callable)
JOBS = [
    ("fapi_exchangeInfo", "fapi", lambda: rest.fapi_exchange_info()),
    ("fapi_ticker24hr_all", "fapi", lambda: rest.fapi_tickers()),
    ("fapi_premiumIndex_all", "fapi", lambda: rest.premium_index()),
    ("fapi_fundingInfo_all", "fapi", lambda: rest.funding_info()),
    ("fapi_depth_btcusdt", "fapi",
     lambda: rest.depth("BTCUSDT", 20)),
    ("fapi_klines_btcusdt_1h", "fapi",
     lambda: rest.klines("BTCUSDT", "1h", 24)),
    ("fapi_markPriceKlines_btcusdt_1h", "fapi",
     lambda: rest.klines("BTCUSDT", "1h", 24, price_type="mark")),
    ("fapi_indexPriceKlines_btcusdt_1h", "fapi",
     lambda: rest.klines("BTCUSDT", "1h", 24, price_type="index")),
    ("fapi_trades_btcusdt", "fapi",
     lambda: rest.trades("BTCUSDT", 20)),
    ("fapi_openInterest_btcusdt", "fapi",
     lambda: rest.open_interest("BTCUSDT")),
    ("sapi_exchangeInfo", "sapi", lambda: rest.sapi_exchange_info()),
    ("sapi_ticker24hr_all", "sapi", lambda: _spot_ticker_sample()),
    ("sapi_klines_btcusdt", "sapi",
     lambda: rest.klines("BTCUSDT", "1h", 24, market="spot")),
    ("sapi_depth_btcusdt", "sapi",
     lambda: rest.depth("BTCUSDT", 20, venue="spot")),
    ("solana_vault_signatures", "solana",
     lambda: chain.solana_vault_signatures(20)),
]


def _spot_ticker_sample() -> list:
    """sapi ticker/24hr ALL sampled for the fixture: every listed row
    plus the first 200 ephemeral options rows (the raw live list is
    ~10 MB of BTC_UP_DOWN_5M_* noise). smoke_live.py does the same."""
    tickers = rest.sapi_tickers()
    listed = {s.get("symbol") for s in
              (rest.sapi_exchange_info() or {}).get("symbols") or []}
    keep = [t for t in tickers if isinstance(t, dict)
            and t.get("symbol") in listed]
    ephem = [t for t in tickers if isinstance(t, dict)
             and t.get("symbol") not in listed]
    return keep + ephem[:200]


def record(out_dir: Path, only: list[str] | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, surface, fn in JOBS:
        if only and name not in only:
            continue
        try:
            payload = fn()
        except Exception as e:  # degrade honestly, keep recording
            print(f"[skip] {name}: {e}")
            continue
        (out_dir / f"{name}.json").write_text(json.dumps(payload,
                                                         indent=1))
        size = (out_dir / f"{name}.json").stat().st_size
        print(f"[ok]   {name}.json ({size} B)")
        written += 1
        time.sleep(0.3)  # be gentle between surfaces
    return written


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Record live Aster responses to tests/fixtures/")
    ap.add_argument("--out", default="tests/fixtures",
                    help="output directory (default tests/fixtures)")
    ap.add_argument("--only", default=None,
                    help="comma-separated fixture names (default: all)")
    a = ap.parse_args()
    only = [s.strip() for s in a.only.split(",")] if a.only else None
    out = Path(a.out)
    n = record(out, only)
    print(f"\n{n} fixture(s) written to {out}/")
    if n == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
