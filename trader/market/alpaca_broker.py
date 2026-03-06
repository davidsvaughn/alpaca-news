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
        """Submit a market buy order.

        If notional is provided but the asset is not fractionable,
        automatically converts to whole-share qty using a quote lookup.
        """
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        kwargs: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": OrderSide.BUY,
            "time_in_force": TimeInForce.DAY,
        }

        if notional is not None and not self.is_fractionable(symbol):
            # Non-fractionable: convert notional to whole shares
            price = self._get_latest_price(symbol)
            if price and price > 0:
                whole_qty = int(notional / price)
                if whole_qty < 1:
                    raise ValueError(
                        f"{symbol} not fractionable: notional ${notional:.2f} < 1 share @ ${price:.2f}"
                    )
                log.info("BUY %s: not fractionable, converting $%.2f -> %d shares @ ~$%.2f",
                         symbol, notional, whole_qty, price)
                kwargs["qty"] = whole_qty
            else:
                # Can't get price — try notional anyway, let Alpaca reject if needed
                kwargs["notional"] = round(notional, 2)
        elif notional is not None:
            kwargs["notional"] = round(notional, 2)
        elif qty is not None:
            kwargs["qty"] = qty
        else:
            raise ValueError("Must provide either notional or qty")

        order = self._client.submit_order(order_data=MarketOrderRequest(**kwargs))
        result = self._to_result(order)
        log.info("BUY order submitted: %s %s notional=%s qty=%s -> %s",
                 symbol, order.id, notional, qty, result.status)
        self._log_tx("buy_submit", symbol.upper(), order_id=result.order_id, status=result.status,
                     detail={"notional": notional, "qty": qty, "kwargs_qty": kwargs.get("qty"),
                             "kwargs_notional": kwargs.get("notional"), "fractionable": "qty" not in kwargs or qty is not None})
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

    def set_stop(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
    ) -> OrderResult:
        """Submit a server-side stop-loss sell order (GTC)."""
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        order = self._client.submit_order(
            order_data=StopOrderRequest(
                symbol=symbol.upper(),
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
            )
        )
        result = self._to_result(order)
        log.info("STOP order submitted: %s %s qty=%s stop=%.2f -> %s",
                 symbol, order.id, qty, stop_price, result.status)
        self._log_tx("stop_submit", symbol.upper(), order_id=result.order_id, status=result.status,
                     detail={"qty": qty, "stop_price": stop_price})
        return result

    def close_position(self, symbol: str) -> OrderResult | None:
        """Close an entire position (market sell). Returns None if no position."""
        try:
            order = self._client.close_position(symbol.upper())
            result = self._to_result(order)
            log.info("CLOSE position: %s -> order %s", symbol, order.id)
            self._log_tx("sell_submit", symbol.upper(), order_id=result.order_id, status=result.status)
            return result
        except Exception as e:
            if "position does not exist" in str(e).lower():
                log.warning("No position to close for %s", symbol)
                self._log_tx("sell_failed", symbol.upper(), status="no_position",
                             detail={"error": str(e)})
                return None
            self._log_tx("sell_failed", symbol.upper(), status="error",
                         detail={"error": str(e)})
            raise

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

    # ------------------------------------------------------------------
    # Confirmed execution — wait for fills
    # ------------------------------------------------------------------

    def wait_for_fill(
        self,
        order_id: str,
        timeout_s: float = 15.0,
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
        timeout_s: float = 15.0,
    ) -> OrderResult:
        """Submit a market buy and wait for fill confirmation.

        Returns OrderResult with actual filled_avg_price and filled_qty.
        Raises on rejection, cancellation, or timeout.
        """
        result = self.buy(symbol, notional=notional, qty=qty)
        try:
            confirmed = self.wait_for_fill(result.order_id, timeout_s=timeout_s)
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
        timeout_s: float = 15.0,
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
