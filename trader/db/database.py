"""SQLite database helpers.

Phase 1 uses SQLite for fast local iteration (no docker required).
We can add Postgres later while keeping the same logical tables.

Snapshot inserts use INSERT OR IGNORE so that re-processing the same
news file (deterministic snapshot_id) is idempotent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, create_engine, text
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


watches_table = Table(
    "watches",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("watch_id", String, unique=True, nullable=False),
    Column("symbol", String, nullable=False),
    Column("status", String, nullable=False),
    Column("entry_snapshot_id", String, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("watch_json", JSON, nullable=False),
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


def snapshot_exists(db: Database, snapshot_id: str) -> bool:
    """Check whether a snapshot with this ID already exists in the DB."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM snapshots WHERE snapshot_id = :sid LIMIT 1"),
            {"sid": snapshot_id},
        ).fetchone()
    return row is not None


def insert_snapshot(db: Database, *, snapshot: dict[str, Any]) -> bool:
    """Insert a snapshot. Returns True if inserted, False if duplicate (idempotent).

    Uses INSERT OR IGNORE so re-processing the same news file is safe.
    """
    trigger = snapshot.get("trigger") or {}
    symbols = trigger.get("symbols") or []
    symbols_str = ",".join(symbols)

    with db.engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT OR IGNORE INTO snapshots (snapshot_id, trigger_type, symbols, snapshot_json) "
                "VALUES (:sid, :ttype, :syms, :sjson)"
            ),
            {
                "sid": snapshot["snapshot_id"],
                "ttype": str(trigger.get("type") or ""),
                "syms": symbols_str,
                "sjson": json.dumps(snapshot, ensure_ascii=False),
            },
        )
    return result.rowcount > 0


# ---------------------------------------------------------------------------
# Watches
# ---------------------------------------------------------------------------


def insert_watch(db: Database, *, watch: dict[str, Any]) -> bool:
    """Insert a watch. Returns True if inserted, False if duplicate."""
    entry = watch.get("entry") or {}
    with db.engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT OR IGNORE INTO watches "
                "(watch_id, symbol, status, entry_snapshot_id, watch_json) "
                "VALUES (:wid, :sym, :status, :esid, :wjson)"
            ),
            {
                "wid": watch["watch_id"],
                "sym": watch["symbol"],
                "status": watch["status"],
                "esid": entry.get("snapshot_id", ""),
                "wjson": json.dumps(watch, ensure_ascii=False),
            },
        )
    return result.rowcount > 0


def update_watch(db: Database, watch_id: str, watch: dict[str, Any]) -> None:
    """Update a watch's JSON, status, and updated_at timestamp."""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE watches SET status = :status, watch_json = :wjson, "
                "updated_at = CURRENT_TIMESTAMP WHERE watch_id = :wid"
            ),
            {
                "wid": watch_id,
                "status": watch["status"],
                "wjson": json.dumps(watch, ensure_ascii=False),
            },
        )


def get_watch(db: Database, watch_id: str) -> dict[str, Any] | None:
    """Fetch a single watch by ID. Returns the parsed JSON or None."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT watch_json FROM watches WHERE watch_id = :wid"),
            {"wid": watch_id},
        ).fetchone()
    if row is None:
        return None
    raw = row[0]
    return json.loads(raw) if isinstance(raw, str) else raw


def count_holding_watches(db: Database) -> int:
    """Count watches currently in 'holding' status."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM watches WHERE status = 'holding'"),
        ).fetchone()
    return row[0] if row else 0


def get_active_watches(db: Database) -> list[dict[str, Any]]:
    """Fetch all watches that are not yet sealed."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT watch_json FROM watches "
                "WHERE status IN ('holding', 'exited', 'retrospective') "
                "ORDER BY created_at"
            ),
        ).fetchall()
    result = []
    for row in rows:
        raw = row[0]
        result.append(json.loads(raw) if isinstance(raw, str) else raw)
    return result
