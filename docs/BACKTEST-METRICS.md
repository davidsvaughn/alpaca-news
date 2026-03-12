# Backtest Performance Metrics

This document describes the two annualized return methods and associated statistics computed by the backtesting system.

---

## Overview

The backtest engine evaluates exit strategies by simulating trades on historical 1-minute OHLCV bar data. For each trade, it records entry/exit prices, timestamps, bars held, and P&L%. Three summary statistics are displayed:

| Metric | Description |
|--------|-------------|
| **Avg P&L** | Simple mean of per-trade P&L% (ignores duration) |
| **Ann(A)** | Annualized return — unlimited capital model |
| **Ann(B)** | Annualized return — fixed capital split model |
| **Sharpe(A)** | Sharpe ratio — per-trade signal quality |
| **Sharpe(B)** | Sharpe ratio — portfolio-level smoothness |

Avg P&L is naive — a 2% gain in 1 day looks identical to 2% in 30 days. The annualized metrics correct for this by normalizing returns by time.

---

## Method A — Unlimited Capital (Time-Weighted Log Return)

### Capital Model

Each trade uses 1 unit of capital independently. No capital constraint — trades can overlap freely without competing for funds. This measures **per-trade signal quality**.

### Math

For each trade *i* with P&L% return r_i and duration d_i (in trading days):

1. Compute log return:

        ℓ_i = ln(1 + r_i)

2. Compute duration in trading days:

        d_i = max(bars_held, 1) / 390

   where 390 = minutes in a trading day (6.5 hours × 60).

3. Compute time-weighted average daily log return:

        daily_log = Σℓ_i / Σd_i

4. Annualize:

        Ann(A) = e^(252 × daily_log) - 1

   where 252 = trading days per year.

### Volatility & Sharpe

Per-trade daily log return rates:

    x_i = ℓ_i / d_i

Annualized volatility:

    σ_annual = std(x_i) × √252

Sharpe ratio:

    Sharpe(A) = (daily_log × 252) / (std(x_i) × √252)
              = daily_log × √252 / std(x_i)

### Interpretation

- Treats each trade as an independent observation of signal quality.
- Overlapping trades both contribute to the numerator (Σℓ_i) and denominator (Σd_i), so overlapping days are counted multiple times. This **dilutes** the daily rate when trades overlap.
- Sharpe(A) measures: "How consistent is the per-trade daily return rate across trades?" A Sharpe(A) of 0.5–1.0 suggests moderate signal; >1.0 is strong.

### Edge Cases

- `pnl_pct is None` → trade skipped
- `bars_held == 0` → floored to 1 bar (prevents division by zero)
- Fewer than 2 valid trades → returns `None`

---

## Method B — Fixed Capital Split (Equity Curve CAGR)

### Capital Model

Total capital = 1. At each time step, capital is divided equally among all active trades (1/N split). This models a portfolio where you invest in everything the strategy triggers, splitting your money across concurrent positions.

### Data Source

Method B uses **real periodic close prices** extracted from cached 1-minute OHLCV bars. During backtest execution, each trade's held bar slice is resampled at a configurable resolution (default: 60 minutes / hourly) using pandas `resample().last()`. These periodic closes are stored on `BacktestResult.periodic_closes` as `list[tuple[str, float]]` — a list of (ISO timestamp, close price) pairs. They are **not** sent to the frontend (stripped by `to_dict()`).

### Math

1. For each trade, compute per-period log returns from actual prices:

        r_i,t = ln(close_t / close_{t-1})

   The first period uses `entry_price` as the previous price.

2. Collect all timestamps across all trades. For each timestamp *t*:
   - Find all trades active at *t*
   - Portfolio return = mean of active trades' period returns (1/N capital split):

            r_t = (1/N) × Σ r_i,t

3. Compound the equity curve:

        V_t = V_{t-1} × e^(r_t)

   starting from V_0 = 1.

4. Annualize (CAGR):

        Ann(B) = V_final^(P/n) - 1

   where P = periods per year, n = number of observed periods.

   Periods per year = 252 × 390 / resolution_minutes.
   For hourly (60 min): P = 252 × 390 / 60 = 1638.

### Volatility & Sharpe

From the portfolio return series {r_t}:

    σ_annual = std(r_t) × √P

    Sharpe(B) = (mean(r_t) × P) / (std(r_t) × √P)
              = mean(r_t) × √P / std(r_t)

### Interpretation

- Models what happens to $1 invested using the strategy with a capital constraint.
- Overlapping trades share capital (1/N split), so the portfolio return at each step is the average of active positions.
- **Ann(B) can be higher than Ann(A)** when trades overlap heavily. This is expected: Ann(A) counts each overlapping day in Σd_i (diluting the rate), while Ann(B) compounds over wall-clock time.
- **Sharpe(B) tends to be inflated** because averaging across N concurrent positions reduces variance by ~√N (diversification effect). Useful for comparing strategies against each other, less meaningful as an absolute number.

### Edge Cases

- `pnl_pct is None` or missing `periodic_closes` → trade skipped
- Fewer than 2 unique timestamps → returns `None`
- Timestamps with no active trades → skipped (no contribution to equity curve)

---

## Resolution Parameter

The sampling resolution for Method B's periodic closes is configurable:

| Setting | Value |
|---------|-------|
| Env variable | `STATS_RESOLUTION_MINUTES` |
| Default | `60` (hourly) |
| Config location | `trader/config.py` → `Settings.stats_resolution_minutes` |

The resolution affects both Ann(B) and Sharpe(B). Lower resolution (e.g., 1 = every minute) gives more data points but higher computational cost. Higher resolution (e.g., 390 = daily) gives fewer points and less granular volatility measurement.

The annualization factor scales with resolution:

    periods_per_year = 252 × 390 / resolution_minutes

| Resolution | Periods/Year |
|-----------|-------------|
| 1 min | 98,280 |
| 15 min | 6,552 |
| 60 min (default) | 1,638 |
| 390 min (daily) | 252 |

---

## Comparison: Method A vs Method B

| Aspect | Method A | Method B |
|--------|----------|----------|
| Capital model | Unlimited (each trade independent) | Fixed total = 1, split 1/N |
| Data source | `bars_held` + `pnl_pct` only | Real periodic close prices |
| Overlap handling | Counts overlapping days multiple times in Σd_i | Averages concurrent trades at each time step |
| Return computation | Aggregate log return / aggregate duration | Compounded equity curve CAGR |
| Volatility source | Dispersion of per-trade daily rates | Dispersion of portfolio period returns |
| Sharpe measures | Per-trade signal consistency | Portfolio return smoothness |
| Diversification effect | None | Yes (√N variance reduction) |
| Best for | Evaluating signal quality per-trade | Estimating realistic portfolio performance |

---

## Implementation

### Key Files

| File | Role |
|------|------|
| `trader/market/backtest.py` | `compute_ann_a()`, `compute_ann_b()`, `_extract_periodic_closes()` |
| `trader/web/app.py` | Calls compute functions, builds summary dict for API response |
| `trader/web/templates/snapshots.html` | Displays metrics in stats row |
| `trader/config.py` | `STATS_RESOLUTION_MINUTES` env parameter |

### API Response Shape

The `/api/strategies/backtest` endpoint returns:

```json
{
  "trades": [ ... ],
  "summary": {
    "count": 59,
    "avg_pnl": 1.23,
    "ann_a": 132.3,
    "sharpe_a": 0.65,
    "ann_b": 215.5,
    "sharpe_b": 10.60
  }
}
```

All summary values are `null` when insufficient data exists for computation.

---

## Caveats

1. **Backtest selection bias** — Metrics are computed on a filtered dataset. High annualized returns and Sharpe ratios reflect the selected window, not future performance.

2. **Sharpe(B) inflation** — Averaging across concurrent positions creates a diversification effect that reduces measured volatility. A Sharpe(B) of 10 does not mean the same thing as a fund's Sharpe of 10. Use Sharpe(B) for **relative comparison** between strategies, not as an absolute quality measure.

3. **Short sample windows** — With only a few trading days of data, volatility estimates are unreliable and Sharpe ratios are unstable. More data = more meaningful metrics.

4. **No capital constraint in Method A** — Method A assumes you can invest independently in every trade simultaneously. Real capital is finite.

---

## Sanity Tests

The file `tests/test_backtest_sanity.py` contains 38 tests that verify the correctness of the backtest computation pipeline. These tests use synthetic 1-minute bar DataFrames (no network calls) and cover:

### Method A — Hand Calculations (7 tests)

| Test | What it verifies |
|------|-----------------|
| 1% gain over 1 trading day | Ann(A) matches `e^(252 × ln(1.01)) − 1` exactly |
| 1% gain in half a day | Shorter duration → higher annualized return |
| Losing trade | Negative P&L → negative Ann(A) |
| `bars_held = 0` | Floors to 1 bar — no division by zero |
| `pnl_pct = None` trades | Silently skipped, don't affect results |
| Fewer than 2 trades | Returns `None` |
| Sharpe sign | Consistent positive trades → positive Sharpe |

### Method B — Equity Curve (4 tests)

| Test | What it verifies |
|------|-----------------|
| Known single trade | CAGR from periodic closes matches hand calculation |
| Two overlapping trades | Returns averaged at each timestamp (1/N capital split) |
| Missing `periodic_closes` | Trade skipped, doesn't corrupt equity curve |
| Fewer than 2 timestamps | Returns `None` |

### Market Hours & Bar Counting (4 tests)

These confirm that **duration is measured in market time, not wall-clock time**:

| Test | What it verifies |
|------|-----------------|
| Overnight gap structure | Bar 389 = 15:59, bar 390 = next day 09:30 (no fake bars) |
| Multi-day `bars_held` | 391 market bars, not 1440 wall-clock minutes |
| Weekend gap | Friday → Monday, no Saturday/Sunday bars |
| Ann(A) uses market time | Same P&L with fewer market bars → higher annualized return |

> **Key insight**: `bars_held` counts actual 1-min OHLCV bars (market hours only). The `hold_minutes` field displayed in the Duration column uses wall-clock time (entry→exit timestamp delta), which includes overnight gaps. This is display-only — no return computation uses `hold_minutes`.

### Trading Hours Filter (3 tests)

| Test | What it verifies |
|------|-----------------|
| Regular hours (`16:00`) | Exactly 390 bars (09:30–15:59) |
| Extended hours (`20:00`) | 630 bars (09:30–19:59) |
| `None` (no filter) | All bars pass through |

### Periodic Close Extraction (4 tests)

| Test | What it verifies |
|------|-----------------|
| Hourly resolution | ~6–7 periods per trading day |
| Every-bar resolution | Returns every bar |
| Zero bars held | Empty list |
| Overnight gap | No phantom periods outside market hours |

### Transaction Cost Deduction (7 tests)

| Test | What it verifies |
|------|-----------------|
| 10 bps reduces P&L | `pnl_pct` decreases by 0.10 percentage points |
| Entry price adjusted | Increases by 0.10% (for Method B equity curve) |
| Zero cost | No change to any field |
| Losing trade | Cost makes it worse |
| `None` P&L | Stays `None` |
| Ann(A) reduction | Cost-adjusted Ann(A) < raw Ann(A) |
| Sign flip | Marginal winner → loser with sufficient cost |

### Sharpe Ratio Properties (3 tests)

| Test | What it verifies |
|------|-----------------|
| Identical trades | Zero variance → Sharpe is `None` |
| Mostly positive | Positive Sharpe |
| All negative | Negative Sharpe |

### Edge Cases (6 tests)

| Test | What it verifies |
|------|-----------------|
| Non-overlapping A vs B | Both methods positive, same ballpark |
| Breakeven (0% P&L) | ~0% annualized return |
| +500% gain | No overflow |
| −99% loss | Finite negative result |
| −100% loss | Handles `ln(0)` gracefully |
| 1-bar trade | Very high annualized return (correct math) |

### Running

```bash
uv run python -m pytest tests/test_backtest_sanity.py -v -s
```
