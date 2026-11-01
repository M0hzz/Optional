"""Runs monitoring passes on the schedule, in a background thread of the API process (or of
scripts/portfolio_monitor.py).

Only one monitor may run per database: the runner holds an exclusive lock file next to it
(`optlab.monitor.lock`) for its lifetime. A second process that tries to start one finds the lock taken
and stays idle, so passes never run twice, whether the API runs under systemd, in Docker, or both by
mistake (the API's own instance lock catches that case first).
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from ..portfolio import db as dbm
from . import settings as msettings
from .calendar import Window
from .engine import Engine, PassSummary

log = logging.getLogger("optlab.monitor")


def lock_path(url: str) -> Path:
    p = dbm.sqlite_path(url)
    return p.with_suffix(".monitor.lock") if p else dbm.PROJECT_ROOT / "data" / "optlab.monitor.lock"


class MonitorLock:
    def __init__(self, url: str):
        self.path = lock_path(url)
        self.fh: TextIO | None = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self.fh = fh
        return True

    def holder(self) -> str | None:
        try:
            return self.path.read_text().strip() or None
        except OSError:
            return None

    def release(self) -> None:
        if self.fh is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None

    @property
    def held(self) -> bool:
        return self.fh is not None


class Runner:
    def __init__(self, db, engine: Engine, clock=lambda: datetime.now(timezone.utc)):
        self.db, self.engine, self.clock = db, engine, clock
        self.lock = MonitorLock(db.url)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.next_due: datetime | None = None
        self.last: PassSummary | None = None
        self.state = "stopped"          # stopped | idle (another process runs it) | waiting | running | disabled
        self.detail: str | None = None
        self.max_sleep = 5.0            # seconds between schedule checks (settings changes apply within this)

    def start(self) -> bool:
        if not self.lock.try_acquire():
            self.state = "elsewhere"
            self.detail = f"Another process runs the monitor for this database (pid {self.lock.holder() or '?'})."
            log.info(self.detail)
            return False
        self.thread = threading.Thread(target=self._loop, name="optlab-monitor", daemon=True)
        self.thread.start()
        return True

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        if self.thread is not None:
            self.thread.join(timeout)
        self.lock.release()
        self.state = "stopped"

    def run_now(self, trigger: str = "manual") -> PassSummary:
        """One pass at once (from the API's 'Run now'). Waits for a pass already in progress."""
        s = self.engine.run_pass(trigger)
        self.last = s
        return s

    def _loop(self) -> None:
        log.info("portfolio monitor started")
        while not self._stop.is_set():
            try:
                with self.db.session(write=False) as s:
                    st = msettings.load(s)
            except Exception as exc:              # database not migrated yet, disk issue...
                self.state, self.detail = "error", f"can't read settings: {exc}"
                self._stop.wait(10)
                continue
            now = self.clock()
            win = Window(st.schedule)
            if not st.enabled:
                self.state, self.detail, self.next_due = "disabled", "Monitoring is switched off in settings.", None
            elif not win.is_open(now):
                self.state, self.detail = "waiting", win.why_closed(now)
                self.next_due = win.next_open(now)
            else:
                if self.next_due is None or self.next_due > win.next_tick(now):
                    self.next_due = win.next_tick(now)      # first tick after start, or the interval changed
                if now >= self.next_due:
                    self.state, self.detail = "running", None
                    try:
                        self.last = self.engine.run_pass("schedule")
                    except Exception:
                        log.exception("monitor pass crashed")
                    self.next_due = win.next_tick(self.clock())
                self.state, self.detail = "waiting", None
            delay = self.max_sleep
            if self.next_due is not None:
                delay = max(min(0.2, self.max_sleep), min(self.max_sleep, (self.next_due - self.clock()).total_seconds()))
            self._wake.wait(delay)
            self._wake.clear()
        log.info("portfolio monitor stopped")

    def status(self) -> dict:
        return {"state": "running" if self.engine.running_since else self.state, "detail": self.detail,
                "next_pass_at": self.next_due, "in_process": self.lock.held, "lock_holder_pid": self.lock.holder(),
                "running_since": self.engine.running_since}
