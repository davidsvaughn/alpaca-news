"""Profile the backtest engine with built-in trace metrics and optional cProfile.

Examples:

  .venv/bin/python3 scripts/profile_backtest.py --list-cases

  .venv/bin/python3 scripts/profile_backtest.py \
    --case vdd_recent_bear_max_positions \
    --repeat 2 \
    --serial \
    --cprofile-top 20 \
    --output /tmp/backtest_profile.json

  .venv/bin/python3 scripts/profile_backtest.py \
    --strategy volume_delta_divergence \
    --params-json '{"lookback": 80}' \
    --created-after 2026-03-08 \
    --created-before 2026-03-11 \
    --conf-min -0.70 \
    --explored-only \
    --limit 140
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from trader.config import load_settings
from trader.db.database import get_all_snapshots, open_sqlite
from trader.market.backtest import run_backtest


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


def _parse_params_json(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("--params-json must decode to an object")
    return {str(k): float(v) for k, v in data.items()}


def _case_by_name(name: str) -> Case:
    for case in CASES:
        if case.name == name:
            return case
    raise KeyError(name)


def _collect_rows(
    *,
    limit: int,
    snapshot_filters: dict[str, Any],
) -> list[dict[str, Any]]:
    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)
    return get_all_snapshots(db, limit=limit, offset=0, **snapshot_filters)


def _cprofile_lines(profile: cProfile.Profile, top_n: int) -> list[str]:
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream).sort_stats("cumulative")
    stats.print_stats(top_n)
    return [line.rstrip() for line in stream.getvalue().splitlines() if line.strip()]


def _run_once(
    *,
    strategy: str,
    params: dict[str, float],
    entries: list[dict[str, Any]],
    market_close: str | None,
    min_hold: int,
    guard_stop_pct: float,
    guard_target_pct: float,
    guard_trail_pct: float,
    stats_resolution_minutes: int,
    cprofile_top: int,
) -> dict[str, Any]:
    trace: dict[str, Any] = {}
    profile = cProfile.Profile() if cprofile_top > 0 else None

    t0 = time.perf_counter()
    if profile is not None:
        profile.enable()
    results = run_backtest(
        strategy,
        params,
        entries,
        market_close=market_close,
        min_hold=min_hold,
        guard_stop_pct=guard_stop_pct,
        guard_target_pct=guard_target_pct,
        guard_trail_pct=guard_trail_pct,
        price_delay_minutes=0,
        stats_resolution_minutes=stats_resolution_minutes,
        trace=trace,
    )
    if profile is not None:
        profile.disable()
    elapsed = time.perf_counter() - t0

    valid = [r for r in results if r.pnl_pct is not None]
    no_data = sum(1 for r in results if r.pnl_pct is None)

    payload: dict[str, Any] = {
        "elapsed_sec": round(elapsed, 6),
        "result_count": len(results),
        "valid_trade_count": len(valid),
        "no_data_trade_count": no_data,
        "avg_pnl_pct": round(sum(r.pnl_pct for r in valid) / len(valid), 4) if valid else None,
        "trace": trace,
    }
    if profile is not None:
        payload["cprofile_top"] = _cprofile_lines(profile, cprofile_top)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-cases", action="store_true", help="List built-in profiling cases and exit")
    parser.add_argument("--case", help="Built-in case name")
    parser.add_argument("--strategy", help="Backtest strategy key for custom runs")
    parser.add_argument("--params-json", help="Strategy params as JSON object, e.g. '{\"lookback\": 80}'")
    parser.add_argument("--symbol", help="Optional symbol filter for custom runs")
    parser.add_argument("--created-after", help="Optional snapshot lower date bound")
    parser.add_argument("--created-before", help="Optional snapshot upper date bound")
    parser.add_argument("--conf-min", type=float, help="Optional minimum confidence filter")
    parser.add_argument("--explored-only", action="store_true", help="Restrict to explored snapshots")
    parser.add_argument("--limit", type=int, default=None, help="Snapshot limit override")
    parser.add_argument("--market-close", default=None, help="Market close cutoff such as 16:00; omit for extended hours")
    parser.add_argument("--min-hold", type=int, default=None, help="Minimum hold bars override")
    parser.add_argument("--guard-stop-pct", type=float, default=None, help="Guard stop percent override")
    parser.add_argument("--guard-target-pct", type=float, default=None, help="Guard target percent override")
    parser.add_argument("--guard-trail-pct", type=float, default=None, help="Guard trailing stop percent override")
    parser.add_argument("--repeat", type=int, default=1, help="Number of repeated runs")
    parser.add_argument("--serial", action="store_true", help="Force BACKTEST_SYMBOL_WORKERS=1 for attribution")
    parser.add_argument("--clear-result-cache", action="store_true", help="Delete ~/.cache/alpaca-news/backtest_results before the first run")
    parser.add_argument("--cprofile-top", type=int, default=0, help="Include top N cumulative cProfile rows")
    parser.add_argument("--output", help="Optional JSON output path")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_cases:
        for case in CASES:
            print(case.name)
        return 0

    if args.serial:
        os.environ["BACKTEST_SYMBOL_WORKERS"] = "1"

    settings = load_settings()

    if args.case:
        case = _case_by_name(args.case)
        strategy = case.strategy
        params = dict(case.params)
        snapshot_filters = dict(case.snapshot_filters)
        limit = args.limit or case.limit
        market_close = case.market_close if args.market_close is None else args.market_close
        min_hold = case.min_hold if args.min_hold is None else args.min_hold
        guard_stop_pct = case.guard_stop_pct if args.guard_stop_pct is None else args.guard_stop_pct
        guard_target_pct = case.guard_target_pct if args.guard_target_pct is None else args.guard_target_pct
        guard_trail_pct = case.guard_trail_pct if args.guard_trail_pct is None else args.guard_trail_pct
        selection: dict[str, Any] = {"case": case.name}
    else:
        if not args.strategy:
            parser.error("Provide --case or --strategy")
        strategy = args.strategy
        params = _parse_params_json(args.params_json)
        snapshot_filters = {}
        if args.symbol:
            snapshot_filters["symbol"] = args.symbol.strip().upper()
        if args.created_after:
            snapshot_filters["created_after"] = args.created_after
        if args.created_before:
            snapshot_filters["created_before"] = args.created_before
        if args.conf_min is not None:
            snapshot_filters["conf_min"] = args.conf_min
        if args.explored_only:
            snapshot_filters["explored_only"] = True
        limit = args.limit or 100
        market_close = args.market_close
        min_hold = 5 if args.min_hold is None else args.min_hold
        guard_stop_pct = 0.0 if args.guard_stop_pct is None else args.guard_stop_pct
        guard_target_pct = 0.0 if args.guard_target_pct is None else args.guard_target_pct
        guard_trail_pct = 0.0 if args.guard_trail_pct is None else args.guard_trail_pct
        selection = {"filters": snapshot_filters}

    rows = _collect_rows(limit=limit, snapshot_filters=snapshot_filters)
    entries = _build_entries(rows)
    if not entries:
        raise SystemExit("No entries matched the requested selection")

    cache_root = Path.home() / ".cache" / "alpaca-news"
    result_cache_dir = cache_root / "backtest_results"
    if args.clear_result_cache:
        shutil.rmtree(result_cache_dir, ignore_errors=True)

    runs: list[dict[str, Any]] = []
    for idx in range(args.repeat):
        run_payload = _run_once(
            strategy=strategy,
            params=params,
            entries=entries,
            market_close=market_close,
            min_hold=min_hold,
            guard_stop_pct=guard_stop_pct,
            guard_target_pct=guard_target_pct,
            guard_trail_pct=guard_trail_pct,
            stats_resolution_minutes=settings.stats_resolution_minutes,
            cprofile_top=args.cprofile_top,
        )
        run_payload["run_index"] = idx + 1
        runs.append(run_payload)

    payload = {
        "selection": selection,
        "strategy": strategy,
        "params": params,
        "market_close": market_close,
        "min_hold": min_hold,
        "guard_stop_pct": guard_stop_pct,
        "guard_target_pct": guard_target_pct,
        "guard_trail_pct": guard_trail_pct,
        "serial": bool(args.serial),
        "stats_resolution_minutes": settings.stats_resolution_minutes,
        "entry_count": len(entries),
        "snapshot_row_count": len(rows),
        "cache_dirs": {
            "ohlcv_1m": str(cache_root / "ohlcv_1m"),
            "backtest_results": str(result_cache_dir),
        },
        "runs": runs,
    }

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
