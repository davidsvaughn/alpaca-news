# Real-Time VDD: Query-Time Volume Delta Divergence from Tick Data

> Design doc for computing VDD exit signals from the tick collector's
> TimescaleDB `trades` table, with configurable bucket intervals and
> time-based lookback windows.
>
> **Status**: Core implementation complete (`tick_collector/vdd.py`). Live monitor integration pending.
>
> See also:
> - [TICK-COLLECTOR.md](TICK-COLLECTOR.md) — Collector service, TimescaleDB schema, L1 field details, timestamps
> - [TICK-COLLECTOR.md § Volume Gap Tracking](TICK-COLLECTOR.md#volume-gap-tracking) — How unclassified volume works
> - [TICK-COLLECTOR.md § Timestamps](TICK-COLLECTOR.md#timestamps) — Dual timestamp design (`time` vs `received_at`)
> - [TICK-COLLECTOR.md § Phase 1 Findings](TICK-COLLECTOR.md#phase-1-findings-2026-03-10-pre-market-9-am-et) — L1 data quality measurements
> - [BACKTEST-STRATEGIES.md](BACKTEST-STRATEGIES.md) — Backtest VDD implementation (bar-based, § 15)
> - [VOLUME-DELTA-REALTIME.md](VOLUME-DELTA-REALTIME.md) — Shadow collector (predecessor)
> - [VDD-COMPARISON.md](VDD-COMPARISON.md) — Bar-based vs tick-level VDD comparison experiment
> - [LIVE-TRADING.md](LIVE-TRADING.md) — Live exit monitor that will consume this VDD signal
>
> Key source files:
> - `trader/market/backtest.py` — `_compute_vdd_signal_indices()` (line ~1304), `_run_volume_delta_divergence()` (line ~1852)
> - `trader/online/live_monitor.py` — `LiveExitMonitor`, `evaluate_exit()` integration point
> - `tick_collector/vdd.py` — Query-time VDD computation (3 volume modes, historical time range support)
> - `tick_collector/classifier.py` — Lee-Ready direction classifier (midpoint comparison + tick rule fallback)
> - `tick_collector/init.sql` — TimescaleDB `trades` table schema
> - `tick_collector/collector.py` — L1 parser, volume differencing

---

## Background

### Current VDD (backtest / live exit monitor)

The existing VDD exit strategy in `trader/market/backtest.py`
(see [BACKTEST-STRATEGIES.md § 15](BACKTEST-STRATEGIES.md) for full description)
works on **1-minute candle bars** from yfinance/Schwab candle API:

1. **Classify each bar**: if `Close > prev_Close` → entire bar's volume = uptick;
   else downtick. (Inter-bar tick rule — crude, all-or-nothing per bar.)
2. **Cumulative delta**: running sum of uptick - downtick volumes.
3. **Signal**: `Close >= max(Close over last N bars)` AND `cum_delta < cum_delta_N_bars_ago`.
4. **Lookback**: expressed as bar count (e.g., 80 bars = 80 minutes with 1-min data).

### What tick data enables

The [tick collector](TICK-COLLECTOR.md) stores L1 updates in TimescaleDB with
per-update `direction` and `volume_delta` (true volume from `total_volume`
differencing — see [Volume Gap Tracking](TICK-COLLECTOR.md#volume-gap-tracking)).
Direction is classified using the **Lee-Ready algorithm** (`tick_collector/classifier.py`):
trade price is compared to the bid/ask midpoint (above → buyer-initiated, below →
seller-initiated), with a simple tick rule fallback when price equals midpoint or
bid/ask is unavailable. This enables:

- **Sub-minute bucketing**: 10s, 30s, or any interval — not locked to 1-minute bars
- **Intra-bar directional data**: ~20-50 L1 updates per minute, each independently
  classified, vs. one binary classification per bar
- **Proportional volume distribution**: use the classified sample to estimate
  direction for unclassified volume (80-90% of total)
- **Time-based lookback**: express lookback in minutes, not bar count — decouples
  the economic signal from the bucket granularity

---

## Design

### Core Idea

The VDD computation happens at **query time** against the raw `trades` table,
not baked into a materialized view. This allows experimenting with different
bucket intervals and lookback windows without schema changes.

### Parameters

| Parameter | Type | Example | Description |
|-----------|------|---------|-------------|
| `symbol` | str | `"AAPL"` | Stock symbol |
| `lookback_m` | float | `80.0` | Time window for divergence detection (minutes). Shared with bar-based VDD — same param, same meaning. |
| `bucket_s` | int | `30` | Bucket interval for aggregation (10, 15, 30, 60, etc.). Tick-based only. |
| `min_trades_per_bucket` | int | `3` | Skip buckets with fewer L1 updates (too noisy) |

The number of bars in the lookback is derived: `lookback_bars = lookback_m * 60 / bucket_s`.
For 80 minutes with 30-second buckets: 160 bars.

> **Note**: `lookback_m` is the single lookback parameter for both bar-based and tick-based VDD.
> Bar-based treats it as bar count (1 bar = 1 minute). Tick-based converts to bucket count.
> Legacy `{"lookback": 80}` configs still work (treated as minutes).

#### Alternative: Trade-Count Bucketing (Tick Clock)

`get_vdd_bars_by_trades(trades_per_bar=20)` groups a fixed number of L1 updates into
each bar instead of using fixed time intervals. Every bar has the same statistical
weight regardless of liquidity — liquid stocks get ~24s bars, thin stocks get ~4min
bars. This eliminates the sparse-bucket problem where 30s buckets on low-volume
stocks contain only 1-2 trades. See [VDD-COMPARISON.md § Trade-Count Bucketing](VDD-COMPARISON.md#trade-count-bucketing-tick-clock) for rationale and comparison results.

### Step 1: Query bucketed bars from TimescaleDB

SQL query buckets raw trades at the specified interval with proportional
volume distribution:

```sql
SELECT
    time_bucket($1::interval, time) AS bucket,
    -- OHLC
    first(price, time) AS open,
    max(price) AS high,
    min(price) AS low,
    last(price, time) AS close,
    -- Total volume (true volume from volume_delta, accurate)
    sum(COALESCE(volume_delta, size)) AS volume,
    -- Classified volumes (from visible L1 trades)
    sum(size) FILTER (WHERE direction = 1) AS raw_uptick,
    sum(size) FILTER (WHERE direction = -1) AS raw_downtick,
    -- Classified total (denominator for proportional split)
    NULLIF(sum(size), 0) AS classified_total,
    -- Trade count (for min_trades_per_bucket filtering)
    count(*) AS trade_count
FROM trades
WHERE symbol = $2
  AND time >= NOW() - $3::interval   -- lookback + buffer
ORDER BY bucket
```

Parameters: `$1` = bucket interval (e.g., `'30 seconds'`), `$2` = symbol,
`$3` = lookback + some buffer (e.g., `'90 minutes'` for 80-min lookback).

### Step 2: Proportional volume distribution (Python)

For each bucket, distribute total volume proportionally based on the
classified uptick/downtick ratio:

```python
# classified ratio from visible L1 trades
uptick_ratio = raw_uptick / classified_total  # e.g., 0.65
downtick_ratio = raw_downtick / classified_total  # e.g., 0.35

# estimated volumes for the full bucket
est_uptick = volume * uptick_ratio
est_downtick = volume * downtick_ratio

# net delta for this bucket
bucket_delta = est_uptick - est_downtick
```

**Why this is better than bar-based**: With 1-minute bars, the old approach
makes one binary decision per bar (all uptick or all downtick). With L1 data,
we have ~20-50 independent direction samples per minute. Even though we only
see 10-20% of actual trades, the *ratio* of uptick to downtick in our sample
provides a much more nuanced estimate of the true split.

**Edge case**: If a bucket has 0 classified volume (all L1 updates had
`last_size=0`, which shouldn't happen since we skip those), use the bar's
close-to-close direction as fallback (same as current bar-based approach).

#### Volume Modes

`tick_collector/vdd.py` supports three volume classification modes (the
`volume_mode` parameter) for comparison testing:

| Mode | Description |
|------|-------------|
| `"proportional"` (default) | Distribute total volume using the visible uptick/downtick ratio from Lee-Ready classified trades. Best estimate of true split. |
| `"visible_only"` | Use only directly classified (visible) trade volume — no extrapolation. Conservative; ignores unclassified volume. |
| `"bar_binary"` | Mimic the backtest inter-bar tick rule — entire bucket volume assigned up or down based on close-to-close direction. Baseline for comparison. |

### Step 3: VDD signal detection (Python)

Same math as `_compute_vdd_signal_indices()` in
[backtest.py:1304](../trader/market/backtest.py), but operating on the bucketed
data with time-based lookback:

```python
import pandas as pd
import numpy as np

def compute_vdd_signal(bars: pd.DataFrame, lookback_bars: int) -> pd.DataFrame:
    """Detect VDD divergence from bucketed tick data.

    bars must have columns: bucket, close, est_uptick, est_downtick
    Returns bars with added columns: cum_delta, signal
    """
    bars = bars.copy()
    bars["bucket_delta"] = bars["est_uptick"] - bars["est_downtick"]
    bars["cum_delta"] = bars["bucket_delta"].cumsum()

    # VDD signal: price at rolling high + cumulative delta declining
    prev_roll_max = bars["close"].shift(1).rolling(lookback_bars, min_periods=lookback_bars).max()
    lag_cum_delta = bars["cum_delta"].shift(lookback_bars)

    bars["signal"] = (
        (bars["close"] >= prev_roll_max) &
        (bars["cum_delta"] < lag_cum_delta)
    ).fillna(False)

    return bars
```

### Step 4: Public API

```python
async def check_vdd_exit(
    pool: asyncpg.Pool,
    symbol: str,
    lookback_m: float = 80.0,
    bucket_s: int = 30,
    min_trades_per_bucket: int = 3,
) -> bool:
    """Check if VDD exit signal is active for a symbol.

    Returns True if the most recent bucket has a VDD divergence signal.
    """
    # 1. Query bucketed bars from TimescaleDB
    # 2. Proportional volume distribution
    # 3. VDD signal detection
    # 4. Return whether the latest bar has signal=True
```

This replaces the current `evaluate_exit("volume_delta_divergence", ...)` call
in [live_monitor.py](../trader/online/live_monitor.py) when tick data is
available, with fallback to the bar-based approach when it's not.

---

## Comparison: Bar-Based vs Tick-Based VDD

| Aspect | Bar-Based (current) | Tick-Based (new) |
|--------|---------------------|------------------|
| **Data source** | yfinance/Schwab 1-min candles | TimescaleDB `trades` table |
| **Direction classification** | 1 binary decision per bar | ~20-50 independent samples per minute |
| **Volume assignment** | 100% of bar volume → one direction | Proportional split based on sample ratio |
| **Bucket granularity** | Fixed 1 minute | Configurable (10s, 30s, 1m, ...) |
| **Lookback** | N bars (coupled to bucket size) | N minutes (decoupled from bucket size) |
| **Detection latency** | Up to 59 seconds (waits for bar close) | As low as bucket_seconds (e.g., 10-30s) |
| **Unclassified volume** | None (all volume assigned, possibly wrong) | Tracked explicitly; proportional estimate |
| **Fallback** | Always available (candle API) | Requires tick collector running |

---

## Implementation Plan

### Phase 1: Fixed Time Window (build first)

> Configurable `lookback_minutes` and `bucket_seconds`. Single VDD computation.

**Files to create/modify:**

1. **`tick_collector/vdd.py`** (implemented) — Core VDD computation:
   - `async def get_vdd_bars(pool, symbol, lookback_m, bucket_s, volume_mode, *, start_time, end_time)` — query + volume classification
   - `def compute_vdd_signal(bars, lookback_bars)` — signal detection (pandas)
   - `async def check_vdd_exit(pool, symbol, ...)` — public API for live exit monitor
   - `def find_first_signal(bars, lookback_bars, after_bucket)` — find first signal in a bar set
   - Three volume modes: `"proportional"` (default), `"visible_only"`, `"bar_binary"`
   - Historical time range queries via `start_time`/`end_time` parameters (UTC)

2. **`trader/market/backtest.py`** (modify) — Accept `lookback_m` param:
   - Bar-based VDD reads `lookback_m` (falls back to legacy `lookback` key)
   - Same exit strategy key `"volume_delta_divergence"`, no new variant needed

3. **`trader/online/live_monitor.py`** (modify) — Wire up tick-based VDD:
   - When bar-based VDD doesn't fire, try tick-based as supplement
   - Enabled via `VDD_TICK_ENABLED=1` env var
   - Lazy asyncpg pool to TimescaleDB (auto-connects, falls back on failure)

**Things to experiment with** (once implemented):
- Bucket sizes: 10s, 15s, 30s, 60s — which gives best signal-to-noise?
- Lookback windows: 40min, 60min, 80min, 120min — optimal for the strategy?
- `min_trades_per_bucket` threshold — skip thin buckets or fill forward?
- Compare tick-based VDD signals against bar-based on historical data
- Measure: does finer granularity actually produce earlier/better exit signals?

### Phase 2: Multi-Resolution (layer on top)

> Compute VDD at multiple granularities simultaneously. Exit when fast signal
> fires AND slower signal confirms. Filters noise while catching early exits.

**Concept:**

```python
async def check_vdd_exit_multi(
    pool: asyncpg.Pool,
    symbol: str,
    # Fast signal: fine granularity, detects divergence forming
    fast_bucket_s: int = 15,
    fast_lookback_m: float = 20.0,
    # Slow signal: coarse granularity, confirms trend
    slow_bucket_s: int = 60,
    slow_lookback_m: float = 80.0,
) -> bool:
    """Multi-resolution VDD: exit when fast signal fires AND slow confirms."""
    fast = await check_vdd_exit(pool, symbol, fast_lookback_m, fast_bucket_s)
    slow = await check_vdd_exit(pool, symbol, slow_lookback_m, slow_bucket_s)
    return fast and slow
```

**Why multi-resolution helps:**
- Fast signal alone (15s/20min) is sensitive but noisy — many false positives
- Slow signal alone (60s/80min) is reliable but late — same as current bar-based
- Fast AND slow = early detection with confirmation = fewer false exits

**Not implementing yet** — need Phase 1 data to tune the parameters. Building
the fixed-window function first gives us the tools to experiment, then we can
layer multi-resolution on top once we know what bucket sizes and lookbacks
work best.

---

## Practical Considerations

### L1 Sample Quality at Fine Granularity

With L1 data updating ~1/sec per symbol (measured in
[Phase 1 Findings](TICK-COLLECTOR.md#phase-1-findings-2026-03-10-pre-market-9-am-et)),
different bucket sizes give different sample counts:

| Bucket | ~Samples/bucket (active stock) | Reliability |
|--------|-------------------------------|-------------|
| 10 sec | 3-8 | Noisy — ratio estimate unreliable |
| 15 sec | 5-12 | Marginal — may need min_trades filter |
| 30 sec | 10-25 | Good — reasonable sample for ratio |
| 60 sec | 20-50 | Best — many samples, but same as bar-based timing |

**Recommendation**: Start with **30-second buckets** as the default. Fine enough
to detect divergence ~30s earlier than 1-minute bars, but with enough L1 samples
per bucket for a reliable uptick/downtick ratio estimate.

### Dual Timestamps

Each trade has `time` (Schwab trade time) and `received_at` (local receipt time) —
see [TICK-COLLECTOR.md § Timestamps](TICK-COLLECTOR.md#timestamps) for details.
The VDD query should bucket by `time` for accurate economic ordering. The
`received_at` is useful for latency monitoring but not for the VDD computation.

Some early trades may have stale `time` values (Schwab reports last trade time
from previous session on first L1 update). The query should filter to
`time >= NOW() - lookback - buffer` AND `received_at >= NOW() - lookback - buffer`
to avoid stale data.

### Fallback Strategy

The tick-based VDD requires the tick collector to be running and TimescaleDB
to have recent data. The live exit monitor should:

1. Try tick-based VDD first (from TimescaleDB)
2. If TimescaleDB is unavailable or has no recent data for the symbol → fall back
   to bar-based VDD (from candle API, current behavior)
3. Log which method was used for each check

### Performance

The query hits the `trades` hypertable (see [schema](TICK-COLLECTOR.md#schema))
with an index on `(symbol, time DESC)`.
For 80 minutes of data at ~50 updates/min = ~4,000 rows per symbol. This should
be sub-100ms even without the continuous aggregate. If performance becomes an
issue, we can add a continuous aggregate at the most common bucket interval.

---

## Open Questions (Resolved)

- [x] **Min `trade_count` per bucket?** Start with 3. Skip buckets below threshold
      (don't fill-forward — avoids fabricating data from thin samples).
- [x] **Fill-forward empty buckets?** No — skip them. The signal math tolerates
      gaps; fabricating bars from no data adds noise. During low-volume periods
      the lookback window naturally covers more wall-clock time.
- [x] **Volume-weighted or count-weighted ratio?** Volume-weighted (sum of `size`
      per direction / total classified `size`). Larger trades carry more signal.
- [x] **First buckets after collector startup?** Filter `volume_delta IS NULL`
      rows out of the query. The first L1 update per symbol has no baseline;
      these rows contribute `size` but no `volume_delta`, so exclude them from
      the proportional distribution.

---

## Live Portfolio Overrides

### The Problem

Tick-based VDD is only available for **live portfolios** (the tick collector
streams held positions), not for backtests (which use historical candle data).
But portfolios are launched from the backtest panel using the same config.

This creates a tension: how do you configure live-only features (tick VDD,
future live-only enhancements) without polluting the backtest param space?

### Design: `live_overrides` on LiveConfig

Add a `live_overrides: dict[str, Any]` field to `LiveConfig`. This is a flat
dict for settings that only apply to the live portfolio — the backtest runner
never sees them.

```python
# LiveConfig dataclass
live_overrides: dict[str, Any] = field(default_factory=dict)

# Example value:
# {
#     "vdd_tick": true,          # enable tick-based VDD supplement
#     "bucket_s": 30,            # tick VDD bucket interval
#     "min_trades_per_bucket": 3, # tick VDD min trades filter
#     "poll_interval_s": 30      # monitor loop interval (auto-set to bucket_s)
# }
```

**Key properties:**

- **Backtest ignores it** — `evaluate_exit()` only reads `exit_params`. The
  `live_overrides` dict is never passed to strategy runners.
- **Live monitor reads it** — `_check_holding()` checks `live_overrides` for
  tick VDD settings (replaces `VDD_TICK_ENABLED` env var).
- **Poll interval** — `poll_interval_s` controls how often the monitor loop
  runs. Auto-set to `bucket_s` in the UI so the poll matches the VDD bucket.
- **Extensible** — future live-only features go here (e.g. position sizing
  tweaks, streaming config).
- **Backward compatible** — existing configs without `live_overrides` work
  unchanged (defaults to empty dict).

### UI Integration

The backtest panel stays unchanged. The "Go Live" dialog gets a collapsible
"Live Overrides" section, shown only when launching a portfolio:

```
Backtest panel:  [strategy params]         ← same as today
Go Live dialog:  [strategy params]
                 ▸ Live Overrides
                   ☑ Tick-based VDD
                   Bucket size: [30]s
                   Min trades/bucket: [3]
```

### Data Flow

```
Backtest panel → exit_params: {"lookback_m": 80}
                                         ↓
Go Live dialog → live_overrides: {"vdd_tick": true, "bucket_s": 30}
                                         ↓
POST /api/live/config → LiveConfig(exit_params=..., live_overrides=...)
                                         ↓
LiveExitMonitor._check_holding():
  1. evaluate_exit(exit_params)           ← bar-based VDD (always runs, handles guards)
  2. if live_overrides.get("vdd_tick"):   ← tick supplement (only if enabled)
       check_vdd_exit(lookback_m, bucket_s, ...)
```

### Live Monitor Behavior

When `live_overrides["vdd_tick"]` is enabled:

1. **Always run bar-based** `evaluate_exit()` first — handles guards (stop/target)
   and bar-based VDD signal
2. **If bar-based says "still open"** AND strategy is VDD → run tick-based check
3. **If tick fires** → exit with reason `"signal_tick"` (distinguishable from
   bar-based `"signal"` in logs and transaction history)
4. **If TimescaleDB unavailable** → silently fall back to bar-based only

This "supplement" approach is safest for initial rollout: bar-based catches
everything it always did, tick-based can only add earlier exits.

### Implementation Steps

1. Add `live_overrides` field to `LiveConfig` (default `{}`)
2. Update `POST /api/live/config` to accept `live_overrides` from request body
3. Update `_check_holding()` to read `live_overrides` instead of env var
4. Add "Live Overrides" collapsible section to Go Live dialog in UI
5. Remove `VDD_TICK_ENABLED` env var (setting moves into config)
