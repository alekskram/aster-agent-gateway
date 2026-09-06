"""Test isolation: fixture-backed urlopen; NO socket may open in CI.

Every upstream surface (fapi/sapi REST, tapi/solana/evm JSON-RPC) is
monkeypatched at the urllib boundary to serve tests/fixtures/*.json.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    """time.monotonic stand-in advanced by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class FakeResponse:
    def __init__(self, payload, headers: dict | None = None):
        self._payload = payload
        self.headers = headers or {}

    def read(self, n=-1) -> bytes:
        data = json.dumps(self._payload).encode()
        return data if n < 0 else data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def load(name: str):
    """Parse a fixture file (missing file -> empty marker, tests may
    skip on it)."""
    p = FIXTURES / name
    if not p.exists():
        raise FileNotFoundError(name)
    return json.loads(p.read_text())


def http_error(url: str, code: int, msg: str):
    import email.message
    return urllib.error.HTTPError(
        url, code, msg, email.message.Message(), None)


@pytest.fixture
def rest_mock(monkeypatch):
    """Serve REST fixtures keyed by URL; track calls + weight headers.

    Yields (rest module, calls list). calls entries:
    {"url":..., "weight_header": int|None}
    """
    import aster_mcp.rest as rest

    clock = FakeClock()
    calls: list[dict] = []

    def route(url: str):
        q = urllib.parse.urlparse(url)
        base = "fapi" if "fapi." in url else "sapi"
        path = q.path.replace("/fapi/v3", "").replace("/api/v3", "")
        name = f"{base}_{_fixture_stem(path)}"
        return name

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append({"url": url, "weight_header": 240})
        name = route(url)
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
        if f is None:
            raise http_error(url, 404, f"no route for {url}")
        try:
            payload = load(f)
        except FileNotFoundError:
            raise http_error(url, 404, f"fixture {f} missing")
        return FakeResponse(payload, {"X-MBX-USED-WEIGHT-1M": "240"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(rest, "_CACHE", {})
    with rest._BUCKET_LOCK:
        rest.BUCKETS["fapi"]["used"] = 0
        rest.BUCKETS["sapi"]["used"] = 0
        rest._THROTTLE_UNTIL["fapi"] = 0.0
        rest._THROTTLE_UNTIL["sapi"] = 0.0
    monkeypatch.setattr(rest.time, "monotonic", clock)
    return rest, calls, clock


def _fixture_stem(path: str) -> str:
    """'/ticker/24hr' -> 'ticker24hr_all'; '/depth' -> 'depth'."""
    stem = path.strip("/").replace("/", "")
    if "ticker" in stem:
        return "ticker24hr_all"
    if "premiumIndex" in stem:
        return "premiumIndex_all"
    if "fundingInfo" in stem:
        return "fundingInfo_all"
    if "exchangeInfo" in stem:
        return "exchangeInfo"
    return stem
