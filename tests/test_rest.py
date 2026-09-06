"""Unit tests for aster_mcp.rest - 100% offline (fixture urlopen)."""
import json
import time

import pytest

import aster_mcp.rest as rest
from conftest import FakeClock, FakeResponse, http_error, load


class TestWeightPricing:
    def test_depth_weight_tiers(self):
        assert rest.depth_weight(5) == 2
        assert rest.depth_weight(10) == 2
        assert rest.depth_weight(50) == 2
        assert rest.depth_weight(100) == 5
        assert rest.depth_weight(500) == 10
        assert rest.depth_weight(1000) == 20
        assert rest.depth_weight(5000) == 20

    def test_kline_weight_tiers(self):
        assert rest.kline_weight(100) == 1
        assert rest.kline_weight(300) == 2
        assert rest.kline_weight(500) == 5
        assert rest.kline_weight(1000) == 5

    def test_trades_weight_tiers(self):
        assert rest.trades_weight(20) == 1
        assert rest.trades_weight(100) == 1
        assert rest.trades_weight(500) == 2
        assert rest.trades_weight(600) == 10
        assert rest.trades_weight(1000) == 10

    def test_ticker_all_heavier_than_single(self):
        assert rest.endpoint_weight("/fapi/v3/ticker/24hr", {}) == 40
        assert rest.endpoint_weight(
            "/fapi/v3/ticker/24hr", {"symbol": "BTCUSDT"}) == 1


class TestWeightHeaderTracking:
    def test_header_recorded_from_response(self, monkeypatch):
        import urllib.request
        monkeypatch.setattr(rest, "_CACHE", {})
        with rest._BUCKET_LOCK:
            rest.BUCKETS["fapi"]["used"] = 0
            rest._THROTTLE_UNTIL["fapi"] = 0.0
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None: FakeResponse(
                                [], {"X-MBX-USED-WEIGHT-1M": "777"}))
        rest.get("fapi", rest.FAPI_BASE, "/ticker/24hr", {"symbol": "X"})
        assert rest.used_weight("fapi") == 777

    def test_self_throttle_above_soft_threshold(self, monkeypatch):
        import urllib.request
        monkeypatch.setattr(rest, "_CACHE", {})
        with rest._BUCKET_LOCK:
            rest.BUCKETS["fapi"]["used"] = 2300  # > 80% of 2400
            rest._THROTTLE_UNTIL["fapi"] = 0.0
        with pytest.raises(rest.UpstreamError) as ei:
            rest.get("fapi", rest.FAPI_BASE, "/ticker/24hr", {})
        assert ei.value.kind == "rate-limit"
        assert "weight" in str(ei.value).lower()

    def test_throttle_window_blocks_followup(self, monkeypatch):
        import urllib.request
        clock = FakeClock()
        monkeypatch.setattr(rest.time, "monotonic", clock)
        monkeypatch.setattr(rest, "_CACHE", {})
        with rest._BUCKET_LOCK:
            rest.BUCKETS["fapi"]["used"] = 2300
            rest._THROTTLE_UNTIL["fapi"] = 0.0
        with pytest.raises(rest.UpstreamError):
            rest.get("fapi", rest.FAPI_BASE, "/ticker/24hr", {})
        # even with used weight reset, the 5s throttle holds
        with rest._BUCKET_LOCK:
            rest.BUCKETS["fapi"]["used"] = 0
        with pytest.raises(rest.UpstreamError) as ei:
            rest.get("fapi", rest.FAPI_BASE, "/ticker/24hr", {})
        assert ei.value.kind == "rate-limit"
        clock.advance(6.0)
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None: FakeResponse([]))
        rest.get("fapi", rest.FAPI_BASE, "/exchangeInfo")
        # un-throttled call succeeded


class TestRetryBackoff:
    def test_429_retry_then_success(self, monkeypatch):
        import urllib.request
        state = {"n": 0}
        sleeps = []
        monkeypatch.setattr(rest, "_CACHE", {})
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        with rest._BUCKET_LOCK:
            rest.BUCKETS["fapi"]["used"] = 0
            rest._THROTTLE_UNTIL["fapi"] = 0.0

        def flaky(req, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                e = http_error(req.full_url, 429, "rate")
                e.headers = {"Retry-After": "3"}
                raise e
            return FakeResponse({"ok": 1})

        monkeypatch.setattr(urllib.request, "urlopen", flaky)
        out = rest.get("fapi", rest.FAPI_BASE, "/exchangeInfo")
        assert out == {"ok": 1}
        assert sleeps == [3.0]  # Retry-After honored

    def test_418_ip_ban_cooldown(self, monkeypatch):
        import urllib.request
        monkeypatch.setattr(rest, "_CACHE", {})
        with rest._BUCKET_LOCK:
            rest.BUCKETS["fapi"]["used"] = 0
            rest._THROTTLE_UNTIL["fapi"] = 0.0

        def banned(req, timeout=None):
            raise http_error(req.full_url, 418, "banned")

        monkeypatch.setattr(urllib.request, "urlopen", banned)
        with pytest.raises(rest.UpstreamError) as ei:
            rest.get("fapi", rest.FAPI_BASE, "/exchangeInfo")
        assert ei.value.kind == "ip-ban"
        assert ei.value.status == 418
        # cooldown refuses further calls without dialing
        def no_dial(req, timeout=None):
            raise AssertionError("must not dial during cooldown")
        monkeypatch.setattr(urllib.request, "urlopen", no_dial)
        with pytest.raises(rest.UpstreamError) as ei2:
            rest.get("fapi", rest.FAPI_BASE, "/exchangeInfo")
        assert ei2.value.kind in ("ip-ban", "rate-limit")

    def test_4xx_no_retry(self, monkeypatch):
        import urllib.request
        state = {"n": 0}
        monkeypatch.setattr(rest, "_CACHE", {})
        monkeypatch.setattr(time, "sleep", lambda s: None)
        with rest._BUCKET_LOCK:
            rest.BUCKETS["fapi"]["used"] = 0
            rest._THROTTLE_UNTIL["fapi"] = 0.0

        def not_found(req, timeout=None):
            state["n"] += 1
            raise http_error(req.full_url, 404, "nope")

        monkeypatch.setattr(urllib.request, "urlopen", not_found)
        with pytest.raises(rest.UpstreamError) as ei:
            rest.get("fapi", rest.FAPI_BASE, "/futures/data/openInterestHist",
                     {"symbol": "BTCUSDT"})
        assert ei.value.kind == "http"
        assert ei.value.status == 404
        assert state["n"] == 1  # no retries on plain 404

    def test_network_error_raises_after_retries(self, monkeypatch):
        import urllib.error
        import urllib.request
        monkeypatch.setattr(rest, "_CACHE", {})
        monkeypatch.setattr(time, "sleep", lambda s: None)
        with rest._BUCKET_LOCK:
            rest.BUCKETS["fapi"]["used"] = 0
            rest._THROTTLE_UNTIL["fapi"] = 0.0

        def dead(req, timeout=None):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(urllib.request, "urlopen", dead)
        with pytest.raises(rest.UpstreamError) as ei:
            rest.get("fapi", rest.FAPI_BASE, "/exchangeInfo")
        assert ei.value.kind == "network"


class TestTTLCache:
    def test_second_call_cached(self, rest_mock):
        rest_mod, calls, clock = rest_mock
        rest_mod.fapi_exchange_info()
        rest_mod.fapi_exchange_info()
        assert len(calls) == 1

    def test_expiry_after_ttl(self, rest_mock):
        rest_mod, calls, clock = rest_mock
        rest_mod.fapi_tickers()   # 15s TTL
        clock.advance(16.0)
        rest_mod.fapi_tickers()
        assert len(calls) == 2

    def test_long_ttl_inside_window(self, rest_mock):
        rest_mod, calls, clock = rest_mock
        rest_mod.fapi_exchange_info()  # 3600s TTL
        clock.advance(600.0)
        rest_mod.fapi_exchange_info()
        assert len(calls) == 1

    def test_cache_age_reported(self, rest_mock):
        rest_mod, calls, clock = rest_mock
        rest_mod.fapi_tickers()
        clock.advance(5.0)
        age = rest_mod.cache_age("fapi", "/ticker/24hr")
        assert 4.0 < age < 6.0

    def test_distinct_params_distinct_entries(self, rest_mock):
        rest_mod, calls, clock = rest_mock
        rest_mod.depth("BTCUSDT", 10)
        rest_mod.depth("ETHUSDT", 10)
        assert len(calls) == 2

    def test_reset_caches(self, rest_mock):
        rest_mod, calls, clock = rest_mock
        rest_mod.fapi_exchange_info()
        rest_mod.reset_caches()
        rest_mod.fapi_exchange_info()
        assert len(calls) == 2


class TestHelpers:
    def test_norm_symbol(self):
        assert rest._norm_symbol("btc/usdt") == "BTCUSDT"
        assert rest._norm_symbol(" BTCUSDT ") == "BTCUSDT"

    def test_snap_depth_limit(self):
        assert rest._snap_depth_limit(3) == 5
        assert rest._snap_depth_limit(10) == 10
        assert rest._snap_depth_limit(9999) == 1000

    def test_snap_kline_limit(self):
        assert rest._snap_kline_limit(24) == 50
        assert rest._snap_kline_limit(1500) == 1000
