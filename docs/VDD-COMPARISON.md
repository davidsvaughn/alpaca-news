# VDD Comparison: Bar-Based vs Tick-Level Volume Delta

> Comparing the bar-based (inter-bar tick rule) and tick-level volume delta methods
> for the Volume Delta Divergence (VDD) exit strategy, using real portfolio data.
>
> **Current comparison tool**: `scripts/vdd_comparison.py` — queries tick data from
> TimescaleDB (populated by `tick_collector/`) and compares against bar-based OHLCV.
> See [TICK-COLLECTOR.md](TICK-COLLECTOR.md) for the data collection system and
> [VDD-REALTIME.md](VDD-REALTIME.md) for the tick-based VDD design.
>
> Started: 2026-03-09 | Updated: 2026-03-12

---

## Motivation

Portfolio `lc_ab8aa7f25745` has been running since 2026-03-05 using VDD exits with
`lookback=80` and the **bar-based** inter-bar tick rule. The live exit monitor calls
`evaluate_exit()` from `backtest.py` — the exact same code path as backtesting (see
[Current Status](VOLUME-DELTA-REALTIME.md#current-status-whats-wired-up-today)).
The shadow collector has been simultaneously gathering **tick-level** volume delta
data for all portfolio symbols.

We want to know: if we had used tick-level volume delta instead of bar-based, how would
the exit signals have differed? Is there a systematic pattern (earlier? later? random)?

## Background: The Two Methods

> For VDD formula and parameter ranges, see
> [BACKTEST-STRATEGIES.md § Volume Delta Divergence](BACKTEST-STRATEGIES.md#15-volume-delta-divergence-volume_delta_divergence).
> For how live trading is wired up (same backtest code, polled once per minute), see
> [VOLUME-DELTA-REALTIME.md § Current Status](VOLUME-DELTA-REALTIME.md#current-status-whats-wired-up-today).

Both methods produce 1-minute bars with uptick/downtick volume splits. The difference
is **how volume is classified within each bar**:

| Aspect | Bar-Based (inter-bar tick rule) | Tick-Level (tick_collector → TimescaleDB) |
|--------|-------------------------------|------------------------------|
| **Classification** | Entire bar volume → uptick OR downtick | Each tick classified via Lee-Ready (bid/ask midpoint) + tick-rule fallback |
| **Rule** | `Close_t > Close_{t-1}` → all uptick | `Price > midpoint` → uptick (Lee-Ready), else `Price > prev_price` (tick rule) |
| **Granularity** | 1 decision per bar (binary) | ~20-50 L1 updates per minute per symbol |
| **Captures intra-bar reversals** | No | Yes |
| **Source** | OHLCV bars (Schwab REST / yfinance fallback) | Schwab LEVELONE_EQUITIES stream → TimescaleDB |
| **Bucket flexibility** | Fixed 1-minute | Configurable: 15s, 30s, 60s via `bucket_s` |
| **Volume modes** | All-or-nothing (binary) | `proportional`, `visible_only`, `bar_binary` (see `tick_collector/vdd.py`) |

The VDD signal fires when **both** conditions are true:
1. Price makes a new high over the previous `lookback` bars
2. Cumulative volume delta is **lower** than it was `lookback` bars ago

## Data Sources

### Current: tick_collector → TimescaleDB

The `tick_collector/` service streams Schwab LEVELONE_EQUITIES data into TimescaleDB.
**Every L1 update is persisted** as a row in the `trades` table with:
- Price, size, direction (Lee-Ready classified), volume_delta, total_volume, exchange
- Two timestamps: `time` (Schwab trade_time) and `received_at` (local clock)

A `bars_1m` continuous aggregate pre-computes 1-minute bars with uptick/downtick splits.
For comparison testing, `tick_collector/vdd.py` can re-bucket at any interval (15s, 30s, 60s)
using the raw `trades` data.

### Legacy: Shadow Collector (SUPERSEDED)

> The shadow collector (`trader/market/volume_delta_shadow.py`) was the predecessor.
> It accumulated uptick/downtick in memory and snapshotted 1-minute bars to JSONL files.
> Only the 1-minute bar snapshots were persisted — individual ticks were discarded.
> **Critical limitation**: Schwab WebSocket drops during market hours with no reconnection.
> Phase 1 analysis (below) used shadow data and was inconclusive due to this data gap.

### Why Raw Tick Storage Is Better

Unlike the shadow collector's 1-minute snapshots:
- Sub-minute bucketing (15s, 30s) enables earlier detection
- Lee-Ready classification uses bid/ask midpoint, not just price-vs-price
- Volume modes (proportional, visible_only) can be compared at query time
- No data loss — every L1 update is preserved

### Why 1-Minute Bars Are Sufficient for This Comparison

The advantage of tick-level data is **not** about checking more frequently (both
methods check every minute in this comparison). It's about **more accurate volume
classification within each bar**:

- Bar-based: "Close went up → ALL 50,000 shares this minute were buying pressure"
- Tick-level: "30,000 on upticks, 20,000 on downticks → net delta = +10,000"

The tick-level minute bar captures intra-bar reversals that the binary bar-based
method misses entirely. That more accurate split flows into cumulative delta, which
flows into VDD signals — so the signal can fire on a different bar even at the same
1-minute resolution.

### Sub-Minute VDD from Tick Data

The `tick_collector` stores **every L1 update** in TimescaleDB's `trades` table.
At query time, `tick_collector/vdd.py` re-buckets at any interval:

| Bucket | L1 samples/bucket | Signal latency vs 1-min |
|--------|-------------------|------------------------|
| 60s    | ~20-50            | Same as bar-based      |
| 30s    | ~10-25            | Up to ~30s earlier     |
| 15s    | ~5-12             | Up to ~45s earlier     |

The comparison script (`scripts/vdd_comparison.py`) sweeps all three bucket sizes
across multiple volume modes and lookback periods.

### Future: Trade-Size Filtering (Institutional Flow)

An interesting enhancement would be to filter trades by size before computing volume
delta — e.g., only count trades > 1,000 shares — to isolate what "big money" is doing
and filter out retail noise. This requires **per-trade data** with individual trade sizes.

**Current limitation**: Schwab's `LEVELONE_EQUITIES` stream (what we use now) provides
~1 update/sec per symbol. The shadow collector uses `last_price` (field 3) +
`total_volume` (field 8) and infers incremental volume by differencing consecutive
`total_volume` readings.

**Update (2026-03-10)**: L1 also provides `Last Size` (field 9), `Trade Time` (field 35),
and `Last MIC ID` (field 41) — we were not subscribing to these. `Last Size` gives the
size of the most recent trade, enabling *approximate* trade-size filtering. However, L1
is **not a per-trade feed**: if 20 trades happen between ~1-sec updates, only the last
trade's price/size is reported. `total_volume` may jump by far more than `last_size`,
meaning we miss individual trades. Approximate coverage: ~30-50% of trades for liquid
stocks. See [TICK-COLLECTOR.md](TICK-COLLECTOR.md) for the three-tier accuracy model.

**Solution: Schwab TIMESALE_EQUITY**

Schwab's streaming API supports a `TIMESALE_EQUITY` service that provides **individual
trade prints** — each message is one trade with its price and size. This is exactly
what's needed for size-filtered volume delta. Key facts:

- Available free with a Schwab brokerage account (which we have)
- Uses the **same WebSocket connection** we already have open for `LEVELONE_EQUITIES`
- Provides per-trade: price, size, timestamp, exchange
- **However**: the `schwabdev` Python library does NOT expose a `timesale_equity()`
  method. It only wraps `level_one_equities()`, `chart_equity()`, and a few others.

**Options to access TIMESALE_EQUITY**:
1. **Send raw subscription message** on the existing schwabdev WebSocket — bypass the
   library's method wrappers and send the TIMESALE subscription JSON directly. The
   underlying WebSocket is the same; schwabdev just hasn't wrapped this service.
2. **Fork/extend schwabdev** — add a `timesale_equity()` method mirroring the pattern
   of existing service methods.
3. **Alpaca paid SIP feed** — per-trade data from all exchanges, but requires a paid
   data subscription.

Not needed for the current bar-vs-tick comparison, but would unlock a more
sophisticated volume delta signal in the future.

## Lookback Translation

With sub-minute buckets, `lookback_m` (minutes) replaces `lookback` (bar count):
- `lookback_m=80` with 60s buckets → 80 bars (same as bar-based)
- `lookback_m=80` with 30s buckets → 160 bars (same time window, finer resolution)
- `lookback_m=80` with 15s buckets → 320 bars

The `bucket_s` parameter in `tick_collector/vdd.py` controls this.

## Data Availability

### tick_collector (current, 2026-03-10+)

**Portfolio**: `lc_d58c66d24787` (started 2026-03-09)
- 69 total watches: 20 holding, 49 exited (39 signal, 10 guard_stop)
- Tick data in TimescaleDB for **151 symbols** (all held + cooling-off)
- **March 11 has excellent coverage**: 7 key symbols at 100% (390/390 bars), many >95%
- March 10 has a gap (collector restarted mid-day)
- tick_collector dynamically syncs symbols from the portfolio

### Shadow collector (legacy, pre-2026-03-10)

**Portfolio**: `lc_ab8aa7f25745` (started 2026-03-05)
- Shadow data stored at `~/.cache/alpaca-news/volume_delta_shadow/{SYMBOL}/`
- **Coverage was insufficient** — Schwab WebSocket drops during market hours
- See Phase 1 results below for details

---

## Phase 1: Direct Signal Comparison

**Goal**: For each VDD-exited position, compare when the bar-based vs tick-level
VDD signal first fires after entry.

### Method

For each of the 33 VDD-exited positions:
1. Load OHLCV bar data for the holding period (same bars the live system used)
2. Load shadow tick-level minute bars for the same period
3. Build a "tick-level bar DataFrame" using OHLCV from the regular bars but replacing
   uptick/downtick with the shadow collector's tick-level classification
4. Run `_compute_vdd_signal_indices()` on both datasets with `lookback=80`
5. Find the first signal at or after entry for each method
6. Record: bar-based exit time, tick-level exit time, delta (minutes), price at each

### Output

A table with one row per position:

| Symbol | Entry | Bar Exit | Tick Exit | Delta (min) | Bar Price | Tick Price | Bar P&L% | Tick P&L% |
|--------|-------|----------|-----------|-------------|-----------|------------|----------|----------|

Plus summary statistics:
- How many signals fired earlier with tick-level? Later? Same bar?
- Median/mean time delta
- Aggregate P&L difference

### Key Detail: Building the Tick-Level DataFrame

The shadow collector stores minute bars as:
```json
{"t": "...", "o": 150.1, "h": 150.5, "l": 149.9, "c": 150.3, "v": 50000,
 "uptick": 30000, "downtick": 20000, "delta": 10000}
```

For the VDD calculation, we need:
- **OHLCV**: use the same bar OHLCV for both methods (so price series is identical)
- **Volume delta**: bar-based uses `_compute_volume_delta(df)` on the OHLCV;
  tick-level uses the shadow collector's `uptick`/`downtick` fields directly

This isolates the variable: same price path, different volume classification.

---

## Phase 2: Portfolio Simulation (if Phase 1 warrants it)

**Goal**: Simulate a counterfactual portfolio (B) that uses tick-level VDD exits,
starting from the same initial state as the real portfolio (A), and track how the
two paths diverge over time.

### How Paths Diverge

1. A VDD exit fires at a different time in B → position closed at a different price
2. Capital freed at different times → different replacement stocks bought
3. Replacement stocks may trigger VDD at different times → further divergence
4. Compounding effects accumulate over the portfolio's lifetime

### The Missing-Data Problem

Portfolio B may buy stocks that weren't in portfolio A, and therefore might not have
shadow tick data. Mitigations:
- Shadow data is **global** (not per-portfolio) — 147 symbols have data. If any
  portfolio tracked the symbol, the data exists.
- For symbols without tick data, fall back to bar-based VDD (and flag which positions
  used which method)
- Track the fraction of B's positions that used tick vs bar data

### Implementation Notes

- Replay the portfolio chronologically from 2026-03-05
- At each exit divergence point, fork: A continues as recorded, B takes the new path
- Use the same acquisition pipeline (confidence, ranking, allocation) for B's replacements
- Report: total P&L difference, per-position attribution, data coverage stats

---

## Open Questions

- [x] Does the shadow collector's minute bar OHLCV match the bar data source?
      → Moot for now — coverage too sparse. When coverage improves, verify timestamps align.
- [x] Are there edge cases where shadow data starts mid-day?
      → Yes, this is the **primary problem**. See Phase 1 results below.
- [ ] For Phase 2: replay acquisition logic the same way the existing backtest does
      (using recorded snapshot scores and the same ranking/allocation pipeline).

---

## Phase 1 Results (2026-03-09)

### Data Quality Problem

**The shadow collector data is far too sparse for a meaningful comparison.**

The portfolio ran from 2026-03-05 (Thursday) through 2026-03-09 (Monday), spanning
3 trading days (Fri 03/06, Mon 03/09; Sat/Sun have no market data). Shadow data
coverage in the signal-relevant window (entry - lookback through exit) averaged only
**4.1%** across all 33 VDD-exited positions. Zero positions met the 50% coverage
threshold for trustworthy results.

**Root causes:**

1. **Portfolio started late on 03/05** — shadow collector began streaming that
   afternoon, capturing only 2 bars for AMPX, zero for most symbols.
2. **03/06 (Friday) had partial coverage** — streaming started mid-day for many
   symbols (3-37 bars out of ~390 possible per full trading day).
3. **03/07 and 03/08 are weekend** — daily JSON files exist but contain stale copies
   of 03/06 data (save_daily ran but no new streaming data arrived).
4. **03/09 (Monday) had the best coverage** — ~190 bars for actively-traded symbols,
   but streaming started at ~11:08 ET (missing first 1.5 hours after market open).

**JSONL bar log files** (`bars/` subdirectory) only exist for dates with actual
streaming: typically `2026-03-06.jsonl` and `2026-03-09.jsonl`. The daily JSON
summaries for 03/07 and 03/08 are duplicates of 03/06 data.

### Raw Results

Script: `scripts/vdd_comparison.py`

Of 33 VDD-exited positions (all with <50% coverage):
- **26 (84%)**: Same bar — tick-level matched bar-based (because ~96% of bars had
  no shadow data and fell back to bar-based classification)
- **3 (10%)**: Tick fired later (HOOD +4112min, ALV +112min, WLY +15min) — likely
  artifacts of sparse data shifting a few bars' classification
- **2 (6%)**: Tick fired earlier (LLY -4039min, MA -1min) — same artifact concern
- **2**: Neither method signaled (KKR, BMY)

Average P&L difference: **-0.03%** (not meaningful given data quality).

### Conclusion

**This comparison is inconclusive.** The shadow data coverage is too low to
distinguish real signal differences from noise introduced by sparse tick data.

### Second Run: `lc_d58c66d24787` (2026-03-09, same-day exits)

Tested with a newer portfolio started the same day (03/09). 9 VDD-exited positions,
all entered and exited on 03/09. Results were identical: **3% average relevant
coverage**, all 7 comparable positions showed same-bar signals.

### Root Cause: Schwab Stream Drops During Market Hours

Investigation revealed the shadow collector's data is almost entirely from
**extended hours** (after 4 PM ET). For NVDA on 03/09:
- Total bars: 241
- Trading hours (09:30-16:00): **10 bars** (scattered: 12:08-12:13, 13:24-13:25, 15:57-15:58)
- Extended hours (16:00+): **231 bars** (continuous)

This pattern is consistent across all symbols. The Schwab WebSocket stream appears
to disconnect during market hours and is not automatically reconnected. The
`schwab_client.py` `start_stream()` method has no reconnection logic — if the
WebSocket drops, streaming stops until the next explicit `start_stream()` call.

### Path Forward

**Before this comparison can be meaningful, two things must happen:**

1. **Fix Schwab stream reliability** — add reconnection/heartbeat logic to
   `schwab_client.py` so the WebSocket stays connected through market hours.
   Without this, the shadow collector will never have continuous trading-hour data.

2. **Wait for full-coverage data** — once streaming is reliable, run the portfolio
   for at least a full trading week. Then re-run `scripts/vdd_comparison.py` on
   positions with >50% relevant coverage.

The comparison script and infrastructure are ready. The blocker is stream reliability.

---

## Phase 3: tick_collector-Based Comparison (2026-03-11, updated 2026-03-12)

### Infrastructure

The `tick_collector` service replaced the shadow collector for data collection.
Key improvements:
- **Every L1 update persisted** in TimescaleDB (not just 1-minute snapshots)
- **Lee-Ready classification** (bid/ask midpoint) instead of simple tick rule
- **Sub-minute bucketing** at query time (15s, 30s, 60s)
- **Three volume modes**: `proportional`, `visible_only`, `bar_binary`

### The Comparison Script

**`scripts/vdd_comparison.py`** — runs three phases:

1. **Phase 1 — Coverage**: For each exited position across all portfolios, checks tick
   data availability in TimescaleDB. Requires ≥10 trades and ≥30% minute coverage.
2. **Phase 2 — Side-by-side**: Default config (60s bucket, proportional, lookback=80).
   Bar-based uses `_get_ohlcv_1m()` from `backtest.py` — the exact same Schwab →
   yfinance infrastructure with disk cache that live trading and backtesting use.
   Tick-based queries TimescaleDB via `tick_collector/vdd.py`.
3. **Phase 3 — Multi-dimensional sweep**: 3 bucket sizes × 3 volume modes × 4 lookback
   periods = 36 configurations, each tested against all usable positions.

Usage: `uv run python scripts/vdd_comparison.py [live_config_id ...]`
(defaults to 4 portfolios if no args)

### Data: 4 Portfolios, 206 Positions

Tested across four portfolios to maximize sample:
- `lc_d58c66d24787`, `lc_72f6df86086e`, `lc_ab8aa7f25745`, `lc_dd6b0b29a8ea`
- **206 total exited positions** (154 signal, 22 guard_stop, 30 other)
- **47 usable** (had sufficient tick data), **159 skipped** (no tick data — entered
  before tick_collector was running, or symbol not in tick_collector's symbol list)
- Tick data primarily covers March 11, 2026 (tick_collector started March 10 with
  a mid-day gap; March 11 has solid coverage)

### Phase 2 Results: Default Config (60s bucket, proportional, lookback=80)

**11 positions had both bar-based and tick-based signals.**

| Symbol | Bar Exit (ET) | Tick Exit (ET) | Delta | Bar P&L | Tick P&L | Actual P&L |
|--------|--------------|----------------|-------|---------|----------|------------|
| MS     | 03/11 12:41  | 03/11 12:41    | 0m    | -0.85%  | -0.88%   | -0.85%     |
| MS     | 03/11 12:41  | 03/11 12:41    | 0m    | -0.85%  | -0.88%   | -0.85%     |
| MOS    | 03/11 13:32  | 03/11 11:03    | -149m | +7.28%  | +7.12%   | +7.28%     |
| MOS    | 03/11 13:32  | 03/11 11:03    | -149m | +6.56%  | +6.40%   | +6.56%     |
| ACTG   | 03/11 12:58  | 03/11 12:58    | 0m    | +5.93%  | +5.93%   | +4.45%     |
| TMC    | 03/11 12:57  | 03/11 13:16    | +19m  | +3.52%  | +3.36%   | +3.52%     |
| TSLA   | 03/11 15:04  | 03/11 15:04    | 0m    | -0.94%  | -0.94%   | -0.94%     |
| TSLA   | 03/11 15:04  | 03/11 15:04    | 0m    | +1.41%  | +1.41%   | +1.33%     |
| GD     | 03/11 13:28  | 03/11 11:37    | -111m | -1.38%  | -1.07%   | -1.37%     |
| XOM    | 03/10 18:45  | 03/11 09:38    | +893m | -0.56%  | -0.14%   | +1.34%     |
| UPST   | 03/11 14:43  | 03/11 14:46    | +3m   | +0.45%  | +0.63%   | +0.19%     |

**Summary:**
- **Bar avg P&L: +1.87%** (median +0.45%)
- **Tick avg P&L: +1.90%** (median +0.63%)
- **P&L diff: +0.03%** — essentially identical
- **Timing**: 5 same (±30s), 3 tick earlier, 3 tick later. **Median delta: 0 min.**
- **20 positions** where bar signaled but tick did NOT (insufficient tick history
  for the lookback window — tick_collector hasn't been running long enough)

**Notable cases:**
- **MOS**: tick fired **149 min earlier** — caught the divergence much sooner, though
  both ended highly profitable (+7.12% vs +7.28%)
- **GD**: tick fired **111 min earlier** — earlier exit avoided more loss (-1.07% vs -1.38%)
- **XOM**: bar fired at 18:45 (after-hours, wouldn't be acted on), tick fired next
  morning at 09:38. Tick P&L was better (-0.14% vs -0.56%), but both missed the
  actual +1.34% the position achieved.
- **MS, ACTG, TSLA**: identical timing — validates that when both methods have data,
  they agree on the signal bar

### Phase 3 Results: Multi-Dimensional Sweep (36 configurations)

Top 10 configurations ranked by average P&L at tick-based VDD exit:

| Bucket | Mode          | Lookback | Signals | Avg P&L | Med P&L | Std P&L |
|--------|---------------|----------|---------|---------|---------|---------|
| 30s    | visible_only  | 40m      | 14/47   | +3.96%  | +3.59%  | 6.47%   |
| 30s    | visible_only  | 100m     | 4/47    | +3.37%  | +3.55%  | 3.59%   |
| 30s    | proportional  | 100m     | 4/47    | +3.33%  | +3.29%  | 3.61%   |
| 15s    | proportional  | 80m      | 4/47    | +3.26%  | +3.92%  | 1.21%   |
| 30s    | bar_binary    | 60m      | 12/47   | +2.93%  | +0.11%  | 7.18%   |
| 15s    | bar_binary    | 60m      | 6/47    | +2.90%  | +1.63%  | 2.86%   |
| 30s    | proportional  | 80m      | 9/47    | +2.76%  | +3.90%  | 2.92%   |
| 15s    | visible_only  | 60m      | 5/47    | +2.76%  | +1.35%  | 3.39%   |
| 60s    | visible_only  | 40m      | 15/47   | +2.54%  | -0.14%  | 6.28%   |
| 30s    | visible_only  | 80m      | 5/47    | +2.46%  | +0.19%  | 3.67%   |

**Bar-based baseline** (lookback=80, inter-bar tick rule): **avg P&L +0.20%**

Bottom 5 (worst performing):

| Bucket | Mode          | Lookback | Signals | Avg P&L |
|--------|---------------|----------|---------|---------|
| 15s    | visible_only  | 100m     | 1/47    | -0.88%  |
| 15s    | proportional  | 100m     | 4/47    | -0.83%  |
| 30s    | bar_binary    | 80m      | 3/47    | -0.33%  |
| 30s    | bar_binary    | 100m     | 2/47    | +0.30%  |
| 15s    | visible_only  | 80m      | 1/47    | +0.46%  |

### Key Observations

**Caveats first**: Sample size is limited (47 usable positions, mostly from one trading
day). Configs with few signals (1-4) are unreliable. These are early observations, not
conclusions.

1. **When both methods have data, they largely agree.** 5 of 11 positions with both
   signals fired on the exact same bar (0 min delta). Median delta was 0 minutes.
   This validates the tick pipeline implementation.

2. **Tick-based VDD can fire significantly earlier.** MOS (-149m) and GD (-111m) are
   the standout cases. The tick method detected the volume-delta divergence while the
   bar-based method was still accumulating evidence. This is the core thesis for
   tick-based VDD.

3. **Shorter lookbacks outperform longer ones.** The best configs cluster around
   40-60 minute lookbacks. The default 80-minute lookback is middle-of-the-road.
   With tick-level volume classification, the signal is cleaner and doesn't need
   as long a lookback to detect divergence.

4. **`visible_only` slightly edges out `proportional`.** The top config is 30s /
   visible_only / 40m. Proportional distribution (extrapolating unclassified volume
   using the uptick/downtick ratio) may add noise — the directly classified trades
   alone may be a cleaner signal.

5. **`bar_binary` on tick data is viable but volatile.** It performs mid-range on
   average but has the highest standard deviation — expected since it throws away
   intra-bar information.

6. **Finer buckets (15s, 30s) find fewer signals but with higher average P&L.**
   The 15s configs often found only 1-4 signals (insufficient for conclusions) but
   those signals tended to be high quality. The 30s bucket seems like a sweet spot
   between signal count and quality.

7. **The big gap is coverage, not methodology.** 20 positions had bar-based signals
   but no tick-based signal — the tick_collector simply hasn't been running long
   enough. With more data, the tick method should match or exceed bar-based signal
   detection rates.

### Limitations

1. **Sample size**: 47 usable positions from one primary trading day (March 11).
   Many configurations had <5 signals — not enough for statistical significance.
2. **Survivorship bias in coverage**: Positions with tick data are biased toward
   recent entries (March 10-11). Earlier entries lack tick data entirely.
3. **No monitoring-window metadata**: Can't distinguish "system was actively trading"
   from "system was off" — using market-hours heuristic (9:30-16:00 ET) as proxy.
4. **Deduplication imperfect**: Same position may appear in multiple portfolios
   with slightly different entry prices (different accounts). Dedup by
   (symbol, entry_time) catches most but not all.

### Trade-Count Bucketing (Tick Clock)

All results above use **time-based** bucketing (fixed 15s/30s/60s intervals). This
creates a problem for thinly-traded stocks: a 30s bucket might contain only 1-2 L1
updates, making the volume classification meaningless. The `min_trades_per_bucket`
filter removes these, but that creates gaps in the time series and stretches the
effective lookback window.

**Trade-count bucketing** solves this by grouping a fixed number of L1 updates into
each bar. Every bar has the same statistical weight regardless of liquidity:

| Stock liquidity | L1 rate  | trades_per_bar=20 | Effective bar width |
|----------------|----------|-------------------|---------------------|
| High (TSLA)    | ~50/min  | 20 trades         | ~24s                |
| Medium (GD)    | ~15/min  | 20 trades          | ~80s                |
| Low (PELI)     | ~3/min   | 20 trades          | ~7min               |

This is "tick clock" or "volume clock" bucketing — a standard technique in market
microstructure (see López de Prado, *Advances in Financial Machine Learning*). The
argument: information arrives per-trade, not per-second. Time-based bars oversample
quiet periods and undersample active ones.

**Implementation**: `get_vdd_bars_by_trades()` in `tick_collector/vdd.py`. Fetches raw
trades, assigns each to a bar group via `row_number // trades_per_bar`, then aggregates
OHLCV + uptick/downtick per group. Returns the same DataFrame format as time-based
`get_vdd_bars()`, so downstream signal detection is unchanged.

**Lookback with trade-count bars**: The lookback window is still specified in minutes
(`lookback_m`), but must be converted to a bar count based on the actual bar rate for
each symbol. For the comparison script, we can compute `lookback_bars` from the data:
`lookback_bars = int(lookback_m * 60 / median_bar_duration_s)`.

**Not yet tested** — added to the comparison script's TODO. Suggested sweep:
`trades_per_bar` in [10, 20, 30, 50] across the same volume modes and lookback periods.

### Next Steps

1. **Accumulate more data**: Run tick_collector for 1+ weeks to get meaningful sample
   (target: 100+ positions with >80% tick coverage each)
2. **Add monitoring-window metadata** to watches (when live_monitor was actively checking)
3. **Re-run comparison** with larger sample — current observations may shift
4. **Test trade-count bucketing**: Sweep `trades_per_bar` in [10, 20, 30, 50] —
   see TODO in `scripts/vdd_comparison.py`
5. **Test recommended config**: Deploy 30s / visible_only / 40m alongside current
   bar-based VDD in shadow mode (compute but don't act) to validate on live data
