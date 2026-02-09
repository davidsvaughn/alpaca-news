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
from trader.db.database import open_sqlite
from trader.knowledge.store import KnowledgeStore
from trader.online.orchestrator import EventBus, process_news_file, run_watch_loop
from trader.web.app import create_app


def main() -> None:
    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)
    knowledge = KnowledgeStore(root_dir=Path(settings.data_dir))
    knowledge.ensure_defaults()

    bus = EventBus()
    t = threading.Thread(
        target=run_watch_loop,
        kwargs={"settings": settings, "db": db, "knowledge": knowledge, "bus": bus},
        daemon=True,
    )
    t.start()

    # Optional: process last N existing files on startup
    if settings.backfill_on_start:
        root = Path(settings.alpaca_output_dir)
        files = sorted([p for p in root.glob("*.json") if p.is_file()])
        for p in files[-settings.backfill_limit :]:
            process_news_file(path=p, settings=settings, db=db, knowledge=knowledge, bus=bus)

    app = create_app(settings=settings, bus=bus)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
