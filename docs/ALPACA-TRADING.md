# Alpaca Paper Trading Integration

> Technical reference for the Alpaca order execution layer.
> Covers architecture, multi-account setup, order lifecycle, reconciliation,
> edge cases, and future considerations.
>
> Created: 2026-03-06

---

## Table of Contents

1. [Overview](#overview)
2. [Multi-Account Architecture](#multi-account-architecture)
3. [Environment Configuration](#environment-configuration)
4. [Key Files](#key-files)
5. [Order Lifecycle](#order-lifecycle)
6. [Two Exit Paths](#two-exit-paths)
7. [Trade Update Stream](#trade-update-stream)
8. [Reconciliation](#reconciliation)
9. [Portfolio ↔ Account Linking](#portfolio--account-linking)
10. [Position Sizing](#position-sizing)
11. [Data Source Separation](#data-source-separation)
12. [Edge Cases & Complications](#edge-cases--complications)
13. [Design Decisions](#design-decisions)
14. [PDT Considerations](#pdt-considerations)
15. [Future Work](#future-work)

---

## Overview

Alpaca provides paper trading accounts that simulate real brokerage execution. Our system optionally connects a LiveConfig portfolio to an Alpaca paper account. When connected:

- **Buy signals** submit real market orders to Alpaca
- **Guard stops** become server-side Alpaca stop orders (survive process crashes)
- **VDD exits** close the Alpaca position and cancel the stop order
- **Alpaca-triggered stops** are detected via WebSocket and update the watch

When NOT connected, portfolios run in virtual-only mode (same as before Phase 4). The `alpaca_account_id` field on LiveConfig is `None` and no orders are placed.

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
| `trader/market/alpaca_broker.py` | `AlpacaAccount`, `AlpacaAccountRegistry`, `AlpacaBrokerPool`, `AlpacaBroker` |
| `trader/market/alpaca_stream.py` | `AlpacaTradeStream` — WebSocket listener for fill/cancel events |
| `trader/market/alpaca_reconcile.py` | `reconcile()` — syncs Alpaca positions with watch state |
| `trader/models/live_config.py` | `LiveConfig.alpaca_account_id` field |
| `trader/models/watch.py` | `Watch.alpaca_buy_order_id`, `Watch.alpaca_stop_order_id` |
| `trader/online/live_monitor.py` | `LivePortfolioManager` (buy path), `LiveExitMonitor` (exit path) |
| `trader/online/orchestrator.py` | Startup: pool init, reconciliation, stream launch |
| `trader/web/app.py` | `/api/alpaca/status`, config creation constraint |
| `trader/web/templates/snapshots.html` | "Go Live" account selection UI |
| `trader/web/templates/partials/_positions_table.html` | Alpaca badge on linked portfolios |

---

## Order Lifecycle

### Entry (Buy)

```
Snapshot sealed
    → LivePortfolioManager._evaluate_for_config()
    → Passes filters + allocation check
    → Creates WatchBuilder
    → If cfg.alpaca_account_id is set:
        1. broker.buy(symbol, notional=position_size)
           → Alpaca MarketOrderRequest (DAY)
           → Records alpaca_buy_order_id on watch
        2. broker.set_stop(symbol, qty=est_qty, stop_price=...)
           → Alpaca StopOrderRequest (GTC, server-side)
           → Records alpaca_stop_order_id on watch
    → Persists watch to DB + JSON
```

**Position sizing**: `notional = starting_capital * alloc_pct / 100`. Uses dollar-based ordering (fractional shares supported).

**Stop qty estimation**: At order time, we don't know the fill qty yet (market order hasn't filled). We estimate: `qty = notional / entry_price`. When the buy fill arrives via the trade stream, the entry price is updated to the actual fill price.

### Exit (VDD signal)

```
LiveExitMonitor._check_holding()
    → evaluate_exit() returns should_exit=True
    → broker.close_position(symbol)
       → Alpaca close_position (market sell)
    → broker.cancel_order(stop_order_id)
       → Cancels the GTC stop (no longer needed)
    → Records exit on watch with fill price
```

### Exit (Alpaca-triggered stop)

```
AlpacaTradeStream._handle_sell_fill()
    → Matches order_id to watch.alpaca_stop_order_id
    → Records exit with fill price and reason "alpaca_stop_fill"
    → Watch transitions: holding → exited → cooling_off → sealed
```

---

## Two Exit Paths

A position can be closed by either of two independent mechanisms:

| Trigger | Who initiates | What happens |
|---------|--------------|--------------|
| **VDD divergence** | Our LiveExitMonitor (every ~60s) | `close_position()` + `cancel_order(stop_id)` |
| **Guard stop hit** | Alpaca server-side | Stop order fills, trade stream notifies us |

**Race condition**: Both could fire near-simultaneously. This is handled gracefully:
- If VDD fires first: `close_position()` sells the shares, then `cancel_order()` cancels the stop. If the stop was already triggered, the cancel is a no-op.
- If stop fires first: The trade stream marks the watch as exited. When VDD next checks, the watch is no longer `"holding"`, so it's skipped. The `close_position()` would return `None` (no position to close).

**No double-sell risk**: Alpaca prevents selling more shares than you hold. If both fire at the same instant, one will succeed and the other will get a "position does not exist" error, which we handle gracefully.

---

## Trade Update Stream

Each linked Alpaca account gets its own `AlpacaTradeStream` running in a daemon thread. The stream receives events via WebSocket:

| Event | Action |
|-------|--------|
| `fill` (buy side) | Update watch entry price with actual fill price |
| `fill` (sell side) | If matched to `alpaca_stop_order_id`: record exit on watch |
| `canceled` | Log warning |
| `rejected` | Log warning |
| `expired` | Log warning |

All events are published to the event bus as `alpaca_trade_update` for UI refresh.

**Reconnection**: If the WebSocket disconnects, the stream auto-reconnects after 5 seconds (infinite retry loop).

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
- Process crash during a buy (order submitted, watch not persisted)
- Process crash during an exit (position closed, watch not updated)
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
- **On activate**: No additional check needed — the create-time check is sufficient because we activate immediately after creation.
- **UI**: The "Go Live" dialog fetches `/api/alpaca/status`, which returns each account's `linked` flag. Already-linked accounts are excluded from the selection list.

---

## Position Sizing

When a portfolio is linked to Alpaca, the position size for each buy is:

```
notional = starting_capital * alloc_pct / 100
```

Where:
- `starting_capital` comes from the LiveConfig (e.g., $100,000)
- `alloc_pct` comes from `allocation_params.alloc_pct` (e.g., 5 = 5%)

**Important**: This uses the *configured* starting capital, not the actual Alpaca account equity. This means:
- Winning trades don't compound (position size stays the same)
- Losing trades don't shrink position size
- The Alpaca account equity may diverge from the configured capital over time

**Future consideration**: Use Alpaca's actual `buying_power` or `equity` for position sizing. This would enable compounding but adds complexity (need to handle margin, concurrent position sizing, etc.).

---

## Data Source Separation

| Purpose | Source | Why |
|---------|--------|-----|
| **VDD exit signals** | Schwab 1-min bars | Matches backtest exactly (full exchange volume) |
| **Guard stop execution** | Alpaca server-side | Survives process crashes |
| **Order execution** | Alpaca API | Paper trading |
| **Entry/exit prices** | Alpaca fill prices | Actual execution prices (slippage tracking) |
| **Market data for filters** | Schwab/MarketDataService | Same as backtest |

Alpaca's free data is IEX-only (~2-5% of exchange volume), which is insufficient for VDD calculations. We only use Alpaca for orders, not data.

---

## Edge Cases & Complications

### 1. Stop order qty mismatch

When we submit a buy as a notional (dollar) amount, we don't know the exact share qty until the fill. We estimate `qty = notional / entry_price` for the stop order. If the fill qty differs (due to price movement between order and fill), the stop order qty may be slightly wrong.

**Impact**: Minor. The stop might try to sell slightly more or fewer shares than we actually hold. Alpaca will adjust to the actual position size.

**Future fix**: Wait for the buy fill (via trade stream), then submit the stop with the actual filled qty.

### 2. Partial fills

Market orders are almost always fully filled immediately on paper trading. But in theory, a partial fill could occur. Currently we treat partial fills the same as full fills.

### 3. Stale stop orders on process restart

If the process crashes, GTC stop orders remain active on Alpaca's servers. On restart, reconciliation ensures our watches match. If a stop fired while we were down, reconciliation will force-exit the watch.

### 4. Multiple watches for the same symbol

If two portfolios (on different accounts) both buy the same symbol, they have separate Alpaca positions on separate accounts. No conflict.

If the same portfolio somehow creates two watches for the same symbol (shouldn't happen, but defensively): the stop order matching uses `alpaca_stop_order_id`, which is unique per watch, so fills are matched correctly.

### 5. Extended hours

Alpaca supports extended hours trading. Our system currently only monitors during regular hours + 30 min after close. Stop orders are GTC and will execute during extended hours if the price is hit, even if our monitor isn't running.

### 6. Day trade restrictions (PDT)

See [PDT Considerations](#pdt-considerations).

### 7. Account equity drift

Over time, wins/losses cause the Alpaca account equity to diverge from the portfolio's `starting_capital`. The portfolio simulation tracks its own P&L independently. The Alpaca account is the financial source of truth; the portfolio sim is for analytics.

### 8. Order rejection

If Alpaca rejects a buy order (insufficient buying power, symbol not tradeable, etc.), the watch is still created but without Alpaca order IDs. It becomes a virtual-only position. The error is logged.

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Multi-account vs single | Multi (up to 5) | Alpaca allows 3 paper accounts; enables A/B testing of strategies |
| Broker pool vs singleton | Pool (one per account) | Each account has its own credentials and TradingClient |
| Optional linking | Yes | Portfolios work without Alpaca; Alpaca is opt-in per portfolio |
| Stop orders | Server-side GTC | Survives crashes; Alpaca handles execution |
| Position sizing | Notional (dollar-based) | Simpler than qty; supports fractional shares |
| Entry price | Updated from fill | Slippage tracking; actual execution price is what matters |
| Reconciliation | On startup only | Minimizes API calls; most drift is caught by trade stream |
| Data source for VDD | Schwab (not Alpaca) | Full exchange volume; matches backtest exactly |
| One account per portfolio | Enforced at API level | Prevents confusion; clean position tracking |
| Trade stream per account | Separate threads | Each account's WebSocket is independent |

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

## Future Work

### Phase 4 completion
- [ ] Wait for buy fill before submitting stop order (exact qty)
- [ ] Handle partial fills explicitly
- [ ] Periodic reconciliation (not just on startup)
- [ ] Track slippage: expected vs actual fill prices in watch data

### Phase 5: Validation
- [ ] Compare Alpaca P&L to portfolio simulation P&L
- [ ] Slippage analysis: how much do fill prices differ from snapshot prices?
- [ ] Guard stop effectiveness: how often does the stop fire vs VDD?

### Phase 6: Paper → Live
- [ ] Circuit breakers (daily loss limit, max trades, equity floor)
- [ ] `ALPACA_PAPER=false` with live credentials
- [ ] Start with reduced position size (1% instead of 5%)
- [ ] Validate paper P&L matches backtest expectations before switching
