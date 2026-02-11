"""Free market data via yfinance.

Provides data that complements Schwab and serves as a fallback:
- Insider transactions (high-signal, ephemeral)
- Company fundamentals (P/E, market cap, sector, etc.)
- Historical OHLCV price data
- Recent company news articles

No API key required. All data is free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InsiderTransaction:
    """A single insider transaction record."""
    insider_name: str
    title: str           # e.g. "CEO", "CFO", "Director"
    action: str          # e.g. "Buy", "Sale", "Sale - Loss"
    shares: int
    value: float         # total dollar value
    date: str            # ISO date string
    ownership_type: str  # e.g. "Direct", "Indirect"

    def to_dict(self) -> dict[str, Any]:
        return {
            "insider_name": self.insider_name,
            "title": self.title,
            "action": self.action,
            "shares": self.shares,
            "value": self.value,
            "date": self.date,
            "ownership_type": self.ownership_type,
        }


@dataclass(frozen=True)
class Fundamentals:
    """Company fundamentals snapshot."""
    symbol: str
    name: str
    sector: str
    industry: str
    market_cap: float | None
    pe_ratio: float | None
    forward_pe: float | None
    eps: float | None
    forward_eps: float | None
    dividend_yield: float | None
    beta: float | None
    week_52_high: float | None
    week_52_low: float | None
    avg_50d: float | None
    avg_200d: float | None
    debt_to_equity: float | None
    current_ratio: float | None
    profit_margin: float | None
    return_on_equity: float | None
    free_cash_flow: float | None
    revenue: float | None
    fetched_at: str  # ISO timestamp

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is not None:
                d[k] = v
        return d


@dataclass(frozen=True)
class NewsArticle:
    """A news article from yfinance."""
    title: str
    summary: str
    publisher: str
    url: str
    published_at: str | None  # ISO timestamp or None
    fetched_at: str            # ISO timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "publisher": self.publisher,
            "url": self.url,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
        }


@dataclass(frozen=True)
class PriceBar:
    """A single OHLCV bar from yfinance."""
    date: str    # ISO date or datetime string
    o: float
    h: float
    l: float
    c: float
    v: int

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "o": self.o, "h": self.h,
                "l": self.l, "c": self.c, "v": self.v}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class YFinanceClient:
    """Free market data client using yfinance. No API key required."""

    # ------------------------------------------------------------------
    # Insider activity
    # ------------------------------------------------------------------

    def check_insider_activity(self, symbol: str) -> dict[str, Any]:
        """Check recent insider transactions for *symbol*.

        Returns a dict with:
        - transactions: list of InsiderTransaction dicts
        - summary: quick stats (net buy/sell, total insiders)
        """
        import yfinance as yf

        result: dict[str, Any] = {
            "symbol": symbol.upper(),
            "transactions": [],
            "summary": {},
            "fetched_at": _now_iso(),
        }

        try:
            ticker = yf.Ticker(symbol.upper())
            txns = ticker.insider_transactions

            if txns is None or txns.empty:
                result["summary"] = {"status": "no_data"}
                return result

            import math

            records: list[dict[str, Any]] = []
            buy_count = 0
            sell_count = 0
            buy_value = 0.0
            sell_value = 0.0

            for _, row in txns.iterrows():
                # yfinance puts action description in 'Text', not 'Transaction'
                text = str(row.get("Text", ""))
                shares = int(row.get("Shares", 0))
                raw_value = row.get("Value", 0)
                value = 0.0 if (raw_value is None or
                                (isinstance(raw_value, float) and
                                 math.isnan(raw_value))) else float(raw_value)

                # Classify action from text
                text_lower = text.lower()
                if "purchase" in text_lower or "buy" in text_lower:
                    action = "Buy"
                elif "sale" in text_lower or "sell" in text_lower:
                    action = "Sale"
                elif "gift" in text_lower:
                    action = "Gift"
                elif "option" in text_lower:
                    action = "Option Exercise"
                else:
                    action = text[:50] if text else "Unknown"

                # Normalize date
                date_val = row.get("Start Date", row.get("Date", None))
                if hasattr(date_val, "isoformat"):
                    date_str = date_val.isoformat()[:10]
                else:
                    date_str = str(date_val) if date_val else ""

                tx = InsiderTransaction(
                    insider_name=str(row.get("Insider", "Unknown")),
                    title=str(row.get("Position", "")),
                    action=action,
                    shares=shares,
                    value=value,
                    date=date_str,
                    ownership_type=str(row.get("Ownership", "")),
                )
                records.append(tx.to_dict())

                # Classify for summary
                if action == "Buy":
                    buy_count += 1
                    buy_value += value
                elif action == "Sale":
                    sell_count += 1
                    sell_value += value

            result["transactions"] = records
            result["summary"] = {
                "total_transactions": len(records),
                "buy_count": buy_count,
                "sell_count": sell_count,
                "net_buy_value": round(buy_value - sell_value, 2),
                "signal": (
                    "net_buying" if buy_value > sell_value
                    else "net_selling" if sell_value > buy_value
                    else "neutral"
                ),
            }
            return result

        except Exception as e:
            if DEBUG:
                raise
            result["summary"] = {"status": "error", "error": str(e)}
            return result

    # ------------------------------------------------------------------
    # Fundamentals
    # ------------------------------------------------------------------

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Get company fundamentals for *symbol*.

        Returns a Fundamentals dict with key valuation, profitability,
        and health metrics. Designed for quick context, not deep analysis.
        """
        import yfinance as yf

        try:
            ticker = yf.Ticker(symbol.upper())
            info = ticker.info or {}

            f = Fundamentals(
                symbol=symbol.upper(),
                name=info.get("longName", symbol.upper()),
                sector=info.get("sector", ""),
                industry=info.get("industry", ""),
                market_cap=info.get("marketCap"),
                pe_ratio=info.get("trailingPE"),
                forward_pe=info.get("forwardPE"),
                eps=info.get("trailingEps"),
                forward_eps=info.get("forwardEps"),
                dividend_yield=info.get("dividendYield"),
                beta=info.get("beta"),
                week_52_high=info.get("fiftyTwoWeekHigh"),
                week_52_low=info.get("fiftyTwoWeekLow"),
                avg_50d=info.get("fiftyDayAverage"),
                avg_200d=info.get("twoHundredDayAverage"),
                debt_to_equity=info.get("debtToEquity"),
                current_ratio=info.get("currentRatio"),
                profit_margin=info.get("profitMargins"),
                return_on_equity=info.get("returnOnEquity"),
                free_cash_flow=info.get("freeCashflow"),
                revenue=info.get("totalRevenue"),
                fetched_at=_now_iso(),
            )
            return f.to_dict()

        except Exception as e:
            if DEBUG:
                raise
            return {"symbol": symbol.upper(), "error": str(e),
                    "fetched_at": _now_iso()}

    # ------------------------------------------------------------------
    # Price history
    # ------------------------------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        period: str = "1mo",
        interval: str = "1d",
    ) -> dict[str, Any]:
        """Get historical OHLCV data for *symbol*.

        Args:
            symbol: Ticker symbol.
            period: yfinance period string (e.g. "5d", "1mo", "3mo", "1y").
            interval: Bar interval (e.g. "1m", "5m", "1h", "1d").

        Returns a dict with bars list and metadata.
        """
        import yfinance as yf

        result: dict[str, Any] = {
            "symbol": symbol.upper(),
            "period": period,
            "interval": interval,
            "bars": [],
            "fetched_at": _now_iso(),
        }

        try:
            ticker = yf.Ticker(symbol.upper())
            df = ticker.history(period=period, interval=interval)

            if df.empty:
                return result

            # Strip timezone for clean serialization
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            bars: list[dict[str, Any]] = []
            for ts, row in df.iterrows():
                bar = PriceBar(
                    date=ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    o=round(float(row.get("Open", 0)), 4),
                    h=round(float(row.get("High", 0)), 4),
                    l=round(float(row.get("Low", 0)), 4),
                    c=round(float(row.get("Close", 0)), 4),
                    v=int(row.get("Volume", 0)),
                )
                bars.append(bar.to_dict())

            result["bars"] = bars
            result["bar_count"] = len(bars)
            return result

        except Exception as e:
            if DEBUG:
                raise
            result["error"] = str(e)
            return result

    # ------------------------------------------------------------------
    # Company news
    # ------------------------------------------------------------------

    def get_company_news(
        self,
        symbol: str,
        max_articles: int = 10,
    ) -> dict[str, Any]:
        """Get recent news articles for *symbol*.

        Returns a dict with articles list. Each article has title, summary,
        publisher, url, and timestamps. The article text at time of fetch
        is ephemeral — articles get edited, paywalled, or removed.
        """
        import yfinance as yf

        result: dict[str, Any] = {
            "symbol": symbol.upper(),
            "articles": [],
            "fetched_at": _now_iso(),
        }

        try:
            ticker = yf.Ticker(symbol.upper())
            news = ticker.get_news(count=max_articles)

            if not news:
                return result

            articles: list[dict[str, Any]] = []
            for article in news:
                parsed = _parse_yf_article(article)
                articles.append(parsed.to_dict())

            result["articles"] = articles
            result["article_count"] = len(articles)
            return result

        except Exception as e:
            if DEBUG:
                raise
            result["error"] = str(e)
            return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current UTC timestamp as ISO string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_yf_article(article: dict[str, Any]) -> NewsArticle:
    """Parse a yfinance news article (handles nested 'content' structure)."""
    fetched_at = _now_iso()

    if "content" in article:
        content = article["content"]
        title = content.get("title", "No title")
        summary = content.get("summary", "")
        provider = content.get("provider", {})
        publisher = provider.get("displayName", "Unknown")

        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        url = url_obj.get("url", "")

        pub_date_str = content.get("pubDate", "")
        published_at = None
        if pub_date_str:
            try:
                dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                published_at = dt.isoformat()
            except (ValueError, AttributeError):
                published_at = pub_date_str
    else:
        title = article.get("title", "No title")
        summary = article.get("summary", "")
        publisher = article.get("publisher", "Unknown")
        url = article.get("link", "")
        published_at = None

    return NewsArticle(
        title=title,
        summary=summary,
        publisher=publisher,
        url=url,
        published_at=published_at,
        fetched_at=fetched_at,
    )
