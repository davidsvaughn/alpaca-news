"""Reliable capture and reconciliation for snapshot ``price_10min`` values."""

from __future__ import annotations

import json
import io
import time
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from trader.db.database import Database, update_snapshot_field


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


def _extract_quote_price(quote: dict[str, Any] | None) -> float | None:
    if not quote or not isinstance(quote, dict):
        return None
    for key in ("last_price", "lastPrice", "regularMarketPrice", "currentPrice", "close", "mark"):
        val = quote.get(key)
        if val is None:
            continue
        try:
            price = float(val)
            if price > 0:
                return price
        except (TypeError, ValueError):
            continue
    return None


def _yfinance_symbol_candidates(raw_symbol: str) -> list[str]:
    """Return normalized yfinance symbol candidates for a feed symbol."""
    raw = str(raw_symbol or "").strip().upper()
    if not raw:
        return []

    # Feed symbols sometimes include a "$" prefix.
    if raw.startswith("$"):
        raw = raw[1:]
    if not raw:
        return []

    cands: list[str] = [raw]

    # Berkshire-style classes: BRK/A -> BRK-A, BRK.A -> BRK-A
    if "/" in raw:
        cands.append(raw.replace("/", "-"))
    if "." in raw and not raw.endswith(".TO"):
        cands.append(raw.replace(".", "-"))

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

    # Deduplicate while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for s in cands:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _get_price_from_history(symbol: str, target: datetime) -> float | None:
    """Get closest historical price near *target* using yfinance minute bars."""
    try:
        import yfinance as yf
    except Exception:
        return None

    now = datetime.now(tz=timezone.utc)
    age_days = (now - target).days

    # 1m bars are available for ~7 days. 5m bars usually extend much further.
    if age_days <= 7:
        interval = "1m"
        max_delta_seconds = 5 * 60
    else:
        interval = "5m"
        max_delta_seconds = 15 * 60

    start = (target - timedelta(hours=2)).strftime("%Y-%m-%d")
    end = (target + timedelta(days=1)).strftime("%Y-%m-%d")

    candidates = _yfinance_symbol_candidates(symbol)
    if not candidates:
        return None

    for cand in candidates:
        try:
            with redirect_stderr(io.StringIO()):
                df = yf.Ticker(cand).history(start=start, end=end, interval=interval)
        except Exception:
            continue

        if df is None or df.empty:
            continue

        try:
            if df.index.tz is None:
                idx = df.index.tz_localize("UTC")
            else:
                idx = df.index.tz_convert("UTC")
            diffs = abs(idx - target)
            closest = int(diffs.argmin())
            if float(diffs[closest].total_seconds()) > max_delta_seconds:
                continue
            price = float(df.iloc[closest]["Close"])
            if price > 0:
                return price
        except Exception:
            continue

    return None


def capture_price_10min_for_snapshot(
    *,
    db: Database,
    snapshot_id: str,
    symbol: str,
    created_at: str | None,
    delay_minutes: int,
    quote_retries: int = 12,
    retry_sleep_seconds: int = 10,
) -> bool:
    """Capture a single snapshot's delayed entry price with retry + fallback."""
    created = _parse_iso_utc(created_at)
    if created is None:
        return False

    target = created + timedelta(minutes=delay_minutes)
    wait_seconds = max(0.0, (target - datetime.now(tz=timezone.utc)).total_seconds())
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    # Try quote-based capture first (fast path).
    market = None
    try:
        from trader.market.data_service import MarketDataService

        market = MarketDataService()
    except Exception:
        market = None

    for _ in range(max(1, quote_retries)):
        if market is not None:
            try:
                quote = market.get_quote(symbol)
                price = _extract_quote_price(quote)
                if price is not None:
                    return update_snapshot_field(db, snapshot_id, "price_10min", float(price))
            except Exception:
                pass
        time.sleep(max(0, retry_sleep_seconds))

    # If quote path failed, reconstruct from minute history around target.
    price = _get_price_from_history(symbol, target)
    if price is not None:
        return update_snapshot_field(db, snapshot_id, "price_10min", float(price))
    return False


def reconcile_missing_price_10min(
    *,
    db: Database,
    delay_minutes: int,
    scan_limit: int = 2000,
    batch_limit: int = 300,
) -> int:
    """Backfill missing ``price_10min`` values for snapshots old enough to resolve."""
    now = datetime.now(tz=timezone.utc)
    updated = 0
    processed = 0

    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT snapshot_json FROM snapshots "
                "WHERE json_extract(snapshot_json, '$.price_10min') IS NULL "
                "ORDER BY created_at DESC LIMIT :lim"
            ),
            {"lim": int(scan_limit)},
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
        target = created + timedelta(minutes=delay_minutes)
        if target > now:
            continue  # not eligible yet

        trigger = snap.get("trigger") or {}
        symbols = trigger.get("symbols") or []
        if not symbols:
            continue
        symbol = str(symbols[0]).strip().upper()
        if not symbol:
            continue

        processed += 1
        price = _get_price_from_history(symbol, target)
        if price is None:
            # Last fallback: if target is near-now, use live quote.
            if (now - target).total_seconds() <= 20 * 60:
                try:
                    from trader.market.data_service import MarketDataService

                    quote = MarketDataService().get_quote(symbol)
                    price = _extract_quote_price(quote)
                except Exception:
                    price = None
        if price is None:
            continue
        if update_snapshot_field(db, sid, "price_10min", float(price)):
            updated += 1

    return updated
