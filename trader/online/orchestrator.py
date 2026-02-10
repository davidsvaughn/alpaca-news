"""Online orchestrator.

Watches ``output/alpaca/*.json`` for new files and produces sealed Snapshots.

Design:
- Watchdog enqueues file paths into a :class:`queue.Queue`.
- A separate worker thread dequeues and processes them sequentially.
  This prevents blocking the watchdog thread during LLM API calls.
- Uses :class:`SnapshotBuilder` to accumulate data and ``.seal()`` a frozen
  Snapshot at the end.
- Snapshot IDs are deterministic (derived from the Alpaca article id) so that
  backfill is idempotent.
"""

from __future__ import annotations

import json
import os
import queue
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from trader.config import Settings
from trader.db.database import Database, insert_snapshot, snapshot_exists
from trader.knowledge.store import KnowledgeStore
from trader.llm.client import LLMClient
from trader.llm.cost_tracker import CostTracker
from trader.llm.mock import MockLLMClient
from trader.market.schwab_client import SchwabMarketClient
from trader.models.snapshot import SnapshotBuilder, Trigger, deterministic_snapshot_id
from trader.online.explorer import explore_two_phase
from trader.online.triage import run_triage

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# SSE event bus
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


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

    # Deterministic ID → idempotent on re-run
    snap_id = deterministic_snapshot_id(news)

    # Skip if already processed (backfill safety)
    if snapshot_exists(db, snap_id):
        if DEBUG:
            print(f"SKIP (already processed): {snap_id} from {path}")
        return

    trigger = Trigger(
        type="alpaca_news",
        alpaca_timestamp=str(news.get("created_at")) if news.get("created_at") else None,
        headline=str(news.get("headline") or ""),
        summary=str(news.get("summary") or "") if news.get("summary") else None,
        source=str(news.get("source") or "") if news.get("source") else None,
        symbols=[str(x) for x in (news.get("symbols") or [])],
        raw=news,
    )

    builder = SnapshotBuilder(trigger=trigger, snapshot_id=snap_id)

    bus.publish(PipelineEvent(type="news_received", payload={"path": str(path), "snapshot_id": snap_id}))

    cost_tracker = CostTracker(
        max_daily_cost=settings.max_daily_cost,
        max_cost_per_item=settings.max_cost_per_news_item,
        debug=settings.debug,
    )
    cost_tracker.reset_item()
    if settings.mock_llm:
        llm: object = MockLLMClient()
    else:
        llm = LLMClient(cost_tracker=cost_tracker)

    # --- Stage 1: Triage ---
    triage = run_triage(
        llm=llm,  # type: ignore[arg-type]
        provider=settings.triage_provider,
        model=settings.triage_model,
        knowledge=knowledge,
        news=news,
    )
    bus.publish(
        PipelineEvent(
            type="triage_decision",
            payload={
                "snapshot_id": snap_id,
                "action": triage.action,
                "confidence": triage.confidence,
                "symbols": triage.symbols,
                "reasoning": triage.reasoning,
            },
        )
    )

    if triage.skip_patterns_learned:
        knowledge.append_skip_keywords(triage.skip_patterns_learned)

    # --- Stage 2: Exploration (if investigate) ---
    if triage.action == "investigate":
        # Use triage-refined symbols (falls back to trigger symbols)
        symbols = triage.symbols if triage.symbols else trigger.symbols

        # Schwab context capture (optional via SCHWAB_DISABLED)
        market: SchwabMarketClient | None = None
        try:
            market = SchwabMarketClient()
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Schwab init failed (set SCHWAB_DISABLED=true to disable): {e}")
            market = None

        if market is not None and market.available and symbols:
            try:
                market.start_stream(symbols)
            except Exception as e:
                if DEBUG:
                    raise
                print(f"WARN: Schwab stream start failed: {e}")

        # Capture market/price context into the Snapshot
        if market is not None and market.available:
            try:
                builder.set_market_context(market.build_market_context())
                if symbols:
                    builder.set_price_context(market.build_price_context(symbols))
            except Exception as e:
                if DEBUG:
                    raise
                print(f"WARN: Schwab context capture failed: {e}")

        explore = explore_two_phase(
            llm=llm,  # type: ignore[arg-type]
            market=market,
            news=news,
            symbols=symbols,
            research_provider=settings.research_provider,
            research_model=settings.research_model,
            xsearch_provider=settings.xsearch_provider,
            xsearch_model=settings.xsearch_model,
            sentiment_provider=settings.sentiment_provider,
            sentiment_model=settings.sentiment_model,
            max_phase1_actions=settings.max_phase1_actions,
            max_phase2_branches=settings.max_phase2_branches,
            max_total_hops=settings.max_total_hops,
        )
        for trace in explore.traces:
            builder.add_tool_trace(trace)

        if market is not None:
            try:
                market.stop_stream()
            except Exception as e:
                if DEBUG:
                    raise
                print(f"WARN: Schwab stop stream failed: {e}")

    # --- Seal snapshot ---
    # Override cost total with the tracker's authoritative figure
    builder.set_cost_total(cost_tracker.item_spent)
    snapshot = builder.seal()

    # Persist to JSON file
    out_path = _snapshot_path(settings, snapshot.snapshot_id)
    snapshot.persist(out_path)

    # Persist to DB (idempotent)
    insert_snapshot(db, snapshot=snapshot.to_dict())

    bus.publish(
        PipelineEvent(type="snapshot_sealed", payload={"snapshot_id": snapshot.snapshot_id, "path": str(out_path)})
    )


# ---------------------------------------------------------------------------
# Watchdog + worker queue
# ---------------------------------------------------------------------------


class _NewsHandler(FileSystemEventHandler):
    """Enqueues new JSON file paths; does NOT process them inline."""

    def __init__(self, *, work_queue: queue.Queue[Path]) -> None:
        self._q = work_queue

    def on_created(self, event):  # type: ignore[override]
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".json":
            return
        # Small delay to let the writer finish flushing
        time.sleep(0.1)
        self._q.put(path)


def _worker_loop(
    *,
    work_queue: queue.Queue[Path],
    settings: Settings,
    db: Database,
    knowledge: KnowledgeStore,
    bus: EventBus,
) -> None:
    """Pull paths from the queue and process them one at a time."""
    while True:
        try:
            path = work_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            process_news_file(
                path=path,
                settings=settings,
                db=db,
                knowledge=knowledge,
                bus=bus,
            )
        except Exception as e:
            if DEBUG:
                raise
            print(f"ERROR processing {path}: {e}")
        finally:
            work_queue.task_done()


def run_watch_loop(
    *,
    settings: Settings,
    db: Database,
    knowledge: KnowledgeStore,
    bus: EventBus,
) -> None:
    """Start watchdog observer + worker thread, block forever."""
    import threading

    watch_dir = Path(settings.alpaca_output_dir)
    watch_dir.mkdir(parents=True, exist_ok=True)

    work_q: queue.Queue[Path] = queue.Queue()

    # Worker thread processes items from the queue
    worker = threading.Thread(
        target=_worker_loop,
        kwargs={
            "work_queue": work_q,
            "settings": settings,
            "db": db,
            "knowledge": knowledge,
            "bus": bus,
        },
        daemon=True,
    )
    worker.start()

    handler = _NewsHandler(work_queue=work_q)
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
