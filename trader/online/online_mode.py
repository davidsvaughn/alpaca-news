"""Thread-safe online mode flag.

When online mode is OFF (system is offline):
- No new news files are processed
- No new follow-up collection cycles run
- No new backfill processing
- No manual explores via dashboard
- In-flight operations finish naturally
- WatchMonitor continues running but LLM check-ins are downgraded
  to lightweight (price-only, free APIs). No API costs incurred.

Auto market hours mode (ONLINE_AUTO_MARKET_HOURS=1):
- System goes ONLINE at market open (9:30 AM ET, Mon-Fri)
- System goes OFFLINE at market close (4:00 PM ET, Mon-Fri)
- Stays OFFLINE all weekend
- Manual toggle sets an override that persists until the next natural auto transition
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class OnlineMode:
    """Thread-safe boolean flag for online mode."""

    def __init__(self, enabled: bool = False) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
        self._auto_thread: threading.Thread | None = None
        self._manual_override: bool = False

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set(self, value: bool) -> None:
        with self._lock:
            self._enabled = value

    def toggle(self) -> bool:
        """Toggle and return the new value. Sets manual override to prevent auto-loop from reverting."""
        with self._lock:
            self._enabled = not self._enabled
            self._manual_override = True
            return self._enabled

    def start_auto_market_hours(self) -> None:
        """Start background thread that syncs online mode to market hours."""
        if self._auto_thread and self._auto_thread.is_alive():
            return
        self._auto_thread = threading.Thread(
            target=self._auto_loop,
            name="online-auto-market",
            daemon=True,
        )
        self._auto_thread.start()
        log.info("Online auto-market-hours enabled")

    def _auto_loop(self) -> None:
        from trader.market.market_hours import is_market_open

        while True:
            try:
                market_open = is_market_open()
                auto_value = market_open  # Online when market open
                with self._lock:
                    if self._manual_override:
                        # Clear override once auto state matches what user set
                        if self._enabled == auto_value:
                            self._manual_override = False
                        # Either way, don't overwrite while override is active
                        continue
                    old = self._enabled
                    self._enabled = auto_value
                    if self._enabled != old:
                        state = "ONLINE (market open)" if self._enabled else "OFFLINE (market closed)"
                        log.info("ONLINE AUTO: %s", state)
                        self._notify_change(state)
            except Exception:
                log.exception("Online auto-market-hours check failed")
            finally:
                time.sleep(30)

    @staticmethod
    def _notify_change(state: str) -> None:
        try:
            from trader.notifications import send_email
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now = datetime.now(tz=ZoneInfo("US/Eastern")).strftime("%I:%M %p ET")
            send_email(
                subject=f"Alpaca News — {state}",
                body=f"System changed to {state} at {now}.",
            )
        except Exception:
            log.warning("Failed to send online mode change notification")
