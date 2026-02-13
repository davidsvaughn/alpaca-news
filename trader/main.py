"""Entry point.

Starts:
- Online orchestrator (watch loop) in a background thread
- FastAPI dashboard (uvicorn)

Run:
  uv run python -m trader.main
"""

from __future__ import annotations

import threading
from pathlib import Path

import uvicorn

from trader.config import load_settings
from trader.db.database import insert_event, open_sqlite, prune_old_events
from trader.knowledge.store import KnowledgeStore
from trader.online.event_bus import EventBus, PipelineEvent
from trader.online.orchestrator import process_news_file, run_watch_loop
from trader.online.x_stream_service import XStreamGuards, XStreamService
from trader.web.app import create_app


def main() -> None:
    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)
    knowledge = KnowledgeStore(root_dir=Path(settings.data_dir))
    knowledge.ensure_defaults()

    bus = EventBus()

    # Persist events to SQLite (survives restarts + tab navigation)
    def _persist_event(evt: PipelineEvent) -> None:
        try:
            insert_event(db, event_type=evt.type, payload=evt.payload)
        except Exception:
            pass  # Don't let persistence failures break the pipeline

    bus.subscribe(_persist_event)
    prune_old_events(db, keep_days=7)

    xstream: XStreamService | None = None
    if settings.x_stream_enabled and settings.x_stream_mode != "off":
        # Conservative guardrails by default
        xstream = XStreamService(
            bus=bus,
            data_dir=Path(settings.data_dir),
            guards=XStreamGuards(
                max_posts_per_day=settings.x_max_posts_per_day,
                max_bursts_per_day=settings.x_max_bursts_per_day,
                burst_ttl_minutes=settings.x_burst_ttl_minutes,
                usage_poll_interval_s=settings.x_usage_poll_interval_s,
            ),
            enabled=True,
        )
        bus.publish(
            PipelineEvent(
                type="x_stream_configured",
                payload={
                    "enabled": True,
                    "mode": settings.x_stream_mode,
                    "burst_ttl_minutes": settings.x_burst_ttl_minutes,
                    "max_bursts_per_day": settings.x_max_bursts_per_day,
                    "max_posts_per_day": settings.x_max_posts_per_day,
                },
            )
        )
    t = threading.Thread(
        target=run_watch_loop,
        kwargs={"settings": settings, "db": db, "knowledge": knowledge, "bus": bus, "xstream": xstream},
        daemon=True,
    )
    t.start()

    # Optional: process last N existing files on startup (background thread)
    if settings.backfill_on_start:
        def _backfill():
            root = Path(settings.alpaca_output_dir)
            files = sorted([p for p in root.glob("*.json") if p.is_file()])
            targets = files[-settings.backfill_limit :]
            print(f"Backfill: processing {len(targets)} files in background...")
            ok = 0
            for i, p in enumerate(targets, 1):
                print(f"Backfill [{i}/{len(targets)}]: {p.name}")
                try:
                    process_news_file(path=p, settings=settings, db=db, knowledge=knowledge, bus=bus, xstream=None)
                    ok += 1
                except Exception as exc:
                    print(f"Backfill [{i}/{len(targets)}] FAILED: {exc}")
                    bus.publish(PipelineEvent(type="manual_explore_error", payload={
                        "headline": p.name, "error": f"Backfill failed: {exc}",
                    }))
            print(f"Backfill complete: {ok}/{len(targets)} succeeded.")

        threading.Thread(target=_backfill, daemon=True).start()

    app = create_app(settings=settings, bus=bus, db=db, knowledge=knowledge)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
