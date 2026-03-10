"""Portfolio-aware symbol management.

Reads held symbols from the trader app's SQLite DB and manages
dynamic add/remove on the Schwab stream subscription.

Symbols are added when bought and retired after a configurable
cool-off period once no longer held by any portfolio.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)


class PortfolioSync:
    """Tracks portfolio holdings and manages dynamic symbol subscriptions."""

    def __init__(
        self,
        trader_db_path: str,
        cooloff_minutes: float = 60.0,
    ) -> None:
        self._db_path = trader_db_path
        self._cooloff_sec = cooloff_minutes * 60

        # symbol → timestamp when it was last seen as held
        self._last_held: dict[str, float] = {}

        # Current active set
        self._active: set[str] = set()

    @property
    def active_symbols(self) -> set[str]:
        return set(self._active)

    def sync(self) -> tuple[set[str], set[str]]:
        """Poll trader.db, return (newly_added, newly_removed) symbols.

        Call this periodically (e.g., every 60s). It:
        1. Reads current holdings from trader.db
        2. Adds any new symbols not already active
        3. Retires symbols past their cool-off period
        """
        held = self._read_holdings()
        now = time.time()

        # Update last_held timestamp for currently held symbols
        for sym in held:
            self._last_held[sym] = now

        # New symbols to add (held but not yet active)
        to_add = held - self._active

        # Candidates for removal: no longer held and past cool-off
        to_remove: set[str] = set()
        for sym in set(self._active):
            if sym not in held:
                last = self._last_held.get(sym, 0)
                if now - last > self._cooloff_sec:
                    to_remove.add(sym)

        # Apply changes
        self._active |= to_add
        self._active -= to_remove

        # Clean up tracking for removed symbols
        for sym in to_remove:
            self._last_held.pop(sym, None)

        if to_add:
            log.info("Portfolio sync: adding %d symbols: %s", len(to_add), sorted(to_add))
        if to_remove:
            log.info("Portfolio sync: retiring %d symbols: %s", len(to_remove), sorted(to_remove))

        return to_add, to_remove

    def _read_holdings(self) -> set[str]:
        """Read distinct held symbols from trader.db."""
        path = Path(self._db_path)
        if not path.exists():
            log.warning("trader.db not found at %s", self._db_path)
            return set()

        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM watches WHERE status = 'holding'"
            ).fetchall()
            conn.close()
            return {r[0].upper() for r in rows}
        except Exception:
            log.exception("Failed to read holdings from trader.db")
            return set()
