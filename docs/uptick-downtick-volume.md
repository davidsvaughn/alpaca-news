# Uptick/Downtick Volume: Primer & Experimental Findings

*Last updated: 2026-02-16*

## What Is Uptick/Downtick Volume?

Every stock trade executes at a specific price. When that price is higher than the previous trade, it's an **uptick**. When it's lower, it's a **downtick**. Uptick volume is the total shares traded on upticks; downtick volume is the total shares on downticks.

The difference — **net delta** — tells you who's in control: buyers lifting offers (positive delta) or sellers hitting bids (negative delta). This is distinct from regular volume, which treats all shares the same regardless of direction.

## Why It Matters for Trading

### 1. Directional Pressure

Raw price movement tells you *what happened*. Volume delta tells you *how aggressively* it happened. A stock grinding up on thin uptick volume is weaker than one surging on heavy uptick volume. The delta reveals conviction behind the move.

### 2. Hidden Accumulation / Distribution

Price can stay flat while large players quietly accumulate (persistent positive delta) or distribute (persistent negative delta). This shows up in the delta before it shows up in the price — sometimes hours or days before a breakout or breakdown.

### 3. Divergences

When price makes a new high but uptick volume is weakening, buyers are losing conviction — a bearish divergence. When price makes a new low but selling pressure is fading, a bounce may be forming — a bullish divergence. These divergences are among the most actionable signals in short-term trading.

### 4. Confirmation

Volume delta can confirm or deny what price action suggests. A breakout above resistance on strong positive delta is more trustworthy than one on neutral or negative delta. This helps filter false breakouts.

### Practical Rule of Thumb

- Net uptick volume > 60% of total volume AND price above VWAP → short-term bullish bias
- Net downtick volume > 60% AND price below VWAP → short-term bearish bias

### Limitations

Volume delta is a **flow indicator**, not a valuation tool. It reflects short-term aggressive buying/selling, not fair value or earnings quality. In today's market, 60–75% of volume is algorithmic, so tick data reflects HFT liquidity provision and VWAP execution as much as directional conviction. Most useful for intraday to multi-day horizons.

---

## The Problem: We Don't Have Tick Data

True uptick/downtick volume requires **every trade print** — each individual execution with `(timestamp, price, size)`. We investigated four data sources:

| Source | Finest Granularity | True Tick Data? | Cost | Lookback |
|--------|-------------------|:-:|------|----------|
| **yfinance** | 1-min OHLCV bars | No | Free | 7 days |
| **Schwab REST** | 1-min OHLCV bars | No | Free (API key) | ~30 days |
| **Schwab Streaming** | Per-trade prints (`TIMESALE_EQUITY`) | Yes, live only | Free (API key) | None (real-time) |
| **Finnhub `/stock/tick`** | Per-trade prints (price, vol, ms timestamp) | Yes, historical | Premium ($50+/mo) | Years |
| **yfinance WebSocket** | Price + last_size updates | Approximate, live only | Free | None (real-time) |

**Bottom line:** We can't get historical tick data for free. We can approximate from 1-minute OHLCV bars (available from both yfinance and Schwab), or capture real-time tick data via streaming during market hours.

---

## Approximation Methods Tested

We implemented five methods that estimate uptick/downtick volume from 1-minute OHLCV bars. All five conserve total volume (uptick + downtick = bar volume).

### Method 1: Close Position Formula

The most commonly cited approach online. Estimates buying/selling pressure based on where the close falls within the bar's high-low range.

```
buy_volume  = volume × (close - low) / (high - low)
sell_volume = volume × (high - close) / (high - low)
```

If close equals the high, all volume is classified as buying. If close equals the low, all volume is selling. Doji bars (high == low) are split 50/50.

**Weakness:** Only considers intra-bar position, ignores bar-to-bar context entirely.

### Method 2: Body Delta

Uses the open-to-close range relative to the high-low range.

```
delta = volume × (close - open) / (high - low)
```

Positive delta = net buying, negative = net selling. The magnitude depends on the body size relative to the wicks.

**Weakness:** Wide-wick bars with small bodies produce near-zero delta regardless of actual pressure.

### Method 3: Inter-bar Tick Rule

The classic tick test applied at the bar level. Assigns the entire bar's volume as uptick or downtick based on whether this bar's close is above or below the previous bar's close.

```
if close > prev_close → uptick_volume += bar_volume
if close < prev_close → downtick_volume += bar_volume
if close == prev_close → use last known direction
```

**Strength:** Captures the actual price trajectory bar-to-bar. Not fooled by intra-bar noise.

### Method 4: Intra-bar Direction

Assigns entire bar volume based on bar color (close vs open).

```
if close > open (green bar) → uptick_volume += bar_volume
if close < open (red bar)   → downtick_volume += bar_volume
if close == open (doji)     → split 50/50
```

**Weakness:** A green bar in a downtrend still counts as uptick. No context from surrounding bars.

### Method 5: Hybrid

Combines Close Position Formula with inter-bar direction weighting. Starts with the Close Position split, then adjusts ±20% based on whether the bar closed higher or lower than the previous bar.

**Weakness:** Added complexity doesn't consistently improve results.

---

## Experimental Results

### Setup

- **Data source:** yfinance 1-minute OHLCV bars, `period="5d"`
- **Test date:** 2026-02-16 (data covers Feb 9–13, 2026)
- **Metrics:**
  - **Correlation with price:** Pearson correlation between cumulative delta and close price over the full period. Higher = method tracks price movement better.
  - **Direction correct:** Does the sign of net delta match the sign of the price change?
  - **Volume conservation:** Uptick + downtick should equal total volume (all methods achieved 1.0000).

### SPY (S&P 500 ETF) — 1,950 bars, -1.04%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| intrabar_direction | **0.9763** | OK |
| interbar_tick | 0.9760 | OK |
| body_delta | 0.9750 | OK |
| hybrid | 0.9541 | OK |
| close_position | 0.7824 | **WRONG** |

Close position got the overall direction wrong on SPY despite a clear -1.04% decline.

### AAPL — 1,950 bars, -7.55%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| close_position | **0.8179** | OK |
| body_delta | 0.8143 | OK |
| hybrid | 0.8170 | OK |
| intrabar_direction | 0.8121 | OK |
| interbar_tick | 0.8098 | OK |

Strong directional move. All methods agreed. Correlations clustered tightly around 0.81.

### NVDA — 1,950 bars, -1.45%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| close_position | **0.8535** | OK |
| intrabar_direction | 0.8035 | OK |
| hybrid | 0.8146 | OK |
| body_delta | 0.7752 | OK |
| interbar_tick | 0.7428 | OK |

All methods agreed on direction. Close position performed best here, but the margin was modest.

### TSLA — 1,950 bars, +1.82%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| **interbar_tick** | **0.6303** | **OK** |
| hybrid | 0.4877 | WRONG |
| close_position | 0.4071 | WRONG |
| intrabar_direction | 0.3521 | WRONG |
| body_delta | 0.2888 | WRONG |

**Critical test case.** Only `interbar_tick` correctly identified the bullish direction on this volatile stock with a small positive move. All four other methods got the direction wrong.

### AMZN — 1,950 bars, -3.47%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| body_delta | **0.9653** | OK |
| hybrid | 0.9521 | OK |
| close_position | 0.9505 | OK |
| intrabar_direction | 0.9304 | OK |
| interbar_tick | 0.9257 | OK |

Clean trending move. All methods agreed. High correlations across the board.

### Summary Table

| Method | SPY | AAPL | NVDA | TSLA | AMZN | Direction Errors |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|
| **interbar_tick** | 0.976 | 0.810 | 0.743 | **0.630** | 0.926 | **0/5** |
| body_delta | 0.975 | 0.814 | 0.775 | 0.289 | **0.965** | 1/5 |
| hybrid | 0.954 | 0.817 | 0.815 | 0.488 | 0.952 | 1/5 |
| intrabar_direction | **0.976** | 0.812 | 0.804 | 0.352 | 0.930 | 1/5 |
| close_position | 0.782 | **0.818** | **0.854** | 0.407 | 0.951 | 2/5 |

---

## Conclusions

### 1. Inter-bar Tick Rule Is the Most Robust

`interbar_tick` was the **only method that never got the direction wrong** across all five tickers. It was the sole correct method on TSLA, the hardest test case (volatile stock, small move). While it doesn't always have the highest correlation, it never fails catastrophically.

### 2. Close Position Formula Is Overrated

Despite being the most commonly recommended method online, `close_position` had the **most direction errors** (2/5) and the lowest correlation on SPY. It works well on clean trending stocks (NVDA, AMZN) but fails when intra-bar noise is high relative to the actual move.

### 3. Volatility Is the Differentiator

On trending stocks with low relative volatility (AAPL -7.55%, AMZN -3.47%), all methods performed similarly. The differences emerge on **volatile stocks with small moves** (TSLA +1.82%, SPY -1.04%), where intra-bar methods get confused by wide ranges that don't reflect the actual directional flow.

### 4. The Approximation Is Useful But Imperfect

Even the best method (interbar_tick) had correlations ranging from 0.63 to 0.98. The approximation works well enough to detect:
- **Divergences** between price and volume pressure (tested: 6% bearish, 17% bullish divergence windows on SPY)
- **Directional bias** over multi-bar windows
- **Volume imbalance at extremes** (positive delta near day highs, negative near day lows)

It should not be used as a standalone signal — combine with VWAP, relative volume, and price structure.

### 5. Recommended Method: `interbar_tick`

```python
def interbar_tick_rule(df):
    prev_close = df["Close"].shift(1)
    direction = np.sign(df["Close"] - prev_close)
    direction = direction.replace(0, np.nan).ffill().fillna(0)
    uptick_vol = df["Volume"].where(direction > 0, 0)
    downtick_vol = df["Volume"].where(direction < 0, 0)
    return uptick_vol, downtick_vol
```

---

## Next Steps

- **Real-time streaming test** (during market hours): Capture actual per-trade prints via Schwab `TIMESALE_EQUITY` or yfinance WebSocket and compare against the 1-minute bar approximation
- **Finnhub premium evaluation:** If true historical tick data is worth $50/mo, it would provide ground truth to calibrate the approximation methods
- **Integration:** Add the `interbar_tick` method to the market data service as a derived signal for the agent pipeline

## Test Code

All methods and tests are implemented in [`tests/test_uptick_volume.py`](../tests/test_uptick_volume.py).
