#!/usr/bin/env python3
"""yfinance demo — shows all data types we fetch (no API key needed).

Usage:
    uv run python demo/yfinance_demo.py [SYMBOL]

No API key required. See docs/src/YFINANCE.md for full documentation.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.market.indicators import get_current_technicals, get_technical_indicators
from trader.market.yfinance_client import YFinanceClient

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "NVDA"


def pp(label: str, data):
    """Pretty-print a section."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    # Truncate large output
    text = json.dumps(data, indent=2, default=str)
    lines = text.split("\n")
    if len(lines) > 40:
        print("\n".join(lines[:35]))
        print(f"  ... ({len(lines)} total lines, truncated)")
    else:
        print(text)


def main():
    yf = YFinanceClient()
    print(f"yfinance Demo — Symbol: {SYMBOL}")
    print("No API key required — all data is free.")

    # 1. Company Fundamentals
    fundamentals = yf.get_fundamentals(SYMBOL)
    pp("Company Fundamentals (ticker.info)", fundamentals)

    # 2. Insider Transactions
    insider = yf.check_insider_activity(SYMBOL)
    pp(f"Insider Activity ({insider.get('summary', {}).get('total_transactions', 0)} transactions)", insider)

    # 3. Price History (1 month, daily)
    history = yf.get_price_history(SYMBOL, period="1mo", interval="1d")
    pp(f"Price History (1mo daily, {history.get('bar_count', 0)} bars)", history)

    # 4. Financial Statements — Income
    income = yf.get_financial_statements(SYMBOL, statement="income", freq="quarterly")
    pp("Income Statement (quarterly)", income)

    # 5. Financial Statements — Balance Sheet
    balance = yf.get_financial_statements(SYMBOL, statement="balance_sheet", freq="quarterly")
    pp("Balance Sheet (quarterly)", balance)

    # 6. Financial Statements — Cash Flow
    cashflow = yf.get_financial_statements(SYMBOL, statement="cash_flow", freq="quarterly")
    pp("Cash Flow (quarterly)", cashflow)

    # 7. Company News
    news = yf.get_company_news(SYMBOL, max_articles=5)
    pp(f"Company News ({news.get('article_count', 0)} articles)", news)

    # 8. Technical Indicators — time series
    technicals = get_technical_indicators(
        SYMBOL,
        indicators=["rsi", "macd", "close_50_sma"],
        lookback_days=5,
    )
    pp("Technical Indicators (RSI, MACD, SMA50 — last 5 days)", technicals)

    # 9. Current Technicals — snapshot
    current = get_current_technicals(SYMBOL)
    pp("Current Technicals Snapshot", current)

    # Summary
    print(f"\n{'='*60}")
    print(f"  Data Sources Summary for {SYMBOL}")
    print(f"{'='*60}")
    print(f"  Sector:      {fundamentals.get('sector', 'N/A')}")
    print(f"  Industry:    {fundamentals.get('industry', 'N/A')}")
    print(f"  Market Cap:  ${fundamentals.get('market_cap', 0)/1e9:.1f}B")
    print(f"  P/E Ratio:   {fundamentals.get('pe_ratio', 'N/A')}")
    print(f"  RSI (14):    {current.get('rsi', 'N/A')}")
    print(f"  MACD Signal: {current.get('macd_signal', 'N/A')}")
    insider_signal = insider.get("summary", {}).get("signal", "N/A")
    print(f"  Insider:     {insider_signal}")


if __name__ == "__main__":
    main()
