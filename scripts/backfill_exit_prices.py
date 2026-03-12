#!/usr/bin/env python3
"""Backfill replacement-exit prices from market data.

Fixes sim watches where replacement exits were written with entry price,
yielding 0.0% realized PnL artifacts.

Price lookup priority:
1) Tick collector last trade at-or-before exit (if available and fresh)
2) Schwab 1-minute candle close at-or-before exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv(".env", override=False)
sys.path.insert(0, ".")

from trader.market.schwab_client import Candle, SchwabMarketClient

DB_PATH = "data/trader.db"


def parse_iso(s: str) -> datetime:
    """Parse ISO 8601 timestamp to UTC datetime."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def lookup_at_or_before(
    series: list[tuple[datetime, float]],
    target_dt: datetime,
) -> tuple[float | None, datetime | None]:
    """Return latest price at-or-before target timestamp."""
    if not series:
        return None, None
    times = [t for t, _ in series]
    idx = bisect_right(times, target_dt) - 1
    if idx < 0:
        return None, None
    ts, px = series[idx]
    return px, ts


async def _tick_price_at_or_before(
    symbol: str,
    ts: datetime,
    *,
    max_age_minutes: int = 90,
) -> tuple[float | None, datetime | None]:
    """Fetch last tick at-or-before ts from TimescaleDB trades table."""
    dsn = os.getenv("TIMESCALE_DSN", "postgresql://tickdata:tickdata_dev@localhost:5433/tickdata")
    try:
        import asyncpg
    except Exception:
        return None, None

    try:
        conn = await asyncpg.connect(dsn)
    except Exception:
        return None, None
    try:
        row = await conn.fetchrow(
            """
            SELECT time, price
            FROM trades
            WHERE symbol = $1 AND time <= $2
            ORDER BY time DESC
            LIMIT 1
            """,
            symbol.upper(),
            ts,
        )
        if not row:
            return None, None
        px = float(row["price"])
        pt = row["time"]
        age = (ts - pt).total_seconds() / 60.0
        if age > max_age_minutes:
            return None, None
        return px, pt
    except Exception:
        return None, None
    finally:
        await conn.close()


def _pnl_pct(entry_price: float, exit_price: float, direction: str) -> float:
    """Direction-aware realized PnL percent."""
    if not entry_price:
        return 0.0
    pct = (exit_price - entry_price) / entry_price * 100.0
    if str(direction).lower() == "bearish":
        pct = -pct
    return round(pct, 4)


def _select_config_ids(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.all_sim:
        rows = conn.execute(
            """
            SELECT config_id
            FROM live_configs
            WHERE json_extract(config_json, '$.alpaca_account_id') IS NULL
            """,
        ).fetchall()
        return sorted([r[0] for r in rows if r[0]])
    if args.config_id:
        return args.config_id
    return ["lc_c45e48da2b27", "lc_026e07c6a0d8"]


def _load_candidates(conn: sqlite3.Connection, config_ids: list[str], all_replaced: bool) -> list[sqlite3.Row]:
    placeholders = ",".join(["?"] * len(config_ids))
    base_sql = f"""
        SELECT watch_id, symbol, status, watch_json
        FROM watches
        WHERE json_extract(watch_json, '$.live_config_id') IN ({placeholders})
          AND json_extract(watch_json, '$.exit.reason') = 'replaced'
          AND json_extract(watch_json, '$.exit.time') IS NOT NULL
          AND status IN ('exited', 'cooling_off', 'sealed')
        ORDER BY json_extract(watch_json, '$.exit.time')
    """
    rows = conn.execute(base_sql, config_ids).fetchall()
    if all_replaced:
        return rows
    out: list[sqlite3.Row] = []
    for r in rows:
        w = json.loads(r["watch_json"])
        entry = w.get("entry") or {}
        ex = w.get("exit") or {}
        ep = float(entry.get("price") or 0)
        xp = float(ex.get("price") or 0)
        rp = ex.get("realized_pnl_pct")
        rp0 = (rp is None) or abs(float(rp)) < 1e-9
        same_px = ep > 0 and xp > 0 and abs(ep - xp) < 1e-9
        if rp0 or same_px:
            out.append(r)
    return out


def _build_symbol_windows(rows: list[sqlite3.Row]) -> dict[str, tuple[datetime, datetime]]:
    windows: dict[str, tuple[datetime, datetime]] = {}
    for r in rows:
        w = json.loads(r["watch_json"])
        ex = w.get("exit") or {}
        t = parse_iso(ex["time"])
        sym = str(r["symbol"]).upper()
        start = t - timedelta(hours=3)
        end = t + timedelta(minutes=5)
        cur = windows.get(sym)
        if cur is None:
            windows[sym] = (start, end)
        else:
            windows[sym] = (min(cur[0], start), max(cur[1], end))
    return windows


def _fetch_schwab_series(
    schwab: SchwabMarketClient,
    windows: dict[str, tuple[datetime, datetime]],
) -> dict[str, list[tuple[datetime, float]]]:
    out: dict[str, list[tuple[datetime, float]]] = {}
    total = len(windows)
    for i, (sym, (start, end)) in enumerate(sorted(windows.items()), 1):
        try:
            candles: list[Candle] = schwab.get_candles_by_date_range(
                sym,
                start=start,
                end=end,
                frequency=1,
                extended_hours=True,
            )
            series = [(parse_iso(c.t), float(c.c)) for c in candles]
            series.sort(key=lambda x: x[0])
            out[sym] = series
        except Exception:
            out[sym] = []
        if i % 20 == 0 or i == total:
            print(f"  Schwab fetched {i}/{total}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill replacement exit prices")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    parser.add_argument(
        "--config-id",
        action="append",
        help="Live config ID (repeatable). Defaults to both sim configs.",
    )
    parser.add_argument(
        "--all-sim",
        action="store_true",
        help="Process all simulated portfolios (no alpaca_account_id).",
    )
    parser.add_argument(
        "--all-replaced",
        action="store_true",
        help="Recompute all replacement exits (default: only 0-pnl/same-price candidates).",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    config_ids = _select_config_ids(conn, args)
    if not config_ids:
        print("No config IDs selected.")
        return
    print(f"Configs: {', '.join(config_ids)}")

    rows = _load_candidates(conn, config_ids, args.all_replaced)
    if not rows:
        print("No candidate replacement exits found.")
        return
    print(f"Found {len(rows)} replacement exits to evaluate")

    windows = _build_symbol_windows(rows)
    print(f"Fetching Schwab 1m bars for {len(windows)} symbols...")
    schwab = SchwabMarketClient()
    schwab_series = _fetch_schwab_series(schwab, windows)

    updated = 0
    skipped = 0
    changed = 0
    by_source = {"tick": 0, "schwab": 0}

    for row in rows:
        watch_id = row["watch_id"]
        symbol = str(row["symbol"]).upper()
        wj = json.loads(row["watch_json"])
        entry = wj.get("entry") or {}
        ex = wj.get("exit") or {}
        if not ex or not ex.get("time"):
            skipped += 1
            continue

        entry_price = float(entry.get("price") or 0)
        exit_dt = parse_iso(ex["time"])
        direction = str(entry.get("direction") or "bullish")
        old_exit_price = float(ex.get("price") or 0)
        old_rpnl = float(ex.get("realized_pnl_pct") or 0)

        new_price: float | None = None
        px_ts: datetime | None = None
        src = ""

        tick_px, tick_ts = asyncio.run(_tick_price_at_or_before(symbol, exit_dt))
        if tick_px and tick_px > 0:
            new_price, px_ts, src = tick_px, tick_ts, "tick"
        else:
            series = schwab_series.get(symbol, [])
            sw_px, sw_ts = lookup_at_or_before(series, exit_dt)
            if sw_px and sw_px > 0:
                new_price, px_ts, src = sw_px, sw_ts, "schwab"

        if not new_price:
            print(f"  SKIP {symbol:6s} {watch_id} — no price source")
            skipped += 1
            continue

        rpnl = _pnl_pct(entry_price, new_price, direction)
        price_changed = abs(new_price - old_exit_price) > 1e-6
        pnl_changed = abs(rpnl - old_rpnl) > 1e-6
        if price_changed or pnl_changed:
            changed += 1
        by_source[src] = by_source.get(src, 0) + 1

        age_m = ((exit_dt - px_ts).total_seconds() / 60.0) if px_ts else None
        print(
            f"  {symbol:6s} {watch_id} src={src:6s} "
            f"entry={entry_price:8.3f} exit {old_exit_price:8.3f}->{new_price:8.3f} "
            f"pnl {old_rpnl:+7.3f}%->{rpnl:+7.3f}% age={age_m:.1f}m",
        )

        if not args.dry_run:
            ex["price"] = round(float(new_price), 4)
            ex["realized_pnl_pct"] = rpnl
            wj["exit"] = ex
            conn.execute(
                "UPDATE watches SET watch_json = ?, status = ? WHERE watch_id = ?",
                (json.dumps(wj), row["status"], watch_id),
            )
            updated += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    prefix = "DRY RUN — " if args.dry_run else ""
    print(f"\n{prefix}Summary")
    print(f"  candidates: {len(rows)}")
    print(f"  changed: {changed}")
    print(f"  updated: {updated}")
    print(f"  skipped: {skipped}")
    print(f"  source_counts: {by_source}")


if __name__ == "__main__":
    main()
