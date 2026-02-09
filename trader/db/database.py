"""SQLite database helpers.

Phase 1 uses SQLite for fast local iteration (no docker required).
We can add Postgres later while keeping the same logical tables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.sql import func


metadata = MetaData()


snapshots_table = Table(
    "snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("snapshot_id", String, unique=True, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("trigger_type", String, nullable=False),
    Column("symbols", String, nullable=False),  # comma-separated for easy filtering
    Column("snapshot_json", JSON, nullable=False),
)


@dataclass(frozen=True)
class Database:
    engine: Engine


def open_sqlite(path: str) -> Database:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{p}")
    metadata.create_all(engine)
    return Database(engine=engine)


def insert_snapshot(db: Database, *, snapshot: dict[str, Any]) -> None:
    trigger = snapshot.get("trigger") or {}
    symbols = trigger.get("symbols") or []
    symbols_str = ",".join(symbols)
    with db.engine.begin() as conn:
        conn.execute(
            snapshots_table.insert().values(
                snapshot_id=snapshot["snapshot_id"],
                trigger_type=str(trigger.get("type") or ""),
                symbols=symbols_str,
                snapshot_json=json.loads(json.dumps(snapshot)),
            )
        )
