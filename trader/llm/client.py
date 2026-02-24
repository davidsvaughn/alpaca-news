"""Unified LLM client.

Phase 1 supports minimal calls needed by triage/explorer.
Providers:
- OpenAI (responses API + web_search tool)
- Gemini (google-genai SDK + GoogleSearch tool)
- Grok (OpenAI-compatible responses API at x.ai; implemented via OpenAI client with base_url)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from openai import OpenAI

from trader.llm.cost_tracker import CostTracker


ProviderName = Literal["openai", "grok", "gemini"]


@dataclass(frozen=True)
class LLMResult:
    text: str
    usage: dict[str, Any]
    cost_usd: float
    raw: Any


class LLMClient:
    def __init__(self, *, cost_tracker: CostTracker):
        self.cost_tracker = cost_tracker

        # OpenAI
        self._openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # Grok (OpenAI-compatible)
        self._grok = OpenAI(
            api_key=os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY"),
            base_url=os.getenv("XAI_BASE_URL") or "https://api.x.ai/v1",
        )

        # Gemini is imported lazily because its import path is slightly different

    def query_openai(
        self,
        *,
        model: str,
        input_text: str,
        instructions: str | None = None,
        web_search: bool = False,
        stage: str,
        purpose: str,
    ) -> LLMResult:
        tools = [{"type": "web_search"}] if web_search else None
        resp = self._openai.responses.create(
            model=model,
            input=input_text,
            instructions=instructions,
            tools=tools,
        )
        usage = {
            "input_tokens": getattr(resp.usage, "input_tokens", None),
            "output_tokens": getattr(resp.usage, "output_tokens", None),
            "total_tokens": getattr(resp.usage, "total_tokens", None),
            "reasoning_tokens": getattr(resp.usage, "reasoning_tokens", None),
            "tool_calls": 0,
        }
        text = resp.output_text
        cost_usd = self.cost_tracker.log_llm_call(
            provider="openai",
            model=model,
            usage=usage,
            tools_used=["web_search"] if web_search else [],
            stage=stage,
            purpose=purpose,
        )
        return LLMResult(text=text, usage=usage, cost_usd=cost_usd, raw=resp)

    def query_grok(
        self,
        *,
        model: str,
        input_text: str,
        instructions: str | None = None,
        web_search: bool = False,
        x_search: bool = False,
        stage: str,
        purpose: str,
    ) -> LLMResult:
        tools: list[dict[str, Any]] = []
        if web_search:
            tools.append({"type": "web_search"})
        if x_search:
            tools.append({"type": "x_search"})
        resp = self._grok.responses.create(
            model=model,
            input=input_text,
            instructions=instructions,
            tools=tools or None,
        )
        usage = {
            "input_tokens": getattr(resp.usage, "input_tokens", None),
            "output_tokens": getattr(resp.usage, "output_tokens", None),
            "total_tokens": getattr(resp.usage, "total_tokens", None),
            "reasoning_tokens": getattr(resp.usage, "reasoning_tokens", None),
            "tool_calls": len(tools),
        }
        text = resp.output_text
        tool_names = [t["type"] for t in tools]
        cost_usd = self.cost_tracker.log_llm_call(
            provider="grok",
            model=model,
            usage=usage,
            tools_used=tool_names,
            stage=stage,
            purpose=purpose,
        )
        return LLMResult(text=text, usage=usage, cost_usd=cost_usd, raw=resp)

    def query_gemini(
        self,
        *,
        model: str,
        input_text: str,
        google_search: bool = False,
        stage: str,
        purpose: str,
    ) -> LLMResult:
        from google import genai
        from google.genai import types

        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY (or GOOGLE_API_KEY) for Gemini provider")

        client = genai.Client(api_key=api_key)
        tools = [types.Tool(google_search=types.GoogleSearch())] if google_search else None

        resp = client.models.generate_content(
            model=model,
            contents=input_text,
            config=types.GenerateContentConfig(tools=tools),
        )

        # google-genai doesn't always report tokens consistently across endpoints;
        # keep fields optional and cost is estimated.
        usage = {
            "input_tokens": getattr(getattr(resp, "usage_metadata", None), "prompt_token_count", None),
            "output_tokens": getattr(getattr(resp, "usage_metadata", None), "candidates_token_count", None),
            "total_tokens": getattr(getattr(resp, "usage_metadata", None), "total_token_count", None),
            "reasoning_tokens": getattr(getattr(resp, "usage_metadata", None), "thoughts_token_count", None),
            "tool_calls": 1 if google_search else 0,
        }
        text = getattr(resp, "text", None) or ""
        cost_usd = self.cost_tracker.log_llm_call(
            provider="gemini",
            model=model,
            usage=usage,
            tools_used=["web_search"] if google_search else [],
            stage=stage,
            purpose=purpose,
        )
        return LLMResult(text=text, usage=usage, cost_usd=cost_usd, raw=resp)
