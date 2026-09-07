"""Unit tests for aster_mcp.server - all 13 tools, 100% offline.

Every upstream surface is monkeypatched at the rest/chain module
boundaries (the server only calls rest.fapi_tickers() etc. by design)
so no HTTP can happen.
"""
import json
import time
from pathlib import Path

import pytest

import aster_mcp.chain as chain
import aster_mcp.rest as rest
import aster_mcp.server as srv

FIXTURES = Path(__file__).parent / "fixtures"

F_INFO = json.loads((FIXTURES / "fapi_exchangeInfo.json").read_text())
F_TICKERS = json.loads((FIXTURES / "fapi_ticker24hr_all.json").read_text())
F_PREMIUM = json.loads((FIXTURES / "fapi_premiumIndex_all.json").read_text())
F_FUNDING = json.loads((FIXTURES / "fapi_fundingInfo_all.json").read_text())
F_DEPTH = json.loads((FIXTURES / "fapi_depth_btcusdt.json").read_text())
F_KLINES = json.loads((FIXTURES / "fapi_klines_btcusdt_1h.json").read_text())
F_MARK_KLINES = json.loads(
    (FIXTURES / "fapi_markPriceKlines_btcusdt_1h.json").read_text())
F_INDEX_KLINES = json.loads(
    (FIXTURES / "fapi_indexPriceKlines_btcusdt_1h.json").read_text())
F_TRADES = json.loads((FIXTURES / "fapi_trades_btcusdt.json").read_text())
F_OI = json.loads((FIXTURES / "fapi_openInterest_btcusdt.json").read_text())
S_INFO = json.loads((FIXTURES / "sapi_exchangeInfo.json").read_text())
S_TICKERS = json.loads((FIXTURES / "sapi_ticker24hr_all.json").read_text())
S_KLINES = json.loads((FIXTURES / "sapi_klines_btcusdt.json").read_text())
S_DEPTH = json.loads((FIXTURES / "sapi_depth_btcusdt.json").read_text())
S_TRADES = json.loads((FIXTURES / "sapi_trades_btcusdt.json").read_text())
SOL_SIGS = json.loads((FIXTURES / "solana_vault_signatures.json").read_text())
# tapi fixtures hold the JSON-RPC *result* directly (live-verified
# shape: privacy-hidden = {address, accountPrivacy}; public adds
# perpAssets/positions)
TAPI_VAULT = json.loads((FIXTURES / "tapi_getBalance_vault.json").read_text())
TAPI_PUBLIC = json.loads(
    (FIXTURES / "tapi_getBalance_public.json").read_text())


@pytest.fixture
def mock_rest(monkeypatch):
    """Replace the rest surface with fixture-backed lambdas; leaks to
    the network fail loudly."""
    def _boom(*a, **k):  # pragma: no cover - only on a bug
        raise AssertionError("live HTTP attempted in offline test!")

    monkeypatch.setattr(rest, "get", _boom)
    monkeypatch.setattr(rest, "fapi_exchange_info", lambda: F_INFO)
    monkeypatch.setattr(rest, "sapi_exchange_info", lambda: S_INFO)
    monkeypatch.setattr(rest, "fapi_tickers", lambda: F_TICKERS)
    monkeypatch.setattr(rest, "sapi_tickers", lambda: S_TICKERS)
    monkeypatch.setattr(rest, "premium_index", lambda symbol=None: F_PREMIUM)
    monkeypatch.setattr(rest, "funding_info", lambda: F_FUNDING)
    monkeypatch.setattr(rest, "depth",
                        lambda symbol, limit=10, venue="futures":
                        F_DEPTH if venue == "futures" else S_DEPTH)
    monkeypatch.setattr(
        rest, "klines",
        lambda symbol, interval="1h", limit=100, market="futures",
        price_type="last": {
            ("futures", "last"): F_KLINES,
            ("futures", "mark"): F_MARK_KLINES,
            ("futures", "index"): F_INDEX_KLINES,
            ("spot", "last"): S_KLINES,
        }.get((market, price_type), F_KLINES))
    monkeypatch.setattr(rest, "trades",
                        lambda symbol, limit=20, venue="futures":
                        F_TRADES if venue == "futures" else S_TRADES)
    monkeypatch.setattr(rest, "open_interest", lambda symbol: F_OI)
    monkeypatch.setattr(rest, "cache_age",
                        lambda base_key, path, params=None: 0.0)


@pytest.fixture
def mock_chain(monkeypatch):
    """Replace the chain surface with fixture-backed lambdas."""
    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("live RPC attempted in offline test!")

    monkeypatch.setattr(chain, "rpc_post", _boom)
    monkeypatch.setattr(chain, "solana_vault_signatures",
                        lambda limit=20, vault=None: SOL_SIGS)
    monkeypatch.setattr(chain, "evm_latest_block", lambda c: 40_000_000)
    monkeypatch.setattr(chain, "evm_vault_transfers",
                        lambda c, f, t, limit_blocks=300: [
                            {"transactionHash": "0xabc",
                             "blockNumber": hex(t - 5),
                             "topics": [chain.TRANSFER_TOPIC, None,
                                        "0x" + "0" * 24 +
                                        chain.EVM_VAULTS[c][2:]],
                             "address": "0xtoken"}])
    monkeypatch.setattr(chain, "tapi_call",
                        lambda method, address, extra=None: TAPI_VAULT)


# ------------------------------------------------------------ numeric hygiene

class TestNumericHygiene:
    def test_f_parses_numeric_strings(self):
        assert srv._f("1.5") == 1.5
        assert srv._f("0.0001") == 0.0001

    def test_f_empty_and_garbage_none(self):
        assert srv._f("") is None
        assert srv._f("   ") is None
        assert srv._f(None) is None
        assert srv._f("abc") is None
        assert srv._f("12.3.4") is None
        assert srv._f({}) is None

    def test_f_never_raises(self):
        for probe in (0, 1e18, "1e5", " 42 ", "-0.5", object(), True):
            srv._f(probe)

    def test_i(self):
        assert srv._i("1757112500") == 1757112500
        assert srv._i("") is None
        assert srv._i("zz") is None

    def test_all_fixture_numerics_parseable(self):
        for t in F_TICKERS:
            for k in ("lastPrice", "priceChangePercent", "quoteVolume"):
                v = srv._f(t.get(k))
                assert v is None or isinstance(v, float)


# --------------------------------------------------------------- tool 1..13

class TestMarketOverview:
    def test_panel_trading_only(self, mock_rest):
        out = srv.market_overview()
        syms = {r["symbol"] for r in out["markets"]}
        info_status = {s["symbol"]: s.get("status")
                       for s in F_INFO["symbols"]}
        settling = {s for s, st in info_status.items()
                    if st != "TRADING"}
        assert not (settling & syms)  # non-TRADING excluded
        assert out["status_counts"].get("SETTLING") >= 10
        assert out["status_counts"].get("TRADING") > 500

    def test_sorted_by_volume_default(self, mock_rest):
        out = srv.market_overview(limit=5)
        vols = [r["quote_volume"] for r in out["markets"]]
        assert vols == sorted(vols, reverse=True)

    def test_sort_change(self, mock_rest):
        out = srv.market_overview(limit=3, sort="change")
        chg = [abs(r["change_pct"]) for r in out["markets"]]
        assert chg == sorted(chg, reverse=True)

    def test_fresh_listings(self, mock_rest):
        out = srv.market_overview()
        assert out["fresh_listings"]
        assert all(r["onboard_date"] for r in out["fresh_listings"])

    def test_quote_counts(self, mock_rest):
        out = srv.market_overview()
        assert out["quote_counts"].get("USDT", 0) > 500
        assert out["quote_counts"].get("USD1") >= 5

    def test_freshness_field(self, mock_rest):
        assert srv.market_overview()["age_seconds"] == 0.0

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom():
            raise rest.UpstreamError("fapi down", kind="network")
        monkeypatch.setattr(rest, "fapi_tickers", boom)
        out = srv.market_overview()
        assert out["error"] and out["source"] == "fapi"
        assert "network" in out["reason"]


class TestExchangeSymbols:
    def test_futures_micro_view(self, mock_rest):
        out = srv.exchange_symbols(venue="futures")
        row = next(r for r in out["futures"]["symbols"]
                   if r["symbol"] == "BTCUSDT")
        assert row["tick_size"] == 0.1
        assert row["step_size"] == 0.001
        assert row["min_notional"] == 5.0
        assert row["max_leverage"] is None or row["max_leverage"] > 1
        assert row["leverage_filter_kind"] in ("LEVERAGE_FILTER", None)

    def test_junk_hidden_by_default(self, mock_rest):
        out = srv.exchange_symbols(venue="futures")
        names = {r["symbol"] for r in out["futures"]["symbols"]}
        rows_all = {s["symbol"]: s for s in F_INFO["symbols"]}
        junk = {s for s, r in rows_all.items()
                if r.get("status") != "TRADING"}
        assert not (junk & names)  # junk statuses never in panel
        assert out["futures"]["junk_filtered"] == len(junk) >= 10

    def test_include_junk_flag(self, mock_rest):
        out = srv.exchange_symbols(venue="futures", include_junk=True)
        names = {r["symbol"] for r in out["futures"]["symbols"]}
        rows_all = {s["symbol"]: s for s in F_INFO["symbols"]}
        junk = [s for s, r in rows_all.items()
                if r.get("status") != "TRADING"]
        assert set(junk) <= names

    def test_spot_test_junk_filtered(self, mock_rest):
        out = srv.exchange_symbols(venue="spot")
        names = {r["symbol"] for r in out["spot"]["symbols"]}
        assert not any(n.startswith("TEST") for n in names)
        n_test = sum(1 for s in S_INFO["symbols"]
                     if s["symbol"].startswith("TEST"))
        assert n_test >= 2  # fixture carries real TEST* listings
        assert out["spot"]["junk_filtered"] == n_test

    def test_single_symbol_detail(self, mock_rest):
        out = srv.exchange_symbols(venue="futures", symbol="BTCUSDT")
        assert len(out["futures"]["symbols"]) == 1
        assert out["futures"]["symbols"][0]["symbol"] == "BTCUSDT"

    def test_unknown_symbol_honest(self, mock_rest):
        out = srv.exchange_symbols(venue="futures", symbol="NOPEUSDT")
        assert out["futures"]["count"] == 0
        assert "not found" in out["futures"]["detail"]

    def test_both_venues(self, mock_rest):
        out = srv.exchange_symbols(venue="both")
        assert "futures" in out and "spot" in out

    def test_bad_venue_raises(self):
        with pytest.raises(ValueError, match="venue"):
            srv.exchange_symbols(venue="options")


class TestOrderBook:
    def test_futures_book(self, mock_rest):
        out = srv.order_book("BTCUSDT", depth=10)
        assert out["bids"] and out["asks"]
        assert out["bids"][0][0] < out["asks"][0][0]
        assert out["weight"] == 2

    def test_weight_aware_snap(self, mock_rest):
        out = srv.order_book("BTCUSDT", depth=400)
        assert out["limit_used"] == 500
        assert out["weight"] == 10
        assert len(out["bids"]) <= 500

    def test_spot_book(self, mock_rest):
        out = srv.order_book("BTCUSDT", venue="spot", depth=5)
        assert out["venue"] == "spot"
        assert out["bids"] and out["bids"][0][0] < out["asks"][0][0]

    def test_symbol_normalization(self, mock_rest):
        out = srv.order_book("btc/usdt")
        assert out["symbol"] == "BTCUSDT"

    def test_upstream_failure(self, monkeypatch):
        def boom(*a, **k):
            raise rest.UpstreamError("429", kind="rate-limit")
        monkeypatch.setattr(rest, "depth", boom)
        out = srv.order_book("BTCUSDT")
        assert out["error"] and out["source"] == "fapi"
        assert out["reason"] == "upstream failure (rate-limit)"

    def test_empty_symbol_raises(self):
        with pytest.raises(ValueError, match="symbol"):
            srv.order_book("")


class TestKlines:
    def test_last_mark_index_paths(self, mock_rest):
        last = srv.klines("BTCUSDT", interval="1h", limit=24)
        assert last["price_type"] == "last"
        assert last["klines"][0]["close"] == 70000.5
        mark = srv.klines("BTCUSDT", price_type="mark")
        assert mark["klines"][0]["close"] == 70001.5
        idx = srv.klines("BTCUSDT", price_type="index")
        assert idx["klines"][0]["close"] == 69999.5

    def test_spot_klines(self, mock_rest):
        out = srv.klines("BTCUSDT", market="spot", limit=24)
        assert out["market"] == "spot"
        assert out["price_type"] is None

    def test_bad_interval_raises(self):
        with pytest.raises(ValueError, match="interval"):
            srv.klines("BTCUSDT", interval="45m")

    def test_bad_price_type_raises(self):
        with pytest.raises(ValueError, match="price_type"):
            srv.klines("BTCUSDT", price_type="oracle")

    def test_limit_snap(self, mock_rest):
        out = srv.klines("BTCUSDT", limit=9999)
        assert out["count"] <= 1000

    def test_upstream_failure(self, monkeypatch):
        def boom(*a, **k):
            raise rest.UpstreamError("404", kind="http", status=404)
        monkeypatch.setattr(rest, "klines", boom)
        out = srv.klines("BTCUSDT")
        assert out["error"] and out["source"] == "fapi"


class TestTrades:
    def test_futures_trades(self, mock_rest):
        out = srv.trades("BTCUSDT", limit=10)
        assert out["count"] == len(F_TRADES)
        assert out["trades"][0]["price"] == float(F_TRADES[0]["price"])
        assert isinstance(out["trades"][0]["is_buyer_maker"], bool)

    def test_spot_trades_alive_on_fixture(self, mock_rest):
        out = srv.trades("BTCUSDT", venue="spot")
        assert out["count"] == len(S_TRADES) >= 1

    def test_spot_trades_dead_honest_error(self, monkeypatch):
        def boom(*a, **k):
            raise rest.UpstreamError("aster sapi 404 on /trades: path dead",
                                     kind="http", status=404)
        monkeypatch.setattr(rest, "trades", boom)
        out = srv.trades("BTCUSDT", venue="spot")
        assert out["error"] and out["source"] == "sapi"
        assert "404" in out["error"]

    def test_empty_symbol_raises(self):
        with pytest.raises(ValueError, match="symbol"):
            srv.trades("")


class TestSpotOverview:
    def test_pairs_and_junk_filter(self, mock_rest):
        out = srv.spot_overview()
        syms = [r["symbol"] for r in out["pairs"]]
        assert "BTCUSDT" in syms
        assert not any(s.startswith("TEST") for s in syms)
        n_junk = sum(1 for t in S_TICKERS
                     if t.get("symbol", "").startswith("TEST"))
        assert out["junk_filtered"] == n_junk >= 1
        trading = {s["symbol"] for s in S_INFO["symbols"]
                   if s.get("status") == "TRADING"}
        expected = {t["symbol"] for t in S_TICKERS
                    if t.get("symbol") in trading
                    and not t["symbol"].startswith("TEST")}
        assert out["count"] == len(expected)

    def test_ephemeral_options_rows_filtered(self, mock_rest):
        # the live spot ticker list carries ~24k BTC_UP_DOWN_5M_* and
        # EVENT_* options rows; the exchangeInfo join must drop them
        out = srv.spot_overview(limit=100)
        syms = [r["symbol"] for r in out["pairs"]]
        assert not any("_UP_DOWN_" in s or s.startswith("EVENT") 
                       for s in syms)
        n_eph = sum(1 for t in S_TICKERS
                    if t.get("symbol", "") not in
                    {s["symbol"] for s in S_INFO["symbols"]})
        assert out["unlisted_or_not_trading_filtered"] == n_eph
        # every kept row carries exchangeInfo fields
        assert all(r["status"] == "TRADING" and r["base"]
                   for r in out["pairs"])

    def test_sorted_by_volume(self, mock_rest):
        out = srv.spot_overview(limit=2)
        vols = [r["quote_volume"] for r in out["pairs"]]
        assert vols == sorted(vols, reverse=True)

    def test_status_join(self, mock_rest):
        out = srv.spot_overview()
        btc = next(r for r in out["pairs"] if r["symbol"] == "BTCUSDT")
        assert btc["status"] == "TRADING"
        assert btc["base"] == "BTC"

    def test_upstream_failure(self, monkeypatch):
        def boom():
            raise rest.UpstreamError("sapi unreachable", kind="network")
        monkeypatch.setattr(rest, "sapi_tickers", boom)
        out = srv.spot_overview()
        assert out["error"] and out["source"] == "sapi"


def _premium_with(sym: str, rate: str) -> list:
    """Focused premiumIndex list: the live row for `sym` with its rate
    pinned (fixture rates drift between captures; pinning keeps these
    tests deterministic without leaving the real schema)."""
    row = next(dict(p) for p in F_PREMIUM if p["symbol"] == sym)
    row["lastFundingRate"] = rate
    return [row]


class TestFundingOverview:
    def test_rows_join_premium_and_fundinginfo(self, mock_rest,
                                                 monkeypatch):
        monkeypatch.setattr(rest, "premium_index",
                          lambda symbol=None:
                          _premium_with("BTCUSDT", "0.00090000"))
        out = srv.funding_overview(limit=100)
        btc = next(r for r in out["funding"]
                   if r["symbol"] == "BTCUSDT")
        assert btc["interval_hours"] == 8
        # live schema: fundingFeeCap/fundingFeeFloor JSON numbers
        assert btc["cap"] == 0.003
        assert btc["floor"] == -0.003
        assert 0 <= btc["interest_rate"] < 0.001
        # SUSHIUSDT live row: 1h interval, cap 0.02 (live schema)
        sushi_f = next(f for f in F_FUNDING
                       if f["symbol"] == "SUSHIUSDT")
        assert sushi_f["fundingIntervalHours"] == 1
        assert float(sushi_f["fundingFeeCap"]) == 0.02
        monkeypatch.setattr(rest, "premium_index",
                            lambda symbol=None:
                            _premium_with("SUSHIUSDT", "0.001"))
        out2 = srv.funding_overview(limit=100)
        sushi = next(r for r in out2["funding"]
                     if r["symbol"] == "SUSHIUSDT")
        assert sushi["interval_hours"] == 1
        assert sushi["cap"] == 0.02

    def test_funding_bounds_legacy_string_fields(self, mock_rest,
                                                 monkeypatch):
        # older captures named the fields cap/floor (strings) - the
        # fallback must keep them working
        legacy = [{"symbol": r["symbol"],
                   "fundingIntervalHours": r["fundingIntervalHours"],
                   "cap": "0.03", "floor": "-0.03",
                   "interestRate": r["interestRate"]}
                  for r in F_FUNDING]
        monkeypatch.setattr(rest, "funding_info", lambda: legacy)
        monkeypatch.setattr(rest, "premium_index",
                          lambda symbol=None:
                          _premium_with("BTCUSDT", "0.00090000"))
        out = srv.funding_overview(limit=100)
        btc = next(r for r in out["funding"]
                   if r["symbol"] == "BTCUSDT")
        assert btc["cap"] == 0.03 and btc["floor"] == -0.03

    def test_mixed_interval_histogram(self, mock_rest):
        out = srv.funding_overview(limit=100)
        hist = out["interval_histogram"]
        # histogram over premiumIndex rows joined with fundingInfo
        assert hist.get("8") >= 5
        assert hist.get("4") >= 1 and hist.get("1") >= 1
        assert len(hist) >= 3  # mixed intervals really present

    def test_annualized_uses_own_interval(self, mock_rest,
                                           monkeypatch):
        monkeypatch.setattr(rest, "premium_index",
                          lambda symbol=None:
                          _premium_with("BTCUSDT", "0.00090000"))
        out = srv.funding_overview(limit=100)
        btc = next(r for r in out["funding"]
                   if r["symbol"] == "BTCUSDT")
        # rate was pinned to 0.0009 by _premium_with above
        exp = 0.0009 * 24 / 8 * 365 * 100
        assert abs(btc["annualized_pct"] - exp) < 0.01

    def test_missing_fundinginfo_row_has_nulls(self, mock_rest,
                                               monkeypatch):
        # premium rows missing from fundingInfo -> null interval fields
        monkeypatch.setattr(rest, "premium_index",
                            lambda symbol=None:
                            _premium_with("BTCUSDT", "0.00090000"))
        monkeypatch.setattr(rest, "funding_info",
                            lambda: [f for f in F_FUNDING
                                     if f["symbol"] != "BTCUSDT"])
        out = srv.funding_overview(limit=100)
        btc = next(r for r in out["funding"]
                   if r["symbol"] == "BTCUSDT")
        assert btc["interval_hours"] is None
        assert btc["cap"] is None and btc["floor"] is None

    def test_sort_by_rate_abs(self, mock_rest):
        out = srv.funding_overview(limit=3)
        rates = [abs(r["last_funding_rate"]) for r in out["funding"]]
        assert rates == sorted(rates, reverse=True)

    def test_upstream_failure(self, monkeypatch):
        def boom(symbol=None):
            raise rest.UpstreamError("fapi 429", kind="rate-limit")
        monkeypatch.setattr(rest, "premium_index", boom)
        out = srv.funding_overview()
        assert out["error"] and out["source"] == "fapi"


class TestTradfiMarkets:
    def test_class_mapping(self, mock_rest):
        out = srv.tradfi_markets(limit=100)
        got = {r["symbol"]: r["asset_class"] for r in out["tradfi"]}
        assert got["XAUUSD1"] == "metals"
        assert got["SPCXUSDT"] == "equity-index"
        assert got["CLUSDT"] == "energy"
        assert got["MUUSDT"] == "equity-single"
        assert got["SNDKUSDT"] == "equity-single"
        assert got["NVDAUSDT"] == "equity-single"
        # pure crypto and crypto lookalikes never classified
        assert "BTCUSDT" not in got
        assert "GNSUSD" not in got

    def test_by_class_aggregation(self, mock_rest):
        out = srv.tradfi_markets()
        assert out["by_class"]["equity-single"]["markets"] >= 4
        assert out["by_class"]["metals"]["quote_volume"] > 1e8
        # classes sum to the panel count
        assert sum(v["markets"] for v in out["by_class"].values()) \
            == out["count"]

    def test_corr_subblock(self, mock_rest):
        out = srv.tradfi_markets(window="24h")
        corr = out["tradfi_crypto_corr"]
        assert corr.get("representative") in ("XAUUSD1", "SPCXUSDT")
        assert isinstance(corr.get("bars"), int)
        assert corr.get("corr") is None or -1 <= corr["corr"] <= 1

    def test_corr_insufficient_data_honest(self, monkeypatch):
        monkeypatch.setattr(rest, "fapi_tickers", lambda: F_TICKERS)
        def boom(*a, **k):
            raise rest.UpstreamError("fapi down", kind="network")
        monkeypatch.setattr(rest, "klines", boom)
        out = srv.tradfi_markets()
        corr = out["tradfi_crypto_corr"]
        assert corr["error"] == "insufficient kline data for correlation"

    def test_upstream_failure(self, monkeypatch):
        def boom():
            raise rest.UpstreamError("fapi down", kind="network")
        monkeypatch.setattr(rest, "fapi_tickers", boom)
        out = srv.tradfi_markets()
        assert out["error"] and out["source"] == "fapi"


class TestFundingScreener:
    def test_ranking_and_regime(self, mock_rest, monkeypatch):
        monkeypatch.setattr(rest, "premium_index",
                          lambda symbol=None:
                          _premium_with("BTCUSDT", "0.00090000"))
        out = srv.funding_screener(top=5)
        top = out["top_by_annualized_rate"]
        assert len(top) == 1  # focused single-row premium list
        # live rows carry non-null cap/floor/regime (fundingFeeCap/
        # fundingFeeFloor schema) - headroom is computable everywhere
        assert all(r["funding_regime"]["cap"] is not None
                   and r["funding_regime"]["floor"] is not None
                   and r["funding_regime"]["regime"] is not None
                   and r["funding_regime"]["headroom_bps"] is not None
                   for r in top)
        btc = top[0]
        # BTC 8h small rate vs 0.003 cap -> normal, positive headroom
        assert btc["symbol"] == "BTCUSDT"
        assert btc["funding_regime"]["regime"] == "normal"
        assert btc["funding_regime"]["headroom_bps"] > 10

    def test_near_cap_when_rate_hits_cap(self, mock_rest,
                                          monkeypatch):
        # SUSHIUSDT 1h row with rate pinned to its 0.02 cap -> zero
        # headroom -> near-cap
        monkeypatch.setattr(rest, "premium_index",
                          lambda symbol=None:
                          _premium_with("SUSHIUSDT", "0.02"))
        out = srv.funding_screener(top=10)
        near = next(r for r in out["top_by_annualized_rate"]
                    if r["symbol"] == "SUSHIUSDT")
        assert near["funding_regime"]["regime"] == "near-cap"
        assert near["funding_regime"]["headroom_bps"] == 0.0

    def test_negative_headroom_near_floor(self, mock_rest,
                                           monkeypatch):
        # TRUTHUSDT 1h row with rate pinned just above its -0.02
        # floor (-0.0199 -> 1 bp of headroom) -> near-floor
        monkeypatch.setattr(rest, "premium_index",
                          lambda symbol=None:
                          _premium_with("TRUTHUSDT", "-0.0199"))
        out = srv.funding_screener(top=10)
        f2 = next(r for r in out["top_by_annualized_rate"]
                  if r["symbol"] == "TRUTHUSDT")
        assert f2["funding_regime"]["regime"] == "near-floor"
        assert f2["funding_rate"] < 0
        assert f2["funding_regime"]["headroom_bps"] < 5

    def test_direction_filter(self, mock_rest):
        out = srv.funding_screener(top=10, direction="long")
        assert all((r["funding_rate"] or 0) > 0
                   for r in out["top_by_annualized_rate"])
        out = srv.funding_screener(top=10, direction="short")
        assert all((r["funding_rate"] or 0) < 0
                   for r in out["top_by_annualized_rate"])

    def test_mark_index_spread_ranking(self, mock_rest):
        out = srv.funding_screener(top=5)
        spreads = [abs(r["mark_index_spread_bps"])
                   for r in out["top_by_mark_index_spread"]]
        assert spreads == sorted(spreads, reverse=True)

    def test_bad_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            srv.funding_screener(direction="sideways")


class TestOiSnapshot:
    def test_explicit_symbols(self, mock_rest):
        out = srv.oi_snapshot(symbols=["BTCUSDT", "ETHUSDT"])
        assert out["count"] == 2
        assert out["open_interest"][0]["open_interest"] == \
            float(F_OI["openInterest"])
        assert "NO keyless OI history" in out["note"]

    def test_top_default_picks_by_volume(self, mock_rest):
        out = srv.oi_snapshot(top=3)
        syms = [r["symbol"] for r in out["open_interest"]]
        vols = {t["symbol"]: float(t["quoteVolume"])
                for t in F_TICKERS}
        assert syms == sorted(syms, key=lambda s: -vols.get(s, 0))

    def test_cap_10_symbols(self, mock_rest, monkeypatch):
        syms = [f"S{i}USDT" for i in range(25)]
        calls = []
        monkeypatch.setattr(rest, "open_interest",
                            lambda s: calls.append(s) or F_OI)
        out = srv.oi_snapshot(symbols=syms)
        assert len(calls) == 10

    def test_per_symbol_failure_warns(self, mock_rest, monkeypatch):
        def flaky(s):
            if s == "ETHUSDT":
                raise rest.UpstreamError("500", kind="http", status=500)
            return F_OI
        monkeypatch.setattr(rest, "open_interest", flaky)
        out = srv.oi_snapshot(symbols=["BTCUSDT", "ETHUSDT"])
        assert out["count"] == 1
        assert any("ETHUSDT" in w["error"] for w in out["warnings"])

    def test_404_history_honest_note(self, mock_rest):
        out = srv.oi_snapshot(symbols=["BTCUSDT"])
        assert "openInterestHist" in out["note"]


class TestDepositFlows:
    def test_solana_chain(self, mock_chain):
        out = srv.deposit_flows(chain_filter="solana", limit=20)
        sol = out["chains"]["solana"]
        assert sol["count"] == len(SOL_SIGS) >= 10
        assert sol["signatures"][0]["slot"] == SOL_SIGS[0]["slot"]
        assert sol["hourly"]  # D4 hourly buckets derived

    def test_stats_block(self, mock_chain):
        out = srv.deposit_flows(chain_filter="all")
        stats = out["deposit_stats"]
        assert stats["per_chain"]["solana"] == len(SOL_SIGS)
        assert "bsc" in stats["per_chain"]

    def test_evm_no_rpc_honest(self, mock_chain, monkeypatch):
        def no_rpc(chain_name, f, t, limit_blocks=300):
            raise chain.RpcError(
                f"no RPC configured for {chain_name}: set "
                f"ASTER_EVM_RPC_URL or ASTER_EVM_RPC_URL_"
                f"{chain_name.upper()} (free public RPCs reject "
                f"vault log queries with -32005, so a real endpoint is "
                f"required)", kind="no-rpc")
        def no_latest(chain_name):
            raise chain.RpcError(
                f"no RPC configured for {chain_name}: set "
                f"ASTER_EVM_RPC_URL or ASTER_EVM_RPC_URL_"
                f"{chain_name.upper()}", kind="no-rpc")
        monkeypatch.setattr(chain, "evm_vault_transfers", no_rpc)
        monkeypatch.setattr(chain, "evm_latest_block", no_latest)
        out = srv.deposit_flows(chain_filter="bsc")
        bsc = out["chains"]["bsc"]
        assert bsc["error"] and bsc["source"] == "bsc"
        assert "no RPC configured" in bsc["error"]
        assert "ASTER_EVM_RPC_URL" in bsc["error"]

    def test_evm_32005_honest(self, mock_chain, monkeypatch):
        def limited(chain_name, f, t, limit_blocks=300):
            raise chain.RpcError("evm rpc error on eth_getLogs: "
                                 "-32005 limit exceeded",
                                 kind="rpc-http", code=-32005)
        monkeypatch.setattr(chain, "evm_latest_block", lambda c: 40_000_000)
        monkeypatch.setattr(chain, "evm_vault_transfers", limited)
        out = srv.deposit_flows(chain_filter="eth")
        assert "-32005" in out["chains"]["eth"]["error"]

    def test_evm_configured_happy(self, mock_chain):
        out = srv.deposit_flows(chain_filter="arb")
        arb = out["chains"]["arb"]
        assert arb["count"] == 1
        assert arb["transfers"][0]["block"] == 39_999_995

    def test_bad_chain_raises(self):
        with pytest.raises(ValueError, match="chain"):
            srv.deposit_flows(chain_filter="polygon")

    def test_solana_failure_degrades(self, mock_chain, monkeypatch):
        def boom(limit=20, vault=None):
            raise chain.RpcError("solana rpc 429", kind="rpc-limit")
        monkeypatch.setattr(chain, "solana_vault_signatures", boom)
        out = srv.deposit_flows(chain_filter="solana")
        sol = out["chains"]["solana"]
        assert sol["error"] and sol["source"] == "solana"


class TestAccountView:
    def test_privacy_empty_honest(self, mock_chain):
        out = srv.account_view(
            "0x128463a60784c4d3f46c23af3f65ed859ba87974")
        assert out["error"]
        assert out["source"] == "tapi"
        assert "privacy" in out["reason"].lower()
        assert out["raw_empty"] is True
        assert out["method"] == "aster_getBalance"

    def test_public_account_data(self, mock_chain, monkeypatch):
        monkeypatch.setattr(chain, "tapi_call",
                            lambda method, address, extra=None:
                            TAPI_PUBLIC)
        out = srv.account_view(
            "0x128463a60784c4d3f46c23af3f65ed859ba87974")
        assert "result" in out and not out.get("error")
        assert out["data"] == "balance"

    def test_data_kinds(self, mock_chain, monkeypatch):
        seen = []

        def spy(method, address, extra=None):
            seen.append(method)
            return []
        monkeypatch.setattr(chain, "tapi_call", spy)
        srv.account_view("0x" + "ab" * 20, data="openOrders")
        srv.account_view("0x" + "ab" * 20, data="userFills")
        srv.account_view("0x" + "ab" * 20, data="spotBalance")
        assert seen == ["aster_openOrders", "aster_userFills",
                        "aster_spotGetBalance"]


    def test_open_orders_32603_maps_to_privacy(self, mock_chain,
                                               monkeypatch):
        def boom(method, address, extra=None):
            raise chain.RpcError(
                "tapi rpc error on aster_openOrders: -32603 "
                "Internal error", kind="rpc-http", code=-32603)
        monkeypatch.setattr(chain, "tapi_call", boom)
        out = srv.account_view("0x" + "ab" * 20, data="openOrders")
        assert out["error"] and out["source"] == "tapi"
        assert "privacy" in out["reason"].lower()
        assert out["raw_empty"] is True

    def test_bad_data_kind_raises(self):
        with pytest.raises(ValueError, match="data"):
            srv.account_view("0x" + "ab" * 20, data="positions")

    def test_bad_address_raises(self):
        with pytest.raises(ValueError, match="address"):
            srv.account_view("not-an-address")

    def test_rpc_failure_error_dict(self, mock_chain, monkeypatch):
        def boom(method, address, extra=None):
            raise chain.RpcError("tapi rpc error: -32601 method not found",
                                 kind="rpc-http", code=-32601)
        monkeypatch.setattr(chain, "tapi_call", boom)
        out = srv.account_view("0x" + "ab" * 20)
        assert out["error"] and out["source"] == "tapi"
        assert out["reason"] == "rpc failure (rpc-http)"


class TestMarkIndexDivergence:
    def test_spread_ranking(self, mock_rest):
        out = srv.mark_index_divergence(limit=5)
        spreads = [r["abs_spread_bps"] for r in out["divergence"]]
        assert spreads == sorted(spreads, reverse=True)
        assert out["returned"] == 5

    def test_spread_value(self, mock_rest):
        out = srv.mark_index_divergence(limit=100)
        top = out["divergence"][0]
        pb = next(p for p in F_PREMIUM
                  if p["symbol"] == top["symbol"])
        exp = (float(pb["markPrice"]) - float(pb["indexPrice"])) \
            / float(pb["indexPrice"]) * 10000
        assert abs(top["spread_bps"] - exp) < 0.05

    def test_crosscheck_top3(self, mock_rest):
        out = srv.mark_index_divergence(limit=5)
        assert len(out["mark_vs_last_crosscheck"]) == 3
        cc = out["mark_vs_last_crosscheck"][0]
        assert "mark_vs_last_bps" in cc

    def test_rows_without_index_skipped(self, mock_rest):
        out = srv.mark_index_divergence(limit=100)
        assert all(r["index_price"] for r in out["divergence"])

    def test_upstream_failure(self, monkeypatch):
        def boom(symbol=None):
            raise rest.UpstreamError("fapi down", kind="network")
        monkeypatch.setattr(rest, "premium_index", boom)
        out = srv.mark_index_divergence()
        assert out["error"] and out["source"] == "fapi"


# ---------------------------------------------------------------- MCP wire

class TestBuildServer:
    def test_all_13_tools_registered(self):
        import asyncio
        mcp = srv.build_server()
        names = {t.name for t in asyncio.run(mcp.list_tools())}
        expected = {
            "market_overview", "exchange_symbols", "order_book",
            "klines", "trades", "spot_overview", "funding_overview",
            "tradfi_markets", "funding_screener", "oi_snapshot",
            "deposit_flows", "account_view", "mark_index_divergence",
        }
        assert names == expected
        assert len(names) == 13

    def test_read_only_annotations(self):
        import asyncio
        mcp = srv.build_server()
        for t in asyncio.run(mcp.list_tools()):
            ann = t.annotations or {}
            ro = (ann.get("read_only_hint") if isinstance(ann, dict)
                  else getattr(ann, "read_only_hint", None))
            de = (ann.get("destructive_hint") if isinstance(ann, dict)
                  else getattr(ann, "destructive_hint", None))
            assert ro is True and de is False, t.name

    def test_call_tool_round_trip(self, mock_rest):
        import asyncio
        mcp = srv.build_server()

        async def call():
            res = await mcp.call_tool("market_overview", {"limit": 3})
            text = res.content[0].text if res.content else "{}"
            return json.loads(text)

        out = asyncio.run(call())
        assert out["returned"] == 3


class TestNoNetwork:
    def test_offline_marker_default_deselects_online(self):
        """Acceptance 2: no online-marked tests run by default."""
        import subprocess
        import sys
        repo = str(Path(__file__).resolve().parent.parent)
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-m", "online"],
            capture_output=True, text=True, cwd=repo)
        assert ("0 selected" in out.stdout
                or "deselected" in out.stdout), out.stdout[-300:]
