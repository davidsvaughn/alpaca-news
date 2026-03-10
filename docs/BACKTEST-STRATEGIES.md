# Backtest Exit Strategies

This document describes the entry signal, all implemented exit strategies, the guard system, global parameters, data sources, and indicator math used by the backtesting engine.

---

## Entry Signal — Agent/LLM Explore Pipeline

The backtesting system does not generate its own entry signals. Entries come from the **snapshot pipeline** — a multi-agent LLM system that investigates news events and produces trading recommendations.

### How Entries Are Generated

1. **News trigger** — An Alpaca Markets news websocket delivers real-time news items. When a headline mentions a publicly traded stock, it triggers the pipeline.

2. **Multi-agent investigation** — The pipeline runs a sequence of LLM agents (Grok → OpenAI → Gemini), each with access to web search, X/Twitter search, and 16 function tools for market data. Each agent investigates the news item independently and passes its findings downstream.

3. **Trading signal extraction** — The final agent produces a structured `TradingSignal` with a direction (BUY/SELL/HOLD), confidence score, and reasoning. If the signal is BUY with sufficient confidence, a **snapshot** is created and optionally a **watch** (live position tracker).

4. **Backtest entry** — Each snapshot records `symbol`, `entry_price`, and `entry_time`. These become the entries fed to the backtest engine. The entry price is the market price at the time the signal was generated.

### Current Limitations and Future Direction

The current trigger is simplistic — every Alpaca news headline for a stock fires the pipeline. This means:
- Many low-quality triggers (routine earnings, analyst notes, etc.)
- No pre-filtering for signal quality
- The pipeline's selectivity comes entirely from the LLM agents' judgment

Future improvements will include:
- Better news sources (beyond Alpaca's feed)
- Pre-filtering triggers before invoking the full pipeline (headline classification, relevance scoring)
- More selective entry criteria (higher confidence thresholds, sector filters, time-of-day filters)
- The backtest engine itself is agnostic to how entries are generated — it only needs `(symbol, entry_price, entry_time)` per trade

### Why This Matters for Backtesting

The backtest evaluates **exit strategies**, not entry strategies. It takes the pipeline's BUY signals as given and asks: "Given this set of entries, which exit strategy would have produced the best risk-adjusted returns?" The entry quality is a separate concern — improving triggers will improve the raw signal, while better exits extract more value from whatever signal exists.

---

## Data Sources

### 1-Minute OHLCV Bars

All strategies operate on **1-minute OHLCV bars** (Open, High, Low, Close, Volume). Data is fetched and cached per (symbol, date):

| Source | Priority | Coverage | Notes |
|--------|----------|----------|-------|
| **Schwab** | Primary | ~10 trading days | Via `SchwabMarketClient`, includes extended hours |
| **yfinance** | Fallback | ~7 calendar days | Fills gaps where Schwab has missing bars |

When both sources return data, Schwab takes priority and yfinance fills gaps. Bars are merged, deduplicated, and cached.

### Persistent Cache

- Location: `~/.cache/alpaca-news/ohlcv_1m/{SYMBOL}/{YYYY-MM-DD}.json`
- Completed past trading days are cached forever
- Today's data is fetched live (not cached, since the day is incomplete)
- Cache format: list of `{t, o, h, l, c, v}` dicts with tz-naive Eastern timestamps

### Timestamp Handling

All timestamps are normalized to **tz-naive US/Eastern time**:
- Schwab returns UTC → converted to Eastern, tz stripped
- yfinance returns tz-aware Eastern → tz stripped
- Cache stores tz-naive Eastern

---

## Global Parameters

These apply to all strategies and are set in the backtest panel UI.

### Market Close (`market_close`)

Controls which bars are included in the simulation.

| Value | Meaning |
|-------|---------|
| `"16:00"` | Regular hours only (9:30 AM – 4:00 PM ET) |
| `"17:30"`, `"20:00"` | Extended hours (includes after-market) |
| `null` / None | All bars (full extended hours) |

Bars outside the window are filtered out before the strategy runs. The market open is always 9:30 AM ET.

### Minimum Hold (`min_hold`)

Number of 1-minute bars to hold before exit checks begin. Default: **5 bars** (5 minutes).

This prevents immediate whipsaw exits on entry volatility. During the min_hold period, neither the primary strategy nor the guards can trigger an exit. The position is simply held.

### Price Delay (`price_delay_minutes`)

Minutes after the snapshot's `entry_time` before the backtest considers the position "entered." Default: **10 minutes**.

This models realistic execution — you can't trade at the exact moment the news hits. The entry price is taken from the bar at `entry_time + price_delay_minutes`. If the entry lands before 9:30 AM + delay (pre-market signal), the engine adjusts to use a bar further into the regular session.

Configurable via env variable `PRICE_DELAY_MINUTES`.

---

## Guard System

Guards are **global safety bounds** that wrap any primary strategy. They act as an independent stop-loss and/or take-profit layer that fires before the primary strategy's exit logic.

### Guard Stop % (`guard_stop_pct`)

    Exit if: Low_t <= entry_price × (1 - guard_stop_pct / 100)

- Checked on each bar's **Low** price (conservative — assumes worst-case fill)
- Exit reason: `guard_stop`
- **Set to 0 to disable** (no guard stop)

### Guard Target % (`guard_target_pct`)

    Exit if: High_t >= entry_price × (1 + guard_target_pct / 100)

- Checked on each bar's **High** price (conservative — assumes best-case fill)
- Exit reason: `guard_target`
- **Set to 0 to disable** (no guard target)

### Priority

On each bar, the engine checks in this order:
1. Has the primary strategy already fired on an earlier bar? → use primary result
2. Guard stop triggered? → exit immediately with `guard_stop`
3. Guard target triggered? → exit immediately with `guard_target`
4. Neither → continue to next bar

If both a guard and the primary strategy fire on the same bar, the primary strategy's result is used (it ran from an earlier starting index due to min_hold, so it fires "first" by bar count).

### Use Cases

- Set guard stop = 10%, guard target = 0% → hard 10% loss limit regardless of what the primary strategy does
- Set guard stop = 0%, guard target = 20% → lock in profits if the stock jumps 20% before the strategy exits
- Set both = 0% → guards disabled, primary strategy has full control

---

## Exit Strategies

### Section 1 — Price-Based

#### 1. Fixed % Stop Loss (`fixed_stop_loss`)

Exit if the price drops a fixed percentage below entry.

    Exit if: Low_t <= entry_price × (1 - stop_pct / 100)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `stop_pct` | 5.0 | 0.1 – 50 | Stop loss percentage |

- Uses bar **Low** for trigger (conservative: assumes fill at worst price)
- Exit price is exactly the stop level
- Exit reason: `stop`

---

#### 2. Fixed % Take Profit (`fixed_take_profit`)

Exit if the price rises a fixed percentage above entry.

    Exit if: High_t >= entry_price × (1 + reward_pct / 100)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `reward_pct` | 10.0 | 0.1 – 100 | Take profit percentage |

- Uses bar **High** for trigger
- Exit price is exactly the target level
- Exit reason: `target`

---

#### 3. Risk / Reward Target (`risk_reward_target`)

Combines a stop loss with a take profit set at a multiple of the risk.

    stop = entry_price × (1 - stop_pct / 100)
    risk = entry_price - stop
    target = entry_price + k × risk

    Exit if: Low_t <= stop  OR  High_t >= target

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `stop_pct` | 5.0 | 0.1 – 50 | Stop loss percentage |
| `risk_multiple` | 2.0 | 0.5 – 10 | Reward-to-risk ratio (k) |

- On each bar, stop is checked first (on Low), then target (on High)
- Exit reason: `stop` or `target`
- Example: stop_pct=5, k=2 → stop at -5%, target at +10%

---

### Section 2 — Trailing

#### 4. Percent Trailing Stop (`pct_trailing_stop`)

Exit if the price drops a fixed percentage below its highest point since entry.

    Track: P_max = max(High_entry, High_{entry+1}, ..., High_t)
    Exit if: Low_t <= P_max × (1 - trail_pct / 100)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `trail_pct` | 5.0 | 0.5 – 30 | Trail percentage below high-water mark |

- P_max ratchets upward — never decreases
- The stop level rises as the stock makes new highs, locking in gains
- Exit reason: `stop`

---

#### 5. ATR Trailing Stop (`atr_trailing_stop`)

Exit if the price drops k ATR units below its highest point since entry.

    Track: P_max (same as above)
    Exit if: Low_t <= P_max - k × ATR_n(t)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `atr_period` | 14 | 5 – 50 | ATR lookback period (bars) |
| `multiplier` | 2.0 | 0.5 – 5 | ATR multiplier (k) |

- Trail distance adapts to current volatility
- Higher ATR (volatile stock) → wider trail → fewer whipsaw exits
- Lower ATR (calm stock) → tighter trail → locks in gains faster
- Bars where ATR is not yet computed (insufficient history) are skipped
- Exit reason: `stop`

---

### Section 3 — Volatility-Based

#### 6. ATR Fixed Stop (`atr_fixed_stop`)

Exit if the price drops k ATR units below entry (ATR measured at entry time).

    ATR_entry = ATR_n at bar nearest to entry
    Exit if: Low_t <= entry_price - k × ATR_entry

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `atr_period` | 14 | 5 – 50 | ATR lookback period (bars) |
| `multiplier` | 2.0 | 0.5 – 5 | ATR multiplier (k) |

- Unlike the ATR trailing stop, this uses a **fixed** stop level set at entry
- ATR is measured once at entry time and does not change
- If ATR is unavailable at entry, the engine searches backward for the nearest valid value
- Exit reason: `stop`

---

### Section 4 — Trend

#### 7. Close Below Moving Average (`close_below_ma`)

Exit if the bar close falls below the simple moving average.

    MA_n = SMA of Close over n bars
    Exit if: Close_t < MA_n(t)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `ma_period` | 20 | 5 – 500 | SMA lookback period (bars) |

- Uses **Close** (not Low) for comparison — the bar must actually close below the MA
- Bars where the MA is not yet computed are skipped
- Exit price: bar's Close
- Exit reason: `signal`

---

#### 8. Moving Average Cross (`ma_cross_exit`)

Exit when the short-term SMA crosses below the long-term SMA.

    Exit if: SMA_short(t) < SMA_long(t)  AND  SMA_short(t-1) >= SMA_long(t-1)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `short_period` | 10 | 3 – 200 | Short SMA period (bars) |
| `long_period` | 50 | 10 – 500 | Long SMA period (bars) |

- Requires a **crossover** — the short MA must have been above the long MA and then drop below
- Bars where either MA is not yet computed are skipped
- Exit price: bar's Close
- Exit reason: `signal`

---

### Section 5 — Momentum

#### 9. RSI Overbought Exit (`rsi_overbought`)

Exit when RSI rises above the overbought threshold.

    Exit if: RSI_n(t) >= threshold

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `rsi_period` | 14 | 5 – 100 | RSI lookback period (bars) |
| `threshold` | 70.0 | 50 – 90 | Overbought threshold |

- Logic: the stock has run up too far, too fast — take profits
- Exit price: bar's Close
- Exit reason: `signal`

---

#### 10. MACD Bearish Cross (`macd_bearish_cross`)

Exit when the MACD line crosses below its signal line.

    MACD = EMA_fast - EMA_slow
    Signal = EMA_signal(MACD)
    Exit if: MACD(t) < Signal(t)  AND  MACD(t-1) >= Signal(t-1)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `fast_period` | 12 | 5 – 100 | Fast EMA period (bars) |
| `slow_period` | 26 | 10 – 200 | Slow EMA period (bars) |
| `signal_period` | 9 | 3 – 50 | Signal EMA period (bars) |

- Requires a **crossover**, not just MACD < Signal
- Classic momentum reversal signal
- Exit price: bar's Close
- Exit reason: `signal`

---

#### 11. ROC Reversal Exit (`roc_reversal`)

Exit when Rate of Change flips from positive to negative (momentum dies).

    ROC_n = (Close_t - Close_{t-n}) / Close_{t-n}
    Exit if: ROC_n(t) < 0  AND  ROC_n(t-1) > 0

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `roc_period` | 10 | 3 – 100 | ROC lookback period (bars) |

- Captures the moment momentum turns negative
- Exit price: bar's Close
- Exit reason: `signal`

---

#### 12. Stochastic Overbought Cross (`stochastic_overbought`)

Exit when %K crosses below %D while in the overbought zone.

    %K_raw = (Close_t - Low_n) / (High_n - Low_n) × 100
    %K = SMA_k(%K_raw)
    %D = SMA_d(%K)

    Exit if: %K(t) < %D(t)  AND  %K(t-1) >= %D(t-1)  AND  %K(t-1) > threshold

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `stoch_period` | 14 | 5 – 50 | Stochastic lookback (bars) |
| `k_smooth` | 3 | 1 – 10 | %K smoothing period |
| `d_smooth` | 3 | 1 – 10 | %D smoothing period |
| `threshold` | 80.0 | 60 – 95 | Overbought threshold |

- Three conditions must all be true: crossover + previously overbought
- More selective than plain RSI — requires both overbought level and momentum reversal
- Exit price: bar's Close
- Exit reason: `signal`

---

#### 13. ADX Trend Decay (`adx_trend_decay`)

Exit when trend strength drops from strong to weak (trend is fading).

    Exit if: ADX(t) < weak_threshold
         AND max(ADX over recent lookback bars) > strong_threshold

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `adx_period` | 14 | 5 – 50 | ADX period (bars) |
| `weak_threshold` | 20.0 | 10 – 30 | Below this = weak trend |
| `strong_threshold` | 30.0 | 20 – 50 | Above this = strong trend |
| `lookback` | 10 | 5 – 50 | How far back to check for recent strength |

- Logic: ADX was recently strong (>30) but has now decayed below 20 — the trend that justified the entry is gone
- Exit price: bar's Close
- Exit reason: `signal`

---

### Section 6 — Volume

#### 14. Volume Fade Exit (`volume_fade`)

Exit if volume drops below a fraction of the rolling average (buying interest drying up).

    AvgVol_n = SMA of Volume over n bars
    Exit if: Volume_t < α × AvgVol_n(t)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `vol_lookback` | 20 | 5 – 200 | Volume SMA lookback (bars) |
| `multiplier` | 0.5 | 0.1 – 1.0 | Volume threshold fraction (α) |

- Logic: if volume dries up, the move is losing steam
- α = 0.5 means exit when volume is less than half the recent average
- Exit price: bar's Close
- Exit reason: `signal`

---

#### 15. Volume Delta Divergence (`volume_delta_divergence`)

Exit when price makes a new rolling high but cumulative volume delta is declining — buyers are losing conviction.

    delta_t = +Volume_t if Close_t > Close_{t-1}, else -Volume_t
    Δ_cum = cumulative sum of delta_t

    Exit if: Close_t >= max(Close over lookback window)
         AND Δ_cum(t) < Δ_cum(t - lookback)

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `lookback` | 30 | 10 – 100 | Lookback window (bars) |

- Uses the **inter-bar tick rule**: each bar's entire volume is classified as uptick or downtick based on whether Close rose or fell vs the prior bar. If Close is unchanged, the prior direction carries forward.
- **Live trading uses this same code path** — `live_monitor.py` calls `evaluate_exit()` from `backtest.py` once per minute. See [VOLUME-DELTA-REALTIME.md § Current Status](VOLUME-DELTA-REALTIME.md#current-status-whats-wired-up-today) for details on what's wired up and how it compares to the tick-level shadow collector.
- Divergence between price highs and volume delta is a classic distribution signal
- Exit price: bar's Close
- Exit reason: `signal`

---

#### 16. Volume Imbalance Flip (`volume_imbalance_flip`)

Exit when the rolling volume imbalance ratio flips from bullish to bearish.

    Imbalance_n = (Σ uptick_vol - Σ downtick_vol) / (Σ uptick_vol + Σ downtick_vol)
                  over rolling window of n bars

    Exit if: Imbalance(t) < -α  AND  max(Imbalance over window) > α

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `window` | 30 | 10 – 100 | Rolling window (bars) |
| `threshold` | 0.05 | 0.01 – 0.20 | Imbalance threshold (α) |

- Also uses the inter-bar tick rule for uptick/downtick classification
- Requires a **flip**: imbalance was recently bullish (>α) and is now bearish (<-α)
- α = 0.05 means a 5% net volume shift is significant
- Exit price: bar's Close
- Exit reason: `signal`

---

### Section 7 — Time-Based

#### 17. Max Holding Period (`max_holding_period`)

Exit after a fixed number of bars regardless of price action.

    Exit if: bars_held >= max_bars

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `max_bars` | 390 | 1 – 3900 | Maximum bars to hold |

- 390 bars = 1 trading day (6.5 hours × 60 min)
- 3900 bars = 10 trading days
- Exit price: Close of the bar at max_bars
- Exit reason: `time`
- If data runs out before max_bars, the trade is marked `still_open`

---

## Exit Reasons

Every backtest result includes an `exit_reason` explaining why the trade was closed:

| Reason | Meaning |
|--------|---------|
| `stop` | Primary strategy stop-loss triggered |
| `target` | Primary strategy take-profit triggered |
| `signal` | Indicator-based exit signal fired |
| `time` | Max holding period reached |
| `guard_stop` | Guard stop-loss triggered |
| `guard_target` | Guard take-profit triggered |
| `still_open` | Trade has not exited — P&L computed from latest available bar |
| `no_data` | Insufficient market data to simulate the trade |
| `unknown_strategy` | Invalid strategy key provided |

---

## Indicator Reference

The following indicators are computed from 1-minute OHLCV bars. All use pandas rolling/ewm operations.

### ATR (Average True Range)

    TR_t = max(High_t - Low_t, |High_t - Close_{t-1}|, |Low_t - Close_{t-1}|)
    ATR_n = SMA(TR, n)

Measures volatility in price units. Used by strategies 5, 6, and internally by ADX.

### SMA (Simple Moving Average)

    SMA_n(t) = (1/n) × Σ Close_{t-i}  for i = 0..n-1

Used by strategies 7, 8, and as a building block for Stochastic smoothing.

### EMA (Exponential Moving Average)

    EMA_t = α × Close_t + (1-α) × EMA_{t-1}
    α = 2 / (n+1)

Used by MACD (strategy 10) and ADX computation.

### RSI (Relative Strength Index)

    delta = Close_t - Close_{t-1}
    AvgGain = SMA(positive deltas, n)
    AvgLoss = SMA(negative deltas, n)
    RS = AvgGain / AvgLoss
    RSI = 100 - 100/(1 + RS)

Range: 0–100. Above 70 is typically "overbought."

### MACD (Moving Average Convergence Divergence)

    MACD = EMA_fast - EMA_slow
    Signal = EMA_signal(MACD)

Bearish cross: MACD drops below Signal line.

### ROC (Rate of Change)

    ROC_n = (Close_t - Close_{t-n}) / Close_{t-n}

Simple momentum measure. Positive = price rising, negative = falling.

### Stochastic Oscillator

    %K_raw = (Close_t - Low_n) / (High_n - Low_n) × 100
    %K = SMA_k(%K_raw)
    %D = SMA_d(%K)

Range: 0–100. Above 80 is typically "overbought."

### ADX (Average Directional Index)

    +DM = max(High_t - High_{t-1}, 0)  (zeroed if < -DM)
    -DM = max(Low_{t-1} - Low_t, 0)    (zeroed if < +DM)
    +DI = EMA(+DM, n) / ATR_n × 100
    -DI = EMA(-DM, n) / ATR_n × 100
    DX = |+DI - (-DI)| / (+DI + (-DI)) × 100
    ADX = EMA(DX, n)

Range: 0–100. Above 25–30 indicates a strong trend; below 20 indicates no trend.

### Volume Delta (Inter-bar Tick Rule)

    If Close_t > Close_{t-1}: direction = +1
    If Close_t < Close_{t-1}: direction = -1
    If Close_t = Close_{t-1}: direction = previous direction

    uptick_vol_t = Volume_t  if direction = +1, else 0
    downtick_vol_t = Volume_t  if direction = -1, else 0

Cumulative delta = running sum of (uptick - downtick). Imbalance ratio = (Σ uptick - Σ downtick) / (Σ uptick + Σ downtick) over a rolling window.

---

## Strategy Selection Summary

| # | Strategy | Section | Key Idea | Parameters |
|---|----------|---------|----------|------------|
| 1 | Fixed % Stop Loss | Price | Hard loss limit | stop_pct |
| 2 | Fixed % Take Profit | Price | Hard profit target | reward_pct |
| 3 | Risk / Reward Target | Price | Combined stop + target at k× risk | stop_pct, risk_multiple |
| 4 | Percent Trailing Stop | Trailing | Trail below high-water mark | trail_pct |
| 5 | ATR Trailing Stop | Trailing | Volatility-adaptive trail | atr_period, multiplier |
| 6 | ATR Fixed Stop | Volatility | Fixed stop in ATR units | atr_period, multiplier |
| 7 | Close Below MA | Trend | Price falls below SMA | ma_period |
| 8 | MA Cross | Trend | Short MA crosses below long MA | short_period, long_period |
| 9 | RSI Overbought | Momentum | RSI exceeds threshold | rsi_period, threshold |
| 10 | MACD Bearish Cross | Momentum | MACD crosses below signal | fast/slow/signal_period |
| 11 | ROC Reversal | Momentum | Momentum flips negative | roc_period |
| 12 | Stochastic Overbought | Momentum | %K crosses %D in overbought zone | stoch_period, k/d_smooth, threshold |
| 13 | ADX Trend Decay | Momentum | ADX drops from strong to weak | adx_period, weak/strong_threshold, lookback |
| 14 | Volume Fade | Volume | Volume drops below average | vol_lookback, multiplier |
| 15 | Volume Delta Divergence | Volume | Price high + delta declining | lookback |
| 16 | Volume Imbalance Flip | Volume | Imbalance flips bullish → bearish | window, threshold |
| 17 | Max Holding Period | Time | Exit after N bars | max_bars |

---

## Implementation

| File | Role |
|------|------|
| `trader/market/backtest.py` | Strategy definitions, indicator helpers, walk-forward runners, guard wrapper, `run_backtest()` |
| `trader/web/app.py` | `/api/strategies/backtest` endpoint — receives entries + params, calls `run_backtest()` |
| `trader/web/templates/snapshots.html` | Backtest panel UI — strategy selector, parameter inputs, guard inputs |
| `trader/config.py` | `PRICE_DELAY_MINUTES`, `STATS_RESOLUTION_MINUTES` env parameters |
| `docs/refs/exit-strategies.md` | Reference document with full mathematical formulations |

---

## See Also

- [ALLOCATION-STRATEGIES.md](ALLOCATION-STRATEGIES.md) — Position management: capacity rules, replacement logic, ranking methods
- [BACKTEST-ARCHITECTURE.md](BACKTEST-ARCHITECTURE.md) — System overview, job flow, frontend state
- [BACKTEST-METRICS.md](BACKTEST-METRICS.md) — Performance metrics and portfolio simulation
