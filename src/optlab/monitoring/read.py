"""Read-side views of monitoring state for the API.

A position's effective status is the monitor's confirmed status, except:
    * not open                      -> CLOSED
    * monitoring switched off       -> NOT_MONITORED (shown as such, never as HOLD)
    * never checked                 -> STALE_DATA ("no pass has checked it yet")
    * last check older than `stale_after_seconds` (the monitor stopped, it is outside the monitoring
      window, or passes are failing) -> STALE_DATA, with the last confirmed status kept alongside
So an old HOLD is never presented as current.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..portfolio.models import MonitorState, Position, PositionSnapshot
from ..portfolio.money import fmt, from_units
from .settings import MonitorSettings
from .status import CLOSED, LABELS, STALE

NOT_MONITORED = "NOT_MONITORED"
ALL_LABELS = {**LABELS, NOT_MONITORED: "NOT MONITORED"}
ATTENTION = {"STALE_DATA", "EXIT_CONDITION_MET", "ACTION_REQUIRED", "WATCH"}


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{int(seconds // 86400)} days"


def snapshot_summary(sn: PositionSnapshot | None) -> dict | None:
    if sn is None:
        return None
    return {"id": sn.id, "taken_at": sn.taken_at, "raw_status": sn.raw_status, "status": sn.status,
            "valuation_status": sn.valuation_status, "value": fmt(from_units(sn.value_units)),
            "exit_value": fmt(from_units(sn.exit_value_units)), "open_pnl": fmt(from_units(sn.open_pnl_units)),
            "open_pnl_pct": sn.open_pnl_pct, "underlying_price": fmt(from_units(sn.underlying_price_units)),
            "greeks": {"delta": sn.delta, "gamma": sn.gamma, "theta": sn.theta, "vega": sn.vega},
            "freshness": sn.freshness, "provider": sn.provider, "delay_minutes": sn.delay_minutes,
            "fetched_at": sn.fetched_at, "quote_age_seconds": sn.quote_age_seconds, "label": "ESTIMATED"}


def position_views(s: Session, positions: list[Position], settings: MonitorSettings, now: datetime,
                   monitor_note: str | None = None, include_rules: bool = False) -> dict[str, dict]:
    ids = [p.id for p in positions]
    states = {st.subject_id: st for st in s.scalars(select(MonitorState).where(
        MonitorState.kind == "position", MonitorState.key == "", MonitorState.subject_id.in_(ids)))} if ids else {}
    snap_ids = [st.snapshot_id for st in states.values() if st.snapshot_id]
    snaps = {sn.id: sn for sn in s.scalars(select(PositionSnapshot).where(PositionSnapshot.id.in_(snap_ids)))} if snap_ids else {}
    out = {}
    for p in positions:
        st = states.get(p.id)
        sn = snaps.get(st.snapshot_id) if st and st.snapshot_id else None
        data = (st.data_json or {}) if st else {}
        v: dict = {"enabled": p.monitoring_enabled, "stored_status": st.status if st else None,
                   "since": st.since if st else None, "checked_at": st.checked_at if st else None,
                   "age_seconds": None, "pending": None, "raw_status": data.get("raw_status"),
                   "raw_reasons": data.get("reasons") or [], "stale_overlay": False,
                   "snapshot": snapshot_summary(sn)}
        if p.lifecycle.value != "OPEN":
            v.update(status=CLOSED, reasons=[f"The position is {p.lifecycle.value.lower()}."])
        elif not p.monitoring_enabled:
            v.update(status=NOT_MONITORED, reasons=["Monitoring is off for this position: nothing checks it."])
        elif st is None or st.status is None or st.checked_at is None:
            v.update(status=STALE, reasons=["No monitoring pass has checked this position yet."
                                            + (f" {monitor_note}" if monitor_note else "")])
        else:
            age = (now - st.checked_at).total_seconds()
            v["age_seconds"] = round(age, 1)
            reasons = data.get("status_reasons") or data.get("reasons") or []
            if age > settings.stale_after_seconds:
                v.update(status=STALE, stale_overlay=True,
                         reasons=[f"Last checked {_ago(age)} ago, so the data is old."
                                  + (f" {monitor_note}" if monitor_note else "")
                                  + f" The last status was {ALL_LABELS.get(st.status, st.status)}: " + "; ".join(reasons)])
            else:
                v.update(status=st.status, reasons=reasons)
            if st.candidate and st.candidate != st.status:
                v["pending"] = {"status": st.candidate, "label": ALL_LABELS.get(st.candidate, st.candidate),
                                "passes": st.candidate_count, "needed": settings.confirm_passes}
        v["label"] = ALL_LABELS.get(v["status"], v["status"])
        if include_rules and sn is not None:
            v["rules"] = sn.rules_json
            v["metrics"] = sn.metrics_json
            v["legs"] = sn.legs_json
        out[p.id] = v
    return out
