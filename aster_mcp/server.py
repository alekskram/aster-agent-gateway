"""aster-agent-gateway - MCP server (Aster DEX public data).

Read-only, keyless tools over the fapi/sapi REST surface (rest.py) and
the on-chain surfaces (chain.py: tapi / Solana / EVM vaults). Exactly
13 tools, names verbatim per spec:

  market_overview, exchange_symbols, order_book, klines, trades,
  spot_overview, funding_overview, tradfi_markets, funding_screener,
  oi_snapshot, deposit_flows, account_view, mark_index_divergence

Style follows the proven gateway pattern: plain functions registered
via build_server() -> FastMCP, all annotated read-only. All upstream
access goes through the `rest` / `chain` module namespaces so tests
monkeypatch them - never `from .rest import x`.

Honesty rules:
  * EVERY numeric from the API is a STRING ("0.0001") - _f() parses,
    never raises, None on non-numeric.
  * Every upstream failure returns an error dict
    {"error", "source", "reason"}, never a traceback.
  * Cached responses carry age_seconds / fetched_at.
  * Degradation paths are first-class: privacy-empty tapi, unconfigured
    EVM RPC, 404 OI history, unknown symbols, dead spot /trades.
"""
from __future__ import annotations

import re
import time

from . import chain
from . import rest

DEFAULT_PORT = 8904
_VERSION = "0.1.1"

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
              "8h", "12h", "1d", "3d", "1w", "1M")

# ---------------------------------------------------------------------------
# TradFi asset-class map (local; per spec D3): built from the futures
# universe observed live 2026-09. EXACT matches avoid false positives
# (crypto bases like SPACE/SPK/CLO/MUBARAK must NOT classify as TradFi
# even though they share prefixes with SPX/CL/MU); only unambiguous
# metal prefixes use prefix matching.
_TRADFI_EXACT: dict[str, str] = {
    # metals
    "GOLD": "metals", "SILVER": "metals",
    # equity index
    "SPCX": "equity-index", "SPX": "equity-index", "SPY": "equity-index",
    "NDX": "equity-index", "NAS": "equity-index", "ND": "equity-index",
    "DXY": "equity-index", "US30": "equity-index",
    "US30K": "equity-index", "JP225": "equity-index",
    # single equities (perp'd stocks)
    "MU": "equity-single", "SNDK": "equity-single",
    "NVDA": "equity-single", "TSLA": "equity-single",
    "COIN": "equity-single", "MSTR": "equity-single",
    "AAPL": "equity-single", "AMZN": "equity-single",
    "GOOGL": "equity-single", "GOOG": "equity-single",
    "META": "equity-single", "MSFT": "equity-single",
    "HOOD": "equity-single", "PLTR": "equity-single",
    "CRCL": "equity-single", "OPEN": "equity-single",
    "SPOT": "equity-single", "NBIS": "equity-single",
    "BABA": "equity-single",
    # energy
    "CL": "energy", "NG": "energy", "OIL": "energy", "WTI": "energy",
    "BRENT": "energy", "RB": "energy", "HO": "energy",
    # treasuries / rates
    "ZN": "treasuries", "ZB": "treasuries", "ZF": "treasuries",
    "UB": "treasuries", "TN": "treasuries", "ZBT": "treasuries",
    "US10Y": "treasuries", "US02Y": "treasuries", "US30Y": "treasuries",
    # forex majors
    "EUR": "forex", "GBP": "forex", "JPY": "forex", "AUD": "forex",
    "CAD": "forex", "CHF": "forex", "NZD": "forex",
}
# unambiguous metal prefixes (no crypto base starts with these)
_TRADFI_PREFIXES: list[tuple[str, str]] = [
    ("XAU", "metals"), ("XAG", "metals"), ("XPT", "metals"),
    ("XPD", "metals"),
]


def _tradfi_class(base: str) -> str | None:
    """Map a base asset to a TradFi class: exact match first, then the
    unambiguous metal prefixes. Crypto lookalikes (SPACE, SPK, CLO,
    MUBARAK) return None."""
    b = (base or "").upper()
    if b in _TRADFI_EXACT:
        return _TRADFI_EXACT[b]
    for prefix, cls in _TRADFI_PREFIXES:
        if b.startswith(prefix):
            return cls
    return None


# ------------------------------------------------------------------ helpers


def _f(s) -> float | None:
    """float(s) for numeric strings ("0.0001", "" -> None); None on
    garbage. Every fapi/sapi numeric is a STRING; empty optionals must
    become None, never a crash."""
    if s is None:
        return None
    if isinstance(s, str) and not s.strip():
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _i(s) -> int | None:
    v = _f(s)
    return int(v) if v is not None else None


def _round(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(x, nd)


def _err(source: str, error: str, reason: str, **extra) -> dict:
    """Honest-degradation error dict (never a traceback)."""
    out = {"error": error, "source": source, "reason": reason,
           "detail": error}
    out.update(extra)
    return out


def _age(base_key: str, path: str, params: dict | None = None) -> dict:
    """Freshness block for a cached REST payload."""
    age = rest.cache_age(base_key, path, params)
    if age is None:
        return {"fetched_at": _now_iso()}
    return {"age_seconds": round(age, 1), "fetched_at": _now_iso()}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sym_info_map(exchange_info: dict) -> dict:
    """symbol -> raw symbol row."""
    return {(s or {}).get("symbol", ""): s for s in
            (exchange_info or {}).get("symbols") or []
            if isinstance(s, dict)}


def _funding_bounds(f: dict | None) -> tuple[float | None, float | None]:
    """(cap, floor) from a fundingInfo row. The live schema names the
    fields fundingFeeCap/fundingFeeFloor (JSON numbers); older
    captures used string cap/floor - both are accepted, None when
    absent."""
    if not isinstance(f, dict):
        return None, None
    cap = _f(f.get("fundingFeeCap"))
    floor = _f(f.get("fundingFeeFloor"))
    if cap is None:
        cap = _f(f.get("cap"))
    if floor is None:
        floor = _f(f.get("floor"))
    return cap, floor


def _split_symbol(symbol: str) -> tuple[str, str]:
    """'BTCUSDT' -> ('BTC', 'USDT') when the quote is a known stable."""
    s = (symbol or "").upper().replace("/", "")
    for q in ("USD1", "USDT", "USD", "USDC"):
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)], q
    return s, ""


# ------------------------------------------------------------------- tools


def market_overview(limit: int = 20, sort: str = "volume") -> dict:
    """Futures market overview from ONE ticker/24hr ALL call joined
    with exchangeInfo: panel of TRADING markets (symbol, last, 24h
    change %, quote volume, trades count), top volumes, status counts,
    and fresh listings (newest onboardDate, N rows).
    `sort` in {volume (default), change, listings}. `limit` max 100.
    Example: market_overview(limit=10)
    """
    key = (sort or "volume").strip().lower()
    n = max(1, min(100, int(limit)))
    try:
        tickers = rest.fapi_tickers()
        info = rest.fapi_exchange_info()
    except rest.UpstreamError as e:
        return _err("fapi", f"market overview unavailable: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(tickers, list) or not tickers:
        return _err("fapi", "ticker/24hr returned no rows",
                    "empty payload")
    smap = _sym_info_map(info)
    statuses: dict[str, int] = {}
    quotes: dict[str, int] = {}
    rows: list[dict] = []
    for t in tickers:
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol", "")
        srow = smap.get(sym, {})
        status = srow.get("status") or "UNKNOWN"
        statuses[status] = statuses.get(status, 0) + 1
        base, quote = _split_symbol(sym)
        if quote:
            quotes[quote] = quotes.get(quote, 0) + 1
        if status != "TRADING":
            continue
        rows.append({
            "symbol": sym,
            "last_price": _f(t.get("lastPrice")),
            "change_pct": _round(_f(t.get("priceChangePercent")), 3),
            "quote_volume": _round(_f(t.get("quoteVolume")), 2),
            "volume": _round(_f(t.get("volume")), 3),
            "high": _f(t.get("highPrice")),
            "low": _f(t.get("lowPrice")),
            "weighted_avg": _f(t.get("weightedAvgPrice")),
            "trades": _i(t.get("count")),
            "onboard_date": _i(srow.get("onboardDate")),
        })
    if key == "change":
        rows.sort(key=lambda r: abs(r.get("change_pct") or 0.0), reverse=True)
    elif key == "listings":
        rows.sort(key=lambda r: r.get("onboard_date") or 0, reverse=True)
    else:
        rows.sort(key=lambda r: r.get("quote_volume") or 0.0, reverse=True)
    # fresh listings: newest onboardDate among TRADING symbols
    fresh = sorted(rows, key=lambda r: r.get("onboard_date") or 0,
                   reverse=True)[:5]
    return {
        "count": len(rows),
        "returned": min(n, len(rows)),
        "sort": key,
        "markets": rows[:n],
        "fresh_listings": [{"symbol": r["symbol"],
                            "onboard_date": r["onboard_date"]}
                           for r in fresh if r.get("onboard_date")],
        "status_counts": statuses,
        "quote_counts": quotes,
        **_age("fapi", "/ticker/24hr"),
        "note": "panel covers TRADING futures symbols only (SETTLING/"
                "PENDING counted in status_counts); volumes are 24h "
                "quote volume in USDT-scale units.",
    }


def exchange_symbols(venue: str = "futures", symbol: str | None = None,
                     include_junk: bool = False) -> dict:
    """Symbol universe with filters/precisions/statuses incl. the micro
    view (MIN_NOTIONAL, stepSize, leverageFilter kind). Junk filter:
    TEST* spot names and SETTLING futures statuses are hidden unless
    include_junk=True. venue in {futures, spot, both}. Single-symbol
    detail when `symbol` given.
    Example: exchange_symbols(venue="futures", symbol="BTCUSDT")
    """
    v = (venue or "futures").strip().lower()
    if v not in ("futures", "spot", "both"):
        raise ValueError(f"unknown venue {venue!r}: use futures, spot or both")
    out: dict = {"venue": v, "include_junk": bool(include_junk)}
    errors: list[dict] = []
    venues = ["futures", "spot"] if v == "both" else [v]
    for venue_key in venues:
        try:
            info = (rest.fapi_exchange_info() if venue_key == "futures"
                    else rest.sapi_exchange_info())
        except rest.UpstreamError as e:
            errors.append(_err(venue_key, f"exchangeInfo unavailable: {e}",
                               f"upstream failure ({e.kind})"))
            continue
        want = rest._norm_symbol(symbol) if symbol else None
        rows: list[dict] = []
        junk: list[dict] = []
        for s in (info or {}).get("symbols") or []:
            if not isinstance(s, dict):
                continue
            sym = s.get("symbol", "")
            if want and sym != want:
                continue
            status = s.get("status") or ""
            # junk: TEST* spot names, plus non-TRADING futures
            # statuses (SETTLING, PENDING, PENDING_TRADING, BREAK) -
            # they are not tradeable panels
            is_junk = sym.startswith("TEST") or (
                venue_key == "futures" and status != "TRADING")
            micro = _symbol_micro(s)
            row = {
                "symbol": sym,
                "status": status,
                "base": s.get("baseAsset"),
                "quote": s.get("quoteAsset"),
                "contract_type": s.get("contractType"),
                "onboard_date": _i(s.get("onboardDate")),
                **micro,
            }
            if is_junk and not include_junk:
                junk.append(row)
            else:
                rows.append(row)
        out[venue_key] = {
            "count": len(rows),
            "symbols": rows if not want else (rows[:1] if rows else []),
            "junk_filtered": len(junk),
        }
        if want and not rows and not junk:
            out[venue_key]["detail"] = (
                f"symbol {symbol!r} not found in {venue_key} exchangeInfo")
        out.setdefault("ages", {})[venue_key] = _age(
            venue_key, "/exchangeInfo")
    if errors:
        out["warnings"] = errors
    return out


def _symbol_micro(s: dict) -> dict:
    """Micro filter view: MIN_NOTIONAL, tickSize, stepSize,
    leverageFilter kind - never crashing on absent filters."""
    filters = {f.get("filterType"): f for f in s.get("filters") or []
               if isinstance(f, dict)}
    price_f = filters.get("PRICE_FILTER") or {}
    lot_f = filters.get("LOT_SIZE") or {}
    notional_f = (filters.get("MIN_NOTIONAL")
                  or filters.get("NOTIONAL") or {})
    lev_f = filters.get("LEVERAGE_FILTER") or {}
    return {
        "tick_size": _f(price_f.get("tickSize")),
        "step_size": _f(lot_f.get("stepSize")),
        "min_notional": _f(notional_f.get("notional")
                           if notional_f.get("notional") is not None
                           else notional_f.get("minNotional")),
        "max_leverage": _f(lev_f.get("maxLeverage")),
        "leverage_filter_kind": (lev_f.get("filterType")
                                 or None),
    }


def order_book(symbol: str, venue: str = "futures", depth: int = 10) -> dict:
    """Aggregated order book: fapi /depth (futures) or sapi /api/v3/
    depth (spot). The requested depth is snapped to the nearest priced
    tier (weight-aware: 5/10/20/50=2, 100=5, 500=10, 1000=20 weight).
    Example: order_book(symbol="BTCUSDT", depth=20)
    """
    raw = (symbol or "").strip()
    if not raw:
        raise ValueError("symbol required")
    v = (venue or "futures").strip().lower()
    if v not in ("futures", "spot"):
        raise ValueError(f"unknown venue {venue!r}: use futures or spot")
    n = max(1, min(1000, int(depth)))
    try:
        book = rest.depth(raw, n, venue=v)
    except rest.UpstreamError as e:
        return _err("fapi" if v == "futures" else "sapi",
                    f"order book unavailable for {symbol!r}: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(book, dict):
        return _err(v, "depth returned a malformed payload",
                    f"expected dict, got {type(book).__name__}")
    snapped = min((c for c in (5, 10, 20, 50, 100, 500, 1000) if n <= c),
                  default=1000)
    return {
        "symbol": rest._norm_symbol(raw),
        "venue": v,
        "depth_requested": n,
        "limit_used": snapped,
        "weight": rest.depth_weight(snapped),
        "bids": [[_f(p), _f(q)] for p, q in (book.get("bids") or [])[:snapped]],
        "asks": [[_f(p), _f(q)] for p, q in (book.get("asks") or [])[:snapped]],
        **_age(v, "/depth", {"symbol": rest._norm_symbol(raw),
                             "limit": snapped}),
        "note": "limit snapped to the nearest priced tier (weight-aware); "
                "rows are [price, qty] with numerics parsed from strings.",
    }


def klines(symbol: str, interval: str = "1h", limit: int = 100,
           market: str = "futures", price_type: str = "last") -> dict:
    """Klines (OHLCV). price_type last|mark|index selects fapi
    klines/markPriceKlines/indexPriceKlines; spot goes to sapi
    /api/v3/klines (price_type ignored). intervals 1m..1M; limit
    snapped to a priced tier (max 1000).
    Example: klines(symbol="BTCUSDT", interval="4h", limit=50)
    """
    raw = (symbol or "").strip()
    if not raw:
        raise ValueError("symbol required")
    iv = (interval or "").strip()
    if iv not in _INTERVALS:
        raise ValueError(f"unknown interval {interval!r}: use one of "
                         f"{', '.join(_INTERVALS)}")
    mk = (market or "futures").strip().lower()
    if mk not in ("futures", "spot"):
        raise ValueError(f"unknown market {market!r}: use futures or spot")
    pt = (price_type or "last").strip().lower()
    if mk == "futures" and pt not in ("last", "mark", "index"):
        raise ValueError(f"unknown price_type {price_type!r}: use last, "
                         f"mark or index")
    n = max(1, min(1000, int(limit)))
    try:
        rows_raw = rest.klines(raw, iv, n, market=mk, price_type=pt)
    except rest.UpstreamError as e:
        return _err("fapi" if mk == "futures" else "sapi",
                    f"klines unavailable for {symbol!r}: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(rows_raw, list):
        return _err(mk, "klines returned a malformed payload",
                    f"expected list, got {type(rows_raw).__name__}")
    rows = [{
        "open_time": _i(k[0]) if len(k) > 0 else None,
        "open": _f(k[1]) if len(k) > 1 else None,
        "high": _f(k[2]) if len(k) > 2 else None,
        "low": _f(k[3]) if len(k) > 3 else None,
        "close": _f(k[4]) if len(k) > 4 else None,
        "volume": _f(k[5]) if len(k) > 5 else None,
        "close_time": _i(k[6]) if len(k) > 6 else None,
        "trades": _i(k[8]) if len(k) > 8 else None,
    } for k in rows_raw if isinstance(k, (list, tuple))]
    path = {"last": "/klines", "mark": "/markPriceKlines",
            "index": "/indexPriceKlines"}.get(pt, "/klines") \
        if mk == "futures" else "/klines"
    return {
        "symbol": rest._norm_symbol(raw),
        "market": mk,
        "price_type": pt if mk == "futures" else None,
        "interval": iv,
        "count": len(rows),
        "klines": rows,
        **_age("fapi" if mk == "futures" else "sapi", path,
               {"symbol": rest._norm_symbol(raw), "interval": iv}),
        "note": "rows oldest-first as served; fapi/sapi numerics arrive "
                "as strings and are parsed (null = not available).",
    }


def trades(symbol: str, limit: int = 20, venue: str = "futures") -> dict:
    """Recent trades. Futures fapi /trades is keyless and fresh. The
    spot /trades path was uncertain at recon time - probed live in
    smoke; if dead the tool returns an honest error dict.
    Example: trades(symbol="BTCUSDT", limit=10)
    """
    raw = (symbol or "").strip()
    if not raw:
        raise ValueError("symbol required")
    v = (venue or "futures").strip().lower()
    if v not in ("futures", "spot"):
        raise ValueError(f"unknown venue {venue!r}: use futures or spot")
    n = max(1, min(1000, int(limit)))
    try:
        raws = rest.trades(raw, n, venue=v)
    except rest.UpstreamError as e:
        return _err("fapi" if v == "futures" else "sapi",
                    f"trades unavailable for {symbol!r}: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(raws, list):
        return _err(v, "trades returned a malformed payload",
                    f"expected list, got {type(raws).__name__}")
    rows = [{
        "id": _i(t.get("id")),
        "price": _f(t.get("price")),
        "qty": _f(t.get("qty")),
        "quote_qty": _f(t.get("quoteQty")),
        "time": _i(t.get("time")),
        "is_buyer_maker": bool(t.get("isBuyerMaker")),
    } for t in raws if isinstance(t, dict)]
    return {
        "symbol": rest._norm_symbol(raw),
        "venue": v,
        "count": len(rows),
        "trades": rows,
        **_age(v, "/trades", {"symbol": rest._norm_symbol(raw),
                              "limit": n}),
    }


def spot_overview(limit: int = 20) -> dict:
    """Spot market overview: sapi ticker/24hr ALL joined with
    exchangeInfo. The spot ticker list carries ~24k ephemeral
    BTC_UP_DOWN_5M_* options rows that never appear in exchangeInfo;
    the join keeps only listed symbols with status TRADING (~68
    pairs). TEST* junk filtered by name.
    Example: spot_overview(limit=10)
    """
    n = max(1, min(100, int(limit)))
    try:
        tickers = rest.sapi_tickers()
        info = rest.sapi_exchange_info()
    except rest.UpstreamError as e:
        return _err("sapi", f"spot overview unavailable: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(tickers, list):
        return _err("sapi", "ticker/24hr returned a malformed payload",
                    f"expected list, got {type(tickers).__name__}")
    smap = _sym_info_map(info)
    rows: list[dict] = []
    junk = 0
    unlisted = 0
    for t in tickers:
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol", "")
        if sym.startswith("TEST"):
            junk += 1
            continue
        srow = smap.get(sym)
        if srow is None or srow.get("status") != "TRADING":
            # ephemeral options rows (BTC_UP_DOWN_5M_*) are not in
            # exchangeInfo at all; BREAK/SETTLING listings are not a
            # tradeable spot panel either
            unlisted += 1
            continue
        rows.append({
            "symbol": sym,
            "last_price": _f(t.get("lastPrice")),
            "change_pct": _round(_f(t.get("priceChangePercent")), 3),
            "quote_volume": _round(_f(t.get("quoteVolume")), 2),
            "status": srow.get("status") or None,
            "base": srow.get("baseAsset"),
            "quote": srow.get("quoteAsset"),
        })
    rows.sort(key=lambda r: r.get("quote_volume") or 0.0, reverse=True)
    return {
        "count": len(rows),
        "returned": min(n, len(rows)),
        "junk_filtered": junk,
        "unlisted_or_not_trading_filtered": unlisted,
        "pairs": rows[:n],
        **_age("sapi", "/ticker/24hr"),
        "note": "ticker rows are inner-joined with sapi exchangeInfo "
                "(status TRADING only); TEST*-named junk and ~24k "
                "ephemeral BTC_UP_DOWN_5M_* options rows are filtered "
                "out; status/base/quote come from exchangeInfo.",
    }


def funding_overview(limit: int = 20, sort: str = "rate") -> dict:
    """Funding panel from premiumIndex ALL + fundingInfo: all ~730
    rates with intervals (1/2/4/8h mixed - flagged honestly), cap/
    floor, interestRate, nextFundingTime. `sort` in {rate (default),
    interval}. Example: funding_overview(limit=10)
    """
    key = (sort or "rate").strip().lower()
    n = max(1, min(100, int(limit)))
    try:
        premium = rest.premium_index()
        finfo = rest.funding_info()
    except rest.UpstreamError as e:
        return _err("fapi", f"funding overview unavailable: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(premium, list):
        return _err("fapi", "premiumIndex returned a malformed payload",
                    f"expected list, got {type(premium).__name__}")
    fmap = {f.get("symbol"): f for f in finfo or []
            if isinstance(f, dict)}
    rows: list[dict] = []
    intervals: dict[str, int] = {}
    for p in premium:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol", "")
        f = fmap.get(sym, {})
        hours = _i(f.get("fundingIntervalHours"))
        intervals[str(hours)] = intervals.get(str(hours), 0) + 1
        rate = _f(p.get("lastFundingRate"))
        cap, floor = _funding_bounds(f)
        rows.append({
            "symbol": sym,
            "last_funding_rate": _round(rate, 8),
            "annualized_pct": _round(rate * 24 / (hours or 8) * 365 * 100,
                                     2) if rate is not None else None,
            "interval_hours": hours,
            "cap": cap,
            "floor": floor,
            "interest_rate": _f(f.get("interestRate")),
            "mark_price": _f(p.get("markPrice")),
            "index_price": _f(p.get("indexPrice")),
            "next_funding_time": _i(p.get("nextFundingTime")),
        })
    if key == "interval":
        rows.sort(key=lambda r: (r.get("interval_hours") or 0,
                                 abs(r.get("last_funding_rate") or 0.0)),
                  reverse=True)
    else:
        rows.sort(key=lambda r: abs(r.get("last_funding_rate") or 0.0),
                  reverse=True)
    return {
        "count": len(rows),
        "returned": min(n, len(rows)),
        "sort": key,
        "funding": rows[:n],
        "interval_histogram": {str(k): v for k, v in
                               sorted(intervals.items(),
                                      key=lambda kv: (kv[0] is None,
                                                      kv[0] or 0))},
        **_age("fapi", "/premiumIndex"),
        "note": "intervals are MIXED across markets (1/2/4/8h - see "
                "interval_histogram); annualized_pct assumes the "
                "market's own interval; premiumInterval rows missing "
                "from fundingInfo carry null interval fields.",
    }


def tradfi_markets(limit: int = 20, window: str | None = None) -> dict:
    """TradFi-perp screener (24/7 markets): local asset-class map by
    baseAsset (metals, equity-index, equity-single, energy, treasuries,
    forex) with per-class volumes/prices/funding, plus the D3 sub-block
    tradfi_crypto_corr: rolling correlation of a TradFi representative
    vs BTC computed locally from klines (at most 2 klines calls, cached
    when possible; honest note when data is insufficient).
    `window` in {24h, 7d, 30d} selects the correlation window.
    Example: tradfi_markets(limit=10, window="7d")
    """
    n = max(1, min(100, int(limit)))
    try:
        tickers = rest.fapi_tickers()
        premium = rest.premium_index()
    except rest.UpstreamError as e:
        return _err("fapi", f"tradfi screener unavailable: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(tickers, list):
        return _err("fapi", "ticker/24hr returned a malformed payload",
                    f"expected list, got {type(tickers).__name__}")
    pmap = {p.get("symbol"): p for p in premium or []
            if isinstance(p, dict)}
    rows: list[dict] = []
    class_agg: dict[str, dict] = {}
    for t in tickers:
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol", "")
        base, quote = _split_symbol(sym)
        cls = _tradfi_class(base)
        if cls is None:
            continue
        p = pmap.get(sym, {})
        vol = _f(t.get("quoteVolume")) or 0.0
        row = {
            "symbol": sym,
            "asset_class": cls,
            "last_price": _f(t.get("lastPrice")),
            "change_pct": _round(_f(t.get("priceChangePercent")), 3),
            "quote_volume": _round(vol, 2),
            "funding_rate": _round(_f(p.get("lastFundingRate")), 8),
        }
        rows.append(row)
        agg = class_agg.setdefault(cls, {"markets": 0, "quote_volume": 0.0})
        agg["markets"] += 1
        agg["quote_volume"] += vol
    rows.sort(key=lambda r: r.get("quote_volume") or 0.0, reverse=True)
    for agg in class_agg.values():
        agg["quote_volume"] = _round(agg["quote_volume"], 2)
    # D3 sub-block: local TradFi<->BTC correlation from klines
    corr = _tradfi_crypto_corr(rows, window)
    return {
        "count": len(rows),
        "returned": min(n, len(rows)),
        "tradfi": rows[:n],
        "by_class": {k: v for k, v in sorted(
            class_agg.items(), key=lambda kv: -kv[1]["quote_volume"])},
        "tradfi_crypto_corr": corr,
        **_age("fapi", "/ticker/24hr"),
        "note": "asset classes are mapped locally by baseAsset prefix "
                "(no venue-provided class field); corr window uses "
                "hourly closes, Pearson on log returns.",
    }


def _tradfi_crypto_corr(rows: list[dict], window: str | None) -> dict:
    """D3: rolling correlation TradFi-representative vs BTC, computed
    LOCALLY from at most 2 klines calls (or cached ones). Honest note
    when either series is too short."""
    hours = {"24h": 24, "7d": 168, "30d": 720}.get(
        (window or "7d").strip().lower(), 168)
    limit = min(1000, hours)
    rep = None
    for r in rows:
        if r.get("asset_class") in ("metals", "equity-index") \
                and (r.get("quote_volume") or 0) > 0:
            rep = r["symbol"]
            break
    if rep is None:
        return {"error": "no tradfi representative market found",
                "source": "local", "reason": "empty tradfi panel"}
    series: dict[str, list[float] | None] = {}
    for sym in (rep, "BTCUSDT"):
        try:
            ks = rest.klines(sym, "1h", limit)
        except rest.UpstreamError:
            series[sym] = None
            continue
        closes: list[float] = []
        for k in ks:
            if isinstance(k, (list, tuple)) and len(k) > 4:
                v = _f(k[4])
                if v is not None:
                    closes.append(v)
        series[sym] = closes or None
    a, b = series.get(rep), series.get("BTCUSDT")
    if not a or not b:
        return {"error": "insufficient kline data for correlation",
                "source": "fapi", "reason":
                f"{rep if not a else 'BTCUSDT'} klines unavailable",
                "representative": rep}
    m = min(len(a), len(b))
    a, b = a[-m:], b[-m:]
    corr = _pearson_log(a, b)
    return {
        "representative": rep,
        "window": window or "7d",
        "bars": m,
        "corr": _round(corr, 4),
        "method": "pearson on hourly log returns",
        "note": "computed locally from 2 klines calls (cached); "
                "short windows are noisy - treat |corr|<0.3 as noise.",
    } if corr is not None else {
        "error": "correlation undefined (degenerate series)",
        "source": "local", "reason": "zero variance in closes",
        "representative": rep,
    }


def _pearson_log(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation of log returns; None when degenerate."""
    if len(a) != len(b) or len(a) < 3:
        return None
    import math
    ra = [math.log(a[i] / a[i - 1]) for i in range(1, len(a))
          if a[i - 1] > 0 and a[i] > 0]
    rb = [math.log(b[i] / b[i - 1]) for i in range(1, len(b))
          if b[i - 1] > 0 and b[i] > 0]
    m = min(len(ra), len(rb))
    if m < 3:
        return None
    ra, rb = ra[-m:], rb[-m:]
    ma = sum(ra) / m
    mb = sum(rb) / m
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va <= 0 or vb <= 0:
        return None
    return cov / (va ** 0.5 * vb ** 0.5)


def funding_screener(top: int = 10, direction: str = "both") -> dict:
    """One-call ranking of ALL markets by annualized funding rate, by
    premium, by mark-index spread; plus the D1 sub-block
    funding_regime: distance to cap/floor (headroom bps) per market
    from fundingInfo. `direction` in {both (default), long, short} -
    long = positive rates (longs pay), short = negative.
    Example: funding_screener(top=5)
    """
    d = (direction or "both").strip().lower()
    if d not in ("both", "long", "short"):
        raise ValueError(f"unknown direction {direction!r}: use both, "
                         f"long or short")
    n = max(1, min(50, int(top)))
    try:
        premium = rest.premium_index()
        finfo = rest.funding_info()
    except rest.UpstreamError as e:
        return _err("fapi", f"funding screener unavailable: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(premium, list):
        return _err("fapi", "premiumIndex returned a malformed payload",
                    f"expected list, got {type(premium).__name__}")
    fmap = {f.get("symbol"): f for f in finfo or []
            if isinstance(f, dict)}
    rows: list[dict] = []
    for p in premium:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol", "")
        f = fmap.get(sym, {})
        rate = _f(p.get("lastFundingRate"))
        mark = _f(p.get("markPrice"))
        index = _f(p.get("indexPrice"))
        hours = _i(f.get("fundingIntervalHours")) or 8
        ann = rate * 24 / hours * 365 * 100 if rate is not None else None
        spread_bps = ((mark - index) / index * 10000
                      if mark is not None and index else None)
        # D1 funding_regime: headroom to cap/floor in bps
        cap, floor = _funding_bounds(f)
        headroom = None
        regime = None
        if rate is not None:
            if rate >= 0 and cap is not None:
                headroom = (cap - rate) * 10000
                regime = "near-cap" if headroom < 5 else "normal"
            elif rate < 0 and floor is not None:
                headroom = (rate - floor) * 10000
                regime = "near-floor" if headroom < 5 else "normal"
        rows.append({
            "symbol": sym,
            "funding_rate": _round(rate, 8),
            "annualized_pct": _round(ann, 2),
            "mark_index_spread_bps": _round(spread_bps, 2),
            "funding_regime": {"interval_hours": hours,
                               "cap": cap, "floor": floor,
                               "headroom_bps": _round(headroom, 2),
                               "regime": regime},
        })
    def _dir_ok(r: dict) -> bool:
        rate = r.get("funding_rate") or 0.0
        if d == "long":
            return rate > 0
        if d == "short":
            return rate < 0
        return True
    by_rate = sorted([r for r in rows if _dir_ok(r)],
                     key=lambda r: abs(r.get("annualized_pct") or 0.0),
                     reverse=True)[:n]
    by_premium = sorted([r for r in rows if _dir_ok(r)],
                        key=lambda r: abs(r.get("mark_index_spread_bps")
                                          or 0.0), reverse=True)[:n]
    return {
        "direction": d,
        "ranked_total": len(rows),
        "top_by_annualized_rate": by_rate,
        "top_by_mark_index_spread": by_premium,
        **_age("fapi", "/premiumIndex"),
        "note": "single premiumIndex call ranks all markets; "
                "annualized = rate * 24/interval * 365; headroom_bps "
                "is distance to the market's own cap/floor.",
    }


def oi_snapshot(symbols: str | list[str] | None = None,
                top: int = 10) -> dict:
    """Open interest snapshot. openInterest is a per-symbol endpoint
    ONLY (each symbol = 1 call); N is capped at 10 per invocation.
    There is NO keyless OI history (/futures/data/openInterestHist
    404s) - stated honestly, never invented. When `symbols` is None
    the top-volume futures symbols are used.
    Example: oi_snapshot(symbols=["BTCUSDT", "ETHUSDT"])
    """
    n = max(1, min(10, int(top)))
    if symbols is None:
        try:
            tickers = rest.fapi_tickers()
        except rest.UpstreamError as e:
            return _err("fapi", f"cannot pick top symbols: {e}",
                        f"upstream failure ({e.kind})")
        ranked = sorted((t for t in tickers if isinstance(t, dict)),
                        key=lambda t: _f(t.get("quoteVolume")) or 0.0,
                        reverse=True)
        syms = [t.get("symbol") for t in ranked[:n] if t.get("symbol")]
    else:
        if isinstance(symbols, str):
            symbols = [symbols]
        syms = [rest._norm_symbol(s) for s in symbols[:n] if s]
    rows: list[dict] = []
    errors: list[dict] = []
    for sym in syms[:10]:
        if not sym:
            continue
        try:
            oi = rest.open_interest(sym)
        except rest.UpstreamError as e:
            errors.append(_err("fapi", f"openInterest failed for "
                                      f"{sym}: {e}",
                               f"upstream failure ({e.kind})"))
            continue
        if isinstance(oi, dict):
            rows.append({"symbol": sym,
                         "open_interest": _f(oi.get("openInterest")),
                         **_age("fapi", "/openInterest",
                                {"symbol": sym})})
        else:
            errors.append(_err("fapi", f"openInterest malformed for "
                                      f"{sym}",
                               f"expected dict, got "
                               f"{type(oi).__name__}"))
    out = {
        "count": len(rows),
        "open_interest": rows,
        "note": "openInterest is per-symbol only (1 call each, max 10 "
                "per invocation); NO keyless OI history exists - the "
                "/futures/data/openInterestHist endpoint returns 404 "
                "on Aster, so historical OI is not available through "
                "this gateway.",
    }
    if errors:
        out["warnings"] = errors
    return out


def deposit_flows(chain_filter: str = "all", limit: int = 20) -> dict:
    """Vault deposit flows: Solana getSignaturesForAddress on the
    Aster vault program (keyless) + EVM eth_getLogs Transfer-to-vault
    on BSC/ETH/ARB when ASTER_EVM_RPC_URL* is configured; plus the D4
    sub-block deposit_stats: hourly buckets and chain breakdown of
    what was fetched. `chain_filter` in {all (default), solana, bsc,
    eth, arb}. Example: deposit_flows(limit=20)
    """
    # NOTE: `chain` param name shadows the module; use chain_filter.
    cf = (chain_filter or "all").strip().lower()
    if cf not in ("all", "solana", "bsc", "eth", "arb"):
        raise ValueError(f"unknown chain {chain_filter!r}: use all, "
                         f"solana, bsc, eth or arb")
    n = max(1, min(100, int(limit)))
    chains_out: dict[str, dict] = {}
    errors: list[dict] = []
    if cf in ("all", "solana"):
        try:
            sigs = chain.solana_vault_signatures(n)
        except chain.RpcError as e:
            chains_out["solana"] = _err(
                "solana", f"vault signatures unavailable: {e}",
                f"rpc failure ({e.kind})")
        else:
            rows = [{
                "signature": s.get("signature"),
                "slot": _i(s.get("slot")),
                "block_time": _i(s.get("blockTime")),
                "err": s.get("err"),
            } for s in sigs if isinstance(s, dict)]
            chains_out["solana"] = {
                "count": len(rows),
                "signatures": rows,
                **_stats_hourly(rows, "block_time"),
            }
    for evm_chain in ("bsc", "eth", "arb"):
        if cf not in ("all", evm_chain):
            continue
        try:
            latest = chain.evm_latest_block(evm_chain)
            logs = chain.evm_vault_transfers(evm_chain,
                                             latest - 300, latest)
        except chain.RpcError as e:
            chains_out[evm_chain] = _err(
                evm_chain, f"vault transfers unavailable: {e}",
                f"rpc failure ({e.kind})")
            continue
        rows = [{
            "tx_hash": lg.get("transactionHash"),
            "block": _i(int(lg.get("blockNumber", "0x0"), 16))
            if isinstance(lg.get("blockNumber"), str) else
            _i(lg.get("blockNumber")),
            "to": ("0x" + lg["topics"][2][-40:])
            if isinstance(lg.get("topics"), list) and len(lg["topics"]) > 2
            else None,
            "token": lg.get("address"),
        } for lg in logs if isinstance(lg, dict)]
        chains_out[evm_chain] = {
            "count": len(rows),
            "transfers": rows,
            "window_blocks": 300,
        }
    # D4 deposit_stats
    stats = _deposit_stats(chains_out)
    out = {"chains": chains_out, "deposit_stats": stats}
    if errors:
        out["warnings"] = errors
    return out


def _stats_hourly(rows: list[dict], time_key: str) -> dict:
    """Hourly buckets over row timestamps (D4 helper)."""
    buckets: dict[str, int] = {}
    for r in rows:
        ts = r.get(time_key)
        if ts:
            hour = time.strftime("%Y-%m-%dT%H:00:00Z",
                                 time.gmtime(ts))
            buckets[hour] = buckets.get(hour, 0) + 1
    return {"hourly": dict(sorted(buckets.items())[-24:])}


def _deposit_stats(chains_out: dict) -> dict:
    """D4: per-chain counts + hourly buckets where derivable."""
    per_chain: dict[str, int] = {}
    hourly: dict[str, int] = {}
    for name, blk in chains_out.items():
        if not isinstance(blk, dict) or blk.get("error"):
            per_chain[name] = 0
            continue
        per_chain[name] = blk.get("count", 0)
        for h, c in (blk.get("hourly") or {}).items():
            hourly[h] = hourly.get(h, 0) + c
    return {
        "per_chain": per_chain,
        "hourly": dict(sorted(hourly.items())[-24:]),
        "note": "EVM token-level breakdown is not derived: the vault "
                "is a router (many tokens); signature counts are a "
                "flow proxy, not USD volume.",
    }


def account_view(address: str, data: str = "balance") -> dict:
    """Keyless tapi account read for ANY address (Aster Chain
    JSON-RPC): aster_getBalance / aster_openOrders / aster_userFills
    (+ spot variants when they exist). Account privacy hides most
    accounts: a privacy-empty result returns an honest error dict
    explaining WHY (privacy, not an API failure).
    `data` in {balance (default), openOrders, userFills, spotBalance,
    spotOpenOrders, spotUserFills}.
    Example: account_view(address="0x...")
    """
    addr = (address or "").strip()
    if not addr:
        raise ValueError("address required")
    kind = (data or "balance").strip()
    method = chain.TAPI_METHODS.get(kind)
    if method is None:
        raise ValueError(f"unknown data {data!r}: use one of "
                         f"{', '.join(chain.TAPI_METHODS)}")
    if _ADDRESS_RE.match(addr):
        addr = addr.lower()
    elif not _SOLANA_RE.match(addr):
        raise ValueError(
            f"not an address: {address!r} - expected 0x + 40 hex or a "
            f"base58 Solana address")
    try:
        result = chain.tapi_call(method, addr)
    except chain.RpcError as e:
        # aster_openOrders/userFills on privacy-hidden accounts return
        # JSON-RPC -32603 "internal error" - that is the same privacy
        # wall, not an upstream outage; degrade to the privacy dict.
        if e.code == -32603:
            return _err(
                "tapi",
                f"{method} returned no data for this address",
                "account privacy: tapi hides orders/fills for private "
                "accounts (JSON-RPC -32603 upstream)",
                address=addr, method=method,
                account_privacy=None, raw_empty=True)
        return _err("tapi", f"{method} failed for {addr[:16]}...: {e}",
                    f"rpc failure ({e.kind})")
    # privacy-empty detection: tapi answers keyless but most accounts
    # are private -> result carries ONLY {address, accountPrivacy:
    # "enabled"} with no balances/orders/fills. That is NOT data;
    # degrade honestly. (Live-verified shape 2026-09-06.)
    def _has_data(r) -> bool:
        if not isinstance(r, dict):
            return bool(r)  # non-empty scalar/list = data
        for key in ("perpAssets", "balances", "positions", "orders",
                    "fills", "spotAssets"):
            if r.get(key):
                return True
        return False

    if not _has_data(result):
        privacy = result.get("accountPrivacy") \
            if isinstance(result, dict) else None
        return _err(
            "tapi",
            f"{method} returned no data for this address",
            "account privacy: most Aster accounts are private by "
            "default; balances/orders/fills are only visible when the "
            "owner disabled account privacy",
            address=addr, method=method,
            account_privacy=privacy,
            raw_empty=True)
    return {
        "address": addr,
        "method": method,
        "data": kind,
        "result": result,
        "note": "tapi answers keyless; this account exposes its data "
                "(account privacy off).",
    }


def mark_index_divergence(limit: int = 20) -> dict:
    """Mark/index divergence screener from ONE premiumIndex call
    (markPrice vs indexPrice spread), cross-checked with
    markPriceKlines vs klines for the widest divergences (up to 3
    symbol-pairs of klines, weight-economical).
    Example: mark_index_divergence(limit=10)
    """
    n = max(1, min(100, int(limit)))
    try:
        premium = rest.premium_index()
    except rest.UpstreamError as e:
        return _err("fapi", f"divergence screener unavailable: {e}",
                    f"upstream failure ({e.kind})")
    if not isinstance(premium, list):
        return _err("fapi", "premiumIndex returned a malformed payload",
                    f"expected list, got {type(premium).__name__}")
    rows: list[dict] = []
    for p in premium:
        if not isinstance(p, dict):
            continue
        mark = _f(p.get("markPrice"))
        index = _f(p.get("indexPrice"))
        if mark is None or index is None or index <= 0:
            continue
        bps = (mark - index) / index * 10000
        rows.append({
            "symbol": p.get("symbol"),
            "mark_price": mark,
            "index_price": index,
            "spread_bps": _round(bps, 2),
            "abs_spread_bps": _round(abs(bps), 2),
        })
    rows.sort(key=lambda r: r.get("abs_spread_bps") or 0.0, reverse=True)
    top = rows[:n]
    # cross-check the widest 3 with mark vs last klines (last 2 bars)
    cross: list[dict] = []
    for r in top[:3]:
        sym = r.get("symbol")
        if not sym:
            continue
        try:
            mark_k = rest.klines(sym, "1h", 5, price_type="mark")
            last_k = rest.klines(sym, "1h", 5, price_type="last")
        except rest.UpstreamError:
            continue
        mc = [_f(k[4]) for k in mark_k or [] if len(k) > 4]
        lc = [_f(k[4]) for k in last_k or [] if len(k) > 4]
        if mc and lc:
            m = min(len(mc), len(lc))
            diff_bps = [((a - b) / b * 10000) if a is not None
                        and b is not None and b > 0 else None
                        for a, b in zip(mc[-m:], lc[-m:])]
            cross.append({
                "symbol": sym,
                "mark_vs_last_bps": [_round(x, 2) for x in diff_bps[-2:]],
            })
    return {
        "count": len(rows),
        "returned": len(top),
        "divergence": top,
        "mark_vs_last_crosscheck": cross,
        **_age("fapi", "/premiumIndex"),
        "note": "spread_bps = (mark - index)/index in bps from ONE "
                "premiumIndex call; crosscheck compares markPriceKlines "
                "vs klines closes for the top 3 (2 klines calls each, "
                "cached).",
    }


# --------------------------------------------------------------- MCP wire


def build_server():
    """FastMCP instance with the 13 read-only tools wired."""
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    mcp = FastMCP(
        "aster-agent-gateway",
        version=_VERSION,
        instructions=(
            "Aster DEX public data, read-only and keyless: futures "
            "(~580 symbols) and spot (~68 pairs) market overviews, "
            "order books, klines (last/mark/index), trades, funding "
            "(mixed 1/2/4/8h intervals), TradFi-perp screener with "
            "BTC correlation, funding/mark-index screeners, open "
            "interest snapshots, vault deposit flows (Solana + EVM), "
            "and tapi account views for addresses with privacy off. "
            "Symbols look like 'BTCUSDT'. All API numerics are strings "
            "upstream and are parsed to floats or null - null always "
            "means 'not available', never zero. No OI history exists "
            "keyless (the endpoint 404s); EVM vault logs need "
            "ASTER_EVM_RPC_URL configured or they degrade honestly."),
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({
            "ok": True, "service": "aster-agent-gateway",
            "version": _VERSION})

    # read-only by design; destructiveHint explicit False (the default
    # true would mislabel this keyless analytics gateway)
    RO = {"readOnlyHint": True, "destructiveHint": False,
          "openWorldHint": True}
    for tool in (market_overview, exchange_symbols, order_book, klines,
                 trades, spot_overview, funding_overview, tradfi_markets,
                 funding_screener, oi_snapshot, deposit_flows,
                 account_view, mark_index_divergence):
        mcp.tool(tool, annotations=RO)
    return mcp


def main():
    """Console entry point (aster-agent-gateway). stdio by default for
    local agents; --http for the hosted form (default port 8904)."""
    import argparse
    ap = argparse.ArgumentParser(prog="aster-agent-gateway")
    ap.add_argument("--http", action="store_true",
                    help="streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    mcp = build_server()
    if a.http:
        mcp.run(transport="http", host=a.host, port=a.port, show_banner=False)
    else:
        mcp.run(show_banner=False)  # stdio: no banner noise in agent logs


if __name__ == "__main__":
    main()
