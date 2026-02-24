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
        # Minimal heuristics:
        # - triage: expects {action, confidence, reasoning, symbols, skip_patterns_learned}
        # - phase1 actions: expects {state_summary, evidence, takeaways}
        # - hypotheses: expects {state_summary, freshness, hypotheses, evidence, takeaways}
        # - rank: expects {selected, dropped, orthogonality_check}
        # - phase2 deepening: expects {state_summary, hypothesis_update, evidence, extracted_signals, stop_signal, takeaways}

        if "triage" in purpose:
            payload = {
                "action": "investigate",
                "confidence": 0.5,
                "reasoning": "MOCK_LLM enabled; not performing real triage.",
                "symbols": [],
                "skip_patterns_learned": [],
            }

        elif "hypotheses" in purpose:
            payload = {
                "state_summary": "MOCK_LLM: generated hypotheses from empty evidence.",
                "freshness": "uncertain",
                "hypotheses": [
                    {
                        "hypothesis_id": "h1",
                        "label": "no_signal",
                        "description": "No external evidence available; this may be low-signal news.",
                        "confidence": 0.4,
                        "category": "recycled",
                        "suggested_action_ids": ["news_confirmation"],
                        "evidence_trace_ids": ["trace_1"],
                    },
                    {
                        "hypothesis_id": "h2",
                        "label": "possible_catalyst",
                        "description": "There could be an unseen catalyst; confirm with a follow-up.",
                        "confidence": 0.5,
                        "category": "fundamental",
                        "suggested_action_ids": ["news_confirmation", "analyst_reaction"],
                        "evidence_trace_ids": ["trace_1"],
                    },
                ],
                "evidence": [],
                "takeaways": ["MOCK_LLM: enable real keys for evidence"],
            }

        elif "rank" in purpose:
            payload = {
                "selected": [
                    {"hypothesis_id": "h2", "assigned_action_id": "news_confirmation", "reasoning": "MOCK"},
                    {"hypothesis_id": "h1", "assigned_action_id": "analyst_reaction", "reasoning": "MOCK"},
                ],
                "dropped": [],
                "orthogonality_check": "MOCK_LLM: selected two different actions",
            }

        elif "phase2:" in purpose:
            payload = {
                "state_summary": "MOCK_LLM: phase2 follow-up produced no evidence.",
                "hypothesis_update": {
                    "hypothesis_id": "h1",
                    "new_confidence": 0.4,
                    "verdict": "inconclusive",
                    "reasoning": "MOCK",
                },
                "evidence": [],
                "extracted_signals": {
                    "sentiment": "neutral",
                    "novelty": "low",
                    "confirmation_strength": "none",
                },
                "stop_signal": {"should_stop": True, "reason": "STOP_LOW_SIGNAL"},
                "takeaways": ["MOCK_LLM"],
            }

        else:
            payload = {
                "state_summary": "MOCK_LLM enabled; no external evidence gathered.",
                "evidence": [],
                "takeaways": ["MOCK_LLM: run with real keys to gather evidence"],
            }
        return MockLLMResult(
            text=json.dumps(payload),
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "tool_calls": 0,
            },
            cost_usd=0.0,
            raw=payload,
        )
