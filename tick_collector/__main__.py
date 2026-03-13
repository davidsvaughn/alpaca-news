"""Entry point: python -m tick_collector"""

import asyncio
import fcntl
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from .collector import TickCollector
from .config import CollectorConfig
from .portfolio import PortfolioSync

load_dotenv()

# Singleton guard: only one tick collector instance allowed
_LOCK_FILE = Path(__file__).parent.parent / "logs" / ".tick_collector.lock"

# Logging defaults (overridable via env vars)
_DEFAULT_LOG_FILE = "logs/tick_collector.log"
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_KEEP_DAYS = 14


def _acquire_lock() -> None:
    """Ensure only one tick collector instance runs at a time."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file object alive for the process lifetime
    global _lock_fd
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
    except OSError:
        log.error("Another tick collector instance is already running. Exiting.")
        sys.exit(1)


def _setup_logging() -> None:
    """Configure root logger with console + rotating file handlers."""
    log_file = os.getenv("TC_LOG_FILE", _DEFAULT_LOG_FILE)
    log_level = os.getenv("TC_LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()
    keep_days = int(os.getenv("TC_LOG_KEEP_DAYS", str(_DEFAULT_KEEP_DAYS)))

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler (INFO+)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    # File handler — rotates daily at midnight
    file_handler = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=keep_days,
    )
    file_handler.setLevel(getattr(logging, log_level, logging.WARNING))
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_handler)

    # Quiet down noisy libs
    logging.getLogger("schwabdev").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging to file: %s (level=%s, keep=%d days)", log_file, log_level, keep_days,
    )


log = logging.getLogger(__name__)


def main() -> None:
    _setup_logging()
    _acquire_lock()
    config = CollectorConfig.from_env()

    # Preview portfolio symbols
    ps = PortfolioSync(config.trader_db_path, config.portfolio_cooloff_min)
    ps.sync()
    active = sorted(ps.active_symbols)

    log.info("Tick Collector")
    log.info("  Portfolio symbols: %d", len(active))
    log.info("  Trader DB:         %s", config.trader_db_path)
    log.info("  DSN:               %s", config.dsn)
    log.info("  Flush:             every %ds", config.flush_interval_sec)
    log.info("  Portfolio sync:    every %ds", config.portfolio_sync_interval_sec)
    log.info("  Cool-off:          %d min", config.portfolio_cooloff_min)
    if active:
        preview = ', '.join(active[:10])
        log.info("  Symbols:           %s%s", preview, '...' if len(active) > 10 else '')

    if not active:
        log.error("No portfolio holdings found in trader.db")
        sys.exit(1)

    collector = TickCollector(config)
    asyncio.run(collector.run())


main()
