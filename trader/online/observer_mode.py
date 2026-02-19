"""Thread-safe observer mode flag.

When observer mode is ON:
- No new news files are processed
- No new follow-up collection cycles run
- No new backfill processing
- No manual explores via dashboard
- In-flight operations finish naturally
- WatchMonitor continues running but LLM check-ins are downgraded
  to lightweight (price-only, free APIs). No API costs incurred.
"""

from __future__ import annotations

import threading


class ObserverMode:
    """Thread-safe boolean flag for observer mode."""

    def __init__(self, enabled: bool = False) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled

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
