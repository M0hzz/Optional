"""Live scanner and position monitor. Checks only; it never places trades.

    python scripts/monitor.py                  # wait for schedule.start, scan every minute until end
    python scripts/monitor.py --once           # one pass right now, print results, exit
    python scripts/monitor.py --now            # start scanning immediately, ignore the clock
    python scripts/monitor.py --start 10:15 --end 15:45
    python scripts/monitor.py --synthetic --once   # offline test with fake data

Settings live in scans.yaml. Results go to data/monitor/ for the dashboard's Monitor tab.
Stop with Ctrl+C.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from optlab.config import load_config  # noqa: E402
from optlab.live.expr import ExprError  # noqa: E402
from optlab.live.feed import make_feed  # noqa: E402
from optlab.live.monitor import AlertTracker, Schedule, notify, write_results  # noqa: E402
from optlab.live.scanner import load_scan_config, run_pass  # noqa: E402

OUT = ROOT / "data" / "monitor"


def show(result, verbose: bool) -> None:
    stamp = time.strftime("%H:%M:%S")
    parts = [f"[{stamp}] {result.seconds:.1f}s, {result.symbols} symbols"]
    if not result.hits.empty:
        for scan, g in result.hits.groupby("scan", sort=False):
            syms = list(dict.fromkeys(g["symbol"]))
            parts.append(f"{scan}: {', '.join(syms[:8])}{' ...' if len(syms) > 8 else ''}")
    else:
        parts.append("no scan hits")
    for r in result.positions.to_dict("records"):
        pnl = f" {r['pnl_pct']:+.0f}%" if pd.notna(r.get("pnl_pct")) else ""
        parts.append(f"{r['position']}: {r['status']}{pnl}")
    print(" | ".join(parts), flush=True)
    if verbose and not result.hits.empty:
        cols = ["scan", "symbol", "price", "expiration", "strike", "type", "bid", "ask", "delta", "iv",
                "open_interest", "annualized_yield", "details"]
        with pd.option_context("display.width", 250, "display.max_columns", 20, "display.max_colwidth", 70):
            print(result.hits[[c for c in cols if c in result.hits]].round(3).to_string(index=False))
    if verbose and not result.positions.empty:
        with pd.option_context("display.width", 250, "display.max_columns", 20, "display.max_colwidth", 70):
            print(result.positions.round(2).to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "scans.yaml"))
    ap.add_argument("--once", action="store_true", help="run one pass now and exit")
    ap.add_argument("--now", action="store_true", help="ignore the schedule; scan continuously from now")
    ap.add_argument("--start", help="override schedule.start, e.g. 10:15")
    ap.add_argument("--end", help="override schedule.end, e.g. 15:45")
    ap.add_argument("--synthetic", action="store_true", help="use offline synthetic data")
    ap.add_argument("--no-notify", action="store_true", help="no desktop notifications")
    args = ap.parse_args()

    try:
        cfg = load_scan_config(args.config)
    except (ExprError, OSError) as exc:
        sys.exit(str(exc))
    sched = Schedule.from_config(cfg.get("schedule"), args.start, args.end)
    data = cfg.get("data") or {}
    source = "synthetic" if args.synthetic else data.get("source", "live")
    alerts_cfg = cfg.get("alerts") or {}
    desktop = alerts_cfg.get("desktop", True) and not args.no_notify
    rate = load_config()["market"]["risk_free_rate"]
    print(f"Loading {source} data feed...", flush=True)
    feed = make_feed(source, rate=rate, chain_source=data.get("chain_source", "cboe"),
                     chain_refresh=float(data.get("chain_refresh_seconds", 50)))
    tracker = AlertTracker(OUT / "alert_state.json", alerts_cfg.get("repeat_exit_minutes", 15))
    if desktop and not notify("optlab monitor", "Notifications are working.", "low"):
        print("notify-send not found: alerts will print here only. On Ubuntu: sudo apt install libnotify-bin")
        desktop = False

    def one_pass():
        result = run_pass(cfg, feed)
        now = sched.now()
        alerts = tracker.alerts(result.hits, result.positions, now)
        write_results(OUT, result, None if args.once else sched, alerts, now)
        show(result, verbose=args.once)
        for title, body, urgency in alerts:
            print(f"  ALERT {title}: {body.replace(chr(10), ' | ')}", flush=True)
            if desktop:
                notify(title, body, urgency)
        for e in result.errors:
            print(f"  ! {e}", flush=True)
        if result.seconds > sched.every and not args.once:
            print(f"  ! pass took {result.seconds:.0f}s, longer than every_seconds={sched.every}; "
                  "shrink the watchlists or raise every_seconds", flush=True)

    if args.once:
        one_pass()
        return

    print(f"Schedule: {'continuous from now' if args.now else sched.describe()}. Ctrl+C to stop.", flush=True)
    waiting_note = None
    try:
        while True:
            now = sched.now()
            if args.now or sched.is_open(now):
                one_pass()
                nxt = sched.next_tick(sched.now())
                time.sleep(max((nxt - sched.now()).total_seconds(), 0.5))
                waiting_note = None
            else:
                start = sched.next_start(now)
                if waiting_note != start:
                    print(f"Waiting until {start:%a %Y-%m-%d %H:%M %Z} ...", flush=True)
                    (OUT / "status.json").parent.mkdir(parents=True, exist_ok=True)
                    status = OUT / "status.json"
                    prev = json.loads(status.read_text()) if status.exists() else {}
                    status.write_text(json.dumps({**prev, "waiting_until": start.isoformat(timespec="minutes"),
                                                  "schedule": sched.describe()}, indent=1))
                    waiting_note = start
                time.sleep(min(max((start - now).total_seconds(), 1), 30))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
