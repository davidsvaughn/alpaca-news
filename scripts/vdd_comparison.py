#!/usr/bin/env python3
"""Phase 1: Compare bar-based vs tick-level VDD exit signals.

For each VDD-exited position in portfolio lc_ab8aa7f25745, compare when the
bar-based vs tick-level VDD signal first fires after entry.

See docs/VDD-COMPARISON.md for full methodology.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Imports from the codebase
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.market.backtest import (
    _compute_volume_delta,
    _compute_vdd_signal_indices,
    _get_ohlcv_1m,
    _filter_trading_hours,
)
from trader.market.volume_delta_shadow import SHADOW_CACHE_DIR

ET = ZoneInfo("US/Eastern")
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trader.db"
LIVE_CONFIG_ID = sys.argv[1] if len(sys.argv) > 1 else "lc_ab8aa7f25745"
LOOKBACK = 80
MIN_HOLD = 5
GUARD_STOP_PCT = 5.0

# Minimum shadow coverage (fraction of OHLCV bars matched) to consider
# a position's tick-level result trustworthy.
MIN_COVERAGE = 0.50


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_vdd_watches() -> list[dict]:
    """Load all VDD-exited watches for the target live config."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT symbol, status, created_at, watch_json FROM watches "
        "WHERE watch_json LIKE ? AND status != 'holding' "
        "ORDER BY created_at",
        (f"%{LIVE_CONFIG_ID}%",),
    ).fetchall()
    db.close()

    watches = []
    for r in rows:
        wj = json.loads(r["watch_json"])
        exit_info = wj.get("exit", {})
        if exit_info.get("reason") != "signal":
            continue  # Skip guard_stop exits — VDD didn't trigger
        entry = wj.get("entry", {})
        watches.append({
            "symbol": r["symbol"],
            "entry_time": entry.get("time", r["created_at"]),
            "entry_price": entry.get("price", 0.0),
            "exit_time": exit_info.get("time"),
            "exit_price": exit_info.get("price"),
            "exit_pnl_pct": exit_info.get("realized_pnl_pct"),
        })
    return watches


def load_shadow_bars_from_jsonl(symbol: str) -> pd.DataFrame:
    """Load shadow minute bars from JSONL bar logs (crash-safe, per-day files).

    These are the ground-truth per-day files. The daily JSON summaries for
    non-streaming days (weekends) are stale copies and should be skipped.

    Returns a DataFrame indexed by tz-naive Eastern time (floored to minute)
    with columns: Open, High, Low, Close, Volume, uptick, downtick, delta
    """
    bars_dir = SHADOW_CACHE_DIR / symbol.upper() / "bars"
    if not bars_dir.exists():
        # Fall back to daily JSON files
        return _load_shadow_bars_from_json(symbol)

    all_bars = []
    for jsonl_file in sorted(bars_dir.glob("*.jsonl")):
        text = jsonl_file.read_text().strip()
        if not text:
            continue
        for line in text.split("\n"):
            try:
                all_bars.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not all_bars:
        return _load_shadow_bars_from_json(symbol)

    return _shadow_bars_to_df(all_bars)


def _load_shadow_bars_from_json(symbol: str) -> pd.DataFrame:
    """Fallback: load from daily JSON files, deduplicating by timestamp."""
    sym_dir = SHADOW_CACHE_DIR / symbol.upper()
    if not sym_dir.exists():
        return pd.DataFrame()

    all_bars = []
    for json_file in sorted(sym_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text())
        except json.JSONDecodeError:
            continue
        if data and data.get("minute_bars"):
            all_bars.extend(data["minute_bars"])

    if not all_bars:
        return pd.DataFrame()

    return _shadow_bars_to_df(all_bars)


def _shadow_bars_to_df(bars: list[dict]) -> pd.DataFrame:
    """Convert shadow bar dicts to a DataFrame with tz-naive Eastern minute index."""
    df = pd.DataFrame(bars)
    # Handle mixed timestamp formats (some with microseconds, some without)
    df["t"] = pd.to_datetime(df["t"], format="ISO8601", utc=True)
    # Convert to tz-naive Eastern and floor to minute
    df["t"] = df["t"].dt.tz_convert(ET).dt.tz_localize(None).dt.floor("min")
    df.set_index("t", inplace=True)
    # Deduplicate — keep last occurrence per minute (most complete data)
    df = df[~df.index.duplicated(keep="last")]
    df.sort_index(inplace=True)
    df.rename(columns={
        "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume",
    }, inplace=True)
    return df


def entry_to_eastern_naive(entry_time: str) -> pd.Timestamp:
    """Convert entry time (UTC ISO) to tz-naive Eastern pd.Timestamp."""
    dt = datetime.fromisoformat(entry_time)
    if dt.tzinfo is not None:
        dt = dt.astimezone(ET).replace(tzinfo=None)
    return pd.Timestamp(dt).floor("min")


# ---------------------------------------------------------------------------
# VDD signal computation
# ---------------------------------------------------------------------------

def find_first_vdd_signal(
    df: pd.DataFrame,
    uptick: pd.Series,
    downtick: pd.Series,
    entry_idx: int,
    lookback: int,
) -> int | None:
    """Find the first VDD signal index at or after entry + lookback."""
    cum_delta = (uptick - downtick).cumsum()
    signal_indices = _compute_vdd_signal_indices(df["Close"], cum_delta, lookback)
    start = max(0, entry_idx + lookback)
    pos = int(np.searchsorted(signal_indices, start))
    if pos < len(signal_indices):
        return int(signal_indices[pos])
    return None


# ---------------------------------------------------------------------------
# Main comparison
# ---------------------------------------------------------------------------

def run_comparison():
    watches = load_vdd_watches()
    print(f"\nLoaded {len(watches)} VDD-exited positions from {LIVE_CONFIG_ID}")
    print(f"Shadow data: {SHADOW_CACHE_DIR}\n")

    results = []
    skipped = []

    for w in watches:
        symbol = w["symbol"]
        entry_time = w["entry_time"]
        exit_time = w["exit_time"]
        entry_price = w["entry_price"]

        # Load OHLCV bars — need bars from before entry (for lookback)
        entry_dt = datetime.fromisoformat(entry_time)
        start_date = (entry_dt - timedelta(days=5)).strftime("%Y-%m-%d")
        exit_dt = datetime.fromisoformat(exit_time)
        end_date = exit_dt.strftime("%Y-%m-%d")

        ohlcv = _get_ohlcv_1m(symbol, start_date, end_date)
        if ohlcv is None or ohlcv.empty:
            skipped.append((symbol, "no OHLCV bar data"))
            continue

        # Filter to trading hours
        ohlcv = _filter_trading_hours(ohlcv, "16:00")
        if ohlcv.empty:
            skipped.append((symbol, "no bars in trading hours"))
            continue

        # Find entry index in OHLCV bars
        entry_ts = entry_to_eastern_naive(entry_time)
        entry_idx = int(ohlcv.index.searchsorted(entry_ts))
        if entry_idx >= len(ohlcv):
            entry_idx = len(ohlcv) - 1

        # Load shadow tick bars
        shadow = load_shadow_bars_from_jsonl(symbol)
        if shadow.empty:
            skipped.append((symbol, "no shadow tick data"))
            continue

        # --- Bar-based VDD ---
        bar_uptick, bar_downtick = _compute_volume_delta(ohlcv)
        bar_signal_idx = find_first_vdd_signal(
            ohlcv, bar_uptick, bar_downtick, entry_idx, LOOKBACK,
        )

        # --- Tick-level VDD ---
        # For each OHLCV bar, look up matching shadow bar by minute timestamp.
        # If no match, fall back to bar-based classification for that bar.
        tick_uptick = bar_uptick.copy()
        tick_downtick = bar_downtick.copy()

        matched = 0
        for i, ts in enumerate(ohlcv.index):
            if ts in shadow.index:
                row = shadow.loc[ts]
                tick_uptick.iloc[i] = row["uptick"]
                tick_downtick.iloc[i] = row["downtick"]
                matched += 1

        coverage = matched / len(ohlcv) if len(ohlcv) > 0 else 0

        # Also compute coverage in the signal-relevant window
        # (entry_idx through end of data — the region where VDD actually matters)
        relevant_start = max(0, entry_idx - LOOKBACK)  # Need lookback bars before entry too
        relevant_bars = ohlcv.index[relevant_start:]
        relevant_matched = sum(1 for ts in relevant_bars if ts in shadow.index)
        relevant_coverage = relevant_matched / len(relevant_bars) if len(relevant_bars) > 0 else 0

        tick_signal_idx = find_first_vdd_signal(
            ohlcv, tick_uptick, tick_downtick, entry_idx, LOOKBACK,
        )

        # --- Compute results ---
        bar_exit_time = ohlcv.index[bar_signal_idx] if bar_signal_idx is not None else None
        tick_exit_time = ohlcv.index[tick_signal_idx] if tick_signal_idx is not None else None

        bar_exit_price = float(ohlcv["Close"].iloc[bar_signal_idx]) if bar_signal_idx is not None else None
        tick_exit_price = float(ohlcv["Close"].iloc[tick_signal_idx]) if tick_signal_idx is not None else None

        bar_pnl = ((bar_exit_price / entry_price) - 1) * 100 if bar_exit_price and entry_price else None
        tick_pnl = ((tick_exit_price / entry_price) - 1) * 100 if tick_exit_price and entry_price else None

        delta_min = None
        if bar_exit_time is not None and tick_exit_time is not None:
            delta_min = int((tick_exit_time - bar_exit_time).total_seconds() / 60)

        trustworthy = relevant_coverage >= MIN_COVERAGE

        result = {
            "symbol": symbol,
            "entry": entry_ts,
            "bar_exit": bar_exit_time,
            "tick_exit": tick_exit_time,
            "delta_min": delta_min,
            "bar_price": bar_exit_price,
            "tick_price": tick_exit_price,
            "bar_pnl": bar_pnl,
            "tick_pnl": tick_pnl,
            "coverage": coverage,
            "relevant_coverage": relevant_coverage,
            "trustworthy": trustworthy,
            "ohlcv_bars": len(ohlcv),
            "shadow_matched": matched,
            "relevant_matched": relevant_matched,
            "relevant_total": len(relevant_bars),
            "actual_pnl": w["exit_pnl_pct"],
        }
        results.append(result)

    # --- Print results ---
    print(f"{'Symbol':8s} {'Bar Exit':14s} {'Tick Exit':14s} {'Delta':>6s} "
          f"{'Cov%':>5s} {'RCov%':>5s} {'Bar P&L':>8s} {'Tick P&L':>9s} {'Trust':>5s}")
    print("-" * 90)

    for r in results:
        bar_str = r["bar_exit"].strftime("%m/%d %H:%M") if r["bar_exit"] else "no signal"
        tick_str = r["tick_exit"].strftime("%m/%d %H:%M") if r["tick_exit"] else "no signal"
        delta_str = f"{r['delta_min']:+d}" if r["delta_min"] is not None else "N/A"
        cov_str = f"{r['coverage']*100:.0f}%"
        rcov_str = f"{r['relevant_coverage']*100:.0f}%"
        bar_pnl_str = f"{r['bar_pnl']:+.2f}%" if r["bar_pnl"] is not None else "N/A"
        tick_pnl_str = f"{r['tick_pnl']:+.2f}%" if r["tick_pnl"] is not None else "N/A"
        trust_str = "YES" if r["trustworthy"] else "no"

        print(f"{r['symbol']:8s} {bar_str:14s} {tick_str:14s} {delta_str:>6s} "
              f"{cov_str:>5s} {rcov_str:>5s} {bar_pnl_str:>8s} {tick_pnl_str:>9s} {trust_str:>5s}")

    if skipped:
        print(f"\nSkipped {len(skipped)} positions:")
        for sym, reason in skipped:
            print(f"  {sym:8s} {reason}")

    # --- Summary statistics ---
    print("\n" + "=" * 90)
    print("SUMMARY")
    print("=" * 90)

    if not results:
        print("No results to summarize.")
        return

    # Split by trustworthiness
    trusted = [r for r in results if r["trustworthy"]]
    untrusted = [r for r in results if not r["trustworthy"]]

    print(f"\nTotal positions analyzed:  {len(results)}")
    print(f"  Trustworthy (>={MIN_COVERAGE*100:.0f}% relevant coverage): {len(trusted)}")
    print(f"  Low coverage (unreliable):   {len(untrusted)}")

    for label, subset in [("ALL positions", results), ("TRUSTWORTHY only", trusted)]:
        if not subset:
            continue
        print(f"\n--- {label} ({len(subset)}) ---")

        both = [r for r in subset if r["bar_exit"] is not None and r["tick_exit"] is not None]
        bar_only = [r for r in subset if r["bar_exit"] is not None and r["tick_exit"] is None]
        tick_only = [r for r in subset if r["bar_exit"] is None and r["tick_exit"] is not None]
        neither = [r for r in subset if r["bar_exit"] is None and r["tick_exit"] is None]

        print(f"  Both signaled:  {len(both)}")
        print(f"  Bar-only:       {len(bar_only)}")
        print(f"  Tick-only:      {len(tick_only)}")
        print(f"  Neither:        {len(neither)}")

        if both:
            deltas = [r["delta_min"] for r in both if r["delta_min"] is not None]
            earlier = sum(1 for d in deltas if d < 0)
            later = sum(1 for d in deltas if d > 0)
            same = sum(1 for d in deltas if d == 0)

            print(f"\n  Tick fired EARLIER: {earlier} ({earlier/len(deltas)*100:.0f}%)")
            print(f"  Tick fired LATER:   {later} ({later/len(deltas)*100:.0f}%)")
            print(f"  Same bar:           {same} ({same/len(deltas)*100:.0f}%)")

            if deltas:
                print(f"\n  Median delta:  {np.median(deltas):+.0f} min")
                print(f"  Mean delta:    {np.mean(deltas):+.1f} min")
                print(f"  Range:         {min(deltas):+d} to {max(deltas):+d} min")

            bar_pnls = [r["bar_pnl"] for r in both if r["bar_pnl"] is not None]
            tick_pnls = [r["tick_pnl"] for r in both if r["tick_pnl"] is not None]
            if bar_pnls and tick_pnls:
                print(f"\n  Avg bar P&L:   {np.mean(bar_pnls):+.2f}%")
                print(f"  Avg tick P&L:  {np.mean(tick_pnls):+.2f}%")
                print(f"  P&L diff:      {np.mean(tick_pnls) - np.mean(bar_pnls):+.2f}%")

    # Coverage stats
    coverages = [r["relevant_coverage"] for r in results]
    print(f"\nRelevant-window shadow coverage:")
    print(f"  Mean:  {np.mean(coverages)*100:.1f}%")
    print(f"  Min:   {min(coverages)*100:.1f}%")
    print(f"  Max:   {max(coverages)*100:.1f}%")


if __name__ == "__main__":
    run_comparison()
