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
