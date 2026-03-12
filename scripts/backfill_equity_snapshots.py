#!/usr/bin/env python3
"""Recalculate equity snapshots for a simulated portfolio using corrected exit prices.

Replays watch entry/exit events at each snapshot timestamp to compute
the correct realized PnL, position count, and equity.

Usage:
    uv run python scripts/backfill_equity_snapshots.py [--dry-run] [--config-id lc_xxx]
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone

DB_PATH = "data/trader.db"


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config-id", default="lc_c45e48da2b27")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    # Get config for starting capital and max_positions
    cfg_row = conn.execute(
        "SELECT config_json FROM live_configs WHERE config_id = ?",
        (args.config_id,),
    ).fetchone()
    if not cfg_row:
        print(f"Config {args.config_id} not found")
        return
    cfg = json.loads(cfg_row["config_json"])
    starting = cfg["starting_capital"]
    max_pos = cfg["allocation_params"].get("max_pos", 20)
    pos_size = starting / max_pos

    # Get all watches with entry/exit times and PnL
    watches = conn.execute(
        """
        SELECT watch_id, symbol, status, watch_json
        FROM watches
        WHERE json_extract(watch_json, '$.live_config_id') = ?
        ORDER BY json_extract(watch_json, '$.entry.time')
        """,
        (args.config_id,),
    ).fetchall()

    # Build event timeline: (timestamp, type, pnl_pct)
    events = []  # (datetime, event_type, pnl_pct)
    for w in watches:
        wj = json.loads(w["watch_json"])
        entry_time = parse_iso(wj["entry"]["time"])
        events.append((entry_time, "entry", 0.0))

        if wj.get("exit") and wj["exit"].get("time"):
            exit_time = parse_iso(wj["exit"]["time"])
            pnl_pct = wj["exit"].get("realized_pnl_pct", 0.0) or 0.0
            events.append((exit_time, "exit", pnl_pct))

    events.sort(key=lambda x: x[0])

    # Get existing equity snapshots
    snapshots = conn.execute(
        """
        SELECT id, timestamp, equity, realized_pnl, unrealized_pnl, position_count, cash
        FROM portfolio_equity_snapshots
        WHERE config_id = ?
        ORDER BY timestamp
        """,
        (args.config_id,),
    ).fetchall()

    print(f"Config: {args.config_id}")
    print(f"Starting capital: ${starting:,.2f}, pos_size: ${pos_size:,.2f}")
    print(f"Watches: {len(watches)}, Events: {len(events)}, Snapshots: {len(snapshots)}")
    print()

    updated = 0
    for snap in snapshots:
        snap_dt = parse_iso(snap["timestamp"])

        # Replay events up to this snapshot time
        holding_count = 0
        realized_dollar = 0.0
        for evt_time, evt_type, pnl_pct in events:
            if evt_time > snap_dt:
                break
            if evt_type == "entry":
                holding_count += 1
            elif evt_type == "exit":
                holding_count -= 1
                realized_dollar += pos_size * pnl_pct / 100

        # We don't have historical unrealized data, keep existing or 0
        unrealized_dollar = 0.0
        equity = starting + realized_dollar + unrealized_dollar
        cash = starting - (holding_count * pos_size) + realized_dollar

        old_equity = snap["equity"]
        if abs(equity - old_equity) > 0.01:
            if not args.dry_run:
                conn.execute(
                    """
                    UPDATE portfolio_equity_snapshots
                    SET equity = ?, cash = ?, realized_pnl = ?, position_count = ?
                    WHERE id = ?
                    """,
                    (round(equity, 2), round(cash, 2), round(realized_dollar, 2),
                     holding_count, snap["id"]),
                )
            updated += 1

            if updated <= 10 or updated % 20 == 0:
                print(f"  {snap['timestamp'][:19]}  ${old_equity:>10,.2f} -> ${equity:>10,.2f}  "
                      f"(realized=${realized_dollar:>+8,.2f}, holding={holding_count})")

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Updated {updated} of {len(snapshots)} snapshots")


if __name__ == "__main__":
    main()
