"""Validate that all model names configured in .env are real and reachable.

These are integration tests — they call real APIs to verify model existence.
They catch configuration errors (like 'gemini-3-flash' instead of
'gemini-3-flash-preview') before they hit production.

Run: uv run python -m pytest tests/test_config_models.py -v -s
"""

from __future__ import annotations

import os

import pytest

from trader.config import load_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_gemini_model(model_name: str) -> None:
    """Check that a Gemini model name is valid via lightweight metadata lookup."""
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        pytest.skip("No GOOGLE_API_KEY — cannot validate Gemini models")
    client = genai.Client(api_key=api_key)
    client.models.get(model=model_name)  # raises ClientError 404 if invalid


def _validate_openai_model(model_name: str) -> None:
    """Check that an OpenAI model name is valid via model retrieval."""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("No OPENAI_API_KEY — cannot validate OpenAI models")
    client = OpenAI(api_key=api_key)
    client.models.retrieve(model_name)  # raises NotFoundError if invalid


def _validate_grok_model(model_name: str) -> None:
    """Check that a Grok/xAI model name is valid via model retrieval."""
    from openai import OpenAI

    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        pytest.skip("No XAI_API_KEY — cannot validate Grok models")
    client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    client.models.retrieve(model_name)  # raises NotFoundError if invalid


# Map provider name → validator
_VALIDATORS = {
    "gemini": _validate_gemini_model,
    "openai": _validate_openai_model,
    "grok": _validate_grok_model,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConfiguredModelsExist:
    """Validate every model name in Settings is reachable via its provider API."""

    @pytest.fixture(scope="class")
    def settings(self):
        return load_settings()

    # --- Pipeline agent models ---

    def test_triage_model(self, settings):
        """Triage model (grok) must be reachable."""
        _VALIDATORS[settings.triage_provider](settings.triage_model)

    def test_research_model(self, settings):
        """Research model (openai) must be reachable."""
        _VALIDATORS[settings.research_provider](settings.research_model)

    def test_sentiment_model(self, settings):
        """Sentiment model (gemini) must be reachable."""
        _VALIDATORS[settings.sentiment_provider](settings.sentiment_model)

    # --- Auxiliary Gemini models ---

    def test_watch_checkin_model(self, settings):
        """Watch check-in model must be a valid Gemini model."""
        _validate_gemini_model(settings.watch_checkin_model)

    def test_follow_up_planner_model(self, settings):
        """Follow-up planner model must be a valid Gemini model."""
        _validate_gemini_model(settings.follow_up_planner_model)

    def test_x_stream_quality_check_model(self, settings):
        """X stream quality check model must be a valid Gemini model."""
        _validate_gemini_model(settings.x_stream_quality_check_model)

    def test_reflection_model(self, settings):
        """Reflection/evaluation model must be a valid Gemini model."""
        _validate_gemini_model(settings.reflection_model)
