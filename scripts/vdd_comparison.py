#!/usr/bin/env python3
"""Compare bar-based vs tick-based VDD exit signals.

For each VDD-exited position across one or more portfolios, compare when the
bar-based VDD signal fires vs tick-based VDD (from TimescaleDB) across multiple
configurations:

  - Bucket sizes: 15s, 30s, 60s
  - Volume modes: proportional, visible_only, bar_binary
  - Lookback periods: 40, 60, 80, 100 minutes

Requires TimescaleDB running with tick data collected by tick_collector.
Uses Schwab (primary) / yfinance (fallback) for bar-based OHLCV via backtest infrastructure.

TODO: Add trade-count bucketing (tick clock) to the sweep.
  - `get_vdd_bars_by_trades()` in tick_collector/vdd.py buckets by trade count
    instead of fixed time intervals. Each bar has exactly N L1 updates,
    guaranteeing equal statistical weight regardless of liquidity.
  - Sweep trades_per_bar: [10, 20, 30, 50] alongside existing time-based configs.
  - This addresses the sparse-bucket problem: thin stocks get wider bars
    automatically instead of noisy 30s bars with 1-2 trades.
  - See docs/VDD-COMPARISON.md § Trade-Count Bucketing for design rationale.

Usage:
    python scripts/vdd_comparison.py [live_config_id ...]

Defaults to all four portfolios if no args given.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Imports from the codebase
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so Schwab keys are available
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from trader.market.backtest import (
    _compute_volume_delta,
    _compute_vdd_signal_indices,
    _get_ohlcv_1m,
)
from tick_collector.vdd import (
    VolumeMode,
    get_vdd_bars,
    compute_vdd_signal,
    find_first_signal,
    DEFAULT_DSN,
)

import asyncpg

ET = ZoneInfo("US/Eastern")
UTC = timezone.utc
DB_PATH = ROOT / "data" / "trader.db"

DEFAULT_PORTFOLIOS = [
    "lc_d58c66d24787",
    "lc_72f6df86086e",
    "lc_ab8aa7f25745",
    "lc_dd6b0b29a8ea",
]
LIVE_CONFIG_IDS = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_PORTFOLIOS

# Comparison dimensions
BUCKET_SIZES = [15, 30, 60]
VOLUME_MODES: list[VolumeMode] = ["proportional", "visible_only", "bar_binary"]
LOOKBACKS_M = [40, 60, 80, 100]
DEFAULT_LOOKBACK = 80
DEFAULT_BUCKET = 60  # 60s for apples-to-apples comparison with 1-min bar-based
MIN_TRADES = 2  # lower threshold for finer buckets

# Guard stop used by this portfolio
GUARD_STOP_PCT = 5.0


# ---------------------------------------------------------------------------
# Data loading from SQLite
# ---------------------------------------------------------------------------

def load_watches(live_config_ids: list[str], signal_only: bool = False) -> list[dict]:
    """Load exited watches across multiple portfolios.

    Deduplicates by (symbol, entry_time) to avoid double-counting when the
    same position appears in multiple accounts.
    """
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    all_rows = []
    for lc_id in live_config_ids:
        rows = db.execute(
            "SELECT symbol, status, created_at, watch_json FROM watches "
            "WHERE watch_json LIKE ? "
            "ORDER BY created_at",
            (f"%{lc_id}%",),
        ).fetchall()
        all_rows.extend(rows)
    db.close()

    seen = set()
    watches = []
    for r in all_rows:
        wj = json.loads(r["watch_json"])
        exit_info = wj.get("exit") or {}
        if not exit_info.get("time"):
            continue  # Still holding
        reason = exit_info.get("reason", "")
        if signal_only and reason != "signal":
            continue
        entry = wj.get("entry", {})
        entry_time = entry.get("time", r["created_at"])

        # Deduplicate by (symbol, entry_time)
        key = (r["symbol"], entry_time)
        if key in seen:
            continue
        seen.add(key)

        watches.append({
            "symbol": r["symbol"],
            "entry_time": entry_time,
            "entry_price": entry.get("price", 0.0),
            "exit_time": exit_info.get("time"),
            "exit_price": exit_info.get("price"),
            "exit_pnl_pct": exit_info.get("realized_pnl_pct"),
            "exit_reason": reason,
            "lookback": wj.get("exit_params", {}).get("lookback", 80),
            "peak_pnl_pct": wj.get("peak_pnl_pct"),
            "trough_pnl_pct": wj.get("trough_pnl_pct"),
            "live_config_id": wj.get("live_config_id", ""),
        })
    return watches


def parse_utc(ts_str: str) -> datetime:
    """Parse ISO timestamp to UTC datetime."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Bar-based VDD (from Schwab/yfinance 1-min candles via MarketDataService)
# ---------------------------------------------------------------------------

def get_bar_based_ohlcv(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame | None:
    """Fetch 1-min OHLCV bars using the same backtest infrastructure.

    Uses _get_ohlcv_1m() which does Schwab → yfinance with persistent disk cache.
    Returns DataFrame with tz-naive Eastern index (matching backtest convention),
    columns: Open, High, Low, Close, Volume.
    """
    start_str = start_dt.astimezone(ET).strftime("%Y-%m-%d")
    end_str = end_dt.astimezone(ET).strftime("%Y-%m-%d")
    return _get_ohlcv_1m(symbol, start_str, end_str)


def find_bar_based_signal(
    ohlcv: pd.DataFrame,
    entry_time_utc: datetime,
    lookback: int,
) -> dict | None:
    """Find first bar-based VDD signal after entry.

    Returns dict with signal details or None.
    """
    # _get_ohlcv_1m returns tz-naive Eastern index — match that
    entry_et = entry_time_utc.astimezone(ET)
    entry_ts = pd.Timestamp(entry_et.replace(tzinfo=None)).floor("s")

    entry_idx = int(ohlcv.index.searchsorted(entry_ts))
    if entry_idx >= len(ohlcv):
        return None

    uptick, downtick = _compute_volume_delta(ohlcv)
    cum_delta = (uptick - downtick).cumsum()
    signal_indices = _compute_vdd_signal_indices(ohlcv["Close"], cum_delta, lookback)

    start = max(0, entry_idx + lookback)
    pos = int(np.searchsorted(signal_indices, start))
    if pos < len(signal_indices):
        idx = int(signal_indices[pos])
        # Convert tz-naive Eastern back to UTC for comparison
        bar_time_et = ohlcv.index[idx]
        bar_time_utc = bar_time_et.tz_localize(ET).astimezone(UTC)
        return {
            "time_et": bar_time_et,
            "time_utc": bar_time_utc,
            "close": float(ohlcv["Close"].iloc[idx]),
            "idx": idx,
            "entry_idx": entry_idx,
            "bars_held": idx - entry_idx,
        }
    return None


# ---------------------------------------------------------------------------
# Tick-based VDD (from TimescaleDB)
# ---------------------------------------------------------------------------

def _filter_market_hours(bars: pd.DataFrame) -> pd.DataFrame:
    """Filter tick bars to regular market hours (9:30-16:00 ET) only.

    The live monitor only runs during trading sessions, so signals outside
    market hours would never have been acted on. Including them biases the
    comparison toward "tick fired earlier" when the system was actually off.
    """
    import datetime as _dt
    if bars.empty or "bucket" not in bars.columns:
        return bars
    # Convert bucket to ET for filtering
    buckets_et = bars["bucket"].dt.tz_convert(ET) if bars["bucket"].dt.tz is not None else bars["bucket"]
    times = buckets_et.dt.time
    mask = (times >= _dt.time(9, 30)) & (times < _dt.time(16, 0))
    return bars[mask].reset_index(drop=True)


async def find_tick_based_signal(
    pool: asyncpg.Pool,
    symbol: str,
    entry_time_utc: datetime,
    end_time_utc: datetime,
    lookback_m: float,
    bucket_s: int,
    volume_mode: VolumeMode,
    market_hours_only: bool = True,
) -> dict | None:
    """Find first tick-based VDD signal after entry.

    Queries TimescaleDB for the full time range and runs signal detection.
    If market_hours_only is True, filters to 9:30-16:00 ET (matching when
    the live monitor would actually be checking).
    """
    # Query from before entry (need lookback history) to after exit
    buffer = timedelta(minutes=lookback_m * 1.2 + 5)
    start = entry_time_utc - buffer
    end = end_time_utc + timedelta(minutes=30)

    bars = await get_vdd_bars(
        pool, symbol,
        lookback_m=lookback_m,
        bucket_s=bucket_s,
        min_trades_per_bucket=MIN_TRADES,
        volume_mode=volume_mode,
        start_time=start,
        end_time=end,
    )
    if bars is None or bars.empty:
        return None

    if market_hours_only:
        bars = _filter_market_hours(bars)
        if bars.empty:
            return None

    lookback_bars = int(lookback_m * 60 / bucket_s)
    entry_bucket = entry_time_utc.replace(second=0, microsecond=0)

    result = find_first_signal(bars, lookback_bars, after_bucket=entry_bucket)
    if result is None:
        return None

    result["bars_total"] = len(bars)
    return result


# ---------------------------------------------------------------------------
# Coverage check
# ---------------------------------------------------------------------------

async def check_tick_coverage(
    pool: asyncpg.Pool,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict:
    """Check tick data coverage for a symbol in a time range."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COUNT(*) as trade_count,
                   MIN(time) as first_trade,
                   MAX(time) as last_trade,
                   COUNT(DISTINCT time_bucket('1 minute', time)) as minutes_with_data
            FROM trades
            WHERE symbol = $1 AND time >= $2 AND time < $3
              AND volume_delta IS NOT NULL
        """, symbol, start_utc, end_utc)

    expected_minutes = max(1, int((end_utc - start_utc).total_seconds() / 60))
    actual = row["minutes_with_data"] or 0

    return {
        "trade_count": row["trade_count"] or 0,
        "minutes_with_data": actual,
        "expected_minutes": expected_minutes,
        "coverage_pct": actual / expected_minutes * 100,
        "first_trade": row["first_trade"],
        "last_trade": row["last_trade"],
    }


# ---------------------------------------------------------------------------
# Main comparison
# ---------------------------------------------------------------------------

async def run_comparison():
    pool = await asyncpg.create_pool(DEFAULT_DSN, min_size=1, max_size=4)

    watches = load_watches(LIVE_CONFIG_IDS, signal_only=False)
    signal_exits = sum(1 for w in watches if w["exit_reason"] == "signal")
    guard_exits = sum(1 for w in watches if w["exit_reason"] == "guard_stop")
    print(f"\nLoaded {len(watches)} exited positions from {len(LIVE_CONFIG_IDS)} portfolios")
    print(f"  Portfolios: {', '.join(LIVE_CONFIG_IDS)}")
    print(f"  signal exits: {signal_exits}, guard_stop exits: {guard_exits}")

    # -----------------------------------------------------------------------
    # Phase 1: Coverage check — which positions have usable tick data?
    # -----------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("PHASE 1: TICK DATA COVERAGE CHECK")
    print("=" * 100)

    usable = []
    skipped = []

    for w in watches:
        symbol = w["symbol"]
        entry_utc = parse_utc(w["entry_time"])
        exit_utc = parse_utc(w["exit_time"])

        cov = await check_tick_coverage(pool, symbol, entry_utc, exit_utc)

        if cov["trade_count"] < 10:
            skipped.append((symbol, f"only {cov['trade_count']} trades"))
            continue

        if cov["coverage_pct"] < 30:
            skipped.append((symbol, f"coverage {cov['coverage_pct']:.0f}%"))
            continue

        w["coverage"] = cov
        usable.append(w)

    print(f"\nUsable positions: {len(usable)}")
    print(f"Skipped: {len(skipped)}")
    for sym, reason in skipped[:10]:
        print(f"  {sym:8s} — {reason}")
    if len(skipped) > 10:
        print(f"  ... and {len(skipped) - 10} more")

    if not usable:
        print("\nNo positions with sufficient tick coverage. Exiting.")
        await pool.close()
        return

    # Print coverage table
    print(f"\n{'Symbol':8s} {'Reason':11s} {'Entry (ET)':18s} {'Exit (ET)':18s} {'P&L%':>7s} "
          f"{'Trades':>7s} {'MinsData':>8s} {'Cov%':>5s}")
    print("-" * 95)
    for w in usable:
        c = w["coverage"]
        entry_et = parse_utc(w["entry_time"]).astimezone(ET)
        exit_et = parse_utc(w["exit_time"]).astimezone(ET)
        pnl = w["exit_pnl_pct"]
        reason = w["exit_reason"]
        print(f"{w['symbol']:8s} {reason:11s} {entry_et.strftime('%m/%d %H:%M'):18s} "
              f"{exit_et.strftime('%m/%d %H:%M'):18s} {pnl:+7.2f} "
              f"{c['trade_count']:7d} {c['minutes_with_data']:8d} {c['coverage_pct']:5.1f}")

    # -----------------------------------------------------------------------
    # Phase 2: Default config comparison (bar-based vs tick-based, lookback=80, 60s buckets)
    # -----------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("PHASE 2: BAR-BASED vs TICK-BASED (default: 60s bucket, proportional, lookback=80)")
    print("=" * 100)

    phase2_results = []
    for w in usable:
        symbol = w["symbol"]
        entry_utc = parse_utc(w["entry_time"])
        exit_utc = parse_utc(w["exit_time"])
        entry_price = w["entry_price"]

        # Bar-based signal
        ohlcv = get_bar_based_ohlcv(symbol, entry_utc, exit_utc)
        bar_result = None
        if ohlcv is not None:
            bar_result = find_bar_based_signal(ohlcv, entry_utc, DEFAULT_LOOKBACK)

        # Tick-based signal (default: 60s proportional)
        tick_result = await find_tick_based_signal(
            pool, symbol, entry_utc, exit_utc,
            lookback_m=DEFAULT_LOOKBACK, bucket_s=DEFAULT_BUCKET,
            volume_mode="proportional",
        )

        bar_time = bar_result["time_utc"] if bar_result else None
        tick_time = tick_result["bucket"] if tick_result else None

        # Ensure tick_time is tz-aware UTC for comparison
        if tick_time is not None and tick_time.tzinfo is None:
            tick_time = tick_time.replace(tzinfo=UTC)

        delta_min = None
        if bar_time and tick_time:
            delta_min = (tick_time - bar_time).total_seconds() / 60

        bar_price = bar_result["close"] if bar_result else None
        tick_price = tick_result["close"] if tick_result else None

        bar_pnl = ((bar_price / entry_price) - 1) * 100 if bar_price and entry_price else None
        tick_pnl = ((tick_price / entry_price) - 1) * 100 if tick_price and entry_price else None

        phase2_results.append({
            "symbol": symbol,
            "exit_reason": w["exit_reason"],
            "entry_price": entry_price,
            "actual_pnl": w["exit_pnl_pct"],
            "bar_time": bar_time,
            "tick_time": tick_time,
            "delta_min": delta_min,
            "bar_price": bar_price,
            "tick_price": tick_price,
            "bar_pnl": bar_pnl,
            "tick_pnl": tick_pnl,
            "coverage_pct": w["coverage"]["coverage_pct"],
        })

    _print_comparison_table(phase2_results, "Phase 2")

    # -----------------------------------------------------------------------
    # Phase 3: Multi-dimensional sweep
    # -----------------------------------------------------------------------
    print("\n" + "=" * 100)
    print("PHASE 3: MULTI-DIMENSIONAL SWEEP")
    print("=" * 100)

    # For each config combo, compute average P&L delta vs bar-based
    sweep_results = []

    for bucket_s in BUCKET_SIZES:
        for mode in VOLUME_MODES:
            for lookback_m in LOOKBACKS_M:
                config_results = []

                for w in usable:
                    symbol = w["symbol"]
                    entry_utc = parse_utc(w["entry_time"])
                    exit_utc = parse_utc(w["exit_time"])
                    entry_price = w["entry_price"]

                    tick_result = await find_tick_based_signal(
                        pool, symbol, entry_utc, exit_utc,
                        lookback_m=lookback_m, bucket_s=bucket_s,
                        volume_mode=mode,
                    )

                    tick_price = tick_result["close"] if tick_result else None
                    tick_pnl = ((tick_price / entry_price) - 1) * 100 if tick_price and entry_price else None

                    tick_time = tick_result["bucket"] if tick_result else None
                    if tick_time is not None and tick_time.tzinfo is None:
                        tick_time = tick_time.replace(tzinfo=UTC)

                    config_results.append({
                        "symbol": symbol,
                        "tick_pnl": tick_pnl,
                        "tick_time": tick_time,
                        "actual_pnl": w["exit_pnl_pct"],
                    })

                # Aggregate
                pnls = [r["tick_pnl"] for r in config_results if r["tick_pnl"] is not None]
                signals_found = sum(1 for r in config_results if r["tick_pnl"] is not None)

                sweep_results.append({
                    "bucket_s": bucket_s,
                    "mode": mode,
                    "lookback_m": lookback_m,
                    "signals_found": signals_found,
                    "total": len(config_results),
                    "avg_pnl": np.mean(pnls) if pnls else None,
                    "median_pnl": np.median(pnls) if pnls else None,
                    "std_pnl": np.std(pnls) if len(pnls) > 1 else None,
                })

    # Print sweep results sorted by avg P&L
    print(f"\n{'Bucket':>6s} {'Mode':>14s} {'Lookback':>8s} {'Signals':>7s} "
          f"{'AvgP&L':>8s} {'MedP&L':>8s} {'StdP&L':>8s}")
    print("-" * 70)

    sweep_sorted = sorted(sweep_results, key=lambda r: r["avg_pnl"] or -999, reverse=True)
    for r in sweep_sorted:
        avg = f"{r['avg_pnl']:+.2f}%" if r["avg_pnl"] is not None else "N/A"
        med = f"{r['median_pnl']:+.2f}%" if r["median_pnl"] is not None else "N/A"
        std = f"{r['std_pnl']:.2f}%" if r["std_pnl"] is not None else "N/A"
        print(f"{r['bucket_s']:>4d}s {r['mode']:>14s} {r['lookback_m']:>6.0f}m "
              f"{r['signals_found']:>3d}/{r['total']:<3d} {avg:>8s} {med:>8s} {std:>8s}")

    # Highlight best config
    best = sweep_sorted[0] if sweep_sorted and sweep_sorted[0]["avg_pnl"] is not None else None
    if best:
        print(f"\nBest config: bucket={best['bucket_s']}s, mode={best['mode']}, "
              f"lookback={best['lookback_m']}m → avg P&L {best['avg_pnl']:+.2f}%")

    # Compare to bar-based baseline
    bar_pnls = [r["bar_pnl"] for r in phase2_results if r["bar_pnl"] is not None]
    if bar_pnls:
        print(f"Bar-based baseline (lookback=80): avg P&L {np.mean(bar_pnls):+.2f}%")

    await pool.close()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_comparison_table(results: list[dict], phase_label: str):
    """Print a formatted comparison table."""
    print(f"\n{'Symbol':8s} {'Reason':11s} {'Bar Exit (ET)':16s} {'Tick Exit (ET)':16s} "
          f"{'Delta':>7s} {'BarP&L':>8s} {'TickP&L':>8s} {'ActP&L':>8s} {'Cov%':>5s}")
    print("-" * 100)

    for r in results:
        bar_str = r["bar_time"].astimezone(ET).strftime("%m/%d %H:%M") if r["bar_time"] else "—"
        tick_str = r["tick_time"].astimezone(ET).strftime("%m/%d %H:%M") if r["tick_time"] else "—"
        delta_str = f"{r['delta_min']:+.0f}m" if r["delta_min"] is not None else "—"
        bar_pnl = f"{r['bar_pnl']:+.2f}%" if r["bar_pnl"] is not None else "—"
        tick_pnl = f"{r['tick_pnl']:+.2f}%" if r["tick_pnl"] is not None else "—"
        act_pnl = f"{r['actual_pnl']:+.2f}%" if r["actual_pnl"] is not None else "—"
        cov = f"{r['coverage_pct']:.0f}"
        reason = r.get("exit_reason", "?")

        print(f"{r['symbol']:8s} {reason:11s} {bar_str:16s} {tick_str:16s} "
              f"{delta_str:>7s} {bar_pnl:>8s} {tick_pnl:>8s} {act_pnl:>8s} {cov:>5s}")

    # Summary stats
    both = [r for r in results if r["bar_pnl"] is not None and r["tick_pnl"] is not None]
    if both:
        bar_pnls = [r["bar_pnl"] for r in both]
        tick_pnls = [r["tick_pnl"] for r in both]
        deltas = [r["delta_min"] for r in both if r["delta_min"] is not None]

        print(f"\n{phase_label} Summary ({len(both)} positions with both signals):")
        print(f"  Bar avg P&L:  {np.mean(bar_pnls):+.2f}%  (median {np.median(bar_pnls):+.2f}%)")
        print(f"  Tick avg P&L: {np.mean(tick_pnls):+.2f}%  (median {np.median(tick_pnls):+.2f}%)")
        print(f"  P&L diff:     {np.mean(tick_pnls) - np.mean(bar_pnls):+.2f}%")

        if deltas:
            earlier = sum(1 for d in deltas if d < -0.5)
            later = sum(1 for d in deltas if d > 0.5)
            same = len(deltas) - earlier - later
            print(f"\n  Tick earlier: {earlier}  |  Same (±30s): {same}  |  Tick later: {later}")
            print(f"  Median delta: {np.median(deltas):+.1f} min")

    no_tick = [r for r in results if r["tick_pnl"] is None and r["bar_pnl"] is not None]
    no_bar = [r for r in results if r["bar_pnl"] is None and r["tick_pnl"] is not None]
    if no_tick:
        print(f"\n  Bar signaled but tick did NOT: {len(no_tick)} "
              f"({', '.join(r['symbol'] for r in no_tick)})")
    if no_bar:
        print(f"  Tick signaled but bar did NOT: {len(no_bar)} "
              f"({', '.join(r['symbol'] for r in no_bar)})")


if __name__ == "__main__":
    asyncio.run(run_comparison())
