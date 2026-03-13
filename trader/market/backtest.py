"""Exit strategy backtesting engine (1-minute bar resolution).

Fetches 1-min OHLCV bars via Schwab (primary) or yfinance (fallback),
caches them persistently on disk per (symbol, date), and walks forward
bar-by-bar applying an exit strategy.

Cache persists forever — data collected within the 10-day Schwab window
remains available for backtesting months later.

Usage::

    from trader.market.backtest import run_backtest, evaluate_exit, STRATEGIES

    # Batch backtest:
    results = run_backtest(
        strategy_key="fixed_stop_loss",
        params={"stop_pct": 5.0},
        entries=[{"snapshot_id": "abc", "symbol": "AAPL",
                  "entry_price": 150.0,
                  "entry_time": "2026-01-15T15:40:00+00:00"}],
    )

    # Live exit evaluation (called each cycle by LiveExitMonitor):
    result = evaluate_exit("volume_delta_divergence", {"lookback": 80},
                           bars=df, entry_idx=42, entry_price=150.0,
                           guard_stop_pct=5.0, min_hold=5)
    if result.should_exit:
        print(f"EXIT: {result.reason} at {result.exit_price}")
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import math
import multiprocessing as mp
import os
import time
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Optional per-run strategy detail trace, scoped via ContextVar so concurrent
# backtests do not share counters.
_STRATEGY_TRACE_CTX: ContextVar[dict[str, dict[str, float | int]] | None] = (
    ContextVar("_STRATEGY_TRACE_CTX", default=None)
)


def _trace_add_sec(key: str, dt: float) -> None:
    ctx = _STRATEGY_TRACE_CTX.get()
    if ctx is None or dt <= 0:
        return
    sec = ctx.setdefault("sec", {})
    sec[key] = float(sec.get(key, 0.0)) + float(dt)


def _trace_inc(key: str, n: int = 1) -> None:
    ctx = _STRATEGY_TRACE_CTX.get()
    if ctx is None or n == 0:
        return
    counts = ctx.setdefault("counts", {})
    counts[key] = int(counts.get(key, 0)) + int(n)


ProgressCallback = Callable[[dict[str, Any]], None]


def _safe_emit_progress(
    progress_cb: ProgressCallback | None,
    payload: dict[str, Any],
) -> None:
    """Best-effort progress emission.

    Progress updates should never interfere with the backtest itself.
    """
    if progress_cb is None:
        return
    try:
        progress_cb(payload)
    except Exception:
        log.debug("Ignoring backtest progress callback failure", exc_info=True)


def _entry_time_bounds(entries: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    times = [
        str(e.get("entry_time", e.get("entry_date", ""))).strip()
        for e in entries
        if str(e.get("entry_time", e.get("entry_date", ""))).strip()
    ]
    if not times:
        return None, None
    times.sort()
    return times[0], times[-1]

# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamDef:
    type: str  # "float", "int", or "select"
    default: float | str
    label: str
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[dict[str, str]] | None = None  # for "select": [{"value": ..., "label": ...}, ...]


@dataclass(frozen=True)
class StrategyDef:
    name: str
    key: str
    section: str
    description: str
    params: dict[str, ParamDef]


STRATEGIES: dict[str, StrategyDef] = {
    "fixed_stop_loss": StrategyDef(
        name="Fixed % Stop Loss",
        key="fixed_stop_loss",
        section="Price-Based",
        description="Exit if price drops below entry by stop %.",
        params={
            "stop_pct": ParamDef("float", 5.0, "Stop %", 0.1, 50, 0.5),
        },
    ),
    "fixed_take_profit": StrategyDef(
        name="Fixed % Take Profit",
        key="fixed_take_profit",
        section="Price-Based",
        description="Exit if price rises above entry by reward %.",
        params={
            "reward_pct": ParamDef("float", 10.0, "Reward %", 0.1, 100, 0.5),
        },
    ),
    "risk_reward_target": StrategyDef(
        name="Risk / Reward Target",
        key="risk_reward_target",
        section="Price-Based",
        description="Set a stop loss and a take profit at k\u00d7 the risk.",
        params={
            "stop_pct": ParamDef("float", 5.0, "Stop %", 0.1, 50, 0.5),
            "risk_multiple": ParamDef("float", 2.0, "Risk multiple (k)", 0.5, 10, 0.5),
        },
    ),
    "pct_trailing_stop": StrategyDef(
        name="Percent Trailing Stop",
        key="pct_trailing_stop",
        section="Trailing",
        description="Exit if price drops trail % below its highest point since entry.",
        params={
            "trail_pct": ParamDef("float", 5.0, "Trail %", 0.5, 30, 0.5),
        },
    ),
    "atr_trailing_stop": StrategyDef(
        name="ATR Trailing Stop",
        key="atr_trailing_stop",
        section="Trailing",
        description="Exit if price drops k \u00d7 ATR below its highest point.",
        params={
            "atr_period": ParamDef("int", 14, "ATR period", 5, 50, 1),
            "multiplier": ParamDef("float", 2.0, "Multiplier (k)", 0.5, 5, 0.5),
        },
    ),
    "atr_fixed_stop": StrategyDef(
        name="ATR Fixed Stop",
        key="atr_fixed_stop",
        section="Volatility",
        description="Exit if price drops k \u00d7 ATR below the entry price.",
        params={
            "atr_period": ParamDef("int", 14, "ATR period", 5, 50, 1),
            "multiplier": ParamDef("float", 2.0, "Multiplier (k)", 0.5, 5, 0.5),
        },
    ),
    "close_below_ma": StrategyDef(
        name="Close Below Moving Average",
        key="close_below_ma",
        section="Trend",
        description="Exit if the close falls below the n-bar SMA.",
        params={
            "ma_period": ParamDef("int", 20, "MA period (bars)", 5, 500, 1),
        },
    ),
    "ma_cross_exit": StrategyDef(
        name="Moving Average Cross",
        key="ma_cross_exit",
        section="Trend",
        description="Exit if the short SMA crosses below the long SMA.",
        params={
            "short_period": ParamDef("int", 10, "Short MA (bars)", 3, 200, 1),
            "long_period": ParamDef("int", 50, "Long MA (bars)", 10, 500, 1),
        },
    ),
    "rsi_overbought": StrategyDef(
        name="RSI Overbought Exit",
        key="rsi_overbought",
        section="Momentum",
        description="Exit when RSI rises above the overbought threshold.",
        params={
            "rsi_period": ParamDef("int", 14, "RSI period (bars)", 5, 100, 1),
            "threshold": ParamDef("float", 70.0, "Threshold", 50, 90, 1),
        },
    ),
    "macd_bearish_cross": StrategyDef(
        name="MACD Bearish Cross",
        key="macd_bearish_cross",
        section="Momentum",
        description="Exit when MACD crosses below its signal line.",
        params={
            "fast_period": ParamDef("int", 12, "Fast EMA (bars)", 5, 100, 1),
            "slow_period": ParamDef("int", 26, "Slow EMA (bars)", 10, 200, 1),
            "signal_period": ParamDef("int", 9, "Signal EMA (bars)", 3, 50, 1),
        },
    ),
    "volume_fade": StrategyDef(
        name="Volume Fade Exit",
        key="volume_fade",
        section="Volume",
        description="Exit if bar volume drops below \u03b1 \u00d7 average volume.",
        params={
            "vol_lookback": ParamDef("int", 20, "Lookback (bars)", 5, 200, 1),
            "multiplier": ParamDef("float", 0.5, "Multiplier (\u03b1)", 0.1, 1.0, 0.05),
        },
    ),
    "roc_reversal": StrategyDef(
        name="ROC Reversal Exit",
        key="roc_reversal",
        section="Momentum",
        description="Exit when Rate of Change flips from positive to negative.",
        params={
            "roc_period": ParamDef("int", 10, "ROC period (bars)", 3, 100, 1),
        },
    ),
    "stochastic_overbought": StrategyDef(
        name="Stochastic Overbought Cross",
        key="stochastic_overbought",
        section="Momentum",
        description="Exit when %K crosses below %D in the overbought zone.",
        params={
            "stoch_period": ParamDef("int", 14, "Lookback (bars)", 5, 50, 1),
            "k_smooth": ParamDef("int", 3, "%K smoothing", 1, 10, 1),
            "d_smooth": ParamDef("int", 3, "%D smoothing", 1, 10, 1),
            "threshold": ParamDef("float", 80.0, "Overbought threshold", 60, 95, 1),
        },
    ),
    "adx_trend_decay": StrategyDef(
        name="ADX Trend Decay",
        key="adx_trend_decay",
        section="Momentum",
        description="Exit when ADX drops from strong to weak (trend fading).",
        params={
            "adx_period": ParamDef("int", 14, "ADX period (bars)", 5, 50, 1),
            "weak_threshold": ParamDef("float", 20.0, "Weak threshold", 10, 30, 1),
            "strong_threshold": ParamDef("float", 30.0, "Strong threshold", 20, 50, 1),
            "lookback": ParamDef("int", 10, "Strength lookback (bars)", 5, 50, 1),
        },
    ),
    "volume_delta_divergence": StrategyDef(
        name="Volume Delta Divergence",
        key="volume_delta_divergence",
        section="Volume",
        description="Exit when price makes a new high but cumulative volume delta is declining.",
        params={
            "lookback": ParamDef("int", 30, "Lookback (bars)", 10, 100, 5),
        },
    ),
    "volume_imbalance_flip": StrategyDef(
        name="Volume Imbalance Flip",
        key="volume_imbalance_flip",
        section="Volume",
        description="Exit when rolling volume imbalance flips from bullish to bearish.",
        params={
            "window": ParamDef("int", 30, "Rolling window (bars)", 10, 100, 5),
            "threshold": ParamDef("float", 0.05, "Imbalance threshold (\u03b1)", 0.01, 0.20, 0.01),
        },
    ),
    "max_holding_period": StrategyDef(
        name="Max Holding Period",
        key="max_holding_period",
        section="Time",
        description="Exit after a fixed number of 1-min bars.",
        params={
            "max_bars": ParamDef("int", 390, "Max bars", 1, 3900, 10),
        },
    ),
}


# ---------------------------------------------------------------------------
# Allocation strategy definitions
# ---------------------------------------------------------------------------

RANK_METHOD_CONFIDENCE = "confidence"
RANK_METHOD_UNREAL_PL = "unreal_pl"
RANK_METHOD_TRAILING_SLOPE = "trailing_slope"
RANK_METHOD_VOLUME_TREND = "volume_trend"
RANK_METHOD_RSI_CURRENT = "rsi_current"
RANK_METHOD_TECH_SCORE = "tech_score"

_ALL_RANK_METHODS = {
    RANK_METHOD_CONFIDENCE, RANK_METHOD_UNREAL_PL,
    RANK_METHOD_TRAILING_SLOPE, RANK_METHOD_VOLUME_TREND,
    RANK_METHOD_RSI_CURRENT, RANK_METHOD_TECH_SCORE,
}


def normalize_rank_method(method: str | None) -> str:
    """Canonicalize rank method keys.

    Accepts legacy ``momentum`` as an alias for ``unreal_pl`` so older saved
    configs continue to work.
    """
    key = str(method or "").strip().lower()
    if key in {"", "momentum"}:
        return RANK_METHOD_UNREAL_PL
    if key in _ALL_RANK_METHODS:
        return key
    return RANK_METHOD_UNREAL_PL


_RANK_OPTIONS = [
    {"value": RANK_METHOD_CONFIDENCE, "label": "Signal Confidence"},
    {"value": RANK_METHOD_UNREAL_PL, "label": "Unrealized P&L"},
    {"value": RANK_METHOD_TRAILING_SLOPE, "label": "Price Momentum (Slope)"},
    {"value": RANK_METHOD_VOLUME_TREND, "label": "Accumulation/Distribution"},
    {"value": RANK_METHOD_RSI_CURRENT, "label": "RSI Exhaustion"},
    {"value": RANK_METHOD_TECH_SCORE, "label": "Technical Composite"},
]

_WHEN_FULL_OPTIONS = [
    {"value": "skip", "label": "Skip"},
    {"value": "replace", "label": "Replace Weakest"},
]

AllocationDef = StrategyDef  # same shape — reuse the dataclass

ALLOCATIONS: dict[str, AllocationDef] = {
    "none": AllocationDef(
        name="None (Unlimited)",
        key="none",
        section="Basic",
        description="No capital constraints. Every signal is traded independently.",
        params={},
    ),
    "fixed_dollar": AllocationDef(
        name="Fixed Dollar Per Trade",
        key="fixed_dollar",
        section="Basic",
        description="Each trade gets a fixed % of initial capital. Skip when capital exhausted.",
        params={
            "alloc_pct": ParamDef("float", 5.0, "Allocation %", 1, 50, 1),
        },
    ),
    "max_positions": AllocationDef(
        name="Max Positions",
        key="max_positions",
        section="Position-Limited",
        description="Limit concurrent positions. Choose skip or replace when full.",
        params={
            "max_pos": ParamDef("int", 10, "Max Positions", 1, 50, 1),
            "when_full": ParamDef("select", "skip", "When Full", options=_WHEN_FULL_OPTIONS),
            "rank_method": ParamDef("select", RANK_METHOD_UNREAL_PL, "Rank By", options=_RANK_OPTIONS),
            "replace_min_margin": ParamDef("float", 0.0, "Min Margin to Replace", 0, 1, 0.01),
        },
    ),
    "ranking_realloc": AllocationDef(
        name="Ranking-Based Reallocation",
        key="ranking_realloc",
        section="Capital-Limited",
        description="Each trade gets a fixed % of capital. When full, replace weakest position.",
        params={
            "alloc_pct": ParamDef("float", 5.0, "Allocation %", 1, 50, 1),
            "rank_method": ParamDef("select", RANK_METHOD_UNREAL_PL, "Rank By", options=_RANK_OPTIONS),
            "replace_min_margin": ParamDef("float", 0.0, "Min Margin to Replace", 0, 1, 0.01),
        },
    ),
}


# ---------------------------------------------------------------------------
# Backtest result
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    snapshot_id: str
    symbol: str
    entry_price: float
    entry_time: str
    exit_price: float | None = None
    exit_time: str | None = None
    pnl_pct: float | None = None
    exit_reason: str = "no_data"  # stop, target, signal, time, still_open, no_data
    bars_held: int = 0
    hold_minutes: int = 0
    # Periodic close prices for equity curve (not sent to frontend).
    # List of (iso_timestamp, close_price) at configured resolution.
    periodic_closes: list[tuple[str, float]] | None = None
    # Pre-computed ranking features at periodic intervals (internal only).
    # List of (iso_timestamp, {slope, rsi, ad_slope}) for forward-looking ranking.
    ranking_features: list[tuple[str, dict[str, float]]] | None = None
    # Ranking features at entry time — used to score the new signal symmetrically.
    entry_features: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("periodic_closes", None)
        d.pop("ranking_features", None)
        d.pop("entry_features", None)
        return d


# ---------------------------------------------------------------------------
# Annualized return helpers
# ---------------------------------------------------------------------------

_TRADING_DAYS_PER_YEAR = 252
_BARS_PER_DAY = 390  # 6.5 hours × 60 minutes

_BACKTEST_RESULT_CACHE_VERSION = "v3"
_BACKTEST_RESULT_CACHE_DIR = Path.home() / ".cache" / "alpaca-news" / "backtest_results"
_DEFAULT_BACKTEST_MAX_WORKERS = 4


def _ts_cache_key(value: Any) -> str:
    """Normalize timestamps for stable cache keys."""
    if value is None:
        return ""
    try:
        return pd.Timestamp(value).isoformat()
    except Exception:
        return str(value)


def _normalize_periodic_closes(
    values: list[Any] | None,
) -> list[tuple[str, float]] | None:
    if not values:
        return None
    out: list[tuple[str, float]] = []
    for item in values:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        out.append((str(item[0]), float(item[1])))
    return out or None


def _normalize_ranking_features(
    values: list[Any] | None,
) -> list[tuple[str, dict[str, float]]] | None:
    if not values:
        return None
    out: list[tuple[str, dict[str, float]]] = []
    for item in values:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        ts = str(item[0])
        raw = item[1] if isinstance(item[1], dict) else {}
        out.append((
            ts,
            {str(k): float(v) for k, v in raw.items()},
        ))
    return out or None


def _serialize_backtest_result(result: BacktestResult) -> dict[str, Any]:
    return asdict(result)


def _deserialize_backtest_result(payload: dict[str, Any]) -> BacktestResult:
    data = dict(payload)
    data["periodic_closes"] = _normalize_periodic_closes(data.get("periodic_closes"))
    data["ranking_features"] = _normalize_ranking_features(data.get("ranking_features"))
    entry_features = data.get("entry_features")
    if isinstance(entry_features, dict):
        data["entry_features"] = {str(k): float(v) for k, v in entry_features.items()}
    else:
        data["entry_features"] = None
    return BacktestResult(**data)


def _backtest_result_cache_path(symbol: str, cache_key: str) -> Path:
    return _BACKTEST_RESULT_CACHE_DIR / symbol.upper() / f"{cache_key}.json"


def _build_backtest_result_cache_key(
    *,
    symbol: str,
    snapshot_id: str,
    entry_time: str,
    actual_entry_bar: Any,
    effective_entry_price: float,
    strategy_key: str,
    params: dict[str, float],
    market_close: str | None,
    min_hold: int,
    guard_stop_pct: float,
    guard_target_pct: float,
    guard_trail_pct: float,
    stats_resolution_minutes: int,
    data_start: Any,
) -> str:
    payload = {
        "version": _BACKTEST_RESULT_CACHE_VERSION,
        "symbol": symbol.upper(),
        "snapshot_id": snapshot_id,
        "entry_time": entry_time,
        "actual_entry_bar": _ts_cache_key(actual_entry_bar),
        "effective_entry_price": round(float(effective_entry_price), 8),
        "strategy_key": strategy_key,
        "params": params,
        "market_close": market_close,
        "min_hold": int(min_hold),
        "guard_stop_pct": float(guard_stop_pct),
        "guard_target_pct": float(guard_target_pct),
        "guard_trail_pct": float(guard_trail_pct),
        "stats_resolution_minutes": int(stats_resolution_minutes),
        "data_start": _ts_cache_key(data_start),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_backtest_cache_valid(
    result: BacktestResult,
    meta: dict[str, Any] | None,
    current_data_end: Any,
) -> bool:
    """Return True when a cached result is valid for the current bar horizon.

    Finalized exits are stable once their exit timestamp is in the available
    data. Open/no-data outcomes can change as newer bars arrive, so they are
    only reusable when the current horizon has not advanced past the cached one.
    """
    if not isinstance(meta, dict):
        return False
    cached_data_end = meta.get("data_end")
    if not cached_data_end:
        return False
    try:
        current_end = pd.Timestamp(current_data_end)
        cached_end = pd.Timestamp(str(cached_data_end))
    except Exception:
        return False
    if current_end < cached_end:
        return False
    if result.exit_reason in {"still_open", "no_data"}:
        return current_end == cached_end
    exit_time = str(result.exit_time or "").strip()
    if not exit_time:
        return current_end == cached_end
    try:
        exit_ts = pd.Timestamp(exit_time)
    except Exception:
        return False
    return exit_ts <= current_end


def _read_backtest_result_cache(
    symbol: str,
    cache_key: str,
    *,
    current_data_end: Any,
) -> BacktestResult | None:
    path = _backtest_result_cache_path(symbol, cache_key)
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        if "result" in payload:
            meta = payload.get("meta")
            result = _deserialize_backtest_result(payload.get("result") or {})
            return result if _is_backtest_cache_valid(result, meta, current_data_end) else None
        result = _deserialize_backtest_result(payload)
        return result if _is_backtest_cache_valid(result, None, current_data_end) else None
    except Exception:
        log.debug("Ignoring corrupt backtest cache for %s", path, exc_info=True)
        return None


def _write_backtest_result_cache(
    symbol: str,
    cache_key: str,
    result: BacktestResult,
    *,
    data_end: Any,
) -> None:
    path = _backtest_result_cache_path(symbol, cache_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _BACKTEST_RESULT_CACHE_VERSION,
            "result": _serialize_backtest_result(result),
            "meta": {
                "data_end": _ts_cache_key(data_end),
            },
        }
        path.write_text(json.dumps(payload))
    except OSError:
        log.debug("Failed to write backtest cache %s", path, exc_info=True)


def _merge_number_dict(target: dict[str, float | int], source: dict[str, Any]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _resolve_backtest_workers(symbol_count: int, total_entries: int) -> int:
    """Return the number of symbol-eval worker processes to use."""
    env_value = os.getenv("BACKTEST_SYMBOL_WORKERS")
    if env_value is not None:
        try:
            requested = max(0, int(env_value))
        except ValueError:
            requested = 0
        if requested <= 1:
            return 1
        return min(symbol_count, requested)

    cpu_count = os.cpu_count() or 1
    if cpu_count <= 1 or symbol_count <= 1 or total_entries < 100:
        return 1
    return min(symbol_count, cpu_count, _DEFAULT_BACKTEST_MAX_WORKERS)


def _evaluate_symbol_entries(
    *,
    symbol: str,
    sym_entries: list[dict[str, Any]],
    parsed_times: list[tuple[dict[str, Any], datetime]],
    df: pd.DataFrame | None,
    strategy_key: str,
    params: dict[str, float],
    market_close: str | None,
    min_hold: int,
    guard_stop_pct: float,
    guard_target_pct: float,
    guard_trail_pct: float,
    stats_resolution_minutes: int,
    capture_trace: bool,
) -> dict[str, Any]:
    """Evaluate all entries for one symbol using already-loaded bars."""
    stage_sec: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    indicator_cache: dict[tuple[Any, ...], Any] = {}
    results: list[BacktestResult] = []
    trace_token = None
    strategy_trace_local: dict[str, dict[str, float | int]] | None = None
    if capture_trace:
        strategy_trace_local = {"sec": {}, "counts": {}}
        trace_token = _STRATEGY_TRACE_CTX.set(strategy_trace_local)

    def _mark(stage: str, start: float) -> None:
        stage_sec[stage] += max(0.0, time.perf_counter() - start)

    sym_start = time.perf_counter()
    last_entry_time: str | None = None
    base_runner = _STRATEGY_RUNNERS[strategy_key]

    try:
        if df is None or df.empty:
            counts["symbols_no_data"] += 1
            for e in sym_entries:
                results.append(BacktestResult(
                    snapshot_id=e["snapshot_id"],
                    symbol=symbol,
                    entry_price=e["entry_price"],
                    entry_time=e.get("entry_time", e.get("entry_date", "")),
                    exit_reason="no_data",
                ))
                counts["trades_no_data"] += 1
                last_entry_time = e.get("entry_time", e.get("entry_date", "")) or last_entry_time
            return {
                "symbol": symbol,
                "results": results,
                "counts": dict(counts),
                "stage_sec": dict(stage_sec),
                "symbol_sec": max(0.0, time.perf_counter() - sym_start),
                "last_entry_time": last_entry_time,
                "strategy_trace": strategy_trace_local or {"sec": {}, "counts": {}},
            }

        counts["symbols_with_data"] += 1
        data_start = df.index[0]
        data_end = df.index[-1]
        for e, entry_dt in parsed_times:
            entry_price = e["entry_price"]
            entry_time_str = e.get("entry_time", e.get("entry_date", ""))
            last_entry_time = entry_time_str or last_entry_time

            t_locate = time.perf_counter()
            entry_ts = pd.Timestamp(entry_dt, tz=df.index.tz).as_unit(df.index.unit)
            entry_idx = df.index.searchsorted(entry_ts)
            if entry_idx >= len(df):
                results.append(BacktestResult(
                    snapshot_id=e["snapshot_id"],
                    symbol=symbol,
                    entry_price=entry_price,
                    entry_time=entry_time_str,
                    exit_reason="no_data",
                ))
                counts["trades_no_data"] += 1
                _mark("entry_locate", t_locate)
                continue

            actual_entry_bar = df.index[entry_idx]
            entry_time_of_day = actual_entry_bar.hour * 60 + actual_entry_bar.minute
            if entry_price <= 0 or entry_time_of_day <= (9 * 60 + 30):
                entry_price = float(df.iloc[entry_idx]["Close"])
            _mark("entry_locate", t_locate)

            cache_key = _build_backtest_result_cache_key(
                symbol=symbol,
                snapshot_id=str(e.get("snapshot_id") or ""),
                entry_time=entry_time_str,
                actual_entry_bar=df.index[entry_idx],
                effective_entry_price=entry_price,
                strategy_key=strategy_key,
                params=params,
                market_close=market_close,
                min_hold=min_hold,
                guard_stop_pct=guard_stop_pct,
                guard_target_pct=guard_target_pct,
                guard_trail_pct=guard_trail_pct,
                stats_resolution_minutes=stats_resolution_minutes,
                data_start=data_start,
            )
            cached_result = _read_backtest_result_cache(
                symbol,
                cache_key,
                current_data_end=data_end,
            )
            if cached_result is not None:
                results.append(cached_result)
                counts["result_cache_hits"] += 1
                if cached_result.pnl_pct is None:
                    counts["trades_no_data"] += 1
                else:
                    counts["trades_valid"] += 1
                if cached_result.exit_reason == "still_open":
                    counts["trades_still_open"] += 1
                elif cached_result.exit_reason.startswith("guard_"):
                    counts["trades_guard_exit"] += 1
                else:
                    counts["trades_strategy_exit"] += 1
                continue
            counts["result_cache_misses"] += 1

            run_idx = min(entry_idx + max(0, min_hold), len(df) - 1)
            guard_stop = (
                entry_price * (1 - guard_stop_pct / 100)
                if guard_stop_pct > 0 else None
            )
            guard_target = (
                entry_price * (1 + guard_target_pct / 100)
                if guard_target_pct > 0 else None
            )
            t_run = time.perf_counter()
            _trace_inc("runner_calls")
            exit_price, exit_time, reason, bars_held = base_runner(
                df, run_idx, entry_price, params,
                guard_stop=guard_stop, guard_target=guard_target,
                indicator_cache=indicator_cache,
            )
            _mark("strategy_eval", t_run)
            bars_held = bars_held + (run_idx - entry_idx)

            if guard_trail_pct > 0:
                highs = df["High"].to_numpy(dtype=float, copy=False)
                lows = df["Low"].to_numpy(dtype=float, copy=False)
                trail_hit = _first_trail_guard_hit(
                    highs, lows, run_idx, entry_price, guard_trail_pct,
                )
                if trail_hit is not None:
                    trail_rel, trail_price, trail_reason = trail_hit
                    trail_abs = run_idx + trail_rel
                    trail_bars = trail_rel + 1 + (run_idx - entry_idx)
                    if exit_price is None or trail_abs <= (entry_idx + bars_held - 1):
                        exit_price = trail_price
                        exit_time = _fmt_ts(df.index, trail_abs)
                        reason = trail_reason
                        bars_held = trail_bars

            t_post = time.perf_counter()
            if exit_price is not None:
                exit_price = float(exit_price)
                pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
            elif reason == "still_open":
                exit_price = float(df.iloc[-1]["Close"])
                pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
                exit_time = _fmt_ts(df.index, len(df) - 1)
            else:
                pnl_pct = None
            bars_held = int(bars_held)
            _mark("trade_post", t_post)

            hold_minutes = bars_held
            if exit_time:
                try:
                    exit_dt = pd.Timestamp(exit_time)
                    entry_bar_dt = df.index[entry_idx]
                    delta = exit_dt - entry_bar_dt
                    hold_minutes = max(1, int(delta.total_seconds() / 60))
                except Exception:
                    pass

            t_curve = time.perf_counter()
            pc = _extract_periodic_closes(
                df, entry_idx, bars_held, stats_resolution_minutes,
            ) if pnl_pct is not None else None
            _mark("periodic_closes", t_curve)

            rf = _extract_ranking_features(
                df, entry_idx, bars_held, stats_resolution_minutes,
            ) if pnl_pct is not None else None
            ef = _compute_entry_features(df, entry_idx) if pnl_pct is not None else None

            result = BacktestResult(
                snapshot_id=e["snapshot_id"],
                symbol=symbol,
                entry_price=entry_price,
                entry_time=entry_time_str,
                exit_price=round(exit_price, 2) if exit_price is not None else None,
                exit_time=exit_time,
                pnl_pct=pnl_pct,
                exit_reason=reason,
                bars_held=bars_held,
                hold_minutes=hold_minutes,
                periodic_closes=pc,
                ranking_features=rf,
                entry_features=ef,
            )
            results.append(result)
            _write_backtest_result_cache(
                symbol,
                cache_key,
                result,
                data_end=data_end,
            )
            if pnl_pct is None:
                counts["trades_no_data"] += 1
            else:
                counts["trades_valid"] += 1
            if reason == "still_open":
                counts["trades_still_open"] += 1
            elif reason.startswith("guard_"):
                counts["trades_guard_exit"] += 1
            else:
                counts["trades_strategy_exit"] += 1

        return {
            "symbol": symbol,
            "results": results,
            "counts": dict(counts),
            "stage_sec": dict(stage_sec),
            "symbol_sec": max(0.0, time.perf_counter() - sym_start),
            "last_entry_time": last_entry_time,
            "strategy_trace": strategy_trace_local or {"sec": {}, "counts": {}},
        }
    finally:
        if trace_token is not None:
            _STRATEGY_TRACE_CTX.reset(trace_token)


def compute_ann_a(results: list[BacktestResult]) -> dict[str, float] | None:
    """Annualized stats — unlimited capital (time-weighted log return).

    Returns dict with ann (%), daily_pnl (%/day), vol (%), sharpe,
    or None if no valid trades.
    """
    daily_rates: list[float] = []  # per-trade daily log return x_i = ℓ_i / d_i
    sum_log = 0.0
    sum_days = 0.0
    for r in results:
        if r.pnl_pct is None:
            continue
        log_r = math.log(1 + r.pnl_pct / 100)
        days = max(r.bars_held, 1) / _BARS_PER_DAY
        sum_log += log_r
        sum_days += days
        daily_rates.append(log_r / days)
    if sum_days <= 0 or len(daily_rates) < 2:
        return None
    daily_log = sum_log / sum_days
    ann = (math.exp(_TRADING_DAYS_PER_YEAR * daily_log) - 1) * 100
    # Volatility: std of per-trade daily rates, annualized
    mean_x = sum(daily_rates) / len(daily_rates)
    var_x = sum((x - mean_x) ** 2 for x in daily_rates) / (len(daily_rates) - 1)
    std_x = math.sqrt(var_x)
    vol = std_x * math.sqrt(_TRADING_DAYS_PER_YEAR) * 100
    sharpe = (daily_log * _TRADING_DAYS_PER_YEAR) / (std_x * math.sqrt(_TRADING_DAYS_PER_YEAR)) if std_x > 0 else None
    daily_pnl = daily_log * 100  # %/trading-day
    return {"ann": ann, "daily_pnl": daily_pnl, "vol": vol, "sharpe": sharpe}



def compute_ann_b(
    results: list[BacktestResult],
    resolution_minutes: int = 60,
    weight_per_trade: float | None = None,
) -> dict[str, float] | None:
    """Annualized stats — fixed capital split (equity curve from real prices).

    Uses periodic close prices (at the given resolution) to build a portfolio
    equity curve.  When *weight_per_trade* is None the classic 1/N equal-weight
    split is used (100 % invested at all times).  When set (e.g. 0.05 for 5 %
    per trade), each position gets that fixed fraction and any uninvested
    capital earns 0 % (cash drag).

    Returns dict with ann (%), vol (%), sharpe, or None if insufficient data.
    """
    periods_per_year = _TRADING_DAYS_PER_YEAR * _BARS_PER_DAY / resolution_minutes

    # For each trade, compute per-period log returns from actual prices
    all_timestamps: set[str] = set()
    trade_returns_list: list[dict[str, float]] = []

    for r in results:
        if r.pnl_pct is None or not r.periodic_closes:
            continue
        prev_price = r.entry_price
        returns: dict[str, float] = {}
        for ts, close in r.periodic_closes:
            if prev_price > 0 and close > 0:
                returns[ts] = math.log(close / prev_price)
            prev_price = close
        if returns:
            trade_returns_list.append(returns)
            all_timestamps.update(returns.keys())

    if not trade_returns_list or len(all_timestamps) < 2:
        return None

    # Sort timestamps chronologically, build portfolio return series
    sorted_ts = sorted(all_timestamps)
    portfolio_returns: list[float] = []
    equity = 1.0
    for ts in sorted_ts:
        active = [tr[ts] for tr in trade_returns_list if ts in tr]
        if active:
            avg_r = sum(active) / len(active)
            if weight_per_trade is not None:
                # Fixed allocation: invested fraction may be < 100 %
                invested = min(len(active) * weight_per_trade, 1.0)
                r_t = invested * avg_r
            else:
                r_t = avg_r  # 1/N capital split (100 % invested)
            portfolio_returns.append(r_t)
            equity *= math.exp(r_t)

    n = len(portfolio_returns)
    if n < 2:
        return None

    # Annualized return (CAGR)
    ann = (equity ** (periods_per_year / n) - 1) * 100

    # Volatility & Sharpe
    mean_r = sum(portfolio_returns) / n
    var_r = sum((x - mean_r) ** 2 for x in portfolio_returns) / (n - 1)
    std_r = math.sqrt(var_r)
    vol = std_r * math.sqrt(periods_per_year) * 100
    sharpe = (mean_r * periods_per_year) / (std_r * math.sqrt(periods_per_year)) if std_r > 0 else None
    return {"ann": ann, "vol": vol, "sharpe": sharpe}


# ---------------------------------------------------------------------------
# Allocation engine — post-hoc capital / position filtering
# ---------------------------------------------------------------------------


def _price_at_time(result: BacktestResult, target_iso: str) -> float | None:
    """Look up a position's price at *target_iso* from its periodic_closes.

    Returns the close of the latest snapshot whose timestamp <= target_iso,
    or None if no periodic data is available.
    """
    if not result.periodic_closes:
        return None
    best: float | None = None
    for ts, close in result.periodic_closes:
        if ts <= target_iso:
            best = close
        else:
            break  # periodic_closes are chronologically ordered
    return best


def _truncate_result(
    result: BacktestResult, exit_iso: str, exit_price: float,
) -> BacktestResult:
    """Return a copy of *result* early-exited at the given time/price."""
    pnl = round((exit_price - result.entry_price) / result.entry_price * 100, 2) if result.entry_price > 0 else 0.0
    # Trim periodic_closes to only include data up to exit
    trimmed_pc = None
    if result.periodic_closes:
        trimmed_pc = [(ts, c) for ts, c in result.periodic_closes if ts <= exit_iso]
    # Approximate hold_minutes from entry_time to exit_iso
    hold_minutes = result.hold_minutes
    try:
        entry_dt = pd.Timestamp(result.entry_time)
        exit_dt = pd.Timestamp(exit_iso)
        hold_minutes = max(1, int((exit_dt - entry_dt).total_seconds() / 60))
    except Exception:
        pass
    return BacktestResult(
        snapshot_id=result.snapshot_id,
        symbol=result.symbol,
        entry_price=result.entry_price,
        entry_time=result.entry_time,
        exit_price=round(exit_price, 2),
        exit_time=exit_iso,
        pnl_pct=pnl,
        exit_reason="replaced",
        bars_held=result.bars_held,
        hold_minutes=hold_minutes,
        periodic_closes=trimmed_pc,
    )


# ---- ranking helpers -------------------------------------------------------


def _rank_confidence(
    open_positions: dict[str, BacktestResult],
    entry_confidence: dict[str, float],
    _target_iso: str,
) -> dict[str, float]:
    """Score each open position by its original signal confidence."""
    return {sid: entry_confidence.get(sid, 0.5) for sid in open_positions}


def _rank_unreal_pl(
    open_positions: dict[str, BacktestResult],
    _entry_confidence: dict[str, float],
    target_iso: str,
) -> dict[str, float]:
    """Score each open position by unrealised P&L at *target_iso*."""
    scores: dict[str, float] = {}
    for sid, r in open_positions.items():
        cur = _price_at_time(r, target_iso)
        if cur is not None and r.entry_price > 0:
            scores[sid] = (cur - r.entry_price) / r.entry_price
        else:
            scores[sid] = 0.0
    return scores


def _features_at_time(result: BacktestResult, target_iso: str) -> dict[str, float] | None:
    """Look up ranking features at *target_iso* from ranking_features.

    Parallel to ``_price_at_time()`` — returns the latest feature dict
    whose timestamp <= target_iso.
    """
    if not result.ranking_features:
        return None
    best: dict[str, float] | None = None
    for ts, feat in result.ranking_features:
        if ts <= target_iso:
            best = feat
        else:
            break
    return best


def _rank_trailing_slope(
    open_positions: dict[str, BacktestResult],
    _entry_confidence: dict[str, float],
    target_iso: str,
) -> dict[str, float]:
    """Score by price momentum: normalized slope of recent closes."""
    scores: dict[str, float] = {}
    for sid, r in open_positions.items():
        feat = _features_at_time(r, target_iso)
        scores[sid] = feat["slope"] if feat else 0.0
    return scores


def _rank_volume_trend(
    open_positions: dict[str, BacktestResult],
    _entry_confidence: dict[str, float],
    target_iso: str,
) -> dict[str, float]:
    """Score by Accumulation/Distribution slope."""
    scores: dict[str, float] = {}
    for sid, r in open_positions.items():
        feat = _features_at_time(r, target_iso)
        scores[sid] = feat["ad_slope"] if feat else 0.0
    return scores


def _rank_rsi_current(
    open_positions: dict[str, BacktestResult],
    _entry_confidence: dict[str, float],
    target_iso: str,
) -> dict[str, float]:
    """Score by RSI exhaustion: lower RSI = more room to run = higher score."""
    scores: dict[str, float] = {}
    for sid, r in open_positions.items():
        feat = _features_at_time(r, target_iso)
        rsi = feat["rsi"] if feat else 50.0
        scores[sid] = 100.0 - rsi  # invert
    return scores


def _rank_tech_score(
    open_positions: dict[str, BacktestResult],
    entry_confidence: dict[str, float],
    target_iso: str,
    weights: tuple[float, float, float] = (0.4, 0.3, 0.3),
) -> dict[str, float]:
    """Technical composite: z-score blend of slope, A/D slope, and inverted RSI."""
    slope_scores = _rank_trailing_slope(open_positions, entry_confidence, target_iso)
    ad_scores = _rank_volume_trend(open_positions, entry_confidence, target_iso)
    rsi_scores = _rank_rsi_current(open_positions, entry_confidence, target_iso)
    sids = list(open_positions.keys())
    if len(sids) < 2:
        return slope_scores  # can't z-score with < 2, fall back to slope

    def _z(vals: list[float]) -> list[float]:
        m = sum(vals) / len(vals)
        v = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
        s = math.sqrt(v) if v > 0 else 1.0
        return [(x - m) / s for x in vals]

    w1, w2, w3 = weights
    z_slope = _z([slope_scores[s] for s in sids])
    z_ad = _z([ad_scores[s] for s in sids])
    z_rsi = _z([rsi_scores[s] for s in sids])
    return {
        sid: w1 * zs + w2 * za + w3 * zr
        for sid, zs, za, zr in zip(sids, z_slope, z_ad, z_rsi)
    }


# Forward-looking methods that use ranking_features
_FEATURE_BASED_METHODS = {
    RANK_METHOD_TRAILING_SLOPE, RANK_METHOD_VOLUME_TREND,
    RANK_METHOD_RSI_CURRENT, RANK_METHOD_TECH_SCORE,
}


def _compute_scores(
    method: str,
    open_positions: dict[str, BacktestResult],
    entry_confidence: dict[str, float],
    target_iso: str,
) -> dict[str, float]:
    """Dispatch to the appropriate ranking function."""
    method = normalize_rank_method(method)
    if method == RANK_METHOD_CONFIDENCE:
        return _rank_confidence(open_positions, entry_confidence, target_iso)
    if method == RANK_METHOD_TRAILING_SLOPE:
        return _rank_trailing_slope(open_positions, entry_confidence, target_iso)
    if method == RANK_METHOD_VOLUME_TREND:
        return _rank_volume_trend(open_positions, entry_confidence, target_iso)
    if method == RANK_METHOD_RSI_CURRENT:
        return _rank_rsi_current(open_positions, entry_confidence, target_iso)
    if method == RANK_METHOD_TECH_SCORE:
        return _rank_tech_score(open_positions, entry_confidence, target_iso)
    return _rank_unreal_pl(open_positions, entry_confidence, target_iso)


def _score_new_signal(
    rank_method: str,
    new_confidence: float,
    new_features: dict[str, float] | None,
) -> float:
    """Compute the raw score for an incoming signal using the same method as holdings."""
    if rank_method == RANK_METHOD_CONFIDENCE:
        return new_confidence
    if rank_method == RANK_METHOD_UNREAL_PL:
        return 0.0  # just entered — no unrealised P&L yet
    # Forward-looking methods: use the signal's own features (symmetric scoring)
    if rank_method in _FEATURE_BASED_METHODS and new_features:
        if rank_method == RANK_METHOD_TRAILING_SLOPE:
            return new_features.get("slope", 0.0)
        if rank_method == RANK_METHOD_VOLUME_TREND:
            return new_features.get("ad_slope", 0.0)
        if rank_method == RANK_METHOD_RSI_CURRENT:
            return 100.0 - new_features.get("rsi", 50.0)
        if rank_method == RANK_METHOD_TECH_SCORE:
            return new_features.get("slope", 0.0)  # proxy; normalized below
    return 0.0


def _normalize_scores_0_1(scores: dict[str, float], new_score: float) -> tuple[dict[str, float], float]:
    """Normalize all scores (holdings + new signal) to the 0–1 range.

    Uses min-max over the full set so that ``replace_min_margin`` is
    uniformly meaningful across all ranking methods.  When all scores are
    equal the function returns 0.5 for everything (no replacement possible
    with any positive margin).
    """
    all_vals = list(scores.values()) + [new_score]
    lo = min(all_vals)
    hi = max(all_vals)
    span = hi - lo
    if span == 0:
        normed = {k: 0.5 for k in scores}
        return normed, 0.5
    normed = {k: (v - lo) / span for k, v in scores.items()}
    new_normed = (new_score - lo) / span
    return normed, new_normed


def _compute_tech_score_with_new(
    open_positions: dict[str, BacktestResult],
    entry_confidence: dict[str, float],
    target_iso: str,
    new_features: dict[str, float],
    weights: tuple[float, float, float] = (0.4, 0.3, 0.3),
) -> tuple[dict[str, float], float]:
    """Compute tech_score for holdings AND new signal in one z-score pass.

    Returns (holding_scores, new_signal_score) — all z-scored together so
    the new signal gets a proper composite score instead of a raw slope proxy.
    """
    # Collect features for all holdings + new signal
    all_features: dict[str, dict[str, float]] = {}
    for sid, r in open_positions.items():
        feat = _features_at_time(r, target_iso)
        all_features[sid] = feat or {"slope": 0.0, "rsi": 50.0, "ad_slope": 0.0}
    new_key = "__new_signal__"
    all_features[new_key] = new_features

    sids = list(all_features.keys())
    if len(sids) < 3:  # need at least 2 holdings + 1 new for meaningful z-scores
        # Fall back to slope for everyone
        holding_scores = {s: all_features[s]["slope"] for s in open_positions}
        return holding_scores, new_features.get("slope", 0.0)

    def _z(vals: list[float]) -> list[float]:
        m = sum(vals) / len(vals)
        v = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
        s = math.sqrt(v) if v > 0 else 1.0
        return [(x - m) / s for x in vals]

    w1, w2, w3 = weights
    z_slope = _z([all_features[s]["slope"] for s in sids])
    z_ad = _z([all_features[s]["ad_slope"] for s in sids])
    z_rsi = _z([100.0 - all_features[s]["rsi"] for s in sids])

    all_scores = {
        sid: w1 * zs + w2 * za + w3 * zr
        for sid, zs, za, zr in zip(sids, z_slope, z_ad, z_rsi)
    }
    holding_scores = {s: all_scores[s] for s in open_positions}
    return holding_scores, all_scores[new_key]


def _try_replace(
    new_sid: str,
    new_confidence: float,
    open_positions: dict[str, BacktestResult],
    entry_confidence: dict[str, float],
    target_iso: str,
    rank_method: str,
    new_features: dict[str, float] | None = None,
    min_margin: float = 0.0,
) -> str | None:
    """If the new signal outranks the weakest open position, return the
    snapshot_id of the position to replace.  Otherwise return None.

    All scores are normalized to 0–1 before comparison so that
    ``min_margin`` is uniformly meaningful regardless of ranking method.
    """
    if not open_positions:
        return None
    rank_method = normalize_rank_method(rank_method)

    # For tech_score: include new signal in z-score computation (symmetric)
    if (rank_method == RANK_METHOD_TECH_SCORE
            and new_features and len(open_positions) >= 2):
        raw_scores, raw_new = _compute_tech_score_with_new(
            open_positions, entry_confidence, target_iso, new_features,
        )
    else:
        raw_scores = _compute_scores(
            rank_method, open_positions, entry_confidence, target_iso,
        )
        raw_new = _score_new_signal(rank_method, new_confidence, new_features)

    scores, new_score = _normalize_scores_0_1(raw_scores, raw_new)
    worst_sid = min(scores, key=scores.get)  # type: ignore[arg-type]
    if new_score > scores[worst_sid] + min_margin:
        return worst_sid
    return None


# ---- main allocation entry point -------------------------------------------


def apply_allocation(
    results: list[BacktestResult],
    entries: list[dict[str, Any]],
    alloc_key: str,
    alloc_params: dict[str, Any],
    progress_cb: ProgressCallback | None = None,
) -> tuple[list[BacktestResult], dict[str, int]]:
    """Filter / modify *results* according to an allocation strategy.

    Args:
        results: Full list from ``run_backtest`` (one per entry, same order).
        entries: Corresponding entry dicts (must include ``confidence``).
        alloc_key: Key into :data:`ALLOCATIONS`.
        alloc_params: User-provided parameter values.

    Returns:
        (filtered_results, stats) where *stats* has counts:
        ``taken``, ``skipped``, ``replaced``.
    """
    if alloc_key == "none" or alloc_key not in ALLOCATIONS:
        return results, {"taken": len(results), "skipped": 0, "replaced": 0}

    # Build a quick lookup: snapshot_id -> (index, result, entry)
    by_sid: dict[str, tuple[int, BacktestResult, dict]] = {}
    for i, (r, e) in enumerate(zip(results, entries)):
        by_sid[r.snapshot_id] = (i, r, e)

    # Sort entries chronologically for the portfolio walk-forward
    chrono = sorted(by_sid.values(), key=lambda t: t[2].get("entry_time", ""))
    timeline_start = chrono[0][2].get("entry_time", "") if chrono else None
    timeline_end = chrono[-1][2].get("entry_time", "") if chrono else None
    total = len(chrono)

    # Determine capacity rule
    if alloc_key == "fixed_dollar":
        alloc_pct = float(alloc_params.get("alloc_pct", 5))
        max_concurrent = max(1, int(100 / alloc_pct))
        do_replace = False
    elif alloc_key == "max_positions":
        max_concurrent = int(alloc_params.get("max_pos", 10))
        do_replace = str(alloc_params.get("when_full", "skip")) == "replace"
    elif alloc_key == "ranking_realloc":
        alloc_pct = float(alloc_params.get("alloc_pct", 5))
        max_concurrent = max(1, int(100 / alloc_pct))
        do_replace = True
    else:
        return results, {"taken": len(results), "skipped": 0, "replaced": 0}

    rank_method = normalize_rank_method(str(alloc_params.get("rank_method", RANK_METHOD_UNREAL_PL)))
    replace_min_margin = float(alloc_params.get("replace_min_margin", 0.0))

    # Walk forward chronologically
    open_positions: dict[str, BacktestResult] = {}   # sid -> result
    entry_confidence: dict[str, float] = {}           # sid -> confidence
    out: dict[int, BacktestResult] = {}               # original index -> final result
    stats = {"taken": 0, "skipped": 0, "replaced": 0}

    _safe_emit_progress(progress_cb, {
        "phase": "allocation",
        "label": "Applying allocation",
        "processed": 0,
        "total": total,
        "phase_progress": 0.0,
        "chrono": True,
        "timeline_start": timeline_start,
        "timeline_end": timeline_end,
    })

    for pos, (orig_idx, result, entry) in enumerate(chrono, start=1):
        sid = result.snapshot_id
        new_entry_time = entry.get("entry_time", "")
        new_confidence = float(entry.get("confidence", 0.5))

        # Evict positions that have already exited by this entry's time
        closed = [
            s for s, r in open_positions.items()
            if r.exit_time is not None and r.exit_time <= new_entry_time
        ]
        for s in closed:
            del open_positions[s]

        # Skip trades that had no data / couldn't be evaluated
        if result.pnl_pct is None and result.exit_reason in ("no_data", "unknown_strategy"):
            out[orig_idx] = result
            stats["taken"] += 1
            continue

        if len(open_positions) < max_concurrent:
            # Capacity available — take the trade
            open_positions[sid] = result
            entry_confidence[sid] = new_confidence
            out[orig_idx] = result
            stats["taken"] += 1
        elif do_replace:
            # Try to replace weakest
            victim_sid = _try_replace(
                sid, new_confidence, open_positions, entry_confidence,
                new_entry_time, rank_method,
                new_features=result.entry_features,
                min_margin=replace_min_margin,
            )
            if victim_sid is not None:
                # Early-exit the victim
                victim_result = open_positions[victim_sid]
                exit_price = _price_at_time(victim_result, new_entry_time)
                if exit_price is None:
                    exit_price = victim_result.entry_price  # fallback
                truncated = _truncate_result(victim_result, new_entry_time, exit_price)
                # Find victim's original index and update
                victim_orig_idx = next(
                    i for i, (idx, _, _) in enumerate(chrono)
                    if chrono[i][1].snapshot_id == victim_sid
                )
                out[chrono[victim_orig_idx][0]] = truncated
                stats["replaced"] += 1
                # Swap in the new position
                del open_positions[victim_sid]
                open_positions[sid] = result
                entry_confidence[sid] = new_confidence
                out[orig_idx] = result
                stats["taken"] += 1
            else:
                # New signal doesn't outrank weakest — skip
                out[orig_idx] = BacktestResult(
                    snapshot_id=sid,
                    symbol=result.symbol,
                    entry_price=result.entry_price,
                    entry_time=result.entry_time,
                    exit_reason="skipped",
                )
                stats["skipped"] += 1
        else:
            # Skip — at capacity
            out[orig_idx] = BacktestResult(
                snapshot_id=sid,
                symbol=result.symbol,
                entry_price=result.entry_price,
                entry_time=result.entry_time,
                exit_reason="skipped",
            )
            stats["skipped"] += 1

        _safe_emit_progress(progress_cb, {
            "phase": "allocation",
            "label": "Applying allocation",
            "processed": pos,
            "total": total,
            "phase_progress": (pos / total) if total > 0 else 1.0,
            "chrono": True,
            "current_entry_time": new_entry_time or None,
            "current_symbol": result.symbol,
            "timeline_start": timeline_start,
            "timeline_end": timeline_end,
            "alloc_taken": stats["taken"],
            "alloc_skipped": stats["skipped"],
            "alloc_replaced": stats["replaced"],
        })

    # Return results in original order
    final = [out[i] for i in range(len(results)) if i in out]
    # Adjust taken count: replaced victims were already counted as taken originally
    stats["taken"] = stats["taken"] - stats["replaced"]
    return final, stats


def weight_per_trade_for_allocation(
    alloc_key: str, alloc_params: dict[str, Any],
) -> float | None:
    """Return the fixed weight per trade for the equity curve, or None for 1/N."""
    if alloc_key == "fixed_dollar":
        return float(alloc_params.get("alloc_pct", 5)) / 100
    if alloc_key == "ranking_realloc":
        return float(alloc_params.get("alloc_pct", 5)) / 100
    if alloc_key == "max_positions":
        return 1.0 / max(1, int(alloc_params.get("max_pos", 10)))
    return None


# ---------------------------------------------------------------------------
# Portfolio simulation — dollar-denominated walk-forward
# ---------------------------------------------------------------------------


def _add_minutes_iso(iso_str: str, minutes: int) -> str:
    """Add *minutes* to an ISO timestamp string, return ISO string."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return (dt + timedelta(minutes=minutes)).isoformat()


def _peak_concurrency(results: list[BacktestResult]) -> int:
    """Compute maximum number of simultaneously open positions."""
    events: list[tuple[str, int]] = []
    for r in results:
        events.append((r.entry_time, 1))
        if r.exit_time:
            events.append((r.exit_time, -1))
    if not events:
        return 1
    events.sort()
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return max(peak, 1)


def compute_portfolio_sim(
    results: list[BacktestResult],
    alloc_key: str,
    alloc_params: dict[str, Any],
    starting_amount: float,
    reinvest_delay_minutes: int = 1,
    progress_cb: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Dollar-denominated portfolio simulation on already-filtered results.

    Walks forward chronologically through trades that were "taken" by the
    allocation engine, tracking actual cash, position sizing, and reinvestment
    delay.  Returns starting/ending balance and return percentage.
    """
    # 1. Filter to taken trades only
    taken = [
        r for r in results
        if r.pnl_pct is not None
        and r.exit_reason not in ("skipped", "no_data", "unknown_strategy")
    ]
    if not taken:
        _safe_emit_progress(progress_cb, {
            "phase": "portfolio_sim",
            "label": "Simulating portfolio",
            "processed": 0,
            "total": 0,
            "phase_progress": 1.0,
            "chrono": True,
            "portfolio_value": round(starting_amount, 2),
        })
        return {
            "sim_starting": round(starting_amount, 2),
            "sim_ending": round(starting_amount, 2),
            "sim_return_pct": 0.0,
            "sim_trades": 0,
        }

    # 2. Sort chronologically
    taken.sort(key=lambda r: r.entry_time)

    # 3. Derive max concurrent positions
    if alloc_key == "max_positions":
        max_concurrent = int(alloc_params.get("max_pos", 10))
    elif alloc_key in ("fixed_dollar", "ranking_realloc"):
        alloc_pct = float(alloc_params.get("alloc_pct", 5))
        max_concurrent = max(1, int(100 / alloc_pct))
    else:  # "none" or unknown
        max_concurrent = _peak_concurrency(taken)

    # 4. Walk forward
    cash = starting_amount
    # sid -> {invested, exit_time, pnl_pct}
    active: dict[str, dict[str, Any]] = {}
    # (available_at_iso, amount)
    pending_cash: list[tuple[str, float]] = []
    sim_trades = 0
    MIN_POSITION = 1.0  # ignore dust
    timeline_start = taken[0].entry_time
    timeline_end = max((r.exit_time or r.entry_time) for r in taken)

    def _sim_equity() -> float:
        active_principal = sum(float(pos["invested"]) for pos in active.values())
        pending_total = sum(float(amount) for _, amount in pending_cash)
        return cash + active_principal + pending_total

    _safe_emit_progress(progress_cb, {
        "phase": "portfolio_sim",
        "label": "Simulating portfolio",
        "processed": 0,
        "total": len(taken),
        "phase_progress": 0.0,
        "chrono": True,
        "timeline_start": timeline_start,
        "timeline_end": timeline_end,
        "portfolio_value": round(starting_amount, 2),
        "sim_trades": 0,
        "sim_active_positions": 0,
    })

    for idx, r in enumerate(taken, start=1):
        cur_time = r.entry_time

        # 4a. Evict closed positions → pending cash
        closed_sids = [
            sid for sid, pos in active.items()
            if pos["exit_time"] is not None and pos["exit_time"] <= cur_time
        ]
        for sid in closed_sids:
            pos = active.pop(sid)
            proceeds = pos["invested"] * (1 + pos["pnl_pct"] / 100)
            if reinvest_delay_minutes > 0:
                avail_at = _add_minutes_iso(pos["exit_time"], reinvest_delay_minutes)
                pending_cash.append((avail_at, proceeds))
            else:
                cash += proceeds

        # 4b. Release matured pending cash
        still_pending: list[tuple[str, float]] = []
        for avail_at, amount in pending_cash:
            if avail_at <= cur_time:
                cash += amount
            else:
                still_pending.append((avail_at, amount))
        pending_cash = still_pending

        # 4c. Try to open position
        open_slots = max_concurrent - len(active)
        if open_slots > 0 and cash >= MIN_POSITION:
            position_size = cash / open_slots
            cash -= position_size
            active[r.snapshot_id] = {
                "invested": position_size,
                "exit_time": r.exit_time,
                "pnl_pct": r.pnl_pct,
            }
            sim_trades += 1

        _safe_emit_progress(progress_cb, {
            "phase": "portfolio_sim",
            "label": "Simulating portfolio",
            "processed": idx,
            "total": len(taken),
            "phase_progress": idx / len(taken),
            "chrono": True,
            "current_entry_time": cur_time,
            "current_symbol": r.symbol,
            "timeline_start": timeline_start,
            "timeline_end": timeline_end,
            "portfolio_value": round(_sim_equity(), 2),
            "sim_trades": sim_trades,
            "sim_active_positions": len(active),
        })

    # 5. Close remaining positions + collect pending cash
    for pos in active.values():
        cash += pos["invested"] * (1 + pos["pnl_pct"] / 100)
    for _, amount in pending_cash:
        cash += amount

    ending = cash
    return_pct = (ending - starting_amount) / starting_amount * 100 if starting_amount > 0 else 0.0

    _safe_emit_progress(progress_cb, {
        "phase": "portfolio_sim",
        "label": "Simulating portfolio",
        "processed": len(taken),
        "total": len(taken),
        "phase_progress": 1.0,
        "chrono": True,
        "current_entry_time": timeline_end,
        "timeline_start": timeline_start,
        "timeline_end": timeline_end,
        "portfolio_value": round(ending, 2),
        "sim_trades": sim_trades,
        "sim_active_positions": 0,
    })

    # Compute CAGR-based daily return over the trading-day span
    sim_daily_pct: float | None = None
    sim_span_days: float | None = None
    if sim_trades > 0 and ending > 0 and starting_amount > 0:
        first_entry = taken[0].entry_time
        # Last exit: latest exit_time among taken trades (or entry_time if no exit)
        last_exit = max(
            (r.exit_time or r.entry_time) for r in taken
            if r.pnl_pct is not None
        )
        try:
            dt_start = _parse_entry_time(first_entry)
            dt_end = _parse_entry_time(last_exit)
            # Business days between dates (Mon-Fri)
            bdays = int(np.busday_count(dt_start.date(), dt_end.date()))
            # Add fractional intraday component
            if dt_start.date() == dt_end.date():
                # Same day: fraction of a trading day (390 min = 1 day)
                minutes = max((dt_end - dt_start).total_seconds() / 60, 1)
                trading_days = minutes / _BARS_PER_DAY
            else:
                trading_days = max(bdays, 1)
            sim_span_days = round(trading_days, 2)
            ratio = ending / starting_amount
            if ratio > 0 and trading_days > 0:
                sim_daily_pct = round((ratio ** (1 / trading_days) - 1) * 100, 4)
        except (TypeError, ValueError, OverflowError):
            pass

    return {
        "sim_starting": round(starting_amount, 2),
        "sim_ending": round(ending, 2),
        "sim_return_pct": round(return_pct, 2),
        "sim_trades": sim_trades,
        "sim_daily_pct": sim_daily_pct,
        "sim_span_days": sim_span_days,
    }


# ---------------------------------------------------------------------------
# Persistent OHLCV cache (1-min bars, stored per symbol per date)
# ---------------------------------------------------------------------------

_CACHE_DIR = Path.home() / ".cache" / "alpaca-news" / "ohlcv_1m"


def _cache_path(symbol: str, date_str: str) -> Path:
    return _CACHE_DIR / symbol.upper() / f"{date_str}.json"


def _read_cache(symbol: str, date_str: str) -> list[dict] | None:
    """Read cached 1-min bars for a symbol on a given date."""
    path = _cache_path(symbol, date_str)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _write_cache(symbol: str, date_str: str, bars: list[dict]) -> None:
    """Write 1-min bars to disk cache."""
    path = _cache_path(symbol, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bars))


def _bars_to_df(bars: list[dict]) -> pd.DataFrame:
    """Convert list of bar dicts to a DataFrame with tz-naive Eastern DatetimeIndex.

    Schwab timestamps are UTC (e.g. 2026-02-25T14:30:00+00:00 = 9:30 AM ET).
    Cached timestamps are tz-naive Eastern (e.g. 2026-02-25T09:30:00).
    We normalize everything to tz-naive Eastern time.
    """
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df.rename(columns={"t": "Time", "o": "Open", "h": "High",
                        "l": "Low", "c": "Close", "v": "Volume"}, inplace=True)
    df["Time"] = pd.to_datetime(df["Time"])
    # Only convert if timestamps have timezone info (UTC from Schwab)
    if df["Time"].dt.tz is not None:
        try:
            from zoneinfo import ZoneInfo
            df["Time"] = df["Time"].dt.tz_convert(ZoneInfo("US/Eastern")).dt.tz_localize(None)
        except Exception:
            df["Time"] = df["Time"].dt.tz_localize(None)
    # else: already tz-naive Eastern (from cache or yfinance)
    df.set_index("Time", inplace=True)
    df.sort_index(inplace=True)
    return df


def _df_to_bars(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame back to list of bar dicts for caching."""
    bars = []
    for ts, row in df.iterrows():
        bars.append({
            "t": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "o": round(float(row["Open"]), 4),
            "h": round(float(row["High"]), 4),
            "l": round(float(row["Low"]), 4),
            "c": round(float(row["Close"]), 4),
            "v": int(row["Volume"]),
        })
    return bars


def _split_by_date(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split a multi-day DataFrame into per-date DataFrames."""
    groups: dict[str, pd.DataFrame] = {}
    for date_str, group in df.groupby(df.index.date):
        groups[str(date_str)] = group
    return groups


def _fetch_schwab_1m(symbol: str, period: int = 10) -> pd.DataFrame | None:
    """Fetch 1-min bars from Schwab for up to `period` trading days."""
    try:
        from trader.market.schwab_client import SchwabMarketClient
        client = SchwabMarketClient()
        if not client.available:
            return None
        candles = client.get_intraday_candles(
            symbol, period=period, frequency=1, extended_hours=True,
        )
        if not candles:
            return None
        bars = [{"t": c.t, "o": c.o, "h": c.h, "l": c.l, "c": c.c, "v": c.v}
                for c in candles]
        return _bars_to_df(bars)
    except Exception as e:
        log.debug("Schwab 1m fetch failed for %s: %s", symbol, e)
        return None


def _fetch_yfinance_1m(symbol: str, period: str = "7d") -> pd.DataFrame | None:
    """Fetch 1-min bars from yfinance (fallback), including extended hours."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol.upper())
        df = ticker.history(period=period, interval="1m", prepost=True)
        if df is None or df.empty:
            return None
        # yfinance returns tz-aware Eastern timestamps — convert to tz-naive Eastern
        if df.index.tz is not None:
            try:
                from zoneinfo import ZoneInfo
                df.index = df.index.tz_convert(ZoneInfo("US/Eastern")).tz_localize(None)
            except Exception:
                df.index = df.index.tz_localize(None)
        return df
    except Exception as e:
        log.debug("yfinance 1m fetch failed for %s: %s", symbol, e)
        return None


def _get_ohlcv_1m(symbol: str, start_date: str, end_date: str | None = None) -> pd.DataFrame | None:
    """Get 1-min OHLCV bars for a symbol from start_date to end_date (or today).

    Uses persistent disk cache. Fetches missing dates via Schwab → yfinance.
    Completed past trading days are cached forever.
    """
    symbol = symbol.upper()
    today_str = datetime.now().strftime("%Y-%m-%d")
    if end_date is None:
        end_date = today_str

    # Determine which dates we need
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    # Collect cached data and identify missing dates
    all_frames: list[pd.DataFrame] = []
    missing_dates: list[str] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # skip weekends
            ds = cur.strftime("%Y-%m-%d")
            cached = _read_cache(symbol, ds)
            if cached:
                all_frames.append(_bars_to_df(cached))
            else:
                missing_dates.append(ds)
        cur += timedelta(days=1)

    # Fetch missing data if any — merge Schwab + yfinance for completeness.
    # Schwab has more bar coverage but thinly-traded stocks may have gaps.
    # yfinance fills those gaps with different data providers.
    if missing_dates:
        schwab_df = _fetch_schwab_1m(symbol, period=10)
        yf_df = _fetch_yfinance_1m(symbol, period="7d")
        if schwab_df is not None and yf_df is not None:
            # Merge: Schwab takes priority, yfinance fills gaps
            fetched_df = pd.concat([schwab_df, yf_df])
            fetched_df = fetched_df[~fetched_df.index.duplicated(keep="first")]
            fetched_df.sort_index(inplace=True)
        else:
            fetched_df = schwab_df if schwab_df is not None else yf_df

        if fetched_df is not None and not fetched_df.empty:
            by_date = _split_by_date(fetched_df)
            for ds in missing_dates:
                if ds in by_date and not by_date[ds].empty:
                    day_df = by_date[ds]
                    # Only cache completed days (not today, which may still be in progress)
                    if ds < today_str:
                        _write_cache(symbol, ds, _df_to_bars(day_df))
                    all_frames.append(day_df)
                # Also cache any other fetched days we don't have yet
            for ds, day_df in by_date.items():
                if ds not in missing_dates and not _read_cache(symbol, ds):
                    if ds < today_str and not day_df.empty:
                        _write_cache(symbol, ds, _df_to_bars(day_df))

    if not all_frames:
        return None

    combined = pd.concat(all_frames)
    combined.sort_index(inplace=True)
    combined = combined[~combined.index.duplicated(keep="first")]
    return combined


# ---------------------------------------------------------------------------
# Indicator helpers (work on any bar frequency)
# ---------------------------------------------------------------------------


def _get_cached_indicator(
    indicator_cache: dict[tuple[Any, ...], Any] | None,
    key: tuple[Any, ...],
    compute_fn,
):
    """Get an indicator from per-symbol cache, computing it once if needed."""
    if indicator_cache is None:
        return compute_fn()
    if key in indicator_cache:
        _trace_inc("indicator_cache_hits")
        return indicator_cache[key]
    _trace_inc("indicator_cache_misses")
    value = compute_fn()
    indicator_cache[key] = value
    return value


def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    t0 = time.perf_counter()
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    out = tr.rolling(period).mean()
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_atr_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_atr_calls")
    return out


def _compute_sma(series: pd.Series, period: int) -> pd.Series:
    t0 = time.perf_counter()
    out = series.rolling(period).mean()
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_sma_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_sma_calls")
    return out


def _compute_ema(series: pd.Series, span: int) -> pd.Series:
    t0 = time.perf_counter()
    out = series.ewm(span=span, adjust=False).mean()
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_ema_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_ema_calls")
    return out


def _compute_rsi(close: pd.Series, period: int) -> pd.Series:
    t0 = time.perf_counter()
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    out = 100 - (100 / (1 + rs))
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_rsi_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_rsi_calls")
    return out


def _compute_macd(
    close: pd.Series, fast: int, slow: int, signal: int,
) -> tuple[pd.Series, pd.Series]:
    t0 = time.perf_counter()
    ema_fast = _compute_ema(close, fast)
    ema_slow = _compute_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _compute_ema(macd_line, signal)
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_macd_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_macd_calls")
    return macd_line, signal_line


def _compute_roc(close: pd.Series, period: int) -> pd.Series:
    t0 = time.perf_counter()
    out = close.pct_change(period)
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_roc_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_roc_calls")
    return out


def _compute_stochastic(
    df: pd.DataFrame, n: int, k_smooth: int, d_smooth: int,
) -> tuple[pd.Series, pd.Series]:
    t0 = time.perf_counter()
    low_n = df["Low"].rolling(n).min()
    high_n = df["High"].rolling(n).max()
    k_raw = (df["Close"] - low_n) / (high_n - low_n) * 100
    k_line = k_raw.rolling(k_smooth).mean()
    d_line = k_line.rolling(d_smooth).mean()
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_stochastic_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_stochastic_calls")
    return k_line, d_line


def _compute_adx(df: pd.DataFrame, period: int) -> pd.Series:
    t0 = time.perf_counter()
    high = df["High"]
    low = df["Low"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    plus_dm = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)
    # Zero out whichever is smaller
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    atr = _compute_atr(df, period)
    plus_di = _compute_ema(plus_dm, period) / atr * 100
    minus_di = _compute_ema(minus_dm, period) / atr * 100
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
    out = _compute_ema(dx, period)
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_adx_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_adx_calls")
    return out


def _compute_volume_delta(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Compute per-bar uptick/downtick volume using the inter-bar tick rule.

    Returns (uptick_vol, downtick_vol) Series aligned to df index.
    """
    t0 = time.perf_counter()
    close = df["Close"].to_numpy(dtype=float, copy=False)
    volume = df["Volume"].to_numpy(dtype=float, copy=False)
    n = len(close)
    direction = np.zeros(n, dtype=float)
    if n > 1:
        step = np.sign(np.diff(close))
        raw = np.empty(n, dtype=float)
        raw[0] = 0.0
        raw[1:] = step
        # Carry forward the previous non-zero sign when close is unchanged.
        prev_nonzero = np.where(raw != 0.0, np.arange(n), 0)
        np.maximum.accumulate(prev_nonzero, out=prev_nonzero)
        direction = raw[prev_nonzero]
    uptick = pd.Series(
        np.where(direction > 0, volume, 0), index=df.index, dtype=float,
    )
    downtick = pd.Series(
        np.where(direction < 0, volume, 0), index=df.index, dtype=float,
    )
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_volume_delta_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_volume_delta_calls")
    return uptick, downtick


def _compute_vdd_signal_indices(
    close: pd.Series, cum_delta: pd.Series, lookback: int,
) -> np.ndarray:
    """Return sorted indices where the VDD signal condition is true."""
    if lookback <= 0 or close.empty:
        return np.empty(0, dtype=np.int64)
    prev_roll_max = close.shift(1).rolling(lookback, min_periods=lookback).max()
    lag_cum_delta = cum_delta.shift(lookback)
    mask = ((close >= prev_roll_max) & (cum_delta < lag_cum_delta)).fillna(False)
    return np.flatnonzero(mask.to_numpy(dtype=bool, copy=False)).astype(np.int64)


# ---------------------------------------------------------------------------
# Ranking feature computation (shared by backtest and live paths)
# ---------------------------------------------------------------------------


def _compute_trailing_slope(closes: np.ndarray, lookback: int = 30) -> float:
    """Normalized slope of linear regression over last *lookback* values.

    Returns slope / mean(values) so the result is in "% per bar" units,
    making it comparable across stocks with different price levels.
    """
    vals = closes[-lookback:] if len(closes) >= lookback else closes
    if len(vals) < 2:
        return 0.0
    x = np.arange(len(vals), dtype=float)
    slope = np.polyfit(x, vals, 1)[0]
    mean = np.mean(vals)
    return float(slope / mean) if mean > 0 else 0.0


def _compute_ad_slope(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    lookback: int = 30,
) -> float:
    """Slope of cumulative Accumulation/Distribution line over last *lookback* bars.

    Uses the Money Flow Multiplier: MFM = ((C-L)-(H-C))/(H-L).
    Each bar's A/D contribution = MFM * volume.
    """
    hl_range = highs - lows
    hl_range = np.where(hl_range == 0, 1.0, hl_range)  # avoid div/0
    mfm = ((closes - lows) - (highs - closes)) / hl_range
    ad = mfm * volumes
    cum_ad = np.cumsum(ad)
    tail = cum_ad[-lookback:] if len(cum_ad) >= lookback else cum_ad
    if len(tail) < 2:
        return 0.0
    x = np.arange(len(tail), dtype=float)
    return float(np.polyfit(x, tail, 1)[0])


def compute_ranking_features(
    df_5m: pd.DataFrame,
    lookback: int = 30,
    rsi_period: int = 14,
) -> dict[str, float]:
    """Compute all forward-looking ranking features from a 5-min OHLCV DataFrame.

    Returns ``{slope, rsi, ad_slope}`` — used by both backtest and live ranking.
    The DataFrame must have columns: Open, High, Low, Close, Volume.
    """
    closes = df_5m["Close"].to_numpy(dtype=float, copy=False)
    highs = df_5m["High"].to_numpy(dtype=float, copy=False)
    lows = df_5m["Low"].to_numpy(dtype=float, copy=False)
    volumes = df_5m["Volume"].to_numpy(dtype=float, copy=False)

    slope = _compute_trailing_slope(closes, lookback)
    ad_slope = _compute_ad_slope(highs, lows, closes, volumes, lookback)

    # RSI: use the existing _compute_rsi on the close series, take last value
    rsi_series = _compute_rsi(df_5m["Close"], rsi_period)
    rsi_val = float(rsi_series.iloc[-1]) if not rsi_series.empty and not np.isnan(rsi_series.iloc[-1]) else 50.0

    return {"slope": slope, "rsi": rsi_val, "ad_slope": ad_slope}


def compute_ranking_features_from_tick(
    tick_bars: pd.DataFrame,
    lookback: int = 30,
    rsi_period: int = 14,
) -> dict[str, float]:
    """Compute ranking features from tick_collector bars (live only).

    *tick_bars* has columns: close, high, low, volume, est_uptick, est_downtick.
    For volume_trend, uses Lee-Ready classified delta instead of A/D formula.
    """
    closes = tick_bars["close"].to_numpy(dtype=float, copy=False)
    highs = tick_bars["high"].to_numpy(dtype=float, copy=False)
    lows = tick_bars["low"].to_numpy(dtype=float, copy=False)

    slope = _compute_trailing_slope(closes, lookback)

    # Volume trend from Lee-Ready classified volume (more accurate than A/D)
    est_up = tick_bars["est_uptick"].to_numpy(dtype=float, copy=False)
    est_down = tick_bars["est_downtick"].to_numpy(dtype=float, copy=False)
    cum_delta = np.cumsum(est_up - est_down)
    tail = cum_delta[-lookback:] if len(cum_delta) >= lookback else cum_delta
    if len(tail) < 2:
        ad_slope = 0.0
    else:
        x = np.arange(len(tail), dtype=float)
        ad_slope = float(np.polyfit(x, tail, 1)[0])

    # RSI from tick closes
    close_series = tick_bars["close"]
    rsi_series = _compute_rsi(close_series, rsi_period)
    rsi_val = float(rsi_series.iloc[-1]) if not rsi_series.empty and not np.isnan(rsi_series.iloc[-1]) else 50.0

    return {"slope": slope, "rsi": rsi_val, "ad_slope": ad_slope}


# ---------------------------------------------------------------------------
# Walk-forward strategy runners
# Each returns (exit_price, exit_timestamp_str, reason, bars_held).
# ---------------------------------------------------------------------------


def _fmt_ts(idx: pd.DatetimeIndex, i: int) -> str:
    """Format a DataFrame index timestamp as a string."""
    ts = idx[i]
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _get_bar(df: pd.DataFrame, i: int) -> pd.Series:
    """Read a single bar and record fine-grained strategy trace counters."""
    t0 = time.perf_counter()
    bar = df.iloc[i]
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("bar_read_sec", dt)
    _trace_inc("bar_read_calls")
    _trace_inc("bars_scanned")
    return bar


def _trace_vector_scan(bars: int, dt: float) -> None:
    if bars <= 0:
        return
    _trace_inc("vector_scan_calls")
    _trace_inc("vector_scan_bars", bars)
    _trace_add_sec("vector_scan_sec", max(0.0, dt))


def _first_true(mask: np.ndarray) -> int | None:
    hits = np.flatnonzero(mask)
    if hits.size == 0:
        return None
    return int(hits[0])


def _first_guard_hit(
    highs: np.ndarray,
    lows: np.ndarray,
    start: int,
    guard_stop: float | None,
    guard_target: float | None,
    limit_bars: int | None = None,
) -> tuple[int, float, str] | None:
    """Return (rel_idx, price, reason) for earliest guard hit from `start`."""
    if guard_stop is None and guard_target is None:
        return None
    if start >= len(lows):
        return None
    end = len(lows)
    if limit_bars is not None:
        end = min(end, start + max(0, limit_bars))
    if end <= start:
        return None
    low_slice = lows[start:end]
    high_slice = highs[start:end]
    t0 = time.perf_counter()
    _trace_inc("guard_checks", len(low_slice))

    best_idx: int | None = None
    best_price: float = 0.0
    best_reason = ""

    if guard_stop is not None:
        idx = _first_true(low_slice <= guard_stop)
        if idx is not None:
            best_idx = idx
            best_price = float(guard_stop)
            best_reason = "guard_stop"
    if guard_target is not None:
        idx = _first_true(high_slice >= guard_target)
        if idx is not None and (best_idx is None or idx < best_idx):
            best_idx = idx
            best_price = float(guard_target)
            best_reason = "guard_target"

    _trace_add_sec("guard_check_sec", max(0.0, time.perf_counter() - t0))
    if best_idx is None:
        return None
    _trace_inc("guard_hits")
    return best_idx, best_price, best_reason


def _first_trail_guard_hit(
    highs: np.ndarray,
    lows: np.ndarray,
    start: int,
    entry_price: float,
    trail_pct: float,
) -> tuple[int, float, str] | None:
    """Return (rel_idx, price, 'guard_trail') for first trailing-stop hit.

    Tracks the running max (high-water mark) from entry and triggers when
    the bar low falls to ``peak * (1 - trail_pct / 100)``.
    Fully vectorised via cumulative-max.
    """
    if start >= len(highs):
        return None
    high_slice = highs[start:]
    low_slice = lows[start:]
    # Cumulative max starting from entry_price
    cummax = np.maximum.accumulate(np.maximum(high_slice, entry_price))
    trail_stops = cummax * (1 - trail_pct / 100)
    idx = _first_true(low_slice <= trail_stops)
    if idx is None:
        return None
    return idx, float(trail_stops[idx]), "guard_trail"


def _check_guards(
    bar: pd.Series,
    guard_stop: float | None,
    guard_target: float | None,
) -> tuple[float, str] | None:
    """Return guard (price, reason) if either guard triggers on this bar."""
    if guard_stop is None and guard_target is None:
        return None
    t0 = time.perf_counter()
    _trace_inc("guard_checks")
    if guard_stop is not None and bar["Low"] <= guard_stop:
        _trace_inc("guard_hits")
        _trace_add_sec("guard_check_sec", max(0.0, time.perf_counter() - t0))
        return guard_stop, "guard_stop"
    if guard_target is not None and bar["High"] >= guard_target:
        _trace_inc("guard_hits")
        _trace_add_sec("guard_check_sec", max(0.0, time.perf_counter() - t0))
        return guard_target, "guard_target"
    _trace_add_sec("guard_check_sec", max(0.0, time.perf_counter() - t0))
    return None


def _run_fixed_stop_loss(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    stop = entry_price * (1 - params["stop_pct"] / 100)
    lows = df["Low"].to_numpy(dtype=float, copy=False)
    start = entry_idx
    if start >= len(lows):
        return None, None, "still_open", 0

    guard_rel = None
    guard_price = None
    guard_reason = None
    if guard_stop is not None or guard_target is not None:
        highs = df["High"].to_numpy(dtype=float, copy=False)
        guard_hit = _first_guard_hit(highs, lows, start, guard_stop, guard_target)
        if guard_hit is not None:
            guard_rel, guard_price, guard_reason = guard_hit

    t_vec = time.perf_counter()
    stop_rel = _first_true(lows[start:] <= stop)
    _trace_vector_scan(len(lows) - start, time.perf_counter() - t_vec)

    if guard_rel is not None and (stop_rel is None or guard_rel <= stop_rel):
        i = start + guard_rel
        bars_held = guard_rel + 1
        return guard_price, _fmt_ts(df.index, i), guard_reason, bars_held
    if stop_rel is not None:
        i = start + stop_rel
        bars_held = stop_rel + 1
        return stop, _fmt_ts(df.index, i), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_fixed_take_profit(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    target = entry_price * (1 + params["reward_pct"] / 100)
    highs = df["High"].to_numpy(dtype=float, copy=False)
    start = entry_idx
    if start >= len(highs):
        return None, None, "still_open", 0

    guard_rel = None
    guard_price = None
    guard_reason = None
    if guard_stop is not None or guard_target is not None:
        lows = df["Low"].to_numpy(dtype=float, copy=False)
        guard_hit = _first_guard_hit(highs, lows, start, guard_stop, guard_target)
        if guard_hit is not None:
            guard_rel, guard_price, guard_reason = guard_hit

    t_vec = time.perf_counter()
    target_rel = _first_true(highs[start:] >= target)
    _trace_vector_scan(len(highs) - start, time.perf_counter() - t_vec)

    if guard_rel is not None and (target_rel is None or guard_rel <= target_rel):
        i = start + guard_rel
        bars_held = guard_rel + 1
        return guard_price, _fmt_ts(df.index, i), guard_reason, bars_held
    if target_rel is not None:
        i = start + target_rel
        bars_held = target_rel + 1
        return target, _fmt_ts(df.index, i), "target", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_risk_reward_target(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    stop_pct = params["stop_pct"]
    k = params["risk_multiple"]
    stop = entry_price * (1 - stop_pct / 100)
    risk = entry_price - stop
    target = entry_price + k * risk
    highs = df["High"].to_numpy(dtype=float, copy=False)
    lows = df["Low"].to_numpy(dtype=float, copy=False)
    start = entry_idx
    if start >= len(highs):
        return None, None, "still_open", 0

    guard_rel = None
    guard_price = None
    guard_reason = None
    if guard_stop is not None or guard_target is not None:
        guard_hit = _first_guard_hit(highs, lows, start, guard_stop, guard_target)
        if guard_hit is not None:
            guard_rel, guard_price, guard_reason = guard_hit

    t_vec = time.perf_counter()
    stop_rel = _first_true(lows[start:] <= stop)
    target_rel = _first_true(highs[start:] >= target)
    _trace_vector_scan(len(highs) - start, time.perf_counter() - t_vec)

    strat_rel = None
    strat_price = None
    strat_reason = None
    if stop_rel is not None and (target_rel is None or stop_rel <= target_rel):
        strat_rel = stop_rel
        strat_price = stop
        strat_reason = "stop"
    elif target_rel is not None:
        strat_rel = target_rel
        strat_price = target
        strat_reason = "target"

    if guard_rel is not None and (strat_rel is None or guard_rel <= strat_rel):
        i = start + guard_rel
        bars_held = guard_rel + 1
        return guard_price, _fmt_ts(df.index, i), guard_reason, bars_held
    if strat_rel is not None:
        i = start + strat_rel
        bars_held = strat_rel + 1
        return strat_price, _fmt_ts(df.index, i), strat_reason, bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_pct_trailing_stop(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    trail_pct = params["trail_pct"]
    p_max = entry_price
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        guard_hit = _check_guards(bar, guard_stop, guard_target)
        if guard_hit is not None:
            price, reason = guard_hit
            return price, _fmt_ts(df.index, i), reason, bars_held
        if bar["High"] > p_max:
            p_max = bar["High"]
        trail_stop = p_max * (1 - trail_pct / 100)
        if bar["Low"] <= trail_stop:
            return trail_stop, _fmt_ts(df.index, i), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_atr_trailing_stop(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    period = int(params["atr_period"])
    k = params["multiplier"]
    atr = _get_cached_indicator(
        indicator_cache, ("atr", period), lambda: _compute_atr(df, period),
    )
    p_max = entry_price
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        if bar["High"] > p_max:
            p_max = bar["High"]
        atr_val = atr.iloc[i]
        if pd.isna(atr_val):
            continue
        trail_stop = p_max - k * atr_val
        if bar["Low"] <= trail_stop:
            return trail_stop, _fmt_ts(df.index, i), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_atr_fixed_stop(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    period = int(params["atr_period"])
    k = params["multiplier"]
    atr = _get_cached_indicator(
        indicator_cache, ("atr", period), lambda: _compute_atr(df, period),
    )
    atr_at_entry = atr.iloc[entry_idx] if entry_idx < len(atr) else None
    if atr_at_entry is None or pd.isna(atr_at_entry):
        for j in range(entry_idx, -1, -1):
            if not pd.isna(atr.iloc[j]):
                atr_at_entry = atr.iloc[j]
                break
    if atr_at_entry is None or pd.isna(atr_at_entry):
        return None, None, "no_data", 0
    stop = entry_price - k * atr_at_entry
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        if bar["Low"] <= stop:
            return stop, _fmt_ts(df.index, i), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_close_below_ma(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    period = int(params["ma_period"])
    sma = _get_cached_indicator(
        indicator_cache, ("sma", period), lambda: _compute_sma(df["Close"], period),
    )
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        ma_val = sma.iloc[i]
        if pd.isna(ma_val):
            continue
        if bar["Close"] < ma_val:
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_ma_cross_exit(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    short_p = int(params["short_period"])
    long_p = int(params["long_period"])
    sma_short = _get_cached_indicator(
        indicator_cache, ("sma", short_p), lambda: _compute_sma(df["Close"], short_p),
    )
    sma_long = _get_cached_indicator(
        indicator_cache, ("sma", long_p), lambda: _compute_sma(df["Close"], long_p),
    )
    prev_above = None
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        sv = sma_short.iloc[i]
        lv = sma_long.iloc[i]
        if pd.isna(sv) or pd.isna(lv):
            continue
        currently_above = sv >= lv
        if prev_above is not None and prev_above and not currently_above:
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
        prev_above = currently_above
    return None, None, "still_open", len(df) - entry_idx


def _run_rsi_overbought(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    period = int(params["rsi_period"])
    threshold = params["threshold"]
    rsi = _get_cached_indicator(
        indicator_cache, ("rsi", period), lambda: _compute_rsi(df["Close"], period),
    )
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        rsi_val = rsi.iloc[i]
        if pd.isna(rsi_val):
            continue
        if rsi_val >= threshold:
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_macd_bearish_cross(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    fast = int(params["fast_period"])
    slow = int(params["slow_period"])
    sig = int(params["signal_period"])
    macd_line, signal_line = _get_cached_indicator(
        indicator_cache,
        ("macd", fast, slow, sig),
        lambda: _compute_macd(df["Close"], fast, slow, sig),
    )
    prev_above = None
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        m = macd_line.iloc[i]
        s = signal_line.iloc[i]
        if pd.isna(m) or pd.isna(s):
            continue
        currently_above = m >= s
        if prev_above is not None and prev_above and not currently_above:
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
        prev_above = currently_above
    return None, None, "still_open", len(df) - entry_idx


def _run_volume_fade(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    lookback = int(params["vol_lookback"])
    alpha = params["multiplier"]
    avg_vol = _get_cached_indicator(
        indicator_cache,
        ("avg_vol", lookback),
        lambda: df["Volume"].rolling(lookback).mean(),
    )
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        av = avg_vol.iloc[i]
        if pd.isna(av):
            continue
        if bar["Volume"] < alpha * av:
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_roc_reversal(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    period = int(params["roc_period"])
    roc = _get_cached_indicator(
        indicator_cache, ("roc", period), lambda: _compute_roc(df["Close"], period),
    )
    prev_roc = None
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        r = roc.iloc[i]
        if pd.isna(r):
            continue
        if prev_roc is not None and prev_roc > 0 and r < 0:
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
        prev_roc = r
    return None, None, "still_open", len(df) - entry_idx


def _run_stochastic_overbought(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    n = int(params["stoch_period"])
    k_smooth = int(params["k_smooth"])
    d_smooth = int(params["d_smooth"])
    threshold = params["threshold"]
    k_line, d_line = _get_cached_indicator(
        indicator_cache,
        ("stochastic", n, k_smooth, d_smooth),
        lambda: _compute_stochastic(df, n, k_smooth, d_smooth),
    )
    prev_k_above = None
    prev_k = None
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        k = k_line.iloc[i]
        d = d_line.iloc[i]
        if pd.isna(k) or pd.isna(d):
            continue
        currently_above = k >= d
        if (prev_k_above is not None and prev_k_above and not currently_above
                and prev_k is not None and prev_k > threshold):
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
        prev_k_above = currently_above
        prev_k = k
    return None, None, "still_open", len(df) - entry_idx


def _run_adx_trend_decay(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    period = int(params["adx_period"])
    weak = params["weak_threshold"]
    strong = params["strong_threshold"]
    lookback = int(params["lookback"])
    adx = _get_cached_indicator(
        indicator_cache, ("adx", period), lambda: _compute_adx(df, period),
    )
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        a = adx.iloc[i]
        if pd.isna(a):
            continue
        if a < weak:
            # Check if ADX was recently strong
            start = max(0, i - lookback)
            recent = adx.iloc[start:i]
            if not recent.empty and recent.max() > strong:
                return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_volume_delta_divergence(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    # lookback_m (minutes) is canonical; fall back to legacy "lookback" (bar count = minutes for 1-min bars)
    lookback = int(params.get("lookback_m") or params["lookback"])
    uptick, downtick = _get_cached_indicator(
        indicator_cache, ("volume_delta",), lambda: _compute_volume_delta(df),
    )
    cum_delta = _get_cached_indicator(
        indicator_cache, ("cum_volume_delta",), lambda: (uptick - downtick).cumsum(),
    )
    signal_indices = _get_cached_indicator(
        indicator_cache,
        ("vdd_signal_indices", lookback),
        lambda: _compute_vdd_signal_indices(df["Close"], cum_delta, lookback),
    )
    close_values = _get_cached_indicator(
        indicator_cache, ("close_np",), lambda: df["Close"].to_numpy(dtype=float, copy=False),
    )
    guard_enabled = guard_stop is not None or guard_target is not None
    guard_idx = None
    guard_price = None
    guard_reason = None
    if guard_enabled:
        highs = df["High"].to_numpy(dtype=float, copy=False)
        lows = df["Low"].to_numpy(dtype=float, copy=False)
        guard_hit = _first_guard_hit(highs, lows, entry_idx, guard_stop, guard_target)
        if guard_hit is not None:
            rel, price, reason = guard_hit
            guard_idx = entry_idx + rel
            guard_price = price
            guard_reason = reason

    signal_idx = None
    start = max(0, entry_idx + lookback)
    if start < len(df):
        t_vec = time.perf_counter()
        pos = int(np.searchsorted(signal_indices, start))
        _trace_vector_scan(max(0, len(df) - start), time.perf_counter() - t_vec)
        if pos < len(signal_indices):
            signal_idx = int(signal_indices[pos])

    if guard_idx is not None and (signal_idx is None or guard_idx <= signal_idx):
        bars_held = guard_idx - entry_idx + 1
        return guard_price, _fmt_ts(df.index, guard_idx), guard_reason, bars_held
    if signal_idx is not None:
        bars_held = signal_idx - entry_idx + 1
        return float(close_values[signal_idx]), _fmt_ts(df.index, signal_idx), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_volume_imbalance_flip(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    window = int(params["window"])
    alpha = params["threshold"]
    uptick, downtick = _get_cached_indicator(
        indicator_cache, ("volume_delta",), lambda: _compute_volume_delta(df),
    )
    roll_up = uptick.rolling(window).sum()
    roll_dn = downtick.rolling(window).sum()
    roll_total = roll_up + roll_dn
    imbalance = (roll_up - roll_dn) / roll_total.replace(0, np.nan)
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        imb = imbalance.iloc[i]
        if pd.isna(imb):
            continue
        if imb < -alpha:
            # Was recently bullish?
            start = max(0, i - window)
            recent = imbalance.iloc[start:i]
            if not recent.empty and recent.max() > alpha:
                return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_max_holding_period(
    df, entry_idx, entry_price, params, guard_stop=None, guard_target=None, indicator_cache=None,
):
    max_bars = int(params["max_bars"])
    n = len(df) - entry_idx
    if n <= 0:
        return None, None, "still_open", 0

    # Fast path when guards are disabled.
    if guard_stop is None and guard_target is None:
        exit_idx = entry_idx + max_bars
        if exit_idx >= len(df):
            return None, None, "still_open", len(df) - entry_idx
        close = float(df.iloc[exit_idx]["Close"])
        return close, _fmt_ts(df.index, exit_idx), "time", max_bars

    highs = df["High"].to_numpy(dtype=float, copy=False)
    lows = df["Low"].to_numpy(dtype=float, copy=False)
    closes = df["Close"].to_numpy(dtype=float, copy=False)
    scan_len = min(n, max_bars + 1)
    guard_hit = _first_guard_hit(
        highs, lows, entry_idx, guard_stop, guard_target, limit_bars=scan_len,
    )
    _trace_vector_scan(scan_len, 0.0)
    if guard_hit is not None:
        rel, price, reason = guard_hit
        i = entry_idx + rel
        bars_held = rel + 1
        return price, _fmt_ts(df.index, i), reason, bars_held

    exit_idx = entry_idx + max_bars
    if exit_idx >= len(df):
        return None, None, "still_open", len(df) - entry_idx
    return float(closes[exit_idx]), _fmt_ts(df.index, exit_idx), "time", max_bars


_STRATEGY_RUNNERS = {
    "fixed_stop_loss": _run_fixed_stop_loss,
    "fixed_take_profit": _run_fixed_take_profit,
    "risk_reward_target": _run_risk_reward_target,
    "pct_trailing_stop": _run_pct_trailing_stop,
    "atr_trailing_stop": _run_atr_trailing_stop,
    "atr_fixed_stop": _run_atr_fixed_stop,
    "close_below_ma": _run_close_below_ma,
    "ma_cross_exit": _run_ma_cross_exit,
    "rsi_overbought": _run_rsi_overbought,
    "macd_bearish_cross": _run_macd_bearish_cross,
    "volume_fade": _run_volume_fade,
    "roc_reversal": _run_roc_reversal,
    "stochastic_overbought": _run_stochastic_overbought,
    "adx_trend_decay": _run_adx_trend_decay,
    "volume_delta_divergence": _run_volume_delta_divergence,
    "volume_imbalance_flip": _run_volume_imbalance_flip,
    "max_holding_period": _run_max_holding_period,
}


# ---------------------------------------------------------------------------
# Live exit evaluation API
# ---------------------------------------------------------------------------


@dataclass
class ExitResult:
    """Result of evaluating an exit strategy on live bar data."""

    should_exit: bool
    exit_price: float | None = None
    exit_time: str | None = None
    reason: str = "still_open"
    bars_held: int = 0


def evaluate_exit(
    strategy_key: str,
    params: dict[str, float],
    bars: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    guard_stop_pct: float = 0,
    guard_target_pct: float = 0,
    guard_trail_pct: float = 0,
    min_hold: int = 5,
    indicator_cache: dict[tuple[Any, ...], Any] | None = None,
) -> ExitResult:
    """Evaluate an exit strategy on bar data — for use by the live exit monitor.

    Same math as the backtest engine, but designed for incremental evaluation:
    call with the latest bars each cycle. If the strategy signals an exit,
    returns ExitResult with should_exit=True.

    Args:
        strategy_key: Key into STRATEGIES / _STRATEGY_RUNNERS.
        params: Strategy-specific parameter values.
        bars: 1-min OHLCV DataFrame (columns: Open, High, Low, Close, Volume).
              Must include bars from before entry through the present.
        entry_idx: Index position of the entry bar in the DataFrame.
        entry_price: Position entry price.
        guard_stop_pct: Guard stop-loss % (0 = disabled).
        guard_target_pct: Guard take-profit % (0 = disabled).
        guard_trail_pct: Trailing stop guard % (0 = disabled). Sells if price
                         drops this % from peak since entry.
        min_hold: Minimum bars before exit checks begin.
        indicator_cache: Optional dict for caching indicators across calls
                         for the same symbol. Reuse across cycles to avoid
                         recomputing indicators on unchanged bar history.

    Returns:
        ExitResult with should_exit=True if strategy signals exit.
    """
    runner = _STRATEGY_RUNNERS.get(strategy_key)
    if runner is None:
        return ExitResult(should_exit=False, reason="unknown_strategy")

    if indicator_cache is None:
        indicator_cache = {}

    # Apply min_hold offset (same as run_backtest)
    run_idx = min(entry_idx + max(0, min_hold), len(bars) - 1)

    guard_stop = (
        entry_price * (1 - guard_stop_pct / 100) if guard_stop_pct > 0 else None
    )
    guard_target = (
        entry_price * (1 + guard_target_pct / 100) if guard_target_pct > 0 else None
    )

    exit_price, exit_time, reason, bars_held = runner(
        bars, run_idx, entry_price, params,
        guard_stop=guard_stop, guard_target=guard_target,
        indicator_cache=indicator_cache,
    )

    # Adjust bars_held to include min_hold period
    bars_held = bars_held + (run_idx - entry_idx)

    # Check trailing stop guard independently (computed after min_hold)
    if guard_trail_pct > 0:
        highs = bars["High"].to_numpy(dtype=float, copy=False)
        lows = bars["Low"].to_numpy(dtype=float, copy=False)
        trail_hit = _first_trail_guard_hit(
            highs, lows, run_idx, entry_price, guard_trail_pct,
        )
        if trail_hit is not None:
            trail_rel, trail_price, trail_reason = trail_hit
            trail_abs = run_idx + trail_rel
            trail_bars = trail_rel + 1 + (run_idx - entry_idx)
            # If strategy also fired, pick whichever is earlier
            if exit_price is not None:
                # Convert strategy exit_time to bar index for comparison
                strat_abs = entry_idx + bars_held - 1
                if trail_abs <= strat_abs:
                    exit_price = trail_price
                    exit_time = _fmt_ts(bars.index, trail_abs)
                    reason = trail_reason
                    bars_held = trail_bars
            else:
                exit_price = trail_price
                exit_time = _fmt_ts(bars.index, trail_abs)
                reason = trail_reason
                bars_held = trail_bars

    if exit_price is not None:
        return ExitResult(
            should_exit=True,
            exit_price=float(exit_price),
            exit_time=exit_time,
            reason=reason,
            bars_held=bars_held,
        )

    return ExitResult(
        should_exit=False,
        reason=reason,
        bars_held=bars_held,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _parse_entry_time(entry_time_str: str) -> datetime:
    """Parse an entry_time string (ISO or date-only) to a tz-naive datetime."""
    s = entry_time_str.strip()
    # Try full ISO first
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            # Convert to Eastern (market time) if tz-aware
            if dt.tzinfo is not None:
                try:
                    from zoneinfo import ZoneInfo
                    dt = dt.astimezone(ZoneInfo("US/Eastern"))
                except Exception:
                    pass
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    # Last resort: just parse date
    return datetime.strptime(s[:10], "%Y-%m-%d")


def _filter_trading_hours(df: pd.DataFrame, market_close: str | None) -> pd.DataFrame:
    """Filter DataFrame to only include bars within trading hours.

    Args:
        market_close: Cutoff time as "HH:MM" in Eastern time (e.g. "16:00"),
                      or None to include all bars (extended hours).

    The DataFrame index is assumed to be tz-naive Eastern time.
    Regular market open is 09:30.
    """
    if not market_close:
        return df
    try:
        close_h, close_m = int(market_close.split(":")[0]), int(market_close.split(":")[1])
    except (ValueError, IndexError):
        return df

    hours = df.index.hour
    minutes = df.index.minute
    time_val = hours * 60 + minutes
    open_val = 9 * 60 + 30   # 09:30
    close_val = close_h * 60 + close_m
    mask = (time_val >= open_val) & (time_val < close_val)
    return df[mask]


def _extract_periodic_closes(
    df: pd.DataFrame,
    entry_idx: int,
    bars_held: int,
    resolution_minutes: int,
) -> list[tuple[str, float]]:
    """Extract close prices at regular intervals from the held bar slice.

    Returns list of (iso_timestamp, close_price) tuples.
    """
    end_idx = min(entry_idx + bars_held, len(df))
    if end_idx <= entry_idx:
        return []
    held = df.iloc[entry_idx:end_idx]
    if resolution_minutes <= 1:
        # Every bar
        resampled = held
    else:
        # Resample to the desired resolution, take last close per window
        rule = f"{resolution_minutes}min"
        resampled = held["Close"].resample(rule).last().dropna()
        return [(t.isoformat(), float(v)) for t, v in resampled.items()]
    return [(t.isoformat(), float(row["Close"])) for t, row in resampled.iterrows()]


def _extract_ranking_features(
    df: pd.DataFrame,
    entry_idx: int,
    bars_held: int,
    resolution_minutes: int,
    slope_lookback: int = 30,
    rsi_period: int = 14,
) -> list[tuple[str, dict[str, float]]]:
    """Pre-compute ranking features at periodic intervals during backtest.

    Resamples to 5-min bars (with warmup before entry for indicator init),
    then computes features at each periodic timestamp.
    """
    end_idx = min(entry_idx + bars_held, len(df))
    if end_idx <= entry_idx:
        return []

    # Warmup: include bars before entry so indicators aren't cold-starting
    warmup_1m = max(slope_lookback, rsi_period) * 5  # 5-min bars → need 5x 1-min bars
    warmup_start = max(0, entry_idx - warmup_1m)
    chunk = df.iloc[warmup_start:end_idx]

    # Resample to 5-min OHLCV
    ohlcv_5m = chunk.resample("5min").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])
    if len(ohlcv_5m) < 2:
        return []

    # Determine the periodic timestamps (same as periodic_closes)
    held = df.iloc[entry_idx:end_idx]
    if resolution_minutes <= 1:
        timestamps = [t.isoformat() for t in held.index]
    else:
        rule = f"{resolution_minutes}min"
        resampled_ts = held["Close"].resample(rule).last().dropna().index
        timestamps = [t.isoformat() for t in resampled_ts]

    if not timestamps:
        return []

    # For each periodic timestamp, compute features on the 5-min slice up to that point
    features: list[tuple[str, dict[str, float]]] = []
    for ts_iso in timestamps:
        # Select 5-min bars up to (and including) this timestamp
        slice_5m = ohlcv_5m.loc[:ts_iso]
        if len(slice_5m) < 2:
            features.append((ts_iso, {"slope": 0.0, "rsi": 50.0, "ad_slope": 0.0}))
            continue
        feat = compute_ranking_features(slice_5m, slope_lookback, rsi_period)
        features.append((ts_iso, feat))

    return features


def _compute_entry_features(
    df: pd.DataFrame,
    entry_idx: int,
    slope_lookback: int = 30,
    rsi_period: int = 14,
) -> dict[str, float]:
    """Compute ranking features at entry time for the new signal.

    Used to score the incoming signal symmetrically against existing positions.
    """
    # Warmup before entry
    warmup_1m = max(slope_lookback, rsi_period) * 5
    warmup_start = max(0, entry_idx - warmup_1m)
    chunk = df.iloc[warmup_start:entry_idx + 1]

    # Resample to 5-min
    ohlcv_5m = chunk.resample("5min").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])

    if len(ohlcv_5m) < 2:
        return {"slope": 0.0, "rsi": 50.0, "ad_slope": 0.0}

    return compute_ranking_features(ohlcv_5m, slope_lookback, rsi_period)


def run_backtest(
    strategy_key: str,
    params: dict[str, float],
    entries: list[dict[str, Any]],
    market_close: str | None = "16:00",
    min_hold: int = 5,
    guard_stop_pct: float = 0,
    guard_target_pct: float = 0,
    guard_trail_pct: float = 0,
    price_delay_minutes: int = 10,
    stats_resolution_minutes: int = 60,
    trace: dict[str, Any] | None = None,
    progress_cb: ProgressCallback | None = None,
) -> list[BacktestResult]:
    """Run an exit strategy backtest for a batch of entries using 1-min bars.

    Args:
        strategy_key: Key into STRATEGIES dict.
        params: User-provided parameter values.
        entries: List of dicts with keys: snapshot_id, symbol,
                 entry_price (float), entry_time (ISO timestamp string).
        market_close: Trading cutoff as "HH:MM" Eastern (e.g. "16:00",
                      "17:30", "20:00"), or None for extended hours.
        min_hold: Minimum bars to hold before exit checks begin (default 5).
        guard_stop_pct: Guard stop-loss % (0 = disabled).
        guard_target_pct: Guard take-profit % (0 = disabled).
        guard_trail_pct: Trailing stop guard % (0 = disabled).
        price_delay_minutes: Legacy compatibility argument; ignored.

    Returns:
        List of BacktestResult, one per entry.
    """
    total_start = time.perf_counter()
    stage_sec: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    symbol_sec: dict[str, float] = defaultdict(float)
    symbol_entries: dict[str, int] = defaultdict(int)
    total_entries = len(entries)
    processed_entries = 0
    prefetched_entries = 0
    timeline_start, timeline_end = _entry_time_bounds(entries)

    def _mark(stage: str, start: float) -> None:
        stage_sec[stage] += max(0.0, time.perf_counter() - start)

    def _emit_run_progress(
        *,
        label: str = "Evaluating exits by symbol",
        current_symbol: str | None = None,
        current_entry_time: str | None = None,
        processed_override: int | None = None,
        phase_progress: float | None = None,
    ) -> None:
        processed_value = processed_entries if processed_override is None else processed_override
        if phase_progress is None:
            phase_progress = (processed_value / total_entries) if total_entries > 0 else 1.0
        _safe_emit_progress(progress_cb, {
            "phase": "engine",
            "label": label,
            "processed": processed_value,
            "total": total_entries,
            "phase_progress": phase_progress,
            "chrono": False,
            "current_symbol": current_symbol,
            "current_entry_time": current_entry_time,
            "timeline_start": timeline_start,
            "timeline_end": timeline_end,
        })

    base_runner = _STRATEGY_RUNNERS.get(strategy_key)
    if base_runner is None:
        if trace is not None:
            total_sec = max(0.0, time.perf_counter() - total_start)
            trace.update({
                "stages_sec": {"total": round(total_sec, 6)},
                "stages_pct": {"total": 100.0},
                "counts": {
                    "entries_total": len(entries),
                    "symbols_total": len({e["symbol"].upper() for e in entries if "symbol" in e}),
                    "unknown_strategy": len(entries),
                },
                "top_symbols": [],
            })
        return [
            BacktestResult(
                snapshot_id=e["snapshot_id"],
                symbol=e["symbol"],
                entry_price=e["entry_price"],
                entry_time=e.get("entry_time", e.get("entry_date", "")),
                exit_reason="unknown_strategy",
            )
            for e in entries
        ]

    trace_token = None
    strategy_trace_local: dict[str, dict[str, float | int]] | None = None
    if trace is not None:
        strategy_trace_local = {"sec": {}, "counts": {}}
        trace_token = _STRATEGY_TRACE_CTX.set(strategy_trace_local)

    results: list[BacktestResult] = []

    # Group entries by symbol to share OHLCV fetches
    t_group = time.perf_counter()
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_symbol[e["symbol"].upper()].append(e)
    _mark("group_entries", t_group)
    counts["entries_total"] = len(entries)
    counts["symbols_total"] = len(by_symbol)
    _emit_run_progress()

    prefetched: list[dict[str, Any]] = []
    for symbol, sym_entries in by_symbol.items():
        symbol_entries[symbol] = len(sym_entries)
        t_parse = time.perf_counter()
        parsed_times = [
            (e, _parse_entry_time(e.get("entry_time", e.get("entry_date", ""))))
            for e in sym_entries
        ]
        _mark("parse_entry_times", t_parse)

        earliest_dt = min(t for _, t in parsed_times)
        start_date = (earliest_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        today_str = datetime.now().strftime("%Y-%m-%d")

        t_load = time.perf_counter()
        df_raw = _get_ohlcv_1m(symbol, start_date, today_str)
        _mark("ohlcv_load", t_load)

        df = None
        if df_raw is not None and not df_raw.empty:
            t_hours = time.perf_counter()
            df = _filter_trading_hours(df_raw, market_close)
            _mark("trading_hours_filter", t_hours)

        prefetched.append({
            "symbol": symbol,
            "sym_entries": sym_entries,
            "parsed_times": parsed_times,
            "df": df,
        })
        prefetched_entries += len(sym_entries)
        prefetch_progress = (prefetched_entries / total_entries) if total_entries > 0 else 1.0
        _emit_run_progress(
            label="Loading bars by symbol",
            current_symbol=symbol,
            current_entry_time=sym_entries[-1].get("entry_time", sym_entries[-1].get("entry_date", "")) or None,
            processed_override=prefetched_entries,
            phase_progress=min(prefetch_progress * 0.2, 0.2),
        )

    def _consume_symbol_output(output: dict[str, Any]) -> None:
        nonlocal processed_entries
        _merge_number_dict(stage_sec, output.get("stage_sec", {}))
        _merge_number_dict(counts, output.get("counts", {}))
        symbol = str(output.get("symbol") or "")
        symbol_sec[symbol] += float(output.get("symbol_sec", 0.0) or 0.0)
        processed_entries += len(output.get("results", []))
        if strategy_trace_local is not None:
            worker_trace = output.get("strategy_trace") or {}
            _merge_number_dict(strategy_trace_local.setdefault("sec", {}), worker_trace.get("sec", {}))
            _merge_number_dict(strategy_trace_local.setdefault("counts", {}), worker_trace.get("counts", {}))
        _emit_run_progress(
            label="Evaluating exits by symbol",
            current_symbol=symbol or None,
            current_entry_time=output.get("last_entry_time") or None,
            processed_override=processed_entries,
            phase_progress=(0.2 + ((processed_entries / total_entries) * 0.8)) if total_entries > 0 else 1.0,
        )

    worker_count = _resolve_backtest_workers(len(prefetched), total_entries)
    outputs: list[dict[str, Any] | None] = [None] * len(prefetched)

    def _run_symbol_jobs_serial() -> None:
        for idx, job in enumerate(prefetched):
            output = _evaluate_symbol_entries(
                symbol=job["symbol"],
                sym_entries=job["sym_entries"],
                parsed_times=job["parsed_times"],
                df=job["df"],
                strategy_key=strategy_key,
                params=params,
                market_close=market_close,
                min_hold=min_hold,
                guard_stop_pct=guard_stop_pct,
                guard_target_pct=guard_target_pct,
                guard_trail_pct=guard_trail_pct,
                stats_resolution_minutes=stats_resolution_minutes,
                capture_trace=trace is not None,
            )
            outputs[idx] = output
            _consume_symbol_output(output)

    if worker_count <= 1:
        _run_symbol_jobs_serial()
    else:
        _safe_emit_progress(progress_cb, {
            "phase": "engine",
            "label": f"Evaluating exits by symbol ({worker_count} workers)",
            "processed": prefetched_entries,
            "total": total_entries,
            "phase_progress": 0.2 if total_entries > 0 else 1.0,
            "chrono": False,
            "timeline_start": timeline_start,
            "timeline_end": timeline_end,
        })
        try:
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=worker_count,
                mp_context=mp.get_context("spawn"),
            ) as executor:
                future_to_idx = {
                    executor.submit(
                        _evaluate_symbol_entries,
                        symbol=job["symbol"],
                        sym_entries=job["sym_entries"],
                        parsed_times=job["parsed_times"],
                        df=job["df"],
                        strategy_key=strategy_key,
                        params=params,
                        market_close=market_close,
                        min_hold=min_hold,
                        guard_stop_pct=guard_stop_pct,
                        guard_target_pct=guard_target_pct,
                        guard_trail_pct=guard_trail_pct,
                        stats_resolution_minutes=stats_resolution_minutes,
                        capture_trace=trace is not None,
                    ): idx
                    for idx, job in enumerate(prefetched)
                }
                for future in concurrent.futures.as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    output = future.result()
                    outputs[idx] = output
                    _consume_symbol_output(output)
        except (OSError, PermissionError):
            counts["parallel_fallbacks"] += 1
            _safe_emit_progress(progress_cb, {
                "phase": "engine",
                "label": "Evaluating exits by symbol",
                "processed": processed_entries,
                "total": total_entries,
                "phase_progress": (0.2 + ((processed_entries / total_entries) * 0.8)) if total_entries > 0 else 1.0,
                "chrono": False,
                "timeline_start": timeline_start,
                "timeline_end": timeline_end,
                "note": "Parallel symbol workers unavailable in this environment; using serial evaluation.",
            })
            _run_symbol_jobs_serial()

    for output in outputs:
        if output is not None:
            results.extend(output.get("results", []))

    total_sec = max(0.0, time.perf_counter() - total_start)
    if trace is not None:
        stage_sec["total"] = total_sec
        stage_pct = {
            k: (v / total_sec * 100.0) if total_sec > 0 else 0.0
            for k, v in stage_sec.items()
        }
        strategy_eval_sec = stage_sec.get("strategy_eval", 0.0)
        strategy_detail: dict[str, Any] = {
            "sec": {},
            "pct_of_strategy_eval": {},
            "breakdown_sec": {},
            "breakdown_pct_of_strategy_eval": {},
            "indicator_breakdown_sec": {},
            "indicator_breakdown_pct_of_indicator_total": {},
            "counts": {},
        }
        if strategy_trace_local is not None:
            sec_raw = strategy_trace_local.get("sec", {})
            counts_raw = strategy_trace_local.get("counts", {})
            sec = {k: float(v) for k, v in sec_raw.items()}
            counts_detail = {k: int(v) for k, v in counts_raw.items()}
            known_parts = (
                sec.get("bar_read_sec", 0.0)
                + sec.get("guard_check_sec", 0.0)
                + sec.get("indicator_total_sec", 0.0)
                + sec.get("vector_scan_sec", 0.0)
            )
            sec["strategy_logic_sec"] = max(0.0, strategy_eval_sec - known_parts)
            pct_detail = {
                k: (v / strategy_eval_sec * 100.0) if strategy_eval_sec > 0 else 0.0
                for k, v in sec.items()
            }
            bar_calls = max(counts_detail.get("bar_read_calls", 0), 1)
            bars_scanned = counts_detail.get("bars_scanned", 0)
            vector_scan_bars = counts_detail.get("vector_scan_bars", 0)
            total_scanned = bars_scanned + vector_scan_bars
            counts_detail["bars_scanned_total"] = total_scanned
            counts_detail["strategy_eval_ms_per_bar"] = round(
                (strategy_eval_sec * 1000.0 / total_scanned), 6,
            ) if total_scanned > 0 else None
            counts_detail["bar_read_us_per_call"] = round(
                (sec.get("bar_read_sec", 0.0) * 1_000_000.0 / bar_calls), 6,
            )
            breakdown_sec = {
                "bar_read_sec": sec.get("bar_read_sec", 0.0),
                "guard_check_sec": sec.get("guard_check_sec", 0.0),
                "indicator_total_sec": sec.get("indicator_total_sec", 0.0),
                "vector_scan_sec": sec.get("vector_scan_sec", 0.0),
                "strategy_logic_sec": sec.get("strategy_logic_sec", 0.0),
            }
            breakdown_pct = {
                k: (v / strategy_eval_sec * 100.0) if strategy_eval_sec > 0 else 0.0
                for k, v in breakdown_sec.items()
            }
            indicator_total = sec.get("indicator_total_sec", 0.0)
            indicator_sec = {
                k: v for k, v in sec.items()
                if k.startswith("indicator_") and k != "indicator_total_sec"
            }
            indicator_pct = {
                k: (v / indicator_total * 100.0) if indicator_total > 0 else 0.0
                for k, v in indicator_sec.items()
            }
            strategy_detail = {
                "sec": {k: round(v, 6) for k, v in sorted(sec.items())},
                "pct_of_strategy_eval": {k: round(v, 2) for k, v in sorted(pct_detail.items())},
                "breakdown_sec": {k: round(v, 6) for k, v in sorted(breakdown_sec.items())},
                "breakdown_pct_of_strategy_eval": {
                    k: round(v, 2) for k, v in sorted(breakdown_pct.items())
                },
                "indicator_breakdown_sec": {
                    k: round(v, 6) for k, v in sorted(indicator_sec.items())
                },
                "indicator_breakdown_pct_of_indicator_total": {
                    k: round(v, 2) for k, v in sorted(indicator_pct.items())
                },
                "counts": counts_detail,
            }
        top_symbols = sorted(symbol_sec.items(), key=lambda kv: kv[1], reverse=True)[:10]
        trace.update({
            "stages_sec": {k: round(v, 6) for k, v in sorted(stage_sec.items())},
            "stages_pct": {k: round(v, 2) for k, v in sorted(stage_pct.items())},
            "counts": {k: int(v) for k, v in sorted(counts.items())},
            "strategy_eval_detail": strategy_detail,
            "top_symbols": [
                {
                    "symbol": sym,
                    "sec": round(sec, 6),
                    "pct_total": round((sec / total_sec * 100.0) if total_sec > 0 else 0.0, 2),
                    "entries": int(symbol_entries.get(sym, 0)),
                }
                for sym, sec in top_symbols
            ],
        })
    if trace_token is not None:
        _STRATEGY_TRACE_CTX.reset(trace_token)
    return results


def _params_to_json(params: dict[str, ParamDef]) -> dict[str, Any]:
    """Serialize a params dict for the frontend."""
    out: dict[str, Any] = {}
    for pk, pv in params.items():
        d: dict[str, Any] = {
            "type": pv.type,
            "default": pv.default,
            "label": pv.label,
        }
        if pv.type == "select":
            d["options"] = pv.options or []
        else:
            d["min"] = pv.min
            d["max"] = pv.max
            d["step"] = pv.step
        out[pk] = d
    return out


def _defs_to_json(defs: dict[str, StrategyDef]) -> dict[str, Any]:
    """Serialize a strategy/allocation registry for the frontend."""
    return {
        key: {
            "name": s.name,
            "key": s.key,
            "section": s.section,
            "description": s.description,
            "params": _params_to_json(s.params),
        }
        for key, s in defs.items()
    }


def strategies_json() -> dict[str, Any]:
    """Return strategy definitions as a JSON-serializable dict."""
    return _defs_to_json(STRATEGIES)


def allocations_json() -> dict[str, Any]:
    """Return allocation definitions as a JSON-serializable dict."""
    return _defs_to_json(ALLOCATIONS)
