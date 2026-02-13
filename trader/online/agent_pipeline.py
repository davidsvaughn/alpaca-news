"""Multi-agent sequential pipeline for news investigation.

Runs multiple PydanticAI agents (each backed by a different LLM provider)
sequentially. Each agent has access to all tools (market data, web search,
x_search, url_fetch, x_stream_cache) and sees accumulated findings from
prior agents.

Architecture:
    Agent 1 (Grok)   → web_search + x_search + all function tools
    Agent 2 (OpenAI) → web_search + all function tools
    Agent 3 (Gemini) → all function tools (no web search) → TradingSignal

The orchestrator passes context between agents and optionally loops
if the final agent's confidence is below a threshold.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic_ai import Agent, UsageLimits, WebSearchTool
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.openai import OpenAIResponsesModel
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
            if hasattr(content, "model_dump"):
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
                    "duration_s": 0.0,  # server-side, no client timing
                    "cost_usd": 0.0,
                },
                "raw_tool_output": content,
                "error": None,
                "builtin": True,
            }
            traces.append(trace)

    return traces


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


@dataclass
class PipelineConfig:
    """Configuration for the full pipeline."""

    agents: list[AgentSpec]
    max_rounds: int = 2
    confidence_threshold: float = 0.7
    # Per-agent budget limits
    request_limit: int = 15
    tool_calls_limit: int = 25
    total_tokens_limit: int = 80_000


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

    # Agent 2: OpenAI (web_search + strong reasoning)
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        openai_model = os.getenv("RESEARCH_MODEL", "gpt-5-mini")
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
        ))

    # Agent 3: Gemini (function tools only — no WebSearchTool)
    # Gemini cannot mix Google grounding with function tools, so it runs
    # with function tools only.  Prior agents' web search findings are
    # passed in the accumulated context, so Gemini can still synthesize
    # web evidence without calling web_search itself.
    google_key = os.getenv("GOOGLE_API_KEY")
    if google_key:
        gemini_model = os.getenv("SYNTHESIS_MODEL", "gemini-3-flash-preview")
        agents.append(AgentSpec(
            name="gemini",
            model=f"google-gla:{gemini_model}",
            builtin_tools=[],
            role_description=(
                "You are the FINAL analyst. Review ALL evidence gathered by "
                "previous investigators. You may use any tool to verify or "
                "extend their findings. Your job is to synthesize everything "
                "into a clear, actionable trading signal. Weigh bull vs bear "
                "cases carefully."
            ),
            is_final=True,
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

    return PipelineConfig(
        agents=agents,
        request_limit=settings.pipeline_request_limit,
        tool_calls_limit=settings.pipeline_tool_calls_limit,
        total_tokens_limit=settings.pipeline_total_tokens_limit,
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
        "You have access to real-time market data, company fundamentals, insider",
        "activity, technical indicators, price history, web search, X/Twitter",
        "search (x_search), URL fetching (url_fetch), and X stream cache.",
        "",
        "## Cost awareness",
        "- Financial data tools, url_fetch, and x_stream_cache are FREE — use liberally.",
        "- web_search and x_search cost per call — use purposefully.",
        "",
        "## How to investigate",
        "Think step by step. Use tools as needed. Focus on areas NOT yet covered",
        "by prior agents (if any). Stop when you have enough evidence.",
    ]

    if spec.is_final:
        parts.extend([
            "",
            "## IMPORTANT: Final synthesis",
            "You are the FINAL agent. You MUST produce a definitive trading signal.",
            "Synthesize ALL evidence from prior agents and your own investigation.",
            "Consider both sides:",
            "- **Bull case:** What evidence supports a price move?",
            "- **Bear case:** What could go wrong or be already priced in?",
            "Then weigh these to reach your conclusion.",
        ])

    return "\n".join(parts)


def _build_pipeline_user_message(
    news: dict[str, Any],
    symbols: list[str],
    prior_rounds: list[dict[str, Any]],
) -> str:
    """Build the user message with news + accumulated findings."""
    parts = [_build_user_message(news, symbols)]

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

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Result of the full multi-agent pipeline."""

    signal: TradingSignal
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
    }
    signal: TradingSignal | None = None

    for round_num in range(config.max_rounds):
        for i, spec in enumerate(config.agents):
            # Fresh deps per agent, but shared trace accumulator
            deps = ExplorerDeps(
                market=market,
                news=news,
                symbols=symbols,
                x_stream_service=x_stream_service,
                xai_api_key=xai_key,
                tool_calls_limit=config.tool_calls_limit,
                request_limit=config.request_limit,
                total_tokens_limit=config.total_tokens_limit,
            )

            # Build agent with appropriate output type
            tracing = TracingToolset(market_toolset)
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
                    toolsets=[tracing],
                )
            else:
                agent = Agent(
                    spec.model,
                    deps_type=ExplorerDeps,
                    output_type=str,
                    system_prompt=system_prompt,
                    builtin_tools=spec.builtin_tools,
                    toolsets=[tracing],
                )

            # Build prompt with accumulated context
            user_message = _build_pipeline_user_message(
                news, symbols, all_rounds,
            )

            if DEBUG:
                print(f"[Pipeline] Round {round_num + 1}, Agent {i + 1}/{len(config.agents)}: "
                      f"{spec.name} ({'final' if spec.is_final else 'intermediate'})")

            start_time = time.time()

            result = await agent.run(
                user_message,
                deps=deps,
                usage_limits=UsageLimits(
                    request_limit=config.request_limit,
                    tool_calls_limit=config.tool_calls_limit,
                    total_tokens_limit=config.total_tokens_limit,
                ),
            )

            elapsed = round(time.time() - start_time, 1)
            usage = result.usage()

            # Accumulate usage
            total_usage["input_tokens"] += usage.input_tokens or 0
            total_usage["output_tokens"] += usage.output_tokens or 0
            total_usage["total_tokens"] += usage.total_tokens or 0
            total_usage["requests"] += usage.requests or 0
            total_usage["tool_calls"] += usage.tool_calls or 0

            # Extract builtin tool calls (e.g. web_search) from message history
            builtin_traces = _extract_builtin_tool_traces(
                result.all_messages(), deps,
            )
            deps.tool_traces.extend(builtin_traces)

            # Collect traces and findings
            all_tool_traces.extend(deps.tool_traces)

            # Determine model name for logging
            model_name = spec.name
            if isinstance(spec.model, str):
                model_name = spec.model
            elif hasattr(spec.model, "model_name"):
                model_name = spec.model.model_name

            # Estimate per-agent cost
            agent_cost = _estimate_agent_cost(
                spec.name, model_name,
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
            )

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
                },
                "cost_usd": agent_cost,
                "elapsed_s": elapsed,
            }
            all_rounds.append(round_record)

            if DEBUG:
                print(f"[Pipeline]   → {len(deps.tool_traces)} tool calls, "
                      f"{usage.total_tokens} tokens, {elapsed}s")

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

    if signal is None:
        raise RuntimeError("Pipeline produced no signal — check agent configuration")

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
