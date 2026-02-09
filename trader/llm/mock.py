"""Mock LLM client.

This exists so you can run the pipeline end-to-end without API keys.

Enable with:
  MOCK_LLM=true
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MockLLMResult:
    text: str
    usage: dict[str, Any]
    cost_usd: float
    raw: Any


class MockLLMClient:
    """Drop-in replacement for :class:`trader.llm.client.LLMClient`.

    It returns deterministic JSON that matches the expectations of triage/explorer.
    """

    def __init__(self) -> None:
        self.total_cost = 0.0

    def query_openai(self, *, model: str, input_text: str, instructions: str | None = None, web_search: bool = False, stage: str, purpose: str):  # type: ignore[override]
        return self._respond(stage=stage, purpose=purpose, input_text=input_text)

    def query_grok(self, *, model: str, input_text: str, instructions: str | None = None, web_search: bool = False, x_search: bool = False, stage: str, purpose: str):  # type: ignore[override]
        return self._respond(stage=stage, purpose=purpose, input_text=input_text)

    def query_gemini(self, *, model: str, input_text: str, google_search: bool = False, stage: str, purpose: str):  # type: ignore[override]
        return self._respond(stage=stage, purpose=purpose, input_text=input_text)

    def _respond(self, *, stage: str, purpose: str, input_text: str) -> MockLLMResult:
        # Minimal heuristics: if the prompt contains "Return STRICT JSON" and "action" then it's triage.
        if "action" in input_text and "triage" in purpose:
            payload = {
                "action": "investigate",
                "confidence": 0.5,
                "reasoning": "MOCK_LLM enabled; not performing real triage.",
                "symbols": [],
                "skip_patterns_learned": [],
            }
        else:
            payload = {
                "state_summary": "MOCK_LLM enabled; no external evidence gathered.",
                "evidence": [],
                "takeaways": ["MOCK_LLM: run with real keys to gather evidence"],
            }
        return MockLLMResult(text=json.dumps(payload), usage={"input_tokens": 0, "output_tokens": 0}, cost_usd=0.0, raw=payload)
