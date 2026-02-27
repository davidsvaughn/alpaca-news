"""Periodic skip-pattern reviewer.

Runs as a daemon thread on a configurable schedule.  Each cycle:
1. Queries recent triage decisions from SQLite.
2. Sends the batch to a powerful LLM (default gpt-5.2) for conservative
   pattern proposals.
3. Validates proposals through the existing quality gate and persists them.

The reviewer sees *all* triage outcomes (LLM-skipped, LLM-investigated,
pre-filter-skipped) and makes its own independent judgment — it may propose
patterns even for headlines that the triage LLM chose to investigate.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import text

from trader.config import Settings, infer_provider_from_model
from trader.db.database import Database
from trader.knowledge.store import KnowledgeStore, validate_skip_pattern
from trader.llm.client import LLMClient
from trader.llm.cost_tracker import CostTracker
from trader.llm.extract import extract_json
from trader.online.event_bus import EventBus, PipelineEvent

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")
_log = logging.getLogger(__name__)


# ── Prompt ──────────────────────────────────────────────────────────────

REVIEW_PROMPT = """\
You are a senior financial-news pattern analyst.

Your job is to review recent triage decisions and propose **new regex skip
patterns** that would let us cheaply pre-filter obvious noise headlines
*before* they reach the triage LLM, saving cost.

## Rules

1. **Conservative** — only propose a pattern when you see a *repeated* category
   of noise (multiple similar headlines), not a single one-off article.
2. **Generic regex** — patterns are applied with `re.search(pattern, text,
   re.IGNORECASE)`.  Write patterns that match the *category*, not a single
   headline.  Use alternation `(a|b)` and wildcards `.*` where appropriate.
3. **Safe** — NEVER propose a pattern that could match genuinely price-moving
   news.  The following categories must NEVER be suppressed:
   earnings/revenue results, guidance changes, M&A activity, FDA/regulatory
   decisions, C-suite changes, buybacks/dividends, legal/fraud news,
   bankruptcy, trading halts, activist investors, contract wins.
4. **No duplicates** — do not propose patterns already covered by the current
   skip patterns or investigate patterns listed below.
5. **Regex quality** — each pattern must be 6-80 characters, valid regex.
6. You may independently conclude that some "investigated" headlines were
   actually noise — your judgment supersedes the triage LLM's decision.

## Current skip patterns (already active)
{skip_patterns}

## Current investigate patterns (force-investigate, never skip)
{investigate_patterns}

## Recent headlines with triage outcomes

### LLM-skipped (triage LLM decided to skip these)
{llm_skipped}

### LLM-investigated (triage LLM decided to investigate these)
{llm_investigated}

### Pre-filter-skipped (already caught by existing patterns)
{prefilter_skipped}

## Response format

Return STRICT JSON:
{{
  "patterns": [
    {{"pattern": "<regex>", "reasoning": "<why this is safe noise>"}},
    ...
  ]
}}

If no new patterns are warranted, return {{"patterns": []}}.
"""


# ── DB query ────────────────────────────────────────────────────────────

def _get_recent_triage_decisions(
    db: Database, lookback_hours: int,
) -> list[dict[str, Any]]:
    """Fetch recent snapshots with datetime precision."""
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    sql = (
        "SELECT snapshot_json FROM snapshots "
        "WHERE created_at >= :cutoff "
        "ORDER BY created_at DESC "
        "LIMIT 500"
    )
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), {"cutoff": cutoff}).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            results.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return results


def _categorize_headlines(
    snapshots: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """Split snapshots into (llm_skipped, llm_investigated, prefilter_skipped) headline lists."""
    llm_skipped: list[str] = []
    llm_investigated: list[str] = []
    prefilter_skipped: list[str] = []

    for snap in snapshots:
        triage = snap.get("triage") or {}
        trigger = snap.get("trigger") or {}
        headline = trigger.get("headline", "")
        if not headline:
            continue

        action = triage.get("action", "")
        provider = triage.get("provider", "")

        if action == "skip" and provider == "pre-filter":
            prefilter_skipped.append(headline)
        elif action == "skip":
            llm_skipped.append(headline)
        elif action == "investigate":
            llm_investigated.append(headline)

    return llm_skipped, llm_investigated, prefilter_skipped


# ── Core reviewer ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class PatternReviewer:
    settings: Settings
    db: Database
    knowledge: KnowledgeStore
    bus: EventBus

    def run_cycle(self) -> None:
        """Run one review cycle."""
        started = time.time()

        # 1. Fetch recent triage decisions
        snapshots = _get_recent_triage_decisions(
            self.db, self.settings.pattern_review_lookback_hours,
        )
        if not snapshots:
            _log.debug("Pattern reviewer: no snapshots in lookback window, skipping")
            return

        llm_skipped, llm_investigated, prefilter_skipped = _categorize_headlines(snapshots)

        # Need at least some LLM decisions to review
        if not llm_skipped and not llm_investigated:
            _log.debug("Pattern reviewer: no LLM-triaged headlines, skipping")
            return

        # 2. Load current patterns
        skip_data = self.knowledge.load_skip_patterns()
        skip_keywords = skip_data.get("headline_keywords", [])
        inv_data = self.knowledge.load_investigate_patterns()
        inv_keywords = inv_data.get("headline_keywords", [])

        # 3. Build prompt
        prompt = REVIEW_PROMPT.format(
            skip_patterns=json.dumps(skip_keywords, indent=2),
            investigate_patterns=json.dumps(inv_keywords, indent=2),
            llm_skipped=_format_headlines(llm_skipped),
            llm_investigated=_format_headlines(llm_investigated),
            prefilter_skipped=_format_headlines(prefilter_skipped, max_items=30),
        )

        # 4. Call LLM
        cost_tracker = CostTracker(
            max_daily_cost=self.settings.max_daily_cost,
            max_cost_per_item=5.0,  # generous per-call limit
            debug=self.settings.debug,
        )
        llm = LLMClient(cost_tracker=cost_tracker)

        model = self.settings.pattern_review_model
        provider = infer_provider_from_model(model)

        try:
            if provider == "openai":
                res = llm.query_openai(
                    model=model, input_text=prompt,
                    stage="review", purpose="pattern_review",
                )
            elif provider == "grok":
                res = llm.query_grok(
                    model=model, input_text=prompt,
                    stage="review", purpose="pattern_review",
                )
            elif provider == "gemini":
                res = llm.query_gemini(
                    model=model, input_text=prompt,
                    stage="review", purpose="pattern_review",
                )
            else:
                _log.error("Pattern reviewer: unknown provider %r", provider)
                return
        except Exception as e:
            _log.error("Pattern reviewer: LLM call failed: %s", e)
            if DEBUG:
                raise
            return

        # 5. Parse response
        try:
            data = extract_json(res.text)
        except RuntimeError as e:
            _log.error("Pattern reviewer: could not parse LLM response: %s", e)
            return

        proposals = data.get("patterns", [])
        if not isinstance(proposals, list):
            _log.error("Pattern reviewer: 'patterns' is not a list")
            return

        # 6. Validate and persist
        accepted: list[str] = []
        rejected: list[tuple[str, str]] = []

        for item in proposals:
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern", "")).strip()
            reasoning = str(item.get("reasoning", ""))
            if not pattern:
                continue

            valid, reason = validate_skip_pattern(pattern)
            if not valid:
                rejected.append((pattern, reason))
                _log.info(
                    "Pattern reviewer: rejected %r (%s) — reasoning: %s",
                    pattern, reason, reasoning,
                )
                continue

            accepted.append(pattern)
            _log.info(
                "Pattern reviewer: accepted %r — reasoning: %s",
                pattern, reasoning,
            )

        added = 0
        if accepted:
            added = self.knowledge.append_skip_keywords(accepted)

        elapsed = round(time.time() - started, 2)
        cost = res.cost_usd

        _log.info(
            "Pattern reviewer: %d headlines reviewed (%d skipped, %d investigated), "
            "%d patterns proposed, %d accepted, %d rejected, %d new. "
            "Cost: $%.4f, elapsed: %.1fs",
            len(snapshots), len(llm_skipped), len(llm_investigated),
            len(proposals), len(accepted), len(rejected), added,
            cost, elapsed,
        )

        self.bus.publish(PipelineEvent(
            type="pattern_review_complete",
            payload={
                "headlines_reviewed": len(snapshots),
                "patterns_proposed": len(proposals),
                "patterns_accepted": len(accepted),
                "patterns_rejected": len(rejected),
                "patterns_new": added,
                "cost_usd": cost,
                "elapsed_s": elapsed,
            },
        ))


def _format_headlines(headlines: list[str], max_items: int = 100) -> str:
    """Format a list of headlines for the prompt."""
    if not headlines:
        return "(none)"
    items = headlines[:max_items]
    lines = [f"- {h}" for h in items]
    if len(headlines) > max_items:
        lines.append(f"... and {len(headlines) - max_items} more")
    return "\n".join(lines)


# ── Daemon loop ─────────────────────────────────────────────────────────

def reviewer_loop(reviewer: PatternReviewer, interval_s: int = 10800) -> None:
    """Periodic review loop. Runs forever in a daemon thread."""
    while True:
        try:
            reviewer.run_cycle()
        except Exception as e:
            if DEBUG:
                raise
            _log.error("Pattern reviewer cycle error: %s", e)
        time.sleep(interval_s)
