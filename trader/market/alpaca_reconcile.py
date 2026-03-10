"""Alpaca ↔ Watch reconciliation.

On startup (and optionally periodically), ensure the internal watch state
matches the actual Alpaca account positions. Alpaca is always the source
of truth — the portfolio adjusts to Alpaca, never the reverse.

Rules:
  1. Alpaca has a position, we have a matching "holding" watch → sync
     entry price AND qty from Alpaca (both must match)
  2. Alpaca has a position, we have NO matching watch → adopt (create watch
     from config) or force-close on Alpaca (if close_orphans=True)
  3. We have a "holding" watch, Alpaca has NO position → VERIFY before
     force-exiting (double-check with per-symbol lookup + check sell history)

SAFETY: Rule 3 never force-exits on a single bulk lookup miss. It requires:
  - Per-symbol get_position() also returns None (confirms not a transient miss)
  - If a recent sell fill is found, uses actual sell price (not entry price)
  - If no sell found and position not found, logs ERROR and skips (phantom miss)
  - All anomalies are printed to console + logged for user visibility
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _log_tx(db: Any, account_id: str, event: str, symbol: str, **kwargs: Any) -> None:
    """Log a reconciliation transaction."""
    if not db:
        return
    try:
        from trader.db.database import log_alpaca_transaction
        log_alpaca_transaction(db, account_id=account_id, event=event, symbol=symbol, **kwargs)
    except Exception:
        log.warning("Failed to log reconcile transaction: %s %s", event, symbol)


def _lookup_snapshot_prediction(db: Any, symbol: str) -> dict[str, Any]:
    """Find the most recent snapshot prediction for a symbol.

    Returns dict with 'confidence' and 'direction', or defaults if not found.
    Per-symbol snapshots have IDs like 'abc123_SYMBOL'.
    """
    import json
    from sqlalchemy import text
    try:
        with db.engine.connect() as conn:
            row = conn.execute(
                text("SELECT snapshot_json FROM snapshots "
                     "WHERE snapshot_id LIKE :pattern "
                     "ORDER BY created_at DESC LIMIT 1"),
                {"pattern": f"%_{symbol}"},
            ).fetchone()
        if row:
            snap = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            pred = snap.get("prediction") or {}
            if pred.get("confidence") is not None:
                return {
                    "confidence": pred["confidence"],
                    "direction": (pred.get("direction") or "bullish").lower(),
                }
    except Exception as e:
        log.warning("RECONCILE: failed to look up snapshot for %s: %s", symbol, e)
    return {"confidence": 0.5, "direction": "bullish"}


def reconcile(
    *,
    broker: Any,  # AlpacaBroker
    db: Any,      # Database
    live_config_id: str | None = None,  # filter watches to this config only
    live_config: Any = None,  # LiveConfig — needed for adopting orphan positions
    close_orphans: bool = False,
) -> dict[str, Any]:
    """Reconcile Alpaca positions with watch state.

    Returns a summary dict with actions taken.
    """
    from trader.db.database import get_active_watches, insert_watch, update_watch
    from trader.models.watch import WatchBuilder

    account_id = getattr(broker, "account_id", None) or "unknown"

    summary: dict[str, Any] = {
        "ok": [],
        "watch_force_exited": [],
        "watch_phantom_miss": [],   # bulk miss but position still exists (transient)
        "alpaca_orphan_closed": [],
        "alpaca_orphan_adopted": [],
        "entry_price_updated": [],
        "qty_updated": [],
    }

    # Get Alpaca positions
    alpaca_positions = {p.symbol: p for p in broker.get_positions()}
    alpaca_symbols = set(alpaca_positions.keys())

    # Get our holding watches
    all_watches = get_active_watches(db)
    holding_watches = [w for w in all_watches if w.get("status") == "holding"]

    # Filter to this config's watches only (prevents cross-account confusion)
    if live_config_id:
        holding_watches = [w for w in holding_watches
                           if w.get("live_config_id") == live_config_id]

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
        needs_update = False
        builder = None

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
            needs_update = True
            summary["entry_price_updated"].append({
                "symbol": symbol,
                "old_price": entry_price,
                "new_price": pos.avg_entry_price,
            })
            log.info("RECONCILE: %s entry price updated %.2f → %.2f",
                     symbol, entry_price, pos.avg_entry_price)
            _log_tx(db, account_id, "reconcile_price_updated", symbol,
                    detail={"old_price": entry_price, "new_price": pos.avg_entry_price,
                            "watch_id": watch["watch_id"]})

        # Check qty match (Alpaca is source of truth)
        watch_qty = watch.get("qty")
        alpaca_qty = float(pos.qty)
        if watch_qty is not None and abs(alpaca_qty - watch_qty) > 0.001:
            if builder is None:
                builder = WatchBuilder.from_dict(watch)
            builder.qty = alpaca_qty
            needs_update = True
            summary["qty_updated"].append({
                "symbol": symbol,
                "old_qty": watch_qty,
                "new_qty": alpaca_qty,
            })
            log.info("RECONCILE: %s qty updated %.4f → %.4f",
                     symbol, watch_qty, alpaca_qty)
            _log_tx(db, account_id, "reconcile_qty_updated", symbol,
                    detail={"old_qty": watch_qty, "new_qty": alpaca_qty,
                            "watch_id": watch["watch_id"]})

        if needs_update:
            updated = builder.to_watch()
            update_watch(db, watch["watch_id"], updated.to_dict())
        else:
            summary["ok"].append(symbol)
            _log_tx(db, account_id, "reconcile_ok", symbol,
                    detail={"entry_price": entry_price, "watch_id": watch["watch_id"]})

    # Rule 3: We have a watch but Alpaca has no position → VERIFY before force-exiting
    for symbol in watch_symbols - alpaca_symbols:
        watch = watch_by_symbol[symbol]

        # Step 1: Double-check with per-symbol lookup (bulk list may be stale)
        per_symbol_pos = broker.get_position(symbol)
        if per_symbol_pos is not None:
            # Position exists! Bulk lookup was wrong (transient API issue).
            summary["watch_phantom_miss"].append(symbol)
            print(f"RECONCILE WARNING: {symbol} missing from bulk positions but per-symbol "
                  f"lookup found it (qty={per_symbol_pos.qty:.4f}) — NOT force-exiting")
            log.warning("RECONCILE: %s phantom miss — bulk positions missed it but "
                        "per-symbol lookup found position (qty=%.4f). No action taken.",
                        symbol, per_symbol_pos.qty)
            _log_tx(db, account_id, "reconcile_phantom_miss", symbol,
                    detail={"watch_id": watch["watch_id"], "qty": per_symbol_pos.qty})
            continue

        # Step 2: Position is truly gone. Check sell history for actual exit price.
        exit_price = watch["entry"]["price"]  # fallback
        exit_reason = "reconcile_no_alpaca_position"
        try:
            recent_sells = broker.get_recent_sells(symbol, limit=5)
            for sell in recent_sells:
                status = sell.status.lower() if isinstance(sell.status, str) else sell.status
                if status == "filled" and sell.filled_avg_price:
                    exit_price = sell.filled_avg_price
                    exit_reason = f"reconcile_confirmed_sell (order={sell.order_id})"
                    log.info("RECONCILE: %s found sell fill at $%.2f (order %s)",
                             symbol, exit_price, sell.order_id)
                    break
            else:
                # No sell fill found — position vanished without a sell order.
                # This should NOT happen. Log loudly and skip to be safe.
                print(f"RECONCILE ERROR: {symbol} has NO Alpaca position AND no recent sell "
                      f"order found! This is unexpected. Watch NOT force-exited. "
                      f"Manual investigation required!")
                log.error("RECONCILE: %s — no position AND no sell fill found. "
                          "Possible API issue or manual intervention. NOT force-exiting. "
                          "Watch: %s", symbol, watch["watch_id"])
                summary["watch_phantom_miss"].append(symbol)
                _log_tx(db, account_id, "reconcile_no_sell_found", symbol,
                        detail={"watch_id": watch["watch_id"]})
                continue
        except Exception:
            log.exception("RECONCILE: %s — failed to query sell history. NOT force-exiting.", symbol)
            summary["watch_phantom_miss"].append(symbol)
            _log_tx(db, account_id, "reconcile_sell_query_failed", symbol,
                    detail={"watch_id": watch["watch_id"]})
            continue

        # Step 3: Confirmed — position is gone AND we found the sell. Force-exit with real price.
        builder = WatchBuilder.from_dict(watch)
        builder.record_exit(price=exit_price, reason=exit_reason)
        updated = builder.to_watch()
        update_watch(db, watch["watch_id"], updated.to_dict())
        summary["watch_force_exited"].append({
            "symbol": symbol,
            "watch_id": watch["watch_id"],
            "exit_price": exit_price,
        })
        print(f"RECONCILE: {symbol} force-exited at ${exit_price:.2f} (confirmed sell on Alpaca)")
        log.warning("RECONCILE: %s watch %s force-exited at $%.2f (confirmed sell)",
                    symbol, watch["watch_id"], exit_price)
        _log_tx(db, account_id, "reconcile_force_exit", symbol,
                detail={"watch_id": watch["watch_id"], "exit_price": exit_price})

    # Rule 2: Alpaca has a position but we have no watch
    # Default: adopt (create watch from config). Fallback: close on Alpaca.
    for symbol in alpaca_symbols - watch_symbols:
        pos = alpaca_positions[symbol]

        if live_config and live_config_id:
            # Adopt: create a watch for this orphan position
            try:
                # Look up real confidence/direction from the most recent snapshot
                snap_pred = _lookup_snapshot_prediction(db, symbol)
                builder = WatchBuilder.create_from_live_config(
                    snapshot_id="reconcile_adopted",
                    symbol=symbol,
                    entry_price=pos.avg_entry_price,
                    confidence=snap_pred["confidence"],
                    direction=snap_pred["direction"],
                    live_config_id=live_config_id,
                    exit_strategy=live_config.exit_strategy,
                    exit_params=live_config.exit_params,
                )
                builder.qty = pos.qty
                builder.alpaca_buy_order_id = "adopted"
                watch = builder.to_watch()
                insert_watch(db, watch=watch.to_dict())
                summary["alpaca_orphan_adopted"].append(symbol)
                log.warning("RECONCILE: %s adopted orphan position (qty=%.4f, entry=%.2f)",
                            symbol, pos.qty, pos.avg_entry_price)
                _log_tx(db, account_id, "reconcile_orphan_adopted", symbol,
                        detail={"qty": pos.qty, "avg_entry_price": pos.avg_entry_price,
                                "watch_id": watch.watch_id,
                                "market_value": pos.market_value})
            except Exception as e:
                log.exception("RECONCILE: failed to adopt orphan %s", symbol)
                _log_tx(db, account_id, "reconcile_orphan_adopt_failed", symbol,
                        detail={"error": str(e), "qty": pos.qty})
        elif close_orphans:
            # Emergency fallback: close orphan on Alpaca
            try:
                all_open_orders = broker.get_open_orders()
                for o in all_open_orders:
                    if o.symbol == symbol:
                        broker.cancel_order(o.order_id)
                        log.info("RECONCILE: cancelled order %s for orphan %s", o.order_id, symbol)
                broker.close_position(symbol)
                summary["alpaca_orphan_closed"].append(symbol)
                log.warning("RECONCILE: %s closed orphan Alpaca position", symbol)
                _log_tx(db, account_id, "reconcile_orphan_closed", symbol,
                        detail={"qty": pos.qty, "avg_entry_price": pos.avg_entry_price,
                                "market_value": pos.market_value})
            except Exception as e:
                log.exception("RECONCILE: failed to close orphan %s on Alpaca", symbol)
                _log_tx(db, account_id, "reconcile_orphan_close_failed", symbol,
                        detail={"error": str(e), "qty": pos.qty})
        else:
            log.warning("RECONCILE: %s orphan Alpaca position (no config to adopt, not closing)",
                        symbol)
            _log_tx(db, account_id, "reconcile_orphan_skipped", symbol,
                    detail={"qty": pos.qty, "avg_entry_price": pos.avg_entry_price})

    total_actions = (
        len(summary["watch_force_exited"])
        + len(summary["alpaca_orphan_closed"])
        + len(summary["alpaca_orphan_adopted"])
        + len(summary["entry_price_updated"])
        + len(summary["qty_updated"])
    )
    n_phantom = len(summary["watch_phantom_miss"])
    if total_actions > 0 or n_phantom > 0:
        msg = (f"RECONCILE complete: {len(summary['ok'])} OK, "
               f"{len(summary['watch_force_exited'])} force-exited, "
               f"{len(summary['alpaca_orphan_closed'])} orphans closed, "
               f"{len(summary['alpaca_orphan_adopted'])} adopted, "
               f"{len(summary['entry_price_updated'])} prices updated, "
               f"{len(summary['qty_updated'])} qty updated")
        if n_phantom:
            msg += f", {n_phantom} PHANTOM MISSES (investigate!)"
        log.info(msg)
        print(msg)
    else:
        log.info("RECONCILE complete: all %d positions in sync", len(summary["ok"]))

    return summary


def ensure_stops(
    *,
    broker: Any,  # AlpacaBroker
    db: Any,      # Database
    live_config_id: str | None = None,  # filter watches to this config only
    guard_stop_pct: float = 0.0,  # fallback if watch has no persisted stop_price
) -> dict[str, Any]:
    """Ensure every holding watch with an Alpaca position has an active stop order.

    Called on startup (after reconcile) and daily at market open.
    For each holding watch:
      - Uses watch.alpaca_stop_price (persisted at buy time) as the stop price
      - Falls back to entry_price * (1 - guard_stop_pct/100) for legacy watches
      - Checks Alpaca open orders as definitive source (not just watch metadata)
      - If stop exists on Alpaca → skip (and update watch metadata if stale)
      - If missing → submit new stop (DAY for fractional, GTC for whole)

    Returns summary of actions taken.
    """
    from trader.db.database import get_active_watches, update_watch
    from trader.models.watch import WatchBuilder

    account_id = getattr(broker, "account_id", None) or "unknown"
    summary: dict[str, Any] = {"submitted": [], "already_open": [], "no_stop_price": [], "errors": []}

    alpaca_positions = {p.symbol: p for p in broker.get_positions()}
    all_watches = get_active_watches(db)
    holding_watches = [w for w in all_watches
                       if w.get("status") == "holding" and w.get("alpaca_buy_order_id")]

    # Filter to this config's watches only (prevents cross-account confusion)
    if live_config_id:
        holding_watches = [w for w in holding_watches
                           if w.get("live_config_id") == live_config_id]

    # Get ALL open orders once (cheaper than per-symbol lookups)
    all_open_orders = broker.get_open_orders()
    # Build symbol → open stop order mapping
    open_stops_by_symbol: dict[str, Any] = {}
    for o in all_open_orders:
        if o.side == "sell" and o.stop_price is not None:
            open_stops_by_symbol[o.symbol] = o

    for watch in holding_watches:
        symbol = watch["symbol"]
        if symbol not in alpaca_positions:
            continue  # no Alpaca position — reconcile() handles this

        pos = alpaca_positions[symbol]
        qty = pos.qty
        watch_id = watch["watch_id"]

        # Determine stop price: persisted on watch, or calculate from config
        stop_price = watch.get("alpaca_stop_price")
        if stop_price is None and guard_stop_pct > 0:
            entry_price = watch["entry"]["price"]
            stop_price = round(entry_price * (1 - guard_stop_pct / 100), 2)
        if stop_price is None:
            summary["no_stop_price"].append(symbol)
            log.warning("ENSURE-STOPS: %s has no stop_price and guard_stop_pct=0 — UNPROTECTED!", symbol)
            continue

        # Check Alpaca directly for an open stop on this symbol
        existing_stop = open_stops_by_symbol.get(symbol)
        if existing_stop:
            # Update watch metadata if stale (e.g. stop was set by fix script
            # but watch metadata was overwritten by concurrent reconcile)
            if watch.get("alpaca_stop_order_id") != existing_stop.order_id:
                builder = WatchBuilder.from_dict(watch)
                builder.alpaca_stop_order_id = existing_stop.order_id
                builder.alpaca_stop_price = stop_price
                updated = builder.to_watch()
                update_watch(db, watch_id, updated.to_dict())
            summary["already_open"].append(symbol)
            continue

        # Submit stop order
        try:
            result = broker.set_stop(symbol, qty=qty, stop_price=stop_price)
        except Exception as e:
            # Retry with available qty if other orders are holding shares
            available = _parse_available_qty(str(e))
            if available is not None and available > 0:
                log.warning("ENSURE-STOPS: %s insufficient qty (%.4f requested, %.4f available) — retrying with available",
                            symbol, qty, available)
                try:
                    result = broker.set_stop(symbol, qty=available, stop_price=stop_price)
                    qty = available  # update for logging below
                except Exception as e2:
                    summary["errors"].append({"symbol": symbol, "error": str(e2)})
                    log.exception("ENSURE-STOPS FAILED: %s — POSITION UNPROTECTED!", symbol)
                    _log_tx(db, account_id, "stop_ensure_failed", symbol,
                            detail={"error": str(e2), "watch_id": watch_id})
                    continue
            else:
                summary["errors"].append({"symbol": symbol, "error": str(e)})
                log.exception("ENSURE-STOPS FAILED: %s — POSITION UNPROTECTED!", symbol)
                _log_tx(db, account_id, "stop_ensure_failed", symbol,
                        detail={"error": str(e), "watch_id": watch_id})
                continue

        builder = WatchBuilder.from_dict(watch)
        builder.alpaca_stop_order_id = result.order_id
        builder.alpaca_stop_price = stop_price
        updated = builder.to_watch()
        update_watch(db, watch_id, updated.to_dict())

        summary["submitted"].append({"symbol": symbol, "stop_price": stop_price,
                                     "qty": qty, "order_id": result.order_id})
        log.info("ENSURE-STOPS: %s qty=%.4f stop=%.2f order=%s",
                 symbol, qty, stop_price, result.order_id)
        _log_tx(db, account_id, "stop_ensure", symbol,
                order_id=result.order_id, detail={"qty": qty, "stop_price": stop_price,
                                                  "watch_id": watch_id})

    n_submitted = len(summary["submitted"])
    n_open = len(summary["already_open"])
    n_errors = len(summary["errors"])
    n_no_price = len(summary["no_stop_price"])
    if n_submitted or n_errors or n_no_price:
        log.info("ENSURE-STOPS: %d submitted, %d already open, %d errors, %d no stop price",
                 n_submitted, n_open, n_errors, n_no_price)
    elif n_open:
        log.info("ENSURE-STOPS: all %d stops active", n_open)

    return summary
