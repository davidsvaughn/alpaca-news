#!/usr/bin/env python3
"""Import external brokerage holdings from a screenshot into a tracking portfolio.

Pipeline:
  1. Send the image to Gemini (multimodal) with a JSON schema and extract
     {symbol, name, qty} per holding.
  2. Fetch a fresh price for each symbol via Schwab (yfinance fallback).
  3. Show the user the result and ask to confirm.
  4. Create a tracking-mode LiveConfig + one Watch per holding.

The created portfolio is read-only:
  - mode="tracking"  → news pipeline skips it (no auto-buy)
  - tracking gate in LiveExitMonitor.run_cycle skips its watches (no auto-sell)
  - alpaca_account_id=None → no broker linkage at all

Usage:
    uv run python scripts/import_holdings.py <image_path> [--name "Vanguard IRA"]
                                               [--yes] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

# Project root on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from trader.config import load_settings
from trader.db.database import insert_live_config, insert_watch, open_sqlite
from trader.market.data_service import MarketDataService
from trader.models.live_config import LiveConfig
from trader.models.watch import WatchBuilder
from trader.valuation import extract_quote_price


GEMINI_MODEL = os.getenv("HOLDINGS_IMPORT_MODEL", "gemini-3-flash-preview")


class HoldingExtract(BaseModel):
    """One holding row from a brokerage screenshot."""

    symbol: str = Field(description="Ticker symbol (e.g. AAPL, GOOG)")
    name: str = Field(default="", description="Company or fund name as shown")
    qty: float = Field(description="Quantity / number of shares held")


class HoldingsExtract(BaseModel):
    """Full set of holdings extracted from one screenshot."""

    holdings: list[HoldingExtract]


EXTRACTION_PROMPT = """\
Extract every brokerage holding shown in this screenshot.

For each holding, return:
  - symbol: the ticker (e.g. "AAPL", "GOOG", "BRK.B"). Uppercase. No exchange suffix.
  - name: the company/fund name as shown (truncated names are fine).
  - qty: the share quantity (number of shares). Use the numeric value as-is.

Rules:
  - Skip header rows, section labels (e.g. "ETFs", "Stocks"), and totals/footers.
  - Do NOT include any "Total" or "Cash" row.
  - Do NOT invent symbols or quantities. If a value is unclear, omit that row.
  - Return one entry per holding in the order they appear.
"""


def extract_holdings(image_path: Path) -> list[HoldingExtract]:
    """Call Gemini with the image and structured-output schema."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit("GOOGLE_API_KEY is not set in the environment / .env")

    image_bytes = image_path.read_bytes()
    mime, _ = mimetypes.guess_type(str(image_path))
    if not mime or not mime.startswith("image/"):
        # Default to png if guess fails; Gemini accepts several types
        mime = "image/png"

    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=HoldingsExtract,
        ),
    )

    text = response.text or ""
    if not text.strip():
        raise SystemExit(f"Gemini returned empty output for {image_path}")

    parsed = HoldingsExtract.model_validate_json(text)
    return parsed.holdings


def fetch_fresh_prices(market: MarketDataService, symbols: list[str]) -> dict[str, float | None]:
    """Fetch one price per symbol via Schwab → yfinance fallback."""
    quotes = market.get_quotes(symbols)
    out: dict[str, float | None] = {}
    for sym in symbols:
        q = quotes.get(sym)
        if q is None:
            # Single-symbol retry for any miss (covers normalisation differences)
            q = market.get_quote(sym)
        out[sym] = extract_quote_price(q) if q else None
    return out


def render_table(rows: list[dict[str, object]]) -> str:
    """Plain-text table for confirmation output."""
    headers = ["Symbol", "Name", "Qty", "Price", "Value"]
    body = [
        [
            str(r["symbol"]),
            str(r["name"])[:30],
            f"{float(r['qty']):,.4f}",
            "—" if r["price"] is None else f"${float(r['price']):,.2f}",
            "—" if r["value"] is None else f"${float(r['value']):,.2f}",
        ]
        for r in rows
    ]
    widths = [
        max(len(h), *(len(row[i]) for row in body)) for i, h in enumerate(headers)
    ] if body else [len(h) for h in headers]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines.extend(fmt.format(*row) for row in body)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a brokerage screenshot into a read-only tracking portfolio.",
    )
    parser.add_argument("image_path", help="Path to the holdings screenshot (png/jpg).")
    parser.add_argument(
        "--name",
        default=None,
        help="Portfolio name (default: derived from image filename).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract + price, but do not write to the database.",
    )
    args = parser.parse_args()

    image_path = Path(args.image_path).expanduser().resolve()
    if not image_path.exists():
        raise SystemExit(f"Image not found: {image_path}")

    portfolio_name = args.name or f"Tracking — {image_path.stem}"

    print(f"Extracting holdings from {image_path} via {GEMINI_MODEL}…")
    holdings = extract_holdings(image_path)
    if not holdings:
        raise SystemExit("Gemini returned no holdings.")
    print(f"  → {len(holdings)} rows extracted")

    symbols = [h.symbol.upper().strip() for h in holdings]
    print(f"Fetching fresh prices for {len(symbols)} symbols (Schwab → yfinance)…")
    settings = load_settings()
    market = MarketDataService()
    prices = fetch_fresh_prices(market, symbols)

    rows: list[dict[str, object]] = []
    for h, sym in zip(holdings, symbols):
        price = prices.get(sym)
        value = (price * h.qty) if price else None
        rows.append({
            "symbol": sym,
            "name": h.name,
            "qty": h.qty,
            "price": price,
            "value": value,
        })

    total_value = sum(float(r["value"]) for r in rows if r["value"] is not None)
    missing = [r["symbol"] for r in rows if r["price"] is None]

    print()
    print(render_table(rows))
    print()
    print(f"Total value: ${total_value:,.2f}")
    if missing:
        print(f"WARNING: no price for: {', '.join(map(str, missing))}")
    print(f"Portfolio name: {portfolio_name}")
    print("Mode: tracking (read-only — no auto-trading)")

    if args.dry_run:
        print("\n[dry-run] Database not modified.")
        return 0

    if not args.yes:
        ans = input("\nCreate this tracking portfolio? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    if missing:
        # We refuse to silently create watches without entry prices — the user
        # should resolve the missing-quote case (rename symbol, retry, etc.)
        # before persisting. Fail loud rather than partial-write.
        raise SystemExit(
            "Refusing to create portfolio with missing prices. "
            "Re-run after resolving quote failures.",
        )

    db = open_sqlite(settings.sqlite_path)

    cfg = LiveConfig.create(
        name=portfolio_name,
        filters={},
        allocation="none",
        allocation_params={},
        starting_capital=round(total_value, 2),
        exit_strategy="",
        exit_params={},
        mode="tracking",
        paused=True,                  # defense in depth
        alpaca_account_id=None,       # no broker linkage
        alpaca_account_name=None,
    )
    cfg.active = True                  # show in dashboard right away
    if not insert_live_config(db, config=cfg.to_dict()):
        raise SystemExit(f"Failed to insert LiveConfig {cfg.config_id}")

    created = 0
    for r in rows:
        builder = WatchBuilder.create_from_manual_holding(
            symbol=str(r["symbol"]),
            qty=float(r["qty"]),
            entry_price=float(r["price"]),  # type: ignore[arg-type]
            live_config_id=cfg.config_id,
            name=str(r["name"]) or None,
        )
        watch_dict = builder.to_watch().to_dict()
        if insert_watch(db, watch=watch_dict):
            created += 1
        else:
            print(f"  ! duplicate watch_id for {r['symbol']}, skipped")

    print()
    print(f"Created tracking portfolio {cfg.config_id} ({portfolio_name})")
    print(f"  watches: {created}/{len(rows)}")
    print(f"  total value: ${total_value:,.2f}")
    print(f"  source image: {image_path}")
    print()
    print(json.dumps({"config_id": cfg.config_id, "watches": created}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
