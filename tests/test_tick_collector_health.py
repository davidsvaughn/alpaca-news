import asyncio
import time

import pytest

from tick_collector.collector import TickCollector
from tick_collector.config import CollectorConfig


class _FakeStream:
    def __init__(self, active: bool) -> None:
        self.active = active


@pytest.mark.asyncio
async def test_health_check_restarts_inactive_stream_quickly(monkeypatch):
    collector = TickCollector(
        CollectorConfig(
            trader_db_path="data/trader.db",
            schwab_app_key="x",
            schwab_app_secret="y",
            health_check_interval_sec=0.01,
            heartbeat_timeout_sec=15.0,
            restart_delay_sec=0.0,
            restart_cooldown_sec=0.05,
        )
    )
    collector._stream = _FakeStream(active=False)
    collector._last_message_time = time.time()

    restart_calls: list[str] = []

    def fake_restart() -> None:
        restart_calls.append("restart")
        collector._stop.set()

    monkeypatch.setattr(collector, "_restart_stream", fake_restart)

    await asyncio.wait_for(collector._health_check_loop(), timeout=1.0)

    assert restart_calls == ["restart"]


def test_disconnect_window_markers_are_recorded():
    collector = TickCollector(
        CollectorConfig(
            trader_db_path="data/trader.db",
            schwab_app_key="x",
            schwab_app_secret="y",
        )
    )

    collector._mark_disconnect_start(reason="inactive")
    assert collector._disconnect_started_at is not None
    assert collector._disconnect_reason == "inactive"

    collector._mark_disconnect_end()

    assert collector._disconnect_started_at is None
    assert collector._disconnect_reason is None
    assert len(collector._disconnect_windows) == 1
    window = collector._disconnect_windows[0]
    assert window["reason"] == "inactive"
    assert window["duration_s"] >= 0.0
