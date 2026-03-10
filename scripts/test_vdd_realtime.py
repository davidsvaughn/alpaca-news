#!/usr/bin/env python3
"""Test real-time tick-based VDD against TimescaleDB.

Queries live tick data, computes VDD signal, and prints results.
Requires TimescaleDB running with tick data collected.

Usage:
    uv run python scripts/test_vdd_realtime.py AAPL
    uv run python scripts/test_vdd_realtime.py AAPL --lookback-m 60 --bucket-s 15
    uv run python scripts/test_vdd_realtime.py AAPL --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from tick_collector.vdd import (
    DEFAULT_DSN,
    check_vdd_exit,
    compute_vdd_signal,
    get_vdd_bars,
)


async def main() -> None:
    import asyncpg
    import os

    parser = argparse.ArgumentParser(description="Test tick-based VDD signal")
    parser.add_argument("symbol", help="Stock symbol (e.g. AAPL)")
    parser.add_argument("--lookback-m", type=float, default=80.0, help="Lookback minutes")
    parser.add_argument("--bucket-s", type=int, default=30, help="Bucket seconds")
    parser.add_argument("--min-trades", type=int, default=3, help="Min trades per bucket")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all bars")
    args = parser.parse_args()

    dsn = os.getenv("TIMESCALE_DSN", DEFAULT_DSN)
    print(f"Connecting to {dsn} ...")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)

    print(f"\n--- {args.symbol} | lookback_m={args.lookback_m} | bucket_s={args.bucket_s} ---\n")

    bars = await get_vdd_bars(
        pool, args.symbol, args.lookback_m, args.bucket_s, args.min_trades,
    )

    if bars is None or bars.empty:
        print("No tick data found. Is the collector running?")
        await pool.close()
        return

    lookback_bars = int(args.lookback_m * 60 / args.bucket_s)
    result = compute_vdd_signal(bars, lookback_bars)

    print(f"Total buckets: {len(result)} (need {lookback_bars} for lookback)")
    print()

    if args.verbose:
        cols = ["bucket", "close", "volume", "est_uptick", "est_downtick",
                "cum_delta", "trade_count", "signal"]
        print(result[cols].to_string(index=False))
        print()

    # Show last few bars
    tail = result.tail(5)
    print("Last 5 buckets:")
    for _, row in tail.iterrows():
        flag = " <<< SIGNAL" if row["signal"] else ""
        print(
            f"  {row['bucket']}  close={row['close']:.2f}  "
            f"vol={row['volume']:.0f}  "
            f"up={row['est_uptick']:.0f}  dn={row['est_downtick']:.0f}  "
            f"cum_delta={row['cum_delta']:.0f}  "
            f"trades={row['trade_count']}{flag}"
        )

    print()
    signal = bool(result.iloc[-1]["signal"])
    print(f"VDD exit signal active: {signal}")

    # Also run the single-call API for comparison
    api_result = await check_vdd_exit(
        pool, args.symbol, args.lookback_m, args.bucket_s, args.min_trades,
    )
    print(f"check_vdd_exit() returned: {api_result}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
