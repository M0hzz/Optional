"""Rehearse a market day: replay a simulated session through the live monitor.

Runs your scans from scans.yaml, plus three demo positions, against a scripted
synthetic day (rally, a momentum burst, an afternoon selloff with an IV spike,
recovery), one pass per simulated minute from 09:30 to 16:00. You see the same
alerts the real monitor would send, stamped with simulated time. Nothing here is
market data and nothing is traded.

    python scripts/simulate_day.py                  # as fast as possible (a minute or two)
    python scripts/simulate_day.py --speed 1        # 1 real second per simulated minute
    python scripts/simulate_day.py --notify         # also send desktop notifications
    python scripts/simulate_day.py --own-positions  # use the positions in scans.yaml instead

Output: printed, plus data/simulation/day_log.txt and alerts.csv.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from optlab.config import load_config  # noqa: E402
from optlab.live.monitor import AlertTracker, notify  # noqa: E402
from optlab.live.replay import MINUTES, ReplayFeed, demo_positions  # noqa: E402
from optlab.live.scanner import load_scan_config, run_pass, scan_symbols  # noqa: E402

OUT = ROOT / "data" / "simulation"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "scans.yaml"))
    ap.add_argument("--date", help="session date to simulate, YYYY-MM-DD (default: next weekday)")
    ap.add_argument("--speed", type=float, default=0.0, help="real seconds per simulated minute (default 0 = fast)")
    ap.add_argument("--notify", action="store_true", help="send desktop notifications for alerts")
    ap.add_argument("--own-positions", action="store_true", help="use positions from scans.yaml")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    cfg = load_scan_config(args.config)
    day = date.fromisoformat(args.date) if args.date else date.today() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    symbols = sorted({s for sc in cfg["scans"] for s in scan_symbols(sc, cfg["watchlists"])}
                     | {"SPY", "IWM", "AAPL"} | {p["symbol"].upper() for p in cfg["positions"]})
    print(f"Building a simulated session for {day:%A %Y-%m-%d} ({len(symbols)} symbols)...", flush=True)
    feed = ReplayFeed(day, symbols, rate=load_config()["market"]["risk_free_rate"], seed=args.seed)
    if not args.own_positions:
        cfg["positions"] = demo_positions(feed)

    OUT.mkdir(parents=True, exist_ok=True)
    for f in ("alert_state.json", "alerts.csv"):
        (OUT / f).unlink(missing_ok=True)
    alerts_cfg = cfg.get("alerts") or {}
    tracker = AlertTracker(OUT / "alert_state.json", alerts_cfg.get("repeat_exit_minutes", 15),
                           alerts_cfg.get("rearm_minutes", 30), alerts_cfg.get("confirm_passes", 3))
    log_lines: list[str] = []

    def say(line: str = "") -> None:
        print(line, flush=True)
        log_lines.append(line)

    say(f"Simulated session {day}: positions held at the open")
    for p in cfg["positions"]:
        legs = ", ".join(f"{'sold' if l['qty'] < 0 else 'bought'} {abs(l['qty'])} "
                         + (f"shares @ {l['price']}" if l["type"] == "stock"
                            else f"{l['expiration']} {l['strike']:g} {l['type']} @ {l['price']}") for l in p["legs"])
        say(f"  - {p['name']}: {legs}")
    say("")

    rows, statuses, all_alerts = [], [], []
    t0 = time.time()
    for minute in range(MINUTES):
        feed.set_minute(minute)
        now = feed.now()
        result = run_pass(cfg, feed, today=day)
        alerts = tracker.alerts(result.hits, result.positions, now)
        spy = feed.spot("SPY")
        if minute == 0:
            spy_open = spy
        for r in result.positions.to_dict("records"):
            statuses.append({"time": now, "position": r["position"], "status": r["status"], "pnl_pct": r.get("pnl_pct")})
        for title, body, urgency in alerts:
            say(f"{now:%H:%M}  ALERT  {title}")
            for ln in body.split("\n"):
                say(f"              {ln}")
            all_alerts.append({"time": now.strftime("%H:%M"), "title": title, "body": body.replace("\n", " | ")})
            if args.notify:
                notify(f"[SIM {now:%H:%M}] {title}", body, urgency)
        for e in result.errors:
            say(f"{now:%H:%M}  !  {e}")
        if minute % 30 == 0 or minute == MINUTES - 1:
            pos = ", ".join(f"{r['position'].split(',')[0]} {r['status']} {r['pnl_pct']:+.0f}%"
                            for r in result.positions.to_dict("records") if pd.notna(r.get("pnl_pct")))
            say(f"{now:%H:%M}  ----   SPY {spy:,.2f} ({100 * (spy / spy_open - 1):+.2f}% on the day) | "
                f"{len(result.hits)} scan hits | {pos}")
        rows.append(result.seconds)
        if args.speed:
            time.sleep(max(args.speed - result.seconds, 0))

    say("")
    say(f"Day summary ({len(all_alerts)} alerts, {time.time() - t0:.0f}s to simulate)")
    if all_alerts:
        a = pd.DataFrame(all_alerts)
        kind = a["title"].str.replace(r"^Scan hit: ", "", regex=True).str.split(":").str[0]
        for k, n in kind.value_counts().items():
            say(f"  {n:3d}  {k}")
    st = pd.DataFrame(statuses)
    for name, g in st.groupby("position", sort=False):
        changes = g[g["status"] != g["status"].shift()]
        path = " -> ".join(f"{r.status} {r.time:%H:%M}" for r in changes.itertuples())
        say(f"  {name}: {path}")
    (OUT / "day_log.txt").write_text("\n".join(log_lines) + "\n")
    pd.DataFrame(all_alerts).to_csv(OUT / "alerts.csv", index=False)
    print(f"\nSaved {OUT / 'day_log.txt'} and alerts.csv")


if __name__ == "__main__":
    main()
