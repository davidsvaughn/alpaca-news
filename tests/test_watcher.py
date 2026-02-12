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
    _get_retro_interval,
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
# Retrospective schedule tests
# ---------------------------------------------------------------------------


def test_get_retro_interval():
    assert _get_retro_interval(0) == 5
    assert _get_retro_interval(10) == 5
    assert _get_retro_interval(14.9) == 5
    assert _get_retro_interval(15) == 15
    assert _get_retro_interval(30) == 15
    assert _get_retro_interval(59.9) == 15
    assert _get_retro_interval(60) == 0  # past schedule → seal
    assert _get_retro_interval(100) == 0


# ---------------------------------------------------------------------------
# Retrospective transition tests
# ---------------------------------------------------------------------------


def _make_exited_watch_dict(
    entry_minutes_ago: float = 30.0,
    exit_minutes_ago: float = 5.0,
    entry_price: float = 100.0,
    exit_price: float = 105.0,
) -> dict:
    """Create an exited watch dict for testing retrospective flow."""
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_test",
        symbol="NVDA",
        entry_price=entry_price,
        signal=FakeSignal(),
    )
    wb.entry = WatchEntry(
        snapshot_id="snap_test",
        price=entry_price,
        time=_iso_minutes_ago(entry_minutes_ago),
        confidence=0.85,
        direction="bullish",
        horizon="60m",
        thesis="Revenue beat",
    )
    wb.record_exit(price=exit_price, reason="Thesis weakened")
    # Override exit time to be N minutes ago
    wb.exit = WatchExit(
        snapshot_id=None,
        price=exit_price,
        time=_iso_minutes_ago(exit_minutes_ago),
        reason="Thesis weakened",
        realized_pnl_pct=round(((exit_price - entry_price) / entry_price) * 100.0, 4),
    )
    return wb.to_watch().to_dict()


def test_transition_to_retrospective(tmp_db, bus, mock_market):
    """Exited watch should transition to retrospective with data initialized."""
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)
    watch_dict = _make_exited_watch_dict(exit_price=105.0)
    insert_watch(tmp_db, watch=watch_dict)

    events: list = []
    bus.subscribe(lambda e: events.append(e))

    monitor._transition_to_retrospective(watch_dict)

    updated = get_watch(tmp_db, watch_dict["watch_id"])
    assert updated["status"] == "retrospective"
    retro = updated["retrospective_data"]
    assert retro is not None
    assert retro["exit_price"] == 105.0
    assert retro["started_at"] is not None
    assert retro["price_checks"] == []
    assert retro["mfe_pct"] == 0.0
    assert retro["mae_pct"] == 0.0

    retro_events = [e for e in events if e.type == "watch_retrospective_started"]
    assert len(retro_events) == 1


def test_retrospective_price_check_updates_mfe_mae(tmp_db, bus, mock_market):
    """Price check during retrospective should track MFE/MAE."""
    # Price went up from exit (105 → 108) = bullish favorable
    mock_market.get_quote.return_value = {"last_price": 108.0, "symbol": "NVDA"}
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)

    # Create a retrospective watch
    watch_dict = _make_exited_watch_dict(exit_price=105.0)
    wb = WatchBuilder.from_dict(watch_dict)
    wb.start_retrospective(105.0)
    wb.last_checkin_at = _iso_minutes_ago(6.0)  # last check 6 min ago
    retro_dict = wb.to_watch().to_dict()
    insert_watch(tmp_db, watch=retro_dict)

    events: list = []
    bus.subscribe(lambda e: events.append(e))

    monitor._retrospective_price_check(retro_dict)

    updated = get_watch(tmp_db, retro_dict["watch_id"])
    retro = updated["retrospective_data"]
    assert len(retro["price_checks"]) == 1
    assert retro["price_checks"][0]["price"] == 108.0
    # MFE should be positive (price went in bullish direction)
    assert retro["mfe_pct"] == pytest.approx(2.8571, abs=0.01)
    assert retro["mae_pct"] == 0.0  # no adverse movement

    # Now price drops below exit
    mock_market.get_quote.return_value = {"last_price": 103.0, "symbol": "NVDA"}
    # Re-read from DB for latest state
    retro_dict = get_watch(tmp_db, retro_dict["watch_id"])
    monitor._retrospective_price_check(retro_dict)

    updated = get_watch(tmp_db, retro_dict["watch_id"])
    retro = updated["retrospective_data"]
    assert len(retro["price_checks"]) == 2
    # MFE should still be the previous high
    assert retro["mfe_pct"] == pytest.approx(2.8571, abs=0.01)
    # MAE should be negative (price went against us)
    assert retro["mae_pct"] == pytest.approx(-1.9048, abs=0.01)

    retro_events = [e for e in events if e.type == "watch_retrospective_checkin"]
    assert len(retro_events) == 2


def test_seal_watch(tmp_db, bus, mock_market):
    """Sealing should set status=sealed, lifecycle_sealed_at, and final_price."""
    mock_market.get_quote.return_value = {"last_price": 107.0, "symbol": "NVDA"}
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)

    # Create a retrospective watch
    watch_dict = _make_exited_watch_dict(exit_price=105.0)
    wb = WatchBuilder.from_dict(watch_dict)
    wb.start_retrospective(105.0)
    retro_dict = wb.to_watch().to_dict()
    insert_watch(tmp_db, watch=retro_dict)

    events: list = []
    bus.subscribe(lambda e: events.append(e))

    monitor._seal_watch(retro_dict)

    updated = get_watch(tmp_db, retro_dict["watch_id"])
    assert updated["status"] == "sealed"
    assert updated["lifecycle_sealed_at"] is not None
    retro = updated["retrospective_data"]
    assert retro["final_price"] == 107.0

    sealed_events = [e for e in events if e.type == "watch_sealed"]
    assert len(sealed_events) == 1


def test_retrospective_auto_seal_after_max_minutes(tmp_db, bus, mock_market):
    """Retrospective watch past max_retro_minutes should be sealed."""
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)

    # Create a retrospective watch with started_at 70 min ago (> 60 min max)
    watch_dict = _make_exited_watch_dict(exit_price=105.0)
    wb = WatchBuilder.from_dict(watch_dict)
    wb.start_retrospective(105.0)
    wb.retrospective_data["started_at"] = _iso_minutes_ago(70.0)
    retro_dict = wb.to_watch().to_dict()
    insert_watch(tmp_db, watch=retro_dict)

    monitor._retrospective_cycle(retro_dict)

    updated = get_watch(tmp_db, retro_dict["watch_id"])
    assert updated["status"] == "sealed"


def test_is_retro_due(tmp_db, bus, mock_market):
    """Retrospective due check should respect schedule intervals."""
    monitor = WatchMonitor(settings=_fake_settings(), db=tmp_db, bus=bus, market=mock_market)

    # Never checked → due
    watch_dict = _make_exited_watch_dict()
    wb = WatchBuilder.from_dict(watch_dict)
    wb.start_retrospective(105.0)
    wb.last_checkin_at = None
    retro_dict = wb.to_watch().to_dict()
    assert monitor._is_retro_due(retro_dict, minutes_since_exit=3.0) is True

    # Checked 2 min ago, 3 min since exit (5 min interval) → not due
    wb.last_checkin_at = _iso_minutes_ago(2.0)
    retro_dict = wb.to_watch().to_dict()
    assert monitor._is_retro_due(retro_dict, minutes_since_exit=3.0) is False

    # Checked 6 min ago, 10 min since exit (5 min interval) → due
    wb.last_checkin_at = _iso_minutes_ago(6.0)
    retro_dict = wb.to_watch().to_dict()
    assert monitor._is_retro_due(retro_dict, minutes_since_exit=10.0) is True


def test_full_lifecycle_via_check_cycles(tmp_db, bus, mock_market):
    """Watch should progress: holding → exited → retrospective → sealed."""
    mock_market.get_quote.return_value = {"last_price": 85.0, "symbol": "NVDA"}
    settings = _fake_settings()
    settings.watch_max_retro_minutes = 0  # seal immediately in retrospective
    monitor = WatchMonitor(settings=settings, db=tmp_db, bus=bus, market=mock_market)

    # 1. Create holding watch, trigger stop-loss exit
    watch_dict = _make_watch_dict(minutes_ago=3.0, entry_price=100.0)
    insert_watch(tmp_db, watch=watch_dict)
    wid = watch_dict["watch_id"]

    # Cycle 1: holding → exited (stop-loss)
    monitor.run_check_cycle()
    w = get_watch(tmp_db, wid)
    assert w["status"] == "exited"

    # Cycle 2: exited → retrospective
    monitor.run_check_cycle()
    w = get_watch(tmp_db, wid)
    assert w["status"] == "retrospective"

    # Cycle 3: retrospective → sealed (max_retro_minutes=0)
    monitor.run_check_cycle()
    w = get_watch(tmp_db, wid)
    assert w["status"] == "sealed"
    assert w["lifecycle_sealed_at"] is not None

    # Cycle 4: sealed watches are not processed (no error)
    monitor.run_check_cycle()
    w = get_watch(tmp_db, wid)
    assert w["status"] == "sealed"


def test_retrospective_data_roundtrip():
    """WatchBuilder.from_dict should preserve retrospective_data."""
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="NVDA",
        entry_price=100.0,
        signal=FakeSignal(),
    )
    wb.record_exit(price=105.0, reason="Target reached")
    wb.start_retrospective(105.0)
    wb.retrospective_data["price_checks"] = [
        {"time": "2026-02-12T10:00:00+00:00", "price": 106.0, "pnl_since_exit_pct": 0.95}
    ]
    wb.retrospective_data["mfe_pct"] = 0.95
    original = wb.to_watch().to_dict()

    rebuilt = WatchBuilder.from_dict(original)
    assert rebuilt.status == "retrospective"
    assert rebuilt.retrospective_data is not None
    assert rebuilt.retrospective_data["exit_price"] == 105.0
    assert len(rebuilt.retrospective_data["price_checks"]) == 1
    assert rebuilt.retrospective_data["mfe_pct"] == 0.95

    roundtripped = rebuilt.to_watch().to_dict()
    assert roundtripped == original


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
        watch_max_retro_minutes = 60
        watch_checkin_model = "gemini-3-flash"
        debug = False
    return S()
