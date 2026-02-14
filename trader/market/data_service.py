"""Unified market data service with automatic vendor fallback.

Provides a single interface for the explorer's financial data tools.
Tries Schwab first (real-time, richer data) and falls back to yfinance
(free, no API key) when Schwab is unavailable or a call fails.

For tools that only one vendor provides, delegates directly:
- Schwab-only: options activity, movers, market hours, streaming, real-time quotes
- yfinance-only: insider activity, company news, technical indicators

For overlapping tools (fundamentals, price history), tries Schwab → yfinance.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


class MarketDataService:
    """Unified market data interface with Schwab → yfinance fallback."""

    def __init__(self) -> None:
        from trader.market.schwab_client import SchwabMarketClient
        from trader.market.yfinance_client import YFinanceClient

        self._schwab = SchwabMarketClient()
        self._yfinance = YFinanceClient()

    @property
    def schwab_available(self) -> bool:
        return self._schwab.available

    # ------------------------------------------------------------------
    # Overlapping tools (Schwab → yfinance fallback)
    # ------------------------------------------------------------------

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Get company fundamentals. Tries Schwab, falls back to yfinance.

        Both sources provide P/E, market cap, EPS, beta, 52-week range.
        yfinance additionally provides sector, industry, debt ratios,
        profit margins, free cash flow.
        """
        if self._schwab.available:
            try:
                result = self._schwab.get_fundamentals(symbol)
                if "error" not in result:
                    result["source"] = "schwab"
                    return result
            except Exception:
                if DEBUG:
                    raise

        # Fallback to yfinance
        result = self._yfinance.get_fundamentals(symbol)
        result["source"] = "yfinance"
        return result

    def get_price_history(
        self,
        symbol: str,
        period: str = "1mo",
        interval: str = "1d",
    ) -> dict[str, Any]:
        """Get historical OHLCV data. Tries Schwab for intraday, yfinance otherwise.

        Schwab excels at intraday (1-min candles, extended hours).
        yfinance covers all periods/intervals with no API key.
        """
        # For intraday, prefer Schwab (richer data, extended hours)
        if self._schwab.available and interval in ("1m", "1min", "minute"):
            try:
                candles = self._schwab.get_intraday_candles(symbol)
                if candles:
                    return {
                        "symbol": symbol.upper(),
                        "period": period,
                        "interval": interval,
                        "bars": [
                            {"date": c.t, "o": c.o, "h": c.h,
                             "l": c.l, "c": c.c, "v": c.v}
                            for c in candles
                        ],
                        "bar_count": len(candles),
                        "source": "schwab",
                        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                    }
            except Exception:
                if DEBUG:
                    raise

        # Fallback / default: yfinance handles all periods and intervals
        result = self._yfinance.get_price_history(symbol, period=period, interval=interval)
        result["source"] = "yfinance"
        return result

    # ------------------------------------------------------------------
    # Schwab-only tools
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Real-time quote snapshot (Schwab only)."""
        quote = self._schwab.get_quote(symbol)
        if quote:
            d = quote.to_dict()
            d["source"] = "schwab"
            return d
        return {"symbol": symbol, "error": "no quote data", "source": "schwab"}

    def check_options_activity(self, symbol: str) -> dict[str, Any]:
        """Options activity — ATM IV, put/call ratios (Schwab only)."""
        result = self._schwab.check_options_activity(symbol)
        result["source"] = "schwab"
        return result

    def get_movers(
        self,
        index: str = "$SPX",
        direction: str = "up",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Market movers (Schwab only, requires market hours)."""
        result = self._schwab.get_movers(index, direction=direction, max_results=max_results)
        result["source"] = "schwab"
        return result

    def get_market_hours(self, market: str = "equity") -> dict[str, Any]:
        """Market hours (Schwab only)."""
        result = self._schwab.get_market_hours(market)
        result["source"] = "schwab"
        return result

    def check_price_spike(self, symbol: str) -> dict[str, Any]:
        """Check for recent price spike (Schwab only — needs intraday candles)."""
        result = self._schwab.check_price_spike(symbol)
        result["source"] = "schwab"
        return result

    def check_volume_regime(self, symbol: str) -> dict[str, Any]:
        """Check for abnormal volume (Schwab only — needs intraday candles)."""
        result = self._schwab.check_volume_regime(symbol)
        result["source"] = "schwab"
        return result

    def build_market_context(self) -> dict[str, Any]:
        """Build market-level context (SPY, VIX, session)."""
        result = self._schwab.build_market_context()
        result["source"] = "schwab"
        return result

    def build_price_context(self, symbols: list[str]) -> dict[str, Any]:
        """Build per-symbol price context for snapshot."""
        result = self._schwab.build_price_context(symbols)
        result["source"] = "schwab"
        return result

    # ------------------------------------------------------------------
    # yfinance-only tools
    # ------------------------------------------------------------------

    def get_financial_statements(
        self,
        symbol: str,
        statement: str = "income",
        freq: str = "quarterly",
        periods: int = 4,
    ) -> dict[str, Any]:
        """Get financial statement data (yfinance only — free)."""
        result = self._yfinance.get_financial_statements(
            symbol, statement=statement, freq=freq, periods=periods,
        )
        result["source"] = "yfinance"
        return result

    def check_insider_activity(self, symbol: str) -> dict[str, Any]:
        """Insider transactions (yfinance only — free, high-signal)."""
        result = self._yfinance.check_insider_activity(symbol)
        result["source"] = "yfinance"
        return result

    def get_company_news(self, symbol: str, max_articles: int = 10) -> dict[str, Any]:
        """Recent company news articles (yfinance only)."""
        result = self._yfinance.get_company_news(symbol, max_articles=max_articles)
        result["source"] = "yfinance"
        return result

    def get_technical_indicators(
        self,
        symbol: str,
        indicators: list[str],
        lookback_days: int = 30,
    ) -> dict[str, Any]:
        """Technical indicators via stockstats (computed locally from yfinance data)."""
        from trader.market.indicators import get_technical_indicators
        result = get_technical_indicators(symbol, indicators, lookback_days=lookback_days)
        result["source"] = "stockstats"
        return result

    def get_current_technicals(
        self,
        symbol: str,
        indicators: list[str] | None = None,
    ) -> dict[str, Any]:
        """Quick snapshot of current technical indicator values."""
        from trader.market.indicators import get_current_technicals
        result = get_current_technicals(symbol, indicators)
        result["source"] = "stockstats"
        return result
