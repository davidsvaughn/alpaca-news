"""Technical indicators computed locally from yfinance OHLCV data.

Uses ``stockstats`` to compute indicators from a yfinance-fetched DataFrame.
Zero cost — no API calls, no LLM involvement.

The LLM chooses which indicators are relevant for the current situation
rather than computing all of them every time.

Supported indicators:
  Trend:      close_50_sma, close_200_sma, close_10_ema
  Momentum:   macd, macds, macdh, rsi
  Volatility: boll, boll_ub, boll_lb, atr
  Volume:     vwma, mfi
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Indicator catalog
# ---------------------------------------------------------------------------

INDICATOR_INFO: dict[str, str] = {
    # Trend
    "close_50_sma": "50-day Simple Moving Average — medium-term trend direction",
    "close_200_sma": "200-day Simple Moving Average — long-term trend benchmark",
    "close_10_ema": "10-day Exponential Moving Average — responsive short-term trend",
    # Momentum
    "macd": "MACD line — momentum via EMA differences; crossovers signal trend changes",
    "macds": "MACD Signal line — EMA of MACD; crossovers trigger trades",
    "macdh": "MACD Histogram — gap between MACD and signal; momentum strength",
    "rsi": "RSI (14) — overbought (>70) / oversold (<30) momentum indicator",
    # Volatility
    "boll": "Bollinger Middle Band (20 SMA) — dynamic price benchmark",
    "boll_ub": "Bollinger Upper Band — potential overbought / breakout zone",
    "boll_lb": "Bollinger Lower Band — potential oversold / support zone",
    "atr": "Average True Range — volatility measure for stop-loss sizing",
    # Volume
    "vwma": "Volume-Weighted Moving Average — confirms trends with volume",
    "mfi": "Money Flow Index — overbought (>80) / oversold (<20) using price + volume",
}

VALID_INDICATORS = set(INDICATOR_INFO.keys())


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorResult:
    """Result of computing one or more technical indicators for a symbol."""
    symbol: str
    indicators: dict[str, list[dict[str, Any]]]  # indicator_name → time series
    lookback_days: int
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "indicators": self.indicators,
            "lookback_days": self.lookback_days,
            "fetched_at": self.fetched_at,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_technical_indicators(
    symbol: str,
    indicators: list[str],
    lookback_days: int = 30,
    history_period: str = "1y",
) -> dict[str, Any]:
    """Compute technical indicators for *symbol* over recent trading days.

    Args:
        symbol: Ticker symbol (e.g. "NVDA").
        indicators: List of indicator names from VALID_INDICATORS.
        lookback_days: How many recent trading days to return values for.
        history_period: How much history to fetch for accurate computation
            (needs enough data for the longest lookback window, e.g. 200-SMA
            needs >200 days). Default "1y" is safe for all indicators.

    Returns:
        Dict with per-indicator time series and metadata.
    """
    import pandas as pd
    import yfinance as yf
    from stockstats import wrap

    symbol = symbol.upper()
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    # Validate indicators
    bad = [i for i in indicators if i not in VALID_INDICATORS]
    if bad:
        return {
            "symbol": symbol,
            "error": f"Unknown indicators: {bad}. Valid: {sorted(VALID_INDICATORS)}",
            "fetched_at": now_iso,
        }

    try:
        # Fetch OHLCV from yfinance
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=history_period)

        if df.empty:
            return {
                "symbol": symbol,
                "error": f"No price data for {symbol}",
                "fetched_at": now_iso,
            }

        # Strip timezone for stockstats compatibility
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        # Reset index so Date becomes a column
        df = df.reset_index()

        # Wrap for stockstats
        ss = wrap(df)

        # Compute each indicator
        result_indicators: dict[str, list[dict[str, Any]]] = {}
        for ind in indicators:
            # Trigger computation
            ss[ind]

            # Extract recent values
            series_data: list[dict[str, Any]] = []
            tail = ss.tail(lookback_days)
            for _, row in tail.iterrows():
                date_val = row.get("Date", row.get("date", None))
                if hasattr(date_val, "isoformat"):
                    date_str = date_val.isoformat()[:10]
                elif hasattr(date_val, "strftime"):
                    date_str = date_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val)

                val = row[ind]
                if pd.isna(val):
                    continue
                series_data.append({
                    "date": date_str,
                    "value": round(float(val), 4),
                })

            result_indicators[ind] = series_data

        result = IndicatorResult(
            symbol=symbol,
            indicators=result_indicators,
            lookback_days=lookback_days,
            fetched_at=now_iso,
        )
        return result.to_dict()

    except Exception as e:
        if DEBUG:
            raise
        return {
            "symbol": symbol,
            "error": str(e),
            "fetched_at": now_iso,
        }


def get_current_technicals(
    symbol: str,
    indicators: list[str] | None = None,
) -> dict[str, Any]:
    """Get the most recent value of each indicator — a quick snapshot.

    If *indicators* is None, computes a default set: RSI, MACD, Bollinger
    position, and ATR. Useful for injecting into Snapshot ``data_modalities``
    without a full time series.

    Returns a flat dict like:
        {"rsi_14": 68.3, "macd_signal": "bullish_crossover", ...}
    """
    import pandas as pd
    import yfinance as yf
    from stockstats import wrap

    symbol = symbol.upper()

    if indicators is None:
        indicators = ["rsi", "macd", "macds", "boll", "boll_ub", "boll_lb", "atr"]

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="6mo")

        if df.empty:
            return {"symbol": symbol, "error": "no data"}

        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        df = df.reset_index()
        ss = wrap(df)

        # Compute all requested indicators
        for ind in indicators:
            if ind in VALID_INDICATORS:
                ss[ind]

        last = ss.iloc[-1]
        close = float(last.get("Close", last.get("close", 0)))
        result: dict[str, Any] = {"symbol": symbol}

        for ind in indicators:
            if ind not in VALID_INDICATORS:
                continue
            val = last.get(ind)
            if val is not None and not pd.isna(val):
                result[ind] = round(float(val), 4)

        # Derived signals (if we have the data)
        if "rsi" in result:
            rsi = result["rsi"]
            if rsi > 70:
                result["rsi_signal"] = "overbought"
            elif rsi < 30:
                result["rsi_signal"] = "oversold"
            else:
                result["rsi_signal"] = "neutral"

        if "macd" in result and "macds" in result:
            if result["macd"] > result["macds"]:
                result["macd_signal"] = "bullish"
            else:
                result["macd_signal"] = "bearish"

        if "boll_ub" in result and "boll_lb" in result and close > 0:
            boll_ub = result["boll_ub"]
            boll_lb = result["boll_lb"]
            boll_range = boll_ub - boll_lb
            if boll_range > 0:
                position = (close - boll_lb) / boll_range
                result["bollinger_pct"] = round(position, 4)
                if position > 0.95:
                    result["bollinger_signal"] = "upper_band"
                elif position < 0.05:
                    result["bollinger_signal"] = "lower_band"
                else:
                    result["bollinger_signal"] = "mid_range"

        result["fetched_at"] = datetime.now(tz=timezone.utc).isoformat()
        return result

    except Exception as e:
        if DEBUG:
            raise
        return {"symbol": symbol, "error": str(e)}
