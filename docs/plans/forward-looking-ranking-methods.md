# Plan: Forward-Looking Ranking Methods + Anti-Churn Guard

## Context

Two parallel portfolios (same config, same start time) diverged: non-Alpaca +1.1% vs Alpaca -0.15%. Root cause: replacement scoring was broken for non-Alpaca (all scores = 0.0, so replacements never fired), while Alpaca churned through 12 replacement exits at small losses. The "broken" portfolio accidentally proved that **less aggressive replacement is better**. Beyond fixing the bug, we need forward-looking ranking methods that answer "which position has the best future prospects?" instead of the current backward-looking methods.

## What We're Building

4 new ranking methods + 1 anti-churn guard, working in both backtest and live trading:

| Method | Key | Score Formula | Data Needed |
|--------|-----|---------------|-------------|
| Price Momentum | `trailing_slope` | Normalized linreg slope of last N 5-min closes | Close prices |
| Accumulation/Distribution | `volume_trend` | Slope of cumulative volume delta | OHLCV + volume classification |
| RSI Exhaustion | `rsi_current` | `100 - RSI(14)` (inverted: lower RSI = higher score) | Close prices |
| Technical Composite | `tech_score` | `w1*Z(slope) + w2*Z(ad) + w3*Z(rsi)` z-score blend | All above |
| **Anti-churn guard** | `replace_min_margin` | New score must exceed worst by at least this margin | N/A |

## Tick Collector Integration (All Methods)

The tick collector (`tick_collector/`) streams L1 trade data into TimescaleDB with Lee-Ready classification. `get_vdd_bars()` returns sub-minute buckets (default 30s) with OHLCV + `est_uptick`/`est_downtick` (classified volume). This benefits ALL ranking methods in live trading:

| Method | Tick Data Advantage |
|--------|-------------------|
| `trailing_slope` | 30s bucket closes = more responsive slope than 1-min Schwab bars |
| `volume_trend` | Lee-Ready classified delta (`est_uptick - est_downtick`) is far more accurate than bar-based A/D or inter-bar tick rule |
| `rsi_current` | Higher resolution RSI from 30s closes catches momentum shifts faster |
| `tech_score` | Benefits from all above |

**Pattern**: Try tick_collector first → fall back to Schwab 1-min bars. Same fallback pattern already used for VDD exit signals in `_try_tick_vdd()`.

**Backtest**: Tick data is NOT available historically (collector started recently). Backtest always uses bar-based computation from the 1-min OHLCV DataFrame. The tick path is live-only.

## Key Design Decision: Backtest Data Access

**Problem**: `apply_allocation()` only gets `BacktestResult` with `periodic_closes` (close-only). The new methods need OHLCV bars and computed indicators.

**Solution**: Pre-compute ranking features during `run_backtest()` and store on `BacktestResult`:
- New field: `ranking_features: list[tuple[str, dict[str, float]]] | None` — feature values at each periodic interval
- New field: `entry_features: dict[str, float] | None` — features at entry time (for scoring the new signal symmetrically)
- Features dict keys: `slope`, `rsi`, `ad_slope`
- Computed from 5-min resampled bars with warmup window before entry
- `to_dict()` strips both fields (internal only)

This avoids bloating BacktestResult with full OHLCV, keeps `apply_allocation()` clean, and computes features only once per trade.

## Implementation Steps

### Step 1: Constants, Options, Anti-Churn Param
**File**: `trader/market/backtest.py`

- Add 4 new `RANK_METHOD_*` constants (after line 274)
- Add 4 entries to `_RANK_OPTIONS` (line 291) — UI auto-populates from this
- Update `normalize_rank_method()` to accept new keys
- Add `replace_min_margin` ParamDef to both `max_positions` and `ranking_realloc` allocation definitions
- Import new constants in `live_monitor.py`

### Step 2: BacktestResult Fields
**File**: `trader/market/backtest.py`

- Add `ranking_features` and `entry_features` fields to `BacktestResult` dataclass (with `None` defaults)
- Update `to_dict()` to pop both

### Step 3: Core Ranking Feature Functions (pure, shared)
**File**: `trader/market/backtest.py` (near existing `_compute_rsi`, line 1207)

```python
def _compute_trailing_slope(closes: np.ndarray, lookback: int = 30) -> float:
    """Normalized slope of linear regression over last N values."""

def _compute_ad_slope(highs, lows, closes, volumes, lookback: int = 30) -> float:
    """Slope of cumulative A/D line over last N bars."""

def compute_ranking_features(df_5m: pd.DataFrame, lookback: int = 30, rsi_period: int = 14) -> dict[str, float]:
    """Compute all ranking features from a 5-min OHLCV DataFrame.
    Returns {slope, rsi, ad_slope}.
    Called by both backtest and live paths.
    """
```

Reuses existing `_compute_rsi()`. All functions are pure numpy/pandas, no side effects.

### Step 4: Feature Extraction for Backtest
**File**: `trader/market/backtest.py`

Add `_extract_ranking_features()` near `_extract_periodic_closes()` (line 2220):
- Takes full 1-min DataFrame, entry_idx, bars_held, resolution
- Resamples to 5-min with warmup window (`max(lookback, rsi_period)` bars before entry)
- At each periodic timestamp, computes `compute_ranking_features()` on the 5-min slice up to that point
- Returns `list[tuple[str, dict[str, float]]]` aligned to periodic_closes timestamps

Also compute `entry_features` at entry_idx (features at moment of entry, used for scoring the new signal).

### Step 5: Wire into `run_backtest()`
**File**: `trader/market/backtest.py`

After `_extract_periodic_closes` call (line ~2480):
```python
rf = _extract_ranking_features(df, entry_idx, bars_held, stats_resolution_minutes) if pnl_pct is not None else None
ef = _compute_entry_features(df, entry_idx) if pnl_pct is not None else None
```
Pass `ranking_features=rf, entry_features=ef` to BacktestResult constructor.

### Step 6: Backtest Ranking Functions
**File**: `trader/market/backtest.py`

Add near existing `_rank_*` functions (line ~595):

- `_features_at_time(result, target_iso) -> dict | None` — parallel to `_price_at_time()`, looks up ranking_features
- `_rank_trailing_slope()` — uses `feat["slope"]`
- `_rank_volume_trend()` — uses `feat["ad_slope"]`
- `_rank_rsi_current()` — uses `100 - feat["rsi"]` (inverted)
- `_rank_tech_score()` — z-score combination of all three, with configurable weights

### Step 7: Update Dispatch + Anti-Churn
**File**: `trader/market/backtest.py`

- `_compute_scores()` (line 598): add cases for 4 new methods
- `_try_replace()` (line 614):
  - Add `min_margin: float = 0.0` param
  - Add `new_features: dict[str, float] | None = None` param
  - For new methods, score incoming signal from `new_features` (symmetric scoring)
  - Change comparison: `new_score > scores[worst_sid] + min_margin`
- `apply_allocation()` (line 648):
  - Read `replace_min_margin` from `alloc_params`
  - Pass `result.entry_features` as new signal's features to `_try_replace()`
  - Pass `min_margin` through

### Step 8: Live Path — Scoring with Tick Data Priority
**File**: `trader/online/live_monitor.py`

Extend `_score_holding_watches()` (line 836):

**Data fetching strategy** (for each holding symbol):
1. **Try tick_collector** first: call `get_vdd_bars(pool, symbol, lookback_m=150, bucket_s=300)` (5-min buckets, 2.5h lookback). Returns DataFrame with OHLCV + `est_uptick`/`est_downtick`.
2. **Fall back to Schwab bars**: call `_get_ohlcv_1m(symbol, start_date)`, resample to 5-min.
3. Compute `compute_ranking_features()` on whichever DataFrame we got.

**For `volume_trend` specifically**:
- Tick path: use `est_uptick - est_downtick` directly as volume delta (Lee-Ready classified). Compute slope of cumulative delta.
- Bar fallback: use A/D Money Flow Multiplier from OHLCV (same as backtest).

**Async handling**: reuse the `_loop` pattern from `_try_tick_vdd()` for tick_collector calls.

Extend `_find_replacement_victim()` (line 906):
- For new methods: fetch bars for the new symbol too, compute features, pass as `new_features`
- Apply `replace_min_margin` from `alloc_params`
- Fix non-Alpaca scoring: for `unreal_pl`, use fresh quote via `self.market.get_quotes()` (partially done already)

### Step 9: Update Docs

**File**: `docs/ALLOCATION-STRATEGIES.md`
- Update "Proposed" → "Implemented" for all 4 methods
- Add anti-churn guard documentation
- Add tick data integration section
- Update implementation status

**File**: `docs/skills/BACKTEST-LIVE-PIPELINE.md`
- Add "Adding a new ranking method" section (parallel to existing guard/strategy guides)
- Document that `_RANK_OPTIONS` auto-populates the UI dropdown

**File**: `docs/skills/PORTFOLIO-DIVERGENCE.md`
- Update "Known divergence causes" with fix status

## Files Modified

| File | Changes |
|------|---------|
| `trader/market/backtest.py` | Constants, BacktestResult fields, 3 computation functions, feature extraction, 4 ranking functions, dispatch updates, anti-churn in _try_replace |
| `trader/online/live_monitor.py` | Scoring extension for new methods, tick_collector + Schwab bar fetching, anti-churn margin, non-Alpaca fresh quote fix |
| `docs/ALLOCATION-STRATEGIES.md` | Update status, add anti-churn + tick integration docs |
| `docs/skills/BACKTEST-LIVE-PIPELINE.md` | Add "ranking method" pipeline guide |
| `docs/skills/PORTFOLIO-DIVERGENCE.md` | Update fix status |

## Key Existing Functions to Reuse

| Function | File | Purpose |
|----------|------|---------|
| `_compute_rsi(close, period)` | backtest.py:1207 | RSI computation |
| `_compute_volume_delta(df)` | backtest.py:1292 | Bar-based uptick/downtick |
| `_price_at_time(result, iso)` | backtest.py:489 | Lookup periodic_closes by time |
| `_extract_periodic_closes(...)` | backtest.py:2220 | Pattern for new feature extraction |
| `compute_vdd_signal(bars, lookback)` | tick_collector/vdd.py:337 | Tick-based VDD signal |
| `get_vdd_bars(pool, symbol, ...)` | tick_collector/vdd.py:152 | Tick-based OHLCV + classified volume |
| `get_pool()` | tick_collector/vdd.py:424 | TimescaleDB async pool singleton |
| `_get_ohlcv_1m(symbol, start)` | backtest.py:1077 | Schwab/yfinance 1-min bars with disk cache |
| `normalize_rank_method(method)` | backtest.py:277 | Canonicalize rank method keys |

## Verification

1. **Backtest**: Run a backtest with `ranking_realloc` using each new method. Verify replacement decisions use correct scores (console output).
2. **Anti-churn**: Same backtest with `replace_min_margin=0.05`. Verify fewer replacements.
3. **Live (non-Alpaca)**: Start test portfolio with `trailing_slope` ranking. Verify `LIVE-EVAL` console shows computed scores for holdings and new signals.
4. **Live (tick data)**: With tick_collector running, verify ranking uses tick data (check for tick-sourced bars in scoring log output). Kill tick_collector and verify fallback to Schwab bars.
5. **UI**: Verify new methods appear in dropdown when allocation is `max_positions` (when_full=replace) or `ranking_realloc`.
