"""Process scoring, points, levels, streaks and achievements. Rewards process, never profit.

What is scored
    Daily objectives (the discipline panel), each met / not met / not applicable:
        REVIEW_POSITIONS         you reviewed your open positions                 (manual; n/a with none open)
        REVIEW_EXPIRATIONS       you reviewed expiration risk                     (manual; n/a with nothing expiring in 14 days)
        CHECK_DATA               you confirmed the data sources are working       (manual)
        DAILY_THESIS             a daily market thesis is in the journal          (automatic)
        MAX_DAILY_RISK           your policy has a maximum daily loss              (automatic)
        INVALIDATION_BEFORE_ENTRY every position opened that day came from a plan with an invalidation
                                                                                   (automatic; n/a with no entries)
        REVIEW_EVENTS            you reviewed the day's scheduled events            (manual)
        EOD_REVIEW               an end-of-day review is in the journal            (automatic)
    For each position opened that day: planned before entry, maximum loss defined in the plan, within the
    risk policy at entry, entered after the plan qualified (each n/a when it can't apply).
    Reviews: no closed position waits more than 2 days for its review.
    Daily process score = met / applicable × 100. A day with no trades has the trade items n/a, so it can
    score 100. A rest day (you mark it) is not scored and doesn't break a streak.

What is never an input
    Profit or loss, position size, the number of trades, time spent on screen. Nothing here reads realized
    or open P&L (tests enforce it). Points never decrease: a rule broken earns nothing (and asks for a
    review), so hiding a losing trade can never pay.

Points (the ledger, process_awards)
    PLAN_BEFORE_ENTRY 10 · MAX_LOSS_DEFINED 10 · WITHIN_RISK 10 · LIQUID_CONTRACT 5 · WAITED_CONFIRMATION 10 ·
    SKIPPED_SETUP 10 (an armed plan skipped or invalidated, never converted) · REVIEW_DONE 10 ·
    FOLLOWED_EXIT 10 · HONEST_REVIEW 5 (reviewing a position recorded against the policy) ·
    DAILY_THESIS 5 · EOD_REVIEW 5 · ALL_OBJECTIVES 10 (a day at 100%).
    Caps per day: position items count for at most 3 positions, skipped setups for at most 3 plans, so
    trading (or skipping) more never earns more.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..portfolio.models import (Account, AuditEvent, DailyObjective, JournalEntry, Position, ProcessAward, RiskPolicy,
                                TradePlan, TradeReview, UserAchievement, utcnow)

MET, NOT_MET, NA, PENDING = "met", "not_met", "na", "pending"
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


# ------------------------------------------------------------------ facts (loaded once per range)
@dataclass
class Facts:
    account_id: str
    tz: ZoneInfo
    today: date
    created: date
    positions: list[Position]
    plans: dict[str, TradePlan]
    reviews: dict[str, TradeReview]             # by position id
    journal: list[JournalEntry]
    manual: dict[tuple[date, str], DailyObjective]
    policies: list[RiskPolicy]
    armed_plans: set[str]                       # plans that were ever armed
    schedule_days: set[int] = field(default_factory=lambda: {0, 1, 2, 3, 4})

    def local(self, dt: datetime | None) -> date | None:
        return None if dt is None else dt.astimezone(self.tz).date()

    def window(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time(0), self.tz)
        return start, start + timedelta(days=1)


def load_facts(s: Session, account_id: str, tz_name: str, now: datetime | None = None) -> Facts:
    now = now or utcnow()
    tz = ZoneInfo(tz_name)
    acc = s.get(Account, account_id)
    positions = list(s.scalars(select(Position).options(selectinload(Position.legs)).where(Position.account_id == account_id)))
    plans = {p.id: p for p in s.scalars(select(TradePlan).where(TradePlan.account_id == account_id))}
    reviews = {r.position_id: r for r in s.scalars(select(TradeReview).where(TradeReview.account_id == account_id))}
    journal = list(s.scalars(select(JournalEntry).where(JournalEntry.account_id == account_id,
                                                        JournalEntry.archived_at.is_(None))))
    manual = {(o.day, o.code): o for o in s.scalars(select(DailyObjective).where(DailyObjective.account_id == account_id))}
    policies = list(s.scalars(select(RiskPolicy).where(RiskPolicy.account_id == account_id).order_by(RiskPolicy.revision)))
    armed = set()
    if plans:
        for a in s.scalars(select(AuditEvent).where(AuditEvent.entity_type == "plan", AuditEvent.action == "state",
                                                    AuditEvent.entity_id.in_(list(plans)))):
            if (a.after_json or {}).get("state") == "ARMED":
                armed.add(a.entity_id)
    from ..monitoring import settings as msettings
    from ..monitoring.settings import DAYS

    sched = {DAYS.index(d) for d in msettings.load(s).schedule.days}
    created = acc.created_at.astimezone(tz).date() if acc else now.astimezone(tz).date()
    return Facts(account_id, tz, now.astimezone(tz).date(), created, positions, plans, reviews, journal, manual, policies,
                 armed, sched)


# ------------------------------------------------------------------ daily score
@dataclass
class Item:
    code: str
    label: str
    group: str                      # objective | trade | review
    status: str
    detail: str = ""
    kind: str = "auto"              # auto | manual
    subject_id: str | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _policy_at(f: Facts, end: datetime) -> RiskPolicy | None:
    cur = None
    for p in f.policies:
        if p.created_at < end and (p.superseded_at is None or p.superseded_at >= end):
            cur = p
    return cur


def _opened_on(f: Facts, day: date) -> list[Position]:
    return [p for p in f.positions if f.local(p.opened_at) == day and p.archive_reason != "entered_in_error"]


def _open_during(f: Facts, day: date) -> list[Position]:
    start, end = f.window(day)
    return [p for p in f.positions if p.opened_at < end and (p.closed_at is None or p.closed_at >= start)
            and p.archive_reason != "entered_in_error"]


def score_day(f: Facts, day: date) -> dict:
    """Pure: the day's components and score from the loaded facts. Reads no profit or loss."""
    start, end = f.window(day)
    is_today = day == f.today
    rest = (day, REST) in f.manual and f.manual[(day, REST)].done
    items: list[Item] = []

    def obj(code: str, status: str, detail: str = "", kind: str = "auto"):
        label = next(lbl for c, lbl, _ in OBJECTIVES if c == code)
        if rest:
            status, detail = NA, "rest day"
        elif status == NOT_MET and is_today:
            status = PENDING
        items.append(Item(code, label, "objective", status, detail, kind))

    def manual(code: str, applicable: bool = True, why_na: str = ""):
        if not applicable:
            return obj(code, NA, why_na, "manual")
        m = f.manual.get((day, code))
        obj(code, MET if m and m.done else NOT_MET, (m.note or "") if m else "", "manual")

    open_ = _open_during(f, day)
    manual("REVIEW_POSITIONS", bool(open_), "no open positions")
    soon = [p for p in open_ if any(lg.expiration and 0 <= (lg.expiration - day).days <= 14 for lg in p.legs)]
    manual("REVIEW_EXPIRATIONS", bool(soon), "nothing expires within 14 days")
    manual("CHECK_DATA")
    jt = [j for j in f.journal if j.entry_date == day and j.kind == "daily_thesis"]
    obj("DAILY_THESIS", MET if jt else NOT_MET, jt[0].title or "written" if jt else "")
    pol = _policy_at(f, end)
    daily = (pol.settings_json or {}).get("max_daily_realized_loss") if pol else None
    obj("MAX_DAILY_RISK", MET if daily else NOT_MET, f"maximum daily loss ${daily}" if daily else
        "set a maximum daily realized loss in the risk policy")
    entries = _opened_on(f, day)
    if not entries:
        obj("INVALIDATION_BEFORE_ENTRY", NA, "no positions opened")
    else:
        bad = [p.name for p in entries if not (p.plan_id and (pl := f.plans.get(p.plan_id)) and pl.created_at <= p.opened_at
                                               and (pl.invalidation or pl.invalidation_conditions
                                                    or pl.invalidation_price_units is not None))]
        obj("INVALIDATION_BEFORE_ENTRY", NOT_MET if bad else MET, ("no written invalidation: " + ", ".join(bad)) if bad else
            f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, all with an invalidation")
    manual("REVIEW_EVENTS")
    eod = [j for j in f.journal if j.entry_date == day and j.kind == "eod_review"]
    obj("EOD_REVIEW", MET if eod else NOT_MET, "written" if eod else "")

    for p in entries:                                       # per-entry process items
        pl = f.plans.get(p.plan_id) if p.plan_id else None
        planned = pl is not None and pl.created_at <= p.opened_at
        items.append(Item("PLANNED", f"{p.name}: planned before entry", "trade", MET if planned else NOT_MET,
                          "" if planned else "recorded without a plan", subject_id=p.id))
        items.append(Item("MAX_LOSS_DEFINED", f"{p.name}: maximum loss defined in the plan", "trade",
                          NA if not planned else (MET if pl and pl.planned_max_loss_units is not None else NOT_MET),
                          "" if planned else "no plan", subject_id=p.id))
        risk = (p.entry_risk_json or {}).get("overall")
        items.append(Item("WITHIN_POLICY", f"{p.name}: within the risk policy", "trade",
                          NA if risk in (None, "UNKNOWN") else (NOT_MET if risk == "BLOCKED" else MET),
                          {None: "not checked", "UNKNOWN": "couldn't be checked"}.get(risk, risk.lower() if risk else ""),
                          subject_id=p.id))
        waited = pl is not None and (pl.comparison_json or {}).get("converted_from") == "QUALIFIED"
        items.append(Item("WAITED", f"{p.name}: entered after the plan qualified", "trade",
                          NA if not planned else (MET if waited else NOT_MET), subject_id=p.id))

    due = [p for p in f.positions if p.closed_at is not None and p.archive_reason != "entered_in_error"
           and p.closed_at <= end - timedelta(days=2)]
    if due:
        late = [p.name for p in due if not ((r := f.reviews.get(p.id)) and r.completed_at and r.completed_at < end)]
        items.append(Item("REVIEWS_ON_TIME", "No closed position waits more than 2 days for its review", "review",
                          NOT_MET if late else MET, ("waiting: " + ", ".join(late[:5])) if late else f"{len(due)} reviewed"))
    applicable = [i for i in items if i.status != NA]
    met = sum(1 for i in applicable if i.status == MET)
    score = round(100 * met / len(applicable)) if applicable and not rest else None
    return {"day": day.isoformat(), "rest_day": rest, "score": score, "met": met, "applicable": len(applicable),
            "in_progress": is_today, "items": [i.as_dict() for i in items]}


# ------------------------------------------------------------------ streaks
def streaks(f: Facts, max_days: int = 400) -> dict:
    cur, best, run, counting = 0, 0, 0, True
    day = f.today
    for _ in range(max_days):
        if day < f.created:
            break
        if day.weekday() in f.schedule_days:
            sc = score_day(f, day)
            if sc["rest_day"] or sc["score"] is None:
                pass
            elif sc["score"] >= STREAK_THRESHOLD:
                run += 1
                if counting:
                    cur += 1
            elif day == f.today:
                pass                                    # today isn't over: it can't break the streak yet
            else:
                counting = False
                best = max(best, run)
                run = 0
        day -= timedelta(days=1)
    return {"current": cur, "best": max(best, run), "threshold": STREAK_THRESHOLD}


# ------------------------------------------------------------------ points ledger
def _award_candidates(f: Facts, score_fn: Callable[[date], dict]) -> list[tuple[str, str, str, date, str]]:
    """(code, subject_type, subject_id, day, description) for every fact that earns points."""
    out: list[tuple[str, str, str, date, str]] = []
    for p in sorted(f.positions, key=lambda x: x.opened_at):
        if p.archive_reason == "entered_in_error":
            continue
        d = f.local(p.opened_at)
        assert d is not None
        pl = f.plans.get(p.plan_id) if p.plan_id else None
        planned = pl is not None and pl.created_at <= p.opened_at
        if planned:
            out.append(("PLAN_BEFORE_ENTRY", "position", p.id, d, p.name))
            if pl is not None and pl.planned_max_loss_units is not None:
                out.append(("MAX_LOSS_DEFINED", "position", p.id, d, p.name))
            if pl is not None and (pl.comparison_json or {}).get("converted_from") == "QUALIFIED":
                out.append(("WAITED_CONFIRMATION", "position", p.id, d, p.name))
        risk = p.entry_risk_json or {}
        if risk.get("overall") in ("PASS", "WARNING"):
            out.append(("WITHIN_RISK", "position", p.id, d, p.name))
        liq = [c for c in risk.get("checks", []) if c.get("code") in ("OPEN_INTEREST", "SPREAD")]
        if liq and all(c.get("result") == "PASS" for c in liq):
            out.append(("LIQUID_CONTRACT", "position", p.id, d, p.name))
    for pl in sorted(f.plans.values(), key=lambda x: x.state_changed_at):
        if pl.state in ("SKIPPED", "INVALIDATED") and pl.id in f.armed_plans and pl.converted_at is None:
            d = f.local(pl.state_changed_at)
            assert d is not None
            out.append(("SKIPPED_SETUP", "plan", pl.id, d, pl.title))
    for r in f.reviews.values():
        if r.completed_at is None:
            continue
        d = f.local(r.completed_at)
        assert d is not None
        pos = next((p for p in f.positions if p.id == r.position_id), None)
        name = pos.name if pos else r.position_id
        out.append(("REVIEW_DONE", "review", r.id, d, name))
        if r.exit_followed_plan == "yes" and pos is not None and pos.plan_id:
            out.append(("FOLLOWED_EXIT", "review", r.id, d, name))
        if pos is not None and pos.rule_violation and r.rule_violation_acknowledged:
            out.append(("HONEST_REVIEW", "review", r.id, d, name))
    for kind, code in (("daily_thesis", "DAILY_THESIS"), ("eod_review", "EOD_REVIEW")):
        for day in sorted({j.entry_date for j in f.journal if j.kind == kind and j.entry_date <= f.today}):
            out.append((code, "day", day.isoformat(), day, day.isoformat()))
    active = {d for (d, _) in f.manual} | {j.entry_date for j in f.journal}
    for day in sorted(x for x in active if f.today - timedelta(days=60) <= x <= f.today):
        sc = score_fn(day)
        if sc["score"] == 100:
            out.append(("ALL_OBJECTIVES", "day", day.isoformat(), day, day.isoformat()))
    return out


def sync_awards(s: Session, f: Facts) -> int:
    """Insert the awards the facts earn that aren't in the ledger yet (idempotent; never removes any)."""
    have = {(a.code, a.subject_type, a.subject_id) for a in s.scalars(select(ProcessAward)
                                                                       .where(ProcessAward.account_id == f.account_id))}
    per_day: dict[tuple[date, str], set[str]] = {}
    for a in s.scalars(select(ProcessAward).where(ProcessAward.account_id == f.account_id)):
        bucket = "position" if a.code in POSITION_CODES else a.code
        per_day.setdefault((a.day, bucket), set()).add(a.subject_id)
    added = 0
    for code, st, sid, day, desc in _award_candidates(f, lambda d: score_day(f, d)):
        if (code, st, sid) in have:
            continue
        bucket = "position" if code in POSITION_CODES else code
        used = per_day.setdefault((day, bucket), set())
        if bucket in ("position", "SKIPPED_SETUP") and sid not in used and len(used) >= DAILY_CAP:
            continue                                     # more trades or skips in a day never earn more
        used.add(sid)
        pts, cat, label = POINTS[code]
        s.add(ProcessAward(account_id=f.account_id, code=code, category=cat, points=pts, subject_type=st, subject_id=sid,
                           day=day, description=f"{label}: {desc}"[:300]))
        have.add((code, st, sid))
        added += 1
    s.flush()
    return added


# ------------------------------------------------------------------ levels and achievements
def level_for(points: int) -> dict:
    lvl = 1
    while 25 * (lvl + 1) * lvl <= points:
        lvl += 1
    cur, nxt = 25 * lvl * (lvl - 1), 25 * (lvl + 1) * lvl
    return {"level": lvl, "colony_name": LEVEL_NAMES_COLONY[min(lvl, len(LEVEL_NAMES_COLONY)) - 1],
            "points_for_level": cur, "points_for_next": nxt, "progress": round((points - cur) / (nxt - cur), 3)}


def achievement_progress(f: Facts, best_streak: int) -> dict[str, tuple[int, dict]]:
    """code -> (progress count towards its goal, detail)."""
    live = [p for p in f.positions if p.archive_reason != "entered_in_error"]
    last5 = sorted(live, key=lambda p: p.opened_at)[-5:]
    planned_run = 0
    for p in reversed(last5):
        if p.plan_id:
            planned_run += 1
        else:
            break
    reviews = sum(1 for r in f.reviews.values() if r.completed_at)
    skips = sum(1 for pl in f.plans.values() if pl.state in ("SKIPPED", "INVALIDATED") and pl.id in f.armed_plans
                and pl.converted_at is None)
    recent = [p for p in live if (f.today - (f.local(p.opened_at) or f.today)).days <= 30]
    first = min((d for p in live if (d := f.local(p.opened_at)) is not None), default=None)
    month_ok = (first is not None and (f.today - first).days >= 30 and bool(recent) and not any(p.rule_violation for p in recent))
    month_days = 0 if first is None else min(30, (f.today - first).days) if not any(p.rule_violation for p in recent) else 0
    opens = [p for p in live if p.closed_at is None]
    noted = {j.position_id for j in f.journal if j.position_id}
    documented = bool(opens) and all(p.plan_id or p.id in noted for p in opens)
    stale = 0
    for p in live:
        for lg in p.legs:
            if lg.expiration and f.today - timedelta(days=30) <= lg.expiration < f.today:
                closed = f.local(p.closed_at)
                if closed is None or (closed - lg.expiration).days > 2:
                    stale += 1
    clean_days = 0 if first is None or stale else min(30, (f.today - first).days)
    exp_review = any(code == "REVIEW_EXPIRATIONS" and o.done for (_, code), o in f.manual.items())
    honest = any(r.completed_at and r.rule_violation_acknowledged and next((p.rule_violation for p in live if p.id == pid), False)
                 for pid, r in f.reviews.items())
    return {"FIRST_PLAN": (1 if f.armed_plans else 0, {}), "PLANNED_FIVE": (planned_run, {}),
            "REVIEWS_TEN": (reviews, {}), "SKIPS_FIVE": (skips, {}),
            "MONTH_WITHIN_LIMITS": (30 if month_ok else month_days, {}),
            "ALL_DOCUMENTED": (1 if documented else 0, {"open": len(opens)}),
            "NO_STALE_30": (clean_days, {"left_open_past_expiry": stale}),
            "EXPIRATION_REVIEW": (1 if exp_review else 0, {}), "STREAK_FIVE": (best_streak, {}),
            "HONEST_RECORD": (1 if honest else 0, {})}


def sync_achievements(s: Session, f: Facts, best_streak: int) -> list[str]:
    have = {a.code for a in s.scalars(select(UserAchievement).where(UserAchievement.account_id == f.account_id))}
    prog = achievement_progress(f, best_streak)
    new = []
    for code, _name, _desc, goal in ACHIEVEMENTS:
        if code not in have and prog[code][0] >= goal:
            s.add(UserAchievement(account_id=f.account_id, code=code, detail_json=prog[code][1]))
            new.append(code)
    s.flush()
    return new


def progress(s: Session, account_id: str, tz_name: str, now: datetime | None = None, write: bool = True) -> dict[str, Any]:
    f = load_facts(s, account_id, tz_name, now)
    st = streaks(f)
    new_awards, new_ach = (sync_awards(s, f), sync_achievements(s, f, st["best"])) if write else (0, [])
    awards = list(s.scalars(select(ProcessAward).where(ProcessAward.account_id == account_id)
                            .order_by(ProcessAward.awarded_at.desc(), ProcessAward.id.desc())))
    total = sum(a.points for a in awards)
    cats = {c: sum(a.points for a in awards if a.category == c) for c in ("discipline", "research", "risk_control", "review")}
    earned = {a.code: a.earned_at for a in s.scalars(select(UserAchievement).where(UserAchievement.account_id == account_id))}
    prog = achievement_progress(f, st["best"])
    lvl = level_for(total)
    return {"points": {"total": total, "by_category": cats}, "level": lvl, "streak": st,
            "achievements": [{"code": c, "name": n, "description": d, "goal": g, "progress": min(prog[c][0], g),
                              "earned_at": earned.get(c)} for c, n, d, g in ACHIEVEMENTS],
            "recent_awards": [{"code": a.code, "category": a.category, "points": a.points, "description": a.description,
                               "day": a.day.isoformat(), "awarded_at": a.awarded_at} for a in awards[:20]],
            "themes": [{"id": k, "unlock_level": v, "unlocked": lvl["level"] >= v} for k, v in THEMES.items()],
            "new": {"awards": new_awards, "achievements": new_ach}, "today": score_day(f, f.today)}
