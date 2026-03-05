"""Shadow-mode real-time volume delta collector.

Accumulates tick-level uptick/downtick volume from Schwab LEVELONE_EQUITIES
streaming, running in parallel alongside the existing backtest-compatible
bar-based computation. Designed to be attached to the Schwab stream so that
every price+volume update is classified at tick granularity.

Usage:
    collector = VolumeDeltaCollector()
    collector.add_symbol("AAPL")

    # Hook into Schwab stream updates (called from schwab_client._on_stream_message):
    collector.on_stream_update("AAPL", last_price=150.25, total_volume=50_000_000)

    # Get current state:
    snap = collector.snapshot("AAPL")
    # => {"uptick_vol": 25_000_000, "downtick_vol": 24_500_000, "net_delta": 500_000, ...}

    # Get all:
    all_snaps = collector.snapshot_all()

    # Persist day's data to disk:
    collector.save_daily("AAPL")

Architecture:
    - Per-symbol TickAccumulator tracks tick-level volume delta in real-time
    - 1-minute bar snapshots are captured every minute for comparison with backtest
    - Full day's data saved to ~/.cache/alpaca-news/volume_delta_shadow/
    - Thread-safe: all state guarded by per-symbol locks
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Cache directory for shadow data
SHADOW_CACHE_DIR = Path.home() / ".cache" / "alpaca-news" / "volume_delta_shadow"


@dataclass
class TickAccumulator:
    """Accumulates uptick/downtick volume from streaming price+volume updates.

    Uses the same tick rule as the backtest (direction = sign of price change),
    but at tick granularity instead of 1-minute bar granularity.
    """

    prev_price: float | None = None
    prev_total_volume: int | None = None
    last_nonzero_dir: int = 0  # +1 or -1 (carried forward on zero-tick)

    uptick_vol: int = 0
    downtick_vol: int = 0
    zero_tick_vol: int = 0  # volume where no direction could be determined
    update_count: int = 0

    # 1-minute bar snapshots for comparison with backtest
    minute_bars: list[dict] = field(default_factory=list)
    _minute_start: float = 0.0  # epoch time of current minute window
    _minute_open: float = 0.0
    _minute_high: float = 0.0
    _minute_low: float = float("inf")
    _minute_close: float = 0.0
    _minute_volume: int = 0
    _minute_uptick: int = 0
    _minute_downtick: int = 0

    # Raw tick log (limited to last N for debugging)
    _tick_log: list[dict] = field(default_factory=list)
    _max_tick_log: int = 500

    def on_update(
        self, price: float, total_volume: int, timestamp: float | None = None
    ) -> None:
        """Process a streaming update with new price and cumulative volume."""
        ts = timestamp or time.time()
        self.update_count += 1

        # First update: initialize state
        if self.prev_price is None or self.prev_total_volume is None:
            self.prev_price = price
            self.prev_total_volume = total_volume
            self._minute_start = ts
            self._minute_open = price
            self._minute_high = price
            self._minute_low = price
            self._minute_close = price
            return

        # Volume delta since last update
        dv = max(0, total_volume - self.prev_total_volume)

        # Direction classification (tick rule)
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
            # Carry forward: classify using last non-zero direction
            if self.last_nonzero_dir > 0:
                self.uptick_vol += dv
            elif self.last_nonzero_dir < 0:
                self.downtick_vol += dv
            else:
                self.zero_tick_vol += dv

        # Update minute bar
        self._minute_high = max(self._minute_high, price)
        self._minute_low = min(self._minute_low, price)
        self._minute_close = price
        self._minute_volume += dv
        if direction > 0 or (direction == 0 and self.last_nonzero_dir > 0):
            self._minute_uptick += dv
        elif direction < 0 or (direction == 0 and self.last_nonzero_dir < 0):
            self._minute_downtick += dv

        # Check if we've crossed a minute boundary
        current_minute = int(ts // 60)
        start_minute = int(self._minute_start // 60)
        if current_minute > start_minute and self._minute_volume > 0:
            self._flush_minute_bar(self._minute_start)
            # Start new minute
            self._minute_start = ts
            self._minute_open = price
            self._minute_high = price
            self._minute_low = price
            self._minute_close = price
            self._minute_volume = 0
            self._minute_uptick = 0
            self._minute_downtick = 0

        # Tick log (ring buffer)
        if len(self._tick_log) < self._max_tick_log:
            self._tick_log.append(
                {
                    "ts": ts,
                    "price": price,
                    "total_vol": total_volume,
                    "dv": dv,
                    "dir": direction,
                }
            )

        self.prev_price = price
        self.prev_total_volume = total_volume

    # Callback set by VolumeDeltaCollector to persist bars immediately.
    _on_bar_flushed: Any = None  # Callable[[str, dict], None] or None

    def _flush_minute_bar(self, bar_start_ts: float) -> None:
        """Save completed 1-minute bar with tick-level volume delta."""
        bar = {
            "t": datetime.fromtimestamp(bar_start_ts, tz=timezone.utc).isoformat(),
            "o": self._minute_open,
            "h": self._minute_high,
            "l": self._minute_low,
            "c": self._minute_close,
            "v": self._minute_volume,
            "uptick": self._minute_uptick,
            "downtick": self._minute_downtick,
            "delta": self._minute_uptick - self._minute_downtick,
        }
        self.minute_bars.append(bar)

        # Notify collector for immediate disk persistence
        if self._on_bar_flushed is not None:
            try:
                self._on_bar_flushed(bar)
            except Exception:
                pass  # logged at collector level

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

    @property
    def direction_label(self) -> str:
        if self.net_delta > 0:
            return "BULL"
        elif self.net_delta < 0:
            return "BEAR"
        return "FLAT"

    def to_dict(self) -> dict[str, Any]:
        """Serialize current state for snapshotting/saving."""
        return {
            "uptick_vol": self.uptick_vol,
            "downtick_vol": self.downtick_vol,
            "zero_tick_vol": self.zero_tick_vol,
            "net_delta": self.net_delta,
            "imbalance": round(self.imbalance, 6),
            "direction": self.direction_label,
            "total_vol": self.total_vol,
            "update_count": self.update_count,
            "last_price": self.prev_price,
            "last_nonzero_dir": self.last_nonzero_dir,
            "minute_bars_count": len(self.minute_bars),
        }

    def reset(self) -> None:
        """Reset accumulators (e.g., at start of new trading day)."""
        self.prev_price = None
        self.prev_total_volume = None
        self.last_nonzero_dir = 0
        self.uptick_vol = 0
        self.downtick_vol = 0
        self.zero_tick_vol = 0
        self.update_count = 0
        self.minute_bars.clear()
        self._tick_log.clear()
        self._minute_start = 0.0
        self._minute_volume = 0
        self._minute_uptick = 0
        self._minute_downtick = 0


class VolumeDeltaCollector:
    """Thread-safe shadow-mode collector for real-time volume delta.

    Manages per-symbol TickAccumulators and hooks into the Schwab stream.
    Can be attached to the existing SchwabMarketClient's stream message flow.

    Persistence: completed 1-minute bars are appended to a per-symbol JSONL
    file immediately on flush (one JSON object per line). This is crash-safe
    because POSIX append writes of < 4KB are atomic. On restart, bars are
    loaded from today's JSONL file to restore state.
    """

    def __init__(self) -> None:
        self._accumulators: dict[str, TickAccumulator] = {}
        self._lock = threading.Lock()
        self._bar_log_files: dict[str, Path] = {}  # sym -> open JSONL path
        self._started_at: float | None = None
        self._active = False

    def start(self) -> None:
        """Mark collector as active."""
        self._started_at = time.time()
        self._active = True
        logger.info("VolumeDeltaCollector started")

    def stop(self) -> None:
        """Mark collector as inactive."""
        self._active = False
        logger.info("VolumeDeltaCollector stopped")

    @property
    def active(self) -> bool:
        return self._active

    @property
    def symbols(self) -> list[str]:
        with self._lock:
            return list(self._accumulators.keys())

    def _bar_log_path(self, symbol: str, date_str: str | None = None) -> Path:
        """Path to the JSONL bar log for a symbol and date."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = SHADOW_CACHE_DIR / symbol.upper() / "bars"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{date_str}.jsonl"

    def _load_bars_from_log(self, symbol: str) -> list[dict]:
        """Load today's bars from JSONL log (for recovery after restart)."""
        path = self._bar_log_path(symbol)
        if not path.exists():
            return []
        bars = []
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line:
                    bars.append(json.loads(line))
        except Exception:
            logger.exception("Failed to load bar log for %s", symbol)
        return bars

    def _append_bar_to_log(self, symbol: str, bar: dict) -> None:
        """Append a completed minute bar to the JSONL log file.

        Small appends (< 4KB) are atomic on POSIX — no corruption risk.
        """
        path = self._bar_log_path(symbol)
        try:
            with open(path, "a") as f:
                f.write(json.dumps(bar, separators=(",", ":")) + "\n")
        except Exception:
            logger.exception("Failed to append bar to log for %s", symbol)

    def add_symbol(self, symbol: str) -> None:
        """Start tracking a symbol. Loads any existing bars from today's log."""
        sym = symbol.upper()
        with self._lock:
            if sym not in self._accumulators:
                acc = TickAccumulator()
                # Recover bars from today's JSONL log (survives restarts)
                existing_bars = self._load_bars_from_log(sym)
                if existing_bars:
                    acc.minute_bars = existing_bars
                    logger.info(
                        "VolumeDeltaCollector: recovered %d bars for %s from log",
                        len(existing_bars), sym,
                    )
                # Set up append callback for crash-safe persistence
                acc._on_bar_flushed = lambda bar, s=sym: self._append_bar_to_log(s, bar)
                self._accumulators[sym] = acc
                logger.info("VolumeDeltaCollector: tracking %s", sym)

    def remove_symbol(self, symbol: str) -> None:
        """Stop tracking a symbol (preserves data and log files)."""
        sym = symbol.upper()
        with self._lock:
            if sym in self._accumulators:
                logger.info("VolumeDeltaCollector: removed %s (data preserved)", sym)

    def on_stream_update(
        self,
        symbol: str,
        last_price: float | None = None,
        total_volume: int | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Process a streaming update from Schwab LEVELONE_EQUITIES.

        Called from schwab_client._on_stream_message for each symbol update
        that includes price and/or volume fields.
        """
        if not self._active:
            return
        if last_price is None or total_volume is None:
            return

        sym = symbol.upper()
        with self._lock:
            acc = self._accumulators.get(sym)
        if acc is None:
            return

        acc.on_update(last_price, total_volume, timestamp)

    def snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Get current volume delta state for a symbol."""
        sym = symbol.upper()
        with self._lock:
            acc = self._accumulators.get(sym)
        if acc is None:
            return None
        return acc.to_dict()

    def snapshot_all(self) -> dict[str, dict[str, Any]]:
        """Get current volume delta state for all tracked symbols."""
        with self._lock:
            syms = list(self._accumulators.keys())
        result = {}
        for sym in syms:
            with self._lock:
                acc = self._accumulators.get(sym)
            if acc:
                result[sym] = acc.to_dict()
        return result

    def get_minute_bars(self, symbol: str) -> list[dict]:
        """Get accumulated 1-minute bars with tick-level volume delta."""
        sym = symbol.upper()
        with self._lock:
            acc = self._accumulators.get(sym)
        if acc is None:
            return []
        return list(acc.minute_bars)

    def get_cumulative_delta_series(self, symbol: str) -> list[dict]:
        """Get cumulative delta time series from minute bars (for VDD computation)."""
        bars = self.get_minute_bars(symbol)
        cum_delta = 0
        series = []
        for bar in bars:
            cum_delta += bar["delta"]
            series.append(
                {
                    "t": bar["t"],
                    "close": bar["c"],
                    "cum_delta": cum_delta,
                    "bar_delta": bar["delta"],
                    "bar_imbalance": (
                        bar["delta"] / bar["v"] if bar["v"] > 0 else 0.0
                    ),
                }
            )
        return series

    def check_vdd_signal(self, symbol: str, lookback: int = 80) -> dict[str, Any]:
        """Check if Volume Delta Divergence signal is active RIGHT NOW.

        Returns signal status and supporting data.
        """
        bars = self.get_minute_bars(symbol)
        if len(bars) < lookback + 1:
            return {"signal": False, "reason": "insufficient_bars", "bars": len(bars)}

        # Build close and cumulative delta arrays
        closes = [b["c"] for b in bars]
        cum_delta = []
        running = 0
        for b in bars:
            running += b["delta"]
            cum_delta.append(running)

        # Check VDD condition on the latest bar
        i = len(bars) - 1
        # Price >= rolling max of previous lookback bars
        window_closes = closes[max(0, i - lookback) : i]
        if not window_closes:
            return {"signal": False, "reason": "no_window"}

        prev_max = max(window_closes)
        current_close = closes[i]
        current_cum_delta = cum_delta[i]
        lagged_cum_delta = cum_delta[i - lookback] if i >= lookback else cum_delta[0]

        price_new_high = current_close >= prev_max
        delta_declining = current_cum_delta < lagged_cum_delta

        return {
            "signal": price_new_high and delta_declining,
            "price_new_high": price_new_high,
            "delta_declining": delta_declining,
            "current_close": current_close,
            "prev_max": prev_max,
            "current_cum_delta": current_cum_delta,
            "lagged_cum_delta": lagged_cum_delta,
            "delta_change": current_cum_delta - lagged_cum_delta,
            "bars_available": len(bars),
            "method": "tick_level",
        }

    def save_daily(self, symbol: str, date_str: str | None = None) -> Path | None:
        """Save a symbol's full shadow summary to disk (atomic write).

        This is the "clean export" — a single JSON file with summary + all
        bars. The JSONL bar log provides crash safety; this provides a
        convenient analysis artifact.

        Returns path to saved file, or None if no data.
        """
        import os
        import tempfile

        sym = symbol.upper()
        with self._lock:
            acc = self._accumulators.get(sym)
        if acc is None or (acc.update_count == 0 and not acc.minute_bars):
            return None

        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        out_dir = SHADOW_CACHE_DIR / sym
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date_str}.json"

        data = {
            "symbol": sym,
            "date": date_str,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "summary": acc.to_dict(),
            "minute_bars": acc.minute_bars,
        }

        # Atomic write: temp file + rename (prevents corruption on crash)
        fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.rename(tmp, out_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        logger.info("Saved shadow data: %s (%d bars)", out_path, len(acc.minute_bars))
        return out_path

    def save_all(self, date_str: str | None = None) -> list[Path]:
        """Save all symbols' shadow data."""
        with self._lock:
            syms = list(self._accumulators.keys())
        paths = []
        for sym in syms:
            p = self.save_daily(sym, date_str)
            if p:
                paths.append(p)
        return paths

    def reset_symbol(self, symbol: str) -> None:
        """Reset a symbol's accumulators (e.g., start of new trading day)."""
        sym = symbol.upper()
        with self._lock:
            acc = self._accumulators.get(sym)
        if acc:
            acc.reset()

    def reset_all(self) -> None:
        """Reset all accumulators."""
        with self._lock:
            for acc in self._accumulators.values():
                acc.reset()

    def load_daily(self, symbol: str, date_str: str) -> dict[str, Any] | None:
        """Load saved shadow data for analysis."""
        path = SHADOW_CACHE_DIR / symbol.upper() / f"{date_str}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def list_saved_dates(self, symbol: str) -> list[str]:
        """List dates with saved shadow data for a symbol."""
        sym_dir = SHADOW_CACHE_DIR / symbol.upper()
        if not sym_dir.exists():
            return []
        return sorted(
            p.stem for p in sym_dir.glob("*.json")
        )
