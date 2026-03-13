"""Schwab market data client.

Wraps ``schwabdev`` to provide:
- Intraday price history (1-min candles) for snapshot context
- Real-time quote snapshots
- Real-time streaming (level-one equities)
- Options activity (ATM IV, put/call ratios)
- Company fundamentals (P/E, market cap, sector, etc.)
- Market movers (top gainers/losers by index)
- Market hours (real session times)

Fail-loud philosophy:
- If Schwab is enabled (default) but credentials are missing or calls fail,
  raise clear exceptions.
- You can explicitly disable Schwab integration with ``SCHWAB_DISABLED=true``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)
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


@dataclass(frozen=True)
class OptionsActivity:
    """Summary of options activity for a symbol — derived from option chain."""
    symbol: str
    atm_iv_call: float | None       # ATM call implied volatility
    atm_iv_put: float | None        # ATM put implied volatility
    atm_iv_avg: float | None        # average of call + put ATM IV
    put_call_volume_ratio: float | None
    put_call_oi_ratio: float | None  # open interest ratio
    total_call_volume: int
    total_put_volume: int
    total_call_oi: int
    total_put_oi: int
    nearest_expiry: str | None       # ISO date of the nearest expiration used
    underlying_price: float | None
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is not None:
                d[k] = v
        return d


@dataclass(frozen=True)
class SchwabFundamentals:
    """Company fundamentals from Schwab instruments API."""
    symbol: str
    name: str
    sector: str
    industry: str
    market_cap: float | None
    pe_ratio: float | None
    forward_pe: float | None
    eps: float | None
    dividend_yield: float | None
    dividend_amount: float | None
    beta: float | None
    week_52_high: float | None
    week_52_low: float | None
    avg_10d_volume: float | None
    avg_1y_volume: float | None
    pb_ratio: float | None        # price-to-book
    net_profit_margin: float | None
    return_on_equity: float | None
    revenue: float | None
    shares_outstanding: float | None
    debt_to_equity: float | None
    short_int_to_float: float | None
    short_int_days_to_cover: float | None
    eps_change_pct_ttm: float | None
    rev_change_pct_ttm: float | None
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is not None:
                d[k] = v
        return d


@dataclass(frozen=True)
class Mover:
    """A single market mover entry."""
    symbol: str
    description: str
    direction: str         # "up" or "down"
    change: float          # absolute change
    pct_change: float      # percent change
    volume: int
    last_price: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "description": self.description,
            "direction": self.direction,
            "change": self.change,
            "pct_change": self.pct_change,
            "volume": self.volume,
            "last_price": self.last_price,
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
    """High-level wrapper around schwabdev for market data."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._streamer: Any | None = None
        self._stream_state = StreamState()
        self._stream_started = False
        self._volume_delta_collector: Any | None = None  # VolumeDeltaCollector (optional)
        self._init_client()

    def _init_client(self) -> None:
        if os.getenv("SCHWAB_DISABLED", "false").lower() in ("true", "1"):
            log.debug("SCHWAB_DISABLED=true — market data disabled")
            return

        app_key = os.getenv("SCHWAB_APP_KEY")
        app_secret = os.getenv("SCHWAB_APP_SECRET")
        if not app_key or not app_secret:
            raise RuntimeError("Missing SCHWAB_APP_KEY / SCHWAB_APP_SECRET (set SCHWAB_DISABLED=true to disable)")
        try:
            import schwabdev
            self._client = schwabdev.Client(app_key, app_secret)
        except Exception as e:
            if DEBUG:
                raise
            raise RuntimeError(f"Could not init Schwab client: {e}") from e

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
        Raises if Schwab is unavailable.
        """
        if not self.available:
            raise RuntimeError("Schwab client unavailable")
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
            raise RuntimeError(f"get_intraday_candles({symbol}) failed: {e}") from e

    def get_candles_by_date_range(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
        frequency: int = 1,
        extended_hours: bool = True,
    ) -> list[Candle]:
        """Fetch minute candles for *symbol* within an absolute date range.

        Uses Schwab's startDate/endDate parameters (more accurate than
        yfinance for recent intraday data).
        """
        if not self.available:
            raise RuntimeError("Schwab client unavailable")
        try:
            resp = self._client.price_history(
                symbol,
                frequencyType="minute",
                frequency=frequency,
                startDate=start,
                endDate=end,
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
            raise RuntimeError(f"get_candles_by_date_range({symbol}) failed: {e}") from e

    # ------------------------------------------------------------------
    # Snapshot quote
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> QuoteSnapshot | None:
        """Fetch a single real-time quote snapshot.

        Raises if Schwab is unavailable.
        """
        if not self.available:
            raise RuntimeError("Schwab client unavailable")
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
            raise RuntimeError(f"get_quote({symbol}) failed: {e}") from e

    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        """Fetch quotes for multiple symbols. Returns dict keyed by symbol."""
        result: dict[str, QuoteSnapshot] = {}
        if not symbols:
            return result
        if not self.available:
            raise RuntimeError("Schwab client unavailable")
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
            raise RuntimeError(f"get_quotes({symbols}) failed: {e}") from e
        return result

    def get_quotes_with_fundamentals(
        self, symbols: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Fetch quotes + fundamental data in a single API call.

        The Schwab quotes endpoint returns both quote and fundamental sections.
        Returns a dict keyed by symbol with quote + fundamental fields merged.
        """
        result: dict[str, dict[str, Any]] = {}
        if not symbols or not self.available:
            return result
        try:
            resp = self._client.quotes(symbols)
            data = resp.json()
            for sym in symbols:
                sym_data = data.get(sym, {})
                q = sym_data.get("quote", {})
                f = sym_data.get("fundamental", {})
                ref = sym_data.get("reference", {})
                if not q:
                    continue
                result[sym] = {
                    # Quote fields
                    "last_price": q.get("lastPrice"),
                    "net_pct_change": q.get("netPercentChange"),
                    "total_volume": q.get("totalVolume"),
                    # Fundamental fields from same response
                    "pe_ratio": f.get("peRatio"),
                    "eps": f.get("eps"),
                    "div_yield": f.get("divYield"),
                    "avg_10d_volume": f.get("avg10DaysVolume"),
                    "avg_1y_volume": f.get("avg1YearVolume"),
                    "shares_outstanding": f.get("sharesOutstanding"),
                    # Reference
                    "description": ref.get("description"),
                    "exchange": ref.get("exchangeName"),
                }
        except Exception as e:
            if DEBUG:
                raise
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
            raise RuntimeError("Schwab client unavailable")
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
            raise RuntimeError(f"start_stream({symbols}) failed: {e}") from e

    def attach_volume_delta_collector(self, collector: Any) -> None:
        """Attach a VolumeDeltaCollector to receive tick-level updates.

        The collector's ``on_stream_update`` is called for every
        LEVELONE_EQUITIES message that includes price and volume.
        """
        self._volume_delta_collector = collector

    def _on_stream_message(self, message: Any) -> None:
        """Handler for incoming stream messages."""
        try:
            # schwabdev may send JSON strings instead of parsed dicts
            if isinstance(message, str):
                import json as _json
                try:
                    message = _json.loads(message)
                except (ValueError, TypeError):
                    return
            if not isinstance(message, dict):
                return
            data_list = message.get("data", [])
            for data in data_list:
                if data.get("service") != "LEVELONE_EQUITIES":
                    continue
                ts = data.get("timestamp")
                ts_sec = ts / 1000.0 if ts else None
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
                    # Feed volume delta shadow collector
                    vdc = self._volume_delta_collector
                    if vdc is not None:
                        price = content.get("3")  # field 3 = last_price
                        vol = content.get("8")    # field 8 = total_volume
                        if price is not None and vol is not None:
                            try:
                                vdc.on_stream_update(
                                    symbol,
                                    last_price=float(price),
                                    total_volume=int(vol),
                                    timestamp=ts_sec,
                                )
                            except Exception:
                                pass  # collector errors must not break stream
        except Exception as e:
            if DEBUG:
                raise
            log.warning("Stream message handler error: %s", e, exc_info=True)

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
                raise RuntimeError(f"stop_stream failed: {e}") from e
            self._stream_started = False

    # ------------------------------------------------------------------
    # Build price context for Snapshot
    # ------------------------------------------------------------------

    def build_price_context(self, symbols: list[str]) -> dict[str, Any]:
        """Build the ``price_context`` dict for a Snapshot.

        Fetches quotes + recent intraday candles for each symbol.
        Returns a dict suitable for ``SnapshotBuilder.set_price_context()``.
        """
        if not symbols:
            return {}
        if not self.available:
            raise RuntimeError("Schwab client unavailable")

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
        Uses the real market hours API when available, with a rough EST
        fallback if the API call fails.
        """
        if not self.available:
            return {}

        ctx: dict[str, Any] = {}

        # Determine market session — try real API first, fall back to rough EST
        try:
            hours = self.get_market_hours("equity")
            ctx["market_is_open"] = hours.get("is_open", False)
            if hours.get("is_open"):
                ctx["session"] = "market_open"
            else:
                # Check pre/post by rough EST if market is closed
                now = datetime.now(tz=timezone.utc)
                hour_et = (now.hour - 5) % 24
                ctx["session"] = "premarket" if 4 <= hour_et < 9.5 else "afterhours"
            # Include session times if available
            for key in ("regularMarket_start", "regularMarket_end",
                        "preMarket_start", "preMarket_end",
                        "postMarket_start", "postMarket_end"):
                if key in hours:
                    ctx[key] = hours[key]
        except Exception:
            # Fallback: rough EST calculation
            now = datetime.now(tz=timezone.utc)
            hour_et = (now.hour - 5) % 24
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
        # VIX symbol support varies; don't crash if unavailable.
        try:
            vix_quote = self.get_quote("$VIX")
            if vix_quote:
                ctx["vix_level"] = vix_quote.last_price
        except Exception:
            pass

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
            raise RuntimeError("Schwab client unavailable")

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
            raise RuntimeError("Schwab client unavailable")

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

    # ------------------------------------------------------------------
    # Options activity
    # ------------------------------------------------------------------

    def check_options_activity(self, symbol: str) -> dict[str, Any]:
        """Check options activity for *symbol* — ATM IV, put/call ratios.

        Fetches the option chain for the nearest expiry and computes:
        - ATM implied volatility (call, put, average)
        - Put/call volume ratio
        - Put/call open interest ratio

        Returns an OptionsActivity dict.
        """
        if not self.available:
            raise RuntimeError("Schwab client unavailable")

        now_iso = datetime.now(tz=timezone.utc).isoformat()

        try:
            resp = self._client.option_chains(
                symbol,
                contractType="ALL",
                strikeCount=10,
                includeUnderlyingQuote=True,
            )
            data = resp.json()

            underlying_price = data.get("underlyingPrice") or data.get("underlying", {}).get("last")

            call_map = data.get("callExpDateMap", {})
            put_map = data.get("putExpDateMap", {})

            if not call_map and not put_map:
                return {"symbol": symbol, "error": "no option chain data", "fetched_at": now_iso}

            # Find nearest expiry in call map
            nearest_expiry = None
            if call_map:
                nearest_expiry = sorted(call_map.keys())[0]
            elif put_map:
                nearest_expiry = sorted(put_map.keys())[0]

            # Aggregate across all expirations
            total_call_vol = 0
            total_put_vol = 0
            total_call_oi = 0
            total_put_oi = 0

            for _exp, strikes in call_map.items():
                for _strike, contracts in strikes.items():
                    for c in contracts:
                        total_call_vol += int(c.get("totalVolume", 0))
                        total_call_oi += int(c.get("openInterest", 0))

            for _exp, strikes in put_map.items():
                for _strike, contracts in strikes.items():
                    for c in contracts:
                        total_put_vol += int(c.get("totalVolume", 0))
                        total_put_oi += int(c.get("openInterest", 0))

            # ATM IV — find strikes closest to underlying price in nearest expiry
            atm_iv_call = _find_atm_iv(call_map, nearest_expiry, underlying_price)
            atm_iv_put = _find_atm_iv(put_map, nearest_expiry, underlying_price)

            atm_iv_avg = None
            if atm_iv_call is not None and atm_iv_put is not None:
                atm_iv_avg = round((atm_iv_call + atm_iv_put) / 2, 4)
            elif atm_iv_call is not None:
                atm_iv_avg = atm_iv_call
            elif atm_iv_put is not None:
                atm_iv_avg = atm_iv_put

            pc_vol_ratio = round(total_put_vol / total_call_vol, 4) if total_call_vol > 0 else None
            pc_oi_ratio = round(total_put_oi / total_call_oi, 4) if total_call_oi > 0 else None

            # Clean up expiry key (Schwab format: "2026-02-21:5" → "2026-02-21")
            expiry_clean = nearest_expiry.split(":")[0] if nearest_expiry else None

            result = OptionsActivity(
                symbol=symbol.upper(),
                atm_iv_call=atm_iv_call,
                atm_iv_put=atm_iv_put,
                atm_iv_avg=atm_iv_avg,
                put_call_volume_ratio=pc_vol_ratio,
                put_call_oi_ratio=pc_oi_ratio,
                total_call_volume=total_call_vol,
                total_put_volume=total_put_vol,
                total_call_oi=total_call_oi,
                total_put_oi=total_put_oi,
                nearest_expiry=expiry_clean,
                underlying_price=float(underlying_price) if underlying_price else None,
                fetched_at=now_iso,
            )
            return result.to_dict()

        except Exception as e:
            if DEBUG:
                raise
            return {"symbol": symbol, "error": str(e), "fetched_at": now_iso}

    # ------------------------------------------------------------------
    # Fundamentals
    # ------------------------------------------------------------------

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Get company fundamentals for *symbol* via Schwab instruments API.

        Uses projection="fundamental" to get valuation, profitability,
        and health metrics.
        """
        if not self.available:
            raise RuntimeError("Schwab client unavailable")

        now_iso = datetime.now(tz=timezone.utc).isoformat()

        try:
            resp = self._client.instruments(symbol, projection="fundamental")
            data = resp.json()

            # Response is {"instruments": [{"symbol": ..., "fundamental": {...}, ...}]}
            instruments = data.get("instruments", [])
            if not instruments:
                return {"symbol": symbol, "error": "no instrument data", "fetched_at": now_iso}

            inst = instruments[0]
            fund = inst.get("fundamental", {})

            result = SchwabFundamentals(
                symbol=symbol.upper(),
                name=inst.get("description", symbol.upper()),
                sector=fund.get("declarationDate", ""),  # Schwab doesn't have sector in fundamental
                industry="",  # Not available in Schwab fundamental projection
                market_cap=_safe_float(fund.get("marketCap")),
                pe_ratio=_safe_float(fund.get("peRatio")),
                forward_pe=_safe_float(fund.get("forwardPeRatio")),  # not always present
                eps=_safe_float(fund.get("epsTTM")),
                dividend_yield=_safe_float(fund.get("dividendYield")),
                dividend_amount=_safe_float(fund.get("dividendAmount")),
                beta=_safe_float(fund.get("beta")),
                week_52_high=_safe_float(fund.get("high52")),
                week_52_low=_safe_float(fund.get("low52")),
                avg_10d_volume=_safe_float(fund.get("vol10DayAvg")),
                avg_1y_volume=_safe_float(fund.get("vol1YrAvg")),
                pb_ratio=_safe_float(fund.get("pbRatio")),
                net_profit_margin=_safe_float(fund.get("netProfitMarginTTM")),
                return_on_equity=_safe_float(fund.get("returnOnEquity")),
                revenue=_safe_float(fund.get("revenueTTM")),
                shares_outstanding=_safe_float(fund.get("sharesOutstanding")),
                debt_to_equity=_safe_float(fund.get("totalDebtToEquity")),
                short_int_to_float=_safe_float(fund.get("shortIntToFloat")),
                short_int_days_to_cover=_safe_float(fund.get("shortIntDayToCover")),
                eps_change_pct_ttm=_safe_float(fund.get("epsChangePercentTTM")),
                rev_change_pct_ttm=_safe_float(fund.get("revChangeTTM")),
                fetched_at=now_iso,
            )
            return result.to_dict()

        except Exception as e:
            if DEBUG:
                raise
            return {"symbol": symbol, "error": str(e), "fetched_at": now_iso}

    # ------------------------------------------------------------------
    # Market movers
    # ------------------------------------------------------------------

    def get_movers(
        self,
        index: str = "$SPX",
        direction: str = "up",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Get top market movers for an index.

        Args:
            index: Index symbol — "$DJI", "$SPX", "$COMPX", "NYSE", "NASDAQ".
            direction: "up" for gainers, "down" for losers.
            max_results: Max movers to return.

        Returns dict with movers list. Must be called during market hours.
        """
        if not self.available:
            raise RuntimeError("Schwab client unavailable")

        now_iso = datetime.now(tz=timezone.utc).isoformat()
        sort_key = "PERCENT_CHANGE_UP" if direction == "up" else "PERCENT_CHANGE_DOWN"

        try:
            resp = self._client.movers(index, sort=sort_key)
            data = resp.json()

            screeners = data.get("screeners", [])
            movers: list[dict[str, Any]] = []

            for entry in screeners[:max_results]:
                m = Mover(
                    symbol=entry.get("symbol", ""),
                    description=entry.get("description", ""),
                    direction=direction,
                    change=float(entry.get("netChange", 0)),
                    pct_change=float(entry.get("netPercentChange", 0)),
                    volume=int(entry.get("volume", 0)),
                    last_price=float(entry.get("lastPrice", 0)),
                )
                movers.append(m.to_dict())

            return {
                "index": index,
                "direction": direction,
                "movers": movers,
                "count": len(movers),
                "fetched_at": now_iso,
            }

        except Exception as e:
            if DEBUG:
                raise
            return {"index": index, "error": str(e), "fetched_at": now_iso}

    # ------------------------------------------------------------------
    # Market hours
    # ------------------------------------------------------------------

    def get_market_hours(self, market: str = "equity") -> dict[str, Any]:
        """Get today's market hours for *market* type.

        Args:
            market: "equity", "option", "bond", "future", "forex".

        Returns dict with session times and open/closed status.
        """
        if not self.available:
            raise RuntimeError("Schwab client unavailable")

        now_iso = datetime.now(tz=timezone.utc).isoformat()

        try:
            resp = self._client.market_hour(market)
            data = resp.json()

            # Response structure: {"equity": {"EQ": {"date": ..., "marketType": ..., ...}}}
            market_data = data.get(market, {})
            if not market_data:
                return {"market": market, "error": "no data", "fetched_at": now_iso}

            # Get first entry (e.g. "EQ" for equity)
            first_key = next(iter(market_data))
            info = market_data[first_key]

            result: dict[str, Any] = {
                "market": market,
                "date": info.get("date", ""),
                "is_open": info.get("isOpen", False),
                "market_type": info.get("marketType", ""),
            }

            # Extract session hours
            sessions = info.get("sessionHours", {})
            for session_name, hours_list in sessions.items():
                if hours_list:
                    h = hours_list[0]
                    result[f"{session_name}_start"] = h.get("start", "")
                    result[f"{session_name}_end"] = h.get("end", "")

            result["fetched_at"] = now_iso
            return result

        except Exception as e:
            if DEBUG:
                raise
            return {"market": market, "error": str(e), "fetched_at": now_iso}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(val: Any) -> float | None:
    """Convert to float, returning None for missing/invalid values."""
    if val is None:
        return None
    try:
        f = float(val)
        import math
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _find_atm_iv(
    exp_date_map: dict[str, Any],
    target_expiry: str | None,
    underlying_price: float | None,
) -> float | None:
    """Find ATM implied volatility in an option chain expiration map.

    Looks up the strike closest to *underlying_price* in *target_expiry*
    and returns its implied volatility.
    """
    if not exp_date_map or not target_expiry or not underlying_price:
        return None

    strikes = exp_date_map.get(target_expiry, {})
    if not strikes:
        return None

    # Strike keys are strings like "150.0"
    best_strike = None
    best_diff = float("inf")

    for strike_str, contracts in strikes.items():
        try:
            strike_val = float(strike_str)
        except (ValueError, TypeError):
            continue
        diff = abs(strike_val - underlying_price)
        if diff < best_diff:
            best_diff = diff
            best_strike = contracts

    if best_strike and len(best_strike) > 0:
        iv = best_strike[0].get("volatility")
        if iv is not None:
            return round(float(iv), 4)

    return None
