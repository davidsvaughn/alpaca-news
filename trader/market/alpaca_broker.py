"""Alpaca paper trading broker.

Supports multiple paper trading accounts via env vars:

    ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_PAPER_ACCOUNT / ALPACA_PAPER_NAME
    ALPACA_API_KEY_2 / ALPACA_SECRET_KEY_2 / ALPACA_PAPER_ACCOUNT_2 / ALPACA_PAPER_NAME_2
    ALPACA_API_KEY_3 / ALPACA_SECRET_KEY_3 / ALPACA_PAPER_ACCOUNT_3 / ALPACA_PAPER_NAME_3

AlpacaAccountRegistry discovers all configured accounts.
AlpacaBrokerPool manages one broker per account (lazy-initialized).

Usage::

    pool = AlpacaBrokerPool()
    broker = pool.get("PA31QXNAPB1H")
    broker.buy("AAPL", notional=5000.0)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Stop order mode: "whole_shares" or "fractional_day" (default)
# - whole_shares: buy() always converts notional to whole-share qty so stops can be GTC
# - fractional_day: buy() uses notional (fractional fill), stops use DAY TIF + daily re-submission
ALPACA_STOP_MODE = os.getenv("ALPACA_STOP_MODE", "fractional_day").lower()

# How long to wait for order fill confirmation (seconds)
ALPACA_FILL_TIMEOUT = float(os.getenv("ALPACA_FILL_TIMEOUT", "30"))
ALPACA_EXTENDED_FILL_TIMEOUT = float(os.getenv("ALPACA_EXTENDED_FILL_TIMEOUT", "60"))


# ------------------------------------------------------------------
# Data types
# ------------------------------------------------------------------

@dataclass
class OrderResult:
    """Minimal order result returned by broker methods."""
    order_id: str
    symbol: str
    side: str
    status: str
    qty: float | None = None
    notional: float | None = None
    filled_avg_price: float | None = None
    filled_qty: float | None = None
    stop_price: float | None = None
    raw: Any = None  # original Alpaca Order object


@dataclass
class PositionInfo:
    """Snapshot of an Alpaca position."""
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    current_price: float


@dataclass
class AccountInfo:
    """Snapshot of Alpaca account state."""
    account_id: str
    name: str
    equity: float
    cash: float
    buying_power: float
    portfolio_value: float
    pattern_day_trader: bool
    daytrade_count: int


@dataclass
class AlpacaAccount:
    """Credentials for one Alpaca paper trading account."""
    account_id: str   # e.g. "PA31QXNAPB1H"
    name: str         # e.g. "AlpacaPaper1"
    api_key: str
    secret_key: str
    paper: bool = True


# ------------------------------------------------------------------
# Account registry — reads all accounts from env vars
# ------------------------------------------------------------------

class AlpacaAccountRegistry:
    """Discovers all configured Alpaca accounts from environment variables."""

    def __init__(self) -> None:
        self._accounts: dict[str, AlpacaAccount] = {}
        self._discover()

    def _discover(self) -> None:
        paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
        suffixes = ["", "_2", "_3", "_4", "_5"]
        for suffix in suffixes:
            api_key = os.getenv(f"ALPACA_API_KEY{suffix}")
            secret_key = os.getenv(f"ALPACA_SECRET_KEY{suffix}")
            account_id = os.getenv(f"ALPACA_PAPER_ACCOUNT{suffix}")
            name = os.getenv(f"ALPACA_PAPER_NAME{suffix}", f"Paper{suffix or '1'}")
            if api_key and secret_key and account_id:
                self._accounts[account_id] = AlpacaAccount(
                    account_id=account_id,
                    name=name,
                    api_key=api_key,
                    secret_key=secret_key,
                    paper=paper,
                )

    @property
    def accounts(self) -> dict[str, AlpacaAccount]:
        return dict(self._accounts)

    def get(self, account_id: str) -> AlpacaAccount | None:
        return self._accounts.get(account_id)

    def list_ids(self) -> list[str]:
        return list(self._accounts.keys())

    def __len__(self) -> int:
        return len(self._accounts)

    def __bool__(self) -> bool:
        return len(self._accounts) > 0


# ------------------------------------------------------------------
# Broker pool — one broker per account (lazy init)
# ------------------------------------------------------------------

class AlpacaBrokerPool:
    """Manages AlpacaBroker instances, one per account."""

    def __init__(self, registry: AlpacaAccountRegistry | None = None, db: Any = None) -> None:
        self.registry = registry or AlpacaAccountRegistry()
        self._brokers: dict[str, AlpacaBroker] = {}
        self._db = db

    def get(self, account_id: str) -> AlpacaBroker | None:
        """Get or create broker for the given account. Returns None if unknown."""
        if account_id in self._brokers:
            return self._brokers[account_id]
        acct = self.registry.get(account_id)
        if not acct:
            log.warning("No Alpaca account configured for %s", account_id)
            return None
        broker = AlpacaBroker(
            api_key=acct.api_key,
            secret_key=acct.secret_key,
            paper=acct.paper,
            account_id=acct.account_id,
            name=acct.name,
            db=self._db,
        )
        self._brokers[account_id] = broker
        return broker

    def get_all_active(self) -> dict[str, AlpacaBroker]:
        """Return all currently instantiated brokers."""
        return dict(self._brokers)

    @property
    def accounts(self) -> dict[str, AlpacaAccount]:
        return self.registry.accounts


# ------------------------------------------------------------------
# Broker — wraps one Alpaca trading account
# ------------------------------------------------------------------

class AlpacaBroker:
    """Alpaca paper trading broker for a single account."""

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool | None = None,
        account_id: str | None = None,
        name: str | None = None,
        db: Any = None,  # Database for transaction logging (optional)
    ) -> None:
        from alpaca.trading.client import TradingClient

        self._api_key = api_key or os.environ["ALPACA_API_KEY"]
        self._secret_key = secret_key or os.environ["ALPACA_SECRET_KEY"]
        if paper is None:
            paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
        self._paper = paper
        self.account_id = account_id
        self.name = name or "default"
        self._db = db

        self._client = TradingClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=self._paper,
        )
        log.info("AlpacaBroker initialized: %s/%s (paper=%s)", self.name, account_id, self._paper)

    def _log_tx(self, event: str, symbol: str, **kwargs: Any) -> None:
        """Log an Alpaca transaction to the database (if db is set)."""
        if not self._db:
            return
        try:
            from trader.db.database import log_alpaca_transaction
            log_alpaca_transaction(
                self._db,
                account_id=self.account_id or "unknown",
                event=event,
                symbol=symbol,
                **kwargs,
            )
        except Exception:
            log.warning("Failed to log Alpaca transaction: %s %s", event, symbol)

    @property
    def client(self) -> Any:
        return self._client

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        """Fetch current account info."""
        acct = self._client.get_account()
        return AccountInfo(
            account_id=str(acct.account_number),
            name=self.name or str(acct.account_number),
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power),
            portfolio_value=float(acct.portfolio_value),
            pattern_day_trader=bool(acct.pattern_day_trader),
            daytrade_count=int(acct.daytrade_count),
        )

    def get_positions(self) -> list[PositionInfo]:
        """Get all open positions."""
        positions = self._client.get_all_positions()
        return [
            PositionInfo(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
                current_price=float(p.current_price),
            )
            for p in positions
        ]

    def get_position(self, symbol: str) -> PositionInfo | None:
        """Get a specific position, or None if not held."""
        try:
            p = self._client.get_open_position(symbol.upper())
            return PositionInfo(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
                current_price=float(p.current_price),
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def is_fractionable(self, symbol: str) -> bool:
        """Check if a symbol supports fractional shares on Alpaca."""
        try:
            asset = self._client.get_asset(symbol.upper())
            return bool(asset.fractionable)
        except Exception:
            return True  # assume fractionable if lookup fails

    def buy(
        self,
        symbol: str,
        *,
        notional: float | None = None,
        qty: float | None = None,
    ) -> OrderResult:
        """Submit a buy order.

        During extended hours (when ALPACA_EXTENDED_HOURS is enabled and
        outside regular 9:30-16:00), submits a limit order with
        extended_hours=True at a slightly aggressive price.

        If notional is provided but the asset is not fractionable (or
        ALPACA_STOP_MODE=whole_shares), converts to whole-share qty so
        that stop orders can use GTC time-in-force.
        """
        from trader.market.market_hours import in_extended_only, ALPACA_EXTENDED_HOURS

        use_extended = ALPACA_EXTENDED_HOURS and in_extended_only()

        if use_extended:
            return self._buy_extended(symbol, notional=notional, qty=qty)
        return self._buy_market(symbol, notional=notional, qty=qty)

    def _buy_market(
        self,
        symbol: str,
        *,
        notional: float | None = None,
        qty: float | None = None,
    ) -> OrderResult:
        """Submit a market buy order (regular hours)."""
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        kwargs: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": OrderSide.BUY,
            "time_in_force": TimeInForce.DAY,
        }

        need_whole_shares = (
            notional is not None
            and (ALPACA_STOP_MODE == "whole_shares" or not self.is_fractionable(symbol))
        )

        if need_whole_shares:
            price = self._get_latest_price(symbol)
            if price and price > 0:
                whole_qty = int(notional / price)
                if whole_qty < 1:
                    raise ValueError(
                        f"{symbol}: notional ${notional:.2f} < 1 share @ ${price:.2f}"
                    )
                log.info("BUY %s: whole-share mode, converting $%.2f -> %d shares @ ~$%.2f",
                         symbol, notional, whole_qty, price)
                kwargs["qty"] = whole_qty
            else:
                kwargs["notional"] = round(notional, 2)
        elif notional is not None:
            kwargs["notional"] = round(notional, 2)
        elif qty is not None:
            kwargs["qty"] = qty
        else:
            raise ValueError("Must provide either notional or qty")

        try:
            order = self._client.submit_order(order_data=MarketOrderRequest(**kwargs))
        except Exception as exc:
            if "trading halt" in str(exc).lower():
                log.warning("BUY %s: market order rejected (trading halt) — falling back to limit order", symbol)
                return self._buy_limit_halt(symbol, notional=notional, qty=qty)
            raise

        result = self._to_result(order)
        log.info("BUY order submitted: %s %s notional=%s qty=%s -> %s",
                 symbol, order.id, notional, qty, result.status)
        self._log_tx("buy_submit", symbol.upper(), order_id=result.order_id, status=result.status,
                     detail={"notional": notional, "qty": qty, "kwargs_qty": kwargs.get("qty"),
                             "kwargs_notional": kwargs.get("notional"), "fractionable": "qty" not in kwargs or qty is not None})
        return result

    def _buy_limit_halt(
        self,
        symbol: str,
        *,
        notional: float | None = None,
        qty: float | None = None,
        slippage_pct: float = 0.02,
    ) -> OrderResult:
        """Fallback limit buy when a market order is rejected due to trading halt.

        Uses the same logic as extended-hours limit orders: ask price + buffer,
        whole-share qty, DAY time-in-force.
        """
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        ask_price = self._get_latest_ask_price(symbol)
        trade_price = self._get_latest_price(symbol)
        price = ask_price or trade_price
        if not price or price <= 0:
            raise ValueError(f"{symbol}: cannot get price for halt limit order")

        effective_slippage = slippage_pct if ask_price else slippage_pct * 2
        limit_price = round(price * (1 + effective_slippage), 2)
        log.info("BUY HALT-LIMIT %s: ask=%.2f trade=%.2f -> limit=%.2f (slippage=%.1f%%)",
                 symbol, ask_price or 0, trade_price or 0, limit_price, effective_slippage * 100)

        if qty is not None:
            buy_qty = int(qty) if qty == int(qty) else int(qty)
        elif notional is not None:
            buy_qty = int(notional / price)
        else:
            raise ValueError("Must provide either notional or qty")

        if buy_qty < 1:
            raise ValueError(
                f"{symbol}: notional ${notional:.2f} < 1 share @ ${price:.2f} (halt limit)"
            )

        order = self._client.submit_order(
            order_data=LimitOrderRequest(
                symbol=symbol.upper(),
                qty=buy_qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
        )
        result = self._to_result(order)
        log.info("BUY HALT-LIMIT submitted: %s %s qty=%d limit=%.2f -> %s",
                 symbol, order.id, buy_qty, limit_price, result.status)
        self._log_tx("buy_submit", symbol.upper(), order_id=result.order_id, status=result.status,
                     detail={"notional": notional, "qty": buy_qty, "limit_price": limit_price,
                             "halt_fallback": True})
        return result

    def _buy_extended(
        self,
        symbol: str,
        *,
        notional: float | None = None,
        qty: float | None = None,
        slippage_pct: float = 0.02,
    ) -> OrderResult:
        """Submit a limit buy order for extended hours.

        Extended hours require: limit orders only, time_in_force=DAY,
        extended_hours=True, and whole-share qty (no fractional/notional).
        """
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        # Prefer ask price (current offer) over last trade price — last trade
        # can be hours stale during pre/post-market.
        ask_price = self._get_latest_ask_price(symbol)
        trade_price = self._get_latest_price(symbol)
        price = ask_price or trade_price
        if not price or price <= 0:
            raise ValueError(f"{symbol}: cannot get price for extended-hours limit order")

        # Use smaller buffer when we have a live ask, larger when falling back to stale trade
        effective_slippage = slippage_pct if ask_price else slippage_pct * 2
        limit_price = round(price * (1 + effective_slippage), 2)
        log.info("BUY EXTENDED %s: ask=%.2f trade=%.2f -> limit=%.2f (slippage=%.1f%%)",
                 symbol, ask_price or 0, trade_price or 0, limit_price, effective_slippage * 100)

        # Extended hours: must use whole-share qty (no notional/fractional)
        if qty is not None:
            buy_qty = int(qty) if qty == int(qty) else int(qty)
        elif notional is not None:
            buy_qty = int(notional / price)
        else:
            raise ValueError("Must provide either notional or qty")

        if buy_qty < 1:
            raise ValueError(
                f"{symbol}: notional ${notional:.2f} < 1 share @ ${price:.2f} (extended hours)"
            )

        order = self._client.submit_order(
            order_data=LimitOrderRequest(
                symbol=symbol.upper(),
                qty=buy_qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                extended_hours=True,
            )
        )
        result = self._to_result(order)
        log.info("BUY EXTENDED order submitted: %s %s qty=%d limit=%.2f -> %s",
                 symbol, order.id, buy_qty, limit_price, result.status)
        self._log_tx("buy_submit_extended", symbol.upper(), order_id=result.order_id,
                     status=result.status,
                     detail={"qty": buy_qty, "limit_price": limit_price,
                             "notional": notional, "slippage_pct": slippage_pct})
        return result

    def _get_latest_price(self, symbol: str) -> float | None:
        """Get latest trade price from Alpaca for qty conversion."""
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            from alpaca.data.historical import StockHistoricalDataClient
            if not hasattr(self, "_data_client"):
                self._data_client = StockHistoricalDataClient(self._api_key, self._secret_key)
            trade = self._data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol.upper())
            )
            if isinstance(trade, dict):
                t = trade.get(symbol.upper())
                return float(t.price) if t else None
            return float(trade.price)
        except Exception:
            log.warning("Could not get latest price for %s", symbol)
            return None

    def _get_latest_ask_price(self, symbol: str) -> float | None:
        """Get latest ask price from Alpaca quote API.

        More accurate than last trade price during extended hours,
        where trades can be infrequent and stale.
        """
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            from alpaca.data.historical import StockHistoricalDataClient
            if not hasattr(self, "_data_client"):
                self._data_client = StockHistoricalDataClient(self._api_key, self._secret_key)
            quote = self._data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol.upper())
            )
            if isinstance(quote, dict):
                q = quote.get(symbol.upper())
                if q and q.ask_price and q.ask_price > 0:
                    return float(q.ask_price)
            elif quote and quote.ask_price and quote.ask_price > 0:
                return float(quote.ask_price)
        except Exception:
            log.warning("Could not get latest ask price for %s", symbol)
        return None

    def set_stop(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
    ) -> OrderResult:
        """Submit a server-side stop-loss sell order (GTC, or DAY for fractional qty)."""
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        is_fractional = qty % 1 != 0
        tif = TimeInForce.DAY if is_fractional else TimeInForce.GTC
        order = self._client.submit_order(
            order_data=StopOrderRequest(
                symbol=symbol.upper(),
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=tif,
                stop_price=round(stop_price, 2),
            )
        )
        result = self._to_result(order)
        log.info("STOP order submitted: %s %s qty=%s stop=%.2f tif=%s -> %s",
                 symbol, order.id, qty, stop_price, tif.value, result.status)
        self._log_tx("stop_submit", symbol.upper(), order_id=result.order_id, status=result.status,
                     detail={"qty": qty, "stop_price": stop_price, "time_in_force": tif.value})
        return result

    def close_position(self, symbol: str) -> OrderResult | None:
        """Close an entire position. Returns None if no position.

        During extended hours, submits a limit sell order (market orders
        are not accepted outside regular hours).
        """
        from trader.market.market_hours import in_extended_only, ALPACA_EXTENDED_HOURS

        use_extended = ALPACA_EXTENDED_HOURS and in_extended_only()

        if use_extended:
            return self._close_position_extended(symbol)
        return self._close_position_market(symbol)

    def _close_position_market(self, symbol: str) -> OrderResult | None:
        """Close position with a market order (regular hours)."""
        # Cancel any open orders first (stops hold shares and can block a market close).
        self._cancel_open_orders(symbol)

        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                order = self._client.close_position(symbol.upper())
                result = self._to_result(order)
                log.info("CLOSE position: %s -> order %s", symbol, order.id)
                self._log_tx("sell_submit", symbol.upper(), order_id=result.order_id, status=result.status)
                return result
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                if "position does not exist" in msg:
                    log.warning("No position to close for %s", symbol)
                    self._log_tx("sell_failed", symbol.upper(), status="no_position",
                                 detail={"error": str(e)})
                    return None
                # Cancels can take a moment to release held shares; retry once.
                if "insufficient qty available" in msg and attempt == 1:
                    log.warning("CLOSE %s blocked by held shares; retrying after cancel settle", symbol)
                    import time
                    time.sleep(1.0)
                    self._cancel_open_orders(symbol)
                    continue
                break

        self._log_tx("sell_failed", symbol.upper(), status="error",
                     detail={"error": str(last_error) if last_error else "unknown"})
        raise last_error if last_error else RuntimeError(f"Failed to close position for {symbol}")

    def _cancel_open_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol. Returns count of cancelled orders."""
        open_orders = self.get_open_orders(symbol)
        cancelled = 0
        for o in open_orders:
            if self.cancel_order(o.order_id):
                cancelled += 1
        if cancelled:
            log.info("Cancelled %d open orders for %s before sell", cancelled, symbol)
            import time; time.sleep(0.5)  # brief pause for cancellations to settle
        return cancelled

    def _close_position_extended(
        self,
        symbol: str,
        slippage_pct: float = 0.005,
    ) -> OrderResult | None:
        """Close position with a limit sell order for extended hours.

        Auto-cancels any open orders (stops, etc.) that hold shares,
        then submits a limit sell. Uses position's current_price for
        the limit (more accurate than data API's latest trade during
        extended hours).
        """
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        pos = self.get_position(symbol)
        if pos is None:
            log.warning("No position to close for %s (extended hours)", symbol)
            self._log_tx("sell_failed", symbol.upper(), status="no_position",
                         detail={"extended_hours": True})
            return None

        # Cancel any open orders (stops hold shares, blocking the sell)
        self._cancel_open_orders(symbol)

        # Use position's current_price (more accurate than data API during extended hours)
        price = pos.current_price
        if not price or price <= 0:
            price = self._get_latest_price(symbol)
        if not price or price <= 0:
            raise ValueError(f"{symbol}: cannot get price for extended-hours limit sell")

        # Aggressive limit price (slightly below current for sell)
        limit_price = round(price * (1 - slippage_pct), 2)

        # Extended hours: fractional qty not supported — round down to whole shares
        sell_qty = int(pos.qty)

        if sell_qty < 1:
            log.warning("CLOSE EXTENDED %s: qty < 1 share (%.4f), cannot sell in extended hours",
                        symbol, pos.qty)
            self._log_tx("sell_failed", symbol.upper(), status="fractional_only",
                         detail={"qty": pos.qty, "extended_hours": True})
            return None

        if sell_qty < pos.qty:
            log.info("CLOSE EXTENDED %s: selling %d of %.4f shares (fractional remainder stays)",
                     symbol, sell_qty, pos.qty)

        order = self._client.submit_order(
            order_data=LimitOrderRequest(
                symbol=symbol.upper(),
                qty=sell_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                extended_hours=True,
            )
        )
        result = self._to_result(order)
        log.info("CLOSE EXTENDED position: %s -> order %s qty=%s limit=%.2f (price=%.2f)",
                 symbol, order.id, sell_qty, limit_price, price)
        self._log_tx("sell_submit_extended", symbol.upper(), order_id=result.order_id,
                     status=result.status,
                     detail={"qty": sell_qty, "limit_price": limit_price,
                             "price_source": "position", "slippage_pct": slippage_pct})
        return result

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled."""
        try:
            self._client.cancel_order_by_id(order_id)
            log.info("Cancelled order %s", order_id)
            self._log_tx("stop_cancel", "", order_id=order_id, status="cancelled")
            return True
        except Exception as e:
            log.warning("Could not cancel order %s: %s", order_id, e)
            self._log_tx("stop_cancel", "", order_id=order_id, status="failed",
                         detail={"error": str(e)})
            return False

    def get_order(self, order_id: str) -> OrderResult | None:
        """Get current state of an order."""
        try:
            order = self._client.get_order_by_id(order_id)
            return self._to_result(order)
        except Exception:
            return None

    def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        """Get open orders, optionally filtered by symbol."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self._client.get_orders(filter=params)
        results = [self._to_result(o) for o in orders]
        if symbol:
            results = [r for r in results if r.symbol == symbol.upper()]
        return results

    def get_recent_sells(self, symbol: str, limit: int = 5) -> list[OrderResult]:
        """Get recent closed sell orders for a symbol (filled, cancelled, etc.)."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide

        params = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            side=OrderSide.SELL,
            symbols=[symbol.upper()],
            limit=limit,
        )
        orders = self._client.get_orders(filter=params)
        return [self._to_result(o) for o in orders]

    # ------------------------------------------------------------------
    # Confirmed execution — wait for fills
    # ------------------------------------------------------------------

    def wait_for_fill(
        self,
        order_id: str,
        timeout_s: float = ALPACA_FILL_TIMEOUT,
        poll_interval_s: float = 0.5,
    ) -> OrderResult:
        """Poll an order until it fills, fails, or times out.

        Returns the final OrderResult. Raises TimeoutError if not
        filled within timeout_s.
        """
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = self.get_order(order_id)
            if result is None:
                raise RuntimeError(f"Order {order_id} not found")
            status = result.status.lower()
            if status == "filled":
                return result
            if status in ("canceled", "expired", "rejected", "suspended"):
                raise RuntimeError(f"Order {order_id} terminal status: {status}")
            time.sleep(poll_interval_s)

        # Final check
        result = self.get_order(order_id)
        if result and result.status.lower() == "filled":
            return result
        raise TimeoutError(
            f"Order {order_id} not filled after {timeout_s}s (status={result.status if result else 'unknown'})"
        )

    def buy_and_confirm(
        self,
        symbol: str,
        *,
        notional: float | None = None,
        qty: float | None = None,
        timeout_s: float | None = None,
    ) -> OrderResult:
        """Submit a market buy and wait for fill confirmation.

        Returns OrderResult with actual filled_avg_price and filled_qty.
        Raises on rejection, cancellation, or timeout.
        """
        # Use longer timeout for extended hours (limit orders, thin liquidity)
        if timeout_s is None:
            from trader.market.market_hours import in_extended_only, ALPACA_EXTENDED_HOURS
            if ALPACA_EXTENDED_HOURS and in_extended_only():
                timeout_s = ALPACA_EXTENDED_FILL_TIMEOUT
            else:
                timeout_s = ALPACA_FILL_TIMEOUT

        result = self.buy(symbol, notional=notional, qty=qty)
        try:
            confirmed = self.wait_for_fill(result.order_id, timeout_s=timeout_s)
        except TimeoutError:
            # Cancel the unfilled order to prevent orphan positions
            log.warning("Buy order %s for %s timed out — cancelling", result.order_id, symbol)
            self.cancel_order(result.order_id)
            # Check one more time — order may have filled between timeout and cancel
            final = self.get_order(result.order_id)
            if final and final.status.lower() == "filled":
                log.info("Order %s for %s filled just before cancel — proceeding", result.order_id, symbol)
                confirmed = final
            else:
                self._log_tx("buy_failed", symbol.upper(), order_id=result.order_id, status="timeout_cancelled",
                             detail={"notional": notional, "qty": qty})
                raise
        except Exception as e:
            self._log_tx("buy_failed", symbol.upper(), order_id=result.order_id, status="timeout_or_error",
                         detail={"error": str(e), "notional": notional, "qty": qty})
            raise
        log.info(
            "BUY CONFIRMED: %s order=%s qty=%s avg_price=%s",
            symbol, confirmed.order_id, confirmed.filled_qty, confirmed.filled_avg_price,
        )
        self._log_tx("buy_confirmed", symbol.upper(), order_id=confirmed.order_id, status="filled",
                     detail={"filled_qty": confirmed.filled_qty, "filled_avg_price": confirmed.filled_avg_price,
                             "notional": notional, "qty": qty})
        return confirmed

    def close_position_and_confirm(
        self,
        symbol: str,
        timeout_s: float = ALPACA_FILL_TIMEOUT,
    ) -> OrderResult | None:
        """Close a position and wait for sell fill confirmation.

        Returns OrderResult with actual exit price, or None if no position.
        """
        result = self.close_position(symbol)
        if result is None:
            return None
        try:
            confirmed = self.wait_for_fill(result.order_id, timeout_s=timeout_s)
        except Exception as e:
            self._log_tx("sell_failed", symbol.upper(), order_id=result.order_id, status="timeout_or_error",
                         detail={"error": str(e)})
            raise
        log.info(
            "SELL CONFIRMED: %s order=%s qty=%s avg_price=%s",
            symbol, confirmed.order_id, confirmed.filled_qty, confirmed.filled_avg_price,
        )
        self._log_tx("sell_confirmed", symbol.upper(), order_id=confirmed.order_id, status="filled",
                     detail={"filled_qty": confirmed.filled_qty, "filled_avg_price": confirmed.filled_avg_price})
        return confirmed

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _to_result(self, order: Any) -> OrderResult:
        # Use .value for enums (e.g. OrderStatus.FILLED → "filled")
        # to avoid str(enum) returning "OrderStatus.FILLED"
        status = order.status
        status_str = status.value if hasattr(status, "value") else str(status)
        side = order.side
        side_str = side.value if hasattr(side, "value") else str(side)
        return OrderResult(
            order_id=str(order.id),
            symbol=str(order.symbol),
            side=side_str,
            status=status_str,
            qty=float(order.qty) if order.qty else None,
            notional=float(order.notional) if order.notional else None,
            filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
            filled_qty=float(order.filled_qty) if order.filled_qty else None,
            stop_price=float(order.stop_price) if order.stop_price else None,
            raw=order,
        )
