"""Notifications: file-based log + optional email via SMTP.

File notifications (always active):
    Appends timestamped entries to logs/notifications.md.
    No configuration needed.

Email notifications (optional):
    NOTIFY_EMAIL_TO       — recipient address
    NOTIFY_EMAIL_FROM     — sender address (defaults to NOTIFY_EMAIL_TO)
    NOTIFY_SMTP_HOST      — SMTP server (default: smtp.gmail.com)
    NOTIFY_SMTP_PORT      — SMTP port (default: 587)
    NOTIFY_SMTP_USER      — SMTP login (defaults to NOTIFY_EMAIL_FROM)
    NOTIFY_SMTP_PASSWORD  — SMTP password / Gmail App Password

If NOTIFY_EMAIL_TO or NOTIFY_SMTP_PASSWORD is unset, send_email() silently
does nothing (no crash).
"""

from __future__ import annotations

import logging
import os
import smtplib
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger(__name__)

NOTIFY_LOG = Path(__file__).resolve().parent.parent / "logs" / "notifications.md"


def notify(subject: str, body: str) -> None:
    """Append a notification to logs/notifications.md (and optionally email)."""
    _append_to_log(subject, body)
    send_email(subject, body)


def _append_to_log(subject: str, body: str) -> None:
    try:
        NOTIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        entry = f"## [{ts}] {subject}\n\n{body}\n\n---\n\n"
        with open(NOTIFY_LOG, "a") as f:
            f.write(entry)
    except Exception:
        log.exception("Failed to write notification to %s", NOTIFY_LOG)


def send_email(subject: str, body: str) -> None:
    """Send an email notification in a background thread (non-blocking)."""
    to = os.environ.get("NOTIFY_EMAIL_TO", "").strip()
    password = os.environ.get("NOTIFY_SMTP_PASSWORD", "").strip()
    if not to or not password:
        return

    threading.Thread(
        target=_send,
        args=(subject, body, to, password),
        name="email-notify",
        daemon=True,
    ).start()


def _send(subject: str, body: str, to: str, password: str) -> None:
    try:
        from_addr = os.environ.get("NOTIFY_EMAIL_FROM", "").strip() or to
        host = os.environ.get("NOTIFY_SMTP_HOST", "smtp.gmail.com").strip()
        port = int(os.environ.get("NOTIFY_SMTP_PORT", "587"))
        user = os.environ.get("NOTIFY_SMTP_USER", "").strip() or from_addr

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to

        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(from_addr, [to], msg.as_string())

        log.info("Email sent: %s → %s", subject, to)
    except Exception:
        log.exception("Failed to send email: %s", subject)
