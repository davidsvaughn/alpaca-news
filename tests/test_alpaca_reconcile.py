from __future__ import annotations

from trader.market.alpaca_broker import OrderResult, PositionInfo
from trader.market.alpaca_reconcile import ensure_stops


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
