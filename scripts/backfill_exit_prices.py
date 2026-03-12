#!/usr/bin/env python3
"""Backfill exit prices for simulated replacement exits that recorded 0.0% PnL.

Fetches 1-min intraday bars from Schwab and finds the closest bar to each
exit timestamp, then updates watch_json with correct exit price and PnL.

Usage:
    uv run python scripts/backfill_exit_prices.py [--dry-run] [--config-id lc_xxx]
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from trader.market.data_service import MarketDataService

DB_PATH = "data/trader.db"


def parse_iso(s: str) -> datetime:
    """Parse ISO 8601 timestamp to UTC datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def find_closest_price(bars: list[dict], target_dt: datetime) -> float | None:
    """Find the bar closest to target_dt and return its close price."""
    if not bars:
        return None

    best = None
    best_delta = None
    for bar in bars:
        bar_dt = parse_iso(bar["date"]) if isinstance(bar["date"], str) else bar["date"]
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=timezone.utc)
        delta = abs((bar_dt - target_dt).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = bar
    return float(best["c"]) if best else None


def main():
    parser = argparse.ArgumentParser(description="Backfill exit prices for simulated replacements")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to DB")
    parser.add_argument("--config-id", default="lc_c45e48da2b27", help="LiveConfig ID to backfill")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    market = MarketDataService()

    # Find all zero-PnL replacements
    rows = conn.execute(
        """
        SELECT watch_id, symbol, watch_json
        FROM watches
        WHERE json_extract(watch_json, '$.live_config_id') = ?
          AND status IN ('exited', 'cooling_off', 'sealed')
          AND json_extract(watch_json, '$.exit.reason') = 'replaced'
          AND json_extract(watch_json, '$.exit.realized_pnl_pct') = 0.0
        ORDER BY json_extract(watch_json, '$.exit.time')
        """,
        (args.config_id,),
    ).fetchall()

    if not rows:
        print(f"No zero-PnL replacements found for {args.config_id}")
        return

    print(f"Found {len(rows)} zero-PnL replacements to backfill\n")

    # Collect unique symbols to batch price fetches
    symbols = sorted(set(r["symbol"] for r in rows))
    print(f"Fetching 1-min bars for {len(symbols)} symbols: {', '.join(symbols)}\n")

    # Fetch 1-min bars per symbol (Schwab primary, yfinance fallback)
    bars_cache: dict[str, list[dict]] = {}
    for sym in symbols:
        try:
            result = market.get_price_history(sym, period="1d", interval="1m")
            bars_cache[sym] = result.get("bars", [])
            src = result.get("source", "?")
            print(f"  {sym}: {len(bars_cache[sym])} bars ({src})")
        except Exception as e:
            print(f"  {sym}: FAILED — {e}")
            bars_cache[sym] = []

    print()

    # Process each watch
    updated = 0
    skipped = 0
    total_realized_pnl = 0.0

    for row in rows:
        watch_id = row["watch_id"]
        symbol = row["symbol"]
        wj = json.loads(row["watch_json"])

        entry_price = wj["entry"]["price"]
        exit_time_str = wj["exit"]["time"]
        exit_dt = parse_iso(exit_time_str)

        bars = bars_cache.get(symbol, [])
        exit_price = find_closest_price(bars, exit_dt)

        if exit_price is None:
            print(f"  SKIP {symbol} ({watch_id}) — no bars available")
            skipped += 1
            continue

        pnl_pct = round((exit_price - entry_price) / entry_price * 100, 4) if entry_price else 0.0
        total_realized_pnl += pnl_pct

        exit_time_et = exit_dt.strftime("%H:%M:%S")
        print(f"  {symbol:6s}  entry={entry_price:>9.2f}  exit={exit_price:>9.2f}  "
              f"pnl={pnl_pct:>+7.3f}%  @{exit_time_et} UTC")

        # Update watch_json
        wj["exit"]["price"] = exit_price
        wj["exit"]["realized_pnl_pct"] = pnl_pct

        if not args.dry_run:
            conn.execute(
                "UPDATE watches SET watch_json = ? WHERE watch_id = ?",
                (json.dumps(wj), watch_id),
            )
            updated += 1

    if not args.dry_run:
        conn.commit()

    conn.close()

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Summary:")
    print(f"  Updated: {updated}")
    print(f"  Skipped: {skipped}")
    n = len(rows) - skipped
    if n > 0:
        print(f"  Avg realized PnL: {total_realized_pnl / n:+.3f}%")


if __name__ == "__main__":
    main()
