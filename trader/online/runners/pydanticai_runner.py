"""PydanticAI fallback runner.

Used for testing with TestModel (no real API calls) and as a backward-
compatible runner for watcher check-ins.  All other pipeline agents use
native SDK runners (grok_runner, openai_runner, gemini_runner).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from trader.online.agent_common import AgentRunResult, TradingSignal

if TYPE_CHECKING:
    from trader.market.data_service import MarketDataService


async def run_pydanticai(
    *,
    spec: Any,          # AgentSpec — imported lazily to avoid circular deps
    system_prompt: str,
    user_message: str,
    market: "MarketDataService",
    x_stream_service: Any = None,
    config: Any,        # PipelineConfig
) -> AgentRunResult:
    """Run an agent using PydanticAI (for TestModel and backward compat)."""
    from pydantic_ai import Agent, UsageLimits
    from trader.online.explorer_agent import (
        ExplorerDeps,
        TracingToolset,
        market_toolset,
    )

    xai_key = os.getenv("XAI_API_KEY")

    deps = ExplorerDeps(
        market=market,
        news={},
        symbols=[],
        x_stream_service=x_stream_service,
        xai_api_key=xai_key,
        tool_calls_limit=config.tool_calls_limit,
        request_limit=config.request_limit,
        web_search_limit=spec.web_search_limit,
        x_search_limit=spec.x_search_limit,
    )

    if spec.function_tools:
        base_toolset = market_toolset
        if spec.excluded_tools:
            base_toolset = market_toolset.filtered(
                lambda _ctx, td, _excl=spec.excluded_tools: td.name not in _excl,
            )
        toolsets = [TracingToolset(base_toolset)]
    else:
        toolsets = []

    usage_limits = UsageLimits(
        request_limit=config.request_limit,
        tool_calls_limit=config.tool_calls_limit,
    )

    if spec.is_final:
        agent: Agent[ExplorerDeps, Any] = Agent(
            spec.model,
            deps_type=ExplorerDeps,
            output_type=TradingSignal,
            system_prompt=system_prompt,
            builtin_tools=spec.builtin_tools,
            toolsets=toolsets,
        )
    else:
        agent = Agent(
            spec.model,
            deps_type=ExplorerDeps,
            output_type=str,
            system_prompt=system_prompt,
            builtin_tools=spec.builtin_tools,
            toolsets=toolsets,
        )

    result = await agent.run(
        user_message,
        deps=deps,
        usage_limits=usage_limits,
        model_settings=spec.model_settings,
    )

    usage = result.usage()

    # Extract builtin tool traces
    builtin_traces = _extract_builtin_tool_traces(result.all_messages(), deps)
    deps.tool_traces.extend(builtin_traces)

    # Extract thinking content
    thinking_summary = _extract_thinking_content(result.all_messages())

    reasoning_tokens = (
        usage.details.get("reasoning_tokens", 0)
        or usage.details.get("thoughts_tokens", 0)
    )

    return AgentRunResult(
        output=result.output,
        tool_traces=deps.tool_traces,
        usage={
            "input_tokens": usage.input_tokens or 0,
            "output_tokens": usage.output_tokens or 0,
            "total_tokens": usage.total_tokens or 0,
            "requests": usage.requests or 0,
            "tool_calls": len(deps.tool_traces),
            "reasoning_tokens": reasoning_tokens,
        },
        thinking_summary=thinking_summary,
    )


def _extract_builtin_tool_traces(
    messages: list[Any],
    deps: Any,
) -> list[dict[str, Any]]:
    """Extract builtin tool calls (e.g. web_search) from PydanticAI message history."""
    from pydantic_ai.messages import ModelResponse

    traces: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        for call_part, return_part in msg.builtin_tool_calls:
            hop = deps.hop_index
            deps.hop_index += 1

            content = return_part.content
            if content is None:
                content = "[server-side grounding — results not exposed by provider]"
            elif hasattr(content, "model_dump"):
                content = content.model_dump()
            elif not isinstance(content, (str, dict, list)):
                content = str(content)

            trace: dict[str, Any] = {
                "trace_id": f"trace_{hop}",
                "hop_index": hop,
                "timestamp": (return_part.timestamp or datetime.now(tz=timezone.utc)).isoformat(),
                "modality": "web_research",
                "action": {
                    "tool": call_part.tool_name,
                    "args": call_part.args if isinstance(call_part.args, dict) else {"query": call_part.args},
                },
                "execution": {
                    "start_time": (msg.timestamp or datetime.now(tz=timezone.utc)).isoformat(),
                    "end_time": (return_part.timestamp or datetime.now(tz=timezone.utc)).isoformat(),
                    "duration_s": 0.0,
                    "cost_usd": 0.0,
                },
                "raw_tool_output": content,
                "error": None,
                "builtin": True,
            }
            traces.append(trace)

    return traces


def _extract_thinking_content(messages: list[Any]) -> str | None:
    """Extract reasoning/thinking summaries from PydanticAI model response messages."""
    from pydantic_ai.messages import ModelResponse, ThinkingPart

    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        for part in msg.parts:
            if isinstance(part, ThinkingPart) and part.content:
                parts.append(part.content)
    return "\n\n".join(parts) if parts else None
