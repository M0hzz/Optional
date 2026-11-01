"""Portfolio monitor without the API: checks the positions, watched contracts and alert rules in the
portfolio database (data/optlab.sqlite). Checks only; it never places trades.

    python scripts/portfolio_monitor.py --once        # one pass now, print what it found, exit
    python scripts/portfolio_monitor.py               # run on the schedule in the monitor settings

Normally you don't need this: the API (optlab-api) runs the same monitor in the background. Only one
monitor runs per database; if the API already runs it, this script says so and exits.
Settings (schedule, rules, notifications) are edited in the dashboard: Monitor → Settings.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from optlab.monitoring.engine import Engine  # noqa: E402
from optlab.monitoring.runner import MonitorLock, Runner  # noqa: E402
from optlab.portfolio import db as dbm  # noqa: E402
from optlab.portfolio.quotes import default_service  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one pass now and exit")
    ap.add_argument("--no-notify", action="store_true", help="no desktop notifications")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = dbm.database_url()
    print(f"Database at revision {dbm.upgrade(url)} ({url})", flush=True)
    db = dbm.Database(url)
    quotes = default_service()
    engine = Engine(db, quotes, **({"notifier": None} if args.no_notify else {}))
    if args.once:
        lock = MonitorLock(url)
        if not lock.try_acquire():
            sys.exit(f"The monitor already runs in another process (pid {lock.holder()}): use the dashboard's "
                     "'Run now' instead.")
        try:
            s = engine.run_pass("manual")
        finally:
            lock.release()
        print(f"{'OK' if s.ok else 'FAILED'} in {s.seconds:.1f}s: {s.positions} positions, {s.contracts} contracts, "
              f"{s.rules} alert rules, {s.events} new events, {s.notified} notified")
        for e in s.errors:
            print(f"  ! {e}")
        from sqlalchemy import select

        from optlab.portfolio.models import Event

        with db.session(write=False) as ses:
            for ev in ses.scalars(select(Event).where(Event.run_id == s.run_id).order_by(Event.seq)):
                print(f"  [{ev.severity}] {ev.title}: {ev.message}")
        return
    runner = Runner(db, engine)
    if not runner.start():
        sys.exit(runner.detail)
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    try:
        print("Monitoring on the schedule in the monitor settings. Ctrl+C to stop.", flush=True)
        done.wait()
    except KeyboardInterrupt:
        pass
    runner.stop()
    print("Stopped.")


if __name__ == "__main__":
    main()
