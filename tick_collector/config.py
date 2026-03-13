"""Configuration for the tick collector."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CollectorConfig:
    # Database
    dsn: str = "postgresql://tickdata:tickdata_dev@localhost:5433/tickdata"

    # Trader app SQLite DB (for portfolio sync)
    trader_db_path: str = ""

    # Schwab credentials
    schwab_app_key: str = ""
    schwab_app_secret: str = ""

    # Portfolio sync
    portfolio_sync_interval_sec: float = 60.0  # how often to poll trader.db
    portfolio_cooloff_min: float = 60.0  # minutes to keep streaming after last held

    # Buffer / flush
    flush_interval_sec: float = 2.0
    flush_batch_size: int = 5000  # max trades per INSERT

    # Heartbeat (during market hours, expect messages this often)
    heartbeat_timeout_sec: float = 15.0
    health_check_interval_sec: float = 2.0
    restart_delay_sec: float = 0.25
    restart_cooldown_sec: float = 5.0

    # Reconnection backoff
    reconnect_delays: list[float] = field(
        default_factory=lambda: [1, 2, 5, 10, 30]
    )

    @classmethod
    def from_env(cls) -> CollectorConfig:
        """Load config from environment variables."""
        project_root = Path(__file__).parent.parent
        default_trader_db = str(project_root / "data" / "trader.db")

        return cls(
            dsn=os.getenv(
                "TIMESCALE_DSN",
                "postgresql://tickdata:tickdata_dev@localhost:5433/tickdata",
            ),
            trader_db_path=os.getenv("TRADER_DB_PATH", default_trader_db),
            schwab_app_key=os.getenv("SCHWAB_APP_KEY", ""),
            schwab_app_secret=os.getenv("SCHWAB_APP_SECRET", ""),
            flush_interval_sec=float(os.getenv("TICK_FLUSH_INTERVAL", "2.0")),
            portfolio_sync_interval_sec=float(os.getenv("TICK_PORTFOLIO_SYNC_INTERVAL", "60.0")),
            portfolio_cooloff_min=float(os.getenv("TICK_PORTFOLIO_COOLOFF_MIN", "60.0")),
            heartbeat_timeout_sec=float(os.getenv("TICK_HEARTBEAT_TIMEOUT", "15.0")),
            health_check_interval_sec=float(os.getenv("TICK_HEALTH_CHECK_INTERVAL", "2.0")),
            restart_delay_sec=float(os.getenv("TICK_RESTART_DELAY", "0.25")),
            restart_cooldown_sec=float(os.getenv("TICK_RESTART_COOLDOWN", "5.0")),
        )
