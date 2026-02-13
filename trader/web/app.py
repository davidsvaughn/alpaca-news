"""FastAPI application for monitoring pipeline progress."""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from trader.config import Settings, load_settings
from trader.db.database import (
    Database,
    count_all_watches,
    count_snapshots,
    count_snapshots_today,
    count_watches_by_status,
    get_active_watches,
    get_all_snapshots,
    get_all_watches,
    get_daily_cost_history,
    get_daily_cost_today,
    get_snapshot,
    get_watch,
    update_watch,
)
from trader.knowledge.store import KnowledgeStore
from trader.models.watch import WatchBuilder
from trader.online.event_bus import EventBus, PipelineEvent
from trader.web.sse import sse_response


def create_app(
    *,
    settings: Settings,
    bus: EventBus,
    db: Database,
    knowledge: KnowledgeStore,
) -> FastAPI:
    app = FastAPI(title="alpaca-news dashboard")
    app.state.settings = settings  # mutable ref for hot-reload

    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    # Custom filter: safely convert _DictObj (or plain dict) to JSON for templates
    def _to_json(value: Any, indent: int | None = None) -> str:
        if isinstance(value, _DictObj):
            value = value.to_dict()
        return json.dumps(value, indent=indent, default=str, ensure_ascii=False)

    templates.env.filters["to_json"] = _to_json

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
        return templates.TemplateResponse(
            request=request,
            name="snapshot_detail.html",
            context={"active_page": "snapshots", "s": _DictObj(snap)},
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

    # ------------------------------------------------------------------
    # API routes (HTML fragments for HTMX)
    # ------------------------------------------------------------------

    @app.get("/api/stats", response_class=HTMLResponse)
    async def api_stats(request: Request):
        counts = count_watches_by_status(db)
        return templates.TemplateResponse(
            request=request,
            name="partials/_stats_cards.html",
            context={
                "holding_count": counts.get("holding", 0),
                "exited_count": counts.get("exited", 0),
                "retro_count": counts.get("retrospective", 0),
                "snapshots_today": count_snapshots_today(db),
                "daily_cost": get_daily_cost_today(db),
                "max_daily_cost": app.state.settings.max_daily_cost,
            },
        )

    @app.get("/api/stats-bar", response_class=HTMLResponse)
    async def api_stats_bar(request: Request):
        counts = count_watches_by_status(db)
        return templates.TemplateResponse(
            request=request,
            name="partials/_stats_bar.html",
            context={
                "holding_count": counts.get("holding", 0),
                "snapshots_today": count_snapshots_today(db),
                "daily_cost": get_daily_cost_today(db),
                "max_daily_cost": app.state.settings.max_daily_cost,
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
    async def api_snapshots(request: Request, symbol: str | None = None):
        snaps = get_all_snapshots(db, symbol=symbol)
        return templates.TemplateResponse(
            request=request,
            name="partials/_snapshots_table.html",
            context={"snapshots": [_DictObj(s) for s in snaps]},
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
                tmp = Path(tempfile.mktemp(suffix=".json", dir=app.state.settings.alpaca_output_dir))
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
        ("Modes", ["learning_mode", "trading_mode", "debug"]),
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
