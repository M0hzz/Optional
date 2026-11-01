"""Domain operations for accounts, positions and watched contracts. The API is a thin layer over these.

Rules
    * Every financial fact is computed here, in Decimal, never in the browser.
    * A position's strategy and risk come from recognize.analyze at creation and whenever its legs change.
    * Creating a position whose maximum loss isn't bounded requires acknowledge_risk=True.
    * Closing is its own operation with closing fills; archiving follows closing (or marks a mistaken entry).
    * Edits send the version they read; a stale version is a CONFLICT, never a silent overwrite.
    * Changes to fills and to closed positions are written to audit_events (before and after).
    * Accounts with positions, snapshots or contracts can't be deleted, only archived (and only with no
      open positions).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import recognize as rz
from .models import (Account, AccountSnapshot, AssetType, AuditEvent, Bound, ContractStatus, IdempotencyRecord, Lifecycle,
                     OptionRight, Position, PositionLeg, Side, UserPreference, WatchedContract, utcnow)
from .money import computed_to_units, fmt, from_units, to_units
from .occ import OCCError, build as occ_build
from .schemas import (AccountCreate, AccountUpdate, ContractIn, ContractUpdate, LegIn, PositionArchive,
                      PositionClose, PositionIn, PositionUpdate, PreferencesIn, SnapshotCreate)


@dataclass
class DomainError(Exception):
    code: str
    message: str
    status: int = 422
    field_errors: list[dict] = field(default_factory=list)
    details: dict | None = None


def _404(kind: str, id_: str) -> DomainError:
    return DomainError("NOT_FOUND", f"{kind} {id_} does not exist.", 404)


def _check_version(row, version: int) -> None:
    if row.version != version:
        raise DomainError("VERSION_CONFLICT", f"This record changed since you loaded it (you have version {version}, "
                                              f"it is now {row.version}). Reload and try again.", 409,
                          details={"current_version": row.version})


def _usd(units: int | None) -> str:
    d = from_units(units)
    if d is None:
        return "unknown"
    return f"{'−' if d < 0 else ''}${abs(d):,.2f}"


def _event(s: Session, type_: str, title: str, message: str, p: Position | None = None,
           wc: WatchedContract | None = None, source: dict | None = None) -> None:
    """Record changes in the event timeline (info: shown in the feed, never notified)."""
    from ..monitoring.events import emit

    emit(s, type_, "info", title, message, symbol=(p.underlying if p else wc.underlying if wc else None),
         account_id=(p.account_id if p else wc.account_id if wc else None), position_id=p.id if p else None,
         contract_id=wc.id if wc else None, source=source or {})


def _set_closed(s: Session, p: Position) -> None:
    """The monitor stops at once: its status for this position becomes CLOSED."""
    from .models import MonitorState

    st = s.get(MonitorState, ("position", p.id, ""))
    if st is not None:
        st.status, st.since, st.candidate, st.candidate_count = "CLOSED", utcnow(), None, 0


def _audit(s: Session, entity: str, id_: str, action: str, before: dict | None, after: dict | None,
           reason: str | None = None) -> None:
    s.add(AuditEvent(entity_type=entity, entity_id=id_, action=action, before_json=before, after_json=after,
                     reason=reason))


# ------------------------------------------------------------------ accounts
def create_account(s: Session, data: AccountCreate) -> Account:
    first = s.scalar(select(func.count()).select_from(Account)) == 0
    if data.is_default or first:
        for a in s.scalars(select(Account).where(Account.is_default)):
            a.is_default = False
    acc = Account(name=data.name, broker=data.broker, is_default=data.is_default or first, notes=data.notes)
    s.add(acc)
    s.flush()
    return acc


def get_account(s: Session, account_id: str) -> Account:
    acc = s.get(Account, account_id)
    if acc is None:
        raise _404("Account", account_id)
    return acc


def list_accounts(s: Session, include_archived: bool = False) -> list[Account]:
    q = select(Account).order_by(Account.created_at)
    if not include_archived:
        q = q.where(Account.archived_at.is_(None))
    return list(s.scalars(q))


def update_account(s: Session, account_id: str, data: AccountUpdate) -> Account:
    acc = get_account(s, account_id)
    _check_version(acc, data.version)
    for k in ("name", "broker", "notes"):
        if k in data.model_fields_set:
            setattr(acc, k, getattr(data, k))
    if data.is_default:
        for a in s.scalars(select(Account).where(Account.is_default, Account.id != acc.id)):
            a.is_default = False
        acc.is_default = True
    s.flush()
    return acc


def _account_counts(s: Session, account_id: str) -> dict:
    return {"positions": s.scalar(select(func.count()).where(Position.account_id == account_id)),
            "open_positions": s.scalar(select(func.count()).where(Position.account_id == account_id,
                                                                  Position.lifecycle == Lifecycle.OPEN)),
            "snapshots": s.scalar(select(func.count()).where(AccountSnapshot.account_id == account_id)),
            "contracts": s.scalar(select(func.count()).where(WatchedContract.account_id == account_id))}


def archive_account(s: Session, account_id: str, version: int) -> Account:
    acc = get_account(s, account_id)
    _check_version(acc, version)
    c = _account_counts(s, account_id)
    if c["open_positions"]:
        raise DomainError("ACCOUNT_HAS_OPEN_POSITIONS", f"Close or archive the {c['open_positions']} open position(s) "
                                                        "before archiving this account.", 409, details=c)
    acc.archived_at = utcnow()
    acc.is_default = False
    s.flush()
    return acc


def delete_account(s: Session, account_id: str) -> None:
    acc = get_account(s, account_id)
    c = _account_counts(s, account_id)
    if c["positions"] or c["snapshots"] or c["contracts"]:
        raise DomainError("ACCOUNT_NOT_EMPTY", "Accounts with positions, snapshots or watched contracts are kept for the "
                                               "record: archive the account instead of deleting it.", 409, details=c)
    s.delete(acc)
    s.flush()


def add_snapshot(s: Session, account_id: str, data: SnapshotCreate) -> AccountSnapshot:
    get_account(s, account_id)
    u = lambda d, f: None if d is None else to_units(d, f)  # noqa: E731
    snap = AccountSnapshot(account_id=account_id, as_of=data.as_of or utcnow(), total_value_units=u(data.total_value, "total_value"),
                           cash_units=u(data.cash, "cash"), buying_power_units=u(data.buying_power, "buying_power"),
                           notes=data.notes)
    s.add(snap)
    s.flush()
    return snap


def latest_snapshot(s: Session, account_id: str) -> AccountSnapshot | None:
    return s.scalar(select(AccountSnapshot).where(AccountSnapshot.account_id == account_id)
                    .order_by(AccountSnapshot.as_of.desc(), AccountSnapshot.created_at.desc()).limit(1))


def account_overview(s: Session, account_id: str, today: date | None = None) -> dict:
    """Numbers from the database only (no live quotes): entry risk, counts, expirations, realized P&L."""
    acc = get_account(s, account_id)
    today = today or date.today()
    snap = latest_snapshot(s, account_id)
    opens = list(s.scalars(select(Position).options(selectinload(Position.legs))
                           .where(Position.account_id == account_id, Position.lifecycle == Lifecycle.OPEN)))
    bounded = [p for p in opens if p.max_loss_state == Bound.BOUNDED]
    at_risk = sum((from_units(u) for p in bounded if (u := p.max_loss_units) is not None), Decimal(0))
    realized = s.scalar(select(func.coalesce(func.sum(Position.realized_pnl_units), 0))
                        .where(Position.account_id == account_id, Position.lifecycle != Lifecycle.OPEN))
    exps: dict[date, list[str]] = {}
    for p in opens:
        for lg in p.legs:
            if lg.expiration is not None:
                exps.setdefault(lg.expiration, [])
                if p.name not in exps[lg.expiration]:
                    exps[lg.expiration].append(p.name)
    upcoming = [{"expiration": e, "days": (e - today).days, "positions": names}
                for e, names in sorted(exps.items())][:8]
    bp = from_units(snap.buying_power_units) if snap else None
    return {
        "account_id": acc.id, "account_name": acc.name,
        "snapshot": snap,
        "open_positions": len(opens),
        "defined_risk_positions": sum(1 for p in opens if p.defined_risk),
        "unbounded_risk_positions": sum(1 for p in opens if p.max_loss_state == Bound.UNBOUNDED),
        "unknown_risk_positions": sum(1 for p in opens if p.max_loss_state == Bound.UNKNOWN),
        "capital_at_risk": fmt(at_risk),
        "capital_at_risk_complete": len(bounded) == len(opens),
        "capital_at_risk_pct_of_buying_power": (float(at_risk / bp * 100) if bp and bp > 0 else None),
        "realized_pnl_total": fmt(from_units(int(realized or 0))),
        "upcoming_expirations": upcoming,
        "strategies": _count_by(opens, lambda p: p.strategy),
    }


def _count_by(items, key) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[key(x)] = out.get(key(x), 0) + 1
    return out


# ------------------------------------------------------------------ legs <-> analysis
def to_rz_legs(legs: list[LegIn]) -> list[rz.Leg]:
    out = []
    for lg in legs:
        mult = lg.multiplier or (100 if lg.asset_type == "OPTION" else 1)
        out.append(rz.Leg(lg.asset_type, lg.side, lg.quantity, lg.option_right, lg.strike, lg.expiration,
                          lg.fill_price, lg.fees, mult))
    return out


def legs_from_rows(rows: list[PositionLeg]) -> list[rz.Leg]:
    return [rz.Leg(r.asset_type.value, r.side.value, r.quantity, r.option_right.value if r.option_right else None,
                   from_units(r.strike_units), r.expiration, from_units(r.fill_price_units),
                   from_units(r.fees_units) or Decimal(0), r.multiplier) for r in rows]


def analyze_legs(legs: list[LegIn], underlying: str, ctx: rz.Context) -> rz.Analysis:
    a = rz.analyze(to_rz_legs(legs), ctx)
    for i, lg in enumerate(legs):                     # OCC representability of option strikes
        if lg.asset_type == "OPTION" and lg.strike is not None and lg.expiration and lg.option_right:
            try:
                occ_build(underlying, lg.expiration, lg.option_right, lg.strike)
            except OCCError as exc:
                a.errors.append(rz.Issue("BAD_CONTRACT", str(exc), i))
    return a


def context_for(s: Session, account_id: str | None, underlying: str, cash_secured: bool,
                earnings: date | None) -> rz.Context:
    bp = None
    if account_id:
        snap = latest_snapshot(s, account_id)
        bp = from_units(snap.buying_power_units) if snap else None
    return rz.Context(cash_secured=cash_secured, buying_power=bp, earnings_date=earnings, today=date.today())


def _apply_analysis(p: Position, a: rz.Analysis) -> None:
    p.strategy, p.strategy_variant, p.strategy_exact, p.direction = a.strategy, a.variant, a.exact, a.direction
    p.net_premium_units = None if a.net_premium is None else computed_to_units(a.net_premium)
    p.fees_units = computed_to_units(a.fees)
    p.max_loss_state, p.max_loss_units = Bound(a.max_loss.state), _bounded_units(a.max_loss)
    p.max_gain_state, p.max_gain_units = Bound(a.max_gain.state), _bounded_units(a.max_gain)
    p.defined_risk = a.defined_risk


def _bounded_units(b: rz.Bounded) -> int | None:
    return computed_to_units(b.value) if b.state == "bounded" and b.value is not None else None


def _leg_rows(underlying: str, legs: list[LegIn]) -> list[PositionLeg]:
    """Legs already passed recognize.validate, so option legs have right, strike and expiration."""
    rows = []
    for i, lg in enumerate(legs):
        is_opt = lg.asset_type == "OPTION"
        occ = None
        if is_opt:
            assert lg.option_right is not None and lg.strike is not None and lg.expiration is not None
            occ = occ_build(underlying, lg.expiration, lg.option_right, lg.strike)
        rows.append(PositionLeg(
            leg_index=i, asset_type=AssetType(lg.asset_type), side=Side(lg.side), quantity=lg.quantity,
            option_right=OptionRight(lg.option_right) if lg.option_right and is_opt else None,
            strike_units=to_units(lg.strike, f"legs[{i}].strike") if is_opt and lg.strike is not None else None,
            expiration=lg.expiration if is_opt else None,
            multiplier=lg.multiplier or (100 if is_opt else 1),
            fill_price_units=None if lg.fill_price is None else to_units(lg.fill_price, f"legs[{i}].fill_price"),
            fees_units=to_units(lg.fees, f"legs[{i}].fees"), filled_at=lg.filled_at,
            broker_order_id=lg.broker_order_id, notes=lg.notes,
            occ_symbol=occ))
    return rows


def _raise_invalid(a: rz.Analysis) -> None:
    if a.errors:
        raise DomainError("INVALID_LEGS", "The legs are incomplete or inconsistent.", 422,
                          field_errors=[{"field": f"legs[{e.leg}]" if e.leg is not None else "legs", "code": e.code,
                                         "message": e.message} for e in a.errors])


def _require_ack(a: rz.Analysis, ack: bool) -> None:
    if a.max_loss.state != "bounded" and not ack:
        what = "unlimited" if a.max_loss.state == "unbounded" else "can't be calculated"
        raise DomainError("RISK_ACK_REQUIRED", f"The maximum loss of this position {what}. Confirm you understand "
                                               "this (acknowledge_risk) to record it.", 422,
                          details={"max_loss_state": a.max_loss.state})


def leg_snapshot(p: Position) -> dict:
    return {"name": p.name, "lifecycle": p.lifecycle.value, "net_premium_units": p.net_premium_units,
            "fees_units": p.fees_units, "realized_pnl_units": p.realized_pnl_units, "notes": p.notes,
            "legs": [{"leg_index": lg.leg_index, "asset_type": lg.asset_type.value,
                      "right": lg.option_right.value if lg.option_right else None, "strike_units": lg.strike_units,
                      "expiration": lg.expiration.isoformat() if lg.expiration else None, "quantity": lg.quantity,
                      "side": lg.side.value, "multiplier": lg.multiplier, "fill_price_units": lg.fill_price_units,
                      "fees_units": lg.fees_units, "filled_at": lg.filled_at.isoformat() if lg.filled_at else None,
                      "close_price_units": lg.close_price_units, "close_fees_units": lg.close_fees_units,
                      "broker_order_id": lg.broker_order_id} for lg in p.legs]}


# ------------------------------------------------------------------ positions
def create_position(s: Session, data: PositionIn, earnings: date | None = None, source: str = "manual") -> Position:
    acc = get_account(s, data.account_id)
    if acc.archived_at is not None:
        raise DomainError("ACCOUNT_ARCHIVED", "This account is archived; positions can't be added to it.", 409)
    a = analyze_legs(data.legs, data.underlying, context_for(s, acc.id, data.underlying, data.cash_secured, earnings))
    _raise_invalid(a)
    _require_ack(a, data.acknowledge_risk)
    p = Position(account_id=acc.id, underlying=data.underlying,
                 name=data.name or f"{data.underlying} {a.name.lower()}", opened_at=data.opened_at or utcnow(),
                 risk_acknowledged=data.acknowledge_risk and a.max_loss.state != "bounded",
                 notes=data.notes, source=source, metadata_json={"cash_secured": data.cash_secured})
    _apply_analysis(p, a)
    p.legs = _leg_rows(data.underlying, data.legs)
    s.add(p)
    s.flush()
    _audit(s, "position", p.id, "create", None, leg_snapshot(p))
    prem = p.net_premium_units
    _event(s, "POSITION_OPENED", f"Recorded: {p.name}",
           f"{a.name}, {'debit' if (prem or 0) > 0 else 'credit'} {_usd(abs(prem) if prem is not None else None)}, "
           f"maximum loss {_usd(p.max_loss_units) if p.max_loss_state == Bound.BOUNDED else p.max_loss_state.value}. "
           f"Source: {source}.", p, source={"strategy": p.strategy})
    return p


def get_position(s: Session, position_id: str) -> Position:
    p = s.scalar(select(Position).options(selectinload(Position.legs)).where(Position.id == position_id))
    if p is None:
        raise _404("Position", position_id)
    return p


SORTS = {"opened_at": Position.opened_at, "underlying": Position.underlying, "strategy": Position.strategy,
         "max_loss": Position.max_loss_units, "name": func.lower(Position.name), "lifecycle": Position.lifecycle,
         "updated_at": Position.updated_at}


def list_positions(s: Session, account_id: str | None = None, lifecycle: Sequence[str] | None = None,
                   underlying: str | None = None, strategy: str | None = None, page: int = 1, page_size: int = 50,
                   sort: str = "-opened_at") -> tuple[list[Position], int]:
    q = select(Position).options(selectinload(Position.legs))
    if account_id:
        q = q.where(Position.account_id == account_id)
    if lifecycle:
        q = q.where(Position.lifecycle.in_([Lifecycle(x) for x in lifecycle]))
    if underlying:
        q = q.where(Position.underlying == underlying.upper())
    if strategy:
        q = q.where(Position.strategy == strategy)
    total = s.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
    col = SORTS.get(sort.lstrip("-"))
    if col is None:
        raise DomainError("BAD_SORT", f"sort must be one of {', '.join(SORTS)} (prefix - for descending)", 422)
    q = q.order_by(col.desc() if sort.startswith("-") else col.asc(), Position.id)
    return list(s.scalars(q.offset((page - 1) * page_size).limit(page_size))), int(total or 0)


def update_position(s: Session, position_id: str, data: PositionUpdate, earnings: date | None = None) -> Position:
    p = get_position(s, position_id)
    _check_version(p, data.version)
    if p.lifecycle == Lifecycle.ARCHIVED:
        raise DomainError("POSITION_ARCHIVED", "Archived positions are read-only.", 409)
    financial = data.legs is not None or data.cash_secured is not None
    if p.lifecycle == Lifecycle.CLOSED and financial and not data.correction_reason:
        raise DomainError("CORRECTION_REASON_REQUIRED", "This position is closed: say why its fills are being corrected "
                                                        "(correction_reason). The original values are kept in the "
                                                        "audit log.", 422)
    before = leg_snapshot(p)
    for k in ("name", "notes"):
        if k in data.model_fields_set and getattr(data, k) is not None:
            setattr(p, k, getattr(data, k))
    if financial:
        if p.lifecycle == Lifecycle.CLOSED and data.legs is not None:
            raise DomainError("CLOSED_LEGS_LOCKED", "Legs of a closed position can't be replaced; archive it as "
                                                    "entered in error and record it again.", 409)
        cash = data.cash_secured if data.cash_secured is not None else bool(p.metadata_json.get("cash_secured"))
        legs_in = data.legs if data.legs is not None else [_leg_in(r) for r in p.legs]
        a = analyze_legs(legs_in, p.underlying, context_for(s, p.account_id, p.underlying, cash, earnings))
        _raise_invalid(a)
        _require_ack(a, bool(data.acknowledge_risk) or (p.risk_acknowledged and a.max_loss.state == p.max_loss_state.value))
        _apply_analysis(p, a)
        if data.legs is not None:
            p.legs.clear()
            s.flush()
            p.legs.extend(_leg_rows(p.underlying, data.legs))
        p.metadata_json = {**p.metadata_json, "cash_secured": cash}
        p.risk_acknowledged = a.max_loss.state != "bounded"
    p.updated_at = utcnow()
    s.flush()
    if financial or before["notes"] != p.notes or before["name"] != p.name:
        corr = p.lifecycle == Lifecycle.CLOSED
        _audit(s, "position", p.id, "correct" if corr else "edit", before, leg_snapshot(p), data.correction_reason)
        what = "fills and legs" if financial else "name or notes"
        _event(s, "POSITION_CORRECTED" if corr else "POSITION_EDITED", f"{'Corrected' if corr else 'Edited'}: {p.name}",
               f"Changed the {what}." + (f" Reason: {data.correction_reason}" if data.correction_reason else "")
               + " The previous values are kept in the audit log.", p)
    return p


def _leg_in(r: PositionLeg) -> LegIn:
    return LegIn(asset_type=r.asset_type.value, side=r.side.value, quantity=r.quantity,
                 option_right=r.option_right.value if r.option_right else None,
                 strike=from_units(r.strike_units), expiration=r.expiration, multiplier=r.multiplier,
                 fill_price=from_units(r.fill_price_units), fees=from_units(r.fees_units), filled_at=r.filled_at, broker_order_id=r.broker_order_id,
                 notes=r.notes)


def close_position(s: Session, position_id: str, data: PositionClose) -> Position:
    """Every leg needs a closing price (or expired_worthless). Realized P&L, per leg:
    sign × quantity × multiplier × (close price − fill price) − opening fees − closing fees."""
    p = get_position(s, position_id)
    _check_version(p, data.version)
    if p.lifecycle != Lifecycle.OPEN:
        raise DomainError("NOT_OPEN", f"Only open positions can be closed (this one is {p.lifecycle.value}).", 409)
    by_id = {lg.id: lg for lg in p.legs}
    given = {c.leg_id: c for c in data.legs}
    errs = []
    for lid in given:
        if lid not in by_id:
            errs.append({"field": "legs", "code": "UNKNOWN_LEG", "message": f"Leg {lid} is not part of this position."})
    for lg in p.legs:
        c = given.get(lg.id)
        if c is None:
            errs.append({"field": f"legs[{lg.leg_index}]", "code": "MISSING_CLOSE",
                         "message": "Every leg needs a closing price, or expired_worthless."})
        elif c.close_price is None and not c.expired_worthless:
            errs.append({"field": f"legs[{lg.leg_index}]", "code": "MISSING_CLOSE_PRICE",
                         "message": "Give the closing price, or mark the leg expired worthless."})
        elif c.expired_worthless and lg.asset_type == AssetType.STOCK:
            errs.append({"field": f"legs[{lg.leg_index}]", "code": "STOCK_CANT_EXPIRE",
                         "message": "Stock legs don't expire; give the price the shares were sold or bought at."})
        if lg.fill_price_units is None:
            errs.append({"field": f"legs[{lg.leg_index}]", "code": "MISSING_FILL_PRICE",
                         "message": "This leg has no opening fill price, so realized P&L can't be calculated. Edit the "
                                    "position first."})
    if errs:
        raise DomainError("INVALID_CLOSE", "The closing fills are incomplete.", 422, field_errors=errs)
    before = leg_snapshot(p)
    closed_at = data.closed_at or utcnow()
    if closed_at < p.opened_at:
        raise DomainError("CLOSE_BEFORE_OPEN", "The closing time is before the position was opened.", 422)
    pnl = Decimal(0)
    for lg in p.legs:
        c = given[lg.id]
        price = Decimal(0) if c.expired_worthless else c.close_price
        assert price is not None and lg.fill_price_units is not None     # checked above
        lg.close_price_units = to_units(price, "close_price")
        lg.close_fees_units = to_units(c.close_fees, "close_fees")
        lg.closed_at = closed_at
        sign = 1 if lg.side == Side.BUY else -1
        pnl += sign * lg.quantity * lg.multiplier * (price - from_units(lg.fill_price_units))
        pnl -= from_units(lg.fees_units) + c.close_fees
    p.realized_pnl_units = computed_to_units(pnl)
    p.lifecycle, p.closed_at = Lifecycle.CLOSED, closed_at
    if data.notes:
        p.notes = (p.notes + "\n\n" if p.notes else "") + data.notes
    s.flush()
    _audit(s, "position", p.id, "close", before, leg_snapshot(p))
    _set_closed(s, p)
    _event(s, "POSITION_CLOSED", f"Closed: {p.name}", f"Realized P&L {_usd(p.realized_pnl_units)} after fees.", p,
           source={"realized_pnl_units": p.realized_pnl_units})
    return p


def archive_position(s: Session, position_id: str, data: PositionArchive) -> Position:
    p = get_position(s, position_id)
    _check_version(p, data.version)
    if p.lifecycle == Lifecycle.ARCHIVED:
        raise DomainError("ALREADY_ARCHIVED", "This position is already archived.", 409)
    if p.lifecycle == Lifecycle.OPEN and data.reason != "entered_in_error":
        raise DomainError("CLOSE_FIRST", "Close the position before archiving it, or archive it with reason "
                                         "entered_in_error if it was recorded by mistake.", 409)
    before = leg_snapshot(p)
    p.lifecycle, p.archived_at, p.archive_reason = Lifecycle.ARCHIVED, utcnow(), data.reason
    if p.closed_at is None:                         # entered in error: never closed, no realized P&L
        p.closed_at = p.archived_at
    s.flush()
    _audit(s, "position", p.id, "archive", before, leg_snapshot(p), data.reason)
    _set_closed(s, p)
    _event(s, "POSITION_ARCHIVED", f"Archived: {p.name}",
           "Archived as entered in error." if data.reason == "entered_in_error" else "Archived after closing.", p)
    return p


def audit_trail(s: Session, entity: str, id_: str) -> list[AuditEvent]:
    return list(s.scalars(select(AuditEvent).where(AuditEvent.entity_type == entity, AuditEvent.entity_id == id_)
                          .order_by(AuditEvent.at, AuditEvent.id)))


# ------------------------------------------------------------------ watched contracts
def check_conditions(text: str | None, target: str = "contract", field: str = "alert_conditions") -> None:
    """Alert conditions are evaluated by the monitor: each line must be a valid condition."""
    from ..monitoring.rules import split_conditions, validate

    errs = [{"field": field, "code": "BAD_CONDITION", "message": e}
            for e in (validate(c, target) for c in split_conditions(text)) if e]
    if errs:
        raise DomainError("BAD_CONDITION", "A condition isn't valid.", 422, field_errors=errs)


def create_contract(s: Session, data: ContractIn) -> WatchedContract:
    if data.account_id:
        get_account(s, data.account_id)
    check_conditions(data.alert_conditions)
    try:
        occ = occ_build(data.underlying, data.expiration, data.option_right, data.strike)
    except OCCError as exc:
        raise DomainError("BAD_CONTRACT", str(exc), 422, field_errors=[{"field": "strike", "code": "BAD_CONTRACT",
                                                                         "message": str(exc)}]) from None
    status = data.status
    if data.expiration < date.today() and status not in ("EXPIRED", "ARCHIVED"):
        status = "EXPIRED"
    wc = WatchedContract(account_id=data.account_id, underlying=data.underlying,
                         option_right=OptionRight(data.option_right), strike_units=to_units(data.strike, "strike"),
                         expiration=data.expiration, occ_symbol=occ, status=ContractStatus(status),
                         target_entry_units=None if data.target_entry is None else to_units(data.target_entry, "target_entry"),
                         invalidation=data.invalidation, alert_conditions=data.alert_conditions, notes=data.notes)
    s.add(wc)
    s.flush()
    _event(s, "CONTRACT_ADDED", f"Watching: {wc.occ_symbol}", f"Status {wc.status.value.replace('_', ' ').lower()}.",
           wc=wc)
    return wc


def get_contract(s: Session, cid: str) -> WatchedContract:
    wc = s.get(WatchedContract, cid)
    if wc is None:
        raise _404("Watched contract", cid)
    return wc


def list_contracts(s: Session, account_id: str | None = None, status: list[str] | None = None,
                   underlying: str | None = None, include_archived: bool = False, page: int = 1,
                   page_size: int = 50) -> tuple[list[WatchedContract], int]:
    q = select(WatchedContract)
    if account_id:
        q = q.where((WatchedContract.account_id == account_id) | WatchedContract.account_id.is_(None))
    if status:
        q = q.where(WatchedContract.status.in_([ContractStatus(x) for x in status]))
    elif not include_archived:
        q = q.where(WatchedContract.status != ContractStatus.ARCHIVED)
    if underlying:
        q = q.where(WatchedContract.underlying == underlying.upper())
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    q = q.order_by(WatchedContract.expiration, WatchedContract.underlying, WatchedContract.id)
    return list(s.scalars(q.offset((page - 1) * page_size).limit(page_size))), int(total or 0)


def update_contract(s: Session, cid: str, data: ContractUpdate) -> WatchedContract:
    wc = get_contract(s, cid)
    _check_version(wc, data.version)
    if wc.status == ContractStatus.ARCHIVED:
        raise DomainError("CONTRACT_ARCHIVED", "Archived contracts are read-only.", 409)
    if "account_id" in data.model_fields_set:
        if data.account_id:
            get_account(s, data.account_id)
        wc.account_id = data.account_id
    if data.status is not None:
        if data.status == "ARCHIVED":
            raise DomainError("USE_ARCHIVE", "Archive a contract with the archive action.", 422)
        if wc.status.value != data.status:
            _event(s, "CONTRACT_STATUS_CHANGED", f"{data.status.replace('_', ' ').capitalize()}: {wc.occ_symbol}",
                   f"Status changed by you from {wc.status.value.replace('_', ' ').lower()} to "
                   f"{data.status.replace('_', ' ').lower()}.", wc=wc, source={"from": wc.status.value, "to": data.status})
        wc.status = ContractStatus(data.status)
    if "target_entry" in data.model_fields_set:
        wc.target_entry_units = None if data.target_entry is None else to_units(data.target_entry, "target_entry")
    if "alert_conditions" in data.model_fields_set:
        check_conditions(data.alert_conditions)
    for k in ("invalidation", "alert_conditions", "notes"):
        if k in data.model_fields_set:
            setattr(wc, k, getattr(data, k))
    s.flush()
    return wc


def archive_contract(s: Session, cid: str, version: int) -> WatchedContract:
    wc = get_contract(s, cid)
    _check_version(wc, version)
    wc.status, wc.archived_at = ContractStatus.ARCHIVED, utcnow()
    s.flush()
    _event(s, "CONTRACT_ARCHIVED", f"Archived: {wc.occ_symbol}", "No longer watched.", wc=wc)
    return wc


# ------------------------------------------------------------------ preferences
PREF_DEFAULTS: dict[str, Any] = {"terminology": "professional", "timezone": "America/New_York", "active_account_id": None,
                                 "theme": "default", "dashboard_layout": ["overview", "discipline", "progress", "monitor",
                                                                          "positions", "events", "contracts", "news"],
                                 "hidden_widgets": []}


def get_preferences(s: Session) -> dict:
    out = dict(PREF_DEFAULTS)
    for row in s.scalars(select(UserPreference)):
        out[row.key] = row.value_json.get("value")
    return out


def set_preferences(s: Session, data: PreferencesIn) -> dict:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if data.timezone is not None:
        try:
            ZoneInfo(data.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise DomainError("BAD_TIMEZONE", f"Unknown time zone {data.timezone!r}.", 422) from None
    if data.active_account_id:
        get_account(s, data.active_account_id)
    if data.theme and data.theme != "default":            # cosmetic themes unlock with the colony level
        from ..process.engine import THEMES, progress

        acc_id = data.active_account_id or get_preferences(s).get("active_account_id")
        if acc_id is None:
            first = s.scalar(select(Account.id).where(Account.archived_at.is_(None)).order_by(Account.created_at).limit(1))
            acc_id = first
        lvl = progress(s, acc_id, get_preferences(s).get("timezone") or "America/New_York", write=False)["level"]["level"] if acc_id else 1
        if lvl < THEMES[data.theme]:
            raise DomainError("THEME_LOCKED", f"The {data.theme} theme unlocks at level {THEMES[data.theme]} (you're at "
                                              f"level {lvl}). Levels come from process points, never from profit.", 409)
    for k in data.model_fields_set:
        if k in ("theme", "dashboard_layout", "hidden_widgets") and getattr(data, k) is None:
            continue
        row = s.get(UserPreference, k) or UserPreference(key=k, value_json={})
        row.value_json = {"value": getattr(data, k)}
        s.add(row)
    s.flush()
    return get_preferences(s)


# ------------------------------------------------------------------ idempotency
def request_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def idempotent_lookup(s: Session, key: str | None, scope: str, req_hash: str) -> IdempotencyRecord | None:
    if not key:
        return None
    rec = s.get(IdempotencyRecord, key)
    if rec is None:
        return None
    if rec.scope != scope or rec.request_hash != req_hash:
        raise DomainError("IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used for a different request.", 409)
    return rec


def idempotent_store(s: Session, key: str | None, scope: str, req_hash: str, status: int, body: dict) -> None:
    if key:
        s.add(IdempotencyRecord(key=key, scope=scope, request_hash=req_hash, status_code=status, response_json=body))
        s.flush()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

