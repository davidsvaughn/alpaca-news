"""Shared portfolio valuation helpers.

These helpers are intentionally small and dependency-light so they can be
used from the web app, live monitor, and repair scripts without drift.
"""

from __future__ import annotations

from typing import Any


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def extract_quote_price(q: dict[str, Any] | None) -> float | None:
    """Extract a usable positive price from mixed vendor quote payloads."""
    if not isinstance(q, dict):
        return None
    for key in (
        "lastPrice",
        "last_price",
        "mark",
        "regularMarketPrice",
        "closePrice",
        "close_price",
        "close",
    ):
        raw = q.get(key)
        if raw is None:
            continue
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def max_positions_for_config(cfg: Any) -> int:
    """Return the effective slot count implied by a config."""
    alloc = _cfg_get(cfg, "allocation", "")
    alloc_params = _cfg_get(cfg, "allocation_params", {}) or {}

    if alloc == "max_positions":
        max_pos = int(alloc_params.get("max_pos", 10) or 10)
    elif alloc in ("fixed_dollar", "ranking_realloc"):
        alloc_pct = float(alloc_params.get("alloc_pct", 5) or 5)
        max_pos = max(1, int(100 / alloc_pct)) if alloc_pct > 0 else 20
    else:
        max_pos = 20

    return max(max_pos, 1)


def position_size_for_config(cfg: Any) -> float | None:
    """Return the fixed per-position notional implied by a config."""
    starting = _cfg_get(cfg, "starting_capital", 0)
    if not starting or starting <= 0:
        return None
    return float(starting) / float(max_positions_for_config(cfg))


def compute_pct_pnl(entry_price: float, current_price: float, direction: str = "bullish") -> float | None:
    """Compute direction-aware PnL percent."""
    if not entry_price or entry_price <= 0 or current_price is None:
        return None
    pnl = ((float(current_price) - float(entry_price)) / float(entry_price)) * 100.0
    if str(direction).lower() == "bearish":
        pnl = -pnl
    return pnl


def dollar_pnl_from_pct(position_size: float | None, pnl_pct: float | None) -> float:
    """Convert a percent PnL into dollar PnL using fixed position sizing."""
    if not position_size or pnl_pct is None:
        return 0.0
    return float(position_size) * float(pnl_pct) / 100.0


def backfill_watch_qty(watch: dict[str, Any], cfg: Any) -> float | None:
    """Return watch qty, inferring it from config position size if absent."""
    qty = watch.get("qty")
    if qty:
        try:
            q = float(qty)
        except (TypeError, ValueError):
            q = None
        else:
            if q > 0:
                return q

    entry = watch.get("entry") or {}
    entry_price = entry.get("price")
    try:
        entry_price_f = float(entry_price)
    except (TypeError, ValueError):
        return None
    if entry_price_f <= 0:
        return None

    pos_size = position_size_for_config(cfg)
    if not pos_size or pos_size <= 0:
        return None
    return pos_size / entry_price_f


def summarize_sim_portfolio(
    watches: list[dict[str, Any]],
    cfg: Any,
    price_by_symbol: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Summarize a simulated portfolio from watch state + current prices."""
    starting = _cfg_get(cfg, "starting_capital", 0) or 0
    pos_size = position_size_for_config(cfg) or 0.0

    realized_dollar = 0.0
    unrealized_dollar = 0.0
    holding_count = 0
    priced_holding_count = 0
    missing_symbols: list[str] = []

    for watch in watches:
        status = str(watch.get("status") or "")
        entry = watch.get("entry") or {}
        exit_data = watch.get("exit") or {}

        if status == "holding":
            holding_count += 1
            sym = str(watch.get("symbol") or "").upper()
            if not price_by_symbol:
                missing_symbols.append(sym)
                continue
            current_price = price_by_symbol.get(sym)
            pnl_pct = compute_pct_pnl(
                float(entry.get("price") or 0),
                current_price,
                str(entry.get("direction") or "bullish"),
            )
            if pnl_pct is None:
                missing_symbols.append(sym)
                continue
            priced_holding_count += 1
            unrealized_dollar += dollar_pnl_from_pct(pos_size, pnl_pct)
            continue

        if status in ("exited", "cooling_off", "sealed", "retrospective"):
            rpnl = exit_data.get("realized_pnl_pct")
            if rpnl is not None:
                realized_dollar += dollar_pnl_from_pct(pos_size, float(rpnl))

    cash = float(starting) - (holding_count * pos_size) + realized_dollar
    equity = float(starting) + realized_dollar + unrealized_dollar

    return {
        "starting_capital": float(starting),
        "position_size": pos_size,
        "holding_count": holding_count,
        "priced_holding_count": priced_holding_count,
        "missing_symbols": missing_symbols,
        "realized_dollar": round(realized_dollar, 2),
        "unrealized_dollar": round(unrealized_dollar, 2),
        "cash": round(cash, 2),
        "equity": round(equity, 2),
    }
