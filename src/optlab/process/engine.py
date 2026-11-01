"""Process scoring, points, levels, streaks and achievements. Rewards process, never profit.

Versions (fixed in code; changing a rule or a point value means a new version)
    process-score/v1        the daily objectives, the per-entry items and how a day is scored
    points-policy/v1        the point values and daily caps
    achievement-rules/v1    the achievements and their goals

Daily score
    Each day's evidence is collected into an input snapshot (record ids and yes/no facts only, never text),
    and the snapshot is evaluated by a pure function into components with one of four states:
        MET              counts, earns credit
        VIOLATED         counts, earns no credit
        UNKNOWN          counts, earns no credit, and is disclosed (the data to judge it is missing)
        NOT_APPLICABLE   excluded from the denominator
    For the current, unfinished day an actionable item not done yet shows TO_DO (an interface state; it
    counts like VIOLATED until done). Score = MET ÷ (MET + VIOLATED + UNKNOWN [+ TO_DO]).
    Daily objectives:
        REVIEW_POSITIONS (manual; n/a with nothing open) · REVIEW_EXPIRATIONS (manual; n/a with nothing
        expiring within 14 days) · CHECK_DATA (manual) · DAILY_THESIS (journal) · MAX_DAILY_RISK (policy) ·
        INVALIDATION_BEFORE_ENTRY (n/a with no entries) · REVIEW_EVENTS (manual) · EOD_REVIEW (journal)
    Per position opened that day: planned before entry · maximum loss defined in the plan · within the risk
    policy at entry (UNKNOWN when the check couldn't be made or wasn't recorded; n/a for imported history) ·
    entered after the plan qualified. Reviews: no closed position waits more than 2 days.
    A day without trades has the entry items not applicable, so it can score 100. A rest day isn't scored.

Finished days
    Once a day is over (in the account's time zone) its score is stored, immutable, with the snapshot,
    components, numerator, denominator, percentage, the day's points, the source ids and a backfill flag
    (calculated more than a day after it ended). If the evidence for a finished day later changes, a new
    revision is stored that references the previous one and says what changed; old revisions stay.
    Scores stored under another scoring version are never recomputed.

Points ledger (append-only; see models.ProcessAward)
    award · revocation · correction · replacement. Points are granted while their evidence holds; if the
    evidence stops holding (a thesis archived, a position archived as entered in error, a review answer
    changed) the award is revoked with the reason, and if it holds again (restored) a replacement is added.
    Caps per day: position items count for at most 3 positions, skipped setups for at most 3 plans.

Never an input: profit or loss, position size, the number of trades, time on screen (tests check the code
and the results).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..portfolio.models import (Account, AuditEvent, DailyObjective, DailyScore, JournalEntry, Position, ProcessAward,
                                RiskPolicy, TradePlan, TradeReview, UserAchievement, utcnow)

SCORE_POLICY, SCORE_VERSION = "process-score", "v1"
POINTS_VERSION = "points-policy/v1"
ACHIEVEMENT_VERSION = "achievement-rules/v1"
MET, VIOLATED, UNKNOWN, NA, TODO = "MET", "VIOLATED", "UNKNOWN", "NOT_APPLICABLE", "TO_DO"
OBJECTIVES = [
    ("REVIEW_POSITIONS", "Review open positions", "manual"),
    ("REVIEW_EXPIRATIONS", "Review expiration risk", "manual"),
    ("CHECK_DATA", "Confirm data-source status", "manual"),
    ("DAILY_THESIS", "Write a daily market thesis", "auto"),
    ("MAX_DAILY_RISK", "Define maximum daily risk", "auto"),
    ("INVALIDATION_BEFORE_ENTRY", "Record invalidation before entry", "auto"),
    ("REVIEW_EVENTS", "Review major scheduled events", "manual"),
    ("EOD_REVIEW", "Complete the end-of-day review", "auto"),
]
MANUAL = {c for c, _, k in OBJECTIVES if k == "manual"}
ACTIONABLE = {"REVIEW_POSITIONS", "REVIEW_EXPIRATIONS", "CHECK_DATA", "DAILY_THESIS", "MAX_DAILY_RISK", "REVIEW_EVENTS",
              "EOD_REVIEW"}
REST = "REST_DAY"
POINTS = {"PLAN_BEFORE_ENTRY": (10, "discipline", "Created a complete trade plan before entry"),
          "MAX_LOSS_DEFINED": (10, "risk_control", "Defined the maximum loss before entry"),
          "WITHIN_RISK": (10, "risk_control", "Stayed within the risk policy"),
          "LIQUID_CONTRACT": (5, "risk_control", "Used liquid contracts"),
          "WAITED_CONFIRMATION": (10, "discipline", "Waited for the plan's confirmation"),
          "SKIPPED_SETUP": (10, "discipline", "Skipped a setup that didn't qualify"),
          "REVIEW_DONE": (10, "review", "Completed a mission report"),
          "FOLLOWED_EXIT": (10, "discipline", "Followed the planned exit"),
          "HONEST_REVIEW": (5, "review", "Reviewed a rule break honestly"),
          "DAILY_THESIS": (5, "research", "Wrote the daily market thesis"),
          "EOD_REVIEW": (5, "review", "Completed the end-of-day review"),
          "ALL_OBJECTIVES": (10, "discipline", "Met every daily objective")}
POSITION_CODES = ("PLAN_BEFORE_ENTRY", "MAX_LOSS_DEFINED", "WITHIN_RISK", "LIQUID_CONTRACT", "WAITED_CONFIRMATION")
DAILY_CAP = 3
STREAK_THRESHOLD = 80
FINALIZE_LOOKBACK_DAYS = 90
LEVEL_NAMES_COLONY = ["Landing site", "Outpost", "Settlement", "Colony", "Township", "Station", "City", "Citadel",
                      "Capital", "Federation"]
THEMES = {"default": 1, "aurora": 3, "nebula": 5, "solar": 7}
ACHIEVEMENTS = [
    ("FIRST_PLAN", "First mission briefed", "Arm your first complete trade plan.", 1),
    ("PLANNED_FIVE", "Five planned in a row", "Your last five positions all came from plans.", 5),
    ("REVIEWS_TEN", "Ten mission reports", "Complete ten post-trade reviews.", 10),
    ("SKIPS_FIVE", "Patience pays", "Skip (or let invalidate) five armed setups that didn't qualify.", 5),
    ("MONTH_WITHIN_LIMITS", "A month inside the lines", "Thirty days of trading without recording a position against your risk policy.", 30),
    ("ALL_DOCUMENTED", "Every expedition documented", "Every open position has a plan or a journal note.", 1),
    ("NO_STALE_30", "Clean records", "Thirty days with no position left open past an expiration.", 30),
    ("EXPIRATION_REVIEW", "Expiration watch", "Complete an expiration-risk review.", 1),
    ("STREAK_FIVE", "Five disciplined days", "Five scored days in a row at 80% or better.", 5),
    ("HONEST_RECORD", "Honest record", "Review a position you recorded against your policy, and acknowledge it.", 1),
]
VERSIONS = {"process_score": f"{SCORE_POLICY}/{SCORE_VERSION}", "points_policy": POINTS_VERSION,
            "achievement_rules": ACHIEVEMENT_VERSION}


# ------------------------------------------------------------------ facts (loaded once)
@dataclass
class Facts:
    account_id: str
    tz_name: str
    tz: ZoneInfo
    now: datetime
    today: date
    created: date
    positions: list[Position]
    plans: dict[str, TradePlan]
    plan_states: dict[str, list[str]]           # each plan's state history, in order
    reviews: dict[str, TradeReview]             # by position id
    journal: list[JournalEntry]                 # including archived ones (to explain revocations)
    manual: dict[tuple[date, str], DailyObjective]
    policies: list[RiskPolicy]
    schedule_days: set[int] = field(default_factory=lambda: {0, 1, 2, 3, 4})
    _index: dict | None = field(default=None, init=False, repr=False)
    _snaps: dict = field(default_factory=dict, init=False, repr=False)

    def index(self) -> dict:
        """Lookups built once per refresh (a refresh evaluates up to 90 days three times)."""
        if self._index is None:
            live = sorted((p for p in self.positions if _live(p)), key=lambda p: p.id)
            rows = [(p.id, p.opened_at, p.closed_at, [lg.expiration for lg in p.legs if lg.expiration], p) for p in live]
            by_open: dict[date, list[Position]] = {}
            for p in live:
                by_open.setdefault(p.opened_at.astimezone(self.tz).date(), []).append(p)
            for v in by_open.values():
                v.sort(key=lambda p: (p.opened_at, p.id))
            journal: dict[date, list[JournalEntry]] = {}
            for j in self.journal:
                if j.archived_at is None:
                    journal.setdefault(j.entry_date, []).append(j)
            self._index = {"live": rows, "by_open": by_open, "journal": journal}
        return self._index

    def local(self, dt: datetime | None) -> date | None:
        return None if dt is None else dt.astimezone(self.tz).date()

    def window(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time(0), self.tz)
        return start, datetime.combine(day + timedelta(days=1), time(0), self.tz)

    @property
    def armed_plans(self) -> set[str]:
        return {pid for pid, st in self.plan_states.items() if "ARMED" in st}


def load_facts(s: Session, account_id: str, tz_name: str, now: datetime | None = None) -> Facts:
    now = now or utcnow()
    tz = ZoneInfo(tz_name)
    acc = s.get(Account, account_id)
    positions = list(s.scalars(select(Position).options(selectinload(Position.legs)).where(Position.account_id == account_id)))
    plans = {p.id: p for p in s.scalars(select(TradePlan).where(TradePlan.account_id == account_id))}
    states: dict[str, list[str]] = {pid: [] for pid in plans}
    if plans:
        for a in s.scalars(select(AuditEvent).where(AuditEvent.entity_type == "plan", AuditEvent.action == "state",
                                                    AuditEvent.entity_id.in_(list(plans))).order_by(AuditEvent.at, AuditEvent.id)):
            st = (a.after_json or {}).get("state")
            if st:
                states[a.entity_id].append(st)
    reviews = {r.position_id: r for r in s.scalars(select(TradeReview).where(TradeReview.account_id == account_id))}
    journal = list(s.scalars(select(JournalEntry).where(JournalEntry.account_id == account_id)))
    manual = {(o.day, o.code): o for o in s.scalars(select(DailyObjective).where(DailyObjective.account_id == account_id))}
    policies = list(s.scalars(select(RiskPolicy).where(RiskPolicy.account_id == account_id).order_by(RiskPolicy.revision)))
    from ..monitoring import settings as msettings
    from ..monitoring.settings import DAYS

    sched = {DAYS.index(d) for d in msettings.load(s).schedule.days}
    created = acc.created_at.astimezone(tz).date() if acc else now.astimezone(tz).date()
    return Facts(account_id, tz_name, tz, now, now.astimezone(tz).date(), created, positions, plans, states, reviews, journal,
                 manual, policies, sched)


# ------------------------------------------------------------------ snapshot (evidence) and evaluation
def _policy_at(f: Facts, end: datetime) -> RiskPolicy | None:
    cur = None
    for p in f.policies:
        if p.created_at < end and (p.superseded_at is None or p.superseded_at >= end):
            cur = p
    return cur


def _live(p: Position) -> bool:
    return p.archive_reason != "entered_in_error"


def day_snapshot(f: Facts, day: date) -> dict:
    """Everything the day's score depends on: record ids and yes/no facts, deterministic order, no free text.
    `display` holds names for the explanations and is not part of the evidence. Memoized per Facts."""
    if day in f._snaps:
        return f._snaps[day]
    start, end = f.window(day)
    ix = f.index()
    rest = bool((m := f.manual.get((day, REST))) and m.done)
    open_ = [r for r in ix["live"] if r[1] < end and (r[2] is None or r[2] >= start)]          # already sorted by id
    expiring = [r[0] for r in open_ if any(0 <= (e - day).days <= 14 for e in r[3])]
    active = ix["journal"].get(day, [])
    pol = _policy_at(f, end)
    entries = []
    display: dict[str, str] = {}
    for p in ix["by_open"].get(day, []):
        pl = f.plans.get(p.plan_id) if p.plan_id else None
        planned = pl is not None and pl.created_at <= p.opened_at
        risk = (p.entry_risk_json or {}).get("overall")
        entries.append({"position_id": p.id, "plan_id": pl.id if pl else None, "planned_before_entry": planned,
                        "plan_has_invalidation": bool(pl and (pl.invalidation or pl.invalidation_conditions
                                                              or pl.invalidation_price_units is not None)),
                        "plan_max_loss_defined": bool(pl and pl.planned_max_loss_units is not None),
                        "entry_risk": risk, "imported": p.source in ("csv", "scans.yaml"),
                        "converted_from": (pl.comparison_json or {}).get("converted_from") if pl else None})
        display[p.id] = p.name
    due = []
    cutoff = end - timedelta(days=2)
    for pid, _o, closed, _e, p in ix["live"]:
        if closed is None or closed > cutoff:
            continue
        r = f.reviews.get(pid)
        due.append({"position_id": pid, "review_id": r.id if r else None,
                    "completed": bool(r and r.completed_at and r.completed_at < end)})
        display[pid] = p.name
    snap = {"version": f"{SCORE_POLICY}/{SCORE_VERSION}", "day": day.isoformat(), "rest_day": rest,
            "manual": {c: bool((m := f.manual.get((day, c))) and m.done) for c in sorted(MANUAL)},
            "open_position_ids": [r[0] for r in open_], "expiring_position_ids": expiring,
            "daily_thesis_ids": sorted(j.id for j in active if j.kind == "daily_thesis"),
            "eod_review_ids": sorted(j.id for j in active if j.kind == "eod_review"),
            "policy": None if pol is None else {"id": pol.id, "revision": pol.revision,
                                                 "daily_loss_limit": (pol.settings_json or {}).get("max_daily_realized_loss")},
            "entries": entries, "reviews_due": due, "display": display}
    f._snaps[day] = snap
    return snap


def evidence_key(snapshot: dict) -> dict:
    """The part of a snapshot that decides the score (everything but display names and the version)."""
    return {k: v for k, v in snapshot.items() if k not in ("display",)}


LABEL = {c: lbl for c, lbl, _ in OBJECTIVES}


def evaluate(snap: dict, in_progress: bool = False) -> dict:
    """Pure: components and score from a snapshot. Reads no profit or loss."""
    comps: list[dict] = []
    rest = snap["rest_day"]
    names = snap.get("display", {})

    def add(code, label, group, state, detail="", kind="auto", subject=None):
        if rest:
            state, detail = NA, "rest day"
        elif in_progress and state == VIOLATED and code in ACTIONABLE:
            state = TODO
        comps.append({"code": code, "label": label, "group": group, "state": state, "detail": detail, "kind": kind,
                      "subject_id": subject})

    m = snap["manual"]
    add("REVIEW_POSITIONS", LABEL["REVIEW_POSITIONS"], "objective",
        NA if not snap["open_position_ids"] else (MET if m["REVIEW_POSITIONS"] else VIOLATED),
        "no open positions" if not snap["open_position_ids"] else "", "manual")
    add("REVIEW_EXPIRATIONS", LABEL["REVIEW_EXPIRATIONS"], "objective",
        NA if not snap["expiring_position_ids"] else (MET if m["REVIEW_EXPIRATIONS"] else VIOLATED),
        "nothing expires within 14 days" if not snap["expiring_position_ids"] else "", "manual")
    add("CHECK_DATA", LABEL["CHECK_DATA"], "objective", MET if m["CHECK_DATA"] else VIOLATED, "", "manual")
    add("DAILY_THESIS", LABEL["DAILY_THESIS"], "objective", MET if snap["daily_thesis_ids"] else VIOLATED,
        "written" if snap["daily_thesis_ids"] else "")
    lim = (snap["policy"] or {}).get("daily_loss_limit")
    add("MAX_DAILY_RISK", LABEL["MAX_DAILY_RISK"], "objective", MET if lim else VIOLATED,
        f"maximum daily loss ${lim} (policy revision {snap['policy']['revision']})" if lim else
        "set a maximum daily realized loss in the risk policy")
    ents = snap["entries"]
    if not ents:
        add("INVALIDATION_BEFORE_ENTRY", LABEL["INVALIDATION_BEFORE_ENTRY"], "objective", NA, "no positions opened")
    else:
        bad = [names.get(e["position_id"], e["position_id"]) for e in ents
               if not (e["planned_before_entry"] and e["plan_has_invalidation"])]
        add("INVALIDATION_BEFORE_ENTRY", LABEL["INVALIDATION_BEFORE_ENTRY"], "objective", VIOLATED if bad else MET,
            ("no written invalidation: " + ", ".join(bad)) if bad else f"{len(ents)} entr{'y' if len(ents) == 1 else 'ies'}, all with an invalidation")
    add("REVIEW_EVENTS", LABEL["REVIEW_EVENTS"], "objective", MET if m["REVIEW_EVENTS"] else VIOLATED, "", "manual")
    add("EOD_REVIEW", LABEL["EOD_REVIEW"], "objective", MET if snap["eod_review_ids"] else VIOLATED,
        "written" if snap["eod_review_ids"] else "")
    for e in ents:
        n, pid, planned = names.get(e["position_id"], e["position_id"]), e["position_id"], e["planned_before_entry"]
        add("PLANNED", f"{n}: planned before entry", "trade", MET if planned else VIOLATED,
            "" if planned else "recorded without a plan (unplanned position)", subject=pid)
        add("MAX_LOSS_DEFINED", f"{n}: maximum loss defined in the plan", "trade",
            NA if not planned else (MET if e["plan_max_loss_defined"] else VIOLATED), "" if planned else "no plan", subject=pid)
        r = e["entry_risk"]
        if r in ("PASS", "WARNING"):
            st, why = MET, r.lower()
        elif r == "BLOCKED":
            st, why = VIOLATED, "recorded against the risk policy"
        elif r == "UNKNOWN":
            st, why = UNKNOWN, "the risk check couldn't be completed (missing data)"
        elif e["imported"]:
            st, why = NA, "imported history: not risk-checked"
        else:
            st, why = UNKNOWN, "no risk check was recorded"
        add("WITHIN_POLICY", f"{n}: within the risk policy", "trade", st, why, subject=pid)
        add("WAITED", f"{n}: entered after the plan qualified", "trade",
            NA if not planned else (MET if e["converted_from"] == "QUALIFIED" else VIOLATED),
            "" if not planned or e["converted_from"] == "QUALIFIED" else f"entered while {str(e['converted_from']).lower()}",
            subject=pid)
    if snap["reviews_due"]:
        late = [names.get(d["position_id"], d["position_id"]) for d in snap["reviews_due"] if not d["completed"]]
        add("REVIEWS_ON_TIME", "No closed position waits more than 2 days for its review", "review",
            VIOLATED if late else MET, ("waiting: " + ", ".join(late[:5])) if late else f"{len(snap['reviews_due'])} reviewed")
    counted = [c for c in comps if c["state"] != NA]
    num = sum(1 for c in counted if c["state"] == MET)
    den = len(counted)
    return {"components": comps, "numerator": num, "denominator": den,
            "percentage": round(100 * num / den) if den and not rest else None,
            "unknown": sum(1 for c in counted if c["state"] == UNKNOWN)}


def _source_ids(snap: dict) -> list[str]:
    ids = set(snap["open_position_ids"]) | set(snap["daily_thesis_ids"]) | set(snap["eod_review_ids"])
    ids |= {e["position_id"] for e in snap["entries"]} | {e["plan_id"] for e in snap["entries"] if e["plan_id"]}
    ids |= {d["position_id"] for d in snap["reviews_due"]} | {d["review_id"] for d in snap["reviews_due"] if d["review_id"]}
    if snap["policy"]:
        ids.add(snap["policy"]["id"])
    return sorted(ids)


def score_day(f: Facts, day: date, points: int | None = None) -> dict:
    snap = day_snapshot(f, day)
    ev = evaluate(snap, in_progress=day == f.today)
    return {"day": day.isoformat(), "timezone": f.tz_name, "version": f"{SCORE_POLICY}/{SCORE_VERSION}",
            "rest_day": snap["rest_day"], "score": ev["percentage"], "met": ev["numerator"], "applicable": ev["denominator"],
            "unknown": ev["unknown"], "in_progress": day == f.today, "items": ev["components"], "points": points,
            "snapshot": snap, "stored": False}


# ------------------------------------------------------------------ the ledger
@dataclass
class Cand:
    code: str
    subject_type: str
    subject_id: str
    day: date
    evidence: dict
    source_ids: list[str]
    desc: str


def _candidates(f: Facts, day_scores: dict[date, int | None]) -> list[Cand]:
    """Every award the current evidence supports."""
    out: list[Cand] = []
    for p in sorted((p for p in f.positions if _live(p)), key=lambda x: (x.opened_at, x.id)):
        d = f.local(p.opened_at)
        assert d is not None
        pl = f.plans.get(p.plan_id) if p.plan_id else None
        planned = pl is not None and pl.created_at <= p.opened_at
        base = {"position_id": p.id, "opened_at": p.opened_at.isoformat()}
        if planned and pl is not None:
            ev = {**base, "plan_id": pl.id, "plan_created_at": pl.created_at.isoformat()}
            out.append(Cand("PLAN_BEFORE_ENTRY", "position", p.id, d, ev, [p.id, pl.id], p.name))
            if pl.planned_max_loss_units is not None:
                out.append(Cand("MAX_LOSS_DEFINED", "position", p.id, d, {**ev, "planned_max_loss_units": pl.planned_max_loss_units},
                                [p.id, pl.id], p.name))
            cf = (pl.comparison_json or {}).get("converted_from")
            if cf == "QUALIFIED":
                out.append(Cand("WAITED_CONFIRMATION", "position", p.id, d, {**ev, "converted_from": cf}, [p.id, pl.id], p.name))
        risk = p.entry_risk_json or {}
        if risk.get("overall") in ("PASS", "WARNING"):
            out.append(Cand("WITHIN_RISK", "position", p.id, d, {**base, "entry_risk": risk.get("overall"),
                                                                 "policy_revision": risk.get("policy_revision")}, [p.id], p.name))
        liq = [c for c in risk.get("checks", []) if c.get("code") in ("OPEN_INTEREST", "SPREAD")]
        if liq and all(c.get("result") == "PASS" for c in liq):
            out.append(Cand("LIQUID_CONTRACT", "position", p.id, d, {**base, "checks": [{"code": c["code"], "value": c.get("value"),
                                                                                        "limit": c.get("limit")} for c in liq]},
                            [p.id], p.name))
    for pl in sorted(f.plans.values(), key=lambda x: (x.created_at, x.id)):
        hist = f.plan_states.get(pl.id, [])
        if "ARMED" not in hist or pl.converted_at is not None:
            continue
        after = hist[hist.index("ARMED") + 1:]
        outcome = next((st for st in after if st in ("SKIPPED", "INVALIDATED")), None)
        if outcome:
            d = f.local(pl.state_changed_at if pl.state == outcome else pl.updated_at)
            assert d is not None
            out.append(Cand("SKIPPED_SETUP", "plan", pl.id, d, {"plan_id": pl.id, "outcome": outcome,
                                                                "reason": pl.outcome_reason}, [pl.id], pl.title))
    names = {p.id: p.name for p in f.positions}
    rule_breaks = {p.id for p in f.positions if p.rule_violation}
    for pid, r in sorted(f.reviews.items()):
        if r.completed_at is None:
            continue
        d = f.local(r.completed_at)
        assert d is not None
        pos = next((p for p in f.positions if p.id == pid), None)
        if pos is None or not _live(pos):
            continue
        ev = {"review_id": r.id, "position_id": pid, "completed_at": r.completed_at.isoformat()}
        out.append(Cand("REVIEW_DONE", "review", r.id, d, ev, [r.id, pid], names.get(pid, pid)))
        if r.exit_followed_plan == "yes" and pos.plan_id:
            out.append(Cand("FOLLOWED_EXIT", "review", r.id, d, {**ev, "exit_followed_plan": "yes", "exit_reason": r.exit_reason,
                                                                 "plan_id": pos.plan_id}, [r.id, pid, pos.plan_id], names.get(pid, pid)))
        if pid in rule_breaks and r.rule_violation_acknowledged:
            out.append(Cand("HONEST_REVIEW", "review", r.id, d, {**ev, "rule_violation_acknowledged": True},
                            [r.id, pid], names.get(pid, pid)))
    for kind, code in (("daily_thesis", "DAILY_THESIS"), ("eod_review", "EOD_REVIEW")):
        by_day: dict[date, list[str]] = {}
        for j in f.journal:
            if j.kind == kind and j.archived_at is None and j.entry_date <= f.today:
                by_day.setdefault(j.entry_date, []).append(j.id)
        for d, ids in sorted(by_day.items()):
            out.append(Cand(code, "day", d.isoformat(), d, {"journal_ids": sorted(ids)}, sorted(ids), d.isoformat()))
    for d, pct in sorted(day_scores.items()):
        if pct == 100:
            out.append(Cand("ALL_OBJECTIVES", "day", d.isoformat(), d, {"score": 100, "version": VERSIONS["process_score"]},
                            [], d.isoformat()))
    return out


def _why_gone(f: Facts, code: str, st: str, sid: str) -> str:
    """Why an award's evidence no longer holds (for the revocation)."""
    if st == "position":
        p = next((x for x in f.positions if x.id == sid), None)
        if p is None:
            return "the position no longer exists"
        if not _live(p):
            return f"position {p.name} was archived as entered in error"
        return f"the evidence for {code} no longer holds for {p.name} (its plan link or entry risk check changed)"
    if st == "plan":
        pl = f.plans.get(sid)
        return "the plan was converted into a position" if pl and pl.converted_at else "the plan's outcome changed"
    if st == "review":
        r = next((x for x in f.reviews.values() if x.id == sid), None)
        if r is None:
            return "the review no longer exists"
        p = next((x for x in f.positions if x.id == r.position_id), None)
        if p is not None and not _live(p):
            return f"position {p.name} was archived as entered in error"
        if code == "FOLLOWED_EXIT":
            return f"review edited: the exit is now marked '{r.exit_followed_plan}', not the planned one"
        if code == "HONEST_REVIEW":
            return "review edited: the rule break is no longer acknowledged"
        return "the review is no longer complete"
    if code in ("DAILY_THESIS", "EOD_REVIEW"):
        kind = "daily thesis" if code == "DAILY_THESIS" else "end-of-day review"
        gone = [j.id for j in f.journal if j.entry_date.isoformat() == sid and j.archived_at is not None
                and j.kind == ("daily_thesis" if code == "DAILY_THESIS" else "eod_review")]
        return f"the {kind} for {sid} was archived" + (f" (entry {', '.join(gone)})" if gone else "")
    if code == "ALL_OBJECTIVES":
        return f"the score for {sid} is no longer 100% (see the score revision for that day)"
    return "the evidence no longer holds"


def _net(entries: list[ProcessAward]) -> int:
    return sum(e.points for e in entries)


def _audit(s: Session, entry: ProcessAward, action: str, reason: str | None) -> str:
    a = AuditEvent(entity_type="process_award", entity_id=entry.id, action=action, reason=reason,
                   after_json={"code": entry.code, "subject": f"{entry.subject_type}:{entry.subject_id}",
                               "points": entry.points, "references": entry.references_id})
    s.add(a)
    s.flush()
    return a.id


def _new_entry(s: Session, f: Facts, entry_type: str, c: Cand | None, code: str, st: str, sid: str, day: date,
               points: int, category: str, desc: str, ref: str | None, reason: str | None, actor: str,
               evidence: dict | None = None, source_ids: list[str] | None = None) -> ProcessAward:
    from ..portfolio.ids import new_id

    backfill = f.today > day + timedelta(days=1)
    e = ProcessAward(id=new_id(), account_id=f.account_id, entry_type=entry_type, code=code, rule_version=POINTS_VERSION,
                     category=category, points=points, subject_type=st, subject_id=sid,
                     source_ids_json=source_ids if source_ids is not None else (c.source_ids if c else []),
                     evidence_json=evidence if evidence is not None else (c.evidence if c else {}), day=day,
                     timezone=f.tz_name, backfill=backfill, description=desc[:300], references_id=ref, reason=reason,
                     actor=actor, awarded_at=utcnow())
    e.audit_id = _audit(s, e, entry_type, reason)
    s.add(e)
    s.flush()
    return e


def sync_awards(s: Session, f: Facts, day_scores: dict[date, int | None], actor: str = "system:process-sync") -> dict:
    """Bring the ledger in line with the evidence. Idempotent: running it again adds nothing.
    New awards for qualifying subjects (within the daily caps); revocations when evidence stops holding;
    replacements when revoked evidence holds again. Nothing is updated or deleted."""
    rows = list(s.scalars(select(ProcessAward).where(ProcessAward.account_id == f.account_id)
                          .order_by(ProcessAward.awarded_at, ProcessAward.id)))
    by_key: dict[tuple[str, str, str], list[ProcessAward]] = {}
    for r in rows:
        by_key.setdefault((r.code, r.subject_type, r.subject_id), []).append(r)
    cands = {(c.code, c.subject_type, c.subject_id): c for c in _candidates(f, day_scores)}
    active_per_day: dict[tuple[date, str], set[str]] = {}
    for (code, st, sid), es in by_key.items():
        if _net(es) > 0:
            bucket = "position" if code in POSITION_CODES else code
            active_per_day.setdefault((es[0].day, bucket), set()).add(sid)
    added = {"award": 0, "revocation": 0, "replacement": 0}
    for key in sorted(set(cands) | {k for k, es in by_key.items() if _net(es) > 0}, key=lambda k: (cands[k].day if k in cands else date.min, k)):
        code, st, sid = key
        es = by_key.get(key, [])
        net = _net(es)
        c = cands.get(key)
        pts, cat, label = POINTS[code]
        if c is not None and net <= 0:
            bucket = "position" if code in POSITION_CODES else code
            used = active_per_day.setdefault((c.day, bucket), set())
            if bucket in ("position", "SKIPPED_SETUP") and sid not in used and len(used) >= DAILY_CAP:
                continue                                       # more trades or skips in a day never earn more
            award = next((e for e in es if e.entry_type == "award"), None)
            last_adj = next((e for e in reversed(es) if e.entry_type in ("revocation", "correction")), None)
            if award is not None and last_adj is not None and not last_adj.actor.startswith("system"):
                continue                                       # a person removed these points: never undo that automatically
            if award is None:
                _new_entry(s, f, "award", c, code, st, sid, c.day, pts, cat, f"{label}: {c.desc}", None, None, actor)
                added["award"] += 1
            else:
                last_rev = next(e for e in reversed(es) if e.entry_type == "revocation")
                _new_entry(s, f, "replacement", c, code, st, sid, award.day, award.points, award.category,
                           f"{label}: {c.desc} (restored)", last_rev.id, "the evidence holds again: "
                           + ", ".join(f"{k}={v}" for k, v in c.evidence.items() if k.endswith("_ids") or k.endswith("_id")),
                           actor)
                added["replacement"] += 1
            used.add(sid)
        elif c is None and net > 0:
            last_pos = next(e for e in reversed(es) if e.points > 0)
            why = _why_gone(f, code, st, sid)
            _new_entry(s, f, "revocation", None, code, st, sid, last_pos.day, -net, last_pos.category,
                       f"Revoked: {last_pos.description}"[:300], last_pos.id, why, actor,
                       evidence={"revokes": last_pos.id, "original_evidence": last_pos.evidence_json}, source_ids=last_pos.source_ids_json)
            added["revocation"] += 1
    s.flush()
    return added


def adjust(s: Session, f: Facts, entry_id: str, kind: str, reason: str, actor: str, delta: int | None = None) -> ProcessAward:
    """A manual revocation or correction of a ledger entry (never an edit)."""
    from ..portfolio.service import DomainError

    ref = s.get(ProcessAward, entry_id)
    if ref is None or ref.account_id != f.account_id:
        raise DomainError("NOT_FOUND", f"Ledger entry {entry_id} does not exist.", 404)
    if not reason.strip():
        raise DomainError("REASON_REQUIRED", "Every adjustment needs a reason.", 422)
    es = list(s.scalars(select(ProcessAward).where(ProcessAward.account_id == f.account_id, ProcessAward.code == ref.code,
                                                   ProcessAward.subject_type == ref.subject_type,
                                                   ProcessAward.subject_id == ref.subject_id)))
    net = _net(es)
    if kind == "revocation":
        if net <= 0:
            raise DomainError("NOTHING_TO_REVOKE", "These points are already revoked.", 409)
        pts = -net
    else:
        if delta is None or delta == 0:
            raise DomainError("BAD_DELTA", "A correction needs a non-zero delta.", 422)
        if net + delta < 0:
            raise DomainError("BAD_DELTA", "A correction can't take a subject's points below zero.", 422)
        pts = delta
    return _new_entry(s, f, kind, None, ref.code, ref.subject_type, ref.subject_id, ref.day, pts, ref.category,
                      f"{'Revoked' if kind == 'revocation' else 'Corrected'}: {ref.description}"[:300], ref.id, reason.strip(),
                      actor, evidence={"adjusts": ref.id}, source_ids=ref.source_ids_json)


# ------------------------------------------------------------------ finished days
def latest_scores(s: Session, account_id: str) -> dict[date, DailyScore]:
    out: dict[date, DailyScore] = {}
    for r in s.scalars(select(DailyScore).where(DailyScore.account_id == account_id).order_by(DailyScore.account_date,
                                                                                             DailyScore.revision)):
        out[r.account_date] = r
    return out


def _points_by_day(s: Session, account_id: str) -> dict[date, int]:
    out: dict[date, int] = {}
    for r in s.scalars(select(ProcessAward).where(ProcessAward.account_id == account_id)):
        out[r.day] = out.get(r.day, 0) + r.points
    return out


def _diff(old: DailyScore, snap: dict, ev: dict, points: int) -> str:
    before = {c["code"] + (":" + c["subject_id"] if c.get("subject_id") else ""): c for c in old.components_json}
    after = {c["code"] + (":" + c["subject_id"] if c.get("subject_id") else ""): c for c in ev["components"]}
    parts = []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b is None:
            assert a is not None
            parts.append(f"{a['label']}: added ({a['state']})")
        elif a is None:
            parts.append(f"{b['label']}: removed (was {b['state']})")
        elif a["state"] != b["state"]:
            parts.append(f"{a['label']}: {b['state']} → {a['state']}" + (f" ({a['detail']})" if a["detail"] else ""))
    if not parts and evidence_key(old.input_snapshot) != evidence_key(snap):
        parts.append("the underlying records changed (same component results)")
    if old.points != points:
        parts.append(f"points for the day {old.points} → {points}")
    return "; ".join(parts)


def finalize_days(s: Session, f: Facts, lookback: int = FINALIZE_LOOKBACK_DAYS) -> dict:
    """Store finished days that have no score yet, and add revisions for finished days whose evidence changed
    (same scoring version only). Idempotent."""
    stored = latest_scores(s, f.account_id)
    pts = _points_by_day(s, f.account_id)
    out = {"stored": 0, "revised": 0}
    start = max(f.created, f.today - timedelta(days=lookback))
    day = start
    while day < f.today:
        snap = day_snapshot(f, day)
        ev = evaluate(snap)
        p = pts.get(day, 0)
        old = stored.get(day)
        if old is None:
            s.add(DailyScore(account_id=f.account_id, account_date=day, timezone=f.tz_name, policy_code=SCORE_POLICY,
                             policy_version=SCORE_VERSION, revision=1, calculated_at=utcnow(),
                             backfill=f.today > day + timedelta(days=1), rest_day=snap["rest_day"], input_snapshot=snap,
                             components_json=ev["components"], numerator=ev["numerator"], denominator=ev["denominator"],
                             percentage=ev["percentage"], points=p, source_ids_json=_source_ids(snap)))
            out["stored"] += 1
        elif old.policy_version == SCORE_VERSION and old.policy_code == SCORE_POLICY and (
                evidence_key(old.input_snapshot) != evidence_key(snap) or old.points != p):
            s.add(DailyScore(account_id=f.account_id, account_date=day, timezone=f.tz_name, policy_code=SCORE_POLICY,
                             policy_version=SCORE_VERSION, revision=old.revision + 1, supersedes_id=old.id,
                             correction_reason=_diff(old, snap, ev, p) or "the evidence changed", calculated_at=utcnow(),
                             backfill=f.today > day + timedelta(days=1), rest_day=snap["rest_day"], input_snapshot=snap,
                             components_json=ev["components"], numerator=ev["numerator"], denominator=ev["denominator"],
                             percentage=ev["percentage"], points=p, source_ids_json=_source_ids(snap)))
            out["revised"] += 1
        day += timedelta(days=1)
    s.flush()
    return out


def score_record(r: DailyScore) -> dict:
    return {"id": r.id, "day": r.account_date.isoformat(), "timezone": r.timezone,
            "version": f"{r.policy_code}/{r.policy_version}", "revision": r.revision, "supersedes_id": r.supersedes_id,
            "correction_reason": r.correction_reason, "calculated_at": r.calculated_at, "backfill": r.backfill,
            "rest_day": r.rest_day, "score": r.percentage, "met": r.numerator, "applicable": r.denominator,
            "unknown": sum(1 for c in r.components_json if c["state"] == UNKNOWN), "points": r.points,
            "items": r.components_json, "snapshot": r.input_snapshot, "source_ids": r.source_ids_json, "stored": True,
            "in_progress": False}


# ------------------------------------------------------------------ streaks, levels, achievements
def _day_pcts(f: Facts, stored: dict[date, DailyScore]) -> dict[date, int | None]:
    """Score per day. A day stored under another scoring version keeps its stored score (never recomputed);
    otherwise the score is evaluated from the current evidence, which is exactly what the stored revision
    holds (or what the next revision will hold), so the ledger and the stored scores agree in one pass."""
    out: dict[date, int | None] = {}
    day = max(f.created, f.today - timedelta(days=FINALIZE_LOOKBACK_DAYS))
    while day <= f.today:
        r = stored.get(day)
        if r is not None and (r.policy_code, r.policy_version) != (SCORE_POLICY, SCORE_VERSION):
            out[day] = None if r.rest_day else r.percentage
        else:
            ev = evaluate(day_snapshot(f, day), in_progress=day == f.today)
            out[day] = ev["percentage"]
        day += timedelta(days=1)
    return out


def streaks(f: Facts, pcts: dict[date, int | None], rest: set[date]) -> dict:
    cur, best, run, counting = 0, 0, 0, True
    for day in sorted(pcts, reverse=True):
        if day.weekday() not in f.schedule_days or day in rest:
            continue
        pct = pcts[day]
        if pct is None:
            continue
        if pct >= STREAK_THRESHOLD:
            run += 1
            if counting:
                cur += 1
        elif day == f.today:
            continue                                     # today isn't over: it can't break the streak yet
        else:
            counting = False
            best = max(best, run)
            run = 0
    return {"current": cur, "best": max(best, run), "threshold": STREAK_THRESHOLD}


def level_for(points: int) -> dict:
    points = max(points, 0)
    lvl = 1
    while 25 * (lvl + 1) * lvl <= points:
        lvl += 1
    cur, nxt = 25 * lvl * (lvl - 1), 25 * (lvl + 1) * lvl
    return {"level": lvl, "colony_name": LEVEL_NAMES_COLONY[min(lvl, len(LEVEL_NAMES_COLONY)) - 1],
            "points_for_level": cur, "points_for_next": nxt, "progress": round((points - cur) / (nxt - cur), 3)}


def achievement_progress(f: Facts, best_streak: int) -> dict[str, tuple[int, dict]]:
    """code -> (progress towards its goal, the evidence)."""
    live = sorted((p for p in f.positions if _live(p)), key=lambda p: (p.opened_at, p.id))
    planned_run: list[str] = []
    for p in reversed(live[-5:]):
        if not p.plan_id:
            break
        planned_run.append(p.id)
    done_reviews = sorted(r.id for r in f.reviews.values() if r.completed_at)
    skips = sorted(pl.id for pl in f.plans.values() if pl.converted_at is None and "ARMED" in f.plan_states.get(pl.id, [])
                   and any(st in ("SKIPPED", "INVALIDATED") for st in f.plan_states[pl.id][f.plan_states[pl.id].index("ARMED"):]))
    recent = [p for p in live if (f.today - (f.local(p.opened_at) or f.today)).days <= 30]
    first = min((d for p in live if (d := f.local(p.opened_at)) is not None), default=None)
    breaks = [p.id for p in recent if p.rule_violation]
    month_ok = first is not None and (f.today - first).days >= 30 and bool(recent) and not breaks
    month_days = 0 if first is None or breaks else min(30, (f.today - first).days)
    opens = [p for p in live if p.closed_at is None]
    noted = {j.position_id for j in f.journal if j.position_id and j.archived_at is None}
    documented = bool(opens) and all(p.plan_id or p.id in noted for p in opens)
    stale: list[str] = []
    for p in live:
        for lg in p.legs:
            if lg.expiration and f.today - timedelta(days=30) <= lg.expiration < f.today:
                closed = f.local(p.closed_at)
                if closed is None or (closed - lg.expiration).days > 2:
                    stale.append(p.id)
    clean_days = 0 if first is None or stale else min(30, (f.today - first).days)
    exp_reviews = sorted(d.isoformat() for (d, code), o in f.manual.items() if code == "REVIEW_EXPIRATIONS" and o.done)
    honest = sorted(r.id for pid, r in f.reviews.items() if r.completed_at and r.rule_violation_acknowledged
                    and next((p.rule_violation for p in live if p.id == pid), False))
    return {"FIRST_PLAN": (1 if f.armed_plans else 0, {"armed_plan_ids": sorted(f.armed_plans)[:5]}),
            "PLANNED_FIVE": (len(planned_run), {"position_ids": planned_run}),
            "REVIEWS_TEN": (len(done_reviews), {"review_ids": done_reviews[:10]}),
            "SKIPS_FIVE": (len(skips), {"plan_ids": skips[:5]}),
            "MONTH_WITHIN_LIMITS": (30 if month_ok else month_days, {"first_entry": first.isoformat() if first else None,
                                                                     "recent_positions": len(recent), "rule_breaks": breaks}),
            "ALL_DOCUMENTED": (1 if documented else 0, {"open_position_ids": sorted(p.id for p in opens)}),
            "NO_STALE_30": (clean_days, {"left_open_past_expiry": stale}),
            "EXPIRATION_REVIEW": (1 if exp_reviews else 0, {"days": exp_reviews[:5]}),
            "STREAK_FIVE": (best_streak, {"best_streak": best_streak}),
            "HONEST_RECORD": (1 if honest else 0, {"review_ids": honest[:5]})}


def sync_achievements(s: Session, f: Facts, best_streak: int) -> list[str]:
    have = {a.code for a in s.scalars(select(UserAchievement).where(UserAchievement.account_id == f.account_id))}
    prog = achievement_progress(f, best_streak)
    new = []
    for code, _name, _desc, goal in ACHIEVEMENTS:
        if code not in have and prog[code][0] >= goal:
            s.add(UserAchievement(account_id=f.account_id, code=code, rule_version=ACHIEVEMENT_VERSION,
                                  detail_json={"progress": prog[code][0], "goal": goal, **prog[code][1]}))
            new.append(code)
    s.flush()
    return new


# ------------------------------------------------------------------ the whole refresh
def refresh(s: Session, account_id: str, tz_name: str, actor: str = "system:process-sync", now: datetime | None = None) -> dict:
    """Ledger, finished-day scores and achievements brought up to date from the records. Idempotent."""
    f = load_facts(s, account_id, tz_name, now)
    stored = latest_scores(s, account_id)
    pcts = _day_pcts(f, stored)
    ledger = sync_awards(s, f, {d: (None if (m := f.manual.get((d, REST))) and m.done else p) for d, p in pcts.items()}, actor)
    days = finalize_days(s, f)
    stored = latest_scores(s, account_id)
    pcts = _day_pcts(f, stored)
    rest = {d for (d, c), o in f.manual.items() if c == REST and o.done}
    st = streaks(f, pcts, rest)
    new_ach = sync_achievements(s, f, st["best"])
    return {"ledger": ledger, "days": days, "achievements": new_ach, "streak": st, "facts": f}


def points_summary(s: Session, account_id: str) -> dict:
    rows = list(s.scalars(select(ProcessAward).where(ProcessAward.account_id == account_id)))
    awarded = sum(r.points for r in rows if r.entry_type in ("award", "replacement"))
    adjusted = sum(r.points for r in rows if r.entry_type in ("revocation", "correction"))
    cats = {c: sum(r.points for r in rows if r.category == c) for c in ("discipline", "research", "risk_control", "review")}
    return {"lifetime_awarded": awarded, "revoked_or_corrected": adjusted, "effective": awarded + adjusted,
            "total": awarded + adjusted, "by_category": cats,
            "entries": {k: sum(1 for r in rows if r.entry_type == k) for k in ("award", "revocation", "correction", "replacement")}}


def ledger_out(r: ProcessAward) -> dict:
    return {"id": r.id, "entry_type": r.entry_type, "code": r.code, "rule_version": r.rule_version, "category": r.category,
            "points": r.points, "subject_type": r.subject_type, "subject_id": r.subject_id, "source_ids": r.source_ids_json,
            "evidence": r.evidence_json, "day": r.day.isoformat(), "timezone": r.timezone, "backfill": r.backfill,
            "description": r.description, "references_id": r.references_id, "reason": r.reason, "actor": r.actor,
            "audit_id": r.audit_id, "awarded_at": r.awarded_at}


def progress(s: Session, account_id: str, tz_name: str, now: datetime | None = None, write: bool = True) -> dict[str, Any]:
    if write:
        res = refresh(s, account_id, tz_name, now=now)
        f, st, new_ach, ledger = res["facts"], res["streak"], res["achievements"], res["ledger"]
    else:
        f = load_facts(s, account_id, tz_name, now)
        pcts = _day_pcts(f, latest_scores(s, account_id))
        st = streaks(f, pcts, {d for (d, c), o in f.manual.items() if c == REST and o.done})
        new_ach, ledger = [], {}
    pts = points_summary(s, account_id)
    earned = {a.code: a for a in s.scalars(select(UserAchievement).where(UserAchievement.account_id == account_id))}
    prog = achievement_progress(f, st["best"])
    lvl = level_for(pts["effective"])
    recent = list(s.scalars(select(ProcessAward).where(ProcessAward.account_id == account_id)
                            .order_by(ProcessAward.awarded_at.desc(), ProcessAward.id.desc()).limit(30)))
    today = score_day(f, f.today, _points_by_day(s, account_id).get(f.today, 0))
    return {"versions": VERSIONS, "points": pts, "level": lvl, "streak": st,
            "achievements": [{"code": c, "name": n, "description": d, "goal": g, "progress": min(prog[c][0], g),
                              "earned_at": earned[c].earned_at if c in earned else None,
                              "rule_version": earned[c].rule_version if c in earned else ACHIEVEMENT_VERSION,
                              "evidence": earned[c].detail_json if c in earned else None} for c, n, d, g in ACHIEVEMENTS],
            "recent_awards": [ledger_out(a) for a in recent],
            "themes": [{"id": k, "unlock_level": v, "unlocked": lvl["level"] >= v} for k, v in THEMES.items()],
            "new": {"ledger": ledger, "achievements": new_ach}, "today": today}


def snapshot_copy(x: dict) -> dict:
    return copy.deepcopy(x)
