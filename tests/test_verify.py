"""Phase 7: backup and migration verification (scripts/portfolio_db.py verify) and safe restores."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from conftest import opt

from optlab.portfolio import db as dbm
from optlab.portfolio.verify import integrity, table_counts, verify

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1"


def seeded(client, account):
    client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ",
                                          "legs": [opt("SELL", "PUT", 95, "1.10"), opt("BUY", "PUT", 90, "0.15")]})
    client.post(f"{API}/journal", json={"account_id": account["id"], "kind": "daily_thesis", "body": "x"})
    client.get(f"{API}/accounts/{account['id']}/progress")


def test_a_healthy_database_passes_every_check(client, account, db_url, tmp_path):
    seeded(client, account)
    rep = verify(db_url, workdir=tmp_path / "w")
    assert rep.ok, rep.text()
    names = [c.name for c in rep.checks]
    assert names == ["backup", "integrity", "revision", "schema", "triggers", "restore", "chain", "round trip"]
    assert table_counts(dbm.sqlite_path(db_url))["positions"] == 1          # the source was only read


def test_an_old_revision_and_missing_triggers_are_reported(db_url, tmp_path):
    dbm.upgrade(db_url, "0006")
    rep = verify(db_url, workdir=tmp_path / "w")
    rev = next(c for c in rep.checks if c.name == "revision")
    assert not rev.ok and "0006" in rev.detail and not rep.ok
    dbm.upgrade(db_url)
    con = sqlite3.connect(dbm.sqlite_path(db_url))
    con.execute("DROP TRIGGER process_awards_no_delete")
    con.commit()
    con.close()
    rep = verify(db_url, workdir=tmp_path / "w2")
    trg = next(c for c in rep.checks if c.name == "triggers")
    assert not trg.ok and "process_awards_no_delete" in trg.detail


def test_a_corrupt_backup_fails_and_cant_be_restored(client, account, db_url, tmp_path):
    seeded(client, account)
    good = dbm.backup(tmp_path / "good.sqlite", db_url)
    bad = tmp_path / "bad.sqlite"
    shutil.copyfile(good, bad)
    data = bytearray(bad.read_bytes())
    data[0:16] = b"not a database!!"
    bad.write_bytes(bytes(data))
    ok, detail = integrity(bad)
    assert not ok and "not a readable SQLite database" in detail
    assert integrity(good)[0]
    rep = verify(db_url, backup_file=bad, workdir=tmp_path / "w")
    assert not rep.ok and [c.name for c in rep.checks] == ["backup", "integrity"]
    env = {"OPTLAB_DB_URL": f"sqlite:///{tmp_path / 'target.sqlite'}", "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "portfolio_db.py"), "restore", str(bad), "--yes"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode != 0 and "Not restoring" in r.stderr
    assert not (tmp_path / "target.sqlite").exists()


def test_the_cli_reports_and_exits_nonzero_on_failure(client, account, db_url, tmp_path):
    seeded(client, account)
    env = {"OPTLAB_DB_URL": db_url, "PATH": "/usr/bin:/bin"}
    ok = subprocess.run([sys.executable, str(ROOT / "scripts" / "portfolio_db.py"), "verify"], capture_output=True,
                        text=True, env=env, timeout=300)
    assert ok.returncode == 0 and "all checks passed" in ok.stdout and ok.stdout.count("PASS") == 8
    missing = subprocess.run([sys.executable, str(ROOT / "scripts" / "portfolio_db.py"), "verify", "--backup",
                              str(tmp_path / "nope.sqlite")], capture_output=True, text=True, env=env, timeout=120)
    assert missing.returncode == 1 and "FAIL" in missing.stdout
