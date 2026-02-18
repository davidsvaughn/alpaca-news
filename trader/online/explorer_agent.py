"""PydanticAI-based explorer agent with market data + research tools.

Provides:
- TradingSignal structured output model
- ExplorerDeps dependency container
- TracingToolset for ToolTrace recording
- Function toolset with market data tools + research tools
  (x_search, url_fetch, x_stream_cache)
- Agent factory and explore() entry point

Used by the multi-agent orchestrator (orchestrator.py) which runs
multiple agents sequentially with different LLM providers.
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


class CheckinDecision(BaseModel):
    """Structured output from a monitoring check-in agent."""
    action: Literal["hold", "exit"]
    reason: str = Field(description="Brief explanation of the hold/exit decision")
    unrealized_pnl_pct: float = Field(description="Current unrealized P&L as a percentage")


# ---------------------------------------------------------------------------
# Dependencies — passed to every tool via RunContext
# ---------------------------------------------------------------------------


@dataclass
class ExplorerDeps:
    """Dependencies injected into the agent via RunContext."""
    market: MarketDataService
    news: dict[str, Any]
    symbols: list[str]
    # Optional services (injected by orchestrator when available)
    x_stream_service: Any = None       # XStreamService instance for cached X posts
    xai_api_key: str | None = None     # For x_search function tool
    # Mutable trace accumulator
    tool_traces: list[dict[str, Any]] = field(default_factory=list)
    hop_index: int = 0
    # Budget visibility (set by pipeline; 0 = don't show budget line)
    tool_calls_limit: int = 0
    request_limit: int = 0
    web_search_limit: int = 0          # 0 = unlimited; >0 = prompt-enforced cap
    x_search_limit: int = 0            # 0 = unlimited; >0 = hard-enforced cap


# ---------------------------------------------------------------------------
# Modality classification — maps tool names to data categories
# ---------------------------------------------------------------------------

TOOL_MODALITY: dict[str, str] = {
    "check_price": "market_data",
    "get_price_history": "market_data",
    "check_price_spike": "market_data",
    "check_volume_regime": "market_data",
    "check_market_context": "macro",
    "check_options_activity": "market_data",
    "get_fundamentals": "fundamentals",
    "get_financial_statements": "fundamentals",
    "get_movers": "market_data",
    "get_technical_indicators": "market_data",
    "check_insider_activity": "fundamentals",
    "get_company_news": "news",
    "get_finnhub_news": "news",
    "get_analyst_ratings": "fundamentals",
    "url_fetch": "web_research",
    "web_search": "web_research",
    "x_search": "social",
    "x_stream_cache": "social",
}

# ---------------------------------------------------------------------------
# Tool tracing — intercepts every tool call for ToolTrace recording
# ---------------------------------------------------------------------------

# x_search and web_search (function tools) make separate API calls to xAI.
# The cost includes a per-call invocation fee PLUS token costs for the inner
# Grok inference.  We extract usage from the result to compute the true cost.
_XAI_PER_CALL_FEE = 0.005  # USD per x_search or web_search invocation
_XAI_INPUT_RATE = 0.20     # USD per 1M input tokens (grok-4-1-fast-reasoning)
_XAI_OUTPUT_RATE = 0.50    # USD per 1M output tokens


def _estimate_tool_call_cost(tool_name: str, result: Any, error: str | None) -> float:
    """Estimate cost of a tool call, including inner API token costs.

    For x_search (and future web_search function tools), the result JSON
    contains a ``usage`` dict with ``input_tokens`` and ``output_tokens``
    from the inner xAI API call.  We add those to the per-call fee.
    """
    if tool_name != "x_search" or error is not None or result is None:
        return 0.0

    cost = _XAI_PER_CALL_FEE
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        if usage:
            inp = usage.get("input_tokens", 0) or 0
            out = usage.get("output_tokens", 0) or 0
            cost += inp * _XAI_INPUT_RATE / 1_000_000
            cost += out * _XAI_OUTPUT_RATE / 1_000_000
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return round(cost, 6)


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
        except Exception as e:
            error = str(e)
            raise
        finally:
            end = time.time()
            hop = ctx.deps.hop_index
            ctx.deps.hop_index += 1

            cost_usd = _estimate_tool_call_cost(name, result, error)

            trace: dict[str, Any] = {
                "trace_id": f"trace_{hop}",
                "hop_index": hop,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "modality": TOOL_MODALITY.get(name, "other"),
                "action": {
                    "tool": name,
                    "args": tool_args,
                },
                "execution": {
                    "start_time": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                    "end_time": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
                    "duration_s": round(end - start, 3),
                    "cost_usd": cost_usd,
                },
                "raw_tool_output": _safe_serialize(result) if error is None else None,
                "error": error,
            }
            ctx.deps.tool_traces.append(trace)

        # Append budget summary so the agent sees its usage naturally
        if isinstance(result, str) and ((ctx.deps.request_limit or 0) > 0 or (ctx.deps.tool_calls_limit or 0) > 0):
            budget = _budget_summary(ctx)
            if budget:
                result = result + "\n\n" + budget

        return result


# ---------------------------------------------------------------------------
# Market data tools — wrapping MarketDataService
# ---------------------------------------------------------------------------


market_toolset = FunctionToolset()


@market_toolset.tool
def check_price(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get real-time quote with trend context: current price, volume, net change,
    period returns (1W/1M/3M/6M/1Y), and 52-week range position.
    Free (Schwab + yfinance). Use this to check current price action and trend."""
    from trader.online.tool_core import check_price as _impl
    return _impl(ctx.deps.market, symbol)


@market_toolset.tool
def check_market_context(ctx: RunContext[ExplorerDeps]) -> str:
    """Get broad market context: SPY price/change, VIX level, market session (open/premarket/afterhours).
    Free (Schwab). Use this to understand the overall market environment."""
    from trader.online.tool_core import check_market_context as _impl
    return _impl(ctx.deps.market)


@market_toolset.tool
def check_options_activity(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get options market activity: ATM implied volatility, put/call volume ratio, put/call OI ratio.
    Free (Schwab). The options market often signals moves before the stock price reacts."""
    from trader.online.tool_core import check_options_activity as _impl
    return _impl(ctx.deps.market, symbol)


@market_toolset.tool
def get_fundamentals(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get company fundamentals: P/E, EPS, market cap, beta, 52-week range, dividend yield.
    Free (Schwab primary, yfinance fallback). Use for valuation context."""
    from trader.online.tool_core import get_fundamentals as _impl
    return _impl(ctx.deps.market, symbol)


@market_toolset.tool
def get_movers(ctx: RunContext[ExplorerDeps], index: str = "$SPX", direction: str = "up") -> str:
    """Get top market movers (gainers or losers) for an index. Must be called during market hours.
    Free (Schwab). Use to check if sector-wide moves are happening.

    Args:
        index: '$SPX', '$DJI', '$COMPX', 'NYSE', or 'NASDAQ'
        direction: 'up' for gainers, 'down' for losers
    """
    from trader.online.tool_core import get_movers as _impl
    return _impl(ctx.deps.market, index, direction)


@market_toolset.tool
def check_insider_activity(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get recent insider transactions: buys, sells, grants. High-signal confirmation tool.
    Free (yfinance). Insider buying is one of the strongest bullish signals."""
    from trader.online.tool_core import check_insider_activity as _impl
    return _impl(ctx.deps.market, symbol)


@market_toolset.tool
def get_company_news(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get recent news articles for a company. Free (yfinance).
    Use to check if this news is already widely reported or if it's truly breaking."""
    from trader.online.tool_core import get_company_news as _impl
    return _impl(ctx.deps.market, symbol)


@market_toolset.tool
def get_finnhub_news(ctx: RunContext[ExplorerDeps], symbol: str, days_back: int = 3) -> str:
    """Get recent company news from FinnHub. Free (60 req/min limit).
    Returns headlines, summaries, sources, and URLs for a ticker.
    Good for checking what's been reported recently about any company.

    Args:
        symbol: Stock ticker (e.g. 'AAPL', 'NVDA')
        days_back: How many days of history (default 3, max 7)
    """
    from trader.online.tool_core import get_finnhub_news as _impl
    return _impl(ctx.deps.market, symbol, days_back)


@market_toolset.tool
def get_analyst_ratings(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get analyst recommendation trends for a stock. Free (FinnHub, 60 req/min).
    Shows monthly buy/hold/sell distribution and how consensus is shifting.
    Use to understand where Wall Street stands and whether sentiment is changing.

    Args:
        symbol: Stock ticker (e.g. 'AAPL', 'NVDA')
    """
    from trader.online.tool_core import get_analyst_ratings as _impl
    return _impl(ctx.deps.market, symbol)


@market_toolset.tool
def get_price_history(ctx: RunContext[ExplorerDeps], symbol: str, period: str = "5d", interval: str = "1d") -> str:
    """Get historical OHLCV price data. Free (Schwab for intraday, yfinance for daily+).

    Args:
        symbol: Stock ticker
        period: '1d', '5d', '1mo', '3mo', '6mo', '1y'
        interval: '1m', '5m', '15m', '1h', '1d'
    """
    from trader.online.tool_core import get_price_history as _impl
    return _impl(ctx.deps.market, symbol, period, interval)


@market_toolset.tool
def get_technical_indicators(ctx: RunContext[ExplorerDeps], symbol: str, indicators: str = "rsi,macd,boll") -> str:
    """Get current technical indicator values with interpretation. Free (computed locally).

    Available indicators: rsi, macd, macds, macdh, boll, boll_ub, boll_lb,
    close_50_sma, close_200_sma, close_10_ema, atr, vwma, mfi

    Args:
        symbol: Stock ticker
        indicators: Comma-separated list of indicator names
    """
    from trader.online.tool_core import get_technical_indicators as _impl
    return _impl(ctx.deps.market, symbol, indicators)


@market_toolset.tool
def check_price_spike(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Check if there's been a significant recent price move (>0.5% in last 5 min).
    Free (Schwab intraday candles). Use to detect if the market has already reacted."""
    from trader.online.tool_core import check_price_spike as _impl
    return _impl(ctx.deps.market, symbol)


@market_toolset.tool
def check_volume_regime(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Check if recent trading volume is abnormally high compared to session average.
    Free (Schwab intraday candles). Volume spikes often confirm real price moves."""
    from trader.online.tool_core import check_volume_regime as _impl
    return _impl(ctx.deps.market, symbol)


@market_toolset.tool
def get_financial_statements(
    ctx: RunContext[ExplorerDeps],
    symbol: str,
    statement: str = "income",
    freq: str = "quarterly",
) -> str:
    """Get financial statement data for deep fundamental analysis.
    Returns key line items for the last 4 periods. Free (yfinance).
    Use this to go beyond summary ratios (P/E, EPS) and examine trends:
    - income: Revenue, gross/operating/net profit, EBITDA, EPS
    - balance_sheet: Assets, liabilities, equity, cash, debt, working capital
    - cash_flow: Operating cash flow, capex, free cash flow, buybacks, dividends

    Args:
        symbol: Stock ticker (e.g. 'AAPL')
        statement: 'income', 'balance_sheet', or 'cash_flow'
        freq: 'quarterly' or 'yearly'
    """
    from trader.online.tool_core import get_financial_statements as _impl
    return _impl(ctx.deps.market, symbol, statement, freq)


# ---------------------------------------------------------------------------
# Research tools — web/social/evidence
# ---------------------------------------------------------------------------


@market_toolset.tool
def url_fetch(ctx: RunContext[ExplorerDeps], url: str) -> str:
    """Fetch a web page and extract its article text. Free (local).
    Use this to read the full content of a URL found via web_search or news.
    Returns extracted article text (via trafilatura), NOT raw HTML.

    Args:
        url: The full URL to fetch and extract text from.
    """
    from trader.online.tool_core import url_fetch as _impl
    return _impl(url)


@market_toolset.tool
def x_search(ctx: RunContext[ExplorerDeps], query: str) -> str:
    """Search X/Twitter for posts related to a query. Uses Grok (xAI) under the hood.
    Costs per call (xAI API). Use this to check social media sentiment and chatter.

    Good queries include cashtags ($NVDA), company names, and specific news terms.
    You can call this multiple times with different queries to refine your search.

    Args:
        query: Search query (e.g. '$NVDA earnings sentiment', 'NVIDIA supply shortage')
    """
    # Enforce x_search limit
    limit = ctx.deps.x_search_limit
    if limit > 0:
        prior = sum(
            1 for t in ctx.deps.tool_traces
            if isinstance(t.get("action"), dict) and t["action"].get("tool") == "x_search"
        )
        if prior >= limit:
            return json.dumps({"error": f"x_search limit reached ({limit}). Use other tools instead."})

    api_key = ctx.deps.xai_api_key or os.getenv("XAI_API_KEY")
    if not api_key:
        return json.dumps({"error": "XAI_API_KEY not configured — x_search unavailable"})

    try:
        import httpx as _httpx

        resp = _httpx.post(
            "https://api.x.ai/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning"),
                "tools": [{"type": "x_search"}],
                "input": [{"role": "user", "content": query}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract text output + citations from the response
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        annotations = content.get("annotations", [])
                        citations = [
                            a["url"]
                            for a in annotations
                            if a.get("type") == "url_citation" and a.get("url")
                        ]
                        result: dict[str, Any] = {
                            "query": query,
                            "answer": content["text"],
                            "citations": citations,
                            "source": "x_search",
                            # Richer data for training — stored via raw_tool_output
                            "annotations": annotations,
                        }
                        usage = data.get("usage")
                        if usage:
                            result["usage"] = usage
                        return json.dumps(result)
        return json.dumps({"query": query, "error": "No output in x_search response"})
    except Exception as e:
        return json.dumps({"query": query, "error": str(e), "source": "x_search"})


@market_toolset.tool
def x_stream_cache(ctx: RunContext[ExplorerDeps], symbol: str, limit: int = 20) -> str:
    """Get cached recent X/Twitter posts from the live filtered stream for a symbol.
    Free (reads from in-memory cache, no API call). The stream must be running
    for there to be cached posts.

    Args:
        symbol: Stock ticker to look up in the cache (e.g. 'NVDA')
        limit: Maximum number of posts to return (default 20)
    """
    from trader.online.tool_core import x_stream_cache as _impl
    return _impl(ctx.deps.x_stream_service, symbol, limit)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


EXPLORER_SYSTEM_PROMPT = """You are a financial research analyst investigating a breaking news event.

## Your tools
You have access to:
- **Market data** (free): real-time quotes, fundamentals, financial statements
  (income, balance sheet, cash flow), insider activity, technicals, options,
  price history, volume analysis
- **Web search** (native, iterative): search the web for related information,
  verify claims, find additional context
- **X/Twitter search** (x_search): check social media sentiment and chatter
- **URL fetch** (free): read the full text of any article or webpage
- **X stream cache** (free): get cached posts from the live X filtered stream

## Cost awareness
- Financial data tools, url_fetch, get_finnhub_news, and x_stream_cache are **free** — use liberally.
- web_search and x_search cost per call — use purposefully, not wastefully.

## Your task
Investigate the news event provided to determine:
1. Is this a real, tradeable signal or noise/recycled content?
2. What is the likely short-term price impact (direction, magnitude, timing)?
3. What is your confidence level?

## How to investigate
Think step by step. A good investigation typically includes:
- Check the current price and recent price action (has the market already reacted?)
- Search the web for more context about the news event
- Check X/Twitter for trader sentiment and chatter
- Look at volume (are people actually trading on this?)
- Check fundamentals (is this stock expensive/cheap? what's the context?)
- Check financial statements if deeper analysis is needed (revenue trends, debt levels, cash flow health)
- Check insider activity (are insiders buying or selling?)
- Look at options activity (what's the options market pricing in?)
- Check technical indicators (is the stock overbought/oversold?)
- Fetch and read key articles for detailed information

Focus on what's relevant to THIS specific news event. Stop when you have
enough evidence to make a judgment.

## Before your final assessment
Consider both sides:
- **Bull case:** What evidence supports a price move? What could go right?
- **Bear case:** What could go wrong? Is this already priced in? What are the risks?

Then weigh these against each other to reach your conclusion.
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_explorer_agent(model: str = "google-gla:gemini-3-flash-preview") -> Agent[ExplorerDeps, TradingSignal]:
    """Create the explorer agent.

    Args:
        model: PydanticAI model string, e.g.:
            - 'google-gla:gemini-3-flash-preview'
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
    model: str = "google-gla:gemini-3-flash-preview",
    market: MarketDataService | None = None,
    request_limit: int = 20,
    tool_calls_limit: int = 30,
) -> ExploreResult:
    """Run the explorer agent on a news event.

    Args:
        news: News event dict with 'headline', 'summary', 'source', etc.
        symbols: Ticker symbols to investigate.
        model: PydanticAI model string.
        market: MarketDataService instance. Created if not provided.
        request_limit: Max LLM round-trips (safety net).
        tool_calls_limit: Max tool executions (safety net).

    Returns:
        ExploreResult with signal, traces, and usage info.
    """
    if market is None:
        market = MarketDataService()

    deps = ExplorerDeps(
        market=market,
        news=news,
        symbols=symbols,
        tool_calls_limit=tool_calls_limit,
        request_limit=request_limit,
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
    signal: TradingSignal | None
    tool_traces: list[dict[str, Any]]
    usage: dict[str, Any]


# ---------------------------------------------------------------------------
# Prompt / message building (delegated to prompt_builder.py)
# ---------------------------------------------------------------------------

from trader.online.prompt_builder import build_user_message as _build_user_message  # noqa: E402


def _budget_summary(ctx: RunContext[ExplorerDeps]) -> str:
    """Build a one-line budget summary from ctx.usage (real provider data).

    Shows request count, tool call count, and expensive tool usage.
    Returns empty string if no limits are set.
    """
    deps = ctx.deps
    req_lim = deps.request_limit or 0
    tc_lim = deps.tool_calls_limit or 0
    if req_lim <= 0 and tc_lim <= 0:
        return ""

    usage = ctx.usage
    parts: list[str] = []

    # Request count
    if req_lim > 0:
        parts.append(f"{usage.requests or 0}/{req_lim} requests")

    # Tool calls count
    if tc_lim > 0:
        parts.append(f"{usage.tool_calls or 0}/{tc_lim} tool calls")

    # Expensive tool counts (from traces — ctx.usage doesn't break down by tool)
    expensive: dict[str, int] = {}
    for trace in deps.tool_traces:
        tool_name = trace.get("action", {}).get("tool", "")
        if tool_name in ("x_search", "web_search"):
            expensive[tool_name] = expensive.get(tool_name, 0) + 1
    ws_count = expensive.get("web_search", 0)
    ws_limit = deps.web_search_limit
    if ws_limit > 0:
        parts.append(f"web_search: {ws_count}/{ws_limit}")
    elif ws_count > 0:
        parts.append(f"web_search: {ws_count} used")
    xs_count = expensive.get("x_search", 0)
    xs_limit = deps.x_search_limit
    if xs_limit > 0:
        parts.append(f"x_search: {xs_count}/{xs_limit}")
    elif xs_count > 0:
        parts.append(f"x_search: {xs_count} used")

    return f"[Budget: {' | '.join(parts)}]"


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
