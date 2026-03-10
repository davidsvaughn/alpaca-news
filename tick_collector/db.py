"""TimescaleDB connection and batch insert logic."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Trade:
    """A single trade/update ready for DB insert."""

    time: datetime
    symbol: str
    price: float
    size: int  # last_size (L1) or trade size (TIMESALE)
    exchange: str | None
    direction: int  # +1, -1, or 0
    source: str = "L1"  # "L1" or "TS"
    volume_delta: int | None = None  # total volume change (L1 only)
    total_volume: int | None = None  # running total_volume snapshot (L1 only)


async def connect(dsn: str) -> asyncpg.Pool:
    """Create a connection pool to TimescaleDB."""
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    log.info("Connected to TimescaleDB")
    return pool


async def insert_trades(pool: asyncpg.Pool, trades: list[Trade]) -> int:
    """Batch insert trades. Returns count inserted."""
    if not trades:
        return 0

    records = [
        (
            t.time, t.symbol, t.price, t.size, t.exchange,
            t.direction, t.source, t.volume_delta, t.total_volume,
        )
        for t in trades
    ]

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO trades
                (time, symbol, price, size, exchange, direction,
                 source, volume_delta, total_volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            records,
        )

    return len(records)
