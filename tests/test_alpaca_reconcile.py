from __future__ import annotations

from trader.market.alpaca_broker import OrderResult, PositionInfo
from trader.market.alpaca_reconcile import ensure_stops, reconcile


def test_ensure_stops_skips_symbol_with_open_non_stop_sell(monkeypatch):
    watch = {
        "watch_id": "watch_1",
        "symbol": "CAPR",
        "status": "holding",
        "alpaca_buy_order_id": "buy_1",
        "alpaca_stop_order_id": None,
        "alpaca_stop_price": 30.0,
        "entry": {"price": 32.0},
        "live_config_id": "cfg_1",
    }

    monkeypatch.setattr(
        "trader.db.database.get_active_watches",
        lambda _db: [watch],
    )
    monkeypatch.setattr(
        "trader.db.database.update_watch_if_current_status",
        lambda *args, **kwargs: True,
    )

    class _Broker:
        account_id = "paper-1"

        def get_positions(self):
            return [
                PositionInfo(
                    symbol="CAPR",
                    qty=144.0,
                    avg_entry_price=32.0,
                    market_value=4608.0,
                    unrealized_pl=0.0,
                    current_price=32.0,
                ),
            ]

        def get_open_orders(self):
            return [
                OrderResult(
                    order_id="sell_open_1",
                    symbol="CAPR",
                    side="sell",
                    status="new",
                    qty=144.0,
                    stop_price=None,  # non-stop sell close order
                ),
            ]

        def set_stop(self, symbol: str, qty: float, stop_price: float):
            raise AssertionError("set_stop should not be called when non-stop sell is open")

    summary = ensure_stops(
        broker=_Broker(),
        db=object(),
        live_config_id="cfg_1",
        guard_stop_pct=0.0,
    )

    assert summary["submitted"] == []
    assert summary["already_open"] == []
    assert summary["errors"] == []
    assert summary["skipped_open_sell"] == ["CAPR"]


def test_ensure_stops_skips_symbol_with_open_buy(monkeypatch):
    watch = {
        "watch_id": "watch_1",
        "symbol": "OLLI",
        "status": "holding",
        "alpaca_buy_order_id": "buy_1",
        "alpaca_stop_order_id": None,
        "alpaca_stop_price": 103.83,
        "entry": {"price": 109.31},
        "live_config_id": "cfg_1",
    }

    monkeypatch.setattr(
        "trader.db.database.get_active_watches",
        lambda _db: [watch],
    )
    monkeypatch.setattr(
        "trader.db.database.update_watch_if_current_status",
        lambda *args, **kwargs: True,
    )

    class _Broker:
        account_id = "paper-1"

        def get_positions(self):
            return [
                PositionInfo(
                    symbol="OLLI",
                    qty=42.0,
                    avg_entry_price=109.31,
                    market_value=4591.02,
                    unrealized_pl=0.0,
                    current_price=109.31,
                ),
            ]

        def get_open_orders(self):
            return [
                OrderResult(
                    order_id="buy_open_1",
                    symbol="OLLI",
                    side="buy",
                    status="partially_filled",
                    qty=45.0,
                ),
            ]

        def set_stop(self, symbol: str, qty: float, stop_price: float):
            raise AssertionError("set_stop should not be called when buy is still open")

    summary = ensure_stops(
        broker=_Broker(),
        db=object(),
        live_config_id="cfg_1",
        guard_stop_pct=0.0,
    )

    assert summary["submitted"] == []
    assert summary["already_open"] == []
    assert summary["errors"] == []
    assert summary["skipped_open_buy"] == ["OLLI"]


def test_reconcile_skips_orphan_adoption_while_buy_is_open(monkeypatch):
    monkeypatch.setattr(
        "trader.db.database.get_active_watches",
        lambda _db: [],
    )
    monkeypatch.setattr(
        "trader.db.database.insert_watch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("insert_watch should not be called")),
    )

    class _Broker:
        account_id = "paper-1"

        def get_positions(self):
            return [
                PositionInfo(
                    symbol="OLLI",
                    qty=42.0,
                    avg_entry_price=109.31,
                    market_value=4591.02,
                    unrealized_pl=0.0,
                    current_price=109.31,
                ),
            ]

        def get_open_orders(self):
            return [
                OrderResult(
                    order_id="buy_open_1",
                    symbol="OLLI",
                    side="buy",
                    status="partially_filled",
                    qty=45.0,
                ),
            ]

    summary = reconcile(
        broker=_Broker(),
        db=object(),
        live_config_id="cfg_1",
        live_config=object(),
    )

    assert summary["alpaca_orphan_adopted"] == []
    assert summary["alpaca_orphan_closed"] == []
    assert summary["alpaca_orphan_pending_buy"] == ["OLLI"]
