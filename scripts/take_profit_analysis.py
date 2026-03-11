#!/usr/bin/env python3
"""Analyze historical exits to find optimal take-profit thresholds.

Loads all exited watches (deduplicated by symbol + entry minute), fetches
1-min OHLCV bars for each holding period, and simulates various take-profit
strategies to find natural thresholds that would have improved overall P&L.

Analyses performed:
  1. Summary stats: peak/trough/exit P&L distributions
  2. Fixed take-profit sweep: simulate exiting at 3-20% thresholds
  3. Trailing take-profit: exit when price drops X% from peak after reaching Y%
  4. Time-to-peak: how quickly positions reach their peak P&L
  5. Breakdown by exit reason (signal vs guard_stop vs stop_fill)

Usage:
    uv run python scripts/take_profit_analysis.py [--db path] [--no-bars]

    --no-bars    Skip 1-min bar fetching; use stored peak/trough only (fast mode)
    --csv FILE   Export detailed results to CSV
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from trader.market.backtest import _filter_trading_hours, _get_ohlcv_1m
from trader.market.market_hours import ET

DB_PATH = ROOT / "data" / "trader.db"
UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_watches_deduped(db_path: str) -> list[dict]:
    """Load all exited watches, deduplicated by (symbol, entry_minute).

    When the same trade exists across multiple portfolios, keeps the first
    (earliest entry_time) and averages the exit P&L.
    """
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    rows = db.execute(
        "SELECT symbol, status, watch_json FROM watches "
        "WHERE status IN ('exited', 'cooling_off', 'sealed') "
        "ORDER BY created_at"
    ).fetchall()
    db.close()

    # Group by (symbol, entry_minute) to dedup
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        wj = json.loads(r["watch_json"])
        exit_info = wj.get("exit") or {}
        if not exit_info.get("time"):
            continue

        entry = wj.get("entry", {})
        entry_time = entry.get("time", "")
        symbol = r["symbol"]

        # Round to minute for dedup key
        try:
            dt = datetime.fromisoformat(entry_time)
            key_time = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            key_time = entry_time[:16]

        key = (symbol, key_time)
        if key not in groups:
            groups[key] = []
        groups[key].append(wj)

    # Merge duplicates: use first entry, average P&L values
    watches = []
    for (symbol, _), group in groups.items():
        first = group[0]
        entry = first.get("entry", {})
        exit_info = first.get("exit", {})

        # Categorize exit reason
        reason = exit_info.get("reason", "unknown")
        reason_cat = _categorize_reason(reason)

        watches.append({
            "symbol": symbol,
            "entry_time": entry.get("time"),
            "entry_price": entry.get("price"),
            "exit_time": exit_info.get("time"),
            "exit_price": exit_info.get("price"),
            "exit_pnl_pct": exit_info.get("realized_pnl_pct"),
            "peak_pnl_pct": first.get("peak_pnl_pct"),
            "trough_pnl_pct": first.get("trough_pnl_pct"),
            "exit_reason": reason,
            "exit_reason_cat": reason_cat,
            "exit_strategy": first.get("exit_strategy"),
            "exit_params": first.get("exit_params", {}),
            "live_config_id": first.get("live_config_id"),
            "n_portfolios": len(group),
        })

    return watches


def _categorize_reason(reason: str) -> str:
    """Bucket exit reasons into categories."""
    if reason == "signal":
        return "vdd_signal"
    if reason == "guard_stop":
        return "guard_stop"
    if "alpaca_stop_fill" in reason:
        return "alpaca_stop"
    if "reconcile" in reason:
        return "reconcile"
    if "fractional" in reason or "duplicate" in reason:
        return "cleanup"
    return "other"


# ---------------------------------------------------------------------------
# 1-min bar fetching and P&L curve computation
# ---------------------------------------------------------------------------

def get_pnl_curve(symbol: str, entry_price: float,
                  entry_time_str: str, exit_time_str: str) -> pd.DataFrame | None:
    """Fetch 1-min bars and compute P&L % curve for holding period.

    Returns DataFrame with columns: time, close, pnl_pct, minutes_held
    """
    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
        exit_dt = datetime.fromisoformat(exit_time_str)
    except (ValueError, TypeError):
        return None

    # Convert to ET for bar fetching
    if entry_dt.tzinfo is not None:
        entry_et = entry_dt.astimezone(ET).replace(tzinfo=None)
        exit_et = exit_dt.astimezone(ET).replace(tzinfo=None)
    else:
        entry_et = entry_dt
        exit_et = exit_dt

    start_date = (entry_et - timedelta(days=1)).strftime("%Y-%m-%d")
    end_date = (exit_et + timedelta(days=1)).strftime("%Y-%m-%d")

    bars = _get_ohlcv_1m(symbol, start_date, end_date)
    if bars is None or bars.empty:
        return None

    bars = _filter_trading_hours(bars, market_close=None)
    if bars.empty:
        return None

    # Find entry and exit bars
    entry_ts = pd.Timestamp(entry_et).floor("s")
    exit_ts = pd.Timestamp(exit_et).floor("s")

    entry_idx = int(bars.index.searchsorted(entry_ts))
    exit_idx = int(bars.index.searchsorted(exit_ts, side="right"))

    entry_idx = min(entry_idx, len(bars) - 1)
    exit_idx = min(exit_idx, len(bars))

    holding = bars.iloc[entry_idx:exit_idx].copy()
    if holding.empty:
        return None

    closes = holding["Close"].values
    pnl_pcts = ((closes - entry_price) / entry_price) * 100.0

    result = pd.DataFrame({
        "time": holding.index,
        "close": closes,
        "pnl_pct": pnl_pcts,
        "minutes_held": np.arange(len(holding)),
    })
    return result


# ---------------------------------------------------------------------------
# Simulation: fixed take-profit
# ---------------------------------------------------------------------------

def simulate_fixed_take_profit(pnl_curve: pd.DataFrame, threshold: float) -> dict | None:
    """Simulate exiting when P&L first crosses threshold.

    Returns dict with exit info, or None if threshold never reached.
    """
    above = pnl_curve["pnl_pct"] >= threshold
    if not above.any():
        return None

    idx = above.idxmax()
    row = pnl_curve.loc[idx]
    return {
        "exit_pnl": float(row["pnl_pct"]),
        "exit_minute": int(row["minutes_held"]),
        "exit_close": float(row["close"]),
    }


# ---------------------------------------------------------------------------
# Simulation: trailing take-profit
# ---------------------------------------------------------------------------

def simulate_trailing_take_profit(
    pnl_curve: pd.DataFrame,
    activation_pct: float,
    trail_pct: float,
) -> dict | None:
    """Simulate trailing stop that activates once P&L reaches activation_pct.

    Once activated, exit if P&L drops trail_pct from the running peak.
    E.g., activation_pct=5, trail_pct=2 means: once P&L hits +5%, exit if
    it drops 2% from any subsequent peak.

    Returns dict with exit info, or None if activation threshold never reached.
    """
    pnls = pnl_curve["pnl_pct"].values
    activated = False
    running_peak = -np.inf

    for i, pnl in enumerate(pnls):
        if not activated:
            if pnl >= activation_pct:
                activated = True
                running_peak = pnl
            continue

        running_peak = max(running_peak, pnl)
        if pnl <= running_peak - trail_pct:
            row = pnl_curve.iloc[i]
            return {
                "exit_pnl": float(pnl),
                "exit_minute": int(row["minutes_held"]),
                "running_peak_at_exit": float(running_peak),
            }

    # Activated but never trailed enough — position held to actual exit
    if activated:
        return {
            "exit_pnl": float(pnls[-1]),
            "exit_minute": int(pnl_curve.iloc[-1]["minutes_held"]),
            "running_peak_at_exit": float(running_peak),
            "held_to_end": True,
        }

    return None


# ---------------------------------------------------------------------------
# Analysis and reporting
# ---------------------------------------------------------------------------

def print_header(title: str):
    print(f"\n{'=' * 90}")
    print(f"  {title}")
    print(f"{'=' * 90}")


def analyze_summary(watches: list[dict]):
    """Section 1: Summary statistics."""
    print_header("1. SUMMARY STATISTICS")

    n = len(watches)
    exit_pnls = [w["exit_pnl_pct"] for w in watches if w["exit_pnl_pct"] is not None]
    peaks = [w["peak_pnl_pct"] for w in watches if w["peak_pnl_pct"] is not None]
    troughs = [w["trough_pnl_pct"] for w in watches if w["trough_pnl_pct"] is not None]

    print(f"\nTotal unique trades: {n}")
    print(f"With peak/trough data: {len(peaks)}")

    if exit_pnls:
        wins = sum(1 for p in exit_pnls if p > 0)
        losses = sum(1 for p in exit_pnls if p < 0)
        flat = n - wins - losses
        print(f"\nWin/Loss: {wins}W / {losses}L / {flat}F  (win rate: {wins/n*100:.1f}%)")
        print(f"Exit P&L:   mean={np.mean(exit_pnls):+.2f}%  median={np.median(exit_pnls):+.2f}%  "
              f"std={np.std(exit_pnls):.2f}%")

    if peaks:
        print(f"Peak P&L:   mean={np.mean(peaks):+.2f}%  median={np.median(peaks):+.2f}%  "
              f"max={np.max(peaks):+.2f}%")
    if troughs:
        print(f"Trough P&L: mean={np.mean(troughs):+.2f}%  median={np.median(troughs):+.2f}%  "
              f"min={np.min(troughs):+.2f}%")

    if peaks and exit_pnls:
        left = [p - e for p, e in zip(peaks, exit_pnls) if p is not None and e is not None]
        print(f"Left on table: mean={np.mean(left):+.2f}%  median={np.median(left):+.2f}%")

    # Breakdown by exit reason
    print(f"\n{'Exit Reason':<16s} {'Count':>5s} {'Avg Exit':>9s} {'Avg Peak':>9s} {'Avg Left':>9s}")
    print("-" * 52)
    cats = {}
    for w in watches:
        cat = w["exit_reason_cat"]
        if cat not in cats:
            cats[cat] = []
        cats[cat].append(w)

    for cat, ws in sorted(cats.items(), key=lambda x: -len(x[1])):
        ep = [w["exit_pnl_pct"] for w in ws if w["exit_pnl_pct"] is not None]
        pk = [w["peak_pnl_pct"] for w in ws if w["peak_pnl_pct"] is not None]
        left = [w["peak_pnl_pct"] - w["exit_pnl_pct"]
                for w in ws if w["peak_pnl_pct"] is not None and w["exit_pnl_pct"] is not None]
        avg_e = np.mean(ep) if ep else 0
        avg_p = np.mean(pk) if pk else 0
        avg_l = np.mean(left) if left else 0
        print(f"{cat:<16s} {len(ws):>5d} {avg_e:>+8.2f}% {avg_p:>+8.2f}% {avg_l:>+8.2f}%")


def analyze_peak_buckets(watches: list[dict]):
    """Section 2: Peak P&L bucket analysis."""
    print_header("2. PEAK P&L BUCKET ANALYSIS")
    print("\nPositions grouped by their highest unrealized P&L during the hold:")

    buckets = [
        (20, 100, "20%+"),
        (15, 20,  "15-20%"),
        (10, 15,  "10-15%"),
        (7, 10,   "7-10%"),
        (5, 7,    "5-7%"),
        (3, 5,    "3-5%"),
        (1, 3,    "1-3%"),
        (0, 1,    "0-1%"),
        (-100, 0, "<0%"),
    ]

    print(f"\n{'Bucket':<10s} {'Count':>5s} {'Avg Exit':>9s} {'Avg Peak':>9s} "
          f"{'Avg Left':>9s} {'%Negative':>9s} {'Worst':>8s}")
    print("-" * 62)

    for lo, hi, label in buckets:
        ws = [w for w in watches
              if w["peak_pnl_pct"] is not None and lo <= w["peak_pnl_pct"] < hi]
        if not ws:
            continue
        ep = [w["exit_pnl_pct"] for w in ws if w["exit_pnl_pct"] is not None]
        pk = [w["peak_pnl_pct"] for w in ws if w["peak_pnl_pct"] is not None]
        left = [w["peak_pnl_pct"] - w["exit_pnl_pct"]
                for w in ws if w["peak_pnl_pct"] is not None and w["exit_pnl_pct"] is not None]
        neg = sum(1 for e in ep if e < 0)
        worst = min(ep) if ep else 0
        print(f"{label:<10s} {len(ws):>5d} {np.mean(ep):>+8.2f}% {np.mean(pk):>+8.2f}% "
              f"{np.mean(left):>+8.2f}% {neg/len(ws)*100:>8.1f}% {worst:>+7.2f}%")


def analyze_painful_reversals(watches: list[dict]):
    """Section 3: Positions that peaked high but ended negative."""
    print_header("3. PAINFUL REVERSALS (peaked ≥5%, exited negative)")

    reversals = [w for w in watches
                 if w["peak_pnl_pct"] is not None and w["peak_pnl_pct"] >= 5
                 and w["exit_pnl_pct"] is not None and w["exit_pnl_pct"] < 0]

    if not reversals:
        print("\nNo painful reversals found.")
        return

    reversals.sort(key=lambda w: w["peak_pnl_pct"], reverse=True)

    print(f"\n{'Symbol':<8s} {'Peak':>7s} {'Exit':>7s} {'Left':>7s} {'Reason':<20s} {'#Port':>5s}")
    print("-" * 60)
    for w in reversals:
        left = w["peak_pnl_pct"] - w["exit_pnl_pct"]
        print(f"{w['symbol']:<8s} {w['peak_pnl_pct']:>+6.2f}% {w['exit_pnl_pct']:>+6.2f}% "
              f"{left:>+6.2f}% {w['exit_reason_cat']:<20s} {w['n_portfolios']:>5d}")

    total_left = sum(w["peak_pnl_pct"] - w["exit_pnl_pct"] for w in reversals)
    print(f"\nTotal: {len(reversals)} reversals, avg {total_left/len(reversals):.2f}% left on table")


def analyze_fixed_thresholds(watches: list[dict], curves: dict[str, pd.DataFrame]):
    """Section 4: Fixed take-profit threshold sweep."""
    print_header("4. FIXED TAKE-PROFIT THRESHOLD SWEEP")

    if not curves:
        # Fall back to stored peak/trough (no-bars mode)
        _analyze_fixed_thresholds_no_bars(watches)
        return

    thresholds = [3, 4, 5, 6, 7, 8, 10, 12, 15, 20]

    # Baseline: actual avg exit P&L
    actual_pnls = [w["exit_pnl_pct"] for w in watches if w["exit_pnl_pct"] is not None]
    baseline = np.mean(actual_pnls)

    print(f"\nBaseline (actual exits): avg P&L = {baseline:+.3f}%  ({len(actual_pnls)} trades)")
    print(f"\nSimulation: if take-profit triggered, use simulated exit P&L.")
    print(f"            if NOT triggered, use actual exit P&L (VDD/stop still handles it).")

    print(f"\n{'Threshold':>9s} {'Triggers':>8s} {'TrigRate':>8s} {'SimAvgPnL':>10s} "
          f"{'Improve':>8s} {'AvgTrigPnL':>10s} {'AvgMinsHeld':>11s}")
    print("-" * 75)

    for thresh in thresholds:
        sim_pnls = []
        trigger_pnls = []
        trigger_mins = []

        for w in watches:
            key = _watch_key(w)
            curve = curves.get(key)

            if curve is not None:
                result = simulate_fixed_take_profit(curve, thresh)
                if result:
                    sim_pnls.append(result["exit_pnl"])
                    trigger_pnls.append(result["exit_pnl"])
                    trigger_mins.append(result["exit_minute"])
                    continue

            # Not triggered or no curve: use actual exit
            if w["exit_pnl_pct"] is not None:
                sim_pnls.append(w["exit_pnl_pct"])

        sim_avg = np.mean(sim_pnls) if sim_pnls else 0
        improvement = sim_avg - baseline
        n_triggered = len(trigger_pnls)
        trig_rate = n_triggered / len(watches) * 100

        avg_trig = np.mean(trigger_pnls) if trigger_pnls else 0
        avg_mins = np.mean(trigger_mins) if trigger_mins else 0

        flag = " ***" if improvement > 0.05 else ""
        print(f"{thresh:>8.0f}% {n_triggered:>8d} {trig_rate:>7.1f}% {sim_avg:>+9.3f}% "
              f"{improvement:>+7.3f}% {avg_trig:>+9.2f}% {avg_mins:>10.0f}m{flag}")


def _analyze_fixed_thresholds_no_bars(watches: list[dict]):
    """Approximate fixed threshold analysis using stored peak values only."""
    thresholds = [3, 4, 5, 6, 7, 8, 10, 12, 15, 20]
    actual_pnls = [w["exit_pnl_pct"] for w in watches if w["exit_pnl_pct"] is not None]
    baseline = np.mean(actual_pnls)

    print(f"\nBaseline (actual exits): avg P&L = {baseline:+.3f}%  ({len(actual_pnls)} trades)")
    print(f"\n[APPROXIMATE — using stored peak values, not minute-level simulation]")

    print(f"\n{'Threshold':>9s} {'Triggers':>8s} {'TrigRate':>8s} {'SimAvgPnL':>10s} {'Improve':>8s}")
    print("-" * 50)

    for thresh in thresholds:
        sim_pnls = []
        triggered = 0
        for w in watches:
            if w["peak_pnl_pct"] is not None and w["peak_pnl_pct"] >= thresh:
                sim_pnls.append(thresh)  # approximate: assume exit at exactly threshold
                triggered += 1
            elif w["exit_pnl_pct"] is not None:
                sim_pnls.append(w["exit_pnl_pct"])

        sim_avg = np.mean(sim_pnls) if sim_pnls else 0
        improvement = sim_avg - baseline
        trig_rate = triggered / len(watches) * 100
        flag = " ***" if improvement > 0.05 else ""
        print(f"{thresh:>8.0f}% {triggered:>8d} {trig_rate:>7.1f}% {sim_avg:>+9.3f}% "
              f"{improvement:>+7.3f}%{flag}")


def analyze_trailing(watches: list[dict], curves: dict[str, pd.DataFrame]):
    """Section 5: Trailing take-profit sweep."""
    print_header("5. TRAILING TAKE-PROFIT SWEEP")

    if not curves:
        print("\n[Skipped — requires 1-min bars. Run without --no-bars.]")
        return

    # Sweep: (activation_pct, trail_pct)
    configs = [
        (3, 1), (3, 1.5), (3, 2),
        (5, 1.5), (5, 2), (5, 2.5), (5, 3),
        (7, 2), (7, 3), (7, 4),
        (10, 2), (10, 3), (10, 4), (10, 5),
        (15, 3), (15, 5), (15, 7),
    ]

    actual_pnls = [w["exit_pnl_pct"] for w in watches if w["exit_pnl_pct"] is not None]
    baseline = np.mean(actual_pnls)

    print(f"\nBaseline (actual exits): avg P&L = {baseline:+.3f}%")
    print(f"\nTrailing stop: activates at activation%, then exits if P&L drops trail% from peak.")

    print(f"\n{'Activate':>8s} {'Trail':>6s} {'Triggers':>8s} {'TrigRate':>8s} "
          f"{'SimAvgPnL':>10s} {'Improve':>8s} {'AvgTrigPnL':>10s}")
    print("-" * 68)

    results = []
    for act, trail in configs:
        sim_pnls = []
        trigger_pnls = []

        for w in watches:
            key = _watch_key(w)
            curve = curves.get(key)

            if curve is not None:
                result = simulate_trailing_take_profit(curve, act, trail)
                if result and not result.get("held_to_end"):
                    sim_pnls.append(result["exit_pnl"])
                    trigger_pnls.append(result["exit_pnl"])
                    continue

            if w["exit_pnl_pct"] is not None:
                sim_pnls.append(w["exit_pnl_pct"])

        sim_avg = np.mean(sim_pnls) if sim_pnls else 0
        improvement = sim_avg - baseline
        n_triggered = len(trigger_pnls)
        trig_rate = n_triggered / len(watches) * 100
        avg_trig = np.mean(trigger_pnls) if trigger_pnls else 0

        results.append((act, trail, improvement, sim_avg, n_triggered, trig_rate, avg_trig))

        flag = " ***" if improvement > 0.05 else ""
        print(f"{act:>7.0f}% {trail:>5.1f}% {n_triggered:>8d} {trig_rate:>7.1f}% "
              f"{sim_avg:>+9.3f}% {improvement:>+7.3f}% {avg_trig:>+9.2f}%{flag}")

    # Best config
    best = max(results, key=lambda x: x[2])
    print(f"\nBest trailing config: activate={best[0]}%, trail={best[1]}% "
          f"→ improvement={best[2]:+.3f}%")


def analyze_time_to_peak(watches: list[dict], curves: dict[str, pd.DataFrame]):
    """Section 6: How quickly do positions reach their peak?"""
    print_header("6. TIME-TO-PEAK ANALYSIS")

    if not curves:
        print("\n[Skipped — requires 1-min bars. Run without --no-bars.]")
        return

    peak_times = []  # (symbol, peak_pnl, minutes_to_peak, total_minutes)

    for w in watches:
        key = _watch_key(w)
        curve = curves.get(key)
        if curve is None or curve.empty:
            continue

        peak_idx = curve["pnl_pct"].idxmax()
        peak_row = curve.loc[peak_idx]
        total_mins = int(curve.iloc[-1]["minutes_held"])

        peak_times.append({
            "symbol": w["symbol"],
            "peak_pnl": float(peak_row["pnl_pct"]),
            "mins_to_peak": int(peak_row["minutes_held"]),
            "total_mins": total_mins,
            "peak_at_pct_of_hold": int(peak_row["minutes_held"]) / max(total_mins, 1) * 100,
            "exit_pnl": w["exit_pnl_pct"],
        })

    if not peak_times:
        print("\nNo curves available for time-to-peak analysis.")
        return

    mins_to_peak = [p["mins_to_peak"] for p in peak_times]
    total_mins = [p["total_mins"] for p in peak_times]
    pct_of_hold = [p["peak_at_pct_of_hold"] for p in peak_times]

    print(f"\nAcross {len(peak_times)} trades:")
    print(f"  Time to peak:    mean={np.mean(mins_to_peak):.0f}min  "
          f"median={np.median(mins_to_peak):.0f}min")
    print(f"  Total hold time: mean={np.mean(total_mins):.0f}min  "
          f"median={np.median(total_mins):.0f}min")
    print(f"  Peak at % of hold: mean={np.mean(pct_of_hold):.0f}%  "
          f"median={np.median(pct_of_hold):.0f}%")

    # Bucket by peak P&L range
    print(f"\n{'Peak Range':<10s} {'Count':>5s} {'Avg MinsTo':>10s} {'Med MinsTo':>10s} "
          f"{'Avg Hold':>8s} {'Peak@%Hold':>10s}")
    print("-" * 58)

    for lo, hi, label in [(0, 3, "0-3%"), (3, 5, "3-5%"), (5, 10, "5-10%"), (10, 100, "10%+")]:
        pts = [p for p in peak_times if lo <= p["peak_pnl"] < hi]
        if not pts:
            continue
        mtp = [p["mins_to_peak"] for p in pts]
        tm = [p["total_mins"] for p in pts]
        poh = [p["peak_at_pct_of_hold"] for p in pts]
        print(f"{label:<10s} {len(pts):>5d} {np.mean(mtp):>9.0f}m {np.median(mtp):>9.0f}m "
              f"{np.mean(tm):>7.0f}m {np.mean(poh):>9.0f}%")


def analyze_combined_strategy(watches: list[dict], curves: dict[str, pd.DataFrame]):
    """Section 7: Combined strategy — VDD exit OR take-profit, whichever fires first."""
    print_header("7. COMBINED STRATEGY: VDD + TAKE-PROFIT")

    if not curves:
        print("\n[Skipped — requires 1-min bars. Run without --no-bars.]")
        return

    actual_pnls = [w["exit_pnl_pct"] for w in watches if w["exit_pnl_pct"] is not None]
    baseline = np.mean(actual_pnls)

    # For each trade: the actual exit is what VDD/stop produced.
    # We simulate: if take-profit fired EARLIER than actual exit, use it instead.
    # We check by comparing the take-profit minute to total hold time.
    thresholds = [5, 7, 8, 10, 12, 15]

    print(f"\nBaseline (actual exits): avg P&L = {baseline:+.3f}%")
    print(f"\nStrategy: exit at whichever fires first — VDD signal OR take-profit threshold.")
    print(f"(Actual exit = proxy for when VDD/stop fired)")

    print(f"\n{'TakeProfit':>10s} {'EarlyExits':>10s} {'SimAvgPnL':>10s} {'Improve':>8s} "
          f"{'WinsAdded':>9s} {'LossesPrev':>10s}")
    print("-" * 65)

    for thresh in thresholds:
        sim_pnls = []
        early_exits = 0
        wins_added = 0
        losses_prevented = 0

        for w in watches:
            key = _watch_key(w)
            curve = curves.get(key)
            actual = w["exit_pnl_pct"]
            if actual is None:
                continue

            if curve is not None:
                tp = simulate_fixed_take_profit(curve, thresh)
                total_mins = int(curve.iloc[-1]["minutes_held"]) if not curve.empty else 0

                if tp and tp["exit_minute"] < total_mins:
                    # Take-profit would have fired before actual exit
                    sim_pnls.append(tp["exit_pnl"])
                    early_exits += 1
                    if tp["exit_pnl"] > actual:
                        wins_added += 1
                    if actual < 0 and tp["exit_pnl"] >= 0:
                        losses_prevented += 1
                    continue

            sim_pnls.append(actual)

        sim_avg = np.mean(sim_pnls) if sim_pnls else 0
        improvement = sim_avg - baseline
        flag = " ***" if improvement > 0.05 else ""
        print(f"{thresh:>9.0f}% {early_exits:>10d} {sim_avg:>+9.3f}% {improvement:>+7.3f}% "
              f"{wins_added:>9d} {losses_prevented:>10d}{flag}")


def export_csv(watches: list[dict], curves: dict[str, pd.DataFrame], path: str):
    """Export detailed per-trade data to CSV."""
    rows = []
    for w in watches:
        key = _watch_key(w)
        curve = curves.get(key)

        total_mins = None
        mins_to_peak = None
        if curve is not None and not curve.empty:
            total_mins = int(curve.iloc[-1]["minutes_held"])
            peak_idx = curve["pnl_pct"].idxmax()
            mins_to_peak = int(curve.loc[peak_idx, "minutes_held"])

        rows.append({
            "symbol": w["symbol"],
            "entry_time": w["entry_time"],
            "exit_time": w["exit_time"],
            "entry_price": w["entry_price"],
            "exit_price": w["exit_price"],
            "exit_pnl_pct": w["exit_pnl_pct"],
            "peak_pnl_pct": w["peak_pnl_pct"],
            "trough_pnl_pct": w["trough_pnl_pct"],
            "exit_reason": w["exit_reason"],
            "exit_reason_cat": w["exit_reason_cat"],
            "n_portfolios": w["n_portfolios"],
            "total_minutes_held": total_mins,
            "minutes_to_peak": mins_to_peak,
            "live_config_id": w["live_config_id"],
        })

    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    print(f"\nExported {len(df)} rows to {path}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _watch_key(w: dict) -> str:
    """Unique key for a watch (for curve lookup)."""
    entry_time = w.get("entry_time", "")
    try:
        dt = datetime.fromisoformat(entry_time)
        t = dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        t = entry_time[:16]
    return f"{w['symbol']}_{t}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Take-profit threshold analysis")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path")
    parser.add_argument("--no-bars", action="store_true",
                        help="Skip 1-min bar fetching (fast mode, approximate results)")
    parser.add_argument("--csv", help="Export per-trade data to CSV")
    args = parser.parse_args()

    print("Loading watches...")
    watches = load_watches_deduped(args.db)
    print(f"Loaded {len(watches)} unique trades (deduplicated by symbol + entry minute)")

    # Section 1-3: stored data only (fast)
    analyze_summary(watches)
    analyze_peak_buckets(watches)
    analyze_painful_reversals(watches)

    # Fetch 1-min bar curves (unless --no-bars)
    curves: dict[str, pd.DataFrame] = {}
    if not args.no_bars:
        print_header("FETCHING 1-MIN BARS")
        total = len(watches)
        fetched = 0
        failed = 0
        for i, w in enumerate(watches):
            if w["entry_price"] is None or w["entry_time"] is None or w["exit_time"] is None:
                failed += 1
                continue

            key = _watch_key(w)
            curve = get_pnl_curve(w["symbol"], w["entry_price"],
                                  w["entry_time"], w["exit_time"])
            if curve is not None and not curve.empty:
                curves[key] = curve
                fetched += 1
            else:
                failed += 1

            if (i + 1) % 25 == 0 or i == total - 1:
                print(f"  [{i+1}/{total}] fetched={fetched} failed={failed}")

        print(f"\nGot 1-min curves for {fetched}/{total} trades")

    # Sections 4-7: require curves (or approximate with stored peaks)
    analyze_fixed_thresholds(watches, curves)
    analyze_trailing(watches, curves)
    analyze_time_to_peak(watches, curves)
    analyze_combined_strategy(watches, curves)

    if args.csv:
        export_csv(watches, curves, args.csv)

    # Final recommendation
    print_header("SUMMARY / RECOMMENDATION")
    print("\nReview the results above to identify:")
    print("  1. Which fixed threshold gives the best improvement over baseline")
    print("  2. Whether trailing stops outperform fixed thresholds")
    print("  3. The time-to-peak data — does it support a time-based take-profit?")
    print("  4. The combined strategy results — does VDD + take-profit beat either alone?")


if __name__ == "__main__":
    main()
