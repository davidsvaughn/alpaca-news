"""FinnHub API client — free tier company news.

Uses the REST endpoint `/company-news` (free, 60 req/min).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

import httpx

_BASE_URL = "https://finnhub.io/api/v1"


def get_company_news(
    symbol: str,
    *,
    days_back: int = 3,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent company news from FinnHub.

    Args:
        symbol: Stock ticker (e.g. 'AAPL').
        days_back: How many days of history to fetch (default 3).
        api_key: FinnHub API key (falls back to FINNHUB_API_KEY env var).

    Returns:
        List of article dicts with keys: category, datetime, headline, id,
        image, related, source, summary, url.
    """
    key = api_key or os.getenv("FINNHUB_API_KEY")
    if not key:
        return []

    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    try:
        resp = httpx.get(
            f"{_BASE_URL}/company-news",
            params={
                "symbol": symbol.upper(),
                "from": from_date,
                "to": to_date,
                "token": key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def format_news_for_prompt(
    articles: list[dict[str, Any]],
    *,
    max_articles: int = 10,
) -> str:
    """Format FinnHub articles into a concise text block for an LLM prompt.

    Returns empty string if no articles.
    """
    if not articles:
        return ""

    lines: list[str] = []
    for article in articles[:max_articles]:
        ts = article.get("datetime", 0)
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
        headline = article.get("headline", "?")
        source = article.get("source", "?")
        summary = article.get("summary", "")
        # Truncate long summaries
        if len(summary) > 200:
            summary = summary[:197] + "..."
        lines.append(f"- [{dt}] ({source}) {headline}")
        if summary:
            lines.append(f"  {summary}")

    return "\n".join(lines)
