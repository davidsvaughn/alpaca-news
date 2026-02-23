"""Grok (xAI) Responses API runner.

Uses the ``openai`` Python SDK pointed at ``https://api.x.ai/v1/`` with:
- Server-side ``x_search`` tool (biggest win — no inner API call overhead)
- Server-side ``web_search`` tool
- Custom function tools from TOOL_REGISTRY
- Tool-calling loop via ``previous_response_id``
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from openai import OpenAI

from trader.market.data_service import MarketDataService
from trader.online.agent_common import AgentRunResult, TradingSignal, build_trace_dict
from trader.online.tool_core import TOOL_BY_NAME, TOOL_REGISTRY, ToolDef

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")

_DEFAULT_MAX_TURNS = 15

# Per-call fee for server-side x_search and web_search on xAI
_XAI_PER_CALL_FEE = 0.005


def _build_grok_tools(
    excluded: frozenset[str],
) -> list[dict[str, Any]]:
    """Build xAI Responses API tool definitions.

    Includes server-side x_search and web_search (unless excluded),
    plus custom function tools from TOOL_REGISTRY.
    """
    tools: list[dict[str, Any]] = []

    # Server-side tools (handled by xAI, not by us)
    if "x_search" not in excluded:
        tools.append({"type": "x_search"})
    if "web_search" not in excluded:
        tools.append({"type": "web_search"})

    # Function tools
    for td in TOOL_REGISTRY:
        if td.name in excluded:
            continue
        tools.append({
            "type": "function",
            "name": td.name,
            "description": td.description,
            "parameters": td.parameters,
        })
    return tools


def _execute_function_call(
    name: str,
    arguments: str,
    market: MarketDataService,
    x_stream_service: Any,
) -> str:
    """Execute a function tool call and return the result string."""
    td = TOOL_BY_NAME.get(name)
    if td is None:
        return json.dumps({"error": f"Unknown tool: {name}"})

    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"Invalid JSON arguments: {arguments}"})

    try:
        if name == "url_fetch":
            return td.func(**args)
        elif name == "x_stream_cache":
            return td.func(x_stream_service, **args)
        else:
            return td.func(market, **args)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


async def run_grok(
    *,
    system_prompt: str,
    user_message: str,
    model: str = "grok-4-1-fast-reasoning",
    api_key: str | None = None,
    excluded_tools: frozenset[str] = frozenset(),
    market: MarketDataService,
    x_stream_service: Any = None,
    max_turns: int = _DEFAULT_MAX_TURNS,
    is_final: bool = False,
) -> AgentRunResult:
    """Run one agent turn via xAI Responses API (Grok).

    Server-side x_search and web_search are the key advantage: the model
    calls them directly with no round-trip overhead. Function tools for
    market data are handled client-side.
    """
    key = api_key or os.environ.get("XAI_API_KEY")
    client = OpenAI(api_key=key, base_url="https://api.x.ai/v1/")

    tools = _build_grok_tools(excluded_tools)
    tool_traces: list[dict[str, Any]] = []
    hop_index = 0

    input_messages: list[Any] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # store=True (default) so previous_response_id works for stateful
    # continuation — required for server-side tools (x_search, web_search)
    # to retain their results across turns in the tool-calling loop.
    response = client.responses.create(
        model=model,
        input=input_messages,
        tools=tools,
    )

    total_usage = {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "requests": 0, "tool_calls": 0, "reasoning_tokens": 0,
        "web_search_calls": 0, "x_search_calls": 0,
    }

    def _accumulate_usage(resp: Any) -> None:
        if hasattr(resp, "usage") and resp.usage:
            total_usage["input_tokens"] += getattr(resp.usage, "input_tokens", 0) or 0
            total_usage["output_tokens"] += getattr(resp.usage, "output_tokens", 0) or 0
            total_usage["total_tokens"] += getattr(resp.usage, "total_tokens", 0) or 0
            details = getattr(resp.usage, "output_tokens_details", None)
            if details:
                total_usage["reasoning_tokens"] += getattr(details, "reasoning_tokens", 0) or 0
        total_usage["requests"] += 1

    _accumulate_usage(response)

    # Names used by xAI for server-side x_search sub-tools
    _X_SEARCH_NAMES = {"x_keyword_search", "x_semantic_search"}

    # Extract server-side tool traces (x_search, web_search)
    def _extract_builtin_traces(resp: Any) -> None:
        nonlocal hop_index
        for item in resp.output:
            item_type = getattr(item, "type", None)
            item_name = getattr(item, "name", "") or ""

            # web_search: still reported as "web_search_call"
            # x_search: now reported as "custom_tool_call" with name
            #   "x_keyword_search" or "x_semantic_search"
            if item_type == "web_search_call":
                tool_name = "web_search"
            elif item_type == "custom_tool_call" and item_name in _X_SEARCH_NAMES:
                tool_name = "x_search"
            elif item_type == "x_search_call":
                # Legacy format (may still appear in older API versions)
                tool_name = "x_search"
            else:
                continue

            # Extract args based on action type or input field
            args: dict[str, Any] = {}
            action = getattr(item, "action", None)
            if action:
                action_type = getattr(action, "type", None)
                if action_type == "search":
                    args["query"] = getattr(action, "query", "") or ""
                elif action_type == "open_page":
                    args["url"] = getattr(action, "url", "") or ""
                elif action_type == "find_in_page":
                    args["pattern"] = getattr(action, "pattern", "") or ""
                    args["url"] = getattr(action, "url", "") or ""
            elif tool_name == "x_search":
                # custom_tool_call format: query is in "input" field (JSON string)
                raw_input = getattr(item, "input", "") or ""
                try:
                    parsed = json.loads(raw_input) if raw_input else {}
                    args["query"] = parsed.get("query", "")
                except (json.JSONDecodeError, TypeError):
                    args["query"] = raw_input

            item_data = item.model_dump() if hasattr(item, "model_dump") else None
            trace = build_trace_dict(
                tool_name=tool_name,
                args=args,
                result=json.dumps(item_data) if item_data else None,
                error=None,
                start=time.time(),
                end=time.time(),
                hop_index=hop_index,
                cost_usd=_XAI_PER_CALL_FEE,
                builtin=True,
            )
            tool_traces.append(trace)
            hop_index += 1
            total_usage["tool_calls"] += 1
            if tool_name == "web_search":
                total_usage["web_search_calls"] += 1
            elif tool_name == "x_search":
                total_usage["x_search_calls"] += 1

    _extract_builtin_traces(response)

    # Tool-calling loop (only for function tools — server-side tools are auto-resolved)
    turn = 0
    while turn < max_turns:
        function_calls = [
            item for item in response.output
            if getattr(item, "type", None) == "function_call"
        ]
        if not function_calls:
            break

        turn += 1
        tool_results: list[dict[str, Any]] = []

        for call in function_calls:
            call_name = call.name
            call_args_str = call.arguments
            call_id = call.call_id

            start = time.time()
            error = None
            try:
                result_str = _execute_function_call(
                    call_name, call_args_str, market, x_stream_service,
                )
            except Exception as e:
                result_str = json.dumps({"error": str(e)})
                error = str(e)
            end = time.time()

            try:
                parsed_args = json.loads(call_args_str) if call_args_str else {}
            except json.JSONDecodeError:
                parsed_args = {"_raw": call_args_str}

            trace = build_trace_dict(
                tool_name=call_name,
                args=parsed_args,
                result=result_str,
                error=error,
                start=start,
                end=end,
                hop_index=hop_index,
            )
            tool_traces.append(trace)
            hop_index += 1
            total_usage["tool_calls"] += 1

            tool_results.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": result_str,
            })

        if DEBUG:
            print(f"  [Grok] Turn {turn}: executed {len(function_calls)} function calls")

        # Use previous_response_id for stateful continuation —
        # this preserves server-side tool results (x_search, web_search)
        # across turns. Requires store=True (default) on initial request.
        response = client.responses.create(
            model=model,
            input=tool_results,
            previous_response_id=response.id,
            tools=tools,
        )
        _accumulate_usage(response)
        _extract_builtin_traces(response)

    # Extract final output
    output_text = getattr(response, "output_text", "") or ""

    # Extract reasoning from output items (xAI may include reasoning items)
    thinking_parts: list[str] = []
    for item in response.output:
        if getattr(item, "type", None) == "reasoning":
            for summary in getattr(item, "summary", []) or []:
                if hasattr(summary, "text") and summary.text:
                    thinking_parts.append(summary.text)
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
