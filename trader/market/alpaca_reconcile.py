"""Alpaca ↔ Watch reconciliation.

On startup (and optionally periodically), ensure the internal watch state
matches the actual Alpaca account positions. Alpaca is always the source
of truth.

Rules:
  1. Alpaca has a position, we have a matching "holding" watch → OK
  2. Alpaca has a position, we have NO matching watch → force-close on Alpaca
     (or create an "orphan" watch — configurable, default: close)
  3. We have a "holding" watch, Alpaca has NO position → force-exit the watch
  4. Entry price mismatch → update watch to match Alpaca's avg_entry_price
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def reconcile(
    *,
    broker: Any,  # AlpacaBroker
    db: Any,      # Database
    close_orphans: bool = True,
) -> dict[str, Any]:
    """Reconcile Alpaca positions with watch state.

    Returns a summary dict with actions taken.
    """
    from trader.db.database import get_active_watches, update_watch
    from trader.models.watch import WatchBuilder

    summary: dict[str, Any] = {
        "ok": [],
        "watch_force_exited": [],
        "alpaca_orphan_closed": [],
        "entry_price_updated": [],
    }

    # Get Alpaca positions
    alpaca_positions = {p.symbol: p for p in broker.get_positions()}
    alpaca_symbols = set(alpaca_positions.keys())

    # Get our holding watches
    all_watches = get_active_watches(db)
    holding_watches = [w for w in all_watches if w.get("status") == "holding"]

    # Build symbol → watch mapping (only watches with Alpaca orders)
    watch_by_symbol: dict[str, dict] = {}
    for w in holding_watches:
        if w.get("alpaca_buy_order_id"):
            watch_by_symbol[w["symbol"]] = w

    watch_symbols = set(watch_by_symbol.keys())

    # Rule 1 & 4: Both sides have the position
    for symbol in alpaca_symbols & watch_symbols:
        pos = alpaca_positions[symbol]
        watch = watch_by_symbol[symbol]
        entry_price = watch["entry"]["price"]

        # Check entry price match
        if abs(pos.avg_entry_price - entry_price) > 0.01:
            builder = WatchBuilder.from_dict(watch)
            from trader.models.watch import WatchEntry
            old_entry = builder.entry
            builder.entry = WatchEntry(
                snapshot_id=old_entry.snapshot_id,
                price=pos.avg_entry_price,
                time=old_entry.time,
                confidence=old_entry.confidence,
                direction=old_entry.direction,
                horizon=old_entry.horizon,
                thesis=old_entry.thesis,
            )
            updated = builder.to_watch()
            update_watch(db, watch["watch_id"], updated.to_dict())
            summary["entry_price_updated"].append({
                "symbol": symbol,
                "old_price": entry_price,
                "new_price": pos.avg_entry_price,
            })
            log.info("RECONCILE: %s entry price updated %.2f → %.2f",
                     symbol, entry_price, pos.avg_entry_price)
        else:
            summary["ok"].append(symbol)

    # Rule 3: We have a watch but Alpaca has no position → force-exit
    for symbol in watch_symbols - alpaca_symbols:
        watch = watch_by_symbol[symbol]
        builder = WatchBuilder.from_dict(watch)
        # Use last known price as exit price
        exit_price = watch["entry"]["price"]  # fallback
        builder.record_exit(price=exit_price, reason="reconcile_no_alpaca_position")
        updated = builder.to_watch()
        update_watch(db, watch["watch_id"], updated.to_dict())
        summary["watch_force_exited"].append({
            "symbol": symbol,
            "watch_id": watch["watch_id"],
        })
        log.warning("RECONCILE: %s watch %s force-exited (no Alpaca position)",
                    symbol, watch["watch_id"])

    # Rule 2: Alpaca has a position but we have no watch → close on Alpaca
    for symbol in alpaca_symbols - watch_symbols:
        if close_orphans:
            try:
                broker.close_position(symbol)
                summary["alpaca_orphan_closed"].append(symbol)
                log.warning("RECONCILE: %s closed orphan Alpaca position", symbol)
            except Exception:
                log.exception("RECONCILE: failed to close orphan %s on Alpaca", symbol)
        else:
            log.warning("RECONCILE: %s orphan Alpaca position (not closing)", symbol)

    total_actions = (
        len(summary["watch_force_exited"])
        + len(summary["alpaca_orphan_closed"])
        + len(summary["entry_price_updated"])
    )
    if total_actions > 0:
        log.info("RECONCILE complete: %d OK, %d force-exited, %d orphans closed, %d prices updated",
                 len(summary["ok"]), len(summary["watch_force_exited"]),
                 len(summary["alpaca_orphan_closed"]), len(summary["entry_price_updated"]))
    else:
        log.info("RECONCILE complete: all %d positions in sync", len(summary["ok"]))

    return summary
