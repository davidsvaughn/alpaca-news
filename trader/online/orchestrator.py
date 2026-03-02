"""Online orchestrator.

Watches configured news output directories for new files and produces sealed Snapshots.

Design:
- Watchdog enqueues file paths into a :class:`queue.Queue`.
- A separate worker thread dequeues and processes them sequentially.
  This prevents blocking the watchdog thread during LLM API calls.
- Uses :class:`SnapshotBuilder` to accumulate data and ``.seal()`` a frozen
  Snapshot at the end.
- Snapshot IDs are deterministic (derived from article id or filename hash) so that
  backfill is idempotent.

Exploration uses the multi-agent PydanticAI pipeline (Grok → OpenAI → Gemini)
via ``agent_pipeline.run_pipeline()``.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from trader.config import Settings, infer_provider_from_model
from trader.db.database import (
    Database,
    count_holding_watches,
    delete_snapshot,
    get_recently_explored_symbols,
    insert_snapshot,
    insert_watch,
    is_mock_snapshot,
    snapshot_exists,
)
from trader.knowledge.store import KnowledgeStore
from trader.llm.client import LLMClient
from trader.llm.cost_tracker import CostTracker
from trader.llm.mock import MockLLMClient
from trader.market.data_service import MarketDataService
from trader.models.snapshot import SnapshotBuilder, Trigger, deterministic_snapshot_id, normalize_news, snapshot_id_for_symbol
from trader.models.watch import WatchBuilder
from trader.evidence.acquirer import acquire_from_traces
from trader.online.agent_pipeline import (
    PipelineConfig,
    AgentSpec,
    run_pipeline,
    estimate_pipeline_cost,
    _extract_model_for_pricing,
)
from trader.online.triage import TriageDecision, run_triage
from trader.online.activity_tracker import JobAborted
from trader.online.event_bus import EventBus, PipelineEvent
from trader.online.price_10min import capture_price_10min_for_snapshot
from trader.online.x_stream_service import QualityVerdict, XStreamService, build_rules_for_symbols

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")

# Semaphore to limit concurrent triage LLM calls.  Initialised by
# ``run_watch_loop`` before worker threads start.
_triage_semaphore: threading.Semaphore | None = None


def _describe_api_error(e: Exception) -> str:
    """Extract provider, status, and actionable info from API errors."""
    try:
        from openai import APIStatusError

        if isinstance(e, APIStatusError):
            url = str(getattr(e, "response", None) and e.response.url or "")
            status = getattr(e, "status_code", "?")
            if "x.ai" in url:
                provider = "xAI/Grok"
            elif "openai.com" in url:
                provider = "OpenAI"
            else:
                provider = f"OpenAI-compatible ({url})"
            hint = ""
            if status == 429:
                hint = " → Check credits/spending limit on provider dashboard"
            return f"[{provider}] HTTP {status}{hint}: {e}"
    except ImportError:
        pass

    error_str = str(e).lower()
    if "google" in error_str or "gemini" in error_str:
        return f"[Gemini/Google] {e}"

    return str(e)

# Thread-local storage for persistent event loops.
# httpx/PydanticAI cache loop references internally; closing the loop between
# calls leaves stale refs that cause "RuntimeError: Event loop is closed".
_thread_local = threading.local()


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Return a per-thread event loop, creating one if needed."""
    loop: asyncio.AbstractEventLoop | None = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_local.loop = loop
    return loop


# Max age (minutes) for news to be considered "fresh" enough for X stream burst.
# Older news should rely on x_search (historical) rather than live streaming.
_X_STREAM_MAX_NEWS_AGE_MINUTES = 15


def _news_is_fresh(trigger: "Trigger", max_age_minutes: int = _X_STREAM_MAX_NEWS_AGE_MINUTES) -> bool:
    """Return True if the news trigger is recent enough for live X streaming."""
    ts = trigger.timestamp
    if not ts:
        return False  # no timestamp → can't verify freshness → skip stream
    try:
        # Timestamps are ISO 8601 (e.g. "2026-02-12T15:30:00Z")
        news_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - news_dt).total_seconds() / 60
        return age_minutes <= max_age_minutes
    except (ValueError, TypeError):
        return False


# Cache: {date_str: is_trading_day}
_trading_day_cache: dict[str, bool] = {}


def _is_market_open() -> bool:
    """Check if US equity market is in regular hours (handles holidays via Schwab)."""
    from zoneinfo import ZoneInfo

    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    today_str = now_et.strftime("%Y-%m-%d")

    # Is today a trading day? (cached per day, Schwab → weekday fallback)
    if today_str not in _trading_day_cache:
        try:
            from trader.market.schwab_client import SchwabMarketClient

            client = SchwabMarketClient()
            hours = client.get_market_hours("equity")
            _trading_day_cache[today_str] = bool(hours.get("is_open"))
        except Exception:
            # Schwab unavailable → fall back to weekday check (misses holidays)
            _trading_day_cache[today_str] = now_et.weekday() < 5

    if not _trading_day_cache[today_str]:
        return False

    # Regular hours: 9:30 AM – 4:00 PM ET
    t = now_et.hour * 60 + now_et.minute
    return 570 <= t < 960  # 9*60+30=570, 16*60=960


def _make_quality_callback(
    *,
    headline: str,
    symbols: list[str],
    summary: str,
    llm: object,
    model: str,
) -> "Callable[[list[dict[str, Any]], list], QualityVerdict]":
    """Create a closure that checks stream quality against news context."""
    from trader.online.stream_quality import check_stream_quality

    def callback(posts: list[dict[str, Any]], current_rules: list) -> QualityVerdict:
        # MockLLMClient doesn't have query_gemini — return "relevant"
        if not hasattr(llm, "query_gemini"):
            return QualityVerdict(relevant=True, confidence=1.0, reasoning="mock mode")

        return check_stream_quality(
            posts=posts,
            headline=headline,
            symbols=symbols,
            summary=summary,
            current_rules=current_rules,
            llm=llm,  # type: ignore[arg-type]
            model=model,
        )

    return callback


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def _load_news_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot_path(settings: Settings, snapshot_id: str) -> Path:
    return Path(settings.snapshots_dir) / f"{snapshot_id}.json"


def _extract_entry_price(snapshot: Any, symbol: str) -> float | None:
    """Extract the last trade price for a symbol from the snapshot's price_context."""
    pc = snapshot.price_context
    if not pc or not isinstance(pc, dict):
        return None
    # price_context is {symbol: {quote data}} or a flat dict with per-symbol keys
    sym_data = pc.get(symbol) or pc.get(symbol.upper())
    if isinstance(sym_data, dict):
        # Try common keys from Schwab/yfinance quote data
        for key in ("lastPrice", "last_price", "regularMarketPrice", "close"):
            val = sym_data.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
    return None


def _build_mock_pipeline_config() -> PipelineConfig:
    """Build a pipeline config using PydanticAI's TestModel (no API calls)."""
    from pydantic_ai.models.test import TestModel

    return PipelineConfig(
        agents=[
            AgentSpec(name="mock_1", model=TestModel(), builtin_tools=[],
                      role_description="Mock investigator 1."),
            AgentSpec(name="mock_2", model=TestModel(), builtin_tools=[],
                      role_description="Mock investigator 2."),
            AgentSpec(name="mock_final", model=TestModel(), builtin_tools=[],
                      role_description="Mock final analyst.", is_final=True),
        ],
        max_rounds=1,
        request_limit=10,
        tool_calls_limit=20,
    )


def _run_single_exploration(
    *,
    symbol: str,
    all_symbols: list[str],
    snap_id: str,
    news: dict[str, Any],
    trigger: Trigger,
    triage_dict: dict[str, Any],
    settings: Settings,
    db: Database,
    bus: EventBus,
    xstream: XStreamService | None,
    tracker: "ActivityTracker | None",
    llm: object,
) -> None:
    """Run one exploration pipeline for a single symbol, producing one snapshot."""
    builder = SnapshotBuilder(trigger=trigger, snapshot_id=snap_id)
    builder.set_triage(triage_dict)

    # Per-exploration cost tracker (independent budget per symbol)
    cost_tracker = CostTracker(
        max_daily_cost=settings.max_daily_cost,
        max_cost_per_item=settings.max_cost_per_news_item,
        debug=settings.debug,
    )
    cost_tracker.reset_item()

    # Activity tracking
    _act_id = f"explore_{snap_id}"
    if tracker is not None:
        from trader.online.activity_tracker import Activity
        tracker.start(Activity(
            id=_act_id,
            type="exploration",
            label=f"{symbol}: {trigger.headline[:70]}" if trigger.headline else symbol,
            symbols=[symbol],
            progress="exploring",
            stage_started_at=datetime.now(tz=timezone.utc).isoformat(),
            detail={"snapshot_id": snap_id},
        ))

    def _check_abort() -> None:
        if tracker is not None and tracker.is_aborted(_act_id):
            raise JobAborted(f"Job {_act_id} aborted by user")

    symbols = all_symbols
    signal = None

    # Optional: start a conservative X stream burst (runs in background).
    if (
        xstream is not None
        and settings.x_stream_enabled
        and settings.x_stream_mode == "burst"
        and triage_dict.get("confidence", 0) >= settings.x_min_triage_confidence_for_burst
        and symbols
        and _news_is_fresh(trigger)
        and (not settings.x_stream_market_hours_only or _is_market_open())
    ):
        try:
            rules = build_rules_for_symbols(symbols=symbols)
            if rules:
                quality_cb = None
                if settings.x_stream_quality_check_enabled:
                    quality_cb = _make_quality_callback(
                        headline=trigger.headline,
                        symbols=symbols,
                        summary=trigger.summary or "",
                        llm=llm,
                        model=settings.x_stream_quality_check_model,
                    )
                xstream.start_burst(
                    rules=rules,
                    remove_rules_after=True,
                    quality_check=quality_cb,
                    quality_check_after=settings.x_stream_quality_check_after,
                    max_quality_retries=settings.x_stream_quality_max_retries,
                )
        except Exception as e:
            if DEBUG:
                raise
            bus.publish(PipelineEvent(type="x_burst_start_error", payload={"error": str(e), "symbols": symbols}))

    # Market data service (Schwab + yfinance fallback)
    market: MarketDataService | None = None
    try:
        market = MarketDataService()
    except Exception as e:
        if DEBUG:
            raise
        print(f"WARN: MarketDataService init failed: {e}")
        market = None

    # Start Schwab streaming for real-time candle capture
    if market is not None and market.schwab_available and symbols:
        try:
            market._schwab.start_stream(symbols)
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Schwab stream start failed: {e}")

    # Capture market/price context into the Snapshot
    if market is not None:
        try:
            builder.set_market_context(market.build_market_context())
            if symbols:
                builder.set_price_context(market.build_price_context(symbols))
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Market context capture failed: {e}")

    # Wire Finnhub rate-limit callback to dashboard
    if tracker is not None:
        from trader.market.finnhub_client import set_rate_limit_callback
        set_rate_limit_callback(
            lambda msg: tracker.update(_act_id, progress=msg)
        )

    # --- Run multi-agent pipeline ---
    pipeline_config: PipelineConfig | None = None
    if settings.mock_llm:
        pipeline_config = _build_mock_pipeline_config()

    def _on_stage(name: str, idx: int, total: int) -> None:
        if tracker is not None:
            tracker.update(
                _act_id,
                progress=f"{name} ({idx}/{total})",
                stage_started_at=datetime.now(tz=timezone.utc).isoformat(),
            )

    pipeline_result = None
    try:
        loop = _get_or_create_loop()
        pipeline_result = loop.run_until_complete(run_pipeline(
            news=news,
            symbols=symbols,
            market=market,
            config=pipeline_config,
            x_stream_service=xstream,
            on_stage=_on_stage if tracker is not None else None,
            abort_check=_check_abort,
        ))
    except JobAborted:
        bus.publish(PipelineEvent(
            type="exploration_aborted",
            payload={
                "snapshot_id": snap_id,
                "symbols": symbols,
                "headline": trigger.headline,
            },
        ))
        if tracker is not None:
            tracker.finish(_act_id)
        return
    except Exception as e:
        # Pipeline crashed entirely — save what we have
        tb = traceback.format_exc()
        bus.publish(PipelineEvent(
            type="exploration_error",
            payload={
                "snapshot_id": snap_id,
                "error": f"{type(e).__name__}: {e}",
                "traceback": tb,
                "symbols": symbols,
                "headline": trigger.headline,
            },
        ))
        print(f"ERROR: Pipeline crashed for {snap_id}: {e}\n{tb}")

    if pipeline_result is not None:
        # Add all tool traces from the pipeline
        for trace in pipeline_result.all_tool_traces:
            builder.add_tool_trace(trace)

        # Store pre-fetched market data text for export display
        builder.prefetched_market_data = pipeline_result.prefetched_market_data

        # Store agent rounds (findings, usage, model) for training data
        for rnd in pipeline_result.rounds:
            builder.add_round(rnd)

        # Compute dollar cost from token usage and feed to CostTracker
        for rnd in pipeline_result.rounds:
            agent_name = rnd["agent"]
            model_string = rnd.get("model", agent_name)
            usage = rnd.get("usage", {})
            provider, raw_model = _extract_model_for_pricing(agent_name, model_string)

            # Build tools_used list from per-type counters set by runners
            tools_used: list[str] = (
                ["web_search"] * int(usage.get("web_search_calls") or 0)
                + ["x_search"] * int(usage.get("x_search_calls") or 0)
            )

            try:
                cost_tracker.log_llm_call(
                    provider=provider,  # type: ignore[arg-type]
                    model=raw_model,
                    usage=usage,
                    tools_used=tools_used,
                    stage="explore",
                    purpose=f"agent_{agent_name}",
                )
            except Exception as e:
                # Don't fail the pipeline on cost estimation errors
                if DEBUG:
                    print(f"WARN: Cost estimation failed for agent {agent_name}: {e}")

        # Update activity with pipeline cost
        if tracker is not None:
            agents_done = len(pipeline_result.rounds)
            tracker.update(_act_id, progress=f"agent {agents_done}/{agents_done}", cost_usd=cost_tracker.item_spent)

        # Store the TradingSignal as the snapshot prediction (if produced)
        signal = pipeline_result.signal
        if signal is not None:
            builder.prediction = signal.model_dump()

        bus.publish(PipelineEvent(
            type="exploration_complete",
            payload={
                "snapshot_id": snap_id,
                "symbols": symbols,
                "headline": trigger.headline,
                "direction": signal.direction if signal else None,
                "confidence": signal.confidence if signal else None,
                "agents": len(pipeline_result.rounds),
                "tool_calls": len(pipeline_result.all_tool_traces),
                "rounds_completed": pipeline_result.rounds_completed,
            },
        ))

        # Optional: explicit acquisition of web evidence for auditability.
        if settings.evidence_acquire_enabled:
            try:
                ar = acquire_from_traces(
                    traces=pipeline_result.all_tool_traces,
                    evidence_root=Path(settings.data_dir) / "evidence",
                    max_docs=settings.evidence_max_docs_per_item,
                    extractor=settings.evidence_extractor,
                )
                # Add acquisition traces after exploration traces
                for t in ar.traces:
                    # Ensure hop indexes are monotonic in the final snapshot
                    t["hop_index"] = len(builder.tool_traces) + 1
                    builder.add_tool_trace(t)
            except Exception as e:
                if DEBUG:
                    raise
                bus.publish(PipelineEvent(type="evidence_acquire_error", payload={"error": str(e)}))

        # Capture X stream burst data (wait for burst to finish, then grab results)
        if xstream is not None and settings.x_stream_enabled and _news_is_fresh(trigger):
            try:
                xstream.wait_burst_complete(timeout=15)
                burst_result = xstream.get_burst_result()
                if burst_result:
                    all_posts = xstream.get_recent_posts(key="_all", limit=100)
                    burst_result["posts"] = all_posts
                    burst_result["posts_in_cache"] = len(all_posts)
                    builder.x_stream_burst = burst_result
            except Exception as e:
                if DEBUG:
                    raise
                bus.publish(PipelineEvent(type="x_cache_attach_error", payload={"error": str(e)}))

    # Stop Schwab streaming
    if market is not None and market.schwab_available:
        try:
            market._schwab.stop_stream()
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Schwab stop stream failed: {e}")

    # Override cost total with the tracker's authoritative figure.
    builder.set_cost_total(cost_tracker.item_spent)

    # Finish activity tracking (cost now reflected in sealed snapshot)
    if tracker is not None:
        tracker.update(_act_id, progress="sealing", cost_usd=cost_tracker.item_spent)

    # --- Seal snapshot ---
    snapshot = builder.seal()

    # Persist to DB first (SQLite rollback on crash = no orphaned files)
    insert_snapshot(db, snapshot=snapshot.to_dict())

    # Then persist to JSON file (atomic write; DB is source of truth)
    out_path = _snapshot_path(settings, snapshot.snapshot_id)
    snapshot.persist(out_path)

    bus.publish(
        PipelineEvent(type="snapshot_sealed", payload={
            "snapshot_id": snapshot.snapshot_id,
            "path": str(out_path),
            "symbols": symbols,
            "headline": trigger.headline,
            "direction": snapshot.prediction.get("direction") if snapshot.prediction else None,
            "confidence": snapshot.prediction.get("confidence") if snapshot.prediction else None,
        })
    )

    # Schedule delayed price capture for the snapshots table
    _delay_min = settings.price_delay_minutes
    _created_at = snapshot.created_at
    def _capture_price_10min():
        try:
            capture_price_10min_for_snapshot(
                db=db,
                snapshot_id=snap_id,
                symbol=symbol,
                created_at=_created_at,
                delay_minutes=_delay_min,
            )
        except Exception:
            pass  # best-effort — price_10min will just stay empty
    threading.Thread(target=_capture_price_10min, daemon=True).start()

    # Activity complete — cost now in sealed snapshot
    if tracker is not None:
        tracker.finish(_act_id)
        from trader.market.finnhub_client import set_rate_limit_callback
        set_rate_limit_callback(None)

    # --- Watch creation (if high confidence) ---
    if (
        settings.watch_enabled
        and signal is not None
        and signal.direction != "neutral"
        and signal.confidence >= settings.watch_confidence_threshold
    ):
        try:
            holding_count = count_holding_watches(db)
            if holding_count >= settings.watch_max_concurrent:
                if DEBUG:
                    print(f"SKIP watch: {holding_count} concurrent watches (max {settings.watch_max_concurrent})")
            else:
                entry_price = _extract_entry_price(snapshot, symbol)
                if entry_price is not None:
                    wb = WatchBuilder.create_from_signal(
                        snapshot_id=snapshot.snapshot_id,
                        symbol=symbol,
                        entry_price=entry_price,
                        signal=signal,
                    )
                    watch = wb.to_watch()
                    watch_path = Path(settings.data_dir) / "watches" / f"{watch.watch_id}.json"
                    insert_watch(db, watch=watch.to_dict())
                    watch.persist(watch_path)
                    bus.publish(PipelineEvent(
                        type="watch_created",
                        payload={
                            "watch_id": watch.watch_id,
                            "symbol": watch.symbol,
                            "direction": watch.entry.direction,
                            "confidence": watch.entry.confidence,
                            "entry_price": watch.entry.price,
                            "snapshot_id": snapshot.snapshot_id,
                        },
                    ))
                elif DEBUG:
                    print(f"SKIP watch: could not extract entry price for {symbol}")
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Watch creation failed: {e}")

    # --- Follow-up for no-buy decisions ---
    watch_was_created = (
        settings.watch_enabled
        and signal is not None
        and signal.direction != "neutral"
        and signal.confidence >= settings.watch_confidence_threshold
    )
    if settings.follow_up_enabled and not watch_was_created:
        try:
            from trader.db.database import insert_follow_up
            from trader.models.follow_up import FollowUpBuilder

            schedule = [s.strip() for s in settings.follow_up_schedule.split(",")]
            fu_config: dict[str, Any] = {
                "web_searches": settings.follow_up_web_searches,
                "x_searches": settings.follow_up_x_searches,
            }
            # Pass prediction context for the query planner
            if signal is not None:
                fu_config["direction"] = signal.direction
                fu_config["confidence"] = round(signal.confidence * 100)
                fu_config["key_catalyst"] = signal.key_catalyst
            # Summarize agent findings for query planner context
            if hasattr(snapshot, "rounds") and snapshot.rounds:
                findings = [r.get("findings", "") for r in snapshot.rounds if r.get("findings")]
                if findings:
                    fu_config["findings_summary"] = " | ".join(
                        f[:200] for f in findings
                    )[:600]

            fu_builder = FollowUpBuilder(
                snapshot_id=snapshot.snapshot_id,
                symbols=[symbol] + [s for s in symbols[1:3] if s != symbol],
                reason="no_buy",
                schedule=schedule,
                config=fu_config,
                headline=trigger.headline,
            )
            insert_follow_up(db, follow_up=fu_builder.to_follow_up().to_dict())
            bus.publish(PipelineEvent(
                type="follow_up_created",
                payload={
                    "follow_up_id": fu_builder.follow_up_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "symbols": [symbol],
                    "reason": "no_buy",
                    "schedule": schedule,
                },
            ))
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Follow-up creation failed: {e}")


def process_news_file(
    *,
    path: Path,
    settings: Settings,
    db: Database,
    knowledge: KnowledgeStore,
    bus: EventBus,
    xstream: XStreamService | None = None,
    tracker: "ActivityTracker | None" = None,
) -> None:
    news = _load_news_json(path)
    normalize_news(news)

    # Deterministic ID → idempotent on re-run
    snap_id = deterministic_snapshot_id(news, source_file=path.name)

    # Skip if already processed (backfill safety) — but replace mock snapshots
    if snapshot_exists(db, snap_id):
        if not settings.mock_llm and is_mock_snapshot(db, snap_id):
            delete_snapshot(db, snap_id)
            print(f"REPLACED mock snapshot: {snap_id} from {path}")
        else:
            if DEBUG:
                print(f"SKIP (already processed): {snap_id} from {path}")
            return

    trigger = Trigger(
        type=news.get("_news_type", "alpaca_news"),
        timestamp=str(news.get("created_at")) if news.get("created_at") else None,
        headline=str(news.get("headline") or ""),
        summary=str(news.get("summary") or "") if news.get("summary") else None,
        source=str(news.get("source") or "") if news.get("source") else None,
        symbols=[str(x) for x in (news.get("symbols") or [])],
        raw=news,
        source_file=path.name,
    )

    bus.publish(PipelineEvent(type="news_received", payload={
        "path": str(path),
        "snapshot_id": snap_id,
        "headline": trigger.headline,
        "symbols": trigger.symbols,
    }))

    # Activity tracking for triage phase
    _act_id = f"triage_{snap_id}"
    if tracker is not None:
        from trader.online.activity_tracker import Activity
        tracker.start(Activity(
            id=_act_id,
            type="exploration",
            label=trigger.headline[:80] if trigger.headline else path.name,
            symbols=trigger.symbols[:3],
            progress="queued",
            stage_started_at=datetime.now(tz=timezone.utc).isoformat(),
            detail={"snapshot_id": snap_id},
        ))

    # Abort check closure — raises JobAborted if the dashboard user clicked abort.
    def _check_abort() -> None:
        if tracker is not None and tracker.is_aborted(_act_id):
            raise JobAborted(f"Job {_act_id} aborted by user")

    triage_cost_tracker = CostTracker(
        max_daily_cost=settings.max_daily_cost,
        max_cost_per_item=settings.max_cost_per_news_item,
        debug=settings.debug,
    )
    triage_cost_tracker.reset_item()
    if settings.mock_llm:
        llm: object = MockLLMClient()
    else:
        llm = LLMClient(cost_tracker=triage_cost_tracker)

    # --- Stage 1: Triage ---
    def _run_triage_with_abort_poll() -> TriageDecision:
        """Run triage in a sub-thread, poll abort every 2 s, hard-timeout after ``triage_timeout_s``."""
        import concurrent.futures
        from contextlib import nullcontext

        sem = _triage_semaphore or nullcontext()
        hard_limit = settings.triage_timeout_s  # default 90 s

        with sem:
            if tracker is not None:
                tracker.update(_act_id, progress="triage",
                               stage_started_at=datetime.now(tz=timezone.utc).isoformat())
            _triage_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            triage_provider = infer_provider_from_model(settings.triage_model)
            _triage_future = _triage_pool.submit(
                run_triage,
                llm=llm,  # type: ignore[arg-type]
                provider=triage_provider,
                model=settings.triage_model,
                knowledge=knowledge,
                news=news,
                web_search=settings.triage_web_search,
            )
            _t0 = time.monotonic()
            try:
                while True:
                    try:
                        return _triage_future.result(timeout=2.0)
                    except concurrent.futures.TimeoutError:
                        _check_abort()  # raises JobAborted if flagged
                        elapsed = time.monotonic() - _t0
                        if elapsed > hard_limit:
                            _triage_future.cancel()
                            raise TimeoutError(
                                f"Triage timed out after {elapsed:.0f}s "
                                f"(limit {hard_limit:.0f}s)"
                            )
            finally:
                _triage_pool.shutdown(wait=False)

    triage: TriageDecision
    incoming_symbols = [str(s).strip().upper() for s in (news.get("symbols") or []) if str(s).strip()]
    cooldown_minutes = max(0, int(settings.triage_symbol_cooldown_minutes))
    manual_explore_source = str(news.get("source") or "").strip().lower() == "dashboard_manual"
    recent_symbols: set[str] = set()
    if cooldown_minutes > 0 and incoming_symbols and not manual_explore_source:
        recent_symbols = set(get_recently_explored_symbols(db, lookback_minutes=cooldown_minutes))
        not_cooled = [s for s in incoming_symbols if s not in recent_symbols]
        if not not_cooled:
            # ALL symbols on cooldown — skip triage entirely
            cooled_unique = sorted(set(incoming_symbols) & recent_symbols)
            triage = TriageDecision(
                action="skip",
                confidence=0.99,
                reasoning=(
                    f"Pre-filter: all symbols on cooldown ({cooldown_minutes}m): "
                    f"{', '.join(cooled_unique)}"
                ),
                symbols=trigger.symbols,
                skip_patterns_learned=[],
            )
        else:
            try:
                triage = _run_triage_with_abort_poll()
            except JobAborted:
                bus.publish(PipelineEvent(
                    type="exploration_aborted",
                    payload={"snapshot_id": snap_id, "symbols": trigger.symbols, "headline": trigger.headline},
                ))
                if tracker is not None:
                    tracker.finish(_act_id)
                raise
            except TimeoutError as exc:
                print(f"TRIAGE TIMEOUT: {exc} — treating as skip")
                triage = TriageDecision(
                    action="skip", confidence=0.0,
                    reasoning=f"Triage timed out: {exc}",
                    symbols=trigger.symbols, skip_patterns_learned=[],
                )
    else:
        try:
            triage = _run_triage_with_abort_poll()
        except JobAborted:
            bus.publish(PipelineEvent(
                type="exploration_aborted",
                payload={"snapshot_id": snap_id, "symbols": trigger.symbols, "headline": trigger.headline},
            ))
            if tracker is not None:
                tracker.finish(_act_id)
            raise
        except (TimeoutError, Exception) as exc:
            if not isinstance(exc, TimeoutError):
                raise
            print(f"TRIAGE TIMEOUT: {exc} — treating as skip")
            triage = TriageDecision(
                action="skip", confidence=0.0,
                reasoning=f"Triage timed out: {exc}",
                symbols=trigger.symbols, skip_patterns_learned=[],
            )
    _was_pre_filter = triage.reasoning.startswith("Pre-filter:")
    triage_provider = (
        triage.provider
        if getattr(triage, "provider", None)
        else ("pre-filter" if _was_pre_filter else infer_provider_from_model(settings.triage_model))
    )
    triage_model = (
        triage.model
        if getattr(triage, "model", None)
        else ("keyword" if _was_pre_filter else settings.triage_model)
    )
    triage_usage = getattr(triage, "usage", {}) or {}
    triage_dict: dict[str, Any] = {
        "action": triage.action,
        "confidence": triage.confidence,
        "reasoning": triage.reasoning,
        "symbols": triage.symbols,
        "skip_patterns_learned": triage.skip_patterns_learned,
        "provider": triage_provider,
        "model": triage_model,
        "usage": {
            "input_tokens": int(triage_usage.get("input_tokens", 0) or 0),
            "output_tokens": int(triage_usage.get("output_tokens", 0) or 0),
            "total_tokens": int(triage_usage.get("total_tokens", 0) or 0),
            "reasoning_tokens": int(triage_usage.get("reasoning_tokens", 0) or 0),
            "tool_calls": int(triage_usage.get("tool_calls", 0) or 0),
        },
        "cost_usd": float(getattr(triage, "cost_usd", 0.0) or 0.0),
        "elapsed_s": float(getattr(triage, "elapsed_s", 0.0) or 0.0),
        "requests": int(getattr(triage, "requests", 0) or 0),
    }

    bus.publish(
        PipelineEvent(
            type="triage_decision",
            payload={
                "snapshot_id": snap_id,
                "action": triage.action,
                "confidence": triage.confidence,
                "symbols": triage.symbols,
                "reasoning": triage.reasoning,
            },
        )
    )

    # Check abort after triage returns (catches abort clicked during triage)
    try:
        _check_abort()
    except JobAborted:
        bus.publish(PipelineEvent(
            type="exploration_aborted",
            payload={"snapshot_id": snap_id, "symbols": trigger.symbols, "headline": trigger.headline},
        ))
        if tracker is not None:
            tracker.finish(_act_id)
        return

    # --- Handle skip (seal triage-only snapshot) ---
    if triage.action != "investigate":
        if tracker is not None:
            tracker.update(_act_id, progress="skip", cost_usd=triage_cost_tracker.item_spent,
                           stage_started_at=datetime.now(tz=timezone.utc).isoformat())
        builder = SnapshotBuilder(trigger=trigger, snapshot_id=snap_id)
        builder.set_triage(triage_dict)
        builder.set_cost_total(triage_cost_tracker.item_spent)
        snapshot = builder.seal()
        insert_snapshot(db, snapshot=snapshot.to_dict())
        out_path = _snapshot_path(settings, snapshot.snapshot_id)
        snapshot.persist(out_path)
        bus.publish(PipelineEvent(type="snapshot_sealed", payload={
            "snapshot_id": snapshot.snapshot_id,
            "path": str(out_path),
            "symbols": trigger.symbols,
            "headline": trigger.headline,
            "direction": None,
            "confidence": None,
        }))
        if tracker is not None:
            tracker.finish(_act_id)
        return

    # --- Stage 2: Prepare symbols for exploration ---
    symbols = triage.symbols if triage.symbols else trigger.symbols

    # Per-symbol cooldown filtering (post-triage)
    if cooldown_minutes > 0 and recent_symbols and not manual_explore_source:
        symbols = [s for s in symbols if s.upper() not in recent_symbols]

    # Symbol-level filtering (blacklist + crypto detection)
    from trader.online.symbol_filter import filter_symbols
    symbols, filtered = filter_symbols(symbols, settings.data_dir, symbol_exchanges=news.get("symbol_exchanges"))
    if filtered:
        bus.publish(PipelineEvent(type="symbols_filtered", payload={
            "snapshot_id": snap_id,
            "filtered": filtered,
        }))
    if not symbols:
        bus.publish(PipelineEvent(type="symbols_all_filtered", payload={
            "snapshot_id": snap_id,
            "headline": trigger.headline,
            "filtered": filtered,
        }))
        if tracker is not None:
            tracker.finish(_act_id)
        return  # Nothing to investigate

    # Finish triage activity — each exploration gets its own
    if tracker is not None:
        tracker.update(_act_id, progress="exploring", cost_usd=triage_cost_tracker.item_spent,
                       stage_started_at=datetime.now(tz=timezone.utc).isoformat())
        tracker.finish(_act_id)

    # --- Launch top K explorations (sequential) ---
    explore_symbols = symbols[:max(1, settings.triage_max_symbols)]

    for sym in explore_symbols:
        sym_snap_id = snap_id if len(explore_symbols) == 1 else snapshot_id_for_symbol(snap_id, sym)

        # Per-symbol idempotency check
        if snapshot_exists(db, sym_snap_id):
            if not settings.mock_llm and is_mock_snapshot(db, sym_snap_id):
                delete_snapshot(db, sym_snap_id)
            else:
                continue

        # Primary symbol first, others as context
        ordered = [sym] + [s for s in symbols if s != sym]

        _run_single_exploration(
            symbol=sym,
            all_symbols=ordered,
            snap_id=sym_snap_id,
            news=news,
            trigger=trigger,
            triage_dict=triage_dict,
            settings=settings,
            db=db,
            bus=bus,
            xstream=xstream,
            tracker=tracker,
            llm=llm,
        )


# ---------------------------------------------------------------------------
# Watchdog + worker queue
# ---------------------------------------------------------------------------


class _NewsHandler(FileSystemEventHandler):
    """Enqueues new JSON file paths; does NOT process them inline."""

    def __init__(self, *, work_queue: queue.Queue[Path]) -> None:
        self._q = work_queue

    def on_created(self, event):  # type: ignore[override]
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".json":
            return
        # Small delay to let the writer finish flushing
        time.sleep(0.1)
        self._q.put(path)


def _worker_loop(
    *,
    work_queue: queue.Queue[Path],
    settings: Settings,
    db: Database,
    knowledge: KnowledgeStore,
    bus: EventBus,
    xstream: XStreamService | None = None,
    tracker: "ActivityTracker | None" = None,
    observer: "ObserverMode | None" = None,
) -> None:
    """Pull paths from the queue and process them one at a time."""
    while True:
        try:
            path = work_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            if observer is not None and observer.enabled:
                print(f"OBSERVER: skipped {path.name}")
                bus.publish(PipelineEvent(
                    type="observer_skipped",
                    payload={"path": str(path), "reason": "observer_mode"},
                ))
                continue

            process_news_file(
                path=path,
                settings=settings,
                db=db,
                knowledge=knowledge,
                bus=bus,
                xstream=xstream,
                tracker=tracker,
            )
        except JobAborted:
            print(f"ABORTED: {path.name}")
        except Exception as e:
            if DEBUG:
                raise
            print(f"ERROR processing {path}: {_describe_api_error(e)}")
        finally:
            work_queue.task_done()


def run_watch_loop(
    *,
    settings: Settings,
    db: Database,
    knowledge: KnowledgeStore,
    bus: EventBus,
    xstream: XStreamService | None = None,
    tracker: "ActivityTracker | None" = None,
    observer: "ObserverMode | None" = None,
) -> None:
    """Start watchdog observer + worker threads, block forever."""
    import threading

    # Initialise the triage concurrency limiter before spawning workers.
    global _triage_semaphore
    _triage_semaphore = threading.Semaphore(max(1, settings.triage_concurrency))

    watch_dirs = [Path(d) for d in settings.news_watch_dirs]
    for d in watch_dirs:
        d.mkdir(parents=True, exist_ok=True)

    work_q: queue.Queue[Path] = queue.Queue()

    # Worker threads process items from the queue
    num_workers = max(1, settings.max_parallel_explores)
    for i in range(num_workers):
        worker = threading.Thread(
            target=_worker_loop,
            kwargs={
                "work_queue": work_q,
                "settings": settings,
                "db": db,
                "knowledge": knowledge,
                "bus": bus,
                "xstream": xstream,
                "tracker": tracker,
                "observer": observer,
            },
            name=f"explorer-{i}",
            daemon=True,
        )
        worker.start()
    if num_workers > 1:
        print(f"Started {num_workers} parallel explorer threads")

    handler = _NewsHandler(work_queue=work_q)
    fs_observer = Observer()
    for d in watch_dirs:
        fs_observer.schedule(handler, str(d), recursive=False)
    fs_observer.start()
    bus.publish(PipelineEvent(type="watching", payload={"dir": ", ".join(str(d) for d in watch_dirs)}))

    # Monitoring thread for active watches
    if settings.watch_enabled:
        from trader.online.watcher import WatchMonitor, monitoring_loop

        monitor = WatchMonitor(settings=settings, db=db, bus=bus, observer=observer)
        monitor_thread = threading.Thread(
            target=monitoring_loop,
            args=(monitor,),
            daemon=True,
        )
        monitor_thread.start()
        bus.publish(PipelineEvent(type="monitoring_started", payload={}))

    # Follow-up collector thread
    if settings.follow_up_enabled:
        from trader.online.follow_up_collector import FollowUpCollector, collector_loop

        fu_collector = FollowUpCollector(settings=settings, db=db, bus=bus, tracker=tracker, observer=observer)
        fu_thread = threading.Thread(
            target=collector_loop,
            args=(fu_collector, settings.follow_up_collector_interval_s),
            daemon=True,
        )
        fu_thread.start()
        bus.publish(PipelineEvent(type="follow_up_collector_started", payload={}))

    # Pattern reviewer thread
    if settings.pattern_review_enabled:
        from trader.online.pattern_reviewer import PatternReviewer, reviewer_loop

        reviewer = PatternReviewer(settings=settings, db=db, knowledge=knowledge, bus=bus)
        reviewer_thread = threading.Thread(
            target=reviewer_loop,
            args=(reviewer, settings.pattern_review_interval_s),
            daemon=True,
        )
        reviewer_thread.start()
        bus.publish(PipelineEvent(type="pattern_reviewer_started", payload={}))

    try:
        while True:
            time.sleep(1)
    finally:
        fs_observer.stop()
        fs_observer.join()
