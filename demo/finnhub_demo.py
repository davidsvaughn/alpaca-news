#!/usr/bin/env python3
"""Finnhub API demo — shows all 6 free-tier endpoints we use.

Usage:
    uv run python demo/finnhub_demo.py [SYMBOL]

Requires: FINNHUB_API_KEY in .env or environment.
See docs/src/FINNHUB.md for full documentation.
"""

import json
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path so `trader` is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Import our Finnhub client functions directly
# ---------------------------------------------------------------------------
from trader.market.finnhub_client import (
    format_earnings_for_prompt,
    format_metrics_for_prompt,
    format_news_for_prompt,
    get_company_news,
    get_earnings_calendar,
    get_earnings_surprises,
    get_insider_transactions,
    get_recommendation_trends,
    get_stock_metrics,
)

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "NVDA"


def pp(label: str, data):
    """Pretty-print a section."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(json.dumps(data, indent=2, default=str))


def main():
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        print("ERROR: Set FINNHUB_API_KEY in .env or environment")
        sys.exit(1)

    print(f"Finnhub Demo — Symbol: {SYMBOL}")
    print(f"API Key: {key[:6]}...{key[-4:]}")

    # 1. Company News
    news = get_company_news(SYMBOL, days_back=3)
    pp(f"Company News (last 3 days) — {len(news)} articles", news[:3])

    # 2. Earnings Surprises
    surprises = get_earnings_surprises(SYMBOL, limit=4)
    pp(f"Earnings Surprises (last 4 quarters)", surprises)

    # 3. Earnings Calendar
    calendar = get_earnings_calendar(SYMBOL)
    pp(f"Earnings Calendar", calendar[:3])

    # 4. Analyst Recommendations
    recs = get_recommendation_trends(SYMBOL)
    pp(f"Analyst Recommendations (last 6 months)", recs[:6])

    # 5. Stock Metrics (growth, valuation, relative performance)
    metrics = get_stock_metrics(SYMBOL)
    pp(f"Stock Metrics (cherry-picked)", metrics)

    # 6. Insider Transactions (SEC Form 4 filings)
    insider_txns = get_insider_transactions(SYMBOL)
    pp(f"Insider Transactions — {len(insider_txns)} records (first 5)", insider_txns[:5])

    # Show how the pipeline formats this for agent prompts
    print(f"\n{'='*60}")
    print(f"  Formatted for Agent Prompt")
    print(f"{'='*60}")

    if news:
        print("\n--- News Context ---")
        print(format_news_for_prompt(news, max_articles=5))

    if surprises or calendar:
        print("\n--- Earnings Context ---")
        print(format_earnings_for_prompt(surprises, calendar))

    if metrics:
        print("\n--- Growth & Valuation ---")
        print(format_metrics_for_prompt(metrics))

    # Summary stats
    print(f"\n{'='*60}")
    print(f"  Summary")
    print(f"{'='*60}")
    print(f"  News articles:    {len(news)}")
    print(f"  Earnings records: {len(surprises)}")
    print(f"  Calendar entries: {len(calendar)}")
    print(f"  Recommendation periods: {len(recs)}")
    print(f"  Metric fields:    {len(metrics)}")
    print(f"  Insider txns:     {len(insider_txns)}")


if __name__ == "__main__":
    main()
