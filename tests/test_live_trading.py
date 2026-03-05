"""Tests for live trading infrastructure (Phase 1-2).

Tests LiveConfig, evaluate_exit, LiveExitMonitor, LivePortfolioManager,
and market hours utilities.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trader.db.database import (
    activate_live_config,
    deactivate_live_config,
    delete_live_config,
    get_active_live_config,
    get_all_live_configs,
    get_live_config,
    insert_live_config,
    insert_watch,
    get_active_watches,
    open_sqlite,
    update_watch,
)
from trader.market.backtest import ExitResult, evaluate_exit
from trader.market.market_hours import ET, add_market_hours, is_market_open
from trader.models.live_config import LiveConfig
from trader.models.watch import WatchBuilder, WatchEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite database for each test."""
    return open_sqlite(str(tmp_path / "test.db"))


@pytest.fixture
def sample_config():
    """Sample LiveConfig matching best backtest parameters."""
    return LiveConfig.create(
        name="Test VDD Config",
        filters={"confidence_min": 85},
        allocation="max_positions",
        allocation_params={"max_pos": 20, "when_full": "replace", "rank_method": "momentum"},
        starting_capital=100000.0,
        exit_strategy="volume_delta_divergence",
        exit_params={"lookback": 80},
        guard_stop_pct=5.0,
        min_hold=5,
        price_delay_minutes=5,
    )


def _make_bars(n: int = 100, start_price: float = 100.0, trend: float = 0.01) -> pd.DataFrame:
    """Generate synthetic 1-min OHLCV bars."""
    et = ZoneInfo("US/Eastern")
    base = datetime(2026, 3, 5, 9, 30, tzinfo=et)
    times = [base + timedelta(minutes=i) for i in range(n)]

    np.random.seed(42)
    closes = [start_price]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + trend + np.random.normal(0, 0.002)))
    closes = np.array(closes)

    df = pd.DataFrame({
        "Open": closes * (1 - np.random.uniform(0, 0.001, n)),
        "High": closes * (1 + np.random.uniform(0, 0.003, n)),
        "Low": closes * (1 - np.random.uniform(0, 0.003, n)),
        "Close": closes,
        "Volume": np.random.randint(1000, 50000, n),
    }, index=pd.DatetimeIndex([t.replace(tzinfo=None) for t in times], name="Time"))

    return df


def _make_snapshot(
    snapshot_id: str = "snap_test",
    symbol: str = "AAPL",
    confidence: float = 0.92,
    direction: str = "bullish",
    entry_price: float = 150.0,
) -> dict:
    """Create a minimal snapshot dict for testing."""
    return {
        "snapshot_id": snapshot_id,
        "trigger": {"symbols": [symbol], "headline": "Test news"},
        "prediction": {
            "confidence": confidence,
            "direction": direction,
            "entry_price": entry_price,
        },
        "price_at": {"5": entry_price},
        "price_context": {"lastPrice": entry_price},
        "triage": {"action": "investigate"},
    }


# ---------------------------------------------------------------------------
# LiveConfig tests
# ---------------------------------------------------------------------------


class TestLiveConfig:
    def test_create_and_roundtrip(self, sample_config):
        d = sample_config.to_dict()
        restored = LiveConfig.from_dict(d)
        assert restored.name == sample_config.name
        assert restored.exit_strategy == "volume_delta_divergence"
        assert restored.guard_stop_pct == 5.0
        assert restored.config_id.startswith("lc_")

    def test_db_crud(self, db, sample_config):
        # Insert
        assert insert_live_config(db, config=sample_config.to_dict())
        # Duplicate
        assert not insert_live_config(db, config=sample_config.to_dict())
        # Get
        fetched = get_live_config(db, sample_config.config_id)
        assert fetched["name"] == "Test VDD Config"
        # List
        all_configs = get_all_live_configs(db)
        assert len(all_configs) == 1
        # Activate
        assert activate_live_config(db, sample_config.config_id)
        active = get_active_live_config(db)
        assert active["config_id"] == sample_config.config_id
        assert active["active"] is True
        # Deactivate
        assert deactivate_live_config(db, sample_config.config_id)
        assert get_active_live_config(db) is None
        # Delete
        assert delete_live_config(db, sample_config.config_id)
        assert get_all_live_configs(db) == []

    def test_multiple_active(self, db):
        """Multiple configs can be active simultaneously (multi-portfolio)."""
        from trader.db.database import get_active_live_configs
        cfg1 = LiveConfig.create(
            name="Config A", filters={}, allocation="none",
            allocation_params={}, starting_capital=100000,
            exit_strategy="fixed_stop_loss", exit_params={"stop_pct": 5},
        )
        cfg2 = LiveConfig.create(
            name="Config B", filters={}, allocation="none",
            allocation_params={}, starting_capital=100000,
            exit_strategy="fixed_take_profit", exit_params={"reward_pct": 5},
        )
        insert_live_config(db, config=cfg1.to_dict())
        insert_live_config(db, config=cfg2.to_dict())
        activate_live_config(db, cfg1.config_id)
        activate_live_config(db, cfg2.config_id)
        # Both should be active
        active = get_active_live_configs(db)
        active_ids = {c["config_id"] for c in active}
        assert cfg1.config_id in active_ids
        assert cfg2.config_id in active_ids
        assert len(active) == 2
        # Deactivate one — other stays active
        deactivate_live_config(db, cfg1.config_id)
        active = get_active_live_configs(db)
        assert len(active) == 1
        assert active[0]["config_id"] == cfg2.config_id


# ---------------------------------------------------------------------------
# evaluate_exit tests
# ---------------------------------------------------------------------------


class TestEvaluateExit:
    def test_still_open(self):
        """If no exit signal, returns should_exit=False."""
        bars = _make_bars(50, trend=0.001)  # gentle uptrend, no divergence
        result = evaluate_exit(
            "volume_delta_divergence", {"lookback": 80},
            bars=bars, entry_idx=5, entry_price=100.0,
            min_hold=5,
        )
        assert isinstance(result, ExitResult)
        assert not result.should_exit
        assert result.reason == "still_open"

    def test_guard_stop_triggers(self):
        """Guard stop should trigger on a big drop."""
        bars = _make_bars(50, trend=-0.01)  # strong downtrend
        result = evaluate_exit(
            "volume_delta_divergence", {"lookback": 80},
            bars=bars, entry_idx=0, entry_price=100.0,
            guard_stop_pct=2.0, min_hold=1,
        )
        assert result.should_exit
        assert result.reason == "guard_stop"

    def test_fixed_stop_loss(self):
        """Fixed stop loss should work standalone."""
        bars = _make_bars(50, trend=-0.005)
        result = evaluate_exit(
            "fixed_stop_loss", {"stop_pct": 3.0},
            bars=bars, entry_idx=0, entry_price=100.0,
            min_hold=1,
        )
        assert result.should_exit
        assert result.reason == "stop"

    def test_max_holding_period(self):
        """Max holding period exits after N bars."""
        bars = _make_bars(100, trend=0.0001)
        result = evaluate_exit(
            "max_holding_period", {"max_bars": 30},
            bars=bars, entry_idx=0, entry_price=100.0,
            min_hold=1,
        )
        assert result.should_exit
        assert result.reason == "time"
        # bars_held includes min_hold offset (1) + strategy bars (30)
        assert result.bars_held == 31

    def test_unknown_strategy(self):
        """Unknown strategy returns gracefully."""
        bars = _make_bars(10)
        result = evaluate_exit(
            "nonexistent_strategy", {},
            bars=bars, entry_idx=0, entry_price=100.0,
        )
        assert not result.should_exit
        assert result.reason == "unknown_strategy"

    def test_indicator_cache_reuse(self):
        """Indicator cache should be reusable across calls."""
        bars = _make_bars(100)
        cache: dict = {}
        # Call twice with same cache — should not error
        evaluate_exit(
            "rsi_overbought", {"rsi_period": 14, "threshold": 70},
            bars=bars, entry_idx=5, entry_price=100.0,
            indicator_cache=cache, min_hold=1,
        )
        assert len(cache) > 0  # cache should have entries
        evaluate_exit(
            "rsi_overbought", {"rsi_period": 14, "threshold": 70},
            bars=bars, entry_idx=5, entry_price=100.0,
            indicator_cache=cache, min_hold=1,
        )


# ---------------------------------------------------------------------------
# Market hours tests
# ---------------------------------------------------------------------------


class TestMarketHours:
    def test_market_open_during_hours(self):
        dt = datetime(2026, 3, 5, 10, 0, tzinfo=ET)  # Thu 10am
        assert is_market_open(dt)

    def test_market_closed_after_hours(self):
        dt = datetime(2026, 3, 5, 17, 0, tzinfo=ET)  # Thu 5pm
        assert not is_market_open(dt)

    def test_market_closed_weekend(self):
        dt = datetime(2026, 3, 7, 10, 0, tzinfo=ET)  # Sat
        assert not is_market_open(dt)

    def test_add_hours_same_day(self):
        start = datetime(2026, 3, 5, 9, 30, tzinfo=ET)
        result = add_market_hours(start, 3.0)
        assert result.hour == 12
        assert result.minute == 30

    def test_add_hours_cross_day(self):
        start = datetime(2026, 3, 5, 9, 30, tzinfo=ET)
        result = add_market_hours(start, 6.5)  # exactly one trading day
        assert result.hour == 16
        assert result.minute == 0

    def test_add_hours_skip_weekend(self):
        # Friday 3pm + 3 market hours = crosses weekend
        start = datetime(2026, 3, 6, 15, 0, tzinfo=ET)  # Fri 3pm
        result = add_market_hours(start, 3.0)
        # 1h left on Friday (15:00-16:00) → 2h remaining
        # Next market day is Monday → Mon 9:30 + 2h = Mon 11:30
        assert result.weekday() == 0  # Monday
        assert result.hour == 11
        assert result.minute == 30

    def test_add_hours_from_after_hours(self):
        start = datetime(2026, 3, 5, 18, 0, tzinfo=ET)  # Thu 6pm
        result = add_market_hours(start, 1.0)
        # Advances to Friday 9:30, then +1h = 10:30
        assert result.weekday() == 4  # Friday
        assert result.hour == 10
        assert result.minute == 30


# ---------------------------------------------------------------------------
# Watch model (live trading fields) tests
# ---------------------------------------------------------------------------


class TestWatchLiveFields:
    def test_create_from_live_config(self):
        wb = WatchBuilder.create_from_live_config(
            snapshot_id="snap_1",
            symbol="NVDA",
            entry_price=800.0,
            confidence=0.95,
            direction="bullish",
            live_config_id="lc_abc",
            exit_strategy="volume_delta_divergence",
            exit_params={"lookback": 80},
        )
        watch = wb.to_watch()
        assert watch.live_config_id == "lc_abc"
        assert watch.exit_strategy == "volume_delta_divergence"
        assert watch.exit_params == {"lookback": 80}
        assert watch.status == "holding"

    def test_cooling_off_lifecycle(self):
        wb = WatchBuilder.create_from_live_config(
            snapshot_id="snap_1", symbol="AAPL", entry_price=150.0,
            confidence=0.9, direction="bullish",
            live_config_id="lc_1", exit_strategy="vdd", exit_params={},
        )
        wb.record_exit(price=155.0, reason="signal")
        assert wb.status == "exited"

        expires = "2026-03-10T14:00:00-04:00"
        wb.start_cooling_off(expires)
        assert wb.status == "cooling_off"
        assert wb.cooling_off_until == expires

        wb.seal()
        assert wb.status == "sealed"
        assert wb.lifecycle_sealed_at is not None

    def test_backward_compat_old_watch(self):
        """Old watches without live fields should still work."""
        old_dict = {
            "watch_id": "watch_old",
            "symbol": "TSLA",
            "status": "holding",
            "entry": {
                "snapshot_id": "s1", "price": 200.0,
                "time": "2026-01-01T00:00:00Z",
                "confidence": 0.8, "direction": "bullish",
                "horizon": "1d", "thesis": "test",
            },
            "exit": None,
            "created_at": "2026-01-01T00:00:00Z",
            "last_checkin_at": None,
            "lifecycle_sealed_at": None,
            "monitoring_snapshot_ids": ["s2"],
            "checkin_history": [{"action": "hold"}],
        }
        builder = WatchBuilder.from_dict(old_dict)
        watch = builder.to_watch()
        assert watch.live_config_id is None
        assert watch.exit_strategy is None
        assert watch.monitoring_snapshot_ids == ["s2"]


# ---------------------------------------------------------------------------
# LivePortfolioManager tests
# ---------------------------------------------------------------------------


class TestLivePortfolioManager:
    def test_skip_when_no_active_config(self, db):
        from trader.online.live_monitor import LivePortfolioManager
        pm = LivePortfolioManager(db=db)
        snap = _make_snapshot()
        assert not pm.evaluate_snapshot(snap, "AAPL")

    def test_buy_with_active_config(self, db, sample_config):
        from trader.online.live_monitor import LivePortfolioManager
        insert_live_config(db, config=sample_config.to_dict())
        activate_live_config(db, sample_config.config_id)

        pm = LivePortfolioManager(db=db)
        snap = _make_snapshot(confidence=0.92, direction="bullish")
        result = pm.evaluate_snapshot(snap, "AAPL")
        assert result is True

        # Watch should exist
        watches = get_active_watches(db)
        assert len(watches) == 1
        assert watches[0]["symbol"] == "AAPL"
        assert watches[0]["live_config_id"] == sample_config.config_id
        assert watches[0]["exit_strategy"] == "volume_delta_divergence"

    def test_skip_low_confidence(self, db, sample_config):
        from trader.online.live_monitor import LivePortfolioManager
        insert_live_config(db, config=sample_config.to_dict())
        activate_live_config(db, sample_config.config_id)

        pm = LivePortfolioManager(db=db)
        snap = _make_snapshot(confidence=0.50, direction="bullish")
        assert not pm.evaluate_snapshot(snap, "AAPL")

    def test_skip_neutral_direction(self, db, sample_config):
        from trader.online.live_monitor import LivePortfolioManager
        insert_live_config(db, config=sample_config.to_dict())
        activate_live_config(db, sample_config.config_id)

        pm = LivePortfolioManager(db=db)
        snap = _make_snapshot(confidence=0.95, direction="neutral")
        assert not pm.evaluate_snapshot(snap, "AAPL")

    def test_skip_at_capacity(self, db, sample_config):
        from trader.online.live_monitor import LivePortfolioManager
        # Set max_pos to 1
        sample_config.allocation_params["max_pos"] = 1
        insert_live_config(db, config=sample_config.to_dict())
        activate_live_config(db, sample_config.config_id)

        pm = LivePortfolioManager(db=db)
        # Buy first stock
        snap1 = _make_snapshot(snapshot_id="snap_1", symbol="AAPL")
        assert pm.evaluate_snapshot(snap1, "AAPL")
        # Second should be skipped (at capacity)
        snap2 = _make_snapshot(snapshot_id="snap_2", symbol="TSLA")
        assert not pm.evaluate_snapshot(snap2, "TSLA")


# ---------------------------------------------------------------------------
# LiveExitMonitor tests
# ---------------------------------------------------------------------------


class TestLiveExitMonitor:
    def test_seal_expired_cooling_off(self, db):
        from trader.online.live_monitor import LiveExitMonitor

        # Create a watch in cooling_off state with expired timestamp
        wb = WatchBuilder.create_from_live_config(
            snapshot_id="snap_1", symbol="AAPL", entry_price=150.0,
            confidence=0.9, direction="bullish",
            live_config_id="lc_1", exit_strategy="vdd", exit_params={},
        )
        wb.record_exit(price=155.0, reason="signal")
        # Set cooling_off_until to the past
        wb.start_cooling_off("2020-01-01T00:00:00+00:00")
        watch = wb.to_watch()
        insert_watch(db, watch=watch.to_dict())

        monitor = LiveExitMonitor(db=db)
        monitor.run_cycle()

        # Should be sealed now
        watches = get_active_watches(db)
        assert len(watches) == 0  # sealed watches are not active

    def test_transition_exited_to_cooling_off(self, db, sample_config):
        from trader.online.live_monitor import LiveExitMonitor

        insert_live_config(db, config=sample_config.to_dict())
        activate_live_config(db, sample_config.config_id)

        # Create an exited watch
        wb = WatchBuilder.create_from_live_config(
            snapshot_id="snap_1", symbol="AAPL", entry_price=150.0,
            confidence=0.9, direction="bullish",
            live_config_id=sample_config.config_id,
            exit_strategy="vdd", exit_params={},
        )
        wb.record_exit(price=155.0, reason="signal")
        watch = wb.to_watch()
        insert_watch(db, watch=watch.to_dict())

        monitor = LiveExitMonitor(db=db)
        monitor.run_cycle()

        # Should be in cooling_off now
        watches = get_active_watches(db)
        assert len(watches) == 1
        assert watches[0]["status"] == "cooling_off"
        assert watches[0]["cooling_off_until"] is not None
