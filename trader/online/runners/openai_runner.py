"""OpenAI Responses API runner.

Uses the ``openai`` Python SDK's ``client.responses.create()`` with:
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

# Maximum tool-calling loop iterations (safety net)
_DEFAULT_MAX_TURNS = 15

# Per-call fee for server-side web_search on OpenAI ($10/1k = $0.01 each)
_OPENAI_WEB_SEARCH_FEE = 0.01


def _build_openai_tools(
    excluded: frozenset[str],
    include_web_search: bool = True,
) -> list[dict[str, Any]]:
    """Build OpenAI Responses API tool definitions."""
    tools: list[dict[str, Any]] = []
    if include_web_search:
        tools.append({"type": "web_search"})
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
        # Most tools take (market, **kwargs); special cases handled here
        if name == "url_fetch":
            return td.func(**args)
        elif name == "x_stream_cache":
            return td.func(x_stream_service, **args)
        elif name == "check_market_context":
            return td.func(market, **args)
        else:
            return td.func(market, **args)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


async def run_openai(
    *,
    system_prompt: str,
    user_message: str,
    model: str = "gpt-5.1",
    api_key: str | None = None,
    excluded_tools: frozenset[str] = frozenset(),
    market: MarketDataService,
    x_stream_service: Any = None,
    max_turns: int = _DEFAULT_MAX_TURNS,
    reasoning_effort: str | None = None,
    reasoning_summary: str | None = None,
    is_final: bool = False,
) -> AgentRunResult:
    """Run one agent turn via OpenAI Responses API.

    Returns AgentRunResult with output text (or TradingSignal if is_final),
    tool traces, and usage stats.
    """
    client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    tools = _build_openai_tools(excluded_tools)
    tool_traces: list[dict[str, Any]] = []
    hop_index = 0

    # Build conversation (accumulated across turns)
    conversation: list[Any] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # Optional reasoning config
    kwargs: dict[str, Any] = {}
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}
        if reasoning_summary:
            kwargs["reasoning"]["summary"] = reasoning_summary

    # First request
    response = client.responses.create(
        model=model,
        input=conversation,
        tools=tools,
        **kwargs,
    )

    total_usage = {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "requests": 0, "tool_calls": 0, "reasoning_tokens": 0,
        "web_search_calls": 0,
    }

    def _accumulate_usage(resp: Any) -> None:
        if hasattr(resp, "usage") and resp.usage:
            total_usage["input_tokens"] += getattr(resp.usage, "input_tokens", 0) or 0
            total_usage["output_tokens"] += getattr(resp.usage, "output_tokens", 0) or 0
            total_usage["total_tokens"] += getattr(resp.usage, "total_tokens", 0) or 0
            # Reasoning tokens are in output_tokens_details
            details = getattr(resp.usage, "output_tokens_details", None)
            if details:
                total_usage["reasoning_tokens"] += getattr(details, "reasoning_tokens", 0) or 0
        total_usage["requests"] += 1

    _accumulate_usage(response)

    # Extract web_search traces from server-side calls
    def _extract_builtin_traces(resp: Any) -> None:
        nonlocal hop_index
        for item in resp.output:
            if getattr(item, "type", None) == "web_search_call":
                # Extract args based on action type:
                #   search → query, open_page → url, find_in_page → pattern+url
                action = getattr(item, "action", None)
                action_type = getattr(action, "type", None) if action else None
                args: dict[str, Any] = {}
                if action_type == "search":
                    args["query"] = getattr(action, "query", "") or ""
                elif action_type == "open_page":
                    args["url"] = getattr(action, "url", "") or ""
                elif action_type == "find_in_page":
                    args["pattern"] = getattr(action, "pattern", "") or ""
                    args["url"] = getattr(action, "url", "") or ""
                # Serialize full item for diagnostics
                item_data = item.model_dump() if hasattr(item, "model_dump") else None
                trace = build_trace_dict(
                    tool_name="web_search",
                    args=args,
                    result=json.dumps(item_data) if item_data else None,
                    error=None,
                    start=time.time(),
                    end=time.time(),
                    hop_index=hop_index,
                    cost_usd=_OPENAI_WEB_SEARCH_FEE,
                    builtin=True,
                )
                tool_traces.append(trace)
                hop_index += 1
                total_usage["tool_calls"] += 1
                total_usage["web_search_calls"] += 1

    _extract_builtin_traces(response)

    # Tool-calling loop
    turn = 0
    while turn < max_turns:
        # Collect function calls from output
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
            print(f"  [OpenAI] Turn {turn}: executed {len(function_calls)} tool calls")

        # Build full conversation for next turn
        conversation.extend(response.output)
        conversation.extend(tool_results)

        response = client.responses.create(
            model=model,
            input=conversation,
            tools=tools,
            **kwargs,
        )
        _accumulate_usage(response)
        _extract_builtin_traces(response)

    # Extract final output text
    output_text = getattr(response, "output_text", "") or ""

    # Extract reasoning summary from output items
    thinking_parts: list[str] = []
    for item in response.output:
        if getattr(item, "type", None) == "reasoning":
            for summary in getattr(item, "summary", []) or []:
                if hasattr(summary, "text") and summary.text:
                    thinking_parts.append(summary.text)
    thinking_summary = "\n\n".join(thinking_parts) if thinking_parts else None

    # Parse TradingSignal if this is the final agent
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

    # Look for ```json ... ``` blocks
    json_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for block in json_blocks:
        try:
            return TradingSignal.model_validate_json(block)
        except Exception:
            continue

    # Try the whole text as JSON
    try:
        return TradingSignal.model_validate_json(text)
    except Exception:
        pass

    # Try to find a JSON object anywhere in the text
    match = re.search(r"\{[^{}]*\"direction\"[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            return TradingSignal.model_validate_json(match.group())
        except Exception:
            pass

    # Return raw text — pipeline will handle signal=None
    return text
