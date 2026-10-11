"""Scheduling, alerts and result files for the live monitor (scripts/monitor.py).

Schedule   runs between `start` and `end` on the listed weekdays, in `timezone`,
           every `every_seconds` seconds aligned to the clock (09:31:00, 09:32:00, ...).
Alerts     a desktop notification when a symbol newly passes a scan, or a position's
           status changes (HOLD -> WATCH -> EXIT). EXIT repeats every
           `repeat_exit_minutes` while it stays true. Nothing is re-sent every minute.
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
    """Remembers what was already alerted (survives restarts within the same day)."""

    def __init__(self, state_path: Path, repeat_exit_minutes: float = 15):
        self.path, self.repeat = state_path, repeat_exit_minutes
        self.state = {"day": str(date.today()), "hits": [], "positions": {}, "exit_alerted": {}}
        if state_path.exists():
            try:
                saved = json.loads(state_path.read_text())
                if saved.get("day") == str(date.today()):
                    self.state.update(saved)
            except Exception:
                pass

    def alerts(self, hits: pd.DataFrame, positions: pd.DataFrame, now: datetime) -> list[tuple[str, str, str]]:
        out = []
        current = {}
        for (scan, sym), g in hits.groupby(["scan", "symbol"], sort=False):
            current[f"{scan}|{sym}"] = g
        seen = set(self.state["hits"])
        for key in [k for k in current if k not in seen]:
            scan, sym = key.split("|", 1)
            g = current[key]
            body = f"{sym} @ {g['price'].iloc[0]:,.2f}"
            if g["contract"].notna().any():
                r = g.iloc[0]
                body += (f"\n{len(g)} contract(s), best: {r['expiration']} {r['strike']:g} {r['type']} "
                         f"bid {r['bid']:.2f} Δ {r['delta']:+.2f} OI {int(r['open_interest'])}")
            elif g["details"].iloc[0]:
                body += f"\n{g['details'].iloc[0]}"
            out.append((f"Scan hit: {scan}", body, "normal"))
        self.state["hits"] = sorted(current)

        for r in positions.to_dict("records"):
            name, status = r["position"], r["status"]
            before = self.state["positions"].get(name)
            last_exit = self.state["exit_alerted"].get(name)
            repeat_due = (status == "EXIT" and last_exit and self.repeat
                          and now.timestamp() - last_exit >= self.repeat * 60)
            changed = status != before and not (before is None and status == "HOLD")
            if changed or repeat_due:
                pnl = f" P&L {r['pnl']:+,.0f} ({r['pnl_pct']:+.0f}%)" if pd.notna(r.get("pnl")) else ""
                title = f"{status}: {name}" if before is None or repeat_due else f"{before} → {status}: {name}"
                out.append((title, (r["reasons"] or "all conditions OK") + pnl,
                            "critical" if status in ("EXIT", "ERROR") else "normal"))
                if status == "EXIT":
                    self.state["exit_alerted"][name] = now.timestamp()
            self.state["positions"][name] = status
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
