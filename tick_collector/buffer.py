"""Thread-safe write buffer for accumulating trades before DB flush."""

from __future__ import annotations

import logging
import threading
from collections import deque

from .db import Trade

log = logging.getLogger(__name__)


class TradeBuffer:
    """Thread-safe buffer: schwabdev callback appends, async flush loop drains.

    The schwabdev Stream runs its callback on a background thread, so all
    append operations must be thread-safe. The flush loop runs on the asyncio
    event loop and drains the buffer periodically.
    """

    def __init__(self, max_batch: int = 5000) -> None:
        self._lock = threading.Lock()
        self._buf: deque[Trade] = deque()
        self._max_batch = max_batch
        self._total_buffered = 0
        self._total_flushed = 0

    def append(self, trade: Trade) -> None:
        with self._lock:
            self._buf.append(trade)
            self._total_buffered += 1

    def drain(self) -> list[Trade]:
        """Remove and return up to max_batch trades from the buffer."""
        with self._lock:
            n = min(len(self._buf), self._max_batch)
            batch = [self._buf.popleft() for _ in range(n)]
            self._total_flushed += len(batch)
            return batch

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buf)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "pending": len(self._buf),
                "total_buffered": self._total_buffered,
                "total_flushed": self._total_flushed,
            }
