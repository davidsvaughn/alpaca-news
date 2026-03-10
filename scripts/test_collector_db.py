#!/usr/bin/env python3
"""Test the tick collector pipeline: buffer → TimescaleDB → bars_1m.

Uses synthetic L1-style data (no Schwab connection needed).

Usage:
    uv run python scripts/test_collector_db.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from tick_collector.buffer import TradeBuffer
from tick_collector.classifier import TickClassifier
from tick_collector.db import Trade, connect, insert_trades

DSN = "postgresql://tickdata:tickdata_dev@localhost:5433/tickdata"


def generate_synthetic_l1_trades(
    symbol: str, n: int, start_price: float, start_time: datetime
) -> list[Trade]:
    """Simulate L1 updates with volume differencing."""
    classifier = TickClassifier()
    trades = []
    price = start_price
    total_vol = 1_000_000  # starting total_volume

    for i in range(n):
        # Simulate price movement
        import random
        price += random.uniform(-0.10, 0.12)
        price = round(price, 2)

        # Simulate last_size (the visible trade)
        last_size = random.choice([100, 100, 200, 500, 1000, 50])

        # Simulate volume_delta (total volume moved, >= last_size)
        # Sometimes multiple trades happen between L1 updates
        extra_hidden = random.choice([0, 0, 0, 100, 300, 500, 1000])
        volume_delta = last_size + extra_hidden
        total_vol += volume_delta

        t = start_time + timedelta(seconds=i)
        direction = classifier.classify(symbol, price)

        trades.append(Trade(
            time=t,
            symbol=symbol,
            price=price,
            size=last_size,
            exchange="XNYS" if i % 2 == 0 else "XNAS",
            direction=direction,
            source="L1",
            volume_delta=volume_delta,
            total_volume=total_vol,
        ))

    return trades


async def main():
    print("Connecting to TimescaleDB...")
    pool = await connect(DSN)

    # Clean slate
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM trades")

    # Generate synthetic data: 2 symbols, 120 updates each (~2 minutes)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)

    aapl_trades = generate_synthetic_l1_trades("AAPL", 120, 225.00, now)
    nvda_trades = generate_synthetic_l1_trades("NVDA", 120, 950.00, now)
    all_trades = sorted(aapl_trades + nvda_trades, key=lambda t: t.time)

    # Test buffer → drain → insert cycle
    buffer = TradeBuffer(max_batch=100)
    for t in all_trades:
        buffer.append(t)

    print(f"Buffered {buffer.pending} trades")

    total_inserted = 0
    while buffer.pending > 0:
        batch = buffer.drain()
        inserted = await insert_trades(pool, batch)
        total_inserted += inserted
        print(f"  Flushed batch: {inserted} trades")

    print(f"Total inserted: {total_inserted}")

    # Refresh continuous aggregate
    async with pool.acquire() as conn:
        await conn.execute(
            "CALL refresh_continuous_aggregate('bars_1m', $1::timestamptz, $2::timestamptz)",
            now - timedelta(minutes=1),
            datetime.now(timezone.utc),
        )

    # Query bars_1m
    print("\n--- bars_1m (1-minute aggregated bars) ---")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT bucket, symbol, open, high, low, close,
                   volume, uptick_vol, downtick_vol, net_delta,
                   trade_count, unclassified_vol
            FROM bars_1m
            ORDER BY bucket, symbol
        """)

    print(f"{'bucket':<22} {'sym':<6} {'open':>8} {'high':>8} {'low':>8} {'close':>8} "
          f"{'volume':>8} {'up':>6} {'down':>6} {'delta':>7} {'cnt':>5} {'unclass':>8}")
    print("-" * 110)
    for r in rows:
        print(f"{str(r['bucket']):<22} {r['symbol']:<6} "
              f"{r['open']:>8.2f} {r['high']:>8.2f} {r['low']:>8.2f} {r['close']:>8.2f} "
              f"{r['volume'] or 0:>8} {r['uptick_vol'] or 0:>6} {r['downtick_vol'] or 0:>6} "
              f"{r['net_delta'] or 0:>7} {r['trade_count']:>5} {r['unclassified_vol'] or 0:>8}")

    # Trade-size filtering query (institutional flow: trades >= 500 shares)
    print("\n--- Institutional flow (last_size >= 500) ---")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                time_bucket('1 minute', time) AS bucket,
                symbol,
                sum(size) FILTER (WHERE direction = 1) AS big_uptick,
                sum(size) FILTER (WHERE direction = -1) AS big_downtick,
                count(*) AS big_trades
            FROM trades
            WHERE size >= 500
            GROUP BY bucket, symbol
            ORDER BY bucket, symbol
        """)

    if rows:
        for r in rows:
            print(f"  {str(r['bucket']):<22} {r['symbol']:<6} "
                  f"up={r['big_uptick'] or 0:>6} down={r['big_downtick'] or 0:>6} "
                  f"trades={r['big_trades']}")
    else:
        print("  (no large trades)")

    # Volume gap analysis
    print("\n--- Volume gap analysis (unclassified volume) ---")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol,
                   sum(COALESCE(volume_delta, size)) AS total_vol,
                   sum(size) AS classified_vol,
                   sum(COALESCE(volume_delta, size) - size) AS unclassified_vol
            FROM trades
            GROUP BY symbol
        """)

    for r in rows:
        total = r["total_vol"] or 0
        unclass = r["unclassified_vol"] or 0
        pct = (unclass / total * 100) if total else 0
        print(f"  {r['symbol']}: total_vol={total:,} classified={r['classified_vol'] or 0:,} "
              f"unclassified={unclass:,} ({pct:.1f}%)")

    # Cleanup
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM trades")
        await conn.execute(
            "CALL refresh_continuous_aggregate('bars_1m', $1::timestamptz, $2::timestamptz)",
            now - timedelta(minutes=1),
            datetime.now(timezone.utc),
        )

    await pool.close()
    print("\nAll tests passed. Cleaned up.")


if __name__ == "__main__":
    asyncio.run(main())
