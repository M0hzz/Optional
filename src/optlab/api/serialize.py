"""ORM rows and domain results -> API response models. Money leaves as 4-decimal strings."""
from __future__ import annotations

from datetime import date
from typing import Literal, cast

from ..portfolio import recognize as rz
from ..portfolio.models import AccountSnapshot, Position, WatchedContract
from ..portfolio.money import fmt, from_units
from ..portfolio.schemas import (AccountOut, AnalysisOut, BoundedOut, ContractOut, IssueOut, LegOut, PositionOut,
                                 SnapshotOut)
from ..portfolio.valuation import PositionValuation


def m(units: int | None) -> str | None:
    return fmt(from_units(units))


def snapshot(s: AccountSnapshot | None) -> SnapshotOut | None:
    if s is None:
        return None
    return SnapshotOut(id=s.id, as_of=s.as_of, total_value=m(s.total_value_units), cash=m(s.cash_units),
                       buying_power=m(s.buying_power_units), source=s.source, notes=s.notes)


def account(a, snap: AccountSnapshot | None) -> AccountOut:
    return AccountOut(id=a.id, name=a.name, broker=a.broker, currency=a.currency, is_default=a.is_default,
                      notes=a.notes, archived_at=a.archived_at, created_at=a.created_at, updated_at=a.updated_at,
                      version=a.version, latest_snapshot=snapshot(snap))


def bounded(state: str, units: int | None) -> BoundedOut:
    return BoundedOut.model_validate({"state": state, "value": m(units) if state == "bounded" else None})


def position(p: Position, today: date | None = None, monitor: dict | None = None) -> PositionOut:
    today = today or date.today()
    exps = [lg.expiration for lg in p.legs if lg.expiration is not None]
    future = [e for e in exps if e >= today]
    nearest = min(future) if future else (max(exps) if exps else None)
    prem = from_units(p.net_premium_units)
    return PositionOut(
        id=p.id, account_id=p.account_id, name=p.name, underlying=p.underlying, strategy=p.strategy,
        strategy_name=rz.NAMES.get(p.strategy, p.strategy) + (" (broken wing)" if p.strategy_variant == "BROKEN_WING" else ""),
        strategy_variant=p.strategy_variant, strategy_exact=p.strategy_exact, direction=p.direction,
        lifecycle=p.lifecycle.value, opened_at=p.opened_at, closed_at=p.closed_at, archived_at=p.archived_at,
        archive_reason=p.archive_reason, net_premium=fmt(prem),
        premium_type="UNKNOWN" if prem is None else ("DEBIT" if prem > 0 else "CREDIT" if prem < 0 else "EVEN"),
        fees=m(p.fees_units) or "0.0000", max_loss=bounded(p.max_loss_state.value, p.max_loss_units),
        max_gain=bounded(p.max_gain_state.value, p.max_gain_units), defined_risk=p.defined_risk,
        risk_acknowledged=p.risk_acknowledged, realized_pnl=m(p.realized_pnl_units),
        monitoring_enabled=p.monitoring_enabled, nearest_expiration=nearest,
        days_to_expiration=(nearest - today).days if nearest else None, notes=p.notes, source=p.source,
        created_at=p.created_at, updated_at=p.updated_at, version=p.version,
        legs=[LegOut(id=lg.id, leg_index=lg.leg_index, asset_type=lg.asset_type.value,
                     option_right=lg.option_right.value if lg.option_right else None, strike=m(lg.strike_units),
                     expiration=lg.expiration, quantity=lg.quantity, side=lg.side.value, multiplier=lg.multiplier,
                     fill_price=m(lg.fill_price_units), fees=m(lg.fees_units) or "0.0000", filled_at=lg.filled_at,
                     broker_order_id=lg.broker_order_id, occ_symbol=lg.occ_symbol,
                     close_price=m(lg.close_price_units), close_fees=m(lg.close_fees_units), closed_at=lg.closed_at,
                     notes=lg.notes) for lg in p.legs], monitor=monitor, plan_id=p.plan_id,
        entry_risk=p.entry_risk_json or None, rule_violation=bool(p.rule_violation))


def contract(w: WatchedContract, today: date | None = None) -> ContractOut:
    today = today or date.today()
    return ContractOut(id=w.id, account_id=w.account_id, underlying=w.underlying, option_right=w.option_right.value,
                       strike=fmt(from_units(w.strike_units)), expiration=w.expiration, days_to_expiration=(w.expiration - today).days,
                       occ_symbol=w.occ_symbol, status=w.status.value, target_entry=m(w.target_entry_units),
                       invalidation=w.invalidation, alert_conditions=w.alert_conditions, notes=w.notes,
                       position_id=w.position_id, archived_at=w.archived_at, created_at=w.created_at,
                       updated_at=w.updated_at, version=w.version)


def analysis(a: rz.Analysis, quotes: dict | None = None) -> AnalysisOut:
    b = lambda x: BoundedOut(state=x.state, value=fmt(x.value) if x.state == "bounded" else None)  # noqa: E731
    return AnalysisOut(
        strategy=a.strategy, strategy_name=a.name, exact=a.exact, confidence="exact" if a.exact else "custom",
        variant=a.variant, direction=a.direction, units=a.units, structure=a.structure, net_premium=fmt(a.net_premium),
        premium_type=a.premium_type, fees=fmt(a.fees), cost_basis=fmt(a.cost_basis), max_gain=b(a.max_gain),
        max_loss=b(a.max_loss), breakevens=None if a.breakevens is None else [fmt(x) for x in a.breakevens],
        breakeven_state=cast(Literal["known", "unknown", "none"], a.breakeven_state), defined_risk=a.defined_risk, buying_power_effect=b(a.buying_power),
        buying_power_basis=a.buying_power_basis, expirations=a.expirations,
        errors=[IssueOut(code=e.code, message=e.message, leg=e.leg) for e in a.errors],
        warnings=[IssueOut(code=w.code, message=w.message, leg=w.leg) for w in a.warnings],
        assumptions=a.assumptions, requires_risk_acknowledgement=a.max_loss.state != "bounded",
        payoff=[{"price": fmt(x), "pnl": fmt(y)} for x, y in a.payoff_points], quotes=quotes)


def freshness_block(v: PositionValuation) -> dict:
    return {"label": v.label, "freshness": v.freshness, "provider": v.provider, "delay_minutes": v.delay_minutes,
            "fetched_at": v.fetched_at, "source_timestamp": v.source_timestamp,
            "quote_age_seconds": None if v.quote_age_seconds is None else round(v.quote_age_seconds, 1),
            "greeks_source": v.greeks_source, "valued_at": v.valued_at}


def valuation(v: PositionValuation) -> dict:
    return {**freshness_block(v), "status": v.status, "reasons": v.reasons, "value": fmt(v.value),
            "exit_value": fmt(v.exit_value), "open_pnl": fmt(v.open_pnl),
            "open_pnl_pct": None if v.open_pnl_pct is None else round(v.open_pnl_pct, 2),
            "underlying_price": fmt(v.underlying_price), "greeks": v.greeks,
            "legs": [{"leg_index": lv.leg_index, "occ_symbol": lv.occ_symbol, "status": lv.status, "reason": lv.reason,
                      "mark": fmt(lv.mark), "exit_price": fmt(lv.exit_price), "value": fmt(lv.value),
                      "bid": fmt(lv.bid), "ask": fmt(lv.ask), "iv": lv.iv, "greeks": lv.greeks, "quality": lv.quality,
                      "nearest": lv.nearest} for lv in v.legs]}
