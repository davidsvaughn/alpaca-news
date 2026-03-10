# Reconciliation Stress Test Plan

**Status**: NOT STARTED
**Target accounts**: AlpacaPaper2 (PA3JDJHWNAKK), AlpacaPaper3 (PA30GHD6UFMQ)
**Paper1 is IN USE — do not touch it for testing.**

## Context

On 2026-03-10, we discovered multiple reconciliation bugs that caused ~$400 P&L drift between the sim and Alpaca. The root causes were:

1. **Aggressive force-exit**: Reconciliation instantly killed watches on a single bulk `get_positions()` miss, with no verification. 11 watches were falsely force-exited at 0% P&L.
2. **Orphan positions**: Buy orders that timed out weren't cancelled, leading to fills with no corresponding watch.
3. **Double-buys**: After false force-exits, the same symbols were re-bought (Alpaca already held them).
4. **Wrong exit prices**: Force-exits used entry price (recording 0% P&L) instead of actual sell fill price.

## Fixes Applied (2026-03-10)

All in the `trader` branch. Key files changed:

- `trader/market/alpaca_reconcile.py` — Safe 3-step verification before force-exit
- `trader/market/alpaca_broker.py` — `get_recent_sells()`, cancel-on-timeout in `buy_and_confirm()`
- `trader/online/live_monitor.py` — Pre-buy Alpaca position check, periodic reconciliation every 15 min
- `trader/web/app.py` — Portfolio value uses Alpaca equity (source of truth) for linked accounts

## Test Scenarios to Verify

Write a script `scripts/test_reconcile_stress.py` that uses Paper2 or Paper3 to simulate each failure mode and verify the fix works. Each test should be independent and clean up after itself.

### Test 1: Pre-buy duplicate prevention
1. Manually buy AAPL on Paper2 via broker API
2. Create a fake snapshot that would trigger a buy for AAPL
3. Run `_evaluate_for_config()` — should SKIP with "Alpaca already holds position"
4. Verify no second buy was placed
5. Clean up: close AAPL position

### Test 2: Reconciliation — position exists but bulk misses it
1. Buy TSLA on Paper2
2. Create a holding watch for TSLA
3. Mock `get_positions()` to return empty list (simulating transient API miss)
4. Run `reconcile()` — should NOT force-exit (per-symbol lookup finds it)
5. Verify watch is still "holding"
6. Clean up

### Test 3: Reconciliation — confirmed sell (stop triggered)
1. Buy MSFT on Paper2, create holding watch
2. Sell MSFT on Paper2 (simulating stop trigger)
3. Run `reconcile()` — should force-exit with actual sell price (not entry price)
4. Verify watch exit price matches the sell fill price
5. Clean up

### Test 4: Reconciliation — orphan adoption
1. Buy GOOG on Paper2 (no watch exists)
2. Run `reconcile()` with a live_config — should adopt the orphan
3. Verify a new watch was created with correct entry price and qty
4. Verify stop order was placed
5. Clean up

### Test 5: Buy timeout + cancel
1. Submit a limit buy for an illiquid stock at a price that won't fill
2. Wait for `ALPACA_FILL_TIMEOUT` to expire
3. Verify the order was cancelled (not left open)
4. Verify no watch was created
5. Clean up

### Test 6: Periodic reconciliation catches drift
1. Buy a position on Paper2, create watch
2. Sell the position manually (simulating stop fill while monitor was busy)
3. Wait for periodic reconcile to run (or call `_periodic_reconcile()` directly)
4. Verify the watch was force-exited with correct sell price
5. Verify stops are re-checked for remaining positions

### Test 7: Alpaca equity sync
1. Create a live_config linked to Paper2
2. Buy a few positions
3. Load the dashboard
4. Verify `sim_ending` matches Paper2's actual equity (not sim calculation)
5. Clean up

## Implementation Notes

- Use `AlpacaBrokerPool` with Paper2/Paper3 credentials
- Each test should print PASS/FAIL clearly
- Tests must clean up all positions and watches they create
- Can run during market hours (paper trading is always open) or after hours
- The script should be runnable standalone: `uv run python scripts/test_reconcile_stress.py`
- Use the existing `trader.db` but create watches with a distinct `live_config_id` (e.g., `lc_test_reconcile`) so they don't interfere with Paper1's real data
- Delete test watches after each test

## Running

```bash
# Prerequisites: .env with Paper2 or Paper3 credentials, trader app NOT running
# (to avoid interference with periodic reconciliation)

uv run python scripts/test_reconcile_stress.py
```
