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
    request_limit: int = 0


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
    "get_analyst_ratings": "fundamentals",
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
def get_analyst_ratings(ctx: RunContext[ExplorerDeps], symbol: str) -> str:
    """Get analyst recommendation trends for a stock. Free (FinnHub, 60 req/min).
    Shows monthly buy/hold/sell distribution and how consensus is shifting.
    Use to understand where Wall Street stands and whether sentiment is changing.

    Args:
        symbol: Stock ticker (e.g. 'AAPL', 'NVDA')
    """
    from trader.market.finnhub_client import get_recommendation_trends

    trends = get_recommendation_trends(symbol)
    if not trends:
        return json.dumps({"symbol": symbol, "trends": [], "note": "No data or FINNHUB_API_KEY not set"})
    # Return last 6 months for trend visibility
    trimmed = []
    for t in trends[:6]:
        total = t.get("buy", 0) + t.get("hold", 0) + t.get("sell", 0) + t.get("strongBuy", 0) + t.get("strongSell", 0)
        trimmed.append({
            "period": t.get("period", ""),
            "strongBuy": t.get("strongBuy", 0),
            "buy": t.get("buy", 0),
            "hold": t.get("hold", 0),
            "sell": t.get("sell", 0),
            "strongSell": t.get("strongSell", 0),
            "total_analysts": total,
        })
    return json.dumps({"symbol": symbol, "trends": trimmed}, default=str)


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
# Helpers
# ---------------------------------------------------------------------------


def _build_user_message(
    news: dict[str, Any],
    symbols: list[str],
    market: "MarketDataService | None" = None,
) -> str:
    """Build the user message from a news event.

    Args:
        news: News event dict.
        symbols: Ticker symbols.
        market: If provided, pre-fetch basic market data for primary symbols
            so agents don't waste tool calls on rote data gathering.
    """
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
    # Auto-fetch FinnHub context for primary symbols
    finnhub_section = _fetch_finnhub_context(symbols)
    if finnhub_section:
        parts.append(finnhub_section)
    earnings_section = _fetch_earnings_context(symbols)
    if earnings_section:
        parts.append(earnings_section)
    # Pre-fetch basic market data for primary symbols (Change 2)
    if market is not None:
        prefetch = _prefetch_market_data(symbols, market)
        if prefetch:
            parts.append(prefetch)
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


def _fetch_earnings_context(symbols: list[str]) -> str:
    """Fetch earnings data for primary symbols and format for the prompt.

    Returns a markdown section string, or empty string if unavailable.
    """
    if not symbols or not os.getenv("FINNHUB_API_KEY"):
        return ""
    try:
        from trader.market.finnhub_client import (
            get_earnings_surprises,
            get_earnings_calendar,
            format_earnings_for_prompt,
        )
    except ImportError:
        return ""

    sections: list[str] = []
    for sym in symbols[:3]:
        surprises = get_earnings_surprises(sym, limit=4)
        calendar = get_earnings_calendar(sym)
        formatted = format_earnings_for_prompt(surprises, calendar)
        if formatted:
            sections.append(f"### {sym}\n{formatted}")

    if not sections:
        return ""
    return "\n## Earnings context (FinnHub)\n" + "\n\n".join(sections)


def _period_returns(bars: list[dict[str, Any]], current_price: float) -> str:
    """Compute percentage returns at standard timeframes from daily OHLCV bars.

    Returns a compact string like: "1W: -2.3% | 1M: +8.1% | 3M: -15.4%"
    """
    if not bars or current_price <= 0:
        return ""
    # bars are chronological (oldest first); find closes at approximate offsets
    n = len(bars)
    periods = [("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126), ("1Y", 252)]
    parts = []
    for label, offset in periods:
        idx = n - offset
        if idx < 0:
            continue
        old_close = bars[idx].get("c", 0)
        if old_close and old_close > 0:
            ret = (current_price - old_close) / old_close * 100
            parts.append(f"{label}: {ret:+.1f}%")
    return " | ".join(parts)


def _prefetch_market_data(symbols: list[str], market: MarketDataService) -> str:
    """Pre-fetch market data for primary symbols, formatted as narrative markdown.

    Provides each agent with rich context upfront:
    - Price + multi-timeframe returns + 52-week position
    - Fundamentals with key ratios
    - Technical indicators with trend direction and relative interpretation
    - Options, volume, insider activity

    Only fetches for the first 3 symbols. All data sources are free (Schwab
    real-time quotes, yfinance history/fundamentals, local stockstats).
    """
    if not symbols:
        return ""

    sections: list[str] = []

    for sym in symbols[:3]:
        sym_sections: list[str] = []
        _fund_cache: dict[str, Any] | None = None  # reuse across sections

        # ----------------------------------------------------------
        # Price & Trend (quote + 1Y daily history → period returns)
        # ----------------------------------------------------------
        current_price: float = 0.0
        w52_high: float | None = None
        w52_low: float | None = None
        try:
            quote = market.get_quote(sym)
            if quote and not quote.get("error"):
                last = quote.get("last_price") or quote.get("lastPrice") or quote.get("regularMarketPrice")
                current_price = float(last) if last else 0.0
                change_pct = quote.get("net_pct_change") or quote.get("netPercentChange", "")
                vol = quote.get("total_volume") or quote.get("totalVolume", "")
                vol_str = f"{vol:,.0f}" if isinstance(vol, (int, float)) else str(vol)

                line1 = f"Current: ${current_price:.4g}"
                if change_pct and isinstance(change_pct, (int, float)):
                    line1 += f" ({change_pct:+.2f}% today)"
                if vol:
                    line1 += f" | Vol: {vol_str}"

                # Fetch 1Y daily bars for period returns
                returns_str = ""
                try:
                    hist = market.get_price_history(sym, period="1y", interval="1d")
                    bars = hist.get("bars", []) if isinstance(hist, dict) else []
                    if bars and current_price > 0:
                        returns_str = _period_returns(bars, current_price)
                except Exception:
                    pass

                # 52-week range (from fundamentals)
                range_str = ""
                try:
                    _fund_cache = market.get_fundamentals(sym)
                    if _fund_cache and not _fund_cache.get("error"):
                        w52_high = _fund_cache.get("week_52_high") or _fund_cache.get("52WeekHigh")
                        w52_low = _fund_cache.get("week_52_low") or _fund_cache.get("52WeekLow")
                        if w52_high and w52_low and isinstance(w52_high, (int, float)) and isinstance(w52_low, (int, float)):
                            rng = w52_high - w52_low
                            if rng > 0 and current_price > 0:
                                pct_of_range = (current_price - w52_low) / rng * 100
                                pos = "near lows" if pct_of_range < 15 else "near highs" if pct_of_range > 85 else "mid-range"
                                range_str = f"52-week: ${w52_low:.2f}–${w52_high:.2f} (at {pct_of_range:.0f}% — {pos})"
                except Exception:
                    pass

                lines = [line1]
                if returns_str:
                    lines.append(f"Returns: {returns_str}")
                if range_str:
                    lines.append(range_str)
                sym_sections.append(f"### {sym} — Price & Trend\n" + "\n".join(lines))
        except Exception:
            pass

        # ----------------------------------------------------------
        # Fundamentals (reuse _fund_cache if already fetched above)
        # ----------------------------------------------------------
        try:
            fund = _fund_cache if _fund_cache and not _fund_cache.get("error") else market.get_fundamentals(sym)
            if fund and not fund.get("error"):
                parts = []
                field_map = [
                    (["market_cap", "marketCap"], "Market Cap"),
                    (["pe_ratio", "peRatio"], "P/E"),
                    (["eps"], "EPS"),
                    (["beta"], "Beta"),
                    (["dividend_yield", "dividendYield"], "Div Yield"),
                    (["sector"], "Sector"),
                    (["industry"], "Industry"),
                ]
                for keys, label in field_map:
                    val = next((fund[k] for k in keys if fund.get(k) not in (None, "")), None)
                    if val is None or val == "":
                        continue
                    if label == "Div Yield" and val == 0.0:
                        continue
                    if label == "Market Cap" and isinstance(val, (int, float)):
                        if val > 1e9:
                            parts.append(f"{label}: ${val/1e9:.1f}B")
                        elif val > 1e6:
                            parts.append(f"{label}: ${val/1e6:.1f}M")
                        else:
                            parts.append(f"{label}: ${val:,.0f}")
                    elif label == "Div Yield" and isinstance(val, (int, float)):
                        parts.append(f"{label}: {val:.2f}%")
                    else:
                        parts.append(f"{label}: {val}")
                if parts:
                    sym_sections.append(f"### {sym} — Fundamentals\n{' | '.join(parts)}")
        except Exception:
            pass

        # ----------------------------------------------------------
        # Technical Summary (current snapshot + 10d trend + relative values)
        # ----------------------------------------------------------
        try:
            techs = market.get_current_technicals(
                sym, ["rsi", "macd", "macds", "boll", "boll_ub", "boll_lb", "atr",
                       "close_50_sma", "close_200_sma"],
            )
            if techs and not techs.get("error"):
                # Fetch 10-day indicator history for trend direction
                trend_data: dict[str, list[dict[str, Any]]] = {}
                try:
                    hist_ind = market.get_technical_indicators(
                        sym, ["rsi", "macd"], lookback_days=10,
                    )
                    if hist_ind and not hist_ind.get("error"):
                        trend_data = hist_ind.get("indicators", {})
                except Exception:
                    pass

                lines = []

                # RSI with trend
                if "rsi" in techs and isinstance(techs["rsi"], (int, float)):
                    rsi = techs["rsi"]
                    signal = "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral"
                    rsi_str = f"RSI(14): {rsi:.1f} ({signal})"
                    rsi_hist = trend_data.get("rsi", [])
                    if len(rsi_hist) >= 2:
                        old_rsi = rsi_hist[0].get("value")
                        if old_rsi is not None:
                            direction = "rising" if rsi > old_rsi + 3 else "declining" if rsi < old_rsi - 3 else "flat"
                            rsi_str += f" — {direction} from {old_rsi:.0f} over {len(rsi_hist)}d"
                    lines.append(rsi_str)

                # MACD with crossover info
                if "macd" in techs and "macds" in techs:
                    macd_val = techs["macd"]
                    signal_val = techs["macds"]
                    if isinstance(macd_val, (int, float)) and isinstance(signal_val, (int, float)):
                        side = "bullish" if macd_val > signal_val else "bearish"
                        macd_str = f"MACD: {macd_val:.4f} ({side}"
                        # Check for recent crossover from history
                        macd_hist = trend_data.get("macd", [])
                        if len(macd_hist) >= 2:
                            # Look backwards for sign change in (macd - signal)
                            # We only have macd history, not signal, so just note direction
                            old_macd = macd_hist[0].get("value")
                            if old_macd is not None:
                                if (macd_val > 0) != (old_macd > 0):
                                    macd_str += f", crossed zero in last {len(macd_hist)}d"
                        macd_str += ")"
                        lines.append(macd_str)

                # Bollinger with position percentage
                if "boll_ub" in techs and "boll_lb" in techs and "boll" in techs:
                    try:
                        ub = float(techs["boll_ub"])
                        lb = float(techs["boll_lb"])
                        mid = float(techs["boll"])
                        boll_range = ub - lb
                        if boll_range > 0 and current_price > 0:
                            pct = (current_price - lb) / boll_range * 100
                            pos = "near upper band" if pct > 80 else "near lower band" if pct < 20 else "mid-band"
                            lines.append(f"Bollinger: at {pct:.0f}% of band ({pos}) — lower: ${lb:.2f}, mid: ${mid:.2f}, upper: ${ub:.2f}")
                        else:
                            lines.append(f"Bollinger: ${lb:.2f} / ${mid:.2f} / ${ub:.2f}")
                    except (TypeError, ValueError):
                        pass

                # Price vs SMAs (relative)
                sma_parts = []
                for sma_key, sma_label in [("close_50_sma", "50-SMA"), ("close_200_sma", "200-SMA")]:
                    sma_val = techs.get(sma_key)
                    if sma_val and isinstance(sma_val, (int, float)) and current_price > 0 and sma_val > 0:
                        pct_diff = (current_price - sma_val) / sma_val * 100
                        side = "above" if pct_diff > 0 else "below"
                        sma_parts.append(f"vs {sma_label}: {pct_diff:+.1f}% ({side})")
                if sma_parts:
                    lines.append("Price " + " | ".join(sma_parts))

                # ATR as % of price (volatility context)
                if "atr" in techs and isinstance(techs["atr"], (int, float)) and current_price > 0:
                    atr = techs["atr"]
                    atr_pct = atr / current_price * 100
                    vol_label = "high" if atr_pct > 5 else "low" if atr_pct < 1 else "moderate"
                    lines.append(f"ATR: ${atr:.4f} ({atr_pct:.1f}% of price — {vol_label} volatility)")

                if lines:
                    sym_sections.append(f"### {sym} — Technical Summary\n" + "\n".join(lines))
        except Exception:
            pass

        # Options activity
        try:
            opts = market.check_options_activity(sym)
            if opts and not opts.get("error"):
                parts = []
                if "atm_iv" in opts:
                    parts.append(f"ATM IV: {opts['atm_iv']}")
                if "put_call_volume_ratio" in opts:
                    parts.append(f"Put/Call Vol: {opts['put_call_volume_ratio']}")
                if "put_call_oi_ratio" in opts:
                    parts.append(f"Put/Call OI: {opts['put_call_oi_ratio']}")
                if parts:
                    sym_sections.append(f"### {sym} — Options Activity\n{' | '.join(parts)}")
        except Exception:
            pass

        # Volume regime
        try:
            vol_data = market.check_volume_regime(sym)
            if vol_data and not vol_data.get("error"):
                regime = vol_data.get("regime", "unknown")
                ratio = vol_data.get("volume_ratio", "")
                line = f"Volume regime: {regime}"
                if ratio:
                    line += f" ({ratio}x avg)" if isinstance(ratio, (int, float)) else f" ({ratio})"
                sym_sections.append(f"### {sym} — Volume Regime\n{line}")
        except Exception:
            pass

        # Price spike
        try:
            spike = market.check_price_spike(sym)
            if spike and not spike.get("error"):
                has_spike = spike.get("spike_detected", False)
                if has_spike:
                    pct = spike.get("price_change_pct") or spike.get("change_pct", "?")
                    sym_sections.append(f"### {sym} — Price Spike\nSpike detected: {pct}% move")
                else:
                    sym_sections.append(f"### {sym} — Price Spike\nNo significant spike detected")
        except Exception:
            pass

        # Insider activity
        try:
            insider = market.check_insider_activity(sym)
            if insider and not insider.get("error"):
                txns = insider.get("transactions", [])
                if txns:
                    lines = []
                    for tx in txns[:5]:
                        name = tx.get("insider_name") or tx.get("insider", "?")
                        action = tx.get("action") or tx.get("type", "?")
                        shares = tx.get("shares", "?")
                        value = tx.get("value") or tx.get("price", "?")
                        date = tx.get("date", "?")
                        lines.append(f"- {name}: {action} {shares} shares (${value}) ({date})")
                    sym_sections.append(f"### {sym} — Insider Activity\n" + "\n".join(lines))
                else:
                    sym_sections.append(f"### {sym} — Insider Activity\nNo recent insider transactions")
        except Exception:
            pass

        # Company news (yfinance)
        try:
            news_items = market.get_company_news(sym, max_articles=5)
            if news_items and isinstance(news_items, list) and news_items:
                lines = []
                for item in news_items[:5]:
                    title = item.get("title") or item.get("headline", "?")
                    pub = item.get("published", "")
                    lines.append(f"- [{pub}] {title}" if pub else f"- {title}")
                sym_sections.append(f"### {sym} — Recent News (yfinance)\n" + "\n".join(lines))
        except Exception:
            pass

        # (Price history is now included via multi-timeframe returns in Price & Trend above.
        #  Agents can call get_price_history tool for raw OHLCV if they need deeper drill-down.)

        if sym_sections:
            sections.extend(sym_sections)

    if not sections:
        return ""
    return "\n\n## Pre-fetched market data\n\n" + "\n\n".join(sections)


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
