# Alpaca Paper Trading Integration

> Technical reference for the Alpaca order execution layer.
> Covers architecture, multi-account setup, confirmed execution,
> account sync, reconciliation, edge cases, and future considerations.
>
> Created: 2026-03-06 | Last updated: 2026-03-06
>
> See also: [LIVE-TRADING.md](LIVE-TRADING.md) — overall live trading architecture, exit strategies, portfolio management

---

## Table of Contents

1. [Overview](#overview)
2. [Multi-Account Architecture](#multi-account-architecture)
3. [Environment Configuration](#environment-configuration)
4. [Key Files](#key-files)
5. [Go Live Flow](#go-live-flow)
6. [Account Sync on Connect](#account-sync-on-connect)
7. [Confirmed Execution](#confirmed-execution)
8. [Order Lifecycle: Entry](#order-lifecycle-entry)
9. [Order Lifecycle: Exit](#order-lifecycle-exit)
10. [Two Exit Paths](#two-exit-paths)
11. [Trade Update Stream](#trade-update-stream)
12. [Reconciliation](#reconciliation)
13. [Portfolio ↔ Account Linking](#portfolio--account-linking)
14. [Position Sizing](#position-sizing)
15. [Data Source Separation](#data-source-separation)
16. [Edge Cases & Complications](#edge-cases--complications)
17. [Design Decisions](#design-decisions)
18. [PDT Considerations](#pdt-considerations)
19. [Future Work](#future-work)

---

## Overview

Alpaca provides paper trading accounts that simulate real brokerage execution. Our system optionally connects a LiveConfig portfolio to an Alpaca paper account. When connected:

- **Buy signals** submit real market orders to Alpaca, **wait for fill confirmation**, then record actual fill price/qty
- **Guard stops** become server-side Alpaca stop orders (survive process crashes)
- **VDD exits** close the Alpaca position (confirmed), cancel the stop order
- **Alpaca-triggered stops** are detected via WebSocket and update the watch
- **Account state is synced** on connect — starting capital matches actual equity

When NOT connected, portfolios run in virtual-only mode (same as before Phase 4). The `alpaca_account_id` field on LiveConfig is `None` and no orders are placed.

**Core principle: nothing is recorded until Alpaca confirms it happened.** No phantom positions, no estimated prices, no assumed fills.

---

## Multi-Account Architecture

Alpaca allows up to 3 paper trading accounts per user. Each has independent credentials, positions, and buying power. We support all 3 simultaneously.

```
                    AlpacaAccountRegistry
                    (reads env vars on init)
                           |
                    AlpacaBrokerPool
                    (lazy broker creation)
                     /       |        \
            AlpacaBroker  AlpacaBroker  AlpacaBroker
            (Paper1)      (Paper2)      (Paper3)
               |              |              |
         TradingClient  TradingClient  TradingClient
         (alpaca-py)    (alpaca-py)    (alpaca-py)
```

**AlpacaAccountRegistry** discovers accounts from environment variables at startup. Reads suffixes `""`, `"_2"`, `"_3"`, `"_4"`, `"_5"`.

**AlpacaBrokerPool** holds one `AlpacaBroker` per account, created lazily on first use. The pool is a module-level singleton (`_broker_pool`) in the orchestrator.

**One broker per account, one account per active portfolio.** Enforced by a 409 error on the config creation endpoint if the account is already linked to another active config.

---

## Environment Configuration

```bash
# Global flag — must be true for paper trading
ALPACA_PAPER=true

# Account 1 (primary)
ALPACA_PAPER_NAME=AlpacaPaper1
ALPACA_PAPER_ACCOUNT=PA31QXNAPB1H
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=p6...

# Account 2
ALPACA_PAPER_NAME_2=AlpacaPaper2
ALPACA_PAPER_ACCOUNT_2=PA3EQVH99AH8
ALPACA_API_KEY_2=PK...
ALPACA_SECRET_KEY_2=FD...

# Account 3
ALPACA_PAPER_NAME_3=AlpacaPaper3
ALPACA_PAPER_ACCOUNT_3=PA37Q17WUUHO
ALPACA_API_KEY_3=PK...
ALPACA_SECRET_KEY_3=yH...
```

All 4 vars (`NAME`, `ACCOUNT`, `API_KEY`, `SECRET_KEY`) must be set for an account to be discovered. The `NAME` var is optional (defaults to `"Paper1"`, `"Paper_2"`, etc.).

---

## Key Files

| File | Purpose |
|------|---------|
| `trader/market/alpaca_broker.py` | `AlpacaAccount`, `AlpacaAccountRegistry`, `AlpacaBrokerPool`, `AlpacaBroker` (incl. `buy_and_confirm`, `close_position_and_confirm`, `wait_for_fill`) |
| `trader/market/alpaca_stream.py` | `AlpacaTradeStream` — WebSocket listener for fill/cancel events (one per account) |
| `trader/market/alpaca_reconcile.py` | `reconcile()` — syncs Alpaca positions with watch state on startup |
| `trader/models/live_config.py` | `LiveConfig.alpaca_account_id` field |
| `trader/models/watch.py` | `Watch.alpaca_buy_order_id`, `Watch.alpaca_stop_order_id` |
| `trader/online/live_monitor.py` | `LivePortfolioManager` (buy path), `LiveExitMonitor` (exit path) |
| `trader/online/orchestrator.py` | Startup: pool init, reconciliation, stream launch |
| `trader/web/app.py` | `/api/alpaca/status` (positions included), config creation (sync + purge), one-to-one constraint |
| `trader/web/templates/snapshots.html` | "Go Live" account selection + purge prompt |
| `trader/web/templates/partials/_positions_table.html` | Alpaca badge on linked portfolios |

---

## Go Live Flow

When the user clicks "Go Live" from the backtest page:

```
1. UI fetches GET /api/alpaca/status
   → Returns all accounts with equity, cash, positions, linked status

2. UI shows account selection prompt
   → Free accounts shown with position count and equity
   → User picks account or "0 = virtual only"

3. If selected account has existing positions:
   → UI shows position list + asks "Sell all? (YES/NO)"
   → YES → body.purge_existing = true
   → NO → keep positions, start with current state

4. UI sends POST /api/live/config
   → Server syncs starting_capital from actual Alpaca equity
   → If purge_existing: sells all positions (confirmed fills)
   → Creates LiveConfig with actual equity as starting_capital
   → Returns config + alpaca_sync info

5. UI sends POST /api/live/config/{id}/activate
   → Portfolio goes live

6. UI shows confirmation with synced equity, purge results
```

---

## Account Sync on Connect

When a portfolio is linked to an Alpaca account at creation time, the server:

1. **Reads actual account state** — equity, cash, buying power, open positions
2. **Overrides `starting_capital`** with the real Alpaca equity (not the UI slider value)
3. **Optionally purges existing positions** — sells each with confirmed fills via `close_position_and_confirm()`
4. **Re-reads account state** after purge to get updated cash/equity
5. **Returns sync info** to the UI — synced equity, cash, purged positions, remaining positions

This ensures the portfolio's `starting_capital` reflects reality. If the account has $98,500 from prior trades, that's the starting point — not the UI default of $100,000.

---

## Confirmed Execution

**Every order waits for fill confirmation before proceeding.** No fire-and-forget.

### `wait_for_fill(order_id, timeout_s=15)`

Polls `get_order(order_id)` every 0.5s until:
- `status == "filled"` → returns `OrderResult` with actual `filled_avg_price` and `filled_qty`
- `status in ("canceled", "expired", "rejected", "suspended")` → raises `RuntimeError`
- Timeout exceeded → raises `TimeoutError`

### `buy_and_confirm(symbol, notional=...)`

`buy()` + `wait_for_fill()`. Returns confirmed fill with actual price and quantity.

### `close_position_and_confirm(symbol)`

`close_position()` + `wait_for_fill()`. Returns confirmed sell with actual exit price.

---

## Order Lifecycle: Entry

```
Snapshot sealed
    → LivePortfolioManager._evaluate_for_config()
    → Passes filters + allocation check
    → Creates WatchBuilder (with snapshot price as initial estimate)
    → If cfg.alpaca_account_id is set:
        1. broker.buy_and_confirm(symbol, notional=position_size)
           → Submits Alpaca MarketOrderRequest (DAY)
           → WAITS for fill confirmation (up to 15s)
           → If REJECTED/TIMEOUT: return False (NO watch created)
        2. Updates watch entry price with ACTUAL fill price
        3. broker.set_stop(symbol, qty=ACTUAL_FILLED_QTY, stop_price=...)
           → Alpaca StopOrderRequest (GTC, server-side)
           → Uses exact qty from confirmed buy fill (not an estimate)
    → Persists watch to DB + JSON (only after buy confirmed)
```

**Key behavior**: If the buy order fails (rejected, insufficient funds, timeout), **no watch is created at all**. The portfolio only contains positions that actually exist on Alpaca.

---

## Order Lifecycle: Exit

### VDD-triggered exit

```
LiveExitMonitor._check_holding()
    → evaluate_exit() returns should_exit=True
    → Cancel stop order FIRST (prevent race condition)
    → broker.close_position_and_confirm(symbol)
       → WAITS for sell fill confirmation
       → Exit price = actual Alpaca fill price
    → Records exit on watch with confirmed price
```

### Alpaca-triggered stop exit

```
AlpacaTradeStream._handle_sell_fill()
    → Matches order_id to watch.alpaca_stop_order_id
    → Records exit with actual fill price and reason "alpaca_stop_fill"
    → Watch transitions: holding → exited → cooling_off → sealed
```

---

## Two Exit Paths

A position can be closed by either of two independent mechanisms:

| Trigger | Who initiates | What happens |
|---------|--------------|--------------|
| **VDD divergence** | Our LiveExitMonitor (every ~60s) | Cancel stop → `close_position_and_confirm()` |
| **Guard stop hit** | Alpaca server-side | Stop order fills, trade stream notifies us |

**Race condition handling**:
- VDD fires first: Cancels stop (may already be triggered — cancel is no-op), sells position (confirmed). Clean.
- Stop fires first: Trade stream marks watch exited. VDD sees watch is no longer `"holding"`, skips it.

**No double-sell risk**: Alpaca prevents selling more shares than you hold. If both fire simultaneously, one succeeds and the other gets "position does not exist", handled gracefully.

---

## Trade Update Stream

Each linked Alpaca account gets its own `AlpacaTradeStream` running in a daemon thread. The stream receives events via WebSocket:

| Event | Action |
|-------|--------|
| `fill` (buy side) | Update watch entry price with actual fill price (backup for confirmed flow) |
| `fill` (sell side) | If matched to `alpaca_stop_order_id`: record exit on watch |
| `canceled` | Log warning |
| `rejected` | Log warning |
| `expired` | Log warning |

All events are published to the event bus as `alpaca_trade_update` for UI refresh.

**Reconnection**: If the WebSocket disconnects, the stream auto-reconnects after 5 seconds (infinite retry loop).

**Note**: With confirmed execution, the trade stream's buy-fill handler is mostly a safety net. The primary path already has the fill data from `buy_and_confirm()`. The stream is critical for stop-loss fills triggered by Alpaca.

---

## Reconciliation

Runs on startup for each linked account. Alpaca is always the source of truth.

| Scenario | Action |
|----------|--------|
| Alpaca has position + we have matching watch | OK (update entry price if mismatched) |
| We have watch + Alpaca has no position | Force-exit the watch (`reconcile_no_alpaca_position`) |
| Alpaca has position + we have no watch | Close the orphan position on Alpaca |
| Entry price differs by > $0.01 | Update watch entry to match Alpaca's `avg_entry_price` |

**When would these happen?**
- Process crash between buy confirmation and watch persist (unlikely but possible)
- Process crash during an exit
- Manual position changes on Alpaca dashboard
- Stop order filled while process was down

---

## Portfolio ↔ Account Linking

### Constraints

- A portfolio (LiveConfig) **does not have to** be linked to any Alpaca account. `alpaca_account_id = None` means virtual-only tracking.
- An Alpaca account can only be linked to **one active portfolio** at a time.
- Multiple portfolios can run simultaneously — up to one per account (max 3 on Alpaca, plus unlimited virtual-only).
- When a portfolio is deactivated or deleted, its account becomes available again.

### Enforcement

- **On create**: `POST /api/live/config` checks all active configs. If the requested `alpaca_account_id` is already linked, returns 409.
- **UI**: The "Go Live" dialog fetches `/api/alpaca/status`, which returns each account's `linked` flag. Already-linked accounts are excluded from the selection list.

---

## Position Sizing

When a portfolio is linked to Alpaca, the position size for each buy is:

```
notional = starting_capital * alloc_pct / 100
```

Where:
- `starting_capital` = actual Alpaca equity at time of portfolio creation (synced, not a UI guess)
- `alloc_pct` comes from `allocation_params.alloc_pct` (e.g., 5 = 5%)

**Current behavior**: Position size is fixed based on the synced starting capital. Wins/losses don't compound.

**Future consideration**: Use Alpaca's real-time `equity` for position sizing to enable compounding.

---

## Data Source Separation

| Purpose | Source | Why |
|---------|--------|-----|
| **VDD exit signals** | Schwab 1-min bars | Matches backtest exactly (full exchange volume) |
| **Guard stop execution** | Alpaca server-side | Survives process crashes |
| **Order execution** | Alpaca API | Paper trading |
| **Entry/exit prices** | Alpaca confirmed fill prices | Actual execution prices (slippage tracking) |
| **Market data for filters** | Schwab/MarketDataService | Same as backtest |

Alpaca's free data is IEX-only (~2-5% of exchange volume), insufficient for VDD. We only use Alpaca for orders, not data.

---

## Edge Cases & Complications

### 1. Buy rejected or timeout

If `buy_and_confirm()` raises (rejected, timeout, insufficient funds), **no watch is created**. The portfolio only tracks confirmed positions. Logged as `ALPACA BUY FAILED`.

### 1b. Non-fractionable stocks

Some stocks (e.g., MMED) are not fractionable on Alpaca. Notional (dollar-based) orders require fractional share support and will be **rejected** for these assets.

**Fix (2026-03-06)**: `buy()` now calls `is_fractionable()` before ordering. For non-fractionable stocks, it fetches the latest trade price via `StockHistoricalDataClient` and converts to whole shares: `qty = int(notional / price)`. If the notional amount is less than 1 share, the buy is skipped with a clear error.

### 2. Stop order submission fails after buy succeeds

The position exists on Alpaca but has no stop protection. Logged as `ALPACA STOP FAILED ... position open without stop protection!`. The VDD exit monitor still runs, but there's no server-side crash protection.

### 3. Partial fills

Market orders are almost always fully filled immediately on paper trading. The `wait_for_fill` only checks for `"filled"` status, not `"partially_filled"`. If a partial fill occurs, the timeout will eventually fire.

### 4. Stale stop orders on process restart

GTC stop orders remain active on Alpaca's servers across restarts. Reconciliation on startup handles this — if a stop fired while we were down, the position will be gone and the watch gets force-exited.

### 5. Multiple watches for the same symbol

If two portfolios (on different accounts) both buy the same symbol, they have separate Alpaca positions on separate accounts. No conflict.

### 6. Extended hours

Alpaca stop orders (GTC) execute during extended hours. Our monitor only runs during regular hours + 30 min. Stop fills during extended hours are caught by the trade stream.

### 7. Account equity drift

Over time, the actual Alpaca equity diverges from `starting_capital`. The portfolio sim tracks its own P&L. Alpaca is the financial source of truth; the portfolio sim is for analytics.

### 8. Existing positions on Go Live

When connecting to an account with existing positions, the user chooses:
- **Purge**: All positions sold (confirmed), portfolio starts fresh with cash
- **Keep**: Portfolio starts with current equity; existing positions are NOT tracked as watches (only new strategy buys get watched)

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Confirmed execution | Wait for fill before persisting | No phantom positions; actual prices/qty everywhere |
| Buy failure = no watch | Yes | Portfolio must match Alpaca reality |
| Stop uses actual fill qty | Yes | No estimation; exact qty from confirmed buy fill |
| Account sync on connect | Override starting_capital from equity | Portfolio starts from reality, not UI defaults |
| Purge option | User chooses at Go Live time | Clean start or inherit existing positions |
| Cancel stop before VDD sell | Yes | Prevent race condition with stop fill |
| Multi-account | Up to 5 (Alpaca allows 3 paper) | A/B testing of strategies on isolated accounts |
| Optional linking | Yes | Portfolios work without Alpaca; opt-in per portfolio |
| Stop orders | Server-side GTC | Survives crashes; Alpaca handles execution |
| Position sizing | Notional with whole-share fallback | Notional for fractionable; auto-converts to whole shares otherwise |
| Reconciliation | On startup per linked account | Catches drift from crashes, manual changes |
| Data source for VDD | Schwab (not Alpaca) | Full exchange volume; matches backtest exactly |
| Trade stream per account | Separate daemon threads | Each account's WebSocket is independent |

---

## PDT Considerations

The Pattern Day Trader (PDT) rule applies to accounts under $25,000.

- PDT limit: 3 day trades in a rolling 5 business days
- A "day trade" = buy and sell the same security on the same day
- Our trades are typically held for hours based on VDD signals; many will span overnight
- Guard stops could trigger same-day if the price drops quickly after entry

**Current approach**: Paper accounts start with $100,000 (above PDT threshold). If account equity drops below $25,000, PDT becomes a concern.

**Future**: Track `daytrade_count` from Alpaca account info and pause trading if approaching the limit.

---

## Bugs Fixed

### Enum string mismatch in alpaca-py (2026-03-06)

**Symptom**: Alpaca accounts showed positions, but local portfolios showed 0 watches. Orders were placed and filled on Alpaca, but the system never detected the fills.

**Root cause**: `str(OrderStatus.FILLED)` returns `"OrderStatus.FILLED"`, not `"filled"`. The `wait_for_fill` loop compared `result.status.lower() == "filled"` which never matched, causing a 15-second timeout on every order. The buy was already submitted and filled on Alpaca, but the timeout exception prevented the watch from being created.

**Fix**: `_to_result()` in `alpaca_broker.py` now uses `.value` for enum fields (`status.value` → `"filled"`). Same fix applied to `alpaca_stream.py` for trade update event/side/status parsing.

**Files**: `trader/market/alpaca_broker.py`, `trader/market/alpaca_stream.py`

### Non-fractionable stock rejection (2026-03-06)

**Symptom**: Certain stocks (e.g., MMED) were bought by virtual portfolios but missing from Alpaca-linked portfolios.

**Root cause**: All buys used `notional=` (dollar amount), which requires fractional share support. Alpaca rejects notional orders for non-fractionable assets.

**Fix**: `buy()` now checks `is_fractionable()` first. For non-fractionable stocks, it fetches the latest trade price and converts to whole shares: `qty = int(notional / price)`.

**Files**: `trader/market/alpaca_broker.py`

---

## Future Work

### Short-term
- [ ] Handle partial fills explicitly in `wait_for_fill`
- [ ] Periodic reconciliation (not just on startup)
- [ ] Track slippage: snapshot price vs actual fill price in watch data

### Validation
- [ ] Compare Alpaca P&L to portfolio simulation P&L
- [ ] Slippage analysis: how much do fill prices differ from snapshot prices?
- [ ] Guard stop effectiveness: how often does the stop fire vs VDD?

### Paper to Live transition
- [ ] Circuit breakers (daily loss limit, max trades, equity floor)
- [ ] `ALPACA_PAPER=false` with live credentials
- [ ] Start with reduced position size (1% instead of 5%)
- [ ] Validate paper P&L matches backtest expectations before switching
