# Simulated Portfolio Equity Fix (2026-03-12)

## Problem

Simulated (non-Alpaca) portfolio equity charts were flat lines stuck at starting capital. Both realized and unrealized PnL were always zero, making the charts useless.

**Affected portfolios:** `lc_c45e48da2b27`, `lc_026e07c6a0d8` (and any future sim portfolio)

## Root Causes

### 1. Replacement exit price = entry price

In `_exit_victim()` ([live_monitor.py](../trader/online/live_monitor.py)), when a simulated portfolio exits a position via replacement, the code defaulted:

```python
exit_price = entry_price  # line 1151
```

With no broker, the `else` branch only checked for an Alpaca position (which doesn't exist for sim portfolios). No fresh quote was ever fetched. Every replacement recorded `realized_pnl_pct: 0.0`.

### 2. Unrealized PnL never computed for sim portfolios

In `_snapshot_equity()` ([live_monitor.py](../trader/online/live_monitor.py)), the sim calculation reads:

```python
upnl = w.get("unrealized_pnl")  # line 1518
```

But `unrealized_pnl` is never set on simulated watch dicts. It's always `None`, so `unrealized_dollar` was always 0.

### Combined effect

```
equity = starting_capital + 0 (realized) + 0 (unrealized) = starting_capital
```

The chart was a flat line at $100,000 regardless of actual market movement.

## Fixes Applied

### Fix 1: Fresh exit prices for simulated replacements

**File:** `trader/online/live_monitor.py`, `_exit_victim()` ~line 1173

Added an `elif self.market` branch that fetches a Schwab quote when there's no broker:

```python
elif self.market:
    try:
        quotes = self.market.get_quotes([symbol])
        q = (quotes or {}).get(symbol.upper()) or (quotes or {}).get(symbol) or {}
        fresh = q.get("lastPrice") or q.get("last_price")
        if fresh and float(fresh) > 0:
            exit_price = float(fresh)
    except Exception:
        log.warning("Could not fetch exit price for %s — using entry price", symbol)
```

### Fix 2: Unrealized PnL in equity snapshots

**Files:** `trader/online/live_monitor.py` (`_snapshot_equity`), `trader/online/orchestrator.py`

1. Added `market` (MarketDataService) param to `LiveExitMonitor.__init__`
2. Orchestrator now passes `_live_market` to the monitor
3. `_snapshot_equity()` sim path now fetches batch quotes via `monitor.market.get_quotes()` for all holding symbols and computes unrealized PnL from `(current_price - entry_price) / entry_price` instead of relying on a watch field that was never set

## Backfill

Two one-off scripts corrected historical data:

### `scripts/backfill_exit_prices.py`

For each zero-PnL replacement exit in `lc_c45e48da2b27`:
1. Fetched Schwab 1-min intraday bars for exit day
2. Found the bar closest to the exit timestamp
3. Updated `watch_json` with correct exit price and `realized_pnl_pct`

**Result:** 29 watches updated. Average realized PnL was **+0.98%** per trade (was recorded as 0.0%).

### `scripts/backfill_equity_full.py`

For each equity snapshot in both sim portfolios (`lc_026e07c6a0d8` and `lc_c45e48da2b27`):
1. Replayed watch entry/exit events up to that snapshot's timestamp
2. Computed realized PnL from corrected exit prices
3. Looked up each holding's price at that timestamp from Schwab 1-min bars (bisect nearest)
4. Computed unrealized PnL as `sum((current_price - entry_price) / entry_price * pos_size)` for all holdings
5. Updated equity, cash, realized_pnl, unrealized_pnl in `portfolio_equity_snapshots`

**Result:** 359 equity snapshots updated across 2 portfolios. Charts now show actual intraday movement.

## Impact

| Portfolio | Before | After |
|---|---|---|
| `lc_026e07c6a0d8` (reference) | Flat line, stepped only on signal exits | Smooth intraday curve tracking all 76 symbols |
| `lc_c45e48da2b27` (simulated) | Flat at $100,000 | Shows buildout, dip (HIMX -12%), recovery to ~$101,400 |
