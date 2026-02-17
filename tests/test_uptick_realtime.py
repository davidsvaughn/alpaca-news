"""Real-time streaming test for uptick/downtick volume.

Captures live trade updates via yfinance WebSocket AND Schwab LEVELONE_EQUITIES,
computes uptick/downtick volume from each stream, then compares against the
1-minute bar approximation (interbar_tick_rule).

Run during market hours:
    uv run python tests/test_uptick_realtime.py [--seconds 120] [--symbols SPY AAPL]
"""

import argparse
import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf


# ── Stream Accumulator ─────────────────────────────────────────────

@dataclass
class TickAccumulator:
    """Accumulates uptick/downtick volume from streaming price updates."""
    prev_price: float | None = None
    prev_day_volume: int | None = None
    last_nonzero_dir: int = 0  # +1 or -1
    uptick_vol: int = 0
    downtick_vol: int = 0
    zero_tick_vol: int = 0
    update_count: int = 0
    ticks: list = field(default_factory=list)  # raw tick log

    def on_update(self, price: float, day_volume: int, timestamp: float | None = None):
        """Process a streaming update."""
        self.update_count += 1

        if self.prev_price is None or self.prev_day_volume is None:
            self.prev_price = price
            self.prev_day_volume = day_volume
            return

        # Volume delta since last update
        dv = max(0, day_volume - self.prev_day_volume)

        # Direction
        if price > self.prev_price:
            direction = 1
            self.uptick_vol += dv
            self.last_nonzero_dir = 1
        elif price < self.prev_price:
            direction = -1
            self.downtick_vol += dv
            self.last_nonzero_dir = -1
        else:
            direction = 0
            if self.last_nonzero_dir > 0:
                self.uptick_vol += dv
            elif self.last_nonzero_dir < 0:
                self.downtick_vol += dv
            else:
                self.zero_tick_vol += dv

        self.ticks.append({
            "time": timestamp or time.time(),
            "price": price,
            "day_volume": day_volume,
            "dv": dv,
            "direction": direction,
        })

        self.prev_price = price
        self.prev_day_volume = day_volume

    @property
    def total_vol(self) -> int:
        return self.uptick_vol + self.downtick_vol + self.zero_tick_vol

    @property
    def net_delta(self) -> int:
        return self.uptick_vol - self.downtick_vol

    @property
    def imbalance(self) -> float:
        total = self.uptick_vol + self.downtick_vol
        return self.net_delta / total if total > 0 else 0.0


# ── Interbar Tick Rule (from test_uptick_volume.py) ────────────────

def interbar_tick_rule(df: pd.DataFrame) -> pd.DataFrame:
    prev_close = df["Close"].shift(1)
    direction = np.sign(df["Close"] - prev_close)
    direction = direction.replace(0, np.nan).ffill().fillna(0)
    uptick_vol = df["Volume"].where(direction > 0, 0)
    downtick_vol = df["Volume"].where(direction < 0, 0)
    neutral = direction == 0
    uptick_vol = uptick_vol + df["Volume"].where(neutral, 0) / 2
    downtick_vol = downtick_vol + df["Volume"].where(neutral, 0) / 2
    return pd.DataFrame({
        "uptick_vol": uptick_vol,
        "downtick_vol": downtick_vol,
        "delta": uptick_vol - downtick_vol,
    }, index=df.index)


# ── Schwab Stream ─────────────────────────────────────────────────

def start_schwab_stream(symbols, accumulators, lock, counter):
    """Start Schwab LEVELONE_EQUITIES stream. Returns (stream, client) or None."""
    if os.getenv("SCHWAB_DISABLED", "").lower() in ("true", "1"):
        print("  Schwab: SCHWAB_DISABLED=true, skipping")
        return None

    app_key = os.getenv("SCHWAB_APP_KEY")
    app_secret = os.getenv("SCHWAB_APP_SECRET")
    if not app_key or not app_secret:
        print("  Schwab: missing SCHWAB_APP_KEY/SCHWAB_APP_SECRET, skipping")
        return None

    try:
        import schwabdev

        client = schwabdev.Client(app_key, app_secret)
        streamer = schwabdev.Stream(client)

        # Fields: 3=LastPrice, 8=TotalVolume, 9=LastSize
        SCHWAB_FIELDS = "0,3,8,9"

        # Track last known price/volume per symbol (Schwab sends only changed fields)
        last_known: dict[str, dict] = {}

        def on_schwab_message(message):
            # Messages arrive as JSON strings, not dicts
            if isinstance(message, str):
                try:
                    message = json.loads(message)
                except json.JSONDecodeError:
                    return
            if not isinstance(message, dict):
                return
            for data in message.get("data", []):
                if data.get("service") != "LEVELONE_EQUITIES":
                    continue
                ts = data.get("timestamp", time.time() * 1000) / 1000.0
                for content in data.get("content", []):
                    symbol = content.get("key")
                    if symbol is None:
                        continue

                    # Merge changed fields into last known state
                    if symbol not in last_known:
                        last_known[symbol] = {}
                    if "3" in content:
                        last_known[symbol]["price"] = float(content["3"])
                    if "8" in content:
                        last_known[symbol]["total_vol"] = int(content["8"])

                    state = last_known[symbol]
                    if "price" not in state or "total_vol" not in state:
                        continue

                    with lock:
                        accumulators[symbol].on_update(
                            state["price"], state["total_vol"], ts,
                        )
                        counter[0] += 1

        streamer.start(receiver=on_schwab_message)
        time.sleep(1)

        streamer.send(
            streamer.level_one_equities(keys=symbols, fields=SCHWAB_FIELDS)
        )
        print("  Schwab: stream started")
        return streamer, client

    except Exception as e:
        print(f"  Schwab: failed to start stream: {e}")
        return None


# ── Reporting Helpers ──────────────────────────────────────────────

def print_results(label, accumulators, symbols):
    """Print streaming results for a set of accumulators."""
    print(f"\n{'Symbol':<8} {'Updates':>8} {'Uptick Vol':>14} {'Downtick Vol':>14} {'Net Delta':>14} {'Imbalance':>10} {'Price Move':>12}")
    print("-" * 82)

    for symbol in symbols:
        acc = accumulators[symbol]
        if acc.update_count < 2:
            print(f"{symbol:<8} {'NO DATA':>8}")
            continue

        first_price = acc.ticks[0]["price"] if acc.ticks else 0
        last_price = acc.ticks[-1]["price"] if acc.ticks else 0
        price_pct = (last_price - first_price) / first_price * 100 if first_price else 0

        print(
            f"{symbol:<8} "
            f"{acc.update_count:>8} "
            f"{acc.uptick_vol:>14,} "
            f"{acc.downtick_vol:>14,} "
            f"{acc.net_delta:>14,} "
            f"{acc.imbalance:>10.4f} "
            f"{price_pct:>+11.3f}%"
        )
        if acc.zero_tick_vol > 0:
            print(f"         (unclassified zero-tick vol: {acc.zero_tick_vol:,})")


def print_tick_log(label, accumulators, symbol):
    """Print tick-level sample for a symbol."""
    acc = accumulators[symbol]
    if not acc.ticks:
        return

    print(f"\n{'='*80}")
    print(f"TICK LOG SAMPLE: {symbol} [{label}] (first & last 10 updates)")
    print(f"{'='*80}")
    print(f"  {'Time':<12} {'Price':>10} {'DayVol':>14} {'dV':>10} {'Dir':>5}")
    print(f"  {'-'*53}")

    sample = acc.ticks[:10] + (["..."] if len(acc.ticks) > 20 else []) + acc.ticks[-10:]
    for t in sample:
        if t == "...":
            print(f"  {'...'}")
            continue
        ts = datetime.fromtimestamp(t["time"], tz=timezone.utc).strftime("%H:%M:%S.%f")[:12]
        dir_str = {1: "UP", -1: "DOWN", 0: "="}[t["direction"]]
        print(f"  {ts:<12} {t['price']:>10.2f} {t['day_volume']:>14,} {t['dv']:>10,} {dir_str:>5}")

    if len(acc.ticks) >= 2:
        times = [t["time"] for t in acc.ticks]
        intervals = np.diff(times)
        total_time = times[-1] - times[0]
        print(f"\n  Update frequency:")
        print(f"    Total updates: {len(acc.ticks)}")
        print(f"    Mean interval: {np.mean(intervals):.3f}s")
        print(f"    Median interval: {np.median(intervals):.3f}s")
        print(f"    Min/Max interval: {np.min(intervals):.3f}s / {np.max(intervals):.3f}s")
        if total_time > 0:
            print(f"    Updates/min: {len(acc.ticks) / (total_time / 60):.1f}")


# ── Main Test ──────────────────────────────────────────────────────

def run_streaming_test(symbols: list[str], duration_seconds: int = 120):
    # -- yfinance accumulators --
    yf_accs: dict[str, TickAccumulator] = defaultdict(TickAccumulator)
    yf_lock = threading.Lock()
    yf_count = 0

    def on_yf_message(msg):
        nonlocal yf_count
        symbol = msg.get("id")
        price = msg.get("price")
        day_vol = msg.get("day_volume") or msg.get("dayVolume")
        ts = msg.get("time")

        if not symbol or price is None or day_vol is None:
            return
        try:
            day_vol = int(day_vol)
            ts = float(ts) / 1000.0 if ts else time.time()
        except (ValueError, TypeError):
            return

        with yf_lock:
            yf_accs[symbol].on_update(float(price), day_vol, ts)
            yf_count += 1

    # -- Schwab accumulators --
    schwab_accs: dict[str, TickAccumulator] = defaultdict(TickAccumulator)
    schwab_lock = threading.Lock()
    schwab_counter = [0]  # mutable for closure

    # -- Start streams --
    start_time = datetime.now(timezone.utc)
    print(f"\nStarting streams...")
    print(f"  Symbols: {symbols}")
    print(f"  Duration: {duration_seconds}s")
    print(f"  Start: {start_time.strftime('%H:%M:%S')} UTC")

    # yfinance WebSocket
    ws = yf.WebSocket(verbose=False)
    ws.subscribe(symbols)
    time.sleep(0.5)
    yf_thread = threading.Thread(target=ws.listen, args=(on_yf_message,), daemon=True)
    yf_thread.start()
    time.sleep(0.5)
    print("  yfinance: WebSocket started")

    # Schwab LEVELONE_EQUITIES
    schwab_handle = start_schwab_stream(symbols, schwab_accs, schwab_lock, schwab_counter)
    schwab_active = schwab_handle is not None

    # -- Collect data --
    for elapsed in range(duration_seconds):
        time.sleep(1)
        if (elapsed + 1) % 15 == 0:
            with yf_lock:
                yf_total = sum(a.update_count for a in yf_accs.values())
            schwab_total = sum(a.update_count for a in schwab_accs.values()) if schwab_active else 0
            parts = [f"yf={yf_total}"]
            if schwab_active:
                parts.append(f"schwab={schwab_total}")
            print(f"  {elapsed + 1}s: {', '.join(parts)} updates")

    end_time = datetime.now(timezone.utc)

    # -- Stop streams --
    ws.close()
    if schwab_handle:
        schwab_handle[0].stop()
    time.sleep(0.5)

    print(f"  End: {end_time.strftime('%H:%M:%S')} UTC")

    # ── Results ────────────────────────────────────────────────────
    sources = [("yfinance WebSocket", yf_accs)]
    if schwab_active:
        sources.append(("Schwab LEVELONE_EQUITIES", schwab_accs))

    for label, accs in sources:
        print(f"\n{'='*80}")
        print(f"STREAMING RESULTS: {label}")
        print(f"{'='*80}")
        print_results(label, accs, symbols)

    # ── Tick logs for first symbol ─────────────────────────────────
    for label, accs in sources:
        print_tick_log(label, accs, symbols[0])

    # ── Comparison ─────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"COMPARISON: All Sources")
    print(f"{'='*80}")

    for symbol in symbols:
        # Fetch today's 1-min bars
        df = yf.download(symbol, period="1d", interval="1m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 3:
            print(f"\n{symbol}: insufficient bar data ({len(df)} bars)")
            continue

        bar_result = interbar_tick_rule(df)
        bar_uptick = bar_result["uptick_vol"].sum()
        bar_downtick = bar_result["downtick_vol"].sum()
        bar_delta = bar_uptick - bar_downtick
        bar_imbalance = bar_delta / (bar_uptick + bar_downtick) if (bar_uptick + bar_downtick) > 0 else 0
        bar_price_pct = (df["Close"].iloc[-1] - df["Close"].iloc[0]) / df["Close"].iloc[0] * 100

        print(f"\n{symbol} ({len(df)} bars today, price {bar_price_pct:+.3f}%):")
        print(f"  {'Source':<28} {'Uptick Vol':>14} {'Downtick Vol':>14} {'Net Delta':>14} {'Imbalance':>10}")
        print(f"  {'-'*82}")

        # Print each source
        row_data = []
        for label, accs in sources:
            acc = accs[symbol]
            if acc.update_count < 2:
                continue
            short = label.split()[0]  # "yfinance" or "Schwab"
            direction = "BULL" if acc.net_delta > 0 else "BEAR" if acc.net_delta < 0 else "FLAT"
            print(
                f"  {short + ' stream':<28} "
                f"{acc.uptick_vol:>14,} "
                f"{acc.downtick_vol:>14,} "
                f"{acc.net_delta:>14,} "
                f"{acc.imbalance:>10.4f}  [{direction}]"
            )
            row_data.append((short, acc.imbalance, direction))

        bar_dir = "BULL" if bar_delta > 0 else "BEAR" if bar_delta < 0 else "FLAT"
        print(
            f"  {'1-min bars (interbar_tick)':<28} "
            f"{bar_uptick:>14,.0f} "
            f"{bar_downtick:>14,.0f} "
            f"{bar_delta:>14,.0f} "
            f"{bar_imbalance:>10.4f}  [{bar_dir}]"
        )

        # Check direction agreement
        directions = [d for _, _, d in row_data] + [bar_dir]
        if len(set(directions)) == 1:
            print(f"  --> All sources AGREE: {directions[0]}")
        else:
            print(f"  --> Sources DISAGREE: {directions}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Real-time uptick/downtick volume test")
    parser.add_argument("--seconds", type=int, default=120, help="Duration to stream (default: 120)")
    parser.add_argument("--symbols", nargs="+", default=["SPY", "AAPL"], help="Symbols to stream")
    args = parser.parse_args()

    run_streaming_test(args.symbols, args.seconds)
