"""Runs the news ingest every `news.interval_minutes` in a background thread of the API process.

Like the monitor, only one process per database fetches news: the runner holds `optlab.news.lock` next to
the database for its lifetime. Switch it off with OPTLAB_NEWS=off (or news.enabled: false).
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from ..portfolio import db as dbm
from . import settings as nsettings
from .pipeline import RunSummary, ingest

log = logging.getLogger("optlab.news")


def lock_path(url: str) -> Path:
    p = dbm.sqlite_path(url)
    return p.with_suffix(".news.lock") if p else dbm.PROJECT_ROOT / "data" / "optlab.news.lock"


class NewsLock:
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


class NewsRunner:
    def __init__(self, db, settings: nsettings.NewsSettings | None = None, first_delay: float = 15.0,
                 clock=lambda: datetime.now(timezone.utc)):
        self.db, self.clock = db, clock
        self.settings = settings or nsettings.load()
        self.lock = NewsLock(db.url)
        self.state: dict = {}                      # provider memory (last calendar fetch, ...)
        self.first_delay = first_delay
        self._stop, self._wake = threading.Event(), threading.Event()
        self._run_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.last: RunSummary | None = None
        self.next_due: datetime | None = None
        self.status_text: str = "stopped"
        self.detail: str | None = None
        self.running_since: datetime | None = None

    def start(self) -> bool:
        if not self.settings.enabled:
            self.status_text, self.detail = "disabled", "news.enabled is false in config.yaml."
            return False
        if not self.lock.try_acquire():
            self.status_text = "elsewhere"
            self.detail = f"Another process fetches news for this database (pid {self.lock.holder() or '?'})."
            log.info(self.detail)
            return False
        self.next_due = self.clock() + timedelta(seconds=self.first_delay)
        self.thread = threading.Thread(target=self._loop, name="optlab-news", daemon=True)
        self.thread.start()
        return True

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        if self.thread is not None:
            self.thread.join(timeout)
        self.lock.release()
        self.status_text = "stopped"

    def run_now(self) -> RunSummary:
        with self._run_lock:
            self.running_since = self.clock()
            try:
                self.last = ingest(self.db, self.settings, state=self.state)
            finally:
                self.running_since = None
            return self.last

    def _loop(self) -> None:
        log.info("news ingest started (every %s minutes)", self.settings.interval_minutes)
        while not self._stop.is_set():
            now = self.clock()
            if self.next_due is not None and now >= self.next_due:
                self.status_text = "running"
                try:
                    s = self.run_now()
                    log.info("news: %s new articles; %s", s.new_articles,
                             ", ".join(f"{r.code} {r.status}" for r in s.sources))
                    self.detail = None
                except Exception as exc:
                    log.exception("news ingest crashed")
                    self.detail = f"the last run failed ({type(exc).__name__})"
                self.next_due = self.clock() + timedelta(minutes=self.settings.interval_minutes)
            self.status_text = "waiting"
            wait = 5.0 if self.next_due is None else max(0.2, min(5.0, (self.next_due - self.clock()).total_seconds()))
            self._wake.wait(wait)
            self._wake.clear()
        log.info("news ingest stopped")

    def status(self) -> dict:
        return {"state": "running" if self.running_since else self.status_text, "detail": self.detail,
                "next_run_at": self.next_due, "interval_minutes": self.settings.interval_minutes,
                "in_process": self.lock.held, "lock_holder_pid": self.lock.holder(),
                "last_run": self.last.as_dict() if self.last else None}
