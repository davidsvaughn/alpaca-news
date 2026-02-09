"""Online orchestrator.

Watches `output/alpaca/*.json` for new files and produces sealed Snapshots.

Phase 1 constraints:
- One snapshot per input file
- Persist snapshot to (a) JSON file under data/snapshots and (b) SQLite row
- Emit events to an in-memory SSE bus (dashboard)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from trader.config import Settings
from trader.db.database import Database, insert_snapshot
from trader.knowledge.store import KnowledgeStore
from trader.llm.client import LLMClient
from trader.llm.cost_tracker import CostTracker
from trader.models.snapshot import Snapshot, Trigger
from trader.online.explorer import explore_phase1
from trader.online.triage import run_triage


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


def _load_news_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot_path(settings: Settings, snapshot_id: str) -> Path:
    return Path(settings.snapshots_dir) / f"{snapshot_id}.json"


def process_news_file(
    *,
    path: Path,
    settings: Settings,
    db: Database,
    knowledge: KnowledgeStore,
    bus: EventBus,
) -> None:
    news = _load_news_json(path)
    trigger = Trigger(
        type="alpaca_news",
        alpaca_timestamp=str(news.get("created_at")) if news.get("created_at") else None,
        headline=str(news.get("headline") or ""),
        summary=str(news.get("summary") or "") if news.get("summary") else None,
        source=str(news.get("source") or "") if news.get("source") else None,
        symbols=[str(x) for x in (news.get("symbols") or [])],
        raw=news,
    )

    snapshot = Snapshot.new(trigger=trigger)
    bus.publish(PipelineEvent(type="news_received", payload={"path": str(path), "snapshot_id": snapshot.snapshot_id}))

    cost_tracker = CostTracker(
        max_daily_cost=settings.max_daily_cost,
        max_cost_per_item=settings.max_cost_per_news_item,
        debug=settings.debug,
    )
    llm = LLMClient(cost_tracker=cost_tracker)

    # Stage 1
    triage = run_triage(
        llm=llm,
        provider=settings.triage_provider,
        model=settings.triage_model,
        knowledge=knowledge,
        news=news,
    )
    bus.publish(
        PipelineEvent(
            type="triage_decision",
            payload={
                "snapshot_id": snapshot.snapshot_id,
                "action": triage.action,
                "confidence": triage.confidence,
                "symbols": triage.symbols,
            },
        )
    )

    if triage.skip_patterns_learned:
        knowledge.append_skip_keywords(triage.skip_patterns_learned)

    tool_traces: list[dict[str, Any]] = []
    if triage.action == "investigate":
        # Stage 2 (Phase 1 minimal)
        explore = explore_phase1(
            llm=llm,
            provider=settings.research_provider,
            model=settings.research_model,
            news=news,
            use_web_search_tool=True,
            use_x_search_tool=(settings.research_provider == "grok"),
        )
        tool_traces.extend(explore.traces)

    # Seal snapshot: include traces and cost summary
    snap_dict = snapshot.to_dict()
    snap_dict["tool_traces"] = tool_traces
    snap_dict["cost_summary"] = {
        "total_usd": cost_tracker.daily_spent,
        "by_tool": {},
    }

    # Persist
    out_path = _snapshot_path(settings, snapshot.snapshot_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snap_dict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    insert_snapshot(db, snapshot=snap_dict)

    bus.publish(
        PipelineEvent(type="snapshot_sealed", payload={"snapshot_id": snapshot.snapshot_id, "path": str(out_path)})
    )


class _NewsHandler(FileSystemEventHandler):
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        knowledge: KnowledgeStore,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.db = db
        self.knowledge = knowledge
        self.bus = bus

    def on_created(self, event):  # type: ignore[override]
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".json":
            return

        # Give the writer a moment to finish.
        time.sleep(0.1)
        process_news_file(
            path=path,
            settings=self.settings,
            db=self.db,
            knowledge=self.knowledge,
            bus=self.bus,
        )


def run_watch_loop(*, settings: Settings, db: Database, knowledge: KnowledgeStore, bus: EventBus) -> None:
    watch_dir = Path(settings.alpaca_output_dir)
    watch_dir.mkdir(parents=True, exist_ok=True)

    handler = _NewsHandler(settings=settings, db=db, knowledge=knowledge, bus=bus)
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()
    bus.publish(PipelineEvent(type="watching", payload={"dir": str(watch_dir)}))
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
