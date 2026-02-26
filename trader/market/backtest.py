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
import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
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
# Annualized return helpers
# ---------------------------------------------------------------------------

_TRADING_DAYS_PER_YEAR = 252
_BARS_PER_DAY = 390  # 6.5 hours × 60 minutes


def compute_ann_a(results: list[BacktestResult]) -> float | None:
    """Annualized return — unlimited capital (time-weighted log return).

    Annualized = exp(252 × Σln(1+rᵢ) / Σdᵢ) - 1
    where dᵢ = bars_held / 390 (trading days).
    Returns percentage, or None if no valid trades.
    """
    sum_log = 0.0
    sum_days = 0.0
    for r in results:
        if r.pnl_pct is None:
            continue
        sum_log += math.log(1 + r.pnl_pct / 100)
        sum_days += max(r.bars_held, 1) / _BARS_PER_DAY
    if sum_days <= 0:
        return None
    daily_log = sum_log / sum_days
    return (math.exp(_TRADING_DAYS_PER_YEAR * daily_log) - 1) * 100


def _parse_date(s: str | None) -> date | None:
    """Parse an ISO-ish timestamp string to a date."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except (ValueError, TypeError):
        return None


def _count_weekdays(start: date, end: date) -> int:
    """Count weekdays (Mon-Fri) from start to end inclusive."""
    if start > end:
        return 1
    count = 0
    d = start
    one_day = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += one_day
    return max(count, 1)


def compute_ann_b(results: list[BacktestResult]) -> float | None:
    """Annualized return — fixed capital split (daily equity curve CAGR).

    Builds a daily equity curve where capital is split equally among
    active trades each day.  Returns percentage, or None if insufficient data.
    """
    # Build per-trade info: date range + daily log rate
    trade_infos: list[tuple[date, date, float]] = []  # (entry_d, exit_d, daily_rate)
    global_min: date | None = None
    global_max: date | None = None

    for r in results:
        if r.pnl_pct is None:
            continue
        entry_d = _parse_date(r.entry_time)
        exit_d = _parse_date(r.exit_time)
        if entry_d is None or exit_d is None:
            continue
        if entry_d > exit_d:
            exit_d = entry_d
        wd = _count_weekdays(entry_d, exit_d)
        daily_rate = math.log(1 + r.pnl_pct / 100) / wd
        trade_infos.append((entry_d, exit_d, daily_rate))
        if global_min is None or entry_d < global_min:
            global_min = entry_d
        if global_max is None or exit_d > global_max:
            global_max = exit_d

    if not trade_infos or global_min is None or global_max is None:
        return None

    # Walk weekdays, build equity curve
    equity = 1.0
    total_weekdays = 0
    d = global_min
    one_day = timedelta(days=1)
    while d <= global_max:
        if d.weekday() < 5:
            total_weekdays += 1
            # Find active trades
            sum_rate = 0.0
            active = 0
            for entry_d, exit_d, daily_rate in trade_infos:
                if entry_d <= d <= exit_d:
                    sum_rate += daily_rate
                    active += 1
            if active > 0:
                equity *= math.exp(sum_rate / active)
        d += one_day

    if total_weekdays <= 0:
        return None
    cagr = (equity ** (_TRADING_DAYS_PER_YEAR / total_weekdays)) - 1
    return cagr * 100


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


def _compute_roc(close: pd.Series, period: int) -> pd.Series:
    return close.pct_change(period)


def _compute_stochastic(
    df: pd.DataFrame, n: int, k_smooth: int, d_smooth: int,
) -> tuple[pd.Series, pd.Series]:
    low_n = df["Low"].rolling(n).min()
    high_n = df["High"].rolling(n).max()
    k_raw = (df["Close"] - low_n) / (high_n - low_n) * 100
    k_line = k_raw.rolling(k_smooth).mean()
    d_line = k_line.rolling(d_smooth).mean()
    return k_line, d_line


def _compute_adx(df: pd.DataFrame, period: int) -> pd.Series:
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
    return _compute_ema(dx, period)


def _compute_volume_delta(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Compute per-bar uptick/downtick volume using the inter-bar tick rule.

    Returns (uptick_vol, downtick_vol) Series aligned to df index.
    """
    close = df["Close"].values
    volume = df["Volume"].values
    n = len(close)
    direction = np.zeros(n)
    for i in range(1, n):
        if close[i] > close[i - 1]:
            direction[i] = 1
        elif close[i] < close[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    uptick = pd.Series(
        np.where(direction > 0, volume, 0), index=df.index, dtype=float,
    )
    downtick = pd.Series(
        np.where(direction < 0, volume, 0), index=df.index, dtype=float,
    )
    return uptick, downtick


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


def _run_roc_reversal(df, entry_idx, entry_price, params):
    period = int(params["roc_period"])
    roc = _compute_roc(df["Close"], period)
    prev_roc = None
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        r = roc.iloc[i]
        if pd.isna(r):
            continue
        if prev_roc is not None and prev_roc > 0 and r < 0:
            return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
        prev_roc = r
    return None, None, "still_open", len(df) - entry_idx


def _run_stochastic_overbought(df, entry_idx, entry_price, params):
    n = int(params["stoch_period"])
    k_smooth = int(params["k_smooth"])
    d_smooth = int(params["d_smooth"])
    threshold = params["threshold"]
    k_line, d_line = _compute_stochastic(df, n, k_smooth, d_smooth)
    prev_k_above = None
    prev_k = None
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
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


def _run_adx_trend_decay(df, entry_idx, entry_price, params):
    period = int(params["adx_period"])
    weak = params["weak_threshold"]
    strong = params["strong_threshold"]
    lookback = int(params["lookback"])
    adx = _compute_adx(df, period)
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
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


def _run_volume_delta_divergence(df, entry_idx, entry_price, params):
    lookback = int(params["lookback"])
    uptick, downtick = _compute_volume_delta(df)
    cum_delta = (uptick - downtick).cumsum()
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
        if i < lookback:
            continue
        # Price at new rolling high?
        window_start = max(entry_idx, i - lookback)
        price_window = df["Close"].iloc[window_start:i]
        if price_window.empty:
            continue
        if bar["Close"] >= price_window.max():
            # But cumulative delta is lower than lookback bars ago?
            if cum_delta.iloc[i] < cum_delta.iloc[window_start]:
                return bar["Close"], _fmt_ts(df.index, i), "signal", bars_held
    return None, None, "still_open", len(df) - entry_idx


def _run_volume_imbalance_flip(df, entry_idx, entry_price, params):
    window = int(params["window"])
    alpha = params["threshold"]
    uptick, downtick = _compute_volume_delta(df)
    roll_up = uptick.rolling(window).sum()
    roll_dn = downtick.rolling(window).sum()
    roll_total = roll_up + roll_dn
    imbalance = (roll_up - roll_dn) / roll_total.replace(0, np.nan)
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx + 1
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
    "roc_reversal": _run_roc_reversal,
    "stochastic_overbought": _run_stochastic_overbought,
    "adx_trend_decay": _run_adx_trend_decay,
    "volume_delta_divergence": _run_volume_delta_divergence,
    "volume_imbalance_flip": _run_volume_imbalance_flip,
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


def _wrap_with_guards(
    runner, guard_stop_pct: float, guard_target_pct: float,
):
    """Wrap a strategy runner with guard stop-loss and take-profit checks.

    Guards fire on each bar BEFORE the primary strategy check.
    Stop uses bar Low, target uses bar High (conservative).
    """
    if guard_stop_pct <= 0 and guard_target_pct <= 0:
        return runner  # no guards, return original

    def guarded_runner(df, entry_idx, entry_price, params):
        stop = entry_price * (1 - guard_stop_pct / 100) if guard_stop_pct > 0 else 0
        target = entry_price * (1 + guard_target_pct / 100) if guard_target_pct > 0 else float("inf")

        for i in range(entry_idx, len(df)):
            bar = df.iloc[i]
            bars_held = i - entry_idx + 1
            # Guard stop (check Low)
            if guard_stop_pct > 0 and bar["Low"] <= stop:
                return stop, _fmt_ts(df.index, i), "guard_stop", bars_held
            # Guard target (check High)
            if guard_target_pct > 0 and bar["High"] >= target:
                return target, _fmt_ts(df.index, i), "guard_target", bars_held
            # Now check primary strategy for this bar only
            # We call the runner starting at this bar, but only check one bar
            # by creating a slice. Instead, just let the runner do a full scan
            # from entry_idx and compare which fires first.
            pass

        # No guard triggered — fall through to primary strategy
        return runner(df, entry_idx, entry_price, params)

    # Better approach: run both in parallel (bar by bar)
    def guarded_runner_v2(df, entry_idx, entry_price, params):
        stop = entry_price * (1 - guard_stop_pct / 100) if guard_stop_pct > 0 else 0
        target = entry_price * (1 + guard_target_pct / 100) if guard_target_pct > 0 else float("inf")

        # Get the primary strategy result
        prim_price, prim_time, prim_reason, prim_bars = runner(
            df, entry_idx, entry_price, params,
        )

        # Walk forward checking guards — if a guard fires earlier, use it
        for i in range(entry_idx, len(df)):
            bar = df.iloc[i]
            bars_held = i - entry_idx + 1

            # Did the primary strategy fire on or before this bar?
            if prim_price is not None and prim_bars <= bars_held:
                return prim_price, prim_time, prim_reason, prim_bars

            # Guard stop
            if guard_stop_pct > 0 and bar["Low"] <= stop:
                return stop, _fmt_ts(df.index, i), "guard_stop", bars_held
            # Guard target
            if guard_target_pct > 0 and bar["High"] >= target:
                return target, _fmt_ts(df.index, i), "guard_target", bars_held

        # Neither guard nor primary triggered
        if prim_price is not None:
            return prim_price, prim_time, prim_reason, prim_bars
        return None, None, "still_open", len(df) - entry_idx

    return guarded_runner_v2


def run_backtest(
    strategy_key: str,
    params: dict[str, float],
    entries: list[dict[str, Any]],
    market_close: str | None = "16:00",
    min_hold: int = 5,
    guard_stop_pct: float = 0,
    guard_target_pct: float = 0,
    price_delay_minutes: int = 10,
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

    Returns:
        List of BacktestResult, one per entry.
    """
    base_runner = _STRATEGY_RUNNERS.get(strategy_key)
    if base_runner is None:
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

    runner = _wrap_with_guards(base_runner, guard_stop_pct, guard_target_pct)

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
            # Add delay minutes to get past the entry point
            et_plus_10 = et + timedelta(minutes=price_delay_minutes)
            parsed_times.append((e, et_plus_10))

        earliest_dt = min(t for _, t in parsed_times)
        start_date = (earliest_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        today_str = datetime.now().strftime("%Y-%m-%d")

        df_raw = _get_ohlcv_1m(symbol, start_date, today_str)
        if df_raw is None or df_raw.empty:
            for e in sym_entries:
                results.append(BacktestResult(
                    snapshot_id=e["snapshot_id"],
                    symbol=symbol,
                    entry_price=e["entry_price"],
                    entry_time=e.get("entry_time", e.get("entry_date", "")),
                    exit_reason="no_data",
                ))
            continue

        # Filter to trading hours (removes extended-hours bars)
        df = _filter_trading_hours(df_raw, market_close)
        if df.empty:
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

            # If entry_price is missing (0) or entry landed before 9:30+delay
            # (pre-market/after-hours quote), use the bar at that offset instead.
            actual_entry_bar = df.index[entry_idx]
            entry_time_of_day = actual_entry_bar.hour * 60 + actual_entry_bar.minute
            cutoff_minutes = 9 * 60 + 30 + price_delay_minutes
            if entry_price <= 0 or entry_time_of_day <= cutoff_minutes:
                # Use the close of the bar N minutes into the session
                target_idx = min(entry_idx + price_delay_minutes, len(df) - 1)
                entry_price = float(df.iloc[target_idx]["Close"])
                entry_idx = target_idx  # walk-forward starts from here too

            # Apply min_hold: start exit checks after min_hold bars
            run_idx = min(entry_idx + max(0, min_hold), len(df) - 1)
            exit_price, exit_time, reason, bars_held = runner(
                df, run_idx, entry_price, params,
            )
            # Adjust bars_held to include the min_hold period
            bars_held = bars_held + (run_idx - entry_idx)

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

            # Compute wall-clock hold_minutes from actual timestamps
            # (bar count doesn't reflect overnight gaps)
            hold_minutes = bars_held  # fallback
            if exit_time:
                try:
                    exit_dt = pd.Timestamp(exit_time)
                    entry_bar_dt = df.index[entry_idx]
                    delta = exit_dt - entry_bar_dt
                    hold_minutes = max(1, int(delta.total_seconds() / 60))
                except Exception:
                    pass

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
                hold_minutes=hold_minutes,
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
