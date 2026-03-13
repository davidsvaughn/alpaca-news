"""Thread-safe manager for news feed websocket subprocesses + watchdog schedules.

Each feed (e.g. Insight Sentry, Alpaca) has a paired websocket subprocess that
writes JSON files and a watchdog directory watch that detects them.  FeedManager
bundles both resources so they can be started/stopped together at runtime via
dashboard toggle buttons.
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler


_LABELS = {
    "insight_sentry": "Sentry",
    "alpaca": "Alpaca",
}


class FeedManager:
    """Thread-safe manager for a single news feed (ws subprocess + watchdog schedule)."""

    def __init__(
        self,
        *,
        name: str,
        script: str,
        watch_dir: str,
        enabled: bool = False,
    ) -> None:
        self.name = name
        self.label = _LABELS.get(name, name)
        self._script = script
        self._watch_dir = watch_dir
        self._lock = threading.Lock()
        self._enabled = enabled
        self._proc: subprocess.Popen | None = None
        self._watch_handle: object | None = None  # ObservedWatch
        self._fs_observer: Observer | None = None
        self._handler: FileSystemEventHandler | None = None

    # -- properties ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def watch_dir(self) -> str:
        return self._watch_dir

    # -- wiring --------------------------------------------------------------

    def set_observer(self, fs_observer: Observer, handler: FileSystemEventHandler) -> None:
        """Called once from run_watch_loop to wire up the shared watchdog Observer."""
        with self._lock:
            self._fs_observer = fs_observer
            self._handler = handler

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch subprocess + schedule watchdog watch. Idempotent."""
        with self._lock:
            self._start_subprocess()
            self._schedule_watch()
            self._enabled = True

    def stop(self) -> None:
        """Terminate subprocess + unschedule watch. Idempotent."""
        with self._lock:
            self._stop_subprocess()
            self._unschedule_watch()
            self._enabled = False

    def toggle(self) -> bool:
        """Toggle and return the new state."""
        with self._lock:
            if self._enabled:
                self._stop_subprocess()
                self._unschedule_watch()
                self._enabled = False
            else:
                self._start_subprocess()
                self._schedule_watch()
                self._enabled = True
            return self._enabled

    def start_subprocess_only(self) -> None:
        """Start just the websocket subprocess (watchdog wired later)."""
        with self._lock:
            self._start_subprocess()

    def schedule_watch(self) -> None:
        """Schedule the watchdog directory watch (called after set_observer)."""
        with self._lock:
            self._schedule_watch()

    def shutdown(self) -> None:
        """Forcefully stop subprocess (for atexit). Does not touch watchdog."""
        with self._lock:
            self._stop_subprocess()

    # -- internal (must hold lock) -------------------------------------------

    def _start_subprocess(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return  # already running
        # Use None (inherit) instead of sys.stdout/sys.stderr directly,
        # because uvicorn may replace them with objects lacking fileno().
        try:
            sys.stdout.fileno()
            stdout, stderr = sys.stdout, sys.stderr
        except (io.UnsupportedOperation, AttributeError):
            stdout, stderr = None, None
        self._proc = subprocess.Popen(
            [sys.executable, "-u", self._script],
            stdout=stdout,
            stderr=stderr,
        )
        log.info("%s websocket started (pid %s)", self.label, self._proc.pid)

    def _stop_subprocess(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        log.info("%s websocket stopped", self.label)
        self._proc = None

    def _schedule_watch(self) -> None:
        if self._fs_observer is None or self._handler is None:
            return  # not wired yet
        if self._watch_handle is not None:
            return  # already scheduled
        d = Path(self._watch_dir)
        d.mkdir(parents=True, exist_ok=True)
        self._watch_handle = self._fs_observer.schedule(
            self._handler, str(d), recursive=False,
        )
        log.info("%s watchdog scheduled: %s", self.label, d)

    def _unschedule_watch(self) -> None:
        if self._fs_observer is None or self._watch_handle is None:
            return
        try:
            self._fs_observer.unschedule(self._watch_handle)
        except Exception:
            pass  # already unscheduled
        log.info("%s watchdog unscheduled", self.label)
        self._watch_handle = None


class FeedRegistry:
    """Holds all FeedManagers by name."""

    def __init__(self) -> None:
        self.feeds: dict[str, FeedManager] = {}

    def register(self, fm: FeedManager) -> None:
        self.feeds[fm.name] = fm

    def get(self, name: str) -> FeedManager | None:
        return self.feeds.get(name)

    def shutdown_all(self) -> None:
        for fm in self.feeds.values():
            fm.shutdown()

    def status(self) -> dict[str, bool]:
        return {name: fm.enabled for name, fm in self.feeds.items()}
