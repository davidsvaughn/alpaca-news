"""Schwab token status checker and reauth launcher.

Reads ~/.schwabdev/tokens.db directly (no schwabdev import) to check
token expiry. Can launch the reauth script in a separate terminal.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

TOKENS_DB = os.path.expanduser("~/.schwabdev/tokens.db")
REFRESH_TOKEN_TIMEOUT = 7 * 24 * 60 * 60  # 7 days in seconds
ACCESS_TOKEN_TIMEOUT = 30 * 60  # 30 minutes in seconds
REAUTH_SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "schwab_reauth.py"


@dataclass
class TokenStatus:
    """Schwab token status."""
    exists: bool = False
    refresh_expires: datetime | None = None
    access_expires: datetime | None = None
    refresh_expired: bool = True
    refresh_expiring_soon: bool = True  # < 1 day
    access_expired: bool = True

    @property
    def needs_reauth(self) -> bool:
        return self.refresh_expired

    @property
    def warn_expiring(self) -> bool:
        return self.refresh_expiring_soon and not self.refresh_expired

    @property
    def healthy(self) -> bool:
        return self.exists and not self.refresh_expired and not self.refresh_expiring_soon


def check_schwab_tokens() -> TokenStatus:
    """Check Schwab token expiry by reading the DB directly."""
    status = TokenStatus()

    if not os.path.exists(TOKENS_DB):
        return status

    try:
        conn = sqlite3.connect(TOKENS_DB, timeout=5)
        row = conn.execute(
            "SELECT access_token_issued, refresh_token_issued FROM schwabdev LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception:
        return status

    if not row:
        return status

    status.exists = True
    now = datetime.now(timezone.utc)

    at_issued = datetime.fromisoformat(row[0])
    if at_issued.tzinfo is None:
        at_issued = at_issued.replace(tzinfo=timezone.utc)

    rt_issued = datetime.fromisoformat(row[1])
    if rt_issued.tzinfo is None:
        rt_issued = rt_issued.replace(tzinfo=timezone.utc)

    status.access_expires = at_issued + timedelta(seconds=ACCESS_TOKEN_TIMEOUT)
    status.refresh_expires = rt_issued + timedelta(seconds=REFRESH_TOKEN_TIMEOUT)

    status.access_expired = now >= status.access_expires
    status.refresh_expired = now >= status.refresh_expires
    status.refresh_expiring_soon = (status.refresh_expires - now) < timedelta(days=1)

    return status


def _find_terminal() -> list[str] | None:
    """Find an available terminal emulator command."""
    candidates = [
        (["gnome-terminal", "--"], "gnome-terminal"),
        (["xfce4-terminal", "-e"], "xfce4-terminal"),
        (["konsole", "-e"], "konsole"),
        (["xterm", "-e"], "xterm"),
    ]
    for cmd, binary in candidates:
        if shutil.which(binary):
            return cmd
    return None


def launch_reauth_terminal() -> bool:
    """Launch the reauth script in a new terminal window.

    Returns True if launched successfully, False otherwise.
    """
    terminal_cmd = _find_terminal()
    if not terminal_cmd:
        log.warning("No terminal emulator found for Schwab reauth")
        return False

    script = str(REAUTH_SCRIPT)
    python = sys.executable

    try:
        subprocess.Popen(
            [*terminal_cmd, python, "-u", script],
            start_new_session=True,
        )
        return True
    except Exception as e:
        log.warning("Could not launch reauth terminal: %s", e)
        return False
