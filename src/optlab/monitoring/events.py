"""The event feed (command-center events) and notifications.

`emit` writes an event in the caller's transaction. Severity is info, warning or critical; warning and
critical events need acknowledging. Whether an event is notified is decided when it is written:

    none          info events, and anything below `notifications.min_severity`
    suppressed    quiet hours (critical still goes through if `critical_during_quiet_hours`), a muted
                  symbol, or a position / alert with notifications off; `notify_detail` says which
    pending       to be delivered: the browser dashboards show it at once (live stream), and `deliver`
                  sends it to the desktop after the transaction commits
    sent | failed | unavailable | browser   the desktop result (browser = desktop notifications are off)

Each desktop attempt is recorded in notification_deliveries. A repeated event with the same `dedupe_key`
within `dedupe_seconds` is dropped (a second monitor pass racing the first can't double-alert).
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..portfolio.models import Event, NotificationDelivery, utcnow
from .settings import MonitorSettings

log = logging.getLogger("optlab.monitor")
SEVERITIES = ("info", "warning", "critical")


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet_hours(settings: MonitorSettings, now: datetime) -> bool:
    n = settings.notifications
    if not n.quiet_hours:
        return False
    local = now.astimezone(ZoneInfo(settings.schedule.timezone)).time()
    a, b = _t(n.quiet_start), _t(n.quiet_end)
    return (a <= local < b) if a <= b else (local >= a or local < b)


def notify_policy(settings: MonitorSettings, severity: str, symbol: str | None, now: datetime,
                  muted: str | None = None) -> tuple[str, str | None]:
    n = settings.notifications
    if SEVERITIES.index(severity) < SEVERITIES.index(n.min_severity) or severity == "info":
        return "none", None
    if muted:
        return "suppressed", muted
    if symbol and symbol.upper() in n.muted_symbols:
        return "suppressed", f"{symbol} is muted in notification settings"
    if in_quiet_hours(settings, now) and not (severity == "critical" and n.critical_during_quiet_hours):
        return "suppressed", f"quiet hours ({n.quiet_start}–{n.quiet_end})"
    return "pending", None


def emit(s: Session, type_: str, severity: str, title: str, message: str, *, settings: MonitorSettings | None = None,
         symbol: str | None = None, account_id: str | None = None, position_id: str | None = None,
         contract_id: str | None = None, alert_rule_id: str | None = None, snapshot_id: str | None = None,
         run_id: str | None = None, source: dict[str, Any] | None = None, dedupe_key: str | None = None,
         dedupe_seconds: int = 50, muted: str | None = None, now: datetime | None = None) -> Event | None:
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    now = now or utcnow()
    if dedupe_key:
        dup = s.scalar(select(Event.seq).where(Event.dedupe_key == dedupe_key,
                                               Event.occurred_at >= now - timedelta(seconds=dedupe_seconds)).limit(1))
        if dup is not None:
            return None
    state, detail = notify_policy(settings, severity, symbol, now, muted) if settings else ("none", None)
    ev = Event(type=type_, severity=severity, title=title[:300], message=message, symbol=symbol,
               account_id=account_id, position_id=position_id, contract_id=contract_id, alert_rule_id=alert_rule_id,
               snapshot_id=snapshot_id, run_id=run_id, dedupe_key=dedupe_key, source_json=source or {},
               requires_ack=severity in ("warning", "critical"), notify_state=state, notify_detail=detail,
               occurred_at=now)
    s.add(ev)
    s.flush()
    return ev


Notifier = Callable[[str, str, str], bool]


def desktop_notifier(title: str, body: str, urgency: str) -> bool:
    from ..live.monitor import notify

    return notify(title, body, urgency)


def deliver(db, seqs: list[int], settings: MonitorSettings, notifier: Notifier | None = desktop_notifier) -> int:
    """Desktop delivery for pending events (after their transaction committed). Returns how many were sent."""
    if not seqs:
        return 0
    with db.session() as s:
        pending = list(s.scalars(select(Event).where(Event.seq.in_(seqs), Event.notify_state == "pending")
                                 .order_by(Event.seq)))
        work = [(e.seq, e.title, e.message, e.severity) for e in pending]
    sent = 0
    results = []
    for seq, title, body, sev in work:
        if not settings.notifications.desktop or notifier is None:
            results.append((seq, "browser", None))
            continue
        try:
            ok = notifier(title, body[:400], "critical" if sev == "critical" else "normal")
        except Exception as exc:                       # never let a notification break a pass
            log.warning("desktop notification failed: %s", exc)
            ok, err = False, f"{type(exc).__name__}"
        else:
            err = None if ok else "notify-send is not available on this machine"
        results.append((seq, "sent" if ok else ("failed" if err and not err.startswith("notify-send") else "unavailable"), err))
        sent += bool(ok)
    with db.session() as s:
        for seq, state, err in results:
            e = s.scalar(select(Event).where(Event.seq == seq))
            if e is None:
                continue
            e.notify_state = state
            e.notify_detail = err
            if state != "browser":
                s.add(NotificationDelivery(event_seq=seq, channel="desktop", status=state, detail=err))
    return sent
