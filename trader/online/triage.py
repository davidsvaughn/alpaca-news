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
press releases with no clear price catalyst, clickbait, listicles, recap/roundup articles,
prediction market speculation, portfolio recommendation lists, pure technical chart analysis,
celebrity/pundit opinion pieces about crypto, analyst rating reiteration with no new info.

ALWAYS INVESTIGATE (never reject):
{investigate_keywords}

Known skip patterns (headlines matching these are auto-skipped before the LLM is called):
{skip_keywords}

TICKER VALIDATION: The `symbols` array comes from a third-party feed and may be wrong.
Before returning, verify each ticker is actually a company central to this news story.
Remove any ticker that is only tangentially related — e.g. an investor, partner, or sector
proxy for a privately-held company named in the headline. If no relevant public tickers
remain after pruning, set action to "skip".

Return STRICT JSON with keys:
  action: "investigate"|"skip"
  confidence: number between 0 and 1
  reasoning: short string
  symbols: array of tickers (strings)

News JSON:
{news_json}
"""


def _compile_patterns(raw: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Compile a list of regex pattern strings, skipping invalid ones."""
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for p in raw:
        try:
            compiled.append((p, re.compile(p, re.IGNORECASE)))
        except re.error:
            if DEBUG:
                print(f"PRE-FILTER: invalid regex, skipping: {p!r}")
    return compiled


# Hard-coded obvious fluff patterns (always active)
_BUILTIN_SKIP = _compile_patterns([
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
])

_SKIP_AUTHORS = ["benzinga insights"]  # auto-generated retrospective pieces


def _pre_filter(
    news: dict[str, Any],
    skip_keywords: list[str],
    investigate_keywords: list[str] | None = None,
) -> TriageDecision | None:
    """Cheap local regex check.  Returns a decision if matched, else None.

    Investigate patterns are checked FIRST and take priority — if a headline
    matches an investigate pattern it will never be skipped by a skip pattern.
    """

    headline = str(news.get("headline") or "")
    summary = str(news.get("summary") or "")
    text = f"{headline} {summary}"
    symbols = [str(s) for s in (news.get("symbols") or [])]

    # ── 1. Investigate patterns (force investigate, highest priority) ──
    if investigate_keywords:
        inv_compiled = _compile_patterns(investigate_keywords)
        for raw, rxp in inv_compiled:
            if rxp.search(text):
                if DEBUG:
                    print(f"PRE-FILTER investigate: matched pattern {raw!r}")
                return TriageDecision(
                    action="investigate",
                    confidence=0.99,
                    reasoning=f"Pre-filter: matched investigate pattern '{raw}'",
                    symbols=symbols,
                    skip_patterns_learned=[],
                )

    # ── 2. Learned skip patterns (regex) ──
    skip_compiled = _compile_patterns(skip_keywords)
    for raw, rxp in skip_compiled:
        if rxp.search(text):
            if DEBUG:
                print(f"PRE-FILTER skip: matched pattern {raw!r} in headline/summary")
            return TriageDecision(
                action="skip",
                confidence=0.95,
                reasoning=f"Pre-filter: matched skip pattern '{raw}'",
                symbols=symbols,
                skip_patterns_learned=[],
            )

    # ── 3. Built-in skip patterns ──
    for raw, rxp in _BUILTIN_SKIP:
        if rxp.search(text):
            if DEBUG:
                print(f"PRE-FILTER skip: matched built-in pattern {raw!r}")
            return TriageDecision(
                action="skip",
                confidence=0.99,
                reasoning=f"Pre-filter: matched built-in skip pattern '{raw}'",
                symbols=symbols,
                skip_patterns_learned=[],
            )

    # ── 4. Skip authors ──
    author = str(news.get("author") or "").lower()
    for a in _SKIP_AUTHORS:
        if a in author:
            if DEBUG:
                print(f"PRE-FILTER skip: matched skip author {a!r}")
            return TriageDecision(
                action="skip",
                confidence=0.95,
                reasoning=f"Pre-filter: matched skip author '{a}'",
                symbols=symbols,
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

    inv = knowledge.load_investigate_patterns()
    investigate_keywords: list[str] = inv.get("headline_keywords", [])

    # --- Cheap pre-filter (no LLM call) ---
    # Note: investigate_keywords are intentionally NOT passed here — investigate
    # pattern hits must go through the LLM so ticker symbols can be validated.
    pre = _pre_filter(news, skip_keywords)
    if pre is not None:
        return pre

    # --- LLM triage ---
    prompt = TRIAGE_PROMPT.format(
        investigate_keywords=json.dumps(investigate_keywords),
        skip_keywords=json.dumps(skip_keywords),
        news_json=json.dumps(news),
    )
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
