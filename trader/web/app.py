"""FastAPI application for monitoring pipeline progress."""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    async def snapshots_page(request: Request, symbol: str | None = None):
        total = count_snapshots(db, symbol=symbol)
        return templates.TemplateResponse(
            request=request,
            name="snapshots.html",
            context={"active_page": "snapshots", "symbol_filter": symbol, "total": total},
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
        page: int = 1,
        per_page: int = 50,
    ):
        offset = (max(page, 1) - 1) * per_page
        total = count_snapshots(db, symbol=symbol)
        snaps = get_all_snapshots(db, symbol=symbol, limit=per_page, offset=offset)
        total_pages = max(1, (total + per_page - 1) // per_page)
        return templates.TemplateResponse(
            request=request,
            name="partials/_snapshots_table.html",
            context={
                "snapshots": [_DictObj(s) for s in snaps],
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "symbol_filter": symbol or "",
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
        "skip_patterns.json",
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
    # Snapshot export (JSON + Markdown downloads)
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

    @app.get("/api/snapshots/{snapshot_id}/export")
    async def api_snapshot_export(snapshot_id: str):
        """Export snapshot + watch + follow-ups as a downloadable JSON file."""
        snap, watch, follow_ups = _export_data(snapshot_id)
        if snap is None:
            return {"error": f"Snapshot {snapshot_id} not found"}
        blob: dict[str, Any] = {
            "snapshot": snap,
            "watch": watch,
            "follow_ups": follow_ups,
        }
        content = json.dumps(blob, indent=2, default=str, ensure_ascii=False)
        fname = _export_filename(snap, "json")
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

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
            "Models & Providers",
            [
                "triage_provider", "triage_model",
                "research_provider", "research_model",
                "sentiment_provider", "sentiment_model",
                "xsearch_provider", "xsearch_model",
            ],
        ),
        (
            "Budgets & Limits",
            [
                "max_daily_cost", "max_cost_per_news_item",
                "max_total_hops", "max_web_searches_per_item", "max_x_searches_per_item",
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
