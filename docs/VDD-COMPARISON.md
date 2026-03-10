# VDD Comparison: Bar-Based vs Tick-Level Volume Delta

> Comparing the bar-based (inter-bar tick rule) and tick-level volume delta methods
> for the Volume Delta Divergence (VDD) exit strategy, using real portfolio data
> from live config `lc_ab8aa7f25745`.
>
> See also: [VOLUME-DELTA-REALTIME.md](VOLUME-DELTA-REALTIME.md) for shadow collector architecture.
>
> Started: 2026-03-09

---

## Motivation

Portfolio `lc_ab8aa7f25745` has been running since 2026-03-05 using VDD exits with
`lookback=80` and the **bar-based** inter-bar tick rule. The shadow collector has been
simultaneously gathering **tick-level** volume delta data for all portfolio symbols.

We want to know: if we had used tick-level volume delta instead of bar-based, how would
the exit signals have differed? Is there a systematic pattern (earlier? later? random)?

## Background: The Two Methods

Both methods produce 1-minute bars with uptick/downtick volume splits. The difference
is **how volume is classified within each bar**:

| Aspect | Bar-Based (inter-bar tick rule) | Tick-Level (shadow collector) |
|--------|-------------------------------|------------------------------|
| **Classification** | Entire bar volume → uptick OR downtick | Each tick's volume classified independently |
| **Rule** | `Close_t > Close_{t-1}` → all uptick | `Price_tick > Price_{prev_tick}` → uptick |
| **Granularity** | 1 decision per bar (binary) | ~55 decisions per bar per minute |
| **Captures intra-bar reversals** | No | Yes |
| **Source** | OHLCV bars (yfinance/Schwab REST) | Schwab LEVELONE_EQUITIES stream |

The VDD signal fires when **both** conditions are true:
1. Price makes a new high over the previous `lookback` bars
2. Cumulative volume delta is **lower** than it was `lookback` bars ago

## What the Shadow Collector Actually Captures

Schwab streams ~1 price+volume update per second per symbol (raw ticks). The shadow
collector does two things with each tick:

1. **Accumulates running totals** — every tick updates cumulative uptick/downtick
   counters in real-time (in memory)
2. **Snapshots into 1-minute bars** — at each minute boundary, it captures the
   minute's OHLCV + uptick/downtick split and writes it to disk (JSONL)

**Only the 1-minute bar snapshots are persisted.** The ~60 individual ticks within
each minute are consumed in real-time and then discarded. This means:

- **Real-time** (live collector in memory): VDD can be checked at any moment — if
  price makes a new high at 10:00:23 and cumulative delta is declining, it detects
  that immediately, not at 10:01:00.
- **From saved data** (retrospective analysis like this comparison): we can only
  evaluate VDD at 1-minute bar boundaries, because that's what's on disk.

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

### Future: Sub-Minute VDD from Saved Data

If we want to evaluate VDD at finer granularity from saved data (e.g., every second
instead of every minute), we would need to modify the shadow collector to persist
finer-grained snapshots — either raw tick data or sub-minute bars (e.g., 5-second
or 15-second intervals). This would increase storage but enable retrospective
sub-minute signal analysis, potentially catching VDD signals up to 59 seconds earlier
than 1-minute bar resolution allows. Not needed for the current comparison, but worth
considering for a future iteration.

## Lookback Translation

**No translation needed.** Both methods produce 1-minute bars, so `lookback=80` means
"80 one-minute bars" (= 80 minutes) in both cases. The only difference is the
uptick/downtick split within each bar, not the bar boundaries or time scale.

## Data Availability

**Portfolio**: `lc_ab8aa7f25745` (started 2026-03-05)
- 56 total watches: 20 holding, 36 exited
- 33 exits by VDD signal, 3 by guard_stop
- Shadow tick data covers **all 55 portfolio symbols** (100%)
- Shadow data covers **all exit dates** (100%)
- Shadow data stored globally by symbol at `~/.cache/alpaca-news/volume_delta_shadow/{SYMBOL}/`

Date range of shadow data varies by symbol (depends on when each was first tracked),
but all symbols have data covering their full holding period in this portfolio.

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

- [ ] Does the shadow collector's minute bar OHLCV match the bar data source (yfinance/Schwab REST)?
      If not, we need to use OHLCV from one source consistently and only swap the volume classification.
- [ ] Are there edge cases where shadow data starts mid-day (symbol added after market open)?
      If so, the first partial day may have fewer bars than the bar-based source.
- [ ] For Phase 2: replay acquisition logic the same way the existing backtest does
      (using recorded snapshot scores and the same ranking/allocation pipeline).

---

## Results

*To be filled in after running the analysis.*
