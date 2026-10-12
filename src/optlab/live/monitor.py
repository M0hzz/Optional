"""Scheduling, alerts and result files for the live monitor (scripts/monitor.py).

Schedule   runs between `start` and `end` on the listed weekdays, in `timezone`,
           every `every_seconds` seconds aligned to the clock (09:31:00, 09:32:00, ...).
Alerts     a desktop notification when a symbol newly passes a scan, or a position's
           status changes (HOLD -> WATCH -> EXIT); see AlertTracker for the guards that
           stop a value sitting on a threshold from alerting every minute.
Files      data/monitor/latest_hits.parquet, latest_positions.parquet and status.json
           (read by the dashboard's Monitor tab) and alerts.csv (a log of every alert).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _hhmm(s) -> dtime:
    if isinstance(s, int):  # YAML reads an unquoted 09:30 as minutes (570)
        return dtime(s // 60, s % 60)
    h, m = str(s).strip().split(":")
    return dtime(int(h), int(m))


@dataclass
class Schedule:
    start: dtime
    end: dtime
    tz: ZoneInfo
    days: frozenset
    every: int

    @classmethod
    def from_config(cls, d: dict | None, start: str | None = None, end: str | None = None) -> "Schedule":
        d = d or {}
        days = d.get("days", ["mon", "tue", "wed", "thu", "fri"])
        return cls(_hhmm(start or d.get("start", "09:30")), _hhmm(end or d.get("end", "16:00")),
                   ZoneInfo(d.get("timezone", "America/New_York")),
                   frozenset(DAYS[str(x).lower()[:3]] for x in days), max(int(d.get("every_seconds", 60)), 5))

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def is_open(self, now: datetime) -> bool:
        return now.weekday() in self.days and self.start <= now.time() < self.end

    def next_start(self, now: datetime) -> datetime:
        """`now` if inside the window, else the next scheduled start."""
        if self.is_open(now):
            return now
        for i in range(8):
            d = (now + timedelta(days=i)).date()
            start = datetime.combine(d, self.start, self.tz)
            if d.weekday() in self.days and start > now:
                return start
        raise ValueError("schedule has no days")

    def next_tick(self, now: datetime) -> datetime:
        """Next clock-aligned run time, e.g. the top of the next minute."""
        epoch = now.timestamp()
        return datetime.fromtimestamp(epoch - epoch % self.every + self.every, self.tz)

    def describe(self) -> str:
        names = [k for k, v in sorted(DAYS.items(), key=lambda kv: kv[1]) if v in self.days]
        return (f"{self.start:%H:%M}-{self.end:%H:%M} {self.tz.key}, {'/'.join(names)}, "
                f"every {self.every}s")


# ------------------------------------------------------------------ alerts
def notify(title: str, body: str, urgency: str = "normal") -> bool:
    """Desktop notification via notify-send (Ubuntu: libnotify-bin). Returns False if unavailable."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    try:
        subprocess.run([exe, "-a", "optlab", "-u", urgency, title, body], timeout=5, check=False)
        return True
    except Exception:
        return False


class AlertTracker:
    """Decides which changes deserve a notification; survives restarts within the same day.

    Two guards keep a symbol or position sitting on a threshold from alerting every minute:
    - a scan hit alerts when it first appears, and again only after it has been out of
      that scan for `rearm_minutes`;
    - a position's new status must hold for `confirm_passes` passes in a row before it is
      alerted (its first status of the day is reported at once, unless it is HOLD).
    An EXIT repeats every `repeat_exit_minutes` while it stays the alerted status.
    """

    def __init__(self, state_path: Path, repeat_exit_minutes: float = 15, rearm_minutes: float = 30,
                 confirm_passes: int = 3):
        self.path, self.repeat = state_path, repeat_exit_minutes
        self.rearm, self.confirm = rearm_minutes, max(int(confirm_passes), 1)
        self.state = {"day": str(date.today()), "hits_seen": {}, "pos": {}}
        if state_path.exists():
            try:
                saved = json.loads(state_path.read_text())
                if saved.get("day") == str(date.today()) and "pos" in saved:
                    self.state.update(saved)
            except Exception:
                pass

    def alerts(self, hits: pd.DataFrame, positions: pd.DataFrame, now: datetime) -> list[tuple[str, str, str]]:
        out, ts = [], now.timestamp()
        seen = self.state["hits_seen"]
        for (scan, sym), g in hits.groupby(["scan", "symbol"], sort=False):
            key = f"{scan}|{sym}"
            last = seen.get(key)
            seen[key] = ts
            if last is not None and ts - last <= self.rearm * 60 + 1:
                continue  # still in the scan, or back after only a short gap
            body = f"{sym} @ {g['price'].iloc[0]:,.2f}"
            if g["contract"].notna().any():
                r = g.iloc[0]
                body += (f"\n{len(g)} contract(s), best: {r['expiration']} {r['strike']:g} {r['type']} "
                         f"bid {r['bid']:.2f} Δ {r['delta']:+.2f} OI {int(r['open_interest'])}")
            elif g["details"].iloc[0]:
                body += f"\n{g['details'].iloc[0]}"
            out.append((f"Scan hit: {scan}", body, "normal"))

        for r in positions.to_dict("records"):
            name, status = r["position"], r["status"]
            p = self.state["pos"].setdefault(name, {"alerted": None, "cand": None, "count": 0, "exit_at": None})
            p["count"] = p["count"] + 1 if status == p["cand"] else 1
            p["cand"] = status
            title = None
            if p["alerted"] is None:
                if status != "HOLD":
                    title = f"{status}: {name}"
                p["alerted"] = status
            elif status != p["alerted"] and p["count"] >= self.confirm:
                title = f"{p['alerted']} → {status}: {name}"
                p["alerted"] = status
            elif (status == p["alerted"] == "EXIT" and self.repeat and p["exit_at"]
                  and ts - p["exit_at"] >= self.repeat * 60):
                title = f"EXIT (still): {name}"
            if title:
                pnl = f" P&L {r['pnl']:+,.0f} ({r['pnl_pct']:+.0f}%)" if pd.notna(r.get("pnl")) else ""
                out.append((title, (r["reasons"] or "all conditions OK") + pnl,
                            "critical" if status in ("EXIT", "ERROR") else "normal"))
                if status == "EXIT":
                    p["exit_at"] = ts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state))
        return out


# ------------------------------------------------------------------- files
def write_results(out_dir: Path, result, schedule: Schedule | None, alerts: list, now: datetime) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in (("latest_hits", result.hits), ("latest_positions", result.positions)):
        tmp = out_dir / f".{name}.tmp.parquet"
        df.astype({c: str for c in ("expiration",) if c in df}).to_parquet(tmp, index=False)
        os.replace(tmp, out_dir / f"{name}.parquet")  # atomic: the dashboard never reads half a file
    status = {"last_run": now.isoformat(timespec="seconds"), "seconds": round(result.seconds, 2),
              "symbols": result.symbols, "hits": int(len(result.hits)), "errors": result.errors[:50],
              "schedule": schedule.describe() if schedule else "single run"}
    (out_dir / "status.json").write_text(json.dumps(status, indent=1))
    if alerts:
        log = out_dir / "alerts.csv"
        rows = pd.DataFrame([{"time": now.isoformat(timespec="seconds"), "title": t, "body": b.replace("\n", " | ")}
                             for t, b, _ in alerts])
        rows.to_csv(log, mode="a", header=not log.exists(), index=False)
