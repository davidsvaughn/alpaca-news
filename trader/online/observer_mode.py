"""Thread-safe observer mode flag.

When observer mode is ON:
- No new news files are processed
- No new follow-up collection cycles run
- No new backfill processing
- No manual explores via dashboard
- In-flight operations finish naturally
- WatchMonitor continues running but LLM check-ins are downgraded
  to lightweight (price-only, free APIs). No API costs incurred.

Auto market hours mode (OBSERVER_AUTO_MARKET_HOURS=1):
- Observer turns ON at market close (4:00 PM ET, Mon-Fri)
- Observer turns OFF at market open (9:30 AM ET, Mon-Fri)
- Stays ON all weekend
- Manual toggle still works but will be overridden at next check
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class ObserverMode:
    """Thread-safe boolean flag for observer mode."""

    def __init__(self, enabled: bool = False) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
        self._auto_thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set(self, value: bool) -> None:
        with self._lock:
            self._enabled = value

    def toggle(self) -> bool:
        """Toggle and return the new value."""
        with self._lock:
            self._enabled = not self._enabled
            return self._enabled

    def start_auto_market_hours(self) -> None:
        """Start background thread that syncs observer mode to market hours."""
        if self._auto_thread and self._auto_thread.is_alive():
            return
        self._auto_thread = threading.Thread(
            target=self._auto_loop,
            name="observer-auto-market",
            daemon=True,
        )
        self._auto_thread.start()
        log.info("Observer auto-market-hours enabled")

    def _auto_loop(self) -> None:
        from trader.market.market_hours import is_market_open

        while True:
            try:
                market_open = is_market_open()
                with self._lock:
                    old = self._enabled
                    # Observer ON when market closed, OFF when open
                    self._enabled = not market_open
                    if self._enabled != old:
                        state = "ON (market closed)" if self._enabled else "OFF (market open)"
                        log.info("OBSERVER AUTO: %s", state)
                        self._notify_change(state)
            except Exception:
                log.exception("Observer auto-market-hours check failed")
            time.sleep(30)

    @staticmethod
    def _notify_change(state: str) -> None:
        try:
            from trader.notifications import send_email
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now = datetime.now(tz=ZoneInfo("US/Eastern")).strftime("%I:%M %p ET")
            send_email(
                subject=f"Alpaca News — Observer {state}",
                body=f"Observer mode changed to {state} at {now}.",
            )
        except Exception:
            log.warning("Failed to send observer change notification")
