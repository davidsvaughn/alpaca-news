from trader.snapshot_decision import (
    build_decision_metrics_payload,
    compute_avg_daily_volume_from_bars,
    decision_symbol_metrics,
    merge_decision_symbol_metrics,
    primary_symbol,
    snapshot_entry_time,
)
import pandas as pd


def test_snapshot_entry_time_prefers_decision_at() -> None:
    snap = {
        "created_at": "2026-03-12T18:30:00+00:00",
        "decision_at": "2026-03-12T18:32:15+00:00",
    }
    assert snapshot_entry_time(snap) == "2026-03-12T18:32:15+00:00"


def test_primary_symbol_reads_first_trigger_symbol() -> None:
    snap = {"trigger": {"symbols": ["hood", "spy"]}}
    assert primary_symbol(snap) == "HOOD"


def test_decision_symbol_metrics_prefers_stored_decision_metrics() -> None:
    snap = {
        "trigger": {"symbols": ["HOOD"]},
        "price_context": {
            "per_symbol": {
                "HOOD": {
                    "last_price": 11.0,
                    "avg_10d_volume": 1_000_000,
                }
            }
        },
        "decision_metrics": build_decision_metrics_payload(
            per_symbol={
                "HOOD": {"price": 12.5, "avg_vol": 2_000_000, "mkt_cap": 5_000_000_000, "pe": 14.0}
            },
            captured_at="2026-03-12T18:32:15+00:00",
            source="quote_fundamentals",
        ),
    }
    metrics = decision_symbol_metrics(snap, "HOOD")
    assert metrics["price"] == 12.5
    assert metrics["avg_vol"] == 2_000_000
    assert metrics["mkt_cap"] == 5_000_000_000
    assert metrics["pe"] == 14.0


def test_merge_decision_symbol_metrics_preserves_existing_fields() -> None:
    snap = {
        "decision_metrics": {
            "captured_at": "2026-03-12T18:32:15+00:00",
            "source": "quote_fundamentals",
            "per_symbol": {
                "HOOD": {
                    "price": 12.5,
                    "avg_vol": 2_000_000,
                    "mkt_cap": 5_000_000_000,
                    "pe": 14.0,
                    "source": "quote_fundamentals",
                }
            },
        }
    }
    updated = merge_decision_symbol_metrics(
        snap,
        symbol="HOOD",
        metrics={"price": 12.75, "source": "historical_1m"},
        captured_at="2026-03-12T18:33:00+00:00",
        source="historical_1m",
    )
    metrics = decision_symbol_metrics(updated, "HOOD")
    assert metrics["price"] == 12.75
    assert metrics["avg_vol"] == 2_000_000
    assert metrics["mkt_cap"] == 5_000_000_000
    assert metrics["pe"] == 14.0
    assert updated["decision_metrics"]["captured_at"] == "2026-03-12T18:33:00+00:00"


def test_compute_avg_daily_volume_from_bars_uses_prior_completed_days_only() -> None:
    idx = pd.to_datetime(
        [
            "2026-03-10 09:30:00",
            "2026-03-10 09:31:00",
            "2026-03-11 09:30:00",
            "2026-03-11 09:31:00",
            "2026-03-12 09:30:00",
        ]
    )
    df = pd.DataFrame({"Volume": [100, 300, 200, 400, 999]}, index=idx)
    avg = compute_avg_daily_volume_from_bars(
        df,
        decision_at="2026-03-12T10:00:00-05:00",
        lookback_days=10,
    )
    assert avg == 500.0


def test_compute_avg_daily_volume_from_bars_returns_none_without_completed_days() -> None:
    idx = pd.to_datetime(["2026-03-12 09:30:00", "2026-03-12 09:31:00"])
    df = pd.DataFrame({"Volume": [100, 300]}, index=idx)
    avg = compute_avg_daily_volume_from_bars(
        df,
        decision_at="2026-03-12T10:00:00-05:00",
        lookback_days=10,
    )
    assert avg is None


def test_decision_symbol_metrics_preserves_zero_avg_volume_from_price_context() -> None:
    snap = {
        "trigger": {"symbols": ["MMED"]},
        "price_context": {
            "per_symbol": {
                "MMED": {
                    "last_price": 15.98,
                    "avg_10d_volume": 0.0,
                    "avg_volume": 12345,
                }
            }
        },
    }
    metrics = decision_symbol_metrics(snap, "MMED")
    assert metrics["avg_vol"] == 0.0
