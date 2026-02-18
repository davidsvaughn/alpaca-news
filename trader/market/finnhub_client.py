"""FinnHub API client — free tier endpoints.

Free endpoints used (60 req/min rate limit):
- /company-news — company news articles
- /stock/earnings — historical earnings surprises
- /calendar/earnings — upcoming/recent earnings dates
- /stock/recommendation — analyst recommendation trends
- /stock/metric — key financial metrics (growth, valuation, margins)
- /stock/insider-transactions — SEC Form 4 insider trades
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


def get_earnings_surprises(
    symbol: str,
    *,
    limit: int = 4,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch historical earnings surprises (actual vs estimate EPS).

    Returns list of dicts with: actual, estimate, surprise, surprisePercent,
    period, quarter, year, symbol.
    """
    key = api_key or os.getenv("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        resp = httpx.get(
            f"{_BASE_URL}/stock/earnings",
            params={"symbol": symbol.upper(), "limit": limit, "token": key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_earnings_calendar(
    symbol: str,
    *,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch earnings calendar entries for a symbol.

    Returns list of dicts with: date, epsActual, epsEstimate, hour (bmo/amc),
    quarter, year, revenueActual, revenueEstimate, symbol.
    """
    key = api_key or os.getenv("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        resp = httpx.get(
            f"{_BASE_URL}/calendar/earnings",
            params={"symbol": symbol.upper(), "token": key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        calendar = data.get("earningsCalendar", [])
        return calendar if isinstance(calendar, list) else []
    except Exception:
        return []


def get_recommendation_trends(
    symbol: str,
    *,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch analyst recommendation trends (buy/hold/sell distribution).

    Returns list of monthly dicts with: buy, hold, sell, strongBuy,
    strongSell, period, symbol. Most recent first.
    """
    key = api_key or os.getenv("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        resp = httpx.get(
            f"{_BASE_URL}/stock/recommendation",
            params={"symbol": symbol.upper(), "token": key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ---- /stock/metric — growth, valuation, relative performance ----

# (api_key, human_label) pairs for cherry-picked metrics
_STOCK_METRIC_KEYS: list[tuple[str, str]] = [
    # Tier 1 — Growth & Relative Strength
    ("epsGrowthTTMYoy", "EPS Growth (TTM YoY)"),
    ("revenueGrowthTTMYoy", "Revenue Growth (TTM YoY)"),
    ("revenueGrowthQuarterlyYoy", "Revenue Growth (Q YoY)"),
    ("pegTTM", "PEG Ratio"),
    ("psTTM", "Price/Sales"),
    ("evEbitdaTTM", "EV/EBITDA"),
    ("priceRelativeToS&P50013Week", "vs S&P 500 (13W)"),
    ("priceRelativeToS&P50026Week", "vs S&P 500 (26W)"),
    ("priceRelativeToS&P50052Week", "vs S&P 500 (52W)"),
    # Tier 2 — Profitability & Quality
    ("grossMarginTTM", "Gross Margin"),
    ("operatingMarginTTM", "Operating Margin"),
    ("roaTTM", "ROA"),
    ("epsGrowthQuarterlyYoy", "EPS Growth (Q YoY)"),
    ("cashFlowPerShareTTM", "CF/Share"),
]


def get_stock_metrics(
    symbol: str,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch key financial metrics from FinnHub (growth, valuation, margins).

    Returns a flat dict of cherry-picked metrics from /stock/metric?metric=all.
    Empty dict on failure or missing API key.
    """
    key = api_key or os.getenv("FINNHUB_API_KEY")
    if not key:
        return {}
    try:
        resp = httpx.get(
            f"{_BASE_URL}/stock/metric",
            params={"symbol": symbol.upper(), "metric": "all", "token": key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        metrics = data.get("metric", {})
        if not isinstance(metrics, dict):
            return {}
        picked: dict[str, Any] = {}
        for metric_key, _label in _STOCK_METRIC_KEYS:
            val = metrics.get(metric_key)
            if val is not None:
                picked[metric_key] = val
        return picked
    except Exception:
        return {}


def format_metrics_for_prompt(metrics: dict[str, Any]) -> str:
    """Format cherry-picked stock metrics into a concise text block for an LLM prompt."""
    if not metrics:
        return ""
    lines: list[str] = []
    for metric_key, label in _STOCK_METRIC_KEYS:
        val = metrics.get(metric_key)
        if val is None:
            continue
        if isinstance(val, float):
            if any(kw in label for kw in ("Growth", "Margin", "ROA", "vs S&P")):
                lines.append(f"{label}: {val:+.1f}%")
            else:
                lines.append(f"{label}: {val:.2f}")
        else:
            lines.append(f"{label}: {val}")
    return " | ".join(lines)


# ---- /stock/insider-transactions — SEC Form 4 filings ----

_TX_CODE_MAP: dict[str, str] = {
    "P": "Purchase",
    "S": "Sale",
    "A": "Award/Grant",
    "M": "Option Exercise",
    "F": "Tax Withholding",
    "G": "Gift",
    "X": "Option Exercise",
    "C": "Conversion",
    "D": "Disposition",
}


def get_insider_transactions(
    symbol: str,
    *,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch insider transactions from FinnHub (SEC Form 4 filings).

    Returns list of raw transaction dicts from /stock/insider-transactions.
    Empty list on failure or missing API key.
    """
    key = api_key or os.getenv("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        resp = httpx.get(
            f"{_BASE_URL}/stock/insider-transactions",
            params={"symbol": symbol.upper(), "token": key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        records = data.get("data", [])
        return records if isinstance(records, list) else []
    except Exception:
        return []


# ---- Prompt formatters ----

def format_earnings_for_prompt(
    surprises: list[dict[str, Any]],
    calendar: list[dict[str, Any]],
) -> str:
    """Format earnings data into a concise text block for an LLM prompt."""
    lines: list[str] = []

    if calendar:
        next_entry = calendar[0]
        date = next_entry.get("date", "?")
        hour = next_entry.get("hour", "?")
        hour_label = {"bmo": "before market open", "amc": "after market close", "dmh": "during market hours"}.get(hour, hour)
        eps_est = next_entry.get("epsEstimate")
        rev_est = next_entry.get("revenueEstimate")
        eps_actual = next_entry.get("epsActual")

        if eps_actual is not None:
            lines.append(f"Last earnings: {date} ({hour_label}) — EPS actual: ${eps_actual:.2f}" +
                         (f" vs est: ${eps_est:.2f}" if eps_est else ""))
        elif eps_est is not None:
            lines.append(f"Next earnings: {date} ({hour_label}) — EPS estimate: ${eps_est:.2f}")
        else:
            lines.append(f"Earnings date: {date} ({hour_label})")

        if rev_est and not next_entry.get("revenueActual"):
            lines.append(f"  Revenue estimate: ${rev_est / 1e9:.1f}B")

    if surprises:
        beats = sum(1 for s in surprises if (s.get("surprise") or 0) > 0)
        misses = sum(1 for s in surprises if (s.get("surprise") or 0) < 0)
        total = len(surprises)
        lines.append(f"Earnings track record (last {total} quarters): {beats} beats, {misses} misses")
        # Show most recent surprise
        latest = surprises[0]
        pct = latest.get("surprisePercent", 0)
        direction = "beat" if pct > 0 else "missed"
        lines.append(f"  Most recent ({latest.get('period', '?')}): {direction} by {abs(pct):.1f}%"
                      f" (actual ${latest.get('actual', '?'):.2f} vs est ${latest.get('estimate', '?'):.2f})")

    return "\n".join(lines)


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
