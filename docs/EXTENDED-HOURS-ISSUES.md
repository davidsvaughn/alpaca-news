# Extended Hours Trading Issues (2026-03-12)

Observed on `AlpacaPaper3` after regular close with `ALPACA_EXTENDED_HOURS=true`.

Notes on timestamps:
- App logs shown in incident are ET.
- `alpaca_transactions.created_at` in SQLite is UTC.

## Executive Summary

1. Many extended-hours buy failures are expected liquidity misses, not code defects.
2. `HKHC` no-price failure is expected when Alpaca has no quote/trade data (and is consistent with OTC metadata).
3. The sell confirmation path has real defects relative to the buy path:
   - Uses 30s timeout by default even in extended hours.
   - Does not perform buy-style timeout recovery (cancel + final recheck), causing false failure signals.
4. `ensure_stops` and replacement exits can race, producing noisy `insufficient qty available` errors while a sell is already holding shares.
5. Repeated exit attempts on the same symbol can churn multiple extended sell orders in short windows.

---

## 1) Extended-Hours Buy Timeouts (Mostly Expected)

### What happened
Symbols like `ACET`, `MOLN`, `LOCO`, `UEIC`, `KRT`, `CIA` submitted extended-hours limit buys, stayed `new` for ~60s, then were canceled after timeout.

### Why this is expected
`_buy_extended()` submits limit orders only (required for extended hours), with a small aggressive buffer:
- +2% over ask when ask exists
- +4% when falling back to last trade

Thin after-hours books often do not cross that limit within 60s.

### Relevant code
- `trader/market/alpaca_broker.py:429` `_buy_extended()`
- `trader/market/alpaca_broker.py:805` `buy_and_confirm()` timeout + cancel
- `trader/market/alpaca_broker.py:820-824` dynamic timeout selection for extended hours

### Clarification on `status=new` in traceback
The timeout message can report `status=new` because that status is captured at timeout poll. Cancellation happens immediately after in exception handling. This is expected sequencing, not contradiction.

### Operational options
- Increase `ALPACA_EXTENDED_FILL_TIMEOUT`
- Increase extended-hours buy slippage buffer
- Add after-hours liquidity filters (spread/quote presence/volume)
- Accept misses as normal behavior in low-liquidity symbols

---

## 2) HKHC No-Price Error (Expected)

### What happened
`ValueError: HKHC: cannot get price for extended-hours limit order`.

### Why this is expected
Code requires either latest ask or latest trade to compute a limit price. If both missing/invalid, it raises rather than placing a blind order.

### Supporting context
Snapshot metadata tags `HKHC` as OTC (`symbol_exchanges: {"HKHC":"OTC"}`), consistent with sparse after-hours quote/trade availability.

### Relevant code
- `trader/market/alpaca_broker.py:447-451` price lookup + raise
- `trader/market/alpaca_broker.py:509` `_get_latest_ask_price()`
- `trader/market/alpaca_broker.py:491` `_get_latest_price()`

---

## 3) Sell Confirmation Path Mismatch (Real Bug)

### What happened
Replacement exits (example: `CAPR`) reported timeout failures around 30s, yet some of those sell orders later partially/fully filled.

### Root causes
1. **Timeout asymmetry with buy path**
- `close_position_and_confirm()` defaults to `ALPACA_FILL_TIMEOUT` (30s) and does not dynamically switch to extended timeout.
- `buy_and_confirm()` does dynamic extended timeout selection.

2. **Timeout recovery asymmetry with buy path**
- `buy_and_confirm()` handles timeout with cancel + final state recheck.
- `close_position_and_confirm()` does not cancel/recheck on timeout; it just raises.

This creates false negatives ("sell failed") when order fills shortly after timeout.

### Relevant code
- `trader/market/alpaca_broker.py:855` `close_position_and_confirm()`
- `trader/market/alpaca_broker.py:805` `buy_and_confirm()` (reference behavior)
- `trader/online/live_monitor.py:1165` `_exit_victim()` uses `close_position_and_confirm()` default timeout

### Recommended fix
Make sell confirmation mirror buy confirmation:
- Dynamic timeout in extended-only sessions
- On timeout: cancel order, then final `get_order()` check
- If fill happened before cancel finalized, treat as success

---

## 4) `ensure_stops` vs Replacement Exit Race (Real Concurrency Issue)

### What happened (CAPR timeline)
- `16:18:21 ET`: replacement sell submitted (`a086...`) for full qty
- `16:18:51 ET`: `ensure_stops` attempted to set stop and got `403 insufficient qty available`, with `held_for_orders` pointing to that sell order
- `16:18:52 ET`: replacement flow logged sell timeout
- `16:20:03 ET`: stream later reported sell fill for that same order

### Interpretation
`ensure_stops` was not wrong about quantity availability; shares were legitimately held by an active sell. The race is between periodic protection maintenance and active replacement execution.

### Relevant code
- `trader/market/alpaca_reconcile.py:444` `ensure_stops()`
- `trader/market/alpaca_reconcile.py:527` stop submission call
- `trader/online/live_monitor.py:1134` `_exit_victim()`
- `trader/online/live_monitor.py:1380` periodic reconcile loop (`reconcile` + `ensure_stops`)

### Recommended fix
- In `ensure_stops()`, skip stop submit when symbol has open sell order (log as informational skip, not error).

---

## 5) Exit-Churn Pattern (Newly Identified Risk)

### What happened
For some symbols (notably `CRVS`, `KRMD`), multiple extended sell submissions occurred in short succession after timeout/cancel cycles.

### Why it matters
Repeated re-entry into exit logic before prior order lifecycle fully settles can create:
- noisy failure logs
- temporary stop/order desynchronization
- extra API calls and state churn

### Recommended fix
- Before submitting a new close order, check for existing open sell for symbol and either:
  - reuse/wait for it, or
  - explicitly cancel-and-confirm-clear before re-submit.

---

## Prioritized Action Plan

### P1 (should do first)
1. Update `close_position_and_confirm()` to use dynamic extended timeout (`None` default + session-based selection).
2. Add timeout recovery parity with `buy_and_confirm()` (cancel + final status check).
3. In `ensure_stops()`, skip symbols with open sell orders.

### P2 (stability and quality)
1. Add idempotency/guard in exit flow to avoid duplicate sell submissions while one is open.
2. Improve logging labels: distinguish `timeout_then_later_filled` from true terminal failures.

### P3 (strategy tuning)
1. Add optional extended-hours liquidity prefilters.
2. Consider separate env vars for extended-hours sell timeout and slippage tuning.

---

## Bottom Line

- Buy-side behavior in the reported examples is mostly expected extended-hours liquidity behavior.
- The sell-side path has genuine logic gaps (timeout handling asymmetry) that can misclassify eventual fills as failures.
- The stop-maintenance race is real but mostly a coordination/ordering issue, not an Alpaca API anomaly.

---

## Implemented Fixes (Applied 2026-03-12)

The following code changes were implemented:

1. Session-aware timeout resolution is now shared across buy/sell confirms.
- Added `_resolve_fill_timeout(timeout_s)` in `trader/market/alpaca_broker.py`.
- `buy_and_confirm()` now uses this helper (behavior unchanged, code deduplicated).
- `close_position_and_confirm()` now also uses session-aware timeout when `timeout_s` is not explicitly provided.

2. Duplicate extended-hours sell churn is reduced by reusing open close orders.
- Added `_get_open_non_stop_sell_order(symbol)` in `trader/market/alpaca_broker.py`.
- `close_position_and_confirm()` now checks for an existing open non-stop sell and reuses it instead of submitting another close order.

3. Sell timeout handling is safer and more explicit.
- In `close_position_and_confirm()`, a timeout now performs one final `get_order()` check.
- If already filled at that point, it is treated as success.
- If still unfilled, it logs `sell_failed` with status `timeout_open` and raises timeout while intentionally leaving the working order open.

4. `ensure_stops()` now avoids racing active close orders.
- In `trader/market/alpaca_reconcile.py`, `ensure_stops()` now detects open non-stop sell orders and skips stop submission for those symbols.
- Added `skipped_open_sell` to summary output and log totals.

5. Regression tests were added for these behaviors.
- `tests/test_alpaca_broker.py`:
  - reuse existing open sell order in `close_position_and_confirm()`
  - timeout path logs `timeout_open` when order remains working
- `tests/test_alpaca_reconcile.py`:
  - `ensure_stops()` skips stop placement when a non-stop sell is already open

Validation run:
- `pytest -q tests/test_alpaca_broker.py tests/test_alpaca_reconcile.py`
- Result: `5 passed`
