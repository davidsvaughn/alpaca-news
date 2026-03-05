"""Live test: shadow collector vs bar-based volume delta.

Runs during market hours. Starts a Schwab LEVELONE_EQUITIES stream with the
VolumeDeltaCollector attached, collects tick-level data for N seconds, then
compares against 1-minute bar-based inter-bar tick rule (the backtest method).

Run:
    uv run python tests/test_shadow_collector.py [--seconds 120] [--symbols SPY AAPL TSLA]
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.market.volume_delta_shadow import VolumeDeltaCollector, TickAccumulator


def interbar_tick_rule_from_bars(bars: list[dict]) -> dict:
    """Compute volume delta using inter-bar tick rule on OHLCV bars."""
    if len(bars) < 2:
        return {"uptick": 0, "downtick": 0, "net_delta": 0, "imbalance": 0.0}

    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]

    uptick = 0
    downtick = 0
    last_dir = 0
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            direction = 1
            last_dir = 1
        elif diff < 0:
            direction = -1
            last_dir = -1
        else:
            direction = last_dir  # carry forward

        if direction > 0:
            uptick += volumes[i]
        elif direction < 0:
            downtick += volumes[i]

    total = uptick + downtick
    return {
        "uptick": uptick,
        "downtick": downtick,
        "net_delta": uptick - downtick,
        "imbalance": (uptick - downtick) / total if total > 0 else 0.0,
    }


def run_schwab_shadow_test(symbols: list[str], duration_seconds: int = 120):
    """Run Schwab streaming with shadow collector, then compare."""

    print(f"\n{'='*80}")
    print(f"  SHADOW COLLECTOR LIVE TEST")
    print(f"  Schwab tick-level vs 1-minute bar inter-bar tick rule")
    print(f"{'='*80}")
    print(f"  Symbols: {symbols}")
    print(f"  Duration: {duration_seconds}s")
    print(f"  Start: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")

    # Check Schwab credentials
    app_key = os.getenv("SCHWAB_APP_KEY")
    app_secret = os.getenv("SCHWAB_APP_SECRET")
    if not app_key or not app_secret:
        print("\n  ERROR: SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set")
        print("  Set these in your .env file")
        return

    # Create collector
    collector = VolumeDeltaCollector()
    collector.start()
    for sym in symbols:
        collector.add_symbol(sym)

    # Also run yfinance streaming for comparison
    yf_accumulators: dict[str, TickAccumulator] = {}
    yf_lock = threading.Lock()
    for sym in symbols:
        yf_accumulators[sym] = TickAccumulator()

    def on_yf_message(msg):
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
            if symbol in yf_accumulators:
                yf_accumulators[symbol].on_update(float(price), day_vol, ts)

    # Start Schwab stream
    print("\n  Starting Schwab stream...")
    try:
        import schwabdev
        client = schwabdev.Client(app_key, app_secret)
        streamer = schwabdev.Stream(client)

        schwab_update_count = [0]
        schwab_lock = threading.Lock()

        # Raw Schwab accumulators for direct comparison
        schwab_raw_accs: dict[str, TickAccumulator] = {}
        for sym in symbols:
            schwab_raw_accs[sym] = TickAccumulator()

        def on_schwab_message(message):
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
                    price = content.get("3")
                    vol = content.get("8")
                    if price is not None and vol is not None:
                        price = float(price)
                        vol = int(vol)
                        # Feed shadow collector
                        collector.on_stream_update(symbol, price, vol, ts)
                        # Feed raw accumulator
                        with schwab_lock:
                            if symbol in schwab_raw_accs:
                                schwab_raw_accs[symbol].on_update(price, vol, ts)
                                schwab_update_count[0] += 1

        streamer.start(receiver=on_schwab_message)
        time.sleep(1)
        streamer.send(
            streamer.level_one_equities(
                keys=",".join(symbols),
                fields="0,3,8,9",  # symbol, last_price, total_volume, last_size
            )
        )
        print("  Schwab stream started")
        schwab_active = True
    except Exception as e:
        print(f"  Schwab stream failed: {e}")
        schwab_active = False

    # Start yfinance stream
    print("  Starting yfinance stream...")
    try:
        ws = yf.WebSocket(verbose=False)
        ws.subscribe(symbols)
        time.sleep(0.5)
        yf_thread = threading.Thread(target=ws.listen, args=(on_yf_message,), daemon=True)
        yf_thread.start()
        print("  yfinance stream started")
        yf_active = True
    except Exception as e:
        print(f"  yfinance stream failed: {e}")
        yf_active = False

    # Collect data
    print(f"\n  Collecting data for {duration_seconds}s...")
    for elapsed in range(duration_seconds):
        time.sleep(1)
        if (elapsed + 1) % 30 == 0:
            parts = []
            if schwab_active:
                parts.append(f"schwab={schwab_update_count[0]}")
            if yf_active:
                yf_total = sum(a.update_count for a in yf_accumulators.values())
                parts.append(f"yf={yf_total}")
            collector_total = sum(
                (collector.snapshot(s) or {}).get("update_count", 0) for s in symbols
            )
            parts.append(f"collector={collector_total}")
            print(f"    {elapsed + 1}s: {', '.join(parts)} updates")

    # Stop streams
    if yf_active:
        ws.close()
    if schwab_active:
        streamer.stop()
    time.sleep(0.5)

    print(f"\n  Collection complete at {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")

    # ── Fetch bar-based comparison data ──────────────────────────
    print("\n  Fetching today's 1-min bars from yfinance for comparison...")

    bar_results = {}
    for sym in symbols:
        df = yf.download(sym, period="1d", interval="1m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 3:
            print(f"    {sym}: insufficient bar data ({len(df)} bars)")
            continue

        bars = [
            {"c": row["Close"], "v": row["Volume"]}
            for _, row in df.iterrows()
        ]
        bar_results[sym] = interbar_tick_rule_from_bars(bars)
        bar_results[sym]["bar_count"] = len(bars)

    # ── Results ──────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  RESULTS: ALL SOURCES COMPARED")
    print(f"{'='*80}")

    for sym in symbols:
        print(f"\n  {sym}:")
        print(f"  {'Source':<30} {'Updates':>8} {'Uptick':>14} {'Downtick':>14} {'Net Delta':>14} {'Imbalance':>10} {'Dir':<6}")
        print(f"  {'-'*100}")

        # Schwab collector (tick-level)
        snap = collector.snapshot(sym)
        if snap and snap["update_count"] > 1:
            print(
                f"  {'Schwab tick (collector)':<30} "
                f"{snap['update_count']:>8} "
                f"{snap['uptick_vol']:>14,} "
                f"{snap['downtick_vol']:>14,} "
                f"{snap['net_delta']:>14,} "
                f"{snap['imbalance']:>10.4f} "
                f"{snap['direction']:<6}"
            )

        # Schwab raw accumulator (direct comparison)
        if schwab_active:
            acc = schwab_raw_accs.get(sym)
            if acc and acc.update_count > 1:
                print(
                    f"  {'Schwab tick (raw)':<30} "
                    f"{acc.update_count:>8} "
                    f"{acc.uptick_vol:>14,} "
                    f"{acc.downtick_vol:>14,} "
                    f"{acc.net_delta:>14,} "
                    f"{acc.imbalance:>10.4f} "
                    f"{acc.direction_label:<6}"
                )

        # yfinance streaming
        if yf_active:
            yf_acc = yf_accumulators.get(sym)
            if yf_acc and yf_acc.update_count > 1:
                print(
                    f"  {'yfinance tick (stream)':<30} "
                    f"{yf_acc.update_count:>8} "
                    f"{yf_acc.uptick_vol:>14,} "
                    f"{yf_acc.downtick_vol:>14,} "
                    f"{yf_acc.net_delta:>14,} "
                    f"{yf_acc.imbalance:>10.4f} "
                    f"{yf_acc.direction_label:<6}"
                )

        # Bar-based (backtest method)
        if sym in bar_results:
            br = bar_results[sym]
            direction = "BULL" if br["net_delta"] > 0 else "BEAR" if br["net_delta"] < 0 else "FLAT"
            print(
                f"  {'1-min bars (backtest)':<30} "
                f"{br['bar_count']:>8} "
                f"{br['uptick']:>14,} "
                f"{br['downtick']:>14,} "
                f"{br['net_delta']:>14,} "
                f"{br['imbalance']:>10.4f} "
                f"{direction:<6}"
            )

        # Collector minute bars (tick-level, aggregated to 1-min)
        collector_bars = collector.get_minute_bars(sym)
        if collector_bars:
            cb_result = interbar_tick_rule_from_bars(collector_bars)
            cb_tick_up = sum(b["uptick"] for b in collector_bars)
            cb_tick_dn = sum(b["downtick"] for b in collector_bars)
            cb_tick_delta = cb_tick_up - cb_tick_dn
            cb_tick_total = cb_tick_up + cb_tick_dn
            cb_tick_imb = cb_tick_delta / cb_tick_total if cb_tick_total > 0 else 0
            direction = "BULL" if cb_tick_delta > 0 else "BEAR" if cb_tick_delta < 0 else "FLAT"
            print(
                f"  {'Collector bars (tick agg)':<30} "
                f"{len(collector_bars):>8} "
                f"{cb_tick_up:>14,} "
                f"{cb_tick_dn:>14,} "
                f"{cb_tick_delta:>14,} "
                f"{cb_tick_imb:>10.4f} "
                f"{direction:<6}"
            )

    # ── Direction agreement analysis ─────────────────────────────
    print(f"\n{'='*80}")
    print(f"  DIRECTION AGREEMENT ANALYSIS")
    print(f"{'='*80}")
    for sym in symbols:
        snap = collector.snapshot(sym)
        br = bar_results.get(sym)
        if not snap or not br or snap["update_count"] < 2:
            continue

        tick_dir = snap["direction"]
        bar_dir = "BULL" if br["net_delta"] > 0 else "BEAR" if br["net_delta"] < 0 else "FLAT"
        agree = "AGREE" if tick_dir == bar_dir else "DISAGREE"

        tick_imb = snap["imbalance"]
        bar_imb = br["imbalance"]
        imb_diff = abs(tick_imb - bar_imb)

        print(f"  {sym}: tick={tick_dir} bar={bar_dir} → {agree}")
        print(f"    Imbalance: tick={tick_imb:+.4f} bar={bar_imb:+.4f} diff={imb_diff:.4f}")

    # ── VDD signal check ─────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  VDD SIGNAL STATUS (lookback=80)")
    print(f"{'='*80}")
    for sym in symbols:
        vdd = collector.check_vdd_signal(sym, lookback=80)
        if vdd.get("reason") == "insufficient_bars":
            print(f"  {sym}: {vdd['bars']} bars (need 81+ for VDD check)")
        elif vdd["signal"]:
            print(f"  {sym}: *** VDD SIGNAL ACTIVE ***")
            print(f"    Price: {vdd['current_close']:.2f} >= prev max {vdd['prev_max']:.2f}")
            print(f"    Delta: {vdd['current_cum_delta']:,.0f} < lagged {vdd['lagged_cum_delta']:,.0f}")
        else:
            parts = []
            if not vdd.get("price_new_high"):
                parts.append("price NOT at new high")
            if not vdd.get("delta_declining"):
                parts.append("delta NOT declining")
            print(f"  {sym}: No signal ({', '.join(parts)})")

    # ── Save shadow data ─────────────────────────────────────────
    print(f"\n  Saving shadow data...")
    paths = collector.save_all()
    for p in paths:
        print(f"    Saved: {p}")

    # ── Update frequency stats ───────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  UPDATE FREQUENCY ANALYSIS")
    print(f"{'='*80}")
    for sym in symbols:
        if schwab_active:
            acc = schwab_raw_accs.get(sym)
            if acc and len(acc._tick_log) >= 2:
                times = [t["ts"] for t in acc._tick_log]
                intervals = np.diff(times)
                total_time = times[-1] - times[0]
                print(f"\n  {sym} (Schwab):")
                print(f"    Updates: {len(acc._tick_log)}")
                print(f"    Mean interval: {np.mean(intervals):.3f}s")
                print(f"    Median interval: {np.median(intervals):.3f}s")
                print(f"    Min/Max: {np.min(intervals):.3f}s / {np.max(intervals):.3f}s")
                if total_time > 0:
                    print(f"    Updates/min: {len(acc._tick_log) / (total_time / 60):.1f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shadow collector live test")
    parser.add_argument("--seconds", type=int, default=180, help="Duration (default: 180)")
    parser.add_argument("--symbols", nargs="+", default=["SPY", "AAPL", "TSLA", "NVDA", "AMZN"],
                        help="Symbols to stream")
    args = parser.parse_args()

    run_schwab_shadow_test(args.symbols, args.seconds)
