# Changelog

## 0.1.1 (2026-09-07)

- Fixed: funding cap/floor now read the live `fundingFeeCap` /
  `fundingFeeFloor` field names (legacy string `cap`/`floor` kept as
  fallback), so `funding_regime` headroom/regime are non-null on live
  data in `funding_overview` and `funding_screener`.
- Fixed: `spot_overview` inner-joins sapi ticker/24hr with
  exchangeInfo (status TRADING only) - the ~24k ephemeral
  `BTC_UP_DOWN_5M_*` / `EVENT_*` options rows no longer inflate the
  count (now ~68 pairs); a new `unlisted_or_not_trading_filtered`
  counter reports what was dropped.
- Fixtures refreshed from live captures (fundingInfo 732 rows in the
  new schema; sapi exchangeInfo 68 symbols; sapi ticker sampled -
  listed rows verbatim + 200 ephemeral rows).
- `scripts/smoke_live.py` budget 10 -> 13 calls: fundingInfo, sapi
  exchangeInfo and sapi ticker/24hr ALL added with live D1 regime and
  spot ephemeral-filter assertions.

## 0.1.0 (2026-09-06)

Initial release.

- Read-only, keyless MCP gateway to Aster DEX public data.
- REST client (`aster_mcp/rest.py`): weight-aware (reads
  `X-MBX-USED-WEIGHT-1M`, self-throttles at 80% of 2400/min fapi and
  6000/min sapi, 429 backoff honoring Retry-After, 418 IP-ban cooldown),
  per-endpoint TTL caches, weight-aware depth/kline limit snapping.
- On-chain client (`aster_mcp/chain.py`): tapi Aster Chain JSON-RPC
  (privacy-aware account views), Solana vault signatures, EVM vault
  Transfer logs gated on `ASTER_EVM_RPC_URL*` env (honest
  "no RPC configured" when unset).
- 13 MCP tools (`aster_mcp/server.py`): market_overview,
  exchange_symbols, order_book, klines, trades, spot_overview,
  funding_overview, tradfi_markets (with tradfi_crypto_corr),
  funding_screener (with funding_regime headroom), oi_snapshot,
  deposit_flows (with deposit_stats), account_view,
  mark_index_divergence - all annotated read-only with honest
  degradation (error dicts, never tracebacks) and freshness fields.
- Offline test suite on schema-realistic fixtures (fixtures refreshed
  from live captures by `scripts/smoke_live.py`, budget 10 calls).
- `scripts/recorder.py` live-fixture recorder + systemd deploy units
  (6h schedule, disabled by default).
