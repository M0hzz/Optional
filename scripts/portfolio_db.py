"""Portfolio database maintenance (data/optlab.sqlite, or OPTLAB_DB_URL).

    python scripts/portfolio_db.py status                 # current and latest migration, journal mode
    python scripts/portfolio_db.py backup                 # online backup to backups/<name>-<UTC time>.sqlite next to the database
    python scripts/portfolio_db.py backup /path/copy.sqlite
    python scripts/portfolio_db.py upgrade                # forward migrations (the API also does this at startup)
    python scripts/portfolio_db.py downgrade <revision> --yes   # backs up first; destructive for the dropped tables
    python scripts/portfolio_db.py restore <backup.sqlite> --yes  # stop the API first; backs up the current file first
    python scripts/portfolio_db.py verify                 # backup + integrity + schema + restore + migration checks
    python scripts/portfolio_db.py verify --backup <file> # the same checks on a backup file (e.g. before restoring it)

Backups use SQLite's online backup API, so they are consistent even while the API is running.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from optlab.portfolio import db as dbm  # noqa: E402


def _default_backup() -> Path:
    """backups/ next to the database file itself."""
    path = dbm.sqlite_path(dbm.database_url()) or ROOT / "data" / "optlab.sqlite"
    return path.parent / "backups" / f"{path.stem}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.sqlite"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    b = sub.add_parser("backup")
    b.add_argument("dest", nargs="?")
    sub.add_parser("upgrade")
    d = sub.add_parser("downgrade")
    d.add_argument("revision")
    d.add_argument("--yes", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("source")
    r.add_argument("--yes", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--backup")
    a = ap.parse_args()
    url = dbm.database_url()
    path = dbm.sqlite_path(url)

    if a.cmd == "status":
        print(f"database   {url}")
        print(f"revision   {dbm.current_revision(url)} (latest {dbm.head_revision()})")
        print(f"journal    {dbm.Database(url).pragma('journal_mode')}")
    elif a.cmd == "backup":
        print(f"backed up to {dbm.backup(a.dest or _default_backup(), url)}")
    elif a.cmd == "upgrade":
        print(f"now at revision {dbm.upgrade(url)}")
    elif a.cmd == "downgrade":
        if not a.yes:
            sys.exit("Downgrading drops the tables added after that revision. Re-run with --yes.")
        print(f"backed up to {dbm.backup(_default_backup(), url)}")
        from alembic import command

        command.downgrade(dbm.alembic_config(url), a.revision)
        print(f"now at revision {dbm.current_revision(url)}")
    elif a.cmd == "verify":
        from optlab.portfolio.verify import verify

        report = verify(url, a.backup)
        print(report.text())
        sys.exit(0 if report.ok else 1)
    elif a.cmd == "restore":
        from optlab.portfolio.verify import integrity

        ok, detail = integrity(Path(a.source))
        if not ok:
            sys.exit(f"Not restoring {a.source}: {detail}")
        if not a.yes:
            sys.exit("Restoring replaces the current database. Stop the API first (systemctl --user stop optlab-api, "
                     "or docker compose stop api), then re-run with --yes.")
        lock = dbm.InstanceLock(url)
        try:
            lock.acquire()                     # refuses while the API is running
        except RuntimeError as exc:
            sys.exit(str(exc))
        try:
            if path.exists():
                print(f"current database backed up to {dbm.backup(_default_backup(), url)}")
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            shutil.copyfile(a.source, path)
            print(f"restored {a.source} -> {path}; revision {dbm.current_revision(url)}")
        finally:
            lock.release()


if __name__ == "__main__":
    main()
