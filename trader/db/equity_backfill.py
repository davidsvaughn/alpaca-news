"""Backfill portfolio_equity_snapshots from watch entry/exit events.

Replays watch lifecycle chronologically to reconstruct an equity curve
for each portfolio. Produces one snapshot per trade event (buy or sell),
giving a step-function equity curve.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from trader.db.database import (
    Database,
    delete_equity_history,
    insert_equity_snapshots_bulk,
)

log = logging.getLogger(__name__)


def _pos_size_for_config(cfg: dict[str, Any]) -> float:
    """Compute the fixed dollar size per position from a LiveConfig dict."""
    starting = cfg.get("starting_capital", 0)
    if not starting or starting <= 0:
        return 0.0
    alloc = cfg.get("allocation", "")
    alloc_params = cfg.get("allocation_params") or {}

    if alloc == "max_positions":
        max_pos = int(alloc_params.get("max_pos", 10))
    elif alloc in ("fixed_dollar", "ranking_realloc"):
        alloc_pct = float(alloc_params.get("alloc_pct", 5))
        max_pos = max(1, int(100 / alloc_pct))
    else:
        max_pos = 20

    return starting / max_pos


def _get_watches_for_config(db: Database, config_id: str) -> list[dict[str, Any]]:
    """Fetch all watches for a given live_config_id."""
    sql = (
        "SELECT watch_json FROM watches "
        "WHERE json_extract(watch_json, '$.live_config_id') = :cid"
    )
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), {"cid": config_id}).fetchall()
    results = []
    for r in rows:
        raw = r[0]
        d = json.loads(raw) if isinstance(raw, str) else raw
        results.append(d)
    return results


def _build_events(watches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract chronological trade events from watches.

    Returns list of {timestamp, type, symbol, pnl_pct, watch_id} sorted by time.
    """
    events = []
    for w in watches:
        entry = w.get("entry", {})
        entry_time = entry.get("time") or w.get("created_at", "")
        if entry_time:
            events.append({
                "timestamp": entry_time,
                "type": "buy",
                "symbol": w.get("symbol", ""),
                "pnl_pct": 0.0,
                "watch_id": w.get("watch_id", ""),
            })

        ex = w.get("exit")
        if ex:
            exit_time = ex.get("time", "")
            rpnl = ex.get("realized_pnl_pct", 0.0)
            if exit_time:
                events.append({
                    "timestamp": exit_time,
                    "type": "sell",
                    "symbol": w.get("symbol", ""),
                    "pnl_pct": float(rpnl) if rpnl is not None else 0.0,
                    "watch_id": w.get("watch_id", ""),
                })

    # Sort chronologically
    events.sort(key=lambda e: e["timestamp"])
    return events


def backfill_portfolio(
    db: Database,
    config_id: str,
    cfg: dict[str, Any],
    *,
    replace: bool = True,
) -> int:
    """Backfill equity snapshots for a single portfolio from its watch history.

    Args:
        db: Database connection
        config_id: The live config ID
        cfg: The live config dict
        replace: If True, delete existing backfill snapshots first

    Returns:
        Number of snapshots inserted
    """
    starting_capital = cfg.get("starting_capital", 0)
    if not starting_capital or starting_capital <= 0:
        log.warning("Skipping backfill for %s: no starting_capital", config_id)
        return 0

    pos_size = _pos_size_for_config(cfg)
    if pos_size <= 0:
        return 0

    watches = _get_watches_for_config(db, config_id)
    if not watches:
        log.info("No watches for %s, inserting initial snapshot only", config_id)

    events = _build_events(watches)

    if replace:
        deleted = delete_equity_history(db, config_id, source="backfill")
        if deleted:
            log.info("Deleted %d existing backfill snapshots for %s", deleted, config_id)

    # Replay events
    cash = starting_capital
    realized_pnl = 0.0
    holdings: dict[str, str] = {}  # watch_id -> symbol (tracks open positions)
    snapshots: list[dict[str, Any]] = []

    # Initial snapshot at config creation time
    created_at = cfg.get("created_at", "")
    if created_at:
        snapshots.append({
            "config_id": config_id,
            "timestamp": created_at,
            "equity": starting_capital,
            "cash": starting_capital,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "position_count": 0,
            "source": "backfill",
        })

    for evt in events:
        wid = evt["watch_id"]

        if evt["type"] == "buy":
            # Deduct position cost from cash
            cash -= pos_size
            holdings[wid] = evt["symbol"]
        elif evt["type"] == "sell":
            # Add back position value (pos_size + P&L)
            dollar_pnl = pos_size * evt["pnl_pct"] / 100.0
            cash += pos_size + dollar_pnl
            realized_pnl += dollar_pnl
            holdings.pop(wid, None)

        # Equity = cash + value of open positions (at cost basis, since we
        # don't have historical intraday prices for unrealized P&L)
        open_value = len(holdings) * pos_size
        equity = cash + open_value

        snapshots.append({
            "config_id": config_id,
            "timestamp": evt["timestamp"],
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "unrealized_pnl": 0.0,  # backfill can't compute unrealized
            "realized_pnl": round(realized_pnl, 2),
            "position_count": len(holdings),
            "source": "backfill",
        })

    count = insert_equity_snapshots_bulk(db, snapshots)
    log.info("Backfilled %d equity snapshots for %s", count, config_id)
    return count


def backfill_all_portfolios(db: Database) -> dict[str, int]:
    """Backfill equity snapshots for all live configs with watch history.

    Returns: {config_id: count_inserted}
    """
    from trader.db.database import get_all_live_configs

    configs = get_all_live_configs(db)
    results = {}
    for cfg in configs:
        config_id = cfg.get("config_id", "")
        if not config_id:
            continue
        count = backfill_portfolio(db, config_id, cfg)
        results[config_id] = count

    total = sum(results.values())
    log.info("Backfill complete: %d snapshots across %d portfolios", total, len(results))
    return results
