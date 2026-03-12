"""Backfill peak/trough PnL metrics for watches from 1-minute market history."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv(".env", override=False)
sys.path.insert(0, ".")

from trader.market.schwab_client import Candle, SchwabMarketClient


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _select_watches(
    conn: sqlite3.Connection,
    config_ids: list[str] | None,
) -> list[dict[str, Any]]:
    if config_ids:
        placeholders = ",".join(["?"] * len(config_ids))
        rows = conn.execute(
            f"""
            SELECT watch_json
            FROM watches
            WHERE json_extract(watch_json, '$.live_config_id') IN ({placeholders})
            ORDER BY created_at
            """,
            config_ids,
        ).fetchall()
    else:
        rows = conn.execute("SELECT watch_json FROM watches ORDER BY created_at").fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        raw = r[0]
        out.append(json.loads(raw) if isinstance(raw, str) else raw)
    return out


def _symbol_windows(watches: list[dict[str, Any]], now_utc: datetime) -> dict[str, tuple[datetime, datetime]]:
    windows: dict[str, tuple[datetime, datetime]] = {}
    for w in watches:
        entry = w.get("entry") or {}
        symbol = str(w.get("symbol") or "").upper()
        if not symbol or not entry.get("time"):
            continue
        try:
            entry_dt = parse_iso(entry["time"])
        except Exception:
            continue
        ex = w.get("exit") or {}
        if ex and ex.get("time"):
            try:
                end_dt = parse_iso(ex["time"])
            except Exception:
                end_dt = now_utc
        else:
            end_dt = now_utc
        start = entry_dt - timedelta(hours=2)
        end = end_dt + timedelta(minutes=2)
        cur = windows.get(symbol)
        if cur is None:
            windows[symbol] = (start, end)
        else:
            windows[symbol] = (min(cur[0], start), max(cur[1], end))
    return windows


def _fetch_series(
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
            pts = [(parse_iso(c.t), float(c.c)) for c in candles]
            pts.sort(key=lambda x: x[0])
            out[sym] = pts
        except Exception:
            out[sym] = []
        if i % 20 == 0 or i == total:
            print(f"  fetched {i}/{total} symbols")
    return out


def _pnl_pct(entry: float, px: float, direction: str) -> float:
    if entry <= 0:
        return 0.0
    p = (px - entry) / entry * 100.0
    if str(direction).lower() == "bearish":
        p = -p
    return p


def _compute_peak_trough(
    watch: dict[str, Any],
    series: list[tuple[datetime, float]],
    now_utc: datetime,
) -> tuple[float, float] | None:
    entry = watch.get("entry") or {}
    ex = watch.get("exit") or {}
    entry_price = float(entry.get("price") or 0)
    if entry_price <= 0 or not entry.get("time"):
        return None
    try:
        entry_dt = parse_iso(entry["time"])
    except Exception:
        return None

    if ex and ex.get("time"):
        try:
            end_dt = parse_iso(ex["time"])
        except Exception:
            end_dt = now_utc
    else:
        end_dt = now_utc
    if end_dt < entry_dt:
        end_dt = entry_dt

    direction = str(entry.get("direction") or "bullish")
    prices: list[float] = [entry_price]

    if series:
        times = [t for t, _ in series]
        i0 = bisect_left(times, entry_dt)
        i1 = bisect_right(times, end_dt)
        for _, px in series[i0:i1]:
            prices.append(px)

    exit_px = ex.get("price")
    if exit_px is not None:
        try:
            prices.append(float(exit_px))
        except Exception:
            pass

    pnl_vals = [_pnl_pct(entry_price, px, direction) for px in prices if px > 0]
    if not pnl_vals:
        return None
    return round(max(pnl_vals), 4), round(min(pnl_vals), 4)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill peak/trough watch PnL")
    parser.add_argument("--db", default="data/trader.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--config-id",
        action="append",
        help="Live config ID(s) to process (repeatable). Default: all watches.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute even if peak/trough already exist.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    now_utc = datetime.now(tz=timezone.utc)

    watches = _select_watches(conn, args.config_id)
    if not watches:
        print("No watches found.")
        return
    print(f"Loaded {len(watches)} watches")

    candidates: list[dict[str, Any]] = []
    for w in watches:
        if not args.force and w.get("peak_pnl_pct") is not None and w.get("trough_pnl_pct") is not None:
            continue
        candidates.append(w)
    print(f"Candidates: {len(candidates)}")
    if not candidates:
        return

    windows = _symbol_windows(candidates, now_utc)
    print(f"Fetching Schwab ranges for {len(windows)} symbols...")
    schwab = SchwabMarketClient()
    by_symbol = _fetch_series(schwab, windows)

    updated = 0
    skipped = 0
    for w in candidates:
        watch_id = w.get("watch_id")
        symbol = str(w.get("symbol") or "").upper()
        result = _compute_peak_trough(w, by_symbol.get(symbol, []), now_utc)
        if result is None:
            skipped += 1
            continue
        peak, trough = result
        old_peak = w.get("peak_pnl_pct")
        old_trough = w.get("trough_pnl_pct")
        if old_peak == peak and old_trough == trough:
            continue
        print(
            f"  {watch_id} {symbol:6s} peak {old_peak}->{peak:+.2f}% "
            f"trough {old_trough}->{trough:+.2f}%",
        )
        if not args.dry_run:
            w["peak_pnl_pct"] = peak
            w["trough_pnl_pct"] = trough
            conn.execute(
                "UPDATE watches SET watch_json = ? WHERE watch_id = ?",
                (json.dumps(w), watch_id),
            )
        updated += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    prefix = "DRY RUN — " if args.dry_run else ""
    print(f"\n{prefix}Updated {updated} watches; skipped {skipped}")


if __name__ == "__main__":
    main()
