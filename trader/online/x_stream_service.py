"""X filtered stream background service (conservative BURST-first).

Design goals:
- Do not run unless explicitly enabled via settings/env.
- Strict guardrails to avoid blowing through credits.
- Provide a tiny in-memory cache for the explorer ("x_stream_cache" tool).
- Publish SSE events for visibility.
- LLM-based quality gate: after N tweets, check relevance and auto-retry
  with revised filter rules if content is noise.

We intentionally start with BURST mode only:
- For a given set of rules, connect to the stream for N minutes
- Collect matching posts into cache + optional JSONL log
- Stop stream and (optionally) remove rules
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from trader.online.event_bus import EventBus, PipelineEvent
from trader.xapi.client import XApiClient
from trader.xapi.rules import StreamRule, add_rules, delete_rules, get_rules
from trader.xapi.stream import StreamParams, stream_posts
from trader.xapi.usage import get_usage


DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


@dataclass(frozen=True)
class XStreamGuards:
    max_posts_per_day: int = 1000
    max_bursts_per_day: int = 10
    burst_ttl_minutes: int = 5
    usage_poll_interval_s: int = 300


@dataclass(frozen=True)
class QualityVerdict:
    """Result of an LLM quality check on streamed tweets."""
    relevant: bool
    confidence: float
    reasoning: str
    revised_rule_values: list[str] | None = None


# Type alias for the quality callback.
# Receives (collected_posts, current_rules) → QualityVerdict
QualityCheckFn = Callable[[list[dict[str, Any]], list[StreamRule]], QualityVerdict]


class XStreamService:
    """Manages X filtered stream bursts and provides a cache."""

    def __init__(
        self,
        *,
        bus: EventBus,
        data_dir: Path,
        guards: XStreamGuards,
        enabled: bool,
    ) -> None:
        self.bus = bus
        self.client = XApiClient()
        self.data_dir = data_dir
        self.guards = guards
        self.enabled = enabled

        # Cache: symbol/tag -> deque of posts
        self._cache: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=500))
        self._lock = threading.Lock()

        # Burst control
        self._day = date.today()
        self._bursts_today = 0
        self._stop_evt = threading.Event()
        self._burst_stop_evt = threading.Event()
        self._current_burst_thread: threading.Thread | None = None

        # Usage polling state
        self._usage_last: dict[str, Any] | None = None
        self._usage_thread = threading.Thread(target=self._usage_loop, daemon=True)
        if self.enabled:
            self._usage_thread.start()

    # -----------------------------------------------------------------
    # Usage monitoring
    # -----------------------------------------------------------------

    def _usage_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                usage = get_usage(client=self.client, days=7)
                self._usage_last = usage
                self.bus.publish(PipelineEvent(type="x_usage", payload={"usage": usage}))
            except Exception as e:
                # Fail loud-ish: publish an event. In DEBUG, raise.
                if DEBUG:
                    raise
                self.bus.publish(PipelineEvent(type="x_usage_error", payload={"error": str(e)}))
            time.sleep(max(5, int(self.guards.usage_poll_interval_s)))

    def usage_snapshot(self) -> dict[str, Any] | None:
        return self._usage_last

    # -----------------------------------------------------------------
    # Cache API for explorer
    # -----------------------------------------------------------------

    def get_recent_posts(self, *, key: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._cache.get(key, deque()))
        return items[-limit:]

    # -----------------------------------------------------------------
    # Rule/burst execution
    # -----------------------------------------------------------------

    def _can_start_burst(self) -> None:
        if not self.enabled:
            raise RuntimeError("XStreamService is disabled")

        # Roll day counters
        today = date.today()
        if today != self._day:
            self._day = today
            self._bursts_today = 0

        if self._bursts_today >= self.guards.max_bursts_per_day:
            raise RuntimeError(
                f"X burst limit reached: bursts_today={self._bursts_today} max={self.guards.max_bursts_per_day}"
            )

    def start_burst(
        self,
        *,
        rules: list[StreamRule],
        remove_rules_after: bool = True,
        stream_params: StreamParams | None = None,
        quality_check: QualityCheckFn | None = None,
        quality_check_after: int = 5,
        max_quality_retries: int = 3,
    ) -> None:
        """Start a burst in a background thread.

        This method returns immediately. If ``quality_check`` is provided,
        the burst will evaluate tweet relevance after ``quality_check_after``
        tweets arrive. On failure, it auto-retries with LLM-suggested revised
        rules up to ``max_quality_retries`` times.
        """

        self._can_start_burst()

        if self._current_burst_thread and self._current_burst_thread.is_alive():
            raise RuntimeError("A burst is already running")

        ttl_s = float(max(1, self.guards.burst_ttl_minutes) * 60)
        self._bursts_today += 1
        self._burst_stop_evt.clear()

        t = threading.Thread(
            target=self._run_burst,
            kwargs={
                "rules": rules,
                "remove_rules_after": remove_rules_after,
                "ttl_s": ttl_s,
                "stream_params": stream_params,
                "quality_check": quality_check,
                "quality_check_after": quality_check_after,
                "max_quality_retries": max_quality_retries,
            },
            daemon=True,
        )
        self._current_burst_thread = t
        t.start()

    def _flush_posts(
        self,
        posts: list[dict[str, Any]],
        log_path: Path,
    ) -> None:
        """Persist, cache, and publish posts that passed the quality gate."""
        for obj in posts:
            # Persist JSONL
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

            # Cache by tag AND _all
            tags = [
                r.get("tag") for r in (obj.get("matching_rules") or []) if r.get("tag")
            ]
            with self._lock:
                self._cache["_all"].append(obj)
                for tag in tags:
                    self._cache[tag].append(obj)

            self.bus.publish(PipelineEvent(type="x_post", payload={"post": obj}))

    def _run_burst(
        self,
        *,
        rules: list[StreamRule],
        remove_rules_after: bool,
        ttl_s: float,
        stream_params: StreamParams | None,
        quality_check: QualityCheckFn | None = None,
        quality_check_after: int = 5,
        max_quality_retries: int = 3,
    ) -> None:
        log_dir = self.data_dir / "x" / "stream"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

        global_start = time.time()
        total_posts = 0
        current_rules = list(rules)

        self.bus.publish(PipelineEvent(
            type="x_burst_start",
            payload={"rules": [r.to_dict() for r in current_rules]},
        ))

        for attempt in range(max_quality_retries + 1):
            # Shared TTL across retries
            elapsed = time.time() - global_start
            remaining_ttl = ttl_s - elapsed
            if remaining_ttl <= 0 or self._burst_stop_evt.is_set():
                break

            # 1) Add current rules
            add_resp = add_rules(client=self.client, rules=current_rules, dry_run=False)
            created: list[dict[str, Any]] = list(add_resp.get("data") or [])
            created_ids = [str(x.get("id")) for x in created if x.get("id")]

            # 2) Stream + quality gate
            # Posts are BUFFERED until quality check passes (fail-closed).
            collected: list[dict[str, Any]] = []
            quality_passed = quality_check is None  # no check = auto-pass
            verdict: QualityVerdict | None = None

            try:
                for obj in stream_posts(client=self.client, params=stream_params, stop_after_s=remaining_ttl):
                    if self._burst_stop_evt.is_set():
                        break

                    total_posts += 1
                    collected.append(obj)

                    # Guardrail: if posts explode, stop early
                    if total_posts > self.guards.max_posts_per_day:
                        raise RuntimeError(
                            f"X posts guard tripped: total_posts={total_posts} max={self.guards.max_posts_per_day}"
                        )

                    if quality_passed:
                        # Already passed — flush this post immediately
                        self._flush_posts([obj], log_path)
                    # else: buffered silently, waiting for quality check

                    # Quality gate: check once per attempt after N tweets
                    if (
                        not quality_passed
                        and len(collected) >= quality_check_after
                    ):
                        try:
                            verdict = quality_check(collected, current_rules)  # type: ignore[misc]
                            self.bus.publish(PipelineEvent(
                                type="x_burst_quality",
                                payload={
                                    "relevant": verdict.relevant,
                                    "confidence": verdict.confidence,
                                    "reasoning": verdict.reasoning,
                                    "revised_rule_values": verdict.revised_rule_values,
                                    "posts_checked": len(collected),
                                    "attempt": attempt + 1,
                                    "max_retries": max_quality_retries,
                                },
                            ))
                            if verdict.relevant:
                                quality_passed = True
                                # Flush buffered posts now that quality is confirmed
                                self._flush_posts(collected, log_path)
                            else:
                                break  # stop streaming, will retry below
                        except Exception as e:
                            if DEBUG:
                                raise
                            self.bus.publish(PipelineEvent(
                                type="x_burst_quality_error",
                                payload={"error": str(e), "attempt": attempt + 1},
                            ))
                            # FAIL-CLOSED: quality check error = stop this attempt
                            break

                    # Global TTL check
                    if time.time() - global_start >= ttl_s:
                        break
            finally:
                # Always clean up this attempt's rules
                if remove_rules_after and created_ids:
                    try:
                        delete_rules(client=self.client, ids=created_ids, dry_run=False)
                    except Exception as e:
                        if DEBUG:
                            raise
                        self.bus.publish(PipelineEvent(
                            type="x_burst_rule_delete_error",
                            payload={"error": str(e)},
                        ))

            # Decide: success, give up, or retry
            if quality_passed:
                break  # quality passed (or no check configured)

            if verdict is None:
                break  # quality check never triggered (too few posts) or error

            # Retry with revised rules if available
            if verdict.revised_rule_values and attempt < max_quality_retries:
                current_rules = [
                    StreamRule(value=v, tag=f"qc_{attempt + 1}_{i}")
                    for i, v in enumerate(verdict.revised_rule_values)
                ]
            else:
                break  # no suggestions or max retries reached

        # Final cleanup: usage snapshot + burst_end event
        try:
            usage = get_usage(client=self.client, days=7)
            self._usage_last = usage
            self.bus.publish(PipelineEvent(type="x_usage", payload={"usage": usage}))
        except Exception as e:
            if DEBUG:
                raise
            self.bus.publish(PipelineEvent(type="x_usage_error", payload={"error": str(e)}))

        self.bus.publish(
            PipelineEvent(
                type="x_burst_end",
                payload={
                    "posts_seen": total_posts,
                    "duration_s": round(time.time() - global_start, 3),
                    "attempts": attempt + 1,
                },
            )
        )

    def stop_burst(self) -> None:
        """Signal the current burst to stop early."""
        self._burst_stop_evt.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._burst_stop_evt.set()


def build_rules_for_symbols(
    *,
    symbols: list[str],
    company_names: dict[str, str] | None = None,
) -> list[StreamRule]:
    """Build X stream filter rules for stock symbols.

    Strategy:
    - Always include $TICKER cashtag (high signal)
    - For short/ambiguous tickers (< 5 chars), also require context:166.*
      (X Stocks domain) to ensure financial relevance
    - If a company name is provided, include it as an alternative match
    - Only include bare TICKER word for longer/uncommon tickers (5+ chars)
    - Exclude retweets, English only

    The context:166.* operator requires Pro tier. If not available, the
    quality gate serves as fallback.
    """
    names = company_names or {}
    out: list[StreamRule] = []
    for s in symbols[:3]:  # cap tightness in the rule set
        sym = s.strip().upper()
        if not sym:
            continue

        name = names.get(sym)

        if len(sym) >= 5:
            # Longer tickers are unlikely to be common words
            value = f"({sym} OR ${sym}) -is:retweet lang:en"
        elif name:
            # Short ticker + known company name: use both for better signal
            value = f"(${sym} OR \"{name}\") -is:retweet lang:en"
        else:
            # Short ticker, no company name: require Stocks domain context
            # (context:166.* = X's ML classification for stock-related content)
            # Falls back to cashtag-only if context: operator unavailable
            value = f"${sym} context:166.* -is:retweet lang:en"
        out.append(StreamRule(value=value, tag=sym))
    return out
