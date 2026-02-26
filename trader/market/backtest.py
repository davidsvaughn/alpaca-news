"""Exit strategy backtesting engine (1-minute bar resolution).

Fetches 1-min OHLCV bars via Schwab (primary) or yfinance (fallback),
caches them persistently on disk per (symbol, date), and walks forward
bar-by-bar applying an exit strategy.

Cache persists forever — data collected within the 10-day Schwab window
remains available for backtesting months later.

Usage::

    from trader.market.backtest import run_backtest, STRATEGIES

    results = run_backtest(
        strategy_key="fixed_stop_loss",
        params={"stop_pct": 5.0},
        entries=[{"snapshot_id": "abc", "symbol": "AAPL",
                  "entry_price": 150.0,
                  "entry_time": "2026-01-15T15:40:00+00:00"}],
    )
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    """Convert list of bar dicts to a DataFrame with DatetimeIndex."""
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df.rename(columns={"t": "Time", "o": "Open", "h": "High",
                        "l": "Low", "c": "Close", "v": "Volume"}, inplace=True)
    df["Time"] = pd.to_datetime(df["Time"])
    df.set_index("Time", inplace=True)
    df.sort_index(inplace=True)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
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
    """Fetch 1-min bars from yfinance (fallback)."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol.upper())
        df = ticker.history(period=period, interval="1m")
        if df is None or df.empty:
            return None
        if df.index.tz is not None:
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

    # Fetch missing data if any
    if missing_dates:
        fetched_df = _fetch_schwab_1m(symbol, period=10)
        if fetched_df is None or fetched_df.empty:
            fetched_df = _fetch_yfinance_1m(symbol, period="7d")

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


def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
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
# Each returns (exit_price, exit_timestamp_str, reason, bars_held).
# ---------------------------------------------------------------------------


def _fmt_ts(idx: pd.DatetimeIndex, i: int) -> str:
    """Format a DataFrame index timestamp as a string."""
    ts = idx[i]
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _run_fixed_stop_loss(df, entry_idx, entry_price, params):
    stop = entry_price * (1 - params["stop_pct"] / 100)
    for i in range(entry_idx, len(df)):
        bars_held = i - entry_idx + 1
        if df.iloc[i]["Low"] <= stop:
            return stop, _fmt_ts(df.index, i), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_fixed_take_profit(df, entry_idx, entry_price, params):
    target = entry_price * (1 + params["reward_pct"] / 100)
    for i in range(entry_idx, len(df)):
        bars_held = i - entry_idx + 1
        if df.iloc[i]["High"] >= target:
            return target, _fmt_ts(df.index, i), "target", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_risk_reward_target(df, entry_idx, entry_price, params):
    stop_pct = params["stop_pct"]
    k = params["risk_multiple"]
    stop = entry_price * (1 - stop_pct / 100)
    risk = entry_price - stop
    target = entry_price + k * risk
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        if bar["Low"] <= stop:
            return stop, _fmt_ts(df.index, i), "stop", bars_held
        if bar["High"] >= target:
            return target, _fmt_ts(df.index, i), "target", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_pct_trailing_stop(df, entry_idx, entry_price, params):
    trail_pct = params["trail_pct"]
    p_max = entry_price
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        if bar["High"] > p_max:
            p_max = bar["High"]
        trail_stop = p_max * (1 - trail_pct / 100)
        if bar["Low"] <= trail_stop:
            return trail_stop, _fmt_ts(df.index, i), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_atr_trailing_stop(df, entry_idx, entry_price, params):
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
            return trail_stop, _fmt_ts(df.index, i), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_atr_fixed_stop(df, entry_idx, entry_price, params):
    period = int(params["atr_period"])
    k = params["multiplier"]
    atr = _compute_atr(df, period)
    atr_at_entry = atr.iloc[entry_idx] if entry_idx < len(atr) else None
    if atr_at_entry is None or pd.isna(atr_at_entry):
        for j in range(entry_idx, -1, -1):
            if not pd.isna(atr.iloc[j]):
                atr_at_entry = atr.iloc[j]
                break
    if atr_at_entry is None or pd.isna(atr_at_entry):
        return None, None, "no_data", 0
    stop = entry_price - k * atr_at_entry
    for i in range(entry_idx, len(df)):
        bars_held = i - entry_idx + 1
        if df.iloc[i]["Low"] <= stop:
            return stop, _fmt_ts(df.index, i), "stop", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_close_below_ma(df, entry_idx, entry_price, params):
    period = int(params["ma_period"])
    sma = _compute_sma(df["Close"], period)
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        ma_val = sma.iloc[i]
        if pd.isna(ma_val):
            continue
        if bar["Close"] < ma_val:
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_ma_cross_exit(df, entry_idx, entry_price, params):
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
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
        prev_above = currently_above
    return None, None, "still_open", len(df) - entry_idx


def _run_rsi_overbought(df, entry_idx, entry_price, params):
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
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_macd_bearish_cross(df, entry_idx, entry_price, params):
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
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
        prev_above = currently_above
    return None, None, "still_open", len(df) - entry_idx


def _run_volume_fade(df, entry_idx, entry_price, params):
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
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_max_holding_period(df, entry_idx, entry_price, params):
    max_bars = int(params["max_bars"])
    exit_idx = entry_idx + max_bars
    if exit_idx >= len(df):
        return None, None, "still_open", len(df) - entry_idx
    bar = df.iloc[exit_idx]
    return bar["Close"], _fmt_ts(df.index, exit_idx), "time", max_bars


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


def run_backtest(
    strategy_key: str,
    params: dict[str, float],
    entries: list[dict[str, Any]],
) -> list[BacktestResult]:
    """Run an exit strategy backtest for a batch of entries using 1-min bars.

    Args:
        strategy_key: Key into STRATEGIES dict.
        params: User-provided parameter values.
        entries: List of dicts with keys: snapshot_id, symbol,
                 entry_price (float), entry_time (ISO timestamp string).

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
                entry_time=e.get("entry_time", e.get("entry_date", "")),
                exit_reason="unknown_strategy",
            )
            for e in entries
        ]

    results: list[BacktestResult] = []

    # Group entries by symbol to share OHLCV fetches
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_symbol[e["symbol"].upper()].append(e)

    for symbol, sym_entries in by_symbol.items():
        # Parse all entry times and find the date range we need
        parsed_times: list[tuple[dict, datetime]] = []
        for e in sym_entries:
            et_str = e.get("entry_time", e.get("entry_date", ""))
            et = _parse_entry_time(et_str)
            # Add 10 minutes to get past the entry point
            et_plus_10 = et + timedelta(minutes=10)
            parsed_times.append((e, et_plus_10))

        earliest_dt = min(t for _, t in parsed_times)
        start_date = (earliest_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        today_str = datetime.now().strftime("%Y-%m-%d")

        df = _get_ohlcv_1m(symbol, start_date, today_str)
        if df is None or df.empty:
            for e in sym_entries:
                results.append(BacktestResult(
                    snapshot_id=e["snapshot_id"],
                    symbol=symbol,
                    entry_price=e["entry_price"],
                    entry_time=e.get("entry_time", e.get("entry_date", "")),
                    exit_reason="no_data",
                ))
            continue

        for e, entry_dt in parsed_times:
            entry_price = e["entry_price"]
            entry_time_str = e.get("entry_time", e.get("entry_date", ""))

            # Find the first bar on or after entry_dt
            entry_ts = pd.Timestamp(entry_dt)
            entry_idx = df.index.searchsorted(entry_ts)
            if entry_idx >= len(df):
                results.append(BacktestResult(
                    snapshot_id=e["snapshot_id"],
                    symbol=symbol,
                    entry_price=entry_price,
                    entry_time=entry_time_str,
                    exit_reason="no_data",
                ))
                continue

            exit_price, exit_time, reason, bars_held = runner(
                df, entry_idx, entry_price, params,
            )

            # Ensure native Python types (not numpy)
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

            results.append(BacktestResult(
                snapshot_id=e["snapshot_id"],
                symbol=symbol,
                entry_price=entry_price,
                entry_time=entry_time_str,
                exit_price=round(exit_price, 2) if exit_price is not None else None,
                exit_time=exit_time,
                pnl_pct=pnl_pct,
                exit_reason=reason,
                bars_held=bars_held,
                hold_minutes=bars_held,
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
