from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


def snapshot_entry_time(snapshot: dict[str, Any]) -> str:
    for key in ("decision_at", "created_at"):
        value = str(snapshot.get(key) or "").strip()
        if value:
            return value
    return ""


def primary_symbol(snapshot: dict[str, Any]) -> str:
    trigger = snapshot.get("trigger") or {}
    symbols = trigger.get("symbols") or []
    if not symbols:
        return ""
    return str(symbols[0] or "").strip().upper()


def build_decision_metrics_payload(
    *,
    per_symbol: dict[str, dict[str, Any]],
    captured_at: str,
    source: str,
) -> dict[str, Any]:
    normalized: dict[str, dict[str, Any]] = {}
    for symbol, metrics in per_symbol.items():
        sym = str(symbol or "").strip().upper()
        if not sym:
            continue
        normalized[sym] = {
            "price": _safe_float(metrics.get("price")),
            "avg_vol": _safe_float(metrics.get("avg_vol")),
            "mkt_cap": _safe_float(metrics.get("mkt_cap")),
            "pe": _safe_float(metrics.get("pe")),
            "source": str(metrics.get("source") or source or "").strip() or None,
        }
    return {
        "captured_at": captured_at,
        "source": source,
        "per_symbol": normalized,
    }


def decision_symbol_metrics(snapshot: dict[str, Any], symbol: str | None = None) -> dict[str, Any]:
    sym = str(symbol or primary_symbol(snapshot) or "").strip().upper()
    if not sym:
        return {"price": None, "avg_vol": None, "mkt_cap": None, "pe": None}

    decision_metrics = snapshot.get("decision_metrics") or {}
    per_symbol = decision_metrics.get("per_symbol") or {}
    metrics = per_symbol.get(sym) or per_symbol.get(sym.upper()) or {}
    if metrics:
        return {
            "price": _safe_float(metrics.get("price")),
            "avg_vol": _safe_float(metrics.get("avg_vol")),
            "mkt_cap": _safe_float(metrics.get("mkt_cap")),
            "pe": _safe_float(metrics.get("pe")),
            "source": metrics.get("source"),
            "captured_at": decision_metrics.get("captured_at"),
        }

    price_context = snapshot.get("price_context") or {}
    per_symbol_ctx = price_context.get("per_symbol") or {}
    ctx = per_symbol_ctx.get(sym) or per_symbol_ctx.get(sym.upper()) or {}
    return {
        "price": _safe_float(ctx.get("last_price") or ctx.get("close")),
        "avg_vol": _safe_float(_first_present(ctx.get("avg_10d_volume"), ctx.get("avg_volume"))),
        "mkt_cap": _safe_float(ctx.get("market_cap")),
        "pe": _safe_float(ctx.get("pe_ratio")),
        "source": price_context.get("source"),
        "captured_at": decision_metrics.get("captured_at"),
    }


def merge_decision_symbol_metrics(
    snapshot: dict[str, Any],
    *,
    symbol: str,
    metrics: dict[str, Any],
    captured_at: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    out = dict(snapshot)
    existing = dict(out.get("decision_metrics") or {})
    per_symbol = dict(existing.get("per_symbol") or {})
    sym = str(symbol or "").strip().upper()
    current = dict(per_symbol.get(sym) or {})
    for key in ("price", "avg_vol", "mkt_cap", "pe"):
        if key in metrics:
            current[key] = _safe_float(metrics.get(key))
    current["source"] = str(metrics.get("source") or source or current.get("source") or "").strip() or None
    per_symbol[sym] = current
    existing["per_symbol"] = per_symbol
    if captured_at:
        existing["captured_at"] = captured_at
    elif not existing.get("captured_at"):
        existing["captured_at"] = datetime.now(tz=timezone.utc).isoformat()
    if source:
        existing["source"] = source
    elif not existing.get("source"):
        existing["source"] = current.get("source")
    out["decision_metrics"] = existing
    return out


def compute_avg_daily_volume_from_bars(
    df: pd.DataFrame | None,
    *,
    decision_at: str,
    lookback_days: int = 10,
) -> float | None:
    """Average completed-day volume prior to ``decision_at``.

    Uses only fully completed trading dates strictly before the decision date,
    which is conservative for intraday and after-hours decisions alike.
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return None

    try:
        from trader.market.backtest import _parse_entry_time
    except Exception:
        return None

    decision_dt = _parse_entry_time(str(decision_at))
    index = df.index
    if getattr(index, "tz", None) is not None:
        try:
            index = index.tz_convert("US/Eastern").tz_localize(None)
        except Exception:
            index = index.tz_localize(None)

    vol = pd.Series(df["Volume"].astype(float).to_numpy(), index=index)
    daily = vol.groupby(pd.Index(index.date)).sum().sort_index()
    previous_days = daily[daily.index < decision_dt.date()].tail(max(1, int(lookback_days)))
    if previous_days.empty:
        return None
    return float(previous_days.mean())


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None
