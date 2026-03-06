# Backtest System Architecture

> Async job-based backtesting with SSE notifications and client-side state persistence.
>
> Last updated: 2026-03-04

---

## Overview

The backtest system runs exit strategy simulations against historical snapshot data using
1-minute OHLCV bars. Backtests can take up to ~1 minute depending on the date range and
number of snapshots, so the system uses an **async job pattern** to avoid blocking the UI.

```
Browser                          Server (FastAPI)
  │                                 │
  │  POST /api/strategies/backtest  │
  │ ──────────────────────────────► │
  │  ◄── 202 {job_id}              │  ← returns immediately
  │                                 │
  │    (background task running)    │  ← asyncio.create_task()
  │                                 │     └─ asyncio.to_thread(run_backtest, ...)
  │  SSE: backtest_complete         │
  │ ◄─────────────────────────────  │  ← EventBus → SSE stream
  │                                 │
  │  GET /api/strategies/backtest/{job_id}
  │ ──────────────────────────────► │
  │  ◄── {status, result}          │  ← fetch full results
```

---

## Backend

### Job store

In-memory dict in `app.py` (closure variable `_backtest_jobs`):

```python
_backtest_jobs: dict[str, dict[str, Any]] = {}
# {job_id: {status, result, error, created_at, strategy}}
```

Jobs are pruned when older than 1 hour (checked on each new POST request).
Not persisted across server restarts (acceptable — backtests are cheap to re-run).

### Endpoints

| Method | Path | Response | Description |
|--------|------|----------|-------------|
| POST | `/api/strategies/backtest` | 202 `{job_id}` | Starts async job, returns immediately |
| GET | `/api/strategies/backtest/{job_id}` | `{status, strategy, result, error}` | Poll job status + results |

### SSE events

| Event | Payload | When |
|-------|---------|------|
| `backtest_started` | `{job_id, strategy}` | Job created |
| `backtest_complete` | `{job_id}` | Job finished successfully |
| `backtest_error` | `{job_id, error}` | Job failed |

Published via `EventBus` → streamed to all connected clients via `/events` SSE endpoint.

### Background task flow

1. **Request handler** (runs synchronously on main thread):
   - Parses request body
   - Builds entry list from filters (reads DB via `_snapshot_rows_for_filters()`)
   - Creates job record, publishes `backtest_started`
   - Returns 202 with `job_id`

2. **Background task** (`asyncio.create_task()`):
   - Runs backtest engine via `asyncio.to_thread(run_backtest, ...)`
   - Applies transaction costs, allocation filtering, portfolio simulation
   - On success: stores results, publishes `backtest_complete`
   - On error: stores error message, publishes `backtest_error`

---

## Frontend

### State management (localStorage)

| Key | Type | Purpose |
|-----|------|---------|
| `bt_job_id` | string (uuid) | Active job ID — present while backtest is running |
| `bt_results` | JSON | Cached completed results (snapshot_id → trade result) |
| `bt_summary` | JSON | Cached summary stats |
| `bt_panel_open` | `"1"` or `""` | Backtest panel visibility |
| `bt_last_strategy` | string | Last selected strategy key (pre-existing) |
| `bt_last_allocation` | string | Last selected allocation key (pre-existing) |
| `strategy_params` | JSON | Strategy parameter values (pre-existing) |
| `allocation_params` | JSON | Allocation parameter values (pre-existing) |

### Lifecycle

#### Starting a backtest
1. `runBacktest()` POSTs to server, receives `{job_id}`
2. Stores `job_id` in localStorage
3. Shows spinner, disables Run button

#### Receiving results (while on page)
1. SSE `backtest_complete` event fires
2. Handler checks `job_id` matches localStorage
3. Fetches full results via GET endpoint
4. Populates `_btResults` / `_btSummary` globals, calls `_displayResults()`
5. Caches results in localStorage, removes `bt_job_id`

#### Restoring state (page load / navigation back)
1. IIFE checks localStorage for `bt_panel_open` → reopens panel if needed
2. Checks for `bt_job_id`:
   - **Found + running**: Shows spinner, waits for SSE completion
   - **Found + complete**: Fetches results, displays immediately
   - **Found + error/expired**: Clears localStorage
   - **Not found**: Restores cached `bt_results`/`bt_summary` if available

#### Clearing results
`clearBacktest()` removes `bt_results`, `bt_summary`, and `bt_job_id` from localStorage.

---

## Backtest engine

See also: [BACKTEST-STRATEGIES.md](BACKTEST-STRATEGIES.md), [BACKTEST-METRICS.md](BACKTEST-METRICS.md)

**File:** `trader/market/backtest.py`

**Data source:** 1-minute OHLCV bars from Schwab (primary, cached per symbol/date)
or yfinance (fallback). Cache persists on disk — data collected within Schwab's 10-day
window remains available for backtesting months later.

**Strategies:** 14+ built-in exit strategies across categories (price-based, trailing,
volatility, trend, momentum, volume, adaptive). Each parameterized with `ParamDef`
(type, range, step).

**Post-processing pipeline:**
1. `run_backtest()` — walks 1-min bars per entry, applies strategy exit logic
2. Transaction cost deduction (configurable bps)
3. `apply_allocation()` — position sizing / filtering
4. `compute_portfolio_sim()` — dollar-denominated portfolio simulation
5. `compute_ann_a()` / `compute_ann_b()` — annualized return + Sharpe calculations

---

## Allocation Strategies

Allocation strategies control **how many positions can be open simultaneously** and
**what happens when a new signal arrives while at capacity**. They sit in the
post-processing pipeline between `run_backtest()` and the portfolio simulation.

**File:** `trader/market/backtest.py` — `apply_allocation()`, `_try_replace()`, ranking helpers.

### Walk-Forward Simulation

Allocation is evaluated **chronologically** (sorted by entry time). At each new signal:

1. Positions that have already exited by this entry time are evicted from the open set.
2. If capacity is available, the trade is taken.
3. If at capacity, behavior depends on the strategy's "When Full" setting.

### Strategies

#### 1. None (Unlimited)

- **Key:** `none`
- No capital constraints. Every signal is traded independently.
- Useful as a baseline — shows raw strategy performance without portfolio effects.

#### 2. Fixed Dollar Per Trade

- **Key:** `fixed_dollar`
- **Params:** `alloc_pct` (default 5%) — percentage of initial capital per trade.
- Max concurrent positions = `floor(100 / alloc_pct)` (e.g., 5% → 20 positions).
- **When full: always skip.** No replacement logic.

#### 3. Max Positions

- **Key:** `max_positions`
- **Params:**
  - `max_pos` (default 10) — hard cap on concurrent open positions.
  - `when_full` — `"skip"` (default) or `"replace"` (Replace Weakest).
  - `rank_method` — ranking function used for replacement decisions (see below).
  - `composite_weight` — only used when `rank_method = "composite"`.
- **When full = Skip:** new signals are discarded (exit_reason = `"skipped"`).
- **When full = Replace Weakest:** triggers the replacement logic (see below).

#### 4. Ranking-Based Reallocation

- **Key:** `ranking_realloc`
- **Params:** `alloc_pct`, `rank_method`, `composite_weight`.
- Max concurrent = `floor(100 / alloc_pct)`, same as Fixed Dollar.
- **Always replaces** when full — there is no "skip" option.
- Otherwise identical to Max Positions with Replace Weakest.

---

### Replacement Logic (`_try_replace`)

When the portfolio is full and replacement is enabled, the system decides whether to
swap out an existing position for the new signal:

1. **Score all open positions** using the selected ranking method.
2. **Score the incoming signal** using the same method.
3. **Find the weakest** (lowest-scoring) open position.
4. **Replace only if the new signal's score strictly exceeds the weakest position's score.**
   If not, the new signal is skipped (exit_reason = `"skipped"`).

When a position is replaced:
- The victim is **early-exited** at its interpolated price at the new signal's entry time.
- The victim's exit_reason is set to `"replaced"`.
- The new signal takes the victim's slot.

### Ranking Methods

#### Unrealized P&L (`momentum`)

Scores each open position by its **unrealized return** at the time of the new signal:

```
score = (current_price - entry_price) / entry_price
```

**Critical detail:** A newly arriving signal has no price history, so it is scored
as **`0.0`** (zero unrealized P&L). This means:

- **Losing position** (negative P&L) → **replaced** (0.0 > negative).
- **Break-even position** (P&L = 0.0) → **NOT replaced** (0.0 is not > 0.0).
- **Winning position** (positive P&L) → **NOT replaced** (0.0 < positive).

**In practice:** if all existing positions are profitable or break-even, new signals
are skipped. New signals only displace positions that are currently underwater.

#### Signal Confidence (`confidence`)

Scores each position by its **original signal confidence** (the LLM-assigned probability
at entry time). The incoming signal uses its own confidence score.

A new signal replaces the weakest position only if its confidence is strictly higher.
Unlike momentum, this comparison is between two meaningful values, so replacement
happens whenever the new signal is more confident than the least-confident open position.

#### Composite (`composite`)

Blends confidence and momentum using z-score normalization:

```
score = weight * Z(confidence) + (1 - weight) * Z(momentum)
```

- `composite_weight` controls the blend (0 = pure momentum, 1 = pure confidence).
- Requires at least 2 open positions to compute z-scores; falls back to momentum otherwise.
- The incoming signal's composite score uses its confidence component only (momentum = 0).

### Stats

`apply_allocation()` returns counts: `taken` (accepted), `skipped` (at capacity),
`replaced` (victim early-exited to make room). These appear in the backtest summary.

---

## Snapshot auto-refresh

The snapshots table auto-refreshes when new snapshots are sealed:

```
EventBus publishes "snapshot_sealed"
    → SSE stream delivers to browser
    → base.html dispatches CustomEvent "sse:snapshot_sealed" on document.body
    → snapshots.html listener calls reloadTable()
    → reloadTable() fetches /api/snapshots with current filter params
    → Server returns filtered HTML, new snapshot appears only if it passes filters
```

No client-side filter matching needed — the server handles all filtering.
