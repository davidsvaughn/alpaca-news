"""Backfill portfolio_equity_snapshots from watch entry/exit events.

Replays watch lifecycle chronologically to reconstruct an equity curve
for each portfolio. Uses Schwab 1-min candles to compute accurate
unrealized P&L for open positions at each event timestamp.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from trader.db.database import (
    Database,
    delete_equity_history,
    insert_equity_snapshots_bulk,
)

log = logging.getLogger(__name__)


def _pos_size_for_config(cfg: dict[str, Any]) -> float:
    """Compute the fixed dollar size per position from a LiveConfig dict."""
    starting = cfg.get("starting_capital", 0)
    if not starting or starting <= 0:
        return 0.0
    alloc = cfg.get("allocation", "")
    alloc_params = cfg.get("allocation_params") or {}

    if alloc == "max_positions":
        max_pos = int(alloc_params.get("max_pos", 10))
    elif alloc in ("fixed_dollar", "ranking_realloc"):
        alloc_pct = float(alloc_params.get("alloc_pct", 5))
        max_pos = max(1, int(100 / alloc_pct))
    else:
        max_pos = 20

    return starting / max_pos


def _get_watches_for_config(db: Database, config_id: str) -> list[dict[str, Any]]:
    """Fetch all watches for a given live_config_id."""
    sql = (
        "SELECT watch_json FROM watches "
        "WHERE json_extract(watch_json, '$.live_config_id') = :cid"
    )
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), {"cid": config_id}).fetchall()
    results = []
    for r in rows:
        raw = r[0]
        d = json.loads(raw) if isinstance(raw, str) else raw
        results.append(d)
    return results


def _build_events(watches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract chronological trade events from watches.

    Returns list of {timestamp, type, symbol, pnl_pct, watch_id, entry_price} sorted by time.
    """
    events = []
    for w in watches:
        entry = w.get("entry", {})
        entry_time = entry.get("time") or w.get("created_at", "")
        if entry_time:
            events.append({
                "timestamp": entry_time,
                "type": "buy",
                "symbol": w.get("symbol", ""),
                "pnl_pct": 0.0,
                "watch_id": w.get("watch_id", ""),
                "entry_price": entry.get("price", 0),
            })

        ex = w.get("exit")
        if ex:
            exit_time = ex.get("time", "")
            rpnl = ex.get("realized_pnl_pct", 0.0)
            if exit_time:
                events.append({
                    "timestamp": exit_time,
                    "type": "sell",
                    "symbol": w.get("symbol", ""),
                    "pnl_pct": float(rpnl) if rpnl is not None else 0.0,
                    "watch_id": w.get("watch_id", ""),
                    "entry_price": entry.get("price", 0),
                })

    events.sort(key=lambda e: e["timestamp"])
    return events


def _parse_ts(ts_str: str) -> datetime:
    """Parse an ISO timestamp string to a tz-aware datetime."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fetch_price_series(symbols: set[str]) -> dict[str, list[tuple[datetime, float]]]:
    """Fetch 10 days of 1-min candles from Schwab for each symbol.

    Returns {symbol: [(timestamp, close_price), ...]} sorted by time.
    Falls back gracefully if Schwab is unavailable.
    """
    price_series: dict[str, list[tuple[datetime, float]]] = {}
    if not symbols:
        return price_series

    try:
        from trader.market.schwab_client import SchwabMarketClient
        schwab = SchwabMarketClient()
        if not schwab.available:
            log.warning("Schwab unavailable for backfill price lookup")
            return price_series
    except Exception:
        log.warning("Could not initialize Schwab client for backfill")
        return price_series

    for sym in symbols:
        try:
            candles = schwab.get_intraday_candles(
                sym, period=10, extended_hours=True,
            )
            series = []
            for c in candles:
                ts = _parse_ts(c.t)
                series.append((ts, c.c))
            series.sort(key=lambda x: x[0])
            price_series[sym] = series
            log.info("Fetched %d candles for %s", len(series), sym)
        except Exception:
            log.warning("Failed to fetch candles for %s", sym, exc_info=True)

    return price_series


def _lookup_price(
    series: list[tuple[datetime, float]],
    target: datetime,
) -> float | None:
    """Find the closest price at or before target timestamp using binary search."""
    if not series:
        return None
    # Extract just timestamps for bisect
    times = [s[0] for s in series]
    idx = bisect_right(times, target) - 1
    if idx < 0:
        return None
    return series[idx][1]


def _compute_unrealized(
    holdings: dict[str, dict[str, Any]],
    pos_size: float,
    price_series: dict[str, list[tuple[datetime, float]]],
    at_time: datetime,
) -> float:
    """Compute total unrealized P&L for all open positions at a given time.

    holdings: {watch_id: {symbol, entry_price}}
    """
    total = 0.0
    for _wid, info in holdings.items():
        sym = info["symbol"]
        entry_price = info["entry_price"]
        if entry_price <= 0:
            continue
        series = price_series.get(sym)
        if not series:
            continue
        current = _lookup_price(series, at_time)
        if current is None:
            continue
        pnl_pct = (current - entry_price) / entry_price
        total += pos_size * pnl_pct
    return total


def backfill_portfolio(
    db: Database,
    config_id: str,
    cfg: dict[str, Any],
    *,
    replace: bool = True,
    price_series: dict[str, list[tuple[datetime, float]]] | None = None,
) -> int:
    """Backfill equity snapshots for a single portfolio from its watch history.

    Args:
        db: Database connection
        config_id: The live config ID
        cfg: The live config dict
        replace: If True, delete existing backfill snapshots first
        price_series: Pre-fetched price data (if None, fetches from Schwab)

    Returns:
        Number of snapshots inserted
    """
    starting_capital = cfg.get("starting_capital", 0)
    if not starting_capital or starting_capital <= 0:
        log.warning("Skipping backfill for %s: no starting_capital", config_id)
        return 0

    pos_size = _pos_size_for_config(cfg)
    if pos_size <= 0:
        return 0

    watches = _get_watches_for_config(db, config_id)
    if not watches:
        log.info("No watches for %s, inserting initial snapshot only", config_id)

    events = _build_events(watches)

    # Fetch price data if not provided
    if price_series is None:
        all_symbols = {w.get("symbol", "") for w in watches if w.get("symbol")}
        price_series = _fetch_price_series(all_symbols)

    has_prices = len(price_series) > 0

    if replace:
        deleted = delete_equity_history(db, config_id, source="backfill")
        if deleted:
            log.info("Deleted %d existing backfill snapshots for %s", deleted, config_id)

    # Replay events
    cash = starting_capital
    realized_pnl = 0.0
    # {watch_id: {symbol, entry_price}}
    holdings: dict[str, dict[str, Any]] = {}
    snapshots: list[dict[str, Any]] = []

    # Initial snapshot at config creation time
    created_at = cfg.get("created_at", "")
    if created_at:
        snapshots.append({
            "config_id": config_id,
            "timestamp": created_at,
            "equity": starting_capital,
            "cash": starting_capital,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "position_count": 0,
            "source": "backfill",
        })

    for evt in events:
        wid = evt["watch_id"]

        if evt["type"] == "buy":
            cash -= pos_size
            holdings[wid] = {
                "symbol": evt["symbol"],
                "entry_price": evt["entry_price"],
            }
        elif evt["type"] == "sell":
            dollar_pnl = pos_size * evt["pnl_pct"] / 100.0
            cash += pos_size + dollar_pnl
            realized_pnl += dollar_pnl
            holdings.pop(wid, None)

        # Compute unrealized P&L using real prices
        evt_time = _parse_ts(evt["timestamp"])
        if has_prices and holdings:
            unrealized = _compute_unrealized(holdings, pos_size, price_series, evt_time)
        else:
            unrealized = 0.0

        open_value = len(holdings) * pos_size
        equity = cash + open_value + unrealized

        snapshots.append({
            "config_id": config_id,
            "timestamp": evt["timestamp"],
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(realized_pnl, 2),
            "position_count": len(holdings),
            "source": "backfill",
        })

    count = insert_equity_snapshots_bulk(db, snapshots)
    log.info("Backfilled %d equity snapshots for %s", count, config_id)
    return count


def backfill_all_portfolios(db: Database) -> dict[str, int]:
    """Backfill equity snapshots for all live configs with watch history.

    Fetches Schwab price data once for all symbols across all portfolios.

    Returns: {config_id: count_inserted}
    """
    from trader.db.database import get_all_live_configs

    configs = get_all_live_configs(db)
    if not configs:
        return {}

    # Collect all symbols across all portfolios for a single batch fetch
    all_symbols: set[str] = set()
    config_watches: dict[str, list[dict[str, Any]]] = {}
    for cfg in configs:
        config_id = cfg.get("config_id", "")
        if not config_id:
            continue
        watches = _get_watches_for_config(db, config_id)
        config_watches[config_id] = watches
        for w in watches:
            sym = w.get("symbol", "")
            if sym:
                all_symbols.add(sym)

    # Fetch prices once for all symbols
    log.info("Fetching Schwab candles for %d symbols", len(all_symbols))
    price_series = _fetch_price_series(all_symbols)
    log.info("Got price data for %d/%d symbols", len(price_series), len(all_symbols))

    results = {}
    for cfg in configs:
        config_id = cfg.get("config_id", "")
        if not config_id:
            continue
        count = backfill_portfolio(db, config_id, cfg, price_series=price_series)
        results[config_id] = count

    total = sum(results.values())
    log.info("Backfill complete: %d snapshots across %d portfolios", total, len(results))
    return results
