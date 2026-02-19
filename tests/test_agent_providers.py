"""Per-provider integration tests for the native SDK runners.

These tests make REAL API calls. They verify that each native runner:
1. Can call the provider's API
2. Server-side search tools work (web_search, x_search)
3. Function tools execute and return results
4. Tool traces are captured correctly
5. Usage stats are populated

Run with: python -m pytest tests/test_agent_providers.py -v -s
Or selectively: python -m pytest tests/test_agent_providers.py -k grok -v -s

Each test uses a simple prompt and tight max_turns to keep costs low.
"""

from __future__ import annotations

import json
import os
import pytest
import asyncio
from unittest.mock import MagicMock

from dotenv import load_dotenv

load_dotenv()

from trader.online.agent_common import AgentRunResult, TradingSignal
from trader.online.explorer_agent import ExplorerDeps, TracingToolset, market_toolset


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
    market.get_financial_statements.return_value = {"statements": []}
    market.get_finnhub_news.return_value = {"symbol": "NVDA", "count": 0, "articles": [], "source": "finnhub"}
    market.get_analyst_ratings.return_value = {"ratings": []}
    return market


@pytest.fixture
def deps(mock_market):
    return ExplorerDeps(
        market=mock_market,
        news={"headline": "NVDA reports record earnings", "summary": "Revenue up 200%"},
        symbols=["NVDA"],
        xai_api_key=os.getenv("XAI_API_KEY"),
    )


SYSTEM_PROMPT = (
    "You are a financial analyst. Investigate briefly using the tools available, "
    "then produce a short 2-3 sentence assessment."
)

USER_MESSAGE = (
    "## The news event\n"
    "**Headline:** NVDA reports record earnings\n"
    "**Summary:** Revenue up 200%\n"
    "**Symbols:** NVDA\n\n"
    "Investigate briefly. Use 2-3 tools max, then produce your assessment."
)


# ---------------------------------------------------------------------------
# Grok (xAI) — native SDK: server-side web_search + x_search + function tools
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("XAI_API_KEY"), reason="XAI_API_KEY not set")
@pytest.mark.asyncio
async def test_grok_with_websearch_and_tools(mock_market):
    """Grok native runner: server-side web_search + x_search + function tools."""
    from trader.online.runners.grok_runner import run_grok

    result = await run_grok(
        system_prompt=SYSTEM_PROMPT,
        user_message=USER_MESSAGE,
        model=os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning"),
        market=mock_market,
        max_turns=5,
        is_final=False,
    )

    assert isinstance(result, AgentRunResult)
    assert isinstance(result.output, str)
    assert len(result.output) > 0

    tool_names = [t["action"]["tool"] for t in result.tool_traces]
    print(f"\n[Grok] Output: {result.output[:200]}")
    print(f"[Grok] Tools called: {tool_names}")
    print(f"[Grok] Usage: {result.usage}")

    assert result.usage["requests"] > 0
    assert result.usage["total_tokens"] > 0


@pytest.mark.skipif(not os.getenv("XAI_API_KEY"), reason="XAI_API_KEY not set")
@pytest.mark.asyncio
async def test_grok_x_search_server_side(mock_market):
    """Verify Grok's server-side x_search works (no inner API call)."""
    from trader.online.runners.grok_runner import run_grok

    result = await run_grok(
        system_prompt=(
            "You are a financial analyst. Search X/Twitter for sentiment about NVDA "
            "using x_search, then summarize what you find."
        ),
        user_message="Check X/Twitter sentiment for NVDA.",
        model=os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning"),
        market=mock_market,
        max_turns=5,
        is_final=False,
    )

    tool_names = [t["action"]["tool"] for t in result.tool_traces]
    print(f"\n[Grok x_search] Tools called: {tool_names}")
    print(f"[Grok x_search] Output: {result.output[:200]}")

    # Server-side x_search should have been called
    assert "x_search" in tool_names, f"x_search not called. Tools: {tool_names}"

    # Traces should be marked as builtin (server-side)
    x_traces = [t for t in result.tool_traces if t["action"]["tool"] == "x_search"]
    for t in x_traces:
        assert t["builtin"] is True, "x_search trace should be marked builtin (server-side)"


# ---------------------------------------------------------------------------
# OpenAI — native SDK: server-side web_search + function tools
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_openai_with_websearch_and_tools(mock_market):
    """OpenAI native runner: Responses API with server-side web_search."""
    from trader.online.runners.openai_runner import run_openai

    result = await run_openai(
        system_prompt=SYSTEM_PROMPT,
        user_message=USER_MESSAGE,
        model=os.getenv("RESEARCH_MODEL", "gpt-5-mini"),
        market=mock_market,
        max_turns=5,
        reasoning_effort="low",
        is_final=False,
    )

    assert isinstance(result, AgentRunResult)
    assert isinstance(result.output, str)
    assert len(result.output) > 0

    tool_names = [t["action"]["tool"] for t in result.tool_traces]
    print(f"\n[OpenAI] Output: {result.output[:200]}")
    print(f"[OpenAI] Tools called: {tool_names}")
    print(f"[OpenAI] Usage: {result.usage}")

    assert result.usage["requests"] > 0
    assert result.usage["total_tokens"] > 0


# ---------------------------------------------------------------------------
# Gemini — native SDK: Google Search grounding + function tools
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not set")
@pytest.mark.asyncio
async def test_gemini_with_function_tools(mock_market):
    """Gemini native runner: Google Search grounding + all function tools."""
    from trader.online.runners.gemini_runner import run_gemini

    result = await run_gemini(
        system_prompt=SYSTEM_PROMPT,
        user_message=USER_MESSAGE,
        model=os.getenv("SENTIMENT_MODEL", "gemini-3-flash-preview"),
        market=mock_market,
        max_turns=5,
        is_final=False,
    )

    assert isinstance(result, AgentRunResult)
    assert isinstance(result.output, str)
    assert len(result.output) > 0

    tool_names = [t["action"]["tool"] for t in result.tool_traces]
    print(f"\n[Gemini] Output: {result.output[:200]}")
    print(f"[Gemini] Tools called: {tool_names}")
    print(f"[Gemini] Usage: {result.usage}")

    assert result.usage["requests"] > 0
    assert result.usage["total_tokens"] > 0


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

    # Traces should be sequentially numbered and have modality tags
    for i, trace in enumerate(result.all_tool_traces):
        assert trace["hop_index"] == i
        assert "modality" in trace, f"Trace {i} missing 'modality' field"
        assert isinstance(trace["modality"], str)

    print(f"\n[Pipeline] Signal: {result.signal.direction}")
    print(f"[Pipeline] Total traces: {len(result.all_tool_traces)}")
    print(f"[Pipeline] Total usage: {result.total_usage}")

    # Verify that traces have valid modality values
    valid_modalities = {"market_data", "macro", "fundamentals", "news", "web_research", "social", "other"}
    modalities_seen = {t["modality"] for t in result.all_tool_traces}
    assert modalities_seen <= valid_modalities, f"Unexpected modalities: {modalities_seen - valid_modalities}"
    print(f"[Pipeline] Modalities seen: {modalities_seen}")


@pytest.mark.asyncio
async def test_snapshot_captures_rounds_and_modalities(mock_market):
    """Verify that Snapshot stores rounds and builds data_modalities index."""
    from pydantic_ai.models.test import TestModel
    from trader.online.agent_pipeline import (
        AgentSpec,
        PipelineConfig,
        run_pipeline,
    )
    from trader.models.snapshot import SnapshotBuilder, Trigger

    config = PipelineConfig(
        agents=[
            AgentSpec(name="agent1", model=TestModel(), builtin_tools=[],
                      role_description="First investigator."),
            AgentSpec(name="final", model=TestModel(), builtin_tools=[],
                      role_description="Final analyst.", is_final=True),
        ],
        max_rounds=1,
        request_limit=10,
        tool_calls_limit=20,
    )

    result = await run_pipeline(
        news={"headline": "AAPL drops 5%", "summary": "iPhone sales decline"},
        symbols=["AAPL"],
        market=mock_market,
        config=config,
    )

    # Build a snapshot from pipeline results
    trigger = Trigger(
        type="test",
        alpaca_timestamp=None,
        headline="AAPL drops 5%",
        summary="iPhone sales decline",
        source="test",
        symbols=["AAPL"],
    )
    builder = SnapshotBuilder(trigger=trigger, snapshot_id="test-snap-001")
    for trace in result.all_tool_traces:
        builder.add_tool_trace(trace)
    for rnd in result.rounds:
        builder.add_round(rnd)
    builder.prediction = result.signal.model_dump()

    snapshot = builder.seal()

    # Rounds are stored
    assert len(snapshot.rounds) == 2
    for rnd in snapshot.rounds:
        assert "agent" in rnd
        assert "findings" in rnd

    # data_modalities index is built from traces
    assert isinstance(snapshot.data_modalities, dict)
    total_indexed = sum(len(v) for v in snapshot.data_modalities.values())
    assert total_indexed == len(snapshot.tool_traces)
    print(f"\n[Snapshot] Rounds: {len(snapshot.rounds)}")
    print(f"[Snapshot] Modalities: {dict(snapshot.data_modalities)}")
    print(f"[Snapshot] Traces: {len(snapshot.tool_traces)}")
