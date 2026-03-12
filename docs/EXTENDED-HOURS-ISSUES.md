# Extended Hours Trading Issues (2026-03-12)

Observed on AlpacaPaper3 after market close (~16:00–16:25 ET) with `ALPACA_EXTENDED_HOURS=true`.

---

## 1. Buy limit orders timing out (ACET, MOLN, LOCO, UEIC, KRT, CIA)

**Severity:** Low — expected behavior for extended hours

`_buy_extended()` submits a limit buy with `extended_hours=True` and a 2% slippage buffer above ask price (4% if falling back to last trade). Orders sit at `status=new` for 60s and never fill. After timeout, `buy_and_confirm()` cancels them.

**Root cause:** Thin liquidity during extended hours, especially for small/mid-cap stocks. Either no sellers exist at the limit price, or the spread is too wide for the slippage buffer to bridge.

**Relevant code:**
- `trader/market/alpaca_broker.py:429` — `_buy_extended()` (limit order submission)
- `trader/market/alpaca_broker.py:805` — `buy_and_confirm()` (timeout + cancel logic)
- Timeout: `ALPACA_EXTENDED_FILL_TIMEOUT` env var (default 60s)
- Slippage: 2% with ask price, 4% without (`slippage_pct` param)

**Options:**
- Increase `slippage_pct` for extended hours
- Increase `ALPACA_EXTENDED_FILL_TIMEOUT` beyond 60s
- Filter out low-liquidity symbols during extended hours (e.g., skip if spread too wide or volume too low)
- Accept as expected — some stocks simply don't trade after hours

---

## 2. HKHC: Cannot get price for extended-hours limit order

**Severity:** Low — working as designed

Both `_get_latest_ask_price()` and `_get_latest_price()` returned `None`/0 for HKHC. Alpaca's data API had no quote or trade data at all.

**Root cause:** HKHC likely has zero extended-hours activity — no quotes, no trades. The code correctly raises `ValueError` rather than submitting a blind order.

**Relevant code:**
- `trader/market/alpaca_broker.py:447-451` — price lookup + ValueError raise
- `trader/market/alpaca_broker.py:509` — `_get_latest_ask_price()`
- `trader/market/alpaca_broker.py:491` — `_get_latest_price()`

**Options:**
- Could add a more informative log message noting this is likely due to no extended-hours activity
- No real fix needed

---

## 3. CAPR: Sell timeout during extended hours (BUG)

**Severity:** Medium — bug in `close_position_and_confirm`

`_exit_victim` tried to sell CAPR as a replacement. The sell order was submitted but timed out at 30s (`status=new`).

**Bug:** `close_position_and_confirm()` uses the default `timeout_s=ALPACA_FILL_TIMEOUT` (30s) even during extended hours. The buy path (`buy_and_confirm()`) correctly checks `in_extended_only()` and uses the 60s extended timeout, but the sell path does not.

**Relevant code:**
- `trader/market/alpaca_broker.py:858` — `close_position_and_confirm(timeout_s=ALPACA_FILL_TIMEOUT)` — hardcoded 30s default
- `trader/market/alpaca_broker.py:819-824` — `buy_and_confirm()` correctly selects extended timeout
- `trader/online/live_monitor.py:1165` — `_exit_victim` calls `close_position_and_confirm` without overriding timeout

**Fix:** `close_position_and_confirm` should dynamically select the extended timeout the same way `buy_and_confirm` does:

```python
def close_position_and_confirm(
    self,
    symbol: str,
    timeout_s: float | None = None,
) -> OrderResult | None:
    if timeout_s is None:
        from trader.market.market_hours import in_extended_only, ALPACA_EXTENDED_HOURS
        if ALPACA_EXTENDED_HOURS and in_extended_only():
            timeout_s = ALPACA_EXTENDED_FILL_TIMEOUT
        else:
            timeout_s = ALPACA_FILL_TIMEOUT
    ...
```

---

## 4. CAPR: `ensure_stops` races with `_exit_victim`

**Severity:** Medium — concurrency issue

The timeline:
- **16:18:51** — `ensure_stops` tries to set a stop for CAPR (144 shares)
- Alpaca returns 403: `insufficient qty available (requested: 144, available: 0)` — an existing sell order (`a086322e`) already holds all shares
- **16:18:53** — `_exit_victim` reports that same sell order (`a086322e`) timed out after 30s

`ensure_stops` and `_exit_victim` ran concurrently. The replacement sell had already been submitted (holding all shares), so the stop submission failed. Then the sell itself timed out (see issue #3 above — 30s is too short for extended hours).

**Relevant code:**
- `trader/market/alpaca_reconcile.py:527` — `ensure_stops` submitting stop orders
- `trader/online/live_monitor.py:1156-1172` — `_exit_victim` cancel stop → close position flow

**Options:**
- `ensure_stops` could skip symbols that have pending sell orders (check open orders before attempting stop)
- Fixing issue #3 (longer sell timeout) may reduce the window where this race matters
- Could add coordination so `ensure_stops` doesn't run while a replacement is in progress

---

## Summary

| # | Issue | Severity | Action |
|---|---|---|---|
| 1 | Buy timeouts on illiquid stocks | Low | Expected; consider liquidity filter |
| 2 | No price data (HKHC) | Low | Working as designed |
| 3 | **`close_position_and_confirm` uses 30s timeout in extended hours** | **Medium** | **Bug — should match `buy_and_confirm` logic** |
| 4 | **`ensure_stops` races with `_exit_victim`** | **Medium** | **Needs coordination or sell-order awareness** |
