"""Prompt / message construction for the explorer pipeline.

Builds the user-facing message from a news event, including:
- News event details (headline, summary, symbols, URL, article content)
- Pre-fetched FinnHub context (recent news, earnings surprises/calendar)
- Pre-fetched market data (price, fundamentals, technicals, options, volume, insider, analyst)

These are pure functions with no dependency on PydanticAI or agent internals.
"""

from __future__ import annotations

import os
from typing import Any

from trader.market.data_service import MarketDataService


def build_user_message(
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
    event_lines: list[str] = []
    if news.get("headline"):
        event_lines.append(f"**Headline:** {news['headline']}")
    if news.get("summary"):
        event_lines.append(f"**Summary:** {news['summary']}")
    if symbols:
        event_lines.append(f"**Symbols:** {', '.join(symbols)}")
    if news.get("source"):
        event_lines.append(f"**Source:** {news['source']}")
    if news.get("created_at"):
        age_min = news.get("news_age_minutes")
        ts_source = news.get("news_timestamp_source", "")
        if age_min is not None:
            age_min = float(age_min)
            if age_min < 2:
                age_str = "just now"
            elif age_min < 60:
                age_str = f"{age_min:.0f} minutes ago"
            elif age_min < 1440:
                age_str = f"{age_min / 60:.1f} hours ago"
            else:
                age_str = f"{age_min / 1440:.1f} days ago"
            if ts_source == "benzinga_rewrite":
                # Benzinga rewrites wire stories; their timestamp can lag
                # the actual event by minutes to hours.
                event_lines.append(
                    f"**Published:** {news['created_at']} ({age_str})"
                    " — NOTE: this is the Benzinga publication time;"
                    " the underlying event may be older"
                )
            else:
                event_lines.append(f"**Published:** {news['created_at']} ({age_str})")
        else:
            event_lines.append(f"**Published:** {news['created_at']}")
    if news.get("url"):
        event_lines.append(f"**URL:** {news['url']}")
    if event_lines:
        # Force hard markdown line breaks so each field is shown on its own line.
        parts.append("  \n".join(event_lines))
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
        prefetch = prefetch_market_data(symbols, market)
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


def _period_returns(bars: list[dict[str, Any]], current_price: float) -> list[tuple[str, str]]:
    """Compute percentage returns at standard timeframes from daily OHLCV bars.

    Returns a list of (label, value) pairs, e.g. [("3D", "+1.2%"), ("1W", "-2.3%")].
    """
    if not bars or current_price <= 0:
        return []
    # bars are chronological (oldest first); find closes at approximate offsets
    n = len(bars)
    periods = [("3D", 3), ("1W", 5), ("1M", 21), ("3M", 63), ("1Y", 252)]
    results = []
    for label, offset in periods:
        idx = n - offset
        if idx < 0:
            continue
        old_close = bars[idx].get("c", 0)
        if old_close and old_close > 0:
            ret = (current_price - old_close) / old_close * 100
            results.append((label, f"{ret:+.1f}%"))
    return results


def prefetch_market_data(symbols: list[str], market: MarketDataService) -> str:
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

                line1 = f"${current_price:.4g}"
                if vol:
                    line1 += f" | Vol: {vol_str}"

                # Fetch 1Y daily bars for period returns
                returns_pairs: list[tuple[str, str]] = []
                try:
                    hist = market.get_price_history(sym, period="1y", interval="1d")
                    bars = hist.get("bars", []) if isinstance(hist, dict) else []
                    if bars and current_price > 0:
                        returns_pairs = _period_returns(bars, current_price)
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
                                range_str = f"${w52_low:.2f}–${w52_high:.2f} (at {pct_of_range:.0f}% — {pos})"
                except Exception:
                    pass

                rows = [
                    "| Metric | Value |",
                    "|--------|-------|",
                    f"| Price | {line1} |",
                ]
                if change_pct and isinstance(change_pct, (int, float)):
                    rows.append(f"| 1D | {change_pct:+.2f}% |")
                for label, val in returns_pairs:
                    rows.append(f"| {label} | {val} |")
                if range_str:
                    rows.append(f"| 52W range | {range_str} |")
                sym_sections.append(f"### {sym} — Price & Trend\n" + "\n".join(rows))
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
                    (["pb_ratio", "pbRatio"], "P/B"),
                    (["eps"], "EPS"),
                    (["beta"], "Beta"),
                    (["dividend_yield", "dividendYield"], "Div Yield"),
                    (["net_profit_margin", "profit_margin"], "Net Margin"),
                    (["return_on_equity"], "ROE"),
                    (["debt_to_equity"], "Debt/Equity"),
                    (["eps_change_pct_ttm"], "EPS Growth"),
                    (["rev_change_pct_ttm"], "Rev Growth"),
                    (["short_int_to_float"], "Short % Float"),
                    (["short_int_days_to_cover"], "Short Days"),
                    (["sector"], "Sector"),
                    (["industry"], "Industry"),
                ]
                # Labels that should be skipped when value is 0
                _skip_zero = {"Div Yield", "Short % Float", "Short Days"}
                # Labels displayed as percentages
                _pct_labels = {"Div Yield", "Net Margin", "ROE", "EPS Growth",
                               "Rev Growth", "Short % Float"}
                for keys, label in field_map:
                    val = next((fund[k] for k in keys if fund.get(k) not in (None, "")), None)
                    if val is None or val == "":
                        continue
                    if label in _skip_zero and val == 0.0:
                        continue
                    if label == "Market Cap" and isinstance(val, (int, float)):
                        if val > 1e9:
                            parts.append(f"{label}: ${val/1e9:.1f}B")
                        elif val > 1e6:
                            parts.append(f"{label}: ${val/1e6:.1f}M")
                        else:
                            parts.append(f"{label}: ${val:,.0f}")
                    elif label in _pct_labels and isinstance(val, (int, float)):
                        parts.append(f"{label}: {val:.1f}%")
                    elif label in ("Debt/Equity", "P/B", "Short Days") and isinstance(val, (int, float)):
                        parts.append(f"{label}: {val:.1f}")
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

                tech_rows = [
                    "| Indicator | Value | Signal |",
                    "|-----------|-------|--------|",
                ]

                # RSI with trend
                if "rsi" in techs and isinstance(techs["rsi"], (int, float)):
                    rsi = techs["rsi"]
                    signal = "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral"
                    trend_str = ""
                    rsi_hist = trend_data.get("rsi", [])
                    if len(rsi_hist) >= 2:
                        old_rsi = rsi_hist[0].get("value")
                        if old_rsi is not None:
                            direction = "rising" if rsi > old_rsi + 3 else "declining" if rsi < old_rsi - 3 else "flat"
                            trend_str = f" — {direction} from {old_rsi:.0f} over {len(rsi_hist)}d"
                    tech_rows.append(f"| RSI(14) | {rsi:.1f} | {signal}{trend_str} |")

                # MACD with crossover info
                if "macd" in techs and "macds" in techs:
                    macd_val = techs["macd"]
                    signal_val = techs["macds"]
                    if isinstance(macd_val, (int, float)) and isinstance(signal_val, (int, float)):
                        side = "bullish" if macd_val > signal_val else "bearish"
                        cross_str = ""
                        macd_hist = trend_data.get("macd", [])
                        if len(macd_hist) >= 2:
                            old_macd = macd_hist[0].get("value")
                            if old_macd is not None and (macd_val > 0) != (old_macd > 0):
                                cross_str = f", crossed zero in last {len(macd_hist)}d"
                        tech_rows.append(f"| MACD | {macd_val:.4f} | {side}{cross_str} |")

                # Bollinger with position percentage
                if "boll_ub" in techs and "boll_lb" in techs and "boll" in techs:
                    try:
                        ub = float(techs["boll_ub"])
                        lb = float(techs["boll_lb"])
                        mid = float(techs["boll"])
                        boll_range = ub - lb
                        if boll_range > 0 and current_price > 0:
                            pct = (current_price - lb) / boll_range * 100
                            pos = "near upper" if pct > 80 else "near lower" if pct < 20 else "mid-band"
                            tech_rows.append(f"| Bollinger | {pct:.0f}% of band | {pos} (${lb:.2f} / ${mid:.2f} / ${ub:.2f}) |")
                    except (TypeError, ValueError):
                        pass

                # Price vs SMAs
                for sma_key, sma_label in [("close_50_sma", "50-SMA"), ("close_200_sma", "200-SMA")]:
                    sma_val = techs.get(sma_key)
                    if sma_val and isinstance(sma_val, (int, float)) and current_price > 0 and sma_val > 0:
                        pct_diff = (current_price - sma_val) / sma_val * 100
                        side = "above" if pct_diff > 0 else "below"
                        tech_rows.append(f"| vs {sma_label} | {pct_diff:+.1f}% | {side} |")

                # ATR as % of price
                if "atr" in techs and isinstance(techs["atr"], (int, float)) and current_price > 0:
                    atr = techs["atr"]
                    atr_pct = atr / current_price * 100
                    vol_label = "high" if atr_pct > 5 else "low" if atr_pct < 1 else "moderate"
                    tech_rows.append(f"| ATR | ${atr:.4f} ({atr_pct:.1f}%) | {vol_label} volatility |")

                if len(tech_rows) > 2:  # more than just the header
                    sym_sections.append(f"### {sym} — Technical Summary\n" + "\n".join(tech_rows))
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

        # Volume delta (uptick/downtick pressure from 1-min bars)
        try:
            vdelta = market.compute_volume_delta(sym)
            if vdelta and not vdelta.get("error"):
                net = vdelta["net_delta"]
                imb = vdelta["imbalance"]
                direction = vdelta["direction"]
                uptick_pct = vdelta["uptick_volume"] / max(vdelta["uptick_volume"] + vdelta["downtick_volume"], 1) * 100
                # Format net delta with K/M suffix
                def _fmt_delta(n: int) -> str:
                    a = abs(n)
                    if a >= 1_000_000:
                        return f"{n/1e6:+.1f}M"
                    if a >= 1_000:
                        return f"{n/1e3:+.0f}K"
                    return f"{n:+,}"

                vd_rows = [
                    "| Day | Net Delta | Imbalance | Direction |",
                    "|-----|-----------|-----------|-----------|",
                    f"| Today | {_fmt_delta(net)} | {imb:+.2f} (uptick {uptick_pct:.0f}% / downtick {100-uptick_pct:.0f}%) | {direction} |",
                ]

                # Multi-day volume delta (yfinance 1-min bars, up to 5 days)
                try:
                    daily_deltas = market.compute_volume_delta_history(sym, days=5)
                    if daily_deltas and len(daily_deltas) > 1:
                        # Skip first entry if it's today (already shown above)
                        for d in daily_deltas[1:]:
                            d_net = d["net_delta"]
                            d_imb = d["imbalance"]
                            d_dir = d["direction"]
                            d_up_pct = (0.5 + d_imb / 2) * 100  # derive from imbalance
                            d_dn_pct = 100 - d_up_pct
                            vd_rows.append(
                                f"| {d['date']} | {_fmt_delta(d_net)} | "
                                f"{d_imb:+.2f} (uptick {d_up_pct:.0f}% / downtick {d_dn_pct:.0f}%) | {d_dir} |"
                            )
                except Exception:
                    pass

                sym_sections.append(f"### {sym} — Volume Delta\n" + "\n".join(vd_rows))
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

        # Analyst ratings — 6-month trend (FinnHub)
        try:
            from trader.market.finnhub_client import get_recommendation_trends
            trends = get_recommendation_trends(sym)
            if trends:
                ar_rows = [
                    "| Month | Strong Buy | Buy | Hold | Sell | Strong Sell | Total |",
                    "|-------|-----------|-----|------|------|------------|-------|",
                ]
                valid_count = 0
                for t in trends[:6]:
                    total = (t.get("strongBuy", 0) + t.get("buy", 0)
                             + t.get("hold", 0) + t.get("sell", 0)
                             + t.get("strongSell", 0))
                    if total <= 0:
                        continue
                    valid_count += 1
                    period = t.get("period", "?")
                    ar_rows.append(
                        f"| {period} | {t.get('strongBuy', 0)} | {t.get('buy', 0)} | "
                        f"{t.get('hold', 0)} | {t.get('sell', 0)} | "
                        f"{t.get('strongSell', 0)} | {total} |"
                    )
                if valid_count > 0:
                    # Compute buy-side trend
                    first = trends[0]
                    last = trends[min(len(trends) - 1, 5)]
                    buy_now = first.get("strongBuy", 0) + first.get("buy", 0)
                    buy_then = last.get("strongBuy", 0) + last.get("buy", 0)
                    delta = buy_now - buy_then
                    if delta > 0:
                        trend_str = f"\nTrend: Buy-side expanding (+{delta} over {valid_count} months)"
                    elif delta < 0:
                        trend_str = f"\nTrend: Buy-side contracting ({delta} over {valid_count} months)"
                    else:
                        trend_str = "\nTrend: Stable"
                    sym_sections.append(
                        f"### {sym} — Analyst Ratings ({valid_count}-month trend)\n"
                        + "\n".join(ar_rows) + trend_str
                    )
        except Exception:
            pass

        # Growth & Valuation metrics (FinnHub)
        try:
            from trader.market.finnhub_client import get_stock_metrics, format_metrics_for_prompt
            metrics = get_stock_metrics(sym)
            if metrics:
                formatted = format_metrics_for_prompt(metrics)
                if formatted:
                    sym_sections.append(f"### {sym} — Growth & Valuation\n{formatted}")
        except Exception:
            pass

        # Analyst price targets (yfinance)
        try:
            import yfinance as yf
            ticker = yf.Ticker(sym)
            targets = ticker.analyst_price_targets
            if targets is not None and hasattr(targets, "get"):
                mean_t = targets.get("mean") or targets.get("current")
                high_t = targets.get("high")
                low_t = targets.get("low")
                if mean_t:
                    parts_t = [f"Mean Target: ${mean_t:.2f}"]
                    if high_t:
                        parts_t.append(f"High: ${high_t:.2f}")
                    if low_t:
                        parts_t.append(f"Low: ${low_t:.2f}")
                    # Compute upside/downside vs current price
                    current = None
                    for s in sym_sections:
                        if "Current:" in s and sym in s:
                            import re
                            m = re.search(r"Current:\s*\$?([\d.]+)", s)
                            if m:
                                current = float(m.group(1))
                            break
                    if current and current > 0:
                        upside = (mean_t - current) / current * 100
                        parts_t.append(f"Upside to mean: {upside:+.1f}%")
                    sym_sections.append(f"### {sym} — Price Targets\n" + " | ".join(parts_t))
        except Exception:
            pass

        # Ownership summary (yfinance)
        try:
            import yfinance as yf
            ticker = yf.Ticker(sym)
            holders = ticker.major_holders
            if holders is not None and not holders.empty:
                own_lines = []
                for _, row in holders.iterrows():
                    own_lines.append(f"{row.iloc[1]}: {row.iloc[0]}")
                if own_lines:
                    sym_sections.append(f"### {sym} — Ownership\n" + " | ".join(own_lines))
        except Exception:
            pass

        # Peer companies (FinnHub)
        try:
            from trader.market.finnhub_client import get_company_peers
            peers = get_company_peers(sym)
            if peers:
                sym_sections.append(f"### {sym} — Peers\n{', '.join(peers[:8])}")
        except Exception:
            pass

        if sym_sections:
            sections.extend(sym_sections)

    # Market context (once, not per-symbol)
    try:
        mkt_ctx = market.build_market_context()
        if mkt_ctx and not mkt_ctx.get("error"):
            lines = []
            if "spy" in mkt_ctx:
                spy = mkt_ctx["spy"]
                spy_price = spy.get("last_price") or spy.get("lastPrice", "?")
                spy_chg = spy.get("net_pct_change") or spy.get("netPercentChange", "")
                line = f"SPY: ${spy_price}"
                if spy_chg and isinstance(spy_chg, (int, float)):
                    line += f" ({spy_chg:+.2f}%)"
                lines.append(line)
            if "vix" in mkt_ctx:
                vix = mkt_ctx["vix"]
                vix_price = vix.get("last_price") or vix.get("lastPrice", "?")
                lines.append(f"VIX: {vix_price}")
            session = mkt_ctx.get("session") or mkt_ctx.get("market_session", "")
            if session:
                lines.append(f"Session: {session}")
            if lines:
                sections.append(f"### Market Context\n" + " | ".join(lines))
    except Exception:
        pass

    if not sections:
        return ""
    return "\n\n## Pre-fetched market data\n\n" + "\n\n".join(sections)
