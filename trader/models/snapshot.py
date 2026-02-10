"""Snapshot artifact (atomic learning unit).

The online pipeline creates one Snapshot per trigger news event and seals it.
Snapshots are immutable once sealed.

Uses a builder pattern: SnapshotBuilder accumulates data during the pipeline,
then .seal() produces a frozen Snapshot.
"""

from __future__ import annotations

import hashlib
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
    """Immutable, sealed snapshot. Created only via SnapshotBuilder.seal()."""

    snapshot_id: str
    version: str
    created_at: str
    trigger: Trigger
    market_context: dict[str, Any]
    price_context: dict[str, Any]
    exploration_budget: ExplorationBudget
    tool_traces: list[dict[str, Any]]
    prediction: dict[str, Any]
    cost_summary: CostSummary

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def persist(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(indent=2) + "\n", encoding="utf-8")


def deterministic_snapshot_id(news: dict[str, Any]) -> str:
    """Derive a deterministic snapshot_id from the Alpaca article.

    Uses the Alpaca article ``id`` if present, otherwise hashes the headline +
    created_at to produce a reproducible UUID.  This makes backfill idempotent:
    processing the same news file twice yields the same snapshot_id.
    """
    alpaca_id = news.get("id")
    if alpaca_id is not None:
        # Stable namespace UUID from the integer article id
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"alpaca-news:{alpaca_id}"))

    # Fallback: hash headline + timestamp
    key = f"{news.get('headline', '')}|{news.get('created_at', '')}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    # Format as UUID for consistency
    return str(uuid.UUID(digest))


class SnapshotBuilder:
    """Mutable accumulator that produces a sealed Snapshot.

    Usage::

        builder = SnapshotBuilder(trigger=trigger, snapshot_id=det_id)
        builder.add_tool_trace(trace_dict)
        builder.set_prediction(...)
        snapshot = builder.seal()
    """

    def __init__(
        self,
        *,
        trigger: Trigger,
        snapshot_id: str | None = None,
        version: str = "v1",
        exploration_budget: ExplorationBudget | None = None,
    ) -> None:
        self.snapshot_id = snapshot_id or str(uuid.uuid4())
        self.version = version
        self.created_at = utc_now().isoformat()
        self.trigger = trigger
        self.market_context: dict[str, Any] = {}
        self.price_context: dict[str, Any] = {}
        self.exploration_budget = exploration_budget or ExplorationBudget()
        self.tool_traces: list[dict[str, Any]] = []
        self.prediction: dict[str, Any] = {}
        self._cost_by_tool: dict[str, float] = {}
        self._cost_total: float = 0.0

    # ------------------------------------------------------------------
    # Builder methods
    # ------------------------------------------------------------------

    def add_tool_trace(self, trace: dict[str, Any]) -> None:
        self.tool_traces.append(trace)
        # Accumulate cost from the trace execution block
        cost = float((trace.get("execution") or {}).get("cost_usd", 0.0))
        tool_name = (trace.get("action") or {}).get("tool", "unknown")
        self._cost_total += cost
        self._cost_by_tool[tool_name] = self._cost_by_tool.get(tool_name, 0.0) + cost

    def set_market_context(self, ctx: dict[str, Any]) -> None:
        self.market_context = ctx

    def set_price_context(self, ctx: dict[str, Any]) -> None:
        self.price_context = ctx

    def set_prediction(self, pred: dict[str, Any]) -> None:
        self.prediction = pred

    def set_cost_total(self, total: float) -> None:
        """Override the auto-accumulated total (e.g. from CostTracker)."""
        self._cost_total = total

    # ------------------------------------------------------------------
    # Seal
    # ------------------------------------------------------------------

    def seal(self) -> Snapshot:
        """Produce a frozen Snapshot from accumulated state."""
        return Snapshot(
            snapshot_id=self.snapshot_id,
            version=self.version,
            created_at=self.created_at,
            trigger=self.trigger,
            market_context=self.market_context,
            price_context=self.price_context,
            exploration_budget=self.exploration_budget,
            tool_traces=list(self.tool_traces),
            prediction=self.prediction,
            cost_summary=CostSummary(
                total_usd=round(self._cost_total, 6),
                by_tool={k: round(v, 6) for k, v in self._cost_by_tool.items()},
            ),
        )
