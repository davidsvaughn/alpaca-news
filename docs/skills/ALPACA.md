# Alpaca: Broker Operations & Order Lifecycle

> How Alpaca orders work, common failure modes, reconciliation behavior, and extended hours quirks.

## Quick reference

| Task | How |
|------|-----|
| Check order status | Query `alpaca_transactions` ([DIAGNOSTICS.md](DIAGNOSTICS.md#alpaca-transactions-query)) |
| See current positions | `/api/positions` or `broker.get_positions()` |
| Force reconciliation | Click "Sync" in UI or `POST /api/portfolio/{config_id}/sync` |
| Check account equity | `/api/alpaca/status` |
| View open orders | `broker.get_open_orders()` |

**Key files:**

| File | What it does |
|------|--------------|
| [alpaca_broker.py](../../trader/market/alpaca_broker.py) | `AlpacaBroker`, buy/sell/stop, extended hours routing |
| [alpaca_reconcile.py](../../trader/market/alpaca_reconcile.py) | `reconcile()`, `ensure_stops()`, fractional cleanup |
| [alpaca_stream.py](../../trader/market/alpaca_stream.py) | WebSocket trade update events (fills, cancels) |
| [live_monitor.py](../../trader/online/live_monitor.py) | `LivePortfolioManager` (buys), `LiveExitMonitor` (sells) |
| [market_hours.py](../../trader/market/market_hours.py) | `in_extended_only()`, `is_trading_session_open()` |

## Order lifecycle

### Buy flow

```
LivePortfolioManager._evaluate_for_config()
  → broker.buy_and_confirm(symbol, notional=N)
    → broker.buy(symbol, notional=N)
      → regular hours: _buy_market()     → MarketOrderRequest
      → extended hours: _buy_extended()  → LimitOrderRequest (ask price + buffer)
    → broker.wait_for_fill(order_id, timeout_s)
      → polls get_order_by_id() every 0.5s
      → success: returns OrderResult with filled_avg_price
      → timeout: cancels order, raises TimeoutError
```

### Sell flow (exit strategy fired)

```
LiveExitMonitor._check_holding()
  → evaluate_exit() fires
  → broker.cancel_order(stop_id)           # cancel stop first
  → broker.close_position_and_confirm()
    → broker.close_position(symbol)
      → regular hours: _close_position_market()    → client.close_position()
      → extended hours: _close_position_extended() → cancel stops, limit sell, whole shares only
    → broker.wait_for_fill(order_id, timeout_s)
```

### Reconciliation flow (periodic + on Sync)

```
reconcile(broker, db, live_config_id, live_config)
  1. Fetch all Alpaca positions + all holding watches
  2. Both exist + qty < 1 → fractional cleanup (cancel stops, close, exit watch)
  3. Both exist + qty >= 1 → sync entry_price and qty from Alpaca
  4. Watch exists, no Alpaca position → verify per-symbol, check sell history, force-exit
  5. Alpaca position, no watch + qty < 1 → fractional cleanup (cancel stops, close)
  6. Alpaca position, no watch + qty >= 1 → adopt into new watch (or close if close_orphans)
```

## Timeouts

| Context | Timeout | Env var | Default |
|---------|---------|---------|---------|
| Regular hours buy/sell | `ALPACA_FILL_TIMEOUT` | `ALPACA_FILL_TIMEOUT` | 30s |
| Extended hours buy/sell | `ALPACA_EXTENDED_FILL_TIMEOUT` | `ALPACA_EXTENDED_FILL_TIMEOUT` | 60s |
| Reconciliation interval | `RECONCILE_INTERVAL` | — | ~15 min |

On timeout, `buy_and_confirm` cancels the pending order to prevent orphan positions. If the order filled in the tiny window between timeout and cancel, it's still accepted.

## Extended hours behavior

**When:** `ALPACA_EXTENDED_HOURS=true` and outside 9:30-16:00 ET (pre-market 4:00 AM, after-hours until 8:00 PM)

**Constraints** (Alpaca-imposed):
- Limit orders only (market orders rejected)
- Whole shares only (fractional not supported)
- `time_in_force=DAY`, `extended_hours=True`
- Stop orders only fire during regular hours (stops are `type=stop`, not limit — cannot use `extended_hours=True`; `live_monitor` is the only after-hours exit protection)

### Buy pricing during extended hours

`_buy_extended()` uses the **live ask price** from `get_stock_latest_quote()` + 2% buffer. Falls back to last trade price + 4% buffer if the quote is unavailable. This is critical because `get_stock_latest_trade()` can return prices hours stale during pre/post-market.

See: [ALPACA-TRADING.md > Extended hours trading](../ALPACA-TRADING.md#6-extended-hours-trading)

### Fractional remainder cleanup

After an extended-hours sell (whole shares only), a sub-1-share fractional remainder may stay on Alpaca. These are automatically cleaned up by `reconcile()` once regular hours resume:

1. Cancel any stop orders holding the shares
2. Close the fractional position via market order
3. Exit the associated watch (if one exists)

During extended hours, fractional remainders are skipped (can't sell fractional outside regular hours).

## Common failure modes

### "held_for_orders" — stop orders blocking sell

**Error:** `insufficient qty available for order (requested: X, available: 0)`
**Cause:** Open stop orders reserve ("hold") shares on Alpaca. You must cancel them before selling.
**Fix:** Call `broker._cancel_open_orders(symbol)` before `broker.close_position(symbol)`.
**Where it matters:** Reconciliation fractional cleanup, extended hours sells.

### Order timeout — status stays "new"

**Error:** `Order X not filled after 30.0s (status=new)`
**Causes:**
- Extended hours: limit price too low (stale trade price used instead of ask)
- Low liquidity stock: no matching orders on the book
- Alpaca paper API lag: order filled but status API is slow to update

**Diagnosis:** Check if reconciliation later adopted the position (means order actually filled).

### alpaca-py enum gotcha

`str(OrderStatus.FILLED)` returns `"OrderStatus.FILLED"`, NOT `"filled"`.
**Always use `.value`** to get the lowercase string. Same for `OrderSide`, `TradeEvent`, etc.

### Non-fractionable stocks

Notional orders fail for non-fractionable assets. `buy()` auto-detects via `is_fractionable()` and converts to whole-share qty. No action needed, but be aware of it in logs.

## Multi-account setup

Three paper accounts configured via env vars:

```
ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_PAPER_ACCOUNT / ALPACA_PAPER_NAME
ALPACA_API_KEY_2 / ALPACA_SECRET_KEY_2 / ALPACA_PAPER_ACCOUNT_2 / ALPACA_PAPER_NAME_2
ALPACA_API_KEY_3 / ALPACA_SECRET_KEY_3 / ALPACA_PAPER_ACCOUNT_3 / ALPACA_PAPER_NAME_3
```

- `AlpacaAccountRegistry` discovers all configured accounts
- `AlpacaBrokerPool` manages one broker per account (lazy-initialized)
- Each `LiveConfig` is linked to one account via `alpaca_account_id`
- Reconciliation runs per-config (filtered by `live_config_id`)

## Transaction log

All order activity is recorded in the `alpaca_transactions` SQLite table. This is the **most reliable diagnostic source** — more complete than `trader.log` (which only captures WARNING+).

See [DIAGNOSTICS.md > Alpaca transaction log](DIAGNOSTICS.md#1-alpaca-transaction-log-sqlite--most-complete) for queries and event type reference.

## Cross-references

- [DIAGNOSTICS.md](DIAGNOSTICS.md) — How to query logs and transaction tables
- [ALPACA-TRADING.md](../ALPACA-TRADING.md) — Full Alpaca trading documentation (order execution, multi-account, fills)
- [LIVE-TRADING.md](../LIVE-TRADING.md) — Live trading plan and implementation
- [RECONCILE-TEST-PLAN.md](../RECONCILE-TEST-PLAN.md) — Stress test plan for reconciliation
