"""Watch monitoring scheduler.

Periodically checks on holding watches:
- Lightweight check-ins: price-only, no LLM
- Agent check-ins: single LLM with market tools, produces hold/exit decision

Runs as a daemon thread alongside the news processing worker.
"""

from __future__ import annotations

import asyncio
import os
import time
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


# ---------------------------------------------------------------------------
# WatchMonitor
# ---------------------------------------------------------------------------


class WatchMonitor:
    """Monitors holding watches with periodic check-ins."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        bus: EventBus,
        market: MarketDataService | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.bus = bus
        self.market = market

    def run_check_cycle(self) -> None:
        """Run one check cycle across all holding watches."""
        watches = get_active_watches(self.db)
        holding = [w for w in watches if w.get("status") == "holding"]

        for watch_dict in holding:
            try:
                if self._is_due(watch_dict):
                    self._run_checkin(watch_dict)
            except Exception as e:
                if DEBUG:
                    raise
                wid = watch_dict.get("watch_id", "?")
                print(f"WARN: Check-in failed for {wid}: {e}")

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

        decision = asyncio.run(self._run_agent(watch_dict, depth, minutes_held))

        builder = WatchBuilder.from_dict(watch_dict)
        builder.last_checkin_at = _utc_now()

        if decision.action == "exit":
            current_price = self._get_current_price(symbol)
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
    ) -> CheckinDecision:
        """Create and run a single monitoring agent."""
        entry = watch_dict["entry"]
        symbol = watch_dict["symbol"]

        model_str = self.settings.watch_checkin_model
        # Prefix for PydanticAI if not already prefixed
        if not any(model_str.startswith(p) for p in ("google-gla:", "openai:", "anthropic:", "openai-responses:")):
            model_str = f"google-gla:{model_str}"

        prompt = MONITORING_PROMPT.format(
            symbol=symbol,
            direction=entry["direction"],
            entry_price=entry["price"],
            entry_time=entry["time"],
            thesis=entry["thesis"],
            horizon=entry["horizon"],
            minutes_held=minutes_held,
            force_exit_instruction=FORCE_EXIT_NOTE if depth == "force_exit" else "",
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
            system_prompt=prompt,
            toolsets=[tracing],
        )

        result = await agent.run(
            f"Check on {symbol} position (entry ${entry['price']:.2f}, "
            f"held {minutes_held:.0f} min, thesis: {entry['thesis']})",
            deps=deps,
            usage_limits=UsageLimits(
                request_limit=5,
                tool_calls_limit=8,
                total_tokens_limit=15_000,
            ),
        )
        return result.output

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
