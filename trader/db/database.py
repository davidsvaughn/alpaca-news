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


event_log_table = Table(
    "event_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("event_type", String, nullable=False),
    Column("payload_json", JSON, nullable=False),
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


evaluations_table = Table(
    "evaluations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("evaluation_id", String, unique=True, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("snapshot_ids", String, nullable=False),  # comma-separated
    Column("evaluation_json", JSON, nullable=False),
)


follow_ups_table = Table(
    "follow_ups",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("follow_up_id", String, unique=True, nullable=False),
    Column("snapshot_id", String, nullable=False),
    Column("symbols", String, nullable=False),       # comma-separated
    Column("reason", String, nullable=False),         # "no_buy" | "post_exit"
    Column("status", String, nullable=False),         # "active" | "complete"
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("follow_up_json", JSON, nullable=False),
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


def delete_snapshot(db: Database, snapshot_id: str) -> bool:
    """Delete a snapshot by ID. Returns True if a row was deleted."""
    with db.engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM snapshots WHERE snapshot_id = :sid"),
            {"sid": snapshot_id},
        )
    return result.rowcount > 0


def is_mock_snapshot(db: Database, snapshot_id: str) -> bool:
    """Check if a snapshot was created with MOCK_LLM (model='test' in rounds)."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT snapshot_json FROM snapshots WHERE snapshot_id = :sid"),
            {"sid": snapshot_id},
        ).fetchone()
    if row is None:
        return False
    data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    rounds = data.get("rounds") or []
    return any(r.get("model") == "test" for r in rounds)


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


def get_watch_by_snapshot(db: Database, snapshot_id: str) -> dict[str, Any] | None:
    """Fetch a watch linked to the given entry_snapshot_id."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT watch_json FROM watches WHERE entry_snapshot_id = :sid LIMIT 1"),
            {"sid": snapshot_id},
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


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------


def insert_follow_up(db: Database, *, follow_up: dict[str, Any]) -> bool:
    """Insert a follow-up. Returns True if inserted, False if duplicate."""
    with db.engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT OR IGNORE INTO follow_ups "
                "(follow_up_id, snapshot_id, symbols, reason, status, follow_up_json) "
                "VALUES (:fuid, :sid, :syms, :reason, :status, :fujson)"
            ),
            {
                "fuid": follow_up["follow_up_id"],
                "sid": follow_up["snapshot_id"],
                "syms": ",".join(follow_up.get("symbols", [])),
                "reason": follow_up["reason"],
                "status": follow_up["status"],
                "fujson": json.dumps(follow_up, ensure_ascii=False),
            },
        )
    return result.rowcount > 0


def update_follow_up(db: Database, follow_up_id: str, follow_up: dict[str, Any]) -> None:
    """Update a follow-up's JSON, status, and updated_at."""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE follow_ups SET status = :status, follow_up_json = :fujson, "
                "updated_at = CURRENT_TIMESTAMP WHERE follow_up_id = :fuid"
            ),
            {
                "fuid": follow_up_id,
                "status": follow_up["status"],
                "fujson": json.dumps(follow_up, ensure_ascii=False),
            },
        )


def get_follow_up(db: Database, follow_up_id: str) -> dict[str, Any] | None:
    """Fetch a single follow-up by ID."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT follow_up_json FROM follow_ups WHERE follow_up_id = :fuid"),
            {"fuid": follow_up_id},
        ).fetchone()
    if row is None:
        return None
    raw = row[0]
    return json.loads(raw) if isinstance(raw, str) else raw


def get_active_follow_ups(db: Database) -> list[dict[str, Any]]:
    """Fetch all follow-ups in 'active' status."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT follow_up_json FROM follow_ups "
                "WHERE status = 'active' ORDER BY created_at"
            ),
        ).fetchall()
    return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]


def get_follow_ups_by_snapshot(db: Database, snapshot_id: str) -> list[dict[str, Any]]:
    """Fetch all follow-ups linked to a snapshot."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT follow_up_json FROM follow_ups WHERE snapshot_id = :sid"),
            {"sid": snapshot_id},
        ).fetchall()
    return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]


def get_all_follow_ups(
    db: Database, *, status: str | None = None, limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    """Fetch follow-ups ordered by created_at DESC, with optional status filter."""
    if status:
        sql = (
            "SELECT follow_up_json FROM follow_ups WHERE status = :status "
            "ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        )
        params: dict[str, Any] = {"status": status, "lim": limit, "off": offset}
    else:
        sql = "SELECT follow_up_json FROM follow_ups ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        params = {"lim": limit, "off": offset}
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]


def count_active_follow_ups(db: Database) -> int:
    """Count follow-ups currently in 'active' status."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM follow_ups WHERE status = 'active'"),
        ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Watch stats
# ---------------------------------------------------------------------------


def count_watches_by_status(db: Database) -> dict[str, int]:
    """Return watch counts grouped by status, e.g. {'holding': 2, 'sealed': 5}."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status, COUNT(*) FROM watches GROUP BY status"),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def count_snapshots_today(db: Database) -> int:
    """Count snapshots created today (UTC)."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM snapshots WHERE date(created_at) = date('now')"),
        ).fetchone()
    return row[0] if row else 0


def get_daily_cost_today(db: Database) -> float:
    """Sum cost_summary.total_usd from today's snapshots."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT snapshot_json FROM snapshots WHERE date(created_at) = date('now')"),
        ).fetchall()
    total = 0.0
    for row in rows:
        raw = row[0]
        snap = json.loads(raw) if isinstance(raw, str) else raw
        cost = snap.get("cost_summary", {})
        total += float(cost.get("total_usd", 0.0))
    return total


def get_daily_cost_history(db: Database, *, days: int = 30) -> list[dict[str, Any]]:
    """Return daily cost summary for the last N days.

    Returns list of dicts: [{"date": "2026-02-10", "total_usd": 1.23, "count": 5, "by_tool": {...}}]
    """
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT date(created_at) as day, snapshot_json FROM snapshots "
                "WHERE date(created_at) >= date('now', :offset) "
                "ORDER BY day"
            ),
            {"offset": f"-{days} days"},
        ).fetchall()

    daily: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = row[0]
        raw = row[1]
        snap = json.loads(raw) if isinstance(raw, str) else raw
        cost = snap.get("cost_summary", {})

        if day not in daily:
            daily[day] = {"date": day, "total_usd": 0.0, "count": 0, "by_tool": {}}
        entry = daily[day]
        entry["total_usd"] += float(cost.get("total_usd", 0.0))
        entry["count"] += 1
        for tool, amt in cost.get("by_tool", {}).items():
            entry["by_tool"][tool] = entry["by_tool"].get(tool, 0.0) + float(amt)

    return list(daily.values())


# ---------------------------------------------------------------------------
# Dashboard listing queries
# ---------------------------------------------------------------------------


def _parse_rows(rows: list, col: int = 0) -> list[dict[str, Any]]:
    """Parse JSON blobs from a list of rows."""
    result = []
    for row in rows:
        raw = row[col]
        result.append(json.loads(raw) if isinstance(raw, str) else raw)
    return result


def get_all_watches(
    db: Database, *, status: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """Fetch watches ordered by created_at DESC, with optional status filter."""
    if status:
        sql = (
            "SELECT watch_json FROM watches WHERE status = :status "
            "ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        )
        params: dict[str, Any] = {"status": status, "lim": limit, "off": offset}
    else:
        sql = "SELECT watch_json FROM watches ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        params = {"lim": limit, "off": offset}
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return _parse_rows(rows)


def count_all_watches(db: Database, *, status: str | None = None) -> int:
    """Count watches, optionally filtered by status."""
    if status:
        sql = "SELECT COUNT(*) FROM watches WHERE status = :status"
        params: dict[str, Any] = {"status": status}
    else:
        sql = "SELECT COUNT(*) FROM watches"
        params = {}
    with db.engine.connect() as conn:
        row = conn.execute(text(sql), params).fetchone()
    return row[0] if row else 0


def get_all_snapshots(
    db: Database, *, symbol: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """Fetch snapshots ordered by created_at DESC, with optional symbol filter."""
    if symbol:
        sql = (
            "SELECT snapshot_json FROM snapshots WHERE symbols LIKE :sym "
            "ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        )
        params: dict[str, Any] = {"sym": f"%{symbol}%", "lim": limit, "off": offset}
    else:
        sql = "SELECT snapshot_json FROM snapshots ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        params = {"lim": limit, "off": offset}
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return _parse_rows(rows)


def count_snapshots(db: Database, *, symbol: str | None = None) -> int:
    """Count snapshots, optionally filtered by symbol."""
    if symbol:
        sql = "SELECT COUNT(*) FROM snapshots WHERE symbols LIKE :sym"
        params: dict[str, Any] = {"sym": f"%{symbol}%"}
    else:
        sql = "SELECT COUNT(*) FROM snapshots"
        params = {}
    with db.engine.connect() as conn:
        row = conn.execute(text(sql), params).fetchone()
    return row[0] if row else 0


def get_snapshot(db: Database, snapshot_id: str) -> dict[str, Any] | None:
    """Fetch a single snapshot by ID. Returns parsed JSON or None."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT snapshot_json FROM snapshots WHERE snapshot_id = :sid"),
            {"sid": snapshot_id},
        ).fetchone()
    if row is None:
        return None
    raw = row[0]
    return json.loads(raw) if isinstance(raw, str) else raw


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def insert_event(db: Database, *, event_type: str, payload: dict[str, Any]) -> None:
    """Persist a pipeline event to the event_log table."""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event_log (event_type, payload_json) VALUES (:etype, :pjson)"
            ),
            {"etype": event_type, "pjson": json.dumps(payload, ensure_ascii=False)},
        )


def get_recent_events(db: Database, *, limit: int = 200) -> list[dict[str, Any]]:
    """Return recent events, newest first."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type, payload_json, created_at "
                "FROM event_log ORDER BY id DESC LIMIT :lim"
            ),
            {"lim": limit},
        ).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        ts = row[2]
        result.append({"type": row[0], "payload": payload, "ts": str(ts) if ts else None})
    return result


def prune_old_events(db: Database, *, keep_days: int = 7) -> int:
    """Delete events older than keep_days. Returns rows deleted."""
    with db.engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM event_log WHERE created_at < datetime('now', :offset)"),
            {"offset": f"-{keep_days} days"},
        )
    return result.rowcount


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


def insert_evaluation(
    db: Database,
    *,
    evaluation_id: str,
    snapshot_ids: list[str],
    evaluation: dict[str, Any],
) -> None:
    """Persist an LLM evaluation result."""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO evaluations (evaluation_id, snapshot_ids, evaluation_json) "
                "VALUES (:eid, :sids, :ejson)"
            ),
            {
                "eid": evaluation_id,
                "sids": ",".join(snapshot_ids),
                "ejson": json.dumps(evaluation, ensure_ascii=False),
            },
        )


def get_recent_evaluations(db: Database, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent evaluations, newest first."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT evaluation_id, snapshot_ids, evaluation_json, created_at "
                "FROM evaluations ORDER BY id DESC LIMIT :lim"
            ),
            {"lim": limit},
        ).fetchall()
    result = []
    for row in rows:
        ejson = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        result.append({
            "evaluation_id": row[0],
            "snapshot_ids": row[1].split(",") if row[1] else [],
            "evaluation": ejson,
            "created_at": str(row[3]) if row[3] else None,
        })
    return result
