"""One-time backfill of peak_pnl_pct / trough_pnl_pct for existing watches.

Fetches 1-min bars for each watch's holding period and computes the
highest and lowest unrealized P&L that occurred.

Usage:
    uv run python scripts/backfill_pnl_extremes.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

import pandas as pd

from trader.db.database import get_all_watches, open_sqlite, update_watch
from trader.market.backtest import _filter_trading_hours, _get_ohlcv_1m
from trader.market.market_hours import ET
from trader.models.watch import WatchBuilder


def backfill_watch(db, watch_dict: dict, *, dry_run: bool = False) -> bool:
    """Compute and store peak/trough P&L for one watch. Returns True if updated."""
    entry = watch_dict.get("entry", {})
    entry_price = entry.get("price")
    entry_time_str = entry.get("time")
    symbol = watch_dict.get("symbol")
    watch_id = watch_dict.get("watch_id")

    if not entry_price or not entry_time_str or not symbol:
        return False

    # Already has values?
    if watch_dict.get("peak_pnl_pct") is not None and watch_dict.get("trough_pnl_pct") is not None:
        return False

    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
    except (ValueError, TypeError):
        print(f"  SKIP {watch_id} ({symbol}): bad entry time")
        return False

    # Determine end time: exit time if exited, else now
    exit_data = watch_dict.get("exit")
    if exit_data and exit_data.get("time"):
        try:
            end_dt = datetime.fromisoformat(exit_data["time"])
        except (ValueError, TypeError):
            end_dt = datetime.now()
    else:
        end_dt = datetime.now()

    start_date = (entry_dt - timedelta(days=2)).strftime("%Y-%m-%d")
    bars = _get_ohlcv_1m(symbol, start_date)
    if bars is None or bars.empty:
        print(f"  SKIP {watch_id} ({symbol}): no bar data")
        return False

    bars = _filter_trading_hours(bars, market_close=None)
    if bars.empty:
        return False

    # Find entry bar
    if entry_dt.tzinfo is not None:
        entry_dt_et = entry_dt.astimezone(ET).replace(tzinfo=None)
    else:
        entry_dt_et = entry_dt
    entry_ts = pd.Timestamp(entry_dt_et).floor("s")
    entry_idx = bars.index.searchsorted(entry_ts)
    # Clamp: if entry is after all bars, use last bar only
    if entry_idx >= len(bars):
        entry_idx = len(bars) - 1

    # Find end bar
    if end_dt.tzinfo is not None:
        end_dt_et = end_dt.astimezone(ET).replace(tzinfo=None)
    else:
        end_dt_et = end_dt
    end_ts = pd.Timestamp(end_dt_et).floor("s")
    end_idx = bars.index.searchsorted(end_ts, side="right")
    end_idx = min(end_idx, len(bars))

    # Compute P&L at each bar's close
    holding_bars = bars.iloc[entry_idx:end_idx]
    if holding_bars.empty:
        # Fallback: use last bar
        holding_bars = bars.iloc[-1:]

    closes = holding_bars["Close"].values
    pnl_pcts = ((closes - entry_price) / entry_price) * 100.0

    peak = round(float(pnl_pcts.max()), 4)
    trough = round(float(pnl_pcts.min()), 4)

    print(f"  {watch_id} ({symbol}): peak={peak:+.2f}% trough={trough:+.2f}% ({len(holding_bars)} bars)")

    if not dry_run:
        builder = WatchBuilder.from_dict(watch_dict)
        builder.peak_pnl_pct = peak
        builder.trough_pnl_pct = trough
        updated = builder.to_watch()
        update_watch(db, watch_id, updated.to_dict())

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill peak/trough P&L for watches")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--db", default="data/trader.db", help="SQLite DB path")
    args = parser.parse_args()

    db = open_sqlite(args.db)
    watches = get_all_watches(db)
    print(f"Found {len(watches)} watches")

    updated = 0
    for w in watches:
        try:
            if backfill_watch(db, w, dry_run=args.dry_run):
                updated += 1
        except Exception as e:
            print(f"  ERROR {w.get('watch_id')}: {e}")

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{prefix}Updated {updated}/{len(watches)} watches")


if __name__ == "__main__":
    main()
