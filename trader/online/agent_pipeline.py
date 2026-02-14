"""Multi-agent sequential pipeline for news investigation.

Runs multiple PydanticAI agents (each backed by a different LLM provider)
sequentially. Basic market data is pre-fetched once and included in the
prompt. Each agent's investigative tool results are passed downstream
via a tool call ledger.

Architecture:
    Agent 1 (Grok)   → web_search + x_search + all function tools
    Agent 2 (OpenAI) → web_search + all function tools
    Agent 3 (Gemini) → Google grounding web search (no function tools) → TradingSignal

Graceful degradation: if an agent fails (e.g. token limit exceeded),
partial traces are preserved and the pipeline continues to the next agent.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic_ai import Agent, UsageLimits, WebSearchTool
from pydantic_ai.exceptions import AgentRunError, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ThinkingPart
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from trader.market.data_service import MarketDataService
from trader.online.explorer_agent import (
    ExplorerDeps,
    ExploreResult,
    TracingToolset,
    TradingSignal,
    market_toolset,
    _build_user_message,
)

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Builtin tool extraction
# ---------------------------------------------------------------------------


def _extract_builtin_tool_traces(
    messages: list[Any],
    deps: ExplorerDeps,
) -> list[dict[str, Any]]:
    """Extract builtin tool calls (e.g. web_search) from message history.

    PydanticAI's WebSearchTool is a server-side builtin tool that doesn't
    go through TracingToolset. We recover those calls from the message
    history as BuiltinToolCallPart / BuiltinToolReturnPart pairs.
    """
    from pydantic_ai.messages import BuiltinToolCallPart, BuiltinToolReturnPart

    traces: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        # Use the convenience property if available
        for call_part, return_part in msg.builtin_tool_calls:
            hop = deps.hop_index
            deps.hop_index += 1

            # Serialize the return content
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
                    "duration_s": 0.0,  # server-side, no client timing available
                    "cost_usd": 0.0,    # baked into provider's token cost
                },
                "raw_tool_output": content,
                "error": None,
                "builtin": True,
            }
            traces.append(trace)

    return traces


def _extract_thinking_content(messages: list[Any]) -> str | None:
    """Extract reasoning/thinking summaries from model response messages.

    PydanticAI represents reasoning summaries as ThinkingPart objects in
    ModelResponse.parts. These come from OpenAI's reasoning_summary or
    Gemini's include_thoughts settings.
    """
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        for part in msg.parts:
            if isinstance(part, ThinkingPart) and part.content:
                parts.append(part.content)
    return "\n\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


@dataclass
class AgentSpec:
    """Specification for one agent in the pipeline."""

    name: str                       # e.g. "grok", "openai", "claude"
    model: Any                      # PydanticAI model string or Model instance
    builtin_tools: list[Any]        # e.g. [WebSearchTool()]
    role_description: str = ""      # injected into the agent's system prompt
    is_final: bool = False          # only the final agent produces TradingSignal
    function_tools: bool = True     # False = skip TracingToolset (e.g. Gemini with grounding)
    excluded_tools: frozenset[str] = frozenset()  # tool names to exclude from this agent
    web_search_limit: int = 0       # 0 = unlimited; >0 = prompt-enforced cap
    model_settings: dict[str, Any] | None = None  # provider-specific settings (reasoning effort, etc.)


@dataclass
class PipelineConfig:
    """Configuration for the full pipeline."""

    agents: list[AgentSpec]
    max_rounds: int = 2
    confidence_threshold: float = 0.7
    # Per-agent safety nets (prevent runaway loops, not budget control)
    request_limit: int = 15
    tool_calls_limit: int = 25
    # Cost-based budget: skip remaining intermediate agents when cumulative
    # pipeline cost exceeds this threshold. Final agent always runs.
    max_cost_usd: float = 0.50


# ---------------------------------------------------------------------------
# Agent factories
# ---------------------------------------------------------------------------


def build_grok_model(
    model_name: str | None = None,
    api_key: str | None = None,
) -> OpenAIResponsesModel:
    """Create an OpenAIResponsesModel pointed at xAI's Responses API."""
    key = api_key or os.environ["XAI_API_KEY"]
    name = model_name or os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning")
    provider = OpenAIProvider(api_key=key, base_url="https://api.x.ai/v1/")
    return OpenAIResponsesModel(name, provider=provider)


def build_default_pipeline() -> PipelineConfig:
    """Build the default 3-agent pipeline from environment variables.

    Falls back gracefully: if a provider's API key is missing, that agent
    is skipped. At minimum one agent must be available.
    """
    agents: list[AgentSpec] = []

    # Agent 1: Grok (web_search + x_search via function tool)
    xai_key = os.getenv("XAI_API_KEY")
    if xai_key:
        grok_model_name = os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning")
        agents.append(AgentSpec(
            name="grok",
            model=build_grok_model(model_name=grok_model_name, api_key=xai_key),
            builtin_tools=[WebSearchTool(search_context_size=None)],
            role_description=(
                "You are the FIRST investigator. Your strength is web and social "
                "media research. Use web_search to find news context, and x_search "
                "to check X/Twitter sentiment and chatter. Also gather key market "
                "data. Focus on breadth — cast a wide net."
            ),
        ))

    # Agent 2: OpenAI (web_search + function tools, no x_search)
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        openai_model = os.getenv("RESEARCH_MODEL", "gpt-5.1")
        agents.append(AgentSpec(
            name="openai",
            model=f"openai-responses:{openai_model}",
            builtin_tools=[WebSearchTool()],
            role_description=(
                "You are the SECOND investigator. Review what the previous agent "
                "found, then dig deeper. Use web_search to verify claims, find "
                "contradicting evidence, or explore angles the first agent missed. "
                "Check market data that hasn't been checked yet. Focus on depth "
                "and verification."
            ),
            excluded_tools=frozenset({"x_search", "x_stream_cache"}),
        ))

    # Agent 3: Gemini (Google grounding web search, no function tools)
    # Gemini cannot mix Google grounding with function tools. With pre-fetched
    # market data and tool ledgers from prior agents, Gemini gets all data in
    # the prompt. Web search is for finding NEW angles, not repeating prior work.
    google_key = os.getenv("GOOGLE_API_KEY")
    if google_key:
        gemini_model = os.getenv("SYNTHESIS_MODEL", "gemini-3-flash-preview")
        agents.append(AgentSpec(
            name="gemini",
            model=f"google-gla:{gemini_model}",
            builtin_tools=[WebSearchTool()],
            role_description=(
                "You are the FINAL analyst. All prior agents' evidence — market "
                "data, web research, X/Twitter sentiment, and full tool results "
                "— is provided in the prompt. Do NOT repeat searches or lookups "
                "that prior agents already performed. Instead: (1) search for NEW "
                "angles, patterns, or evidence that prior agents missed, "
                "(2) deeply synthesize ALL accumulated evidence, and (3) think "
                "through the implications for short-term stock price movement. "
                "Produce a clear, actionable trading signal."
            ),
            is_final=True,
            function_tools=False,
        ))

    if not agents:
        raise RuntimeError(
            "No LLM API keys configured. Set at least one of: "
            "XAI_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY"
        )

    # Mark the last agent as final (in case some were skipped)
    agents[-1].is_final = True

    from trader.config import load_settings
    settings = load_settings()

    # Apply web_search_limit to gpt-5-mini agents only
    ws_limit = settings.openai_web_search_limit
    if ws_limit > 0:
        for a in agents:
            model_str = a.model if isinstance(a.model, str) else getattr(a.model, "model_name", "")
            if "gpt-5-mini" in model_str:
                a.web_search_limit = ws_limit

    # Apply reasoning / thinking settings per provider
    for a in agents:
        if a.name == "openai":
            a.model_settings = OpenAIResponsesModelSettings(
                openai_reasoning_effort=settings.openai_reasoning_effort,  # type: ignore[arg-type]
                openai_reasoning_summary="detailed",
            )
        elif a.name == "gemini":
            thinking_level = settings.gemini_thinking_level
            if thinking_level == "off":
                thinking_config: dict[str, Any] = {"thinking_budget": 0}
            elif thinking_level == "dynamic":
                thinking_config = {"include_thoughts": True}
            else:
                # low, medium, high → pass as thinking_level
                thinking_config = {"thinking_level": thinking_level, "include_thoughts": True}
            a.model_settings = GoogleModelSettings(google_thinking_config=thinking_config)

    return PipelineConfig(
        agents=agents,
        request_limit=settings.pipeline_request_limit,
        tool_calls_limit=settings.pipeline_tool_calls_limit,
        max_cost_usd=settings.pipeline_max_cost_usd,
    )


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_system_prompt(
    spec: AgentSpec,
    agent_index: int,
    total_agents: int,
) -> str:
    """Build a role-aware system prompt for an agent."""
    parts = [
        f"You are a financial research analyst investigating a breaking news event.",
        f"You are Agent {agent_index + 1} of {total_agents} in a sequential research pipeline.",
        "",
        "## Your role",
        spec.role_description,
        "",
        "## Your tools",
    ]

    if spec.function_tools:
        parts.extend([
            "**Research tools (cost per call — use purposefully):**",
            "- web_search — search the web for news, analysis, SEC filings, earnings reports",
        ])
        if "x_search" not in spec.excluded_tools:
            parts.append(
                "- x_search — search X/Twitter for real-time sentiment and chatter"
            )
        parts.extend([
            "- url_fetch — fetch and read the full text of any web page or article",
            "",
            "**Market data tools (free — use liberally for non-primary symbols):**",
            "- check_price — real-time quote with price, bid/ask, volume, change + trend context",
            "- get_price_history — historical OHLCV bars (1d–1y periods, 1m–1d intervals)",
            "- get_fundamentals — P/E, EPS, market cap, beta, 52-week range, dividend yield",
            "- get_financial_statements — income statement, balance sheet, or cash flow (last 4 periods)",
            "- get_technical_indicators — RSI, MACD, Bollinger, SMAs, ATR, etc. with interpretation",
            "- check_options_activity — ATM IV, put/call volume & OI ratios",
            "- check_volume_regime — current volume vs session average (detects abnormal volume)",
            "- check_price_spike — detects significant recent price moves (>0.5% in 5 min)",
            "- check_insider_activity — recent insider buys/sells/grants",
            "- get_company_news — recent news articles (yfinance)",
            "- get_finnhub_news — recent news with headlines, summaries, sources (FinnHub)",
            "- get_analyst_ratings — analyst buy/hold/sell consensus and trends",
            "- get_movers — top market gainers/losers by index (check for sector-wide moves)",
            "- check_market_context — SPY, VIX, market session status",
        ])
    else:
        parts.append(
            "You have web search (Google grounding) for finding new angles."
        )

    parts.extend([
        "",
        "**Background data is ALREADY provided** in the prompt below — current price,",
        "fundamentals, technicals, options, volume, insider activity, analyst ratings,",
        "market context, and news for the primary symbol(s). Do NOT re-fetch this data.",
    ])

    if spec.function_tools:
        parts.extend([
            "",
            "**Your job is to INVESTIGATE beyond the basics:**",
            "- Search the web for breaking analysis, earnings context, or SEC filings",
            "- Read important articles with url_fetch for detailed information",
            "- Use market data tools for OTHER symbols (peers, sector ETFs, competitors)",
            "- Use get_financial_statements for deep fundamental digs (revenue trends, debt, cash flow)",
            "- Use get_movers / check_market_context to check sector or market-wide dynamics",
        ])
    else:
        parts.extend([
            "",
            "**Your job is to SYNTHESIZE and find NEW angles:**",
            "- Use web search to find perspectives, analysis, or evidence that prior agents missed",
            "- Cross-reference and verify prior agents' findings against each other",
            "- Look for contradictions, patterns, or implications that weren't explored",
            "- You do NOT have market data tools or url_fetch — all data is in the prompt",
        ])

    # Web search budget (prompt-enforced)
    if spec.web_search_limit > 0:
        parts.extend([
            "",
            f"## Web search budget",
            f"You have a STRICT budget of **{spec.web_search_limit} web searches**.",
            "Plan your searches carefully. Each search should have a clear purpose.",
            "Do NOT repeat searches that prior agents already performed.",
        ])

    if spec.function_tools:
        parts.extend([
            "",
            "## Cost awareness",
            "- web_search and x_search cost per call — use purposefully, not wastefully.",
            "- All market data tools and url_fetch are **free** — use liberally.",
        ])

    parts.extend([
        "",
        "## How to investigate",
        "Think step by step. Focus on areas NOT yet covered by prior agents (if any).",
        "Stop when you have enough evidence.",
    ])

    if spec.is_final:
        parts.extend([
            "",
            "## IMPORTANT: Final synthesis",
            "You are the FINAL agent. You MUST produce a definitive trading signal.",
            "All prior agents' findings and tool results are in the prompt — do NOT",
            "repeat their work. If you use web search, look for NEW angles or evidence",
            "that prior agents did not explore.",
            "",
            "Your primary job is deep synthesis and reasoning:",
            "- **Bull case:** What evidence supports a price move? What could go right?",
            "- **Bear case:** What could go wrong? Is this already priced in? What are the risks?",
            "- **Weigh** these against each other to reach your conclusion.",
        ])

    return "\n".join(parts)


def _build_pipeline_user_message(
    news: dict[str, Any],
    symbols: list[str],
    prior_rounds: list[dict[str, Any]],
    market: "MarketDataService | None" = None,
) -> str:
    """Build the user message with news + accumulated findings + tool ledger."""
    parts = [_build_user_message(news, symbols, market=market)]

    if prior_rounds:
        parts.append("\n\n---\n\n## Prior agent findings\n")
        for rnd in prior_rounds:
            agent_name = rnd["agent"]
            model_name = rnd.get("model", "unknown")
            findings = rnd["findings"]
            tool_count = len(rnd.get("tool_traces", []))
            parts.append(
                f"### Agent: {agent_name} (model: {model_name}, {tool_count} tool calls)\n"
                f"{findings}\n"
            )
            # Tool results ledger — pass investigative results downstream
            ledger = _format_tool_ledger(rnd.get("tool_traces", []))
            if ledger:
                parts.append(ledger)

    return "\n".join(parts)


# Tools whose results are already in the prompt via pre-fetch (Change 2).
# Their outputs are NOT included in the tool ledger to avoid duplication.
_PREFETCHED_TOOLS = frozenset({
    "check_price", "get_fundamentals", "get_technical_indicators",
    "check_options_activity", "check_volume_regime", "check_price_spike",
    "check_insider_activity", "get_company_news", "get_finnhub_news",
    "get_analyst_ratings", "get_price_history", "check_market_context",
})

# url_fetch can return very long articles — cap to keep prompts reasonable.
_URL_FETCH_MAX_CHARS = 3000


def _format_tool_ledger(tool_traces: list[dict[str, Any]]) -> str:
    """Format investigative tool results as a ledger for downstream agents.

    Includes full output for web_search, x_search, x_stream_cache.
    Caps url_fetch at _URL_FETCH_MAX_CHARS.
    Skips pre-fetched data tools entirely (already in prompt).
    """
    lines: list[str] = []

    for trace in tool_traces:
        action = trace.get("action", {})
        tool_name = action.get("tool", "")

        # Skip pre-fetched tools — their data is already in the prompt
        if tool_name in _PREFETCHED_TOOLS:
            continue

        # Skip errored calls
        if trace.get("error"):
            continue

        raw = trace.get("raw_tool_output")
        if raw is None:
            continue

        # Format the args summary
        args = action.get("args", {})
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""

        # Get string representation of output
        if isinstance(raw, str):
            output_str = raw
        elif isinstance(raw, dict):
            output_str = json.dumps(raw, default=str)
        elif isinstance(raw, list):
            output_str = json.dumps(raw, default=str)
        else:
            output_str = str(raw)

        # Cap url_fetch output
        if tool_name == "url_fetch" and len(output_str) > _URL_FETCH_MAX_CHARS:
            output_str = output_str[:_URL_FETCH_MAX_CHARS] + "... [truncated]"

        lines.append(f"**{tool_name}({args_str})**:\n```\n{output_str}\n```\n")

    if not lines:
        return ""
    return "#### Tool results from this agent:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Result of the full multi-agent pipeline."""

    signal: TradingSignal | None
    rounds: list[dict[str, Any]]
    all_tool_traces: list[dict[str, Any]]
    total_usage: dict[str, Any]
    rounds_completed: int


async def run_pipeline(
    *,
    news: dict[str, Any],
    symbols: list[str],
    market: MarketDataService | None = None,
    config: PipelineConfig | None = None,
    x_stream_service: Any = None,
) -> PipelineResult:
    """Run the multi-agent sequential pipeline.

    Args:
        news: News event dict with 'headline', 'summary', etc.
        symbols: Ticker symbols to investigate.
        market: MarketDataService instance (created if not provided).
        config: Pipeline configuration (built from env if not provided).
        x_stream_service: Optional XStreamService for cached X posts.

    Returns:
        PipelineResult with signal, all traces, and accumulated context.
    """
    if market is None:
        market = MarketDataService()
    if config is None:
        config = build_default_pipeline()

    xai_key = os.getenv("XAI_API_KEY")
    all_tool_traces: list[dict[str, Any]] = []
    all_rounds: list[dict[str, Any]] = []
    total_usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
        "tool_calls": 0,
        "reasoning_tokens": 0,
    }
    signal: TradingSignal | None = None

    # UsageLimits: only request_limit and tool_calls_limit as safety nets.
    # No token limits — agents run to completion. Budget control is done
    # at the pipeline level: check cumulative cost after each agent.
    usage_limits = UsageLimits(
        request_limit=config.request_limit,
        tool_calls_limit=config.tool_calls_limit,
    )
    cumulative_cost: float = 0.0

    for round_num in range(config.max_rounds):
        for i, spec in enumerate(config.agents):
            # Cost-based skip: if we've exceeded budget and this isn't the
            # final agent, skip to the next one (final agent always runs).
            if not spec.is_final and cumulative_cost >= config.max_cost_usd > 0:
                if DEBUG:
                    print(f"[Pipeline]   Skipping {spec.name} — "
                          f"cumulative cost ${cumulative_cost:.3f} >= ${config.max_cost_usd:.2f}")
                continue

            # Fresh deps per agent, but shared trace accumulator
            deps = ExplorerDeps(
                market=market,
                news=news,
                symbols=symbols,
                x_stream_service=x_stream_service,
                xai_api_key=xai_key,
                tool_calls_limit=config.tool_calls_limit,
                request_limit=config.request_limit,
                web_search_limit=spec.web_search_limit,
            )

            # Build agent with appropriate output type
            # Agents with function_tools=False (e.g. Gemini with Google grounding)
            # skip TracingToolset — they can't mix builtin + function tools.
            if spec.function_tools:
                base_toolset = market_toolset
                if spec.excluded_tools:
                    base_toolset = market_toolset.filtered(
                        lambda _ctx, td, _excl=spec.excluded_tools: td.name not in _excl,
                    )
                toolsets = [TracingToolset(base_toolset)]
            else:
                toolsets = []
            system_prompt = _build_system_prompt(
                spec, agent_index=i, total_agents=len(config.agents),
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

            # Build prompt with accumulated context + pre-fetched market data
            # Only pass market to the first agent's message (subsequent agents
            # get it via the first message, which is re-used in prior_rounds).
            msg_market = market if (round_num == 0 and i == 0) else None
            user_message = _build_pipeline_user_message(
                news, symbols, all_rounds, market=msg_market,
            )

            if DEBUG:
                print(f"[Pipeline] Round {round_num + 1}, Agent {i + 1}/{len(config.agents)}: "
                      f"{spec.name} ({'final' if spec.is_final else 'intermediate'})")

            start_time = time.time()

            # Determine model name for logging (needed in both success and error paths)
            model_name = spec.name
            if isinstance(spec.model, str):
                model_name = spec.model
            elif hasattr(spec.model, "model_name"):
                model_name = spec.model.model_name

            try:
                result = await agent.run(
                    user_message,
                    deps=deps,
                    usage_limits=usage_limits,
                    model_settings=spec.model_settings,
                )
            except (UsageLimitExceeded, AgentRunError) as e:
                # Graceful degradation: capture partial work and continue
                elapsed = round(time.time() - start_time, 1)
                all_tool_traces.extend(deps.tool_traces)
                round_record = {
                    "agent": spec.name,
                    "model": model_name,
                    "round": round_num + 1,
                    "system_prompt": system_prompt,
                    "user_message": user_message,
                    "findings": f"[INCOMPLETE: {type(e).__name__}: {e}]",
                    "tool_traces": deps.tool_traces,
                    "usage": {},
                    "cost_usd": 0.0,
                    "elapsed_s": elapsed,
                    "error": {"type": type(e).__name__, "message": str(e)},
                }
                all_rounds.append(round_record)
                if DEBUG:
                    print(f"[Pipeline]   → FAILED: {e}")
                continue  # try next agent

            elapsed = round(time.time() - start_time, 1)
            usage = result.usage()

            # Accumulate usage
            total_usage["input_tokens"] += usage.input_tokens or 0
            total_usage["output_tokens"] += usage.output_tokens or 0
            total_usage["total_tokens"] += usage.total_tokens or 0
            total_usage["requests"] += usage.requests or 0
            total_usage["tool_calls"] += usage.tool_calls or 0
            total_usage["reasoning_tokens"] += (
                usage.details.get("reasoning_tokens", 0)
                or usage.details.get("thoughts_tokens", 0)
            )

            # Extract builtin tool calls (e.g. web_search) from message history
            builtin_traces = _extract_builtin_tool_traces(
                result.all_messages(), deps,
            )
            deps.tool_traces.extend(builtin_traces)

            # Collect traces and findings
            all_tool_traces.extend(deps.tool_traces)

            # Estimate per-agent cost
            agent_cost = _estimate_agent_cost(
                spec.name, model_name,
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
            )

            cumulative_cost += agent_cost

            # Extract reasoning data
            # OpenAI: details['reasoning_tokens'], Gemini: details['thoughts_tokens']
            reasoning_tokens = (
                usage.details.get("reasoning_tokens", 0)
                or usage.details.get("thoughts_tokens", 0)
            )
            thinking_summary = _extract_thinking_content(result.all_messages())

            round_record = {
                "agent": spec.name,
                "model": model_name,
                "round": round_num + 1,
                "system_prompt": system_prompt,
                "user_message": user_message,
                "findings": str(result.output) if not spec.is_final else "",
                "tool_traces": deps.tool_traces,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                    "requests": usage.requests,
                    "tool_calls": usage.tool_calls,
                    "reasoning_tokens": reasoning_tokens,
                    "details": dict(usage.details),
                },
                "cost_usd": agent_cost,
                "elapsed_s": elapsed,
                "thinking_summary": thinking_summary,
            }
            all_rounds.append(round_record)

            if DEBUG:
                print(f"[Pipeline]   → {len(deps.tool_traces)} tool calls, "
                      f"{usage.total_tokens} tokens, ${agent_cost:.3f} "
                      f"(cumulative: ${cumulative_cost:.3f}), {elapsed}s")

            # If this is the final agent, capture the signal
            if spec.is_final:
                signal = result.output
                round_record["findings"] = (
                    f"Direction: {signal.direction}, "
                    f"Confidence: {signal.confidence}, "
                    f"Catalyst: {signal.key_catalyst}"
                )

        # Check if we should loop again
        if signal is not None and signal.confidence >= config.confidence_threshold:
            if DEBUG:
                print(f"[Pipeline] Confidence {signal.confidence} >= {config.confidence_threshold} — done.")
            break

        if round_num < config.max_rounds - 1:
            if DEBUG:
                print(f"[Pipeline] Confidence {signal.confidence if signal else 'N/A'} "
                      f"< {config.confidence_threshold} — starting round {round_num + 2}")

    if signal is None and DEBUG:
        print("[Pipeline] WARNING: Pipeline produced no signal (all agents may have failed)")

    # Re-number all traces sequentially
    for idx, trace in enumerate(all_tool_traces):
        trace["hop_index"] = idx
        trace["trace_id"] = f"trace_{idx}"

    return PipelineResult(
        signal=signal,
        rounds=all_rounds,
        all_tool_traces=all_tool_traces,
        total_usage=total_usage,
        rounds_completed=round_num + 1,
    )


# ---------------------------------------------------------------------------
# Convenience entry point (matches explore() signature)
# ---------------------------------------------------------------------------


async def explore(
    *,
    news: dict[str, Any],
    symbols: list[str],
    market: MarketDataService | None = None,
    config: PipelineConfig | None = None,
    x_stream_service: Any = None,
) -> ExploreResult:
    """Run the multi-agent pipeline and return an ExploreResult.

    This is a drop-in replacement for the single-agent explore() in
    explorer_agent.py.
    """
    result = await run_pipeline(
        news=news,
        symbols=symbols,
        market=market,
        config=config,
        x_stream_service=x_stream_service,
    )

    return ExploreResult(
        signal=result.signal,
        tool_traces=result.all_tool_traces,
        usage=result.total_usage,
    )


# ---------------------------------------------------------------------------
# Cost estimation from pipeline results
# ---------------------------------------------------------------------------


# Map default agent names → provider names for pricing lookup
_AGENT_TO_PROVIDER: dict[str, str] = {
    "grok": "grok",
    "openai": "openai",
    "gemini": "gemini",
}


def _estimate_agent_cost(
    agent_name: str,
    model_string: str,
    *,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate USD cost for a single agent's run. Returns 0.0 on error."""
    from trader.llm.pricing import (
        estimate_token_cost_grok,
        estimate_token_cost_openai,
        estimate_token_cost_gemini,
    )

    if not (input_tokens or output_tokens):
        return 0.0

    provider, raw_model = _extract_model_for_pricing(agent_name, model_string)
    try:
        if provider == "openai":
            return estimate_token_cost_openai(
                raw_model, input_tokens=input_tokens, output_tokens=output_tokens,
            ).total_cost_usd
        elif provider == "grok":
            return estimate_token_cost_grok(
                raw_model, input_tokens=input_tokens, output_tokens=output_tokens,
            ).total_cost_usd
        elif provider == "gemini":
            return estimate_token_cost_gemini(
                raw_model, input_tokens=input_tokens, output_tokens=output_tokens,
            ).total_cost_usd
    except (KeyError, ValueError):
        pass
    return 0.0


def _extract_model_for_pricing(spec_name: str, model_string: str) -> tuple[str, str]:
    """Extract (provider, raw_model_name) for pricing lookup.

    Handles:
      - "grok" agent name → provider "grok", model from model_string
      - "openai-responses:gpt-5-mini" → ("openai", "gpt-5-mini")
      - "google-gla:gemini-3-flash-preview" → ("gemini", "gemini-3-flash-preview")
      - OpenAIResponsesModel objects → model_name attribute
    """
    provider = _AGENT_TO_PROVIDER.get(spec_name, spec_name)

    # Strip PydanticAI model prefixes
    raw_model = model_string
    for prefix in ("openai-responses:", "openai:", "google-gla:", "anthropic:"):
        if raw_model.startswith(prefix):
            raw_model = raw_model[len(prefix):]
            break

    return provider, raw_model


def estimate_pipeline_cost(result: PipelineResult) -> float:
    """Estimate total USD cost from a PipelineResult using pricing tables.

    If rounds already have `cost_usd` (computed during execution), sums those.
    Otherwise falls back to re-computing from token usage.
    """
    total_cost = 0.0
    for rnd in result.rounds:
        if "cost_usd" in rnd:
            total_cost += rnd["cost_usd"]
        else:
            usage = rnd.get("usage", {})
            total_cost += _estimate_agent_cost(
                rnd["agent"],
                rnd.get("model", rnd["agent"]),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            )
    return total_cost
