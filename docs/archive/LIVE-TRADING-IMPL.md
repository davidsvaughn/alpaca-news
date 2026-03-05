# Live Trading Implementation Plan

> Hub document for the "go live" feature: mirroring backtest behavior in real-time.
> Created: 2026-03-05

---

## Vision

Activate a **LiveConfig** (the same parameters used in backtesting) and run it forward in real-time. As news arrives and snapshots are created, the system applies the same filters, allocation strategy, and exit strategy as backtest — but on live data. No LLM check-ins for exit decisions; exits are purely mechanical (same math as backtest).

---

## Architecture Overview

```
Alpaca News Feed
    |
    v
Snapshot Pipeline (Grok -> OpenAI -> Gemini)
    |
    v
Snapshot sealed (visible in Snapshots tab immediately)
    |
    v
LivePortfolioManager
    |-- Applies filters (same as backtest: confidence, price, volume, market cap, P/E, etc.)
    |-- Applies allocation strategy (fixed_dollar, max_positions, ranking_realloc, etc.)
    |-- Decision: BUY / SKIP / REPLACE
    |
    [BUY] --> Create Watch (status=holding)
    |          + Start streaming (Schwab + shadow collector)
    |          + Position visible in Positions tab
    |
    v
LiveExitMonitor (runs every ~60s)
    |-- For each "holding" watch:
    |     Fetch recent 1-min bars from Schwab
    |     Run exit strategy (same functions as backtest.py)
    |     Check guards (stop loss / take profit)
    |     If exit signal --> mark exited, record price/time/reason
    |
    |-- For each "cooling_off" watch:
    |     Check if cooling period has elapsed (market hours only)
    |     If expired --> seal watch, stop streaming (if no other watches need symbol)
    |
    v
Post-exit: streaming continues for configurable period (default 24 market hours)
    Purpose: save tick-level data for post-mortem comparison
```

---

## LiveConfig

Persisted configuration that mirrors backtest parameters:

```python
@dataclass
class LiveConfig:
    # Identity
    config_id: str              # unique ID
    name: str                   # user-friendly name
    active: bool                # is this config currently running?
    created_at: str             # ISO timestamp

    # Filters (same as backtest UI)
    filters: dict               # confidence_min, price_min/max, volume_min/max,
                                # market_cap_min/max, pe_min/max, etc.

    # Allocation
    allocation: str             # "none" | "fixed_dollar" | "max_positions" | "ranking_realloc"
    allocation_params: dict     # alloc_pct, max_pos, when_full, rank_method, composite_weight, etc.
    starting_capital: float     # portfolio starting amount

    # Exit strategy
    exit_strategy: str          # strategy name (e.g., "volume_delta_divergence")
    exit_params: dict           # strategy-specific params (e.g., lookback=80)

    # Guards
    guard_stop_pct: float       # hard stop loss % (0 = disabled)
    guard_target_pct: float     # hard take profit % (0 = disabled)

    # Timing
    min_hold: int               # minimum bars before exit checks
    price_delay_minutes: int    # delay after snapshot before "entry" (default 10)
    market_close: str | None    # "16:00", "17:30", "20:00", None (extended)

    # Post-exit
    cooling_off_market_hours: float  # hours of market time to keep streaming after exit (default 24)
```

**Storage**: SQLite table `live_configs` (JSON blob like watches). Only one config can be `active=True` at a time.

**API**:
- `POST /api/live/config` — create/update config
- `GET /api/live/config` — get active config
- `POST /api/live/config/{id}/activate` — activate
- `POST /api/live/config/{id}/deactivate` — deactivate
- `GET /api/live/configs` — list all saved configs

---

## Watch Model Changes

### New states
- `holding` — position open, exit strategy being evaluated
- `exited` — exit triggered, cooling_off period starts
- `cooling_off` — streaming continues, no position, data collection only
- `sealed` — done, streaming stopped

### Removed
- LLM check-in fields (`checkin_history` with agent data, `depth`, system_prompt, etc.)
- `retrospective` state (replaced by `cooling_off`)
- `monitoring_snapshot_ids`, `retrospective_snapshot_ids` (no longer needed)
- `retrospective_data` (replaced by simpler cooling_off tracking)

### Added
- `exit_strategy`: which strategy triggered the exit
- `exit_params`: strategy params at time of exit
- `cooling_off_until`: ISO timestamp when cooling_off expires
- `bar_history`: list of 1-min bars used for exit evaluation (for audit trail)
- `live_config_id`: which LiveConfig this watch was created under

### Kept
- `watch_id`, `symbol`, `status`, `entry`, `exit`, `created_at`
- `last_checkin_at` (repurposed: last time exit monitor evaluated this watch)

---

## WatchMonitor Disposition

The existing `WatchMonitor` in `watcher.py` with its LLM check-in system is **disabled but preserved**:

```python
# ============================================================
# LEGACY: LLM-based check-in system (disabled since 2026-03-05)
# Replaced by LiveExitMonitor which uses mechanical exit
# strategies (same as backtest). Kept for potential future use.
# Do NOT activate — the live system uses live_monitor.py instead.
# ============================================================
```

The `monitoring_loop()` function and `WatchMonitor` class remain in the file but are not imported or started by the orchestrator when the live system is active.

---

## LiveExitMonitor

Replaces WatchMonitor. Runs in a daemon thread.

### Check cycle (every ~60s during market hours)
```
for each watch where status == "holding":
    1. Fetch 1-min bars from Schwab (last N bars, enough for strategy lookback)
    2. Check guards first:
       - If Low <= entry * (1 - guard_stop_pct) --> EXIT (reason: "guard_stop")
       - If High >= entry * (1 + guard_target_pct) --> EXIT (reason: "guard_target")
    3. Check min_hold (skip exit strategy if < min_hold bars since entry)
    4. Run exit strategy function (from backtest.py, extracted for reuse)
    5. If exit signal --> EXIT (reason: strategy name)
    6. Update last_checkin_at

for each watch where status == "cooling_off":
    1. Compute elapsed market hours since exit
    2. If >= cooling_off_market_hours --> seal watch
```

### Bar data source
- Schwab 1-min bars via existing `MarketDataService`
- Same data source as backtest (full exchange volume, not IEX)
- Bars fetched fresh each cycle (not accumulated from stream)
- Shadow collector runs independently, saving tick-level data for comparison

### Exit strategy reuse
Extract from `backtest.py` the strategy runner functions so they can be called with:
```python
result = run_exit_strategy(
    strategy_name="volume_delta_divergence",
    params={"lookback": 80},
    bars=bars_df,          # 1-min OHLCV DataFrame
    entry_idx=entry_bar,   # index of entry bar
    entry_price=price,
    guards={"stop_pct": 5.0, "target_pct": 0.0},
    min_hold=5,
)
# Returns: (should_exit: bool, exit_price: float, reason: str, bars_held: int)
#   or None if no exit signal yet
```

---

## LivePortfolioManager

Evaluates new snapshots against the active LiveConfig.

### On new snapshot sealed:
```
1. Check if LiveConfig is active
2. Apply filters to this snapshot (same logic as _snapshot_rows_for_filters)
3. If snapshot doesn't pass filters --> skip (log reason)
4. Check allocation:
   a. Count current "holding" watches
   b. If capacity available --> BUY
   c. If at capacity + replace strategy:
      - Rank existing positions (by chosen method)
      - If new > weakest --> REPLACE (exit weakest, buy new)
      - Else --> SKIP
   d. If at capacity + no replace --> SKIP
5. If BUY:
   - Determine entry price (current price, or price at +delay_minutes)
   - Create Watch with status="holding"
   - Add symbol to Schwab stream + shadow collector
   - Publish "watch_created" event
```

### Capital tracking
- Track `available_capital` and `allocated_capital` per position
- Each position gets `starting_capital * alloc_pct / 100`
- When a position exits, its capital (+ P&L) returns to available pool

---

## Post-Exit Cooling Off

### Purpose
After exiting a position, continue collecting real-time stream data so we can later analyze:
- Would a different exit strategy have performed better?
- How does tick-level VDD compare to bar-level VDD?
- What happened to the stock after we sold?

### Implementation
- On exit: `watch.status = "cooling_off"`, compute `cooling_off_until`
- `cooling_off_until` = current time + N market hours (default 24)
- Market hours calculation: only count time during 9:30-16:00 ET (or extended hours if configured)
- Shadow collector keeps running for this symbol
- When `cooling_off_until` reached:
  - Seal watch
  - `collector.save_daily(symbol)` — flush shadow data
  - Remove symbol from stream (only if no other active watches need it)

### Configurable parameter
- `cooling_off_market_hours: float = 24.0` — in LiveConfig
- 24 market hours ~ 3.7 trading days (6.5h per day)
- User can adjust via API/UI

---

## Positions Tab (New UI)

### What it shows
- **Header**: Active LiveConfig name, status (running/stopped), capital summary
- **Open Positions**: List of `holding` watches
  - Symbol, entry price, entry time, current price, unrealized P&L %
  - Time held, exit strategy name, next check time
  - Progress bar toward min_hold
- **Recently Exited**: List of `cooling_off` watches
  - Symbol, entry/exit price, realized P&L %, exit reason
  - Cooling off progress (time remaining)
  - "Would still be holding" indicator (if exit strategy hasn't re-fired)
- **Closed**: List of `sealed` watches (most recent first)
  - Full trade summary: entry/exit, P&L, hold time, exit reason
- **Portfolio Stats**:
  - Total P&L (realized + unrealized)
  - Win rate, avg win, avg loss
  - Capital deployed vs available
  - Positions taken / skipped / replaced today

### Future enhancements (not now)
- Equity curve chart (like backtest but real-time)
- Per-position price chart with entry/exit markers
- Side-by-side comparison: live exit vs what backtest would have done

---

## Implementation Phases

### Phase 1: Foundation (current)
- [ ] Extract exit strategy functions from backtest.py for standalone use
- [ ] Create LiveConfig model + SQLite storage
- [ ] Create LiveConfig API endpoints
- [ ] Modify Watch model (add cooling_off state, simplify)
- [ ] Fence off legacy WatchMonitor code

### Phase 2: Core Live Loop
- [ ] Implement LiveExitMonitor (daemon thread, bar-based exit evaluation)
- [ ] Implement LivePortfolioManager (filter + allocate new snapshots)
- [ ] Wire into orchestrator (replace WatchMonitor startup)
- [ ] Integrate with Schwab streaming (add/remove symbols)
- [ ] Integrate with shadow collector (continue during cooling_off)

### Phase 3: Positions UI
- [ ] Backend: API endpoints for positions data
- [ ] Frontend: Positions tab with open/exited/closed sections
- [ ] Frontend: LiveConfig editor (save backtest config as live config)
- [ ] Frontend: Activate/deactivate live config

### Phase 4: Post-Exit Analysis
- [ ] Market hours calculation for cooling_off expiry
- [ ] Save shadow collector data on seal
- [ ] API endpoint for post-mortem comparison data
- [ ] UI for comparing live exit vs alternative strategies

### Phase 5: Real Orders (Future — Alpaca Integration)
- [ ] Alpaca paper trading API integration
- [ ] Replace virtual buy/sell with actual orders
- [ ] Guard stops as server-side Alpaca stop orders
- [ ] Fill price tracking (actual vs expected)
- [ ] Risk controls (daily loss limit, max trades, etc.)

---

## Open Questions

1. **Entry price timing**: In backtest, entry is at `snapshot_time + price_delay_minutes`. In live, do we:
   - (a) Wait delay_minutes then fetch price and create watch? (more faithful to backtest)
   - (b) Create watch immediately at current price? (simpler, more realistic)
   - Current plan: (a) to mirror backtest exactly. Use a delayed task.

2. **Multiple LiveConfigs**: Should we support running multiple configs simultaneously (paper A/B testing)? For now, one active config. Revisit later.

3. **Historical backfill**: When activating a LiveConfig, should we retroactively evaluate recent snapshots that would have passed filters? Or only forward-looking? Current plan: forward-only.

4. **Extended hours**: Backtest supports market_close options. Live should too, but Schwab streaming during extended hours may have gaps. Need to test.

---

## Design Choices Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Exit method | Mechanical (same as backtest) | User wants live to mirror backtest exactly; LLM exits were unpredictable |
| Bar data source | Schwab 1-min fetch | Matches backtest data source; full exchange volume |
| Shadow collector role | Parallel data collection only | Save tick data for future comparison, don't use for exit decisions yet |
| Watch simplification | Strip LLM fields, keep structure | Minimal changes to DB schema; old data still readable |
| Cooling off vs retrospective | Replace retrospective with cooling_off | Simpler model; no LLM involved; just time-based streaming |
| One active config | Yes, for now | Simpler portfolio tracking; A/B testing is Phase N |
| Allocation replace ranking | User choice (same as backtest UI) | Matches backtest behavior; no new divergence |
