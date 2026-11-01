"""What the monitoring pass does for plans and risk limits (called from monitoring.engine).

Plans (ARMED, WAITING, QUALIFIED), on the symbol's chain snapshot and bars:
    invalidation   any invalidation condition holds, or the price crosses the invalidation price against the
                   plan's direction                                    -> INVALIDATED
    trigger        every trigger condition holds, and the price is inside the entry zone when one is set
                   (no trigger conditions and no zone: the monitor never moves the plan; you do)
    confirmation   every confirmation condition holds                   -> QUALIFIED
                   trigger holds but no confirmation conditions        -> WAITING (you confirm)
                   trigger stops holding                               -> back to ARMED
    A move needs `confirm_passes` passes in a row; unknown results (missing data) move nothing.
    Any plan (draft included) whose expiry time has passed              -> EXPIRED
Risk limits: each account's limits are checked every pass; a limit reaching WARNING or BLOCKED raises one
event, again only after it has cleared (the same re-arm rule as alerts).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..monitoring.events import emit
from ..monitoring.rules import Context, explain, names_used, split_conditions
from ..monitoring.transitions import step_alert
from ..portfolio.models import Account, MonitorState, TradePlan
from ..portfolio.money import from_units
from . import policy as pol
from . import riskcheck as rc
from .plans import LABEL, LIVE, TERMINAL, set_state


def _state(s: Session, kind: str, subject: str, key: str = "") -> MonitorState:
    st = s.get(MonitorState, (kind, subject, key))
    if st is None:
        st = MonitorState(kind=kind, subject_id=subject, key=key, candidate_count=0, active=False, data_json={})
        s.add(st)
    return st


def plan_expressions(p: TradePlan) -> tuple[list[str], list[str], list[str]]:
    trig = split_conditions(p.trigger_conditions)
    lo, hi = from_units(p.entry_zone_low_units), from_units(p.entry_zone_high_units)
    if lo is not None:
        trig.append(f"price >= {lo.normalize()}")
    if hi is not None:
        trig.append(f"price <= {hi.normalize()}")
    conf = split_conditions(p.confirmation_conditions)
    inv = split_conditions(p.invalidation_conditions)
    ip = from_units(p.invalidation_price_units)
    if ip is not None and p.direction in ("bullish", "bearish"):
        inv.append(f"price {'<=' if p.direction == 'bullish' else '>='} {ip.normalize()}")
    return trig, conf, inv


def needs_bars(p: TradePlan, vocab: set[str]) -> bool:
    for e in sum(plan_expressions(p), []):
        try:
            if names_used(e) & vocab:
                return True
        except Exception:
            continue
    return False


def evaluate_plans(s: Session, plans: list[TradePlan], contexts: dict[str, Context], st, now: datetime,
                   run_id: str) -> list[int]:
    out: list[int] = []
    for p0 in plans:
        p = s.get(TradePlan, p0.id)
        if p is None or p.state in TERMINAL:
            continue
        if p.expires_at is not None and p.expires_at <= now:
            set_state(s, p, "EXPIRED", None, "monitor", st, run_id, f"Its expiry time ({p.expires_at:%Y-%m-%d %H:%M} UTC) passed.")
            continue
        if p.state not in LIVE:
            continue
        ctx = contexts.get(p.symbol)
        ms = _state(s, "plan", p.id)
        ms.checked_at = now
        if ctx is None:
            ms.data_json = {"note": "no data for the symbol this pass"}
            continue
        trig, conf, inv = plan_expressions(p)
        rt = [ctx.evaluate(x, "trigger") for x in trig]
        rcf = [ctx.evaluate(x, "confirmation") for x in conf]
        ri = [ctx.evaluate(x, "invalidation") for x in inv]
        ms.data_json = {"trigger": [r.as_dict() for r in rt], "confirmation": [r.as_dict() for r in rcf],
                        "invalidation": [r.as_dict() for r in ri]}
        target: str | None = None
        detail = ""
        if any(r.result for r in ri):
            target = "INVALIDATED"
            detail = "Invalidation: " + "; ".join(explain(r) for r in ri if r.result) + "."
        elif rt and not any(r.result is None for r in rt):
            if all(r.result for r in rt):
                if rcf and any(r.result is None for r in rcf):
                    target = None
                elif rcf and all(r.result for r in rcf):
                    target = "QUALIFIED"
                    detail = "Trigger: " + "; ".join(explain(r) for r in rt) + ". Confirmation: " + "; ".join(explain(r) for r in rcf) + "."
                else:
                    target = "WAITING"
                    detail = "Trigger: " + "; ".join(explain(r) for r in rt) + "." + (
                        " Waiting for: " + "; ".join(explain(r) for r in rcf if r.result is False) + "." if rcf else
                        " No confirmation conditions: confirm it yourself.")
            else:
                target = "ARMED"
                detail = "Trigger no longer holds: " + "; ".join(explain(r) for r in rt if r.result is False) + "."
        if target is None or target == p.state:
            ms.candidate, ms.candidate_count = None, 0
            continue
        ms.candidate_count = (ms.candidate_count or 0) + 1 if ms.candidate == target else 1
        ms.candidate = target
        if ms.candidate_count >= st.confirm_passes:
            ms.candidate, ms.candidate_count, ms.last_event_at = None, 0, now
            set_state(s, p, target, None, "monitor", st, run_id, detail)
    from ..portfolio.models import Event

    s.flush()
    out += [e.seq for e in s.scalars(select(Event).where(Event.run_id == run_id, Event.type == "PLAN_STATE_CHANGED"))]
    return out


def risk_limit_events(s: Session, st, now: datetime, run_id: str, tz: str) -> list[int]:
    out: list[int] = []
    for acc in s.scalars(select(Account).where(Account.archived_at.is_(None))):
        settings, row = pol.load(s, acc.id)
        port = rc.portfolio_state(s, acc.id, now, tz)
        if not port.positions and port.realized_week == 0:
            continue
        rep = rc.utilization(pol.effective(settings, port.account_value), port, row, now)
        for c in rep.checks:
            ms = _state(s, "risk", acc.id, c.code)
            bad = None if c.result == rc.UNKNOWN else c.result in (rc.WARNING, rc.BLOCKED)
            was = ms.status
            act = step_alert(ms, bad, now, 1, st.rearm_minutes, 0)
            ms.checked_at = now
            escalated = bad and was == rc.WARNING and c.result == rc.BLOCKED and ms.active
            if bad is not None:
                ms.status = c.result
            if act == "fire" or escalated:
                ms.last_event_at = now
                reached = c.result == rc.BLOCKED
                ev = emit(s, "RISK_LIMIT", "critical" if reached else "warning",
                          f"{'Limit reached' if reached else 'Close to a limit'}: {c.label}",
                          c.reason + (" No new risk until it clears." if reached else ""), settings=st,
                          account_id=acc.id, run_id=run_id,
                          source={"check": c.__dict__, "policy_revision": rep.policy_revision},
                          dedupe_key=f"risk:{acc.id}:{c.code}:{c.result}", now=now)
                if ev:
                    out.append(ev.seq)
    return out


__all__ = ["evaluate_plans", "risk_limit_events", "plan_expressions", "needs_bars", "LABEL"]
