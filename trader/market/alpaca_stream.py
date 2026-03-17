"""Alpaca trade update stream.

Runs TradingStream WebSockets in daemon threads to receive real-time
order fill/cancel/reject events. Supports multiple accounts — one
stream per account.

Usage::

    stream = AlpacaTradeStream(db=db, bus=bus, api_key=key, secret_key=secret)
    stream.start()  # launches daemon thread
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)


class AlpacaTradeStream:
    """Listens for Alpaca trade update events for one account."""

    def __init__(
        self,
        *,
        db: Any,
        bus: Any = None,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        account_label: str = "",
        account_id: str = "",
        on_fill: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.bus = bus
        self._on_fill_callback = on_fill
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._label = account_label
        self._account_id = account_id or account_label
        self._thread: threading.Thread | None = None
        self._stream: Any = None

    def start(self) -> None:
        """Start the trade update stream in a daemon thread."""
        if self._thread and self._thread.is_alive():
            log.warning("AlpacaTradeStream(%s) already running", self._label)
            return

        self._thread = threading.Thread(
            target=self._run,
            name=f"alpaca-stream-{self._label}",
            daemon=True,
        )
        self._thread.start()
        log.info("AlpacaTradeStream started: %s (paper=%s)", self._label, self._paper)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass

    def _run(self) -> None:
        from alpaca.trading.stream import TradingStream

        self._stream = TradingStream(
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=self._paper,
        )
        self._stream.subscribe_trade_updates(self._handle_update)

        while True:
            try:
                log.info("Connecting to Alpaca trade updates: %s ...", self._label)
                self._stream.run()
            except Exception:
                log.exception("Alpaca trade stream (%s) disconnected, reconnecting in 5s...", self._label)
                import time
                time.sleep(5)

    async def _handle_update(self, data: Any) -> None:
        try:
            event_raw = data.event
            event = event_raw.value if hasattr(event_raw, "value") else str(event_raw)
            order = data.order
            symbol = str(order.symbol)
            order_id = str(order.id)
            side = order.side.value if hasattr(order.side, "value") else str(order.side)
            status = order.status.value if hasattr(order.status, "value") else str(order.status)

            log.info(
                "ALPACA EVENT [%s]: %s %s %s side=%s status=%s filled_qty=%s avg_price=%s",
                self._label, event, symbol, order_id, side, status,
                order.filled_qty, order.filled_avg_price,
            )

            # Log every event to the transaction log
            self._log_tx(
                event=f"stream_{event}",
                symbol=symbol,
                order_id=order_id,
                status=status,
                detail={
                    "side": side,
                    "filled_qty": float(order.filled_qty) if order.filled_qty else None,
                    "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
                    "stop_price": float(order.stop_price) if order.stop_price else None,
                },
            )

            if event == "fill":
                fill_info = {
                    "event": event,
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "status": status,
                    "filled_qty": float(order.filled_qty) if order.filled_qty else 0,
                    "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else 0,
                    "stop_price": float(order.stop_price) if order.stop_price else None,
                    "account_label": self._label,
                }

                if side.lower() == "sell":
                    self._handle_sell_fill(fill_info)
                if side.lower() == "buy":
                    self._handle_buy_fill(fill_info)
                if self._on_fill_callback:
                    try:
                        self._on_fill_callback(fill_info)
                    except Exception:
                        log.exception("on_fill callback error")

            elif event in ("canceled", "rejected", "expired"):
                log.warning("ALPACA ORDER %s [%s]: %s %s %s", event.upper(), self._label, symbol, order_id, side)

            if self.bus:
                from trader.online.event_bus import PipelineEvent
                self.bus.publish(PipelineEvent(
                    type="alpaca_trade_update",
                    payload={
                        "event": event,
                        "symbol": symbol,
                        "order_id": order_id,
                        "side": side,
                        "status": status,
                        "account_label": self._label,
                    },
                ))

        except Exception:
            log.exception("Error handling Alpaca trade update [%s]", self._label)

    def _log_tx(self, event: str, symbol: str, **kwargs: Any) -> None:
        """Log a transaction to the database."""
        if not self.db:
            return
        try:
            from trader.db.database import log_alpaca_transaction
            log_alpaca_transaction(
                self.db,
                account_id=self._account_id,
                event=event,
                symbol=symbol,
                **kwargs,
            )
        except Exception:
            log.warning("Failed to log stream transaction: %s %s", event, symbol)

    def _handle_sell_fill(self, fill: dict[str, Any]) -> None:
        """Handle a sell fill — stop-loss or regular sell (possibly late after timeout)."""
        from trader.db.database import get_active_watches, update_watch
        from trader.models.watch import WatchBuilder

        order_id = fill["order_id"]
        symbol = fill["symbol"]
        fill_price = fill["filled_avg_price"]

        watches = get_active_watches(self.db)
        for w in watches:
            if w.get("symbol") != symbol:
                continue

            # Case 1: Stop-loss fill on a holding watch
            if w.get("status") == "holding" and w.get("alpaca_stop_order_id") == order_id:
                builder = WatchBuilder.from_dict(w)
                reason = f"alpaca_stop_fill (stop_price={fill.get('stop_price')})"
                builder.record_exit(price=fill_price, reason=reason)
                builder.clear_exit_pending()
                builder.last_checkin_at = datetime.now(tz=timezone.utc).isoformat()
                updated = builder.to_watch()
                update_watch(self.db, w["watch_id"], updated.to_dict())
                log.info("STOP FILL handled [%s]: %s %s price=%.2f",
                         self._label, symbol, w["watch_id"], fill_price)
                return

            # Case 2: Late sell fill — order timed out, watch already exited
            # with a bar-price estimate. Update with the actual fill price.
            if w.get("alpaca_sell_order_id") == order_id:
                builder = WatchBuilder.from_dict(w)
                old_exit = builder.exit
                if old_exit:
                    old_price = old_exit.price
                    from dataclasses import replace
                    builder.exit = replace(old_exit, price=fill_price)
                    builder.clear_exit_pending()
                    updated = builder.to_watch()
                    update_watch(self.db, w["watch_id"], updated.to_dict())
                    log.info("LATE SELL FILL [%s]: %s %s updated exit price %.2f -> %.2f",
                             self._label, symbol, w["watch_id"], old_price, fill_price)
                else:
                    # Watch hasn't been exited yet (sell submitted but exit not recorded)
                    reason = builder.pending_exit_reason or "sell_fill"
                    builder.record_exit(price=fill_price, reason=reason)
                    builder.clear_exit_pending()
                    builder.last_checkin_at = datetime.now(tz=timezone.utc).isoformat()
                    updated = builder.to_watch()
                    update_watch(self.db, w["watch_id"], updated.to_dict())
                    log.info("SELL FILL handled [%s]: %s %s price=%.2f",
                             self._label, symbol, w["watch_id"], fill_price)
                return

    def _handle_buy_fill(self, fill: dict[str, Any]) -> None:
        """Handle a buy fill — update watch with actual fill price."""
        from trader.db.database import get_active_watches, update_watch
        from trader.models.watch import WatchBuilder, WatchEntry

        order_id = fill["order_id"]
        fill_price = fill["filled_avg_price"]

        watches = get_active_watches(self.db)
        for w in watches:
            if w.get("alpaca_buy_order_id") == order_id:
                builder = WatchBuilder.from_dict(w)
                old_entry = builder.entry
                builder.entry = WatchEntry(
                    snapshot_id=old_entry.snapshot_id,
                    price=fill_price,
                    time=old_entry.time,
                    confidence=old_entry.confidence,
                    direction=old_entry.direction,
                    horizon=old_entry.horizon,
                    thesis=old_entry.thesis,
                )
                updated = builder.to_watch()
                update_watch(self.db, w["watch_id"], updated.to_dict())
                log.info("BUY FILL [%s]: %s %s price %.2f -> %.2f",
                         self._label, w["symbol"], w["watch_id"], old_entry.price, fill_price)
                break
