"""Backfill processor for existing Alpaca news files.

Usage:
  uv run python -m trader.online.backfill --limit 5
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trader.config import load_settings
from trader.db.database import open_sqlite
from trader.knowledge.store import KnowledgeStore
from trader.online.event_bus import EventBus
from trader.online.orchestrator import process_news_file


def iter_news_files(root: Path) -> list[Path]:
    return sorted([p for p in root.glob("*.json") if p.is_file()])


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill process existing output/alpaca/*.json into snapshots")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N files (default from BACKFILL_LIMIT)")
    args = parser.parse_args()

    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)
    knowledge = KnowledgeStore(root_dir=Path(settings.data_dir))
    knowledge.ensure_defaults()
    bus = EventBus()

    root = Path(settings.alpaca_output_dir)
    files = iter_news_files(root)
    limit = args.limit if args.limit is not None else settings.backfill_limit
    for p in files[-limit:]:
        process_news_file(path=p, settings=settings, db=db, knowledge=knowledge, bus=bus)

    print(f"Backfill complete. Processed up to {limit} files.")


if __name__ == "__main__":
    main()
