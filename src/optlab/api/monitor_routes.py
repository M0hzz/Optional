"""/api/v1 routes for monitoring (Phase 2): monitor status and settings, a manual pass, the event feed and
acknowledgements, alert rules, position snapshots and timeline, scans.yaml import, and the live stream.
Informational only: nothing here places orders."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..monitoring import legacy
from ..monitoring import settings as msettings
from ..monitoring.calendar import Window
from ..monitoring.events import emit
from ..monitoring.read import ALL_LABELS, position_views
from ..monitoring.rules import CHAIN_DESCR, NEWS_ONLY, TARGET_NAMES, bar_vocabulary, news_names, validate
from ..monitoring.schemas import (AckIn, AlertRuleIn, AlertRuleOut, AlertRuleUpdate, BulkAckIn, EventOut, EventPage,
                                  PositionMonitorIn, ValidateIn, YamlCommitIn)
from ..monitoring.settings import MonitorSettings
from ..monitoring.status import LABELS
from ..portfolio import service as svc
from ..portfolio.models import (AlertRule, AuditEvent, ContractSnapshot, Event, MonitorRun, MonitorState, PositionSnapshot, utcnow)
from ..portfolio.money import fmt, from_units
from ..portfolio.schemas import ArchiveIn
from .deps import Read, Write

router = APIRouter()
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ serialisation
def event_out(e: Event) -> EventOut:
    return EventOut(id=e.id, seq=e.seq, occurred_at=e.occurred_at, type=e.type, severity=e.severity,  # type: ignore[arg-type]
                    title=e.title, message=e.message, symbol=e.symbol, account_id=e.account_id,
                    position_id=e.position_id, contract_id=e.contract_id, alert_rule_id=e.alert_rule_id,
                    snapshot_id=e.snapshot_id, requires_ack=e.requires_ack, acknowledged_at=e.acknowledged_at,
                    ack_note=e.ack_note, notify=e.notify_state not in ("none", "suppressed"),
                    notify_state=e.notify_state, notify_detail=e.notify_detail, source=e.source_json or {})


def rule_out(r: AlertRule, st: MonitorState | None = None) -> AlertRuleOut:
    state = None
    if st is not None:
        state = {"active": st.active, "checked_at": st.checked_at, "last_fired_at": st.last_event_at,
                 "last_cleared_at": st.last_cleared_at, "passes_true": st.candidate_count,
                 "last": (st.data_json or {}).get("last")}
    return AlertRuleOut(id=r.id, name=r.name, target_type=r.target_type, position_id=r.position_id,  # type: ignore[arg-type]
                        contract_id=r.contract_id, symbol=r.symbol, account_id=r.account_id, condition=r.condition,
                        timeframe=r.timeframe, severity=r.severity, enabled=r.enabled, notify=r.notify,  # type: ignore[arg-type]
                        confirm_passes=r.confirm_passes, rearm_minutes=r.rearm_minutes,
                        repeat_minutes=r.repeat_minutes, archived_at=r.archived_at, created_at=r.created_at,
                        updated_at=r.updated_at, version=r.version, state=state)


def _cond_error(text: str, target: str, field: str = "condition") -> None:
    err = validate(text, target)
    if err:
        raise svc.DomainError("BAD_CONDITION", err, 422, field_errors=[{"field": field, "code": "BAD_CONDITION",
                                                                        "message": err}])


def _rules_errors(rs) -> None:
    errs = []
    for k in ("exit_when", "action_when", "watch_when"):
        for i, c in enumerate(getattr(rs, k)):
            e = validate(c, "position")
            if e:
                errs.append({"field": f"{k}[{i}]", "code": "BAD_CONDITION", "message": e})
    if errs:
        raise svc.DomainError("BAD_CONDITION", "A rule isn't valid.", 422, field_errors=errs)


def monitor_note(request: Request, st: MonitorSettings, now: datetime) -> str | None:
    """A short explanation of why positions may not have recent checks."""
    if not st.enabled:
        return "Monitoring is switched off in the monitor settings."
    runner = getattr(request.app.state, "runner", None)
    w = Window(st.schedule)
    why = w.why_closed(now)
    if why:
        nxt = w.next_open(now)
        return f"Outside the monitoring window: {why}" + (f" Next check {nxt:%a %H:%M %Z}." if nxt else "")
    if runner is None or runner.state in ("stopped", "error"):
        held = runner is not None and runner.lock.holder()
        return ("The monitor isn't running in this server (OPTLAB_MONITOR=off)." if runner is None and not held else
                "The monitor isn't running.")
    return None


# ------------------------------------------------------------------ monitor status, settings, manual pass
@router.get("/monitor/status", tags=["monitoring"])
def monitor_status(request: Request, s: Read):
    st = msettings.load(s)
    now = now_utc()
    w = Window(st.schedule)
    last = s.scalar(select(MonitorRun).order_by(MonitorRun.started_at.desc()).limit(1))
    last_ok = s.scalar(select(MonitorRun.started_at).where(MonitorRun.ok.is_(True)).order_by(MonitorRun.started_at.desc()).limit(1))
    runner = getattr(request.app.state, "runner", None)
    unacked = dict(s.execute(select(Event.severity, func.count()).where(Event.requires_ack.is_(True),
                                                                         Event.acknowledged_at.is_(None))
                             .group_by(Event.severity)).all())
    from .deps import quotes_of

    return {
        "enabled": st.enabled, "schedule": w.describe(), "window_open": w.is_open(now), "why_closed": w.why_closed(now),
        "next_window_open": w.next_open(now),
        "runner": runner.status() if runner else {"state": "off", "detail": "This API was started with OPTLAB_MONITOR=off.",
                                                   "in_process": False, "lock_holder_pid": None, "next_pass_at": None,
                                                   "running_since": None},
        "last_run": None if last is None else {
            "id": last.id, "started_at": last.started_at, "finished_at": last.finished_at, "seconds": last.seconds,
            "trigger": last.trigger, "ok": last.ok, "errors": last.errors_json, "positions": last.positions_checked,
            "contracts": last.contracts_checked, "rules": last.rules_checked, "events": last.events_emitted,
            "mode": last.mode, "provider": last.provider},
        "last_successful_run_at": last_ok, "stale_after_seconds": st.stale_after_seconds,
        "unacknowledged": {k: int(unacked.get(k, 0)) for k in ("warning", "critical")},
        "latest_seq": s.scalar(select(func.max(Event.seq))) or 0, "quotes": quotes_of(request).status(),
        "note": monitor_note(request, st, now),
    }


@router.post("/monitor/run", tags=["monitoring"])
def monitor_run(request: Request):
    """Run one pass now (any time of day; quotes outside market hours are the last session's)."""
    from .main import run_manual_pass

    s = run_manual_pass(request.app)
    return {"run_id": s.run_id, "ok": s.ok, "seconds": round(s.seconds, 2), "positions": s.positions,
            "contracts": s.contracts, "rules": s.rules, "events": s.events, "notified": s.notified, "errors": s.errors}


@router.get("/monitor/settings", response_model=MonitorSettings, tags=["monitoring"])
def get_monitor_settings(s: Read):
    return msettings.load(s)


@router.put("/monitor/settings", response_model=MonitorSettings, tags=["monitoring"])
def put_monitor_settings(body: MonitorSettings, s: Write):
    _rules_errors(body.default_rules)
    before = msettings.load(s).model_dump(mode="json")
    out = msettings.save(s, body)
    s.add(AuditEvent(entity_type="monitor_settings", entity_id="monitor_settings", action="edit", before_json=before,
                     after_json=out.model_dump(mode="json")))
    emit(s, "MONITOR_SETTINGS_CHANGED", "info", "Monitor settings changed",
         "Schedule, rules or notification settings were updated. The previous values are in the audit log.")
    return out


@router.get("/monitor/runs", tags=["monitoring"])
def monitor_runs(s: Read, limit: Annotated[int, Query(ge=1, le=500)] = 50):
    rows = s.scalars(select(MonitorRun).order_by(MonitorRun.started_at.desc()).limit(limit))
    return [{"id": r.id, "started_at": r.started_at, "seconds": r.seconds, "trigger": r.trigger, "ok": r.ok,
             "positions": r.positions_checked, "contracts": r.contracts_checked, "rules": r.rules_checked,
             "events": r.events_emitted, "errors": r.errors_json, "provider": r.provider, "mode": r.mode} for r in rows]


@router.get("/monitor/vocabulary", tags=["monitoring"])
def vocabulary():
    """Names each kind of condition can use, with what they mean (for the rule editors)."""
    bn, bf = bar_vocabulary()
    return {"targets": {t: [{"name": k, "meaning": v} for k, v in names.items()] for t, names in TARGET_NAMES.items()},
            "chain": [{"name": k, "meaning": v} for k, v in CHAIN_DESCR.items()],
            "news": [{"name": k, "meaning": v} for k, v in news_names().items()], "news_rule": NEWS_ONLY,
            "bar_names": sorted(bn), "bar_functions": sorted(bf), "statuses": LABELS}


@router.post("/monitor/validate", tags=["monitoring"])
def validate_condition(body: ValidateIn):
    err = validate(body.condition, body.target)
    return {"valid": err is None, "error": err}


# ------------------------------------------------------------------ events
def _event_query(account_id, position_id, contract_id, symbol, type_, severity, unacknowledged, after_seq):
    q = select(Event)
    if account_id:
        q = q.where((Event.account_id == account_id) | Event.account_id.is_(None))
    if position_id:
        q = q.where(Event.position_id == position_id)
    if contract_id:
        q = q.where(Event.contract_id == contract_id)
    if symbol:
        q = q.where(Event.symbol == symbol.upper())
    if type_:
        q = q.where(Event.type.in_(type_))
    if severity:
        q = q.where(Event.severity.in_(severity))
    if unacknowledged:
        q = q.where(Event.requires_ack.is_(True), Event.acknowledged_at.is_(None))
    if after_seq is not None:
        q = q.where(Event.seq > after_seq)
    return q


@router.get("/events", response_model=EventPage, tags=["events"])
def list_events(s: Read, account_id: str | None = None, position_id: str | None = None, contract_id: str | None = None,
                symbol: str | None = None, type: Annotated[list[str] | None, Query()] = None,
                severity: Annotated[list[Literal["info", "warning", "critical"]] | None, Query()] = None,
                unacknowledged: bool = False, after_seq: int | None = None,
                page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=200)] = 50):
    q = _event_query(account_id, position_id, contract_id, symbol, type, severity, unacknowledged, after_seq)
    total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
    items = s.scalars(q.order_by(Event.seq.desc()).offset((page - 1) * page_size).limit(page_size))
    return EventPage(items=[event_out(e) for e in items], total=int(total), page=page, page_size=page_size,
                     latest_seq=s.scalar(select(func.max(Event.seq))) or 0)


def _get_event(s: Session, event_id: str) -> Event:
    e = s.scalar(select(Event).where(Event.id == event_id))
    if e is None:
        raise svc.DomainError("NOT_FOUND", f"Event {event_id} does not exist.", 404)
    return e


@router.get("/events/{event_id}", response_model=EventOut, tags=["events"])
def get_event(event_id: str, s: Read):
    return event_out(_get_event(s, event_id))


@router.post("/events/{event_id}/acknowledge", response_model=EventOut, tags=["events"])
def ack_event(event_id: str, body: AckIn, s: Write):
    """Acknowledging records that you saw it. It changes nothing else; repeating it is harmless."""
    e = _get_event(s, event_id)
    if e.acknowledged_at is None:
        e.acknowledged_at, e.ack_note = utcnow(), body.note
    s.flush()
    return event_out(e)


@router.post("/events/acknowledge", tags=["events"])
def ack_many(body: BulkAckIn, s: Write):
    q = select(Event).where(Event.requires_ack.is_(True), Event.acknowledged_at.is_(None))
    if body.ids is not None:
        q = q.where(Event.id.in_(body.ids))
    else:
        q = q.where(Event.seq <= body.up_to_seq)
    if body.position_id:
        q = q.where(Event.position_id == body.position_id)
    now, n = utcnow(), 0
    for e in s.scalars(q):
        e.acknowledged_at, e.ack_note = now, body.note
        n += 1
    return {"acknowledged": n}


# ------------------------------------------------------------------ alert rules
def _get_rule(s: Session, rid: str) -> AlertRule:
    r = s.get(AlertRule, rid)
    if r is None:
        raise svc.DomainError("NOT_FOUND", f"Alert rule {rid} does not exist.", 404)
    return r


@router.get("/alert-rules", response_model=list[AlertRuleOut], tags=["alerts"])
def list_rules(s: Read, position_id: str | None = None, contract_id: str | None = None, symbol: str | None = None,
               account_id: str | None = None, include_archived: bool = False):
    q = select(AlertRule)
    if position_id:
        q = q.where(AlertRule.position_id == position_id)
    if contract_id:
        q = q.where(AlertRule.contract_id == contract_id)
    if symbol:
        q = q.where(AlertRule.symbol == symbol.upper())
    if account_id:
        q = q.where((AlertRule.account_id == account_id) | AlertRule.account_id.is_(None))
    if not include_archived:
        q = q.where(AlertRule.archived_at.is_(None))
    rules = list(s.scalars(q.order_by(AlertRule.created_at)))
    states = {st.subject_id: st for st in s.scalars(select(MonitorState).where(
        MonitorState.kind == "alert_rule", MonitorState.subject_id.in_([r.id for r in rules])))} if rules else {}
    return [rule_out(r, states.get(r.id)) for r in rules]


@router.post("/alert-rules", response_model=AlertRuleOut, status_code=201, tags=["alerts"])
def create_rule(body: AlertRuleIn, s: Write):
    _cond_error(body.condition, body.target_type)
    account_id = body.account_id
    if body.target_type == "position":
        p = svc.get_position(s, body.position_id or "")
        account_id = p.account_id
    elif body.target_type == "contract":
        account_id = svc.get_contract(s, body.contract_id or "").account_id
    elif account_id:
        svc.get_account(s, account_id)
    r = AlertRule(name=body.name, target_type=body.target_type,
                  position_id=body.position_id if body.target_type == "position" else None,
                  contract_id=body.contract_id if body.target_type == "contract" else None,
                  symbol=body.symbol if body.target_type == "symbol" else None, account_id=account_id,
                  condition=body.condition, timeframe=body.timeframe, severity=body.severity, enabled=body.enabled,
                  notify=body.notify, confirm_passes=body.confirm_passes, rearm_minutes=body.rearm_minutes,
                  repeat_minutes=body.repeat_minutes, metadata_json={"notes": body.notes} if body.notes else {})
    s.add(r)
    s.flush()
    return rule_out(r)


@router.patch("/alert-rules/{rule_id}", response_model=AlertRuleOut, tags=["alerts"])
def update_rule(rule_id: str, body: AlertRuleUpdate, s: Write):
    r = _get_rule(s, rule_id)
    svc._check_version(r, body.version)
    if r.archived_at is not None:
        raise svc.DomainError("RULE_ARCHIVED", "Archived alert rules are read-only.", 409)
    if body.condition is not None:
        _cond_error(body.condition, r.target_type)
    for k in ("name", "condition", "timeframe", "severity", "enabled", "notify", "confirm_passes", "rearm_minutes",
              "repeat_minutes"):
        v = getattr(body, k)
        if v is not None:
            setattr(r, k, v)
    if body.condition is not None or body.enabled is False:          # a changed rule starts disarmed-and-clear
        st = s.get(MonitorState, ("alert_rule", r.id, ""))
        if st is not None:
            s.delete(st)
    s.flush()
    return rule_out(r)


@router.post("/alert-rules/{rule_id}/archive", response_model=AlertRuleOut, tags=["alerts"])
def archive_rule(rule_id: str, body: ArchiveIn, s: Write):
    r = _get_rule(s, rule_id)
    svc._check_version(r, body.version)
    r.archived_at, r.enabled = utcnow(), False
    s.flush()
    return rule_out(r)


# ------------------------------------------------------------------ position monitoring
@router.get("/positions/{position_id}/monitor", tags=["monitoring"])
def position_monitor(position_id: str, request: Request, s: Read):
    from ..monitoring.status import rules_for

    p = svc.get_position(s, position_id)
    st = msettings.load(s)
    now = now_utc()
    view = position_views(s, [p], st, now, monitor_note(request, st, now), include_rules=True)[p.id]
    rules, timeframe, defaults = rules_for(p, st)
    return {**view, "rules_config": {"use_defaults": defaults, "rules": rules.model_dump(), "timeframe": timeframe,
                                     "notify": (p.monitor_json or {}).get("notify", True),
                                     "defaults": st.default_rules.model_dump()},
            "confirm_passes": st.confirm_passes, "version": p.version}


@router.put("/positions/{position_id}/monitor", tags=["monitoring"])
def set_position_monitor(position_id: str, body: PositionMonitorIn, request: Request, s: Write):
    p = svc.get_position(s, position_id)
    svc._check_version(p, body.version)
    if p.lifecycle.value == "ARCHIVED":
        raise svc.DomainError("POSITION_ARCHIVED", "Archived positions are read-only.", 409)
    before = {"monitoring_enabled": p.monitoring_enabled, "monitor": dict(p.monitor_json or {})}
    cfg = dict(p.monitor_json or {})
    if body.rules is not None:
        _rules_errors(body.rules)
        cfg["rules"] = body.rules.model_dump()
    if body.use_defaults:
        cfg["rules"] = None
    if body.timeframe is not None:
        cfg["timeframe"] = body.timeframe
    if body.notify is not None:
        cfg["notify"] = body.notify
    changes = []
    if body.monitoring_enabled is not None and body.monitoring_enabled != p.monitoring_enabled:
        p.monitoring_enabled = body.monitoring_enabled
        changes.append("monitoring switched " + ("on" if body.monitoring_enabled else "off"))
        if body.monitoring_enabled:                      # start fresh: an old status must not carry over
            st = s.get(MonitorState, ("position", p.id, ""))
            if st is not None:
                s.delete(st)
    if cfg != (p.monitor_json or {}):
        changes.append("rules or notification settings changed")
        p.monitor_json = cfg
    if changes:
        p.updated_at = utcnow()
        s.flush()
        s.add(AuditEvent(entity_type="position", entity_id=p.id, action="monitor", before_json=before,
                         after_json={"monitoring_enabled": p.monitoring_enabled, "monitor": cfg}))
        emit(s, "MONITORING_CHANGED", "info", f"Monitoring changed: {p.name}", "; ".join(changes).capitalize() + ".",
             symbol=p.underlying, account_id=p.account_id, position_id=p.id)
    s.flush()
    return position_monitor(position_id, request, s)


@router.get("/positions/{position_id}/snapshots", tags=["monitoring"])
def position_snapshots(position_id: str, s: Read, since: datetime | None = None,
                       limit: Annotated[int, Query(ge=1, le=5000)] = 1000):
    svc.get_position(s, position_id)
    q = select(PositionSnapshot).where(PositionSnapshot.position_id == position_id)
    if since is not None:
        q = q.where(PositionSnapshot.taken_at >= since.astimezone(timezone.utc))
    rows = list(s.scalars(q.order_by(PositionSnapshot.taken_at.desc()).limit(limit)))[::-1]
    return [{"taken_at": r.taken_at, "status": r.status, "raw_status": r.raw_status,
             "open_pnl": fmt(from_units(r.open_pnl_units)), "open_pnl_pct": r.open_pnl_pct,
             "value": fmt(from_units(r.value_units)), "underlying_price": fmt(from_units(r.underlying_price_units)),
             "delta": r.delta, "gamma": r.gamma, "theta": r.theta, "vega": r.vega, "freshness": r.freshness,
             "valuation_status": r.valuation_status} for r in rows]


@router.get("/positions/{position_id}/timeline", tags=["monitoring"])
def position_timeline(position_id: str, s: Read, limit: Annotated[int, Query(ge=1, le=1000)] = 300):
    """Everything that happened to a position, newest first: its events (status changes, alerts, warnings,
    record changes) and the audit log of its edits."""
    svc.get_position(s, position_id)
    events = s.scalars(select(Event).where(Event.position_id == position_id).order_by(Event.seq.desc()).limit(limit))
    audit = svc.audit_trail(s, "position", position_id)
    items = [{"kind": "event", "at": e.occurred_at, "event": event_out(e).model_dump(mode="json")} for e in events]
    items += [{"kind": "audit", "at": a.at, "audit": {"id": a.id, "action": a.action, "reason": a.reason,
                                                      "before": a.before_json, "after": a.after_json}} for a in audit]
    items.sort(key=lambda x: x["at"], reverse=True)  # type: ignore[arg-type, return-value]
    return items[:limit]


@router.get("/watched-contracts/{contract_id}/snapshots", tags=["monitoring"])
def contract_snapshots(contract_id: str, s: Read, limit: Annotated[int, Query(ge=1, le=5000)] = 500):
    svc.get_contract(s, contract_id)
    rows = list(s.scalars(select(ContractSnapshot).where(ContractSnapshot.contract_id == contract_id)
                          .order_by(ContractSnapshot.taken_at.desc()).limit(limit)))[::-1]
    return [{"taken_at": r.taken_at, "quote_status": r.quote_status, "bid": fmt(from_units(r.bid_units)),
             "ask": fmt(from_units(r.ask_units)), "mid": fmt(from_units(r.mid_units)), "iv": r.iv, "delta": r.delta,
             "spread_pct": r.spread_pct, "open_interest": r.open_interest, "freshness": r.freshness,
             "conditions": r.conditions_json} for r in rows]


@router.get("/watched-contracts/{contract_id}/monitor", tags=["monitoring"])
def contract_monitor(contract_id: str, s: Read):
    w = svc.get_contract(s, contract_id)
    q = s.get(MonitorState, ("contract", w.id, "quote"))
    e = s.get(MonitorState, ("contract", w.id, "entry"))
    st = msettings.load(s)
    return {"checked_at": q.checked_at if q else None, "quote_status": q.status if q else None,
            "conditions": (q.data_json or {}).get("conditions", []) if q else [],
            "freshness": (q.data_json or {}).get("freshness") if q else None,
            "reason": (q.data_json or {}).get("reason") if q else None,
            "pending": ({"status": e.candidate, "passes": e.candidate_count, "needed": st.confirm_passes}
                        if e and e.candidate else None),
            "automated": w.status.value in ("WAITING_FOR_CONFIRMATION", "ENTRY_QUALIFIED", "ENTRY_CONDITION_LOST")}


# ------------------------------------------------------------------ scans.yaml import
@router.post("/imports/scans-yaml/preview", tags=["imports"])
def yaml_preview(account_id: str, s: Read):
    """Positions in the project's scans.yaml (a fixed path: no file names are taken from the request)."""
    try:
        rows = legacy.preview(s, account_id)
    except OSError as exc:
        raise svc.DomainError("NO_SCANS_YAML", f"Can't read scans.yaml: {exc.strerror}.", 404) from None
    return {"path": str(legacy.SCANS_YAML.name), "rows": [r.as_dict() for r in rows]}


@router.post("/imports/scans-yaml/commit", tags=["imports"])
def yaml_commit(account_id: str, body: YamlCommitIn, s: Write, idempotency_key: IdemKey = None):
    h = svc.request_hash({"account": account_id, "ack": body.acknowledge_risk})
    prev = svc.idempotent_lookup(s, idempotency_key, "scans_yaml.import", h)
    if prev:
        return prev.response_json
    try:
        rows, created = legacy.commit(s, account_id, body.acknowledge_risk)
    except OSError as exc:
        raise svc.DomainError("NO_SCANS_YAML", f"Can't read scans.yaml: {exc.strerror}.", 404) from None
    if created:
        emit(s, "IMPORT", "info", f"Imported {len(created)} position(s) from scans.yaml",
             ", ".join(p.name for p in created) + ". The YAML monitor now skips them.", account_id=account_id)
    out = {"imported": [{"id": p.id, "name": p.name} for p in created], "rows": [r.as_dict() for r in rows]}
    svc.idempotent_store(s, idempotency_key, "scans_yaml.import", h, 200, json.loads(json.dumps(out, default=str)))
    return out


# ------------------------------------------------------------------ live stream (server-sent events)
def _since(db, last_seq: int, last_run: str | None) -> tuple[list[dict], dict | None]:
    with db.session(write=False) as s:
        evs = [event_out(e).model_dump(mode="json") for e in
               s.scalars(select(Event).where(Event.seq > last_seq).order_by(Event.seq).limit(200))]
        run = s.scalar(select(MonitorRun).where(MonitorRun.finished_at.is_not(None))
                       .order_by(MonitorRun.started_at.desc()).limit(1))
        r = None
        if run is not None and run.id != last_run:
            r = {"id": run.id, "started_at": run.started_at.isoformat(), "ok": run.ok, "positions": run.positions_checked,
                 "events": run.events_emitted, "seconds": run.seconds}
        return evs, r


def _head(db) -> tuple[int, str | None]:
    with db.session(write=False) as s:
        seq = s.scalar(select(func.max(Event.seq))) or 0
        run = s.scalar(select(MonitorRun.id).where(MonitorRun.finished_at.is_not(None))
                       .order_by(MonitorRun.started_at.desc()).limit(1))
        return int(seq), run


@router.get("/stream", tags=["monitoring"], response_class=StreamingResponse)
async def stream(request: Request, after: int | None = None,
                 last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
                 max_seconds: Annotated[float, Query(ge=0.5, le=3600)] = 600):
    """Server-sent events: `event` for each new event (id = its seq; reconnecting with Last-Event-ID resumes
    without gaps), `pass` when a monitoring pass finishes, comments as keep-alives. The stream ends after
    `max_seconds`; browsers reconnect by themselves."""
    db = request.app.state.db
    poll = float(getattr(request.app.state, "stream_poll", 1.5))
    start = after if after is not None else (int(last_event_id) if last_event_id and last_event_id.isdigit() else None)

    async def gen():
        head, last_run = await asyncio.to_thread(_head, db)
        last = head if start is None else start
        yield f"retry: 3000\nevent: hello\ndata: {json.dumps({'latest_seq': head})}\n\n"
        t0 = ping = time.monotonic()
        while time.monotonic() - t0 < max_seconds:
            if await request.is_disconnected():
                break
            evs, run = await asyncio.to_thread(_since, db, last, last_run)
            for e in evs:
                last = e["seq"]
                yield f"id: {e['seq']}\nevent: event\ndata: {json.dumps(e)}\n\n"
            if run is not None:
                last_run = run["id"]
                yield f"event: pass\ndata: {json.dumps(run)}\n\n"
            if time.monotonic() - ping > 15:
                ping = time.monotonic()
                yield ": keep-alive\n\n"
            await asyncio.sleep(poll)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


__all__ = ["router", "ALL_LABELS"]
