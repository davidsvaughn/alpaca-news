# Live Trading

> Hub document for the live trading feature: from backtest simulation to real-time paper trading.
> Consolidates the former LIVE-TRADING-PLAN.md and LIVE-TRADING-IMPL.md.
>
> Created: 2026-03-04 | Last updated: 2026-03-06
>
> See also: [ALPACA-TRADING.md](ALPACA-TRADING.md) — Alpaca order execution, multi-account setup, confirmed fills, reconciliation, edge cases

---

## Table of Contents

1. [Vision](#vision)
2. [Architecture](#architecture)
3. [Implementation Phases](#implementation-phases)
4. [LiveConfig](#liveconfig)
5. [Watch Model Changes](#watch-model-changes)
6. [LiveExitMonitor](#liveexitmonitor)
7. [LivePortfolioManager](#liveportfoliomanager)
8. [Post-Exit Cooling Off](#post-exit-cooling-off)
9. [Positions Tab (New UI)](#positions-tab-new-ui)
10. [Reference: Best Backtest Configuration](#reference-best-backtest-configuration)
11. [Reference: Backtest vs Live Differences](#reference-backtest-vs-live-differences)
12. [Reference: Volume Delta Analysis](#reference-volume-delta-analysis)
13. [Reference: Alpaca Trading API](#reference-alpaca-trading-api)
14. [Reference: Data Sources](#reference-data-sources)
15. [Reference: Risk Controls](#reference-risk-controls)
16. [Open Questions](#open-questions)
17. [Design Choices](#design-choices)
18. [Progress Log](#progress-log)

---

## Vision

Activate a **LiveConfig** — the same parameters used in backtesting (filters, allocation, exit strategy) — and run it forward in real-time. As news arrives and snapshots are created, the system applies identical filters, allocation strategy, and exit strategy as backtest. Exit decisions are purely mechanical (same math as backtest), not LLM-based.

Later phases add actual order execution via Alpaca API (paper first, then live).

---

## Architecture

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

**Key principle**: The live system mirrors backtest exactly. Same filter logic, same allocation logic, same exit strategy math. The only difference is data arrives in real-time instead of being walked forward over historical bars.

---

## Implementation Phases

### Phase 1: Foundation (DONE)
- [x] Extract exit strategy functions from backtest.py for standalone use
- [x] Create LiveConfig model + SQLite storage + API endpoints
- [x] Modify Watch model (add `cooling_off` state, simplify fields)
- [x] Fence off legacy WatchMonitor LLM check-in code

### Phase 2: Core Live Loop (DONE)
- [x] Implement LiveExitMonitor (daemon thread, bar-based exit evaluation)
- [x] Implement LivePortfolioManager (filter + allocate new snapshots)
- [x] Wire into orchestrator (replace WatchMonitor startup)
- [x] Market hours calculation for cooling_off expiry
- [x] 26 integration tests (all passing)
- [x] Integrate with Schwab streaming (add/remove symbols on buy/sell)
- [x] Integrate with shadow collector (continue during cooling_off)

### Phase 3: Positions UI (DONE)
- [x] Backend: API endpoint `/api/positions` with portfolio stats
- [x] Frontend: Positions tab with open/exited/closed sections
- [x] Frontend: LiveConfig summary + deactivate button
- [ ] Frontend: LiveConfig editor (save backtest config as live config) — deferred
- [ ] Frontend: Activate from saved configs list — deferred

### Phase 4: Alpaca Paper Trading (DONE)
- [x] Alpaca paper trading credentials + config (multi-account: up to 5)
- [x] Order execution layer: `buy_and_confirm`, `close_position_and_confirm`, `set_stop`
- [x] Replace virtual buy/sell with actual Alpaca orders (confirmed fills)
- [x] Guard stops as server-side Alpaca stop orders (GTC)
- [x] Fill price tracking (actual Alpaca fill price replaces snapshot estimate)
- [x] Trade update stream (fills, rejects) per account
- [x] Non-fractionable stock handling (auto whole-share conversion)
- [x] Reconciliation on startup (Alpaca = source of truth)
- [x] See [ALPACA-TRADING.md](ALPACA-TRADING.md) for full details

### Phase 5: Validation & Shadow Mode
- [ ] Compare live exits vs what backtest would have done
- [ ] Slippage tracking: expected vs actual fill prices
- [ ] Volume delta shadow: tick-level vs bar-level comparison over time
- [ ] Save shadow collector data on seal for post-mortem

### Phase 6: Paper -> Live Transition
- [ ] Validate paper trading P&L matches backtest expectations
- [ ] Circuit breakers (daily loss limit, max trades, equity floor)
- [ ] Switch ALPACA_PAPER=false with live credentials
- [ ] Start with reduced position size (1% instead of 5%)

---

## LiveConfig

Persisted configuration that mirrors backtest parameters. Only one config can be `active=True` at a time.

```python
@dataclass
class LiveConfig:
    config_id: str
    name: str
    active: bool
    created_at: str

    # Filters (same as backtest UI)
    filters: dict               # confidence_min, price_min/max, volume_min/max,
                                # market_cap_min/max, pe_min/max, etc.

    # Allocation
    allocation: str             # "none" | "fixed_dollar" | "max_positions" | "ranking_realloc"
    allocation_params: dict     # alloc_pct, max_pos, when_full, rank_method, composite_weight
    starting_capital: float

    # Exit strategy
    exit_strategy: str          # e.g., "volume_delta_divergence"
    exit_params: dict           # e.g., {"lookback": 80}

    # Guards
    guard_stop_pct: float       # hard stop loss % (0 = disabled)
    guard_target_pct: float     # hard take profit % (0 = disabled)

    # Timing
    min_hold: int               # minimum bars before exit checks
    price_delay_minutes: int    # delay after snapshot before "entry" (default 10)
    market_close: str | None    # "16:00", "17:30", "20:00", None

    # Post-exit
    cooling_off_market_hours: float  # hours of market time to keep streaming (default 24)
```

**Storage**: SQLite table `live_configs` (JSON blob, like watches).

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
- `exited` — exit triggered, transition to cooling_off
- `cooling_off` — streaming continues, no position, data collection only
- `sealed` — done, streaming stopped

### Simplified fields
**Removed** (legacy LLM check-in system):
- `checkin_history` agent data (system_prompt, user_message, findings, tool_traces, usage, thinking_summary)
- `retrospective` state and `retrospective_data` / `retrospective_snapshot_ids`
- `monitoring_snapshot_ids`
- Check-in `depth` concept (lightweight/medium/full/force_exit)

**Added**:
- `exit_strategy` — which strategy triggered the exit
- `exit_params` — strategy params at time of exit
- `cooling_off_until` — ISO timestamp when cooling_off expires
- `live_config_id` — which LiveConfig this watch was created under

**Kept**:
- `watch_id`, `symbol`, `status`, `entry`, `exit`, `created_at`
- `last_checkin_at` (repurposed: last time exit monitor evaluated this watch)

---

## LiveExitMonitor

Replaces WatchMonitor. Runs in a daemon thread during market hours.

### Check cycle (~60s interval)

```
for each watch where status == "holding":
    1. Fetch 1-min bars from Schwab (enough for strategy lookback window)
    2. Check guards first:
       - Low <= entry * (1 - guard_stop_pct) --> EXIT "guard_stop"
       - High >= entry * (1 + guard_target_pct) --> EXIT "guard_target"
    3. Skip exit strategy if bars_held < min_hold
    4. Run exit strategy function (reused from backtest.py)
    5. If exit signal --> EXIT with strategy reason
    6. Update last_checkin_at

for each watch where status == "cooling_off":
    1. Compute elapsed market hours since exit
    2. If >= cooling_off_market_hours --> seal watch
```

### Bar data source
- Schwab 1-min bars via existing MarketDataService (full exchange volume)
- Same source as backtest — no divergence
- Bars fetched fresh each cycle
- Shadow collector runs independently for tick-level comparison data

### Exit strategy reuse

Extract from backtest.py so strategies can be called standalone:
```python
result = run_exit_strategy(
    strategy_name="volume_delta_divergence",
    params={"lookback": 80},
    bars=bars_df,
    entry_idx=entry_bar,
    entry_price=price,
    guards={"stop_pct": 5.0, "target_pct": 0.0},
    min_hold=5,
)
# Returns: ExitResult(should_exit, exit_price, reason, bars_held) or None
```

---

## LivePortfolioManager

Evaluates new snapshots against the active LiveConfig.

### On new snapshot sealed:
```
1. Is LiveConfig active? If not --> skip
2. Apply filters (same logic as _snapshot_rows_for_filters)
3. If doesn't pass --> skip (log reason)
4. Check allocation:
   a. Count current "holding" watches
   b. Capacity available --> BUY
   c. At capacity + replace strategy --> rank positions, replace weakest if new is stronger
   d. At capacity + no replace --> SKIP
5. If BUY:
   - Wait price_delay_minutes (delayed task)
   - Fetch entry price
   - Create Watch (status="holding", live_config_id)
   - Add symbol to Schwab stream + shadow collector
   - Publish "watch_created" event
```

### Capital tracking
- `available_capital` + `allocated_capital` per position
- Each position gets `starting_capital * alloc_pct / 100`
- On exit: capital (+ P&L) returns to available pool

---

## Post-Exit Cooling Off

### Purpose
After exiting, continue collecting real-time stream data for post-mortem analysis:
- Would a different exit strategy have performed better?
- How does tick-level VDD compare to bar-level VDD?
- What happened to the stock after we sold?

### Implementation
- On exit: `watch.status = "cooling_off"`, compute `cooling_off_until`
- `cooling_off_until` = exit_time + N market hours (default 24)
- Market hours = 9:30-16:00 ET (or extended if configured)
- 24 market hours ~ 3.7 trading days (6.5h/day)
- Shadow collector keeps running for this symbol
- Bar data persisted immediately via JSONL append (crash-safe, zero data loss)
- On expiry: seal watch, export summary JSON (atomic write), remove symbol from stream (if no other watches need it)

### Data persistence
- **Real-time**: Each 1-minute bar appended to `~/.cache/alpaca-news/volume_delta_shadow/{SYMBOL}/bars/{date}.jsonl`
- **Summary**: Clean JSON export at `~/.cache/alpaca-news/volume_delta_shadow/{SYMBOL}/{date}.json` (on seal + hourly)
- **Recovery**: On restart, `add_symbol()` loads today's JSONL to restore in-memory state

### Configurable
- `cooling_off_market_hours: float = 24.0` in LiveConfig
- Adjustable via API/UI

---

## Positions Tab (New UI)

### Sections
- **Header**: Active LiveConfig name, status (running/stopped), capital summary
- **Open Positions** (`holding` watches):
  - Symbol, entry price/time, current price, unrealized P&L %
  - Time held, exit strategy, progress toward min_hold
- **Recently Exited** (`cooling_off` watches):
  - Symbol, entry/exit price, realized P&L %, exit reason
  - Cooling off progress (time remaining)
- **Closed** (`sealed` watches, most recent first):
  - Full trade summary: entry/exit, P&L, hold time, exit reason
- **Portfolio Stats**:
  - Total P&L (realized + unrealized), win rate, avg win/loss
  - Capital deployed vs available
  - Positions taken / skipped / replaced today

### Future enhancements (not now)
- Real-time equity curve
- Per-position price chart with entry/exit markers
- Side-by-side: live exit vs what alternative strategies would have done

---

## Reference: Best Backtest Configuration

From best-performing parameter set (updated 2026-03-05):

| Category | Parameter | Value |
|----------|-----------|-------|
| Exit Strategy | `volume_delta_divergence` lookback | **80 bars** |
| Guard Stop | `guard_stop_pct` | **5%** |
| Guard Target | `guard_target_pct` | **0%** (disabled) |
| Market Close | | **16:00** (regular hours) |
| Min Hold | | **5 bars** |
| Price Delay | | **5 min** |
| Cost BPS | | **0.5** |
| Allocation | `max_positions` | **20 concurrent** |
| When Full | | **Replace weakest** (by unrealized P&L) |
| Starting Capital | | **$100,000** |
| Confidence | | **>= 85% bullish** |

---

## Reference: Backtest vs Live Differences

| Aspect | Backtest | Live Trading |
|--------|----------|-------------|
| Data | Historical OHLCV bars | Real-time bars + optional tick |
| Execution | Instantaneous | Latency ~50-500ms, slippage |
| Entry price | Bar close at entry_time + delay | Current price at delay time (Phase 1-3) / Alpaca fill (Phase 4+) |
| Exit price | Bar close/low/high at signal | Current price at signal (Phase 1-3) / Alpaca fill (Phase 4+) |
| Guards | Checked per bar retroactively | Checked per cycle / Alpaca stop orders (Phase 4+) |
| Volume delta | Inter-bar tick rule (1-min) | Same (Phase 1) / tick-level shadow (comparison) |
| Positions | Simulated slots | Virtual tracking (Phase 1-3) / Real Alpaca positions (Phase 4+) |

---

## Reference: Volume Delta Analysis

### Test Results (2026-03-04)

Comparison of volume delta methods on 5-day data for SPY, AAPL, TSLA.

**Inter-bar tick rule** (backtest): each bar's entire volume classified as uptick/downtick based on close vs previous close.

**Key findings**:
1. **Per-bar direction agreement**: ~81-86% (15-19% of bars flip between methods)
2. **Rolling imbalance correlation**: 0.64-0.79
3. **VDD signal overlap**: 40-85% of signals shared; close-position formula generates more signals
4. **VDD signal timing**: Median 0 bars difference, but ~40% of signals don't match within +/-5 bars
5. **Cumulative delta curve**: Correlation 0.29 (AAPL) to 0.75 (SPY) — highly symbol-dependent
6. **Bar direction noise**: ~48-52% of consecutive bars flip direction

**Implications**:
- Start with bar-based approach (identical to backtest)
- Run tick-level in shadow mode for comparison
- Guards are unaffected (price-based, not volume-based)
- VDD lookback=80 was tuned on bar data — may need recalibration for tick-level
- TSLA shows better method agreement than AAPL

**Phased volume delta plan**:
1. **Launch**: 1-min bars + inter-bar tick rule (identical to backtest)
2. **Shadow**: Tick-level accumulation running in parallel, logging signals
3. **Calibrate**: After 2+ weeks, analyze if tick-level improves exit timing
4. **Switch**: If improvement shown, switch primary signal source

### Three Volume Delta Formulas

There are actually three distinct approaches to computing volume-based momentum, each
with different granularity:

| Method | Granularity | How Volume Is Classified |
|--------|-------------|--------------------------|
| **Inter-bar tick rule** | Binary per bar | Entire bar volume = uptick if close > prev close, else downtick |
| **AD Money Flow Multiplier** | Proportional per bar | `((C-L)-(H-C))/(H-L) * V` — distributes volume based on where close falls in the bar's range |
| **Tick-level** | Per trade | Each trade classified by last sale direction (true uptick/downtick) |

The **inter-bar tick rule** is what we currently use in both the backtest exit strategy
(`volume_delta_divergence`) and live exit monitoring. It's all-or-nothing: if AAPL closes
at $150.01 vs $150.00, the entire bar's 500K shares count as uptick.

The **AD Money Flow Multiplier** is more nuanced — if the close is near the high of the
bar, most volume is classified as buying pressure; if near the low, as selling pressure.
This is being considered for the new allocation ranking methods (see
[ALLOCATION-STRATEGIES.md](ALLOCATION-STRATEGIES.md#candidate-2-volume-weighted-trend--accumulation-distribution-volume_trend)).

The **tick-level** approach (what the shadow collector will enable) is the gold standard —
each individual trade is classified based on whether it executed at the bid or ask.

The shadow collector is gathering data to compare all three. Once we have enough data,
we can determine which formula produces the best signals for both exit strategies and
allocation ranking.

---

## Reference: Alpaca Trading API

### SDK & Authentication

```python
from alpaca.trading.client import TradingClient
from alpaca.data.live import StockDataStream
from alpaca.trading.stream import TradingStream

client = TradingClient(api_key, secret_key, paper=True)
```

Env vars: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true`

### Order Types

**Entry — Market Order**:
```python
MarketOrderRequest(symbol, notional=position_size, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
```

**Guard Stop — Stop Order** (server-side, recommended):
```python
StopOrderRequest(symbol, qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                 stop_price=round(entry_price * (1 - guard_stop_pct/100), 2))
```

**VDD Exit — Close Position**:
```python
client.close_position(symbol)
```

### Data Feed

| Plan | Data | Cost |
|------|------|------|
| Basic (free) | IEX only (~2-5% volume) | $0 |
| Algo Trader Plus | Full SIP (all exchanges) | ~$99/mo |

For VDD: use Schwab (full exchange volume, already integrated). Alpaca for orders only.

---

## Reference: Data Sources

| Source | Coverage | Latency | Cost | Use For |
|--------|----------|---------|------|---------|
| Schwab | All exchanges, 10-day window | Real-time | Free | VDD bars, primary data |
| yfinance | All exchanges, 7-day window | ~15 min delay | Free | Fallback |
| Alpaca IEX | IEX only (~2-5% volume) | Real-time | Free | Orders only |
| Alpaca SIP | All exchanges | Real-time | ~$99/mo | Future upgrade |

**Strategy**: Schwab for VDD monitoring (matches backtest), Alpaca for order execution.

---

## Reference: Risk Controls

### Circuit Breakers (Phase 4+)

| Breaker | Threshold | Action |
|---------|-----------|--------|
| Daily loss limit | -2% of portfolio | Stop opening new positions |
| Single position loss | guard_stop_pct (default 5%) | Exit immediately |
| Max positions | From allocation config | Skip new entries |
| Max daily trades | 50 (PDT-safe buffer) | Stop for the day |
| API error rate | 3 consecutive failures | Pause trading, alert |
| Account equity floor | -5% from starting | Halt all trading |

### PDT Considerations
- Applies to accounts under $25,000
- 3 day trades in rolling 5 business days
- Our trades typically held for hours — not day trades if held overnight
- Track day trade count, pause if approaching limit

---

## Open Questions

### Active

1. **Entry price timing**: Wait `price_delay_minutes` then fetch price (mirrors backtest) vs create watch immediately at current price (simpler). Current plan: wait, to mirror backtest.

2. **Multiple LiveConfigs**: Support running multiple simultaneously (A/B testing)? Current plan: one active config for now.

3. **Historical backfill**: On activation, retroactively evaluate recent snapshots? Current plan: forward-only.

4. **Extended hours**: Schwab streaming during extended hours may have gaps. Need to test.

5. **Short selling**: Backtest supports bearish signals. Include in live? Adds complexity (margin, locate fees). Defer for now.

### Resolved

- **Allocation replace ranking**: Same user choice as backtest UI (confidence / momentum / composite). No divergence.
- **Snapshot visibility**: Snapshots visible immediately after pipeline. Watch lifecycle is separate.
- **Bar data source**: Schwab 1-min bars (matches backtest). Tick-level in shadow mode only.
- **Legacy WatchMonitor**: Disabled but preserved with clear fencing comments.

---

## Design Choices

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Exit method | Mechanical (same math as backtest) | User wants live to mirror backtest exactly |
| Bar data source | Schwab 1-min fetch | Matches backtest; full exchange volume |
| Shadow collector role | Parallel data collection only | Save tick data for comparison, don't use for exits yet |
| Watch simplification | Strip LLM fields, keep structure | Minimal schema changes; old data still readable |
| cooling_off replaces retrospective | Yes | Simpler; no LLM; time-based streaming only |
| One active config | For now | Simpler portfolio tracking; A/B testing later |
| Positions tab | New dedicated tab | Clean separation from Snapshots |
| Guard stops (Phase 1-3) | Client-side check per cycle | No Alpaca orders yet |
| Guard stops (Phase 4+) | Server-side Alpaca stop orders | Survives process crashes |

---

## Progress Log

| Date | What | Status |
|------|------|--------|
| 2026-03-04 | Volume delta comparison tests | Done |
| 2026-03-04 | Shadow collector implementation | Done |
| 2026-03-04 | Live trading plan (initial) | Done |
| 2026-03-05 | Consolidated plan document | Done |
| 2026-03-05 | Phase 1: Foundation | Done |

### Phase 1 Details (2026-03-05)
- `evaluate_exit()` public API added to `trader/market/backtest.py` — thin wrapper around existing strategy runners for live use
- `ExitResult` dataclass added for clean return type
- `trader/models/live_config.py` created — `LiveConfig` dataclass with all backtest-mirroring parameters
- `live_configs` SQLite table + full CRUD in `trader/db/database.py` (insert, update, get, list, activate, deactivate, delete)
- API endpoints in `trader/web/app.py`: `GET/POST /api/live/config`, `GET /api/live/configs`, activate/deactivate/delete
- Watch model updated: `cooling_off` status, `live_config_id`, `exit_strategy`, `exit_params`, `cooling_off_until` fields
- `WatchBuilder.create_from_live_config()` factory method added
- `WatchBuilder.start_cooling_off()` method added
- Full backward compatibility with existing watch DB records
- Legacy WatchMonitor fenced off (requires `WATCH_LEGACY_MONITOR=1` env var to activate)
- `watcher.py` docstring updated with legacy notice

| 2026-03-05 | Phase 2: Core Live Loop | Done |

### Phase 2 Details (2026-03-05)
- `trader/market/market_hours.py` — `is_market_open()`, `add_market_hours()` for market-hours-aware timestamps
- `trader/online/live_monitor.py` — `LiveExitMonitor` + `LivePortfolioManager` + `live_monitoring_loop()`
- **LiveExitMonitor**: daemon thread, fetches Schwab 1-min bars, runs `evaluate_exit()` per holding watch, manages exited→cooling_off→sealed transitions
- **LivePortfolioManager**: evaluates snapshots against active LiveConfig (confidence + direction filters, allocation capacity check), creates watches via `WatchBuilder.create_from_live_config()`
- Orchestrator wired: `LiveExitMonitor` thread starts when `watch_enabled=True`; `LivePortfolioManager` called on snapshot seal when a LiveConfig is active (falls back to legacy signal-threshold when no config active)
- `tests/test_live_trading.py` — 26 tests covering: LiveConfig CRUD, evaluate_exit (5 strategies), market hours (6 scenarios), Watch live fields, LivePortfolioManager (4 scenarios), LiveExitMonitor (2 lifecycle tests)
- All 26 new tests + 10 existing watch tests pass

| 2026-03-05 | Streaming integration (shadow collector + Schwab) | Done |

### Streaming Integration Details (2026-03-05)
- `VolumeDeltaCollector` created at startup in `run_watch_loop`, attached to shared `SchwabMarketClient`
- On startup: resumes streaming for existing holding/cooling_off watches
- On buy (`LivePortfolioManager.evaluate_snapshot`): `collector.add_symbol()` + `schwab.start_stream([symbol])`
- On seal (`LiveExitMonitor._seal_watch`): `collector.save_daily()` + remove symbol if no other active watches need it
- Module-level `_live_collector` and `_live_market` shared between orchestrator and LivePortfolioManager
- Collector runs throughout cooling_off period, accumulating tick-level data for post-mortem

| 2026-03-05 | Crash-safe JSONL bar persistence | Done |

### JSONL Persistence Details (2026-03-05)
- **Problem**: `save_daily()` used non-atomic `write_text()`, and periodic full snapshots meant up to 5 min of data lost on crash
- **Solution**: Two-layer persistence (WAL pattern, same as SQLite/Redis):
  1. **JSONL append-on-flush**: Each completed 1-minute bar is immediately appended to `~/.cache/alpaca-news/volume_delta_shadow/{SYMBOL}/bars/{date}.jsonl`. POSIX atomic for writes < 4KB.
  2. **Atomic summary export**: `save_daily()` now uses temp-file + rename. Runs hourly and on seal (convenience, not safety).
- **Recovery on restart**: `add_symbol()` loads existing bars from today's JSONL log
- **Data loss on crash**: At most 1 incomplete minute bar (the one being accumulated)
- `TickAccumulator._on_bar_flushed` callback hooks bar completion to JSONL append

| 2026-03-05 | Phase 3: Positions UI | Done |

### Phase 3 Details (2026-03-05)
- `trader/web/templates/positions.html` — main page with LiveConfig summary card, deactivate button, HTMX-loaded positions content
- `trader/web/templates/partials/_positions_table.html` — three-section table (Open, Recently Exited, Closed) with portfolio stats cards (open count, cooling count, closed count, win rate, total P&L, W/L)
- `GET /api/positions` endpoint computes stats from all watches, splits by status, renders partial
- Nav bar updated with Positions link (between Dashboard and Watches)
- `cooling_off` badge style added (cyan)
- Positions page auto-refreshes via HTMX (`every 30s` + SSE events)

| 2026-03-05 | Go Live from UI + filter fixes | Done |

### Go Live UI + Filter Fixes (2026-03-05)
- **"Go Live" button** added to Snapshots backtest panel (green, next to Run/Clear)
  - Gathers all current backtest settings (strategy, params, filters, allocation, guards)
  - Creates + activates a LiveConfig in one click with confirmation dialog
  - Shows "Live: {name}" (outline green) when a config is already active; click to deactivate
- **Filter key fix**: backtest UI sends `conf_min`, `price_min`, `avg_vol_min`, etc. LivePortfolioManager now reads these keys correctly (was looking for `confidence_min`)
- **Market metric filters enforced**: `_apply_market_filters()` checks price, volume, market cap, P/E ranges using MarketDataService (permissive on data fetch failure)
- **Positions page** now displays all active filters: `Confidence >= 79% | AvgVol >= 1M | MktCap >= 1B` etc.
- Fixed missing `get_active_watches` import in orchestrator
