"""Thread-safe activity tracker for in-flight operations.

Tracks backfills, explorations, and follow-up collections so the dashboard
can show what is currently running and accumulating cost.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Activity:
    """A single in-flight operation."""

    id: str
    type: str  # "backfill" | "exploration" | "follow_up_collection"
    label: str  # human-readable description
    symbols: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())
    progress: str = ""  # "3/10", "triage", "agent 1/3", etc.
    cost_usd: float = 0.0
    stage_started_at: str = ""  # ISO timestamp of when current stage/progress began
    detail: dict[str, Any] = field(default_factory=dict)


class ActivityTracker:
    """Thread-safe tracker for all in-flight operations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activities: dict[str, Activity] = {}

    def start(self, activity: Activity) -> None:
        """Register a new in-flight activity."""
        with self._lock:
            self._activities[activity.id] = activity

    def update(self, id: str, **kwargs: Any) -> None:
        """Update fields on an existing activity. Unknown id is silently ignored."""
        with self._lock:
            act = self._activities.get(id)
            if act is None:
                return
            for k, v in kwargs.items():
                if hasattr(act, k):
                    object.__setattr__(act, k, v)

    def finish(self, id: str) -> None:
        """Remove an activity (operation complete)."""
        with self._lock:
            self._activities.pop(id, None)

    def get_all(self) -> list[Activity]:
        """Return a snapshot of all active operations."""
        with self._lock:
            return list(self._activities.values())

    def get_inflight_cost(self) -> float:
        """Total cost of all in-flight operations."""
        with self._lock:
            return sum(a.cost_usd for a in self._activities.values())
