"""FollowUp data collector.

Periodically checks active follow-ups and runs scheduled data collections.
Each collection has two phases:
1. LLM query planner (gemini-3-flash) — proposes targeted search queries
2. Mechanical data gathering — price, news, web search, X search

Runs as a daemon thread alongside the news processing worker and WatchMonitor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from trader.config import Settings
from trader.db.database import (
    Database,
    get_active_follow_ups,
    update_follow_up,
)
from trader.market.data_service import MarketDataService
from trader.models.follow_up import (
    FollowUpBuilder,
    FollowUpCollection,
    parse_offset_to_minutes,
)
from trader.online.event_bus import EventBus, PipelineEvent

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")

_thread_local = threading.local()


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Return a per-thread event loop, creating one if needed."""
    loop: asyncio.AbstractEventLoop | None = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_local.loop = loop
    return loop


# ---------------------------------------------------------------------------
# Query planner output schema
# ---------------------------------------------------------------------------


class QueryPlan(BaseModel):
    """Structured output from the LLM query planner."""

    web_queries: list[str] = Field(description="Web search queries to run")
    x_queries: list[str] = Field(description="X/Twitter search queries to run")
    reasoning: str = Field(description="Brief explanation of query strategy")


# ---------------------------------------------------------------------------
# FollowUpCollector
# ---------------------------------------------------------------------------


class FollowUpCollector:
    """Check active follow-ups and run collections that are due."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        bus: EventBus,
        market: MarketDataService | None = None,
        tracker: "ActivityTracker | None" = None,
        online: "OnlineMode | None" = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.bus = bus
        self.market = market or MarketDataService()
        self.tracker = tracker
        self.online = online

    def run_cycle(self) -> None:
        """Check all active follow-ups, run collections that are due."""
        if self.online is not None and not self.online.enabled:
            return  # Skip entire cycle when offline

        for fu_dict in get_active_follow_ups(self.db):
            try:
                builder = FollowUpBuilder.from_dict(fu_dict)
                offset_label = builder.next_offset_label()

                if offset_label is None:
                    # All scheduled collections done — complete it
                    self._complete_follow_up(builder)
                    continue

                if self._is_collection_due(builder, offset_label):
                    self._run_collection(builder, offset_label)
            except Exception as e:
                fid = fu_dict.get("follow_up_id", "?")
                if DEBUG:
                    raise
                log.warning("Follow-up %s collection error: %s", fid, e)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _is_collection_due(
        self, builder: FollowUpBuilder, offset_label: str
    ) -> bool:
        """Check if the next collection is due based on offset from start."""
        offset_minutes = parse_offset_to_minutes(offset_label)
        started = datetime.fromisoformat(builder.started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds() / 60.0
        return elapsed >= offset_minutes

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def _run_collection(
        self, builder: FollowUpBuilder, offset_label: str
    ) -> None:
        """Run a single data collection for a follow-up."""
        settings = self.settings
        cost_so_far = builder.total_cost_usd
        budget_remaining = settings.follow_up_max_cost - cost_so_far
        cost_this = 0.0

        symbols = builder.symbols
        headline = builder.headline
        snapshot_id = builder.snapshot_id

        # Activity tracking
        _act_id = f"fu_{builder.follow_up_id}_{offset_label}"
        if self.tracker is not None:
            from trader.online.activity_tracker import Activity
            self.tracker.start(Activity(
                id=_act_id,
                type="follow_up_collection",
                label=f"{', '.join(symbols)} {offset_label}",
                symbols=symbols,
                progress=offset_label,
                detail={"follow_up_id": builder.follow_up_id, "snapshot_id": snapshot_id},
            ))

        def _aborted() -> bool:
            return self.tracker is not None and self.tracker.is_aborted(_act_id)

        if _aborted():
            if self.tracker is not None:
                self.tracker.finish(_act_id)
            return

        # Phase 1: LLM query planner
        query_plan = self._plan_queries(builder, offset_label)
        query_plan_dict = query_plan.model_dump()

        if _aborted():
            if self.tracker is not None:
                self.tracker.finish(_act_id)
            return

        # Phase 2: Mechanical data gathering

        # 2a: Price (free)
        price_data: dict[str, Any] = {}
        for sym in symbols:
            try:
                price_data[sym] = self.market.get_quote(sym)
            except Exception as e:
                price_data[sym] = {"error": str(e)}

        # 2b: News (free — yfinance + Finnhub merged)
        news_data: list[dict[str, Any]] = []
        for sym in symbols:
            try:
                news_data.append(self.market.get_company_news(sym, max_articles=10))
            except Exception as e:
                news_data.append({"symbol": sym, "error": str(e)})

        # 2c: Web search (paid — skip if over budget)
        web_results: list[dict[str, Any]] = []
        if budget_remaining > 0.01:
            for query in query_plan.web_queries[: settings.follow_up_web_searches]:
                if _aborted():
                    break
                result = _run_web_search(query)
                result["quality"] = _rate_quality(result)
                web_results.append(result)
                cost_this += result.get("cost_usd", 0.005)

        # 2d: X search (paid — skip if over budget)
        x_results: list[dict[str, Any]] = []
        if budget_remaining - cost_this > 0.01:
            for query in query_plan.x_queries[: settings.follow_up_x_searches]:
                if _aborted():
                    break
                result = _run_x_search(query)
                result["quality"] = _rate_quality(result)
                x_results.append(result)
                cost_this += result.get("cost_usd", 0.005)

        # If aborted after searches, clean up without saving
        if _aborted():
            if self.tracker is not None:
                self.tracker.finish(_act_id)
            return

        # Build collection and add to builder
        collection = FollowUpCollection(
            collected_at=datetime.now(tz=timezone.utc).isoformat(),
            offset_label=offset_label,
            price=price_data,
            news=news_data,
            web_results=web_results,
            x_results=x_results,
            query_plan=query_plan_dict,
            cost_usd=round(cost_this, 6),
        )
        builder.add_collection(collection)

        # Check if all collections are now done
        if builder.next_offset_label() is None:
            builder.complete()

        # Persist
        update_follow_up(
            self.db, builder.follow_up_id, builder.to_follow_up().to_dict()
        )

        # Finish activity tracking
        if self.tracker is not None:
            self.tracker.finish(_act_id)

        self.bus.publish(PipelineEvent(
            type="follow_up_collection",
            payload={
                "follow_up_id": builder.follow_up_id,
                "snapshot_id": snapshot_id,
                "offset_label": offset_label,
                "symbols": symbols,
                "cost_usd": cost_this,
                "web_queries": len(web_results),
                "x_queries": len(x_results),
            },
        ))

        if builder.status == "complete":
            self.bus.publish(PipelineEvent(
                type="follow_up_complete",
                payload={
                    "follow_up_id": builder.follow_up_id,
                    "snapshot_id": snapshot_id,
                    "symbols": symbols,
                    "total_cost_usd": builder.total_cost_usd,
                    "collections": len(builder.collections),
                },
            ))

    def _complete_follow_up(self, builder: FollowUpBuilder) -> None:
        """Mark a follow-up as complete (all collections done)."""
        builder.complete()
        update_follow_up(
            self.db, builder.follow_up_id, builder.to_follow_up().to_dict()
        )
        self.bus.publish(PipelineEvent(
            type="follow_up_complete",
            payload={
                "follow_up_id": builder.follow_up_id,
                "snapshot_id": builder.snapshot_id,
                "symbols": builder.symbols,
                "total_cost_usd": builder.total_cost_usd,
                "collections": len(builder.collections),
            },
        ))

    # ------------------------------------------------------------------
    # LLM Query Planner
    # ------------------------------------------------------------------

    def _plan_queries(
        self, builder: FollowUpBuilder, offset_label: str
    ) -> QueryPlan:
        """Use gemini-3-flash to propose targeted search queries."""
        n_web = self.settings.follow_up_web_searches
        n_x = self.settings.follow_up_x_searches

        # Build context from prior collections
        prior_summary = _build_prior_summary(builder.collections)

        symbols_str = ", ".join(builder.symbols)
        prompt = (
            f"You are planning search queries for a follow-up data collection "
            f"on a stock event.\n\n"
            f"Event headline: {builder.headline}\n"
            f"Symbols: {symbols_str}\n"
            f"Reason for follow-up: {builder.reason}\n"
            f"Time since event: {offset_label}\n"
        )

        config = builder.config
        if config.get("direction"):
            prompt += (
                f"Original prediction: {config['direction']} "
                f"({config.get('confidence', '?')}%) — "
                f"{config.get('key_catalyst', 'N/A')}\n"
            )
        if config.get("findings_summary"):
            prompt += f"Key findings: {config['findings_summary']}\n"

        if prior_summary:
            prompt += f"\nPrior collections:\n{prior_summary}\n"

        prompt += (
            f"\nGenerate exactly {n_web} web search queries and {n_x} "
            f"X/Twitter search queries.\n"
            f"Focus on: what happened since the event, market reaction, new "
            f"developments, analyst commentary, related sector moves.\n"
            f"Avoid repeating queries that returned poor results previously.\n"
            f"Build on queries that worked well.\n"
            f"Make queries specific to the symbols and event — not generic."
        )

        try:
            model_name = f"google-gla:{self.settings.follow_up_planner_model}"
            planner = Agent(
                model_name,
                output_type=QueryPlan,
                system_prompt=(
                    "You plan targeted search queries for stock event follow-up "
                    "data collection. Output a structured QueryPlan with specific, "
                    "actionable queries."
                ),
            )
            result = _get_or_create_loop().run_until_complete(planner.run(prompt))
            return result.output
        except Exception as e:
            log.warning("Query planner failed, using fallback: %s", e)
            return _fallback_query_plan(builder, n_web, n_x)


# ---------------------------------------------------------------------------
# Standalone helpers (no self — testable in isolation)
# ---------------------------------------------------------------------------


def _build_prior_summary(collections: list[FollowUpCollection]) -> str:
    """Build a concise summary of prior collections for the planner."""
    if not collections:
        return ""
    lines: list[str] = []
    for c in collections:
        lines.append(f"  [{c.offset_label}] collected at {c.collected_at}")
        for wr in c.web_results:
            q = wr.get("query", "?")
            quality = wr.get("quality", "?")
            lines.append(f"    web: \"{q}\" → {quality}")
        for xr in c.x_results:
            q = xr.get("query", "?")
            quality = xr.get("quality", "?")
            lines.append(f"    x: \"{q}\" → {quality}")
    return "\n".join(lines)


def _fallback_query_plan(
    builder: FollowUpBuilder, n_web: int, n_x: int
) -> QueryPlan:
    """Template-based fallback if LLM planner fails."""
    symbols_str = " ".join(f"${s}" for s in builder.symbols)
    headline_short = builder.headline[:80] if builder.headline else "stock event"

    web_queries = [
        f"{symbols_str} latest news today",
        f"{symbols_str} analyst reaction {headline_short}",
        f"{symbols_str} price movement analysis",
    ][:n_web]

    x_queries = [
        f"{symbols_str} sentiment",
        f"{symbols_str} market reaction",
    ][:n_x]

    return QueryPlan(
        web_queries=web_queries,
        x_queries=x_queries,
        reasoning="Fallback template queries (LLM planner unavailable)",
    )


def _rate_quality(result: dict[str, Any]) -> str:
    """Rate search result quality using simple heuristic."""
    if result.get("error"):
        return "error"
    answer = result.get("answer", "")
    if len(answer) > 100:
        return "good"
    return "empty"


def _run_web_search(query: str) -> dict[str, Any]:
    """Run a web search via Grok API (direct httpx call)."""
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        return {"query": query, "error": "XAI_API_KEY not configured", "source": "web_search"}

    try:
        resp = httpx.post(
            "https://api.x.ai/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning"),
                "tools": [{"type": "web_search"}],
                "input": [{"role": "user", "content": query}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return _extract_search_result(data, query, "web_search")
    except Exception as e:
        return {"query": query, "error": str(e), "source": "web_search"}


def _run_x_search(query: str) -> dict[str, Any]:
    """Run an X/Twitter search via Grok API (direct httpx call)."""
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        return {"query": query, "error": "XAI_API_KEY not configured", "source": "x_search"}

    try:
        resp = httpx.post(
            "https://api.x.ai/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning"),
                "tools": [{"type": "x_search"}],
                "input": [{"role": "user", "content": query}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return _extract_search_result(data, query, "x_search")
    except Exception as e:
        return {"query": query, "error": str(e), "source": "x_search"}


def _compute_search_cost(usage: dict[str, Any] | None, source: str) -> float:
    """Compute token + per-call cost for a follow-up search call.

    The xAI API response ``usage`` dict contains ``input_tokens`` and
    ``output_tokens`` (which include server-side search processing tokens).
    We price those at the Grok model rate and add the per-call invocation fee.
    """
    from trader.llm.pricing import estimate_token_cost_grok, estimate_tool_cost

    tool_fee = 0.005  # fallback
    try:
        tool_fee = estimate_tool_cost(provider="grok", tool_name=source, calls=1)
    except KeyError:
        pass

    if not usage:
        return tool_fee

    model = os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning")
    try:
        token_cost = estimate_token_cost_grok(
            model,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        ).total_cost_usd
    except (KeyError, ValueError):
        token_cost = 0.0

    return round(token_cost + tool_fee, 6)


def _extract_search_result(
    data: dict[str, Any], query: str, source: str
) -> dict[str, Any]:
    """Extract text + citations from a Grok API response."""
    usage = data.get("usage")
    cost = _compute_search_cost(usage, source)

    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    annotations = content.get("annotations", [])
                    citations = [
                        a["url"]
                        for a in annotations
                        if a.get("type") == "url_citation" and a.get("url")
                    ]
                    result: dict[str, Any] = {
                        "query": query,
                        "answer": content["text"],
                        "citations": citations,
                        "source": source,
                        "cost_usd": cost,
                    }
                    if usage:
                        result["usage"] = usage
                    return result
    return {"query": query, "error": "No output in response", "source": source, "cost_usd": cost}


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


def collector_loop(collector: FollowUpCollector, interval_s: int = 300) -> None:
    """Periodic collection loop. Runs forever in a daemon thread."""
    while True:
        try:
            collector.run_cycle()
        except Exception as e:
            if DEBUG:
                raise
            log.warning("Follow-up collector cycle error: %s", e)
        time.sleep(interval_s)
