"""Journal (archives), mission reports (post-trade reviews) and daily objective check-offs."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..monitoring.events import emit
from ..portfolio import service as svc
from ..portfolio.models import AuditEvent, DailyObjective, JournalEntry, Lifecycle, TradeReview, utcnow
from ..portfolio.schemas import Note, Strict
from .engine import MANUAL, REST

Kind = Literal["daily_thesis", "eod_review", "decision", "note", "lesson"]
YesNo = Literal["yes", "partly", "no", "no_plan"]
ExitReason = Literal["target", "stop", "invalidation", "time_stop", "risk_rule", "discretionary", "expired", "assigned", "other"]
Mistake = Literal["no_plan", "chased_entry", "oversized", "ignored_stop", "moved_stop", "early_exit", "late_exit",
                  "averaging_down", "revenge_trade", "fomo", "ignored_liquidity", "ignored_earnings", "other"]
Body = Annotated[str, Field(min_length=1, max_length=20_000)]


class JournalIn(Strict):
    account_id: str
    kind: Kind = "note"
    entry_date: date | None = None
    title: Annotated[str | None, Field(max_length=200)] = None
    body: Body
    tags: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=20)] = []
    position_id: str | None = None
    plan_id: str | None = None


class JournalUpdate(Strict):
    version: StrictInt
    title: Annotated[str | None, Field(max_length=200)] = None
    body: Body | None = None
    tags: Annotated[list[Annotated[str, Field(max_length=40)]] | None, Field(max_length=20)] = None


class ReviewIn(Strict):
    version: StrictInt | None = None              # required when editing an existing review
    followed_plan: YesNo | None = None
    exit_reason: ExitReason | None = None
    exit_followed_plan: YesNo | None = None
    mistakes: Annotated[list[Mistake], Field(max_length=13)] = []
    went_well: Note = None
    went_wrong: Note = None
    lessons: Note = None
    rule_violation_acknowledged: bool = False
    would_take_again: Literal["yes", "no", "unsure"] | None = None
    complete: bool = False                         # mark the review complete (needs the required answers)


class ObjectiveIn(Strict):
    day: date | None = None
    done: bool = True
    note: Annotated[str | None, Field(max_length=500)] = None


def tz_of(s: Session) -> str:
    return svc.get_preferences(s).get("timezone") or "America/New_York"


def today(s: Session) -> date:
    return utcnow().astimezone(ZoneInfo(tz_of(s))).date()


def _clean(v: str | None) -> str | None:
    from ..portfolio.schemas import _text

    return _text(v)


# ------------------------------------------------------------------ journal
def journal_create(s: Session, data: JournalIn) -> JournalEntry:
    svc.get_account(s, data.account_id)
    if data.position_id:
        svc.get_position(s, data.position_id)
    if data.plan_id:
        from ..planning.plans import get as get_plan

        get_plan(s, data.plan_id)
    day = data.entry_date or today(s)
    if day > today(s):
        raise svc.DomainError("FUTURE_DATE", "Journal entries can't be dated in the future.", 422)
    e = JournalEntry(account_id=data.account_id, kind=data.kind, entry_date=day, title=_clean(data.title),
                     body=_clean(data.body) or "", tags_json=sorted({t.strip().lower() for t in data.tags if t.strip()}),
                     position_id=data.position_id, plan_id=data.plan_id, metadata_json={})
    s.add(e)
    s.flush()
    if data.kind in ("daily_thesis", "eod_review", "lesson", "decision"):
        emit(s, "JOURNAL", "info", {"daily_thesis": "Daily thesis written", "eod_review": "End-of-day review written",
                                    "lesson": "Lesson recorded", "decision": "Decision recorded"}[data.kind],
             (e.title or e.body)[:200], account_id=e.account_id, position_id=e.position_id,
             source={"journal_id": e.id, "plan_id": e.plan_id})
    return e


def journal_get(s: Session, jid: str) -> JournalEntry:
    e = s.get(JournalEntry, jid)
    if e is None:
        raise svc.DomainError("NOT_FOUND", f"Journal entry {jid} does not exist.", 404)
    return e


def journal_update(s: Session, jid: str, data: JournalUpdate) -> JournalEntry:
    e = journal_get(s, jid)
    svc._check_version(e, data.version)
    if e.archived_at is not None:
        raise svc.DomainError("ARCHIVED", "Archived journal entries are read-only.", 409)
    before = {"title": e.title, "body": e.body, "tags": e.tags_json}
    if "title" in data.model_fields_set:
        e.title = _clean(data.title)
    if data.body is not None:
        e.body = _clean(data.body) or e.body
    if data.tags is not None:
        e.tags_json = sorted({t.strip().lower() for t in data.tags if t.strip()})
    e.updated_at = utcnow()
    s.flush()
    s.add(AuditEvent(entity_type="journal", entity_id=e.id, action="edit", before_json=before,
                     after_json={"title": e.title, "body": e.body, "tags": e.tags_json}))
    return e


def journal_archive(s: Session, jid: str, version: int) -> JournalEntry:
    e = journal_get(s, jid)
    svc._check_version(e, version)
    if e.archived_at is not None:
        raise svc.DomainError("ALREADY_ARCHIVED", "This journal entry is already archived.", 409)
    e.archived_at = utcnow()
    s.flush()
    s.add(AuditEvent(entity_type="journal", entity_id=e.id, action="archive", before_json={"archived_at": None},
                     after_json={"archived_at": e.archived_at.isoformat()}))
    return e


def journal_restore(s: Session, jid: str, version: int) -> JournalEntry:
    """Bring an archived entry back. Its points come back as a replacement ledger entry (never a re-award)."""
    e = journal_get(s, jid)
    svc._check_version(e, version)
    if e.archived_at is None:
        raise svc.DomainError("NOT_ARCHIVED", "This journal entry isn't archived.", 409)
    before = {"archived_at": e.archived_at.isoformat()}
    e.archived_at = None
    e.updated_at = utcnow()
    s.flush()
    s.add(AuditEvent(entity_type="journal", entity_id=e.id, action="restore", before_json=before, after_json={"archived_at": None}))
    return e


def journal_list(s: Session, account_id: str, kind: list[str] | None = None, start: date | None = None,
                 end: date | None = None, position_id: str | None = None, plan_id: str | None = None,
                 include_archived: bool = False, limit: int = 100) -> list[JournalEntry]:
    q = select(JournalEntry).where(JournalEntry.account_id == account_id)
    if kind:
        q = q.where(JournalEntry.kind.in_(kind))
    if start:
        q = q.where(JournalEntry.entry_date >= start)
    if end:
        q = q.where(JournalEntry.entry_date <= end)
    if position_id:
        q = q.where(JournalEntry.position_id == position_id)
    if plan_id:
        q = q.where(JournalEntry.plan_id == plan_id)
    if not include_archived:
        q = q.where(JournalEntry.archived_at.is_(None))
    return list(s.scalars(q.order_by(JournalEntry.entry_date.desc(), JournalEntry.created_at.desc()).limit(limit)))


# ------------------------------------------------------------------ reviews (mission reports)
REQUIRED = ("followed_plan", "exit_reason", "exit_followed_plan", "lessons")


def policy_results(s: Session, p) -> dict:
    """Every risk result recorded for the position at entry, as reviewed: rule code and name, result, observed value
    and limit, the explanation, the policy revision, and whether an explicit override was recorded at entry.
    Also says whether the position was unplanned (no plan, or a plan written after the entry)."""
    from ..planning.plans import get as get_plan

    risk = p.entry_risk_json or {}
    plan = get_plan(s, p.plan_id) if p.plan_id else None
    unplanned = plan is None or plan.created_at > p.opened_at
    override = bool(p.rule_violation)
    order = {"BLOCKED": 0, "UNKNOWN": 1, "WARNING": 2, "PASS": 3}
    checks = [{"code": c.get("code"), "name": c.get("label"), "result": c.get("result"), "value": c.get("value"),
               "limit": c.get("limit"), "explanation": c.get("reason"), "source": c.get("source"),
               "policy_revision": risk.get("policy_revision"),
               "override_recorded": override and c.get("result") == "BLOCKED"} for c in risk.get("checks", [])]
    checks.sort(key=lambda c: (order.get(c["result"] or "", 9), c["code"] or ""))
    if not risk:
        checks = [{"code": "NO_RISK_CHECK", "name": "Entry risk check", "result": "UNKNOWN", "value": None, "limit": None,
                   "explanation": "No risk check was recorded when this position was entered (imported, or entered "
                                  "before risk checks existed): compliance can't be judged.", "source": None,
                   "policy_revision": None, "override_recorded": False}]
    return {"overall": risk.get("overall") or "UNKNOWN", "policy_revision": risk.get("policy_revision"),
            "policy_id": risk.get("policy_id"), "evaluated_at": risk.get("evaluated_at"), "override_recorded": override,
            "unplanned": unplanned,
            "plan": None if plan is None else {"id": plan.id, "title": plan.title,
                                               "created_after_entry": plan.created_at > p.opened_at},
            "violated": [c for c in checks if c["result"] == "BLOCKED"],
            "unknown": [c for c in checks if c["result"] == "UNKNOWN"],
            "checks": checks}


def review_get(s: Session, position_id: str) -> TradeReview | None:
    return s.scalar(select(TradeReview).where(TradeReview.position_id == position_id))


def review_save(s: Session, position_id: str, data: ReviewIn) -> TradeReview:
    p = svc.get_position(s, position_id)
    if p.lifecycle == Lifecycle.OPEN:
        raise svc.DomainError("POSITION_OPEN", "Review a position after closing it.", 409)
    if p.archive_reason == "entered_in_error":
        raise svc.DomainError("ENTERED_IN_ERROR", "Positions archived as entered in error aren't reviewed.", 409)
    r = review_get(s, position_id)
    before = None
    if r is None:
        r = TradeReview(position_id=p.id, account_id=p.account_id, mistakes_json=[], metadata_json={})
        s.add(r)
    else:
        if data.version is None:
            raise svc.DomainError("VERSION_REQUIRED", "This review exists: send the version you loaded.", 422)
        svc._check_version(r, data.version)
        before = {k: getattr(r, k) for k in ("followed_plan", "exit_reason", "exit_followed_plan", "went_well", "went_wrong",
                                             "lessons", "would_take_again", "rule_violation_acknowledged")} | {
            "mistakes": r.mistakes_json, "completed": r.completed_at is not None, "version": r.version}
    for k in ("followed_plan", "exit_reason", "exit_followed_plan", "went_well", "went_wrong", "lessons", "would_take_again"):
        setattr(r, k, getattr(data, k))
    if not p.plan_id:
        r.followed_plan = r.followed_plan or "no_plan"
    r.mistakes_json = sorted(set(data.mistakes))
    r.rule_violation_acknowledged = data.rule_violation_acknowledged
    pr = policy_results(s, p)
    before_meta = r.metadata_json or {}
    r.metadata_json = {**before_meta, "policy_results": pr,
                       "acknowledged_rules": [c["code"] for c in pr["violated"]] if data.rule_violation_acknowledged else []}
    if data.complete:
        missing = [k for k in REQUIRED if not getattr(r, k)]
        if p.rule_violation and not data.rule_violation_acknowledged:
            missing.append("rule_violation_acknowledged")
        if missing:
            raise svc.DomainError("REVIEW_INCOMPLETE", "Answer these to complete the review.", 422,
                                  field_errors=[{"field": m, "code": "REQUIRED", "message": "Required to complete the review."}
                                                for m in missing])
        if r.completed_at is None:
            r.completed_at = utcnow()
            emit(s, "REVIEW_COMPLETED", "info", f"Mission report: {p.name}",
                 f"Followed the plan: {r.followed_plan}; exit: {r.exit_reason}; lesson: {(r.lessons or '')[:160]}",
                 symbol=p.underlying, account_id=p.account_id, position_id=p.id, source={"review_id": r.id})
    r.updated_at = utcnow()
    s.flush()
    after = {k: getattr(r, k) for k in ("followed_plan", "exit_reason", "exit_followed_plan", "went_well", "went_wrong",
                                        "lessons", "would_take_again", "rule_violation_acknowledged")}
    s.add(AuditEvent(entity_type="review", entity_id=r.id, action="edit" if before else "create", before_json=before,
                     after_json={**after, "mistakes": r.mistakes_json, "completed": r.completed_at is not None,
                                 "version": r.version}))
    return r


def review_history(s: Session, position_id: str) -> list[dict]:
    """Every saved version of the review (oldest first), from the audit trail: nothing is overwritten unrecorded."""
    r = review_get(s, position_id)
    if r is None:
        return []
    rows = s.scalars(select(AuditEvent).where(AuditEvent.entity_type == "review", AuditEvent.entity_id == r.id)
                     .order_by(AuditEvent.at, AuditEvent.id))
    return [{"at": a.at, "action": a.action, "before": a.before_json, "after": a.after_json} for a in rows]


def pending_reviews(s: Session, account_id: str) -> list[dict]:
    from ..portfolio.models import Position

    rows = list(s.scalars(select(Position).where(Position.account_id == account_id, Position.lifecycle != Lifecycle.OPEN,
                                                 (Position.archive_reason.is_(None)) | (Position.archive_reason != "entered_in_error"))
                          .order_by(Position.closed_at.desc())))
    done = {r.position_id: r for r in s.scalars(select(TradeReview).where(TradeReview.account_id == account_id))}
    now = utcnow()
    out = []
    for p in rows:
        r = done.get(p.id)
        if r and r.completed_at:
            continue
        days = (now - p.closed_at).days if p.closed_at else None
        out.append({"position_id": p.id, "name": p.name, "underlying": p.underlying, "closed_at": p.closed_at,
                    "days_waiting": days, "overdue": days is not None and days >= 2, "rule_violation": p.rule_violation,
                    "started": r is not None, "plan_id": p.plan_id})
    out.sort(key=lambda x: (not x["rule_violation"], not x["overdue"]))
    return out


# ------------------------------------------------------------------ objectives
def objective_set(s: Session, account_id: str, code: str, data: ObjectiveIn) -> DailyObjective | None:
    svc.get_account(s, account_id)
    if code not in MANUAL and code != REST:
        raise svc.DomainError("AUTOMATIC_OBJECTIVE", "This objective is met automatically from your records "
                                                     "(journal or risk policy); it can't be ticked by hand.", 422)
    t = today(s)
    day = data.day or t
    if day > t or day < t - timedelta(days=7):
        raise svc.DomainError("BAD_DAY", "Objectives can be recorded for today or the past 7 days.", 422)
    row = s.scalar(select(DailyObjective).where(DailyObjective.account_id == account_id, DailyObjective.day == day,
                                                DailyObjective.code == code))
    if row is None:
        row = DailyObjective(account_id=account_id, day=day, code=code)
        s.add(row)
    row.done, row.note, row.at = data.done, data.note, utcnow()
    s.flush()
    return row


__all__ = ["datetime"]
