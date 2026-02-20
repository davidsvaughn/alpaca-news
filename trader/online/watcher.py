"""Watch monitoring scheduler.

Periodically checks on all active watches:
- Holding: lightweight or agent check-ins (price, LLM hold/exit decision)
- Exited: immediate transition to retrospective phase
- Retrospective: lightweight price tracking (MFE/MAE), auto-seal after max duration

Runs as a daemon thread alongside the news processing worker.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic_ai import Agent, UsageLimits

from trader.config import Settings
from trader.db.database import Database, get_active_watches, update_watch
from trader.market.data_service import MarketDataService
from trader.models.watch import WatchBuilder, _utc_now
from trader.online.event_bus import EventBus, PipelineEvent
from trader.online.explorer_agent import (
    CheckinDecision,
    ExplorerDeps,
    TracingToolset,
    market_toolset,
)

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
# Check-in schedule: (max_minutes, interval_minutes, depth)
# ---------------------------------------------------------------------------

CHECKIN_SCHEDULE: list[tuple[int, int, str]] = [
    (10, 2, "lightweight"),     # 0-10 min: every 2 min, price only
    (30, 5, "medium"),          # 10-30 min: every 5 min, may use LLM
    (60, 10, "medium"),         # 30-60 min: every 10 min
    (240, 15, "full"),          # 60-240 min: every 15 min, full tools
]
# After 240 min → force_exit

# Retrospective schedule: (max_minutes_since_exit, interval_minutes)
RETRO_SCHEDULE: list[tuple[int, int]] = [
    (15, 5),    # 0-15 min after exit: every 5 min
    (60, 15),   # 15-60 min after exit: every 15 min
]
# After max_retro_minutes → auto-seal


def _minutes_since(iso_time: str) -> float:
    """Minutes elapsed since an ISO timestamp."""
    t = datetime.fromisoformat(iso_time)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - t).total_seconds() / 60.0


def _get_schedule(minutes_since_entry: float) -> tuple[int, str]:
    """Return (interval_minutes, depth) for the given elapsed time."""
    for max_min, interval, depth in CHECKIN_SCHEDULE:
        if minutes_since_entry < max_min:
            return interval, depth
    return 0, "force_exit"


def _get_retro_interval(minutes_since_exit: float) -> int:
    """Return interval_minutes for retrospective phase, or 0 if past schedule."""
    for max_min, interval in RETRO_SCHEDULE:
        if minutes_since_exit < max_min:
            return interval
    return 0  # past schedule → seal


def compute_pnl(entry_price: float, current_price: float, direction: str) -> float:
    """Compute unrealized P&L percentage."""
    pnl = ((current_price - entry_price) / entry_price) * 100.0
    if direction == "bearish":
        pnl = -pnl
    return round(pnl, 4)


# ---------------------------------------------------------------------------
# Monitoring system prompt
# ---------------------------------------------------------------------------

MONITORING_PROMPT = """You are monitoring an active trading position.

## Position Details
- Symbol: {symbol}
- Direction: {direction}
- Entry price: ${entry_price:.2f}
- Entry time: {entry_time}
- Entry thesis: {thesis}
- Horizon: {horizon}
- Minutes held: {minutes_held:.0f}

## Your Task
Check the current market state for {symbol} using the available tools, then decide whether to HOLD or EXIT this position.

Consider:
- Current price vs. entry price (unrealized P&L)
- Whether the original thesis still holds
- Any new developments that change the outlook
- Risk factors and momentum

{force_exit_instruction}

Provide your decision as a CheckinDecision with action (hold/exit), reason, and current unrealized P&L percentage.
"""

FORCE_EXIT_NOTE = (
    "IMPORTANT: This position has exceeded the maximum hold duration. "
    "You MUST decide to EXIT. Provide your exit reasoning."
)


@dataclass
class AgentCheckinResult:
    """Rich result from an LLM check-in agent, including traces."""
    decision: CheckinDecision
    system_prompt: str
    user_message: str
    tool_traces: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    findings: str = ""
    thinking_summary: str | None = None
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# WatchMonitor
# ---------------------------------------------------------------------------


class WatchMonitor:
    """Monitors active watches across all lifecycle phases."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        bus: EventBus,
        market: MarketDataService | None = None,
        observer: "ObserverMode | None" = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.bus = bus
        self.market = market
        self.observer = observer

    def run_check_cycle(self) -> None:
        """Run one check cycle across all active (non-sealed) watches."""
        watches = get_active_watches(self.db)

        for watch_dict in watches:
            status = watch_dict.get("status")
            try:
                if status == "holding":
                    if self._is_due(watch_dict):
                        self._run_checkin(watch_dict)
                elif status == "exited":
                    self._transition_to_retrospective(watch_dict)
                elif status == "retrospective":
                    self._retrospective_cycle(watch_dict)
            except Exception as e:
                if DEBUG:
                    raise
                wid = watch_dict.get("watch_id", "?")
                print(f"WARN: Check-in failed for {wid}: {e}")
                # Advance last_checkin_at to prevent retry storms
                self._bump_last_checkin(watch_dict)

    def _is_due(self, watch_dict: dict[str, Any]) -> bool:
        """Check if enough time has passed since the last check-in."""
        entry_time = watch_dict.get("entry", {}).get("time")
        if not entry_time:
            return False

        minutes_held = _minutes_since(entry_time)
        interval, _depth = _get_schedule(minutes_held)

        # force_exit is always due
        if _depth == "force_exit":
            return True

        last = watch_dict.get("last_checkin_at")
        if last is None:
            return True  # never checked in yet
        minutes_since_last = _minutes_since(last)
        return minutes_since_last >= interval

    def _run_checkin(self, watch_dict: dict[str, Any]) -> None:
        """Dispatch to the appropriate check-in depth."""
        entry = watch_dict.get("entry", {})
        minutes_held = _minutes_since(entry.get("time", _utc_now()))
        _interval, depth = _get_schedule(minutes_held)

        if depth == "lightweight":
            self._lightweight_checkin(watch_dict, minutes_held)
        elif self.observer is not None and self.observer.enabled:
            # Observer mode: downgrade LLM check-ins to lightweight (no API costs)
            self._lightweight_checkin(watch_dict, minutes_held)
        else:
            self._agent_checkin(watch_dict, depth, minutes_held)

    # ------------------------------------------------------------------
    # Lightweight check-in (no LLM)
    # ------------------------------------------------------------------

    def _lightweight_checkin(
        self, watch_dict: dict[str, Any], minutes_held: float
    ) -> None:
        """Price-only check. No LLM call."""
        entry = watch_dict["entry"]
        symbol = watch_dict["symbol"]
        wid = watch_dict["watch_id"]

        current_price = self._get_current_price(symbol)
        if current_price is None:
            return  # can't check without a price

        pnl = compute_pnl(entry["price"], current_price, entry["direction"])

        # Update last_checkin_at
        builder = WatchBuilder.from_dict(watch_dict)
        builder.last_checkin_at = _utc_now()

        # Auto-exit on extreme loss (-10%) as a safety net
        if pnl < -10.0:
            builder.checkin_history.append({
                "time": _utc_now(), "depth": "lightweight",
                "action": "auto_stop_loss", "reason": f"Auto stop-loss: {pnl:.1f}%",
                "price": current_price, "pnl_pct": pnl, "model": None,
            })
            builder.record_exit(
                price=current_price,
                reason=f"Auto stop-loss: {pnl:.1f}% unrealized loss",
            )
            updated = builder.to_watch()
            update_watch(self.db, wid, updated.to_dict())
            self.bus.publish(PipelineEvent(
                type="watch_exited",
                payload={
                    "watch_id": wid, "symbol": symbol,
                    "reason": "auto_stop_loss", "pnl_pct": pnl,
                    "exit_price": current_price,
                },
            ))
            return

        builder.checkin_history.append({
            "time": _utc_now(), "depth": "lightweight",
            "action": "hold", "reason": None,
            "price": current_price, "pnl_pct": pnl, "model": None,
        })
        updated = builder.to_watch()
        update_watch(self.db, wid, updated.to_dict())
        self.bus.publish(PipelineEvent(
            type="watch_checkin",
            payload={
                "watch_id": wid, "symbol": symbol,
                "depth": "lightweight", "pnl_pct": pnl,
                "current_price": current_price,
                "minutes_held": round(minutes_held, 1),
            },
        ))

    # ------------------------------------------------------------------
    # Agent check-in (LLM)
    # ------------------------------------------------------------------

    def _agent_checkin(
        self, watch_dict: dict[str, Any], depth: str, minutes_held: float
    ) -> None:
        """Run a single-agent check-in with function tools."""
        entry = watch_dict["entry"]
        symbol = watch_dict["symbol"]
        wid = watch_dict["watch_id"]

        agent_result = _get_or_create_loop().run_until_complete(
            self._run_agent(watch_dict, depth, minutes_held)
        )
        decision = agent_result.decision

        # Hard override: force_exit must always exit, even if agent says hold
        if depth == "force_exit" and decision.action != "exit":
            decision = CheckinDecision(
                action="exit",
                reason=f"Force exit override (agent wanted hold: {decision.reason})",
                unrealized_pnl_pct=decision.unrealized_pnl_pct,
            )

        builder = WatchBuilder.from_dict(watch_dict)
        builder.last_checkin_at = _utc_now()
        current_price = self._get_current_price(symbol)

        # Estimate LLM cost from token usage
        from trader.online.agent_pipeline import _estimate_agent_cost
        cost_usd = _estimate_agent_cost(
            "gemini", self.settings.watch_checkin_model,
            input_tokens=agent_result.usage.get("input_tokens", 0),
            output_tokens=agent_result.usage.get("output_tokens", 0),
        )

        builder.checkin_history.append({
            "time": _utc_now(), "depth": depth,
            "action": decision.action,
            "reason": decision.reason,
            "price": current_price,
            "pnl_pct": decision.unrealized_pnl_pct,
            "model": self.settings.watch_checkin_model,
            "cost_usd": cost_usd,
            # LLM trace data
            "system_prompt": agent_result.system_prompt,
            "user_message": agent_result.user_message,
            "findings": agent_result.findings,
            "tool_traces": agent_result.tool_traces,
            "usage": agent_result.usage,
            "thinking_summary": agent_result.thinking_summary,
            "elapsed_s": agent_result.elapsed_s,
        })

        if decision.action == "exit":
            exit_price = current_price if current_price is not None else entry["price"]
            builder.record_exit(price=exit_price, reason=decision.reason)
            updated = builder.to_watch()
            update_watch(self.db, wid, updated.to_dict())
            self.bus.publish(PipelineEvent(
                type="watch_exited",
                payload={
                    "watch_id": wid, "symbol": symbol,
                    "reason": decision.reason,
                    "pnl_pct": decision.unrealized_pnl_pct,
                    "exit_price": exit_price, "depth": depth,
                },
            ))
        else:
            updated = builder.to_watch()
            update_watch(self.db, wid, updated.to_dict())
            self.bus.publish(PipelineEvent(
                type="watch_checkin",
                payload={
                    "watch_id": wid, "symbol": symbol,
                    "depth": depth, "pnl_pct": decision.unrealized_pnl_pct,
                    "reason": decision.reason,
                    "minutes_held": round(minutes_held, 1),
                },
            ))

    async def _run_agent(
        self,
        watch_dict: dict[str, Any],
        depth: str,
        minutes_held: float,
    ) -> AgentCheckinResult:
        """Create and run a single monitoring agent, returning full traces."""
        from trader.online.runners.pydanticai_runner import _extract_thinking_content

        entry = watch_dict["entry"]
        symbol = watch_dict["symbol"]

        model_str = self.settings.watch_checkin_model
        # Prefix for PydanticAI if not already prefixed
        if not any(model_str.startswith(p) for p in ("google-gla:", "openai:", "anthropic:", "openai-responses:")):
            model_str = f"google-gla:{model_str}"

        system_prompt = MONITORING_PROMPT.format(
            symbol=symbol,
            direction=entry["direction"],
            entry_price=entry["price"],
            entry_time=entry["time"],
            thesis=entry["thesis"],
            horizon=entry["horizon"],
            minutes_held=minutes_held,
            force_exit_instruction=FORCE_EXIT_NOTE if depth == "force_exit" else "",
        )

        user_message = (
            f"Check on {symbol} position (entry ${entry['price']:.2f}, "
            f"held {minutes_held:.0f} min, thesis: {entry['thesis']})"
        )

        # Reuse ExplorerDeps + market_toolset (avoids duplicating tool defs)
        deps = ExplorerDeps(
            market=self.market or MarketDataService(),
            news={"watch_context": watch_dict},
            symbols=[symbol],
        )

        tracing = TracingToolset(market_toolset)
        agent: Agent[ExplorerDeps, CheckinDecision] = Agent(
            model_str,
            deps_type=ExplorerDeps,
            output_type=CheckinDecision,
            system_prompt=system_prompt,
            toolsets=[tracing],
        )

        t0 = time.time()
        result = await agent.run(
            user_message,
            deps=deps,
            usage_limits=UsageLimits(
                request_limit=5,
                tool_calls_limit=8,
                total_tokens_limit=15_000,
            ),
        )
        elapsed = time.time() - t0

        usage = result.usage()
        thinking = _extract_thinking_content(result.all_messages())

        return AgentCheckinResult(
            decision=result.output,
            system_prompt=system_prompt,
            user_message=user_message,
            tool_traces=deps.tool_traces,
            usage={
                "input_tokens": usage.input_tokens or 0,
                "output_tokens": usage.output_tokens or 0,
                "total_tokens": usage.total_tokens or 0,
                "requests": usage.requests or 0,
                "tool_calls": len(deps.tool_traces),
            },
            findings=str(result.output),
            thinking_summary=thinking,
            elapsed_s=round(elapsed, 2),
        )

    # ------------------------------------------------------------------
    # Retrospective phase (post-exit price tracking)
    # ------------------------------------------------------------------

    def _transition_to_retrospective(self, watch_dict: dict[str, Any]) -> None:
        """Move an exited watch into retrospective phase."""
        wid = watch_dict["watch_id"]
        symbol = watch_dict["symbol"]
        exit_data = watch_dict.get("exit", {})
        exit_price = exit_data.get("price", 0.0)

        builder = WatchBuilder.from_dict(watch_dict)
        builder.start_retrospective(exit_price)
        updated = builder.to_watch()
        update_watch(self.db, wid, updated.to_dict())

        self.bus.publish(PipelineEvent(
            type="watch_retrospective_started",
            payload={"watch_id": wid, "symbol": symbol, "exit_price": exit_price},
        ))

    def _retrospective_cycle(self, watch_dict: dict[str, Any]) -> None:
        """Check if retrospective is due for a price check or should be sealed."""
        retro = watch_dict.get("retrospective_data") or {}
        started_at = retro.get("started_at")
        if not started_at:
            # Malformed — seal immediately
            self._seal_watch(watch_dict)
            return

        minutes_since_exit = _minutes_since(started_at)
        max_retro = self.settings.watch_max_retro_minutes

        if minutes_since_exit >= max_retro:
            self._seal_watch(watch_dict)
            return

        if self._is_retro_due(watch_dict, minutes_since_exit):
            self._retrospective_price_check(watch_dict)

    def _is_retro_due(
        self, watch_dict: dict[str, Any], minutes_since_exit: float
    ) -> bool:
        """Check if a retrospective price check is due."""
        interval = _get_retro_interval(minutes_since_exit)
        if interval == 0:
            return False  # past schedule, will be sealed
        last = watch_dict.get("last_checkin_at")
        if last is None:
            return True
        return _minutes_since(last) >= interval

    def _retrospective_price_check(self, watch_dict: dict[str, Any]) -> None:
        """Lightweight price check during retrospective. Updates MFE/MAE."""
        symbol = watch_dict["symbol"]
        wid = watch_dict["watch_id"]
        entry = watch_dict["entry"]

        current_price = self._get_current_price(symbol)
        if current_price is None:
            return

        builder = WatchBuilder.from_dict(watch_dict)
        builder.last_checkin_at = _utc_now()

        retro = builder.retrospective_data or {}
        exit_price = retro.get("exit_price", entry["price"])

        # P&L since exit (direction-aware)
        pnl_since_exit = compute_pnl(exit_price, current_price, entry["direction"])

        # Append price checkpoint
        checks = retro.get("price_checks", [])
        checks.append({
            "time": _utc_now(),
            "price": current_price,
            "pnl_since_exit_pct": pnl_since_exit,
        })
        retro["price_checks"] = checks

        # Update MFE/MAE
        retro["mfe_pct"] = max(retro.get("mfe_pct", 0.0), pnl_since_exit)
        retro["mae_pct"] = min(retro.get("mae_pct", 0.0), pnl_since_exit)
        builder.retrospective_data = retro

        updated = builder.to_watch()
        update_watch(self.db, wid, updated.to_dict())

        self.bus.publish(PipelineEvent(
            type="watch_retrospective_checkin",
            payload={
                "watch_id": wid, "symbol": symbol,
                "current_price": current_price,
                "pnl_since_exit_pct": pnl_since_exit,
                "mfe_pct": retro["mfe_pct"],
                "mae_pct": retro["mae_pct"],
            },
        ))

    def _seal_watch(self, watch_dict: dict[str, Any]) -> None:
        """Seal a watch — final lifecycle state, no more processing."""
        wid = watch_dict["watch_id"]
        symbol = watch_dict["symbol"]
        entry = watch_dict["entry"]

        builder = WatchBuilder.from_dict(watch_dict)

        # Record final price if available
        retro = builder.retrospective_data or {}
        current_price = self._get_current_price(symbol)
        if current_price is not None:
            retro["final_price"] = current_price
            pnl_since_exit = compute_pnl(
                retro.get("exit_price", entry["price"]),
                current_price,
                entry["direction"],
            )
            retro["mfe_pct"] = max(retro.get("mfe_pct", 0.0), pnl_since_exit)
            retro["mae_pct"] = min(retro.get("mae_pct", 0.0), pnl_since_exit)
        builder.retrospective_data = retro

        builder.seal()
        updated = builder.to_watch()
        update_watch(self.db, wid, updated.to_dict())

        self.bus.publish(PipelineEvent(
            type="watch_sealed",
            payload={
                "watch_id": wid, "symbol": symbol,
                "mfe_pct": retro.get("mfe_pct", 0.0),
                "mae_pct": retro.get("mae_pct", 0.0),
                "final_price": retro.get("final_price"),
            },
        ))

        # Create post-exit follow-up for continued data collection
        if self.settings.follow_up_enabled:
            try:
                from trader.db.database import insert_follow_up
                from trader.models.follow_up import FollowUpBuilder

                schedule = [
                    s.strip()
                    for s in self.settings.follow_up_schedule.split(",")
                ]
                fu_config: dict[str, Any] = {
                    "web_searches": self.settings.follow_up_web_searches,
                    "x_searches": self.settings.follow_up_x_searches,
                    "direction": entry.get("direction", ""),
                    "confidence": round(entry.get("confidence", 0) * 100),
                }
                fu_builder = FollowUpBuilder(
                    snapshot_id=entry.get("snapshot_id", ""),
                    symbols=[symbol],
                    reason="post_exit",
                    schedule=schedule,
                    config=fu_config,
                    watch_id=wid,
                    headline=entry.get("thesis", ""),
                )
                insert_follow_up(
                    self.db, follow_up=fu_builder.to_follow_up().to_dict()
                )
                self.bus.publish(PipelineEvent(
                    type="follow_up_created",
                    payload={
                        "follow_up_id": fu_builder.follow_up_id,
                        "snapshot_id": entry.get("snapshot_id", ""),
                        "symbols": [symbol],
                        "reason": "post_exit",
                        "watch_id": wid,
                    },
                ))
            except Exception as e:
                if DEBUG:
                    raise
                print(f"WARN: Post-exit follow-up creation failed: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _bump_last_checkin(self, watch_dict: dict[str, Any]) -> None:
        """Advance last_checkin_at on failure to prevent retry storms."""
        try:
            wid = watch_dict.get("watch_id", "")
            builder = WatchBuilder.from_dict(watch_dict)
            builder.last_checkin_at = _utc_now()
            updated = builder.to_watch()
            update_watch(self.db, wid, updated.to_dict())
        except Exception:
            pass  # best-effort — don't mask the original error

    def _get_current_price(self, symbol: str) -> float | None:
        """Get current price from market data service."""
        if self.market is None:
            return None
        try:
            quote = self.market.get_quote(symbol)
            price = quote.get("last_price")
            return float(price) if price is not None else None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Monitoring loop (run in daemon thread)
# ---------------------------------------------------------------------------


def monitoring_loop(monitor: WatchMonitor, interval_s: int = 60) -> None:
    """Periodic monitoring loop. Runs forever in a daemon thread."""
    while True:
        try:
            monitor.run_check_cycle()
        except Exception as e:
            if DEBUG:
                raise
            print(f"WARN: Monitoring cycle error: {e}")
        time.sleep(interval_s)
