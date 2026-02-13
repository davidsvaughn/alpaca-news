"""Tests for ActivityTracker."""

from __future__ import annotations

import threading

from trader.online.activity_tracker import Activity, ActivityTracker


def _make_activity(**kwargs) -> Activity:
    defaults = {
        "id": "test_1",
        "type": "exploration",
        "label": "NVDA — NVIDIA beats earnings",
        "symbols": ["NVDA"],
        "progress": "triage",
        "cost_usd": 0.0,
    }
    defaults.update(kwargs)
    return Activity(**defaults)


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


def test_start_and_get_all():
    tracker = ActivityTracker()
    a = _make_activity()
    tracker.start(a)
    activities = tracker.get_all()
    assert len(activities) == 1
    assert activities[0].id == "test_1"


def test_update_fields():
    tracker = ActivityTracker()
    tracker.start(_make_activity())
    tracker.update("test_1", progress="agent 1/3", cost_usd=0.05)
    activities = tracker.get_all()
    assert activities[0].progress == "agent 1/3"
    assert activities[0].cost_usd == 0.05


def test_update_unknown_id_is_noop():
    tracker = ActivityTracker()
    tracker.update("nonexistent", progress="foo")  # should not raise


def test_finish_removes_activity():
    tracker = ActivityTracker()
    tracker.start(_make_activity(id="a"))
    tracker.start(_make_activity(id="b"))
    assert len(tracker.get_all()) == 2
    tracker.finish("a")
    activities = tracker.get_all()
    assert len(activities) == 1
    assert activities[0].id == "b"


def test_finish_unknown_id_is_noop():
    tracker = ActivityTracker()
    tracker.finish("nonexistent")  # should not raise


def test_get_inflight_cost():
    tracker = ActivityTracker()
    tracker.start(_make_activity(id="a", cost_usd=0.10))
    tracker.start(_make_activity(id="b", cost_usd=0.25))
    assert abs(tracker.get_inflight_cost() - 0.35) < 1e-9


def test_inflight_cost_updates_on_update():
    tracker = ActivityTracker()
    tracker.start(_make_activity(id="a", cost_usd=0.0))
    assert tracker.get_inflight_cost() == 0.0
    tracker.update("a", cost_usd=0.12)
    assert abs(tracker.get_inflight_cost() - 0.12) < 1e-9


def test_inflight_cost_zero_after_finish():
    tracker = ActivityTracker()
    tracker.start(_make_activity(id="a", cost_usd=0.10))
    tracker.finish("a")
    assert tracker.get_inflight_cost() == 0.0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_start_finish():
    """Multiple threads starting and finishing activities concurrently."""
    tracker = ActivityTracker()
    errors: list[Exception] = []

    def worker(thread_id: int):
        try:
            for i in range(50):
                aid = f"t{thread_id}_{i}"
                tracker.start(_make_activity(id=aid, cost_usd=0.001))
                tracker.update(aid, progress=f"step_{i}")
                tracker.finish(aid)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(tracker.get_all()) == 0
    assert tracker.get_inflight_cost() == 0.0


# ---------------------------------------------------------------------------
# Multiple types
# ---------------------------------------------------------------------------


def test_mixed_activity_types():
    tracker = ActivityTracker()
    tracker.start(_make_activity(id="bf", type="backfill", progress="3/10"))
    tracker.start(_make_activity(id="ex", type="exploration", progress="triage"))
    tracker.start(_make_activity(id="fu", type="follow_up_collection", progress="+1h"))

    activities = tracker.get_all()
    types = {a.type for a in activities}
    assert types == {"backfill", "exploration", "follow_up_collection"}

    tracker.finish("bf")
    assert len(tracker.get_all()) == 2
