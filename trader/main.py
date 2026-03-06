"""Entry point.

Starts:
- Online orchestrator (watch loop) in a background thread
- FastAPI dashboard (uvicorn)

Run:
  uv run python -m trader.main
"""

from __future__ import annotations

import argparse
import atexit
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

from trader.config import load_settings
from trader.online.observer_mode import ObserverMode
from trader.db.database import insert_event, open_sqlite, prune_old_events
from trader.knowledge.store import KnowledgeStore
from trader.online.activity_tracker import Activity, ActivityTracker
from trader.online.event_bus import EventBus, PipelineEvent
from trader.online.orchestrator import process_news_file, run_watch_loop
from trader.online.price_10min import reconcile_missing_prices
from trader.online.x_stream_service import XStreamGuards, XStreamService
from trader.web.app import create_app


def _kill_stale_servers(primary_port: int = 8000, legacy_port: int = 8765) -> None:
    """Kill any orphaned Python server processes listening on our ports.

    Prevents stale servers (from previous sessions or standalone launches)
    from hogging the Schwab websocket or causing port conflicts.
    Only targets Python processes (safe — won't kill unrelated services).
    """
    import re
    import signal

    for port in (primary_port, legacy_port):
        try:
            result = subprocess.run(
                ["ss", "-tlnp", f"sport = :{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if f":{port}" not in line:
                    continue
                # Only kill python processes — pattern: ("python3",pid=XXXX,fd=YY)
                if "python" not in line.lower():
                    continue
                match = re.search(r"pid=(\d+)", line)
                if not match:
                    continue
                pid = int(match.group(1))
                if pid == os.getpid():
                    continue
                print(f"Killing stale Python server on port {port} (PID {pid})")
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
                try:
                    os.kill(pid, 0)  # check if still alive
                    os.kill(pid, signal.SIGKILL)
                    print(f"  Force-killed PID {pid}")
                except OSError:
                    pass  # already dead
        except Exception as e:
            print(f"WARN: stale server check on port {port} failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="alpaca-news trader")
    parser.add_argument("--observer", action="store_true",
                        help="Start in observer mode (no new jobs launched)")
    args = parser.parse_args()

    _kill_stale_servers()

    # Make this process a session leader so all child processes (websockets,
    # threads) share our process group.  Killing the group kills everything.
    import signal
    os.setpgrp()

    def _kill_group(signum: int, _frame: object) -> None:
        # Kill entire process group (us + all children) then exit
        os.killpg(os.getpgid(os.getpid()), signal.SIGKILL)

    signal.signal(signal.SIGTERM, _kill_group)
    signal.signal(signal.SIGINT, _kill_group)

    settings = load_settings()
    observer = ObserverMode(enabled=args.observer or settings.observer_mode)
    if observer.enabled:
        print("OBSERVER MODE: active — no new jobs will be launched")
    db = open_sqlite(settings.sqlite_path)
    knowledge = KnowledgeStore(root_dir=Path(settings.data_dir))
    knowledge.ensure_defaults()

    bus = EventBus()

    # Persist events to SQLite (survives restarts + tab navigation)
    def _persist_event(evt: PipelineEvent) -> None:
        try:
            insert_event(db, event_type=evt.type, payload=evt.payload)
        except Exception:
            pass  # Don't let persistence failures break the pipeline

    bus.subscribe(_persist_event)
    prune_old_events(db, keep_days=7)

    tracker = ActivityTracker()

    # Startup health check — validate API keys and provider availability
    from trader.online.health_check import run_health_checks
    run_health_checks()

    # Launch news websocket subprocesses (controlled by WS_INSIGHT_SENTRY / WS_ALPACA)
    ws_procs: list[subprocess.Popen] = []
    _ws_scripts: list[tuple[bool, str, str]] = [
        (settings.ws_insight_sentry, "websocket/insight_sentry_news.py", "Insight Sentry"),
        (settings.ws_alpaca, "websocket/alpaca_news.py", "Alpaca"),
    ]
    for enabled, script, label in _ws_scripts:
        if enabled:
            p = subprocess.Popen(
                [sys.executable, "-u", script],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            ws_procs.append(p)
            print(f"{label} websocket started (pid {p.pid})")

    def _shutdown_ws():
        for p in ws_procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    atexit.register(_shutdown_ws)

    xstream: XStreamService | None = None
    if settings.x_stream_enabled and settings.x_stream_mode != "off":
        # Purge any stale X stream rules left by previous crashes
        try:
            from trader.xapi.client import XApiClient
            from trader.xapi.rules import delete_all_rules, get_rules

            _xc = XApiClient()
            _existing = get_rules(client=_xc)
            _rule_data = _existing.get("data") or []
            if _rule_data:
                _tags = [r.get("tag", r.get("id", "?")) for r in _rule_data]
                delete_all_rules(client=_xc)
                print(f"X stream startup: purged {len(_rule_data)} stale rule(s): {_tags}")
                bus.publish(PipelineEvent(
                    type="x_rules_purged",
                    payload={"count": len(_rule_data), "tags": _tags, "reason": "startup_cleanup"},
                ))
        except Exception as e:
            print(f"X stream startup: rule purge failed (non-fatal): {e}")

        # Conservative guardrails by default
        xstream = XStreamService(
            bus=bus,
            data_dir=Path(settings.data_dir),
            guards=XStreamGuards(
                max_posts_per_day=settings.x_max_posts_per_day,
                max_bursts_per_day=settings.x_max_bursts_per_day,
                burst_ttl_minutes=settings.x_burst_ttl_minutes,
                usage_poll_interval_s=settings.x_usage_poll_interval_s,
            ),
            enabled=True,
        )
        bus.publish(
            PipelineEvent(
                type="x_stream_configured",
                payload={
                    "enabled": True,
                    "mode": settings.x_stream_mode,
                    "burst_ttl_minutes": settings.x_burst_ttl_minutes,
                    "max_bursts_per_day": settings.x_max_bursts_per_day,
                    "max_posts_per_day": settings.x_max_posts_per_day,
                },
            )
        )
    t = threading.Thread(
        target=run_watch_loop,
        kwargs={"settings": settings, "db": db, "knowledge": knowledge, "bus": bus, "xstream": xstream, "tracker": tracker, "observer": observer},
        daemon=True,
    )
    t.start()

    # Backfill missing price_at maps (all offsets 5-20) in the background.
    def _price_at_maintenance() -> None:
        while True:
            try:
                updated = reconcile_missing_prices(db=db)
                if updated:
                    print(f"Price@5-20: backfilled {updated} snapshot(s).")
            except Exception as exc:
                print(f"Price@5-20 maintenance error: {exc}")
            time.sleep(60)

    threading.Thread(target=_price_at_maintenance, daemon=True).start()

    # Optional: process last N existing files on startup (background thread)
    if settings.backfill_on_start and observer.enabled:
        print("OBSERVER: backfill skipped")
    elif settings.backfill_on_start:
        def _backfill():
            files: list[Path] = []
            for _d in settings.news_watch_dirs:
                files.extend(p for p in Path(_d).glob("*.json") if p.is_file())
            files.sort()
            targets = files[-settings.backfill_limit :]
            total = len(targets)
            print(f"Backfill: processing {total} files in background...")
            bus.publish(PipelineEvent(type="backfill_started", payload={"total": total}))
            tracker.start(Activity(
                id="backfill_main",
                type="backfill",
                label=f"Backfill ({total} files)",
                progress=f"0/{total}",
            ))
            ok = 0
            for i, p in enumerate(targets, 1):
                print(f"Backfill [{i}/{total}]: {p.name}")
                # Extract symbols from the file for dashboard display
                try:
                    import json as _json
                    from trader.models.snapshot import normalize_news as _normalize
                    _news = _json.loads(p.read_text(encoding="utf-8"))
                    _normalize(_news)
                    _syms = [str(s) for s in (_news.get("symbols") or [])]
                except Exception:
                    _syms = []
                tracker.update("backfill_main", progress=f"{i}/{total}", symbols=_syms, label=p.name)
                bus.publish(PipelineEvent(type="backfill_progress", payload={"current": i, "total": total, "file": p.name}))
                try:
                    process_news_file(path=p, settings=settings, db=db, knowledge=knowledge, bus=bus, xstream=None, tracker=tracker)
                    ok += 1
                except Exception as exc:
                    print(f"Backfill [{i}/{total}] FAILED: {exc}")
                    bus.publish(PipelineEvent(type="manual_explore_error", payload={
                        "headline": p.name, "error": f"Backfill failed: {exc}",
                    }))
            tracker.finish("backfill_main")
            bus.publish(PipelineEvent(type="backfill_complete", payload={"succeeded": ok, "total": total}))
            print(f"Backfill complete: {ok}/{total} succeeded.")

        threading.Thread(target=_backfill, daemon=True).start()

    # Graceful shutdown: clean up X stream rules on exit
    if xstream is not None:
        def _shutdown_xstream():
            try:
                xstream.stop()
                print("X stream shutdown: cleanup complete.")
            except Exception as e:
                print(f"X stream shutdown: cleanup failed: {e}")

        atexit.register(_shutdown_xstream)

    app = create_app(settings=settings, bus=bus, db=db, knowledge=knowledge, tracker=tracker, observer=observer)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
