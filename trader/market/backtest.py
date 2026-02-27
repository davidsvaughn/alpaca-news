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
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    # Periodic close prices for equity curve (not sent to frontend).
    # List of (iso_timestamp, close_price) at configured resolution.
    periodic_closes: list[tuple[str, float]] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("periodic_closes", None)
        return d


# ---------------------------------------------------------------------------
# Annualized return helpers
# ---------------------------------------------------------------------------

_TRADING_DAYS_PER_YEAR = 252
_BARS_PER_DAY = 390  # 6.5 hours × 60 minutes


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
) -> dict[str, float] | None:
    """Annualized stats — fixed capital split (equity curve from real prices).

    Uses periodic close prices (at the given resolution) to build a portfolio
    equity curve with 1/N capital split among active trades.
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
            r_t = sum(active) / len(active)  # 1/N capital split
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
    dt = max(0.0, time.perf_counter() - t0)
    _trace_add_sec("indicator_total_sec", dt)
    _trace_add_sec("indicator_volume_delta_sec", dt)
    _trace_inc("indicator_total_calls")
    _trace_inc("indicator_volume_delta_calls")
    return uptick, downtick


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
    lookback = int(params["lookback"])
    uptick, downtick = _get_cached_indicator(
        indicator_cache, ("volume_delta",), lambda: _compute_volume_delta(df),
    )
    cum_delta = (uptick - downtick).cumsum()
    guard_enabled = guard_stop is not None or guard_target is not None
    for i in range(entry_idx, len(df)):
        bar = _get_bar(df, i)
        bars_held = i - entry_idx + 1
        if guard_enabled:
            guard_hit = _check_guards(bar, guard_stop, guard_target)
            if guard_hit is not None:
                price, reason = guard_hit
                return price, _fmt_ts(df.index, i), reason, bars_held
        if (i - entry_idx) < lookback:
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


def run_backtest(
    strategy_key: str,
    params: dict[str, float],
    entries: list[dict[str, Any]],
    market_close: str | None = "16:00",
    min_hold: int = 5,
    guard_stop_pct: float = 0,
    guard_target_pct: float = 0,
    price_delay_minutes: int = 10,
    stats_resolution_minutes: int = 60,
    trace: dict[str, Any] | None = None,
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
    total_start = time.perf_counter()
    stage_sec: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    symbol_sec: dict[str, float] = defaultdict(float)
    symbol_entries: dict[str, int] = defaultdict(int)

    def _mark(stage: str, start: float) -> None:
        stage_sec[stage] += max(0.0, time.perf_counter() - start)

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

    for symbol, sym_entries in by_symbol.items():
        sym_start = time.perf_counter()
        symbol_entries[symbol] = len(sym_entries)
        # Reuse indicators across trades for the same symbol/time window.
        indicator_cache: dict[tuple[Any, ...], Any] = {}
        # Parse all entry times and find the date range we need
        t_parse = time.perf_counter()
        parsed_times: list[tuple[dict, datetime]] = []
        for e in sym_entries:
            et_str = e.get("entry_time", e.get("entry_date", ""))
            et = _parse_entry_time(et_str)
            # Add delay minutes to get past the entry point
            et_plus_10 = et + timedelta(minutes=price_delay_minutes)
            parsed_times.append((e, et_plus_10))
        _mark("parse_entry_times", t_parse)

        earliest_dt = min(t for _, t in parsed_times)
        start_date = (earliest_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        today_str = datetime.now().strftime("%Y-%m-%d")

        t_load = time.perf_counter()
        df_raw = _get_ohlcv_1m(symbol, start_date, today_str)
        _mark("ohlcv_load", t_load)
        if df_raw is None or df_raw.empty:
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
            symbol_sec[symbol] += max(0.0, time.perf_counter() - sym_start)
            continue

        # Filter to trading hours (removes extended-hours bars)
        t_hours = time.perf_counter()
        df = _filter_trading_hours(df_raw, market_close)
        _mark("trading_hours_filter", t_hours)
        if df.empty:
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
            symbol_sec[symbol] += max(0.0, time.perf_counter() - sym_start)
            continue

        counts["symbols_with_data"] += 1
        for e, entry_dt in parsed_times:
            entry_price = e["entry_price"]
            entry_time_str = e.get("entry_time", e.get("entry_date", ""))

            # Find the first bar on or after entry_dt
            t_locate = time.perf_counter()
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
                counts["trades_no_data"] += 1
                _mark("entry_locate", t_locate)
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
            _mark("entry_locate", t_locate)

            # Apply min_hold: start exit checks after min_hold bars
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
            # Adjust bars_held to include the min_hold period
            bars_held = bars_held + (run_idx - entry_idx)

            # Ensure native Python types (not numpy)
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

            # Extract periodic close prices for equity curve stats
            t_curve = time.perf_counter()
            pc = _extract_periodic_closes(
                df, entry_idx, bars_held, stats_resolution_minutes,
            ) if pnl_pct is not None else None
            _mark("periodic_closes", t_curve)

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
                periodic_closes=pc,
            ))
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
        symbol_sec[symbol] += max(0.0, time.perf_counter() - sym_start)

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
            )
            sec["strategy_logic_sec"] = max(0.0, strategy_eval_sec - known_parts)
            pct_detail = {
                k: (v / strategy_eval_sec * 100.0) if strategy_eval_sec > 0 else 0.0
                for k, v in sec.items()
            }
            bar_calls = max(counts_detail.get("bar_read_calls", 0), 1)
            bars_scanned = counts_detail.get("bars_scanned", 0)
            counts_detail["strategy_eval_ms_per_bar"] = round(
                (strategy_eval_sec * 1000.0 / bars_scanned), 6,
            ) if bars_scanned > 0 else None
            counts_detail["bar_read_us_per_call"] = round(
                (sec.get("bar_read_sec", 0.0) * 1_000_000.0 / bar_calls), 6,
            )
            breakdown_sec = {
                "bar_read_sec": sec.get("bar_read_sec", 0.0),
                "guard_check_sec": sec.get("guard_check_sec", 0.0),
                "indicator_total_sec": sec.get("indicator_total_sec", 0.0),
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
