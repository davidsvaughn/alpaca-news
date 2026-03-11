"""Watch lifecycle model.

A Watch tracks a virtual position from entry to exit, with optional
post-exit cooling-off period for continued data collection.

Lifecycle: holding → exited → cooling_off → sealed

The legacy lifecycle included LLM-based check-ins and a "retrospective"
phase. That system is disabled (see watcher.py). The live trading system
uses mechanical exit strategies (same math as backtest) instead.

Uses the same builder pattern as Snapshot: WatchBuilder accumulates
mutable state, then produces a frozen Watch.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

# "retrospective" kept for backward compatibility with existing DB records.
WatchStatus = Literal["holding", "exited", "cooling_off", "retrospective", "sealed"]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class WatchEntry:
    snapshot_id: str
    price: float
    time: str              # ISO timestamp
    confidence: float
    direction: str         # "bullish" | "bearish"
    horizon: str           # "15m" | "60m" | "1d"
    thesis: str            # key_catalyst from TradingSignal


@dataclass(frozen=True)
class WatchExit:
    snapshot_id: str | None
    price: float
    time: str
    reason: str
    realized_pnl_pct: float


@dataclass(frozen=True)
class Watch:
    """Immutable watch. Created only via WatchBuilder."""

    watch_id: str
    symbol: str
    status: WatchStatus
    entry: WatchEntry
    exit: WatchExit | None
    created_at: str
    last_checkin_at: str | None
    lifecycle_sealed_at: str | None

    # Live trading fields (Phase 1+)
    live_config_id: str | None = None
    exit_strategy: str | None = None
    exit_params: dict[str, Any] | None = None
    cooling_off_until: str | None = None

    # Position sizing
    qty: float | None = None  # shares held (from Alpaca fill or calculated)

    # P&L extremes during holding period (updated each monitor cycle)
    peak_pnl_pct: float | None = None   # highest unrealized P&L % while held
    trough_pnl_pct: float | None = None  # lowest unrealized P&L % while held

    # Alpaca order tracking (Phase 4+)
    alpaca_buy_order_id: str | None = None
    alpaca_stop_order_id: str | None = None
    alpaca_stop_price: float | None = None  # persisted stop price (set at buy time)

    # Legacy fields — kept for backward compatibility with existing DB records.
    # New watches created by the live system will have empty lists/None here.
    monitoring_snapshot_ids: list[str] = field(default_factory=list)
    retrospective_snapshot_ids: list[str] = field(default_factory=list)
    checkin_history: list[dict[str, Any]] = field(default_factory=list)
    retrospective_data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def persist(self, path: str | Path) -> None:
        from trader.models import atomic_write_text

        atomic_write_text(Path(path), self.to_json(indent=2) + "\n")


class WatchBuilder:
    """Mutable accumulator that produces frozen Watch instances.

    Usage::

        builder = WatchBuilder.create_from_signal(
            snapshot_id="snap_142",
            symbol="NVDA",
            entry_price=612.30,
            signal=trading_signal,
        )
        # ... later, during monitoring ...
        builder.add_monitoring_snapshot("snap_143")
        # ... exit ...
        builder.record_exit(price=618.50, reason="Momentum exhausting", snapshot_id="snap_148")
        watch = builder.to_watch()
    """

    def __init__(
        self,
        *,
        watch_id: str | None = None,
        symbol: str,
        entry: WatchEntry,
    ) -> None:
        self.watch_id = watch_id or f"watch_{uuid.uuid4().hex[:12]}"
        self.symbol = symbol
        self.status: WatchStatus = "holding"
        self.entry = entry
        self.exit: WatchExit | None = None
        self.created_at = _utc_now()
        self.last_checkin_at: str | None = None
        self.lifecycle_sealed_at: str | None = None
        # Live trading fields
        self.live_config_id: str | None = None
        self.exit_strategy: str | None = None
        self.exit_params: dict[str, Any] | None = None
        self.cooling_off_until: str | None = None
        # Position sizing
        self.qty: float | None = None
        # P&L extremes
        self.peak_pnl_pct: float | None = None
        self.trough_pnl_pct: float | None = None
        # Alpaca order tracking
        self.alpaca_buy_order_id: str | None = None
        self.alpaca_stop_order_id: str | None = None
        self.alpaca_stop_price: float | None = None
        # Legacy fields (kept for backward compatibility)
        self.monitoring_snapshot_ids: list[str] = []
        self.retrospective_snapshot_ids: list[str] = []
        self.checkin_history: list[dict[str, Any]] = []
        self.retrospective_data: dict[str, Any] | None = None

    @classmethod
    def create_from_signal(
        cls,
        *,
        snapshot_id: str,
        symbol: str,
        entry_price: float,
        signal: Any,  # TradingSignal (avoid circular import)
    ) -> WatchBuilder:
        """Create a WatchBuilder from a TradingSignal and entry price."""
        entry = WatchEntry(
            snapshot_id=snapshot_id,
            price=entry_price,
            time=_utc_now(),
            confidence=signal.confidence,
            direction=signal.direction,
            horizon=signal.horizon,
            thesis=signal.key_catalyst,
        )
        return cls(symbol=symbol, entry=entry)

    @classmethod
    def create_from_live_config(
        cls,
        *,
        snapshot_id: str,
        symbol: str,
        entry_price: float,
        confidence: float,
        direction: str,
        live_config_id: str,
        exit_strategy: str,
        exit_params: dict[str, Any],
    ) -> WatchBuilder:
        """Create a WatchBuilder for the live trading system.

        Unlike create_from_signal, this takes explicit values rather than
        a TradingSignal object, and records the live config + exit strategy.
        """
        entry = WatchEntry(
            snapshot_id=snapshot_id,
            price=entry_price,
            time=_utc_now(),
            confidence=confidence,
            direction=direction,
            horizon="1d",  # live positions use daily horizon
            thesis="live_trading",
        )
        builder = cls(symbol=symbol, entry=entry)
        builder.live_config_id = live_config_id
        builder.exit_strategy = exit_strategy
        builder.exit_params = exit_params
        return builder

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WatchBuilder:
        """Reconstitute a WatchBuilder from a stored watch dict."""
        entry = WatchEntry(**d["entry"])
        builder = cls(watch_id=d["watch_id"], symbol=d["symbol"], entry=entry)
        builder.status = d["status"]
        builder.created_at = d["created_at"]
        builder.last_checkin_at = d.get("last_checkin_at")
        builder.lifecycle_sealed_at = d.get("lifecycle_sealed_at")
        # Live trading fields
        builder.live_config_id = d.get("live_config_id")
        builder.exit_strategy = d.get("exit_strategy")
        builder.exit_params = d.get("exit_params")
        builder.cooling_off_until = d.get("cooling_off_until")
        # Position sizing
        builder.qty = d.get("qty")
        # P&L extremes
        builder.peak_pnl_pct = d.get("peak_pnl_pct")
        builder.trough_pnl_pct = d.get("trough_pnl_pct")
        # Alpaca order tracking
        builder.alpaca_buy_order_id = d.get("alpaca_buy_order_id")
        builder.alpaca_stop_order_id = d.get("alpaca_stop_order_id")
        builder.alpaca_stop_price = d.get("alpaca_stop_price")
        # Legacy fields
        builder.monitoring_snapshot_ids = list(d.get("monitoring_snapshot_ids", []))
        builder.retrospective_snapshot_ids = list(d.get("retrospective_snapshot_ids", []))
        builder.checkin_history = list(d.get("checkin_history", []))
        builder.retrospective_data = d.get("retrospective_data")
        if d.get("exit"):
            builder.exit = WatchExit(**d["exit"])
        return builder

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    def add_monitoring_snapshot(self, snapshot_id: str) -> None:
        self.monitoring_snapshot_ids.append(snapshot_id)

    def record_exit(
        self,
        *,
        price: float,
        reason: str,
        snapshot_id: str | None = None,
    ) -> None:
        pnl_pct = ((price - self.entry.price) / self.entry.price) * 100.0
        if self.entry.direction == "bearish":
            pnl_pct = -pnl_pct  # invert for short thesis
        self.exit = WatchExit(
            snapshot_id=snapshot_id,
            price=price,
            time=_utc_now(),
            reason=reason,
            realized_pnl_pct=round(pnl_pct, 4),
        )
        self.status = "exited"

    def start_cooling_off(self, cooling_off_until: str) -> None:
        """Transition from exited to cooling_off phase.

        Args:
            cooling_off_until: ISO timestamp when cooling_off expires.
        """
        self.status = "cooling_off"
        self.cooling_off_until = cooling_off_until

    # Legacy retrospective methods — kept for backward compat with old watches.
    def start_retrospective(self, exit_price: float) -> None:
        """Transition from exited to retrospective phase (legacy)."""
        self.status = "retrospective"
        self.retrospective_data = {
            "exit_price": exit_price,
            "started_at": _utc_now(),
            "price_checks": [],
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
        }

    def add_retrospective_snapshot(self, snapshot_id: str) -> None:
        self.retrospective_snapshot_ids.append(snapshot_id)

    def seal(self) -> None:
        self.lifecycle_sealed_at = _utc_now()
        self.status = "sealed"

    # ------------------------------------------------------------------
    # Produce frozen Watch
    # ------------------------------------------------------------------

    def to_watch(self) -> Watch:
        return Watch(
            watch_id=self.watch_id,
            symbol=self.symbol,
            status=self.status,
            entry=self.entry,
            exit=self.exit,
            created_at=self.created_at,
            last_checkin_at=self.last_checkin_at,
            lifecycle_sealed_at=self.lifecycle_sealed_at,
            # Live trading fields
            live_config_id=self.live_config_id,
            exit_strategy=self.exit_strategy,
            exit_params=self.exit_params,
            cooling_off_until=self.cooling_off_until,
            # Position sizing
            qty=self.qty,
            # P&L extremes
            peak_pnl_pct=self.peak_pnl_pct,
            trough_pnl_pct=self.trough_pnl_pct,
            # Alpaca order tracking
            alpaca_buy_order_id=self.alpaca_buy_order_id,
            alpaca_stop_order_id=self.alpaca_stop_order_id,
            alpaca_stop_price=self.alpaca_stop_price,
            # Legacy fields
            monitoring_snapshot_ids=list(self.monitoring_snapshot_ids),
            retrospective_snapshot_ids=list(self.retrospective_snapshot_ids),
            checkin_history=list(self.checkin_history),
            retrospective_data=self.retrospective_data,
        )
