"""Multi-agent sequential pipeline for news investigation.

Runs multiple agents sequentially, each backed by a different LLM provider
using native SDKs (not PydanticAI). Basic market data is pre-fetched once
and included in the prompt. Each agent's investigative tool results are
passed downstream via a tool call ledger.

Architecture (native SDK runners):
    Agent 1 (Grok)   → xAI Responses API — server-side x_search + web_search + function tools
    Agent 2 (OpenAI) → OpenAI Responses API — server-side web_search + function tools
    Agent 3 (Gemini) → google-genai SDK — Google Search grounding (no function tools) → TradingSignal

PydanticAI fallback runner is available for testing (TestModel) and
watcher check-ins.

Graceful degradation: if an agent fails, partial traces are preserved
and the pipeline continues to the next agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from trader.market.data_service import MarketDataService
from trader.online.agent_common import AgentRunResult, TradingSignal
from trader.online.explorer_agent import ExploreResult
from trader.online.prompt_builder import build_user_message as _build_user_message

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


@dataclass
class AgentSpec:
    """Specification for one agent in the pipeline."""

    name: str                       # e.g. "grok", "openai", "gemini"
    model: Any                      # model name string or PydanticAI Model object (for testing)
    builtin_tools: list[Any] = field(default_factory=list)  # PydanticAI only (for fallback)
    role_description: str = ""
    is_final: bool = False          # only the final agent produces TradingSignal
    function_tools: bool = True     # False = skip function tools (legacy; native runners ignore)
    excluded_tools: frozenset[str] = frozenset()
    web_search_limit: int = 0       # 0 = unlimited; >0 = prompt-enforced cap
    x_search_limit: int = 0         # 0 = unlimited; >0 = prompt-enforced cap
    model_settings: dict[str, Any] | None = None  # runner-specific settings
    runner: str = "pydanticai"      # "grok", "openai", "gemini", or "pydanticai" (fallback/testing)


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
    # Per-agent timeout in seconds. 0 = no timeout.
    agent_timeout_s: float = 120.0


# ---------------------------------------------------------------------------
# Agent factories
# ---------------------------------------------------------------------------


def build_default_pipeline() -> PipelineConfig:
    """Build the default agent pipeline from environment variables.

    Default: Grok → Gemini (2 agents). Set PIPELINE_INCLUDE_OPENAI=1 for
    the full 3-agent pipeline (Grok → OpenAI → Gemini).

    Uses native SDK runners for each provider:
    - Grok: xAI Responses API (server-side x_search + web_search)
    - OpenAI: OpenAI Responses API (server-side web_search) [optional]
    - Gemini: google-genai SDK (Google Search grounding + function tools)

    Falls back gracefully: if a provider's API key is missing, that agent
    is skipped. At minimum one agent must be available.
    """
    from trader.config import load_settings
    settings = load_settings()

    agents: list[AgentSpec] = []

    # Agent 1: Grok (server-side x_search + web_search + function tools)
    xai_key = os.getenv("XAI_API_KEY")
    if xai_key:
        grok_model_name = os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning")
        agents.append(AgentSpec(
            name="grok",
            model=grok_model_name,
            runner="grok",
            role_description=(
                "You are the FIRST investigator. Your strength is web and social "
                "media research. Use web_search to find news context, and x_search "
                "to check X/Twitter sentiment and chatter. Also gather key market "
                "data. Focus on breadth — cast a wide net."
            ),
        ))

    # Agent 2: OpenAI (optional — OFF by default, set PIPELINE_INCLUDE_OPENAI=1)
    if settings.pipeline_include_openai:
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            openai_model = os.getenv("RESEARCH_MODEL", "gpt-5.1")
            agents.append(AgentSpec(
                name="openai",
                model=openai_model,
                runner="openai",
                role_description=(
                    "You are the SECOND investigator. Review what the previous agent "
                    "found, then dig deeper. Use web_search to verify claims, find "
                    "contradicting evidence, or explore angles the first agent missed. "
                    "Check market data that hasn't been checked yet. Focus on depth "
                    "and verification."
                ),
                excluded_tools=frozenset({"x_search", "x_stream_cache"}),
            ))

    # Agent 3: Gemini (Google Search grounding only — synthesis agent)
    google_key = os.getenv("GOOGLE_API_KEY")
    if google_key:
        gemini_model = os.getenv("SYNTHESIS_MODEL", "gemini-3-flash-preview")
        agents.append(AgentSpec(
            name="gemini",
            model=gemini_model,
            runner="gemini",
            function_tools=False,
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
            excluded_tools=frozenset({"x_search", "x_stream_cache"}),
        ))

    if not agents:
        raise RuntimeError(
            "No LLM API keys configured. Set at least one of: "
            "XAI_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY"
        )

    # Mark the last agent as final (in case some were skipped)
    agents[-1].is_final = True

    # Apply web_search_limit to gpt-5-mini agents only
    ws_limit = settings.openai_web_search_limit
    if ws_limit > 0:
        for a in agents:
            model_str = a.model if isinstance(a.model, str) else getattr(a.model, "model_name", "")
            if "gpt-5-mini" in model_str:
                a.web_search_limit = ws_limit

    # Apply x_search_limit to agents that have x_search (not excluded)
    xs_limit = settings.max_x_searches_per_item
    if xs_limit > 0:
        for a in agents:
            if "x_search" not in a.excluded_tools:
                a.x_search_limit = xs_limit

    # Apply reasoning / thinking settings per provider
    for a in agents:
        if a.runner == "openai":
            a.model_settings = {
                "reasoning_effort": settings.openai_reasoning_effort,
                "reasoning_summary": "detailed",
            }
        elif a.runner == "gemini":
            thinking_level = settings.gemini_thinking_level
            if thinking_level == "off":
                a.model_settings = {"thinking_config": {"thinking_budget": 0}}
            elif thinking_level == "dynamic":
                a.model_settings = {"thinking_config": {"include_thoughts": True}}
            else:
                a.model_settings = {"thinking_config": {"thinking_level": thinking_level, "include_thoughts": True}}

    return PipelineConfig(
        agents=agents,
        request_limit=settings.pipeline_request_limit,
        tool_calls_limit=settings.pipeline_tool_calls_limit,
        max_cost_usd=settings.pipeline_max_cost_usd,
        agent_timeout_s=settings.pipeline_agent_timeout_s,
    )


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_system_prompt(
    spec: AgentSpec,
    agent_index: int,
    total_agents: int,
    primary_symbols: list[str] | None = None,
) -> str:
    """Build a role-aware system prompt for an agent."""
    parts = [
        "You are a financial research analyst investigating a breaking news event.",
        f"You are Agent {agent_index + 1} of {total_agents} in a sequential research pipeline.",
        "",
        "## Your role",
        spec.role_description,
        "",
        "## Your tools",
    ]

    has_tools = spec.function_tools

    if has_tools:
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
            "**Market data tools (free — use for peer/competitor analysis):**",
            "- check_price — real-time quote with price, bid/ask, volume, returns, 52-week range",
            "- get_price_history — historical OHLCV bars (1d–1y periods, 1m–1d intervals)",
            "- get_fundamentals — P/E, EPS, market cap, beta, 52-week range, dividend yield",
            "- get_financial_statements — income statement, balance sheet, or cash flow (last 4 periods)",
            "- get_technical_indicators — RSI, MACD, Bollinger, SMAs, ATR, etc. with interpretation",
        ])
    else:
        parts.append(
            "You have web search (Google grounding) for finding new angles."
        )

    primary_str = ", ".join((primary_symbols or [])[:3]) or "the primary symbol(s)"
    parts.extend([
        "",
        f"**Background data is ALREADY provided** in the prompt below for **{primary_str}**.",
        "Do NOT call these tools for the primary symbol(s) — the data is right there:",
        "check_price, get_fundamentals, get_technical_indicators, check_options_activity,",
        "check_volume_regime, check_price_spike, check_insider_activity, get_company_news,",
        "get_analyst_ratings, get_price_history.",
        "Market context (SPY, VIX, session) is also already provided — do NOT call check_market_context.",
        "",
        "These tools are **free** and encouraged for OTHER symbols (peers, sector ETFs, competitors).",
    ])

    if has_tools:
        parts.extend([
            "",
            "**Your job is to INVESTIGATE beyond the basics:**",
            "- Search the web for breaking analysis, earnings context, or SEC filings",
            "- Read important articles with url_fetch for detailed information",
            "- Use market data tools for OTHER symbols (peers, sector ETFs, competitors)",
            "- Use get_financial_statements for deep fundamental digs (revenue trends, debt, cash flow)",
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
            "## Web search budget",
            f"You have a STRICT budget of **{spec.web_search_limit} web searches**.",
            "Plan your searches carefully. Each search should have a clear purpose.",
            "Do NOT repeat searches that prior agents already performed.",
        ])

    # x_search budget
    if spec.x_search_limit > 0 and "x_search" not in spec.excluded_tools:
        parts.extend([
            "",
            "## X/Twitter search budget",
            f"You have a budget of **{spec.x_search_limit} x_search calls**.",
            "Use x_search to check real-time sentiment and chatter — aim for at least 1-2 calls.",
        ])

    if has_tools:
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

    if not spec.is_final:
        parts.extend([
            "",
            "## IMPORTANT: Automated pipeline",
            "You are in an automated pipeline with no human in the loop.",
            "Do NOT ask follow-up questions, present menus of options,",
            "or offer to do additional work. State your findings concisely and stop.",
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

        # JSON output instructions for native runners
        if spec.runner != "pydanticai":
            parts.extend([
                "",
                "## Output format",
                "After your analysis, you MUST output a JSON object in a ```json code block",
                "with EXACTLY this schema:",
                "```json",
                "{",
                '  "direction": "bullish" | "bearish" | "neutral",',
                '  "confidence": 0.0 to 1.0,',
                '  "horizon": "15m" | "60m" | "1d",',
                '  "magnitude_estimate": "e.g. 0.5-1.5%",',
                '  "key_catalyst": "one-sentence summary of the main catalyst",',
                '  "bull_case": "brief bull case argument",',
                '  "bear_case": "brief bear case argument",',
                '  "risk_factors": ["risk1", "risk2", ...]',
                "}",
                "```",
                "This JSON block MUST be the last thing in your response.",
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
            ledger = _format_tool_ledger(
                rnd.get("tool_traces", []),
                primary_symbols=symbols,
            )
            if ledger:
                parts.append(ledger)

    return "\n".join(parts)


# Tools whose results are already in the prompt via pre-fetch.
# Their outputs are NOT included in the tool ledger to avoid duplication.
_PREFETCHED_TOOLS = frozenset({
    "check_price", "get_fundamentals", "get_technical_indicators",
    "check_options_activity", "check_volume_regime", "check_price_spike",
    "check_insider_activity", "get_company_news",
    "get_analyst_ratings", "get_price_history", "check_market_context",
})

# url_fetch can return very long articles — cap to keep prompts reasonable.
_URL_FETCH_MAX_CHARS = 3000


def _format_tool_ledger(
    tool_traces: list[dict[str, Any]],
    primary_symbols: list[str] | None = None,
) -> str:
    """Format tool results as a ledger for downstream agents.

    - **Builtin (server-side) search traces** (web_search, x_search from
      Grok/OpenAI): shown as a compact list of queries/URLs. The actual
      results are opaque (consumed internally by the model) and reflected
      in the agent's findings text — NOT in the trace output.
    - **Function tool results** (get_financial_statements, get_movers, etc.):
      shown with full output.
    - **Pre-fetched tools** (check_price, get_fundamentals, etc.): skipped
      for primary symbols (already in the prompt), but INCLUDED for peer
      symbols that were not pre-fetched.
    - **url_fetch**: capped at _URL_FETCH_MAX_CHARS.
    """
    primary_set = {s.upper() for s in (primary_symbols or [])[:3]}
    func_lines: list[str] = []
    builtin_lines: list[str] = []

    for trace in tool_traces:
        action = trace.get("action", {})
        tool_name = action.get("tool", "")
        args = action.get("args", {})

        if trace.get("error"):
            continue

        # Builtin (server-side) search traces — compact summary only
        if trace.get("builtin"):
            if "query" in args:
                builtin_lines.append(f"- {tool_name}: \"{args['query']}\"")
            elif "url" in args:
                builtin_lines.append(f"- {tool_name}: opened {args['url']}")
            elif "pattern" in args:
                builtin_lines.append(
                    f"- {tool_name}: find_in_page \"{args['pattern']}\""
                )
            continue

        # Pre-fetched tools: skip for primary symbols, include for peers
        if tool_name in _PREFETCHED_TOOLS:
            call_symbol = args.get("symbol", "")
            if not call_symbol or call_symbol.upper() in primary_set:
                continue
            # Peer-symbol call — fall through to include output

        raw = trace.get("raw_tool_output")
        if raw is None:
            continue

        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""

        if isinstance(raw, str):
            output_str = raw
        elif isinstance(raw, (dict, list)):
            output_str = json.dumps(raw, default=str)
        else:
            output_str = str(raw)

        if tool_name == "url_fetch" and len(output_str) > _URL_FETCH_MAX_CHARS:
            output_str = output_str[:_URL_FETCH_MAX_CHARS] + "... [truncated]"

        func_lines.append(f"**{tool_name}({args_str})**:\n```\n{output_str}\n```\n")

    result_parts: list[str] = []

    if builtin_lines:
        result_parts.append(
            "#### Web/social searches performed by this agent:\n"
            "(Results were consumed by the agent and reflected in the "
            "findings above. Do NOT repeat these searches.)\n"
            + "\n".join(builtin_lines)
        )

    if func_lines:
        result_parts.append(
            "#### Tool results from this agent:\n" + "\n".join(func_lines)
        )

    return "\n\n".join(result_parts)


# ---------------------------------------------------------------------------
# Runner dispatch
# ---------------------------------------------------------------------------


async def _dispatch_runner(
    spec: AgentSpec,
    system_prompt: str,
    user_message: str,
    market: MarketDataService,
    x_stream_service: Any,
    config: PipelineConfig,
) -> AgentRunResult:
    """Dispatch to the appropriate runner based on spec.runner."""

    if spec.runner == "grok":
        from trader.online.runners.grok_runner import run_grok
        return await run_grok(
            system_prompt=system_prompt,
            user_message=user_message,
            model=spec.model,
            excluded_tools=spec.excluded_tools,
            market=market,
            x_stream_service=x_stream_service,
            max_turns=config.request_limit,
            is_final=spec.is_final,
        )

    elif spec.runner == "openai":
        from trader.online.runners.openai_runner import run_openai
        settings = spec.model_settings or {}
        return await run_openai(
            system_prompt=system_prompt,
            user_message=user_message,
            model=spec.model,
            excluded_tools=spec.excluded_tools,
            market=market,
            x_stream_service=x_stream_service,
            max_turns=config.request_limit,
            reasoning_effort=settings.get("reasoning_effort"),
            reasoning_summary=settings.get("reasoning_summary"),
            is_final=spec.is_final,
        )

    elif spec.runner == "gemini":
        from trader.online.runners.gemini_runner import run_gemini
        settings = spec.model_settings or {}
        return await run_gemini(
            system_prompt=system_prompt,
            user_message=user_message,
            model=spec.model,
            excluded_tools=spec.excluded_tools,
            market=market,
            x_stream_service=x_stream_service,
            max_turns=config.request_limit,
            thinking_config=settings.get("thinking_config"),
            is_final=spec.is_final,
        )

    elif spec.runner == "pydanticai":
        from trader.online.runners.pydanticai_runner import run_pydanticai
        return await run_pydanticai(
            spec=spec,
            system_prompt=system_prompt,
            user_message=user_message,
            market=market,
            x_stream_service=x_stream_service,
            config=config,
        )

    else:
        raise ValueError(f"Unknown runner: {spec.runner!r}")


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
    prefetched_market_data: str = ""


async def run_pipeline(
    *,
    news: dict[str, Any],
    symbols: list[str],
    market: MarketDataService | None = None,
    config: PipelineConfig | None = None,
    x_stream_service: Any = None,
    on_stage: Callable[[str, int, int], None] | None = None,
    abort_check: Callable[[], None] | None = None,
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
    cumulative_cost: float = 0.0

    # Pre-compute market data text (used in first agent's prompt, stored for export)
    from trader.online.prompt_builder import prefetch_market_data
    prefetch_text = prefetch_market_data(symbols, market) if market else ""

    for round_num in range(config.max_rounds):
        for i, spec in enumerate(config.agents):
            # Cost-based skip
            if not spec.is_final and cumulative_cost >= config.max_cost_usd > 0:
                if DEBUG:
                    print(f"[Pipeline]   Skipping {spec.name} — "
                          f"cumulative cost ${cumulative_cost:.3f} >= ${config.max_cost_usd:.2f}")
                continue

            system_prompt = _build_system_prompt(
                spec, agent_index=i, total_agents=len(config.agents),
                primary_symbols=symbols,
            )

            # Build prompt with accumulated context + pre-fetched market data
            msg_market = market if (round_num == 0 and i == 0) else None
            user_message = _build_pipeline_user_message(
                news, symbols, all_rounds, market=msg_market,
            )

            # Determine model name for logging
            model_name = spec.name
            if isinstance(spec.model, str):
                model_name = spec.model
            elif hasattr(spec.model, "model_name"):
                model_name = spec.model.model_name

            if DEBUG:
                print(f"[Pipeline] Round {round_num + 1}, Agent {i + 1}/{len(config.agents)}: "
                      f"{spec.name} ({spec.runner}, {'final' if spec.is_final else 'intermediate'})")

            if on_stage is not None:
                on_stage(spec.name, i + 1, len(config.agents))

            # Cooperative abort check — raises JobAborted if flagged
            if abort_check is not None:
                abort_check()

            start_time = time.time()

            try:
                _coro = _dispatch_runner(
                    spec=spec,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    market=market,
                    x_stream_service=x_stream_service,
                    config=config,
                )
                if config.agent_timeout_s > 0:
                    agent_result = await asyncio.wait_for(_coro, timeout=config.agent_timeout_s)
                else:
                    agent_result = await _coro

            except (TimeoutError, Exception) as e:
                # Graceful degradation
                elapsed = round(time.time() - start_time, 1)
                error_msg = str(e)
                round_record = {
                    "agent": spec.name,
                    "model": model_name,
                    "round": round_num + 1,
                    "system_prompt": system_prompt,
                    "user_message": user_message,
                    "findings": f"[INCOMPLETE: {type(e).__name__}: {e}]",
                    "tool_traces": [],
                    "usage": {"tool_calls": 0},
                    "cost_usd": 0.0,
                    "elapsed_s": elapsed,
                    "error": {"type": type(e).__name__, "message": error_msg},
                }
                all_rounds.append(round_record)
                print(
                    f"[Pipeline] Agent {spec.name} ({model_name}) FAILED "
                    f"after {elapsed}s: {type(e).__name__}: {error_msg}"
                )
                continue

            elapsed = round(time.time() - start_time, 1)

            # Accumulate usage
            for key in ("input_tokens", "output_tokens", "total_tokens",
                        "requests", "tool_calls", "reasoning_tokens"):
                total_usage[key] += agent_result.usage.get(key, 0)

            # Collect traces — stamp agent name on each
            for _tr in agent_result.tool_traces:
                _tr["agent"] = spec.name
            all_tool_traces.extend(agent_result.tool_traces)

            # Estimate per-agent cost
            agent_cost = _estimate_agent_cost(
                spec.name, model_name,
                input_tokens=agent_result.usage.get("input_tokens", 0),
                output_tokens=agent_result.usage.get("output_tokens", 0),
            )
            cumulative_cost += agent_cost

            # Build round record
            if isinstance(agent_result.output, TradingSignal):
                signal = agent_result.output
                findings = (
                    f"Direction: {signal.direction}, "
                    f"Confidence: {signal.confidence}, "
                    f"Catalyst: {signal.key_catalyst}"
                )
            else:
                findings = str(agent_result.output) if agent_result.output else ""

            # Always capture raw output text for diagnostics
            raw_output = str(agent_result.output) if agent_result.output else ""
            signal_dict = None
            if isinstance(agent_result.output, TradingSignal):
                signal_dict = agent_result.output.model_dump()

            round_record = {
                "agent": spec.name,
                "model": model_name,
                "round": round_num + 1,
                "system_prompt": system_prompt,
                "user_message": user_message,
                "findings": findings,
                "raw_output": raw_output,
                "signal": signal_dict,
                "tool_traces": agent_result.tool_traces,
                "usage": agent_result.usage,
                "cost_usd": agent_cost,
                "elapsed_s": elapsed,
                "thinking_summary": agent_result.thinking_summary,
            }
            all_rounds.append(round_record)

            if DEBUG:
                print(f"[Pipeline]   → {len(agent_result.tool_traces)} tool calls, "
                      f"{agent_result.usage.get('total_tokens', 0)} tokens, ${agent_cost:.3f} "
                      f"(cumulative: ${cumulative_cost:.3f}), {elapsed}s")

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
        prefetched_market_data=prefetch_text,
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
    on_stage: Callable[[str, int, int], None] | None = None,
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
        on_stage=on_stage,
    )

    return ExploreResult(
        signal=result.signal,
        tool_traces=result.all_tool_traces,
        usage=result.total_usage,
    )


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


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
      - Raw model names like "gpt-5.1" → use agent name for provider
    """
    provider = _AGENT_TO_PROVIDER.get(spec_name, spec_name)

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
