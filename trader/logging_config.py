"""Centralized logging setup for the trader app.

Configures both console (stderr) and rotating file output.

Env vars:
    LOG_FILE       — Path to log file (default: logs/trader.log)
    LOG_LEVEL      — Minimum level for file logging (default: WARNING)
    LOG_KEEP_DAYS  — How many days of log files to keep (default: 14)
    LIVE_EVAL_VERBOSE — Set to 1/true to show LIVE-EVAL/LIVE-PM debug
                        messages on console (default: off)

The file handler captures WARNING+ by default so you always have a
record of errors. Console output remains INFO as before.

Log files rotate daily at midnight. Old files beyond LOG_KEEP_DAYS
are automatically deleted.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Defaults
_DEFAULT_LOG_FILE = "logs/trader.log"
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_KEEP_DAYS = 14


def setup_logging() -> None:
    """Configure root logger with console + rotating file handlers."""
    log_file = os.getenv("LOG_FILE", _DEFAULT_LOG_FILE)
    log_level = os.getenv("LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()
    keep_days = int(os.getenv("LOG_KEEP_DAYS", str(_DEFAULT_KEEP_DAYS)))

    # Ensure log directory exists
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # let handlers decide their own levels

    # Console handler (INFO+) — preserves existing behavior
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    # File handler — rotates daily at midnight, keeps LOG_KEEP_DAYS days
    file_handler = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=keep_days,
    )
    file_handler.setLevel(getattr(logging, log_level, logging.WARNING))
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_handler)

    # Quiet noisy third-party libs
    for name in (
        "httpx", "httpcore", "watchfiles",
        "Schwabdev", "schwabdev",
        "google_genai", "google_genai.models",
        "alpaca", "alpaca.trading.stream",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    # yfinance logs ERROR for every non-stock symbol (futures, forex, indices) — pure noise
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    # Suppress uvicorn access logs (GET /api/... 200 OK) — keep error logs
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)

    # VolumeDeltaCollector logs ~150 "tracking X" messages on startup
    logging.getLogger("trader.market.volume_delta_shadow").setLevel(logging.WARNING)

    # LIVE-EVAL / LIVE-PM verbose console output (off by default)
    live_eval_verbose = os.getenv("LIVE_EVAL_VERBOSE", "").lower() in ("1", "true", "yes")
    if not live_eval_verbose:
        logging.getLogger("trader.online.live_monitor").setLevel(logging.INFO)

    logging.getLogger(__name__).info(
        "Logging to file: %s (level=%s, keep=%d days)", log_file, log_level, keep_days,
    )
