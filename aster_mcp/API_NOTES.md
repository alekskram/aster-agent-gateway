# API notes - Aster public surfaces (as verified 2026-09-05/06)

Facts the gateway depends on. Every claim below was checked against the
live endpoints before v0.1.0 was coded; fixtures in `tests/fixtures/`
mirror these shapes.

## fapi - futures (https://fapi.asterdex.com/fapi/v3/*)

- Binance-compatible v3 REST, keyless for market data. ALL numerics
  arrive as STRINGS ("0.0001"); parse with a never-raising `_f()`.
- **Rate limits: weight-based per IP, 2400 weight/min** on fapi
  (header `X-MBX-USED-WEIGHT-1M` on every response). 429 -> backoff
  honoring `Retry-After`; repeated 429 -> **418 IP ban** (the client
  cools down 60s and refuses calls).
- `exchangeInfo` (weight 1): ~580 symbols; statuses ~559 TRADING /
  15 SETTLING / 6 PENDING; quotes USDT 568 / USD1 10; symbol rows
  carry `onboardDate`, `contractType`, `status`, `filters`
  (PRICE_FILTER tickSize, LOT_SIZE stepSize, MIN_NOTIONAL,
  LEVERAGE_FILTER).
- `ticker/24hr` with NO symbol param (weight ~40): ALL ~574 rows in
  ONE call - symbol, lastPrice, priceChangePercent, quoteVolume,
  volume, highPrice, lowPrice, weightedAvgPrice, count.
- `premiumIndex` no param (weight ~10): ALL ~730 rows - symbol,
  markPrice, indexPrice, lastFundingRate, nextFundingTime,
  interestRate.
- `fundingInfo` (weight 1): ~730 rows - fundingIntervalHours MIXED
  (1/2/4/8h histogram ~351/3/103/273), cap 0.003-0.03, floor -0.03,
  interestRate.
- `depth?symbol=&limit=` - weight by limit: 5/10/20/50 = 2, 100 = 5,
  500 = 10, 1000 = 20 (client snaps requests down to a priced tier).
- `klines` / `markPriceKlines` / `indexPriceKlines` -
  `?symbol=&interval=&limit=`; weight 1 (limit<=100) .. 5.
- `/trades?symbol=` - fresh keyless trades.
- `openInterest?symbol=` - per-symbol snapshot ONLY (weight 1).
  **`/futures/data/openInterestHist` returns 404** - no keyless OI
  history exists; the gateway says so instead of inventing data.

## sapi - spot (https://sapi.asterdex.com/api/v3/*)

- MIGRATION note: the old `/sapi/v1/*` paths **404**; live base is
  `/api/v3/*`.
- Rate limit 6000 weight/min (separate bucket; same header).
- `exchangeInfo`: 68 TRADING pairs plus TEST*-named junk symbols -
  filtered by name in this gateway (flag to include).
- `klines`, `depth`: live 200, keyless.
- Spot `/trades`: path was uncertain at recon; probed in the smoke
  script - if it 404s the tool degrades to an honest error dict.

## tapi - Aster Chain JSON-RPC (https://tapi.asterdex.com/info)

- Body `{"jsonrpc":"2.0","id":1,"method":"aster_getBalance","params":
  ["0x...", "latest"]}` (PARAMS IS AN ARRAY: address + blockTag; the
  object form `{"userAddress": ...}` returns HTTP 400 - live-verified)
  - answers KEYLESS, but **account privacy hides most accounts**:
  private accounts return a result with only {address,
  accountPrivacy: "enabled"} and no balances (observed on the BSC vault
  address and a burn address). Empty = privacy, not an API failure -
  the gateway returns an honest error-dict explaining why.
- `aster_openOrders` on a privacy-hidden account returns JSON-RPC
  -32603 (internal error) - also mapped to the privacy degradation
  path.
- Methods used (read-only): `aster_getBalance`, `aster_openOrders`,
  `aster_userFills` (+ `aster_spotGetBalance` /
  `aster_spotOpenOrders` / `aster_spotUserFills` variants).
- `eth_*` NOT supported (eth_chainId -> -32601 method not found): the
  Aster Chain EVM-RPC is closed.

## Solana vault (https://api.mainnet-beta.solana.com)

- `getSignaturesForAddress` for the Aster vault program
  `EhUtRgu9iEbZXXRpEvDj6n1wnQRjMi2SERDo3c6bmN2c` - live 200 keyless,
  fresh signatures (signature / slot / blockTime / err). Treated
  politely (10 req/min local cap) - it is a free public node.

## EVM vaults (BSC / ETH / ARB)

- Vaults: BSC `0x128463A60784c4D3f46c23Af3f65Ed859Ba87974`, ETH
  `0x604DD02d620633Ae427888d41bfd15e38483736E`, ARB
  `0x9E36CB86a159d479cEd94Fa05036f235Ac40E1d5`.
- Deposit flow = ERC-20 `Transfer` (topic0 `0xddf252ad...523b3ef`)
  with `to` = vault, via `eth_getLogs`.
- Free public RPCs reject even 300-block windows with **-32005
  (limit exceeded)**, so the RPC URL comes from env:
  `ASTER_EVM_RPC_URL` (generic) or `ASTER_EVM_RPC_URL_{BSC,ETH,ARB}`
  (per-chain). Unset -> honest `"no RPC configured"` error dict -
  never a traceback, never invented logs.

## Endpoints NOT used, and why

- **`/futures/data/*` history endpoints** - 404 on Aster (only
  openInterestHist was probed; the futures/data tree is absent).
- **WebSocket streams** (`fstream`/`sstream`) - deferred; the MCP
  request/response model gains little from push feeds and the TTL
  caches already cover freshness.
- **Signed/trading endpoints** - out of scope by design: this gateway
  is strictly read-only and keyless; no API keys ever enter the
  process.
- **aster-scan.com / asterscan.io explorers** - dead/unreachable at
  recon time; not a dependency.
