"""Tests for FollowUp model, database helpers, and eval_record integration."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from trader.db.database import (
    count_active_follow_ups,
    get_active_follow_ups,
    get_all_follow_ups,
    get_follow_up,
    get_follow_ups_by_snapshot,
    insert_follow_up,
    open_sqlite,
    update_follow_up,
)
from trader.models.follow_up import (
    FollowUp,
    FollowUpBuilder,
    FollowUpCollection,
    parse_offset_to_minutes,
)
from trader.reflection.eval_record import build_eval_record


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as d:
        db = open_sqlite(str(Path(d) / "test.db"))
        yield db


def _make_builder(**kwargs) -> FollowUpBuilder:
    defaults = {
        "snapshot_id": "snap_001",
        "symbols": ["VALE"],
        "reason": "no_buy",
        "headline": "Vale reports Q3 earnings beat",
    }
    defaults.update(kwargs)
    return FollowUpBuilder(**defaults)


def _make_collection(offset_label: str = "+1h", cost: float = 0.01) -> FollowUpCollection:
    return FollowUpCollection(
        collected_at="2025-01-01T12:00:00+00:00",
        offset_label=offset_label,
        price={"VALE": {"last_price": 12.50}},
        news=[{"symbol": "VALE", "articles": []}],
        web_results=[{"query": "VALE earnings", "answer": "Revenue beat...", "citations": [], "quality": "good"}],
        x_results=[{"query": "$VALE sentiment", "answer": "Positive...", "citations": [], "quality": "good"}],
        query_plan={"web_queries": ["VALE earnings"], "x_queries": ["$VALE sentiment"], "reasoning": "test"},
        cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# parse_offset_to_minutes
# ---------------------------------------------------------------------------


def test_parse_offset_minutes():
    assert parse_offset_to_minutes("+30m") == 30.0


def test_parse_offset_hours():
    assert parse_offset_to_minutes("+4h") == 240.0


def test_parse_offset_days():
    assert parse_offset_to_minutes("+3d") == 4320.0


def test_parse_offset_no_plus():
    assert parse_offset_to_minutes("1h") == 60.0


def test_parse_offset_invalid():
    with pytest.raises(ValueError):
        parse_offset_to_minutes("+5w")


# ---------------------------------------------------------------------------
# FollowUpBuilder
# ---------------------------------------------------------------------------


def test_builder_defaults():
    b = _make_builder()
    assert b.snapshot_id == "snap_001"
    assert b.symbols == ["VALE"]
    assert b.reason == "no_buy"
    assert b.status == "active"
    assert b.schedule == ["+1h", "+4h", "+1d", "+3d", "+5d"]
    assert b.collections == []
    assert b.total_cost_usd == 0.0
    assert b.follow_up_id.startswith("fu_")


def test_builder_add_collection():
    b = _make_builder()
    c = _make_collection("+1h", 0.02)
    b.add_collection(c)
    assert len(b.collections) == 1
    assert b.total_cost_usd == 0.02


def test_builder_next_offset_label():
    b = _make_builder(schedule=["+1h", "+4h", "+1d"])
    assert b.next_offset_label() == "+1h"
    b.add_collection(_make_collection("+1h"))
    assert b.next_offset_label() == "+4h"
    b.add_collection(_make_collection("+4h"))
    assert b.next_offset_label() == "+1d"
    b.add_collection(_make_collection("+1d"))
    assert b.next_offset_label() is None


def test_builder_complete():
    b = _make_builder()
    b.complete()
    assert b.status == "complete"


def test_builder_to_follow_up():
    b = _make_builder()
    b.add_collection(_make_collection("+1h", 0.01))
    fu = b.to_follow_up()
    assert isinstance(fu, FollowUp)
    assert fu.snapshot_id == "snap_001"
    assert len(fu.collections) == 1
    assert fu.total_cost_usd == 0.01
    assert fu.status == "active"


def test_builder_from_dict_roundtrip():
    b = _make_builder(watch_id="w_123")
    b.add_collection(_make_collection("+1h", 0.015))
    fu = b.to_follow_up()
    d = fu.to_dict()

    b2 = FollowUpBuilder.from_dict(d)
    fu2 = b2.to_follow_up()
    assert fu2.follow_up_id == fu.follow_up_id
    assert fu2.snapshot_id == fu.snapshot_id
    assert fu2.symbols == fu.symbols
    assert fu2.reason == fu.reason
    assert fu2.watch_id == "w_123"
    assert len(fu2.collections) == 1
    assert fu2.total_cost_usd == fu.total_cost_usd


# ---------------------------------------------------------------------------
# Database CRUD
# ---------------------------------------------------------------------------


def test_insert_and_get_follow_up(tmp_db):
    b = _make_builder()
    fu_dict = b.to_follow_up().to_dict()
    assert insert_follow_up(tmp_db, follow_up=fu_dict) is True
    # Duplicate insert ignored
    assert insert_follow_up(tmp_db, follow_up=fu_dict) is False

    row = get_follow_up(tmp_db, b.follow_up_id)
    assert row is not None
    assert row["follow_up_id"] == b.follow_up_id
    assert row["snapshot_id"] == "snap_001"
    assert row["reason"] == "no_buy"
    assert row["status"] == "active"


def test_update_follow_up(tmp_db):
    b = _make_builder()
    fu_dict = b.to_follow_up().to_dict()
    insert_follow_up(tmp_db, follow_up=fu_dict)

    b.add_collection(_make_collection("+1h", 0.02))
    b.complete()
    updated = b.to_follow_up().to_dict()
    update_follow_up(tmp_db, b.follow_up_id, updated)

    row = get_follow_up(tmp_db, b.follow_up_id)
    assert row is not None
    assert row["status"] == "complete"
    assert len(row["collections"]) == 1


def test_get_active_follow_ups(tmp_db):
    b1 = _make_builder(snapshot_id="snap_a")
    b2 = _make_builder(snapshot_id="snap_b")
    b3 = _make_builder(snapshot_id="snap_c")
    b3.complete()

    insert_follow_up(tmp_db, follow_up=b1.to_follow_up().to_dict())
    insert_follow_up(tmp_db, follow_up=b2.to_follow_up().to_dict())
    insert_follow_up(tmp_db, follow_up=b3.to_follow_up().to_dict())

    active = get_active_follow_ups(tmp_db)
    assert len(active) == 2
    assert count_active_follow_ups(tmp_db) == 2


def test_get_follow_ups_by_snapshot(tmp_db):
    b1 = _make_builder(snapshot_id="snap_x")
    b2 = _make_builder(snapshot_id="snap_x", reason="post_exit", watch_id="w_1")
    b3 = _make_builder(snapshot_id="snap_y")

    insert_follow_up(tmp_db, follow_up=b1.to_follow_up().to_dict())
    insert_follow_up(tmp_db, follow_up=b2.to_follow_up().to_dict())
    insert_follow_up(tmp_db, follow_up=b3.to_follow_up().to_dict())

    results = get_follow_ups_by_snapshot(tmp_db, "snap_x")
    assert len(results) == 2


def test_get_all_follow_ups_with_filter(tmp_db):
    b1 = _make_builder(snapshot_id="snap_1")
    b2 = _make_builder(snapshot_id="snap_2")
    b2.complete()

    insert_follow_up(tmp_db, follow_up=b1.to_follow_up().to_dict())
    insert_follow_up(tmp_db, follow_up=b2.to_follow_up().to_dict())

    all_fus = get_all_follow_ups(tmp_db)
    assert len(all_fus) == 2

    active_only = get_all_follow_ups(tmp_db, status="active")
    assert len(active_only) == 1

    complete_only = get_all_follow_ups(tmp_db, status="complete")
    assert len(complete_only) == 1


# ---------------------------------------------------------------------------
# Eval record integration
# ---------------------------------------------------------------------------


def test_eval_record_with_follow_ups():
    snapshot = {
        "snapshot_id": "snap_001",
        "trigger": {"headline": "Vale Q3 beat", "symbols": ["VALE"]},
        "created_at": "2025-01-01T10:00:00Z",
        "triage": {"action": "research", "confidence": 0.8},
        "rounds": [],
        "prediction": {"direction": "bullish", "confidence": 0.75, "horizon": "1d"},
    }
    follow_ups = [
        {
            "follow_up_json": {
                "follow_up_id": "fu_abc123",
                "snapshot_id": "snap_001",
                "symbols": ["VALE"],
                "reason": "no_buy",
                "status": "active",
                "schedule": ["+1h", "+4h"],
                "started_at": "2025-01-01T10:05:00Z",
                "total_cost_usd": 0.02,
                "collections": [
                    {
                        "offset_label": "+1h",
                        "collected_at": "2025-01-01T11:05:00Z",
                        "price": {"VALE": {"last_price": 12.5}},
                        "news": [],
                        "web_results": [{"query": "VALE news", "answer": "...", "quality": "good"}],
                        "x_results": [],
                        "query_plan": {},
                        "cost_usd": 0.02,
                    }
                ],
            }
        }
    ]

    record = build_eval_record(snapshot, follow_ups=follow_ups)
    assert record["snapshot_id"] == "snap_001"

    # Should have: triage, prediction, follow_up
    types = [n["type"] for n in record["nodes"]]
    assert "triage" in types
    assert "prediction" in types
    assert "follow_up" in types

    # Follow-up node should have collection child
    fu_node = [n for n in record["nodes"] if n["type"] == "follow_up"][0]
    assert "no_buy" in fu_node["summary"]
    assert len(fu_node["children"]) == 1
    assert fu_node["children"][0]["type"] == "follow_up_collection"


def test_eval_record_without_follow_ups():
    """Existing behavior unchanged when no follow-ups passed."""
    snapshot = {
        "snapshot_id": "snap_002",
        "trigger": {"headline": "Test", "symbols": ["AAPL"]},
        "created_at": "2025-01-01T10:00:00Z",
        "triage": {},
        "rounds": [],
        "prediction": {},
    }
    record = build_eval_record(snapshot)
    types = [n["type"] for n in record["nodes"]]
    assert "follow_up" not in types
