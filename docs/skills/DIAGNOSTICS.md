# Diagnostics: Where to Find What

> How to investigate errors, check order status, and find the right data source. Read this BEFORE looking at logs.

## Quick reference

| What you need | Where to look | How |
|---------------|---------------|-----|
| All order activity (buys, sells, fills, failures) | `alpaca_transactions` table | [SQL query](#alpaca-transactions-query) |
| Errors, tracebacks, warnings | `logs/trader.log` | `cat logs/trader.log` |
| Real-time console output | Terminal running `trader.main` | Prefixed lines (see [Console prefixes](#console-output-prefixes)) |
| Position lifecycle (hold/exit/seal) | `watches` table | [SQL query](#watches-query) |
| Investigation results | `snapshots` table | `/api/snapshots` or [SQL query](#snapshots-query) |
| Current Alpaca positions | Alpaca API via broker | `/api/positions` or `/api/alpaca/status` |
| Cost breakdown | SQLite helpers | `/costs` page or `get_daily_cost_today_by_provider(db)` |
| SSE events (7-day history) | `event_log` table | `/api/events/recent` |

## Log sources (ranked by usefulness)

### 1. Alpaca transaction log (SQLite) — MOST COMPLETE

The `alpaca_transactions` table is the **single source of truth** for all order activity. Every buy, sell, fill, failure, reconciliation action, and stream event is logged here with full detail.

**Unlike `trader.log`, this captures ALL events regardless of log level.**

#### Alpaca transactions query

```python
from trader.db.database import open_sqlite
import os

db = open_sqlite(os.getenv("TRADER_DB", "data/trader.db"))
from sqlalchemy import text

with db.engine.connect() as conn:
    rows = conn.execute(text(
        "SELECT created_at, event, symbol, status "
        "FROM alpaca_transactions "
        "WHERE created_at >= '2026-03-11' "  # adjust date
        "ORDER BY created_at"
    )).fetchall()
    for r in rows:
        print(f"{r[0]}  {r[1]:35s} {r[2]:6s} {r[3]}")
```

**Filter to buys/sells only:**
```sql
WHERE event IN ('buy_submit', 'buy_confirmed', 'buy_failed',
                'buy_submit_extended', 'sell_submit', 'sell_confirmed',
                'sell_submit_extended', 'sell_failed')
```

**Filter by symbol:**
```sql
WHERE symbol = 'AAPL' ORDER BY created_at DESC LIMIT 20
```

**Filter by account:**
```sql
WHERE account_id = 'PA31OTPTIYRB' AND created_at >= '2026-03-11'
```

**Event types reference:**

| Event | Meaning |
|-------|---------|
| `buy_submit` / `buy_submit_extended` | Order sent to Alpaca |
| `buy_confirmed` | Fill confirmed (has `filled_qty`, `filled_avg_price` in detail_json) |
| `buy_failed` | Timeout, rejection, or error |
| `sell_submit` / `sell_submit_extended` | Sell order sent |
| `sell_confirmed` | Sell fill confirmed |
| `sell_failed` | Sell timeout or error |
| `stop_submit` | Stop-loss order placed |
| `stop_triggered` | Stop-loss fired |
| `stream_fill` / `stream_canceled` | WebSocket trade update events |
| `reconcile_ok` | Position in sync |
| `reconcile_force_exit` | Watch exited due to missing Alpaca position |
| `reconcile_orphan_adopted` | Alpaca position adopted into a new watch |
| `reconcile_fractional_liquidated` | Sub-1-share remainder sold |
| `reconcile_price_updated` / `reconcile_qty_updated` | Watch synced to Alpaca |

### 2. Trader log file (WARNING+ only)

**Path:** `logs/trader.log`
**Level:** WARNING and above (errors, tracebacks, failed orders, timeouts)
**Rotation:** Daily at midnight, 14-day retention

```bash
cat logs/trader.log          # recent errors
tail -f logs/trader.log      # live tail
grep "SYMBOL" logs/trader.log  # filter by symbol
```

**Env vars:** `LOG_FILE`, `LOG_LEVEL` (default: `WARNING`), `LOG_KEEP_DAYS` (default: `14`)
**Config:** [logging_config.py](../../trader/logging_config.py)

### 3. Console output (INFO+, not persisted)

The terminal running the trader shows INFO-level output. This is **more verbose** than `trader.log` but is **not persisted** — if the process restarts, it's gone.

#### Console output prefixes

| Prefix | Source | Meaning |
|--------|--------|---------|
| `LIVE-PM:` | live_monitor.py | Portfolio manager buy decision |
| `LIVE-EVAL:` | live_monitor.py | Symbol evaluation (filters, allocation, confidence) |
| `LIVE-EXIT:` | live_monitor.py | Exit strategy fired |
| `LIVE-DEBUG:` | orchestrator.py | Watch creation decision trace |
| `RECONCILE:` | alpaca_reconcile.py | Reconciliation actions |
| `RECONCILE WARNING:` | alpaca_reconcile.py | Phantom miss (investigate!) |
| `RECONCILE ERROR:` | alpaca_reconcile.py | Position vanished without sell order |
| `ONLINE:` | online_mode.py | Market hours, mode changes |
| `SKIP watch:` | orchestrator.py | Watch creation rejected (reason follows) |
| `SKIP (already processed):` | orchestrator.py | Duplicate news event |
| `TRIAGE TIMEOUT:` | orchestrator.py | Triage agent timed out |

### 4. SQLite watches table

#### Watches query

```python
with db.engine.connect() as conn:
    rows = conn.execute(text(
        "SELECT watch_id, symbol, status, created_at "
        "FROM watches "
        "WHERE status = 'holding' "
        "ORDER BY created_at DESC"
    )).fetchall()
```

**Watch statuses:** `holding` → `exited` → `cooling_off` → `sealed`

### 5. SQLite snapshots table

#### Snapshots query

```python
with db.engine.connect() as conn:
    rows = conn.execute(text(
        "SELECT snapshot_id, symbols, created_at "
        "FROM snapshots "
        "WHERE created_at >= '2026-03-11' "
        "ORDER BY created_at DESC"
    )).fetchall()
```

### 6. Dashboard API endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /api/stats` | Summary counts, today's cost |
| `GET /api/positions` | Current Alpaca positions + watch status |
| `GET /api/alpaca/status` | Account equity, cash, buying power |
| `GET /api/events/recent` | Last 200 SSE events (7-day retention) |
| `GET /api/activity-panel` | In-flight jobs (backfill, exploration) |
| `GET /api/debug/shadow` | Volume delta shadow collector state |
| `POST /api/portfolio/{config_id}/sync` | Force reconciliation |

## Proactive log review

At the start of a conversation (especially during or after market hours), **check the error logs proactively**:

1. Read `logs/trader.log` for today's errors and warnings
2. If yesterday's rotated log exists (`trader.log.YYYY-MM-DD`), scan it too
3. Summarize what you find for the user — categorize by error type, affected symbols, and frequency
4. **Do NOT implement fixes without asking the user first.** Present findings and proposed fixes, then wait for approval before changing any code.

This applies even if the user hasn't asked about errors — surface issues early so they can be triaged.

## Common diagnostic workflows

### "Why didn't the system buy X today?"

1. Check the transaction log for buy attempts:
   ```sql
   SELECT * FROM alpaca_transactions
   WHERE symbol = 'X' AND created_at >= 'today' ORDER BY created_at
   ```
2. If no rows: the symbol was filtered out before reaching Alpaca. Check console for `LIVE-EVAL: X SKIP` lines.
3. If `buy_submit` but no `buy_confirmed`: the order timed out or was rejected. Check `detail_json` for error.
4. If `buy_confirmed` + `sell_confirmed`: it was bought and sold (exit strategy fired).

### "Why did a buy fail?"

1. Query `alpaca_transactions WHERE event = 'buy_failed' AND symbol = 'X'`
2. Check `detail_json` for the status (`timeout_cancelled`, `rejected`, etc.)
3. Check `trader.log` for the full traceback (search by order ID or symbol)
4. Common causes:
   - **Timeout (30s/60s)**: order stayed `new`/`pending_new` — liquidity issue or stale limit price
   - **`held_for_orders`**: existing stop order holding shares — need to cancel stops first
   - **`insufficient qty`**: tried to sell more than available
   - **403 Forbidden**: Alpaca rejected (usually order constraint violation)

### "Is the system actively trading?"

1. Check the transaction log for today's activity (see [quick reference](#quick-reference))
2. **DO NOT** rely solely on `trader.log` — it only shows WARNING+, so successful buys don't appear there

## Gotchas

### `trader.log` only shows WARNING+ (the #1 agent mistake)

The file logger is set to `WARNING` by default. **Successful operations log at INFO level and do NOT appear in `trader.log`.** If you only check `trader.log`, you'll see only failures and conclude everything is broken — when in reality, many operations succeeded.

**Always check the `alpaca_transactions` table for the complete picture.**

> This mistake was made on 2026-03-11: an agent checked only `trader.log`, saw only buy failures, and incorrectly reported "every single buy attempt today failed" — when in fact dozens of buys succeeded.

### Console output is not persisted

INFO-level print statements (`LIVE-PM:`, `LIVE-EVAL:`, etc.) go to the terminal but are lost on restart. For historical analysis, use the SQLite tables.

### Timestamps in logs vs database

- `trader.log`: local time (ET), format `YYYY-MM-DD HH:MM:SS`
- `alpaca_transactions.created_at`: UTC (SQLite `func.now()`)
- Console output: local time, format `HH:MM:SS`
- Watch/snapshot JSON: UTC ISO 8601

### Database path

Default: `data/trader.db`. Override with `TRADER_DB` env var. Open with:
```python
from trader.db.database import open_sqlite
import os
db = open_sqlite(os.getenv("TRADER_DB", "data/trader.db"))
```

## Cross-references

- [ALPACA.md](ALPACA.md) — Alpaca order lifecycle, reconciliation details, extended hours behavior
- [ALPACA-TRADING.md](../ALPACA-TRADING.md) — Full Alpaca trading documentation
- [LIVE-TRADING.md](../LIVE-TRADING.md) — Live trading plan and implementation
- [logging_config.py](../../trader/logging_config.py) — Logger setup
- [alpaca_broker.py](../../trader/market/alpaca_broker.py) — Broker implementation
- [alpaca_reconcile.py](../../trader/market/alpaca_reconcile.py) — Reconciliation logic
- [database.py](../../trader/db/database.py) — SQLite schema and query helpers
