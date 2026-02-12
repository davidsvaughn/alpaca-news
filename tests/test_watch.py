"""Tests for Watch model, database helpers, and orchestrator integration."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from trader.db.database import (
    count_holding_watches,
    get_active_watches,
    get_watch,
    insert_watch,
    open_sqlite,
    update_watch,
)
from trader.models.watch import Watch, WatchBuilder, WatchEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeSignal:
    """Minimal stand-in for TradingSignal to avoid importing explorer_agent."""
    def __init__(
        self,
        direction="bullish",
        confidence=0.85,
        horizon="60m",
        key_catalyst="Revenue beat expectations",
    ):
        self.direction = direction
        self.confidence = confidence
        self.horizon = horizon
        self.key_catalyst = key_catalyst


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as d:
        db = open_sqlite(str(Path(d) / "test.db"))
        yield db


# ---------------------------------------------------------------------------
# Watch model tests
# ---------------------------------------------------------------------------


def test_watch_builder_create_from_signal():
    signal = FakeSignal()
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="NVDA",
        entry_price=612.30,
        signal=signal,
    )
    assert wb.symbol == "NVDA"
    assert wb.status == "holding"
    assert wb.entry.price == 612.30
    assert wb.entry.confidence == 0.85
    assert wb.entry.direction == "bullish"
    assert wb.entry.horizon == "60m"
    assert wb.entry.thesis == "Revenue beat expectations"

    watch = wb.to_watch()
    assert isinstance(watch, Watch)
    assert watch.status == "holding"
    assert watch.exit is None
    assert watch.monitoring_snapshot_ids == []
    assert watch.lifecycle_sealed_at is None


def test_watch_lifecycle_transitions():
    signal = FakeSignal()
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="AAPL",
        entry_price=200.00,
        signal=signal,
    )

    # Add monitoring snapshots
    wb.add_monitoring_snapshot("snap_002")
    wb.add_monitoring_snapshot("snap_003")

    # Exit with profit
    wb.record_exit(price=204.00, reason="Target reached", snapshot_id="snap_004")
    assert wb.status == "exited"
    assert wb.exit is not None
    assert wb.exit.realized_pnl_pct == pytest.approx(2.0, abs=0.01)
    assert wb.exit.reason == "Target reached"

    # Add retrospective
    wb.add_retrospective_snapshot("snap_005")

    # Seal
    wb.seal()
    watch = wb.to_watch()
    assert watch.status == "sealed"
    assert len(watch.monitoring_snapshot_ids) == 2
    assert len(watch.retrospective_snapshot_ids) == 1
    assert watch.lifecycle_sealed_at is not None


def test_watch_bearish_pnl():
    """Bearish direction should invert P&L."""
    signal = FakeSignal(direction="bearish")
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="TSLA",
        entry_price=300.00,
        signal=signal,
    )
    # Price drops — profit for bearish thesis
    wb.record_exit(price=291.00, reason="Target hit", snapshot_id="snap_002")
    assert wb.exit is not None
    assert wb.exit.realized_pnl_pct == pytest.approx(3.0, abs=0.01)


def test_watch_to_dict_and_json():
    signal = FakeSignal()
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="NVDA",
        entry_price=612.30,
        signal=signal,
    )
    watch = wb.to_watch()
    d = watch.to_dict()
    assert d["symbol"] == "NVDA"
    assert d["entry"]["price"] == 612.30
    assert d["status"] == "holding"

    j = watch.to_json()
    parsed = json.loads(j)
    assert parsed["watch_id"] == watch.watch_id


def test_watch_persist(tmp_path):
    signal = FakeSignal()
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="NVDA",
        entry_price=612.30,
        signal=signal,
    )
    watch = wb.to_watch()
    out = tmp_path / "watches" / f"{watch.watch_id}.json"
    watch.persist(out)
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["symbol"] == "NVDA"


# ---------------------------------------------------------------------------
# Database tests
# ---------------------------------------------------------------------------


def test_insert_and_get_watch(tmp_db):
    signal = FakeSignal()
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="NVDA",
        entry_price=612.30,
        signal=signal,
    )
    watch = wb.to_watch()
    d = watch.to_dict()

    inserted = insert_watch(tmp_db, watch=d)
    assert inserted is True

    # Duplicate insert is idempotent
    inserted2 = insert_watch(tmp_db, watch=d)
    assert inserted2 is False

    # Fetch
    fetched = get_watch(tmp_db, watch.watch_id)
    assert fetched is not None
    assert fetched["symbol"] == "NVDA"
    assert fetched["entry"]["price"] == 612.30


def test_count_holding_watches(tmp_db):
    assert count_holding_watches(tmp_db) == 0

    for i in range(3):
        signal = FakeSignal()
        wb = WatchBuilder.create_from_signal(
            snapshot_id=f"snap_{i}",
            symbol="NVDA",
            entry_price=100.0 + i,
            signal=signal,
        )
        watch = wb.to_watch()
        insert_watch(tmp_db, watch=watch.to_dict())

    assert count_holding_watches(tmp_db) == 3


def test_get_active_watches(tmp_db):
    # Create 2 holding + 1 sealed
    for i in range(3):
        signal = FakeSignal()
        wb = WatchBuilder.create_from_signal(
            snapshot_id=f"snap_{i}",
            symbol="NVDA",
            entry_price=100.0 + i,
            signal=signal,
        )
        if i == 2:
            wb.record_exit(price=105.0, reason="test")
            wb.seal()
        watch = wb.to_watch()
        insert_watch(tmp_db, watch=watch.to_dict())

    active = get_active_watches(tmp_db)
    assert len(active) == 2  # sealed one excluded


def test_update_watch(tmp_db):
    signal = FakeSignal()
    wb = WatchBuilder.create_from_signal(
        snapshot_id="snap_001",
        symbol="NVDA",
        entry_price=612.30,
        signal=signal,
    )
    watch = wb.to_watch()
    insert_watch(tmp_db, watch=watch.to_dict())

    # Exit the watch
    wb.record_exit(price=620.00, reason="Target reached", snapshot_id="snap_002")
    updated_watch = wb.to_watch()
    update_watch(tmp_db, watch.watch_id, updated_watch.to_dict())

    fetched = get_watch(tmp_db, watch.watch_id)
    assert fetched is not None
    assert fetched["status"] == "exited"
    assert fetched["exit"]["price"] == 620.00

    # Holding count should be 0 now
    assert count_holding_watches(tmp_db) == 0


# ---------------------------------------------------------------------------
# Orchestrator integration test
# ---------------------------------------------------------------------------


def test_extract_entry_price():
    """Verify _extract_entry_price handles various price_context formats."""
    from trader.online.orchestrator import _extract_entry_price

    class FakeSnapshot:
        def __init__(self, pc):
            self.price_context = pc

    # Schwab format
    snap = FakeSnapshot({"NVDA": {"lastPrice": 612.30, "netChange": 5.2}})
    assert _extract_entry_price(snap, "NVDA") == pytest.approx(612.30)

    # yfinance format
    snap = FakeSnapshot({"AAPL": {"regularMarketPrice": 185.50}})
    assert _extract_entry_price(snap, "AAPL") == pytest.approx(185.50)

    # Missing symbol
    snap = FakeSnapshot({"NVDA": {"lastPrice": 612.30}})
    assert _extract_entry_price(snap, "TSLA") is None

    # Empty context
    snap = FakeSnapshot({})
    assert _extract_entry_price(snap, "NVDA") is None

    # None context
    snap = FakeSnapshot(None)
    assert _extract_entry_price(snap, "NVDA") is None
