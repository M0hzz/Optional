"""Backup and migration verification (Phase 7). Everything runs on copies in a temporary folder: the database
you point it at is only read (through SQLite's online backup API, safe while the API is running).

    1  backup        an online backup of the database (or the backup file given)
    2  integrity     PRAGMA integrity_check and foreign_key_check on the backup
    3  revision      the backup's migration revision against the latest one
    4  schema        no difference between the backup's schema and the models (alembic check)
    5  triggers      the append-only triggers (scores, points ledger, achievements) are present
    6  restore       the backup restored into a fresh file opens, and every table has the same row count
    7  chain         an empty database migrates from base to the latest revision and matches the models
    8  round trip    on a copy: the latest migration is downgraded and re-applied, and every table that
                     existed before it keeps its row count
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbm

TRIGGERS = {f"{t}_no_{op}" for t in ("daily_scores", "process_awards", "user_achievements") for op in ("update", "delete")}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    source: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append(Check(name, ok, detail))
        return ok

    def text(self) -> str:
        lines = [f"verification of {self.source}"]
        lines += [f"  {'PASS' if c.ok else 'FAIL'}  {c.name:12} {c.detail}" for c in self.checks]
        lines.append("result: " + ("all checks passed" if self.ok else "FAILED"))
        return "\n".join(lines)


def table_counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in sorted(names)}
    finally:
        con.close()


def integrity(path: Path) -> tuple[bool, str]:
    if not Path(path).exists():
        return False, f"{path} doesn't exist"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ic = [r[0] for r in con.execute("PRAGMA integrity_check")]
            fk = list(con.execute("PRAGMA foreign_key_check"))
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        return False, f"not a readable SQLite database ({exc})"
    ok = ic == ["ok"] and not fk
    return ok, ("integrity ok, no foreign-key violations" if ok else
                f"integrity: {'; '.join(ic[:3])}; foreign-key violations: {len(fk)}")


def _schema_diff(url: str) -> list:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from .models import Base

    eng = dbm.make_engine(url)
    try:
        with eng.connect() as c:
            return compare_metadata(MigrationContext.configure(c), Base.metadata)
    finally:
        eng.dispose()


def verify(url: str | None = None, backup_file: str | Path | None = None, workdir: str | Path | None = None) -> Report:
    url = url or dbm.database_url()
    src = Path(backup_file) if backup_file else dbm.sqlite_path(url)
    rep = Report(str(src))
    tmp = Path(workdir or tempfile.mkdtemp(prefix="optlab-verify-"))
    tmp.mkdir(parents=True, exist_ok=True)
    head = dbm.head_revision()

    # 1 backup
    copy = tmp / "backup.sqlite"
    try:
        if backup_file:
            shutil.copyfile(backup_file, copy)
            rep.add("backup", True, f"copied {backup_file}")
        else:
            dbm.backup(copy, url)
            rep.add("backup", True, f"online backup ({copy.stat().st_size:,} bytes)")
    except Exception as exc:
        rep.add("backup", False, f"{type(exc).__name__}: {exc}")
        return rep

    # 2 integrity
    ok, detail = integrity(copy)
    if not rep.add("integrity", ok, detail):
        return rep

    # 3 revision
    curl = f"sqlite:///{copy}"
    rev = dbm.current_revision(curl)
    rep.add("revision", rev == head, f"{rev} (latest {head})" + ("" if rev == head else ": start the API (or run "
                                                                                         "`portfolio_db.py upgrade`) to migrate"))

    # 4 schema (only meaningful at the latest revision)
    if rev == head:
        diff = _schema_diff(curl)
        rep.add("schema", not diff, "matches the models" if not diff else f"{len(diff)} difference(s): {diff[:3]}")

    # 5 triggers
    con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    con.close()
    need = TRIGGERS if rev and rev >= "0005" else set()
    rep.add("triggers", need <= have, f"{len(need & have)} of {len(need)} append-only triggers present"
            + (f"; missing {sorted(need - have)}" if need - have else ""))

    # 6 restore into a fresh file, compare counts
    before = table_counts(copy)
    restored = tmp / "restored.sqlite"
    s = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    d = sqlite3.connect(restored)
    s.backup(d)
    s.close()
    d.close()
    after = table_counts(restored)
    diff_counts = {t: (before.get(t), after.get(t)) for t in set(before) | set(after) if before.get(t) != after.get(t)}
    ok_r, det_r = integrity(restored)
    rep.add("restore", not diff_counts and ok_r,
            f"{len(after)} tables, {sum(after.values()):,} rows restored identically" if not diff_counts and ok_r
            else f"differences: {diff_counts}; {det_r}")

    # 7 chain from empty
    empty = f"sqlite:///{tmp / 'empty.sqlite'}"
    try:
        r7 = dbm.upgrade(empty)
        diff = _schema_diff(empty)
        rep.add("chain", r7 == head and not diff, f"base → {r7} on an empty database; schema matches the models"
                if not diff else f"base → {r7}; {len(diff)} schema difference(s)")
    except Exception as exc:
        rep.add("chain", False, f"{type(exc).__name__}: {exc}")

    # 8 round trip of the latest migration on a copy
    if rev == head:
        rt = tmp / "roundtrip.sqlite"
        shutil.copyfile(copy, rt)
        rturl = f"sqlite:///{rt}"
        try:
            from alembic import command
            from alembic.script import ScriptDirectory

            prev = ScriptDirectory.from_config(dbm.alembic_config(rturl)).get_revision(head).down_revision
            command.downgrade(dbm.alembic_config(rturl), str(prev))
            mid = table_counts(rt)
            dbm.upgrade(rturl)
            end = table_counts(rt)
            kept = {t: before[t] for t in mid if t in before}
            changed = {t: (kept[t], end.get(t)) for t in kept if t != "alembic_version" and end.get(t) != kept[t]}
            rep.add("round trip", not changed and dbm.current_revision(rturl) == head,
                    f"{head} → {prev} → {head}; {len(kept) - 1} pre-existing tables keep their rows" if not changed
                    else f"row counts changed: {changed}")
        except Exception as exc:
            rep.add("round trip", False, f"{type(exc).__name__}: {exc}")
    return rep
