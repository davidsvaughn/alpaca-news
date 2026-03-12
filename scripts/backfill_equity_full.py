#!/usr/bin/env python3
"""Recalculate equity snapshots with both realized AND unrealized PnL.

For each equity snapshot timestamp:
1. Replay watch entry/exit events to get realized PnL and current holdings
2. Look up each holding's price at that timestamp from Schwab 1-min bars
3. Compute unrealized PnL from (current_price - entry_price) for each holding
4. Update the equity snapshot

Usage:
    uv run python scripts/backfill_equity_full.py [--dry-run] [--config-id lc_xxx]
    uv run python scripts/backfill_equity_full.py --all  # both sim portfolios
"""

import argparse
import json
import sqlite3
import sys
import time
from bisect import bisect_right
from datetime import datetime, timezone

sys.path.insert(0, ".")
from trader.market.data_service import MarketDataService

DB_PATH = "data/trader.db"


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_price_index(bars: list[dict]) -> tuple[list[float], list[float]]:
    """Convert bars to sorted (timestamp_seconds, close_price) arrays for bisect lookup."""
    timestamps = []
    prices = []
    for bar in bars:
        bar_dt = parse_iso(bar["date"]) if isinstance(bar["date"], str) else bar["date"]
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=timezone.utc)
        timestamps.append(bar_dt.timestamp())
        prices.append(float(bar["c"]))
    return timestamps, prices


def lookup_price(timestamps: list[float], prices: list[float], target_ts: float) -> float | None:
    """Find the closest bar price to target timestamp."""
    if not timestamps:
        return None
    idx = bisect_right(timestamps, target_ts)
    # Check both neighbors
    candidates = []
    if idx > 0:
        candidates.append((abs(timestamps[idx - 1] - target_ts), prices[idx - 1]))
    if idx < len(timestamps):
        candidates.append((abs(timestamps[idx] - target_ts), prices[idx]))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def process_config(conn, market, config_id: str, price_cache: dict, dry_run: bool):
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
    max_pos = cfg["allocation_params"].get("max_pos", 20)
    pos_size = starting / max_pos

    # Get all watches
    watches = conn.execute(
        """
        SELECT watch_id, symbol, status, watch_json
        FROM watches
        WHERE json_extract(watch_json, '$.live_config_id') = ?
        ORDER BY json_extract(watch_json, '$.entry.time')
        """,
        (config_id,),
    ).fetchall()

    # Build event timeline: (timestamp, event_type, symbol, entry_price, pnl_pct)
    events = []
    for w in watches:
        wj = json.loads(w["watch_json"])
        entry_time = parse_iso(wj["entry"]["time"])
        entry_price = float(wj["entry"]["price"])
        symbol = w["symbol"]
        events.append((entry_time, "entry", symbol, entry_price, 0.0))

        if wj.get("exit") and wj["exit"].get("time"):
            exit_time = parse_iso(wj["exit"]["time"])
            pnl_pct = wj["exit"].get("realized_pnl_pct", 0.0) or 0.0
            events.append((exit_time, "exit", symbol, entry_price, pnl_pct))
    events.sort(key=lambda x: x[0])

    # Collect all symbols we need prices for
    all_symbols = sorted(set(w["symbol"] for w in watches))
    new_symbols = [s for s in all_symbols if s not in price_cache]
    if new_symbols:
        print(f"  Fetching bars for {len(new_symbols)} new symbols...")
        for i, sym in enumerate(new_symbols):
            try:
                result = market.get_price_history(sym, period="5d", interval="1m")
                bars = result.get("bars", [])
                price_cache[sym] = build_price_index(bars)
                if (i + 1) % 20 == 0:
                    print(f"    {i + 1}/{len(new_symbols)} fetched...")
            except Exception as e:
                print(f"    {sym}: FAILED — {e}")
                price_cache[sym] = ([], [])
        print(f"    Done. {len(new_symbols)} symbols fetched.")

    # Get equity snapshots
    snapshots = conn.execute(
        """
        SELECT id, timestamp, equity, realized_pnl, unrealized_pnl, position_count, cash
        FROM portfolio_equity_snapshots
        WHERE config_id = ?
        ORDER BY timestamp
        """,
        (config_id,),
    ).fetchall()

    print(f"  {len(snapshots)} snapshots to process")

    updated = 0
    for snap in snapshots:
        snap_dt = parse_iso(snap["timestamp"])
        snap_ts = snap_dt.timestamp()

        # Replay events to get holdings and realized PnL at this timestamp
        holdings = {}  # symbol -> entry_price
        realized_dollar = 0.0
        for evt_time, evt_type, symbol, entry_price, pnl_pct in events:
            if evt_time > snap_dt:
                break
            if evt_type == "entry":
                holdings[symbol] = entry_price
            elif evt_type == "exit":
                holdings.pop(symbol, None)
                realized_dollar += pos_size * pnl_pct / 100

        # Compute unrealized PnL from current prices
        unrealized_dollar = 0.0
        for symbol, entry_price in holdings.items():
            ts_list, px_list = price_cache.get(symbol, ([], []))
            current_price = lookup_price(ts_list, px_list, snap_ts)
            if current_price and entry_price:
                upnl_pct = (current_price - entry_price) / entry_price * 100
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config-id", default=None)
    parser.add_argument("--all", action="store_true", help="Process all sim portfolios")
    args = parser.parse_args()

    if not args.config_id and not args.all:
        args.all = True

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    market = MarketDataService()

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
        process_config(conn, market, cid, price_cache, args.dry_run)

    conn.close()


if __name__ == "__main__":
    main()
