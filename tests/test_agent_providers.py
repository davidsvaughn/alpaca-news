"""Per-provider integration tests for the multi-agent pipeline.

These tests make REAL API calls. They verify that each provider:
1. Can construct a PydanticAI Agent
2. WebSearchTool works alongside function tools
3. The agent can call function tools and produce output
4. TracingToolset captures all tool calls

Run with: python -m pytest tests/test_agent_providers.py -v -s
Or selectively: python -m pytest tests/test_agent_providers.py -k grok -v -s

Each test uses a simple prompt and limits to keep costs low.
"""

from __future__ import annotations

import json
import os
import pytest
import asyncio
from unittest.mock import MagicMock

from dotenv import load_dotenv

load_dotenv()

from pydantic_ai import Agent, UsageLimits, WebSearchTool
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from trader.online.explorer_agent import (
    ExplorerDeps,
    TradingSignal,
    TracingToolset,
    market_toolset,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_market():
    """Mock MarketDataService — avoids needing Schwab credentials."""
    market = MagicMock()
    market.get_quote.return_value = {"symbol": "NVDA", "last": 190.05, "change": 1.46}
    market.build_market_context.return_value = {"spy": 605.0, "vix": 15.2, "session": "open"}
    market.check_options_activity.return_value = {"atm_iv": 0.42, "put_call_ratio": 0.85}
    market.get_fundamentals.return_value = {"pe": 65.0, "market_cap": "1.5T"}
    market.get_movers.return_value = {"movers": []}
    market.check_insider_activity.return_value = {"transactions": []}
    market.get_company_news.return_value = {"articles": []}
    market.get_price_history.return_value = {"bars": []}
    market.get_current_technicals.return_value = {"rsi": 55.0, "macd": "bullish"}
    market.check_price_spike.return_value = {"spike": False}
    market.check_volume_regime.return_value = {"abnormal": False}
    return market


@pytest.fixture
def deps(mock_market):
    return ExplorerDeps(
        market=mock_market,
        news={"headline": "NVDA reports record earnings", "summary": "Revenue up 200%"},
        symbols=["NVDA"],
        xai_api_key=os.getenv("XAI_API_KEY"),
    )


NEWS_PROMPT = (
    "## The news event\n"
    "**Headline:** NVDA reports record earnings\n"
    "**Summary:** Revenue up 200%\n"
    "**Symbols:** NVDA\n\n"
    "Investigate briefly. Use 2-3 tools max, then produce your assessment."
)

# Tight limits to keep costs low
TEST_LIMITS = UsageLimits(request_limit=5, tool_calls_limit=8, total_tokens_limit=30_000)


# ---------------------------------------------------------------------------
# Grok (xAI) — WebSearchTool + function tools
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("XAI_API_KEY"), reason="XAI_API_KEY not set")
@pytest.mark.asyncio
async def test_grok_with_websearch_and_tools(deps):
    """Grok via OpenAIResponsesModel: WebSearchTool + function tools."""
    provider = OpenAIProvider(
        api_key=os.environ["XAI_API_KEY"],
        base_url="https://api.x.ai/v1/",
    )
    model = OpenAIResponsesModel(
        os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning"),
        provider=provider,
    )

    tracing = TracingToolset(market_toolset)
    agent = Agent(
        model,
        deps_type=ExplorerDeps,
        output_type=TradingSignal,
        system_prompt="You are a financial analyst. Investigate briefly, use a few tools, then produce a trading signal.",
        builtin_tools=[WebSearchTool(search_context_size=None)],
        toolsets=[tracing],
    )

    result = await agent.run(NEWS_PROMPT, deps=deps, usage_limits=TEST_LIMITS)

    # Verify structured output
    signal = result.output
    assert isinstance(signal, TradingSignal)
    assert signal.direction in ("bullish", "bearish", "neutral")
    assert 0.0 <= signal.confidence <= 1.0
    print(f"\n[Grok] Signal: {signal.direction} @ {signal.confidence}")
    print(f"[Grok] Catalyst: {signal.key_catalyst}")

    # Verify tool traces were captured
    traces = deps.tool_traces
    assert len(traces) > 0, "No tool traces captured"
    tool_names = [t["action"]["tool"] for t in traces]
    print(f"[Grok] Tools called: {tool_names}")

    # Verify usage
    usage = result.usage()
    print(f"[Grok] Usage: {usage.requests} requests, {usage.total_tokens} tokens")
    assert usage.requests > 0
    assert usage.total_tokens > 0


# ---------------------------------------------------------------------------
# Grok — x_search function tool
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("XAI_API_KEY"), reason="XAI_API_KEY not set")
@pytest.mark.asyncio
async def test_grok_x_search_function_tool(deps):
    """Verify x_search function tool works (calls xAI Responses API)."""
    provider = OpenAIProvider(
        api_key=os.environ["XAI_API_KEY"],
        base_url="https://api.x.ai/v1/",
    )
    model = OpenAIResponsesModel(
        os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning"),
        provider=provider,
    )

    tracing = TracingToolset(market_toolset)
    agent = Agent(
        model,
        deps_type=ExplorerDeps,
        output_type=str,
        system_prompt=(
            "You are a financial analyst. Check X/Twitter sentiment about NVDA "
            "using the x_search tool, then check the current price. "
            "Summarize what you find in 2-3 sentences."
        ),
        toolsets=[tracing],
    )

    result = await agent.run(
        "Check X/Twitter sentiment and price for NVDA.",
        deps=deps,
        usage_limits=TEST_LIMITS,
    )

    tool_names = [t["action"]["tool"] for t in deps.tool_traces]
    print(f"\n[Grok x_search] Tools called: {tool_names}")
    print(f"[Grok x_search] Output: {result.output[:200]}")

    # x_search should have been called
    assert "x_search" in tool_names, f"x_search not called. Tools: {tool_names}"

    # Check the x_search trace has actual results (not an error)
    x_traces = [t for t in deps.tool_traces if t["action"]["tool"] == "x_search"]
    for t in x_traces:
        raw = t["raw_tool_output"]
        assert raw is not None
        assert "error" not in raw or raw.get("error") is None, f"x_search error: {raw}"
        print(f"[Grok x_search] Citations: {raw.get('citations', [])[:3]}")


# ---------------------------------------------------------------------------
# OpenAI — WebSearchTool + function tools
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_openai_with_websearch_and_tools(deps):
    """OpenAI Responses API: WebSearchTool + function tools."""
    model_name = os.getenv("RESEARCH_MODEL", "gpt-4o-mini")

    tracing = TracingToolset(market_toolset)
    agent = Agent(
        f"openai-responses:{model_name}",
        deps_type=ExplorerDeps,
        output_type=TradingSignal,
        system_prompt="You are a financial analyst. Investigate briefly, use a few tools, then produce a trading signal.",
        builtin_tools=[WebSearchTool()],
        toolsets=[tracing],
    )

    result = await agent.run(NEWS_PROMPT, deps=deps, usage_limits=TEST_LIMITS)

    signal = result.output
    assert isinstance(signal, TradingSignal)
    assert signal.direction in ("bullish", "bearish", "neutral")
    print(f"\n[OpenAI] Signal: {signal.direction} @ {signal.confidence}")
    print(f"[OpenAI] Catalyst: {signal.key_catalyst}")

    traces = deps.tool_traces
    tool_names = [t["action"]["tool"] for t in traces]
    print(f"[OpenAI] Tools called: {tool_names}")

    usage = result.usage()
    print(f"[OpenAI] Usage: {usage.requests} requests, {usage.total_tokens} tokens")
    assert usage.requests > 0


# ---------------------------------------------------------------------------
# Gemini (Google) — function tools only (no WebSearchTool)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not set")
@pytest.mark.asyncio
async def test_gemini_with_function_tools(deps):
    """Gemini: function tools only (cannot mix with Google grounding)."""
    model_name = os.getenv("SYNTHESIS_MODEL", "gemini-2.5-flash")

    tracing = TracingToolset(market_toolset)
    agent = Agent(
        f"google-gla:{model_name}",
        deps_type=ExplorerDeps,
        output_type=TradingSignal,
        system_prompt="You are a financial analyst. Investigate briefly, use a few tools, then produce a trading signal.",
        toolsets=[tracing],
    )

    result = await agent.run(NEWS_PROMPT, deps=deps, usage_limits=TEST_LIMITS)

    signal = result.output
    assert isinstance(signal, TradingSignal)
    assert signal.direction in ("bullish", "bearish", "neutral")
    print(f"\n[Gemini] Signal: {signal.direction} @ {signal.confidence}")
    print(f"[Gemini] Catalyst: {signal.key_catalyst}")

    traces = deps.tool_traces
    tool_names = [t["action"]["tool"] for t in traces]
    print(f"[Gemini] Tools called: {tool_names}")

    usage = result.usage()
    print(f"[Gemini] Usage: {usage.requests} requests, {usage.total_tokens} tokens")
    assert usage.requests > 0


# ---------------------------------------------------------------------------
# Full pipeline test (TestModel — no API calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_with_test_model(mock_market):
    """Full 3-agent pipeline using TestModel (no real API calls)."""
    from pydantic_ai.models.test import TestModel
    from trader.online.agent_pipeline import (
        AgentSpec,
        PipelineConfig,
        PipelineResult,
        run_pipeline,
    )

    config = PipelineConfig(
        agents=[
            AgentSpec(name="agent1", model=TestModel(), builtin_tools=[],
                      role_description="First investigator."),
            AgentSpec(name="agent2", model=TestModel(), builtin_tools=[],
                      role_description="Second investigator."),
            AgentSpec(name="agent3", model=TestModel(), builtin_tools=[],
                      role_description="Final analyst.", is_final=True),
        ],
        max_rounds=1,
        request_limit=10,
        tool_calls_limit=20,
    )

    result = await run_pipeline(
        news={"headline": "NVDA beats earnings", "summary": "Revenue up 200%"},
        symbols=["NVDA"],
        market=mock_market,
        config=config,
    )

    assert isinstance(result, PipelineResult)
    assert isinstance(result.signal, TradingSignal)
    assert len(result.rounds) == 3
    assert result.rounds_completed == 1

    # Traces should be sequentially numbered
    for i, trace in enumerate(result.all_tool_traces):
        assert trace["hop_index"] == i

    print(f"\n[Pipeline] Signal: {result.signal.direction}")
    print(f"[Pipeline] Total traces: {len(result.all_tool_traces)}")
    print(f"[Pipeline] Total usage: {result.total_usage}")
