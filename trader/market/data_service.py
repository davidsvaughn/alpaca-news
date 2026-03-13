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
from datetime import datetime, timedelta, timezone
from typing import Any

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


def _schwab_symbol(raw: str) -> str:
    """Normalize to Schwab format: BRK.A -> BRK/A, BRK-A -> BRK/A."""
    s = raw.strip().upper()
    # Share classes: only transform single-letter suffixes (BRK.A, BRK-A)
    for sep in (".", "-"):
        if sep in s:
            base, suffix = s.rsplit(sep, 1)
            if len(suffix) == 1 and suffix.isalpha():
                return f"{base}/{suffix}"
    return s


def _yfinance_symbol(raw: str) -> str:
    """Normalize to yfinance format: BRK.A -> BRK-A, BRK/A -> BRK-A."""
    s = raw.strip().upper()
    for sep in (".", "/"):
        if sep in s:
            base, suffix = s.rsplit(sep, 1)
            if len(suffix) == 1 and suffix.isalpha():
                return f"{base}-{suffix}"
    return s


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

    def get_latest_minute_closes(self, symbols: list[str]) -> dict[str, float]:
        """Latest 1-minute close at-or-before now for each symbol.

        Intended for canonical portfolio marking where consistency matters
        more than quote freshness. Uses Schwab intraday candles and returns
        only symbols with a usable close.
        """
        result: dict[str, float] = {}
        if not symbols:
            return result

        if self._schwab.available:
            # Use the most recently completed minute, not an in-progress bar.
            end = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)
            start = end - timedelta(minutes=5)
            for sym in symbols:
                try:
                    candles = self._schwab.get_candles_by_date_range(
                        _schwab_symbol(sym),
                        start=start,
                        end=end,
                        frequency=1,
                        extended_hours=True,
                    )
                    if not candles:
                        continue
                    # Date-range lookup is more stable than "latest intraday" for
                    # completed-minute portfolio marks.
                    usable = [
                        c for c in candles
                        if datetime.fromisoformat(c.t.replace("Z", "+00:00")) <= end
                    ]
                    if not usable:
                        continue
                    close = usable[-1].c
                    if close is not None and float(close) > 0:
                        result[sym.upper()] = float(close)
                except Exception:
                    if DEBUG:
                        raise

        return result

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Get company fundamentals. Tries Schwab, falls back to yfinance.

        Both sources provide P/E, market cap, EPS, beta, 52-week range.
        yfinance additionally provides sector, industry, debt ratios,
        profit margins, free cash flow.
        """
        if self._schwab.available:
            try:
                result = self._schwab.get_fundamentals(_schwab_symbol(symbol))
                if "error" not in result:
                    result["source"] = "schwab"
                    return result
            except Exception:
                if DEBUG:
                    raise

        # Fallback to yfinance
        result = self._yfinance.get_fundamentals(_yfinance_symbol(symbol))
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
                candles = self._schwab.get_intraday_candles(_schwab_symbol(symbol))
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
        result = self._yfinance.get_price_history(_yfinance_symbol(symbol), period=period, interval=interval)
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

    def compute_volume_delta_history(
        self, symbol: str, days: int = 5,
    ) -> list[dict[str, Any]]:
        """Compute daily volume deltas for the last N trading days.

        Uses Schwab 1-min candles (up to 10 trading days back) with
        yfinance fallback (~7 calendar days of 1-min bars).
        Returns list of per-day dicts (most recent first), each with
        net_delta, imbalance, direction.
        """
        import numpy as np
        from datetime import date as date_type

        bars: list[dict] = []

        # Try Schwab first — supports up to period=10 days of 1-min bars
        if self._schwab.available:
            try:
                candles = self._schwab.get_intraday_candles(
                    symbol, period=min(days + 1, 10),
                )
                if candles:
                    bars = [
                        {"date": c.t, "c": c.c, "v": c.v}
                        for c in candles
                    ]
            except Exception:
                pass

        # Fallback to yfinance
        if not bars:
            try:
                import yfinance as yf
                ticker = yf.Ticker(symbol)
                df = ticker.history(period=f"{days + 2}d", interval="1m")
                if df is not None and not df.empty:
                    idx = df.index.tz_convert(None) if df.index.tz else df.index
                    bars = [
                        {"date": str(idx[i]), "c": float(df["Close"].iloc[i]), "v": float(df["Volume"].iloc[i])}
                        for i in range(len(df))
                    ]
            except Exception:
                pass

        if len(bars) < 10:
            return []

        # Group bars by trading date and compute per-day delta
        from collections import defaultdict
        by_date: dict[str, list[dict]] = defaultdict(list)
        for b in bars:
            day_str = b["date"][:10]  # YYYY-MM-DD prefix
            by_date[day_str].append(b)

        results: list[dict[str, Any]] = []
        for day_str in sorted(by_date.keys(), reverse=True):
            day_bars = by_date[day_str]
            if len(day_bars) < 5:
                continue

            close = np.array([b["c"] for b in day_bars], dtype=float)
            volume = np.array([b["v"] for b in day_bars], dtype=float)

            prev_close = np.roll(close, 1)
            direction = np.sign(close - prev_close)
            direction[0] = 0
            for i in range(1, len(direction)):
                if direction[i] == 0:
                    direction[i] = direction[i - 1]

            uptick = float(np.sum(volume[direction > 0]))
            downtick = float(np.sum(volume[direction < 0]))
            total = uptick + downtick
            net = uptick - downtick
            imbalance = net / total if total > 0 else 0.0

            results.append({
                "date": day_str,
                "net_delta": int(net),
                "imbalance": round(imbalance, 4),
                "direction": "bullish" if imbalance > 0.02 else ("bearish" if imbalance < -0.02 else "neutral"),
            })

        return results[:days]

    # ------------------------------------------------------------------
    # Schwab-only tools
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Real-time quote snapshot. Tries Schwab, then yfinance, then Finnhub."""
        if self._schwab.available:
            try:
                quote = self._schwab.get_quote(_schwab_symbol(symbol))
                if quote:
                    d = quote.to_dict()
                    d["source"] = "schwab"
                    return d
            except Exception:
                if DEBUG:
                    raise
        # Fallback to yfinance
        yf_q = self._yfinance.get_quote(_yfinance_symbol(symbol))
        yf_last = yf_q.get("last_price") if isinstance(yf_q, dict) else None
        if yf_q and "error" not in yf_q and yf_last not in (None, 0):
            return yf_q

        # Final fallback: Finnhub (if FINNHUB_API_KEY is configured)
        try:
            from trader.market.finnhub_client import get_quote as finnhub_get_quote
            fh_q = finnhub_get_quote(symbol)
            if fh_q and fh_q.get("last_price") not in (None, 0):
                return fh_q
        except Exception:
            if DEBUG:
                raise
        return yf_q if isinstance(yf_q, dict) else {}

    def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Batch real-time quotes. Tries Schwab batch, then yfinance, then Finnhub."""
        result: dict[str, dict[str, Any]] = {}
        if not symbols:
            return result
        if self._schwab.available:
            try:
                schwab_syms = [_schwab_symbol(s) for s in symbols]
                schwab_quotes = self._schwab.get_quotes(schwab_syms)
                for sym, qs in schwab_quotes.items():
                    d = qs.to_dict()
                    d["source"] = "schwab"
                    result[sym] = d
                if result:
                    return result
            except Exception:
                if DEBUG:
                    raise
        # Fallback 1: yfinance per-symbol
        missing: list[str] = []
        for sym in symbols:
            try:
                q = self._yfinance.get_quote(_yfinance_symbol(sym))
                last = q.get("last_price") if isinstance(q, dict) else None
                if q and "error" not in q and last not in (None, 0):
                    result[sym] = q
                else:
                    missing.append(sym)
            except Exception:
                missing.append(sym)

        # Fallback 2: Finnhub for symbols still missing a usable quote
        if missing:
            try:
                from trader.market.finnhub_client import get_quote as finnhub_get_quote

                for sym in missing:
                    try:
                        q = finnhub_get_quote(sym)
                        if q and q.get("last_price") not in (None, 0):
                            result[sym] = q
                    except Exception:
                        if DEBUG:
                            raise
            except Exception:
                if DEBUG:
                    raise
        return result

    def get_quotes_with_fundamentals(
        self,
        symbols: list[str],
        *,
        fill_avg_volume_from_history: bool = False,
        as_of: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Batch quotes + fundamentals with per-field Schwab/yfinance merge."""
        if not symbols:
            return {}

        result: dict[str, dict[str, Any]] = {sym: {} for sym in symbols}
        schwab_result: dict[str, dict[str, Any]] = {}

        if self._schwab.available:
            try:
                schwab_result = self._schwab.get_quotes_with_fundamentals(
                    [_schwab_symbol(s) for s in symbols]
                ) or {}
            except Exception:
                if DEBUG:
                    raise
                schwab_result = {}

        for sym in symbols:
            data = dict(schwab_result.get(sym) or {})
            if data:
                data["source"] = "schwab"
            result[sym] = data

        def _needs_yf_fill(entry: dict[str, Any]) -> bool:
            return any(
                entry.get(key) in (None, "")
                for key in ("last_price", "pe_ratio", "market_cap", "avg_10d_volume")
            )

        def _fill_pe_from_eps(entry: dict[str, Any]) -> None:
            if entry.get("pe_ratio") not in (None, ""):
                return
            eps = entry.get("eps")
            price = entry.get("last_price")
            try:
                if eps is None or price is None:
                    return
                eps_f = float(eps)
                price_f = float(price)
                if eps_f > 0:
                    entry["pe_ratio"] = price_f / eps_f
                elif eps_f <= 0:
                    # Treat non-positive EPS as a non-positive P/E bucket instead of
                    # leaving it indistinguishable from "unknown".
                    entry["pe_ratio"] = 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                return

        for sym in symbols:
            entry = result.get(sym) or {}
            if not _needs_yf_fill(entry):
                shares = entry.get("shares_outstanding")
                price = entry.get("last_price")
                if entry.get("market_cap") in (None, "") and shares and price:
                    entry["market_cap"] = shares * price
                _fill_pe_from_eps(entry)
                result[sym] = entry
                continue

            try:
                q = self._yfinance.get_quote(_yfinance_symbol(sym))
                f = self._yfinance.get_fundamentals(_yfinance_symbol(sym))
            except Exception:
                q = {}
                f = {}

            if q and "error" not in q:
                entry.setdefault("last_price", q.get("last_price"))
                entry.setdefault("net_pct_change", q.get("net_pct_change"))
                entry.setdefault("total_volume", q.get("total_volume"))

            if f and "error" not in f:
                if entry.get("pe_ratio") in (None, ""):
                    entry["pe_ratio"] = f.get("pe_ratio")
                if entry.get("market_cap") in (None, ""):
                    entry["market_cap"] = f.get("market_cap")
                if entry.get("avg_10d_volume") in (None, ""):
                    entry["avg_10d_volume"] = f.get("avg_volume")
                entry.setdefault("eps", f.get("eps"))
                entry.setdefault("shares_outstanding", f.get("shares_outstanding"))

            shares = entry.get("shares_outstanding")
            price = entry.get("last_price")
            if entry.get("market_cap") in (None, "") and shares and price:
                try:
                    entry["market_cap"] = float(shares) * float(price)
                except (TypeError, ValueError):
                    pass

            _fill_pe_from_eps(entry)

            if f and "error" not in f:
                entry["source"] = "schwab+yfinance" if schwab_result.get(sym) else "yfinance"
            elif not entry.get("source"):
                entry["source"] = "schwab" if schwab_result.get(sym) else None

            if entry:
                result[sym] = entry

        if fill_avg_volume_from_history:
            history_as_of = as_of or datetime.now(tz=timezone.utc)
            for sym in symbols:
                entry = result.get(sym) or {}
                if entry.get("avg_10d_volume") not in (None, ""):
                    continue
                avg_vol = self._compute_avg_volume_from_history(sym, as_of=history_as_of)
                if avg_vol is None:
                    continue
                entry["avg_10d_volume"] = avg_vol
                entry["source"] = f"{entry.get('source') or 'history'}+avg_volume_history"
                result[sym] = entry

        return {sym: data for sym, data in result.items() if data}

    def _compute_avg_volume_from_history(
        self,
        symbol: str,
        *,
        as_of: datetime,
        lookback_days: int = 10,
    ) -> float | None:
        """Compute average completed-day volume from cached/fetched 1-minute bars."""
        try:
            from trader.market.backtest import _get_ohlcv_1m
            from trader.snapshot_decision import compute_avg_daily_volume_from_bars
        except Exception:
            return None

        local_as_of = as_of.astimezone(timezone.utc) if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        end_date = local_as_of.date().isoformat()
        start_date = (local_as_of.date() - timedelta(days=max(30, lookback_days * 3))).isoformat()
        try:
            df = _get_ohlcv_1m(symbol.upper(), start_date, end_date)
        except Exception:
            return None
        return compute_avg_daily_volume_from_bars(
            df,
            decision_at=local_as_of.isoformat(),
            lookback_days=lookback_days,
        )

    def check_options_activity(self, symbol: str) -> dict[str, Any]:
        """Options activity — ATM IV, put/call ratios (Schwab only)."""
        result = self._schwab.check_options_activity(_schwab_symbol(symbol))
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
        result = self._schwab.check_price_spike(_schwab_symbol(symbol))
        result["source"] = "schwab"
        return result

    def check_volume_regime(self, symbol: str) -> dict[str, Any]:
        """Check for abnormal volume (Schwab only — needs intraday candles)."""
        result = self._schwab.check_volume_regime(_schwab_symbol(symbol))
        result["source"] = "schwab"
        return result

    def build_market_context(self) -> dict[str, Any]:
        """Build market-level context (SPY, VIX, session)."""
        result = self._schwab.build_market_context()
        result["source"] = "schwab"
        return result

    def build_price_context(self, symbols: list[str]) -> dict[str, Any]:
        """Build per-symbol price context for snapshot."""
        result = self._schwab.build_price_context([_schwab_symbol(s) for s in symbols])
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
            _yfinance_symbol(symbol), statement=statement, freq=freq, periods=periods,
        )
        result["source"] = "yfinance"
        return result

    def check_insider_activity(self, symbol: str) -> dict[str, Any]:
        """Insider transactions (Finnhub primary, yfinance title enrichment + fallback)."""
        from datetime import datetime, timezone

        try:
            from trader.market.finnhub_client import get_insider_transactions, _TX_CODE_MAP

            raw = get_insider_transactions(symbol.upper())
            if raw:
                # Best-effort title enrichment from yfinance
                title_map: dict[str, str] = {}
                try:
                    import yfinance as yf
                    yf_txns = yf.Ticker(_yfinance_symbol(symbol)).insider_transactions
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
        result = self._yfinance.check_insider_activity(_yfinance_symbol(symbol))
        result["source"] = "yfinance"
        return result

    def get_company_news(
        self, symbol: str, max_articles: int = 10, finnhub_days_back: int = 7,
    ) -> dict[str, Any]:
        """Recent company news articles (yfinance + Finnhub, merged)."""
        from datetime import datetime as _dt, timezone as _tz

        result = self._yfinance.get_company_news(_yfinance_symbol(symbol), max_articles=max_articles)

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

        articles = _fh_news(symbol.upper(), days_back=min(days_back, 7))
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
        result = get_technical_indicators(_yfinance_symbol(symbol), indicators, lookback_days=lookback_days)
        result["source"] = "stockstats"
        return result

    def get_current_technicals(
        self,
        symbol: str,
        indicators: list[str] | None = None,
    ) -> dict[str, Any]:
        """Quick snapshot of current technical indicator values."""
        from trader.market.indicators import get_current_technicals
        result = get_current_technicals(_yfinance_symbol(symbol), indicators)
        result["source"] = "stockstats"
        return result
