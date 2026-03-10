"""Real-time VDD (Volume Delta Divergence) from tick data.

Query-time computation against the TimescaleDB ``trades`` table with
configurable bucket intervals and time-based lookback windows.

See docs/VDD-REALTIME.md for design details.
"""

from __future__ import annotations

import logging
import os

import asyncpg
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://tickdata:tickdata_dev@localhost:5433/tickdata"

# ---------------------------------------------------------------------------
# SQL: bucketed bars with classified volumes
# ---------------------------------------------------------------------------

_BARS_SQL = """
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


# ---------------------------------------------------------------------------
# Step 1: Query bucketed bars from TimescaleDB
# ---------------------------------------------------------------------------

async def get_vdd_bars(
    pool: asyncpg.Pool,
    symbol: str,
    lookback_m: float = 80.0,
    bucket_s: int = 30,
    min_trades_per_bucket: int = 3,
) -> pd.DataFrame | None:
    """Query TimescaleDB for bucketed bars with proportional volume split.

    Returns a DataFrame with columns:
        bucket, open, high, low, close, volume,
        est_uptick, est_downtick, trade_count
    or None if no data.
    """
    bucket_interval = f"{bucket_s} seconds"
    # Add 10% buffer so rolling window has enough history
    query_minutes = lookback_m * 1.1 + 2.0
    query_interval = f"{query_minutes:.0f} minutes"

    async with pool.acquire() as conn:
        rows = await conn.fetch(_BARS_SQL, bucket_interval, symbol, query_interval)

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

    # Proportional volume distribution
    df["raw_uptick"] = df["raw_uptick"].fillna(0)
    df["raw_downtick"] = df["raw_downtick"].fillna(0)

    has_classified = df["classified_total"].notna() & (df["classified_total"] > 0)
    uptick_ratio = (df["raw_uptick"] / df["classified_total"]).where(has_classified, 0.5)
    downtick_ratio = (df["raw_downtick"] / df["classified_total"]).where(has_classified, 0.5)

    df["est_uptick"] = df["volume"] * uptick_ratio
    df["est_downtick"] = df["volume"] * downtick_ratio

    return df[["bucket", "open", "high", "low", "close", "volume",
               "est_uptick", "est_downtick", "trade_count"]]


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
) -> bool:
    """Check if tick-based VDD exit signal is active for *symbol*.

    Returns True if the most recent bucket shows a VDD divergence signal.
    Returns False if no data or insufficient history.
    """
    bars = await get_vdd_bars(
        pool, symbol, lookback_m, bucket_s, min_trades_per_bucket,
    )
    if bars is None or bars.empty:
        return False

    lookback_bars = int(lookback_m * 60 / bucket_s)
    result = compute_vdd_signal(bars, lookback_bars)

    if result.empty:
        return False

    return bool(result.iloc[-1]["signal"])


# ---------------------------------------------------------------------------
# Pool helper (for use by trader app — lazy singleton)
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool | None:
    """Get or create a shared asyncpg pool for VDD queries.

    Returns None if connection fails (TimescaleDB not running).
    """
    global _pool
    if _pool is not None:
        return _pool
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
