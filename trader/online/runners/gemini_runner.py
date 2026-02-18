"""Gemini (Google) runner using the google-genai SDK.

Uses ``google.genai.Client.models.generate_content()`` with:
- Google Search grounding via ``types.Tool(google_search=types.GoogleSearch())``
- Custom function tools from TOOL_REGISTRY (via FunctionDeclaration)
- Manual tool-calling loop for full tracing control
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from google import genai
from google.genai import types

from trader.market.data_service import MarketDataService
from trader.online.agent_common import AgentRunResult, TradingSignal, build_trace_dict
from trader.online.tool_core import TOOL_BY_NAME, TOOL_REGISTRY, ToolDef

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")

_DEFAULT_MAX_TURNS = 15


def _build_gemini_tools(
    excluded: frozenset[str],
    include_google_search: bool = True,
) -> list[types.Tool]:
    """Build Gemini tool definitions.

    Combines Google Search grounding with function declarations.
    """
    tools: list[types.Tool] = []

    if include_google_search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))

    # Function declarations
    func_decls = []
    for td in TOOL_REGISTRY:
        if td.name in excluded:
            continue
        func_decls.append(types.FunctionDeclaration(
            name=td.name,
            description=td.description,
            parameters_json_schema=td.parameters,
        ))

    if func_decls:
        tools.append(types.Tool(function_declarations=func_decls))

    return tools


def _execute_function_call(
    name: str,
    args: dict[str, Any],
    market: MarketDataService,
    x_stream_service: Any,
) -> str:
    """Execute a function tool call and return the result string."""
    td = TOOL_BY_NAME.get(name)
    if td is None:
        return json.dumps({"error": f"Unknown tool: {name}"})

    try:
        if name == "url_fetch":
            return td.func(**args)
        elif name == "x_stream_cache":
            return td.func(x_stream_service, **args)
        else:
            return td.func(market, **args)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


async def run_gemini(
    *,
    system_prompt: str,
    user_message: str,
    model: str = "gemini-3-flash-preview",
    api_key: str | None = None,
    excluded_tools: frozenset[str] = frozenset(),
    market: MarketDataService,
    x_stream_service: Any = None,
    max_turns: int = _DEFAULT_MAX_TURNS,
    thinking_config: dict[str, Any] | None = None,
    is_final: bool = False,
) -> AgentRunResult:
    """Run one agent turn via Google genai SDK (Gemini).

    Combines Google Search grounding with function tools — something
    PydanticAI couldn't do. Uses manual tool-calling loop for tracing.
    """
    key = api_key or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=key)

    tools = _build_gemini_tools(excluded_tools)
    tool_traces: list[dict[str, Any]] = []
    hop_index = 0

    # Build config
    config_kwargs: dict[str, Any] = {
        "tools": tools,
        "system_instruction": system_prompt,
        # Disable automatic function calling — we handle it manually for tracing
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    if thinking_config:
        config_kwargs["thinking_config"] = types.ThinkingConfig(**thinking_config)

    config = types.GenerateContentConfig(**config_kwargs)

    # Build initial contents
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)]),
    ]

    total_usage = {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "requests": 0, "tool_calls": 0, "reasoning_tokens": 0,
    }

    def _accumulate_usage(resp: Any) -> None:
        um = getattr(resp, "usage_metadata", None)
        if um:
            total_usage["input_tokens"] += getattr(um, "prompt_token_count", 0) or 0
            total_usage["output_tokens"] += getattr(um, "candidates_token_count", 0) or 0
            total_usage["total_tokens"] += getattr(um, "total_token_count", 0) or 0
            total_usage["reasoning_tokens"] += getattr(um, "thoughts_token_count", 0) or 0
        total_usage["requests"] += 1

    # Extract Google Search grounding traces
    def _extract_grounding_traces(resp: Any) -> None:
        nonlocal hop_index
        for candidate in getattr(resp, "candidates", []) or []:
            gm = getattr(candidate, "grounding_metadata", None)
            if gm:
                queries = getattr(gm, "web_search_queries", []) or []
                for q in queries:
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

    # First request
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    _accumulate_usage(response)
    _extract_grounding_traces(response)

    # Tool-calling loop
    turn = 0
    while turn < max_turns:
        # Check for function calls
        function_calls = getattr(response, "function_calls", None) or []
        if not function_calls:
            break

        turn += 1

        # Add model's response (with function calls) to contents
        if response.candidates and response.candidates[0].content:
            contents.append(response.candidates[0].content)

        # Execute each function call and build response parts
        function_response_parts: list[types.Part] = []

        for fc in function_calls:
            fc_name = fc.name
            fc_args = dict(fc.args) if fc.args else {}

            start = time.time()
            error = None
            try:
                result_str = _execute_function_call(
                    fc_name, fc_args, market, x_stream_service,
                )
            except Exception as e:
                result_str = json.dumps({"error": str(e)})
                error = str(e)
            end = time.time()

            trace = build_trace_dict(
                tool_name=fc_name,
                args=fc_args,
                result=result_str,
                error=error,
                start=start,
                end=end,
                hop_index=hop_index,
            )
            tool_traces.append(trace)
            hop_index += 1
            total_usage["tool_calls"] += 1

            # Parse result for Gemini's expected format
            try:
                result_dict = json.loads(result_str)
            except (json.JSONDecodeError, TypeError):
                result_dict = {"result": result_str}

            function_response_parts.append(
                types.Part.from_function_response(
                    name=fc_name,
                    response=result_dict,
                )
            )

        if DEBUG:
            print(f"  [Gemini] Turn {turn}: executed {len(function_calls)} function calls")

        # Add function responses to contents
        contents.append(types.Content(role="user", parts=function_response_parts))

        # Next request
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        _accumulate_usage(response)
        _extract_grounding_traces(response)

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
