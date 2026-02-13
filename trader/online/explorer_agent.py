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

from trader.evidence.extract import extract_article
from trader.evidence.fetch import fetch_url
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
    "get_movers": "market_data",
    "get_technical_indicators": "market_data",
    "check_insider_activity": "fundamentals",
    "get_company_news": "news",
    "get_finnhub_news": "news",
    "url_fetch": "web_research",
    "web_search": "web_research",
    "x_search": "social",
    "x_stream_cache": "social",
}

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
                "modality": TOOL_MODALITY.get(name, "other"),
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

        # Append budget summary so the agent sees its usage naturally
        if isinstance(result, str) and ctx.deps.tool_calls_limit > 0:
            budget = _budget_summary(ctx.deps)
            if budget:
                result = result + "\n\n" + budget

        return result


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
def get_finnhub_news(ctx: RunContext[ExplorerDeps], symbol: str, days_back: int = 3) -> str:
    """Get recent company news from FinnHub. Free (60 req/min limit).
    Returns headlines, summaries, sources, and URLs for a ticker.
    Good for checking what's been reported recently about any company.

    Args:
        symbol: Stock ticker (e.g. 'AAPL', 'NVDA')
        days_back: How many days of history (default 3, max 7)
    """
    from trader.market.finnhub_client import get_company_news as _fh_news

    days_back = min(days_back, 7)
    articles = _fh_news(symbol, days_back=days_back)
    if not articles:
        return json.dumps({"symbol": symbol, "articles": [], "note": "No articles found or FINNHUB_API_KEY not set"})
    # Return top 15 articles with key fields only
    trimmed = []
    for a in articles[:15]:
        trimmed.append({
            "headline": a.get("headline", ""),
            "summary": (a.get("summary") or "")[:300],
            "source": a.get("source", ""),
            "datetime": a.get("datetime", 0),
            "url": a.get("url", ""),
            "related": a.get("related", ""),
        })
    return json.dumps({"symbol": symbol, "count": len(articles), "articles": trimmed}, default=str)


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
    try:
        fetch_result = fetch_url(url=url, user_agent="Mozilla/5.0 (compatible; alpaca-news/0.1)")
        content_type = fetch_result.content_type or ""
        if "html" not in content_type and "text" not in content_type:
            return json.dumps({
                "url": url,
                "error": f"Non-text content type: {content_type}",
                "status_code": fetch_result.status_code,
            })
        article = extract_article(html=fetch_result.content, url=url)
        # Truncate to avoid blowing up context
        text = article.text[:5000]
        return json.dumps({
            "url": url,
            "final_url": fetch_result.final_url,
            "text": text,
            "title": article.metadata.get("title", ""),
            "author": article.metadata.get("author", ""),
            "date": article.metadata.get("date", ""),
            "truncated": len(article.text) > 5000,
        })
    except Exception as e:
        return json.dumps({"url": url, "error": str(e)})


@market_toolset.tool
def x_search(ctx: RunContext[ExplorerDeps], query: str) -> str:
    """Search X/Twitter for posts related to a query. Uses Grok (xAI) under the hood.
    Costs per call (xAI API). Use this to check social media sentiment and chatter.

    Good queries include cashtags ($NVDA), company names, and specific news terms.
    You can call this multiple times with different queries to refine your search.

    Args:
        query: Search query (e.g. '$NVDA earnings sentiment', 'NVIDIA supply shortage')
    """
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
    svc = ctx.deps.x_stream_service
    if svc is None:
        return json.dumps({
            "symbol": symbol,
            "posts": [],
            "note": "X stream service not available",
        })

    try:
        posts = svc.get_recent_posts(key=symbol.upper(), limit=limit)
        # Also check the "_all" bucket
        if not posts:
            posts = svc.get_recent_posts(key="_all", limit=limit)
        return json.dumps({
            "symbol": symbol,
            "posts": posts,
            "count": len(posts),
            "source": "x_stream_cache",
        })
    except Exception as e:
        return json.dumps({"symbol": symbol, "error": str(e), "source": "x_stream_cache"})


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


EXPLORER_SYSTEM_PROMPT = """You are a financial research analyst investigating a breaking news event.

## Your tools
You have access to:
- **Market data** (free): real-time quotes, fundamentals, insider activity,
  technicals, options, price history, volume analysis
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
        tool_calls_limit=tool_calls_limit,
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
    # Include full article content if available (stripped of HTML tags)
    content = news.get("content")
    if content and isinstance(content, str):
        text = _strip_html(content).strip()
        if text and text != news.get("summary", ""):
            parts.append(f"\n## Full article content\n{text}")
    # Auto-fetch recent FinnHub news for primary symbols
    finnhub_section = _fetch_finnhub_context(symbols)
    if finnhub_section:
        parts.append(finnhub_section)
    return "\n".join(parts)


def _strip_html(html: str) -> str:
    """Strip HTML tags, returning plain text. Uses stdlib only."""
    import re
    from html.parser import HTMLParser

    _BLOCK_TAGS = frozenset({
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "br", "tr", "blockquote", "figure", "figcaption",
    })
    pieces: list[str] = []

    class _TagStripper(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag in _BLOCK_TAGS:
                pieces.append("\n")

        def handle_data(self, data: str) -> None:
            pieces.append(data)

    _TagStripper().feed(html)
    # Collapse excessive whitespace while preserving paragraph breaks
    text = "".join(pieces)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fetch_finnhub_context(symbols: list[str]) -> str:
    """Fetch recent FinnHub news for primary symbols and format for the prompt.

    Returns a markdown section string, or empty string if unavailable.
    """
    if not symbols or not os.getenv("FINNHUB_API_KEY"):
        return ""
    try:
        from trader.market.finnhub_client import (
            get_company_news as _fh_news,
            format_news_for_prompt,
        )
    except ImportError:
        return ""

    sections: list[str] = []
    for sym in symbols[:3]:  # limit to first 3 symbols
        articles = _fh_news(sym, days_back=3)
        if articles:
            formatted = format_news_for_prompt(articles, max_articles=10)
            sections.append(f"### {sym}\n{formatted}")

    if not sections:
        return ""
    return "\n## Recent news coverage (FinnHub)\n" + "\n\n".join(sections)


def _budget_summary(deps: ExplorerDeps) -> str:
    """Build a one-line budget summary from accumulated tool traces.

    Returns empty string if budget tracking is disabled (tool_calls_limit == 0).
    """
    if deps.tool_calls_limit <= 0:
        return ""

    total = len(deps.tool_traces)
    limit = deps.tool_calls_limit

    # Count expensive tool uses
    expensive: dict[str, int] = {}
    for trace in deps.tool_traces:
        tool_name = trace.get("action", {}).get("tool", "")
        if tool_name in ("x_search", "web_search"):
            expensive[tool_name] = expensive.get(tool_name, 0) + 1

    parts = [f"{total}/{limit} tool calls"]
    for tool_name in ("x_search", "web_search"):
        if tool_name in expensive:
            parts.append(f"{tool_name}: {expensive[tool_name]} used")

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
