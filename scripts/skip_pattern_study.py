"""
Skip Pattern Study: Rigorous analysis of headline patterns vs subsequent stock returns.

Scans the insight_sentry news archive, matches headlines against patterns,
fetches price data, and computes max-gain / max-drawdown over rolling windows.

Usage:
    uv run python scripts/skip_pattern_study.py [--days 10] [--workers 8]
"""

import argparse
import csv
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Add project root to path for trader imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np
from trader.market.alpaca_env import get_alpaca_account_env

# ── Config ──────────────────────────────────────────────────────────────────

ARCHIVE_DIR = Path("data/news/archive/insight_sentry")
INCOMING_DIR = Path("data/news/incoming/insight_sentry")
OUTPUT_CSV = Path("data/skip_pattern_study.csv")
SUMMARY_CSV = Path("data/skip_pattern_summary.csv")

# US equity exchange prefixes (insight_sentry uses these)
US_EXCHANGE_PREFIXES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS", "OTC"}

# Windows to evaluate (in market-hours minutes approx)
WINDOWS = {
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "8h": timedelta(hours=8),
    "1d": timedelta(days=1),
    "2d": timedelta(days=2),
}

# Headline patterns to test
PATTERNS = {
    # Currently active skip patterns (should these stay?)
    "maintains_reiterate": re.compile(
        r"(maintains|reiterates|reiterated).*(price target|outperform|in-line|buy|neutral|sector perform|overweight|rating)",
        re.I,
    ),
    "initiated_coverage": re.compile(r"initiat.* coverage", re.I),
    "shares_trading_lower": re.compile(r"shares are trading lower", re.I),
    # Currently commented out
    "shares_trading_higher": re.compile(r"shares are trading higher", re.I),
    "whats_going_on": re.compile(r"what's going on with .* stock", re.I),
    # Proposed new patterns
    "raised_to_from": re.compile(r"raised to .* from", re.I),
    "upgraded_to": re.compile(r"upgrade[sd]? .* to (buy|outperform|overweight)", re.I),
    "stock_rallies_surges": re.compile(r"stock (rallies|surges|jumps)", re.I),
    "shares_rise_jump_surge": re.compile(
        r"shares (rise|jump|surge|climb|rally)", re.I
    ),
    "retail_sees_cheers": re.compile(r"retail (sees|cheers)", re.I),
    "reports_results_quarter": re.compile(
        r"reports results for the quarter", re.I
    ),
    # Catalyst-type patterns (for comparison, not skip candidates)
    "FDA_phase_trial": re.compile(
        r"(FDA|Phase \d|clinical trial|breakthrough therapy)", re.I
    ),
    "earnings_beat": re.compile(r"(earnings|EPS) beat", re.I),
    "guidance_outlook": re.compile(r"(guidance|outlook) ", re.I),
    "buyback_repurchase": re.compile(r"(buyback|repurchase|share buy)", re.I),
    "acquisition_merger": re.compile(r"(acqui|merger|takeover|buyout)", re.I),
    "contract_order_win": re.compile(
        r"(wins? .* contract|contract win|order.* \$\d)", re.I
    ),
}

# Exchange prefixes to strip
_EXCHANGE_RE = re.compile(r"^([A-Z_]{2,10}):")


def parse_symbol(raw: str) -> tuple[str | None, str]:
    """Parse 'NYSE:AAPL' -> ('NYSE', 'AAPL'). Bare 'AAPL' -> (None, 'AAPL')."""
    m = _EXCHANGE_RE.match(str(raw).strip())
    if m:
        prefix = m.group(1)
        sym = str(raw).strip()[m.end():]
        return prefix, sym.strip().upper()
    return None, str(raw).strip().upper()


def is_us_equity(raw: str) -> tuple[bool, str]:
    """Check if symbol is a US-traded equity based on exchange prefix.

    Returns (is_us, clean_symbol).
    All insight_sentry symbols have prefixes — only keep US exchange ones.
    """
    prefix, sym = parse_symbol(raw)

    if not sym or len(sym) > 6 or not sym.isalpha():
        return False, sym

    # Skip currencies and crypto
    for bad in ["USD", "EUR", "GBP", "JPY", "BTC", "ETH", "XRP", "SOL", "DOGE"]:
        if sym == bad:
            return False, sym

    # If prefix is present, it must be a US exchange
    if prefix is not None:
        return prefix in US_EXCHANGE_PREFIXES, sym

    # No prefix — ambiguous, but include (rare in insight_sentry)
    return True, sym


def load_alpaca_tradeable_symbols() -> set[str] | None:
    """Load set of tradeable US equity symbols from Alpaca API.

    Returns None if Alpaca credentials aren't available.
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus

        api_key = get_alpaca_account_env("ALPACA_API_KEY", 1)
        secret_key = get_alpaca_account_env("ALPACA_SECRET_KEY", 1)
        if not api_key or not secret_key:
            return None

        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        assets = client.get_all_assets(
            filter=GetAssetsRequest(
                status=AssetStatus.ACTIVE,
                asset_class=AssetClass.US_EQUITY,
            )
        )
        tradeable = {a.symbol for a in assets if a.tradable}
        print(f"Loaded {len(tradeable)} tradeable US equity symbols from Alpaca")
        return tradeable
    except Exception as e:
        print(f"Warning: could not load Alpaca assets: {e}")
        return None


# ── Step 1: Extract articles ───────────────────────────────────────────────


def load_articles(max_days: int = 14) -> list[dict]:
    """Load all insight_sentry articles from archive + incoming."""
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)

    # Archive zips
    if ARCHIVE_DIR.exists():
        for zf_path in sorted(ARCHIVE_DIR.glob("*.zip")):
            with zipfile.ZipFile(zf_path) as zf:
                for name in zf.namelist():
                    if not name.endswith(".json"):
                        continue
                    try:
                        data = json.loads(zf.read(name))
                        ts = data.get("published_at", 0)
                        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                        if dt < cutoff:
                            continue
                        data["_dt"] = dt
                        articles.append(data)
                    except Exception:
                        pass

    # Incoming (not yet archived)
    if INCOMING_DIR.exists():
        for fp in INCOMING_DIR.glob("*.json"):
            try:
                data = json.loads(fp.read_text())
                ts = data.get("published_at", 0)
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                if dt < cutoff:
                    continue
                data["_dt"] = dt
                articles.append(data)
            except Exception:
                pass

    print(f"Loaded {len(articles)} articles")
    return articles


# ── Step 2: Extract symbols & match patterns ──────────────────────────────


def process_articles(
    articles: list[dict], alpaca_symbols: set[str] | None = None
) -> list[dict]:
    """Extract symbols, match patterns, return enriched records."""
    records = []
    prefix_stats: dict[str, int] = defaultdict(int)
    rejected_prefix = 0
    rejected_alpaca = 0

    for art in articles:
        title = art.get("title", "")
        source = art.get("source", "unknown")
        dt = art["_dt"]

        # Extract symbols
        raw_syms = art.get("related_symbols", []) or []
        symbols = []
        for s in raw_syms:
            is_us, sym = is_us_equity(str(s))
            prefix, _ = parse_symbol(str(s))
            prefix_stats[prefix or "NONE"] += 1

            if not is_us:
                rejected_prefix += 1
                continue

            # If we have Alpaca list, validate against it
            if alpaca_symbols is not None and sym not in alpaca_symbols:
                rejected_alpaca += 1
                continue

            symbols.append(sym)

        if not symbols:
            continue  # skip articles with no US equity symbols

        # Match patterns
        matched = []
        for pname, pat in PATTERNS.items():
            if pat.search(title):
                matched.append(pname)

        # One record per symbol
        for sym in symbols[:3]:  # limit to first 3 symbols per article
            records.append(
                {
                    "symbol": sym,
                    "title": title[:200],
                    "source": source,
                    "dt": dt,
                    "patterns": matched,
                    "pattern_str": "|".join(matched) if matched else "none",
                }
            )

    # Print filtering stats
    us_prefixes = sum(v for k, v in prefix_stats.items() if k in US_EXCHANGE_PREFIXES)
    total_syms = sum(prefix_stats.values())
    print(f"\nSymbol filtering stats:")
    print(f"  Total symbol refs: {total_syms}")
    print(f"  US exchange prefix: {us_prefixes} ({100*us_prefixes/max(total_syms,1):.1f}%)")
    print(f"  Rejected (non-US prefix): {rejected_prefix}")
    if alpaca_symbols is not None:
        print(f"  Rejected (not in Alpaca): {rejected_alpaca}")
    print(f"  Top prefixes: {dict(sorted(prefix_stats.items(), key=lambda x: -x[1])[:10])}")
    print(f"\nExtracted {len(records)} symbol-article pairs")
    return records


# ── Step 3: Fetch price data ──────────────────────────────────────────────


def _init_schwab():
    """Initialize SchwabMarketClient (singleton)."""
    try:
        from trader.market.schwab_client import SchwabMarketClient
        client = SchwabMarketClient()
        if client.available:
            return client
    except Exception as e:
        print(f"Warning: Schwab unavailable: {e}")
    return None


def fetch_prices_schwab(
    schwab, sym: str, start: datetime, end: datetime
) -> pd.DataFrame | None:
    """Fetch 5-min bars from Schwab. Returns DataFrame with UTC index."""
    try:
        candles = schwab.get_candles_by_date_range(
            sym,
            start=start - timedelta(hours=1),
            end=end + timedelta(days=3),
            frequency=5,
            extended_hours=False,
        )
        if not candles:
            return None
        rows = [{"Open": c.o, "High": c.h, "Low": c.l, "Close": c.c, "Volume": c.v,
                 "dt": pd.Timestamp(c.t)} for c in candles]
        df = pd.DataFrame(rows).set_index("dt")
        df.index = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
        return df if len(df) > 0 else None
    except Exception:
        return None


def fetch_prices_yfinance(sym: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    """Fallback: fetch 5-min bars from yfinance."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(sym)
        df = ticker.history(
            start=start - timedelta(hours=1),
            end=end + timedelta(days=3),
            interval="5m",
            prepost=False,
        )
        if df is None or df.empty:
            return None
        df.index = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
        return df
    except Exception:
        return None


def fetch_all_prices(
    symbols: list[str], records: list[dict], workers: int = 8
) -> dict[str, pd.DataFrame]:
    """Fetch price data for all symbols: Schwab primary, yfinance fallback."""
    # Determine date range per symbol
    sym_ranges: dict[str, tuple[datetime, datetime]] = {}
    for rec in records:
        sym = rec["symbol"]
        dt = rec["dt"]
        if sym not in sym_ranges:
            sym_ranges[sym] = (dt, dt)
        else:
            mn, mx = sym_ranges[sym]
            sym_ranges[sym] = (min(mn, dt), max(mx, dt))

    schwab = _init_schwab()
    source_label = "Schwab (yfinance fallback)" if schwab else "yfinance only"
    print(f"Fetching price data for {len(symbols)} symbols via {source_label} ({workers} workers)...")

    prices: dict[str, pd.DataFrame] = {}
    failed = []
    schwab_ok = 0
    yf_ok = 0

    def _fetch(sym):
        mn, mx = sym_ranges[sym]
        # Try Schwab first
        if schwab:
            df = fetch_prices_schwab(schwab, sym, mn, mx)
            if df is not None:
                return sym, df, "schwab"
        # Fallback to yfinance
        df = fetch_prices_yfinance(sym, mn, mx)
        if df is not None:
            return sym, df, "yfinance"
        return sym, None, "failed"

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            done += 1
            if done % 50 == 0:
                print(f"  ... {done}/{len(symbols)} symbols fetched")
            try:
                sym, df, src = fut.result()
                if df is not None and len(df) > 0:
                    prices[sym] = df
                    if src == "schwab":
                        schwab_ok += 1
                    else:
                        yf_ok += 1
                else:
                    failed.append(sym)
            except Exception:
                failed.append(futures[fut])

    print(f"Got price data for {len(prices)} symbols ({schwab_ok} Schwab, {yf_ok} yfinance), {len(failed)} failed")
    return prices


# ── Step 4: Compute returns ───────────────────────────────────────────────


def compute_returns(
    rec: dict, price_df: pd.DataFrame
) -> dict | None:
    """Compute max gain and max drawdown for each window after article time."""
    dt = rec["dt"]

    # Find the entry price: first bar AT or AFTER article time
    mask = price_df.index >= dt
    future = price_df.loc[mask]
    if future.empty or len(future) < 2:
        return None

    entry_price = future.iloc[0]["Open"]
    if entry_price <= 0 or pd.isna(entry_price):
        return None

    result = {
        "symbol": rec["symbol"],
        "title": rec["title"],
        "source": rec["source"],
        "dt": dt.isoformat(),
        "entry_price": round(entry_price, 4),
        "patterns": rec["pattern_str"],
    }

    for wname, wdelta in WINDOWS.items():
        window_end = dt + wdelta
        window_bars = future.loc[future.index <= window_end]

        if window_bars.empty:
            result[f"{wname}_max_gain"] = None
            result[f"{wname}_max_dd"] = None
            result[f"{wname}_bars"] = 0
            continue

        highs = window_bars["High"].values
        lows = window_bars["Low"].values

        max_high = np.nanmax(highs)
        min_low = np.nanmin(lows)

        max_gain_pct = ((max_high - entry_price) / entry_price) * 100
        max_dd_pct = ((min_low - entry_price) / entry_price) * 100

        result[f"{wname}_max_gain"] = round(max_gain_pct, 3)
        result[f"{wname}_max_dd"] = round(max_dd_pct, 3)
        result[f"{wname}_bars"] = len(window_bars)

    return result


# ── Step 5: Aggregate & summarize ─────────────────────────────────────────


def summarize(results: list[dict]) -> pd.DataFrame:
    """Aggregate results by pattern."""
    rows = []

    # For each pattern, compare matched vs all
    for pname in PATTERNS:
        matched = [r for r in results if pname in (r.get("patterns") or "")]
        if len(matched) < 5:
            continue

        row = {"pattern": pname, "n": len(matched)}
        for wname in WINDOWS:
            gains = [
                r[f"{wname}_max_gain"]
                for r in matched
                if r.get(f"{wname}_max_gain") is not None
            ]
            dds = [
                r[f"{wname}_max_dd"]
                for r in matched
                if r.get(f"{wname}_max_dd") is not None
            ]
            if gains:
                row[f"{wname}_avg_gain"] = round(np.mean(gains), 2)
                row[f"{wname}_med_gain"] = round(np.median(gains), 2)
                row[f"{wname}_pct_2pct_gain"] = round(
                    100 * sum(1 for g in gains if g >= 2.0) / len(gains), 1
                )
            if dds:
                row[f"{wname}_avg_dd"] = round(np.mean(dds), 2)
                row[f"{wname}_med_dd"] = round(np.median(dds), 2)
                row[f"{wname}_pct_5pct_dd"] = round(
                    100 * sum(1 for d in dds if d <= -5.0) / len(dds), 1
                )
        rows.append(row)

    # Also compute baseline (all articles, no pattern filter)
    row = {"pattern": "_BASELINE_ALL", "n": len(results)}
    for wname in WINDOWS:
        gains = [
            r[f"{wname}_max_gain"]
            for r in results
            if r.get(f"{wname}_max_gain") is not None
        ]
        dds = [
            r[f"{wname}_max_dd"]
            for r in results
            if r.get(f"{wname}_max_dd") is not None
        ]
        if gains:
            row[f"{wname}_avg_gain"] = round(np.mean(gains), 2)
            row[f"{wname}_med_gain"] = round(np.median(gains), 2)
            row[f"{wname}_pct_2pct_gain"] = round(
                100 * sum(1 for g in gains if g >= 2.0) / len(gains), 1
            )
        if dds:
            row[f"{wname}_avg_dd"] = round(np.mean(dds), 2)
            row[f"{wname}_med_dd"] = round(np.median(dds), 2)
            row[f"{wname}_pct_5pct_dd"] = round(
                100 * sum(1 for d in dds if d <= -5.0) / len(dds), 1
            )
    rows.append(row)

    # "no pattern" baseline
    no_pattern = [r for r in results if r.get("patterns") == "none"]
    row = {"pattern": "_BASELINE_NO_PATTERN", "n": len(no_pattern)}
    for wname in WINDOWS:
        gains = [
            r[f"{wname}_max_gain"]
            for r in no_pattern
            if r.get(f"{wname}_max_gain") is not None
        ]
        dds = [
            r[f"{wname}_max_dd"]
            for r in no_pattern
            if r.get(f"{wname}_max_dd") is not None
        ]
        if gains:
            row[f"{wname}_avg_gain"] = round(np.mean(gains), 2)
            row[f"{wname}_med_gain"] = round(np.median(gains), 2)
            row[f"{wname}_pct_2pct_gain"] = round(
                100 * sum(1 for g in gains if g >= 2.0) / len(gains), 1
            )
        if dds:
            row[f"{wname}_avg_dd"] = round(np.mean(dds), 2)
            row[f"{wname}_med_dd"] = round(np.median(dds), 2)
            row[f"{wname}_pct_5pct_dd"] = round(
                100 * sum(1 for d in dds if d <= -5.0) / len(dds), 1
            )
    rows.append(row)

    return pd.DataFrame(rows)


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Skip pattern study")
    parser.add_argument("--days", type=int, default=12, help="Days of archive to scan")
    parser.add_argument("--workers", type=int, default=8, help="Parallel price fetch workers")
    parser.add_argument("--max-symbols", type=int, default=0, help="Limit symbols (0=all)")
    parser.add_argument("--no-alpaca", action="store_true", help="Skip Alpaca symbol validation")
    args = parser.parse_args()

    print("=" * 60)
    print("Skip Pattern Study — Insight Sentry Archive")
    print("=" * 60)

    # Load Alpaca tradeable symbols for validation
    alpaca_symbols = None
    if not args.no_alpaca:
        alpaca_symbols = load_alpaca_tradeable_symbols()

    # Step 1
    articles = load_articles(max_days=args.days)

    # Step 2
    records = process_articles(articles, alpaca_symbols=alpaca_symbols)

    # Stats
    pattern_counts = defaultdict(int)
    for rec in records:
        for p in rec["patterns"]:
            pattern_counts[p] += 1
        if not rec["patterns"]:
            pattern_counts["_no_pattern"] += 1

    print("\n=== Pattern match counts (article-symbol pairs) ===")
    for p, c in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        print(f"  {p}: {c}")

    # Step 3: unique symbols
    unique_symbols = sorted(set(r["symbol"] for r in records))
    if args.max_symbols:
        unique_symbols = unique_symbols[: args.max_symbols]
        records = [r for r in records if r["symbol"] in set(unique_symbols)]

    print(f"\n{len(unique_symbols)} unique symbols to fetch")

    prices = fetch_all_prices(unique_symbols, records, workers=args.workers)

    # Step 4: compute returns
    print("\nComputing returns...")
    results = []
    skipped = 0
    for rec in records:
        sym = rec["symbol"]
        if sym not in prices:
            skipped += 1
            continue
        ret = compute_returns(rec, prices[sym])
        if ret:
            results.append(ret)
        else:
            skipped += 1

    print(f"Computed returns for {len(results)} records ({skipped} skipped)")

    # Save raw results
    if results:
        df_raw = pd.DataFrame(results)
        df_raw.to_csv(OUTPUT_CSV, index=False)
        print(f"\nRaw results saved to {OUTPUT_CSV}")

    # Step 5: summarize
    if results:
        df_summary = summarize(results)
        df_summary.to_csv(SUMMARY_CSV, index=False)
        print(f"Summary saved to {SUMMARY_CSV}")

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)

        # Print a readable version
        for _, row in df_summary.iterrows():
            pname = row["pattern"]
            n = row["n"]
            print(f"\n  {pname} (n={n})")
            for wname in WINDOWS:
                g = row.get(f"{wname}_avg_gain", "?")
                d = row.get(f"{wname}_avg_dd", "?")
                mg = row.get(f"{wname}_med_gain", "?")
                pct2 = row.get(f"{wname}_pct_2pct_gain", "?")
                print(
                    f"    {wname:>3s}: avg_gain={g:>6}%  med_gain={mg:>6}%  "
                    f"avg_dd={d:>7}%  pct>=2%gain={pct2}%"
                )


if __name__ == "__main__":
    main()
