# Uptick/Downtick Volume: Primer & Experimental Findings

*Last updated: 2026-02-17*

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
| **Schwab Streaming** | `LEVELONE_EQUITIES` (conflated, ~1/sec) | Approximate, live only | Free (API key) | None (real-time) |
| **Finnhub `/stock/tick`** | Per-trade prints (price, vol, ms timestamp) | Yes, historical | Premium ($50+/mo) | Years |
| **yfinance WebSocket** | Price + day_volume updates (~1/sec) | Approximate, live only | Free | None (real-time) |

Note: Schwab's `TIMESALE_EQUITY` service (true per-trade prints) exists in the Schwab API but is **not supported** in the schwabdev Python library. The `LEVELONE_EQUITIES` service provides conflated updates with `last_price` and `total_volume` at ~1-second intervals.

**Bottom line:** We can't get historical tick data for free. We can approximate from 1-minute OHLCV bars (yfinance or Schwab REST), or use real-time streaming (yfinance WebSocket or Schwab LEVELONE_EQUITIES) for ~1-second granularity during market hours.

---

## Part 1: Historical Approximation from 1-Minute Bars

### Methods Tested

We implemented five methods that estimate uptick/downtick volume from 1-minute OHLCV bars. All five conserve total volume (uptick + downtick = bar volume).

#### Method 1: Close Position Formula

The most commonly cited approach online. Estimates buying/selling pressure based on where the close falls within the bar's high-low range.

```
buy_volume  = volume × (close - low) / (high - low)
sell_volume = volume × (high - close) / (high - low)
```

If close equals the high, all volume is classified as buying. If close equals the low, all volume is selling. Doji bars (high == low) are split 50/50.

**Weakness:** Only considers intra-bar position, ignores bar-to-bar context entirely.

#### Method 2: Body Delta

Uses the open-to-close range relative to the high-low range.

```
delta = volume × (close - open) / (high - low)
```

Positive delta = net buying, negative = net selling. The magnitude depends on the body size relative to the wicks.

**Weakness:** Wide-wick bars with small bodies produce near-zero delta regardless of actual pressure.

#### Method 3: Inter-bar Tick Rule

The classic tick test applied at the bar level. Assigns the entire bar's volume as uptick or downtick based on whether this bar's close is above or below the previous bar's close.

```
if close > prev_close → uptick_volume += bar_volume
if close < prev_close → downtick_volume += bar_volume
if close == prev_close → use last known direction
```

**Strength:** Captures the actual price trajectory bar-to-bar. Not fooled by intra-bar noise.

#### Method 4: Intra-bar Direction

Assigns entire bar volume based on bar color (close vs open).

```
if close > open (green bar) → uptick_volume += bar_volume
if close < open (red bar)   → downtick_volume += bar_volume
if close == open (doji)     → split 50/50
```

**Weakness:** A green bar in a downtrend still counts as uptick. No context from surrounding bars.

#### Method 5: Hybrid

Combines Close Position Formula with inter-bar direction weighting. Starts with the Close Position split, then adjusts ±20% based on whether the bar closed higher or lower than the previous bar.

**Weakness:** Added complexity doesn't consistently improve results.

### Results

**Setup:** yfinance 1-minute OHLCV bars, `period="5d"`, tested 2026-02-16 (data covers Feb 9–13, 2026).

**Metrics:**
- **Correlation with price:** Pearson correlation between cumulative delta and close price over the full period. Higher = method tracks price movement better.
- **Direction correct:** Does the sign of net delta match the sign of the price change?
- **Volume conservation:** Uptick + downtick should equal total volume (all methods achieved 1.0000).

#### SPY (S&P 500 ETF) — 1,950 bars, -1.04%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| intrabar_direction | **0.9763** | OK |
| interbar_tick | 0.9760 | OK |
| body_delta | 0.9750 | OK |
| hybrid | 0.9541 | OK |
| close_position | 0.7824 | **WRONG** |

Close position got the overall direction wrong on SPY despite a clear -1.04% decline.

#### AAPL — 1,950 bars, -7.55%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| close_position | **0.8179** | OK |
| body_delta | 0.8143 | OK |
| hybrid | 0.8170 | OK |
| intrabar_direction | 0.8121 | OK |
| interbar_tick | 0.8098 | OK |

Strong directional move. All methods agreed. Correlations clustered tightly around 0.81.

#### NVDA — 1,950 bars, -1.45%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| close_position | **0.8535** | OK |
| intrabar_direction | 0.8035 | OK |
| hybrid | 0.8146 | OK |
| body_delta | 0.7752 | OK |
| interbar_tick | 0.7428 | OK |

All methods agreed on direction. Close position performed best here, but the margin was modest.

#### TSLA — 1,950 bars, +1.82%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| **interbar_tick** | **0.6303** | **OK** |
| hybrid | 0.4877 | WRONG |
| close_position | 0.4071 | WRONG |
| intrabar_direction | 0.3521 | WRONG |
| body_delta | 0.2888 | WRONG |

**Critical test case.** Only `interbar_tick` correctly identified the bullish direction on this volatile stock with a small positive move. All four other methods got the direction wrong.

#### AMZN — 1,950 bars, -3.47%

| Method | Corr w/ Price | Direction |
|--------|:---:|:---:|
| body_delta | **0.9653** | OK |
| hybrid | 0.9521 | OK |
| close_position | 0.9505 | OK |
| intrabar_direction | 0.9304 | OK |
| interbar_tick | 0.9257 | OK |

Clean trending move. All methods agreed. High correlations across the board.

#### Summary Table

| Method | SPY | AAPL | NVDA | TSLA | AMZN | Direction Errors |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|
| **interbar_tick** | 0.976 | 0.810 | 0.743 | **0.630** | 0.926 | **0/5** |
| body_delta | 0.975 | 0.814 | 0.775 | 0.289 | **0.965** | 1/5 |
| hybrid | 0.954 | 0.817 | 0.815 | 0.488 | 0.952 | 1/5 |
| intrabar_direction | **0.976** | 0.812 | 0.804 | 0.352 | 0.930 | 1/5 |
| close_position | 0.782 | **0.818** | **0.854** | 0.407 | 0.951 | 2/5 |

### Historical Approximation Conclusions

1. **Inter-bar Tick Rule is the most robust.** Only method with 0/5 direction errors. The sole correct method on TSLA (volatile stock, small move). It doesn't always have the highest correlation, but it never fails catastrophically.

2. **Close Position Formula is overrated.** Most commonly recommended online, but had the most direction errors (2/5) and lowest correlation on SPY. Works on clean trending stocks but fails when intra-bar noise is high relative to the actual move.

3. **Volatility is the differentiator.** On trending stocks (AAPL -7.55%, AMZN -3.47%), all methods perform similarly. Differences emerge on volatile stocks with small moves (TSLA +1.82%, SPY -1.04%).

4. **Divergence detection works.** Tested on SPY: found 6% bearish divergences (price up, delta down) and 17% bullish divergences (price down, delta up) in 15-bar rolling windows. Volume imbalance at price extremes was directionally correct (positive delta near day highs, negative near day lows).

---

## Part 2: Real-Time Streaming (Live Market Test)

### Setup

Tested during market hours on 2026-02-17. Two concurrent streams captured simultaneously:

| Stream Source | Update Rate | Timestamp Precision | Auth Required |
|--------------|:-----------:|:-------------------:|:---:|
| **yfinance WebSocket** | ~60/min per symbol | Seconds | No |
| **Schwab LEVELONE_EQUITIES** | ~58/min per symbol | Milliseconds | API key |

Both use the same **delta-volume tick rule** algorithm:
1. Track `prev_price` and `prev_day_volume` per symbol
2. On each update: `dv = day_volume - prev_day_volume` (volume since last update)
3. If `price > prev_price` → uptick_vol += dv
4. If `price < prev_price` → downtick_vol += dv
5. If `price == prev_price` → use last known direction

### Schwab Streaming Notes

Schwab's `LEVELONE_EQUITIES` uses **conflated delivery** — only changed fields are sent. Key fields:
- Field 3: `last_price`
- Field 8: `total_volume` (cumulative session volume)
- Field 9: `last_size` (not used — `total_volume` deltas are more reliable)

Messages arrive as JSON strings (not pre-parsed dicts) and require `json.loads()`. Since only changed fields are sent, the handler must merge partial updates into a running state per symbol before feeding the accumulator.

The schwabdev Python library does **not** support `TIMESALE_EQUITY` (true per-trade prints). Only `LEVELONE_EQUITIES` is available, which delivers conflated updates at ~1-second intervals.

### Results: 120-Second Window (SPY, AAPL, NVDA)

```
Symbol   Source                  Uptick Vol   Downtick Vol   Net Delta   Imbalance
─────────────────────────────────────────────────────────────────────────────────
SPY      yfinance WebSocket         158,991        112,328     +46,663     +0.172
SPY      Schwab LEVELONE            132,522        129,585      +2,937     +0.011
SPY      1-min bars (full day)   31,641,523     29,462,404  +2,179,119     +0.036
         Direction:               ALL AGREE → BULL

AAPL     yfinance WebSocket          51,092        109,666     -58,574     -0.364
AAPL     Schwab LEVELONE             46,686        110,200     -63,514     -0.405
AAPL     1-min bars (full day)   16,419,162     10,171,288  +6,247,874     +0.235
         Direction:               DISAGREE (streams=BEAR, bars=BULL)

NVDA     yfinance WebSocket         245,528        300,248     -54,720     -0.100
NVDA     Schwab LEVELONE            298,773        222,078     +76,695     +0.147
NVDA     1-min bars (full day)   51,830,407     76,124,277 -24,293,870     -0.190
         Direction:               DISAGREE (yf=BEAR, schwab=BULL, bars=BEAR)
```

### Stream Tick Log (SPY, Schwab LEVELONE_EQUITIES)

```
Time              Price        DayVol         dV    Dir
19:52:44.903     683.59    60,994,502      1,230     UP
19:52:45.948     683.62    60,997,769      3,267     UP
19:52:46.993     683.68    61,000,516      2,747     UP
19:52:48.039     683.66    61,003,634      3,118   DOWN
19:52:49.082     683.64    61,005,516      1,882   DOWN
19:52:50.127     683.64    61,009,449      3,933      =
19:52:51.172     683.62    61,010,795      1,346   DOWN
  ...
19:54:40.986     683.99    61,248,518      2,089     UP
19:54:42.031     683.94    61,249,853      1,335   DOWN
19:54:43.075     683.85    61,255,379      5,526   DOWN
```

### Stream Quality Comparison

| Metric | yfinance WebSocket | Schwab LEVELONE_EQUITIES |
|--------|:---:|:---:|
| Updates/2min (SPY) | 125 | 115 |
| Mean interval | 0.98s | 1.05s |
| **Interval consistency** | **Poor** (-3s to +4s) | **Excellent** (1.0–1.3s) |
| Timestamp precision | Seconds | Milliseconds |
| Out-of-order updates | Yes (negative intervals seen) | Never observed |
| Auth required | No | API key |
| Cost | Free | Free |

### Streaming Conclusions

1. **Both streams work** for real-time uptick/downtick volume via the delta-volume approach. The `TickAccumulator` class handles both data sources identically.

2. **Schwab has superior data quality.** Update intervals are highly consistent (1.0–1.3s standard deviation). yfinance timestamps are second-granularity with occasional out-of-order delivery (negative intervals), likely due to Yahoo's CDN-based distribution.

3. **Streams capture local direction, not full-day direction.** A 2-minute window can show BEAR while the full day is BULL (AAPL: local pullback during a +3.5% day). This is expected and useful — it shows *current* pressure, not historical.

4. **yfinance and Schwab can disagree on short windows.** NVDA showed opposite directions between the two streams over the same 2-minute window. This happens because each data source reports a slightly different "last price" at each update, leading to different tick classifications. Over longer windows, they should converge.

5. **Neither stream is true tick data.** Both conflate multiple trades into ~1-second updates. All volume between updates is assigned to the direction of the final price change, which can misclassify volume when the price oscillated within that second. This is an inherent limitation of conflated feeds.

---

## Overall Conclusions

### Recommended Approaches

**For historical analysis** (e.g., scanning for divergences in recent days):

Use `interbar_tick` on 1-minute OHLCV bars from yfinance (free, 7-day lookback) or Schwab REST (~30-day lookback).

```python
def interbar_tick_rule(df):
    prev_close = df["Close"].shift(1)
    direction = np.sign(df["Close"] - prev_close)
    direction = direction.replace(0, np.nan).ffill().fillna(0)
    uptick_vol = df["Volume"].where(direction > 0, 0)
    downtick_vol = df["Volume"].where(direction < 0, 0)
    return uptick_vol, downtick_vol
```

**For real-time monitoring** (e.g., current pressure during a position):

Use the `TickAccumulator` with either yfinance WebSocket (free, no API key) or Schwab LEVELONE_EQUITIES (better data quality, needs API key). Feed it `(price, day_volume)` updates and read `imbalance` / `net_delta` at any time.

### What It Can't Do

- **Not a standalone signal.** Combine with VWAP, relative volume, and price structure.
- **Not suitable for backtesting** without historical tick data (Finnhub premium or similar).
- **Not accurate at session boundaries.** Volume resets and auction prints introduce noise at open/close.
- **Conflated feeds lose information.** Multiple trades at different prices within a 1-second window get assigned to one direction.

### Possible Next Steps

- **Finnhub premium evaluation:** $50/mo gets true historical tick data — ground truth to calibrate approximation methods
- **Integration:** Add `interbar_tick` to the market data service as a derived signal for the agent pipeline
- **Streaming accumulator in production:** Run `TickAccumulator` on the existing Schwab stream to provide real-time volume delta alongside other market context

## Test Code

- Historical approximation methods: [`tests/test_uptick_volume.py`](../tests/test_uptick_volume.py)
- Real-time streaming test: [`tests/test_uptick_realtime.py`](../tests/test_uptick_realtime.py)
