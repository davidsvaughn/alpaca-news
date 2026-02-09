"""Stage 1: news triage filter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from trader.knowledge.store import KnowledgeStore
from trader.llm.client import LLMClient


@dataclass(frozen=True)
class TriageDecision:
    action: str  # "investigate" | "skip"
    confidence: float
    reasoning: str
    symbols: list[str]
    skip_patterns_learned: list[str]


TRIAGE_PROMPT = """You are a financial news triage agent.

Evaluate whether this news item could signal imminent stock price movement (within minutes to hours).

REJECT if: retrospective/hypothetical articles, generic market commentary, old re-hashed news,
press releases with no clear price catalyst, clickbait, listicles.

Known skip patterns:
{skip_keywords}

Return STRICT JSON with keys:
  action: "investigate"|"skip"
  confidence: number between 0 and 1
  reasoning: short string
  symbols: array of tickers (strings)
  skip_patterns_learned: array of new skip keyword/phrases

News JSON:
{news_json}
"""


def run_triage(
    *,
    llm: LLMClient,
    provider: str,
    model: str,
    knowledge: KnowledgeStore,
    news: dict[str, Any],
) -> TriageDecision:
    skip = knowledge.load_skip_patterns()
    skip_keywords = skip.get("headline_keywords", [])

    prompt = TRIAGE_PROMPT.format(skip_keywords=json.dumps(skip_keywords), news_json=json.dumps(news))

    if provider == "openai":
        res = llm.query_openai(model=model, input_text=prompt, stage="triage", purpose="triage")
    elif provider == "grok":
        res = llm.query_grok(model=model, input_text=prompt, stage="triage", purpose="triage")
    elif provider == "gemini":
        res = llm.query_gemini(model=model, input_text=prompt, stage="triage", purpose="triage")
    else:
        raise ValueError(f"Unknown provider: {provider}")

    try:
        data = json.loads(res.text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Triage model did not return valid JSON. Text was: {res.text[:500]!r}") from e

    decision = TriageDecision(
        action=str(data.get("action")),
        confidence=float(data.get("confidence")),
        reasoning=str(data.get("reasoning")),
        symbols=[str(x) for x in (data.get("symbols") or [])],
        skip_patterns_learned=[str(x) for x in (data.get("skip_patterns_learned") or [])],
    )
    if decision.action not in ("investigate", "skip"):
        raise RuntimeError(f"Invalid triage action: {decision.action}")
    return decision
