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
import re
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
    from trader.db.database import (
        get_active_watches,
        insert_watch,
        update_watch_if_current_status,
    )
    from trader.models.watch import WatchBuilder

    account_id = getattr(broker, "account_id", None) or "unknown"

    summary: dict[str, Any] = {
        "ok": [],
        "watch_force_exited": [],
        "watch_exit_in_flight": [],
        "watch_phantom_miss": [],   # bulk miss but position still exists (transient)
        "alpaca_orphan_closed": [],
        "alpaca_orphan_adopted": [],
        "alpaca_orphan_exit_in_flight": [],
        "alpaca_orphan_pending_buy": [],
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
    exit_in_flight_symbols = {
        w["symbol"] for w in all_watches
        if w.get("exit_in_flight")
    }
    all_open_orders = broker.get_open_orders()
    open_buy_symbols = {
        o.symbol for o in all_open_orders
        if (o.side or "").lower() == "buy"
    }

    # Rule 1 & 4: Both sides have the position
    for symbol in alpaca_symbols & watch_symbols:
        pos = alpaca_positions[symbol]
        watch = watch_by_symbol[symbol]

        # Fractional remainder cleanup: < 1 share with a watch → liquidate and exit watch
        if float(pos.qty) < 1.0:
            from trader.market.market_hours import in_extended_only, ALPACA_EXTENDED_HOURS
            in_extended = ALPACA_EXTENDED_HOURS and in_extended_only()
            if not in_extended:
                try:
                    # Cancel stops first — they hold shares and block the sell
                    broker._cancel_open_orders(symbol)
                    broker.close_position(symbol)
                    builder = WatchBuilder.from_dict(watch)
                    exit_price = pos.current_price or pos.avg_entry_price
                    builder.record_exit(
                        price=exit_price,
                        reason="fractional_remainder_liquidated",
                    )
                    updated = builder.to_watch()
                    ok = update_watch_if_current_status(
                        db,
                        watch["watch_id"],
                        updated.to_dict(),
                        expected_status="holding",
                    )
                    if not ok:
                        log.info("RECONCILE: %s fractional liquidation skipped stale watch %s",
                                 symbol, watch["watch_id"])
                    summary.setdefault("fractional_liquidated", []).append(symbol)
                    log.info("RECONCILE: %s liquidated fractional remainder (qty=%.4f) "
                             "and exited watch %s",
                             symbol, float(pos.qty), watch["watch_id"])
                    _log_tx(db, account_id, "reconcile_fractional_liquidated", symbol,
                            detail={"qty": float(pos.qty), "watch_id": watch["watch_id"],
                                    "exit_price": exit_price})
                except Exception:
                    log.exception("RECONCILE: failed to liquidate fractional %s", symbol)
                    _log_tx(db, account_id, "reconcile_fractional_failed", symbol,
                            detail={"qty": float(pos.qty), "watch_id": watch["watch_id"]})
                continue
            else:
                log.info("RECONCILE: %s fractional remainder (qty=%.4f) — skipping "
                         "until regular hours", symbol, float(pos.qty))
                # Fall through to normal sync

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
            ok = update_watch_if_current_status(
                db,
                watch["watch_id"],
                updated.to_dict(),
                expected_status="holding",
            )
            if not ok:
                log.info("RECONCILE: %s sync skipped stale watch %s",
                         symbol, watch["watch_id"])
        else:
            summary["ok"].append(symbol)
            _log_tx(db, account_id, "reconcile_ok", symbol,
                    detail={"entry_price": entry_price, "watch_id": watch["watch_id"]})

    # Rule 3: We have a watch but Alpaca has no position → VERIFY before force-exiting
    for symbol in watch_symbols - alpaca_symbols:
        watch = watch_by_symbol[symbol]

        if watch.get("exit_in_flight"):
            summary["watch_exit_in_flight"].append(symbol)
            log.info("RECONCILE: %s missing from positions but watch %s has exit_in_flight — skipping force-exit",
                     symbol, watch["watch_id"])
            _log_tx(db, account_id, "reconcile_exit_in_flight_skip", symbol,
                    detail={"watch_id": watch["watch_id"]})
            continue

        # Step 1: Double-check with per-symbol lookup (bulk list may be stale)
        per_symbol_pos = broker.get_position(symbol)
        if per_symbol_pos is not None:
            # Position exists! Bulk lookup was wrong (transient API issue).
            summary["watch_phantom_miss"].append(symbol)
            log.warning("RECONCILE: %s missing from bulk positions but per-symbol lookup found it (qty=%.4f) — NOT force-exiting", symbol, per_symbol_pos.qty)
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
                log.error("RECONCILE: %s has NO Alpaca position AND no recent sell order found! Manual investigation required!", symbol)
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
        ok = update_watch_if_current_status(
            db,
            watch["watch_id"],
            updated.to_dict(),
            expected_status="holding",
        )
        if not ok:
            log.info("RECONCILE: %s force-exit skipped stale watch %s",
                     symbol, watch["watch_id"])
            continue
        summary["watch_force_exited"].append({
            "symbol": symbol,
            "watch_id": watch["watch_id"],
            "exit_price": exit_price,
        })
        log.warning("RECONCILE: %s watch %s force-exited at $%.2f (confirmed sell)",
                    symbol, watch["watch_id"], exit_price)
        _log_tx(db, account_id, "reconcile_force_exit", symbol,
                detail={"watch_id": watch["watch_id"], "exit_price": exit_price})

    # Rule 2: Alpaca has a position but we have no watch
    # Default: adopt (create watch from config). Fallback: close on Alpaca.
    # Special case: fractional remainders (< 1 share) from extended-hours sells
    # are liquidated during regular hours instead of being adopted.
    for symbol in alpaca_symbols - watch_symbols:
        pos = alpaca_positions[symbol]

        if symbol in exit_in_flight_symbols:
            summary["alpaca_orphan_exit_in_flight"].append(symbol)
            log.info("RECONCILE: %s position without holding watch but an active watch has exit_in_flight — skipping adoption",
                     symbol)
            _log_tx(db, account_id, "reconcile_orphan_exit_in_flight", symbol,
                    detail={"qty": pos.qty, "avg_entry_price": pos.avg_entry_price})
            continue

        # A live buy is still working for this symbol. Do not adopt/close yet.
        if symbol in open_buy_symbols:
            summary["alpaca_orphan_pending_buy"].append(symbol)
            log.info("RECONCILE: %s position without watch but open buy order present — waiting", symbol)
            _log_tx(db, account_id, "reconcile_orphan_pending_buy", symbol,
                    detail={"qty": pos.qty, "avg_entry_price": pos.avg_entry_price})
            continue

        # Fractional remainder cleanup: < 1 share orphan → liquidate during regular hours
        if float(pos.qty) < 1.0:
            from trader.market.market_hours import in_extended_only, ALPACA_EXTENDED_HOURS
            in_extended = ALPACA_EXTENDED_HOURS and in_extended_only()
            if not in_extended:
                try:
                    # Cancel stops first — they hold shares and block the sell
                    broker._cancel_open_orders(symbol)
                    broker.close_position(symbol)
                    summary.setdefault("fractional_liquidated", []).append(symbol)
                    log.info("RECONCILE: %s liquidated fractional remainder (qty=%.4f)",
                             symbol, float(pos.qty))
                    _log_tx(db, account_id, "reconcile_fractional_liquidated", symbol,
                            detail={"qty": float(pos.qty),
                                    "avg_entry_price": pos.avg_entry_price})
                except Exception:
                    log.exception("RECONCILE: failed to liquidate fractional %s", symbol)
                    _log_tx(db, account_id, "reconcile_fractional_failed", symbol,
                            detail={"qty": float(pos.qty)})
                continue
            else:
                log.info("RECONCILE: %s fractional remainder (qty=%.4f) — skipping "
                         "until regular hours", symbol, float(pos.qty))
                continue

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

    n_fractional = len(summary.get("fractional_liquidated", []))
    total_actions = (
        len(summary["watch_force_exited"])
        + len(summary["alpaca_orphan_closed"])
        + len(summary["alpaca_orphan_adopted"])
        + len(summary["alpaca_orphan_pending_buy"])
        + len(summary["entry_price_updated"])
        + len(summary["qty_updated"])
        + n_fractional
    )
    n_phantom = len(summary["watch_phantom_miss"])
    if total_actions > 0 or n_phantom > 0:
        msg = (f"RECONCILE complete: {len(summary['ok'])} OK, "
               f"{len(summary['watch_force_exited'])} force-exited, "
               f"{len(summary['alpaca_orphan_closed'])} orphans closed, "
               f"{len(summary['alpaca_orphan_adopted'])} adopted, "
               f"{len(summary['alpaca_orphan_pending_buy'])} pending buys, "
               f"{len(summary['entry_price_updated'])} prices updated, "
               f"{len(summary['qty_updated'])} qty updated")
        if n_fractional:
            msg += f", {n_fractional} fractional remainders liquidated"
        if n_phantom:
            msg += f", {n_phantom} PHANTOM MISSES (investigate!)"
        log.info(msg)
    else:
        log.info("RECONCILE complete: all %d positions in sync", len(summary["ok"]))

    return summary


def _parse_available_qty(error_msg: str) -> float | None:
    """Extract 'available' qty from Alpaca insufficient-qty error message.

    Error JSON looks like: {"available":"0","code":40310000,...,"message":"insufficient qty available for order (requested: 7.67, available: 0)"}
    """
    m = re.search(r'"available"\s*:\s*"([^"]+)"', error_msg)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


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
    from trader.db.database import get_active_watches, update_watch_if_current_status
    from trader.models.watch import WatchBuilder

    account_id = getattr(broker, "account_id", None) or "unknown"
    summary: dict[str, Any] = {
        "submitted": [],
        "already_open": [],
        "skipped_open_buy": [],
        "skipped_open_sell": [],
        "stop_breached_closed": [],
        "no_stop_price": [],
        "errors": [],
    }

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
    open_buys: set[str] = set()
    open_non_stop_sells: set[str] = set()
    for o in all_open_orders:
        side = (o.side or "").lower()
        if side == "buy":
            open_buys.add(o.symbol)
        elif side == "sell":
            if o.stop_price is not None:
                open_stops_by_symbol[o.symbol] = o
            else:
                open_non_stop_sells.add(o.symbol)

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
                update_watch_if_current_status(
                    db,
                    watch_id,
                    updated.to_dict(),
                    expected_status="holding",
                )
            summary["already_open"].append(symbol)
            continue

        # Alpaca can reject a stop while the buy order is still working.
        if symbol in open_buys:
            summary["skipped_open_buy"].append(symbol)
            log.info("ENSURE-STOPS: %s skipped (open buy order present)", symbol)
            continue

        # Active close/replacement sell order is already holding shares.
        # Do not submit a stop; retry on next ensure cycle.
        if symbol in open_non_stop_sells:
            summary["skipped_open_sell"].append(symbol)
            log.info("ENSURE-STOPS: %s skipped (open non-stop sell order present)", symbol)
            continue

        # If price has already fallen past the stop level, close immediately
        current_price = float(pos.current_price) if pos.current_price else None
        if current_price and stop_price >= current_price:
            log.warning(
                "ENSURE-STOPS: %s stop $%.2f >= market $%.2f — stop BREACHED, closing position immediately",
                symbol, stop_price, current_price,
            )
            try:
                close_result = broker.close_position(symbol)
                if close_result:
                    log.warning("ENSURE-STOPS: %s market sell submitted -> order %s",
                                symbol, close_result.order_id)
                    builder = WatchBuilder.from_dict(watch)
                    builder.record_exit(price=current_price, reason="stop_breached_at_startup")
                    updated = builder.to_watch()
                    update_watch_if_current_status(
                        db, watch_id, updated.to_dict(), expected_status="holding",
                    )
                    summary["stop_breached_closed"].append({
                        "symbol": symbol, "stop_price": stop_price,
                        "market_price": current_price, "order_id": close_result.order_id,
                    })
                    _log_tx(db, account_id, "stop_breached_close", symbol,
                            order_id=close_result.order_id,
                            detail={"stop_price": stop_price, "market_price": current_price,
                                    "watch_id": watch_id})
                    from trader.notifications import notify
                    notify(
                        subject=f"Stop breached: {symbol} closed at market",
                        body=(f"{symbol} stop ${stop_price:.2f} >= market ${current_price:.2f}.\n"
                              f"Position closed via market sell (order {close_result.order_id}).\n"
                              f"Account: {account_id}"),
                    )
            except Exception as e:
                log.exception("ENSURE-STOPS: %s stop breached but CLOSE FAILED — POSITION UNPROTECTED!", symbol)
                summary["errors"].append({"symbol": symbol, "error": f"stop_breached_close_failed: {e}"})
                _log_tx(db, account_id, "stop_breached_close_failed", symbol,
                        detail={"error": str(e), "stop_price": stop_price,
                                "market_price": current_price, "watch_id": watch_id})
                from trader.notifications import notify
                notify(
                    subject=f"CRITICAL: {symbol} stop breached, close FAILED",
                    body=(f"{symbol} stop ${stop_price:.2f} >= market ${current_price:.2f}.\n"
                          f"Market sell FAILED: {e}\n"
                          f"Account: {account_id} — MANUAL INTERVENTION NEEDED"),
                )
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
        ok = update_watch_if_current_status(
            db,
            watch_id,
            updated.to_dict(),
            expected_status="holding",
        )
        if not ok:
            log.info("ENSURE-STOPS: %s skipped stale watch %s", symbol, watch_id)
            continue

        summary["submitted"].append({"symbol": symbol, "stop_price": stop_price,
                                     "qty": qty, "order_id": result.order_id})
        log.info("ENSURE-STOPS: %s qty=%.4f stop=%.2f order=%s",
                 symbol, qty, stop_price, result.order_id)
        _log_tx(db, account_id, "stop_ensure", symbol,
                order_id=result.order_id, detail={"qty": qty, "stop_price": stop_price,
                                                  "watch_id": watch_id})

    n_submitted = len(summary["submitted"])
    n_open = len(summary["already_open"])
    n_skipped_buy = len(summary["skipped_open_buy"])
    n_skipped_sell = len(summary["skipped_open_sell"])
    n_breached = len(summary["stop_breached_closed"])
    n_errors = len(summary["errors"])
    n_no_price = len(summary["no_stop_price"])
    if n_submitted or n_errors or n_no_price or n_skipped_sell or n_skipped_buy or n_breached:
        log.info("ENSURE-STOPS: %d submitted, %d already open, %d skipped (open buy), %d skipped (open sell), %d breached/closed, %d errors, %d no stop price",
                 n_submitted, n_open, n_skipped_buy, n_skipped_sell, n_breached, n_errors, n_no_price)
    elif n_open:
        log.info("ENSURE-STOPS: all %d stops active", n_open)

    return summary
