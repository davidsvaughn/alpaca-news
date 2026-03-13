"""Sanity-check tests for backtest computations.

Verifies annualized return math, bar counting, duration handling,
cost deductions, equity curve construction, and edge cases using
synthetic 1-min bar DataFrames (no network calls).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trader.market.backtest import (
    BacktestResult,
    _extract_periodic_closes,
    _filter_trading_hours,
    _is_backtest_cache_valid,
    apply_allocation,
    compute_ann_a,
    compute_ann_b,
    compute_portfolio_sim,
)

# ---------------------------------------------------------------------------
# Helpers — synthetic 1-min bar DataFrames
# ---------------------------------------------------------------------------

_BARS_PER_DAY = 390
_TRADING_DAYS_PER_YEAR = 252


def _make_bars(
    start: str,
    n_bars: int,
    open_price: float = 100.0,
    drift_per_bar: float = 0.0,
    spread: float = 0.1,
    volume: int = 1000,
) -> pd.DataFrame:
    """Create a synthetic 1-min OHLCV DataFrame with market-hours-only bars.

    Generates bars starting at `start` (e.g. "2026-01-20 09:30"), skipping
    overnight/weekend gaps — exactly like real cached data.  Each trading day
    contributes up to 390 bars (09:30–15:59).

    Price drifts linearly by `drift_per_bar` per bar from `open_price`.
    """
    timestamps: list[pd.Timestamp] = []
    ts = pd.Timestamp(start)

    while len(timestamps) < n_bars:
        # Skip weekends
        if ts.weekday() >= 5:
            ts = ts + pd.Timedelta(days=(7 - ts.weekday()))
            ts = ts.replace(hour=9, minute=30)
            continue
        # Only allow 09:30–15:59
        t_min = ts.hour * 60 + ts.minute
        if t_min < 9 * 60 + 30:
            ts = ts.replace(hour=9, minute=30)
            continue
        if t_min >= 16 * 60:
            # Jump to next day 09:30
            ts = ts + pd.Timedelta(days=1)
            ts = ts.replace(hour=9, minute=30)
            continue
        timestamps.append(ts)
        ts = ts + pd.Timedelta(minutes=1)

    idx = pd.DatetimeIndex(timestamps)
    closes = [open_price + drift_per_bar * i for i in range(n_bars)]
    df = pd.DataFrame(
        {
            "Open": [c - spread / 2 for c in closes],
            "High": [c + spread for c in closes],
            "Low": [c - spread for c in closes],
            "Close": closes,
            "Volume": [volume] * n_bars,
        },
        index=idx,
    )
    return df


def _make_result(
    pnl_pct: float,
    bars_held: int,
    entry_price: float = 100.0,
    periodic_closes: list[tuple[str, float]] | None = None,
) -> BacktestResult:
    """Create a minimal BacktestResult for testing compute functions."""
    exit_price = entry_price * (1 + pnl_pct / 100)
    return BacktestResult(
        snapshot_id="test",
        symbol="TEST",
        entry_price=entry_price,
        entry_time="2026-01-20T10:00:00",
        exit_price=round(exit_price, 4),
        exit_time="2026-01-20T12:00:00",
        pnl_pct=pnl_pct,
        exit_reason="signal",
        bars_held=bars_held,
        periodic_closes=periodic_closes,
    )


# ---------------------------------------------------------------------------
# Test: compute_ann_a hand calculations
# ---------------------------------------------------------------------------


class TestComputeAnnA:
    """Method A — unlimited capital, time-weighted log return."""

    def test_single_trade_1pct_1day(self):
        """1% gain over exactly 1 trading day → Ann(A) ≈ (e^(252×ln(1.01)) - 1)×100."""
        r = _make_result(pnl_pct=1.0, bars_held=_BARS_PER_DAY)
        stats = compute_ann_a([r, r])  # need ≥2 trades
        assert stats is not None
        # Hand calc: daily_log = ln(1.01)/1 = 0.00995; ann = e^(252*0.00995) - 1
        expected = (math.exp(252 * math.log(1.01)) - 1) * 100
        assert stats["ann"] == pytest.approx(expected, rel=0.01)

    def test_single_trade_half_day(self):
        """1% gain in half a day → double the daily rate → much higher Ann(A)."""
        half_day = _BARS_PER_DAY // 2  # 195 bars
        r = _make_result(pnl_pct=1.0, bars_held=half_day)
        stats = compute_ann_a([r, r])
        assert stats is not None
        # daily_log = ln(1.01) / 0.5 = 2×ln(1.01)
        daily_log = math.log(1.01) / (half_day / _BARS_PER_DAY)
        expected = (math.exp(252 * daily_log) - 1) * 100
        assert stats["ann"] == pytest.approx(expected, rel=0.01)

    def test_losing_trade_negative_ann(self):
        """A losing trade should produce negative annualized return."""
        r = _make_result(pnl_pct=-2.0, bars_held=_BARS_PER_DAY)
        stats = compute_ann_a([r, r])
        assert stats is not None
        assert stats["ann"] < 0


class TestBacktestProgress:
    def test_apply_allocation_progress_is_chronological(self):
        events: list[dict[str, object]] = []
        results = [
            BacktestResult(
                snapshot_id="late",
                symbol="AAPL",
                entry_price=100.0,
                entry_time="2026-01-03T10:00:00",
                exit_price=102.0,
                exit_time="2026-01-03T11:00:00",
                pnl_pct=2.0,
                exit_reason="signal",
                bars_held=60,
            ),
            BacktestResult(
                snapshot_id="early",
                symbol="MSFT",
                entry_price=100.0,
                entry_time="2026-01-02T10:00:00",
                exit_price=101.0,
                exit_time="2026-01-02T11:00:00",
                pnl_pct=1.0,
                exit_reason="signal",
                bars_held=60,
            ),
        ]
        entries = [
            {"snapshot_id": "late", "entry_time": "2026-01-03T10:00:00", "confidence": 0.8},
            {"snapshot_id": "early", "entry_time": "2026-01-02T10:00:00", "confidence": 0.7},
        ]

        final, stats = apply_allocation(
            results,
            entries,
            "max_positions",
            {"max_pos": 2},
            progress_cb=events.append,
        )

        assert len(final) == 2
        assert stats == {"taken": 2, "skipped": 0, "replaced": 0}
        assert events[0]["phase"] == "allocation"
        assert events[0]["processed"] == 0
        assert events[-1]["processed"] == 2
        assert events[-1]["total"] == 2
        assert events[-1]["chrono"] is True
        assert events[1]["current_entry_time"] == "2026-01-02T10:00:00"
        assert events[2]["current_entry_time"] == "2026-01-03T10:00:00"

    def test_portfolio_sim_progress_reports_equity(self):
        events: list[dict[str, object]] = []
        results = [
            BacktestResult(
                snapshot_id="a",
                symbol="AAPL",
                entry_price=100.0,
                entry_time="2026-01-02T10:00:00",
                exit_price=110.0,
                exit_time="2026-01-02T11:00:00",
                pnl_pct=10.0,
                exit_reason="signal",
                bars_held=60,
            ),
            BacktestResult(
                snapshot_id="b",
                symbol="MSFT",
                entry_price=100.0,
                entry_time="2026-01-02T12:00:00",
                exit_price=95.0,
                exit_time="2026-01-02T13:00:00",
                pnl_pct=-5.0,
                exit_reason="signal",
                bars_held=60,
            ),
        ]

        sim = compute_portfolio_sim(
            results,
            "max_positions",
            {"max_pos": 1},
            1000.0,
            0,
            progress_cb=events.append,
        )

        assert sim["sim_ending"] == pytest.approx(1045.0)
        assert events[0]["phase"] == "portfolio_sim"
        assert events[0]["processed"] == 0
        assert events[-1]["phase_progress"] == pytest.approx(1.0)
        assert events[-1]["portfolio_value"] == pytest.approx(sim["sim_ending"])
        assert events[-1]["chrono"] is True

    def test_portfolio_sim_normalizes_mixed_timezone_timestamps(self):
        results = [
            BacktestResult(
                snapshot_id="mixed",
                symbol="AAPL",
                entry_price=100.0,
                entry_time="2026-01-02T15:00:00+00:00",
                exit_price=102.0,
                exit_time="2026-01-02T11:00:00",
                pnl_pct=2.0,
                exit_reason="signal",
                bars_held=60,
            ),
        ]

        sim = compute_portfolio_sim(
            results,
            "max_positions",
            {"max_pos": 1},
            1000.0,
            0,
        )

        assert sim["sim_ending"] == pytest.approx(1020.0)
        assert sim["sim_span_days"] == pytest.approx(round(60 / _BARS_PER_DAY, 2), rel=1e-6)
        assert sim["sim_daily_pct"] is not None

    def test_finalized_cache_result_stays_valid_when_data_horizon_advances(self):
        result = BacktestResult(
            snapshot_id="done",
            symbol="AAPL",
            entry_price=100.0,
            entry_time="2026-01-02T10:00:00",
            exit_price=103.0,
            exit_time="2026-01-02T11:00:00",
            pnl_pct=3.0,
            exit_reason="signal",
            bars_held=60,
        )

        assert _is_backtest_cache_valid(
            result,
            {"data_end": "2026-01-02T12:00:00"},
            "2026-01-02T15:30:00",
        ) is True

    def test_open_cache_result_invalidates_when_data_horizon_advances(self):
        result = BacktestResult(
            snapshot_id="open",
            symbol="AAPL",
            entry_price=100.0,
            entry_time="2026-01-02T10:00:00",
            exit_price=101.0,
            exit_time="2026-01-02T12:00:00",
            pnl_pct=1.0,
            exit_reason="still_open",
            bars_held=120,
        )

        assert _is_backtest_cache_valid(
            result,
            {"data_end": "2026-01-02T12:00:00"},
            "2026-01-02T12:00:00",
        ) is True
        assert _is_backtest_cache_valid(
            result,
            {"data_end": "2026-01-02T12:00:00"},
            "2026-01-02T15:30:00",
        ) is False

class TestComputeAnnAExtra:
    def test_zero_bars_floors_to_one(self):
        """bars_held=0 should be floored to 1 bar, not cause division by zero."""
        r = _make_result(pnl_pct=0.5, bars_held=0)
        stats = compute_ann_a([r, r])
        assert stats is not None
        # With 1 bar: days = 1/390 ≈ 0.00256; daily_log = ln(1.005)/0.00256
        days = 1 / _BARS_PER_DAY
        daily_log = math.log(1.005) / days
        expected = (math.exp(252 * daily_log) - 1) * 100
        assert stats["ann"] == pytest.approx(expected, rel=0.01)

    def test_skip_none_pnl(self):
        """Trades with pnl_pct=None should be silently skipped."""
        good = _make_result(pnl_pct=1.0, bars_held=_BARS_PER_DAY)
        bad = _make_result(pnl_pct=1.0, bars_held=_BARS_PER_DAY)
        bad.pnl_pct = None
        stats = compute_ann_a([good, bad, good])
        assert stats is not None
        # Should be same as two good trades
        stats2 = compute_ann_a([good, good])
        assert stats["ann"] == pytest.approx(stats2["ann"], rel=1e-9)

    def test_insufficient_trades_returns_none(self):
        """Fewer than 2 valid trades → None."""
        r = _make_result(pnl_pct=1.0, bars_held=390)
        assert compute_ann_a([r]) is None
        assert compute_ann_a([]) is None

    def test_daily_pnl_matches_ann_a_intermediate(self):
        """daily_pnl should equal daily_log × 100, the pre-annualization rate."""
        r = _make_result(pnl_pct=1.0, bars_held=_BARS_PER_DAY)
        stats = compute_ann_a([r, r])
        assert stats is not None
        # Hand calc: daily_log = ln(1.01) / 1.0
        expected_daily_pnl = math.log(1.01) * 100  # %/day
        assert stats["daily_pnl"] == pytest.approx(expected_daily_pnl, rel=1e-6)

    def test_daily_pnl_negative_for_losing_trades(self):
        """Losing trades should produce negative daily_pnl."""
        r = _make_result(pnl_pct=-2.0, bars_held=_BARS_PER_DAY)
        stats = compute_ann_a([r, r])
        assert stats is not None
        assert stats["daily_pnl"] < 0

    def test_daily_pnl_scales_with_duration(self):
        """Same P&L in half the time → double the daily_pnl."""
        r_full = _make_result(pnl_pct=1.0, bars_held=_BARS_PER_DAY)
        r_half = _make_result(pnl_pct=1.0, bars_held=_BARS_PER_DAY // 2)
        stats_full = compute_ann_a([r_full, r_full])
        stats_half = compute_ann_a([r_half, r_half])
        assert stats_full is not None and stats_half is not None
        assert stats_half["daily_pnl"] == pytest.approx(
            stats_full["daily_pnl"] * 2, rel=0.02,
        )

    def test_sharpe_positive_for_consistent_gains(self):
        """Consistent positive trades should produce positive Sharpe."""
        results = [_make_result(pnl_pct=0.5, bars_held=_BARS_PER_DAY) for _ in range(10)]
        stats = compute_ann_a(results)
        assert stats is not None
        # All identical → std=0 → Sharpe is None (or infinite)
        # Use slight variation instead
        results2 = [
            _make_result(pnl_pct=0.5 + i * 0.01, bars_held=_BARS_PER_DAY)
            for i in range(10)
        ]
        stats2 = compute_ann_a(results2)
        assert stats2 is not None
        assert stats2["sharpe"] is not None
        assert stats2["sharpe"] > 0


# ---------------------------------------------------------------------------
# Test: compute_ann_b hand calculations
# ---------------------------------------------------------------------------


class TestComputeAnnB:
    """Method B — fixed capital split, equity curve from periodic closes."""

    def _make_periodic_closes(self, entry_price, exit_price, n_periods):
        """Linear price path from entry to exit over n periods."""
        prices = np.linspace(entry_price, exit_price, n_periods + 1)
        base = pd.Timestamp("2026-01-20 10:00")
        closes = []
        for i in range(1, len(prices)):
            ts = (base + pd.Timedelta(hours=i)).isoformat()
            closes.append((ts, float(prices[i])))
        return closes

    def test_single_trade_known_return(self):
        """A single trade: 100→102 (2%) over 10 hourly periods."""
        pc = self._make_periodic_closes(100.0, 102.0, 10)
        r = _make_result(pnl_pct=2.0, bars_held=600, entry_price=100.0, periodic_closes=pc)
        # Need 2 trades for the function to work (≥2 timestamps)
        stats = compute_ann_b([r])
        assert stats is not None
        # Total log return = ln(102/100) = ln(1.02)
        # 10 periods; periods_per_year = 252*390/60 = 1638
        # CAGR = equity^(1638/10) - 1
        total_log = math.log(1.02)
        equity = math.exp(total_log)
        periods_per_year = 252 * 390 / 60
        expected = (equity ** (periods_per_year / 10) - 1) * 100
        assert stats["ann"] == pytest.approx(expected, rel=0.05)

    def test_two_overlapping_trades_average(self):
        """Two concurrent trades: returns should be averaged (1/N split)."""
        base = pd.Timestamp("2026-01-20 10:00")
        # Trade A: 100 → 104 (4%) in 2 periods
        pc_a = [
            ((base + pd.Timedelta(hours=1)).isoformat(), 102.0),
            ((base + pd.Timedelta(hours=2)).isoformat(), 104.0),
        ]
        # Trade B: 100 → 100 (0%) in 2 periods
        pc_b = [
            ((base + pd.Timedelta(hours=1)).isoformat(), 100.0),
            ((base + pd.Timedelta(hours=2)).isoformat(), 100.0),
        ]
        r_a = _make_result(pnl_pct=4.0, bars_held=120, entry_price=100.0, periodic_closes=pc_a)
        r_b = _make_result(pnl_pct=0.0, bars_held=120, entry_price=100.0, periodic_closes=pc_b)

        stats = compute_ann_b([r_a, r_b])
        assert stats is not None

        # At each timestamp, portfolio return = mean of individual returns
        # Period 1: mean(ln(102/100), ln(100/100)) = ln(1.02)/2
        # Period 2: mean(ln(104/102), ln(100/100)) = ln(104/102)/2
        r1 = math.log(102 / 100) / 2
        r2 = math.log(104 / 102) / 2
        equity = math.exp(r1) * math.exp(r2)
        periods_per_year = 252 * 390 / 60
        expected = (equity ** (periods_per_year / 2) - 1) * 100
        assert stats["ann"] == pytest.approx(expected, rel=0.01)

    def test_skip_missing_periodic_closes(self):
        """Trades without periodic_closes should be skipped."""
        pc = [
            ("2026-01-20T11:00:00", 101.0),
            ("2026-01-20T12:00:00", 102.0),
        ]
        good = _make_result(pnl_pct=2.0, bars_held=120, entry_price=100.0, periodic_closes=pc)
        bad = _make_result(pnl_pct=1.0, bars_held=120, entry_price=100.0, periodic_closes=None)
        stats = compute_ann_b([good, bad])
        stats_good_only = compute_ann_b([good])
        # bad trade should be skipped — same result
        assert stats is not None
        assert stats_good_only is not None
        assert stats["ann"] == pytest.approx(stats_good_only["ann"], rel=1e-9)

    def test_insufficient_timestamps_returns_none(self):
        """Fewer than 2 unique timestamps → None."""
        pc = [("2026-01-20T11:00:00", 101.0)]
        r = _make_result(pnl_pct=1.0, bars_held=60, entry_price=100.0, periodic_closes=pc)
        assert compute_ann_b([r]) is None
        assert compute_ann_b([]) is None


# ---------------------------------------------------------------------------
# Test: bars_held counts market bars, not wall-clock time
# ---------------------------------------------------------------------------


class TestBarsHeldMarketHoursOnly:
    """Verify that bar-based duration excludes overnight/weekend gaps."""

    def test_overnight_gap_no_extra_bars(self):
        """Bars spanning two trading days should NOT include overnight hours."""
        # Day 1: 09:30–15:59 = 390 bars; Day 2: 09:30–15:59 = 390 bars
        df = _make_bars("2026-01-20 09:30", 780)  # 2 full trading days
        assert len(df) == 780

        # Verify gap: last bar of day 1 should be 15:59, first of day 2 = 09:30
        day1_last = df.index[389]
        day2_first = df.index[390]
        assert day1_last.hour == 15 and day1_last.minute == 59
        assert day2_first.hour == 9 and day2_first.minute == 30
        # Wall-clock gap is ~17.5 hours, but bar count is consecutive
        assert day2_first.date() > day1_last.date()

    def test_bars_held_for_multiday_trade(self):
        """A trade spanning 2 days should have bars_held = market bars only."""
        df = _make_bars("2026-01-20 09:30", 780)
        # Entry at bar 195 (noon day 1), exit at bar 585 (noon day 2)
        entry_idx = 195
        exit_idx = 585
        bars_held = exit_idx - entry_idx + 1
        # Should be 391 bars (market time), not 24*60 = 1440 (wall clock)
        assert bars_held == 391

        # In annualization, this equals 391/390 ≈ 1.003 trading days
        days = bars_held / _BARS_PER_DAY
        assert days == pytest.approx(1.003, abs=0.01)

    def test_weekend_gap_skipped(self):
        """Bars across a weekend should skip Saturday/Sunday entirely."""
        # Start Friday 09:30 → need enough bars to cross into Monday
        df = _make_bars("2026-01-23 09:30", 780)  # Fri + Mon
        fri_last = df.index[389]
        mon_first = df.index[390]
        assert fri_last.weekday() == 4  # Friday
        assert mon_first.weekday() == 0  # Monday
        # No Saturday/Sunday bars exist
        assert (mon_first - fri_last).days >= 2

    def test_ann_a_uses_bars_not_wall_clock(self):
        """Ann(A) uses bars_held/390 for duration, not wall-clock hours.

        Two identical trades: one with bars_held=390 (1 market day),
        another pretending bars_held=1440 (24 hours as minutes).
        They should give different Ann(A) because the formula uses bars.
        """
        r_market = _make_result(pnl_pct=1.0, bars_held=390)
        r_wall = _make_result(pnl_pct=1.0, bars_held=1440)

        stats_market = compute_ann_a([r_market, r_market])
        stats_wall = compute_ann_a([r_wall, r_wall])
        assert stats_market is not None and stats_wall is not None
        # Market-time version should show HIGHER annualized return
        # (same return in fewer trading days)
        assert stats_market["ann"] > stats_wall["ann"]


# ---------------------------------------------------------------------------
# Test: _filter_trading_hours
# ---------------------------------------------------------------------------


class TestFilterTradingHours:
    """Verify that the trading hours filter correctly removes out-of-hours bars."""

    def test_regular_hours_filter(self):
        """Only bars 09:30–15:59 should survive with market_close='16:00'."""
        # Create a 24-hour range including pre/post market
        idx = pd.date_range("2026-01-20 04:00", periods=960, freq="1min")
        df = pd.DataFrame(
            {"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
            index=idx,
        )
        filtered = _filter_trading_hours(df, "16:00")
        # 09:30 to 15:59 = 390 minutes
        assert len(filtered) == 390
        assert filtered.index[0].hour == 9 and filtered.index[0].minute == 30
        assert filtered.index[-1].hour == 15 and filtered.index[-1].minute == 59

    def test_extended_hours_filter(self):
        """market_close='20:00' should include bars up to 19:59."""
        idx = pd.date_range("2026-01-20 04:00", periods=960, freq="1min")
        df = pd.DataFrame(
            {"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
            index=idx,
        )
        filtered = _filter_trading_hours(df, "20:00")
        # 09:30 to 19:59 = 630 minutes
        assert len(filtered) == 630

    def test_none_close_returns_all(self):
        """market_close=None should return all bars (extended hours mode)."""
        idx = pd.date_range("2026-01-20 04:00", periods=100, freq="1min")
        df = pd.DataFrame(
            {"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
            index=idx,
        )
        filtered = _filter_trading_hours(df, None)
        assert len(filtered) == len(df)


# ---------------------------------------------------------------------------
# Test: _extract_periodic_closes
# ---------------------------------------------------------------------------


class TestExtractPeriodicCloses:
    """Verify periodic close extraction for equity curve construction."""

    def test_hourly_resolution(self):
        """60-min resolution should produce ~6 closes per trading day."""
        df = _make_bars("2026-01-20 09:30", 390)  # 1 full day
        pc = _extract_periodic_closes(df, entry_idx=0, bars_held=390, resolution_minutes=60)
        # 390 min / 60 = 6.5 → 6 or 7 periods depending on resampling alignment
        assert 5 <= len(pc) <= 8
        # Each entry is (iso_string, float)
        for ts, price in pc:
            assert isinstance(ts, str)
            assert isinstance(price, float)
            assert price > 0

    def test_every_bar_resolution(self):
        """resolution_minutes=1 should return every bar."""
        df = _make_bars("2026-01-20 09:30", 100)
        pc = _extract_periodic_closes(df, entry_idx=0, bars_held=100, resolution_minutes=1)
        assert len(pc) == 100

    def test_no_bars_returns_empty(self):
        """bars_held=0 should return empty list."""
        df = _make_bars("2026-01-20 09:30", 100)
        pc = _extract_periodic_closes(df, entry_idx=50, bars_held=0, resolution_minutes=60)
        assert pc == []

    def test_overnight_gap_no_phantom_periods(self):
        """Periodic closes across overnight gap should not create phantom bars.

        Note: pandas resample() aligns to clock-hour boundaries, so the
        09:30-09:59 bin gets a label of 09:00.  We allow that (bin label ≥ 09:00)
        but reject anything truly outside market hours (e.g. 03:00 or 17:00+).
        """
        df = _make_bars("2026-01-20 09:30", 780)  # 2 trading days
        pc = _extract_periodic_closes(df, entry_idx=0, bars_held=780, resolution_minutes=60)
        for ts_str, _ in pc:
            ts = pd.Timestamp(ts_str)
            t_min = ts.hour * 60 + ts.minute
            # Bin label can be 09:00 (for 09:30–09:59 bin), so allow ≥ 09:00
            assert t_min >= 9 * 60, f"Phantom bar at {ts_str} (before market open)"
            assert t_min < 16 * 60, f"Phantom bar at {ts_str} (after market close)"


# ---------------------------------------------------------------------------
# Test: Sharpe ratio properties
# ---------------------------------------------------------------------------


class TestSharpeProperties:
    """Verify Sharpe ratio behaves correctly under known conditions."""

    def test_identical_trades_sharpe_none(self):
        """Identical trades → zero std → Sharpe should be None."""
        results = [_make_result(pnl_pct=1.0, bars_held=390) for _ in range(5)]
        stats = compute_ann_a(results)
        assert stats is not None
        assert stats["sharpe"] is None  # zero variance

    def test_mixed_trades_positive_sharpe(self):
        """Mostly positive trades should give positive Sharpe."""
        results = [
            _make_result(pnl_pct=2.0, bars_held=390),
            _make_result(pnl_pct=1.5, bars_held=390),
            _make_result(pnl_pct=1.0, bars_held=390),
            _make_result(pnl_pct=0.5, bars_held=390),
            _make_result(pnl_pct=-0.5, bars_held=390),
        ]
        stats = compute_ann_a(results)
        assert stats is not None
        assert stats["sharpe"] > 0

    def test_all_losing_trades_negative_sharpe(self):
        """All losing trades should give negative Sharpe."""
        results = [
            _make_result(pnl_pct=-1.0, bars_held=390),
            _make_result(pnl_pct=-2.0, bars_held=390),
            _make_result(pnl_pct=-0.5, bars_held=390),
        ]
        stats = compute_ann_a(results)
        assert stats is not None
        assert stats["sharpe"] < 0


# ---------------------------------------------------------------------------
# Test: Method A vs B relationship
# ---------------------------------------------------------------------------


class TestMethodAvsB:
    """Verify expected relationships between the two annualization methods."""

    def test_single_non_overlapping_trade_similar(self):
        """With one trade and no overlap, A and B should be in the same ballpark."""
        pc = [
            ("2026-01-20T10:00:00", 100.5),
            ("2026-01-20T11:00:00", 101.0),
            ("2026-01-20T12:00:00", 101.5),
            ("2026-01-20T13:00:00", 102.0),
        ]
        # 2% gain over ~4 hours (240 bars)
        r = _make_result(pnl_pct=2.0, bars_held=240, entry_price=100.0, periodic_closes=pc)
        # Need 2 trades for Ann(A)
        r2 = _make_result(pnl_pct=2.0, bars_held=240, entry_price=100.0, periodic_closes=[
            ("2026-01-21T10:00:00", 100.5),
            ("2026-01-21T11:00:00", 101.0),
            ("2026-01-21T12:00:00", 101.5),
            ("2026-01-21T13:00:00", 102.0),
        ])
        stats_a = compute_ann_a([r, r2])
        stats_b = compute_ann_b([r, r2])
        assert stats_a is not None and stats_b is not None
        # Both should be positive and large (short-duration 2% trades)
        assert stats_a["ann"] > 0
        assert stats_b["ann"] > 0

    def test_breakeven_trade(self):
        """A 0% P&L trade should produce ~0% annualized return."""
        r = _make_result(pnl_pct=0.0, bars_held=390)
        stats = compute_ann_a([r, r])
        assert stats is not None
        assert abs(stats["ann"]) < 0.1  # effectively zero


# ---------------------------------------------------------------------------
# Test: edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases that could cause crashes or incorrect results."""

    def test_very_large_pnl(self):
        """Extremely large P&L should not cause overflow."""
        r = _make_result(pnl_pct=500.0, bars_held=390)  # 500% gain
        stats = compute_ann_a([r, r])
        assert stats is not None
        assert math.isfinite(stats["ann"])

    def test_near_total_loss(self):
        """-99% loss should not cause math domain errors."""
        r = _make_result(pnl_pct=-99.0, bars_held=390)
        stats = compute_ann_a([r, r])
        assert stats is not None
        assert stats["ann"] < 0
        assert math.isfinite(stats["ann"])

    def test_exactly_minus_100_pct(self):
        """-100% loss → ln(0) → should handle gracefully."""
        r = _make_result(pnl_pct=-100.0, bars_held=390)
        # log(1 + (-1.0)) = log(0) → -inf; function should handle
        try:
            stats = compute_ann_a([r, r])
            # If it returns, ann should be very negative or None
            if stats is not None:
                assert stats["ann"] < -99
        except (ValueError, OverflowError):
            pass  # Acceptable to raise on log(0)

    def test_one_bar_trade(self):
        """Trade lasting exactly 1 bar should work."""
        r = _make_result(pnl_pct=0.1, bars_held=1)
        stats = compute_ann_a([r, r])
        assert stats is not None
        # 0.1% in 1 bar (1/390 of a day) → very high annualized
        assert stats["ann"] > 1000
