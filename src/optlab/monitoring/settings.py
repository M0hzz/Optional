"""Monitor settings: schedule, confirmation, re-arm and repeat rules, default position rules, notifications.

Stored in the portfolio database (user_preferences, key `monitor_settings`) and edited from the dashboard.
Until they are first saved, the defaults come from scans.yaml (its `schedule`, `alerts` and
`position_defaults` sections) so the portfolio monitor starts out behaving like the existing monitor.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from ..portfolio.models import UserPreference

KEY = "monitor_settings"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# `entry` is + for a debit paid and - for a credit received, so each rule applies to the right kind of trade
# whatever its size: a bought option can't lose more than 100% of its cost, so a -200% stop never fires on one.
DEFAULT_EXIT = ["pnl_pct >= 50", "loss_pct_of_max >= 50", "entry < 0 and pnl_pct <= -200", "entry < 0 and dte <= 21"]
DEFAULT_WATCH = ["short_strike_otm_pct < 3", "earnings_before_expiry", "entry > 0 and dte <= 7"]
Severity = Literal["info", "warning", "critical"]
Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "1d", "1wk"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _hhmm(v: str) -> str:
    v = str(v).strip()
    if not HHMM.match(v):
        raise ValueError("use 24-hour HH:MM, e.g. 09:30")
    return v


class ScheduleSettings(_M):
    start: str = "09:30"
    end: str = "16:00"
    timezone: str = "America/New_York"
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = ["mon", "tue", "wed", "thu", "fri"]
    every_seconds: Annotated[int, Field(ge=15, le=3600)] = 60
    skip_market_holidays: bool = True

    @field_validator("start", "end")
    @classmethod
    def _times(cls, v: str) -> str:
        return _hhmm(v)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {v!r}") from None
        return v


class RuleSet(_M):
    exit_when: Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(max_length=20)] = []
    action_when: Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(max_length=20)] = []
    watch_when: Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(max_length=20)] = []


class NotificationSettings(_M):
    desktop: bool = True                     # notify-send on the machine running the API
    min_severity: Severity = "warning"
    quiet_hours: bool = False
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    critical_during_quiet_hours: bool = True
    muted_symbols: Annotated[list[Annotated[str, Field(max_length=16)]], Field(max_length=200)] = []

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def _times(cls, v: str) -> str:
        return _hhmm(v)

    @field_validator("muted_symbols")
    @classmethod
    def _syms(cls, v: list[str]) -> list[str]:
        return sorted({s.strip().upper() for s in v if s.strip()})


class MonitorSettings(_M):
    enabled: bool = True
    schedule: ScheduleSettings = ScheduleSettings()
    confirm_passes: Annotated[int, Field(ge=1, le=20)] = 3
    repeat_urgent_minutes: Annotated[int, Field(ge=0, le=1440)] = 15
    rearm_minutes: Annotated[int, Field(ge=0, le=1440)] = 30
    stale_after_seconds: Annotated[int, Field(ge=60, le=86400)] = 300
    default_rules: RuleSet = RuleSet(exit_when=DEFAULT_EXIT, watch_when=DEFAULT_WATCH)
    timeframe: Timeframe = "1d"
    completed_bars_only: bool = True
    assignment_risk_days: Annotated[int, Field(ge=0, le=60)] = 5
    expiry_action: bool = True
    expiration_warning_days: Annotated[list[Annotated[int, Field(ge=0, le=365)]], Field(max_length=6)] = [7, 1]
    liquidity_max_spread_pct: Annotated[float, Field(gt=0, le=500)] = 25.0
    retention_full_days: Annotated[int, Field(ge=1, le=3650)] = 30
    notifications: NotificationSettings = NotificationSettings()


_YAML_CACHE: dict[str, tuple[tuple, MonitorSettings]] = {}


def from_scans_yaml(path=None) -> MonitorSettings:
    """Defaults seeded from scans.yaml, falling back to the built-in ones for anything missing or invalid.
    Cached by the file's modification time and size, so an edit is picked up at once."""
    import os

    from ..config import PROJECT_ROOT

    p = str(path or PROJECT_ROOT / "scans.yaml")
    try:
        st = os.stat(p)
        stamp: tuple = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = ("missing",)
    hit = _YAML_CACHE.get(p)
    if hit and hit[0] == stamp:
        return hit[1].model_copy(deep=True)
    out = _from_scans_yaml(p)
    _YAML_CACHE[p] = (stamp, out)
    return out.model_copy(deep=True)


def _from_scans_yaml(path) -> MonitorSettings:
    import yaml

    base = MonitorSettings()
    try:
        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return base
    out = base.model_dump()
    sch = cfg.get("schedule") or {}
    for k in ("start", "end"):
        v = sch.get(k)
        if isinstance(v, int):                       # YAML reads unquoted 09:30 as minutes
            v = f"{v // 60:02d}:{v % 60:02d}"
        if v is not None:
            out["schedule"][k] = str(v)
    if sch.get("timezone"):
        out["schedule"]["timezone"] = sch["timezone"]
    if sch.get("days"):
        out["schedule"]["days"] = [str(d).lower()[:3] for d in sch["days"]]
    if sch.get("every_seconds"):
        out["schedule"]["every_seconds"] = max(int(sch["every_seconds"]), 15)
    al = cfg.get("alerts") or {}
    for src, dst in (("confirm_passes", "confirm_passes"), ("rearm_minutes", "rearm_minutes"),
                     ("repeat_exit_minutes", "repeat_urgent_minutes")):
        if al.get(src) is not None:
            out[dst] = int(al[src])
    if "desktop" in al:
        out["notifications"]["desktop"] = bool(al["desktop"])
    pd_ = cfg.get("position_defaults") or {}
    if pd_.get("exit_when") is not None:
        out["default_rules"]["exit_when"] = [str(x) for x in pd_["exit_when"]]
    if pd_.get("watch_when") is not None:
        out["default_rules"]["watch_when"] = [str(x) for x in pd_["watch_when"]]
    try:
        return MonitorSettings.model_validate(out)
    except ValueError:
        return base


def load(s: Session) -> MonitorSettings:
    row = s.get(UserPreference, KEY)
    if row is None:
        return from_scans_yaml()
    try:
        return MonitorSettings.model_validate(row.value_json.get("value") or {})
    except ValueError:
        return from_scans_yaml()


def save(s: Session, settings: MonitorSettings) -> MonitorSettings:
    row = s.get(UserPreference, KEY) or UserPreference(key=KEY, value_json={})
    row.value_json = {"value": settings.model_dump(mode="json")}
    s.add(row)
    s.flush()
    return settings
