"""Unified market data service with automatic vendor fallback.

Provides a single interface for the explorer's financial data tools.
Tries Schwab first (real-time, richer data) and falls back to yfinance
(free, no API key) when Schwab is unavailable or a call fails.

For tools that only one vendor provides, delegates directly:
- Schwab-only: options activity, movers, market hours, streaming, real-time quotes
- yfinance-only: company news (yfinance), technical indicators
- Finnhub (yfinance fallback): company news (finnhub), insider activity

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

    def compute_volume_delta(self, symbol: str) -> dict[str, Any]:
        """Compute uptick/downtick volume from today's 1-min bars.

        Uses the inter-bar tick rule: assigns each bar's entire volume as
        uptick or downtick based on whether close > or < previous close.
        Most robust approximation method (0/5 direction errors in testing).

        Returns dict with net_delta, imbalance, direction, and totals.
        Falls back to yfinance if Schwab unavailable.
        """
        import numpy as np

        try:
            hist = self.get_price_history(symbol, period="1d", interval="1m")
            bars = hist.get("bars", []) if isinstance(hist, dict) else []
            if len(bars) < 5:
                return {"error": f"insufficient bars ({len(bars)})", "symbol": symbol}

            close = np.array([b["c"] for b in bars], dtype=float)
            volume = np.array([b["v"] for b in bars], dtype=float)

            # Inter-bar tick rule
            prev_close = np.roll(close, 1)
            direction = np.sign(close - prev_close)
            direction[0] = 0  # no previous bar for first

            # Forward-fill zero-ticks with last non-zero direction
            for i in range(1, len(direction)):
                if direction[i] == 0:
                    direction[i] = direction[i - 1]

            uptick = float(np.sum(volume[direction > 0]))
            downtick = float(np.sum(volume[direction < 0]))
            total = uptick + downtick
            net = uptick - downtick
            imbalance = net / total if total > 0 else 0.0

            if imbalance > 0.02:
                dir_label = "bullish"
            elif imbalance < -0.02:
                dir_label = "bearish"
            else:
                dir_label = "neutral"

            return {
                "symbol": symbol.upper(),
                "uptick_volume": int(uptick),
                "downtick_volume": int(downtick),
                "net_delta": int(net),
                "imbalance": round(imbalance, 4),
                "direction": dir_label,
                "bar_count": len(bars),
                "source": hist.get("source", "unknown"),
            }
        except Exception as e:
            if DEBUG:
                raise
            return {"error": str(e), "symbol": symbol}

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
        """Insider transactions (Finnhub primary, yfinance title enrichment + fallback)."""
        from datetime import datetime, timezone

        try:
            from trader.market.finnhub_client import get_insider_transactions, _TX_CODE_MAP

            raw = get_insider_transactions(symbol)
            if raw:
                # Best-effort title enrichment from yfinance
                title_map: dict[str, str] = {}
                try:
                    import yfinance as yf
                    yf_txns = yf.Ticker(symbol.upper()).insider_transactions
                    if yf_txns is not None and not yf_txns.empty:
                        for _, row in yf_txns.iterrows():
                            name = str(row.get("Insider", "")).strip()
                            pos = str(row.get("Position", "")).strip()
                            if name and pos:
                                title_map[name.upper()] = pos
                except Exception:
                    pass

                transactions: list[dict[str, Any]] = []
                buy_count = 0
                sell_count = 0
                buy_value = 0.0
                sell_value = 0.0

                for rec in raw:
                    code = rec.get("transactionCode", "")
                    action = _TX_CODE_MAP.get(code, code)
                    change = rec.get("change", 0) or 0
                    price = rec.get("transactionPrice", 0) or 0
                    shares = abs(change)
                    value = round(shares * price, 2) if price else 0.0
                    name = rec.get("name", "Unknown")

                    transactions.append({
                        "insider_name": name,
                        "title": title_map.get(name.upper(), ""),
                        "action": action,
                        "shares": shares,
                        "value": value,
                        "date": rec.get("transactionDate", ""),
                        "ownership_type": "",
                    })

                    if code == "P":
                        buy_count += 1
                        buy_value += value
                    elif code == "S":
                        sell_count += 1
                        sell_value += value

                return {
                    "symbol": symbol.upper(),
                    "transactions": transactions,
                    "summary": {
                        "total_transactions": len(transactions),
                        "buy_count": buy_count,
                        "sell_count": sell_count,
                        "net_buy_value": round(buy_value - sell_value, 2),
                        "signal": (
                            "net_buying" if buy_value > sell_value
                            else "net_selling" if sell_value > buy_value
                            else "neutral"
                        ),
                    },
                    "source": "finnhub",
                    "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                }
        except Exception:
            pass

        # Fallback to yfinance
        result = self._yfinance.check_insider_activity(symbol)
        result["source"] = "yfinance"
        return result

    def get_company_news(
        self, symbol: str, max_articles: int = 10, finnhub_days_back: int = 7,
    ) -> dict[str, Any]:
        """Recent company news articles (yfinance + Finnhub, merged)."""
        from datetime import datetime as _dt, timezone as _tz

        result = self._yfinance.get_company_news(symbol, max_articles=max_articles)

        # Tag yfinance articles
        for a in result.get("articles", []):
            a.setdefault("source", "yfinance")

        # Merge Finnhub articles
        try:
            fh = self.get_finnhub_news(symbol, days_back=finnhub_days_back, max_articles=max_articles)
            for a in fh.get("articles", []):
                a.setdefault("source", "finnhub")
                # Normalize field names to match yfinance shape
                if "headline" in a and "title" not in a:
                    a["title"] = a["headline"]
                if "datetime" in a and "published_at" not in a:
                    ts = a["datetime"]
                    if isinstance(ts, (int, float)) and ts > 0:
                        a["published_at"] = _dt.fromtimestamp(ts, tz=_tz.utc).isoformat()
                if "source" not in a:
                    a["source"] = "finnhub"
                result.setdefault("articles", []).append(a)
        except Exception:
            pass  # Finnhub failure shouldn't break news collection

        result["source"] = "yfinance+finnhub"
        result["article_count"] = len(result.get("articles", []))
        return result

    def get_finnhub_news(
        self, symbol: str, days_back: int = 7, max_articles: int = 15
    ) -> dict[str, Any]:
        """Recent company news from Finnhub."""
        from trader.market.finnhub_client import get_company_news as _fh_news

        articles = _fh_news(symbol, days_back=min(days_back, 7))
        articles = articles[:max_articles]
        return {"symbol": symbol, "count": len(articles), "articles": articles, "source": "finnhub"}

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
