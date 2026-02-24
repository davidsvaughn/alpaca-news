"""Tests for skip pattern quality gate and regex pre-filter.

Run: uv run python -m pytest tests/test_skip_pattern_gate.py -v -s
"""

from __future__ import annotations

import pytest

from trader.knowledge.store import validate_skip_pattern, PROTECTED_KEYWORDS


# ---------------------------------------------------------------------------
# validate_skip_pattern
# ---------------------------------------------------------------------------


class TestValidateSkipPattern:
    """Quality gate for candidate skip patterns."""

    def test_good_patterns_pass(self):
        good = [
            "weekly market recap",
            "thought experiment",
            "crypto fear greed index",
            "prediction market odds",
            "13F Filing",
            "Equities Trading UP",
            "maintains (price target|outperform)",
            "Q\\d+ Preview",
        ]
        for p in good:
            valid, reason = validate_skip_pattern(p)
            assert valid, f"{p!r} rejected: {reason}"

    def test_empty_rejected(self):
        assert not validate_skip_pattern("")[0]
        assert not validate_skip_pattern("   ")[0]

    def test_too_short_rejected(self):
        valid, reason = validate_skip_pattern("hi")
        assert not valid
        assert "short" in reason

    def test_too_long_rejected(self):
        valid, reason = validate_skip_pattern("a" * 81)
        assert not valid
        assert "long" in reason

    def test_invalid_regex_rejected(self):
        valid, reason = validate_skip_pattern("(unclosed group")
        assert not valid
        assert "invalid regex" in reason

    def test_protected_keywords_rejected(self):
        dangerous = [
            "preliminary takeover talks",
            "CEO shake-up at company",
            "FDA approval for new drug",
            "shares reported earnings beat",
            "MHRA approves treatment",
            "company announces buyback",
            "activist investor pushes for",
            "files for bankruptcy protection",
            "trading halt announced",
            "guidance raise announced",
        ]
        for p in dangerous:
            valid, reason = validate_skip_pattern(p)
            assert not valid, f"{p!r} should be rejected (protected keyword)"
            assert "protected keyword" in reason


# ---------------------------------------------------------------------------
# Regex pre-filter
# ---------------------------------------------------------------------------


class TestPreFilterRegex:
    """Verify regex matching in the triage pre-filter."""

    @pytest.fixture()
    def _make_news(self):
        def factory(headline: str, summary: str = "") -> dict:
            return {"headline": headline, "summary": summary, "symbols": ["TEST"]}
        return factory

    def test_regex_skip_pattern_matches(self, _make_news):
        from trader.online.triage import _pre_filter

        news = _make_news("Analyst maintains $250 price target on AAPL")
        result = _pre_filter(news, ["maintains .* price target"])
        assert result is not None
        assert result.action == "skip"

    def test_regex_investigate_overrides_skip(self, _make_news):
        from trader.online.triage import _pre_filter

        # This headline matches both a skip AND an investigate pattern
        news = _make_news("Company XYZ earnings beat, analyst maintains outperform")
        result = _pre_filter(
            news,
            skip_keywords=["maintains outperform"],
            investigate_keywords=["earnings (beat|miss)"],
        )
        assert result is not None
        assert result.action == "investigate"

    def test_investigate_pattern_forces_investigate(self, _make_news):
        from trader.online.triage import _pre_filter

        news = _make_news("FDA approves new cancer drug for BioGen")
        result = _pre_filter(
            news,
            skip_keywords=[],
            investigate_keywords=["(fda|ema|mhra) (approv|reject|clear)"],
        )
        assert result is not None
        assert result.action == "investigate"

    def test_no_match_returns_none(self, _make_news):
        from trader.online.triage import _pre_filter

        news = _make_news("Company announces new product launch")
        result = _pre_filter(news, ["13F Filing", "thought experiment"])
        assert result is None

    def test_builtin_skip_still_works(self, _make_news):
        from trader.online.triage import _pre_filter

        news = _make_news("If you had invested $1000 in AAPL 5 years ago")
        result = _pre_filter(news, [])
        assert result is not None
        assert result.action == "skip"

    def test_invalid_regex_skipped_gracefully(self, _make_news):
        from trader.online.triage import _pre_filter

        # Bad regex should not crash, just be ignored
        news = _make_news("Some normal headline")
        result = _pre_filter(news, ["(unclosed group", "valid pattern"])
        assert result is None  # Neither matched
