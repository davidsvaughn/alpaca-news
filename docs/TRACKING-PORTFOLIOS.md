# Tracking Portfolios

> Design doc for read-only "tracking" portfolios with per-stock, multi-strategy exit alerts.

**Status**: Design — not yet implemented
**Date**: 2026-03-18

---

## Motivation

We hold real-money positions in external brokerages (e.g. a Vanguard IRA) that we want to monitor using our existing exit-strategy engine — but **without any automated trading**. The system should:

- Import holdings manually (symbol + quantity, optionally cost basis)
- Track prices and volume using our normal data sources (Schwab primary)
- Evaluate exit strategies continuously, same math as live trading
- **Notify** (email + log) when a strategy would trigger an exit, instead of selling
- Support per-stock strategy assignment and multiple simultaneous strategies

---

## Vanguard IRA — Initial Portfolio

Source: `data/vanguard/image.png` (screenshot captured 2026-03-18)

| Symbol | Name | Qty | Price | Value |
|--------|------|-----|-------|-------|
| ARKQ | ARK Autonomous Tech & Robotics ETF | 40.000 | $118.95 | $4,758.00 |
| CHAT | Roundhill Generative AI & Tech ETF | 290.000 | $66.00 | $19,140.00 |
| GOLY | Strategy Shares Gold-Hedged Bond ETF | 100.000 | $32.66 | $3,266.00 |
| NUKZ | Range Nuclear Renaissance ETF | 150.000 | $68.72 | $10,308.00 |
| QTUM | Defiance Quantum ETF | 80.000 | $110.84 | $8,867.20 |
| SMH | VanEck Semiconductor ETF | 20.075 | $393.67 | $7,902.93 |
| CRM | Salesforce Inc | 30.000 | $194.34 | $5,830.20 |
| GOOG | Alphabet Inc (Class C) | 20.014 | $306.30 | $6,130.29 |
| IBRX | ImmunityBio Inc | 1,920.000 | $8.56 | $16,435.20 |
| IOVA | Iovance Biotherapeutics | 540.000 | $3.77 | $2,035.80 |
| PLTR | Palantir Technologies | 50.000 | $152.77 | $7,638.50 |

**Total**: ~$92,312

---

## Core concept: StrategySlot

The key design idea is decoupling *strategy definitions* from *holdings* via a many-to-many relationship.

```
TrackingPortfolio (LiveConfig with mode="tracking")
  ├── StrategySlot "vdd-aggressive"    {strategy_key, params, guards}
  ├── StrategySlot "trailing-5pct"     {strategy_key, params, guards}
  └── StrategySlot "stop-loss-10pct"   {strategy_key, params, guards}

Holding (Watch with source="manual"):
  └── subscribed_slots: ["vdd-aggressive", "trailing-5pct"]
      (or "*" = subscribe to all slots)
```

### StrategySlot fields

| Field | Type | Description |
|-------|------|-------------|
| `slot_id` | string | Auto-generated (`ss_` prefix) |
| `name` | string | Human label (e.g. "VDD aggressive", "trailing 5%") |
| `strategy_key` | string | Maps to `evaluate_exit()` strategies in `backtest.py` |
| `params` | dict | Strategy-specific: `lookback_m`, `bucket_s`, etc. |
| `guards` | dict | `stop_pct`, `target_pct`, `trail_pct` |
| `notify_on_exit` | bool | Send email on trigger (default true) |
| `enabled` | bool | Can pause a slot without deleting it |

### How stocks subscribe

Each Watch (holding) carries a `subscribed_slots` field:

- **Explicit list**: `["ss_abc123", "ss_def456"]` — only those slots evaluate
- **Wildcard**: `["*"]` — all enabled slots on the portfolio evaluate
- **Empty**: `[]` — no exit evaluation (just track price)

This gives full flexibility: apply one strategy to everything, or fine-tune per stock.

---

## Tracking vs. live portfolios

| Aspect | Live Portfolio (current) | Tracking Portfolio (new) |
|--------|--------------------------|--------------------------|
| Holdings created by | News pipeline (automatic) | Manual import |
| Trading | Buys/sells via Alpaca | None — alert only |
| Exit strategy | One per portfolio | N strategy slots, per-stock opt-in |
| On exit signal | Sell the position | `notify()` email + log event |
| Strategy changes | Patch config or restart | Edit slot in-place, immediate next cycle |
| Alpaca account | Required | None (no broker linkage) |
| Position quantity | Fractional (notional orders) | Exact shares from external brokerage |

---

## Storage: Option A — Embedded in LiveConfig (recommended)

Keep everything in the existing `config_json` blob. No schema migrations needed.

### LiveConfig additions

```python
{
    "config_id": "lc_vanguard_ira",
    "name": "Vanguard IRA Tracker",
    "mode": "tracking",              # NEW — "live" (default) | "tracking"
    "auto_trade": False,             # NEW — no buys or sells ever
    "strategy_slots": [              # NEW — replaces single exit_strategy for tracking mode
        {
            "slot_id": "ss_vdd_agg",
            "name": "VDD aggressive",
            "strategy_key": "volume_delta_divergence",
            "params": {"lookback_m": 80, "bucket_s": 30},
            "guards": {"stop_pct": 0, "target_pct": 0, "trail_pct": 0},
            "notify_on_exit": True,
            "enabled": True
        },
        {
            "slot_id": "ss_trail5",
            "name": "Trailing stop 5%",
            "strategy_key": "trailing_stop",
            "params": {},
            "guards": {"stop_pct": 0, "target_pct": 0, "trail_pct": 5.0},
            "notify_on_exit": True,
            "enabled": True
        }
    ],
    "active": True,
    "filters": {},
    "allocation": "none",
    "allocation_params": {},
    "starting_capital": 92312.0,
    "exit_strategy": "",             # unused in tracking mode
    "exit_params": {},               # unused in tracking mode
    ...
}
```

### Watch additions (per holding)

```python
{
    "watch_id": "w_...",
    "symbol": "CHAT",
    "status": "holding",
    "source": "manual",              # NEW — "pipeline" (default) | "manual"
    "manual_qty": 290.0,             # NEW — actual shares in external account
    "subscribed_slots": ["*"],       # NEW — which strategy slots apply
    "entry": {
        "price": 66.00,             # price at import time (or cost basis if known)
        "time": "2026-03-18T...",
        ...
    },
    "live_config_id": "lc_vanguard_ira",
    ...
}
```

### Storage: Option B — Separate tables (future)

If slot CRUD becomes frequent or we need cross-portfolio slot sharing:

- `strategy_slots` table (slot_id, config_id, name, strategy_key, params_json, enabled)
- `watch_slot_subscriptions` join table (watch_id, slot_id)

Migrate to this only if Option A gets unwieldy.

---

## Monitor loop changes

In `LiveExitMonitor._check_holding()`, for tracking-mode configs:

```python
def _check_holding_tracking(self, watch_dict, config_dict):
    """Evaluate all subscribed strategy slots — notify instead of sell."""
    symbol = watch_dict["symbol"]
    slots = config_dict.get("strategy_slots", [])
    subscribed = set(watch_dict.get("subscribed_slots", ["*"]))

    for slot in slots:
        if not slot.get("enabled", True):
            continue
        if "*" not in subscribed and slot["slot_id"] not in subscribed:
            continue

        result = evaluate_exit(
            strategy_key=slot["strategy_key"],
            params=slot.get("params", {}),
            bars=bars,
            entry_idx=entry_idx,
            entry_price=entry_price,
            guard_stop_pct=slot.get("guards", {}).get("stop_pct", 0),
            guard_target_pct=slot.get("guards", {}).get("target_pct", 0),
            guard_trail_pct=slot.get("guards", {}).get("trail_pct", 0),
        )

        if result.should_exit:
            notify(
                subject=f"EXIT SIGNAL: {symbol} ({slot['name']})",
                body=(
                    f"Strategy '{slot['name']}' triggered exit for {symbol}\n"
                    f"Reason: {result.reason}\n"
                    f"Price: ${result.exit_price:.2f}\n"
                    f"Bars held: {result.bars_held}\n"
                    f"Qty: {watch_dict.get('manual_qty', '?')} shares\n"
                    f"Portfolio: {config_dict.get('name')}"
                ),
            )
            log_exit_alert(db, watch_id, slot["slot_id"], result)
```

Key differences from live mode:
- **No sell order** — notification only
- **No status change** — watch stays `"holding"` (the stock is still held externally)
- **Multiple triggers** — if two slots fire on the same bar, two notifications sent
- **Re-triggering** — need a cooldown mechanism so the same slot doesn't spam alerts every cycle after first trigger (e.g. `last_triggered_at` per slot per watch)

---

## Alert cooldown / re-trigger logic

Once a slot triggers for a stock, we don't want it firing every 60 seconds forever. Options:

1. **One-shot**: slot fires once per watch, then auto-unsubscribes (record in watch state). User can re-arm manually.
2. **Cooldown window**: after trigger, suppress re-alerts for N hours (configurable per slot, e.g. `cooldown_hours: 24`).
3. **Sticky acknowledge**: alert stays "pending" until user acknowledges via dashboard/API, then can re-arm.

Recommendation: start with **one-shot + manual re-arm** (simplest), add cooldown later.

Track triggered state per watch per slot:
```python
{
    "slot_alerts": {
        "ss_vdd_agg": {
            "triggered_at": "2026-03-19T14:32:00+00:00",
            "exit_price": 64.50,
            "reason": "vdd_bearish",
            "acknowledged": False
        }
    }
}
```

---

## Implementation phases

### Phase 1 — Core (MVP)

- [ ] Add `mode`, `auto_trade`, `strategy_slots` fields to `LiveConfig`
- [ ] Add `source`, `manual_qty`, `subscribed_slots`, `slot_alerts` fields to Watch
- [ ] Create script to import Vanguard holdings → LiveConfig + Watches
- [ ] Add tracking-mode branch in `LiveExitMonitor._check_holding()`
- [ ] Notify on exit signal (email + `logs/notifications.md`)
- [ ] One-shot alert with triggered state tracking
- [ ] API endpoints: create tracking portfolio, add/edit/remove slots, re-arm alerts

### Phase 2 — Dashboard

- [ ] Dashboard page for tracking portfolios (separate from live portfolios)
- [ ] View holdings with current prices, P&L since import
- [ ] View strategy slots, toggle enabled/disabled
- [ ] Per-stock slot subscription editor
- [ ] Alert history with acknowledge button

### Phase 3 — Advanced

- [ ] Slot templates (save a slot config and apply across portfolios)
- [ ] Strategy performance comparison (which slot would have been right?)
- [ ] Cooldown windows as alternative to one-shot
- [ ] Import from CSV / other brokerages
- [ ] Cost basis tracking (for P&L from actual purchase price)
- [ ] Eventually: opt-in auto-trade on specific slots (bridge back to live mode)

---

## Open questions

1. **Entry prices**: Screenshot doesn't show cost basis. Use today's prices as "entry" (track from now), or import cost basis separately?

2. **Default strategy slots**: Start with VDD + trailing stop + hard stop? Or specific strategies you want to test?

3. **Naming**: "tracking" portfolio feels right — distinct from "live" and "backtest". Other suggestions?

---

## Cross-references

- [LIVE-TRADING.md](LIVE-TRADING.md) — Live trading implementation (the system this extends)
- [BACKTEST-STRATEGIES.md](BACKTEST-STRATEGIES.md) — Available exit strategies and parameters
- [ALPACA-TRADING.md](ALPACA-TRADING.md) — Alpaca order execution (not used by tracking portfolios)
- [trader/market/backtest.py](../trader/market/backtest.py) — `evaluate_exit()` and `ExitResult`
- [trader/online/live_monitor.py](../trader/online/live_monitor.py) — `LiveExitMonitor` (will be extended)
- [trader/models/live_config.py](../trader/models/live_config.py) — `LiveConfig` model (will get new fields)
