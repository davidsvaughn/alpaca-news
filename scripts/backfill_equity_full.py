#!/usr/bin/env python3
"""Recalculate equity snapshots with realized + unrealized PnL from watch history.

For each stored equity snapshot timestamp:
1. Replay watch entry/exit events to derive holdings + realized PnL
2. Look up each open holding's latest 1-minute price at-or-before that timestamp
3. Compute unrealized PnL from (current - entry) * fixed position size
4. Update portfolio_equity_snapshots in place

Usage:
    uv run python scripts/backfill_equity_full.py [--dry-run] [--config-id lc_xxx]
    uv run python scripts/backfill_equity_full.py --all
"""

import argparse
import json
import sqlite3
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv(".env", override=False)
sys.path.insert(0, ".")
from trader.market.schwab_client import SchwabMarketClient
from trader.valuation import compute_pct_pnl, position_size_for_config

DB_PATH = "data/trader.db"


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_price_index_from_candles(
    candles,
) -> tuple[list[float], list[float]]:
    """Convert candles to sorted arrays for binary-search lookups."""
    timestamps = []
    prices = []
    for c in candles:
        c_dt = parse_iso(c.t) if isinstance(c.t, str) else c.t
        if c_dt.tzinfo is None:
            c_dt = c_dt.replace(tzinfo=timezone.utc)
        timestamps.append(c_dt.timestamp())
        prices.append(float(c.c))
    return timestamps, prices


def lookup_price_at_or_before(
    timestamps: list[float],
    prices: list[float],
    target_ts: float,
) -> tuple[float | None, float | None]:
    """Find the latest bar at-or-before target timestamp."""
    if not timestamps:
        return None, None
    idx = bisect_right(timestamps, target_ts) - 1
    if idx < 0:
        return None, None
    return prices[idx], timestamps[idx]


def process_config(
    conn: sqlite3.Connection,
    schwab: SchwabMarketClient,
    config_id: str,
    price_cache: dict[str, tuple[list[float], list[float]]],
    dry_run: bool,
) -> None:
    """Process one config's equity snapshots."""
    cfg_row = conn.execute(
        "SELECT config_json FROM live_configs WHERE config_id = ?",
        (config_id,),
    ).fetchone()
    if not cfg_row:
        print(f"Config {config_id} not found")
        return
    cfg = json.loads(cfg_row["config_json"])
    starting = cfg["starting_capital"]
    pos_size = position_size_for_config(cfg)
    if not pos_size or pos_size <= 0:
        print(f"  Invalid position sizing for {config_id}")
        return

    # Load all watches for this config
    watches = conn.execute(
        """
        SELECT watch_id, symbol, status, watch_json
        FROM watches
        WHERE json_extract(watch_json, '$.live_config_id') = ?
        ORDER BY json_extract(watch_json, '$.entry.time')
        """,
        (config_id,),
    ).fetchall()

    if not watches:
        print("  No watches found")
        return

    # Build event timeline:
    # (event_dt, order, event_type, watch_id, symbol, entry_price, pnl_pct)
    # order enforces entry before exit when timestamps tie.
    events = []
    watch_meta: dict[str, dict[str, Any]] = {}
    for w in watches:
        wj = json.loads(w["watch_json"])
        watch_meta[w["watch_id"]] = wj
        entry_time = parse_iso(wj["entry"]["time"])
        entry_price = float(wj["entry"]["price"])
        symbol = w["symbol"]
        watch_id = w["watch_id"]
        events.append((entry_time, 0, "entry", watch_id, symbol, entry_price, 0.0))

        if wj.get("exit") and wj["exit"].get("time"):
            exit_time = parse_iso(wj["exit"]["time"])
            pnl_pct = wj["exit"].get("realized_pnl_pct", 0.0) or 0.0
            events.append((exit_time, 1, "exit", watch_id, symbol, entry_price, pnl_pct))
    events.sort(key=lambda x: (x[0], x[1]))

    # Load snapshots to rebuild
    snapshots = conn.execute(
        """
        SELECT id, timestamp, equity, realized_pnl, unrealized_pnl, position_count, cash
        FROM portfolio_equity_snapshots
        WHERE config_id = ?
        ORDER BY timestamp
        """,
        (config_id,),
    ).fetchall()

    if not snapshots:
        print("  No snapshots found")
        return

    snap_times = [parse_iso(s["timestamp"]) for s in snapshots]
    min_ts = min(snap_times)
    max_ts = max(snap_times)

    # Fetch 1-minute candles once per symbol over exact snapshot range.
    all_symbols = sorted(set(w["symbol"] for w in watches))
    new_symbols = [s for s in all_symbols if s not in price_cache]
    if new_symbols:
        start = min_ts - timedelta(hours=2)
        end = max_ts + timedelta(minutes=5)
        print(
            f"  Fetching 1m Schwab candles for {len(new_symbols)} symbols "
            f"from {start.isoformat()} to {end.isoformat()}...",
        )
        for i, sym in enumerate(new_symbols, 1):
            try:
                candles = schwab.get_candles_by_date_range(
                    sym,
                    start=start,
                    end=end,
                    frequency=1,
                    extended_hours=True,
                )
                price_cache[sym] = build_price_index_from_candles(candles)
            except Exception as e:
                print(f"    {sym}: FAILED — {e}")
                price_cache[sym] = ([], [])
            if i % 20 == 0 or i == len(new_symbols):
                print(f"    {i}/{len(new_symbols)} fetched...")

    print(f"  {len(snapshots)} snapshots to process")

    # Replay events incrementally while walking snapshots in time.
    evt_idx = 0
    realized_dollar = 0.0
    holdings: dict[str, tuple[str, float, str]] = {}  # watch_id -> (symbol, entry_price, direction)

    updated = 0
    missing_price_total = 0
    stale_price_total = 0
    for snap in snapshots:
        snap_dt = parse_iso(snap["timestamp"])
        snap_ts = snap_dt.timestamp()

        while evt_idx < len(events) and events[evt_idx][0] <= snap_dt:
            evt_time, _order, evt_type, watch_id, symbol, entry_price, pnl_pct = events[evt_idx]
            if evt_type == "entry":
                wj = watch_meta.get(watch_id) or {}
                direction = str((wj.get("entry") or {}).get("direction") or "bullish")
                holdings[watch_id] = (symbol, entry_price, direction)
            elif evt_type == "exit":
                holdings.pop(watch_id, None)
                realized_dollar += pos_size * pnl_pct / 100
            evt_idx += 1

        unrealized_dollar = 0.0
        for symbol, entry_price, direction in holdings.values():
            ts_list, px_list = price_cache.get(symbol, ([], []))
            current_price, px_ts = lookup_price_at_or_before(ts_list, px_list, snap_ts)
            if current_price is None or not entry_price:
                missing_price_total += 1
                continue
            age_min = (snap_ts - px_ts) / 60.0
            if age_min > 60:
                stale_price_total += 1
            upnl_pct = compute_pct_pnl(entry_price, current_price, direction)
            if upnl_pct is not None:
                unrealized_dollar += pos_size * upnl_pct / 100

        holding_count = len(holdings)
        equity = starting + realized_dollar + unrealized_dollar
        cash = starting - (holding_count * pos_size) + realized_dollar

        old_equity = snap["equity"]
        old_unrealized = snap["unrealized_pnl"]

        if abs(equity - old_equity) > 0.01 or abs(unrealized_dollar - old_unrealized) > 0.01:
            if not dry_run:
                conn.execute(
                    """
                    UPDATE portfolio_equity_snapshots
                    SET equity = ?, cash = ?, realized_pnl = ?,
                        unrealized_pnl = ?, position_count = ?
                    WHERE id = ?
                    """,
                    (round(equity, 2), round(cash, 2), round(realized_dollar, 2),
                     round(unrealized_dollar, 2), holding_count, snap["id"]),
                )
            updated += 1

            if updated <= 5 or updated % 50 == 0:
                print(f"    {snap['timestamp'][:19]}  ${old_equity:>10,.2f} -> ${equity:>10,.2f}  "
                      f"(real=${realized_dollar:>+8,.2f} unreal=${unrealized_dollar:>+8,.2f} "
                      f"hold={holding_count})")

    if not dry_run:
        conn.commit()

    print(f"  {'DRY RUN — ' if dry_run else ''}Updated {updated} of {len(snapshots)} snapshots\n")
    if missing_price_total or stale_price_total:
        print(
            f"  Price coverage notes: missing-lookups={missing_price_total}, "
            f"stale-lookups(>60m)={stale_price_total}\n",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config-id", default=None)
    parser.add_argument("--all", action="store_true", help="Process all sim portfolios")
    args = parser.parse_args()

    if not args.config_id and not args.all:
        args.all = True

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    schwab = SchwabMarketClient()

    # Shared price cache across configs
    price_cache: dict[str, tuple[list[float], list[float]]] = {}

    if args.all:
        # Find all sim portfolios (no alpaca_account_id)
        configs = conn.execute(
            """
            SELECT config_id FROM live_configs
            WHERE json_extract(config_json, '$.alpaca_account_id') IS NULL
              AND json_extract(config_json, '$.active') = 1
            """,
        ).fetchall()
        config_ids = [r["config_id"] for r in configs]
    else:
        config_ids = [args.config_id]

    print(f"Processing {len(config_ids)} config(s): {', '.join(config_ids)}\n")

    for cid in config_ids:
        print(f"=== {cid} ===")
        process_config(conn, schwab, cid, price_cache, args.dry_run)

    conn.close()


if __name__ == "__main__":
    main()
