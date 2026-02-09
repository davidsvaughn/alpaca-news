"""Stage 2: exploration (Phase 1 minimal).

Implements a minimal multi-hop trace builder. For Phase 1:
- if triage says investigate, do up to 1 web search and optionally 1 x_search
- store evidence (top items) rather than raw web dumps
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from trader.llm.client import LLMClient
from trader.models.tool_trace import TraceExecution, new_tool_trace, utc_now_iso


@dataclass(frozen=True)
class ExploreResult:
    traces: list[dict[str, Any]]
    cost_usd: float


PHASE1_PROMPT = """You are a trading research assistant.

Goal: quickly gather *evidence* about this news item and summarize key takeaways.
Do not speculate. Prefer recency and direct sources.

Return STRICT JSON with keys:
  state_summary: short string
  evidence: array of up to 8 items. each item:
    - source_type: "web"|"x"|"other"
    - title: string
    - url: string (optional)
    - timestamp: string (optional)
    - snippet: string
  takeaways: array of short bullets

News JSON:
{news_json}
"""


def explore_phase1(
    *,
    llm: LLMClient,
    provider: str,
    model: str,
    news: dict[str, Any],
    use_web_search_tool: bool,
    use_x_search_tool: bool,
) -> ExploreResult:
    traces: list[dict[str, Any]] = []
    total_cost = 0.0

    prompt = PHASE1_PROMPT.format(news_json=json.dumps(news))
    trace_id = f"trace_{1}"
    start = utc_now_iso()

    if provider == "openai":
        res = llm.query_openai(
            model=model,
            input_text=prompt,
            web_search=use_web_search_tool,
            stage="explore",
            purpose="phase1",
        )
        tool_name = "web_search" if use_web_search_tool else "none"
    elif provider == "grok":
        res = llm.query_grok(
            model=model,
            input_text=prompt,
            web_search=use_web_search_tool,
            x_search=use_x_search_tool,
            stage="explore",
            purpose="phase1",
        )
        tool_name = "x_search" if use_x_search_tool else ("web_search" if use_web_search_tool else "none")
    elif provider == "gemini":
        res = llm.query_gemini(
            model=model,
            input_text=prompt,
            google_search=use_web_search_tool,
            stage="explore",
            purpose="phase1",
        )
        tool_name = "GoogleSearch" if use_web_search_tool else "none"
    else:
        raise ValueError(f"Unknown provider: {provider}")

    end = utc_now_iso()
    total_cost += res.cost_usd

    try:
        data = json.loads(res.text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Explore phase1 did not return valid JSON. Text was: {res.text[:500]!r}") from e

    results = data.get("evidence") or []

    traces.append(
        new_tool_trace(
            trace_id=trace_id,
            hop_index=1,
            parent_trace_id=None,
            decision_context={
                "state_summary": str(data.get("state_summary") or ""),
                "reason_for_action": "Phase1: broad evidence gathering",
            },
            action={
                "tool": tool_name,
                "provider": provider,
                "query_template": "phase1_evidence",
                "query": "(implicit by model)",
                "filters": {"max_items": 8},
            },
            execution=TraceExecution(model=model, start_time=start, end_time=end, cost_usd=res.cost_usd),
            results=[dict(r) for r in results][:8],
            extracted_signals={"takeaways": data.get("takeaways") or []},
            stop_signal={"should_stop": True, "reason": "Phase 1 complete"},
        )
    )

    return ExploreResult(traces=traces, cost_usd=total_cost)
