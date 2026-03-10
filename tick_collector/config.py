"""Configuration for the tick collector."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Default symbols file location (relative to project root)
DEFAULT_SYMBOLS_FILE = Path(__file__).parent / "symbols.txt"


@dataclass
class CollectorConfig:
    # Database
    dsn: str = "postgresql://tickdata:tickdata_dev@localhost:5433/tickdata"

    # Schwab credentials
    schwab_app_key: str = ""
    schwab_app_secret: str = ""

    # Symbols
    symbols: list[str] = field(default_factory=list)

    # Buffer / flush
    flush_interval_sec: float = 2.0
    flush_batch_size: int = 5000  # max trades per INSERT

    # Heartbeat (during market hours, expect messages this often)
    heartbeat_timeout_sec: float = 15.0

    # Reconnection backoff
    reconnect_delays: list[float] = field(
        default_factory=lambda: [1, 2, 5, 10, 30]
    )

    @classmethod
    def from_env(cls) -> CollectorConfig:
        """Load config from environment variables + symbols file."""
        cfg = cls(
            dsn=os.getenv(
                "TIMESCALE_DSN",
                "postgresql://tickdata:tickdata_dev@localhost:5433/tickdata",
            ),
            schwab_app_key=os.getenv("SCHWAB_APP_KEY", ""),
            schwab_app_secret=os.getenv("SCHWAB_APP_SECRET", ""),
            flush_interval_sec=float(os.getenv("TICK_FLUSH_INTERVAL", "2.0")),
        )

        # Load symbols from env var or file
        env_symbols = os.getenv("TICK_SYMBOLS", "")
        if env_symbols:
            cfg.symbols = [s.strip().upper() for s in env_symbols.split(",") if s.strip()]
        else:
            cfg.symbols = load_symbols_file(DEFAULT_SYMBOLS_FILE)

        return cfg


def load_symbols_file(path: Path) -> list[str]:
    """Load symbols from a text file (one per line, # comments, blank lines ok)."""
    if not path.exists():
        return []
    symbols = []
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip().upper()
        if line:
            symbols.append(line)
    return symbols
