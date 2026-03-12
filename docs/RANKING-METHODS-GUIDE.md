# Ranking Methods: User Guide

> How the system decides which position to replace when the portfolio is full.
>
> Last updated: 2026-03-12

---

## When Does Ranking Matter?

Ranking only applies when **all** of these are true:

1. Your allocation strategy is **Max Positions** (with "When Full" = Replace Weakest) or **Ranking-Based Reallocation**
2. The portfolio is at capacity (all slots filled)
3. A new signal arrives

The system scores every open position and the incoming signal using the same ranking method, then asks: *"Is this new signal better than my worst holding?"* If yes, the worst holding is sold and the new signal takes its slot.

If you use **None (Unlimited)** or **Fixed Dollar** allocation, or **Max Positions with "When Full" = Skip**, ranking is never used.

---

## Method Summary

| Method | Key | Question It Answers | Data Source | Symmetric? |
|--------|-----|---------------------|-------------|------------|
| Signal Confidence | `confidence` | "How confident was the LLM at entry?" | LLM score (static) | No |
| Unrealized P&L | `unreal_pl` | "How much has this position gained/lost?" | Current price vs entry | No |
| Composite | `composite` | Blend of confidence + P&L | Both above | No |
| **Price Momentum** | `trailing_slope` | "Is the price trending up or down right now?" | Recent 5-min closes | **Yes** |
| **Accumulation/Distribution** | `volume_trend` | "Is money flowing in or out?" | OHLCV + volume | **Yes** |
| **RSI Exhaustion** | `rsi_current` | "How much gas is left in the tank?" | Recent closes (RSI-14) | **Yes** |
| **Technical Composite** | `tech_score` | Blend of slope + A/D + RSI | All above | **Yes** |

**Symmetric** means the incoming signal and existing holdings are scored with the exact same computation. The first three methods (legacy) are asymmetric — the incoming signal gets a proxy score (0.0 for P&L, its own confidence for confidence).

---

## Typical Score Ranges

Understanding the scale of each method is critical for setting `replace_min_margin` and interpreting backtest results.

| Method | Typical Range | Units | Example Scores |
|--------|--------------|-------|----------------|
| `confidence` | 0.5 – 0.95 | Probability (0–1) | Strong signal: 0.85, Weak: 0.55 |
| `unreal_pl` | -0.05 – +0.05 | Fraction (not %) | +2% gain = 0.02, -3% loss = -0.03 |
| `composite` | -2.0 – +2.0 | z-score blend | Depends on weight param |
| `trailing_slope` | -0.005 – +0.005 | % per 5-min bar | Strong uptrend: +0.003, Flat: ~0.0 |
| `volume_trend` | -1,000 – +1,000 | A/D slope (raw) | Heavy accumulation: +500, Distribution: -300 |
| `rsi_current` | 0 – 100 | Inverted RSI points | Oversold (room to run): 70, Overbought: 25 |
| `tech_score` | -2.0 – +2.0 | z-score blend | Above average: +0.8, Below: -0.5 |

**Warning**: Because these scales differ wildly, a single `replace_min_margin` value (e.g., 0.05) means completely different things for each method. See [Anti-Churn Guard](#anti-churn-guard-replace_min_margin) below.

---

## Detailed Method Descriptions

### Signal Confidence (`confidence`)

**How it works:** Each position keeps the LLM confidence score it was assigned at entry. The incoming signal uses its own confidence. The weakest position (lowest confidence) is replaced if the new signal has higher confidence.

**Strengths:**
- Simple, no market data needed
- Directly reflects the quality of the original trading signal

**Weaknesses:**
- **Stale**: The LLM's opinion at entry time never updates. Conditions may have changed.
- **Asymmetric**: Compares static historical scores against a fresh score.

**Best for:** Portfolios where signal quality varies significantly and you trust the LLM's confidence calibration.

---

### Unrealized P&L (`unreal_pl`)

**How it works:** Scores each position by `(current_price - entry_price) / entry_price`. The incoming signal scores **0.0** (just entered, no P&L yet).

**Replacement behavior:**

| Existing Position | Score | Replaced by new signal? |
|-------------------|-------|------------------------|
| Down 3% | -0.03 | **Yes** (0.0 > -0.03) |
| Break even | 0.0 | **No** (0.0 is not > 0.0) |
| Up 2% | +0.02 | **No** (0.0 < 0.02) |

**Strengths:**
- Intuitive — sell losers, keep winners
- No extra data needed beyond current price

**Weaknesses:**
- **Backward-looking**: A stock down 5% might be bottoming (good to hold); a stock up 3% might be topping.
- **Asymmetric**: New signal always scores 0.0, so it can only replace underwater positions.

**Best for:** Conservative portfolios where you want to cut losses but never sell winners.

---

### Composite (`composite`)

**How it works:** Blends confidence and unrealized P&L using z-score normalization:

```
score = weight * Z(confidence) + (1 - weight) * Z(unrealized_pnl)
```

The `composite_weight` parameter (0–1) controls the blend:
- `weight = 1.0` → pure confidence
- `weight = 0.0` → pure unrealized P&L
- `weight = 0.5` → equal blend (default)

**Strengths:**
- More nuanced than either component alone

**Weaknesses:**
- Inherits both components' problems (stale confidence + backward-looking P&L)
- Needs at least 2 open positions to compute z-scores (falls back to P&L otherwise)
- Incoming signal is scored asymmetrically (confidence component only, P&L = 0)

**Best for:** When you want to consider both signal quality and current performance.

---

### Price Momentum / Trailing Slope (`trailing_slope`)

**How it works:** Fits a linear regression to the last 30 five-minute closing prices. The slope is normalized by the mean price, giving a "% change per bar" score.

```
score = linreg_slope(last 30 closes) / mean(last 30 closes)
```

**Interpreting scores:**

| Score | Meaning | What Happens |
|-------|---------|--------------|
| +0.003 | Price rising ~0.3% per 5-min bar | Strong — unlikely to be replaced |
| +0.001 | Gentle uptrend | Moderate — safe unless something better arrives |
| ~0.000 | Flat / sideways | Vulnerable to replacement |
| -0.002 | Declining | Likely replaced by any positive-slope candidate |

**Strengths:**
- **Symmetric**: Both holdings and new signals scored identically from their own bar data
- Simple, fast, intuitive
- Captures recent momentum direction

**Weaknesses:**
- Pure price — ignores volume (a rally on thin volume is scored the same as one on heavy volume)
- Lookback is fixed (30 bars = 2.5 hours). Misses longer-term trends.

**Best for:** Momentum-based strategies where you want to hold stocks that are actively moving up and replace those that have stalled or reversed.

**Live data priority:** Tick collector (30s buckets) → Schwab 1-min bars (resampled to 5-min)

---

### Accumulation/Distribution (`volume_trend`)

**How it works:** Computes the Money Flow Multiplier (MFM) for each bar, which measures where the close falls within the high-low range:

```
MFM = ((Close - Low) - (High - Close)) / (High - Low)    # ranges from -1 to +1
A/D contribution = MFM * Volume
Score = slope of cumulative A/D line over last 30 bars
```

Close near the high → MFM near +1 (buying pressure). Close near the low → MFM near -1 (selling pressure).

**What it reveals that price alone doesn't:**

| Price Action | A/D Trend | Interpretation |
|-------------|-----------|----------------|
| Rising | Rising | Healthy rally — accumulation confirms price |
| Rising | **Falling** | **Distribution** — smart money selling into the rally |
| Falling | Falling | Confirmed downtrend — distribution |
| Falling | **Rising** | **Accumulation** — smart money buying the dip |

**Strengths:**
- **Symmetric**: Same computation for holdings and new signals
- Captures buying/selling pressure invisible to price-only methods
- Distribution under stable prices = early warning of weakness

**Weaknesses:**
- Scores are in raw A/D slope units (can be ±thousands) — not intuitive
- Requires volume data (always available from our data sources)
- High-volume stocks naturally produce larger A/D values

**Best for:** Identifying hidden strength/weakness. Pairs well with price momentum — a stock rising on increasing volume scores better than one rising on thin volume.

**Live data priority:** Tick collector uses Lee-Ready classified volume (more accurate than bar-based A/D) → Schwab bars use A/D formula as fallback.

---

### RSI Exhaustion (`rsi_current`)

**How it works:** Computes RSI(14) on 5-min closes, then **inverts** it: `score = 100 - RSI`. Lower RSI (oversold) → higher score (more room to run). Higher RSI (overbought) → lower score (running out of gas).

**Interpreting scores:**

| RSI | Inverted Score | Interpretation |
|-----|---------------|----------------|
| 75 | 25 | Overbought — vulnerable to replacement |
| 60 | 40 | Moderate momentum — somewhat vulnerable |
| 50 | 50 | Neutral |
| 35 | 65 | Oversold — room to run, protected |
| 25 | 75 | Deeply oversold — strongest "room to run" score |

**Strengths:**
- **Symmetric**: Same computation for holdings and new signals
- Well-understood indicator
- Captures "momentum exhaustion" — positions that have run too far too fast

**Weaknesses:**
- **Mean-reverting bias**: In strong sustained uptrends, RSI stays overbought. This method would prematurely eject winning positions in a strong bull move.
- Doesn't consider direction — a stock with RSI 35 could be in freefall (bad) or recovering from a dip (good)

**Best for:** Range-bound or mean-reverting strategies. Not ideal for trend-following.

**Live data priority:** Tick collector (higher-resolution RSI from 30s closes) → Schwab 1-min bars

---

### Technical Composite (`tech_score`)

**How it works:** Combines all three forward-looking methods into a single score using z-score normalization:

```
score = 0.4 * Z(trailing_slope) + 0.3 * Z(ad_slope) + 0.3 * Z(inverted_rsi)
```

Each component is z-scored across all currently open positions, so scores represent "how many standard deviations above/below the average holding."

**Interpreting scores:**

| Score | Meaning |
|-------|---------|
| > +1.0 | Well above average on multiple dimensions — strong hold |
| +0.5 | Above average — safe |
| 0.0 | Average holding |
| -0.5 | Below average — replacement candidate |
| < -1.0 | Significantly below average — likely replaced |

**Component weights** (default: 0.4 / 0.3 / 0.3):

| Weight | Component | What It Contributes |
|--------|-----------|-------------------|
| 0.4 | Trailing Slope | Price direction (momentum) |
| 0.3 | A/D Slope | Volume confirmation (smart money) |
| 0.3 | Inverted RSI | Exhaustion / room to run |

**Strengths:**
- More robust than any single indicator — one noisy signal gets diluted
- Z-score normalization makes components comparable despite different scales
- Captures momentum, volume, and exhaustion simultaneously

**Weaknesses:**
- Requires at least 2 open positions to compute z-scores (falls back to trailing_slope if only 1)
- Incoming signals can't be z-scored against holdings (uses raw slope as proxy) — slight asymmetry
- Harder to interpret why a specific position scored high or low

**Best for:** General-purpose ranking when you don't have a strong prior about which signal matters most. Good default choice.

**Live data priority:** Benefits from tick collector for all three components.

---

## Anti-Churn Guard (`replace_min_margin`)

### The Problem

Without a margin, any score difference — no matter how tiny — triggers a replacement. In volatile markets, positions slightly underwater get replaced by marginally better signals in rapid succession. Each replacement incurs slippage and transaction costs, compounding losses ("death churn").

### How It Works

```
Replace only if: new_score > worst_score + replace_min_margin
```

A margin of 0.0 (default) means any improvement triggers replacement. Higher values require the new signal to be meaningfully better.

### The Scale Problem

**The margin is a raw additive number**, but each method produces scores on different scales:

| Method | Margin = 0.05 means... | Effect |
|--------|----------------------|--------|
| `confidence` | 5% confidence gap | Reasonable — moderate anti-churn |
| `unreal_pl` | 5 percentage points of P&L | Huge — almost never replaces |
| `trailing_slope` | 0.05 in %/bar units | Unreachable — scores are ±0.005 |
| `volume_trend` | 0.05 in A/D slope units | Trivial — scores are ±1000 |
| `rsi_current` | 0.05 on a 100-point scale | Essentially zero — always replaces |
| `tech_score` | 0.05 z-score gap | Reasonable — small but meaningful |

**Practical guidance for setting margin by method:**

| Method | Gentle | Moderate | Aggressive Anti-Churn |
|--------|--------|----------|-----------------------|
| `confidence` | 0.02 | 0.05 | 0.10 |
| `unreal_pl` | 0.005 | 0.01 | 0.02 |
| `trailing_slope` | 0.0005 | 0.001 | 0.002 |
| `volume_trend` | 50 | 200 | 500 |
| `rsi_current` | 3 | 8 | 15 |
| `tech_score` | 0.1 | 0.3 | 0.5 |

> **Note:** A future update may normalize all scores to a common 0–1 scale so that a single margin value works uniformly across methods.

---

## Choosing a Method

### Decision Tree

```
Do you trust the LLM confidence scores?
├── Yes, and conditions don't change much after entry
│   └── Use: confidence
├── Somewhat, but I also want to cut losers
│   └── Use: composite (adjust weight to taste)
└── No / I want current market data to decide
    ├── I care most about price direction
    │   └── Use: trailing_slope
    ├── I want to detect hidden buying/selling pressure
    │   └── Use: volume_trend
    ├── I want to avoid holding overbought positions
    │   └── Use: rsi_current
    └── I want a balanced approach
        └── Use: tech_score (recommended default)
```

### Comparison Table

| Factor | confidence | unreal_pl | composite | trailing_slope | volume_trend | rsi_current | tech_score |
|--------|-----------|-----------|-----------|---------------|-------------|-------------|------------|
| Uses current market data | No | Yes (price only) | Partially | Yes | Yes | Yes | Yes |
| Symmetric scoring | No | No | No | Yes | Yes | Yes | Mostly |
| Works with 1 position | Yes | Yes | Falls back | Yes | Yes | Yes | Falls back |
| Captures momentum | No | Indirectly | Indirectly | **Yes** | Partially | Partially | **Yes** |
| Captures volume | No | No | No | No | **Yes** | No | **Yes** |
| Captures exhaustion | No | No | No | No | No | **Yes** | **Yes** |
| Computation cost | None | Low | Low | Medium | Medium | Medium | High |
| Interpretability | High | High | Medium | High | Low | High | Low |

### When Each Method Shines

| Market Condition | Best Method | Why |
|-----------------|-------------|-----|
| Strong trending market | `trailing_slope` | Rewards positions riding the trend |
| Choppy / range-bound | `rsi_current` | Identifies exhaustion before reversals |
| Suspected distribution | `volume_trend` | Catches smart money exiting before price drops |
| Mixed conditions | `tech_score` | Balances all signals |
| Low-frequency trading | `confidence` | Signal quality matters more than intraday dynamics |
| Fast rebalancing | `unreal_pl` | Quick cut-losers, keep-winners logic |

---

## Live vs Backtest Data Sources

In live trading, the system tries to use the highest-quality data available:

| Priority | Source | Resolution | Quality | Available For |
|----------|--------|------------|---------|---------------|
| 1 | Tick collector (TimescaleDB) | 30s–5min buckets | Best (Lee-Ready classified volume) | Live only |
| 2 | Schwab 1-min bars | 1-min (resampled to 5-min) | Good (extended hours included) | Live only |
| 3 | Backtest DataFrame | 1-min (resampled to 5-min) | Historical only | Backtest only |

The tick collector provides **Lee-Ready classified volume** — each trade is tagged as buyer-initiated or seller-initiated based on the bid/ask midpoint. This is significantly more accurate than the bar-based A/D formula, which estimates buying/selling pressure from where the close falls in the high-low range.

| Method | Tick Collector Advantage |
|--------|------------------------|
| `trailing_slope` | 30s closes = more responsive slope detection |
| `volume_trend` | Lee-Ready delta >> bar-based A/D approximation |
| `rsi_current` | Sub-minute RSI catches momentum shifts faster |
| `tech_score` | Benefits from all the above |

---

## See Also

- [ALLOCATION-STRATEGIES.md](ALLOCATION-STRATEGIES.md) — Allocation strategy definitions, replacement logic, implementation details
- [BACKTEST-STRATEGIES.md](BACKTEST-STRATEGIES.md) — Exit strategies (when to close a position)
- [BACKTEST-METRICS.md](BACKTEST-METRICS.md) — Performance metrics and portfolio simulation
- [skills/BACKTEST-LIVE-PIPELINE.md](skills/BACKTEST-LIVE-PIPELINE.md) — How to add new ranking methods to the pipeline
- [skills/PORTFOLIO-DIVERGENCE.md](skills/PORTFOLIO-DIVERGENCE.md) — Diagnosing portfolio divergence (where ranking bugs were discovered)
- [TICK-COLLECTOR.md](TICK-COLLECTOR.md) — Tick collector service and Lee-Ready classification
