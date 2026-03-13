"""VDD (Volume Delta Divergence) from tick data.

Query-time computation against the TimescaleDB ``trades`` table with
configurable bucket intervals, time-based lookback windows, and multiple
volume classification modes for comparison testing.

Bucketing modes:
  - **Time-based** (default): fixed-width intervals (15s, 30s, 60s) via
    TimescaleDB ``time_bucket()``. Simple, predictable bar timing.
  - **Trade-count** (tick clock): fixed number of L1 updates per bar.
    Guarantees every bar has the same statistical weight regardless of
    liquidity. Liquid stocks get short bars (~15-30s), thin stocks get
    longer bars (~2-3min). See ``get_vdd_bars_by_trades()``.

Classification modes:
  - ``proportional`` (default): distribute total volume using the visible
    uptick/downtick ratio from Lee-Ready classified trades.
  - ``visible_only``: use only the directly classified (visible) trade
    volume — no extrapolation to unclassified volume.
  - ``bar_binary``: mimic the backtest inter-bar tick rule — each bucket's
    entire volume is assigned up or down based on close-to-close direction.

See docs/VDD-REALTIME.md and docs/TICK-COLLECTOR.md for design details.
"""

from __future__ import annotations

import logging
import os

from datetime import datetime, timedelta, timezone
from typing import Literal

import asyncpg
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://tickdata:tickdata_dev@localhost:5433/tickdata"

VolumeMode = Literal["proportional", "visible_only", "bar_binary"]

# ---------------------------------------------------------------------------
# SQL: bucketed bars with classified volumes
# ---------------------------------------------------------------------------

# Real-time query (uses NOW())
_BARS_SQL_REALTIME = """
SELECT
    time_bucket($1::interval, time) AS bucket,
    first(price, time)              AS open,
    max(price)                      AS high,
    min(price)                      AS low,
    last(price, time)               AS close,
    -- True volume (from volume_delta differencing)
    sum(volume_delta)               AS volume,
    -- Classified volumes (from visible L1 trades with known direction)
    sum(size) FILTER (WHERE direction =  1) AS raw_uptick,
    sum(size) FILTER (WHERE direction = -1) AS raw_downtick,
    -- Classified total (denominator for proportional split)
    NULLIF(
        COALESCE(sum(size) FILTER (WHERE direction =  1), 0)
      + COALESCE(sum(size) FILTER (WHERE direction = -1), 0),
        0
    ) AS classified_total,
    count(*) AS trade_count
FROM trades
WHERE symbol = $2
  AND volume_delta IS NOT NULL
  AND time        >= NOW() - $3::interval
  AND received_at >= NOW() - $3::interval
GROUP BY bucket
ORDER BY bucket
"""

# Historical query (uses explicit time range)
_BARS_SQL_HISTORICAL = """
SELECT
    time_bucket($1::interval, time) AS bucket,
    first(price, time)              AS open,
    max(price)                      AS high,
    min(price)                      AS low,
    last(price, time)               AS close,
    sum(volume_delta)               AS volume,
    sum(size) FILTER (WHERE direction =  1) AS raw_uptick,
    sum(size) FILTER (WHERE direction = -1) AS raw_downtick,
    NULLIF(
        COALESCE(sum(size) FILTER (WHERE direction =  1), 0)
      + COALESCE(sum(size) FILTER (WHERE direction = -1), 0),
        0
    ) AS classified_total,
    count(*) AS trade_count
FROM trades
WHERE symbol = $2
  AND volume_delta IS NOT NULL
  AND time >= $3
  AND time <  $4
GROUP BY bucket
ORDER BY bucket
"""


def _apply_volume_mode(df: pd.DataFrame, mode: VolumeMode) -> pd.DataFrame:
    """Add est_uptick / est_downtick columns based on the chosen mode."""
    df["raw_uptick"] = df["raw_uptick"].fillna(0)
    df["raw_downtick"] = df["raw_downtick"].fillna(0)

    if mode == "visible_only":
        # Use only directly classified trade volume
        df["est_uptick"] = df["raw_uptick"]
        df["est_downtick"] = df["raw_downtick"]

    elif mode == "bar_binary":
        # Mimic backtest: entire volume assigned by close-to-close direction
        close = df["close"].to_numpy(dtype=float)
        volume = df["volume"].to_numpy(dtype=float)
        n = len(close)
        direction = np.zeros(n, dtype=float)
        if n > 1:
            step = np.sign(np.diff(close))
            raw = np.empty(n, dtype=float)
            raw[0] = 0.0
            raw[1:] = step
            prev_nonzero = np.where(raw != 0.0, np.arange(n), 0)
            np.maximum.accumulate(prev_nonzero, out=prev_nonzero)
            direction = raw[prev_nonzero]
        df["est_uptick"] = np.where(direction > 0, volume, 0.0)
        df["est_downtick"] = np.where(direction < 0, volume, 0.0)

    else:  # proportional (default)
        has_classified = df["classified_total"].notna() & (df["classified_total"] > 0)
        uptick_ratio = (df["raw_uptick"] / df["classified_total"]).where(has_classified, 0.5)
        downtick_ratio = (df["raw_downtick"] / df["classified_total"]).where(has_classified, 0.5)
        df["est_uptick"] = df["volume"] * uptick_ratio
        df["est_downtick"] = df["volume"] * downtick_ratio

    return df


_RESULT_COLS = [
    "bucket", "open", "high", "low", "close", "volume",
    "est_uptick", "est_downtick", "raw_uptick", "raw_downtick",
    "classified_total", "trade_count",
]


# ---------------------------------------------------------------------------
# Step 1: Query bucketed bars from TimescaleDB
# ---------------------------------------------------------------------------

async def get_vdd_bars(
    pool: asyncpg.Pool,
    symbol: str,
    lookback_m: float = 80.0,
    bucket_s: int = 30,
    min_trades_per_bucket: int = 3,
    volume_mode: VolumeMode = "proportional",
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> pd.DataFrame | None:
    """Query TimescaleDB for bucketed bars with volume classification.

    For real-time use: omit start_time/end_time (queries relative to NOW()).
    For historical/backtest: provide both start_time and end_time (UTC).

    Returns a DataFrame with columns:
        bucket, open, high, low, close, volume,
        est_uptick, est_downtick, raw_uptick, raw_downtick,
        classified_total, trade_count
    or None if no data.
    """
    bucket_interval = timedelta(seconds=bucket_s)

    async with pool.acquire() as conn:
        if start_time is not None and end_time is not None:
            rows = await conn.fetch(
                _BARS_SQL_HISTORICAL, bucket_interval, symbol,
                start_time, end_time,
            )
        else:
            query_interval = timedelta(minutes=lookback_m * 1.1 + 2.0)
            rows = await conn.fetch(
                _BARS_SQL_REALTIME, bucket_interval, symbol, query_interval,
            )

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=[
        "bucket", "open", "high", "low", "close", "volume",
        "raw_uptick", "raw_downtick", "classified_total", "trade_count",
    ])

    # Filter thin buckets
    df = df[df["trade_count"] >= min_trades_per_bucket].reset_index(drop=True)
    if df.empty:
        return None

    # Cast numeric columns
    for col in ("open", "high", "low", "close", "volume",
                "raw_uptick", "raw_downtick", "classified_total"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = _apply_volume_mode(df, volume_mode)

    return df[_RESULT_COLS]


# ---------------------------------------------------------------------------
# Step 1b: Trade-count bucketing (tick clock)
# ---------------------------------------------------------------------------

# Real-time trade-count query
_TRADES_SQL_REALTIME = """
SELECT time, price, size, direction, volume_delta
FROM trades
WHERE symbol = $1
  AND volume_delta IS NOT NULL
  AND time        >= NOW() - $2::interval
  AND received_at >= NOW() - $2::interval
ORDER BY time
"""

# Historical trade-count query
_TRADES_SQL_HISTORICAL = """
SELECT time, price, size, direction, volume_delta
FROM trades
WHERE symbol = $1
  AND volume_delta IS NOT NULL
  AND time >= $2
  AND time <  $3
ORDER BY time
"""


def _bucket_by_trade_count(
    trades_df: pd.DataFrame,
    trades_per_bar: int,
) -> pd.DataFrame:
    """Aggregate raw trades into bars with a fixed number of trades each.

    Each bar contains exactly ``trades_per_bar`` trades (last bar may have
    fewer). This is "tick clock" bucketing — bars are equally weighted by
    trade count, so liquid stocks get frequent short bars and thin stocks
    get longer bars.

    Returns DataFrame with same columns as time-bucketed bars.
    """
    n = len(trades_df)
    if n == 0:
        return pd.DataFrame()

    # Assign each trade to a bar group via integer division
    trades_df = trades_df.copy()
    trades_df["bar_group"] = np.arange(n) // trades_per_bar

    grouped = trades_df.groupby("bar_group")
    bars = pd.DataFrame({
        "bucket": grouped["time"].first(),
        "open": grouped["price"].first(),
        "high": grouped["price"].max(),
        "low": grouped["price"].min(),
        "close": grouped["price"].last(),
        "volume": grouped["volume_delta"].sum(),
        "raw_uptick": grouped.apply(
            lambda g: g.loc[g["direction"] == 1, "size"].sum(), include_groups=False),
        "raw_downtick": grouped.apply(
            lambda g: g.loc[g["direction"] == -1, "size"].sum(), include_groups=False),
        "trade_count": grouped["price"].count(),
    })

    # classified_total for proportional mode
    bars["classified_total"] = bars["raw_uptick"] + bars["raw_downtick"]
    bars.loc[bars["classified_total"] == 0, "classified_total"] = np.nan

    return bars.reset_index(drop=True)


async def get_vdd_bars_by_trades(
    pool: asyncpg.Pool,
    symbol: str,
    lookback_m: float = 80.0,
    trades_per_bar: int = 20,
    volume_mode: VolumeMode = "proportional",
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> pd.DataFrame | None:
    """Query raw trades and bucket by trade count (tick clock).

    Instead of fixed time intervals, each bar contains exactly
    ``trades_per_bar`` L1 updates. This guarantees every bar has the same
    statistical weight regardless of the stock's liquidity.

    For liquid stocks (~50 L1/min), trades_per_bar=20 ≈ 24s bars.
    For thin stocks (~5 L1/min), trades_per_bar=20 ≈ 4min bars.

    Returns same DataFrame format as ``get_vdd_bars()``, or None.
    """
    async with pool.acquire() as conn:
        if start_time is not None and end_time is not None:
            rows = await conn.fetch(
                _TRADES_SQL_HISTORICAL, symbol, start_time, end_time,
            )
        else:
            query_interval = timedelta(minutes=lookback_m * 1.1 + 2.0)
            rows = await conn.fetch(
                _TRADES_SQL_REALTIME, symbol, query_interval,
            )

    if not rows:
        return None

    trades_df = pd.DataFrame(rows, columns=[
        "time", "price", "size", "direction", "volume_delta",
    ])

    # Cast numeric
    for col in ("price", "size", "volume_delta"):
        trades_df[col] = pd.to_numeric(trades_df[col], errors="coerce")
    trades_df["direction"] = pd.to_numeric(trades_df["direction"], errors="coerce").fillna(0).astype(int)

    bars = _bucket_by_trade_count(trades_df, trades_per_bar)
    if bars.empty:
        return None

    bars = _apply_volume_mode(bars, volume_mode)
    return bars[_RESULT_COLS]


# ---------------------------------------------------------------------------
# Step 2: VDD signal detection
# ---------------------------------------------------------------------------

def compute_vdd_signal(bars: pd.DataFrame, lookback_bars: int) -> pd.DataFrame:
    """Detect VDD divergence from bucketed tick data.

    ``bars`` must have columns: close, est_uptick, est_downtick.
    Returns ``bars`` with added columns: cum_delta, signal.
    """
    bars = bars.copy()
    bars["bucket_delta"] = bars["est_uptick"] - bars["est_downtick"]
    bars["cum_delta"] = bars["bucket_delta"].cumsum()

    prev_roll_max = (
        bars["close"].shift(1)
        .rolling(lookback_bars, min_periods=lookback_bars)
        .max()
    )
    lag_cum_delta = bars["cum_delta"].shift(lookback_bars)

    bars["signal"] = (
        (bars["close"] >= prev_roll_max)
        & (bars["cum_delta"] < lag_cum_delta)
    ).fillna(False)

    return bars


# ---------------------------------------------------------------------------
# Step 3: Public API
# ---------------------------------------------------------------------------

async def check_vdd_exit(
    pool: asyncpg.Pool,
    symbol: str,
    lookback_m: float = 80.0,
    bucket_s: int = 30,
    min_trades_per_bucket: int = 3,
    volume_mode: VolumeMode = "proportional",
) -> bool:
    """Check if tick-based VDD exit signal is active for *symbol*.

    Returns True if the most recent bucket shows a VDD divergence signal.
    Returns False if no data or insufficient history.
    """
    bars = await get_vdd_bars(
        pool, symbol, lookback_m, bucket_s, min_trades_per_bucket,
        volume_mode=volume_mode,
    )
    if bars is None or bars.empty:
        return False

    lookback_bars = int(lookback_m * 60 / bucket_s)
    result = compute_vdd_signal(bars, lookback_bars)

    if result.empty:
        return False

    return bool(result.iloc[-1]["signal"])


def find_first_signal(bars: pd.DataFrame, lookback_bars: int,
                      after_bucket: datetime | None = None) -> dict | None:
    """Find the first VDD signal in the bars DataFrame.

    If ``after_bucket`` is given, only signals at or after that time count.
    Returns dict with signal details, or None if no signal found.
    """
    result = compute_vdd_signal(bars, lookback_bars)
    signals = result[result["signal"]]
    if after_bucket is not None:
        signals = signals[signals["bucket"] >= after_bucket]
    if signals.empty:
        return None
    row = signals.iloc[0]
    return {
        "bucket": row["bucket"],
        "close": float(row["close"]),
        "cum_delta": float(row["cum_delta"]),
        "bucket_delta": float(row["bucket_delta"]),
    }


# ---------------------------------------------------------------------------
# Pool helper (for use by trader app — lazy singleton)
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool | None:
    """Get or create a shared asyncpg pool for VDD queries.

    Returns None if connection fails (TimescaleDB not running).
    Automatically reconnects if the existing pool's connections are dead.
    """
    global _pool
    if _pool is not None:
        # Health check: verify a connection is still usable
        try:
            async with _pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return _pool
        except Exception:
            log.warning("VDD: pool connections dead, reconnecting...")
            try:
                await _pool.close()
            except Exception:
                pass
            _pool = None
    dsn = os.getenv("TIMESCALE_DSN", DEFAULT_DSN)
    try:
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        log.info("VDD: connected to TimescaleDB at %s", dsn)
        return _pool
    except Exception:
        log.warning("VDD: TimescaleDB unavailable, tick-based VDD disabled", exc_info=True)
        return None


async def close_pool() -> None:
    """Close the shared pool (call on shutdown)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
