"""Tests for WatchMonitor scheduling, check-ins, and exit handling."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trader.db.database import (
    count_holding_watches,
    get_watch,
    insert_watch,
    open_sqlite,
    update_watch,
)
from trader.models.watch import WatchBuilder, WatchEntry, WatchExit
from trader.online.event_bus import EventBus
from trader.online.watcher import (
    WatchMonitor,
    _get_schedule,
    _minutes_since,
    compute_pnl,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_minutes_ago(minutes: float) -> str:
    """Return ISO timestamp N minutes in the past."""
    t = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    return t.isoformat()


class FakeSignal:
    direction = "bullish"
    confidence = 0.85
    horizon = "60m"
    key_catalyst = "Revenue beat"


def _make_watch_dict(
    minutes_ago: float = 5.0,
    last_checkin_minutes_ago: float | None = None,
    entry_price: float = 100.0,
    direction: str = "bullish",
) -> dict:
    """Create a watch dict as it would appear from DB."""
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_test",
        symbol="NVDA",
        entry_price=entry_price,
        signal=FakeSignal(),
    )
    # Override entry time to be N minutes ago
    wb.entry = WatchEntry(
        snapshot_id="snap_test",
        price=entry_price,
        time=_iso_minutes_ago(minutes_ago),
        confidence=0.85,
        direction=direction,
        horizon="60m",
        thesis="Revenue beat",
    )
    if last_checkin_minutes_ago is not None:
        wb.last_checkin_at = _iso_minutes_ago(last_checkin_minutes_ago)
    watch = wb.to_watch()
    return watch.to_dict()


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as d:
        db = open_sqlite(str(Path(d) / "test.db"))
        yield db


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def mock_market():
    market = MagicMock()
    market.get_quote.return_value = {"last_price": 105.0, "symbol": "NVDA"}
    return market


# ---------------------------------------------------------------------------
# Schedule tests
# ---------------------------------------------------------------------------


def test_get_schedule():
    assert _get_schedule(0) == (2, "lightweight")
    assert _get_schedule(5) == (2, "lightweight")
    assert _get_schedule(9.9) == (2, "lightweight")
    assert _get_schedule(10) == (5, "medium")
    assert _get_schedule(25) == (5, "medium")
    assert _get_schedule(30) == (10, "medium")
    assert _get_schedule(55) == (10, "medium")
    assert _get_schedule(60) == (15, "full")
    assert _get_schedule(200) == (15, "full")
    assert _get_schedule(240) == (0, "force_exit")
    assert _get_schedule(500) == (0, "force_exit")


def test_minutes_since():
    t = _iso_minutes_ago(10.0)
    elapsed = _minutes_since(t)
    assert 9.5 < elapsed < 10.5


def test_compute_pnl():
    # Bullish: price went up = profit
    assert compute_pnl(100.0, 105.0, "bullish") == pytest.approx(5.0)
    # Bullish: price went down = loss
    assert compute_pnl(100.0, 95.0, "bullish") == pytest.approx(-5.0)
    # Bearish: price went down = profit
    assert compute_pnl(100.0, 95.0, "bearish") == pytest.approx(5.0)
    # Bearish: price went up = loss
    assert compute_pnl(100.0, 105.0, "bearish") == pytest.approx(-5.0)


# ---------------------------------------------------------------------------
# is_due tests
# ---------------------------------------------------------------------------


def test_is_due_never_checked_in(tmp_db, bus, mock_market):
    """A watch that was never checked in should be due."""
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)
    watch = _make_watch_dict(minutes_ago=3.0, last_checkin_minutes_ago=None)
    assert monitor._is_due(watch) is True


def test_is_due_recently_checked_in(tmp_db, bus, mock_market):
    """A watch checked in recently should NOT be due."""
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)
    # 5 min held → lightweight, 2 min interval. Checked 1 min ago → not due
    watch = _make_watch_dict(minutes_ago=5.0, last_checkin_minutes_ago=1.0)
    assert monitor._is_due(watch) is False


def test_is_due_interval_elapsed(tmp_db, bus, mock_market):
    """A watch past its interval should be due."""
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)
    # 5 min held → lightweight, 2 min interval. Checked 3 min ago → due
    watch = _make_watch_dict(minutes_ago=5.0, last_checkin_minutes_ago=3.0)
    assert monitor._is_due(watch) is True


def test_is_due_force_exit(tmp_db, bus, mock_market):
    """A watch past max hold is always due (force_exit)."""
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)
    watch = _make_watch_dict(minutes_ago=250.0, last_checkin_minutes_ago=0.5)
    assert monitor._is_due(watch) is True


# ---------------------------------------------------------------------------
# Lightweight check-in tests
# ---------------------------------------------------------------------------


def test_lightweight_checkin_updates_last_checkin(tmp_db, bus, mock_market):
    """Lightweight check-in should update last_checkin_at in DB."""
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)
    watch_dict = _make_watch_dict(minutes_ago=3.0, entry_price=100.0)
    insert_watch(tmp_db, watch=watch_dict)

    monitor._lightweight_checkin(watch_dict, minutes_held=3.0)

    updated = get_watch(tmp_db, watch_dict["watch_id"])
    assert updated is not None
    assert updated["last_checkin_at"] is not None
    assert updated["status"] == "holding"


def test_lightweight_checkin_auto_stop_loss(tmp_db, bus, mock_market):
    """Extreme loss should trigger auto-exit."""
    # Price dropped from 100 to 85 → -15% loss
    mock_market.get_quote.return_value = {"last_price": 85.0, "symbol": "NVDA"}
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)
    watch_dict = _make_watch_dict(minutes_ago=3.0, entry_price=100.0)
    insert_watch(tmp_db, watch=watch_dict)

    events: list = []
    bus.subscribe(lambda e: events.append(e))

    monitor._lightweight_checkin(watch_dict, minutes_held=3.0)

    updated = get_watch(tmp_db, watch_dict["watch_id"])
    assert updated is not None
    assert updated["status"] == "exited"
    assert "stop-loss" in (updated.get("exit") or {}).get("reason", "").lower()

    # Should have published watch_exited event
    exit_events = [e for e in events if e.type == "watch_exited"]
    assert len(exit_events) == 1


def test_lightweight_checkin_normal(tmp_db, bus, mock_market):
    """Normal price movement should keep holding."""
    mock_market.get_quote.return_value = {"last_price": 102.0, "symbol": "NVDA"}
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)
    watch_dict = _make_watch_dict(minutes_ago=3.0, entry_price=100.0)
    insert_watch(tmp_db, watch=watch_dict)

    events: list = []
    bus.subscribe(lambda e: events.append(e))

    monitor._lightweight_checkin(watch_dict, minutes_held=3.0)

    updated = get_watch(tmp_db, watch_dict["watch_id"])
    assert updated["status"] == "holding"

    checkin_events = [e for e in events if e.type == "watch_checkin"]
    assert len(checkin_events) == 1
    assert checkin_events[0].payload["depth"] == "lightweight"
    assert checkin_events[0].payload["pnl_pct"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# WatchBuilder.from_dict roundtrip
# ---------------------------------------------------------------------------


def test_watch_builder_from_dict_roundtrip():
    """from_dict → to_watch → to_dict should produce equivalent data."""
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="NVDA",
        entry_price=612.30,
        signal=FakeSignal(),
    )
    wb.add_monitoring_snapshot("snap_002")
    wb.last_checkin_at = "2026-02-12T10:00:00+00:00"
    original = wb.to_watch().to_dict()

    # Roundtrip
    rebuilt = WatchBuilder.from_dict(original)
    assert rebuilt.watch_id == original["watch_id"]
    assert rebuilt.symbol == "NVDA"
    assert rebuilt.last_checkin_at == "2026-02-12T10:00:00+00:00"
    assert rebuilt.monitoring_snapshot_ids == ["snap_002"]

    roundtripped = rebuilt.to_watch().to_dict()
    assert roundtripped == original


def test_watch_builder_from_dict_with_exit():
    """from_dict should handle exited watches."""
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="AAPL",
        entry_price=200.0,
        signal=FakeSignal(),
    )
    wb.record_exit(price=210.0, reason="Target reached", snapshot_id="snap_002")
    original = wb.to_watch().to_dict()

    rebuilt = WatchBuilder.from_dict(original)
    assert rebuilt.status == "exited"
    assert rebuilt.exit is not None
    assert rebuilt.exit.price == 210.0


# ---------------------------------------------------------------------------
# Fake settings helper
# ---------------------------------------------------------------------------


def _fake_settings():
    """Minimal Settings-like object for tests."""
    class S:
        watch_enabled = True
        watch_confidence_threshold = 0.7
        watch_max_concurrent = 5
        watch_monitoring_budget = 0.50
        watch_max_hold_minutes = 240
        watch_checkin_model = "gemini-3-flash"
        debug = False
    return S()
