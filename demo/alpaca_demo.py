#!/usr/bin/env python3
"""Alpaca News API demo — shows both REST and WebSocket news access.

Usage:
    # REST: fetch recent news (non-blocking, exits after printing)
    uv run python demo/alpaca_demo.py [SYMBOL]

    # WebSocket: stream live news (blocking, Ctrl+C to stop)
    uv run python demo/alpaca_demo.py --stream

Requires: ALPACA_API_KEY + ALPACA_SECRET_KEY in .env or environment.
See docs/src/ALPACA.md for full documentation.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")


def pp(label: str, data):
    """Pretty-print a section."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    text = json.dumps(data, indent=2, default=str)
    lines = text.split("\n")
    if len(lines) > 50:
        print("\n".join(lines[:45]))
        print(f"  ... ({len(lines)} total lines, truncated)")
    else:
        print(text)


def demo_rest(symbol: str):
    """Fetch recent news via REST API (the way we'd backfill)."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    print(f"Alpaca REST News Demo — Symbol: {symbol}")

    news_client = NewsClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    # Fetch last 7 days of news
    request = NewsRequest(
        symbols=symbol,
        start=datetime.now(tz=timezone.utc) - timedelta(days=7),
        limit=10,
        include_content=True,
    )
    response = news_client.get_news(request)

    articles = []
    for article in response.news:
        a = {
            "id": article.id,
            "headline": article.headline,
            "source": article.source,
            "author": article.author,
            "created_at": article.created_at.isoformat() if article.created_at else None,
            "symbols": article.symbols,
            "url": article.url,
            "summary": (article.summary or "")[:200],
            "has_content": bool(article.content),
        }
        articles.append(a)

    pp(f"Recent News for {symbol} ({len(articles)} articles)", articles)

    # Show the raw fields that feed into our Trigger
    if articles:
        first = articles[0]
        print(f"\n{'='*60}")
        print(f"  How This Becomes a Trigger")
        print(f"{'='*60}")
        print(f'  type:             "alpaca_news"')
        print(f'  alpaca_timestamp: "{first["created_at"]}"')
        print(f'  headline:         "{first["headline"]}"')
        print(f'  source:           "{first["source"]}"')
        print(f'  symbols:          {first["symbols"]}')
        print(f'  source_file:      "<timestamp>_{first["id"]}.json"')


def demo_stream():
    """Stream live news via WebSocket (blocking — Ctrl+C to stop)."""
    from alpaca.data.live import NewsDataStream

    print("Alpaca WebSocket News Stream Demo")
    print("Listening for all news... (Ctrl+C to stop)\n")

    async def handler(article):
        ts = article.created_at.strftime("%H:%M:%S") if article.created_at else "?"
        symbols = ", ".join(article.symbols) if article.symbols else "none"
        print(f"[{ts}] ({article.source}) [{symbols}]")
        print(f"  {article.headline}")
        print()

    stream = NewsDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    stream.subscribe_news(handler, "*")

    try:
        stream.run()
    except KeyboardInterrupt:
        print("\nStream stopped.")


def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("ERROR: Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        sys.exit(1)

    if "--stream" in sys.argv:
        demo_stream()
    else:
        symbol = next((a for a in sys.argv[1:] if not a.startswith("-")), "AAPL")
        demo_rest(symbol)


if __name__ == "__main__":
    main()
