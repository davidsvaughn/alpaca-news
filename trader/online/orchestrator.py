"""Online orchestrator.

Watches ``output/alpaca/*.json`` for new files and produces sealed Snapshots.

Design:
- Watchdog enqueues file paths into a :class:`queue.Queue`.
- A separate worker thread dequeues and processes them sequentially.
  This prevents blocking the watchdog thread during LLM API calls.
- Uses :class:`SnapshotBuilder` to accumulate data and ``.seal()`` a frozen
  Snapshot at the end.
- Snapshot IDs are deterministic (derived from the Alpaca article id) so that
  backfill is idempotent.

Exploration uses the multi-agent PydanticAI pipeline (Grok → OpenAI → Gemini)
via ``agent_pipeline.run_pipeline()``.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from trader.config import Settings
from trader.db.database import Database, insert_snapshot, insert_watch, count_holding_watches, snapshot_exists
from trader.knowledge.store import KnowledgeStore
from trader.llm.client import LLMClient
from trader.llm.cost_tracker import CostTracker
from trader.llm.mock import MockLLMClient
from trader.market.data_service import MarketDataService
from trader.models.snapshot import SnapshotBuilder, Trigger, deterministic_snapshot_id
from trader.models.watch import WatchBuilder
from trader.evidence.acquirer import acquire_from_traces
from trader.online.agent_pipeline import (
    PipelineConfig,
    AgentSpec,
    run_pipeline,
    estimate_pipeline_cost,
    _extract_model_for_pricing,
)
from trader.online.triage import run_triage
from trader.online.event_bus import EventBus, PipelineEvent
from trader.online.x_stream_service import XStreamService, build_rules_for_symbols

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


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


def process_news_file(
    *,
    path: Path,
    settings: Settings,
    db: Database,
    knowledge: KnowledgeStore,
    bus: EventBus,
    xstream: XStreamService | None = None,
) -> None:
    news = _load_news_json(path)

    # Deterministic ID → idempotent on re-run
    snap_id = deterministic_snapshot_id(news)

    # Skip if already processed (backfill safety)
    if snapshot_exists(db, snap_id):
        if DEBUG:
            print(f"SKIP (already processed): {snap_id} from {path}")
        return

    trigger = Trigger(
        type="alpaca_news",
        alpaca_timestamp=str(news.get("created_at")) if news.get("created_at") else None,
        headline=str(news.get("headline") or ""),
        summary=str(news.get("summary") or "") if news.get("summary") else None,
        source=str(news.get("source") or "") if news.get("source") else None,
        symbols=[str(x) for x in (news.get("symbols") or [])],
        raw=news,
    )

    builder = SnapshotBuilder(trigger=trigger, snapshot_id=snap_id)

    bus.publish(PipelineEvent(type="news_received", payload={"path": str(path), "snapshot_id": snap_id}))

    cost_tracker = CostTracker(
        max_daily_cost=settings.max_daily_cost,
        max_cost_per_item=settings.max_cost_per_news_item,
        debug=settings.debug,
    )
    cost_tracker.reset_item()
    if settings.mock_llm:
        llm: object = MockLLMClient()
    else:
        llm = LLMClient(cost_tracker=cost_tracker)

    # --- Stage 1: Triage ---
    triage = run_triage(
        llm=llm,  # type: ignore[arg-type]
        provider=settings.triage_provider,
        model=settings.triage_model,
        knowledge=knowledge,
        news=news,
    )
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

    if triage.skip_patterns_learned:
        knowledge.append_skip_keywords(triage.skip_patterns_learned)

    # --- Stage 2: Exploration (if investigate) ---
    signal = None  # set inside investigate block, used for watch creation
    if triage.action == "investigate":
        # Use triage-refined symbols (falls back to trigger symbols)
        symbols = triage.symbols if triage.symbols else trigger.symbols

        # Optional: start a conservative X stream burst (runs in background).
        # This is intentionally gated to avoid runaway usage/cost.
        if (
            xstream is not None
            and settings.x_stream_enabled
            and settings.x_stream_mode == "burst"
            and triage.confidence >= settings.x_min_triage_confidence_for_burst
            and symbols
        ):
            try:
                rules = build_rules_for_symbols(symbols=symbols)
                if rules:
                    xstream.start_burst(rules=rules, remove_rules_after=True)
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

        # --- Run multi-agent pipeline ---
        pipeline_config: PipelineConfig | None = None
        if settings.mock_llm:
            pipeline_config = _build_mock_pipeline_config()

        pipeline_result = asyncio.run(run_pipeline(
            news=news,
            symbols=symbols,
            market=market,
            config=pipeline_config,
            x_stream_service=xstream,
        ))

        # Add all tool traces from the pipeline
        for trace in pipeline_result.all_tool_traces:
            builder.add_tool_trace(trace)

        # Store agent rounds (findings, usage, model) for training data
        for rnd in pipeline_result.rounds:
            builder.add_round(rnd)

        # Compute dollar cost from token usage and feed to CostTracker
        for rnd in pipeline_result.rounds:
            agent_name = rnd["agent"]
            model_string = rnd.get("model", agent_name)
            usage = rnd.get("usage", {})
            provider, raw_model = _extract_model_for_pricing(agent_name, model_string)

            try:
                cost_tracker.log_llm_call(
                    provider=provider,  # type: ignore[arg-type]
                    model=raw_model,
                    usage=usage,
                    tools_used=[],
                    stage="explore",
                    purpose=f"agent_{agent_name}",
                )
            except Exception as e:
                # Don't fail the pipeline on cost estimation errors
                if DEBUG:
                    print(f"WARN: Cost estimation failed for agent {agent_name}: {e}")

        # Store the TradingSignal as the snapshot prediction
        signal = pipeline_result.signal
        builder.prediction = signal.model_dump()

        bus.publish(PipelineEvent(
            type="exploration_complete",
            payload={
                "snapshot_id": snap_id,
                "direction": signal.direction,
                "confidence": signal.confidence,
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

        # Optional: attach a small snapshot of X cache as evidence.
        # Note: because the burst runs asynchronously, this may be empty. It's still useful
        # for learning once we tune the timing/strategy.
        if xstream is not None and settings.x_stream_enabled:
            try:
                x_items = xstream.get_recent_posts(key="_all", limit=10)
                if x_items:
                    from trader.models.tool_trace import TraceExecution, new_tool_trace, utc_now_iso

                    builder.add_tool_trace(
                        new_tool_trace(
                            trace_id=f"trace_x_cache_{int(time.time())}",
                            hop_index=len(builder.tool_traces) + 1,
                            parent_trace_id=None,
                            decision_context={
                                "state_summary": "",
                                "reason_for_action": "Attach recent X stream cache posts",
                                "symbols": symbols,
                            },
                            action={
                                "tool": "x_stream_cache",
                                "provider": "xapi",
                                "query_template": "x_stream_cache_recent",
                                "query": "_all",
                                "filters": {"limit": 10},
                            },
                            execution=TraceExecution(model="xapi", start_time=utc_now_iso(), end_time=utc_now_iso(), cost_usd=0.0),
                            results=[{"source_type": "x_stream", "title": "x_post", "snippet": json.dumps(x, ensure_ascii=False)[:800]} for x in x_items],
                            extracted_signals={"posts_included": len(x_items)},
                            stop_signal={"should_stop": False, "reason": ""},
                        )
                    )
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
    # We intentionally keep SnapshotBuilder's per-tool breakdown, which reflects
    # the observed costs per executed action (web_search / x_search / market, etc.).
    builder.set_cost_total(cost_tracker.item_spent)

    # --- Seal snapshot ---
    snapshot = builder.seal()

    # Persist to JSON file
    out_path = _snapshot_path(settings, snapshot.snapshot_id)
    snapshot.persist(out_path)

    # Persist to DB (idempotent)
    insert_snapshot(db, snapshot=snapshot.to_dict())

    bus.publish(
        PipelineEvent(type="snapshot_sealed", payload={"snapshot_id": snapshot.snapshot_id, "path": str(out_path)})
    )

    # --- Stage 3: Watch creation (if high confidence) ---
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
                primary_symbol = snapshot.trigger.symbols[0] if snapshot.trigger.symbols else None
                entry_price = _extract_entry_price(snapshot, primary_symbol) if primary_symbol else None
                if entry_price is not None and primary_symbol is not None:
                    wb = WatchBuilder.create_from_signal(
                        snapshot_id=snapshot.snapshot_id,
                        symbol=primary_symbol,
                        entry_price=entry_price,
                        signal=signal,
                    )
                    watch = wb.to_watch()
                    watch_path = Path(settings.data_dir) / "watches" / f"{watch.watch_id}.json"
                    watch.persist(watch_path)
                    insert_watch(db, watch=watch.to_dict())
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
                    print(f"SKIP watch: could not extract entry price for {primary_symbol}")
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Watch creation failed: {e}")


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
) -> None:
    """Pull paths from the queue and process them one at a time."""
    while True:
        try:
            path = work_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            process_news_file(
                path=path,
                settings=settings,
                db=db,
                knowledge=knowledge,
                bus=bus,
                xstream=xstream,
            )
        except Exception as e:
            if DEBUG:
                raise
            print(f"ERROR processing {path}: {e}")
        finally:
            work_queue.task_done()


def run_watch_loop(
    *,
    settings: Settings,
    db: Database,
    knowledge: KnowledgeStore,
    bus: EventBus,
    xstream: XStreamService | None = None,
) -> None:
    """Start watchdog observer + worker thread, block forever."""
    import threading

    watch_dir = Path(settings.alpaca_output_dir)
    watch_dir.mkdir(parents=True, exist_ok=True)

    work_q: queue.Queue[Path] = queue.Queue()

    # Worker thread processes items from the queue
    worker = threading.Thread(
        target=_worker_loop,
        kwargs={
            "work_queue": work_q,
            "settings": settings,
            "db": db,
            "knowledge": knowledge,
            "bus": bus,
            "xstream": xstream,
        },
        daemon=True,
    )
    worker.start()

    handler = _NewsHandler(work_queue=work_q)
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()
    bus.publish(PipelineEvent(type="watching", payload={"dir": str(watch_dir)}))

    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
