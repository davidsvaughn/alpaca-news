"""PydanticAI-based explorer agent for free-form tool-use investigation.

Replaces the rigid Phase 1/2 explorer with a single PydanticAI agent that:
- Receives a news event + context
- Decides which tools to call and in what order
- Records every tool call as a ToolTrace (via TracingToolset)
- Stops when it has enough evidence
- Produces a structured TradingSignal as output

The agent loop, message threading, tool dispatch, and budget enforcement
are all handled by PydanticAI.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, FunctionToolset, RunContext, UsageLimits, WrapperToolset
from pydantic_ai.toolsets import ToolsetTool

from trader.market.data_service import MarketDataService

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Structured output — the agent must produce this when it decides to stop
# ---------------------------------------------------------------------------


class TradingSignal(BaseModel):
    """Structured trading signal produced by the explorer agent."""
    direction: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0, description="0.0 = no confidence, 1.0 = certain")
    horizon: Literal["15m", "60m", "1d"]
    magnitude_estimate: str = Field(description="Expected price move, e.g. '0.5-1.5%'")
    key_catalyst: str = Field(description="One-sentence summary of the main catalyst")
    bull_case: str = Field(description="Brief bull case argument")
    bear_case: str = Field(description="Brief bear case argument")
    risk_factors: list[str] = Field(description="Key risk factors that could invalidate the thesis")


# ---------------------------------------------------------------------------
# Dependencies — passed to every tool via RunContext
# ---------------------------------------------------------------------------


@dataclass
class ExplorerDeps:
    """Dependencies injected into the agent via RunContext."""
    market: MarketDataService
    news: dict[str, Any]
    symbols: list[str]
    # Mutable trace accumulator
    tool_traces: list[dict[str, Any]] = field(default_factory=list)
    hop_index: int = 0


# ---------------------------------------------------------------------------
# Tool tracing — intercepts every tool call for ToolTrace recording
# ---------------------------------------------------------------------------


class TracingToolset(WrapperToolset):
    """Wraps a toolset to record every tool call as a ToolTrace dict."""

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[ExplorerDeps],
        tool: ToolsetTool,
    ) -> Any:
        start = time.time()
        error = None
        result = None

        try:
            result = await super().call_tool(name, tool_args, ctx, tool)
            return result
        except Exception as e:
            error = str(e)
            raise
        finally:
            end = time.time()
            hop = ctx.deps.hop_index
            ctx.deps.hop_index += 1

            trace: dict[str, Any] = {
                "trace_id": f"trace_{hop}",
                "hop_index": hop,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "action": {
                    "tool": name,
                    "args": tool_args,
                },
                "execution": {
                    "start_time": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                    "end_time": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
                    "duration_s": round(end - start, 3),
                    "cost_usd": 0.0,  # financial data tools are free
                },
                "raw_tool_output": _safe_serialize(result) if error is None else None,
                "error": error,
            }
            ctx.deps.tool_traces.append(trace)


# ---------------------------------------------------------------------------
# Market data tools — wrapping MarketDataService
# ---------------------------------------------------------------------------


market_toolset = FunctionToolset()


@market_toolset.tool
def check_price(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get real-time quote for a stock symbol. Returns last price, bid/ask, volume, net change.
    Free (Schwab). Use this to check current price action."""
    result = ctx.deps.market.get_quote(symbol)
    return json.dumps(result, default=str)


@market_toolset.tool
def check_market_context(ctx: RunContext[ExplorerDeps]) -> str:
    """Get broad market context: SPY price/change, VIX level, market session (open/premarket/afterhours).
    Free (Schwab). Use this to understand the overall market environment."""
    result = ctx.deps.market.build_market_context()
    return json.dumps(result, default=str)


@market_toolset.tool
def check_options_activity(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get options market activity: ATM implied volatility, put/call volume ratio, put/call OI ratio.
    Free (Schwab). The options market often signals moves before the stock price reacts."""
    result = ctx.deps.market.check_options_activity(symbol)
    return json.dumps(result, default=str)


@market_toolset.tool
def get_fundamentals(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get company fundamentals: P/E, EPS, market cap, beta, 52-week range, dividend yield.
    Free (Schwab primary, yfinance fallback). Use for valuation context."""
    result = ctx.deps.market.get_fundamentals(symbol)
    return json.dumps(result, default=str)


@market_toolset.tool
def get_movers(ctx: RunContext[ExplorerDeps], index: str = "$SPX", direction: str = "up") -> str:
    """Get top market movers (gainers or losers) for an index. Must be called during market hours.
    Free (Schwab). Use to check if sector-wide moves are happening.

    Args:
        index: '$SPX', '$DJI', '$COMPX', 'NYSE', or 'NASDAQ'
        direction: 'up' for gainers, 'down' for losers
    """
    result = ctx.deps.market.get_movers(index, direction=direction)
    return json.dumps(result, default=str)


@market_toolset.tool
def check_insider_activity(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get recent insider transactions: buys, sells, grants. High-signal confirmation tool.
    Free (yfinance). Insider buying is one of the strongest bullish signals."""
    result = ctx.deps.market.check_insider_activity(symbol)
    return json.dumps(result, default=str)


@market_toolset.tool
def get_company_news(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get recent news articles for a company. Free (yfinance).
    Use to check if this news is already widely reported or if it's truly breaking."""
    result = ctx.deps.market.get_company_news(symbol, max_articles=5)
    return json.dumps(result, default=str)


@market_toolset.tool
def get_price_history(ctx: RunContext[ExplorerDeps], symbol: str, period: str = "5d", interval: str = "1d") -> str:
    """Get historical OHLCV price data. Free (Schwab for intraday, yfinance for daily+).

    Args:
        symbol: Stock ticker
        period: '1d', '5d', '1mo', '3mo', '6mo', '1y'
        interval: '1m', '5m', '15m', '1h', '1d'
    """
    result = ctx.deps.market.get_price_history(symbol, period=period, interval=interval)
    return json.dumps(result, default=str)


@market_toolset.tool
def get_technical_indicators(ctx: RunContext[ExplorerDeps], symbol: str, indicators: str = "rsi,macd,boll") -> str:
    """Get current technical indicator values and signals. Free (computed locally from yfinance data).

    Available indicators: rsi, macd, macds, macdh, boll, boll_ub, boll_lb,
    close_50_sma, close_200_sma, close_10_ema, atr, vwma, mfi

    Args:
        symbol: Stock ticker
        indicators: Comma-separated list of indicator names
    """
    indicator_list = [i.strip() for i in indicators.split(",")]
    result = ctx.deps.market.get_current_technicals(symbol, indicator_list)
    return json.dumps(result, default=str)


@market_toolset.tool
def check_price_spike(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Check if there's been a significant recent price move (>0.5% in last 5 min).
    Free (Schwab intraday candles). Use to detect if the market has already reacted."""
    result = ctx.deps.market.check_price_spike(symbol)
    return json.dumps(result, default=str)


@market_toolset.tool
def check_volume_regime(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Check if recent trading volume is abnormally high compared to session average.
    Free (Schwab intraday candles). Volume spikes often confirm real price moves."""
    result = ctx.deps.market.check_volume_regime(symbol)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


EXPLORER_SYSTEM_PROMPT = """You are a financial research assistant investigating a breaking news event.

## Your tools
You have access to real-time market data, company fundamentals, insider activity,
technical indicators, and price history. All financial data tools are FREE — use
them liberally. The more data you gather, the better your analysis will be.

## Budget
Financial data tools are free and unlimited. Use as many as needed.

## Your task
Investigate the news event provided to determine:
1. Is this a real, tradeable signal or noise/recycled content?
2. What is the likely short-term price impact (direction, magnitude, timing)?
3. What is your confidence level?

## How to investigate
Think step by step. A good investigation typically includes:
- Check the current price and recent price action (has the market already reacted?)
- Look at volume (are people actually trading on this?)
- Check fundamentals (is this stock expensive/cheap? what's the context?)
- Check insider activity (are insiders buying or selling?)
- Look at options activity (what's the options market pricing in?)
- Check technical indicators (is the stock overbought/oversold?)
- Look at recent news (is this truly new information?)

You don't need to use ALL tools every time. Focus on what's relevant to THIS
specific news event. Stop when you have enough evidence to make a judgment.

## Before your final assessment
Consider both sides:
- **Bull case:** What evidence supports a price move? What could go right?
- **Bear case:** What could go wrong? Is this already priced in? What are the risks?

Then weigh these against each other to reach your conclusion.
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_explorer_agent(model: str = "google-gla:gemini-2.5-flash") -> Agent[ExplorerDeps, TradingSignal]:
    """Create the explorer agent.

    Args:
        model: PydanticAI model string, e.g.:
            - 'google-gla:gemini-2.5-flash'
            - 'anthropic:claude-sonnet-4-5'
            - 'openai:gpt-5-mini'
    """
    tracing_toolset = TracingToolset(market_toolset)

    return Agent(
        model,
        deps_type=ExplorerDeps,
        output_type=TradingSignal,
        system_prompt=EXPLORER_SYSTEM_PROMPT,
        toolsets=[tracing_toolset],
    )


async def explore(
    *,
    news: dict[str, Any],
    symbols: list[str],
    model: str = "google-gla:gemini-2.5-flash",
    market: MarketDataService | None = None,
    request_limit: int = 20,
    tool_calls_limit: int = 30,
    total_tokens_limit: int = 100_000,
) -> ExploreResult:
    """Run the explorer agent on a news event.

    Args:
        news: News event dict with 'headline', 'summary', 'source', etc.
        symbols: Ticker symbols to investigate.
        model: PydanticAI model string.
        market: MarketDataService instance. Created if not provided.
        request_limit: Max LLM round-trips.
        tool_calls_limit: Max tool executions.
        total_tokens_limit: Max total tokens (input + output).

    Returns:
        ExploreResult with signal, traces, and usage info.
    """
    if market is None:
        market = MarketDataService()

    deps = ExplorerDeps(
        market=market,
        news=news,
        symbols=symbols,
    )

    agent = build_explorer_agent(model=model)

    # Build the user message from the news event
    user_message = _build_user_message(news, symbols)

    result = await agent.run(
        user_message,
        deps=deps,
        usage_limits=UsageLimits(
            request_limit=request_limit,
            tool_calls_limit=tool_calls_limit,
            total_tokens_limit=total_tokens_limit,
        ),
    )

    usage = result.usage()

    return ExploreResult(
        signal=result.output,
        tool_traces=deps.tool_traces,
        usage={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "requests": usage.requests,
            "tool_calls": usage.tool_calls,
        },
    )


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExploreResult:
    """Result of an explorer agent run."""
    signal: TradingSignal
    tool_traces: list[dict[str, Any]]
    usage: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_user_message(news: dict[str, Any], symbols: list[str]) -> str:
    """Build the user message from a news event."""
    parts = ["## The news event"]
    if news.get("headline"):
        parts.append(f"**Headline:** {news['headline']}")
    if news.get("summary"):
        parts.append(f"**Summary:** {news['summary']}")
    if symbols:
        parts.append(f"**Symbols:** {', '.join(symbols)}")
    if news.get("source"):
        parts.append(f"**Source:** {news['source']}")
    if news.get("created_at"):
        parts.append(f"**Timestamp:** {news['created_at']}")
    if news.get("url"):
        parts.append(f"**URL:** {news['url']}")
    return "\n".join(parts)


def _safe_serialize(obj: Any) -> Any:
    """Attempt to serialize an object for ToolTrace storage."""
    if obj is None:
        return None
    if isinstance(obj, str):
        # Try to parse as JSON first for cleaner storage
        try:
            return json.loads(obj)
        except (json.JSONDecodeError, TypeError):
            return obj
    if isinstance(obj, (dict, list, int, float, bool)):
        return obj
    return str(obj)
