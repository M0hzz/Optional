"""/api/v1 routes for Phase 4: progress (points, level, streaks, achievements), the daily process score and
objectives, the journal (archives) and mission reports (post-trade reviews). Nothing here reads P&L to score."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import Field, StrictInt
from sqlalchemy import select

from ..portfolio import service as svc
from ..portfolio.models import DailyScore, ProcessAward, TradeReview
from ..portfolio.schemas import ArchiveIn, Strict
from ..process import engine as pe
from ..process import service as ps
from .deps import Read, Write

router = APIRouter()


USER = "user:local"            # the single local user (no accounts or sign-in in this app yet)


def _refresh(s, account_id: str) -> None:
    """Bring the points ledger, finished-day scores and achievements up to date right after a write that can
    change scoring evidence (idempotent: an unchanged record adds nothing)."""
    pe.refresh(s, account_id, ps.tz_of(s), actor="system:process-sync")


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
            "updated_at": r.updated_at, "version": r.version,
            "policy_results": (r.metadata_json or {}).get("policy_results"),
            "acknowledged_rules": (r.metadata_json or {}).get("acknowledged_rules", [])}


def _subject_notes(s, rows: list[ProcessAward]) -> dict[str, str]:
    """Why points are kept for a subject that has since been archived (archiving after the fact is housekeeping)."""
    from ..portfolio.models import Position, TradePlan

    notes: dict[str, str] = {}
    for r in rows:
        if r.subject_type == "plan" and r.subject_id not in notes:
            pl = s.get(TradePlan, r.subject_id)
            if pl is not None and pl.archived_at is not None:
                notes[r.subject_id] = ("The plan was archived after its outcome was recorded; archiving is housekeeping "
                                       "and the skip still stands, so the points are kept.")
        elif r.subject_type == "position" and r.subject_id not in notes:
            p = s.get(Position, r.subject_id)
            if p is not None and p.archived_at is not None and p.archive_reason != "entered_in_error":
                notes[r.subject_id] = ("The position was archived after closing; archiving is housekeeping, so the "
                                       "process points earned at entry are kept.")
    return notes


def _ledger(s, rows: list[ProcessAward]) -> list[dict]:
    notes = _subject_notes(s, rows)
    return [{**pe.ledger_out(r), "note": notes.get(r.subject_id)} for r in rows]


# ------------------------------------------------------------------ progress, scores and the ledger
@router.get("/accounts/{account_id}/progress", tags=["process"])
def get_progress(account_id: str, s: Write):
    """Points (lifetime awarded, revoked or corrected, effective; by category), colony level, streaks,
    achievements (with their evidence and rule version), recent ledger entries, themes and today's score.
    Brings the points ledger and the finished-day scores up to date from your records (idempotent)."""
    svc.get_account(s, account_id)
    out = pe.progress(s, account_id, ps.tz_of(s))
    rows = list(s.scalars(select(ProcessAward).where(ProcessAward.id.in_([a["id"] for a in out["recent_awards"]]))))
    notes = _subject_notes(s, rows)
    for a in out["recent_awards"]:
        a["note"] = notes.get(a["subject_id"])
    return out


@router.get("/accounts/{account_id}/process-score", tags=["process"])
def process_score(account_id: str, s: Read, day: date | None = None):
    """Today's score is live (TO_DO for what's still open). A finished day returns its stored, immutable score
    (the latest revision), or the computed score marked stored=false if it hasn't been finalized yet."""
    svc.get_account(s, account_id)
    f = pe.load_facts(s, account_id, ps.tz_of(s))
    d = day or f.today
    if d > f.today:
        raise svc.DomainError("FUTURE_DATE", "Days in the future aren't scored.", 422)
    if d < f.today and (r := pe.latest_scores(s, account_id).get(d)) is not None:
        out = pe.score_record(r)
        out["revisions"] = r.revision
        return out
    return pe.score_day(f, d, pe._points_by_day(s, account_id).get(d, 0))


@router.get("/accounts/{account_id}/process-score/history", tags=["process"])
def process_history(account_id: str, s: Read, days: Annotated[int, Query(ge=1, le=120)] = 30):
    svc.get_account(s, account_id)
    f = pe.load_facts(s, account_id, ps.tz_of(s))
    stored = pe.latest_scores(s, account_id)
    out = []
    for i in range(days - 1, -1, -1):
        d = f.today - timedelta(days=i)
        if d < f.created:
            continue
        r = stored.get(d) if d < f.today else None
        sc = pe.score_record(r) if r is not None else pe.score_day(f, d)
        out.append({"day": sc["day"], "score": sc["score"], "rest_day": sc["rest_day"], "met": sc["met"],
                    "applicable": sc["applicable"], "unknown": sc["unknown"], "stored": sc["stored"],
                    "revision": r.revision if r is not None else None, "version": sc["version"],
                    "in_progress": sc["in_progress"], "scheduled": d.weekday() in f.schedule_days})
    return out


@router.get("/accounts/{account_id}/scores/{day}", tags=["process"])
def score_revisions(account_id: str, day: date, s: Read):
    """Every stored revision of a finished day's score, oldest first. Revisions are never overwritten: a
    correction is a new revision that references the one it supersedes and says what changed."""
    svc.get_account(s, account_id)
    rows = s.scalars(select(DailyScore).where(DailyScore.account_id == account_id, DailyScore.account_date == day)
                     .order_by(DailyScore.revision))
    return [pe.score_record(r) for r in rows]


@router.get("/accounts/{account_id}/ledger", tags=["process"])
def ledger(account_id: str, s: Read, subject_id: str | None = None, entry_type: str | None = None,
           limit: Annotated[int, Query(ge=1, le=1000)] = 200):
    """The append-only points ledger: awards, revocations, corrections and replacements, newest first."""
    svc.get_account(s, account_id)
    q = select(ProcessAward).where(ProcessAward.account_id == account_id)
    if subject_id:
        q = q.where(ProcessAward.subject_id == subject_id)
    if entry_type:
        q = q.where(ProcessAward.entry_type == entry_type)
    rows = list(s.scalars(q.order_by(ProcessAward.awarded_at.desc(), ProcessAward.id.desc()).limit(limit)))
    return {"summary": pe.points_summary(s, account_id), "versions": pe.VERSIONS, "entries": _ledger(s, rows)}


class AdjustIn(Strict):
    reason: Annotated[str, Field(min_length=3, max_length=500)]
    points: StrictInt | None = None           # corrections only: the change in points (+/-)


@router.post("/accounts/{account_id}/ledger/{entry_id}/revoke", status_code=201, tags=["process"])
def revoke(account_id: str, entry_id: str, body: AdjustIn, s: Write):
    """Revoke an erroneous, duplicated or invalid award. Adds a revocation entry; the original stays."""
    svc.get_account(s, account_id)
    f = pe.load_facts(s, account_id, ps.tz_of(s))
    e = pe.adjust(s, f, entry_id, "revocation", body.reason, USER)
    pe.finalize_days(s, f)
    return pe.ledger_out(e)


@router.post("/accounts/{account_id}/ledger/{entry_id}/correct", status_code=201, tags=["process"])
def correct(account_id: str, entry_id: str, body: AdjustIn, s: Write):
    """Correct an entry's points by a signed amount. Adds a correction entry; the original stays."""
    svc.get_account(s, account_id)
    f = pe.load_facts(s, account_id, ps.tz_of(s))
    e = pe.adjust(s, f, entry_id, "correction", body.reason, USER, body.points)
    pe.finalize_days(s, f)
    return pe.ledger_out(e)


@router.post("/accounts/{account_id}/objectives/{code}", tags=["process"])
def set_objective(account_id: str, code: str, body: ps.ObjectiveIn, s: Write):
    """Tick (or untick) a manual daily objective, or mark a rest day (code REST_DAY)."""
    ps.objective_set(s, account_id, code.upper(), body)
    _refresh(s, account_id)
    return process_score(account_id, s, body.day)


@router.get("/accounts/{account_id}/awards", tags=["process"])
def awards(account_id: str, s: Read, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    svc.get_account(s, account_id)
    rows = list(s.scalars(select(ProcessAward).where(ProcessAward.account_id == account_id)
                          .order_by(ProcessAward.awarded_at.desc(), ProcessAward.id.desc()).limit(limit)))
    return _ledger(s, rows)


# ------------------------------------------------------------------ journal
@router.get("/journal", tags=["journal"])
def list_journal(s: Read, account_id: str, kind: Annotated[list[str] | None, Query()] = None, start: date | None = None,
                 end: date | None = None, position_id: str | None = None, plan_id: str | None = None,
                 include_archived: bool = False, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    return [journal_out(e) for e in ps.journal_list(s, account_id, kind, start, end, position_id, plan_id,
                                                     include_archived, limit)]


@router.post("/journal", status_code=201, tags=["journal"])
def create_journal(body: ps.JournalIn, s: Write):
    e = ps.journal_create(s, body)
    _refresh(s, e.account_id)
    return journal_out(e)


@router.patch("/journal/{entry_id}", tags=["journal"])
def update_journal(entry_id: str, body: ps.JournalUpdate, s: Write):
    """Edit the text or tags. Wording isn't scoring evidence, so this never changes points or scores."""
    return journal_out(ps.journal_update(s, entry_id, body))


@router.post("/journal/{entry_id}/archive", tags=["journal"])
def archive_journal(entry_id: str, body: ArchiveIn, s: Write):
    """Archive an entry. If it was scoring evidence, its points are revoked (with the reason) and the day's
    stored score gets a new revision; nothing is deleted."""
    e = ps.journal_archive(s, entry_id, body.version)
    _refresh(s, e.account_id)
    return journal_out(e)


@router.post("/journal/{entry_id}/restore", tags=["journal"])
def restore_journal(entry_id: str, body: ArchiveIn, s: Write):
    """Restore an archived entry. Revoked points come back as a replacement entry, and the day gets a new revision."""
    e = ps.journal_restore(s, entry_id, body.version)
    _refresh(s, e.account_id)
    return journal_out(e)


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


@router.get("/positions/{position_id}/review/history", tags=["reviews"])
def review_history(position_id: str, s: Read):
    """Every saved version of the review (the answers before and after each save)."""
    svc.get_position(s, position_id)
    return ps.review_history(s, position_id)


@router.get("/positions/{position_id}/policy-results", tags=["reviews"])
def policy_results(position_id: str, s: Read):
    """Each risk result recorded at entry (rule, result, value, limit, explanation, policy revision, override)."""
    return ps.policy_results(s, svc.get_position(s, position_id))


@router.put("/positions/{position_id}/review", tags=["reviews"])
def save_review(position_id: str, body: ps.ReviewIn, s: Write):
    r = ps.review_save(s, position_id, body)
    _refresh(s, r.account_id)
    return review_out(r)


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
    pts = list(s.scalars(select(ProcessAward).where(ProcessAward.subject_id.in_(subj))
                         .order_by(ProcessAward.awarded_at, ProcessAward.id)))
    return {"position": _pos(request, s, p).model_dump(mode="json"),
            "plan": plan_out(s, plan).model_dump(mode="json") if plan else None,
            "review": review_out(review), "journal": [journal_out(e) for e in ps.journal_list(s, p.account_id, position_id=p.id)],
            "timeline": position_timeline(position_id, s, 300),
            "policy_results": ps.policy_results(s, p), "review_history": ps.review_history(s, position_id),
            "points": _ledger(s, pts), "points_net": sum(a.points for a in pts),
            "pnl_note": "Realized P&L is shown for the record. It never counts towards the process score, points or level."}
