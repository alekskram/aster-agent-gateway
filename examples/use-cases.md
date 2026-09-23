# Real Trading Problems Solved

Four scenarios on 756 funding markets and the 24/7 TradFi board. Real
captures from 2026-09-22, lightly shortened.

## 1. "Where are the funding extremes, properly annualized?"

Funding intervals differ per market, 1h to 8h. Annualize everything at ×24×365
and an 8h market reads 8x too cold; the true board needs per-market math.

```
→ funding_overview()

  RTXUSDT    rate +0.474%  → +4148.8%/yr  interval 1h   cap 2%   OI thin
  COTIUSDT   rate -0.289%  →  -633.4%/yr  interval 4h   floor -2%
  AIW3USDT   rate +0.235%  →  +515.4%/yr  interval 4h
  GPROUSD1   rate +0.097%  →  +105.7%/yr  interval 8h   (TradFi pre-IPO)

  count: 756 markets, ranked by true annualized rate,
  every row carries interval_hours, cap/floor and next_funding_time
```

Every row's `annualized_pct` uses its own interval, so a tame-looking 1h
rate and a scary-looking 8h one can be the same trade. Caps and floors are
read from the live field names with a legacy fallback, which also tells you
when a rate is sitting at its cap instead of promising infinite carry.

## 2. "Trade gold and oil at 3am?"

TradFi is closed. The idea won't wait for the open.

```
→ tradfi_markets()

  XAUUSD1  metals         4365.01  +0.47%   vol $216.8M   funding +0.0044%/1h
  CLUSD1   energy         89.87    -2.21%   vol $74.5M    funding -0.0001%/1h  ← longs GET paid
  MUUSD1   equity-single  1092.48  +4.78%   vol $54.4M    funding 0
  SPCXUSD1 equity-index   154.34   +1.34%   vol $26.3M    funding +0.0045%/1h

  32 TradFi perps: metals, energy, equity singles/indices — 24/7,
  next to crypto in one consistent API
```

This is the part no other venue offers: TradFi exposure on crypto rails with
funding analytics attached. Note CLUSD1's negative funding, meaning the carry
pays the long side. That posture is worth noticing at a glance.

## 3. "Which markets are dislocated from their index?"

A perp mark drifting from its index is either squeeze fuel or a stale oracle.
Worth knowing which one, and where.

```
→ mark_index_divergence()

  RTXUSDT   mark 0.7456   index 0.7098   spread +505 bps   ← 5% rich
  MEMESUSDT mark 0.000652 index 0.000679 spread -397 bps
  BAYUSDT   spread +295 bps   ZCATUSDT +193 bps   XIAOMIUSDT +163 bps

  all 756 markets ranked by |spread_bps|
```

RTXUSDT tops the funding board and the divergence board at the same time.
Hot funding plus a 5% mark premium is the classic pre-squeeze picture, and one
ranking over the whole venue surfaces it.

## 4. "What is the real open interest?"

Most gateways guess OI, or serve aggregates of unknown age.

```
→ oi_snapshot()

  ETHUSDT  96,718 ETH    XAUUSD1  17,040 (gold!)   CLUSD1 271,821 (oil)
  BTCUSDT  5,854 BTC     ASTERUSDT 67.2M           DOGEUSDT 49.2M

  per-symbol fresh calls (max 10 per invocation), each row age-stamped
```

The tool is upfront about its limits: keyless OI history simply doesn't
exist on Aster (`openInterestHist` 404s), so it serves fresh per-symbol
snapshots with `age_seconds` and repeats that caveat in every response,
instead of pretending to a history it can't get.
