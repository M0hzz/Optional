"""Trade plans (missions): written before a position exists, checked against the risk policy, watched by the
monitor, and converted into a position with a planned-versus-actual comparison.

States and who moves them
    DRAFT        being written; editable. Arm it when the plan is complete and the risk check isn't BLOCKED.
    ARMED        complete; the monitor watches its trigger.
    WAITING      the trigger holds; waiting for the confirmation conditions (or for you to confirm).
    QUALIFIED    trigger and confirmation hold: entering now follows the plan.
    INVALIDATED  the invalidation happened (monitor or you).
    CONVERTED    became a position (convert). The plan is kept unchanged.
    SKIPPED      you decided not to take it (a reason is required). Skipping is a legitimate outcome.
    EXPIRED      its expiry time passed first.
    ARCHIVED     put away; read-only.
Editing is allowed in DRAFT only: move an armed plan back to DRAFT to change it, then arm it again (so the
risk check always matches what's armed). Every change of state is an event; every edit is in the audit log.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..monitoring.events import emit
from ..monitoring.rules import split_conditions, validate as validate_condition
from ..portfolio import recognize as rz
from ..portfolio import service as svc
from ..portfolio.models import AuditEvent, MonitorState, Position, TradePlan, utcnow
from ..portfolio.money import fmt, from_units, to_units
from ..portfolio.schemas import LegIn, PositionIn
from . import policy as pol
from . import riskcheck as rc
from .schemas import PlanConvert, PlanCreate, PlanFields, PlanTransition, PlanUpdate

TERMINAL = {"INVALIDATED", "CONVERTED", "SKIPPED", "EXPIRED", "ARCHIVED"}
LIVE = ("ARMED", "WAITING", "QUALIFIED")
ALLOWED: dict[str, set[str]] = {
    "DRAFT": {"ARMED", "SKIPPED", "ARCHIVED"},
    "ARMED": {"DRAFT", "WAITING", "QUALIFIED", "INVALIDATED", "SKIPPED", "EXPIRED", "ARCHIVED"},
    "WAITING": {"DRAFT", "ARMED", "QUALIFIED", "INVALIDATED", "SKIPPED", "EXPIRED", "ARCHIVED"},
    "QUALIFIED": {"DRAFT", "ARMED", "WAITING", "INVALIDATED", "SKIPPED", "EXPIRED", "ARCHIVED"},
    "INVALIDATED": {"ARCHIVED"}, "SKIPPED": {"ARCHIVED"}, "EXPIRED": {"ARCHIVED"}, "CONVERTED": {"ARCHIVED"},
    "ARCHIVED": set(),
}
LABEL = {"DRAFT": "Draft", "ARMED": "Armed", "WAITING": "Waiting", "QUALIFIED": "Qualified", "INVALIDATED": "Invalidated",
         "CONVERTED": "Converted to position", "SKIPPED": "Skipped", "EXPIRED": "Expired", "ARCHIVED": "Archived"}
MONEY = {"entry_zone_low": "entry_zone_low_units", "entry_zone_high": "entry_zone_high_units",
         "max_entry_debit": "max_entry_debit_units", "min_entry_credit": "min_entry_credit_units",
         "target_price": "target_price_units", "invalidation_price": "invalidation_price_units",
         "planned_max_loss": "planned_max_loss_units"}
PLAIN = ("thesis", "direction", "intended_strategy", "market_regime", "setup_type", "trigger", "trigger_conditions",
         "confirmation", "confirmation_conditions", "max_spread_pct", "min_volume", "min_open_interest", "target",
         "invalidation", "invalidation_conditions", "planned_contracts", "expiration_from", "expiration_to", "delta_min",
         "delta_max", "time_stop", "event_risks", "related_news", "chart_reference", "notes", "expires_at")
COND_FIELDS = ("trigger_conditions", "confirmation_conditions", "invalidation_conditions")


def _err(code: str, msg: str, status: int = 409, **kw) -> svc.DomainError:
    return svc.DomainError(code, msg, status, **kw)


# ------------------------------------------------------------------ fields
def fields_of(p: TradePlan) -> dict[str, Any]:
    out: dict[str, Any] = {k: getattr(p, k) for k in PLAIN}
    for k, col in MONEY.items():
        out[k] = fmt(from_units(getattr(p, col)))
    for k in ("expiration_from", "expiration_to", "time_stop"):
        out[k] = out[k].isoformat() if out[k] else None
    out["expires_at"] = p.expires_at.isoformat() if p.expires_at else None
    return out


def _apply(p: TradePlan, data: PlanFields, only_set: bool) -> None:
    keys = data.model_fields_set if only_set else set(PlanFields.model_fields)
    for k in keys:
        v = getattr(data, k)
        if k in MONEY:
            setattr(p, MONEY[k], None if v is None else to_units(v, k))
        elif k == "legs":
            p.legs_json = [] if v is None else [lg.model_dump(mode="json") for lg in v]
        elif k == "title":
            if v:
                p.title = v
        elif k in PLAIN:
            setattr(p, k, v)
    for f in COND_FIELDS:
        bad = [e for e in (validate_condition(c, "symbol") for c in split_conditions(getattr(p, f))) if e]
        if bad:
            raise svc.DomainError("BAD_CONDITION", "A condition isn't valid.", 422,
                                  field_errors=[{"field": f, "code": "BAD_CONDITION", "message": e} for e in bad])


def planned_legs(p: TradePlan) -> list[LegIn]:
    return [LegIn.model_validate(x) for x in (p.legs_json or [])]


def _audit(s: Session, p: TradePlan, action: str, before: dict | None, reason: str | None = None) -> None:
    s.add(AuditEvent(entity_type="plan", entity_id=p.id, action=action, before_json=before,
                     after_json={**fields_of(p), "state": p.state, "legs": p.legs_json}, reason=reason))


# ------------------------------------------------------------------ CRUD
def create(s: Session, data: PlanCreate) -> TradePlan:
    acc = svc.get_account(s, data.account_id)
    if acc.archived_at is not None:
        raise _err("ACCOUNT_ARCHIVED", "This account is archived.")
    title = data.title or f"{data.symbol} {rz.NAMES.get(data.intended_strategy or '', 'plan').lower()}"
    p = TradePlan(account_id=acc.id, symbol=data.symbol, title=title, state="DRAFT", state_changed_at=utcnow(),
                  legs_json=[], metadata_json={})
    _apply(p, data, only_set=False)
    s.add(p)
    s.flush()
    _audit(s, p, "create", None)
    emit(s, "PLAN_CREATED", "info", f"Plan drafted: {p.title}", f"{p.symbol}: {p.thesis or 'no thesis yet'}",
         symbol=p.symbol, account_id=p.account_id, source={"plan_id": p.id})
    return p


def get(s: Session, plan_id: str) -> TradePlan:
    p = s.get(TradePlan, plan_id)
    if p is None:
        raise svc.DomainError("NOT_FOUND", f"Plan {plan_id} does not exist.", 404)
    return p


def list_plans(s: Session, account_id: str | None = None, states: list[str] | None = None, symbol: str | None = None,
               page: int = 1, page_size: int = 50) -> tuple[list[TradePlan], int]:
    q = select(TradePlan)
    if account_id:
        q = q.where(TradePlan.account_id == account_id)
    if states:
        q = q.where(TradePlan.state.in_(states))
    if symbol:
        q = q.where(TradePlan.symbol == symbol.upper())
    total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = q.order_by(TradePlan.updated_at.desc(), TradePlan.id)
    return list(s.scalars(q.offset((page - 1) * page_size).limit(page_size))), int(total)


def update(s: Session, plan_id: str, data: PlanUpdate) -> TradePlan:
    p = get(s, plan_id)
    svc._check_version(p, data.version)
    if p.state != "DRAFT":
        raise _err("PLAN_NOT_DRAFT", f"This plan is {LABEL[p.state].lower()}. Only drafts can be edited: move it back "
                                     "to Draft first (then arm it again)." if p.state in LIVE else
                                     f"This plan is {LABEL[p.state].lower()} and read-only. Duplicate it to start a new one.")
    before = {**fields_of(p), "legs": p.legs_json}
    _apply(p, data, only_set=True)
    p.updated_at = utcnow()
    s.flush()
    _audit(s, p, "edit", before)
    return p


def duplicate(s: Session, plan_id: str) -> TradePlan:
    src = get(s, plan_id)
    p = TradePlan(account_id=src.account_id, symbol=src.symbol, title=f"{src.title} (copy)"[:160], state="DRAFT",
                  state_changed_at=utcnow(), legs_json=list(src.legs_json or []), metadata_json={"copied_from": src.id})
    for k in PLAIN:
        setattr(p, k, getattr(src, k))
    for col in MONEY.values():
        setattr(p, col, getattr(src, col))
    p.expires_at = None
    s.add(p)
    s.flush()
    _audit(s, p, "create", None, f"copy of plan {src.id}")
    return p


# ------------------------------------------------------------------ validation and risk
@dataclass
class Item:
    item: str
    ok: bool
    level: str          # error | warning
    detail: str


def validate(s: Session, p: TradePlan, today: date | None = None) -> dict:
    today = today or date.today()
    items: list[Item] = []

    def need(ok: object, item: str, detail: str, level: str = "error"):
        items.append(Item(item, bool(ok), level, detail))
    need(p.thesis, "Thesis", "Why this trade: what you expect and why.")
    need(p.direction, "Direction", "Bullish, bearish, neutral or volatility.")
    need(p.intended_strategy, "Strategy", "The structure you intend to use.")
    need(p.trigger or p.trigger_conditions or p.entry_zone_low_units is not None, "Trigger",
         "What has to happen before you enter (text, conditions the monitor checks, or an entry zone).")
    need(p.confirmation or p.confirmation_conditions, "Confirmation", "What confirms the trigger.", "warning")
    need(p.invalidation or p.invalidation_conditions or p.invalidation_price_units is not None, "Invalidation",
         "What would prove the idea wrong, written before entry.")
    need(p.planned_max_loss_units is not None, "Planned maximum loss", "The most you accept losing, in dollars.")
    need(p.planned_contracts, "Planned size", "How many contracts (or spread units).")
    is_option = p.intended_strategy not in ("LONG_STOCK", "SHORT_STOCK", None)
    if is_option:
        need(p.expiration_from or p.expiration_to or p.legs_json, "Expiration range", "Which expirations you'll use.")
    need(p.target or p.target_price_units is not None, "Target", "Where you'd take profit.", "warning")
    need(p.max_entry_debit_units is not None or p.min_entry_credit_units is not None or p.entry_zone_low_units is not None,
         "Entry price limit", "Maximum debit or minimum credit you'll accept (or an entry zone).", "warning")
    need(p.expires_at is not None, "Plan expiry", "When the plan stops being valid; without it, it stays armed.", "warning")
    if p.expires_at is not None and p.expires_at <= utcnow() and p.state == "DRAFT":
        need(False, "Plan expiry", "The expiry time has already passed.")
    if p.expiration_to and p.expiration_to < today:
        need(False, "Expiration range", "The latest expiration is in the past.")
    if p.legs_json:
        legs = planned_legs(p)
        a = svc.analyze_legs(legs, p.symbol, svc.context_for(s, p.account_id, p.symbol, False, None))
        for e in a.errors:
            need(False, "Planned legs", e.message)
        if not a.errors:
            if p.intended_strategy and a.strategy != p.intended_strategy:
                need(False, "Planned legs", f"The legs form a {a.name}, not the intended "
                     f"{rz.NAMES.get(p.intended_strategy, p.intended_strategy)}.", "warning")
            if p.planned_max_loss_units is not None and a.max_loss.state == "bounded" and a.max_loss.value is not None \
                    and a.max_loss.value > from_units(p.planned_max_loss_units):
                need(False, "Planned legs", f"The legs can lose {rc.usd(a.max_loss.value)}, more than the planned "
                     f"{rc.usd(from_units(p.planned_max_loss_units))}.", "warning")
            if a.max_loss.state != "bounded":
                need(False, "Planned legs", f"The legs' maximum loss is {a.max_loss.state}.", "warning")
    errors = [i for i in items if not i.ok and i.level == "error"]
    return {"ready": not errors, "errors": [i.__dict__ for i in errors],
            "warnings": [i.__dict__ for i in items if not i.ok and i.level == "warning"],
            "checklist": [i.__dict__ for i in items]}


def leg_quotes(quotes, underlying: str, legs: list[LegIn]) -> dict | None:
    """Quotes and Greeks for planned or actual legs from the shared snapshot (None if no chain)."""
    if quotes is None:
        return None
    snap = quotes.chain(underlying)
    if snap.rows is None:
        return None
    out, greeks, complete = [], {"delta": 0.0, "theta": 0.0}, True
    for lg in legs:
        sign = 1 if lg.side == "BUY" else -1
        if lg.asset_type == "STOCK":
            greeks["delta"] += sign * lg.quantity
            continue
        if lg.expiration is None or lg.option_right is None or lg.strike is None:
            complete = False
            continue
        cq, _ = quotes.contract(underlying, lg.expiration, lg.option_right, lg.strike, snap)
        out.append({"occ_symbol": cq.occ_symbol, "status": cq.status, "open_interest": cq.open_interest,
                    "spread_pct": cq.spread_pct, "delta": cq.delta})
        n = sign * lg.quantity * (lg.multiplier or 100)
        for k in greeks:
            v = getattr(cq, k)
            if v is None:
                complete = False
            else:
                greeks[k] += v * n
    return {"legs": out, "greeks": greeks if complete else None, "underlying_price": snap.underlying_price()}


def _tz(s: Session) -> str:
    return svc.get_preferences(s).get("timezone") or "America/New_York"


def check_legs(s: Session, account_id: str, underlying: str, legs: list[LegIn], cash_secured: bool,
               earnings: date | None, quotes_info: dict | None, has_plan: bool,
               exclude_position: str | None = None) -> tuple[rz.Analysis, rc.Report]:
    now = utcnow()
    a = svc.analyze_legs(legs, underlying, svc.context_for(s, account_id, underlying, cash_secured, earnings))
    settings, row = pol.load(s, account_id)
    port = rc.portfolio_state(s, account_id, now, _tz(s), exclude_position)
    eff = pol.effective(settings, port.account_value)
    cand = rc.candidate_from_analysis(underlying, a, svc.to_rz_legs(legs), earnings, quotes_info, has_plan)
    return a, rc.evaluate(eff, port, cand, row, now)


def assess(s: Session, p: TradePlan, quotes=None, earnings: date | None = None) -> rc.Report:
    """The plan's risk check: from its planned legs when it has them, else from its planned numbers."""
    now = utcnow()
    settings, row = pol.load(s, p.account_id)
    port = rc.portfolio_state(s, p.account_id, now, _tz(s))
    eff = pol.effective(settings, port.account_value)
    if p.legs_json:
        legs = planned_legs(p)
        a = svc.analyze_legs(legs, p.symbol, svc.context_for(s, p.account_id, p.symbol, False, earnings))
        if not a.errors:
            info = None
            try:
                info = leg_quotes(quotes, p.symbol, legs)
            except Exception:
                info = None
            cand = rc.candidate_from_analysis(p.symbol, a, svc.to_rz_legs(legs), earnings, info, True)
            cand.label = "This plan"
            return rc.evaluate(eff, port, cand, row, now)
    ml = from_units(p.planned_max_loss_units)
    undefined = p.intended_strategy in rc.UNDEFINED_STRATEGIES
    cand = rc.Candidate(underlying=p.symbol, strategy=p.intended_strategy,
                        max_loss_state="unbounded" if undefined else ("bounded" if ml is not None else "unknown"),
                        max_loss=None if undefined else ml, capital=None, defined_risk=None if not p.intended_strategy else not undefined,
                        contracts=p.planned_contracts,
                        expirations=[d for d in (p.expiration_from, p.expiration_to) if d], earnings=earnings,
                        leg_quotes=None, has_plan=True, label="This plan")
    return rc.evaluate(eff, port, cand, row, now)


# ------------------------------------------------------------------ state machine
def set_state(s: Session, p: TradePlan, to: str, reason: str | None, by: str, settings=None, run_id: str | None = None,
              detail: str | None = None) -> None:
    prev = p.state
    p.state, p.state_changed_at = to, utcnow()
    if to in ("SKIPPED", "INVALIDATED") and reason:
        p.outcome_reason = reason
    if to == "ARCHIVED":
        p.archived_at = utcnow()
    _audit(s, p, "state", {"state": prev}, reason)
    sev = "warning" if to in ("QUALIFIED", "INVALIDATED") and by == "monitor" else "info"
    who = "the monitor" if by == "monitor" else "you"
    msg = f"{LABEL[prev]} → {LABEL[to]} (by {who})." + (f" {detail}" if detail else "") + (f" Reason: {reason}" if reason else "")
    if to == "QUALIFIED":
        msg += " Entering now follows the plan. Informational only: no order is placed."
    emit(s, "PLAN_STATE_CHANGED", sev, f"Plan {LABEL[to].lower()}: {p.title}", msg, settings=settings, symbol=p.symbol,
         account_id=p.account_id, run_id=run_id, source={"plan_id": p.id, "from": prev, "to": to, "by": by},
         dedupe_key=f"plan:{p.id}:{to}" if by == "monitor" else None)


def transition(s: Session, plan_id: str, data: PlanTransition, quotes=None) -> TradePlan:
    p = get(s, plan_id)
    svc._check_version(p, data.version)
    to = data.to
    if to == p.state:
        return p
    if to == "CONVERTED":
        raise _err("USE_CONVERT", "Convert a plan with the convert action, which records the position.", 422)
    if to not in ALLOWED[p.state]:
        raise _err("BAD_TRANSITION", f"A {LABEL[p.state].lower()} plan can't become {LABEL[to].lower()}. Allowed: "
                                     f"{', '.join(LABEL[x] for x in sorted(ALLOWED[p.state])) or 'none'}.", 409,
                   details={"allowed": sorted(ALLOWED[p.state])})
    if to in ("SKIPPED", "INVALIDATED") and not (data.reason or "").strip():
        raise svc.DomainError("REASON_REQUIRED", f"Say why the plan is {LABEL[to].lower()}: it's part of the record.", 422,
                              field_errors=[{"field": "reason", "code": "REQUIRED", "message": "Required."}])
    if to == "ARMED" and p.state == "DRAFT":
        v = validate(s, p)
        if not v["ready"]:
            raise svc.DomainError("PLAN_INCOMPLETE", "The plan isn't complete enough to arm.", 422,
                                  field_errors=[{"field": e["item"], "code": "MISSING", "message": e["detail"]}
                                                for e in v["errors"]], details={"validation": v})
        from ..portfolio.quotes import earnings_date

        rep = assess(s, p, quotes, earnings_date(p.symbol))
        p.risk_json = rep.as_dict()
        if rep.overall == rc.BLOCKED:
            raise svc.DomainError("RISK_BLOCKED", "The plan breaks your risk policy: " + rep.summary, 422,
                                  details={"risk": rep.as_dict()})
    if to == "DRAFT":                                  # monitor state starts again when re-armed
        st = s.get(MonitorState, ("plan", p.id, ""))
        if st is not None:
            s.delete(st)
    set_state(s, p, to, data.reason, "user")
    s.flush()
    return p


# ------------------------------------------------------------------ conversion
def _per_share(a: rz.Analysis, legs: list[LegIn]) -> Decimal | None:
    if a.net_premium is None:
        return None
    opts = [lg for lg in legs if lg.asset_type == "OPTION"]
    if opts and len(opts) == len(legs):
        q = min(lg.quantity for lg in opts)
        mult = opts[0].multiplier or 100
        return abs(a.net_premium) / (q * mult) if q else None
    return None


def compare(p: TradePlan, a: rz.Analysis, legs: list[LegIn], at_state: str, underlying_price: Decimal | None,
            opened_at: datetime) -> dict:
    items: list[dict] = []

    def add(aspect, planned, actual, ok: bool | None, note=""):
        items.append({"aspect": aspect, "planned": planned, "actual": actual,
                      "result": "not_compared" if ok is None else ("as_planned" if ok else "deviated"), "note": note})
    # strategy
    if p.intended_strategy:
        add("Strategy", rz.NAMES.get(p.intended_strategy, p.intended_strategy), a.name, a.strategy == p.intended_strategy)
    # size
    contracts = max((lg.quantity for lg in legs if lg.asset_type == "OPTION"), default=None)
    if p.planned_contracts:
        if contracts is None:
            add("Size", f"{p.planned_contracts} contract(s)", "shares only", None)
        else:
            add("Size", f"{p.planned_contracts} contract(s)", f"{contracts} contract(s)", contracts <= p.planned_contracts,
                "" if contracts <= p.planned_contracts else "larger than planned")
    # risk
    planned_ml = from_units(p.planned_max_loss_units)
    if planned_ml is not None:
        if a.max_loss.state == "bounded" and a.max_loss.value is not None:
            ok = a.max_loss.value <= planned_ml
            add("Maximum loss", rc.usd(planned_ml), rc.usd(a.max_loss.value), ok, "" if ok else "more risk than planned")
        else:
            add("Maximum loss", rc.usd(planned_ml), a.max_loss.state, False, "the position's loss isn't capped")
    # entry price
    per = _per_share(a, legs)
    if p.max_entry_debit_units is not None:
        lim = from_units(p.max_entry_debit_units)
        if per is None or a.premium_type != "DEBIT":
            add("Entry price", f"debit ≤ {rc.usd(lim)}", a.premium_type.lower() if per is None else f"{a.premium_type.lower()} {rc.usd(per)}",
                None if per is None else False, "" if per is None else "not a debit")
        else:
            add("Entry price", f"debit ≤ {rc.usd(lim)}", f"debit {rc.usd(per)}", per <= lim, "" if per <= lim else "paid more than planned")
    if p.min_entry_credit_units is not None:
        lim = from_units(p.min_entry_credit_units)
        if per is None or a.premium_type != "CREDIT":
            add("Entry price", f"credit ≥ {rc.usd(lim)}", a.premium_type.lower(), None if per is None else False)
        else:
            add("Entry price", f"credit ≥ {rc.usd(lim)}", f"credit {rc.usd(per)}", per >= lim,
                "" if per >= lim else "received less than planned")
    # entry zone
    if p.entry_zone_low_units is not None or p.entry_zone_high_units is not None:
        lo, hi = from_units(p.entry_zone_low_units), from_units(p.entry_zone_high_units)
        zone = f"{rc.usd(lo) if lo is not None else '…'} – {rc.usd(hi) if hi is not None else '…'}"
        if underlying_price is None:
            add("Entry zone", zone, "underlying price not known", None)
        else:
            ok = (lo is None or underlying_price >= lo) and (hi is None or underlying_price <= hi)
            add("Entry zone", zone, rc.usd(underlying_price), ok, "" if ok else "entered outside the zone")
    # expirations
    exps = sorted({lg.expiration for lg in legs if lg.expiration})
    if (p.expiration_from or p.expiration_to) and exps:
        ok = all((p.expiration_from is None or e >= p.expiration_from) and (p.expiration_to is None or e <= p.expiration_to)
                 for e in exps)
        add("Expiration", f"{p.expiration_from or '…'} to {p.expiration_to or '…'}", ", ".join(map(str, exps)), ok)
    # timing
    add("Timing", "after the plan qualified", LABEL[at_state].lower(), at_state == "QUALIFIED",
        "" if at_state == "QUALIFIED" else "entered before the trigger and confirmation were met")
    if p.expires_at is not None:
        add("Within the plan's validity", f"before {p.expires_at:%Y-%m-%d %H:%M} UTC", f"{opened_at:%Y-%m-%d %H:%M} UTC",
            opened_at <= p.expires_at)
    done = [i for i in items if i["result"] != "not_compared"]
    return {"items": items, "as_planned": sum(i["result"] == "as_planned" for i in done),
            "deviated": sum(i["result"] == "deviated" for i in done), "compared": len(done), "converted_from": at_state,
            "plan_snapshot": {**fields_of(p), "legs": p.legs_json, "title": p.title, "symbol": p.symbol}}


def convert(s: Session, plan_id: str, data: PlanConvert, quotes=None) -> tuple[TradePlan, Position, rc.Report]:
    from ..portfolio.quotes import earnings_date

    p = get(s, plan_id)
    svc._check_version(p, data.version)
    if p.state not in LIVE:
        raise _err("PLAN_NOT_LIVE", f"Only armed, waiting or qualified plans can become positions (this one is "
                                    f"{LABEL[p.state].lower()}). Arm it first, or record the position without a plan.")
    earn = earnings_date(p.symbol)
    info = None
    try:
        info = leg_quotes(quotes, p.symbol, data.legs)
    except Exception:
        info = None
    a, rep = check_legs(s, p.account_id, p.symbol, data.legs, data.cash_secured, earn, info, True)
    if rep.overall == rc.BLOCKED and not data.acknowledge_rule_violation:
        raise svc.DomainError("RULE_VIOLATION", "This position breaks your risk policy. Record it anyway only if it "
                                                "already happened at the broker (acknowledge_rule_violation).", 422,
                              details={"risk": rep.as_dict()})
    position = svc.create_position(s, PositionIn(account_id=p.account_id, underlying=p.symbol, name=data.name or p.title,
                                                 opened_at=data.opened_at, legs=data.legs, cash_secured=data.cash_secured,
                                                 acknowledge_risk=data.acknowledge_risk,
                                                 notes=data.notes), earn, source="plan")
    record_risk(s, position, rep)
    position.plan_id = p.id
    under = data.underlying_price_at_entry
    if under is None and info is not None:
        under = info.get("underlying_price")
    comp = compare(p, a, data.legs, p.state, under, position.opened_at)
    p.comparison_json = comp
    p.position_id, p.converted_at = position.id, utcnow()
    set_state(s, p, "CONVERTED", None, "user",
              detail=f"Position {position.name}: {comp['as_planned']} of {comp['compared']} aspects as planned.")
    s.flush()
    return p, position, rep


def record_risk(s: Session, position: Position, rep: rc.Report) -> None:
    """Keep the entry risk check with the position; a recorded rule violation is flagged and announced."""
    position.entry_risk_json = rep.as_dict()
    position.rule_violation = rep.overall == rc.BLOCKED
    if position.rule_violation:
        broken = [c for c in rep.checks if c.result == rc.BLOCKED]
        emit(s, "RULE_VIOLATION", "warning", f"Recorded against your risk policy: {position.name}",
             "; ".join(f"{c.label}: {c.reason}" for c in broken) + " Recorded honestly; review it when you close it.",
             symbol=position.underlying, account_id=position.account_id, position_id=position.id,
             source={"risk": rep.as_dict()})
