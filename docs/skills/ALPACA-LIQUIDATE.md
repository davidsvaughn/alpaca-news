# Alpaca: Liquidating Paper Accounts

> How to cancel all orders and close all positions on Alpaca paper accounts — including dealing with stuck orders.

## Quick reference

| Task | How |
|------|-----|
| Cancel all orders | `client.cancel_orders()` or `DELETE /v2/orders` |
| Close all positions | `client.close_all_positions(cancel_orders=True)` or `DELETE /v2/positions?cancel_orders=true` |
| Close one position | `client.close_position("SYMBOL")` |
| Cancel one order | `client.cancel_order_by_id(order_id)` |

**Env vars** (in `.env`):

| Account | Key var | Secret var |
|---------|---------|------------|
| AlpacaPaper1 | `ALPACA_API_KEY_1` | `ALPACA_SECRET_KEY_1` |
| AlpacaPaper2 | `ALPACA_API_KEY_2` | `ALPACA_SECRET_KEY_2` |
| AlpacaPaper3 | `ALPACA_API_KEY_3` | `ALPACA_SECRET_KEY_3` |

## Procedure

### Step 1: Cancel all orders first

Orders (especially stop orders) hold shares, making positions un-closeable. Always cancel orders before closing positions.

```python
from alpaca.trading.client import TradingClient

client = TradingClient(api_key, secret_key, paper=True)

# Cancel all open orders
client.cancel_orders()
```

Wait 2-3 seconds after cancelling before proceeding.

### Step 2: Close all positions

```python
# Close all positions (also cancels any remaining orders)
client.close_all_positions(cancel_orders=True)
```

### Step 3: Verify

```python
import time
time.sleep(3)

orders = client.get_orders()
positions = client.get_all_positions()
print(f"Orders: {len(orders)}, Positions: {len(positions)}")

for o in orders:
    print(f"  {o.symbol} {o.side.value} {o.type.value} status={o.status.value} id={o.id}")
for p in positions:
    print(f"  {p.symbol}: qty={p.qty}")
```

### Step 4: Handle stuck orders (if any remain)

If orders are stuck in `pending_cancel`, positions tied to them will show `available: 0` and refuse to close. This is a known Alpaca paper trading bug.

**Symptoms:**
- Orders stuck in `pending_cancel` status indefinitely
- Cancel attempts return `422` / `"order pending cancel"`
- Position close returns `"insufficient qty available for order"` with `available: 0` and `held_for_orders` equal to full qty
- `close_all_positions` returns `503` / `"service unavailable"`

**There is no API fix for stuck `pending_cancel` orders.** Options:

1. **Delete and recreate the paper account** in the Alpaca dashboard. This is the fastest nuclear option:
   - Dashboard → click paper account number (upper left) → "Account Settings" → "Delete Account"
   - Then: "Open New Paper Account" → generate new API keys
   - Update `.env` with new `ALPACA_PAPER_ACCOUNT_1`, `ALPACA_API_KEY_1`, `ALPACA_SECRET_KEY_1`
   - New account starts with $100k balance
2. **Contact Alpaca support** (support@alpaca.markets) with the stuck order IDs and ask them to force-cancel server-side.
3. **Wait for market session boundary** — stuck orders sometimes resolve on their own at market open/close. No guarantee.

> **Why can't this be automated?** Paper account create/delete is dashboard-only. The Broker API (`/v1/accounts`) supports account creation, but requires broker-dealer credentials — not available to individual Trading API keys (`PK...`).

## Full liquidation script

Run from project root:

```bash
uv run python scripts/alpaca_liquidate.py          # all accounts
uv run python scripts/alpaca_liquidate.py 1         # Paper1 only
uv run python scripts/alpaca_liquidate.py 1 2       # Paper1 and Paper2
```

## Gotchas

- **Stop orders hold shares**: A sell-stop on 100 shares means `held_for_orders=100`, `available=0`. You MUST cancel the stop before you can close the position.
- **`pending_cancel` is a dead end**: Once an order enters `pending_cancel` and stays there, no API call can fix it. Don't waste time retrying.
- **`close_all_positions` can 503**: The paper trading backend is flaky. If bulk close fails, fall back to closing positions one-by-one.
- **Use `.value` on enums**: `str(OrderStatus.FILLED)` gives `"OrderStatus.FILLED"`, not `"filled"`. Always use `.value`.
- **Paper vs live**: Paper accounts use `paper=True` and `https://paper-api.alpaca.markets`. Never run liquidation scripts against live accounts without extreme care.

## Cross-references

- [ALPACA.md](ALPACA.md) — Order lifecycle, failure modes, reconciliation
- [DIAGNOSTICS.md](DIAGNOSTICS.md) — Log sources, SQL queries for `alpaca_transactions`
- [ALPACA-TRADING.md](../ALPACA-TRADING.md) — Multi-account setup, extended hours, confirmed fills
