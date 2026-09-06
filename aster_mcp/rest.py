"""Stdlib-only REST client for Aster DEX public data (fapi + sapi).

Bases (live-verified 2026-09-05/06, see API_NOTES.md):
    futures  https://fapi.asterdex.com/fapi/v3/*
    spot     https://sapi.asterdex.com/api/v3/*   (old /sapi/v1/* 404s)

Keyless, read-only: no auth headers, no signing, no account mutations -
public market data only.

Rate model: weight-based per IP, tracked via the X-MBX-USED-WEIGHT-1M
response header. fapi allows 2400 weight/min, sapi 6000/min. The client
reads the header after every call, self-throttles when usage crosses
the soft threshold (80%), backs off on 429 respecting Retry-After, and
treats repeated 429s (HTTP 418 IP ban) as a hard cooldown. Over-budget
calls raise a clear UpstreamError; callers (MCP tools) degrade to an
honest error dict.

Weight pricing (Binance-compatible v3, enforced locally):
    ticker/24hr ALL ~40, exchangeInfo 1, premiumIndex ALL ~10 (per row
    1 capped), depth 5/10/20/50 = 2/2/2/2, 100 = 5, 500 = 10, 1000 = 20,
    klines 1 (limit<=100) .. 5 (limit 500..1000), trades 1 (limit<=100)
    .. 10 (limit 1000), openInterest 1, fundingInfo 1.

TTL caches: short for quotes/depth (5-15s), longer for exchangeInfo
(3600s), premiumIndex/fundingInfo (15-60s), klines (60s), trades (10s),
openInterest (30s).

Schema gotchas: ALL fapi/sapi numerics arrive as STRINGS ("0.0001");
ticker/24hr without symbol returns ALL rows in ONE call; openInterest
is per-symbol snapshot only (the /futures/data/openInterestHist history
endpoint 404s - degrade honestly, never invent history).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

FAPI_BASE = "https://fapi.asterdex.com/fapi/v3"
SAPI_BASE = "https://sapi.asterdex.com/api/v3"

UA = {"User-Agent": "aster-agent-gateway/0.1",
      "Accept": "application/json"}

# ---------------------------------------------------------------------------
# Weight buckets per base (per-IP limits from the Aster docs)

BUCKETS = {
    "fapi": {"limit": 2400, "used": 0, "header_ts": 0.0},
    "sapi": {"limit": 6000, "used": 0, "header_ts": 0.0},
}
_BUCKET_LOCK = threading.Lock()
_SOFT_THRESHOLD = 0.80          # self-throttle above 80% of the limit
_BAN_COOLDOWN = 60.0            # seconds to refuse calls after a 418

_THROTTLE_UNTIL: dict[str, float] = {"fapi": 0.0, "sapi": 0.0}
_LAST_429: dict[str, float] = {"fapi": 0.0, "sapi": 0.0}

_TIMEOUT = 20.0
_RETRIES = 3  # 1 try + 2 retries


class UpstreamError(Exception):
    """Aster fapi/sapi failure with taxonomy kind + HTTP status.

    kind: "rate-limit" (429; retried with Retry-After, still failing),
    "ip-ban" (418 - repeated 429s; cooldown), "http" (other 4xx/5xx),
    "network" (unreachable after retries).
    """

    def __init__(self, message: str, *, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


# ---------------------------------------------------------------------------
# Local weight pricing (Binance v3-compatible; used to admit/throttle)

_DEPTH_WEIGHT = [(5, 2), (10, 2), (20, 2), (50, 2), (100, 5),
                 (500, 10), (1000, 20)]
_KLINE_WEIGHT = [(100, 1), (499, 2), (500, 5), (1000, 5)]
_TRADES_WEIGHT = [(100, 1), (500, 2), (1000, 10)]


def depth_weight(limit: int) -> int:
    for cap, w in _DEPTH_WEIGHT:
        if limit <= cap:
            return w
    return 20


def kline_weight(limit: int) -> int:
    for cap, w in _KLINE_WEIGHT:
        if limit <= cap:
            return w
    return 5


def trades_weight(limit: int) -> int:
    for cap, w in _TRADES_WEIGHT:
        if limit <= cap:
            return w
    return 10


def endpoint_weight(path: str, params: dict) -> int:
    """Local estimate of one request's weight (admission control)."""
    p = path.rstrip("/")
    if p.endswith("/ticker/24hr"):
        return 40 if not params.get("symbol") else 1
    if p.endswith("/premiumIndex"):
        return 10 if not params.get("symbol") else 1
    if p.endswith("/depth"):
        return depth_weight(_int(params.get("limit"), 10))
    if p.endswith("/klines") or p.endswith("PriceKlines"):
        return kline_weight(_int(params.get("limit"), 100))
    if p.endswith("/trades"):
        return trades_weight(_int(params.get("limit"), 20))
    return 1  # exchangeInfo, fundingInfo, openInterest, ticker/* single


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Weight-header tracking + admission


def used_weight(base_key: str) -> int:
    """Last observed X-MBX-USED-WEIGHT-1M for a base (observability)."""
    with _BUCKET_LOCK:
        return BUCKETS[base_key]["used"]


def _record_header(base_key: str, resp) -> None:
    """Read X-MBX-USED-WEIGHT-1M off a response into the bucket."""
    raw = resp.headers.get("X-MBX-USED-WEIGHT-1M") \
        or resp.headers.get("x-mbx-used-weight-1m")
    if raw and str(raw).strip().isdigit():
        with _BUCKET_LOCK:
            BUCKETS[base_key]["used"] = int(raw)
            BUCKETS[base_key]["header_ts"] = time.monotonic()


def _admit(base_key: str, weight: int) -> None:
    """Refuse calls past the soft threshold (self-throttle) or in a 418
    cooldown. Never sleep-blocks: raises UpstreamError(kind=rate-limit
    or ip-ban) so tools degrade honestly."""
    now = time.monotonic()
    with _BUCKET_LOCK:
        b = BUCKETS[base_key]
        if now < _THROTTLE_UNTIL[base_key]:
            raise UpstreamError(
                f"{base_key} weight throttle active "
                f"({b['used']}/{b['limit']} used) - retry shortly",
                kind="rate-limit")
        if b["used"] + weight > int(b["limit"] * _SOFT_THRESHOLD):
            # one short pause for the 1m window to roll, then honest raise
            _THROTTLE_UNTIL[base_key] = now + 5.0
            raise UpstreamError(
                f"{base_key} weight budget nearly exhausted: header says "
                f"{b['used']}/{b['limit']}/min, next call needs {weight} - "
                f"pausing 5s; narrow the query or wait for the window",
                kind="rate-limit")


def _backoff_seconds(resp, attempt: int) -> float:
    """Retry-After when the server sends it, else exponential."""
    ra = resp.headers.get("Retry-After") if resp is not None else None
    if ra:
        try:
            return min(30.0, float(ra))
        except ValueError:
            pass
    return 2.0 * (attempt + 1)


# ---------------------------------------------------------------------------
# TTL caches

_TTLS: dict[str, float] = {
    "fapi:/exchangeInfo": 3600.0,
    "fapi:/ticker/24hr": 15.0,
    "fapi:/premiumIndex": 15.0,
    "fapi:/fundingInfo": 600.0,
    "fapi:/openInterest": 30.0,
    "sapi:/exchangeInfo": 3600.0,
    "sapi:/ticker/24hr": 15.0,
}
# klines / depth / trades carry their own per-key TTLs at call time
_DEFAULT_TTL = 15.0
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(base_key: str, path: str, params: dict) -> str:
    ident = urllib.parse.urlencode(sorted((k, v) for k, v in
                                           (params or {}).items()
                                           if v is not None))
    return f"{base_key}:{path}?{ident}"


def cache_age(base_key: str, path: str, params: dict | None = None) \
        -> float | None:
    """Seconds since this exact request was cached; None when not."""
    key = _cache_key(base_key, path, dict(params or {}))
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry is None:
        return None
    return time.monotonic() - entry[0]


def _cached_get(base_key: str, base: str, path: str,
                params: dict | None = None, ttl: float | None = None) -> Any:
    key = _cache_key(base_key, path, dict(params or {}))
    eff_ttl = _TTLS.get(f"{base_key}:{path}", _DEFAULT_TTL) \
        if ttl is None else ttl
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None and time.monotonic() - entry[0] <= eff_ttl:
            return entry[1]
    payload = get(base_key, base, path, params)
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), payload)
    return payload


# ---------------------------------------------------------------------------
# Core GET


def get(base_key: str, base: str, path: str, params: dict | None = None,
        retries: int = _RETRIES) -> Any:
    """GET {base}{path}?{params}; decodes JSON. Tracks the weight header,
    admits against the local budget, retries 429/5xx honoring
    Retry-After. Raises UpstreamError on final failure."""
    qs = urllib.parse.urlencode(
        {k: v for k, v in (params or {}).items() if v is not None})
    url = f"{base}{path}" + (f"?{qs}" if qs else "")
    weight = endpoint_weight(path, dict(params or {}))
    _admit(base_key, weight)
    last_error: UpstreamError | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                _record_header(base_key, r)
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read(300).decode("utf-8", "replace")
            except Exception:
                body = ""
            if e.code == 418:
                with _BUCKET_LOCK:
                    _THROTTLE_UNTIL[base_key] = time.monotonic() \
                        + _BAN_COOLDOWN
                raise UpstreamError(
                    f"aster {base_key} 418: IP temporarily banned "
                    f"(repeated rate-limit) on {path}; cooling down "
                    f"{_BAN_COOLDOWN:.0f}s",
                    kind="ip-ban", status=418) from e
            if e.code == 429:
                with _BUCKET_LOCK:
                    _LAST_429[base_key] = time.monotonic()
                if attempt < retries - 1:
                    time.sleep(_backoff_seconds(e, attempt))
                    continue
                raise UpstreamError(
                    f"aster {base_key} 429 rate limited on {path} after "
                    f"{attempt + 1} attempt(s){' - ' + body if body else ''}",
                    kind="rate-limit", status=429) from e
            if 500 <= e.code <= 599 and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            hint = body or e.reason
            raise UpstreamError(
                f"aster {base_key} {e.code} on {path}: {hint}",
                kind="http", status=e.code) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                OSError) as e:
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise UpstreamError(
                f"aster {base_key} unreachable on {path} after "
                f"{retries} tries: {e}", kind="network") from e
        except ValueError as e:
            raise UpstreamError(
                f"aster {base_key} returned invalid JSON on {path}: {e}",
                kind="http") from e
    raise UpstreamError(f"aster {base_key} failed on {path}: {last_error}",
                        kind="network")


# ---------------------------------------------------------------------------
# Typed helpers (cached) - what the tools call. All fapi/sapi numerics
# are STRINGS upstream; parsing happens in server.py (_f never raises).

def fapi(path: str, params: dict | None = None, ttl: float | None = None) \
        -> Any:
    return _cached_get("fapi", FAPI_BASE, path, params, ttl)


def sapi(path: str, params: dict | None = None, ttl: float | None = None) \
        -> Any:
    return _cached_get("sapi", SAPI_BASE, path, params, ttl)


def fapi_exchange_info() -> dict:
    """Futures exchangeInfo: ~580 symbols with status/filters/
    onboardDate/contractType (3600s cache)."""
    return fapi("/exchangeInfo")


def sapi_exchange_info() -> dict:
    """Spot exchangeInfo: ~68 TRADING pairs + TEST* junk (3600s cache)."""
    return sapi("/exchangeInfo")


def fapi_tickers() -> list:
    """ticker/24hr with NO symbol: ALL ~574 rows in ONE call (15s cache)."""
    return fapi("/ticker/24hr")


def sapi_tickers() -> list:
    """Spot ticker/24hr ALL rows (15s cache)."""
    return sapi("/ticker/24hr")


def premium_index(symbol: str | None = None) -> Any:
    """premiumIndex: ALL ~730 rows when symbol is None (mark/index/
    lastFundingRate/nextFundingTime); 15s cache."""
    return fapi("/premiumIndex", {"symbol": symbol} if symbol else None)


def funding_info() -> list:
    """fundingInfo ALL ~730 rows: fundingIntervalHours (1/2/4/8 mixed),
    cap/floor, interestRate (600s cache)."""
    return fapi("/fundingInfo")


def depth(symbol: str, limit: int = 10, venue: str = "futures") -> dict:
    """Order book. Weight-aware limit mapping: the requested depth is
    snapped DOWN to the nearest priced tier (5/10/20/50/100/500/1000)
    so a caller cannot accidentally spend weight-20 budget."""
    snapped = _snap_depth_limit(limit)
    if (venue or "futures").lower() == "spot":
        return sapi("/depth", {"symbol": _norm_symbol(symbol),
                               "limit": snapped}, ttl=5.0)
    return fapi("/depth", {"symbol": _norm_symbol(symbol),
                           "limit": snapped}, ttl=5.0)


_KLINE_LIMITS = (5, 10, 20, 50, 100, 200, 500, 1000)


def _snap_depth_limit(limit: int) -> int:
    for cap in (5, 10, 20, 50, 100, 500, 1000):
        if limit <= cap:
            return cap
    return 1000


def _snap_kline_limit(limit: int) -> int:
    for cap in _KLINE_LIMITS:
        if limit <= cap:
            return cap
    return 1000


def klines(symbol: str, interval: str = "1h", limit: int = 100,
           market: str = "futures", price_type: str = "last") -> list:
    """Klines. price_type last|mark|index selects klines/
    markPriceKlines/indexPriceKlines on fapi; spot goes to sapi
    /api/v3/klines. limit snapped to a priced tier."""
    snapped = _snap_kline_limit(limit)
    sym = _norm_symbol(symbol)
    if (market or "futures").lower() == "spot":
        return sapi("/klines", {"symbol": sym, "interval": interval,
                                "limit": snapped}, ttl=60.0)
    path = {"last": "/klines", "mark": "/markPriceKlines",
            "index": "/indexPriceKlines"}.get(
                (price_type or "last").lower(), "/klines")
    return fapi(path, {"symbol": sym, "interval": interval,
                       "limit": snapped}, ttl=60.0)


def trades(symbol: str, limit: int = 20, venue: str = "futures") -> list:
    """Recent trades. fapi /trades is keyless-fresh; the spot /trades
    path was uncertain at recon time - probed in smoke_live; failures
    degrade to an honest error dict in the tool."""
    snapped = min(1000, max(1, int(limit)))
    if (venue or "futures").lower() == "spot":
        return sapi("/trades", {"symbol": _norm_symbol(symbol),
                                "limit": snapped}, ttl=10.0)
    return fapi("/trades", {"symbol": _norm_symbol(symbol),
                            "limit": snapped}, ttl=10.0)


def open_interest(symbol: str) -> dict:
    """Per-symbol openInterest snapshot ONLY (30s cache). The
    /futures/data/openInterestHist history endpoint 404s on Aster - no
    history exists keyless; tools say so honestly."""
    return fapi("/openInterest", {"symbol": _norm_symbol(symbol)})


# ---------------------------------------------------------------------------
# Input normalization


def _norm_symbol(symbol: str) -> str:
    """'BTC/USDT' -> 'BTCUSDT'; uppercases; strips."""
    s = (symbol or "").strip().upper().replace("/", "")
    return s


def reset_caches() -> None:
    """Drop all TTL caches + weight buckets (test hook)."""
    with _CACHE_LOCK:
        _CACHE.clear()
    with _BUCKET_LOCK:
        for b in BUCKETS.values():
            b["used"] = 0
            b["header_ts"] = 0.0
        for k in _THROTTLE_UNTIL:
            _THROTTLE_UNTIL[k] = 0.0
            _LAST_429[k] = 0.0
