"""X filtered stream background service (conservative BURST-first).

Design goals:
- Do not run unless explicitly enabled via settings/env.
- Strict guardrails to avoid blowing through credits.
- Provide a tiny in-memory cache for the explorer ("x_stream_cache" tool).
- Publish SSE events for visibility.

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
from typing import Any

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
    ) -> None:
        """Start a burst in a background thread.

        This method returns immediately.
        """

        self._can_start_burst()

        if self._current_burst_thread and self._current_burst_thread.is_alive():
            raise RuntimeError("A burst is already running")

        ttl_s = float(max(1, self.guards.burst_ttl_minutes) * 60)
        self._bursts_today += 1

        t = threading.Thread(
            target=self._run_burst,
            kwargs={
                "rules": rules,
                "remove_rules_after": remove_rules_after,
                "ttl_s": ttl_s,
                "stream_params": stream_params,
            },
            daemon=True,
        )
        self._current_burst_thread = t
        t.start()

    def _run_burst(
        self,
        *,
        rules: list[StreamRule],
        remove_rules_after: bool,
        ttl_s: float,
        stream_params: StreamParams | None,
    ) -> None:
        # 1) Add rules
        self.bus.publish(PipelineEvent(type="x_burst_start", payload={"rules": [r.to_dict() for r in rules]}))

        add_resp = add_rules(client=self.client, rules=rules, dry_run=False)
        created: list[dict[str, Any]] = list(add_resp.get("data") or [])
        created_ids = [str(x.get("id")) for x in created if x.get("id")]

        # 2) Connect stream for TTL
        # Log stream payloads to JSONL (optional but useful)
        log_dir = self.data_dir / "x" / "stream"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

        posts_seen = 0
        start = time.time()
        try:
            for obj in stream_posts(client=self.client, params=stream_params, stop_after_s=ttl_s):
                posts_seen += 1

                # Guardrail: if posts explode, stop early
                if posts_seen > self.guards.max_posts_per_day:
                    raise RuntimeError(
                        f"X posts guard tripped within burst: posts_seen={posts_seen} max_posts_per_day={self.guards.max_posts_per_day}"
                    )

                # Persist JSONL
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")

                # Cache keying: rule tags are not guaranteed to show in stream payload.
                # We cache by a simple derived key if possible (author/user/symbol parsing later).
                # For now, store in a generic "_all" bucket.
                with self._lock:
                    self._cache["_all"].append(obj)

                self.bus.publish(PipelineEvent(type="x_post", payload={"post": obj}))

                # TTL check (stop_after_s already does it, but keep belt+suspenders)
                if time.time() - start >= ttl_s:
                    break

        finally:
            # 3) Optionally delete the rules we added
            if remove_rules_after and created_ids:
                try:
                    delete_rules(client=self.client, ids=created_ids, dry_run=False)
                except Exception as e:
                    if DEBUG:
                        raise
                    self.bus.publish(PipelineEvent(type="x_burst_rule_delete_error", payload={"error": str(e)}))

            # publish a final usage poll snapshot if we have it
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
                        "posts_seen": posts_seen,
                        "duration_s": round(time.time() - start, 3),
                        "created_rule_ids": created_ids,
                    },
                )
            )

    def stop(self) -> None:
        self._stop_evt.set()


def build_rules_for_symbols(*, symbols: list[str]) -> list[StreamRule]:
    """Very conservative starter rules.

    We keep rules tight:
    - exclude retweets
    - English only
    """
    out: list[StreamRule] = []
    for s in symbols[:3]:  # cap tightness in the rule set
        sym = s.strip().upper()
        if not sym:
            continue
        # Verified with dry_run: bare $TSLA cashtag token is accepted by your plan.
        value = f"({sym} OR ${sym}) -is:retweet lang:en"
        out.append(StreamRule(value=value, tag=sym))
    return out
