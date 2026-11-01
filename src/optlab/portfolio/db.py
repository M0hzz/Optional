"""SQLite engine, sessions, migrations and backups for the portfolio database.

Connection settings (applied to every new connection):
    journal_mode=WAL      readers never block the writer and the writer never blocks readers
    synchronous=NORMAL    safe with WAL: a power cut can lose the last commit, never corrupt the file
    foreign_keys=ON       SQLite ships with them off
    busy_timeout=5000     a second writer waits up to 5 s for the lock instead of failing at once

Transactions: one session per API request; `session_scope` commits on success and rolls back on any
exception. Writes use BEGIN IMMEDIATE so a write transaction takes the lock up front and can't fail
halfway with "database is locked" after reading.

Migrations: `upgrade()` runs Alembic forward to head under an exclusive file lock (so two processes
starting together don't migrate twice). Downgrades are never run automatically.

Backups: `backup()` uses SQLite's online backup API, which copies a consistent snapshot even while the
WAL is active and other connections are writing; copying the .sqlite file alone would miss the WAL.

Replacing SQLite with PostgreSQL later means changing OPTLAB_DB_URL; the pragmas and BEGIN IMMEDIATE
apply only to SQLite.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import PROJECT_ROOT

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "optlab.sqlite"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def database_url() -> str:
    return os.environ.get("OPTLAB_DB_URL") or f"sqlite:///{DEFAULT_DB_PATH}"


def sqlite_path(url: str) -> Path | None:
    return Path(url.removeprefix("sqlite:///")) if url.startswith("sqlite:///") else None


def make_engine(url: str | None = None, write: bool = True) -> Engine:
    url = url or database_url()
    path = sqlite_path(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 5} if path else {},
                        pool_pre_ping=True)
    if path is not None:
        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _record):
            dbapi_conn.isolation_level = None          # let SQLAlchemy emit BEGIN itself (see below)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        @event.listens_for(eng, "begin")
        def _begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
    return eng


class Database:
    """One engine + session factory. The API keeps one per process."""

    def __init__(self, url: str | None = None):
        self.url = url or database_url()
        self.engine = make_engine(self.url)                       # BEGIN IMMEDIATE: writes
        self.read_engine = make_engine(self.url, write=False)     # plain BEGIN: reads never queue behind writes
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.read_sessions = sessionmaker(self.read_engine, expire_on_commit=False)

    @contextmanager
    def session(self, write: bool = True) -> Iterator[Session]:
        """Commit on success, roll back on any exception."""
        s = (self.sessions if write else self.read_sessions)()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()

    def pragma(self, name: str):
        with self.engine.connect() as c:
            return c.exec_driver_sql(f"PRAGMA {name}").scalar()

    def dispose(self) -> None:
        self.engine.dispose()
        self.read_engine.dispose()


# ------------------------------------------------------------------ migrations
def alembic_config(url: str):
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def upgrade(url: str | None = None, revision: str = "head") -> str | None:
    """Forward migrations only, under an exclusive lock. Returns the revision now in place."""
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    url = url or database_url()
    path = sqlite_path(url)
    lock = (path.with_suffix(".migrate.lock") if path else PROJECT_ROOT / "data" / "optlab.migrate.lock")
    with _file_lock(lock):
        command.upgrade(alembic_config(url), revision)
    eng = make_engine(url)
    try:
        with eng.connect() as c:
            return MigrationContext.configure(c).get_current_revision()
    finally:
        eng.dispose()


def current_revision(url: str | None = None) -> str | None:
    from alembic.runtime.migration import MigrationContext

    eng = make_engine(url)
    try:
        with eng.connect() as c:
            return MigrationContext.configure(c).get_current_revision()
    finally:
        eng.dispose()


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(database_url())).get_current_head() or "base"


# ------------------------------------------------------------------ backup and single instance
def backup(dest: str | Path, url: str | None = None) -> Path:
    """Consistent online copy of a SQLite database (safe while WAL is active and others write)."""
    src = sqlite_path(url or database_url())
    if src is None:
        raise ValueError("backup() supports SQLite only; use pg_dump for PostgreSQL")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
        s.backup(d)
    return dest


class InstanceLock:
    """Held for the API process's lifetime so two API instances (say, the systemd service and a Docker
    container) can't serve the same SQLite file at once. Phase 2 background jobs rely on it too."""

    def __init__(self, url: str | None = None):
        path = sqlite_path(url or database_url())
        self.path = (path.with_suffix(".api.lock") if path else PROJECT_ROOT / "data" / "optlab.api.lock")
        self.fh: TextIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise RuntimeError(f"Another optlab API instance is already using this database (lock {self.path}). "
                               "Stop it first: systemctl --user stop optlab-api, or docker compose down.") from None
        fh.write(str(os.getpid()))
        fh.flush()
        self.fh = fh

    def release(self) -> None:
        if self.fh is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None
