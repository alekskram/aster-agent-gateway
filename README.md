# aster-agent-gateway

Status: pre-release (v0.1.0; PyPI publication pending - repository URL
is a placeholder until the owner account publishes).

An MCP (Model Context Protocol) server that gives AI agents read-only,
keyless access to **Aster DEX** public data - ~580 futures symbols
(including 24/7 TradFi perps: metals, equity indices, energy,
treasuries), ~68 spot pairs, funding, order books, klines, vault
deposit flows and tapi account views. No API keys, no auth, no
signing, no writes: every tool reads public endpoints only
(`fapi.asterdex.com/fapi/v3`, `sapi.asterdex.com/api/v3`,
`tapi.asterdex.com/info`, `api.mainnet-beta.solana.com`, plus optional
EVM RPCs), cached and rate-limited so an enthusiastic agent cannot
hammer the upstream.

## Quickstart

stdio (default, for local agents):

```bash
uvx --from git+https://github.com/aster-agent-gateway/aster-agent-gateway aster-agent-gateway
```

or from a checkout:

```bash
git clone https://github.com/aster-agent-gateway/aster-agent-gateway
cd aster-agent-gateway
uv sync
uv run aster-agent-gateway
```

Claude Desktop / Cursor config:

```json
{
  "mcpServers": {
    "aster": {
      "command": "uvx",
      "args": ["--from",
               "git+https://github.com/aster-agent-gateway/aster-agent-gateway",
               "aster-agent-gateway"]
    }
  }
}
```

Hosted form - streamable HTTP on port **8904**:

```bash
uv run aster-agent-gateway --http            # 127.0.0.1:8904
curl http://127.0.0.1:8904/health   # -> {"ok": true, "service": "aster-agent-gateway", ...}
```

## Tools

All 13 tools are read-only (annotated `readOnlyHint: true,
destructiveHint: false`).

| # | Tool | Signature | What it does |
|---|------|-----------|--------------|
| 1 | `market_overview` | `market_overview(limit=20, sort="volume")` | Futures panel from ONE ticker/24hr ALL call joined with exchangeInfo: TRADING markets, top volumes, status counts, fresh listings (onboardDate). |
| 2 | `exchange_symbols` | `exchange_symbols(venue="futures", symbol=None, include_junk=False)` | Symbol universe with filters/precisions incl. MIN_NOTIONAL, stepSize, leverageFilter; TEST*/SETTLING junk filtered by default. |
| 3 | `order_book` | `order_book(symbol, venue="futures", depth=10)` | fapi depth / sapi api/v3 depth; limit snapped to a priced tier (weight-aware). |
| 4 | `klines` | `klines(symbol, interval="1h", limit=100, market="futures", price_type="last")` | last/mark/index klines on fapi; spot via sapi. |
| 5 | `trades` | `trades(symbol, limit=20, venue="futures")` | Fresh keyless trades; spot path probed live (honest error dict if dead). |
| 6 | `spot_overview` | `spot_overview(limit=20)` | Spot pairs from sapi ticker/24hr + exchangeInfo; TEST* junk filtered. |
| 7 | `funding_overview` | `funding_overview(limit=20, sort="rate")` | All ~730 rates from premiumIndex + fundingInfo: mixed 1/2/4/8h intervals (flagged), cap/floor, interestRate, nextFundingTime. |
| 8 | `tradfi_markets` | `tradfi_markets(limit=20, window=None)` | TradFi-perp screener by asset class (metals/equity/energy/treasuries/forex) + `tradfi_crypto_corr` sub-block: local TradFi-vs-BTC correlation from klines. |
| 9 | `funding_screener` | `funding_screener(top=10, direction="both")` | One-call ranking by annualized funding, premium, mark-index spread + `funding_regime` headroom to cap/floor. |
| 10 | `oi_snapshot` | `oi_snapshot(symbols=None, top=10)` | Per-symbol openInterest (max 10 symbols/call). No keyless OI history (404) - stated honestly. |
| 11 | `deposit_flows` | `deposit_flows(chain="all", limit=20)` | Solana vault signatures (keyless) + EVM vault Transfers when `ASTER_EVM_RPC_URL*` set + `deposit_stats` hourly/chain buckets. |
| 12 | `account_view` | `account_view(address, data="balance")` | tapi `aster_getBalance`/`openOrders`/`userFills` keyless for any address; privacy-empty returns an honest error dict explaining why. |
| 13 | `mark_index_divergence` | `mark_index_divergence(limit=20)` | mark vs index spread screener from ONE premiumIndex call + markPriceKlines-vs-klines crosscheck on the top 3. |

## Rate limits

Two REST buckets, locally enforced and weight-aware:

- **fapi 2400 weight/min**, **sapi 6000/min** (header
  `X-MBX-USED-WEIGHT-1M` read after every call). Above 80% of a
  bucket the client self-throttles; 429 backs off honoring
  `Retry-After`; a 418 (repeated 429 = IP ban) triggers a 60s refusal
  cooldown. Depth/kline limits snap to priced tiers so callers cannot
  accidentally burn weight.
- **Solana 10 req/min, tapi 30/min, EVM 20/min** local budgets
  (separate ledgers, fail-fast honest errors).

TTL caches: exchangeInfo 3600s, fundingInfo 600s, premiumIndex and
tickers 15s, depth 5s, klines 60s, trades 10s, openInterest 30s.

## Honesty and degradation

- Every numeric from the API is a STRING upstream; parsed with a
  never-raising helper - `null` always means "not available", never
  zero.
- Every upstream failure returns an error dict
  `{"error", "source", "reason"}`, never a traceback.
- **No OI history exists keyless** (`/futures/data/openInterestHist`
  404s) - the gateway says so instead of inventing data.
- **EVM vault logs** need `ASTER_EVM_RPC_URL` (or per-chain
  `ASTER_EVM_RPC_URL_{BSC,ETH,ARB}`); free public RPCs reject vault
  queries with -32005. Unset -> honest "no RPC configured".
- **tapi account privacy**: most accounts are private; empty results
  are reported as privacy, not as data.
- Cached responses carry `age_seconds` / `fetched_at` freshness
  fields.

## Offline tests

The suite runs 100% offline against schema-realistic fixtures in
`tests/fixtures/` - no network, no live API:

```bash
uv sync --dev
uv run pytest -q          # offline suite
uv run python scripts/smoke_live.py   # LIVE capture (<=10 calls), records fixtures
```

Fixtures were initially hand-shaped from the documented API shapes,
then verified/refreshed from live captures by
`scripts/smoke_live.py` (hard budget: 10 live calls). Periodic
refresh: `scripts/recorder.py` (see `deploy/` for the optional 6h
systemd timer, disabled by default).

## License

MIT.
