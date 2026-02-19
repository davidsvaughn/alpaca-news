"""FollowUp model for post-event data collection.

A FollowUp schedules lightweight, periodic data collection after a snapshot
is sealed — either for stocks we chose NOT to buy (no_buy) or after a watch
completes its lifecycle (post_exit).

Each FollowUp has a schedule of collection times (e.g. +1h, +4h, +1d, +3d, +5d).
At each scheduled time, the FollowUpCollector gathers price, news, web search,
and X/Twitter search data mechanically (no agent reasoning).

Uses the same builder pattern as Watch: FollowUpBuilder accumulates mutable
state, then produces a frozen FollowUp.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

FollowUpReason = Literal["no_buy", "post_exit"]
FollowUpStatus = Literal["active", "complete"]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def parse_offset_to_minutes(label: str) -> float:
    """Convert offset label like '+1h', '+4h', '+1d' to minutes.

    Supported formats: +Nm (minutes), +Nh (hours), +Nd (days).
    """
    label = label.strip().lstrip("+")
    if label.endswith("m"):
        return float(label[:-1])
    elif label.endswith("h"):
        return float(label[:-1]) * 60
    elif label.endswith("d"):
        return float(label[:-1]) * 1440
    raise ValueError(f"Unknown offset format: {label}")


@dataclass(frozen=True)
class FollowUpCollection:
    """A single point-in-time data collection."""

    collected_at: str                   # ISO timestamp
    offset_label: str                   # "+1h", "+4h", "+1d", etc.
    price: dict[str, Any]               # get_quote() result per symbol
    news: list[dict[str, Any]]          # get_company_news() results (yfinance + Finnhub merged)
    web_results: list[dict[str, Any]]   # [{query, answer, citations, quality}, ...]
    x_results: list[dict[str, Any]]     # [{query, answer, citations, quality}, ...]
    query_plan: dict[str, Any]          # {web_queries, x_queries, reasoning}
    cost_usd: float


@dataclass(frozen=True)
class FollowUp:
    """Immutable follow-up record. Created only via FollowUpBuilder."""

    follow_up_id: str
    snapshot_id: str
    symbols: list[str]
    reason: FollowUpReason
    schedule: list[str]                 # ["+1h", "+4h", "+1d", "+3d", "+5d"]
    started_at: str
    collections: list[FollowUpCollection]
    status: FollowUpStatus
    total_cost_usd: float
    config: dict[str, Any]              # web_searches_per_checkin, etc.
    watch_id: str | None                # set for post_exit
    headline: str                       # original trigger headline

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def persist(self, path: str | Path) -> None:
        from trader.models import atomic_write_text

        atomic_write_text(Path(path), self.to_json(indent=2) + "\n")


class FollowUpBuilder:
    """Mutable accumulator that produces a sealed FollowUp.

    Usage::

        builder = FollowUpBuilder(snapshot_id=sid, symbols=["VALE"], reason="no_buy")
        builder.add_collection(collection)
        follow_up = builder.to_follow_up()
    """

    def __init__(
        self,
        *,
        follow_up_id: str | None = None,
        snapshot_id: str,
        symbols: list[str],
        reason: FollowUpReason,
        schedule: list[str] | None = None,
        config: dict[str, Any] | None = None,
        watch_id: str | None = None,
        headline: str = "",
    ) -> None:
        self.follow_up_id = follow_up_id or f"fu_{uuid.uuid4().hex[:12]}"
        self.snapshot_id = snapshot_id
        self.symbols = list(symbols)
        self.reason = reason
        self.schedule = schedule or ["+1h", "+4h", "+1d", "+3d", "+5d"]
        self.started_at = _utc_now()
        self.collections: list[FollowUpCollection] = []
        self.status: FollowUpStatus = "active"
        self.total_cost_usd: float = 0.0
        self.config = config or {}
        self.watch_id = watch_id
        self.headline = headline

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FollowUpBuilder:
        """Reconstitute from stored follow-up dict."""
        builder = cls(
            follow_up_id=d["follow_up_id"],
            snapshot_id=d["snapshot_id"],
            symbols=list(d.get("symbols", [])),
            reason=d["reason"],
            schedule=list(d.get("schedule", [])),
            config=d.get("config", {}),
            watch_id=d.get("watch_id"),
            headline=d.get("headline", ""),
        )
        builder.started_at = d["started_at"]
        builder.status = d["status"]
        builder.total_cost_usd = d.get("total_cost_usd", 0.0)
        builder.collections = [
            FollowUpCollection(**c) for c in d.get("collections", [])
        ]
        return builder

    def add_collection(self, collection: FollowUpCollection) -> None:
        self.collections.append(collection)
        self.total_cost_usd += collection.cost_usd

    def complete(self) -> None:
        self.status = "complete"

    def next_offset_label(self) -> str | None:
        """Return the next uncollected offset label, or None if all done."""
        collected_labels = {c.offset_label for c in self.collections}
        for label in self.schedule:
            if label not in collected_labels:
                return label
        return None

    def to_follow_up(self) -> FollowUp:
        return FollowUp(
            follow_up_id=self.follow_up_id,
            snapshot_id=self.snapshot_id,
            symbols=list(self.symbols),
            reason=self.reason,
            schedule=list(self.schedule),
            started_at=self.started_at,
            collections=list(self.collections),
            status=self.status,
            total_cost_usd=round(self.total_cost_usd, 6),
            config=dict(self.config),
            watch_id=self.watch_id,
            headline=self.headline,
        )
