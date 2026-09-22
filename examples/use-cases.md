# Real Trading Problems Solved

Four scenarios on 756 funding markets and 24/7 TradFi perps — single MCP
calls, real captures from 2026-09-22, lightly shortened.

## 1. "Where are the funding extremes — properly annualized?"

**Pain:** Funding intervals differ per market (1h/4h/8h). A naive ×24×365
overstates 8h markets by 8×. You need the true annualized board.

```
→ funding_overview()

  RTXUSDT    rate +0.474%  → +4148.8%/yr  interval 1h   cap 2%   OI thin
  COTIUSDT   rate -0.289%  →  -633.4%/yr  interval 4h   floor -2%
  AIW3USDT   rate +0.235%  →  +515.4%/yr  interval 4h
  GPROUSD1   rate +0.097%  →  +105.7%/yr  interval 8h   (TradFi pre-IPO)

  count: 756 markets, ranked by true annualized rate,
  every row carries interval_hours, cap/floor and next_funding_time
```

**Why it matters:** each row's `annualized_pct` is computed from that
market's own interval — the rate that looks tame on a 1h market and the one
that looks extreme may be the same trade. Caps and floors come from the live
field names with a legacy fallback, so you also know when the rate is
capped (no infinite carry illusions).

## 2. "Trade gold and oil at 3am?"

**Pain:** TradFi is closed; the position idea is now.

```
→ tradfi_markets()

  XAUUSD1  metals         4365.01  +0.47%   vol $216.8M   funding +0.0044%/1h
  CLUSD1   energy         89.87    -2.21%   vol $74.5M    funding -0.0001%/1h  ← longs GET paid
  MUUSD1   equity-single  1092.48  +4.78%   vol $54.4M    funding 0
  SPCXUSD1 equity-index   154.34   +1.34%   vol $26.3M    funding +0.0045%/1h

  32 TradFi perps: metals, energy, equity singles/indices — 24/7,
  next to crypto in one consistent API
```

**Why it matters:** this is the differentiator — TradFi exposure on crypto
 rails with crypto-style funding analytics. CLUSD1's negative funding means
 the carry pays the long side: an unusual TradFi posture worth seeing at a
 glance.

## 3. "Which markets are dislocated from their index?"

**Pain:** Perp mark drifting from index = squeeze fuel or stale oracle.

```
→ mark_index_divergence()

  RTXUSDT   mark 0.7456   index 0.7098   spread +505 bps   ← 5% rich
  MEMESUSDT mark 0.000652 index 0.000679 spread -397 bps
  BAYUSDT   spread +295 bps   ZCATUSDT +193 bps   XIAOMIUSDT +163 bps

  all 756 markets ranked by |spread_bps|
```

**Why it matters:** RTXUSDT simultaneously tops the funding board and the
divergence board — that coincidence (hot funding + 5% mark premium) is the
classic pre-squeeze signature. One ranking, the whole venue.

## 4. "What is the real open interest?"

**Pain:** Most gateways guess OI or serve stale aggregates.

```
→ oi_snapshot()

  ETHUSDT  96,718 ETH    XAUUSD1  17,040 (gold!)   CLUSD1 271,821 (oil)
  BTCUSDT  5,854 BTC     ASTERUSDT 67.2M           DOGEUSDT 49.2M

  per-symbol fresh calls (max 10 per invocation), each row age-stamped
```

**Why it matters:** the tool is honest about its own limits: keyless OI
history does not exist on Aster (`openInterestHist` 404s), so it serves
fresh per-symbol snapshots with `age_seconds` — and says so in every
response, instead of pretending to have a history it cannot get.
