"""Pure tool functions and registry for the native-SDK runners.

Each tool is a plain function: (market, **kwargs) -> str.
No PydanticAI RunContext dependency — any framework can call these.

The TOOL_REGISTRY list provides JSON-schema metadata so runners
can register tools with their respective SDKs.
"""

from __future__ import annotations

import json
import os
from typing import Any

from trader.market.data_service import MarketDataService
from trader.online.agent_common import TOOL_MODALITY, ToolDef


# ---------------------------------------------------------------------------
# Market data tools
# ---------------------------------------------------------------------------


def check_price(market: MarketDataService, symbol: str) -> str:
    """Get real-time quote with trend context: current price, volume, net change,
    period returns (1W/1M/3M/6M/1Y), and 52-week range position."""
    result = market.get_quote(symbol)
    if not result or result.get("error"):
        return json.dumps(result, default=str)

    last = result.get("last_price") or result.get("lastPrice") or result.get("regularMarketPrice")
    current_price = float(last) if last else 0.0

    if current_price > 0:
        try:
            hist = market.get_price_history(symbol, period="1y", interval="1d")
            bars = hist.get("bars", []) if isinstance(hist, dict) else []
            if bars:
                returns_str = _period_returns(bars, current_price)
                if returns_str:
                    result["period_returns"] = returns_str
        except Exception:
            pass

        try:
            fund = market.get_fundamentals(symbol)
            if fund and not fund.get("error"):
                w52_high = fund.get("week_52_high") or fund.get("52WeekHigh")
                w52_low = fund.get("week_52_low") or fund.get("52WeekLow")
                if (w52_high and w52_low
                        and isinstance(w52_high, (int, float))
                        and isinstance(w52_low, (int, float))
                        and w52_high > w52_low):
                    pct = (current_price - w52_low) / (w52_high - w52_low) * 100
                    result["52_week_range"] = f"${w52_low:.2f}–${w52_high:.2f}"
                    result["52_week_position_pct"] = round(pct, 1)
        except Exception:
            pass

    return json.dumps(result, default=str)


def check_market_context(market: MarketDataService) -> str:
    """Get broad market context: SPY price/change, VIX level, market session."""
    result = market.build_market_context()
    return json.dumps(result, default=str)


def check_options_activity(market: MarketDataService, symbol: str) -> str:
    """Get options market activity: ATM IV, put/call volume ratio, put/call OI ratio."""
    result = market.check_options_activity(symbol)
    return json.dumps(result, default=str)


def get_fundamentals(market: MarketDataService, symbol: str) -> str:
    """Get company fundamentals: P/E, EPS, market cap, beta, 52-week range, dividend yield."""
    result = market.get_fundamentals(symbol)
    return json.dumps(result, default=str)


def get_movers(market: MarketDataService, index: str = "$SPX", direction: str = "up") -> str:
    """Get top market movers (gainers or losers) for an index.

    Args:
        index: '$SPX', '$DJI', '$COMPX', 'NYSE', or 'NASDAQ'
        direction: 'up' for gainers, 'down' for losers
    """
    result = market.get_movers(index, direction=direction)
    return json.dumps(result, default=str)


def check_insider_activity(market: MarketDataService, symbol: str) -> str:
    """Get recent insider transactions: buys, sells, grants."""
    result = market.check_insider_activity(symbol)
    return json.dumps(result, default=str)


def get_company_news(market: MarketDataService, symbol: str) -> str:
    """Get recent news articles for a company (yfinance + Finnhub)."""
    result = market.get_company_news(symbol, max_articles=10)
    return json.dumps(result, default=str)



def get_analyst_ratings(market: MarketDataService, symbol: str) -> str:
    """Get analyst recommendation trends for a stock (FinnHub)."""
    from trader.market.finnhub_client import get_recommendation_trends

    trends = get_recommendation_trends(symbol)
    if not trends:
        return json.dumps({"symbol": symbol, "trends": [], "note": "No data or FINNHUB_API_KEY not set"})
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


def get_price_history(market: MarketDataService, symbol: str, period: str = "5d", interval: str = "1d") -> str:
    """Get historical OHLCV price data.

    Args:
        symbol: Stock ticker
        period: '1d', '5d', '1mo', '3mo', '6mo', '1y'
        interval: '1m', '5m', '15m', '1h', '1d'
    """
    result = market.get_price_history(symbol, period=period, interval=interval)
    return json.dumps(result, default=str)


def get_technical_indicators(market: MarketDataService, symbol: str, indicators: str = "rsi,macd,boll") -> str:
    """Get current technical indicator values with interpretation.

    Available: rsi, macd, macds, macdh, boll, boll_ub, boll_lb,
    close_50_sma, close_200_sma, close_10_ema, atr, vwma, mfi

    Args:
        symbol: Stock ticker
        indicators: Comma-separated list of indicator names
    """
    indicator_list = [i.strip() for i in indicators.split(",")]
    result = market.get_current_technicals(symbol, indicator_list)
    if not result or result.get("error"):
        return json.dumps(result, default=str)

    interp: dict[str, str] = {}
    if "rsi" in result and isinstance(result["rsi"], (int, float)):
        rsi = result["rsi"]
        if rsi > 70:
            interp["rsi"] = "overbought"
        elif rsi > 60:
            interp["rsi"] = "elevated"
        elif rsi < 30:
            interp["rsi"] = "oversold"
        elif rsi < 40:
            interp["rsi"] = "depressed"
        else:
            interp["rsi"] = "neutral"

    if "macd" in result and "macds" in result:
        macd_val = result["macd"]
        sig_val = result["macds"]
        if isinstance(macd_val, (int, float)) and isinstance(sig_val, (int, float)):
            interp["macd"] = "bullish" if macd_val > sig_val else "bearish"
            if macd_val > 0 and sig_val > 0:
                interp["macd"] += " (above zero)"
            elif macd_val < 0 and sig_val < 0:
                interp["macd"] += " (below zero)"

    current_price: float = 0.0
    needs_price = (
        ("boll_ub" in result and "boll_lb" in result)
        or any(result.get(k) for k in ("close_50_sma", "close_200_sma"))
    )
    if needs_price:
        try:
            quote = market.get_quote(symbol)
            current_price = float(quote.get("last_price") or quote.get("lastPrice") or 0)
        except (TypeError, ValueError):
            pass

    if "boll_ub" in result and "boll_lb" in result and current_price > 0:
        try:
            ub = float(result["boll_ub"])
            lb = float(result["boll_lb"])
            boll_range = ub - lb
            if boll_range > 0:
                pct = (current_price - lb) / boll_range * 100
                if pct > 80:
                    interp["bollinger"] = f"near upper band ({pct:.0f}%)"
                elif pct < 20:
                    interp["bollinger"] = f"near lower band ({pct:.0f}%)"
                else:
                    interp["bollinger"] = f"mid-band ({pct:.0f}%)"
        except (TypeError, ValueError):
            pass

    if current_price > 0:
        for sma_key, label in [("close_50_sma", "vs_50sma"), ("close_200_sma", "vs_200sma")]:
            sma_val = result.get(sma_key)
            if sma_val and isinstance(sma_val, (int, float)) and sma_val > 0:
                pct_diff = (current_price - sma_val) / sma_val * 100
                interp[label] = f"{pct_diff:+.1f}% ({'above' if pct_diff > 0 else 'below'})"

    if interp:
        result["interpretation"] = interp

    return json.dumps(result, default=str)


def check_price_spike(market: MarketDataService, symbol: str) -> str:
    """Check if there's been a significant recent price move (>0.5% in last 5 min)."""
    result = market.check_price_spike(symbol)
    return json.dumps(result, default=str)


def check_volume_regime(market: MarketDataService, symbol: str) -> str:
    """Check if recent trading volume is abnormally high compared to session average."""
    result = market.check_volume_regime(symbol)
    return json.dumps(result, default=str)


def get_financial_statements(
    market: MarketDataService,
    symbol: str,
    statement: str = "income",
    freq: str = "quarterly",
) -> str:
    """Get financial statement data for deep fundamental analysis.

    Args:
        symbol: Stock ticker (e.g. 'AAPL')
        statement: 'income', 'balance_sheet', or 'cash_flow'
        freq: 'quarterly' or 'yearly'
    """
    result = market.get_financial_statements(symbol, statement=statement, freq=freq)
    return json.dumps(result, default=str)


def url_fetch(url: str, **_kwargs: Any) -> str:
    """Fetch a web page and extract its article text.

    Args:
        url: The full URL to fetch and extract text from.
    """
    from trader.evidence.extract import extract_article
    from trader.evidence.fetch import fetch_url

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


def x_stream_cache(x_stream_service: Any, symbol: str, limit: int = 20) -> str:
    """Get cached recent X/Twitter posts from the live filtered stream.

    Args:
        symbol: Stock ticker to look up in the cache
        limit: Maximum number of posts to return (default 20)
    """
    if x_stream_service is None:
        return json.dumps({
            "symbol": symbol,
            "posts": [],
            "note": "X stream service not available",
        })
    try:
        posts = x_stream_service.get_recent_posts(key=symbol.upper(), limit=limit)
        if not posts:
            posts = x_stream_service.get_recent_posts(key="_all", limit=limit)
        return json.dumps({
            "symbol": symbol,
            "posts": posts,
            "count": len(posts),
            "source": "x_stream_cache",
        })
    except Exception as e:
        return json.dumps({"symbol": symbol, "error": str(e), "source": "x_stream_cache"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _period_returns(bars: list[dict[str, Any]], current_price: float) -> str:
    """Compute percentage returns at standard timeframes from daily OHLCV bars."""
    if not bars or current_price <= 0:
        return ""
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


# ---------------------------------------------------------------------------
# Tool registry — JSON schema metadata for SDK registration
# ---------------------------------------------------------------------------


def _sym_param(desc: str = "Stock ticker symbol (e.g. 'AAPL', 'NVDA')") -> dict:
    return {"type": "object", "properties": {"symbol": {"type": "string", "description": desc}}, "required": ["symbol"], "additionalProperties": False}


TOOL_REGISTRY: list[ToolDef] = [
    ToolDef(
        name="check_price",
        func=check_price,
        description="Get real-time quote with trend context: current price, volume, net change, period returns (1W/1M/3M/6M/1Y), and 52-week range position.",
        parameters=_sym_param(),
        modality="market_data",
    ),
    ToolDef(
        name="check_market_context",
        func=check_market_context,
        description="Get broad market context: SPY price/change, VIX level, market session (open/premarket/afterhours).",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        modality="macro",
    ),
    ToolDef(
        name="check_options_activity",
        func=check_options_activity,
        description="Get options market activity: ATM implied volatility, put/call volume ratio, put/call OI ratio.",
        parameters=_sym_param(),
        modality="market_data",
    ),
    ToolDef(
        name="get_fundamentals",
        func=get_fundamentals,
        description="Get company fundamentals: P/E, EPS, market cap, beta, 52-week range, dividend yield.",
        parameters=_sym_param(),
        modality="fundamentals",
    ),
    ToolDef(
        name="get_movers",
        func=get_movers,
        description="Get top market movers (gainers or losers) for an index. Must be called during market hours.",
        parameters={
            "type": "object",
            "properties": {
                "index": {"type": "string", "description": "'$SPX', '$DJI', '$COMPX', 'NYSE', or 'NASDAQ'", "default": "$SPX"},
                "direction": {"type": "string", "description": "'up' for gainers, 'down' for losers", "default": "up"},
            },
            "required": [],
            "additionalProperties": False,
        },
        modality="market_data",
    ),
    ToolDef(
        name="check_insider_activity",
        func=check_insider_activity,
        description="Get recent insider transactions: buys, sells, grants. Insider buying is one of the strongest bullish signals.",
        parameters=_sym_param(),
        modality="fundamentals",
    ),
    ToolDef(
        name="get_company_news",
        func=get_company_news,
        description="Get recent news articles for a company (yfinance + Finnhub merged).",
        parameters=_sym_param(),
        modality="news",
    ),
    ToolDef(
        name="get_analyst_ratings",
        func=get_analyst_ratings,
        description="Get analyst recommendation trends: buy/hold/sell distribution and how consensus is shifting (FinnHub).",
        parameters=_sym_param(),
        modality="fundamentals",
    ),
    ToolDef(
        name="get_price_history",
        func=get_price_history,
        description="Get historical OHLCV price data.",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker"},
                "period": {"type": "string", "description": "'1d', '5d', '1mo', '3mo', '6mo', '1y'", "default": "5d"},
                "interval": {"type": "string", "description": "'1m', '5m', '15m', '1h', '1d'", "default": "1d"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
        modality="market_data",
    ),
    ToolDef(
        name="get_technical_indicators",
        func=get_technical_indicators,
        description="Get current technical indicator values with interpretation. Available: rsi, macd, macds, macdh, boll, boll_ub, boll_lb, close_50_sma, close_200_sma, close_10_ema, atr, vwma, mfi.",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker"},
                "indicators": {"type": "string", "description": "Comma-separated list of indicator names", "default": "rsi,macd,boll"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
        modality="market_data",
    ),
    ToolDef(
        name="check_price_spike",
        func=check_price_spike,
        description="Check if there's been a significant recent price move (>0.5% in last 5 min).",
        parameters=_sym_param(),
        modality="market_data",
    ),
    ToolDef(
        name="check_volume_regime",
        func=check_volume_regime,
        description="Check if recent trading volume is abnormally high compared to session average.",
        parameters=_sym_param(),
        modality="market_data",
    ),
    ToolDef(
        name="get_financial_statements",
        func=get_financial_statements,
        description="Get financial statement data: income statement, balance sheet, or cash flow for the last 4 periods.",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker (e.g. 'AAPL')"},
                "statement": {"type": "string", "description": "'income', 'balance_sheet', or 'cash_flow'", "default": "income"},
                "freq": {"type": "string", "description": "'quarterly' or 'yearly'", "default": "quarterly"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
        modality="fundamentals",
    ),
    ToolDef(
        name="url_fetch",
        func=url_fetch,
        description="Fetch a web page and extract its article text. Returns extracted text, NOT raw HTML.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The full URL to fetch and extract text from"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        modality="web_research",
    ),
    ToolDef(
        name="x_stream_cache",
        func=x_stream_cache,
        description="Get cached recent X/Twitter posts from the live filtered stream for a symbol. Free (reads from cache).",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker to look up in the cache (e.g. 'NVDA')"},
                "limit": {"type": "integer", "description": "Maximum number of posts to return (default 20)", "default": 20},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
        modality="social",
    ),
]

# Quick lookup by name
TOOL_BY_NAME: dict[str, ToolDef] = {td.name: td for td in TOOL_REGISTRY}
