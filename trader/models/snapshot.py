"""Snapshot artifact (atomic learning unit).

The online pipeline creates one Snapshot per trigger news event and seals it.
Snapshots are immutable once persisted.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class ExplorationBudget:
    max_hops: int = 3
    max_cost_usd: float = 0.35


@dataclass(frozen=True)
class Trigger:
    type: str
    alpaca_timestamp: str | None
    headline: str
    summary: str | None
    source: str | None
    symbols: list[str]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CostSummary:
    total_usd: float = 0.0
    by_tool: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    version: str
    created_at: str
    trigger: Trigger
    market_context: dict[str, Any] = field(default_factory=dict)
    price_context: dict[str, Any] = field(default_factory=dict)
    exploration_budget: ExplorationBudget = field(default_factory=ExplorationBudget)
    tool_traces: list[dict[str, Any]] = field(default_factory=list)
    prediction: dict[str, Any] = field(default_factory=dict)
    cost_summary: CostSummary = field(default_factory=CostSummary)

    @staticmethod
    def new(*, trigger: Trigger, version: str = "v1") -> "Snapshot":
        return Snapshot(
            snapshot_id=str(uuid.uuid4()),
            version=version,
            created_at=utc_now().isoformat(),
            trigger=trigger,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # dataclass -> nested dict already; keep stable.
        return d

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def persist(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(indent=2), encoding="utf-8")
