# Real-Time Volume Delta: Shadow Mode & Data Source Analysis

> Hub document for real-time volume delta computation, shadow mode collector,
> and the transition from backtest bar-based approximation to tick-level signals.
>
> Last updated: 2026-03-10

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Current Status: What's Wired Up Today](#current-status-whats-wired-up-today)
3. [Data Source Comparison](#data-source-comparison)
3. [Live Test Results](#live-test-results)
4. [Shadow Mode Collector](#shadow-mode-collector)
5. [Bar-Based vs Tick-Level: 10-Symbol Study](#bar-based-vs-tick-level-10-symbol-study)
6. [Recommendations](#recommendations)
7. [File Map](#file-map)

---

## Problem Statement

The backtest uses the **inter-bar tick rule** to compute volume delta from 1-minute
OHLCV bars. Each bar's *entire* volume is classified as uptick or downtick based on
whether close > or < previous bar's close. This is a binary approximation.

For live trading, we can instead classify volume at **tick granularity** — each streaming
price+volume update is independently classified. This is more accurate but may produce
different signal timing.

**Key question:** If we switch from bar-based to tick-level volume delta for the
Volume Delta Divergence (VDD) exit strategy, how much do the signals differ?
Can we trust the backtest's tuned parameters (lookback=80) in a tick-level environment?

---

## Current Status: What's Wired Up Today

**Both backtesting and live trading use the same bar-based VDD computation.**

The live exit monitor (`live_monitor.py`) calls `evaluate_exit()` from `backtest.py`,
which uses `_compute_volume_delta()` — the inter-bar tick rule on 1-minute OHLCV bars.
Each bar's entire volume is classified as uptick or downtick based on a single binary
decision (`Close > prev Close`). This is identical to what backtesting uses. The live
monitor polls once per minute (`interval_s=60`), fetching fresh 1-minute bars and
running `evaluate_exit()` for every active position each cycle.

For the VDD formula and parameter ranges, see
[BACKTEST-STRATEGIES.md § Volume Delta Divergence](BACKTEST-STRATEGIES.md#15-volume-delta-divergence-volume_delta_divergence).
For an empirical comparison of bar-based vs tick-level signal timing, see
[VDD-COMPARISON.md § Background: The Two Methods](VDD-COMPARISON.md#background-the-two-methods).

The tick-level alternative (`VolumeDeltaCollector.check_vdd_signal()` in
`volume_delta_shadow.py`) exists and classifies volume at ~55 updates/minute per symbol
from Schwab's LEVELONE_EQUITIES stream. **It is not connected to exit decisions.** It
runs as a shadow collector for comparison purposes only.

| Aspect | Backtesting | Live Exit Monitor | Shadow Collector |
|--------|------------|-------------------|------------------|
| **Entry point** | `_run_volume_delta_divergence()` | `evaluate_exit()` (same backtest code) | `check_vdd_signal()` |
| **Volume delta** | Inter-bar tick rule (binary) | Inter-bar tick rule (binary) | Tick-level (~55/min) |
| **VDD formula** | `close >= rolling_max AND cum_delta < lagged_delta` | Same | Same |
| **Used for exits?** | Yes (backtest) | Yes (live) | No (shadow only) |
| **Code path** | `backtest.py` → `_compute_volume_delta()` | `backtest.py` → `_compute_volume_delta()` | `volume_delta_shadow.py` → `TickAccumulator` |

**Bottom line:** Backtest and live produce identical VDD signals given the same bar data.
The tick-level method uses the same formula but classifies volume differently, which can
cause ~40% of signals to fire at different times (see
[VDD-COMPARISON.md](VDD-COMPARISON.md) for the empirical analysis).

---

## Data Source Comparison

### Free Tier Capabilities

| Feature | Schwab (free) | Alpaca (free/IEX) | yfinance (free) |
|---------|--------------|-------------------|-----------------|
| **Streaming protocol** | WebSocket (LEVELONE_EQUITIES) | WebSocket (StockDataStream) | WebSocket |
| **Data coverage** | All US exchanges (aggregated) | IEX only (~2-3% of volume) | Yahoo Finance (aggregated) |
| **Max symbols per stream** | **500** per subscription | 30 (trades/quotes), unlimited (bars) | No documented limit |
| **Concurrent connections** | 1 per account | 1 per endpoint | Unknown (appears unlimited) |
| **Update frequency** | ~1/sec (QoS 2), up to 2/sec (QoS 0) | Real-time per trade | ~1-2/sec |
| **Fields for volume delta** | `last_price` (3) + `total_volume` (8) + `last_size` (9) + `trade_time` (35) + `last_mic_id` (41) | trades: price + size; bars: OHLCV | price + day_volume |
| **Volume accuracy** | Full exchange (best) | IEX only (~2-3%, unreliable) | Full exchange (good) |
| **Monthly cost** | $0 (with brokerage account) | $0 | $0 |
| **Authentication** | OAuth2 (schwabdev auto-manages) | API key + secret | None |
| **Already integrated** | Yes (`schwab_client.py`) | News only | Yes (test code) |

### Verdict: **Schwab is the clear winner** for real-time volume delta

1. **Full exchange coverage** — all exchanges aggregated, vs IEX's ~2-3%
2. **500 symbols per stream** — far more than our max 20 concurrent positions
3. **Already integrated** — streaming code exists in `schwab_client.py`
4. **Free** — no additional cost
5. **Reliable** — consistent ~1 update/sec per symbol

Alpaca free tier is **not suitable** for volume delta — IEX captures only ~2-3% of
actual trading volume, making volume-based signals unreliable. Alpaca remains our
**news source** and will be our **trading execution** provider.

yfinance streaming works but has no guaranteed SLA and less documentation.

---

## Live Test Results (2026-03-05, Market Hours)

### Test 1: Shadow Collector (180 seconds, 5 symbols)

Schwab LEVELONE_EQUITIES + yfinance WebSocket + 1-min bar comparison.

**Update frequency (Schwab):**
| Symbol | Updates | Mean Interval | Updates/min |
|--------|---------|--------------|-------------|
| SPY | 30 | 1.101s | 56.4 |
| AAPL | 28 | 1.182s | 52.6 |
| TSLA | 30 | 1.101s | 56.4 |
| NVDA | 29 | 1.140s | 54.5 |
| AMZN | 28 | 1.182s | 52.6 |

**~55 updates/minute per symbol** = ~55x more granular than 1-min bars.

**Direction agreement (Schwab tick vs bar-based):**
| Symbol | Schwab Tick | Bar-Based | yfinance Tick | Agreement |
|--------|------------|-----------|---------------|-----------|
| SPY | BEAR (-0.196) | BEAR (-0.068) | BEAR (-0.320) | All agree |
| AAPL | BEAR (-0.057) | BEAR (-0.183) | BEAR (-0.504) | All agree |
| TSLA | BULL (+0.087) | BULL (+0.142) | BEAR (-0.273) | Schwab+bar agree, yf disagrees |
| NVDA | BULL (+0.123) | BULL (+0.033) | BEAR (-0.163) | Schwab+bar agree, yf disagrees |
| AMZN | BULL (+0.237) | BEAR (-0.031) | BEAR (-0.200) | Schwab disagrees with bar+yf |

**Key observations:**
- Schwab tick-level agreed with bar-based direction on **4 of 5 symbols** (80%)
- yfinance tick-level agreed with bar-based on only **3 of 5** (60%)
- Imbalance magnitude differs significantly between methods (tick vs bar)
- Schwab tick is more responsive — detects direction changes before bars close
- The AMZN disagreement (tick=BULL, bar=BEAR) shows where tick-level granularity
  captures intra-bar dynamics that bar-level misses. Bar imbalance was very small
  (-0.031), meaning it was borderline — tick-level may have been more accurate.

### Test 2: 10-Symbol Bar-Based Comparison (5 days)

Inter-bar tick rule vs Close Position Formula on 1-minute bars.

| Symbol | Bars | Direction Agree | Rolling Imb Corr | Rolling Imb Dir Agree |
|--------|------|----------------|-------------------|----------------------|
| SPY | 1616 | 80.9% | 0.619 | 71.1% |
| AAPL | 1615 | 84.5% | 0.683 | 78.1% |
| TSLA | 1616 | 85.6% | 0.776 | 77.6% |
| NVDA | 1616 | 85.0% | 0.778 | 74.4% |
| AMZN | 1616 | 82.4% | 0.774 | 79.3% |
| MSFT | 1616 | 84.4% | 0.820 | 78.9% |
| META | 1616 | 84.1% | 0.795 | 79.2% |
| GOOG | 1616 | 84.3% | 0.753 | 76.5% |
| AMD | 1616 | 85.3% | 0.785 | 78.7% |
| INTC | 1616 | 83.9% | 0.741 | 74.8% |
| **Average** | | **84.0%** | **0.752** | **76.9%** |

**VDD Signal overlap (inter-bar vs close-position):**
| Symbol | Both Agree | Only Inter-bar | Only Close-Position |
|--------|-----------|---------------|-------------------|
| SPY | 11 | 14 | 28 |
| AAPL | 5 | 11 | 5 |
| TSLA | 12 | 6 | 14 |
| NVDA | 7 | 9 | 25 |
| AMZN | 19 | 5 | 21 |

**VDD signal timing differences:**
- Median difference: 0 bars (signals that overlap hit on the same bar)
- But ~40% of inter-bar signals have no matching close-position signal within ±5 bars
- This means ~40% of exits would occur at meaningfully different times

---

## Shadow Mode Collector

### Architecture

```
Schwab LEVELONE_EQUITIES stream
    │
    ├──→ StreamState (existing: latest fields per symbol)
    │
    └──→ VolumeDeltaCollector (new: tick-level accumulation)
            │
            ├── Per-symbol TickAccumulator
            │   ├── Running uptick/downtick totals
            │   ├── 1-minute bar snapshots (tick-aggregated)
            │   ├── Cumulative delta series
            │   └── VDD signal checker
            │
            └── Daily persistence → ~/.cache/alpaca-news/volume_delta_shadow/
```

### Key Files

| File | Purpose |
|------|---------|
| [`trader/market/volume_delta_shadow.py`](trader/market/volume_delta_shadow.py) | `VolumeDeltaCollector` + `TickAccumulator` |
| [`trader/market/schwab_client.py`](trader/market/schwab_client.py) | Schwab stream with collector hook |
| [`tests/test_shadow_collector.py`](tests/test_shadow_collector.py) | Live test: tick vs bar comparison |
| [`tests/test_volume_delta_comparison.py`](tests/test_volume_delta_comparison.py) | Bar-based method comparison |

### Usage

```python
from trader.market.volume_delta_shadow import VolumeDeltaCollector

# Create and start collector
collector = VolumeDeltaCollector()
collector.start()
collector.add_symbol("AAPL")

# Attach to Schwab stream
schwab_client.attach_volume_delta_collector(collector)
schwab_client.start_stream(["AAPL"])

# ... stream runs, collector accumulates tick-level data ...

# Check current state
snap = collector.snapshot("AAPL")
# {"uptick_vol": 25M, "downtick_vol": 24.5M, "net_delta": 500K, "imbalance": 0.0102}

# Check VDD signal (uses tick-aggregated minute bars)
vdd = collector.check_vdd_signal("AAPL", lookback=80)
# {"signal": True, "price_new_high": True, "delta_declining": True, ...}

# Get minute bars with tick-level volume delta
bars = collector.get_minute_bars("AAPL")
# [{"t": "...", "o": 150.1, "h": 150.5, "l": 149.9, "c": 150.3, "v": 50000,
#   "uptick": 30000, "downtick": 20000, "delta": 10000}, ...]

# Save day's data for later analysis
collector.save_all()
# ~/.cache/alpaca-news/volume_delta_shadow/AAPL/2026-03-05.json
```

### Integration with Watch System

The collector and stream are initialized once at startup, then symbols are
added/removed dynamically as positions open and close:

```python
# At startup: create collector and attach to Schwab stream
collector = VolumeDeltaCollector()
collector.start()
schwab_client.attach_volume_delta_collector(collector)

# Resume tracking for any existing active watches
for watch in get_active_watches():
    collector.add_symbol(watch.symbol)
    schwab_client.start_stream([watch.symbol])  # ADD to existing stream

# When a new BUY signal fires:
def on_watch_created(watch):
    collector.add_symbol(watch.symbol)
    schwab_client.start_stream([watch.symbol])  # ADD to stream

# When a position is closed:
def on_watch_exited(watch):
    collector.save_daily(watch.symbol)  # persist tick data before removing
    collector.remove_symbol(watch.symbol)
    # Stream keeps running — harmless to leave symbol subscribed

# At market close (or daily cleanup):
collector.save_all()
collector.reset_all()
```

### Schwab Stream Capacity

| Constraint | Limit | Our Usage |
|-----------|-------|-----------|
| Symbols per subscription | 500 | Max 20 (concurrent positions) |
| Concurrent connections | 1 per account | 1 (shared) |
| Update frequency | ~1/sec per symbol | ~55 updates/min per symbol |
| Data throughput | ~20 × 55 = 1100 updates/min | Well within capacity |

**500 symbols >> 20 max positions** — no practical limit on concurrent positions
from the streaming perspective.

### Dynamic Symbol Subscription

Schwab streaming supports **three subscription commands** on a single persistent
WebSocket connection:

| Command | Behavior |
|---------|----------|
| **`ADD`** (default) | Add new symbols to the existing subscription — doesn't affect symbols already streaming |
| **`SUBS`** | Replace the entire subscription set — overwrites everything |
| **`UNSUBS`** | Remove specific symbols from the subscription |

Our `start_stream()` method uses `ADD` by default, so you can call it repeatedly:

```python
# Open WebSocket + subscribe to initial symbols
schwab_client.start_stream(["AAPL", "TSLA"])

# Later, new position opened — just add to the same stream
schwab_client.start_stream(["NVDA"])
# Now streaming AAPL, TSLA, NVDA on one connection

# Position closed — leave it streaming (harmless) or unsubscribe
# (UNSUBS not currently exposed but trivially addable)
```

**No need for multiple connections or stream restarts.** The WebSocket opens
once and stays open; new symbols are added on the fly via `ADD` messages.
This is critical for live trading — when a new BUY signal fires, we just
add the symbol to the existing stream and start accumulating tick data
immediately.

### Saved Data Format

Each daily file: `~/.cache/alpaca-news/volume_delta_shadow/{SYMBOL}/{YYYY-MM-DD}.json`

```json
{
  "symbol": "AAPL",
  "date": "2026-03-05",
  "saved_at": "2026-03-05T20:01:00+00:00",
  "summary": {
    "uptick_vol": 25000000,
    "downtick_vol": 24500000,
    "net_delta": 500000,
    "imbalance": 0.0102,
    "direction": "BULL",
    "update_count": 23400,
    "minute_bars_count": 390
  },
  "minute_bars": [
    {
      "t": "2026-03-05T14:30:00+00:00",
      "o": 258.50, "h": 258.90, "l": 258.30, "c": 258.75,
      "v": 125000,
      "uptick": 75000, "downtick": 50000, "delta": 25000
    }
  ]
}
```

Each minute bar contains both OHLCV **and** the tick-level uptick/downtick split.
This enables direct comparison with the backtest's inter-bar tick rule on the same
time window.

---

## Recommendations

### Phase 1 (Now): Shadow Data Collection

1. **Attach collector to orchestrator** — whenever the Schwab stream is active for
   watches, the collector accumulates tick-level data in parallel
2. **Save daily** — persist shadow data at market close for all tracked symbols
3. **No signal changes** — continue using bar-based VDD for actual decisions

### Phase 2 (After 1-2 weeks): Analysis

1. Compare tick-level vs bar-level VDD signals on the same time windows
2. Measure: how often does tick-level signal fire before bar-level?
3. Measure: does tick-level improve exit timing (better P&L)?
4. Check if lookback=80 is still optimal for tick-level data

### Phase 3 (After validation): Switch

1. If tick-level shows improvement, switch VDD computation to use collector data
2. Keep bar-level as fallback (in case Schwab stream disconnects)
3. May need to recalibrate lookback parameter

### Three Tiers of Volume Delta Accuracy (2026-03-10)

| Tier | Source | What You Get | Trade-Size Filtering |
|------|--------|-------------|---------------------|
| **1 (current)** | L1: `last_price` + `total_volume` differencing | Aggregate uptick/downtick per ~1s update | No — all trades lumped together |
| **2 (available now)** | L1 + fields 9, 35, 41: `last_size`, `trade_time`, `last_mic_id` | Per-update: last trade's price, size, time, exchange | Approximate — only sees ~30-50% of individual trades (L1 coalesces between updates) |
| **3 (ideal)** | `TIMESALE_EQUITY` per-trade feed | Every individual trade: price, size, time, exchange | Exact — every trade visible, full size filtering |

**Why Tier 2 is approximate**: LEVELONE_EQUITIES updates ~1/sec. If 20 trades
happen between updates, only the last trade's `last_size` is reported. The
`total_volume` jump captures aggregate volume, but individual trade sizes are
lost. Example: `total_volume` jumps by 5,000 but `last_size` = 200 — the other
4,800 shares came from trades we never saw individually.

**Tier 2 is still useful**: Even approximate large-trade detection is better than
none. If `last_size >= 1000`, you know at least one large trade happened. Combined
with `total_volume` differencing for aggregate flow, this provides a meaningful
hybrid signal. See [TICK-COLLECTOR.md](TICK-COLLECTOR.md) for the full plan.

### Data Source Strategy for Live Trading

```
┌─────────────────────────────────────────────────────────┐
│  Alpaca API                                             │
│  ├── News WebSocket (trigger source)                    │
│  ├── TradingClient (order execution, paper=True)        │
│  └── TradingStream (order fill notifications)           │
│                                                         │
│  Schwab API                                             │
│  ├── LEVELONE_EQUITIES stream (volume delta, prices)    │
│  ├── REST: quotes, candles, fundamentals, options       │
│  └── Volume Delta Shadow Collector (tick-level VDD)     │
│                                                         │
│  yfinance                                               │
│  └── Fallback: bars, fundamentals, news                 │
└─────────────────────────────────────────────────────────┘
```

---

## Known Issue: Schwab Stream Drops During Market Hours (2026-03-09)

During the VDD comparison analysis (see [VDD-COMPARISON.md](VDD-COMPARISON.md)), we
discovered that the Schwab LEVELONE_EQUITIES WebSocket **disconnects during regular
trading hours** and is not automatically reconnected. The shadow collector's data is
almost entirely from extended hours (after 4 PM ET).

**Evidence (NVDA, 2026-03-09):**
- Total shadow bars: 241
- Trading hours (09:30-16:00): **10 bars** (scattered fragments)
- Extended hours (16:00+): **231 bars** (continuous)
- Pattern is consistent across all ~147 tracked symbols

**Root cause:** `schwab_client.py` `start_stream()` creates a `schwabdev.Stream`
and calls `start()` once. There is no reconnection logic — if the WebSocket drops
mid-day, streaming silently stops until the next explicit `start_stream()` call.

**Update (2026-03-10):** Investigation of schwabdev internals revealed that
`_run_streamer()` **already has built-in reconnection** with exponential backoff
(2s → 4s → ... → 120s cap) and automatically re-subscribes all recorded
subscriptions on reconnect. The reconnection triggers on `ConnectionClosedError`
but NOT on silent disconnects (WebSocket stays open but no data flows). The
trading-hour dropouts may be silent disconnects that bypass this logic. Needs
market-hours testing to confirm. See [TICK-COLLECTOR.md](TICK-COLLECTOR.md)
for the ongoing investigation.

**Impact:**
- Shadow collector captures almost no trading-hour data — useless for VDD comparison
- Any live feature depending on Schwab streaming (real-time quotes, volume delta)
  is unreliable during market hours

**Fix needed:**
- [x] Investigate schwabdev's built-in reconnect capabilities — has exponential
      backoff + auto re-subscribe (2026-03-10)
- [ ] Test during market hours to see if built-in reconnection is sufficient
- [ ] If not: add heartbeat monitor (detect silent disconnects — no data for N
      seconds → force reconnect)
- [ ] Log reconnection events for debugging

---

## Open Items

- [x] Hook collector into orchestrator watch lifecycle (start on watch creation, save on seal)
- [ ] **Fix Schwab stream reliability** — reconnection logic (see above). This is the
      blocker for all streaming-dependent features.
- [ ] Add `/api/shadow/status` endpoint to dashboard (show collector state)
- [x] Build comparison analysis tool — `scripts/vdd_comparison.py` (done, blocked on data)
- [ ] Determine if Schwab QoS 0 (500ms) is worth requesting vs default QoS 2 (1000ms)
- [ ] Test with 20 concurrent symbols to verify stream stability at max capacity
