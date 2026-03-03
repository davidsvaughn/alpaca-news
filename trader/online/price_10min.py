"""Delayed entry-price capture at multiple minute offsets (5-20).

For each snapshot, prices at every integer minute offset from 5 to 20
are fetched in a single API call and stored in ``snapshot_json.price_at``
as ``{"5": 123.45, "6": 123.50, ..., "20": 124.00}``.

Legacy ``price_10min`` field is preserved for backward compatibility.
"""

from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from trader.db.database import Database, update_snapshot_field

# All integer offsets to capture (inclusive).
PRICE_OFFSETS = list(range(5, 21))  # [5, 6, 7, ..., 20]
MIN_OFFSET = 5
MAX_OFFSET = 20
_MAX_BAR_DELTA_S = 120  # accept a bar within 2 minutes of the target


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _yfinance_symbol_candidates(raw_symbol: str) -> list[str]:
    """Return normalized yfinance symbol candidates for a feed symbol."""
    raw = str(raw_symbol or "").strip().upper()
    if not raw:
        return []

    if raw.startswith("$"):
        raw = raw[1:]
    if not raw:
        return []

    cands: list[str] = []

    # Berkshire-style classes: prefer yfinance format (BRK-A) first
    if "/" in raw:
        cands.append(raw.replace("/", "-"))
        cands.append(raw)
    elif "." in raw and not raw.endswith(".TO"):
        cands.append(raw.replace(".", "-"))
        cands.append(raw)
    else:
        cands.append(raw)

    # TSX feed symbols: TSX:WCP -> WCP.TO
    if ":" in raw:
        exch, sym = raw.split(":", 1)
        if exch in {"TSX", "TSE"} and sym:
            cands.append(f"{sym}.TO")

    # Common crypto feed format: BTCUSD -> BTC-USD
    if raw.endswith("USD") and "-" not in raw and ":" not in raw and len(raw) > 3:
        base = raw[:-3]
        if base.isalpha():
            cands.append(f"{base}-USD")

    out: list[str] = []
    seen: set[str] = set()
    for s in cands:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ------------------------------------------------------------------
# Multi-offset price extraction from a bar series
# ------------------------------------------------------------------

def _extract_offsets_from_bars(
    bars: list[tuple[datetime, float]],
    created_at: datetime,
) -> dict[str, float]:
    """Given (timestamp, close) bars, extract prices at each offset in PRICE_OFFSETS.

    Returns ``{"5": price, "6": price, ...}`` (string keys for JSON).
    """
    if not bars:
        return {}

    results: dict[str, float] = {}
    for offset in PRICE_OFFSETS:
        target = created_at + timedelta(minutes=offset)
        best_delta = float("inf")
        best_price = 0.0
        for bar_t, bar_c in bars:
            delta = abs((bar_t - target).total_seconds())
            if delta < best_delta:
                best_delta = delta
                best_price = bar_c
        if best_delta <= _MAX_BAR_DELTA_S and best_price > 0:
            results[str(offset)] = round(best_price, 4)
    return results


def _schwab_symbol(raw: str) -> str:
    """Normalize to Schwab format: BRK.A -> BRK/A, BRK-A -> BRK/A."""
    s = raw.strip().upper()
    for sep in (".", "-"):
        if sep in s:
            base, suffix = s.rsplit(sep, 1)
            if len(suffix) == 1 and suffix.isalpha():
                return f"{base}/{suffix}"
    return s


def _get_prices_via_schwab(
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    created_at: datetime,
) -> dict[str, float]:
    """Try Schwab date-range candle fetch. Returns empty dict on failure."""
    try:
        from trader.market.schwab_client import SchwabMarketClient
        client = SchwabMarketClient()
        if not client.available:
            return {}
        candles = client.get_candles_by_date_range(
            _schwab_symbol(symbol),
            start=window_start,
            end=window_end,
        )
        if not candles:
            return {}
        bars = []
        for c in candles:
            t = _parse_iso_utc(c.t)
            if t is not None and c.c > 0:
                bars.append((t, c.c))
        return _extract_offsets_from_bars(bars, created_at)
    except Exception:
        return {}


def _get_prices_via_yfinance(
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    created_at: datetime,
) -> dict[str, float]:
    """Fallback: yfinance 1-min bars. Returns empty dict on failure."""
    try:
        import yfinance as yf
    except Exception:
        return {}

    now = datetime.now(tz=timezone.utc)
    age_days = (now - created_at).days

    if age_days <= 7:
        interval = "1m"
    else:
        interval = "5m"

    start_str = (window_start - timedelta(hours=1)).strftime("%Y-%m-%d")
    end_str = (window_end + timedelta(days=1)).strftime("%Y-%m-%d")

    candidates = _yfinance_symbol_candidates(symbol)
    if not candidates:
        return {}

    for cand in candidates:
        try:
            with redirect_stderr(io.StringIO()):
                df = yf.Ticker(cand).history(start=start_str, end=end_str, interval=interval)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        try:
            if df.index.tz is None:
                idx = df.index.tz_localize("UTC")
            else:
                idx = df.index.tz_convert("UTC")
            bars = [(idx[i].to_pydatetime(), float(df.iloc[i]["Close"])) for i in range(len(df))]
            results = _extract_offsets_from_bars(bars, created_at)
            if results:
                return results
        except Exception:
            continue

    return {}


def get_prices_at_offsets(symbol: str, created_at: datetime) -> dict[str, float]:
    """Fetch prices at all minute offsets (5-20) after *created_at*.

    Tries Schwab first for accuracy, falls back to yfinance.
    Single API call covers the entire window.
    Returns ``{"5": price, "6": price, ...}``.
    """
    # Pad window by 1 minute on each side
    window_start = created_at + timedelta(minutes=MIN_OFFSET - 1)
    window_end = created_at + timedelta(minutes=MAX_OFFSET + 1)

    # Schwab first (more accurate intraday data)
    results = _get_prices_via_schwab(symbol, window_start, window_end, created_at)
    if results:
        return results

    # Fallback to yfinance
    return _get_prices_via_yfinance(symbol, window_start, window_end, created_at)


# ------------------------------------------------------------------
# Live capture (called from orchestrator after snapshot creation)
# ------------------------------------------------------------------

def capture_prices_for_snapshot(
    *,
    db: Database,
    snapshot_id: str,
    symbol: str,
    created_at: str | None,
) -> bool:
    """Capture delayed entry prices at all offsets (5-20 min) for a snapshot.

    Sleeps until the latest offset (20 min) has elapsed, then fetches
    all bars in a single call.
    """
    created = _parse_iso_utc(created_at)
    if created is None:
        return False

    # Wait until the latest offset window has passed.
    target = created + timedelta(minutes=MAX_OFFSET)
    wait_seconds = max(0.0, (target - datetime.now(tz=timezone.utc)).total_seconds())
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    # Small extra delay to let the last bar settle.
    time.sleep(5)

    prices = get_prices_at_offsets(symbol, created)
    if not prices:
        return False

    # Store the full price_at map.
    update_snapshot_field(db, snapshot_id, "price_at", prices)

    # Backward compat: also write price_10min if offset 10 is available.
    if "10" in prices:
        update_snapshot_field(db, snapshot_id, "price_10min", prices["10"])

    return True


# ------------------------------------------------------------------
# Background reconciliation (called from main.py maintenance thread)
# ------------------------------------------------------------------

def reconcile_missing_prices(
    *,
    db: Database,
    scan_limit: int = 2000,
    batch_limit: int = 300,
) -> int:
    """Backfill missing ``price_at`` maps for snapshots old enough to resolve.

    Also migrates legacy ``price_10min`` values into ``price_at`` where needed.
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(minutes=MAX_OFFSET)
    updated = 0
    processed = 0

    with db.engine.connect() as conn:
        # Find snapshots missing price_at entirely (or with legacy price_10min only).
        rows = conn.execute(
            text(
                "SELECT snapshot_json FROM snapshots "
                "WHERE json_extract(snapshot_json, '$.price_at') IS NULL "
                "AND created_at <= :cutoff "
                "ORDER BY created_at DESC LIMIT :lim"
            ),
            {"cutoff": cutoff.isoformat(), "lim": int(scan_limit)},
        ).fetchall()

    for (raw,) in rows:
        if processed >= batch_limit:
            break
        snap = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(snap, dict):
            continue

        sid = str(snap.get("snapshot_id") or "").strip()
        if not sid:
            continue

        created = _parse_iso_utc(snap.get("created_at"))
        if created is None:
            continue

        trigger = snap.get("trigger") or {}
        symbols = trigger.get("symbols") or []
        if not symbols:
            continue
        symbol = str(symbols[0]).strip().upper()
        if not symbol:
            continue

        processed += 1
        prices = get_prices_at_offsets(symbol, created)

        # Migrate legacy price_10min if we got no data from the API
        # (snapshot may be too old for minute bars).
        legacy = snap.get("price_10min")
        if not prices and legacy is not None:
            try:
                prices = {"10": round(float(legacy), 4)}
            except (TypeError, ValueError):
                pass

        if not prices:
            continue

        update_snapshot_field(db, sid, "price_at", prices)

        # Backward compat
        if "10" in prices and snap.get("price_10min") is None:
            update_snapshot_field(db, sid, "price_10min", prices["10"])

        updated += 1

    return updated
