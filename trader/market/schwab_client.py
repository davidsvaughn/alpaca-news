"""Schwab market data client.

Wraps ``schwabdev`` to provide:
- Intraday price history (1-min candles) for snapshot context
- Real-time quote snapshots
- Real-time streaming (level-one equities)

All methods are designed to be *safe to call even when Schwab credentials
are missing* — they return empty / None results and log a warning.  This
lets the pipeline run in mock / offline modes without crashing.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candle:
    """A single OHLCV candle."""
    t: str            # ISO timestamp
    o: float          # open
    h: float          # high
    l: float          # low
    c: float          # close
    v: int            # volume


@dataclass(frozen=True)
class QuoteSnapshot:
    """Summary quote for one symbol at a point in time."""
    symbol: str
    last_price: float
    bid: float
    ask: float
    total_volume: int
    high: float
    low: float
    open_price: float
    close_price: float
    net_change: float
    net_pct_change: float
    mark: float
    timestamp_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "bid": self.bid,
            "ask": self.ask,
            "total_volume": self.total_volume,
            "high": self.high,
            "low": self.low,
            "open_price": self.open_price,
            "close_price": self.close_price,
            "net_change": self.net_change,
            "net_pct_change": self.net_pct_change,
            "mark": self.mark,
            "timestamp_ms": self.timestamp_ms,
        }


@dataclass
class StreamState:
    """Thread-safe mutable state updated by the real-time streamer."""
    latest: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, symbol: str, fields: dict[str, Any]) -> None:
        with self._lock:
            if symbol not in self.latest:
                self.latest[symbol] = {}
            self.latest[symbol].update(fields)

    def get(self, symbol: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.latest.get(symbol, {}))

    def get_all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self.latest.items()}


# ---------------------------------------------------------------------------
# Stream field mapping (key id → human name)
# ---------------------------------------------------------------------------

STREAM_FIELDS = "0,1,2,3,4,5,8,10,11,12,17,18,33,42"
# 0=Symbol, 1=Bid, 2=Ask, 3=Last, 4=BidSize, 5=AskSize, 8=TotalVolume,
# 10=High, 11=Low, 12=Close, 17=Open, 18=NetChange, 33=Mark, 42=NetPctChange

FIELD_NAMES: dict[str, str] = {
    "0": "symbol", "1": "bid", "2": "ask", "3": "last_price",
    "4": "bid_size", "5": "ask_size", "8": "total_volume",
    "10": "high", "11": "low", "12": "close", "17": "open",
    "18": "net_change", "33": "mark", "42": "net_pct_change",
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SchwabMarketClient:
    """High-level wrapper around schwabdev for market data.

    If Schwab credentials are missing, all methods return empty results
    and log a warning (never crash).
    """

    def __init__(self) -> None:
        self._client: Any | None = None
        self._streamer: Any | None = None
        self._stream_state = StreamState()
        self._stream_started = False
        self._init_client()

    def _init_client(self) -> None:
        app_key = os.getenv("SCHWAB_APP_KEY")
        app_secret = os.getenv("SCHWAB_APP_SECRET")
        if not app_key or not app_secret:
            if DEBUG:
                print("WARN: SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set — market data disabled")
            return
        try:
            import schwabdev
            self._client = schwabdev.Client(app_key, app_secret)
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Could not init Schwab client: {e}")

    @property
    def available(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # Price history (candles)
    # ------------------------------------------------------------------

    def get_intraday_candles(
        self,
        symbol: str,
        *,
        period_type: str = "day",
        period: int = 1,
        frequency_type: str = "minute",
        frequency: int = 1,
        extended_hours: bool = True,
    ) -> list[Candle]:
        """Fetch intraday candles for *symbol*.

        Defaults: last 1 trading day, 1-min bars, with extended hours.
        Returns empty list if Schwab is unavailable.
        """
        if not self.available:
            return []
        try:
            resp = self._client.price_history(
                symbol,
                periodType=period_type,
                period=period,
                frequencyType=frequency_type,
                frequency=frequency,
                needExtendedHoursData=extended_hours,
            )
            data = resp.json()
            if data.get("empty", True):
                return []
            candles: list[Candle] = []
            for c in data.get("candles", []):
                ts_ms = c.get("datetime", 0)
                ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
                candles.append(Candle(
                    t=ts_iso,
                    o=float(c.get("open", 0)),
                    h=float(c.get("high", 0)),
                    l=float(c.get("low", 0)),
                    c=float(c.get("close", 0)),
                    v=int(c.get("volume", 0)),
                ))
            return candles
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: get_intraday_candles({symbol}): {e}")
            return []

    # ------------------------------------------------------------------
    # Snapshot quote
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> QuoteSnapshot | None:
        """Fetch a single real-time quote snapshot.

        Returns None if unavailable.
        """
        if not self.available:
            return None
        try:
            resp = self._client.quote(symbol)
            data = resp.json()
            sym_data = data.get(symbol, {})
            q = sym_data.get("quote", {})
            return QuoteSnapshot(
                symbol=symbol,
                last_price=float(q.get("lastPrice", 0)),
                bid=float(q.get("bidPrice", 0)),
                ask=float(q.get("askPrice", 0)),
                total_volume=int(q.get("totalVolume", 0)),
                high=float(q.get("highPrice", 0)),
                low=float(q.get("lowPrice", 0)),
                open_price=float(q.get("openPrice", 0)),
                close_price=float(q.get("closePrice", 0)),
                net_change=float(q.get("netChange", 0)),
                net_pct_change=float(q.get("netPercentChange", 0)),
                mark=float(q.get("mark", 0)),
                timestamp_ms=int(q.get("tradeTime", 0)),
            )
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: get_quote({symbol}): {e}")
            return None

    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        """Fetch quotes for multiple symbols. Returns dict keyed by symbol."""
        result: dict[str, QuoteSnapshot] = {}
        if not self.available or not symbols:
            return result
        try:
            resp = self._client.quotes(symbols)
            data = resp.json()
            for sym in symbols:
                sym_data = data.get(sym, {})
                q = sym_data.get("quote", {})
                if not q:
                    continue
                result[sym] = QuoteSnapshot(
                    symbol=sym,
                    last_price=float(q.get("lastPrice", 0)),
                    bid=float(q.get("bidPrice", 0)),
                    ask=float(q.get("askPrice", 0)),
                    total_volume=int(q.get("totalVolume", 0)),
                    high=float(q.get("highPrice", 0)),
                    low=float(q.get("lowPrice", 0)),
                    open_price=float(q.get("openPrice", 0)),
                    close_price=float(q.get("closePrice", 0)),
                    net_change=float(q.get("netChange", 0)),
                    net_pct_change=float(q.get("netPercentChange", 0)),
                    mark=float(q.get("mark", 0)),
                    timestamp_ms=int(q.get("tradeTime", 0)),
                )
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: get_quotes({symbols}): {e}")
        return result

    # ------------------------------------------------------------------
    # Real-time streaming
    # ------------------------------------------------------------------

    def start_stream(self, symbols: list[str]) -> None:
        """Start real-time level-one equity stream for *symbols*.

        Updates are stored in ``self._stream_state`` which can be polled
        via :meth:`get_stream_snapshot`.  Safe to call multiple times
        (additional symbols are added).
        """
        if not self.available:
            return
        try:
            import schwabdev

            if not self._stream_started:
                self._streamer = schwabdev.Stream(self._client)
                self._streamer.start(receiver=self._on_stream_message)
                self._stream_started = True
                # Small delay to let the websocket connect
                time.sleep(0.5)

            self._streamer.send(
                self._streamer.level_one_equities(
                    keys=symbols,
                    fields=STREAM_FIELDS,
                )
            )
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: start_stream({symbols}): {e}")

    def _on_stream_message(self, message: Any) -> None:
        """Handler for incoming stream messages."""
        try:
            if not isinstance(message, dict):
                return
            data_list = message.get("data", [])
            for data in data_list:
                if data.get("service") != "LEVELONE_EQUITIES":
                    continue
                for content in data.get("content", []):
                    symbol = content.get("key")
                    if not symbol:
                        continue
                    fields: dict[str, Any] = {}
                    for k, v in content.items():
                        if k in FIELD_NAMES:
                            fields[FIELD_NAMES[k]] = v
                    if fields:
                        self._stream_state.update(symbol, fields)
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: stream message handler error: {e}")

    def get_stream_snapshot(self, symbol: str) -> dict[str, Any]:
        """Return latest streamed fields for *symbol*."""
        return self._stream_state.get(symbol)

    def get_all_stream_snapshots(self) -> dict[str, dict[str, Any]]:
        """Return latest streamed fields for all subscribed symbols."""
        return self._stream_state.get_all()

    def stop_stream(self) -> None:
        """Stop the real-time stream."""
        if self._streamer and self._stream_started:
            try:
                self._streamer.stop(clear_subscriptions=True)
            except Exception as e:
                if DEBUG:
                    raise
                print(f"WARN: stop_stream: {e}")
            self._stream_started = False

    # ------------------------------------------------------------------
    # Build price context for Snapshot
    # ------------------------------------------------------------------

    def build_price_context(self, symbols: list[str]) -> dict[str, Any]:
        """Build the ``price_context`` dict for a Snapshot.

        Fetches quotes + recent intraday candles for each symbol.
        Returns a dict suitable for ``SnapshotBuilder.set_price_context()``.
        """
        if not self.available or not symbols:
            return {}

        per_symbol: dict[str, Any] = {}
        for sym in symbols:
            entry: dict[str, Any] = {}

            # Quote
            quote = self.get_quote(sym)
            if quote:
                entry["last_price"] = quote.last_price
                entry["bid"] = quote.bid
                entry["ask"] = quote.ask
                entry["total_volume"] = quote.total_volume
                entry["net_change"] = quote.net_change
                entry["net_pct_change"] = quote.net_pct_change
                entry["mark"] = quote.mark

            # Stream state (if streaming)
            stream = self.get_stream_snapshot(sym)
            if stream:
                entry["stream_last"] = stream

            # Recent 1-min candles (last few for context, capped at 30)
            candles = self.get_intraday_candles(sym)
            recent = candles[-30:] if candles else []
            entry["recent_candles_1m"] = [
                {"t": c.t, "o": c.o, "h": c.h, "l": c.l, "c": c.c, "v": c.v}
                for c in recent
            ]

            per_symbol[sym] = entry

        return {"per_symbol": per_symbol}

    def build_market_context(self) -> dict[str, Any]:
        """Build market-level context (SPY, VIX, session info).

        Returns a dict suitable for ``SnapshotBuilder.set_market_context()``.
        """
        if not self.available:
            return {}

        ctx: dict[str, Any] = {}

        # Determine market session
        now = datetime.now(tz=timezone.utc)
        hour_et = (now.hour - 5) % 24  # rough EST offset (not DST-aware)
        if 4 <= hour_et < 9.5:
            ctx["session"] = "premarket"
        elif 9.5 <= hour_et < 16:
            ctx["session"] = "market_open"
        else:
            ctx["session"] = "afterhours"

        # SPY context
        spy_quote = self.get_quote("SPY")
        if spy_quote:
            ctx["spy_last"] = spy_quote.last_price
            ctx["spy_net_change"] = spy_quote.net_change
            ctx["spy_net_pct_change"] = spy_quote.net_pct_change

        # VIX context
        vix_quote = self.get_quote("$VIX")
        if vix_quote:
            ctx["vix_level"] = vix_quote.last_price

        return ctx

    # ------------------------------------------------------------------
    # Price check actions (non-LLM market data actions)
    # ------------------------------------------------------------------

    def check_price_spike(self, symbol: str) -> dict[str, Any]:
        """Check if there's a significant recent price move for *symbol*.

        Returns a dict with spike detection results. Used by the
        ``price_spike_check`` action in the explorer.
        """
        result: dict[str, Any] = {"symbol": symbol, "spike_detected": False}
        if not self.available:
            return result

        candles = self.get_intraday_candles(symbol)
        if len(candles) < 5:
            result["reason"] = "insufficient candle data"
            return result

        recent = candles[-5:]
        prices = [c.c for c in recent]
        volumes = [c.v for c in recent]

        # Simple spike: >0.5% move in last 5 candles
        price_change = (prices[-1] - prices[0]) / prices[0] if prices[0] else 0
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        last_vol = volumes[-1] if volumes else 0

        result["price_change_pct"] = round(price_change * 100, 3)
        result["last_price"] = prices[-1]
        result["avg_volume_5m"] = round(avg_vol)
        result["last_volume"] = last_vol

        if abs(price_change) > 0.005:  # >0.5%
            result["spike_detected"] = True
            result["direction"] = "up" if price_change > 0 else "down"

        return result

    def check_volume_regime(self, symbol: str) -> dict[str, Any]:
        """Check for abnormal volume regime for *symbol*.

        Compares recent volume to session average.
        """
        result: dict[str, Any] = {"symbol": symbol, "regime_shift": False}
        if not self.available:
            return result

        candles = self.get_intraday_candles(symbol)
        if len(candles) < 20:
            result["reason"] = "insufficient candle data"
            return result

        all_volumes = [c.v for c in candles]
        recent_volumes = [c.v for c in candles[-5:]]

        avg_all = sum(all_volumes) / len(all_volumes) if all_volumes else 0
        avg_recent = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0

        ratio = avg_recent / avg_all if avg_all > 0 else 0

        result["avg_session_volume"] = round(avg_all)
        result["avg_recent_5m_volume"] = round(avg_recent)
        result["volume_ratio"] = round(ratio, 2)

        if ratio > 2.0:  # recent volume > 2x session average
            result["regime_shift"] = True
            result["description"] = f"Volume {ratio:.1f}x above session average"

        return result
