"""Main collector: Schwab stream → buffer → TimescaleDB."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone

from .buffer import TradeBuffer
from .classifier import TickClassifier
from .config import CollectorConfig
from .db import Trade, connect, insert_trades

log = logging.getLogger(__name__)

# L1 fields: original set + Tier 2 fields (9, 16, 35, 41)
L1_FIELDS = "0,1,2,3,4,5,8,9,10,11,12,16,17,18,33,35,41,42"


class TickCollector:
    """Streams Schwab L1 (and optionally TIMESALE) data into TimescaleDB.

    Architecture:
    - schwabdev Stream runs its callback on a background thread
    - Callback parses messages → Trade objects → thread-safe buffer
    - Async flush loop drains buffer → batch INSERT to TimescaleDB
    """

    def __init__(self, config: CollectorConfig) -> None:
        self.config = config
        self.buffer = TradeBuffer(max_batch=config.flush_batch_size)
        self.classifier = TickClassifier()

        # Per-symbol L1 state for volume differencing
        self._prev_total_volume: dict[str, int] = {}

        # schwabdev objects (set during run)
        self._stream = None
        self._pool = None
        self._stop = asyncio.Event()
        self._timesale_available = False

        # Stats
        self._start_time: float | None = None
        self._l1_count = 0
        self._ts_count = 0
        self._flush_count = 0
        self._last_message_time: float = 0

    # ------------------------------------------------------------------
    # Message parsing (runs on schwabdev's background thread)
    # ------------------------------------------------------------------

    def _on_message(self, message) -> None:
        """Handle all WebSocket messages from schwabdev."""
        try:
            if isinstance(message, str):
                message = json.loads(message)
            if not isinstance(message, dict):
                return

            # Subscription responses
            if "response" in message:
                for resp in message["response"]:
                    service = resp.get("service", "?")
                    command = resp.get("command", "?")
                    code = resp.get("content", {}).get("code", "?")
                    msg = resp.get("content", {}).get("msg", "")
                    log.info("[%s] %s → code=%s %s", service, command, code, msg)
                    if service == "TIMESALE_EQUITY" and code == 0:
                        self._timesale_available = True
                    elif service == "TIMESALE_EQUITY" and code != 0:
                        log.warning("TIMESALE_EQUITY unavailable (code=%s), using L1 only", code)
                return

            self._last_message_time = time.time()

            for data in message.get("data", []):
                service = data.get("service")
                if service == "LEVELONE_EQUITIES":
                    self._parse_l1(data)
                elif service == "TIMESALE_EQUITY":
                    self._parse_timesale(data)

        except Exception:
            log.exception("Error in message handler")

    def _parse_l1(self, data: dict) -> None:
        """Parse LEVELONE_EQUITIES update into Trade records."""
        for content in data.get("content", []):
            symbol = content.get("key", "")
            if not symbol:
                continue

            last_price = content.get("3")  # Last Price
            total_volume = content.get("8")  # Total Volume
            last_size = content.get("9")  # Last Size
            trade_time_ms = content.get("35")  # Trade Time in Long
            last_mic_id = content.get("41")  # Last MIC ID

            # Skip if no trade data
            if last_price is None or last_size is None:
                continue

            # Volume differencing: compute volume_delta from total_volume
            volume_delta = None
            if total_volume is not None:
                prev = self._prev_total_volume.get(symbol)
                self._prev_total_volume[symbol] = int(total_volume)
                if prev is not None:
                    volume_delta = int(total_volume) - prev
                    if volume_delta <= 0:
                        return  # no new trades

            # Timestamp from trade_time_ms or fallback to now
            if trade_time_ms is not None:
                trade_time = datetime.fromtimestamp(
                    trade_time_ms / 1000, tz=timezone.utc
                )
            else:
                trade_time = datetime.now(timezone.utc)

            direction = self.classifier.classify(symbol, float(last_price))

            trade = Trade(
                time=trade_time,
                symbol=symbol,
                price=float(last_price),
                size=int(last_size),
                exchange=str(last_mic_id) if last_mic_id is not None else None,
                direction=direction,
                source="L1",
                volume_delta=volume_delta,
                total_volume=int(total_volume) if total_volume is not None else None,
            )
            self.buffer.append(trade)
            self._l1_count += 1

    def _parse_timesale(self, data: dict) -> None:
        """Parse TIMESALE_EQUITY messages into Trade records."""
        for content in data.get("content", []):
            symbol = content.get("key", content.get("0", ""))
            if not symbol:
                continue

            time_ms = content.get("1")
            price = content.get("2")
            size = content.get("3")

            if price is None or size is None:
                continue

            if time_ms is not None:
                trade_time = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc)
            else:
                trade_time = datetime.now(timezone.utc)

            direction = self.classifier.classify(symbol, float(price))

            trade = Trade(
                time=trade_time,
                symbol=symbol,
                price=float(price),
                size=int(size),
                exchange=None,  # TIMESALE doesn't provide exchange in fields 0-4
                direction=direction,
                source="TS",
            )
            self.buffer.append(trade)
            self._ts_count += 1

    # ------------------------------------------------------------------
    # Async flush loop
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Periodically drain the buffer and batch-insert to TimescaleDB."""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.config.flush_interval_sec)
                batch = self.buffer.drain()
                if batch and self._pool:
                    inserted = await insert_trades(self._pool, batch)
                    self._flush_count += inserted
                    if inserted > 0:
                        log.debug("Flushed %d trades (pending=%d)", inserted, self.buffer.pending)
            except Exception:
                log.exception("Flush error")

    async def _status_loop(self) -> None:
        """Log status every 60 seconds."""
        while not self._stop.is_set():
            await asyncio.sleep(60)
            elapsed = time.time() - (self._start_time or time.time())
            stats = self.buffer.stats
            log.info(
                "Status [%.0fs]: L1=%d TS=%d flushed=%d pending=%d",
                elapsed, self._l1_count, self._ts_count,
                self._flush_count, stats["pending"],
            )

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the collector. Blocks until stopped via signal."""
        cfg = self.config

        if not cfg.schwab_app_key or not cfg.schwab_app_secret:
            raise RuntimeError("Missing SCHWAB_APP_KEY / SCHWAB_APP_SECRET")
        if not cfg.symbols:
            raise RuntimeError("No symbols configured")

        log.info("Starting tick collector with %d symbols", len(cfg.symbols))

        # Connect to TimescaleDB
        self._pool = await connect(cfg.dsn)

        # Start schwabdev stream
        import schwabdev

        client = schwabdev.Client(cfg.schwab_app_key, cfg.schwab_app_secret)
        self._stream = schwabdev.Stream(client)
        self._stream.start(receiver=self._on_message)
        await asyncio.sleep(1)  # let WebSocket connect

        if not self._stream.active:
            raise RuntimeError("Stream failed to connect")
        log.info("WebSocket connected")

        keys_str = ",".join(cfg.symbols)
        self._start_time = time.time()

        # Subscribe to LEVELONE_EQUITIES with extended fields (always)
        l1_req = self._stream.level_one_equities(keys=cfg.symbols, fields=L1_FIELDS)
        log.info("Subscribing LEVELONE_EQUITIES: %s", keys_str)
        self._stream.send(l1_req)

        # Also try TIMESALE_EQUITY (may not be available)
        ts_req = self._stream.basic_request(
            service="TIMESALE_EQUITY",
            command="ADD",
            parameters={"keys": keys_str, "fields": "0,1,2,3,4"},
        )
        log.info("Subscribing TIMESALE_EQUITY: %s", keys_str)
        self._stream.send(ts_req)

        # Set up signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal)

        # Run flush + status loops until stopped
        log.info("Collector running. Press Ctrl+C to stop.")
        await asyncio.gather(
            self._flush_loop(),
            self._status_loop(),
        )

        # Shutdown
        await self._shutdown()

    def _handle_signal(self) -> None:
        log.info("Shutdown signal received")
        self._stop.set()

    async def _shutdown(self) -> None:
        """Flush remaining buffer and clean up."""
        log.info("Shutting down...")

        # Final flush
        batch = self.buffer.drain()
        if batch and self._pool:
            inserted = await insert_trades(self._pool, batch)
            log.info("Final flush: %d trades", inserted)

        # Stop stream
        if self._stream:
            try:
                self._stream.stop(clear_subscriptions=True)
            except Exception:
                log.exception("Error stopping stream")

        # Close DB pool
        if self._pool:
            await self._pool.close()

        elapsed = time.time() - (self._start_time or time.time())
        log.info(
            "Stopped after %.0fs. Total: L1=%d TS=%d flushed=%d",
            elapsed, self._l1_count, self._ts_count, self._flush_count,
        )
