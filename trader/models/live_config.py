"""LiveConfig model.

A LiveConfig captures the same parameters as a backtest configuration
and runs them forward in real-time: filters, allocation strategy, exit
strategy, guards, and timing.  Only one config can be active at a time.

Uses the same builder-free pattern as other models — dataclass with
to_dict/from_dict for JSON serialization.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class LiveConfig:
    """Persisted configuration that mirrors backtest parameters."""

    config_id: str
    name: str
    active: bool

    # Snapshot filters (same keys as backtest UI)
    filters: dict[str, Any]

    # Allocation strategy
    allocation: str                  # "none" | "fixed_dollar" | "max_positions" | "ranking_realloc"
    allocation_params: dict[str, Any]
    starting_capital: float

    # Exit strategy
    exit_strategy: str               # strategy key, e.g. "volume_delta_divergence"
    exit_params: dict[str, Any]      # e.g. {"lookback_m": 80, "bucket_s": 30}

    paused: bool = False             # paused = no new buys, but exits still run

    # Guards
    guard_stop_pct: float = 0.0      # hard stop loss % (0 = disabled)
    guard_target_pct: float = 0.0    # hard take profit % (0 = disabled)
    guard_trail_pct: float = 0.0     # trailing stop loss % from peak (0 = disabled)

    # Timing
    min_hold: int = 5                # minimum bars before exit checks
    price_delay_minutes: int = 10    # delay after snapshot before "entry"
    market_close: str | None = "16:00"  # "HH:MM" Eastern or None for extended

    # Post-exit
    cooling_off_market_hours: float = 24.0  # market hours to keep streaming after exit

    # Live-only overrides (ignored by backtest, read by live monitor)
    # e.g. {"vdd_tick": true, "bucket_s": 30, "min_trades_per_bucket": 3}
    live_overrides: dict[str, Any] = field(default_factory=dict)

    # Alpaca paper trading (optional — when set, orders are executed via Alpaca)
    alpaca_account_id: str | None = None  # e.g. "PA31QXNAPB1H"
    alpaca_account_name: str | None = None  # e.g. "AlpacaPaper1"

    # Metadata
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _utc_now()
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LiveConfig:
        """Reconstitute from a stored dict."""
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def create(
        cls,
        *,
        name: str,
        filters: dict[str, Any],
        allocation: str,
        allocation_params: dict[str, Any],
        starting_capital: float,
        exit_strategy: str,
        exit_params: dict[str, Any],
        **kwargs: Any,
    ) -> LiveConfig:
        """Create a new LiveConfig with a generated ID."""
        return cls(
            config_id=f"lc_{uuid.uuid4().hex[:12]}",
            name=name,
            active=False,
            filters=filters,
            allocation=allocation,
            allocation_params=allocation_params,
            starting_capital=starting_capital,
            exit_strategy=exit_strategy,
            exit_params=exit_params,
            **kwargs,
        )
