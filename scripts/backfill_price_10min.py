"""Backfill price_10min for existing snapshots.

Uses yfinance 1-minute historical data (available for the last ~7 days).
For each snapshot missing price_10min, finds the 1-min bar closest to
created_at + 10 minutes and stores its close price.

Usage:
    uv run python scripts/backfill_price_10min.py [--dry-run]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from trader.config import load_settings
from trader.db.database import open_sqlite, update_snapshot_field

try:
    import yfinance as yf
except ImportError:
    print("yfinance not installed — run: uv sync")
    sys.exit(1)


def main(dry_run: bool = False) -> None:
    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)

    # Fetch all snapshots
    from sqlalchemy import text

    with db.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT snapshot_json FROM snapshots ORDER BY created_at DESC")
        ).fetchall()

    updated = 0
    skipped = 0
    no_data = 0

    for (raw,) in rows:
        snap = json.loads(raw) if isinstance(raw, str) else raw
        sid = snap.get("snapshot_id", "?")

        # Skip if already has price_10min
        if snap.get("price_10min"):
            skipped += 1
            continue

        # Parse created_at
        created_str = snap.get("created_at", "")
        if not created_str:
            no_data += 1
            continue

        try:
            # Try various ISO formats
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
            ):
                try:
                    created = datetime.strptime(created_str.strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                no_data += 1
                continue

            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            no_data += 1
            continue

        target = created + timedelta(minutes=10)
        now = datetime.now(tz=timezone.utc)

        # Skip if less than 10 min old (not yet eligible)
        if target > now:
            skipped += 1
            continue

        # yfinance 1-min data only available for last ~7 days
        if (now - target).days > 7:
            no_data += 1
            continue

        # Get primary symbol
        trigger = snap.get("trigger", {})
        symbols = trigger.get("symbols", [])
        if not symbols:
            no_data += 1
            continue

        sym = symbols[0]

        # Fetch 1-day of 1-min bars around the target date
        try:
            ticker = yf.Ticker(sym)
            start = (created - timedelta(hours=1)).strftime("%Y-%m-%d")
            end = (created + timedelta(days=1)).strftime("%Y-%m-%d")
            df = ticker.history(start=start, end=end, interval="1m")

            if df.empty:
                no_data += 1
                continue

            # Make index tz-aware UTC for comparison
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")

            # Find bar closest to target time
            target_naive = target.astimezone(timezone.utc)
            diffs = abs(df.index - target_naive)
            closest_idx = diffs.argmin()
            price = float(df.iloc[closest_idx]["Close"])

            # Only accept if within 5 minutes of target
            if diffs[closest_idx].total_seconds() > 300:
                no_data += 1
                continue

            if dry_run:
                print(f"  [DRY] {sid[:12]} {sym} @ {target.strftime('%H:%M')} -> ${price:.2f}")
            else:
                update_snapshot_field(db, sid, "price_10min", price)
                print(f"  {sid[:12]} {sym} @ {target.strftime('%H:%M')} -> ${price:.2f}")
            updated += 1

        except Exception as e:
            print(f"  ERR {sid[:12]} {sym}: {e}")
            no_data += 1

    print(f"\nDone. Updated: {updated}, Skipped (already set): {skipped}, No data: {no_data}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if dry:
        print("=== DRY RUN ===")
    main(dry_run=dry)
