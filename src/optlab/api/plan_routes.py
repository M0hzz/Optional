"""/api/v1 routes for Phase 3: trade plans (missions), risk policies, risk checks and the risk dashboard.
Informational only: a BLOCKED result means "breaks your rules"; nothing here places or stops orders."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field
from sqlalchemy import func, select

from ..monitoring.events import emit
from ..planning import plans as pl
from ..planning import policy as pol
from ..planning import riskcheck as rc
from ..planning.schemas import PlanConvert, PlanCreate, PlanOut, PlanTransition, PlanUpdate, PolicyIn
from ..portfolio import service as svc
from ..portfolio.models import AuditEvent, Event, Lifecycle, MonitorState, Position, PositionSnapshot, TradePlan
from ..portfolio.quotes import earnings_date
from ..portfolio.schemas import LegIn, Page, Strict, Symbol
from .deps import Read, Write, quotes_of
from .monitor_routes import event_out

router = APIRouter()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def plan_out(s, p: TradePlan, detail: bool = False) -> PlanOut:
    allowed = sorted(pl.ALLOWED[p.state]) + (["CONVERTED"] if p.state in pl.LIVE else [])
    ms = s.get(MonitorState, ("plan", p.id, ""))
    mon = None
    if ms is not None:
        mon = {"checked_at": ms.checked_at, **(ms.data_json or {}),
               "pending": {"state": ms.candidate, "passes": ms.candidate_count} if ms.candidate else None}
    return PlanOut(id=p.id, account_id=p.account_id, symbol=p.symbol, title=p.title, state=p.state,  # type: ignore[arg-type]
                   state_changed_at=p.state_changed_at, fields=pl.fields_of(p), legs=p.legs_json or [],
                   expires_at=p.expires_at, outcome_reason=p.outcome_reason, risk=p.risk_json or None,
                   validation=pl.validate(s, p) if detail and p.state == "DRAFT" else None, position_id=p.position_id,
                   converted_at=p.converted_at, comparison=p.comparison_json or None, allowed_transitions=allowed,
                   monitor=mon, archived_at=p.archived_at, created_at=p.created_at, updated_at=p.updated_at,
                   version=p.version)


# ------------------------------------------------------------------ plans
@router.get("/plans", response_model=Page[PlanOut], tags=["plans"])
def list_plans(s: Read, account_id: str | None = None, state: Annotated[list[str] | None, Query()] = None,
               symbol: str | None = None, page: Annotated[int, Query(ge=1)] = 1,
               page_size: Annotated[int, Query(ge=1, le=200)] = 50):
    items, total = pl.list_plans(s, account_id, state, symbol, page, page_size)
    return Page[PlanOut](items=[plan_out(s, p) for p in items], total=total, page=page, page_size=page_size)


@router.post("/plans", response_model=PlanOut, status_code=201, tags=["plans"])
def create_plan(body: PlanCreate, s: Write):
    return plan_out(s, pl.create(s, body), detail=True)


@router.get("/plans/{plan_id}", response_model=PlanOut, tags=["plans"])
def get_plan(plan_id: str, s: Read):
    return plan_out(s, pl.get(s, plan_id), detail=True)


@router.patch("/plans/{plan_id}", response_model=PlanOut, tags=["plans"])
def update_plan(plan_id: str, body: PlanUpdate, s: Write):
    return plan_out(s, pl.update(s, plan_id, body), detail=True)


@router.post("/plans/{plan_id}/assess", response_model=PlanOut, tags=["plans"])
def assess_plan(plan_id: str, request: Request, s: Write):
    """Run the risk check now (with quotes for planned legs) and keep it on the plan."""
    p = pl.get(s, plan_id)
    if p.state in pl.TERMINAL:
        raise svc.DomainError("PLAN_CLOSED", "This plan is finished; its last risk check is kept as it was.", 409)
    p.risk_json = pl.assess(s, p, quotes_of(request), earnings_date(p.symbol)).as_dict()
    s.flush()
    return plan_out(s, p, detail=True)


@router.post("/plans/{plan_id}/transition", response_model=PlanOut, tags=["plans"])
def transition_plan(plan_id: str, body: PlanTransition, request: Request, s: Write):
    return plan_out(s, pl.transition(s, plan_id, body, quotes_of(request)), detail=True)


@router.post("/plans/{plan_id}/convert", tags=["plans"])
def convert_plan(plan_id: str, body: PlanConvert, request: Request, s: Write):
    from .routes import _pos

    p, position, rep = pl.convert(s, plan_id, body, quotes_of(request))
    return {"plan": plan_out(s, p).model_dump(mode="json"), "position": _pos(request, s, position).model_dump(mode="json"),
            "risk": rep.as_dict()}


@router.post("/plans/{plan_id}/duplicate", response_model=PlanOut, status_code=201, tags=["plans"])
def duplicate_plan(plan_id: str, s: Write):
    return plan_out(s, pl.duplicate(s, plan_id), detail=True)


@router.get("/plans/{plan_id}/timeline", tags=["plans"])
def plan_timeline(plan_id: str, s: Read):
    pl.get(s, plan_id)
    evs = s.scalars(select(Event).where(func.json_extract(Event.source_json, "$.plan_id") == plan_id)
                    .order_by(Event.seq.desc()).limit(300))
    items = [{"kind": "event", "at": e.occurred_at, "event": event_out(e).model_dump(mode="json")} for e in evs]
    items += [{"kind": "audit", "at": a.at, "audit": {"id": a.id, "action": a.action, "reason": a.reason,
                                                      "before": a.before_json, "after": a.after_json}}
              for a in svc.audit_trail(s, "plan", plan_id)]
    items.sort(key=lambda x: x["at"], reverse=True)  # type: ignore[arg-type, return-value]
    return items


# ------------------------------------------------------------------ risk policy
def _policy_payload(s, account_id: str) -> dict:
    settings, row = pol.load(s, account_id)
    port = rc.portfolio_state(s, account_id, now_utc(), pl._tz(s))
    eff = pol.effective(settings, port.account_value)
    return {"settings": settings.model_dump(mode="json"), "revision": row.revision if row else None,
            "id": row.id if row else None, "created_at": row.created_at if row else None, "notes": row.notes if row else None,
            "is_default": row is None, "effective": {**eff, "limits": pol.PolicySettings.model_validate(
                {k: v for k, v in eff["limits"].items()}).model_dump(mode="json")},
            "defaults": pol.PolicySettings().model_dump(mode="json")}


@router.get("/accounts/{account_id}/risk-policy", tags=["risk"])
def get_policy(account_id: str, s: Read):
    svc.get_account(s, account_id)
    return _policy_payload(s, account_id)


@router.put("/accounts/{account_id}/risk-policy", tags=["risk"])
def put_policy(account_id: str, body: PolicyIn, s: Write):
    svc.get_account(s, account_id)
    old, _ = pol.load(s, account_id)
    row = pol.save(s, account_id, body.settings, body.notes)
    s.add(AuditEvent(entity_type="risk_policy", entity_id=account_id, action="edit", before_json=old.model_dump(mode="json"),
                     after_json=body.settings.model_dump(mode="json"), reason=body.notes))
    emit(s, "RISK_POLICY_CHANGED", "info", f"Risk policy revision {row.revision}",
         "Your risk limits changed. Earlier risk checks keep the revision they used.", account_id=account_id,
         source={"revision": row.revision})
    return _policy_payload(s, account_id)


@router.get("/accounts/{account_id}/risk-policy/history", tags=["risk"])
def policy_history(account_id: str, s: Read):
    svc.get_account(s, account_id)
    return [{"id": r.id, "revision": r.revision, "created_at": r.created_at, "superseded_at": r.superseded_at,
             "notes": r.notes, "settings": r.settings_json} for r in pol.history(s, account_id)]


class EvaluateIn(Strict):
    account_id: str
    underlying: Symbol
    legs: Annotated[list[LegIn], Field(min_length=1, max_length=12)]
    cash_secured: bool = False
    plan_id: str | None = None
    include_quotes: bool = True


@router.post("/risk/evaluate", tags=["risk"])
def evaluate(body: EvaluateIn, request: Request, s: Read):
    """Risk check of legs you're about to record. Persists nothing."""
    svc.get_account(s, body.account_id)
    info = None
    if body.include_quotes:
        try:
            info = pl.leg_quotes(quotes_of(request), body.underlying, body.legs)
        except Exception:
            info = None
    a, rep = pl.check_legs(s, body.account_id, body.underlying, body.legs, body.cash_secured,
                           earnings_date(body.underlying), info, bool(body.plan_id))
    if a.errors:
        return {"overall": "UNKNOWN", "checks": [], "summary": "Fix the legs first: " + "; ".join(e.message for e in a.errors),
                "small_account_active": rep.small_account_active, "small_account_reason": rep.small_account_reason,
                "policy_revision": rep.policy_revision}
    return rep.as_dict()


@router.get("/accounts/{account_id}/risk", tags=["risk"])
def risk_dashboard(account_id: str, s: Read):
    """The account against its policy now, and every open position against the current policy."""
    from ..portfolio.money import fmt

    svc.get_account(s, account_id)
    now = now_utc()
    settings, row = pol.load(s, account_id)
    port = rc.portfolio_state(s, account_id, now, pl._tz(s))
    eff = pol.effective(settings, port.account_value)
    util = rc.utilization(eff, port, row, now)
    positions = []
    opens = list(s.scalars(select(Position).where(Position.account_id == account_id, Position.lifecycle == Lifecycle.OPEN)
                           .order_by(Position.opened_at)))
    for p in opens:
        st = s.get(MonitorState, ("position", p.id, ""))
        snap = s.get(PositionSnapshot, st.snapshot_id) if st and st.snapshot_id else None
        legs = svc.legs_from_rows(p.legs)
        from ..portfolio import recognize as rzm

        a = rzm.analyze(legs, rzm.Context(cash_secured=bool((p.metadata_json or {}).get("cash_secured")), today=port.today))
        cand = rc.candidate_from_analysis(p.underlying, a, legs, earnings_date(p.underlying), None, bool(p.plan_id))
        if snap is not None:
            cand.leg_quotes = [{"occ_symbol": lg.get("occ_symbol"), "open_interest": lg.get("open_interest"),
                                "spread_pct": lg.get("spread_pct")} for lg in snap.legs_json if lg.get("occ_symbol")]
        r = rc._Run(eff, rc.Portfolio(port.account_value, port.cash, port.buying_power,
                                      [x for x in port.positions if x.id != p.id], port.realized_today, port.realized_week,
                                      port.today))
        rc.position_checks(r, cand)
        positions.append({"id": p.id, "name": p.name, "underlying": p.underlying, "overall": rc.overall(r.out),
                          "checks": [c.__dict__ for c in r.out if c.result != rc.PASS],
                          "entry_risk": (p.entry_risk_json or {}).get("overall"), "rule_violation": p.rule_violation,
                          "plan_id": p.plan_id, "max_loss": {"state": p.max_loss_state.value, "value": fmt(rc.from_units(p.max_loss_units))}})
    bounded = sum((x.max_loss for x in port.positions if x.max_loss is not None and x.max_loss_state == "bounded"), rc.Decimal(0))
    plans_live = s.scalar(select(func.count()).select_from(TradePlan).where(TradePlan.account_id == account_id,
                                                                            TradePlan.state.in_(pl.LIVE))) or 0
    return {"utilization": util.as_dict(), "policy": {"revision": row.revision if row else None, "is_default": row is None},
            "small_account_active": eff["small_account_active"], "small_account_reason": eff["small_account_reason"],
            "account_value": fmt(port.account_value), "cash": fmt(port.cash), "open_risk": fmt(bounded),
            "open_unbounded": sum(1 for x in port.positions if x.max_loss_state != "bounded"),
            "realized_today": fmt(port.realized_today), "realized_week": fmt(port.realized_week),
            "positions": positions, "live_plans": plans_live,
            "rule_violations": sum(1 for p in opens if p.rule_violation)}


__all__ = ["router", "Literal"]
