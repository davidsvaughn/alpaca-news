#!/usr/bin/env python3
"""Schwab (schwabdev) API demo — shows all endpoints we use.

Usage:
    uv run python demo/schwab_demo.py [SYMBOL]

Requires: SCHWAB_APP_KEY + SCHWAB_APP_SECRET in .env or environment.
Set SCHWAB_DISABLED=true to skip (demo will show what would happen).
See docs/src/SCHWABDEV.md for full documentation.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from trader.market.schwab_client import SchwabMarketClient

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "AAPL"


def pp(label: str, data):
    """Pretty-print a section."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    if hasattr(data, "to_dict"):
        print(json.dumps(data.to_dict(), indent=2, default=str))
    elif isinstance(data, list) and data and hasattr(data[0], "t"):
        # list of Candle dataclasses
        for c in data[:5]:
            print(f"  {c.t}  O={c.o:.2f}  H={c.h:.2f}  L={c.l:.2f}  C={c.c:.2f}  V={c.v}")
        if len(data) > 5:
            print(f"  ... ({len(data)} total candles)")
    else:
        print(json.dumps(data, indent=2, default=str))


def main():
    client = SchwabMarketClient()

    if not client.available:
        print("Schwab client is not available.")
        print("Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET in .env,")
        print("or set SCHWAB_DISABLED=true to explicitly disable.")
        sys.exit(1)

    print(f"Schwab Demo — Symbol: {SYMBOL}")
    print(f"Client available: {client.available}")

    # 1. Real-time quote
    quote = client.get_quote(SYMBOL)
    if quote:
        pp(f"Quote: {SYMBOL}", quote)
    else:
        print(f"\nNo quote data for {SYMBOL}")

    # 2. Intraday candles (1-min bars)
    candles = client.get_intraday_candles(SYMBOL)
    pp(f"Intraday 1-Min Candles ({len(candles)} bars)", candles)

    # 3. Options activity
    options = client.check_options_activity(SYMBOL)
    pp(f"Options Activity", options)

    # 4. Fundamentals
    fundamentals = client.get_fundamentals(SYMBOL)
    pp(f"Fundamentals", fundamentals)

    # 5. Market movers
    movers = client.get_movers("$SPX", "up")
    pp(f"S&P 500 Top Gainers", movers)

    # 6. Market hours
    hours = client.get_market_hours("equity")
    pp(f"Market Hours (equity)", hours)

    # 7. Derived: price spike detection
    spike = client.check_price_spike(SYMBOL)
    pp(f"Price Spike Check", spike)

    # 8. Derived: volume regime
    volume = client.check_volume_regime(SYMBOL)
    pp(f"Volume Regime", volume)

    # 9. Derived: full price context (used in pipeline)
    price_ctx = client.build_price_context([SYMBOL])
    pp(f"Price Context (pipeline format)", price_ctx)

    # 10. Derived: market context (SPY, VIX, session)
    market_ctx = client.build_market_context()
    pp(f"Market Context (SPY + VIX + session)", market_ctx)


if __name__ == "__main__":
    main()
