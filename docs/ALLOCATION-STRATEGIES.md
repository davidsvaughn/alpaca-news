# Allocation Strategies

> Portfolio-level position management: capacity rules, replacement logic, and ranking methods.
>
> Last updated: 2026-03-12

---

## Overview

Allocation strategies control **how many positions can be open simultaneously** and
**what happens when a new signal arrives while at capacity**. They sit in the
post-processing pipeline between `run_backtest()` and the portfolio simulation.

**File:** `trader/market/backtest.py` — `apply_allocation()`, `_try_replace()`, ranking helpers.

---

## Walk-Forward Simulation

Allocation is evaluated **chronologically** (sorted by entry time). At each new signal:

1. Positions that have already exited by this entry time are evicted from the open set.
2. If capacity is available, the trade is taken.
3. If at capacity, behavior depends on the strategy's "When Full" setting.

---

## Strategies

### 1. None (Unlimited)

- **Key:** `none`
- No capital constraints. Every signal is traded independently.
- Useful as a baseline — shows raw strategy performance without portfolio effects.

### 2. Fixed Dollar Per Trade

- **Key:** `fixed_dollar`
- **Params:** `alloc_pct` (default 5%) — percentage of initial capital per trade.
- Max concurrent positions = `floor(100 / alloc_pct)` (e.g., 5% = 20 positions).
- **When full: always skip.** No replacement logic.

### 3. Max Positions

- **Key:** `max_positions`
- **Params:**
  - `max_pos` (default 10) — hard cap on concurrent open positions.
  - `when_full` — `"skip"` (default) or `"replace"` (Replace Weakest).
  - `rank_method` — ranking function used for replacement decisions (see below).
  - `composite_weight` — only used when `rank_method = "composite"`.
- **When full = Skip:** new signals are discarded (exit_reason = `"skipped"`).
- **When full = Replace Weakest:** triggers the replacement logic (see below).

### 4. Ranking-Based Reallocation

- **Key:** `ranking_realloc`
- **Params:** `alloc_pct`, `rank_method`, `composite_weight`.
- Max concurrent = `floor(100 / alloc_pct)`, same as Fixed Dollar.
- **Always replaces** when full — there is no "skip" option.
- Otherwise identical to Max Positions with Replace Weakest.

---

## Replacement Logic (`_try_replace`)

When the portfolio is full and replacement is enabled, the system decides whether to
swap out an existing position for the new signal:

1. **Score all open positions** using the selected ranking method.
2. **Score the incoming signal** using the same method.
3. **Find the weakest** (lowest-scoring) open position.
4. **Replace only if the new signal's score strictly exceeds the weakest position's score.**
   If not, the new signal is skipped (exit_reason = `"skipped"`).

When a position is replaced:
- The victim is **early-exited** at its interpolated price at the new signal's entry time.
- The victim's exit_reason is set to `"replaced"`.
- The new signal takes the victim's slot.

---

## Current Ranking Methods

### Unrealized P&L (`unreal_pl`)

Legacy alias: `momentum` (accepted for backward compatibility).

Scores each open position by its **unrealized return** at the time of the new signal:

```
score = (current_price - entry_price) / entry_price
```

**Critical detail:** A newly arriving signal has no price history, so it is scored
as **`0.0`** (zero unrealized P&L). This means:

- **Losing position** (negative P&L) → **replaced** (0.0 > negative).
- **Break-even position** (P&L = 0.0) → **NOT replaced** (0.0 is not > 0.0).
- **Winning position** (positive P&L) → **NOT replaced** (0.0 < positive).

**In practice:** if all existing positions are profitable or break-even, new signals
are skipped. New signals only displace positions that are currently underwater.

### Signal Confidence (`confidence`)

Scores each position by its **original signal confidence** (the LLM-assigned probability
at entry time). The incoming signal uses its own confidence score.

A new signal replaces the weakest position only if its confidence is strictly higher.
Unlike unrealized P&L, this comparison is between two meaningful values, so replacement
happens whenever the new signal is more confident than the least-confident open position.

### Composite (`composite`)

Blends confidence and unrealized P&L using z-score normalization:

```
score = weight * Z(confidence) + (1 - weight) * Z(unrealized_pnl)
```

- `composite_weight` controls the blend (0 = pure unrealized P&L, 1 = pure confidence).
- Requires at least 2 open positions to compute z-scores; falls back to unrealized P&L otherwise.
- The incoming signal's composite score uses its confidence component only (unrealized P&L = 0).

---

## Limitations of Current Ranking Methods

The existing methods don't answer the right question:

| Method | Problem |
|--------|---------|
| **Unrealized P&L** | Backward-looking — "how has this done so far?" is sunk cost thinking. A stock down 5% might be bottoming (good to hold); a stock up 3% might be topping (bad to hold). |
| **Signal Confidence** | Stale — the LLM's opinion at entry time doesn't update. Conditions may have changed drastically since entry. |
| **Composite** | Inherits both problems. Also, the incoming signal gets scored asymmetrically (confidence only, unrealized P&L = 0). |

What we really want: **"which position has the best prospects going forward from right now?"**

---

## Forward-Looking Ranking Methods (Implemented 2026-03-12)

### Data Availability (confirmed 2026-03-06)

The key insight that enables forward-looking ranking: **we can fetch recent bar history
for the incoming signal**, making the comparison symmetric (same scoring method for both
existing positions and the new candidate).

| Source | Intraday Lookback | Intervals | Fetch Latency | Quality |
|--------|-------------------|-----------|---------------|---------|
| **Schwab** (preferred) | 10 trading days | 1, 5, 10, 15, 30 min | 0.3–0.5s | Zero gaps, extended hours |
| **yfinance** (fallback) | 8 days (1m), 60 days (5m) | 1, 2, 5, 15, 30 min, 1h | 0.02–0.07s | Some zero-vol bars, regular hours only |
| **Finnhub** | N/A | N/A | N/A | `/stock/candle` is paywalled (403 on free tier) |

Both existing positions and the incoming signal can be scored identically using fetched
bars — no more asymmetric proxy scores.

See: [SCHWABDEV.md](src/SCHWABDEV.md), [YFINANCE.md](src/YFINANCE.md), [FINNHUB.md](src/FINNHUB.md)

### Candidate 1: Trailing Slope (`trailing_slope`)

**Concept:** Score each stock by its **recent price trajectory** — the slope of a linear
regression over the last N bars. A stock climbing into the signal moment scores high;
a stock drifting down scores low.

**Computation:**
```
bars = last N 5-min closes for the stock
score = linear_regression_slope(bars) / mean(bars)   # normalized to %/bar
```

**Parameters:**
- `lookback_bars` (default 30) — number of 5-min bars = 2.5 hours of price action
- Higher values capture longer trends; lower values react faster

**Properties:**
- Simple, fast, intuitive
- Symmetric — same computation for existing positions and incoming signal
- Replaces positions whose price is trending down with candidates trending up
- A flat stock (slope ~0) is vulnerable to replacement by any stock with positive momentum

**Backtest context:** For existing positions, use the 1-min bars already available from
the backtest data (resampled to 5-min if needed). For the incoming signal, bars need to
be fetched — this is the main latency cost.

**Live context:** Straightforward — fetch bars for all candidates at decision time.

### Candidate 2: Volume-Weighted Trend / Accumulation-Distribution (`volume_trend`)

**Concept:** Price alone doesn't tell the full story. A stock rising on declining volume
is weaker than one rising on increasing volume. Score using the slope of the
Accumulation/Distribution (AD) line.

**Computation:**
```
MFM_t = ((close - low) - (high - close)) / (high - low)   # Money Flow Multiplier [-1, +1]
AD_t  = MFM_t * volume_t                                   # Accumulation/Distribution for bar
score = linear_regression_slope(cumulative_AD, last N bars)
```

**Properties:**
- Captures buying/selling pressure that pure price doesn't show
- Distribution under stable prices (falling AD + flat price) = weak position
- Accumulation during pullbacks (rising AD + falling price) = strong position
- Requires volume data — available from both Schwab and yfinance

**Relationship to Volume Delta Divergence exit strategy:**

The existing `volume_delta_divergence` exit strategy (in `backtest.py`) also measures
volume-based momentum, but uses a different formula — the **inter-bar tick rule**:

```
If close > prev_close → entire bar volume = uptick
If close < prev_close → entire bar volume = downtick
cum_delta = running sum of (uptick - downtick)
```

This is a binary classification — all-or-nothing per bar. The AD Money Flow Multiplier
above is more nuanced: it distributes each bar's volume proportionally based on where
the close falls within the high-low range (e.g., close near the high → mostly buying
pressure, close near the low → mostly selling pressure).

For a full comparison of all three volume delta formulas (inter-bar tick rule, AD Money
Flow Multiplier, and tick-level), see
[LIVE-TRADING.md — Three Volume Delta Formulas](LIVE-TRADING.md#three-volume-delta-formulas).

**Decision needed:** For the ranking method, we could use either:
- **AD formula** (more nuanced, independent signal from the exit strategy)
- **Inter-bar tick rule** (simpler, consistent with existing `_compute_volume_delta()`)
- **Both as options** (let the user pick via a parameter)

### Candidate 3: RSI at Current Time (`rsi_current`)

**Concept:** RSI measures momentum exhaustion. Positions with RSI > 70 may be overbought
(running out of steam); positions with moderate RSI (~50–65) may have more room to run.

**Computation:**
```
rsi = standard RSI(14) computed on 5-min bars up to current time
score = 100 - rsi   # invert: lower RSI = more room to run = higher score
```

**Properties:**
- Captures "how much gas is left" rather than direction
- Overbought positions (RSI > 70) get low scores — vulnerable to replacement
- Candidate stocks with moderate RSI score higher — more upside potential
- Well-understood indicator, easy to explain

**Caveat:** RSI is mean-reverting by nature. In strong trends, RSI stays overbought for
a long time. May prematurely eject winning positions in strong uptrends.

### Candidate 4: Composite Technical Score (`tech_score`)

**Concept:** Combine multiple signals into a single forward-looking health score.

**Possible formula:**
```
score = w1 * norm(trailing_slope) + w2 * norm(ad_slope) + w3 * norm(inverted_rsi)
```

**Properties:**
- More robust than any single indicator
- Can be tuned via weights
- Higher implementation complexity
- Weights: 0.4 slope + 0.3 A/D slope + 0.3 inverted RSI (z-score normalized)

### Anti-Churn Guard: `replace_min_margin`

**Added 2026-03-12** — prevents the "death churn" where positions slightly underwater
get replaced by marginally better signals in rapid succession.

- **Parameter:** `replace_min_margin` (default 0.0, range 0–1)
- **Effect:** `new_score > worst_score + replace_min_margin` (must exceed by margin)
- Available in both `max_positions` and `ranking_realloc` allocation params
- A value of 0.05 means the new signal must score at least 0.05 better than the worst position

**Origin:** Discovered 2026-03-12 when comparing two parallel portfolios. The Alpaca portfolio
churned through 12 replacement exits totaling -$745, while the non-Alpaca portfolio (which
accidentally had replacements disabled due to a scoring bug) gained +1.1%. See
[PORTFOLIO-DIVERGENCE.md](skills/PORTFOLIO-DIVERGENCE.md) for the full analysis.

---

## Tick Collector Integration (Live Only, 2026-03-12)

All forward-looking ranking methods benefit from tick-level data when the tick_collector
service is running. The live scoring path follows this priority:

1. **Tick collector** (`tick_collector/vdd.py` → `get_vdd_bars()`): 5-min buckets with
   Lee-Ready classified uptick/downtick volume. Higher quality than bar-based methods.
2. **Schwab 1-min bars** (fallback): Standard OHLCV resampled to 5-min.

| Method | Tick Data Advantage |
|--------|-------------------|
| `trailing_slope` | 30s bucket closes = more responsive slope |
| `volume_trend` | Lee-Ready classified delta vs bar-based A/D formula |
| `rsi_current` | Higher resolution RSI from sub-minute closes |
| `tech_score` | Benefits from all above |

**Backtest** always uses bar-based computation (tick data not available historically).

---

## Stats

`apply_allocation()` returns counts: `taken` (accepted), `skipped` (at capacity),
`replaced` (victim early-exited to make room). These appear in the backtest summary.

---

## Implementation Files

| File | Role |
|------|------|
| `trader/market/backtest.py` | Constants, `BacktestResult` fields, ranking functions, feature computation, `_try_replace()`, anti-churn |
| `trader/online/live_monitor.py` | Live scoring with tick/bar fallback, `_find_replacement_victim()`, `replace_min_margin` |
| `tick_collector/vdd.py` | `get_vdd_bars()` — tick-based OHLCV + classified volume for live ranking |
| `trader/market/data_service.py` | `get_price_history()` — Schwab-first with yfinance fallback |
| `trader/market/schwab_client.py` | `get_intraday_candles()` — primary bar data source |
| `trader/web/templates/snapshots.html` | Backtest panel UI — allocation selector, parameter inputs (auto-populated) |

---

## See Also

- [BACKTEST-ARCHITECTURE.md](BACKTEST-ARCHITECTURE.md) — System overview, job flow, frontend state
- [BACKTEST-STRATEGIES.md](BACKTEST-STRATEGIES.md) — Exit strategies (what triggers position close)
- [BACKTEST-METRICS.md](BACKTEST-METRICS.md) — Performance metrics and portfolio simulation
- [SCHWABDEV.md](src/SCHWABDEV.md) — Schwab data source details and limits
- [YFINANCE.md](src/YFINANCE.md) — yfinance data source details and limits
