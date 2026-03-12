# Backtest → Live Pipeline: Adding Features

> Step-by-step guide to adding new parameters, guards, or strategies to the backtest/live trading pipeline. Use this as a checklist whenever a new feature needs to flow from the UI through backtest into live portfolio management.

## Quick reference

### Key files (in order of data flow)

| Step | File | What to change |
|------|------|----------------|
| 1. UI input | [`trader/web/templates/snapshots.html`](../../trader/web/templates/snapshots.html) | HTML input + JS save/load/send |
| 2. API parse | [`trader/web/app.py`](../../trader/web/app.py) | Parse from request body |
| 3. Backtest engine | [`trader/market/backtest.py`](../../trader/market/backtest.py) | `run_backtest()` + `evaluate_exit()` |
| 4. Live config model | [`trader/models/live_config.py`](../../trader/models/live_config.py) | Add field to `LiveConfig` dataclass |
| 5. Live monitor | [`trader/online/live_monitor.py`](../../trader/online/live_monitor.py) | Read from config, pass to `evaluate_exit()` |
| 6. Positions display | [`trader/web/templates/partials/_positions_table.html`](../../trader/web/templates/partials/_positions_table.html) | Show in portfolio detail/summary |

### Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│  FRONTEND (snapshots.html)                                  │
│                                                             │
│  saveBtSettings() ──► localStorage                          │
│  loadBtSettings() ◄── localStorage                          │
│                                                             │
│  runBacktest()  ──► POST /api/strategies/backtest           │
│  goLive()       ──► POST /api/live/config                   │
└──────────────┬──────────────────────────┬───────────────────┘
               │                          │
               ▼                          ▼
┌──────────────────────┐   ┌──────────────────────────────────┐
│  BACKTEST ENGINE     │   │  LIVE CONFIG MODEL               │
│  backtest.py         │   │  live_config.py                  │
│                      │   │                                  │
│  run_backtest()      │   │  LiveConfig dataclass            │
│  evaluate_exit()     │   │  (persisted to SQLite)           │
│  _STRATEGY_RUNNERS   │   └──────────────┬───────────────────┘
└──────────────────────┘                  │
                                          ▼
                              ┌────────────────────────────────┐
                              │  LIVE MONITOR                  │
                              │  live_monitor.py               │
                              │                                │
                              │  LiveExitMonitor._check_holding│
                              │  → reads LiveConfig            │
                              │  → calls evaluate_exit()       │
                              │  → closes Alpaca position      │
                              └────────────────────────────────┘
```

## Adding a new guard parameter

Guards are independent of the exit strategy — they override any strategy. Examples: `guard_stop_pct` (hard stop), `guard_target_pct` (hard target), `guard_trail_pct` (trailing stop).

### Step 1: UI — Add input to Risk & Cost Controls

Location: `snapshots.html` → look for `bt-section-risk` section (~line 424).

```html
<div class="bt-inline-field">
  <label class="bt-control-label mb-0" for="bt-guard-NEWPARAM"
         title="Tooltip description">Label</label>
  <input type="number" id="bt-guard-NEWPARAM" value="0" min="0" max="50" step="0.5"
         class="form-control form-control-sm bt-short-input" onchange="saveBtSettings()">
</div>
```

### Step 2: JS — Save, load, and send the value

Three functions to update in `snapshots.html`:

**`saveBtSettings()`** — Add to the `var` declarations and the `JSON.stringify()` call:
```javascript
var guardNewParam = parseFloat(document.getElementById('bt-guard-NEWPARAM').value) || 0;
// Add to localStorage JSON:
localStorage.setItem(_BT_SETTINGS_KEY, JSON.stringify({..., guardNewParam: guardNewParam, ...}));
```

**`loadBtSettings()`** — Add restoration:
```javascript
if (s.guardNewParam != null) {
  document.getElementById('bt-guard-NEWPARAM').value = s.guardNewParam;
}
```

**`runBacktest()`** — Read the value and include in the POST body:
```javascript
var guardNewParam = parseFloat(document.getElementById('bt-guard-NEWPARAM').value) || 0;
// Add to JSON.stringify body: guard_new_param_pct: guardNewParam
```

**`goLive()`** — Same read, include in the config body:
```javascript
var guardNewParam = parseFloat(document.getElementById('bt-guard-NEWPARAM').value) || 0;
// Add to body object: guard_new_param_pct: guardNewParam
```

### Step 3: API — Parse from request

**Backtest endpoint** in `app.py` (~line 998):
```python
guard_new_param_pct = body.get("guard_new_param_pct", 0)
```
Then pass it to the `run_backtest()` call (~line 1102).

**Live config creation** in `app.py` (~line 1767):
```python
guard_new_param_pct=float(body.get("guard_new_param_pct", 0)),
```

### Step 4: Backtest engine — Implement the logic

In `backtest.py`:

1. **Add parameter** to `run_backtest()` signature (after `guard_target_pct`)
2. **Add parameter** to `evaluate_exit()` signature
3. **Implement the guard computation** — either:
   - **Fixed guard** (like stop/target): compute a price level from entry_price, pass to runners via `guard_stop`/`guard_target` params
   - **Dynamic guard** (like trailing stop): compute independently after the strategy runner returns, pick whichever fires first by comparing bar indices

**Pattern for dynamic guards** (computed independently of strategy):
```python
# After strategy runner returns (exit_price, exit_time, reason, bars_held):
if guard_new_param_pct > 0:
    # Compute guard exit using vectorized numpy
    highs = df["High"].to_numpy(dtype=float, copy=False)
    lows = df["Low"].to_numpy(dtype=float, copy=False)
    guard_hit = _my_guard_function(highs, lows, run_idx, entry_price, guard_new_param_pct)
    if guard_hit is not None:
        guard_rel, guard_price, guard_reason = guard_hit
        guard_abs = run_idx + guard_rel
        guard_bars = guard_rel + 1 + (run_idx - entry_idx)
        # Pick whichever fires earlier
        if exit_price is None or guard_abs <= (entry_idx + bars_held - 1):
            exit_price = guard_price
            exit_time = _fmt_ts(df.index, guard_abs)
            reason = guard_reason
            bars_held = guard_bars
```

**Important**: Add this logic in BOTH `run_backtest()` and `evaluate_exit()` — they share the same math but `evaluate_exit()` is the live API used by the monitor.

### Step 5: LiveConfig model — Add field

In `live_config.py`, add to the Guards section:
```python
guard_new_param_pct: float = 0.0  # description (0 = disabled)
```

Must have a default value so existing configs (already in SQLite) deserialize without error.

### Step 6: Live monitor — Pass through

In `live_monitor.py` `_check_holding()`:
1. Initialize: `guard_new_param_pct = 0.0`
2. Read from config: `guard_new_param_pct = cfg.guard_new_param_pct`
3. Pass to `evaluate_exit()`: `guard_new_param_pct=guard_new_param_pct`

### Step 7: Positions table — Display

In `_positions_table.html`:
- **Detail view** (~line 316): Add a line showing the value
- **Summary line** (~line 279): Optionally show when non-zero

## Adding a new exit strategy

This is different from guards — strategies are the primary exit mechanism selected from the dropdown.

### Step 1: Define strategy metadata

In `backtest.py`, add to the `STRATEGIES` dict (~line 96):
```python
"my_strategy_key": StrategyDef(
    name="Human-Readable Name",
    key="my_strategy_key",
    section="Category",        # e.g. "Trailing", "Price-Based", "Indicator"
    description="Tooltip text explaining the strategy.",
    params={
        "param1": ParamDef("float", 5.0, "Label", 0.5, 30.0, 0.5),
        "param2": ParamDef("int", 14, "Period", 5, 50, 1),
    },
),
```

### Step 2: Implement runner function

Add before `_STRATEGY_RUNNERS` dict:
```python
def _run_my_strategy(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    # Read params
    param1 = params["param1"]

    # Bar-by-bar iteration pattern (use for strategies needing state):
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1

        # Always check guards first
        guard_hit = _check_guards(bar, guard_stop, guard_target)
        if guard_hit is not None:
            price, reason = guard_hit
            return price, _fmt_ts(df.index, i), reason, bars_held

        # Your strategy logic here
        if should_exit:
            return exit_price, _fmt_ts(df.index, i), "stop", bars_held

    return None, None, "still_open", len(df) - entry_idx
```

### Step 3: Register in runner dispatch

```python
_STRATEGY_RUNNERS = {
    ...
    "my_strategy_key": _run_my_strategy,
}
```

### Step 4: UI auto-updates

**No UI code needed.** The strategy dropdown and parameter inputs are auto-generated from the `STRATEGIES` dict via `strategies.html`. The backtest panel reads from there.

### Step 5: Live path works automatically

Since `evaluate_exit()` dispatches via `_STRATEGY_RUNNERS`, and `LiveConfig` stores `exit_strategy` + `exit_params`, the live monitor will call your new strategy with no additional code.

## Gotchas

### Guard vs strategy: different extension patterns
- **New guard**: Touch all 7 files in the pipeline. Guards are independent of strategy choice.
- **New strategy**: Only touch `backtest.py` (define + implement + register). UI and live path auto-discover.
- **New guard type that needs state** (like trailing stop): Can't use `_check_guards()` or `_first_guard_hit()` — those are stateless. Compute independently after the strategy runner returns, then compare bar indices to pick the earlier exit.

### `run_backtest()` vs `evaluate_exit()` — keep them in sync
Both call the same runners, but:
- `run_backtest()` is the batch backtest path (runs all historical trades)
- `evaluate_exit()` is the live path (called each monitor cycle with latest bars)
- Any new guard logic must be added to BOTH functions

### Positional arguments in `run_backtest()`
The `app.py` call to `run_backtest()` uses **positional arguments** (line ~1102):
```python
run_backtest, strategy_key, params, entries, market_close, min_hold,
    guard_stop_pct, guard_target_pct, guard_trail_pct,
    price_delay_minutes, res_min, engine_trace,
```
When adding a new parameter, **order matters**. Insert it in the correct position in both the function signature and the call site. Alternatively, convert to keyword arguments if the arg list gets unwieldy.

### LiveConfig backward compatibility
`LiveConfig.from_dict()` uses `**{k: v for k, v in d.items() if k in cls.__dataclass_fields__}`. New fields MUST have defaults so existing configs in SQLite (which lack the field) deserialize correctly.

### JavaScript `|| 0` gotcha
`parseFloat(value) || 0` means a legitimate value of `0` is indistinguishable from empty/NaN. This is fine for guards (0 = disabled), but be careful if 0 is a meaningful non-default value.

### Vectorized vs bar-by-bar strategies
- **Vectorized** (e.g., `_run_fixed_stop_loss`): Uses `_first_true()` on numpy arrays + `_first_guard_hit()`. Fastest, but can't maintain running state.
- **Bar-by-bar** (e.g., `_run_pct_trailing_stop`): Iterates with `_check_guards()` per bar. Needed for strategies that track state (peak price, cumulative indicators, etc.).
- **Independent guards** (e.g., trailing stop guard): Computed after the runner via vectorized numpy (`np.maximum.accumulate`), then pick earlier exit by bar index. Best for guards that need state but shouldn't touch existing runners.

### Exit reason strings
The `reason` string (e.g., `"guard_stop"`, `"guard_target"`, `"guard_trail"`, `"stop"`, `"still_open"`) appears in:
- Backtest result tooltips (shown as-is)
- Log messages
- `BacktestResult.exit_reason` field
No enum or registry — just use a descriptive lowercase string. Prefix guard reasons with `guard_` to distinguish from strategy exits.

### `ensure_stops()` is separate from guard logic
`ensure_stops()` in `app.py` creates Alpaca server-side stop-loss orders. It only uses `guard_stop_pct` (fixed stop). Trailing stops and targets are evaluated client-side by the live monitor on each cycle — they are NOT Alpaca server-side orders.

## Cross-references

- [DIAGNOSTICS.md](DIAGNOSTICS.md) — Debugging exit issues, log sources, SQL queries
- [ALPACA.md](ALPACA.md) — Order execution after exit signal, fill confirmation
- [TRADE-ANALYSIS.md](TRADE-ANALYSIS.md) — Analyzing backtest results, P&L queries
- [BACKTEST-*.md](../BACKTEST-ARCHITECTURE.md) — Backtest architecture docs
- [LIVE-TRADING.md](../LIVE-TRADING.md) — Live trading plan and implementation details
- [ALPACA-TRADING.md](../ALPACA-TRADING.md) — Alpaca order execution, extended hours
