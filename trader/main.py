"""Entry point.

Starts:
- Online orchestrator (watch loop) in a background thread
- FastAPI dashboard (uvicorn)

Run:
  uv run python -m trader.main
"""

from __future__ import annotations

import threading

import uvicorn

from trader.config import load_settings
from trader.db.database import open_sqlite
from trader.knowledge.store import KnowledgeStore
from trader.online.orchestrator import EventBus, run_watch_loop
from trader.web.app import create_app


def main() -> None:
    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)
    knowledge = KnowledgeStore(root_dir=__import__("pathlib").Path(settings.data_dir))
    knowledge.ensure_defaults()

    bus = EventBus()
    t = threading.Thread(
        target=run_watch_loop,
        kwargs={"settings": settings, "db": db, "knowledge": knowledge, "bus": bus},
        daemon=True,
    )
    t.start()

    app = create_app(settings=settings, bus=bus)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
