"""Run a small matrix of backtests and write normalized results to JSON.

Use this before and after refactors. Exact equality is expected only when the
semantic model is unchanged. For cache changes, the normalized outputs should
match exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from trader.config import load_settings
from trader.db.database import get_all_snapshots, open_sqlite
from trader.market.backtest import (
    apply_allocation,
    compute_ann_a,
    compute_ann_b,
    compute_portfolio_sim,
    run_backtest,
    weight_per_trade_for_allocation,
)


@dataclass(frozen=True)
class Case:
    name: str
    snapshot_filters: dict[str, Any]
    limit: int
    strategy: str
    params: dict[str, float]
    market_close: str | None
    min_hold: int
    guard_stop_pct: float
    guard_target_pct: float
    guard_trail_pct: float
    allocation: str
    allocation_params: dict[str, Any]
    starting_amount: float
    reinvest_delay_minutes: int


CASES: list[Case] = [
    Case(
        name="fixed_stop_recent_bull_no_alloc",
        snapshot_filters={"explored_only": True, "created_after": "2026-03-10", "conf_min": 0.70},
        limit=120,
        strategy="fixed_stop_loss",
        params={"stop_pct": 6.0},
        market_close="16:00",
        min_hold=5,
        guard_stop_pct=0.0,
        guard_target_pct=0.0,
        guard_trail_pct=0.0,
        allocation="none",
        allocation_params={},
        starting_amount=0.0,
        reinvest_delay_minutes=1,
    ),
    Case(
        name="vdd_recent_bear_max_positions",
        snapshot_filters={
            "explored_only": True,
            "created_after": "2026-03-08",
            "created_before": "2026-03-11",
            "conf_min": -0.70,
        },
        limit=140,
        strategy="volume_delta_divergence",
        params={"lookback": 80.0},
        market_close="16:00",
        min_hold=5,
        guard_stop_pct=0.0,
        guard_target_pct=0.0,
        guard_trail_pct=0.0,
        allocation="max_positions",
        allocation_params={"max_pos": 10, "when_full": "skip"},
        starting_amount=10000.0,
        reinvest_delay_minutes=1,
    ),
    Case(
        name="ma_cross_mid_march_extended",
        snapshot_filters={
            "explored_only": True,
            "created_after": "2026-03-05",
            "created_before": "2026-03-10",
        },
        limit=100,
        strategy="ma_cross_exit",
        params={"short_period": 10.0, "long_period": 50.0},
        market_close=None,
        min_hold=0,
        guard_stop_pct=0.0,
        guard_target_pct=0.0,
        guard_trail_pct=0.0,
        allocation="ranking_realloc",
        allocation_params={"alloc_pct": 10, "rank_method": "unreal_pl", "replace_min_margin": 0.0},
        starting_amount=10000.0,
        reinvest_delay_minutes=1,
    ),
    Case(
        name="max_hold_guarded_fixed_dollar",
        snapshot_filters={
            "explored_only": True,
            "created_after": "2026-03-06",
            "created_before": "2026-03-11",
        },
        limit=120,
        strategy="max_holding_period",
        params={"max_bars": 120.0},
        market_close="16:00",
        min_hold=1,
        guard_stop_pct=4.0,
        guard_target_pct=8.0,
        guard_trail_pct=3.0,
        allocation="fixed_dollar",
        allocation_params={"alloc_pct": 20},
        starting_amount=10000.0,
        reinvest_delay_minutes=0,
    ),
]


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _primary_symbol(row: dict[str, Any]) -> str:
    trigger = row.get("trigger") or {}
    symbols = trigger.get("symbols") or []
    if not symbols:
        return ""
    return str(symbols[0]).strip().upper()


def _entry_time(row: dict[str, Any]) -> str:
    for key in ("decision_at", "created_at"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_trade(trade: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "snapshot_id",
        "symbol",
        "entry_price",
        "entry_time",
        "exit_price",
        "exit_time",
        "pnl_pct",
        "exit_reason",
        "bars_held",
        "hold_minutes",
    )
    return {k: trade.get(k) for k in keep}


def _canonical_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.get("snapshot_id") or "").strip()
        entry_time = _entry_time(row)
        symbol = _primary_symbol(row)
        if not sid or not entry_time or not symbol:
            continue
        pred = row.get("prediction") or {}
        confidence = _safe_float(pred.get("confidence")) or 0.5
        entries.append(
            {
                "snapshot_id": sid,
                "symbol": symbol,
                "entry_price": 0.0,
                "entry_time": entry_time,
                "confidence": confidence,
            }
        )
    return entries


def _run_case(case: Case) -> dict[str, Any]:
    print(f"Running case: {case.name}", flush=True)
    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)
    rows = get_all_snapshots(
        db,
        limit=case.limit,
        offset=0,
        **case.snapshot_filters,
    )
    entries = _build_entries(rows)
    if not entries:
        raise RuntimeError(f"Case {case.name}: no entries matched")

    started = time.perf_counter()
    results = run_backtest(
        case.strategy,
        case.params,
        entries,
        market_close=case.market_close,
        min_hold=case.min_hold,
        guard_stop_pct=case.guard_stop_pct,
        guard_target_pct=case.guard_target_pct,
        guard_trail_pct=case.guard_trail_pct,
        price_delay_minutes=0,
        stats_resolution_minutes=settings.stats_resolution_minutes,
    )
    results, alloc_stats = apply_allocation(
        results,
        entries,
        case.allocation,
        case.allocation_params,
    )

    wpt = weight_per_trade_for_allocation(case.allocation, case.allocation_params)
    stats_a = compute_ann_a(results)
    stats_b = compute_ann_b(results, settings.stats_resolution_minutes, weight_per_trade=wpt)
    sim_result = None
    if case.starting_amount > 0:
        sim_result = compute_portfolio_sim(
            results,
            case.allocation,
            case.allocation_params,
            case.starting_amount,
            case.reinvest_delay_minutes,
        )
    elapsed = time.perf_counter() - started

    trades = [_normalize_trade(r.to_dict()) for r in results]
    summary = {
        "count": sum(1 for r in results if r.pnl_pct is not None),
        "avg_pnl": round(
            sum(r.pnl_pct for r in results if r.pnl_pct is not None)
            / max(1, sum(1 for r in results if r.pnl_pct is not None)),
            2,
        ) if any(r.pnl_pct is not None for r in results) else None,
        "daily_pnl": round(stats_a["daily_pnl"], 6) if stats_a else None,
        "ann_a": round(stats_a["ann"], 6) if stats_a else None,
        "sharpe_a": round(stats_a["sharpe"], 6) if stats_a and stats_a["sharpe"] is not None else None,
        "ann_b": round(stats_b["ann"], 6) if stats_b else None,
        "sharpe_b": round(stats_b["sharpe"], 6) if stats_b and stats_b["sharpe"] is not None else None,
        "alloc_taken": alloc_stats.get("taken", 0),
        "alloc_skipped": alloc_stats.get("skipped", 0),
        "alloc_replaced": alloc_stats.get("replaced", 0),
        "sim_starting": sim_result["sim_starting"] if sim_result else None,
        "sim_ending": sim_result["sim_ending"] if sim_result else None,
        "sim_return_pct": sim_result["sim_return_pct"] if sim_result else None,
        "sim_trades": sim_result["sim_trades"] if sim_result else None,
        "sim_daily_pct": sim_result["sim_daily_pct"] if sim_result else None,
        "sim_span_days": sim_result["sim_span_days"] if sim_result else None,
    }
    normalized = {
        "case": case.name,
        "entry_count": len(entries),
        "strategy": case.strategy,
        "params": case.params,
        "market_close": case.market_close,
        "min_hold": case.min_hold,
        "guard_stop_pct": case.guard_stop_pct,
        "guard_target_pct": case.guard_target_pct,
        "guard_trail_pct": case.guard_trail_pct,
        "allocation": case.allocation,
        "allocation_params": case.allocation_params,
        "starting_amount": case.starting_amount,
        "reinvest_delay_minutes": case.reinvest_delay_minutes,
        "entry_model": "decision_time_first_eligible_bar",
        "stats_resolution_minutes": settings.stats_resolution_minutes,
        "summary": summary,
        "trades": trades,
    }
    return {
        "case": case.name,
        "elapsed_sec": round(elapsed, 3),
        "entry_count": len(entries),
        "summary": summary,
        "hash": _canonical_hash(normalized),
        "normalized": normalized,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a small backtest regression matrix")
    parser.add_argument(
        "--output",
        default="data/qa/backtest_regression_decision_time.json",
        help="Path to write JSON results",
    )
    parser.add_argument(
        "--clear-result-cache",
        action="store_true",
        help="Delete the per-snapshot backtest result cache before running",
    )
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.clear_result_cache:
        cache_dir = Path.home() / ".cache" / "alpaca-news" / "backtest_results"
        shutil.rmtree(cache_dir, ignore_errors=True)

    cases: list[dict[str, Any]] = []
    for case in CASES:
        result = _run_case(case)
        cases.append(result)
        print(
            f"Finished case: {result['case']} "
            f"entries={result['entry_count']} elapsed={result['elapsed_sec']}s "
            f"hash={result['hash'][:12]}",
            flush=True,
        )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases": cases,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {out_path}")
    for case in payload["cases"]:
        print(
            f"{case['case']}: entries={case['entry_count']} "
            f"elapsed={case['elapsed_sec']}s hash={case['hash'][:12]}"
        )


if __name__ == "__main__":
    main()
