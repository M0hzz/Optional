"""SQLite behaviour: migrations, pragmas, constraints, transactions, concurrency, backups, locks."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from optlab.portfolio import db as dbm
from optlab.portfolio.models import (Account, AssetType, Base, Lifecycle, Position, PositionLeg, Side)


def _account(s, name="A"):
    a = Account(name=name)
    s.add(a)
    s.flush()
    return a


def _position(account_id, **kw):
    now = datetime.now(timezone.utc)
    p = Position(account_id=account_id, name="p", underlying="XYZ", strategy="LONG_STOCK", opened_at=now, **kw)
    p.legs = [PositionLeg(leg_index=0, asset_type=AssetType.STOCK, side=Side.BUY, quantity=1, multiplier=1,
                          fill_price_units=10000)]
    return p


def test_migration_from_empty_matches_the_models_and_rolls_back(db_url):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    assert dbm.upgrade(db_url) == dbm.head_revision() == "0002"
    eng = dbm.make_engine(db_url)
    with eng.connect() as c:
        assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []      # no drift
    eng.dispose()
    from alembic import command

    command.downgrade(dbm.alembic_config(db_url), "base")
    path = dbm.sqlite_path(db_url)
    tables = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables - {"sqlite_sequence"} == {"alembic_version"}     # sqlite_sequence: SQLite's own AUTOINCREMENT table
    assert dbm.upgrade(db_url) == "0002"                         # and forward again
    assert dbm.upgrade(db_url) == "0002"                         # idempotent


def test_concurrent_startup_migrations_run_once(db_url):
    errors = []

    def go():
        try:
            dbm.upgrade(db_url)
        except Exception as exc:            # pragma: no cover - reported below
            errors.append(exc)
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and dbm.current_revision(db_url) == "0002"


def test_pragmas(database):
    assert database.pragma("journal_mode") == "wal"
    assert database.pragma("foreign_keys") == 1
    assert database.pragma("busy_timeout") == 5000
    assert database.pragma("synchronous") == 1                    # NORMAL


def test_constraints_foreign_keys_and_monitoring_flag(database):
    with pytest.raises(IntegrityError):                            # position for a missing account
        with database.session() as s:
            s.add(_position("no-such-account"))
    with database.session() as s:
        a = _account(s)
        aid = a.id
    with database.session() as s:                                  # Phase 2 (migration 0002) lifted the guard
        s.add(_position(aid, monitoring_enabled=True))
    with pytest.raises(IntegrityError):                            # quantity must be positive
        with database.session() as s:
            p = _position(aid)
            p.legs[0].quantity = 0
            s.add(p)
    with pytest.raises(IntegrityError):                            # an option leg needs strike and expiration
        with database.session() as s:
            p = _position(aid)
            p.legs[0].asset_type = AssetType.OPTION
            s.add(p)
    with pytest.raises(IntegrityError):                            # account with positions can't be deleted
        with database.session() as s:
            s.add(_position(aid))
            s.flush()
            s.execute(text("DELETE FROM accounts WHERE id = :i"), {"i": aid})
    with pytest.raises(StatementError, match="naive datetime"):    # naive timestamps are refused
        with database.session() as s:
            p = _position(aid)
            p.opened_at = datetime(2026, 1, 1)
            s.add(p)


def test_transaction_rolls_back_on_error(database):
    with pytest.raises(RuntimeError):
        with database.session() as s:
            _account(s, "will vanish")
            raise RuntimeError("boom")
    with database.session(write=False) as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 0


def test_timestamps_roundtrip_as_utc(database):
    with database.session() as s:
        a = _account(s)
        p = _position(a.id)
        p.opened_at = datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc).astimezone()
        s.add(p)
        s.flush()
        pid = p.id
    with database.session(write=False) as s:
        got = s.get(Position, pid).opened_at
    assert got.tzinfo == timezone.utc and got == datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc)


def test_concurrent_reads_and_writes(db_url, database):
    with database.session() as s:
        aid = _account(s).id
    errors, n = [], 25

    def writer():
        d = dbm.Database(db_url)
        try:
            for _ in range(n):
                with d.session() as s:
                    s.add(_position(aid))
        except Exception as exc:
            errors.append(exc)
        finally:
            d.dispose()

    def reader():
        d = dbm.Database(db_url)
        try:
            for _ in range(n):
                with d.session(write=False) as s:
                    s.scalar(select(func.count()).select_from(Position))
        except Exception as exc:
            errors.append(exc)
        finally:
            d.dispose()
    ts = [threading.Thread(target=f) for f in (writer, writer, writer, reader, reader)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    with database.session(write=False) as s:
        assert s.scalar(select(func.count()).select_from(Position)) == 3 * n


def test_version_guard_rejects_an_update_of_a_changed_row(database):
    """A write session holds the lock from its first read, so two can't interleave; the API checks the
    client's version (409 VERSION_CONFLICT). Underneath, SQLAlchemy's version guard refuses an UPDATE
    whose row version changed since it was read."""
    from sqlalchemy.orm.exc import StaleDataError

    with database.session() as s:
        aid = _account(s).id
    with pytest.raises(StaleDataError):
        with database.session() as s:
            a = s.get(Account, aid)
            assert a.version == 1
            s.execute(text("UPDATE accounts SET version = version + 1 WHERE id = :i"), {"i": aid})
            a.name = "renamed"
            s.flush()
    with database.session() as s:
        a = s.get(Account, aid)
        a.name = "renamed"
    with database.session(write=False) as s:
        assert s.get(Account, aid).version == 2          # the failed transaction (and its bump) rolled back


def test_backup_while_wal_is_active(tmp_path, db_url, database):
    with database.session() as s:
        aid = _account(s).id
        for _ in range(10):
            s.add(_position(aid))
    holder = database.sessions()                      # an open reader keeps the WAL in use
    holder.scalar(select(func.count()).select_from(Position))
    out = dbm.backup(tmp_path / "backup" / "copy.sqlite", db_url)
    holder.close()
    with sqlite3.connect(out) as c:
        assert c.execute("SELECT count(*) FROM positions").fetchone()[0] == 10
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_single_instance_lock(db_url):
    a, b = dbm.InstanceLock(db_url), dbm.InstanceLock(db_url)
    a.acquire()
    with pytest.raises(RuntimeError, match="Another optlab API instance"):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


def test_lifecycle_check_constraint(database):
    with database.session() as s:
        aid = _account(s).id
    with pytest.raises(IntegrityError):
        with database.session() as s:
            p = _position(aid)
            p.lifecycle = Lifecycle.OPEN
            p.closed_at = datetime.now(timezone.utc)
            s.add(p)

