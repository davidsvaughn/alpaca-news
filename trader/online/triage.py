"""Stage 1: news triage filter.

Includes a cheap keyword pre-filter that skips obvious fluff *before* calling
the LLM, saving API cost on headlines like "if you had invested 5 years ago…".

Patterns in both skip_patterns.json and investigate_patterns.json are treated
as **regex** (case-insensitive).  Investigate patterns are checked first and
take priority over skip patterns.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from trader.knowledge.store import KnowledgeStore
from trader.llm.client import LLMClient
from trader.llm.extract import extract_json

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


@dataclass(frozen=True)
class TriageDecision:
    action: str  # "investigate" | "skip"
    confidence: float
    reasoning: str
    symbols: list[str]
    skip_patterns_learned: list[str]
    provider: str = "pre-filter"
    model: str = "keyword"
    usage: dict[str, Any] = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "tool_calls": 0,
    })
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    requests: int = 0


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


def _pre_filter(
    news: dict[str, Any],
    skip_keywords: list[str],
) -> TriageDecision | None:
    """Cheap local keyword check.  Returns a skip decision if matched, else None."""

    headline = str(news.get("headline") or "").lower()
    summary = str(news.get("summary") or "").lower()
    text = f"{headline} {summary}"

    # Check learned skip keywords
    for kw in skip_keywords:
        if kw.lower() in text:
            if DEBUG:
                print(f"PRE-FILTER skip: matched keyword {kw!r} in headline/summary")
            return TriageDecision(
                action="skip",
                confidence=0.95,
                reasoning=f"Pre-filter: matched skip keyword '{kw}'",
                symbols=[str(s) for s in (news.get("symbols") or [])],
                skip_patterns_learned=[],
            )

    # Hard-coded obvious fluff patterns (always active)
    _BUILTIN_SKIP = [
        "if you had invested",
        "years ago would be worth",
        "invested in this stock",
        "would be worth this much",
        "this much today",
        "top stocks to buy",
        "stocks to watch this week",
        "dividend aristocrat",
        "best stocks for",
        "stock picks for",
    ]
    for pattern in _BUILTIN_SKIP:
        if pattern in text:
            if DEBUG:
                print(f"PRE-FILTER skip: matched built-in pattern {pattern!r}")
            return TriageDecision(
                action="skip",
                confidence=0.99,
                reasoning=f"Pre-filter: matched built-in skip pattern '{pattern}'",
                symbols=[str(s) for s in (news.get("symbols") or [])],
                skip_patterns_learned=[],
            )

    # Check known skip sources
    source = str(news.get("source") or "").lower()
    author = str(news.get("author") or "").lower()
    _SKIP_AUTHORS = ["benzinga insights"]  # auto-generated retrospective pieces
    for a in _SKIP_AUTHORS:
        if a in author:
            if DEBUG:
                print(f"PRE-FILTER skip: matched skip author {a!r}")
            return TriageDecision(
                action="skip",
                confidence=0.95,
                reasoning=f"Pre-filter: matched skip author '{a}'",
                symbols=[str(s) for s in (news.get("symbols") or [])],
                skip_patterns_learned=[],
            )

    return None


def run_triage(
    *,
    llm: LLMClient,
    provider: str,
    model: str,
    knowledge: KnowledgeStore,
    news: dict[str, Any],
) -> TriageDecision:
    skip = knowledge.load_skip_patterns()
    skip_keywords: list[str] = skip.get("headline_keywords", [])

    # --- Cheap pre-filter (no LLM call) ---
    pre = _pre_filter(news, skip_keywords)
    if pre is not None:
        return pre

    # --- LLM triage ---
    prompt = TRIAGE_PROMPT.format(skip_keywords=json.dumps(skip_keywords), news_json=json.dumps(news))
    started = time.time()

    if provider == "openai":
        res = llm.query_openai(model=model, input_text=prompt, stage="triage", purpose="triage")
    elif provider == "grok":
        res = llm.query_grok(model=model, input_text=prompt, stage="triage", purpose="triage")
    elif provider == "gemini":
        res = llm.query_gemini(model=model, input_text=prompt, stage="triage", purpose="triage")
    else:
        raise ValueError(f"Unknown provider: {provider}")

    data = extract_json(res.text)

    decision = TriageDecision(
        action=str(data.get("action")),
        confidence=float(data.get("confidence", 0.0)),
        reasoning=str(data.get("reasoning", "")),
        symbols=[str(x) for x in (data.get("symbols") or [])],
        skip_patterns_learned=[str(x) for x in (data.get("skip_patterns_learned") or [])],
        provider=provider,
        model=model,
        usage={
            "input_tokens": int((res.usage or {}).get("input_tokens") or 0),
            "output_tokens": int((res.usage or {}).get("output_tokens") or 0),
            "total_tokens": int((res.usage or {}).get("total_tokens") or 0),
            "reasoning_tokens": int((res.usage or {}).get("reasoning_tokens") or 0),
            "tool_calls": int((res.usage or {}).get("tool_calls") or 0),
        },
        cost_usd=float(getattr(res, "cost_usd", 0.0) or 0.0),
        elapsed_s=round(time.time() - started, 3),
        requests=1,
    )
    if decision.action not in ("investigate", "skip"):
        raise RuntimeError(f"Invalid triage action: {decision.action}")
    return decision
