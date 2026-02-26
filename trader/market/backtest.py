"""Exit strategy backtesting engine.

Fetches daily OHLCV from yfinance, walks forward bar-by-bar applying an
exit strategy, and returns per-snapshot results (exit price, date, P&L).

Usage::

    from trader.market.backtest import run_backtest, STRATEGIES

    results = run_backtest(
        strategy_key="fixed_stop_loss",
        params={"stop_pct": 5.0},
        entries=[{"snapshot_id": "abc", "symbol": "AAPL",
                  "entry_price": 150.0, "entry_date": "2026-01-15"}],
    )
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamDef:
    type: str  # "float" or "int"
    default: float
    label: str
    min: float | None = None
    max: float | None = None
    step: float | None = None


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
        description="Exit if the close falls below the n-period SMA.",
        params={
            "ma_period": ParamDef("int", 20, "MA period", 5, 200, 1),
        },
    ),
    "ma_cross_exit": StrategyDef(
        name="Moving Average Cross",
        key="ma_cross_exit",
        section="Trend",
        description="Exit if the short SMA crosses below the long SMA.",
        params={
            "short_period": ParamDef("int", 10, "Short MA", 3, 50, 1),
            "long_period": ParamDef("int", 50, "Long MA", 10, 200, 1),
        },
    ),
    "rsi_overbought": StrategyDef(
        name="RSI Overbought Exit",
        key="rsi_overbought",
        section="Momentum",
        description="Exit when RSI rises above the overbought threshold.",
        params={
            "rsi_period": ParamDef("int", 14, "RSI period", 5, 30, 1),
            "threshold": ParamDef("float", 70.0, "Threshold", 50, 90, 1),
        },
    ),
    "macd_bearish_cross": StrategyDef(
        name="MACD Bearish Cross",
        key="macd_bearish_cross",
        section="Momentum",
        description="Exit when MACD crosses below its signal line.",
        params={
            "fast_period": ParamDef("int", 12, "Fast EMA", 5, 30, 1),
            "slow_period": ParamDef("int", 26, "Slow EMA", 10, 50, 1),
            "signal_period": ParamDef("int", 9, "Signal EMA", 3, 20, 1),
        },
    ),
    "volume_fade": StrategyDef(
        name="Volume Fade Exit",
        key="volume_fade",
        section="Volume",
        description="Exit if daily volume drops below \u03b1 \u00d7 average volume.",
        params={
            "vol_lookback": ParamDef("int", 20, "Lookback", 5, 60, 1),
            "multiplier": ParamDef("float", 0.5, "Multiplier (\u03b1)", 0.1, 1.0, 0.05),
        },
    ),
    "max_holding_period": StrategyDef(
        name="Max Holding Period",
        key="max_holding_period",
        section="Time",
        description="Exit after a fixed number of trading days.",
        params={
            "max_days": ParamDef("int", 10, "Max days", 1, 120, 1),
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
    entry_date: str
    exit_price: float | None = None
    exit_date: str | None = None
    pnl_pct: float | None = None
    exit_reason: str = "no_data"  # stop, target, signal, time, still_open, no_data
    bars_held: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# OHLCV cache (module-level, 1-hour TTL)
# ---------------------------------------------------------------------------

_ohlcv_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 3600  # seconds


def _get_ohlcv(symbol: str, start_date: str) -> pd.DataFrame | None:
    """Fetch daily OHLCV from yfinance with caching.

    Returns DataFrame with columns: Open, High, Low, Close, Volume.
    Index is tz-naive DatetimeIndex.  Returns None on failure.
    """
    cache_key = f"{symbol}|{start_date}"
    now = time.time()
    cached = _ohlcv_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        ticker = yf.Ticker(symbol.upper())
        df = ticker.history(start=start_date, interval="1d")
        if df is None or df.empty:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        _ohlcv_cache[cache_key] = (now, df)
        return df
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Indicator helpers (compute full series, let strategies index into them)
# ---------------------------------------------------------------------------


def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range."""
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _compute_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def _compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _compute_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _compute_macd(
    close: pd.Series, fast: int, slow: int, signal: int,
) -> tuple[pd.Series, pd.Series]:
    ema_fast = _compute_ema(close, fast)
    ema_slow = _compute_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _compute_ema(macd_line, signal)
    return macd_line, signal_line


# ---------------------------------------------------------------------------
# Walk-forward strategy runners
# ---------------------------------------------------------------------------


def _run_fixed_stop_loss(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    """Returns (exit_price, exit_date, reason, bars_held)."""
    stop = entry_price * (1 - params["stop_pct"] / 100)
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        if bar["Low"] <= stop:
            return stop, str(df.index[i].date()), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_fixed_take_profit(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    target = entry_price * (1 + params["reward_pct"] / 100)
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        if bar["High"] >= target:
            return target, str(df.index[i].date()), "target", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_risk_reward_target(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    stop_pct = params["stop_pct"]
    k = params["risk_multiple"]
    stop = entry_price * (1 - stop_pct / 100)
    risk = entry_price - stop
    target = entry_price + k * risk
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        # Check stop first (conservative: assumes stop triggered before target on same bar)
        if bar["Low"] <= stop:
            return stop, str(df.index[i].date()), "stop", bars_held
        if bar["High"] >= target:
            return target, str(df.index[i].date()), "target", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_pct_trailing_stop(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    trail_pct = params["trail_pct"]
    p_max = entry_price
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        if bar["High"] > p_max:
            p_max = bar["High"]
        trail_stop = p_max * (1 - trail_pct / 100)
        if bar["Low"] <= trail_stop:
            return trail_stop, str(df.index[i].date()), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_atr_trailing_stop(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    period = int(params["atr_period"])
    k = params["multiplier"]
    atr = _compute_atr(df, period)
    p_max = entry_price
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        if bar["High"] > p_max:
            p_max = bar["High"]
        atr_val = atr.iloc[i]
        if pd.isna(atr_val):
            continue
        trail_stop = p_max - k * atr_val
        if bar["Low"] <= trail_stop:
            return trail_stop, str(df.index[i].date()), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_atr_fixed_stop(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    period = int(params["atr_period"])
    k = params["multiplier"]
    atr = _compute_atr(df, period)
    atr_at_entry = atr.iloc[entry_idx] if entry_idx < len(atr) else None
    if atr_at_entry is None or pd.isna(atr_at_entry):
        # Fall back to nearest valid ATR value before entry
        for j in range(entry_idx, -1, -1):
            if not pd.isna(atr.iloc[j]):
                atr_at_entry = atr.iloc[j]
                break
    if atr_at_entry is None or pd.isna(atr_at_entry):
        return None, None, "no_data", 0
    stop = entry_price - k * atr_at_entry
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        if bar["Low"] <= stop:
            return stop, str(df.index[i].date()), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_close_below_ma(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    period = int(params["ma_period"])
    sma = _compute_sma(df["Close"], period)
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        ma_val = sma.iloc[i]
        if pd.isna(ma_val):
            continue
        if bar["Close"] < ma_val:
            return bar["Close"], str(df.index[i].date()), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_ma_cross_exit(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    short_p = int(params["short_period"])
    long_p = int(params["long_period"])
    sma_short = _compute_sma(df["Close"], short_p)
    sma_long = _compute_sma(df["Close"], long_p)
    prev_above = None
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        sv = sma_short.iloc[i]
        lv = sma_long.iloc[i]
        if pd.isna(sv) or pd.isna(lv):
            continue
        currently_above = sv >= lv
        if prev_above is not None and prev_above and not currently_above:
            return bar["Close"], str(df.index[i].date()), "signal", bars_held
        prev_above = currently_above
    return None, None, "still_open", len(df) - entry_idx


def _run_rsi_overbought(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    period = int(params["rsi_period"])
    threshold = params["threshold"]
    rsi = _compute_rsi(df["Close"], period)
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        rsi_val = rsi.iloc[i]
        if pd.isna(rsi_val):
            continue
        if rsi_val >= threshold:
            return bar["Close"], str(df.index[i].date()), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_macd_bearish_cross(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    fast = int(params["fast_period"])
    slow = int(params["slow_period"])
    sig = int(params["signal_period"])
    macd_line, signal_line = _compute_macd(df["Close"], fast, slow, sig)
    prev_above = None
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        m = macd_line.iloc[i]
        s = signal_line.iloc[i]
        if pd.isna(m) or pd.isna(s):
            continue
        currently_above = m >= s
        if prev_above is not None and prev_above and not currently_above:
            return bar["Close"], str(df.index[i].date()), "signal", bars_held
        prev_above = currently_above
    return None, None, "still_open", len(df) - entry_idx


def _run_volume_fade(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    lookback = int(params["vol_lookback"])
    alpha = params["multiplier"]
    avg_vol = df["Volume"].rolling(lookback).mean()
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        av = avg_vol.iloc[i]
        if pd.isna(av):
            continue
        if bar["Volume"] < alpha * av:
            return bar["Close"], str(df.index[i].date()), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_max_holding_period(
    df: pd.DataFrame, entry_idx: int, entry_price: float, params: dict,
) -> tuple[float | None, str | None, str, int]:
    max_days = int(params["max_days"])
    exit_idx = entry_idx + max_days
    if exit_idx >= len(df):
        return None, None, "still_open", len(df) - entry_idx
    bar = df.iloc[exit_idx]
    return bar["Close"], str(df.index[exit_idx].date()), "time", max_days


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
    "max_holding_period": _run_max_holding_period,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _lookback_days_for_strategy(strategy_key: str, params: dict) -> int:
    """How many calendar days before entry we need for indicator warm-up."""
    if strategy_key in ("atr_trailing_stop", "atr_fixed_stop"):
        return int(params.get("atr_period", 14)) * 2 + 10
    if strategy_key == "close_below_ma":
        return int(params.get("ma_period", 20)) * 2 + 10
    if strategy_key == "ma_cross_exit":
        return int(params.get("long_period", 50)) * 2 + 10
    if strategy_key == "rsi_overbought":
        return int(params.get("rsi_period", 14)) * 2 + 10
    if strategy_key == "macd_bearish_cross":
        return int(params.get("slow_period", 26)) * 2 + 10
    if strategy_key == "volume_fade":
        return int(params.get("vol_lookback", 20)) * 2 + 10
    return 10  # minimal lookback for price-only strategies


def run_backtest(
    strategy_key: str,
    params: dict[str, float],
    entries: list[dict[str, Any]],
) -> list[BacktestResult]:
    """Run an exit strategy backtest for a batch of entries.

    Args:
        strategy_key: Key into STRATEGIES dict.
        params: User-provided parameter values.
        entries: List of dicts with keys: snapshot_id, symbol,
                 entry_price (float), entry_date (YYYY-MM-DD string).

    Returns:
        List of BacktestResult, one per entry.
    """
    runner = _STRATEGY_RUNNERS.get(strategy_key)
    if runner is None:
        return [
            BacktestResult(
                snapshot_id=e["snapshot_id"],
                symbol=e["symbol"],
                entry_price=e["entry_price"],
                entry_date=e["entry_date"],
                exit_reason="unknown_strategy",
            )
            for e in entries
        ]

    lookback = _lookback_days_for_strategy(strategy_key, params)
    results: list[BacktestResult] = []

    # Group entries by symbol to share OHLCV fetches
    from collections import defaultdict

    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_symbol[e["symbol"].upper()].append(e)

    for symbol, sym_entries in by_symbol.items():
        # Find earliest entry date for this symbol
        earliest = min(e["entry_date"] for e in sym_entries)
        start = (
            datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=lookback)
        ).strftime("%Y-%m-%d")

        df = _get_ohlcv(symbol, start)
        if df is None or df.empty:
            for e in sym_entries:
                results.append(BacktestResult(
                    snapshot_id=e["snapshot_id"],
                    symbol=symbol,
                    entry_price=e["entry_price"],
                    entry_date=e["entry_date"],
                    exit_reason="no_data",
                ))
            continue

        for e in sym_entries:
            entry_price = e["entry_price"]
            entry_date = e["entry_date"]

            # Find the first bar on or after entry_date
            entry_dt = pd.Timestamp(entry_date)
            entry_idx = df.index.searchsorted(entry_dt)
            if entry_idx >= len(df):
                results.append(BacktestResult(
                    snapshot_id=e["snapshot_id"],
                    symbol=symbol,
                    entry_price=entry_price,
                    entry_date=entry_date,
                    exit_reason="no_data",
                ))
                continue

            exit_price, exit_date, reason, bars_held = runner(
                df, entry_idx, entry_price, params,
            )

            # Ensure native Python types (not numpy)
            if exit_price is not None:
                exit_price = float(exit_price)
                pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
            elif reason == "still_open":
                exit_price = float(df.iloc[-1]["Close"])
                pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
                exit_date = str(df.index[-1].date())
            else:
                pnl_pct = None
            bars_held = int(bars_held)

            results.append(BacktestResult(
                snapshot_id=e["snapshot_id"],
                symbol=symbol,
                entry_price=entry_price,
                entry_date=entry_date,
                exit_price=round(exit_price, 2) if exit_price is not None else None,
                exit_date=exit_date,
                pnl_pct=pnl_pct,
                exit_reason=reason,
                bars_held=bars_held,
            ))

    return results


def strategies_json() -> dict[str, Any]:
    """Return strategy definitions as a JSON-serializable dict."""
    out: dict[str, Any] = {}
    for key, s in STRATEGIES.items():
        out[key] = {
            "name": s.name,
            "key": s.key,
            "section": s.section,
            "description": s.description,
            "params": {
                pk: {
                    "type": pv.type,
                    "default": pv.default,
                    "label": pv.label,
                    "min": pv.min,
                    "max": pv.max,
                    "step": pv.step,
                }
                for pk, pv in s.params.items()
            },
        }
    return out
