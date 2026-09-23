# aster-agent-gateway

<!-- mcp-name: io.github.alekskram/aster-agent-gateway -->

[![CI](https://github.com/alekskram/aster-agent-gateway/actions/workflows/tests.yml/badge.svg)](https://github.com/alekskram/aster-agent-gateway/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/aster-agent-gateway.svg)](https://pypi.org/project/aster-agent-gateway/)
[![PyPI downloads](https://img.shields.io/pypi/dm/aster-agent-gateway?label=downloads)](https://pypi.org/project/aster-agent-gateway/)
[![MCP Catalog](https://img.shields.io/badge/MCP_Catalog-glama.ai-4f46e5)](https://glama.ai/mcp/servers/alekskram/aster-agent-gateway)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

Aster lists roughly 580 futures, and about 32 of them are things you would not expect on a crypto DEX: gold, oil, treasuries, single equities and index perps that trade 24/7 while the underlying market sleeps. This MCP server puts the whole board, plus ~68 spot pairs, funding, order books, klines and vault deposit flows, in front of an AI agent. Read-only and keyless; public endpoints only (`fapi.asterdex.com/fapi/v3`, `sapi.asterdex.com/api/v3`, `tapi.asterdex.com/info`, Solana RPC, optional EVM RPCs), cached and rate-limited.

## Use cases

Funding first, because this is where Aster is different. Intervals vary per market, 1h to 8h; annualize naively and an 8h market reads 8x too cold. The screener computes every row from its own interval and shows cap/floor next to it, so a 4,148%/yr print comes with the context that explains it.

Then the TradFi board at 3am: gold at $4,365 with live volume, oil paying longs to hold (negative funding, -0.0001%/1h when we looked), Micron up 4.8% overnight. One API, same conventions as the crypto rows.

`mark_index_divergence` ranks all ~756 markets by mark-vs-index spread. The morning we captured it, RTXUSDT topped both that board (505 bps rich) and the funding board at once, which is the classic pre-squeeze picture. Vault deposit flows show who parks capital where; OI snapshots are per-symbol and honest about the fact that Aster has no keyless OI history.

Full walkthroughs: [examples/use-cases.md](examples/use-cases.md).

## Quickstart

**Claude Code:**
```bash
claude mcp add aster -- uvx aster-agent-gateway
```

stdio (default, for local agents):

```bash
uvx aster-agent-gateway
```

or from a checkout:

```bash
git clone https://github.com/alekskram/aster-agent-gateway
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
      "args": ["aster-agent-gateway"]
    }
  }
}
```

Hosted form, streamable HTTP on port **8904**:

```bash
uvx aster-agent-gateway --http             # 127.0.0.1:8904
curl http://127.0.0.1:8904/health   # -> {"ok": true, "service": "aster-agent-gateway", ...}
```

<details>
<summary><b>Codex</b> (~/.codex/config.toml)</summary>

```toml
[mcp_servers.aster]
command = "uvx"
args = ["aster-agent-gateway"]
```
</details>

<details>
<summary><b>ZCode</b>: register the server (copy-paste)</summary>

```bash
# 1) start the gateway (keep it running)
uvx aster-agent-gateway --http --port 8904 &

# 2) register it (merges into ~/.zcode/cli/config.json)
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.zcode/cli/config.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
cfg = json.load(open(p)) if os.path.exists(p) else {}
cfg.setdefault("mcp", {}).setdefault("servers", {})["aster"] = {
    "type": "http", "url": "http://127.0.0.1:8904/mcp"}
json.dump(cfg, open(p, "w"), indent=2)
print("aster-agent-gateway registered:", p)
PY
```
</details>

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
| 11 | `deposit_flows` | `deposit_flows(chain_filter="all", limit=20)` | Solana vault signatures (keyless) + EVM vault Transfers when `ASTER_EVM_RPC_URL*` set + `deposit_stats` hourly/chain buckets. |
| 12 | `account_view` | `account_view(address, data="balance")` | tapi `aster_getBalance`/`openOrders`/`userFills` keyless for any address; privacy-empty returns an honest error dict explaining why. |
| 13 | `mark_index_divergence` | `mark_index_divergence(limit=20)` | mark vs index spread screener from ONE premiumIndex call + markPriceKlines-vs-klines crosscheck on the top 3. |

## Why a gateway and not the raw API?

Aster's `fapi`/`sapi`/`tapi` endpoints are plain REST. The traps are in the semantics:

| Raw API gives you | You would have to build |
|---|---|
| per-market funding intervals that differ (1h to 8h) | correct annualization per symbol; a naive ×24×365 overstates 8h markets by 8× |
| funding cap/floor fields under live vs legacy names | name negotiation with fallback (this gateway reads `fundingFeeCap`/`FundingFeeFloor` and degrades honestly) |
| ~580 symbols mixing crypto, TradFi 24/7 perps and spot | chain/asset-class filtering, spot index dislocation ranking, capacity filtering where OI is not published |
| three separate API surfaces (futures/spot/tapi) + Solana | one tool surface with consistent symbol handling and per-field source tags |

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

## Data notes

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


## Part of the suite

Four sibling read-only MCP gateways, one style: keyless, cached, honest degradation.

| Gateway | Focus |
|---|---|
| [dydx-agent-gateway](https://github.com/alekskram/dydx-agent-gateway) | dYdX v4: verified trader PnL, funding/OI anomaly detectors, leaderboard |
| [arcus-agent-gateway](https://github.com/alekskram/arcus-agent-gateway) | 194 tokenized US equities on Robinhood Chain: quotes, holders, whale transfers |
| [hyperliquid-agent-gateway](https://github.com/alekskram/hyperliquid-agent-gateway) | Hyperliquid: 233 perps + spot, funding carry, account risk, HyperEVM |
| **aster-agent-gateway** (you are here) | Aster DEX: ~580 futures incl. 24/7 TradFi perps, funding caps/floors |

All four are on [glama.ai](https://glama.ai/mcp/servers/alekskram/aster-agent-gateway) and PyPI; any of them installs with `uvx <name>`.

## License

MIT.
