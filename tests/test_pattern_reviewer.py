"""Tests for the periodic pattern reviewer.

Run: uv run python -m pytest tests/test_pattern_reviewer.py -v -s
"""

from __future__ import annotations

import json

import pytest

from trader.online.pattern_reviewer import (
    REVIEW_PROMPT,
    _categorize_headlines,
    _format_headlines,
)


# ---------------------------------------------------------------------------
# _categorize_headlines
# ---------------------------------------------------------------------------


class TestCategorizeHeadlines:
    """Verify headline categorization by triage outcome."""

    def _snap(self, headline: str, action: str, provider: str = "grok") -> dict:
        return {
            "trigger": {"headline": headline},
            "triage": {"action": action, "provider": provider},
        }

    def test_llm_skipped(self):
        snaps = [self._snap("Some fluff article", "skip", "grok")]
        skipped, investigated, prefilter = _categorize_headlines(snaps)
        assert skipped == ["Some fluff article"]
        assert investigated == []
        assert prefilter == []

    def test_llm_investigated(self):
        snaps = [self._snap("FDA approves new drug", "investigate", "openai")]
        skipped, investigated, prefilter = _categorize_headlines(snaps)
        assert skipped == []
        assert investigated == ["FDA approves new drug"]
        assert prefilter == []

    def test_prefilter_skipped(self):
        snaps = [self._snap("Weekly recap article", "skip", "pre-filter")]
        skipped, investigated, prefilter = _categorize_headlines(snaps)
        assert skipped == []
        assert investigated == []
        assert prefilter == ["Weekly recap article"]

    def test_mixed(self):
        snaps = [
            self._snap("Noise headline", "skip", "gemini"),
            self._snap("Real news", "investigate", "grok"),
            self._snap("Pattern matched", "skip", "pre-filter"),
            self._snap("More noise", "skip", "openai"),
        ]
        skipped, investigated, prefilter = _categorize_headlines(snaps)
        assert len(skipped) == 2
        assert len(investigated) == 1
        assert len(prefilter) == 1

    def test_empty_headline_skipped(self):
        snaps = [{"trigger": {"headline": ""}, "triage": {"action": "skip", "provider": "grok"}}]
        skipped, investigated, prefilter = _categorize_headlines(snaps)
        assert skipped == []

    def test_missing_triage(self):
        snaps = [{"trigger": {"headline": "Something"}, "triage": {}}]
        skipped, investigated, prefilter = _categorize_headlines(snaps)
        assert skipped == []
        assert investigated == []
        assert prefilter == []


# ---------------------------------------------------------------------------
# _format_headlines
# ---------------------------------------------------------------------------


class TestFormatHeadlines:
    def test_empty(self):
        assert _format_headlines([]) == "(none)"

    def test_simple(self):
        result = _format_headlines(["Headline A", "Headline B"])
        assert "- Headline A" in result
        assert "- Headline B" in result

    def test_truncation(self):
        headlines = [f"Headline {i}" for i in range(200)]
        result = _format_headlines(headlines, max_items=50)
        assert "... and 150 more" in result


# ---------------------------------------------------------------------------
# Prompt structure
# ---------------------------------------------------------------------------


class TestPromptStructure:
    """Verify the review prompt can be formatted without errors."""

    def test_prompt_formats_cleanly(self):
        prompt = REVIEW_PROMPT.format(
            skip_patterns=json.dumps(["pattern1", "pattern2"]),
            investigate_patterns=json.dumps(["earnings (beat|miss)"]),
            llm_skipped="- Some headline\n- Another headline",
            llm_investigated="- FDA approves drug",
            prefilter_skipped="(none)",
        )
        assert "pattern1" in prompt
        assert "earnings (beat|miss)" in prompt
        assert "Some headline" in prompt
        assert "FDA approves drug" in prompt

    def test_prompt_contains_key_instructions(self):
        """Verify the prompt includes critical safety instructions."""
        assert "Conservative" in REVIEW_PROMPT or "conservative" in REVIEW_PROMPT.lower()
        assert "NEVER" in REVIEW_PROMPT
        assert "earnings" in REVIEW_PROMPT.lower()
        assert "regex" in REVIEW_PROMPT.lower()


# ---------------------------------------------------------------------------
# Response parsing (integration with extract_json)
# ---------------------------------------------------------------------------


class TestResponseParsing:
    """Verify that expected LLM response formats parse correctly."""

    def test_valid_response(self):
        from trader.llm.extract import extract_json

        response = json.dumps({
            "patterns": [
                {"pattern": "weekly market recap", "reasoning": "repeated noise"},
                {"pattern": "stock of the day", "reasoning": "clickbait category"},
            ]
        })
        data = extract_json(response)
        assert len(data["patterns"]) == 2
        assert data["patterns"][0]["pattern"] == "weekly market recap"

    def test_empty_patterns(self):
        from trader.llm.extract import extract_json

        response = json.dumps({"patterns": []})
        data = extract_json(response)
        assert data["patterns"] == []

    def test_code_fenced_response(self):
        from trader.llm.extract import extract_json

        response = "Here are my suggestions:\n```json\n" + json.dumps({
            "patterns": [{"pattern": "test pattern", "reasoning": "noise"}]
        }) + "\n```"
        data = extract_json(response)
        assert len(data["patterns"]) == 1


# ---------------------------------------------------------------------------
# Quality gate integration
# ---------------------------------------------------------------------------


class TestQualityGateIntegration:
    """Verify that proposed patterns go through the existing quality gate."""

    def test_protected_keyword_rejected(self):
        from trader.knowledge.store import validate_skip_pattern

        # A pattern the reviewer LLM might propose that contains a protected keyword
        valid, reason = validate_skip_pattern("preliminary earnings report")
        assert not valid
        assert "protected keyword" in reason

    def test_good_pattern_accepted(self):
        from trader.knowledge.store import validate_skip_pattern

        valid, reason = validate_skip_pattern("weekly market recap")
        assert valid

    def test_too_short_rejected(self):
        from trader.knowledge.store import validate_skip_pattern

        valid, reason = validate_skip_pattern("news")
        assert not valid
        assert "short" in reason
