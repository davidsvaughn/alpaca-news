#!/usr/bin/env python3
"""Fix existing fractional positions: set DAY stops or sell if below stop price.

Usage:
    uv run python scripts/fix_fractional_stops.py [--dry-run]

For each Alpaca paper account with an active LiveConfig:
  1. Get all holding watches with Alpaca positions
  2. Calculate stop price from entry_price * (1 - guard_stop_pct/100)
  3. If current price <= stop price → sell the position
  4. If current price > stop price → submit a DAY stop order
"""
from __future__ import annotations

import argparse
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from trader.config import load_settings
from trader.db.database import get_active_live_configs, get_active_watches, open_sqlite, update_watch
from trader.market.alpaca_broker import AlpacaBrokerPool
from trader.models.live_config import LiveConfig
from trader.models.watch import WatchBuilder


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix fractional positions without stop protection")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    args = parser.parse_args()

    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)
    pool = AlpacaBrokerPool(db=db)

    if not pool.registry:
        print("No Alpaca accounts configured")
        return

    active_cfgs = get_active_live_configs(db)
    if not active_cfgs:
        print("No active LiveConfigs")
        return

    all_watches = get_active_watches(db)
    holding_watches = [w for w in all_watches if w.get("status") == "holding" and w.get("alpaca_buy_order_id")]

    if not holding_watches:
        print("No holding watches with Alpaca orders")
        return

    for cfg_dict in active_cfgs:
        acct_id = cfg_dict.get("alpaca_account_id")
        if not acct_id:
            continue

        cfg = LiveConfig.from_dict(cfg_dict)
        broker = pool.get(acct_id)
        if not broker:
            print(f"  SKIP: no broker for {acct_id}")
            continue

        acct_info = broker.get_account()
        print(f"\n{'='*60}")
        print(f"Account: {acct_info.name} ({acct_id})")
        print(f"  Equity: ${acct_info.equity:,.2f}  Cash: ${acct_info.cash:,.2f}")
        print(f"  guard_stop_pct: {cfg.guard_stop_pct}%")

        if cfg.guard_stop_pct <= 0:
            print("  SKIP: guard_stop_pct is 0")
            continue

        positions = {p.symbol: p for p in broker.get_positions()}
        print(f"  Positions: {list(positions.keys())}")

        # Filter watches to this config only
        config_watches = [w for w in holding_watches
                          if w.get("live_config_id") == cfg.config_id]

        for watch in config_watches:
            symbol = watch["symbol"]
            if symbol not in positions:
                continue

            pos = positions[symbol]
            entry_price = watch["entry"]["price"]
            stop_price = round(entry_price * (1 - cfg.guard_stop_pct / 100), 2)
            current_price = pos.current_price
            qty = pos.qty
            is_fractional = qty % 1 != 0

            existing_stop_id = watch.get("alpaca_stop_order_id")
            has_active_stop = False
            if existing_stop_id:
                existing = broker.get_order(existing_stop_id)
                if existing and existing.status.lower() in ("new", "accepted", "pending_new"):
                    has_active_stop = True

            print(f"\n  {symbol}:")
            print(f"    qty={qty} ({'fractional' if is_fractional else 'whole'})")
            print(f"    entry=${entry_price:.2f}  current=${current_price:.2f}  stop=${stop_price:.2f}")
            print(f"    existing_stop={'ACTIVE' if has_active_stop else 'NONE/EXPIRED'}")

            if has_active_stop:
                print(f"    -> SKIP (stop already active)")
                continue

            if current_price <= stop_price:
                print(f"    -> SELL (current ${current_price:.2f} <= stop ${stop_price:.2f})")
                if not args.dry_run:
                    try:
                        result = broker.close_position_and_confirm(symbol)
                        if result:
                            print(f"       SOLD: order={result.order_id} fill=${result.filled_avg_price}")
                            builder = WatchBuilder.from_dict(watch)
                            builder.record_exit(price=result.filled_avg_price or current_price,
                                                reason="manual_stop_triggered")
                            updated = builder.to_watch()
                            update_watch(db, watch["watch_id"], updated.to_dict())
                            print(f"       Watch exited: {watch['watch_id']}")
                    except Exception as e:
                        print(f"       SELL FAILED: {e}")
            else:
                tif = "DAY" if is_fractional else "GTC"
                print(f"    -> SET STOP: qty={qty} stop=${stop_price:.2f} tif={tif}")
                if not args.dry_run:
                    try:
                        result = broker.set_stop(symbol, qty=qty, stop_price=stop_price)
                        print(f"       STOP SET: order={result.order_id}")
                        builder = WatchBuilder.from_dict(watch)
                        builder.alpaca_stop_order_id = result.order_id
                        builder.alpaca_stop_price = stop_price
                        updated = builder.to_watch()
                        update_watch(db, watch["watch_id"], updated.to_dict())
                        print(f"       Watch updated: {watch['watch_id']}")
                    except Exception as e:
                        print(f"       STOP FAILED: {e}")

    if args.dry_run:
        print(f"\n{'='*60}")
        print("DRY RUN — no actions taken. Remove --dry-run to execute.")


if __name__ == "__main__":
    main()
