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

See **[ALLOCATION-STRATEGIES.md](ALLOCATION-STRATEGIES.md)** for full documentation of:

- 4 allocation strategies (None, Fixed Dollar, Max Positions, Ranking-Based Reallocation)
- Replacement logic (`_try_replace`) — how the system decides whether to swap positions
- 3 current ranking methods (Unrealized P&L, Signal Confidence, Composite)
- Proposed forward-looking ranking methods (Trailing Slope, Volume-Weighted Trend, RSI)
- Implementation plan

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
