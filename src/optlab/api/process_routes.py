"""/api/v1 routes for Phase 4: progress (points, level, streaks, achievements), the daily process score and
objectives, the journal (archives) and mission reports (post-trade reviews). Nothing here reads P&L to score."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from ..portfolio import service as svc
from ..portfolio.models import ProcessAward, TradeReview
from ..portfolio.schemas import ArchiveIn
from ..process import engine as pe
from ..process import service as ps
from .deps import Read, Write

router = APIRouter()


def journal_out(e) -> dict:
    return {"id": e.id, "account_id": e.account_id, "kind": e.kind, "entry_date": e.entry_date, "title": e.title,
            "body": e.body, "tags": e.tags_json, "position_id": e.position_id, "plan_id": e.plan_id,
            "archived_at": e.archived_at, "created_at": e.created_at, "updated_at": e.updated_at, "version": e.version}


def review_out(r: TradeReview | None) -> dict | None:
    if r is None:
        return None
    return {"id": r.id, "position_id": r.position_id, "followed_plan": r.followed_plan, "exit_reason": r.exit_reason,
            "exit_followed_plan": r.exit_followed_plan, "mistakes": r.mistakes_json, "went_well": r.went_well,
            "went_wrong": r.went_wrong, "lessons": r.lessons, "rule_violation_acknowledged": r.rule_violation_acknowledged,
            "would_take_again": r.would_take_again, "completed_at": r.completed_at, "created_at": r.created_at,
            "updated_at": r.updated_at, "version": r.version}


# ------------------------------------------------------------------ progress and score
@router.get("/accounts/{account_id}/progress", tags=["process"])
def get_progress(account_id: str, s: Write):
    """Points (by category), colony level, streaks, achievements, recent awards, themes, and today's score.
    Brings the points ledger up to date from your records (idempotent)."""
    svc.get_account(s, account_id)
    return pe.progress(s, account_id, ps.tz_of(s))


@router.get("/accounts/{account_id}/process-score", tags=["process"])
def process_score(account_id: str, s: Read, day: date | None = None):
    svc.get_account(s, account_id)
    f = pe.load_facts(s, account_id, ps.tz_of(s))
    return pe.score_day(f, day or f.today)


@router.get("/accounts/{account_id}/process-score/history", tags=["process"])
def process_history(account_id: str, s: Read, days: Annotated[int, Query(ge=1, le=120)] = 30):
    svc.get_account(s, account_id)
    f = pe.load_facts(s, account_id, ps.tz_of(s))
    out = []
    for i in range(days - 1, -1, -1):
        d = f.today - timedelta(days=i)
        if d < f.created:
            continue
        sc = pe.score_day(f, d)
        out.append({"day": sc["day"], "score": sc["score"], "rest_day": sc["rest_day"], "met": sc["met"],
                    "applicable": sc["applicable"], "scheduled": d.weekday() in f.schedule_days})
    return out


@router.post("/accounts/{account_id}/objectives/{code}", tags=["process"])
def set_objective(account_id: str, code: str, body: ps.ObjectiveIn, s: Write):
    """Tick (or untick) a manual daily objective, or mark a rest day (code REST_DAY)."""
    ps.objective_set(s, account_id, code.upper(), body)
    f = pe.load_facts(s, account_id, ps.tz_of(s))
    return pe.score_day(f, body.day or f.today)


@router.get("/accounts/{account_id}/awards", tags=["process"])
def awards(account_id: str, s: Read, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    svc.get_account(s, account_id)
    rows = s.scalars(select(ProcessAward).where(ProcessAward.account_id == account_id)
                     .order_by(ProcessAward.awarded_at.desc()).limit(limit))
    return [{"code": a.code, "category": a.category, "points": a.points, "description": a.description,
             "day": a.day, "subject_type": a.subject_type, "subject_id": a.subject_id, "awarded_at": a.awarded_at} for a in rows]


# ------------------------------------------------------------------ journal
@router.get("/journal", tags=["journal"])
def list_journal(s: Read, account_id: str, kind: Annotated[list[str] | None, Query()] = None, start: date | None = None,
                 end: date | None = None, position_id: str | None = None, plan_id: str | None = None,
                 include_archived: bool = False, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    return [journal_out(e) for e in ps.journal_list(s, account_id, kind, start, end, position_id, plan_id,
                                                     include_archived, limit)]


@router.post("/journal", status_code=201, tags=["journal"])
def create_journal(body: ps.JournalIn, s: Write):
    return journal_out(ps.journal_create(s, body))


@router.patch("/journal/{entry_id}", tags=["journal"])
def update_journal(entry_id: str, body: ps.JournalUpdate, s: Write):
    return journal_out(ps.journal_update(s, entry_id, body))


@router.post("/journal/{entry_id}/archive", tags=["journal"])
def archive_journal(entry_id: str, body: ArchiveIn, s: Write):
    return journal_out(ps.journal_archive(s, entry_id, body.version))


# ------------------------------------------------------------------ reviews and mission reports
@router.get("/accounts/{account_id}/reviews/pending", tags=["reviews"])
def pending(account_id: str, s: Read):
    svc.get_account(s, account_id)
    return ps.pending_reviews(s, account_id)


@router.get("/accounts/{account_id}/reviews", tags=["reviews"])
def reviews(account_id: str, s: Read, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    svc.get_account(s, account_id)
    rows = s.scalars(select(TradeReview).where(TradeReview.account_id == account_id, TradeReview.completed_at.is_not(None))
                     .order_by(TradeReview.completed_at.desc()).limit(limit))
    out = []
    for r in rows:
        p = svc.get_position(s, r.position_id)
        out.append({**(review_out(r) or {}), "position": {"id": p.id, "name": p.name, "underlying": p.underlying,
                                                          "strategy_name": p.strategy, "closed_at": p.closed_at,
                                                          "plan_id": p.plan_id, "rule_violation": p.rule_violation}})
    return out


@router.put("/positions/{position_id}/review", tags=["reviews"])
def save_review(position_id: str, body: ps.ReviewIn, s: Write):
    return review_out(ps.review_save(s, position_id, body))


@router.get("/positions/{position_id}/report", tags=["reviews"])
def mission_report(position_id: str, request: Request, s: Read):
    """Everything about one position for the debrief: the position, its plan and the planned-versus-actual
    comparison, the entry risk check, the review, the journal notes, the timeline and the points it earned.
    Realized P&L is included for the record; it is not part of any score."""
    from ..planning.plans import get as get_plan
    from .monitor_routes import position_timeline
    from .plan_routes import plan_out
    from .routes import _pos

    p = svc.get_position(s, position_id)
    plan = get_plan(s, p.plan_id) if p.plan_id else None
    review = ps.review_get(s, position_id)
    subj = {position_id} | ({review.id} if review else set())
    pts = list(s.scalars(select(ProcessAward).where(ProcessAward.subject_id.in_(subj))))
    return {"position": _pos(request, s, p).model_dump(mode="json"),
            "plan": plan_out(s, plan).model_dump(mode="json") if plan else None,
            "review": review_out(review), "journal": [journal_out(e) for e in ps.journal_list(s, p.account_id, position_id=p.id)],
            "timeline": position_timeline(position_id, s, 300),
            "points": [{"code": a.code, "points": a.points, "category": a.category, "description": a.description} for a in pts],
            "pnl_note": "Realized P&L is shown for the record. It never counts towards the process score, points or level."}
