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
from .portfolio import PortfolioSync

log = logging.getLogger(__name__)

# L1 fields: only what we use
# 1=BidPrice, 2=AskPrice, 3=LastPrice, 8=TotalVolume,
# 9=LastSize, 11=TradeTime, 35=TradeTimeLong, 41=LastMICID
L1_FIELDS = "1,2,3,8,9,35,41"


class TickCollector:
    """Streams Schwab L1 data into TimescaleDB.

    Architecture:
    - schwabdev Stream runs its callback on a background thread
    - Callback parses messages → Trade objects → thread-safe buffer
    - Async flush loop drains buffer → batch INSERT to TimescaleDB
    """

    def __init__(self, config: CollectorConfig) -> None:
        self.config = config
        self.buffer = TradeBuffer(max_batch=config.flush_batch_size)
        self.classifier = TickClassifier()

        # Portfolio-aware symbol management
        self._portfolio = PortfolioSync(
            trader_db_path=config.trader_db_path,
            cooloff_minutes=config.portfolio_cooloff_min,
        )

        # Per-symbol L1 state for volume differencing
        self._prev_total_volume: dict[str, int] = {}

        # schwabdev objects (set during run)
        self._stream = None
        self._pool = None
        self._stop = asyncio.Event()

        # Stats
        self._start_time: float | None = None
        self._l1_count = 0
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
                return

            self._last_message_time = time.time()

            received_at = datetime.now(timezone.utc)

            for data in message.get("data", []):
                service = data.get("service")
                if service == "LEVELONE_EQUITIES":
                    self._parse_l1(data, received_at)

        except Exception:
            log.exception("Error in message handler")

    def _parse_l1(self, data: dict, received_at: datetime) -> None:
        """Parse LEVELONE_EQUITIES update into Trade records."""
        stream_ts = data.get("timestamp")  # Schwab message-level timestamp (ms)

        for content in data.get("content", []):
            symbol = content.get("key", "")
            if not symbol:
                continue

            bid_price = content.get("1")  # Bid Price
            ask_price = content.get("2")  # Ask Price
            last_price = content.get("3")  # Last Price
            total_volume = content.get("8")  # Total Volume
            last_size = content.get("9")  # Last Size
            trade_time_ms = content.get("35")  # Trade Time in Long
            last_mic_id = content.get("41")  # Last MIC ID

            # Skip if no trade data or quote-only update (last_size=0)
            if last_price is None or last_size is None or int(last_size) == 0:
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

            # Trade time: prefer field 35, fall back to stream timestamp
            if trade_time_ms is not None:
                trade_time = datetime.fromtimestamp(
                    trade_time_ms / 1000, tz=timezone.utc
                )
            elif stream_ts is not None:
                trade_time = datetime.fromtimestamp(
                    stream_ts / 1000, tz=timezone.utc
                )
            else:
                trade_time = received_at

            direction = self.classifier.classify(
                symbol, float(last_price),
                bid=float(bid_price) if bid_price is not None else None,
                ask=float(ask_price) if ask_price is not None else None,
            )

            trade = Trade(
                time=trade_time,
                received_at=received_at,
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
            n_syms = len(self._portfolio.active_symbols)
            active = self._stream.active if self._stream else False
            log.info(
                "Status [%.0fs]: symbols=%d L1=%d flushed=%d pending=%d stream=%s",
                elapsed, n_syms, self._l1_count,
                self._flush_count, stats["pending"],
                "active" if active else "DEAD",
            )

    # ------------------------------------------------------------------
    # Stream health check
    # ------------------------------------------------------------------

    async def _health_check_loop(self) -> None:
        """Monitor stream health and force-restart if dead."""
        check_interval = 30  # seconds between checks
        dead_threshold = 90  # seconds with no messages before restart
        max_restarts = 10  # give up after this many consecutive restarts

        consecutive_restarts = 0

        while not self._stop.is_set():
            await asyncio.sleep(check_interval)

            if not self._stream or not self._last_message_time:
                continue

            silence = time.time() - self._last_message_time
            stream_active = self._stream.active

            # Stream is active and we got recent data — all good
            if stream_active and silence < dead_threshold:
                consecutive_restarts = 0
                continue

            # Stream looks dead
            if not stream_active or silence >= dead_threshold:
                consecutive_restarts += 1
                if consecutive_restarts > max_restarts:
                    log.error(
                        "Stream restart limit reached (%d). Giving up — manual intervention required.",
                        max_restarts,
                    )
                    continue

                log.warning(
                    "Stream appears dead (active=%s, silence=%.0fs). "
                    "Attempting restart %d/%d...",
                    stream_active, silence, consecutive_restarts, max_restarts,
                )

                try:
                    self._restart_stream()
                    log.info("Stream restarted successfully (active=%s)", self._stream.active)
                except Exception:
                    log.exception("Stream restart failed")

    def _restart_stream(self) -> None:
        """Stop and restart the schwabdev stream, preserving subscriptions."""
        if not self._stream:
            return

        # Stop without clearing subscriptions — schwabdev replays them on reconnect
        try:
            self._stream.stop(clear_subscriptions=False)
        except Exception:
            log.exception("Error during stream stop")

        # Small delay before reconnecting
        time.sleep(2)

        # Restart — schwabdev resets _should_stop and replays all subscriptions
        self._stream.start(receiver=self._on_message)

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def _subscribe(self, symbols: list[str]) -> None:
        """Subscribe to L1 for given symbols."""
        if not symbols or not self._stream:
            return
        keys = list(symbols)
        keys_str = ",".join(keys)

        l1_req = self._stream.level_one_equities(keys=keys, fields=L1_FIELDS)
        log.info("Subscribing L1 (%d symbols): %s", len(keys), keys_str)
        self._stream.send(l1_req)

    def _unsubscribe(self, symbols: set[str]) -> None:
        """Unsubscribe symbols from L1 stream."""
        if not symbols or not self._stream:
            return
        keys = sorted(symbols)
        keys_str = ",".join(keys)

        unsub_req = self._stream.basic_request(
            service="LEVELONE_EQUITIES",
            command="UNSUBS",
            parameters={"keys": keys_str},
        )
        log.info("Unsubscribing L1 (%d symbols): %s", len(keys), keys_str)
        self._stream.send(unsub_req)

    async def _portfolio_sync_loop(self) -> None:
        """Periodically sync symbols with trader app portfolio."""
        while not self._stop.is_set():
            await asyncio.sleep(self.config.portfolio_sync_interval_sec)
            try:
                added, removed = self._portfolio.sync()
                if added:
                    self._subscribe(sorted(added))
                if removed:
                    self._unsubscribe(removed)
            except Exception:
                log.exception("Portfolio sync error")

    async def run(self) -> None:
        """Start the collector. Blocks until stopped via signal."""
        cfg = self.config

        if not cfg.schwab_app_key or not cfg.schwab_app_secret:
            raise RuntimeError("Missing SCHWAB_APP_KEY / SCHWAB_APP_SECRET")

        # Initial portfolio sync: merge base symbols + current holdings
        self._portfolio.sync()
        initial_symbols = sorted(self._portfolio.active_symbols)

        if not initial_symbols:
            raise RuntimeError("No portfolio holdings found in trader.db")

        log.info("Starting tick collector with %d portfolio symbols", len(initial_symbols))

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

        self._start_time = time.time()

        # Subscribe to all initial symbols
        self._subscribe(initial_symbols)

        # Set up signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal)

        # Run flush + status + portfolio sync + health check loops until stopped
        log.info("Collector running. Press Ctrl+C to stop.")
        await asyncio.gather(
            self._flush_loop(),
            self._status_loop(),
            self._portfolio_sync_loop(),
            self._health_check_loop(),
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
            "Stopped after %.0fs. Total: L1=%d flushed=%d",
            elapsed, self._l1_count, self._flush_count,
        )
