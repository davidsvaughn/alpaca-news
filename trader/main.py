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

# Load .env BEFORE any trader imports so module-level os.getenv() calls
# (e.g. ALPACA_EXTENDED_HOURS in market_hours.py) see the correct values.
from dotenv import load_dotenv
load_dotenv()

import uvicorn

from trader.config import load_settings
from trader.logging_config import setup_logging
from trader.online.feed_manager import FeedManager, FeedRegistry
from trader.online.online_mode import OnlineMode
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
    parser.add_argument("--offline", action="store_true",
                        help="Start offline (no new jobs launched)")
    args = parser.parse_args()

    _kill_stale_servers()
    setup_logging()

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

    # Check Schwab token status before launching anything heavy
    if os.getenv("SCHWAB_DISABLED", "false").lower() not in ("true", "1"):
        from trader.market.schwab_tokens import check_schwab_tokens, launch_reauth_terminal
        token_status = check_schwab_tokens()
        if token_status.needs_reauth:
            print("SCHWAB: Refresh token expired — launching reauth in a new terminal...")
            if launch_reauth_terminal():
                print("SCHWAB: Reauth terminal opened. Complete auth there, then restart the app.")
                print("SCHWAB: Waiting for reauth to complete (checking every 5s)...")
                import time as _time
                for _ in range(120):  # wait up to 10 minutes
                    _time.sleep(5)
                    new_status = check_schwab_tokens()
                    if not new_status.needs_reauth:
                        print("SCHWAB: Reauth successful! Continuing startup...")
                        break
                else:
                    print("SCHWAB: Timed out waiting for reauth. Continuing anyway (Schwab may not work).")
            else:
                print("SCHWAB: Could not open terminal. Run manually: uv run python scripts/schwab_reauth.py")
        elif token_status.warn_expiring:
            from datetime import datetime as _dt, timezone as _tz
            remaining = token_status.refresh_expires - _dt.now(_tz.utc)
            print(f"SCHWAB: Refresh token expiring in {str(remaining).split('.')[0]} — consider reauthorizing soon.")

    online = OnlineMode(enabled=not args.offline and settings.online)
    if settings.online_auto_market_hours:
        online.start_auto_market_hours()
        from trader.market.market_hours import ALPACA_EXTENDED_HOURS
        hours_label = "4:00-20:00" if ALPACA_EXTENDED_HOURS else "9:30-16:00"
        print(f"ONLINE: auto market hours enabled (ON during {hours_label} ET)")
    if not online.enabled:
        print("OFFLINE: no new jobs will be launched")
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
    feed_registry = FeedRegistry()
    _feeds_config: list[tuple[str, str, str, bool]] = [
        ("insight_sentry", "websocket/insight_sentry_news.py", "output/insight_sentry", settings.ws_insight_sentry),
        ("alpaca", "websocket/alpaca_news.py", "output/alpaca", settings.ws_alpaca),
    ]
    for name, script, watch_dir, enabled in _feeds_config:
        fm = FeedManager(name=name, script=script, watch_dir=watch_dir, enabled=enabled)
        if enabled:
            fm.start_subprocess_only()
        feed_registry.register(fm)

    atexit.register(feed_registry.shutdown_all)

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
        kwargs={"settings": settings, "db": db, "knowledge": knowledge, "bus": bus, "xstream": xstream, "tracker": tracker, "online": online, "feed_registry": feed_registry},
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
    if settings.backfill_on_start and not online.enabled:
        print("OFFLINE: backfill skipped")
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

    app = create_app(settings=settings, bus=bus, db=db, knowledge=knowledge, tracker=tracker, online=online, feed_registry=feed_registry)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
