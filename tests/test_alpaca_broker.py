from __future__ import annotations

from types import SimpleNamespace

from trader.market.alpaca_broker import AlpacaBroker, OrderResult


def test_cancel_order_pending_cancel_treated_as_in_progress_success():
    class _Client:
        def cancel_order_by_id(self, _order_id: str) -> None:
            raise RuntimeError('{"code":42210000,"message":"order pending cancel"}')

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client = _Client()
    tx_events: list[dict] = []
    broker._log_tx = lambda event, symbol, **kwargs: tx_events.append(  # type: ignore[assignment]
        {"event": event, "symbol": symbol, **kwargs},
    )

    ok = broker.cancel_order("abc123")

    assert ok is True
    assert tx_events
    assert tx_events[-1]["event"] == "stop_cancel"
    assert tx_events[-1]["status"] == "pending_cancel"


def test_close_position_market_retries_after_held_shares():
    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        def close_position(self, _symbol: str):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("insufficient qty available for order")
            return SimpleNamespace(id="sell-1")

    client = _Client()
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client = client
    broker._log_tx = lambda *args, **kwargs: None  # type: ignore[assignment]
    broker._to_result = lambda order: OrderResult(  # type: ignore[assignment]
        order_id=str(order.id),
        symbol="OXY",
        side="sell",
        status="new",
    )

    cancel_calls: list[str] = []
    wait_calls: list[tuple[str, float]] = []

    broker._cancel_open_orders = lambda symbol: cancel_calls.append(symbol) or 1  # type: ignore[assignment]
    broker._wait_for_sell_orders_clear = (  # type: ignore[assignment]
        lambda symbol, timeout_s=5.0, poll_interval_s=0.25:
        wait_calls.append((symbol, timeout_s)) or True
    )

    result = broker._close_position_market("OXY")

    assert result is not None
    assert result.order_id == "sell-1"
    assert client.calls == 2
    assert cancel_calls == ["OXY", "OXY"]
    assert len(wait_calls) == 2


def test_close_position_and_confirm_reuses_open_sell_order():
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._log_tx = lambda *args, **kwargs: None  # type: ignore[assignment]
    broker._resolve_fill_timeout = lambda timeout_s: 77.0  # type: ignore[assignment]

    existing = OrderResult(
        order_id="sell-open-1",
        symbol="CAPR",
        side="sell",
        status="new",
        qty=10.0,
    )
    broker._get_open_non_stop_sell_order = lambda symbol: existing  # type: ignore[assignment]
    broker.close_position = lambda symbol: (_ for _ in ()).throw(AssertionError("should not submit new sell"))  # type: ignore[assignment]

    wait_calls: list[tuple[str, float]] = []
    broker.wait_for_fill = (  # type: ignore[assignment]
        lambda order_id, timeout_s, poll_interval_s=0.5:
        wait_calls.append((order_id, timeout_s)) or OrderResult(
            order_id=order_id,
            symbol="CAPR",
            side="sell",
            status="filled",
            filled_qty=10.0,
            filled_avg_price=32.0,
        )
    )

    result = broker.close_position_and_confirm("CAPR")

    assert result is not None
    assert result.order_id == "sell-open-1"
    assert wait_calls == [("sell-open-1", 77.0)]


def test_close_position_and_confirm_timeout_leaves_order_open():
    broker = AlpacaBroker.__new__(AlpacaBroker)
    tx_events: list[dict] = []
    broker._log_tx = lambda event, symbol, **kwargs: tx_events.append(  # type: ignore[assignment]
        {"event": event, "symbol": symbol, **kwargs},
    )
    broker._resolve_fill_timeout = lambda timeout_s: 60.0  # type: ignore[assignment]
    broker._get_open_non_stop_sell_order = lambda symbol: None  # type: ignore[assignment]

    submitted = OrderResult(
        order_id="sell-new-1",
        symbol="CRVS",
        side="sell",
        status="new",
        qty=50.0,
    )
    broker.close_position = lambda symbol: submitted  # type: ignore[assignment]
    broker.wait_for_fill = lambda order_id, timeout_s, poll_interval_s=0.5: (_ for _ in ()).throw(TimeoutError("timeout"))  # type: ignore[assignment]
    broker.get_order = lambda order_id: OrderResult(  # type: ignore[assignment]
        order_id=order_id,
        symbol="CRVS",
        side="sell",
        status="new",
    )

    try:
        broker.close_position_and_confirm("CRVS")
        raised = False
    except TimeoutError:
        raised = True

    assert raised is True
    assert tx_events
    assert tx_events[-1]["event"] == "sell_failed"
    assert tx_events[-1]["status"] == "timeout_open"
