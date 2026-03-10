#!/usr/bin/env python3
"""Phase 1: Test Schwab streaming data sources.

Tests both TIMESALE_EQUITY (per-trade feed) and LEVELONE_EQUITIES (with
extended fields including Last Size, Trade Time, Last MIC ID) to compare
data quality for volume delta computation.

Three tiers of volume delta accuracy:
  Tier 1 (current): L1 last_price + total_volume differencing
  Tier 2 (this test): L1 + last_size/trade_time/last_mic_id fields
  Tier 3 (ideal):     TIMESALE_EQUITY per-trade feed

Usage:
    uv run python scripts/test_timesale.py [--symbols AAPL,NVDA,TSLA] [--duration 300]

Requires: SCHWAB_APP_KEY, SCHWAB_APP_SECRET env vars.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Stats tracker
# ---------------------------------------------------------------------------


class TradeStats:
    """Thread-safe trade statistics accumulator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # TIMESALE trades
        self.trades: dict[str, list[dict]] = defaultdict(list)
        # L1 updates (full detail)
        self.l1_updates: dict[str, list[dict]] = defaultdict(list)
        self.first_event_at: float | None = None
        self.last_event_at: float | None = None
        self.errors: list[str] = []

    def _touch_time(self) -> None:
        now = time.time()
        if self.first_event_at is None:
            self.first_event_at = now
        self.last_event_at = now

    def add_trade(self, symbol: str, trade: dict) -> None:
        with self._lock:
            self.trades[symbol].append(trade)
            self._touch_time()

    def add_l1(self, symbol: str, update: dict) -> None:
        with self._lock:
            self.l1_updates[symbol].append(update)
            self._touch_time()

    def add_error(self, msg: str) -> None:
        with self._lock:
            self.errors.append(msg)

    @property
    def elapsed(self) -> float:
        if self.first_event_at and self.last_event_at:
            return self.last_event_at - self.first_event_at
        return 0

    def summary(self) -> str:
        with self._lock:
            lines = ["\n" + "=" * 80]
            lines.append("Schwab Streaming Test Results")
            lines.append("=" * 80)

            elapsed = self.elapsed
            total_trades = sum(len(t) for t in self.trades.values())
            total_l1 = sum(len(u) for u in self.l1_updates.values())

            lines.append(f"\nElapsed (first→last event): {elapsed:.1f}s")

            # --- TIMESALE section ---
            lines.append(f"\n--- TIMESALE_EQUITY (Tier 3: per-trade feed) ---")
            lines.append(f"Total trades: {total_trades}")
            lines.append(f"Symbols with trades: {len(self.trades)}")

            if total_trades > 0:
                lines.append(
                    f"\n{'Symbol':<8} {'Trades':>8} {'Trades/min':>12} "
                    f"{'Avg Size':>10} {'Min Size':>10} {'Max Size':>10} {'Price Range':>20}"
                )
                lines.append("-" * 90)
                for sym in sorted(self.trades.keys()):
                    trades = self.trades[sym]
                    count = len(trades)
                    sizes = [t["size"] for t in trades if t.get("size")]
                    prices = [t["price"] for t in trades if t.get("price")]
                    tpm = (count / elapsed * 60) if elapsed > 0 else 0
                    avg_sz = sum(sizes) / len(sizes) if sizes else 0
                    min_sz = min(sizes) if sizes else 0
                    max_sz = max(sizes) if sizes else 0
                    p_range = (
                        f"{min(prices):.2f}-{max(prices):.2f}" if prices else "n/a"
                    )
                    lines.append(
                        f"{sym:<8} {count:>8} {tpm:>12.1f} "
                        f"{avg_sz:>10.0f} {min_sz:>10} {max_sz:>10} {p_range:>20}"
                    )

            # --- L1 section ---
            lines.append(f"\n--- LEVELONE_EQUITIES (Tier 2: extended fields) ---")
            lines.append(f"Total updates: {total_l1}")
            lines.append(f"Symbols with updates: {len(self.l1_updates)}")

            if total_l1 > 0:
                lines.append(
                    f"\n{'Symbol':<8} {'Updates':>8} {'Upd/min':>10} "
                    f"{'Has LastSz':>12} {'Has TrdTime':>12} {'Has MIC':>10} "
                    f"{'Vol Gaps':>10}"
                )
                lines.append("-" * 90)
                for sym in sorted(self.l1_updates.keys()):
                    updates = self.l1_updates[sym]
                    count = len(updates)
                    upm = (count / elapsed * 60) if elapsed > 0 else 0
                    has_last_size = sum(
                        1 for u in updates if u.get("last_size") is not None
                    )
                    has_trade_time = sum(
                        1 for u in updates if u.get("trade_time_ms") is not None
                    )
                    has_mic = sum(
                        1 for u in updates if u.get("last_mic_id") is not None
                    )
                    # Count volume gaps: where total_volume jumped by more than
                    # last_size (meaning we missed trades between updates)
                    vol_gaps = 0
                    prev_vol = None
                    for u in updates:
                        tv = u.get("total_volume")
                        ls = u.get("last_size")
                        if tv is not None and prev_vol is not None and ls is not None:
                            vol_delta = tv - prev_vol
                            if vol_delta > 0 and vol_delta > ls:
                                vol_gaps += 1
                        if tv is not None:
                            prev_vol = tv
                    lines.append(
                        f"{sym:<8} {count:>8} {upm:>10.1f} "
                        f"{has_last_size:>12} {has_trade_time:>12} {has_mic:>10} "
                        f"{vol_gaps:>10}"
                    )

                # Volume gap analysis
                lines.append(f"\nVolume Gap Analysis (L1 total_volume jump vs last_size):")
                lines.append(
                    "  'Vol Gaps' = updates where total_volume increased by MORE than last_size,"
                )
                lines.append(
                    "  meaning multiple trades happened between L1 updates (missed by L1)."
                )
                for sym in sorted(self.l1_updates.keys()):
                    updates = self.l1_updates[sym]
                    gaps = []
                    prev_vol = None
                    for u in updates:
                        tv = u.get("total_volume")
                        ls = u.get("last_size")
                        if (
                            tv is not None
                            and prev_vol is not None
                            and ls is not None
                            and tv > prev_vol
                        ):
                            vol_delta = tv - prev_vol
                            if vol_delta > ls:
                                gaps.append(
                                    {
                                        "vol_jump": vol_delta,
                                        "last_size": ls,
                                        "missed": vol_delta - ls,
                                    }
                                )
                        if tv is not None:
                            prev_vol = tv
                    if gaps:
                        total_missed = sum(g["missed"] for g in gaps)
                        total_vol_change = sum(g["vol_jump"] for g in gaps)
                        pct_missed = (
                            (total_missed / total_vol_change * 100)
                            if total_vol_change
                            else 0
                        )
                        lines.append(
                            f"  {sym}: {len(gaps)} gaps, "
                            f"total missed volume={total_missed:,}, "
                            f"{pct_missed:.1f}% of volume in gap updates"
                        )

            # --- TIMESALE vs L1 comparison ---
            if total_trades > 0 and total_l1 > 0:
                lines.append(f"\n--- Comparison ---")
                for sym in sorted(
                    set(self.trades.keys()) | set(self.l1_updates.keys())
                ):
                    ts_count = len(self.trades.get(sym, []))
                    l1_count = len(self.l1_updates.get(sym, []))
                    ratio = (ts_count / l1_count) if l1_count else 0
                    lines.append(
                        f"  {sym}: TIMESALE={ts_count} trades, L1={l1_count} updates, "
                        f"ratio={ratio:.1f}x"
                    )

            # --- Sample data ---
            if total_trades > 0:
                lines.append(f"\nSample TIMESALE trades (first 3 per symbol):")
                for sym in sorted(self.trades.keys()):
                    for t in self.trades[sym][:3]:
                        lines.append(f"  {sym}: {t}")

            if total_l1 > 0:
                lines.append(f"\nSample L1 updates (first 3 per symbol):")
                for sym in sorted(self.l1_updates.keys()):
                    for u in self.l1_updates[sym][:3]:
                        lines.append(f"  {sym}: {u}")

            if self.errors:
                lines.append(f"\nErrors ({len(self.errors)}):")
                for e in self.errors[:10]:
                    lines.append(f"  {e}")

            lines.append("=" * 80)
            return "\n".join(lines)


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

stats = TradeStats()
VERBOSE = False


def on_message(message):
    """Handle all WebSocket messages (both L1 and TIMESALE)."""
    try:
        if isinstance(message, str):
            message = json.loads(message)
        if not isinstance(message, dict):
            return

        # Handle subscription responses
        if "response" in message:
            for resp in message["response"]:
                service = resp.get("service", "?")
                command = resp.get("command", "?")
                code = resp.get("content", {}).get("code", "?")
                msg = resp.get("content", {}).get("msg", "")
                print(f"  [{service}] {command} → code={code} {msg}")
            return

        data_list = message.get("data", [])
        for data in data_list:
            service = data.get("service")
            ts = data.get("timestamp")

            if service == "TIMESALE_EQUITY":
                for content in data.get("content", []):
                    symbol = content.get("key", content.get("0", "?"))
                    trade = {
                        "time_ms": content.get("1"),
                        "price": content.get("2"),
                        "size": content.get("3"),
                        "sequence": content.get("4"),
                        "seq": content.get("seq"),
                    }
                    stats.add_trade(symbol, trade)
                    if VERBOSE:
                        t_str = ""
                        if trade["time_ms"]:
                            t_str = datetime.fromtimestamp(
                                trade["time_ms"] / 1000, tz=timezone.utc
                            ).strftime("%H:%M:%S.%f")[:-3]
                        sz = trade.get("size", "?")
                        px = trade.get("price")
                        px_str = f"{px:.2f}" if px else "?"
                        print(f"  TRADE {symbol}: {px_str} x {sz} @ {t_str}")

            elif service == "LEVELONE_EQUITIES":
                for content in data.get("content", []):
                    symbol = content.get("key", "?")
                    update = {
                        "last_price": content.get("3"),
                        "total_volume": content.get("8"),
                        # New Tier 2 fields
                        "last_size": content.get("9"),
                        "trade_time_ms": content.get("35"),
                        "last_mic_id": content.get("41"),
                        "last_id": content.get("16"),
                        "stream_ts": ts,
                    }
                    # Only keep fields that were actually present
                    update = {k: v for k, v in update.items() if v is not None}
                    stats.add_l1(symbol, update)
                    if VERBOSE:
                        ls = update.get("last_size", "?")
                        px = update.get("last_price")
                        px_str = f"{px:.2f}" if px else "?"
                        mic = update.get("last_mic_id", "?")
                        print(f"  L1    {symbol}: {px_str} x {ls} mic={mic}")

    except Exception as e:
        stats.add_error(f"message handler: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# L1 fields: original set + new Tier 2 fields (9, 16, 35, 41)
L1_FIELDS = "0,1,2,3,4,5,8,9,10,11,12,16,17,18,33,35,41,42"
# 0=Symbol, 1=Bid, 2=Ask, 3=Last, 4=BidSize, 5=AskSize, 8=TotalVolume,
# 9=LastSize, 10=High, 11=Low, 12=Close, 16=LastID, 17=Open, 18=NetChange,
# 33=Mark, 35=TradeTimeInLong, 41=LastMICID, 42=NetPctChange


def main():
    global VERBOSE

    parser = argparse.ArgumentParser(
        description="Test TIMESALE_EQUITY and LEVELONE_EQUITIES (extended fields)"
    )
    parser.add_argument(
        "--symbols",
        default="AAPL,NVDA,TSLA,SPY,AMD",
        help="Comma-separated symbols (default: AAPL,NVDA,TSLA,SPY,AMD)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Duration in seconds (default: 300 = 5 minutes)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Print every trade/update"
    )
    parser.add_argument(
        "--no-l1", action="store_true", help="Skip LEVELONE_EQUITIES subscription"
    )
    parser.add_argument(
        "--no-timesale",
        action="store_true",
        help="Skip TIMESALE_EQUITY subscription",
    )
    args = parser.parse_args()

    VERBOSE = args.verbose
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    print("Schwab Streaming Test")
    print(f"  Symbols:    {symbols}")
    print(f"  Duration:   {args.duration}s")
    print(f"  TIMESALE:   {not args.no_timesale}")
    print(f"  L1 (ext):   {not args.no_l1}")
    print(f"  L1 fields:  {L1_FIELDS}")
    print()

    # --- Init Schwab client ---
    app_key = os.getenv("SCHWAB_APP_KEY")
    app_secret = os.getenv("SCHWAB_APP_SECRET")
    if not app_key or not app_secret:
        print("ERROR: Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET env vars")
        sys.exit(1)

    import schwabdev

    client = schwabdev.Client(app_key, app_secret)
    stream = schwabdev.Stream(client)

    # --- Start stream ---
    print("Starting WebSocket stream...")
    stream.start(receiver=on_message)
    time.sleep(1)  # let WebSocket connect

    if not stream.active:
        print("ERROR: Stream failed to connect")
        sys.exit(1)
    print("  WebSocket connected.\n")

    keys_str = ",".join(symbols)

    # --- Subscribe to TIMESALE_EQUITY ---
    if not args.no_timesale:
        timesale_req = stream.basic_request(
            service="TIMESALE_EQUITY",
            command="ADD",
            parameters={"keys": keys_str, "fields": "0,1,2,3,4"},
        )
        print(f"Subscribing TIMESALE_EQUITY: {keys_str}")
        stream.send(timesale_req)

    # --- Subscribe to LEVELONE_EQUITIES with extended fields ---
    if not args.no_l1:
        l1_req = stream.level_one_equities(keys=symbols, fields=L1_FIELDS)
        print(f"Subscribing LEVELONE_EQUITIES (extended): {keys_str}")
        stream.send(l1_req)

    print(f"\nListening for {args.duration}s... (Ctrl+C to stop early)\n")

    # --- Progress updates ---
    stop = threading.Event()

    def on_sigint(sig, frame):
        print("\nInterrupted — stopping...")
        stop.set()

    signal.signal(signal.SIGINT, on_sigint)

    start_time = time.time()
    last_report = start_time
    while not stop.is_set():
        elapsed = time.time() - start_time
        if elapsed >= args.duration:
            break

        # Print progress every 30 seconds
        if time.time() - last_report >= 30:
            total_ts = sum(len(t) for t in stats.trades.values())
            total_l1 = sum(len(u) for u in stats.l1_updates.values())
            remaining = args.duration - elapsed
            print(
                f"  [{elapsed:.0f}s] timesale={total_ts}, l1={total_l1}, "
                f"remaining={remaining:.0f}s"
            )
            last_report = time.time()

        stop.wait(1)

    # --- Cleanup ---
    print("\nStopping stream...")
    stream.stop(clear_subscriptions=True)
    time.sleep(0.5)

    # --- Print results ---
    print(stats.summary())


if __name__ == "__main__":
    main()
