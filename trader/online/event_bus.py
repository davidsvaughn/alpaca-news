"""In-memory event bus for SSE.

Extracted from orchestrator.py to avoid circular imports when optional
services (like XStreamService) need to publish events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PipelineEvent:
    type: str
    payload: dict[str, Any]


class EventBus:
    """Very small in-memory pub/sub for SSE."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[PipelineEvent], None]] = []

    def publish(self, event: PipelineEvent) -> None:
        for cb in list(self._subscribers):
            cb(event)

    def subscribe(self, cb: Callable[[PipelineEvent], None]) -> Callable[[], None]:
        self._subscribers.append(cb)

        def unsubscribe() -> None:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

        return unsubscribe
