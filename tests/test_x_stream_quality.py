"""Tests for X stream quality gate: fail-closed, buffering, and noise rejection.

Uses real PARA noise data from a production stream to verify the quality gate
correctly identifies and rejects irrelevant tweets.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trader.online.event_bus import EventBus, PipelineEvent
from trader.online.x_stream_service import (
    QualityVerdict,
    XStreamGuards,
    XStreamService,
    build_rules_for_symbols,
)
from trader.xapi.rules import StreamRule


# ---------------------------------------------------------------------------
# Real PARA noise data (from 2026-02-13 stream)
# ---------------------------------------------------------------------------

PARA_NOISE_POSTS: list[dict[str, Any]] = [
    {
        "data": {
            "author_id": "1148931515293818880",
            "created_at": "2026-02-13T06:33:21.000Z",
            "id": "2022197385368416697",
            "lang": "en",
            "public_metrics": {"retweet_count": 0, "reply_count": 0, "like_count": 0},
            "text": "@alo93210 Aver \U0001f924\U0001f924",
        },
        "includes": {
            "users": [
                {"id": "1148931515293818880", "name": "poncho", "username": "poncho52981378"},
            ]
        },
        "matching_rules": [{"id": "2022098345804574720", "tag": "PARA"}],
    },
    {
        "data": {
            "author_id": "1832926413483036673",
            "created_at": "2026-02-13T06:33:26.000Z",
            "id": "2022197405429743705",
            "lang": "en",
            "public_metrics": {"retweet_count": 0, "reply_count": 0, "like_count": 0},
            "text": "@DerechaDiarioIS Trump lighting a torch in Jerusalem sounds poetic",
        },
        "includes": {
            "users": [
                {"id": "1832926413483036673", "name": "truth.phd", "username": "truthdotphd"},
            ]
        },
        "matching_rules": [{"id": "2022098345804574720", "tag": "PARA"}],
    },
    {
        "data": {
            "author_id": "704515682721996800",
            "created_at": "2026-02-13T06:33:42.000Z",
            "id": "2022197472517611652",
            "lang": "en",
            "public_metrics": {"retweet_count": 0, "reply_count": 0, "like_count": 0},
            "text": "@NinersCARP @MenngaRP @notluisdelpi Entonces Coudet.",
        },
        "includes": {
            "users": [
                {"id": "704515682721996800", "name": "Joven RP", "username": "muchachoSDR"},
            ]
        },
        "matching_rules": [{"id": "2022098345804574720", "tag": "PARA"}],
    },
    {
        "data": {
            "author_id": "1706728233322852352",
            "created_at": "2026-02-13T06:33:44.000Z",
            "id": "2022197479509569689",
            "lang": "en",
            "public_metrics": {"retweet_count": 0, "reply_count": 0, "like_count": 0},
            "text": "@insozayn NO WAYY UR SEEING HARRY AND BTS?????",
        },
        "includes": {
            "users": [
                {"id": "1706728233322852352", "name": "ally horan", "username": "allyspaperhouse"},
            ]
        },
        "matching_rules": [{"id": "2022098345804574720", "tag": "PARA"}],
    },
    {
        "data": {
            "author_id": "1920394512515387392",
            "created_at": "2026-02-13T06:33:47.000Z",
            "id": "2022197492222505320",
            "lang": "en",
            "public_metrics": {"retweet_count": 0, "reply_count": 0, "like_count": 0},
            "text": "Like o md para chat hot\U0001f525\U0001f608 https://t.co/TmbAHPsYJd",
        },
        "includes": {
            "users": [
                {"id": "1920394512515387392", "name": "spam_user", "username": "spam_user"},
            ]
        },
        "matching_rules": [{"id": "2022098345804574720", "tag": "PARA"}],
    },
]

# A legitimate financial post for comparison
RELEVANT_POST: dict[str, Any] = {
    "data": {
        "author_id": "999999",
        "created_at": "2026-02-13T15:00:00.000Z",
        "id": "111111",
        "lang": "en",
        "public_metrics": {"retweet_count": 5, "reply_count": 2, "like_count": 20},
        "text": "$PARA Paramount Global Q4 earnings beat estimates, stock up 3% premarket",
    },
    "includes": {
        "users": [
            {"id": "999999", "name": "Market Watch", "username": "MarketWatch"},
        ]
    },
    "matching_rules": [{"id": "123", "tag": "PARA"}],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path, bus: EventBus | None = None) -> XStreamService:
    """Create an XStreamService with mocked X API client."""
    bus = bus or EventBus()
    svc = XStreamService(
        bus=bus,
        data_dir=tmp_path,
        guards=XStreamGuards(
            max_posts_per_day=100,
            max_bursts_per_day=10,
            burst_ttl_minutes=1,
            usage_poll_interval_s=9999,
        ),
        enabled=True,
    )
    # Mock the X API client so we don't make real API calls
    svc.client = MagicMock()
    return svc


def _collect_events(bus: EventBus) -> list[PipelineEvent]:
    """Subscribe to all events and return them as a list."""
    events: list[PipelineEvent] = []
    bus.subscribe(lambda e: events.append(e))
    return events


# ---------------------------------------------------------------------------
# Tests: build_rules_for_symbols
# ---------------------------------------------------------------------------


class TestBuildRules:
    def test_short_ticker_uses_context_domain(self):
        """Short tickers (< 5 chars) should use context:166.* for stock domain."""
        rules = build_rules_for_symbols(symbols=["PARA"])
        assert len(rules) == 1
        assert "context:166.*" in rules[0].value
        assert "$PARA" in rules[0].value
        assert "lang:en" in rules[0].value

    def test_short_ticker_with_company_name(self):
        """When company name is provided, use it instead of context domain."""
        rules = build_rules_for_symbols(
            symbols=["PARA"],
            company_names={"PARA": "Paramount Global"},
        )
        assert len(rules) == 1
        assert '"Paramount Global"' in rules[0].value
        assert "$PARA" in rules[0].value
        # Should NOT use context when company name is available
        assert "context:166" not in rules[0].value

    def test_long_ticker_uses_bare_word(self):
        """Tickers >= 5 chars should include bare word match."""
        rules = build_rules_for_symbols(symbols=["GOOGL"])
        assert len(rules) == 1
        assert "GOOGL" in rules[0].value
        assert "$GOOGL" in rules[0].value
        # No context domain needed for long tickers
        assert "context:" not in rules[0].value

    def test_max_three_symbols(self):
        """Should cap at 3 rules."""
        rules = build_rules_for_symbols(symbols=["A", "B", "C", "D", "E"])
        assert len(rules) == 3

    def test_empty_symbols(self):
        rules = build_rules_for_symbols(symbols=[])
        assert rules == []


# ---------------------------------------------------------------------------
# Tests: Quality gate behavior
# ---------------------------------------------------------------------------


class TestQualityGate:
    """Test the quality gate in _run_burst via start_burst."""

    def test_noise_rejected_no_posts_published(self, tmp_path: Path):
        """When quality check says IRRELEVANT, no x_post events should be published."""
        bus = EventBus()
        events = _collect_events(bus)
        svc = _make_service(tmp_path, bus)

        # Mock stream_posts to yield our noise data
        with patch("trader.online.x_stream_service.stream_posts") as mock_stream, \
             patch("trader.online.x_stream_service.add_rules") as mock_add, \
             patch("trader.online.x_stream_service.delete_rules"), \
             patch("trader.online.x_stream_service.get_usage", return_value={}):

            mock_add.return_value = {"data": [{"id": "rule1"}]}
            mock_stream.return_value = iter(PARA_NOISE_POSTS)

            def quality_reject(posts, rules):
                return QualityVerdict(
                    relevant=False,
                    confidence=0.95,
                    reasoning="PARA matching Spanish word, not Paramount stock",
                    revised_rule_values=None,
                )

            svc.start_burst(
                rules=[StreamRule(value="$PARA -is:retweet lang:en", tag="PARA")],
                quality_check=quality_reject,
                quality_check_after=3,
                max_quality_retries=0,
            )

            # Wait for burst thread to finish
            svc._current_burst_thread.join(timeout=5)

        # NO x_post events should have been published (buffered and discarded)
        x_post_events = [e for e in events if e.type == "x_post"]
        assert len(x_post_events) == 0, f"Expected 0 x_post events, got {len(x_post_events)}"

        # Should have a quality verdict event showing IRRELEVANT
        quality_events = [e for e in events if e.type == "x_burst_quality"]
        assert len(quality_events) == 1
        assert quality_events[0].payload["relevant"] is False

        # Cache should be empty (noise was never cached)
        assert len(svc.get_recent_posts(key="PARA")) == 0
        assert len(svc.get_recent_posts(key="_all")) == 0

    def test_relevant_posts_flushed_after_quality_passes(self, tmp_path: Path):
        """When quality check says RELEVANT, buffered posts should be published."""
        bus = EventBus()
        events = _collect_events(bus)
        svc = _make_service(tmp_path, bus)

        relevant_posts = [RELEVANT_POST] * 5

        with patch("trader.online.x_stream_service.stream_posts") as mock_stream, \
             patch("trader.online.x_stream_service.add_rules") as mock_add, \
             patch("trader.online.x_stream_service.delete_rules"), \
             patch("trader.online.x_stream_service.get_usage", return_value={}):

            mock_add.return_value = {"data": [{"id": "rule1"}]}
            mock_stream.return_value = iter(relevant_posts)

            def quality_accept(posts, rules):
                return QualityVerdict(
                    relevant=True,
                    confidence=0.9,
                    reasoning="Tweets about Paramount stock earnings",
                )

            svc.start_burst(
                rules=[StreamRule(value="$PARA -is:retweet lang:en", tag="PARA")],
                quality_check=quality_accept,
                quality_check_after=3,
                max_quality_retries=0,
            )

            svc._current_burst_thread.join(timeout=5)

        # All 5 posts should be published (3 buffered + flushed, then 2 streamed directly)
        x_post_events = [e for e in events if e.type == "x_post"]
        assert len(x_post_events) == 5

        # Cache should have posts under "PARA" tag AND "_all"
        assert len(svc.get_recent_posts(key="PARA")) == 5
        assert len(svc.get_recent_posts(key="_all")) == 5

    def test_fail_closed_on_quality_check_error(self, tmp_path: Path):
        """If quality check throws an exception, burst should STOP (fail-closed)."""
        bus = EventBus()
        events = _collect_events(bus)
        svc = _make_service(tmp_path, bus)

        with patch("trader.online.x_stream_service.stream_posts") as mock_stream, \
             patch("trader.online.x_stream_service.add_rules") as mock_add, \
             patch("trader.online.x_stream_service.delete_rules"), \
             patch("trader.online.x_stream_service.get_usage", return_value={}):

            mock_add.return_value = {"data": [{"id": "rule1"}]}
            mock_stream.return_value = iter(PARA_NOISE_POSTS)

            def quality_crash(posts, rules):
                raise RuntimeError("Gemini API timeout")

            svc.start_burst(
                rules=[StreamRule(value="$PARA -is:retweet lang:en", tag="PARA")],
                quality_check=quality_crash,
                quality_check_after=3,
                max_quality_retries=0,
            )

            svc._current_burst_thread.join(timeout=5)

        # NO x_post events (fail-closed: error = stop, don't continue)
        x_post_events = [e for e in events if e.type == "x_post"]
        assert len(x_post_events) == 0, f"Expected 0 x_post events, got {len(x_post_events)}"

        # Should have a quality error event
        error_events = [e for e in events if e.type == "x_burst_quality_error"]
        assert len(error_events) == 1
        assert "Gemini API timeout" in error_events[0].payload["error"]

    def test_no_quality_check_publishes_immediately(self, tmp_path: Path):
        """Without a quality check, posts are published immediately."""
        bus = EventBus()
        events = _collect_events(bus)
        svc = _make_service(tmp_path, bus)

        with patch("trader.online.x_stream_service.stream_posts") as mock_stream, \
             patch("trader.online.x_stream_service.add_rules") as mock_add, \
             patch("trader.online.x_stream_service.delete_rules"), \
             patch("trader.online.x_stream_service.get_usage", return_value={}):

            mock_add.return_value = {"data": [{"id": "rule1"}]}
            mock_stream.return_value = iter(PARA_NOISE_POSTS[:2])

            svc.start_burst(
                rules=[StreamRule(value="$PARA -is:retweet lang:en", tag="PARA")],
                quality_check=None,  # no quality check
            )

            svc._current_burst_thread.join(timeout=5)

        # All posts published (no gating)
        x_post_events = [e for e in events if e.type == "x_post"]
        assert len(x_post_events) == 2

    def test_retry_with_revised_rules(self, tmp_path: Path):
        """Quality check suggests revised rules, burst retries with new rules."""
        bus = EventBus()
        events = _collect_events(bus)
        svc = _make_service(tmp_path, bus)

        attempt_count = 0

        with patch("trader.online.x_stream_service.stream_posts") as mock_stream, \
             patch("trader.online.x_stream_service.add_rules") as mock_add, \
             patch("trader.online.x_stream_service.delete_rules"), \
             patch("trader.online.x_stream_service.get_usage", return_value={}):

            mock_add.return_value = {"data": [{"id": "rule1"}]}

            # First attempt yields noise, second attempt yields relevant posts
            def stream_side_effect(**kwargs):
                nonlocal attempt_count
                attempt_count += 1
                if attempt_count == 1:
                    return iter(PARA_NOISE_POSTS)
                else:
                    return iter([RELEVANT_POST] * 4)

            mock_stream.side_effect = stream_side_effect

            call_count = 0

            def quality_cb(posts, rules):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return QualityVerdict(
                        relevant=False,
                        confidence=0.9,
                        reasoning="Noise — not about Paramount",
                        revised_rule_values=['$PARA OR "Paramount Global" -is:retweet lang:en'],
                    )
                else:
                    return QualityVerdict(
                        relevant=True,
                        confidence=0.85,
                        reasoning="Now seeing Paramount stock tweets",
                    )

            svc.start_burst(
                rules=[StreamRule(value="$PARA -is:retweet lang:en", tag="PARA")],
                quality_check=quality_cb,
                quality_check_after=3,
                max_quality_retries=2,
            )

            svc._current_burst_thread.join(timeout=5)

        # Should have 2 quality events (one reject, one accept)
        quality_events = [e for e in events if e.type == "x_burst_quality"]
        assert len(quality_events) == 2
        assert quality_events[0].payload["relevant"] is False
        assert quality_events[1].payload["relevant"] is True

        # Only the second attempt's posts should be published (4 posts)
        x_post_events = [e for e in events if e.type == "x_post"]
        assert len(x_post_events) == 4

    def test_jsonl_only_written_for_quality_posts(self, tmp_path: Path):
        """JSONL log should only contain posts that passed quality."""
        bus = EventBus()
        svc = _make_service(tmp_path, bus)

        with patch("trader.online.x_stream_service.stream_posts") as mock_stream, \
             patch("trader.online.x_stream_service.add_rules") as mock_add, \
             patch("trader.online.x_stream_service.delete_rules"), \
             patch("trader.online.x_stream_service.get_usage", return_value={}):

            mock_add.return_value = {"data": [{"id": "rule1"}]}
            mock_stream.return_value = iter(PARA_NOISE_POSTS)

            def quality_reject(posts, rules):
                return QualityVerdict(
                    relevant=False,
                    confidence=0.95,
                    reasoning="Noise",
                )

            svc.start_burst(
                rules=[StreamRule(value="$PARA -is:retweet lang:en", tag="PARA")],
                quality_check=quality_reject,
                quality_check_after=3,
                max_quality_retries=0,
            )

            svc._current_burst_thread.join(timeout=5)

        # Check that no JSONL was written (noise posts aren't persisted)
        log_dir = tmp_path / "x" / "stream"
        jsonl_files = list(log_dir.glob("*.jsonl")) if log_dir.exists() else []
        if jsonl_files:
            lines = jsonl_files[0].read_text().strip().split("\n")
            assert len(lines) == 0 or all(l.strip() == "" for l in lines), \
                f"Expected no JSONL entries for rejected noise, got {len(lines)}"
        # If no file exists at all, that's also correct


# ---------------------------------------------------------------------------
# Integration tests: Real LLM quality check with PARA noise
# ---------------------------------------------------------------------------

import os

from dotenv import load_dotenv

load_dotenv()


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not set")
def test_llm_rejects_para_noise():
    """REAL LLM CALL: Gemini should identify PARA noise tweets as IRRELEVANT.

    This test feeds the actual PARA noise data through the real quality check
    to verify the LLM correctly catches the noise.
    """
    from trader.llm.client import LLMClient
    from trader.llm.cost_tracker import CostTracker
    from trader.online.stream_quality import check_stream_quality

    cost_tracker = CostTracker(max_daily_cost=1.0, max_cost_per_item=0.10)
    llm = LLMClient(cost_tracker=cost_tracker)

    verdict = check_stream_quality(
        posts=PARA_NOISE_POSTS,
        headline="Paramount Global Reports Q4 Earnings Beat",
        symbols=["PARA"],
        summary="Paramount Global stock rises 3% after beating revenue estimates",
        current_rules=[StreamRule(value="$PARA -is:retweet lang:en", tag="PARA")],
        llm=llm,
        model="gemini-2.0-flash",
    )

    print(f"\n[LLM Quality] relevant={verdict.relevant}, confidence={verdict.confidence}")
    print(f"[LLM Quality] reasoning: {verdict.reasoning}")
    print(f"[LLM Quality] revised_rules: {verdict.revised_rule_values}")

    assert verdict.relevant is False, (
        f"LLM should have rejected PARA noise, but said relevant=True: {verdict.reasoning}"
    )
    assert verdict.confidence >= 0.7, (
        f"Expected high confidence rejection, got {verdict.confidence}"
    )
    # Should suggest better rules (including company name)
    if verdict.revised_rule_values:
        rules_text = " ".join(verdict.revised_rule_values).lower()
        assert "paramount" in rules_text, (
            f"Revised rules should include 'Paramount': {verdict.revised_rule_values}"
        )


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not set")
def test_llm_accepts_relevant_stock_tweets():
    """REAL LLM CALL: Gemini should identify legitimate stock tweets as RELEVANT."""
    from trader.llm.client import LLMClient
    from trader.llm.cost_tracker import CostTracker
    from trader.online.stream_quality import check_stream_quality

    cost_tracker = CostTracker(max_daily_cost=1.0, max_cost_per_item=0.10)
    llm = LLMClient(cost_tracker=cost_tracker)

    relevant_posts = [
        {
            "data": {
                "author_id": "111",
                "id": "1",
                "lang": "en",
                "text": "$PARA Paramount Global Q4 earnings beat expectations, revenue up 12% YoY. Stock up 3% premarket.",
            },
            "includes": {"users": [{"id": "111", "name": "StockWatch", "username": "StockWatch"}]},
            "matching_rules": [{"id": "1", "tag": "PARA"}],
        },
        {
            "data": {
                "author_id": "222",
                "id": "2",
                "lang": "en",
                "text": "Paramount Global $PARA breaking out above resistance. Strong volume. Earnings catalyst driving momentum.",
            },
            "includes": {"users": [{"id": "222", "name": "TraderJoe", "username": "TraderJoe"}]},
            "matching_rules": [{"id": "1", "tag": "PARA"}],
        },
        {
            "data": {
                "author_id": "333",
                "id": "3",
                "lang": "en",
                "text": "Analysts raising $PARA price targets after solid Q4. Paramount streaming subscriber growth impressive.",
            },
            "includes": {"users": [{"id": "333", "name": "MarketPulse", "username": "MarketPulse"}]},
            "matching_rules": [{"id": "1", "tag": "PARA"}],
        },
    ]

    verdict = check_stream_quality(
        posts=relevant_posts,
        headline="Paramount Global Reports Q4 Earnings Beat",
        symbols=["PARA"],
        summary="Paramount Global stock rises 3% after beating revenue estimates",
        current_rules=[StreamRule(value="$PARA -is:retweet lang:en", tag="PARA")],
        llm=llm,
        model="gemini-2.0-flash",
    )

    print(f"\n[LLM Quality] relevant={verdict.relevant}, confidence={verdict.confidence}")
    print(f"[LLM Quality] reasoning: {verdict.reasoning}")

    assert verdict.relevant is True, (
        f"LLM should have accepted relevant stock tweets, but said relevant=False: {verdict.reasoning}"
    )
