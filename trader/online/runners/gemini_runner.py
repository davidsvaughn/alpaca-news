"""Gemini (Google) runner using the google-genai SDK.

Uses ``google.genai.Client.models.generate_content()`` with:
- Google Search grounding via ``types.Tool(google_search=types.GoogleSearch())``
- No function tools — Gemini is the synthesis agent; all market data and
  web research from prior agents is provided in the prompt.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from google import genai
from google.genai import types

from trader.online.agent_common import AgentRunResult, TradingSignal, build_trace_dict

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


async def run_gemini(
    *,
    system_prompt: str,
    user_message: str,
    model: str = "gemini-3-flash-preview",
    api_key: str | None = None,
    excluded_tools: frozenset[str] = frozenset(),
    market: Any = None,
    x_stream_service: Any = None,
    max_turns: int = 1,
    thinking_config: dict[str, Any] | None = None,
    is_final: bool = False,
) -> AgentRunResult:
    """Run one agent turn via Google genai SDK (Gemini).

    Uses native Google Search grounding only (no function tools).
    All market data and prior research is provided in the prompt.
    Single request — no tool-calling loop needed.
    """
    key = api_key or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=key)

    tools = [types.Tool(google_search=types.GoogleSearch())]

    config_kwargs: dict[str, Any] = {
        "tools": tools,
        "system_instruction": system_prompt,
    }
    if thinking_config:
        config_kwargs["thinking_config"] = types.ThinkingConfig(**thinking_config)

    config = types.GenerateContentConfig(**config_kwargs)

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)]),
    ]

    total_usage: dict[str, int] = {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "requests": 0, "tool_calls": 0, "reasoning_tokens": 0,
    }
    tool_traces: list[dict[str, Any]] = []
    hop_index = 0

    # Single request with Google Search grounding
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )

    # Accumulate usage
    um = getattr(response, "usage_metadata", None)
    if um:
        total_usage["input_tokens"] += getattr(um, "prompt_token_count", 0) or 0
        total_usage["output_tokens"] += getattr(um, "candidates_token_count", 0) or 0
        total_usage["total_tokens"] += getattr(um, "total_token_count", 0) or 0
        total_usage["reasoning_tokens"] += getattr(um, "thoughts_token_count", 0) or 0
    total_usage["requests"] += 1

    # Extract Google Search grounding traces
    for candidate in getattr(response, "candidates", []) or []:
        gm = getattr(candidate, "grounding_metadata", None)
        if gm:
            queries = getattr(gm, "web_search_queries", []) or []
            for q in queries:
                if not q:  # skip empty grounding queries
                    continue
                trace = build_trace_dict(
                    tool_name="web_search",
                    args={"query": q},
                    result=None,
                    error=None,
                    start=time.time(),
                    end=time.time(),
                    hop_index=hop_index,
                    builtin=True,
                )
                tool_traces.append(trace)
                hop_index += 1
                total_usage["tool_calls"] += 1

    # Extract final text
    output_text = getattr(response, "text", "") or ""

    # Extract thinking content
    thinking_parts: list[str] = []
    if response.candidates:
        for part in response.candidates[0].content.parts or []:
            if getattr(part, "thought", False) and part.text:
                thinking_parts.append(part.text)
    thinking_summary = "\n\n".join(thinking_parts) if thinking_parts else None

    # Parse TradingSignal if final agent
    output: Any = output_text
    if is_final and output_text:
        output = _parse_trading_signal(output_text)

    return AgentRunResult(
        output=output,
        tool_traces=tool_traces,
        usage=total_usage,
        thinking_summary=thinking_summary,
    )


def _parse_trading_signal(text: str) -> TradingSignal | str:
    """Try to extract a TradingSignal from JSON in the model's output text."""
    import re

    json_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for block in json_blocks:
        try:
            return TradingSignal.model_validate_json(block)
        except Exception:
            continue

    try:
        return TradingSignal.model_validate_json(text)
    except Exception:
        pass

    match = re.search(r"\{[^{}]*\"direction\"[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            return TradingSignal.model_validate_json(match.group())
        except Exception:
            pass

    return text
