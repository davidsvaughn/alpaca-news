"""FastAPI application for monitoring pipeline progress."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from trader.config import Settings, load_settings
from trader.db.database import (
    Database,
    count_active_follow_ups,
    count_all_watches,
    count_snapshots,
    count_snapshots_today,
    count_watches_by_status,
    delete_snapshots_bulk,
    get_active_follow_ups,
    get_active_watches,
    get_all_follow_ups,
    get_all_snapshots,
    get_all_watches,
    get_daily_cost_history,
    get_daily_cost_today,
    get_daily_cost_today_by_provider,
    get_follow_ups_by_snapshot,
    get_recent_events,
    get_snapshot,
    get_watch,
    get_watch_by_snapshot,
    insert_watch,
    update_watch,
)
from trader.knowledge.store import KnowledgeStore
from trader.models.watch import WatchBuilder
from trader.online.activity_tracker import ActivityTracker
from trader.online.event_bus import EventBus, PipelineEvent
from trader.online.observer_mode import ObserverMode
from trader.reflection.eval_record import build_eval_record, snapshot_export_to_markdown
from trader.web.sse import sse_response


def create_app(
    *,
    settings: Settings,
    bus: EventBus,
    db: Database,
    knowledge: KnowledgeStore,
    tracker: ActivityTracker | None = None,
    observer: ObserverMode | None = None,
) -> FastAPI:
    app = FastAPI(title="alpaca-news dashboard")
    app.state.settings = settings  # mutable ref for hot-reload
    app.state.observer = observer

    # Market data service + fundamentals cache for snapshot enrichment
    from trader.market.data_service import MarketDataService
    app.state.market = MarketDataService()
    _fundamentals_cache: dict[str, tuple[float, dict]] = {}  # symbol -> (timestamp, data)
    _quote_cache: dict[str, tuple[float, float | None]] = {}  # symbol -> (timestamp, last_price)

    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    # Custom filter: safely convert _DictObj (or plain dict) to JSON for templates
    def _to_json(value: Any, indent: int | None = None) -> str:
        if isinstance(value, _DictObj):
            value = value.to_dict()
        return json.dumps(value, indent=indent, default=str, ensure_ascii=False)

    templates.env.filters["to_json"] = _to_json

    # Custom filter: format ISO timestamps to human-readable local time
    def _fmt_dt(value: Any, fmt: str = "short") -> str:
        """Convert ISO timestamp string to readable format.

        fmt="short"  → "Feb 13 4:32 PM"  (no year, for recent items)
        fmt="long"   → "Feb 13, 2026 4:32 PM"
        fmt="full"   → "Feb 13, 2026 4:32:45 PM"
        """
        if not value:
            return "?"
        from datetime import datetime as _dt, timezone as _tz
        s = str(value).strip()
        # Parse various ISO formats
        for pattern in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                dt = _dt.strptime(s, pattern)
                break
            except ValueError:
                continue
        else:
            return s  # unparseable — return as-is

        # Normalize to UTC if no tzinfo
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)

        # Convert to US Eastern (server-side best guess for user's local)
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo("US/Eastern"))
        except Exception:
            pass  # fall back to UTC

        if fmt == "short":
            return dt.strftime("%b %d %-I:%M %p")
        elif fmt == "long":
            return dt.strftime("%b %d, %Y %-I:%M %p")
        else:  # full
            return dt.strftime("%b %d, %Y %-I:%M:%S %p")

    templates.env.filters["fmt_dt"] = _fmt_dt

    def _normalize_signal_filter(value: str | None) -> str | None:
        if not value:
            return None
        v = value.strip().lower()
        if v in {"bull", "bullish"}:
            return "bullish"
        if v in {"bear", "bearish"}:
            return "bearish"
        if v == "neutral":
            return "neutral"
        return None

    def _parse_optional_float(value: str | None) -> float | None:
        if value is None:
            return None
        v = value.strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    def _parse_optional_scaled_float(value: str | None, scale: float) -> float | None:
        parsed = _parse_optional_float(value)
        if parsed is None:
            return None
        return parsed * scale

    def _normalize_sort(value: str | None, direction: str | None) -> tuple[str, str]:
        allowed = {"created", "headline", "symbols", "signal", "price_10", "price", "avg_vol", "mkt_cap", "pe"}
        col = (value or "created").strip().lower()
        if col not in allowed:
            col = "created"
        dir_norm = (direction or "desc").strip().lower()
        if dir_norm not in {"asc", "desc"}:
            dir_norm = "desc"
        return col, dir_norm

    def _build_query_suffix(params: dict[str, str]) -> str:
        pairs = [(k, v) for k, v in params.items() if v != ""]
        if not pairs:
            return ""
        return "&" + urlencode(pairs)

    def _primary_symbol(snap: dict[str, Any]) -> str:
        trigger = snap.get("trigger") or {}
        symbols = trigger.get("symbols") or []
        if not symbols:
            return ""
        return str(symbols[0]).strip().upper()

    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_iso_utc(value: Any) -> datetime | None:
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

    def _matches_range(value: float | None, min_val: float | None, max_val: float | None) -> bool:
        if min_val is None and max_val is None:
            return True
        if value is None:
            return False
        if min_val is not None and value < min_val:
            return False
        if max_val is not None and value > max_val:
            return False
        return True

    def _fmt_volume(v: float | None) -> str:
        if v is None:
            return "\u2014"
        if v >= 1e9:
            return f"{v / 1e9:.1f}B"
        if v >= 1e6:
            return f"{v / 1e6:.1f}M"
        if v >= 1e3:
            return f"{v / 1e3:.0f}K"
        return str(int(round(v)))

    def _fmt_mkt_cap(v: float | None) -> str:
        if v is None:
            return "\u2014"
        if v >= 1e12:
            return f"${v / 1e12:.2f}T"
        if v >= 1e9:
            return f"${v / 1e9:.1f}B"
        if v >= 1e6:
            return f"${v / 1e6:.0f}M"
        return f"${int(round(v)):,}"

    def _fmt_price(v: float | None) -> str:
        if v is None:
            return "\u2014"
        return f"${v:.2f}"

    def _fmt_pe(v: float | None) -> str:
        if v is None:
            return "\u2014"
        return f"{v:.1f}"

    def _sort_snapshot_rows(rows: list[dict[str, Any]], sort_col: str, sort_dir: str) -> list[dict[str, Any]]:
        signal_rank = {"bearish": 0, "neutral": 1, "bullish": 2}

        def _value(row: dict[str, Any]) -> Any:
            if sort_col == "created":
                return row.get("created_at") or ""
            if sort_col == "headline":
                trig = row.get("trigger") or {}
                return str(trig.get("headline") or "").lower()
            if sort_col == "symbols":
                return row.get("_primary_symbol") or ""
            if sort_col == "signal":
                pred = row.get("prediction") or {}
                d = str(pred.get("direction") or "").lower()
                return signal_rank.get(d)
            if sort_col == "price_10":
                return _safe_float(row.get("price_10min"))
            if sort_col == "price":
                return _safe_float(row.get("_price"))
            if sort_col == "avg_vol":
                return _safe_float(row.get("_avg_vol"))
            if sort_col == "mkt_cap":
                return _safe_float(row.get("_mkt_cap"))
            if sort_col == "pe":
                return _safe_float(row.get("_pe"))
            return row.get("created_at") or ""

        with_values: list[tuple[Any, dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        for row in rows:
            v = _value(row)
            if v is None or v == "":
                missing.append(row)
            else:
                with_values.append((v, row))

        with_values.sort(key=lambda x: x[0], reverse=(sort_dir == "desc"))
        return [row for _, row in with_values] + missing

    def _fetch_market_metrics(symbols: list[str]) -> dict[str, dict[str, float | None]]:
        result: dict[str, dict[str, float | None]] = {}
        uniq: list[str] = []
        seen: set[str] = set()
        for sym in symbols:
            s = str(sym).strip().upper()
            if not s or s in seen:
                continue
            seen.add(s)
            uniq.append(s)
            result[s] = {"price": None, "avg_vol": None, "mkt_cap": None, "pe": None}
        if not uniq:
            return result

        import time as _time
        now = _time.time()
        fundamentals_ttl = 86400.0  # 24h
        quote_ttl = 60.0            # 1m

        # Fill from caches first.
        missing: list[str] = []
        for sym in uniq:
            cached_f = _fundamentals_cache.get(sym)
            cached_q = _quote_cache.get(sym)
            has_f = cached_f and (now - cached_f[0]) < fundamentals_ttl
            has_q = cached_q and (now - cached_q[0]) < quote_ttl
            if has_f:
                f = cached_f[1]
                result[sym]["avg_vol"] = _safe_float(f.get("avg_volume"))
                result[sym]["mkt_cap"] = _safe_float(f.get("market_cap"))
                result[sym]["pe"] = _safe_float(f.get("pe_ratio"))
            if has_q:
                result[sym]["price"] = _safe_float(cached_q[1])
            if not (has_f and has_q):
                missing.append(sym)

        if not missing:
            return result

        market: MarketDataService = app.state.market
        chunk_size = 50
        for i in range(0, len(missing), chunk_size):
            chunk = missing[i:i + chunk_size]
            batch: dict[str, dict[str, Any]] = {}
            try:
                batch = market.get_quotes_with_fundamentals(chunk)
            except Exception:
                batch = {}
            for sym in chunk:
                d = batch.get(sym) or {}
                price = _safe_float(d.get("last_price"))
                pe = _safe_float(d.get("pe_ratio"))
                mkt_cap = _safe_float(d.get("market_cap"))
                avg_vol = _safe_float(d.get("avg_10d_volume") or d.get("avg_volume"))

                if price is not None:
                    _quote_cache[sym] = (now, price)
                if pe is not None or mkt_cap is not None or avg_vol is not None:
                    _fundamentals_cache[sym] = (
                        now,
                        {
                            "pe_ratio": pe,
                            "market_cap": mkt_cap,
                            "avg_volume": avg_vol,
                        },
                    )

                # Keep any cache-provided values if a fresh value is missing.
                if price is not None:
                    result[sym]["price"] = price
                if avg_vol is not None:
                    result[sym]["avg_vol"] = avg_vol
                if mkt_cap is not None:
                    result[sym]["mkt_cap"] = mkt_cap
                if pe is not None:
                    result[sym]["pe"] = pe
        return result

    def _snapshot_rows_for_filters(
        *,
        symbol: str | None = None,
        explored: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        headline: str | None = None,
        signal: str | None = None,
        price_10_min: str | None = None,
        price_10_max: str | None = None,
        price_min: str | None = None,
        price_max: str | None = None,
        avg_vol_min: str | None = None,
        avg_vol_max: str | None = None,
        mkt_cap_min: str | None = None,
        mkt_cap_max: str | None = None,
        pe_min: str | None = None,
        pe_max: str | None = None,
        sort_col: str | None = None,
        sort_dir: str | None = None,
        include_market_metrics: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        explored_only = explored == "1"
        signal_direction = _normalize_signal_filter(signal)
        signal_filter = (
            "bull" if signal_direction == "bullish"
            else "bear" if signal_direction == "bearish"
            else "neutral" if signal_direction == "neutral"
            else ""
        )
        sort_col_norm, sort_dir_norm = _normalize_sort(sort_col, sort_dir)
        price_10_min_val = _parse_optional_float(price_10_min)
        price_10_max_val = _parse_optional_float(price_10_max)
        price_min_val = _parse_optional_float(price_min)
        price_max_val = _parse_optional_float(price_max)
        # UI inputs are unit-scaled:
        # - AvgVol filters are in millions (M)
        # - MktCap filters are in billions (B)
        avg_vol_min_val = _parse_optional_scaled_float(avg_vol_min, 1e6)
        avg_vol_max_val = _parse_optional_scaled_float(avg_vol_max, 1e6)
        mkt_cap_min_val = _parse_optional_scaled_float(mkt_cap_min, 1e9)
        mkt_cap_max_val = _parse_optional_scaled_float(mkt_cap_max, 1e9)
        pe_min_val = _parse_optional_float(pe_min)
        pe_max_val = _parse_optional_float(pe_max)

        base_total = count_snapshots(
            db,
            symbol=symbol,
            explored_only=explored_only,
            created_after=created_after,
            created_before=created_before,
            headline=headline,
            signal_direction=signal_direction,
            price_10_min=price_10_min_val,
            price_10_max=price_10_max_val,
        )
        base_snaps = get_all_snapshots(
            db,
            symbol=symbol,
            explored_only=explored_only,
            created_after=created_after,
            created_before=created_before,
            headline=headline,
            signal_direction=signal_direction,
            price_10_min=price_10_min_val,
            price_10_max=price_10_max_val,
            limit=max(base_total, 1),
            offset=0,
        )

        symbols = [_primary_symbol(s) for s in base_snaps]
        market_by_symbol = _fetch_market_metrics(symbols) if include_market_metrics else {}
        now_utc = datetime.now(timezone.utc)
        price_delay_delta = timedelta(minutes=max(0, settings.price_delay_minutes))

        filtered_rows: list[dict[str, Any]] = []
        for snap in base_snaps:
            row = dict(snap)
            sym = _primary_symbol(row)
            md = market_by_symbol.get(sym) or {}
            row["_primary_symbol"] = sym
            row["_price"] = _safe_float(md.get("price"))
            row["_avg_vol"] = _safe_float(md.get("avg_vol"))
            row["_mkt_cap"] = _safe_float(md.get("mkt_cap"))
            row["_pe"] = _safe_float(md.get("pe"))
            row["_price_text"] = _fmt_price(row["_price"])
            row["_avg_vol_text"] = _fmt_volume(row["_avg_vol"])
            row["_mkt_cap_text"] = _fmt_mkt_cap(row["_mkt_cap"])
            row["_pe_text"] = _fmt_pe(row["_pe"])
            created_at = _parse_iso_utc(row.get("created_at"))
            has_price_10 = _safe_float(row.get("price_10min")) is not None
            row["_price_10_pending"] = (
                not has_price_10
                and created_at is not None
                and (created_at + price_delay_delta) > now_utc
            )

            if include_market_metrics:
                if not _matches_range(row["_price"], price_min_val, price_max_val):
                    continue
                if not _matches_range(row["_avg_vol"], avg_vol_min_val, avg_vol_max_val):
                    continue
                if not _matches_range(row["_mkt_cap"], mkt_cap_min_val, mkt_cap_max_val):
                    continue
                if not _matches_range(row["_pe"], pe_min_val, pe_max_val):
                    continue

            filtered_rows.append(row)

        sorted_rows = _sort_snapshot_rows(filtered_rows, sort_col_norm, sort_dir_norm)
        meta = {
            "explored_only": explored_only,
            "signal_filter": signal_filter,
            "sort_col": sort_col_norm,
            "sort_dir": sort_dir_norm,
            "symbol_filter": symbol or "",
            "headline_filter": headline or "",
            "created_after": created_after or "",
            "created_before": created_before or "",
            "price_10_min_filter": price_10_min or "",
            "price_10_max_filter": price_10_max or "",
            "price_min_filter": price_min or "",
            "price_max_filter": price_max or "",
            "avg_vol_min_filter": avg_vol_min or "",
            "avg_vol_max_filter": avg_vol_max or "",
            "mkt_cap_min_filter": mkt_cap_min or "",
            "mkt_cap_max_filter": mkt_cap_max or "",
            "pe_min_filter": pe_min or "",
            "pe_max_filter": pe_max or "",
        }
        return sorted_rows, meta

    # ------------------------------------------------------------------
    # Page routes
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={"active_page": "dashboard"},
        )

    @app.get("/watches", response_class=HTMLResponse)
    async def watches_page(request: Request, status: str | None = None):
        total = count_all_watches(db, status=status)
        return templates.TemplateResponse(
            request=request,
            name="watches.html",
            context={"active_page": "watches", "status_filter": status, "total": total},
        )

    @app.get("/watches/{watch_id}", response_class=HTMLResponse)
    async def watch_detail_page(request: Request, watch_id: str):
        watch_dict = get_watch(db, watch_id)
        if watch_dict is None:
            return HTMLResponse("<h3>Watch not found</h3>", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="watch_detail.html",
            context={"active_page": "watches", "w": _DictObj(watch_dict)},
        )

    @app.get("/snapshots", response_class=HTMLResponse)
    async def snapshots_page(
        request: Request,
        symbol: str | None = None,
        explored: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        headline: str | None = None,
        signal: str | None = None,
        price_10_min: str | None = None,
        price_10_max: str | None = None,
        price_min: str | None = None,
        price_max: str | None = None,
        avg_vol_min: str | None = None,
        avg_vol_max: str | None = None,
        mkt_cap_min: str | None = None,
        mkt_cap_max: str | None = None,
        pe_min: str | None = None,
        pe_max: str | None = None,
        sort_col: str | None = None,
        sort_dir: str | None = None,
    ):
        explored_only = explored == "1"
        signal_direction = _normalize_signal_filter(signal)
        signal_filter = (
            "bull" if signal_direction == "bullish"
            else "bear" if signal_direction == "bearish"
            else "neutral" if signal_direction == "neutral"
            else ""
        )
        sort_col_norm, sort_dir_norm = _normalize_sort(sort_col, sort_dir)
        price_10_min_val = _parse_optional_float(price_10_min)
        price_10_max_val = _parse_optional_float(price_10_max)
        total = count_snapshots(
            db,
            symbol=symbol,
            explored_only=explored_only,
            created_after=created_after,
            created_before=created_before,
            headline=headline,
            signal_direction=signal_direction,
            price_10_min=price_10_min_val,
            price_10_max=price_10_max_val,
        )
        return templates.TemplateResponse(
            request=request,
            name="snapshots.html",
            context={"active_page": "snapshots", "symbol_filter": symbol,
                     "headline_filter": headline,
                     "signal_filter": signal_filter,
                     "price_10_min_filter": price_10_min or "",
                     "price_10_max_filter": price_10_max or "",
                     "price_min_filter": price_min or "",
                     "price_max_filter": price_max or "",
                     "avg_vol_min_filter": avg_vol_min or "",
                     "avg_vol_max_filter": avg_vol_max or "",
                     "mkt_cap_min_filter": mkt_cap_min or "",
                     "mkt_cap_max_filter": mkt_cap_max or "",
                     "pe_min_filter": pe_min or "",
                     "pe_max_filter": pe_max or "",
                     "sort_col": sort_col_norm,
                     "sort_dir": sort_dir_norm,
                     "explored_only": explored_only, "total": total,
                     "created_after": created_after or "", "created_before": created_before or "",
                     "price_delay_minutes": settings.price_delay_minutes},
        )

    @app.get("/snapshots/{snapshot_id}", response_class=HTMLResponse)
    async def snapshot_detail_page(request: Request, snapshot_id: str):
        snap = get_snapshot(db, snapshot_id)
        if snap is None:
            return HTMLResponse("<h3>Snapshot not found</h3>", status_code=404)
        watch = get_watch_by_snapshot(db, snapshot_id)
        follow_ups = get_follow_ups_by_snapshot(db, snapshot_id)
        timeline = build_eval_record(snap, watch, follow_ups=follow_ups)
        return templates.TemplateResponse(
            request=request,
            name="snapshot_detail.html",
            context={
                "active_page": "snapshots",
                "s": _DictObj(snap),
                "timeline": timeline,
                "watch": _DictObj(watch) if watch else None,
            },
        )

    @app.get("/strategies", response_class=HTMLResponse)
    async def strategies_page(request: Request):
        from trader.market.backtest import STRATEGIES
        return templates.TemplateResponse(
            request=request,
            name="strategies.html",
            context={"active_page": "strategies", "strategies": STRATEGIES},
        )

    @app.get("/costs", response_class=HTMLResponse)
    async def costs_page(request: Request):
        daily_history = get_daily_cost_history(db, days=30)
        # Compute today's totals
        today_cost = get_daily_cost_today(db)
        today_count = count_snapshots_today(db)
        # Aggregate today's by-tool breakdown from history
        today_by_tool: dict[str, float] = {}
        if daily_history:
            from datetime import date

            today_str = date.today().isoformat()
            for day in daily_history:
                if day["date"] == today_str:
                    today_by_tool = day["by_tool"]
                    break
        return templates.TemplateResponse(
            request=request,
            name="costs.html",
            context={
                "active_page": "costs",
                "today_cost": today_cost,
                "today_count": today_count,
                "today_by_tool": today_by_tool,
                "max_daily": app.state.settings.max_daily_cost,
                "daily_history": daily_history,
            },
        )

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        groups = _settings_groups(app.state.settings)
        return templates.TemplateResponse(
            request=request,
            name="config.html",
            context={"active_page": "config", "groups": groups},
        )

    @app.get("/knowledge", response_class=HTMLResponse)
    async def knowledge_page(request: Request):
        knowledge.ensure_defaults()
        files: list[tuple[str, dict]] = []
        for path in sorted(knowledge.knowledge_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {"error": "Could not read file"}
            files.append((path.name, data))
        return templates.TemplateResponse(
            request=request,
            name="knowledge.html",
            context={
                "active_page": "knowledge",
                "knowledge_dir": str(knowledge.knowledge_dir),
                "files": files,
            },
        )

    @app.get("/reflection", response_class=HTMLResponse)
    async def reflection_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="reflection.html",
            context={"active_page": "reflection"},
        )

    # ------------------------------------------------------------------
    # API routes (HTML fragments for HTMX)
    # ------------------------------------------------------------------

    @app.get("/api/stats", response_class=HTMLResponse)
    async def api_stats(request: Request):
        counts = count_watches_by_status(db)
        inflight = tracker.get_inflight_cost() if tracker else 0.0
        sealed_cost = get_daily_cost_today(db)
        daily_cost_by_provider = get_daily_cost_today_by_provider(db)
        return templates.TemplateResponse(
            request=request,
            name="partials/_stats_cards.html",
            context={
                "holding_count": counts.get("holding", 0),
                "exited_count": counts.get("exited", 0),
                "retro_count": counts.get("retrospective", 0),
                "snapshots_today": count_snapshots_today(db),
                "daily_cost": sealed_cost + inflight,
                "inflight_cost": inflight,
                "daily_cost_by_provider": daily_cost_by_provider,
                "max_daily_cost": app.state.settings.max_daily_cost,
            },
        )

    @app.get("/api/stats-bar", response_class=HTMLResponse)
    async def api_stats_bar(request: Request):
        counts = count_watches_by_status(db)
        inflight = tracker.get_inflight_cost() if tracker else 0.0
        sealed_cost = get_daily_cost_today(db)
        return templates.TemplateResponse(
            request=request,
            name="partials/_stats_bar.html",
            context={
                "holding_count": counts.get("holding", 0),
                "snapshots_today": count_snapshots_today(db),
                "daily_cost": sealed_cost + inflight,
                "max_daily_cost": app.state.settings.max_daily_cost,
                "observer_mode": observer.enabled if observer else False,
            },
        )

    @app.get("/api/watches/active", response_class=HTMLResponse)
    async def api_watches_active(request: Request):
        watches = get_active_watches(db)
        return templates.TemplateResponse(
            request=request,
            name="partials/_active_watches.html",
            context={"watches": [_DictObj(w) for w in watches]},
        )

    @app.get("/api/watches", response_class=HTMLResponse)
    async def api_watches(request: Request, status: str | None = None):
        watches = get_all_watches(db, status=status)
        return templates.TemplateResponse(
            request=request,
            name="partials/_watches_table.html",
            context={"watches": [_DictObj(w) for w in watches]},
        )

    @app.get("/api/snapshots", response_class=HTMLResponse)
    async def api_snapshots(
        request: Request,
        symbol: str | None = None,
        explored: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        headline: str | None = None,
        signal: str | None = None,
        price_10_min: str | None = None,
        price_10_max: str | None = None,
        price_min: str | None = None,
        price_max: str | None = None,
        avg_vol_min: str | None = None,
        avg_vol_max: str | None = None,
        mkt_cap_min: str | None = None,
        mkt_cap_max: str | None = None,
        pe_min: str | None = None,
        pe_max: str | None = None,
        sort_col: str | None = None,
        sort_dir: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ):
        per_page = max(1, min(per_page, 500))
        sorted_rows, meta = _snapshot_rows_for_filters(
            symbol=symbol,
            explored=explored,
            created_after=created_after,
            created_before=created_before,
            headline=headline,
            signal=signal,
            price_10_min=price_10_min,
            price_10_max=price_10_max,
            price_min=price_min,
            price_max=price_max,
            avg_vol_min=avg_vol_min,
            avg_vol_max=avg_vol_max,
            mkt_cap_min=mkt_cap_min,
            mkt_cap_max=mkt_cap_max,
            pe_min=pe_min,
            pe_max=pe_max,
            sort_col=sort_col,
            sort_dir=sort_dir,
        )
        total = len(sorted_rows)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page
        page_rows = sorted_rows[offset:offset + per_page]

        query_suffix = _build_query_suffix(
            {
                "symbol": meta["symbol_filter"],
                "headline": meta["headline_filter"],
                "created_after": meta["created_after"],
                "created_before": meta["created_before"],
                "signal": meta["signal_filter"],
                "price_10_min": meta["price_10_min_filter"],
                "price_10_max": meta["price_10_max_filter"],
                "price_min": meta["price_min_filter"],
                "price_max": meta["price_max_filter"],
                "avg_vol_min": meta["avg_vol_min_filter"],
                "avg_vol_max": meta["avg_vol_max_filter"],
                "mkt_cap_min": meta["mkt_cap_min_filter"],
                "mkt_cap_max": meta["mkt_cap_max_filter"],
                "pe_min": meta["pe_min_filter"],
                "pe_max": meta["pe_max_filter"],
                "sort_col": meta["sort_col"],
                "sort_dir": meta["sort_dir"],
                "explored": "1" if meta["explored_only"] else "",
            }
        )

        return templates.TemplateResponse(
            request=request,
            name="partials/_snapshots_table.html",
            context={
                "snapshots": [_DictObj(s) for s in page_rows],
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "symbol_filter": meta["symbol_filter"],
                "headline_filter": meta["headline_filter"],
                "signal_filter": meta["signal_filter"],
                "price_10_min_filter": meta["price_10_min_filter"],
                "price_10_max_filter": meta["price_10_max_filter"],
                "price_min_filter": meta["price_min_filter"],
                "price_max_filter": meta["price_max_filter"],
                "avg_vol_min_filter": meta["avg_vol_min_filter"],
                "avg_vol_max_filter": meta["avg_vol_max_filter"],
                "mkt_cap_min_filter": meta["mkt_cap_min_filter"],
                "mkt_cap_max_filter": meta["mkt_cap_max_filter"],
                "pe_min_filter": meta["pe_min_filter"],
                "pe_max_filter": meta["pe_max_filter"],
                "sort_col": meta["sort_col"],
                "sort_dir": meta["sort_dir"],
                "query_suffix": query_suffix,
                "explored_only": meta["explored_only"],
                "created_after": meta["created_after"],
                "created_before": meta["created_before"],
                "price_delay_minutes": settings.price_delay_minutes,
            },
        )

    @app.post("/api/snapshots/delete")
    async def api_snapshots_delete(request: Request):
        body = await request.json()
        ids = body.get("snapshot_ids", [])
        if not ids:
            return {"deleted": 0}
        # Also remove JSON files from disk
        snap_dir = Path(settings.data_dir) / "snapshots"
        for sid in ids:
            for f in snap_dir.glob(f"*{sid}*"):
                f.unlink(missing_ok=True)
        deleted = delete_snapshots_bulk(db, ids)
        return {"deleted": deleted}

    # ------------------------------------------------------------------
    # Market data API (for snapshots table enrichment)
    # ------------------------------------------------------------------

    @app.get("/api/market/quotes")
    async def api_market_quotes(symbols: str = ""):
        """Batch current prices for the snapshots table (polled every 60s).

        Uses the lightweight get_quotes() — price data only.
        """
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not symbol_list:
            return {}
        market: MarketDataService = app.state.market
        quotes = market.get_quotes(symbol_list[:50])
        result: dict[str, Any] = {}
        for sym, q in quotes.items():
            result[sym] = {
                "last_price": q.get("last_price"),
                "net_pct_change": q.get("net_pct_change"),
            }
        return result

    @app.get("/api/market/fundamentals")
    async def api_market_fundamentals(symbols: str = ""):
        """Batch fundamentals (P/E, Mkt Cap, Avg Vol) — cached 1 hour.

        Uses the same Schwab quotes endpoint (which includes fundamental
        data like avg10DaysVolume, peRatio, sharesOutstanding).
        """
        import time as _time

        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not symbol_list:
            return {}
        now = _time.time()
        market: MarketDataService = app.state.market

        # Check cache; collect uncached symbols
        result: dict[str, Any] = {}
        uncached: list[str] = []
        for sym in symbol_list[:50]:
            cached = _fundamentals_cache.get(sym)
            if cached and (now - cached[0]) < 86400:  # 24 hours
                result[sym] = cached[1]
            else:
                uncached.append(sym)

        if uncached:
            data = market.get_quotes_with_fundamentals(uncached)
            for sym, d in data.items():
                entry = {
                    "pe_ratio": d.get("pe_ratio"),
                    "market_cap": d.get("market_cap"),
                    "avg_volume": d.get("avg_10d_volume"),
                }
                _fundamentals_cache[sym] = (now, entry)
                result[sym] = entry

        return result

    # ------------------------------------------------------------------
    # Exit strategy backtest API
    # ------------------------------------------------------------------

    @app.get("/api/strategies")
    async def api_strategies():
        from trader.market.backtest import strategies_json
        return strategies_json()

    @app.get("/api/allocations")
    async def api_allocations():
        from trader.market.backtest import allocations_json
        return allocations_json()

    @app.post("/api/strategies/backtest")
    async def api_backtest(request: Request):
        import asyncio
        from trader.market.backtest import (
            apply_allocation, compute_ann_a, compute_ann_b, run_backtest,
            weight_per_trade_for_allocation,
        )

        api_start = time.perf_counter()
        api_stages: dict[str, float] = {}

        def _mark(stage: str, start: float) -> None:
            api_stages[stage] = api_stages.get(stage, 0.0) + max(0.0, time.perf_counter() - start)

        body = await request.json()
        strategy_key = body.get("strategy", "")
        params = body.get("params", {})
        entries = body.get("entries", [])
        filters = body.get("filters")
        market_close = body.get("market_close", "16:00")  # None = extended hours
        min_hold = body.get("min_hold", 5)
        guard_stop_pct = body.get("guard_stop_pct", 0)
        guard_target_pct = body.get("guard_target_pct", 0)
        cost_bps = body.get("cost_bps", 10)
        trace_enabled = bool(body.get("trace", False))
        allocation_key = body.get("allocation", "none")
        allocation_params = body.get("allocation_params", {})

        if isinstance(filters, dict):
            t_filter = time.perf_counter()
            market_filter_keys = (
                "price_min", "price_max",
                "avg_vol_min", "avg_vol_max",
                "mkt_cap_min", "mkt_cap_max",
                "pe_min", "pe_max",
            )
            needs_market_metric_filter = any(
                str(filters.get(k) or "").strip() for k in market_filter_keys
            )
            sort_col_val = str(filters.get("sort_col") or "").strip().lower()
            needs_market_metric_sort = sort_col_val in {"price", "avg_vol", "mkt_cap", "pe"}
            include_market_metrics = needs_market_metric_filter or needs_market_metric_sort
            sorted_rows, _ = _snapshot_rows_for_filters(
                symbol=(str(filters.get("symbol")).strip() if filters.get("symbol") is not None else None),
                explored=(str(filters.get("explored")).strip() if filters.get("explored") is not None else None),
                created_after=(str(filters.get("created_after")).strip() if filters.get("created_after") is not None else None),
                created_before=(str(filters.get("created_before")).strip() if filters.get("created_before") is not None else None),
                headline=(str(filters.get("headline")).strip() if filters.get("headline") is not None else None),
                signal=(str(filters.get("signal")).strip() if filters.get("signal") is not None else None),
                price_10_min=(str(filters.get("price_10_min")).strip() if filters.get("price_10_min") is not None else None),
                price_10_max=(str(filters.get("price_10_max")).strip() if filters.get("price_10_max") is not None else None),
                price_min=(str(filters.get("price_min")).strip() if filters.get("price_min") is not None else None),
                price_max=(str(filters.get("price_max")).strip() if filters.get("price_max") is not None else None),
                avg_vol_min=(str(filters.get("avg_vol_min")).strip() if filters.get("avg_vol_min") is not None else None),
                avg_vol_max=(str(filters.get("avg_vol_max")).strip() if filters.get("avg_vol_max") is not None else None),
                mkt_cap_min=(str(filters.get("mkt_cap_min")).strip() if filters.get("mkt_cap_min") is not None else None),
                mkt_cap_max=(str(filters.get("mkt_cap_max")).strip() if filters.get("mkt_cap_max") is not None else None),
                pe_min=(str(filters.get("pe_min")).strip() if filters.get("pe_min") is not None else None),
                pe_max=(str(filters.get("pe_max")).strip() if filters.get("pe_max") is not None else None),
                sort_col=(str(filters.get("sort_col")).strip() if filters.get("sort_col") is not None else None),
                sort_dir=(str(filters.get("sort_dir")).strip() if filters.get("sort_dir") is not None else None),
                include_market_metrics=include_market_metrics,
            )
            _mark("filter_rows", t_filter)

            t_entries = time.perf_counter()
            entries = []
            for row in sorted_rows:
                sid = str(row.get("snapshot_id") or "").strip()
                entry_time = str(row.get("created_at") or "").strip()
                sym = str(row.get("_primary_symbol") or "").strip().upper()
                price = _safe_float(row.get("price_10min")) or 0.0
                if not sid or not entry_time or not sym:
                    continue
                pred = row.get("prediction") or {}
                confidence = _safe_float(pred.get("confidence")) or 0.5
                entries.append(
                    {
                        "snapshot_id": sid,
                        "symbol": sym,
                        "entry_price": price if price > 0 else 0,
                        "entry_time": entry_time,
                        "confidence": confidence,
                    }
                )
            _mark("build_entries", t_entries)

        if not entries:
            return []
        res_min = settings.stats_resolution_minutes
        engine_trace: dict[str, Any] | None = {} if trace_enabled else None
        t_engine = time.perf_counter()
        results = await asyncio.to_thread(
            run_backtest, strategy_key, params, entries, market_close, min_hold,
            guard_stop_pct, guard_target_pct, settings.price_delay_minutes,
            res_min, engine_trace,
        )
        _mark("run_backtest", t_engine)
        # Apply transaction cost deduction
        t_cost = time.perf_counter()
        if cost_bps > 0:
            cost_pct = cost_bps / 100  # bps → percentage points
            cost_frac = cost_bps / 10000  # bps → fraction
            for r in results:
                if r.pnl_pct is not None:
                    r.pnl_pct -= cost_pct
                    r.pnl_pct = round(r.pnl_pct, 4)
                # Adjust entry price upward so Method B's equity curve reflects cost
                if r.entry_price and r.entry_price > 0:
                    r.entry_price *= 1 + cost_frac
        _mark("apply_cost", t_cost)

        # Apply allocation filtering (skip / replace)
        t_alloc = time.perf_counter()
        results, alloc_stats = apply_allocation(
            results, entries, allocation_key, allocation_params,
        )
        wpt = weight_per_trade_for_allocation(allocation_key, allocation_params)
        _mark("apply_allocation", t_alloc)

        t_serialize = time.perf_counter()
        trades = [r.to_dict() for r in results]
        _mark("serialize_trades", t_serialize)
        t_summary = time.perf_counter()
        valid = [r for r in results if r.pnl_pct is not None]
        count = len(valid)
        avg_pnl = round(sum(r.pnl_pct for r in valid) / count, 2) if count else None
        stats_a = compute_ann_a(results)
        stats_b = compute_ann_b(results, res_min, weight_per_trade=wpt)
        _mark("compute_summary", t_summary)

        response = {
            "trades": trades,
            "summary": {
                "count": count,
                "avg_pnl": avg_pnl,
                "daily_pnl": round(stats_a["daily_pnl"], 3) if stats_a else None,
                "ann_a": round(stats_a["ann"], 1) if stats_a else None,
                "sharpe_a": round(stats_a["sharpe"], 2) if stats_a and stats_a["sharpe"] is not None else None,
                "ann_b": round(stats_b["ann"], 1) if stats_b else None,
                "sharpe_b": round(stats_b["sharpe"], 2) if stats_b and stats_b["sharpe"] is not None else None,
                "alloc_taken": alloc_stats.get("taken", 0),
                "alloc_skipped": alloc_stats.get("skipped", 0),
                "alloc_replaced": alloc_stats.get("replaced", 0),
            },
        }
        if trace_enabled:
            total_sec = max(0.0, time.perf_counter() - api_start)
            api_stages["total"] = total_sec
            stage_pct = {
                k: (v / total_sec * 100.0) if total_sec > 0 else 0.0
                for k, v in api_stages.items()
            }
            response["trace"] = {
                "api_stages_sec": {k: round(v, 6) for k, v in sorted(api_stages.items())},
                "api_stages_pct": {k: round(v, 2) for k, v in sorted(stage_pct.items())},
                "counts": {
                    "entries_in": len(entries),
                    "trades_out": len(results),
                    "valid_trades": count,
                },
                "engine": engine_trace or {},
            }
        return response

    @app.get("/api/activity-panel", response_class=HTMLResponse)
    async def api_activity_panel(request: Request):
        activities = tracker.get_all() if tracker else []
        active_fus = get_active_follow_ups(db)
        # Enrich follow-ups with schedule progress info
        fu_summaries = []
        for fu in active_fus:
            fj = fu.get("follow_up_json", fu) if isinstance(fu, dict) else fu
            schedule = fj.get("schedule", [])
            collections = fj.get("collections", [])
            fu_summaries.append({
                "symbols": fj.get("symbols", []),
                "reason": fj.get("reason", ""),
                "collections_done": len(collections),
                "total_scheduled": len(schedule),
                "next_offset": schedule[len(collections)] if len(collections) < len(schedule) else "done",
            })
        return templates.TemplateResponse(
            request=request,
            name="partials/_activity_panel.html",
            context={
                "activities": [dataclasses.asdict(a) for a in activities],
                "follow_ups": fu_summaries,
            },
        )

    @app.post("/api/activity/{activity_id}/abort", response_class=HTMLResponse)
    async def api_abort_activity(activity_id: str):
        if tracker is None:
            return HTMLResponse("<span class='text-muted small'>Tracker not available</span>", status_code=503)
        tracker.request_abort(activity_id)
        bus.publish(PipelineEvent(
            type="job_aborted",
            payload={"activity_id": activity_id},
        ))
        return HTMLResponse("<span class='text-warning small'>Abort requested</span>")

    # ------------------------------------------------------------------
    # Control actions (POST)
    # ------------------------------------------------------------------

    @app.post("/api/reload-settings", response_class=HTMLResponse)
    async def api_reload_settings(request: Request):
        """Re-read .env and replace Settings. Returns HTML diff of changes."""
        old = app.state.settings
        try:
            new = load_settings(override=True)
        except Exception as e:
            return HTMLResponse(
                f"<div class='alert alert-danger'>Failed to reload: {e}</div>",
            )

        # Compute diff
        changes: list[tuple[str, Any, Any]] = []
        for field in dataclasses.fields(old):
            old_val = getattr(old, field.name)
            new_val = getattr(new, field.name)
            if old_val != new_val:
                changes.append((field.name, old_val, new_val))

        # Sync observer mode if .env changed it
        if observer is not None:
            for name, old_v, new_v in changes:
                if name == "observer_mode":
                    observer.set(new_v)
                    bus.publish(PipelineEvent(
                        type="observer_mode_changed",
                        payload={"enabled": new_v, "source": "reload"},
                    ))
                    break

        app.state.settings = new

        if not changes:
            return HTMLResponse(
                "<div class='alert alert-info'>Settings reloaded — no changes detected.</div>"
            )

        rows = "".join(
            f"<tr><td><code>{name}</code></td>"
            f"<td><code>{old_v}</code></td>"
            f"<td><strong><code>{new_v}</code></strong></td></tr>"
            for name, old_v, new_v in changes
        )
        restart_fields = {"mock_llm", "sqlite_path", "data_dir", "backfill_on_start",
                          "backfill_limit", "x_stream_enabled", "x_stream_mode"}
        needs_restart = any(name in restart_fields for name, _, _ in changes)
        restart_note = (
            "<div class='alert alert-warning mt-2'>"
            "Some changed settings (marked above) only take full effect after a server restart."
            "</div>"
            if needs_restart else ""
        )

        return HTMLResponse(
            f"<div class='alert alert-success'>Settings reloaded — {len(changes)} change(s):</div>"
            "<table class='table table-sm'><thead><tr>"
            "<th>Setting</th><th>Old</th><th>New</th>"
            "</tr></thead><tbody>"
            f"{rows}</tbody></table>"
            f"{restart_note}"
            "<script>setTimeout(() => location.reload(), 3000)</script>"
        )

    # ------------------------------------------------------------------
    # Observer mode
    # ------------------------------------------------------------------

    @app.get("/api/observer-status")
    async def api_observer_status():
        return {"observer_mode": observer.enabled if observer else False}

    @app.post("/api/observer-toggle", response_class=HTMLResponse)
    async def api_observer_toggle(request: Request):
        if observer is None:
            return HTMLResponse(
                "<div class='alert alert-danger'>Observer mode not configured</div>",
                status_code=500,
            )
        new_state = observer.toggle()
        bus.publish(PipelineEvent(
            type="observer_mode_changed",
            payload={"enabled": new_state},
        ))
        label = "ON" if new_state else "OFF"
        return HTMLResponse(f"<span class='small text-muted'>Observer mode: {label}</span>")

    @app.post("/api/watches/{watch_id}/exit", response_class=HTMLResponse)
    async def api_force_exit_watch(request: Request, watch_id: str):
        """Force-exit a holding watch at current market price."""
        watch_dict = get_watch(db, watch_id)
        if watch_dict is None:
            return HTMLResponse("<div class='alert alert-danger'>Watch not found</div>", status_code=404)
        if watch_dict["status"] != "holding":
            return HTMLResponse(
                f"<div class='alert alert-warning'>Watch is '{watch_dict['status']}', not holding</div>",
            )

        builder = WatchBuilder.from_dict(watch_dict)
        symbol = builder.symbol

        # Try to get current price; fall back to entry price
        exit_price = builder.entry.price
        try:
            from trader.market.data_service import MarketDataService

            market = MarketDataService()
            quote = market.get_quote(symbol)
            if quote.get("last_price"):
                exit_price = float(quote["last_price"])
        except Exception:
            pass  # Use entry price as fallback

        builder.record_exit(price=exit_price, reason="Manual exit from dashboard")
        updated = builder.to_watch().to_dict()
        update_watch(db, watch_id, updated)

        bus.publish(PipelineEvent(
            type="watch_exited",
            payload={
                "watch_id": watch_id,
                "symbol": symbol,
                "reason": "Manual exit from dashboard",
                "exit_price": exit_price,
                "pnl_pct": updated.get("exit", {}).get("realized_pnl_pct", 0),
            },
        ))

        return HTMLResponse(
            "<div class='alert alert-success'>Watch exited successfully. Refreshing...</div>"
            "<script>setTimeout(() => location.reload(), 1000)</script>",
        )

    @app.post("/api/explore", response_class=HTMLResponse)
    async def api_manual_explore(
        request: Request,
        headline: str = Form(...),
        symbols: str = Form(""),
    ):
        """Trigger manual exploration for a headline + symbols."""
        if observer is not None and observer.enabled:
            return HTMLResponse(
                "<div class='alert alert-warning'>Observer mode is active — manual exploration is blocked.</div>"
            )
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not headline.strip():
            return HTMLResponse("<div class='alert alert-warning'>Headline is required</div>")

        news = {
            "headline": headline.strip(),
            "summary": None,
            "source": "dashboard_manual",
            "symbols": symbol_list,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        bus.publish(PipelineEvent(
            type="manual_explore_started",
            payload={"headline": headline.strip(), "symbols": symbol_list},
        ))

        # Run in background thread to avoid blocking
        def _run():
            try:
                from trader.online.orchestrator import process_news_file

                # Write temp file to trigger the standard pipeline
                import tempfile
                tmp = Path(tempfile.mktemp(suffix=".json", dir=tempfile.gettempdir()))
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(news, ensure_ascii=False), encoding="utf-8")

                process_news_file(
                    path=tmp,
                    settings=app.state.settings,
                    db=db,
                    knowledge=knowledge,
                    bus=bus,
                    tracker=tracker,
                )
                bus.publish(PipelineEvent(
                    type="manual_explore_complete",
                    payload={"headline": headline.strip(), "symbols": symbol_list},
                ))
            except Exception as e:
                bus.publish(PipelineEvent(
                    type="manual_explore_error",
                    payload={"headline": headline.strip(), "error": str(e)},
                ))

        threading.Thread(target=_run, daemon=True).start()

        return HTMLResponse(
            "<div class='alert alert-info'>"
            f"Exploration started for: <strong>{headline.strip()}</strong> "
            f"({', '.join(symbol_list) or 'no symbols'}). "
            "Watch the live feed for progress.</div>"
        )

    _KNOWLEDGE_WHITELIST = {
        "skip_patterns.jsonc",
        "investigate_patterns.json",
        "reliable_sources.json",
        "search_strategies.json",
        "x_search_strategies.json",
        "signal_patterns.json",
        "anti_patterns.json",
        "model_notes.json",
        "symbol_lists.json",
    }

    @app.post("/api/knowledge/{filename}", response_class=HTMLResponse)
    async def api_update_knowledge(request: Request, filename: str, content: str = Form(...)):
        """Update a knowledge file with new JSON content."""
        if filename not in _KNOWLEDGE_WHITELIST:
            return HTMLResponse(
                f"<div class='alert alert-danger'>File '{filename}' is not editable</div>",
                status_code=403,
            )

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            return HTMLResponse(
                f"<div class='alert alert-danger'>Invalid JSON: {e}</div>",
            )

        path = knowledge.knowledge_dir / filename
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        bus.publish(PipelineEvent(
            type="knowledge_updated",
            payload={"filename": filename},
        ))

        return HTMLResponse(
            f"<div class='alert alert-success'>Saved {filename} successfully.</div>"
            "<script>setTimeout(() => location.reload(), 1000)</script>",
        )

    @app.get("/api/events/recent")
    async def api_events_recent(limit: int = 200):
        """Return recent persisted events as JSON (newest first)."""
        return get_recent_events(db, limit=min(limit, 500))

    # ------------------------------------------------------------------
    # Snapshot export (Markdown download)
    # ------------------------------------------------------------------

    def _export_data(snapshot_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list]:
        """Fetch snapshot + watch + follow-ups for export."""
        snap = get_snapshot(db, snapshot_id)
        if snap is None:
            return None, None, []
        watch = get_watch_by_snapshot(db, snapshot_id)
        follow_ups = get_follow_ups_by_snapshot(db, snapshot_id)
        return snap, watch, follow_ups

    def _export_filename(snap: dict[str, Any], ext: str) -> str:
        """Build export filename like snapshot_AAPL_0c5470b4.json."""
        trigger = snap.get("trigger") or {}
        symbols = trigger.get("symbols") or []
        ticker = symbols[0] if symbols else "UNK"
        short_id = snap.get("snapshot_id", "unknown")[:8]
        return f"snapshot_{ticker}_{short_id}.{ext}"

    @app.get("/api/snapshots/{snapshot_id}/export/md")
    async def api_snapshot_export_md(snapshot_id: str):
        """Export snapshot + watch + follow-ups as a downloadable Markdown file."""
        snap, watch, follow_ups = _export_data(snapshot_id)
        if snap is None:
            return Response(content="Snapshot not found", status_code=404)
        content = snapshot_export_to_markdown(snap, watch, follow_ups)
        fname = _export_filename(snap, "md")
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # ------------------------------------------------------------------
    # Manual override: BUY / SELL
    # ------------------------------------------------------------------

    @app.post("/api/snapshots/{snapshot_id}/override", response_class=HTMLResponse)
    async def api_snapshot_override(
        request: Request,
        snapshot_id: str,
        direction: str = Form(...),
    ):
        """Create a watch from a manual BUY/SELL decision."""
        if direction not in ("bullish", "bearish"):
            return HTMLResponse(
                '<div class="alert alert-danger">Invalid direction</div>', status_code=400,
            )

        # Check if a watch already exists
        existing = get_watch_by_snapshot(db, snapshot_id)
        if existing:
            return HTMLResponse(
                '<div class="alert alert-warning">A watch already exists for this snapshot</div>',
            )

        snap = get_snapshot(db, snapshot_id)
        if snap is None:
            return HTMLResponse(
                '<div class="alert alert-danger">Snapshot not found</div>', status_code=404,
            )

        symbols = snap.get("trigger", {}).get("symbols", [])
        primary_symbol = symbols[0] if symbols else None
        if not primary_symbol:
            return HTMLResponse(
                '<div class="alert alert-danger">No symbol found in snapshot</div>',
            )

        # Fetch live price
        entry_price: float | None = None
        try:
            from trader.market.data_service import MarketDataService
            market = MarketDataService()
            quote = market.get_quote(primary_symbol)
            if quote:
                for key in ("lastPrice", "last_price", "regularMarketPrice", "close"):
                    val = quote.get(key)
                    if val is not None:
                        entry_price = float(val)
                        break
        except Exception:
            pass

        # Fallback: try snapshot's stored price_context
        if entry_price is None:
            pc = snap.get("price_context", {})
            sym_data = pc.get(primary_symbol, {})
            if isinstance(sym_data, dict):
                for key in ("lastPrice", "last_price", "regularMarketPrice", "close"):
                    val = sym_data.get(key)
                    if val is not None:
                        try:
                            entry_price = float(val)
                            break
                        except (TypeError, ValueError):
                            continue

        if entry_price is None:
            return HTMLResponse(
                '<div class="alert alert-danger">Could not determine entry price for '
                f'{primary_symbol}</div>',
            )

        # Create the watch
        from trader.models.watch import WatchEntry
        entry = WatchEntry(
            snapshot_id=snapshot_id,
            price=entry_price,
            time=datetime.now(tz=timezone.utc).isoformat(),
            confidence=1.0,  # manual override = full conviction
            direction=direction,
            horizon="1d",
            thesis=f"Manual {direction} override by user",
        )
        wb = WatchBuilder(symbol=primary_symbol, entry=entry)
        watch = wb.to_watch()
        watch_path = Path(settings.data_dir) / "watches" / f"{watch.watch_id}.json"
        watch.persist(watch_path)
        insert_watch(db, watch=watch.to_dict())

        bus.publish(PipelineEvent(
            type="watch_created",
            payload={
                "watch_id": watch.watch_id,
                "symbol": watch.symbol,
                "direction": direction,
                "confidence": 1.0,
                "entry_price": entry_price,
                "snapshot_id": snapshot_id,
                "source": "manual",
            },
        ))

        action = "BUY" if direction == "bullish" else "SELL"
        return HTMLResponse(
            f'<div class="alert alert-success">{action} watch created for '
            f'{primary_symbol} @ ${entry_price:.2f}</div>',
        )

    # ------------------------------------------------------------------
    # Follow-ups API
    # ------------------------------------------------------------------

    @app.get("/api/follow-ups")
    async def api_follow_ups(status: str | None = None, limit: int = 50):
        """Return follow-ups, optionally filtered by status."""
        return get_all_follow_ups(db, status=status, limit=min(limit, 500))

    # ------------------------------------------------------------------
    # Reflection API
    # ------------------------------------------------------------------

    @app.get("/api/reflection/snapshots", response_class=HTMLResponse)
    async def api_reflection_snapshots(request: Request):
        """Return snapshot table with checkboxes for the reflection page."""
        snaps = get_all_snapshots(db)
        return templates.TemplateResponse(
            request=request,
            name="partials/_reflection_snapshots.html",
            context={"snapshots": [_DictObj(s) for s in snaps]},
        )

    @app.post("/api/reflection/evaluate", response_class=HTMLResponse)
    async def api_reflection_evaluate(request: Request):
        """Trigger LLM evaluation on selected snapshots."""
        body = await request.json()
        snapshot_ids: list[str] = body.get("snapshot_ids", [])
        if not snapshot_ids:
            return HTMLResponse("<div class='alert alert-warning'>No snapshots selected.</div>")

        try:
            from trader.reflection.evaluator import evaluate_snapshots

            result = evaluate_snapshots(
                snapshot_ids=snapshot_ids,
                db=db,
                knowledge=knowledge,
                model=app.state.settings.reflection_model,
            )
            return templates.TemplateResponse(
                request=request,
                name="partials/_reflection_results.html",
                context={"evaluation": result},
            )
        except Exception as e:
            return templates.TemplateResponse(
                request=request,
                name="partials/_reflection_results.html",
                context={"error": f"Evaluation failed: {e}"},
            )

    @app.post("/api/reflection/apply", response_class=HTMLResponse)
    async def api_reflection_apply(request: Request, action_json: str = Form(...)):
        """Apply a Tier A insight to knowledge files."""
        try:
            action = json.loads(action_json)
            atype = action.get("type", "")
            if atype == "add_skip_keyword":
                knowledge.append_skip_keywords([action["keyword"]])
                msg = f"Added skip keyword: {action['keyword']}"
            elif atype == "add_signal_pattern":
                knowledge.append_to_list("signal_patterns.json", "patterns", action["pattern"])
                msg = f"Added signal pattern"
            elif atype == "add_anti_pattern":
                knowledge.append_to_list("anti_patterns.json", "patterns", action["pattern"])
                msg = f"Added anti-pattern"
            elif atype == "add_search_template":
                knowledge.append_to_list("search_strategies.json", "templates", action["template"])
                msg = f"Added search template"
            elif atype == "add_model_note":
                knowledge.append_to_list("model_notes.json", "notes", action["note"])
                msg = f"Added model note"
            else:
                return HTMLResponse(f"<div class='alert alert-warning'>Unknown action type: {atype}</div>")

            bus.publish(PipelineEvent(type="knowledge_updated", payload={"action": atype}))
            return HTMLResponse(f"<div class='alert alert-success'>{msg}</div>")
        except Exception as e:
            return HTMLResponse(f"<div class='alert alert-danger'>Apply failed: {e}</div>")

    @app.post("/api/reflection/save", response_class=HTMLResponse)
    async def api_reflection_save(
        request: Request,
        text: str = Form(...),
        category: str = Form("general"),
    ):
        """Save a Tier B suggestion to markdown."""
        try:
            suggestions_dir = Path(app.state.settings.data_dir) / "reflection" / "suggestions"
            suggestions_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
            filepath = suggestions_dir / f"{ts}_{category}.md"
            filepath.write_text(
                f"# Suggestion ({category})\n\n{text}\n\n---\nGenerated: {ts}\n",
                encoding="utf-8",
            )
            return HTMLResponse(
                f"<div class='alert alert-success'>Saved suggestion to {filepath.name}</div>"
            )
        except Exception as e:
            return HTMLResponse(f"<div class='alert alert-danger'>Save failed: {e}</div>")

    # ------------------------------------------------------------------
    # SSE stream
    # ------------------------------------------------------------------

    @app.get("/events")
    async def events():
        return sse_response(bus=bus, ping_interval_s=app.state.settings.sse_ping_interval_s)

    return app


def _settings_groups(s: Settings) -> list[tuple[str, list[tuple[str, Any]]]]:
    """Group Settings fields by category for display."""
    # Map field names to groups (order matters for display)
    groups_map: list[tuple[str, list[str]]] = [
        ("Modes", ["learning_mode", "trading_mode", "debug", "observer_mode"]),
        ("Paths", ["alpaca_output_dir", "data_dir", "sqlite_path", "snapshots_dir"]),
        (
            "Models",
            [
                "triage_model",
                "research_model",
                "xsearch_model",
            ],
        ),
        (
            "Budgets & Limits",
            [
                "max_daily_cost", "max_cost_per_news_item",
                "max_total_hops", "max_web_searches_per_item", "max_x_searches_per_item",
                "triage_symbol_cooldown_minutes",
                "max_phase1_actions", "max_phase2_branches",
            ],
        ),
        ("SSE", ["sse_ping_interval_s"]),
        ("Dev / Testing", ["mock_llm", "backfill_on_start", "backfill_limit"]),
        (
            "X Stream",
            [
                "x_stream_enabled", "x_stream_mode",
                "x_stream_market_hours_only",
                "x_max_posts_per_day", "x_max_bursts_per_day",
                "x_burst_ttl_minutes", "x_usage_poll_interval_s",
                "x_min_triage_confidence_for_burst",
            ],
        ),
        (
            "Evidence Acquisition",
            ["evidence_acquire_enabled", "evidence_max_docs_per_item", "evidence_extractor"],
        ),
        (
            "Watch Lifecycle",
            [
                "watch_enabled", "watch_confidence_threshold", "watch_max_concurrent",
                "watch_monitoring_budget", "watch_max_hold_minutes",
                "watch_max_retro_minutes", "watch_checkin_model",
            ],
        ),
        (
            "Pipeline",
            [
                "pipeline_request_limit", "pipeline_tool_calls_limit",
                "pipeline_max_cost_usd", "pipeline_agent_timeout_s",
                "max_parallel_explores",
                "openai_web_search_limit",
                "openai_reasoning_effort", "gemini_thinking_level",
            ],
        ),
        ("Reflection", ["reflection_model"]),
    ]
    result: list[tuple[str, list[tuple[str, Any]]]] = []
    for group_name, field_names in groups_map:
        fields = [(name, getattr(s, name)) for name in field_names if hasattr(s, name)]
        if fields:
            result.append((group_name, fields))
    return result


class _DictObj:
    """Thin wrapper so Jinja2 templates can use dot notation on dicts."""

    def __init__(self, d: dict[str, Any]) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                setattr(self, k, _DictObj(v))
            elif isinstance(v, list):
                setattr(self, k, [_DictObj(i) if isinstance(i, dict) else i for i in v])
            else:
                setattr(self, k, v)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> list[str]:
        return [k for k in self.__dict__ if not k.startswith("_")]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, v) for k, v in self.__dict__.items() if not k.startswith("_")]

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __bool__(self) -> bool:
        return bool(self.keys())

    def to_dict(self) -> dict[str, Any]:
        """Recursively convert back to a plain dict (e.g. for tojson filter)."""
        result: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if k.startswith("_"):
                continue
            if isinstance(v, _DictObj):
                result[k] = v.to_dict()
            elif isinstance(v, list):
                result[k] = [i.to_dict() if isinstance(i, _DictObj) else i for i in v]
            else:
                result[k] = v
        return result
