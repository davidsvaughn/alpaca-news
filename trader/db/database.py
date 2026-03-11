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

from sqlalchemy import JSON, Column, DateTime, Float, Integer, MetaData, String, Table, create_engine, text
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


alpaca_transactions_table = Table(
    "alpaca_transactions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("account_id", String, nullable=False),      # e.g. "PA31QXNAPB1H"
    Column("event", String, nullable=False),            # buy, sell, stop, cancel, fill, reject, reconcile, error
    Column("symbol", String, nullable=False),
    Column("order_id", String),                         # Alpaca order ID (if applicable)
    Column("status", String),                           # filled, rejected, canceled, timeout, etc.
    Column("detail_json", JSON, nullable=False),        # full request/response data
)


live_configs_table = Table(
    "live_configs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("config_id", String, unique=True, nullable=False),
    Column("name", String, nullable=False),
    Column("active", Integer, nullable=False, server_default="0"),  # 0/1 boolean
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("config_json", JSON, nullable=False),
)


equity_snapshots_table = Table(
    "portfolio_equity_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("config_id", String, nullable=False),           # links to live_configs.config_id
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("equity", Float, nullable=False),               # total portfolio value
    Column("cash", Float, nullable=False),                 # uninvested capital
    Column("unrealized_pnl", Float, nullable=False),       # open position P&L
    Column("realized_pnl", Float, nullable=False),         # cumulative closed P&L
    Column("position_count", Integer, nullable=False),     # number of holdings
    Column("source", String, nullable=False),              # "backfill" | "live" | "alpaca"
)


@dataclass(frozen=True)
class Database:
    engine: Engine


def open_sqlite(path: str) -> Database:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{p}")
    metadata.create_all(engine)
    # Hot query paths in the dashboard/backtest rely on created_at ordering and
    # symbol filtering. Ensure indexes exist for existing DBs as well.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_created_at "
            "ON snapshots(created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_symbols "
            "ON snapshots(symbols)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_equity_snapshots_config_ts "
            "ON portfolio_equity_snapshots(config_id, timestamp)"
        ))
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


def delete_snapshots_bulk(db: Database, snapshot_ids: list[str]) -> int:
    """Delete multiple snapshots and their linked follow-ups/watches. Returns count deleted."""
    if not snapshot_ids:
        return 0
    placeholders = ",".join(f":sid{i}" for i in range(len(snapshot_ids)))
    params = {f"sid{i}": sid for i, sid in enumerate(snapshot_ids)}
    with db.engine.begin() as conn:
        # Delete linked follow-ups
        conn.execute(text(f"DELETE FROM follow_ups WHERE snapshot_id IN ({placeholders})"), params)
        # Delete linked watches
        conn.execute(text(f"DELETE FROM watches WHERE entry_snapshot_id IN ({placeholders})"), params)
        # Delete snapshots
        result = conn.execute(text(f"DELETE FROM snapshots WHERE snapshot_id IN ({placeholders})"), params)
    return result.rowcount


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


def update_snapshot_field(db: Database, snapshot_id: str, field: str, value: Any) -> bool:
    """Update a single field inside snapshot_json. Returns True if updated."""
    with db.engine.begin() as conn:
        row = conn.execute(
            text("SELECT snapshot_json FROM snapshots WHERE snapshot_id = :sid"),
            {"sid": snapshot_id},
        ).fetchone()
        if row is None:
            return False
        snap = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        snap[field] = value
        conn.execute(
            text("UPDATE snapshots SET snapshot_json = :sjson WHERE snapshot_id = :sid"),
            {"sid": snapshot_id, "sjson": json.dumps(snap, ensure_ascii=False)},
        )
    return True


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


def count_holding_watches(db: Database, *, live_config_id: str | None = None) -> int:
    """Count watches currently in 'holding' status.

    Excludes fractional dust (qty < 1 share) — these are leftover remnants
    from extended-hours sells and should not block new position slots.

    If live_config_id is provided, counts only watches for that portfolio.
    """
    # Watches without qty (legacy) or with qty >= 1 count as real positions.
    # Watches with qty < 1 are fractional dust and are excluded.
    dust = ("AND (json_extract(watch_json, '$.qty') IS NULL "
            "OR json_extract(watch_json, '$.qty') >= 1)")
    if live_config_id:
        with db.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM watches WHERE status = 'holding' "
                    "AND json_extract(watch_json, '$.live_config_id') = :cid "
                    + dust
                ),
                {"cid": live_config_id},
            ).fetchone()
    else:
        with db.engine.connect() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) FROM watches WHERE status = 'holding' " + dust),
            ).fetchone()
    return row[0] if row else 0


def get_active_watches(db: Database) -> list[dict[str, Any]]:
    """Fetch all watches that are not yet sealed."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT watch_json FROM watches "
                "WHERE status IN ('holding', 'exited', 'retrospective', 'cooling_off') "
                "ORDER BY created_at"
            ),
        ).fetchall()
    result = []
    for row in rows:
        raw = row[0]
        result.append(json.loads(raw) if isinstance(raw, str) else raw)
    return result


# ---------------------------------------------------------------------------
# Live configs
# ---------------------------------------------------------------------------


def _normalize_live_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize live config payload fields for backward compatibility."""
    out = dict(config)
    alloc = out.get("allocation_params")
    if isinstance(alloc, dict):
        alloc_out = dict(alloc)
        # Legacy alias: rank_method=momentum -> unreal_pl
        if str(alloc_out.get("rank_method", "")).strip().lower() == "momentum":
            alloc_out["rank_method"] = "unreal_pl"
        out["allocation_params"] = alloc_out
    return out


def insert_live_config(db: Database, *, config: dict[str, Any]) -> bool:
    """Insert a live config. Returns True if inserted, False if duplicate."""
    config = _normalize_live_config(config)
    with db.engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT OR IGNORE INTO live_configs "
                "(config_id, name, active, config_json) "
                "VALUES (:cid, :name, :active, :cjson)"
            ),
            {
                "cid": config["config_id"],
                "name": config["name"],
                "active": 1 if config.get("active") else 0,
                "cjson": json.dumps(config, ensure_ascii=False),
            },
        )
    return result.rowcount > 0


def update_live_config(db: Database, config_id: str, config: dict[str, Any]) -> None:
    """Update a live config's JSON, name, active flag, and updated_at."""
    config = _normalize_live_config(config)
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE live_configs SET name = :name, active = :active, "
                "config_json = :cjson, updated_at = CURRENT_TIMESTAMP "
                "WHERE config_id = :cid"
            ),
            {
                "cid": config_id,
                "name": config["name"],
                "active": 1 if config.get("active") else 0,
                "cjson": json.dumps(config, ensure_ascii=False),
            },
        )


def get_live_config(db: Database, config_id: str) -> dict[str, Any] | None:
    """Fetch a single live config by ID."""
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT config_json FROM live_configs WHERE config_id = :cid"),
            {"cid": config_id},
        ).fetchone()
    if row is None:
        return None
    raw = row[0]
    cfg = json.loads(raw) if isinstance(raw, str) else raw
    return _normalize_live_config(cfg)


def get_active_live_config(db: Database) -> dict[str, Any] | None:
    """Fetch any active live config (returns first if multiple). For single-config compat."""
    configs = get_active_live_configs(db)
    return configs[0] if configs else None


def get_active_live_configs(db: Database) -> list[dict[str, Any]]:
    """Fetch all active live configs (supports multiple portfolios)."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT config_json FROM live_configs WHERE active = 1 ORDER BY created_at"),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        out.append(_normalize_live_config(cfg))
    return out


def get_all_live_configs(db: Database) -> list[dict[str, Any]]:
    """Fetch all live configs ordered by created_at DESC."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT config_json FROM live_configs ORDER BY created_at DESC"),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        out.append(_normalize_live_config(cfg))
    return out


def activate_live_config(db: Database, config_id: str) -> bool:
    """Activate a config (additive — does NOT deactivate others).

    Multiple configs can be active simultaneously for multi-portfolio support.
    """
    with db.engine.begin() as conn:
        result = conn.execute(
            text("UPDATE live_configs SET active = 1, updated_at = CURRENT_TIMESTAMP WHERE config_id = :cid"),
            {"cid": config_id},
        )
        if result.rowcount > 0:
            row = conn.execute(
                text("SELECT config_json FROM live_configs WHERE config_id = :cid"),
                {"cid": config_id},
            ).fetchone()
            if row:
                cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                cfg["active"] = True
                cfg = _normalize_live_config(cfg)
                conn.execute(
                    text("UPDATE live_configs SET config_json = :cjson WHERE config_id = :cid"),
                    {"cid": config_id, "cjson": json.dumps(cfg, ensure_ascii=False)},
                )
    return result.rowcount > 0


def deactivate_live_config(db: Database, config_id: str) -> bool:
    """Deactivate a specific config. Returns True if config exists."""
    with db.engine.begin() as conn:
        result = conn.execute(
            text("UPDATE live_configs SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE config_id = :cid"),
            {"cid": config_id},
        )
        if result.rowcount > 0:
            row = conn.execute(
                text("SELECT config_json FROM live_configs WHERE config_id = :cid"),
                {"cid": config_id},
            ).fetchone()
            if row:
                cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                cfg["active"] = False
                cfg = _normalize_live_config(cfg)
                conn.execute(
                    text("UPDATE live_configs SET config_json = :cjson WHERE config_id = :cid"),
                    {"cid": config_id, "cjson": json.dumps(cfg, ensure_ascii=False)},
                )
    return result.rowcount > 0


def delete_live_config(db: Database, config_id: str) -> bool:
    """Delete a live config. Returns True if deleted."""
    with db.engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM live_configs WHERE config_id = :cid"),
            {"cid": config_id},
        )
    return result.rowcount > 0


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


def get_recently_explored_symbols(
    db: Database,
    *,
    lookback_minutes: int,
) -> dict[str, str]:
    """Return symbols explored in the last N minutes mapped to most-recent timestamp.

    Exploration is defined as snapshots with triage action "investigate" and
    at least one agent round recorded.
    """
    if lookback_minutes <= 0:
        return {}

    recent: dict[str, str] = {}
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT created_at, snapshot_json FROM snapshots "
                "WHERE created_at >= datetime('now', :offset) "
                "ORDER BY created_at DESC"
            ),
            {"offset": f"-{lookback_minutes} minutes"},
        ).fetchall()

    for row in rows:
        created_at = str(row[0]) if row[0] else ""
        snap = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        triage = snap.get("triage") or {}
        if triage.get("action") != "investigate":
            continue
        if not (snap.get("rounds") or []):
            continue
        explored_symbols = triage.get("symbols") or (snap.get("trigger") or {}).get("symbols") or []
        for sym in explored_symbols:
            key = str(sym).strip().upper()
            if key and key not in recent:
                recent[key] = created_at
    return recent


def _sum_watch_checkin_costs(watch_rows: list, date_filter: str | None = None) -> float:
    """Sum cost_usd from watch checkin_history entries, optionally filtering by date."""
    total = 0.0
    for row in watch_rows:
        watch = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        for ci in watch.get("checkin_history", []):
            cost = ci.get("cost_usd", 0.0)
            if not cost:
                continue
            if date_filter:
                ci_time = ci.get("time", "")
                if not ci_time.startswith(date_filter):
                    continue
            total += float(cost)
    return total


def _sum_follow_up_costs(fu_rows: list, date_filter: str | None = None) -> float:
    """Sum cost_usd from follow-up collections, optionally filtering by date."""
    total = 0.0
    for row in fu_rows:
        fu = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if date_filter:
            # Sum per-collection costs for matching dates
            for coll in fu.get("collections", []):
                collected_at = coll.get("collected_at", "")
                if collected_at.startswith(date_filter):
                    total += float(coll.get("cost_usd", 0.0))
        else:
            total += float(fu.get("total_cost_usd", 0.0))
    return total


def get_daily_cost_today(db: Database) -> float:
    """Sum all costs from today: snapshots + watch check-ins + follow-ups."""
    from datetime import date as _date
    today_str = _date.today().isoformat()  # "2026-02-19"

    total = 0.0
    with db.engine.connect() as conn:
        # Snapshot costs
        for row in conn.execute(
            text("SELECT snapshot_json FROM snapshots WHERE date(created_at) = date('now')"),
        ).fetchall():
            snap = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            total += float(snap.get("cost_summary", {}).get("total_usd", 0.0))

        # Watch check-in costs (filter individual entries by today's date)
        watch_rows = conn.execute(
            text("SELECT watch_json FROM watches WHERE date(updated_at) = date('now')"),
        ).fetchall()
        total += _sum_watch_checkin_costs(watch_rows, date_filter=today_str)

        # Follow-up costs (filter individual collections by today's date)
        fu_rows = conn.execute(
            text("SELECT follow_up_json FROM follow_ups WHERE date(updated_at) = date('now')"),
        ).fetchall()
        total += _sum_follow_up_costs(fu_rows, date_filter=today_str)

    return total


def _provider_from_model(model: str) -> str:
    """Heuristic: map a model name to a provider key."""
    m = model.lower()
    if "gemini" in m:
        return "gemini"
    if "grok" in m:
        return "grok"
    if any(k in m for k in ("gpt", "o3", "o4")):
        return "openai"
    return ""


def get_daily_cost_today_by_provider(db: Database) -> dict[str, float]:
    """Sum today's costs by provider (openai, grok, gemini).

    Includes pipeline rounds, triage, follow-up searches, and watch check-ins.
    """
    from datetime import date as _date

    totals: dict[str, float] = {"openai": 0.0, "grok": 0.0, "gemini": 0.0}
    today_str = _date.today().isoformat()

    with db.engine.connect() as conn:
        # --- Snapshots: pipeline rounds + triage ---
        snap_rows = conn.execute(
            text("SELECT snapshot_json FROM snapshots WHERE date(created_at) = date('now')"),
        ).fetchall()

        for row in snap_rows:
            snap = json.loads(row[0]) if isinstance(row[0], str) else row[0]

            # Pipeline agent rounds
            for rnd in snap.get("rounds", []) or []:
                provider = str(rnd.get("agent") or "").strip().lower()
                cost = float(rnd.get("cost_usd", 0.0) or 0.0)
                if provider in totals:
                    totals[provider] += cost

            # Triage cost (attributed to its provider)
            triage = snap.get("triage") or {}
            triage_cost = float(triage.get("cost_usd", 0.0) or 0.0)
            if triage_cost > 0:
                triage_provider = str(triage.get("provider", "")).strip().lower()
                if triage_provider in totals:
                    totals[triage_provider] += triage_cost

        # --- Follow-up search costs (always use Grok / xAI API) ---
        fu_rows = conn.execute(
            text("SELECT follow_up_json FROM follow_ups WHERE date(updated_at) = date('now')"),
        ).fetchall()
        for row in fu_rows:
            fu = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            for coll in fu.get("collections", []):
                if not coll.get("collected_at", "").startswith(today_str):
                    continue
                fu_cost = float(coll.get("cost_usd", 0.0))
                if fu_cost > 0:
                    totals["grok"] += fu_cost

        # --- Watch check-in costs (attributed by model name) ---
        watch_rows = conn.execute(
            text("SELECT watch_json FROM watches WHERE date(updated_at) = date('now')"),
        ).fetchall()
        for row in watch_rows:
            watch = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            for ci in watch.get("checkin_history", []):
                ci_cost = float(ci.get("cost_usd", 0.0) or 0.0)
                if ci_cost <= 0:
                    continue
                if not ci.get("time", "").startswith(today_str):
                    continue
                prov = _provider_from_model(str(ci.get("model", "")))
                if prov in totals:
                    totals[prov] += ci_cost
                else:
                    totals["gemini"] += ci_cost  # default for watch check-ins

    return {k: round(v, 6) for k, v in totals.items()}


def get_daily_cost_history(db: Database, *, days: int = 30) -> list[dict[str, Any]]:
    """Return daily cost summary for the last N days.

    Includes costs from snapshots, watch check-ins, and follow-up collections.
    Returns list of dicts: [{"date": "2026-02-10", "total_usd": 1.23, "count": 5, "by_tool": {...}}]
    """
    offset_param = f"-{days} days"
    daily: dict[str, dict[str, Any]] = {}

    def _ensure_day(day: str) -> dict[str, Any]:
        if day not in daily:
            daily[day] = {"date": day, "total_usd": 0.0, "count": 0, "by_tool": {}}
        return daily[day]

    with db.engine.connect() as conn:
        # Snapshot costs (by creation date)
        for row in conn.execute(
            text(
                "SELECT date(created_at) as day, snapshot_json FROM snapshots "
                "WHERE date(created_at) >= date('now', :offset) ORDER BY day"
            ),
            {"offset": offset_param},
        ).fetchall():
            day = row[0]
            snap = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            cost = snap.get("cost_summary", {})
            entry = _ensure_day(day)
            entry["total_usd"] += float(cost.get("total_usd", 0.0))
            entry["count"] += 1
            for tool, amt in cost.get("by_tool", {}).items():
                entry["by_tool"][tool] = entry["by_tool"].get(tool, 0.0) + float(amt)

        # Watch check-in costs (by individual checkin timestamp)
        for row in conn.execute(
            text(
                "SELECT watch_json FROM watches "
                "WHERE date(updated_at) >= date('now', :offset)"
            ),
            {"offset": offset_param},
        ).fetchall():
            watch = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            for ci in watch.get("checkin_history", []):
                cost = ci.get("cost_usd", 0.0)
                if not cost:
                    continue
                ci_day = ci.get("time", "")[:10]  # "2026-02-19"
                if ci_day:
                    entry = _ensure_day(ci_day)
                    entry["total_usd"] += float(cost)
                    entry["by_tool"]["watch_checkin"] = (
                        entry["by_tool"].get("watch_checkin", 0.0) + float(cost)
                    )

        # Follow-up collection costs (by individual collection timestamp)
        for row in conn.execute(
            text(
                "SELECT follow_up_json FROM follow_ups "
                "WHERE date(updated_at) >= date('now', :offset)"
            ),
            {"offset": offset_param},
        ).fetchall():
            fu = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            for coll in fu.get("collections", []):
                cost = coll.get("cost_usd", 0.0)
                if not cost:
                    continue
                coll_day = coll.get("collected_at", "")[:10]
                if coll_day:
                    entry = _ensure_day(coll_day)
                    entry["total_usd"] += float(cost)
                    entry["by_tool"]["follow_up"] = (
                        entry["by_tool"].get("follow_up", 0.0) + float(cost)
                    )

    return sorted(daily.values(), key=lambda d: d["date"])


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
    db: Database, *, status: str | None = None, limit: int = 0, offset: int = 0
) -> list[dict[str, Any]]:
    """Fetch watches ordered by created_at DESC, with optional status filter.

    Args:
        limit: Max rows to return. 0 (default) = no limit (return all).
    """
    parts = ["SELECT watch_json FROM watches"]
    params: dict[str, Any] = {}
    if status:
        parts.append("WHERE status = :status")
        params["status"] = status
    parts.append("ORDER BY created_at DESC")
    if limit > 0:
        parts.append("LIMIT :lim")
        params["lim"] = limit
    if offset > 0:
        parts.append("OFFSET :off")
        params["off"] = offset
    sql = " ".join(parts)
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


def _price_expr(price_delay: int = 10) -> str:
    """Build a COALESCE expression that reads price_at first, then legacy price_10min."""
    delay_s = str(int(price_delay))
    return (
        f"CAST(COALESCE("
        f"json_extract(snapshot_json, '$.price_at.\"{delay_s}\"'), "
        f"json_extract(snapshot_json, '$.price_10min')"
        f") AS REAL)"
    )


_SIGNED_CONF_EXPR = (
    "CASE LOWER(json_extract(snapshot_json, '$.prediction.direction'))"
    " WHEN 'bullish' THEN CAST(json_extract(snapshot_json, '$.prediction.confidence') AS REAL)"
    " WHEN 'bearish' THEN -CAST(json_extract(snapshot_json, '$.prediction.confidence') AS REAL)"
    " ELSE 0 END"
)


def _add_common_snapshot_clauses(
    clauses: list[str],
    params: dict[str, Any],
    *,
    symbol: str | None = None,
    explored_only: bool = False,
    created_after: str | None = None,
    created_before: str | None = None,
    headline: str | None = None,
    conf_min: float | None = None,
    price_10_min: float | None = None,
    price_delay: int = 10,
) -> None:
    """Populate *clauses* and *params* for snapshot queries."""
    if symbol:
        clauses.append("symbols LIKE :sym")
        params["sym"] = f"%{symbol}%"
    if explored_only:
        clauses.append("json_extract(snapshot_json, '$.triage.action') = 'investigate'")
    if created_after:
        clauses.append("date(created_at) >= date(:created_after)")
        params["created_after"] = created_after
    if created_before:
        clauses.append("date(created_at) <= date(:created_before)")
        params["created_before"] = created_before
    if headline:
        clauses.append("json_extract(snapshot_json, '$.trigger.headline') LIKE :headline")
        params["headline"] = f"%{headline}%"
    if conf_min is not None:
        clauses.append(f"({_SIGNED_CONF_EXPR}) >= :conf_min")
        params["conf_min"] = conf_min
    if price_10_min is not None:
        clauses.append(f"{_price_expr(price_delay)} >= :price_10_min")
        params["price_10_min"] = float(price_10_min)


def get_all_snapshots(
    db: Database, *, symbol: str | None = None, explored_only: bool = False,
    created_after: str | None = None, created_before: str | None = None,
    headline: str | None = None,
    conf_min: float | None = None,
    price_10_min: float | None = None,
    price_delay: int = 10,
    limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    """Fetch snapshots ordered by created_at DESC, with optional filters."""
    clauses: list[str] = []
    params: dict[str, Any] = {"lim": limit, "off": offset}
    _add_common_snapshot_clauses(
        clauses, params,
        symbol=symbol, explored_only=explored_only,
        created_after=created_after, created_before=created_before,
        headline=headline, conf_min=conf_min,
        price_10_min=price_10_min, price_delay=price_delay,
    )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT snapshot_json FROM snapshots{where} ORDER BY created_at DESC LIMIT :lim OFFSET :off"
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return _parse_rows(rows)


def count_snapshots(
    db: Database,
    *,
    symbol: str | None = None,
    explored_only: bool = False,
    created_after: str | None = None,
    created_before: str | None = None,
    headline: str | None = None,
    conf_min: float | None = None,
    price_10_min: float | None = None,
    price_delay: int = 10,
) -> int:
    """Count snapshots, optionally filtered by symbol and/or explored-only."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    _add_common_snapshot_clauses(
        clauses, params,
        symbol=symbol, explored_only=explored_only,
        created_after=created_after, created_before=created_before,
        headline=headline, conf_min=conf_min,
        price_10_min=price_10_min, price_delay=price_delay,
    )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT COUNT(*) FROM snapshots{where}"
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


# ---------------------------------------------------------------------------
# Alpaca transaction log
# ---------------------------------------------------------------------------


def log_alpaca_transaction(
    db: Database,
    *,
    account_id: str,
    event: str,
    symbol: str,
    order_id: str | None = None,
    status: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append an Alpaca transaction record.

    Events: buy_submit, buy_confirmed, buy_failed, sell_submit, sell_confirmed,
            sell_failed, stop_submit, stop_confirmed, stop_failed, stop_cancel,
            fill (stream), reject (stream), cancel (stream),
            reconcile_ok, reconcile_force_exit, reconcile_orphan_closed,
            reconcile_price_updated
    """
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO alpaca_transactions "
                "(account_id, event, symbol, order_id, status, detail_json) "
                "VALUES (:acct, :evt, :sym, :oid, :st, :djson)"
            ),
            {
                "acct": account_id,
                "evt": event,
                "sym": symbol,
                "oid": order_id,
                "st": status,
                "djson": json.dumps(detail or {}, ensure_ascii=False, default=str),
            },
        )


def get_alpaca_transactions(
    db: Database,
    *,
    symbol: str | None = None,
    account_id: str | None = None,
    event: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query Alpaca transactions with optional filters."""
    clauses = []
    params: dict[str, Any] = {"lim": limit}
    if symbol:
        clauses.append("symbol = :sym")
        params["sym"] = symbol.upper()
    if account_id:
        clauses.append("account_id = :acct")
        params["acct"] = account_id
    if event:
        clauses.append("event = :evt")
        params["evt"] = event

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT created_at, account_id, event, symbol, order_id, status, detail_json "
        f"FROM alpaca_transactions {where} ORDER BY id DESC LIMIT :lim"
    )
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [
        {
            "created_at": str(r[0]) if r[0] else None,
            "account_id": r[1],
            "event": r[2],
            "symbol": r[3],
            "order_id": r[4],
            "status": r[5],
            "detail": json.loads(r[6]) if isinstance(r[6], str) else r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Portfolio equity snapshots
# ---------------------------------------------------------------------------


def insert_equity_snapshot(
    db: Database,
    *,
    config_id: str,
    timestamp: str,
    equity: float,
    cash: float,
    unrealized_pnl: float,
    realized_pnl: float,
    position_count: int,
    source: str = "live",
) -> None:
    """Insert a single portfolio equity snapshot."""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO portfolio_equity_snapshots "
                "(config_id, timestamp, equity, cash, unrealized_pnl, realized_pnl, position_count, source) "
                "VALUES (:cid, :ts, :eq, :cash, :upnl, :rpnl, :pcnt, :src)"
            ),
            {
                "cid": config_id,
                "ts": timestamp,
                "eq": equity,
                "cash": cash,
                "upnl": unrealized_pnl,
                "rpnl": realized_pnl,
                "pcnt": position_count,
                "src": source,
            },
        )


def insert_equity_snapshots_bulk(
    db: Database,
    rows: list[dict[str, Any]],
) -> int:
    """Bulk insert equity snapshots (for backfill). Returns count inserted."""
    if not rows:
        return 0
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO portfolio_equity_snapshots "
                "(config_id, timestamp, equity, cash, unrealized_pnl, realized_pnl, position_count, source) "
                "VALUES (:config_id, :timestamp, :equity, :cash, :unrealized_pnl, :realized_pnl, :position_count, :source)"
            ),
            rows,
        )
    return len(rows)


def get_equity_history(
    db: Database,
    config_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    """Fetch equity snapshots for a portfolio, ordered by timestamp ASC."""
    clauses = ["config_id = :cid"]
    params: dict[str, Any] = {"cid": config_id, "lim": limit}
    if since:
        clauses.append("timestamp >= :since")
        params["since"] = since
    if until:
        clauses.append("timestamp <= :until")
        params["until"] = until

    where = " AND ".join(clauses)
    sql = (
        f"SELECT timestamp, equity, cash, unrealized_pnl, realized_pnl, position_count, source "
        f"FROM portfolio_equity_snapshots WHERE {where} "
        f"ORDER BY timestamp ASC LIMIT :lim"
    )
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [
        {
            "timestamp": str(r[0]),
            "equity": r[1],
            "cash": r[2],
            "unrealized_pnl": r[3],
            "realized_pnl": r[4],
            "position_count": r[5],
            "source": r[6],
        }
        for r in rows
    ]


def delete_equity_history(db: Database, config_id: str, *, source: str | None = None) -> int:
    """Delete equity snapshots for a portfolio. Optionally filter by source. Returns count deleted."""
    clauses = ["config_id = :cid"]
    params: dict[str, Any] = {"cid": config_id}
    if source:
        clauses.append("source = :src")
        params["src"] = source
    where = " AND ".join(clauses)
    with db.engine.begin() as conn:
        result = conn.execute(text(f"DELETE FROM portfolio_equity_snapshots WHERE {where}"), params)
    return result.rowcount


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
