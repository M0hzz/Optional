"""Typed request and response models for the /api/v1 API (pydantic v2).

Money crosses the API as decimal strings ("1.2500"), never floats: inputs are validated by money.to_units
(at most 4 decimal places, rejected otherwise). Timestamps must carry a timezone; they are stored and
returned in UTC. Enum values are explicit strings.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, StrictInt

from .money import MoneyPrecisionError, parse_decimal, to_units

T = TypeVar("T")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _money(v):
    if isinstance(v, float):
        raise ValueError("send money as a decimal string (e.g. \"1.25\"), not a binary float")
    try:
        to_units(v)
    except MoneyPrecisionError as exc:
        raise ValueError(str(exc).split(": ", 1)[-1]) from None
    return parse_decimal(v)


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamps need a timezone (e.g. 2026-11-02T14:30:00-05:00 or ...Z)")
    return v.astimezone(timezone.utc)


def _text(v: str | None) -> str | None:
    """Notes are stored as plain text: control characters removed, never interpreted as HTML."""
    if v is None:
        return None
    v = _CONTROL.sub("", v).strip()
    if len(v) > 5000:
        raise ValueError("at most 5,000 characters")
    return v or None


def _symbol(v: str) -> str:
    v = v.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", v):
        raise ValueError("symbol must be 1-10 letters, digits, dots or dashes, starting with a letter")
    return v


_MONEY_OUT = PlainSerializer(lambda d: f"{d:.4f}", return_type=str, when_used="json")   # canonical "250.0000"
Money = Annotated[Decimal, BeforeValidator(_money), _MONEY_OUT]
NonNegMoney = Annotated[Decimal, BeforeValidator(_money), AfterValidator(lambda d: _nonneg(d)), _MONEY_OUT]
UTC = Annotated[datetime, AfterValidator(_aware)]
Note = Annotated[str | None, AfterValidator(_text)]
Symbol = Annotated[str, AfterValidator(_symbol)]


def _nonneg(d: Decimal) -> Decimal:
    if d < 0:
        raise ValueError("must not be negative")
    return d


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ------------------------------------------------------------------ accounts
class AccountCreate(Strict):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    broker: Annotated[str | None, Field(max_length=120)] = None
    is_default: bool = False
    notes: Note = None


class AccountUpdate(Strict):
    version: StrictInt
    name: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    broker: Annotated[str | None, Field(max_length=120)] = None
    is_default: bool | None = None
    notes: Note = None


class SnapshotCreate(Strict):
    as_of: UTC | None = None
    total_value: Money | None = None
    cash: Money | None = None
    buying_power: Money | None = None
    notes: Note = None


class SnapshotOut(BaseModel):
    id: str
    as_of: datetime
    total_value: str | None
    cash: str | None
    buying_power: str | None
    source: str
    notes: str | None


class AccountOut(BaseModel):
    id: str
    name: str
    broker: str | None
    currency: str
    is_default: bool
    notes: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
    latest_snapshot: SnapshotOut | None


# ------------------------------------------------------------------ legs and positions
class LegIn(Strict):
    asset_type: Literal["STOCK", "OPTION"]
    side: Literal["BUY", "SELL"]
    quantity: Annotated[StrictInt, Field(gt=0, le=1_000_000)]
    option_right: Literal["CALL", "PUT"] | None = None
    strike: Money | None = None
    expiration: date | None = None
    multiplier: Annotated[StrictInt | None, Field(gt=0, le=10_000)] = None
    fill_price: NonNegMoney | None = None
    fees: NonNegMoney = Decimal(0)
    filled_at: UTC | None = None
    broker_order_id: Annotated[str | None, Field(max_length=64)] = None
    notes: Note = None


class PositionIn(Strict):
    account_id: str
    underlying: Symbol
    name: Annotated[str | None, Field(max_length=160)] = None
    opened_at: UTC | None = None
    legs: Annotated[list[LegIn], Field(min_length=1, max_length=12)]
    cash_secured: bool = False
    acknowledge_risk: bool = False
    acknowledge_rule_violation: bool = False     # record it although it breaks the risk policy (it's flagged)
    notes: Note = None


class AnalyzeIn(Strict):
    underlying: Symbol
    legs: Annotated[list[LegIn], Field(min_length=1, max_length=12)]
    account_id: str | None = None
    cash_secured: bool = False
    include_quotes: bool = True


class PositionUpdate(Strict):
    version: StrictInt
    name: Annotated[str | None, Field(max_length=160)] = None
    notes: Note = None
    legs: Annotated[list[LegIn] | None, Field(min_length=1, max_length=12)] = None
    cash_secured: bool | None = None
    acknowledge_risk: bool | None = None
    correction_reason: Annotated[str | None, Field(max_length=500)] = None


class LegClose(Strict):
    leg_id: str
    close_price: NonNegMoney | None = None
    close_fees: NonNegMoney = Decimal(0)
    expired_worthless: bool = False


class PositionClose(Strict):
    version: StrictInt
    closed_at: UTC | None = None
    legs: Annotated[list[LegClose], Field(min_length=1, max_length=12)]
    notes: Note = None


class PositionArchive(Strict):
    version: StrictInt
    reason: Literal["closed", "entered_in_error"] = "closed"


class BoundedOut(BaseModel):
    state: Literal["bounded", "unbounded", "unknown"]
    value: str | None = None


class IssueOut(BaseModel):
    code: str
    message: str
    leg: int | None = None


class LegOut(BaseModel):
    id: str
    leg_index: int
    asset_type: str
    option_right: str | None
    strike: str | None
    expiration: date | None
    quantity: int
    side: str
    multiplier: int
    fill_price: str | None
    fees: str
    filled_at: datetime | None
    broker_order_id: str | None
    occ_symbol: str | None
    close_price: str | None
    close_fees: str | None
    closed_at: datetime | None
    notes: str | None


class PositionOut(BaseModel):
    id: str
    account_id: str
    name: str
    underlying: str
    strategy: str
    strategy_name: str
    strategy_variant: str | None
    strategy_exact: bool
    direction: str
    lifecycle: Literal["OPEN", "CLOSED", "ARCHIVED"]
    opened_at: datetime
    closed_at: datetime | None
    archived_at: datetime | None
    archive_reason: str | None
    net_premium: str | None
    premium_type: str
    fees: str
    max_loss: BoundedOut
    max_gain: BoundedOut
    defined_risk: bool
    risk_acknowledged: bool
    realized_pnl: str | None
    monitoring_enabled: bool
    nearest_expiration: date | None
    days_to_expiration: int | None
    notes: str | None
    source: str
    created_at: datetime
    updated_at: datetime
    version: int
    legs: list[LegOut]
    monitor: dict | None = None          # effective monitoring status, reasons and latest snapshot (Phase 2)
    plan_id: str | None = None           # the plan it came from (Phase 3)
    entry_risk: dict | None = None       # the risk check when it was recorded
    rule_violation: bool = False         # recorded although it broke the risk policy


class AnalysisOut(BaseModel):
    strategy: str
    strategy_name: str
    exact: bool
    confidence: Literal["exact", "custom"]
    variant: str | None
    direction: str
    units: int
    structure: list[dict]
    net_premium: str | None
    premium_type: str
    fees: str
    cost_basis: str | None
    max_gain: BoundedOut
    max_loss: BoundedOut
    breakevens: list[str] | None
    breakeven_state: Literal["known", "unknown", "none"]
    defined_risk: bool
    buying_power_effect: BoundedOut
    buying_power_basis: str
    expirations: list[date]
    errors: list[IssueOut]
    warnings: list[IssueOut]
    assumptions: list[str]
    requires_risk_acknowledgement: bool
    payoff: list[dict]
    quotes: dict | None = None


# ------------------------------------------------------------------ watched contracts
ContractStatusT = Literal["RESEARCHING", "WAITING_FOR_CONFIRMATION", "ENTRY_QUALIFIED", "ENTRY_CONDITION_LOST",
                          "IN_POSITION", "EXPIRED", "ARCHIVED"]


class ContractIn(Strict):
    account_id: str | None = None
    underlying: Symbol
    option_right: Literal["CALL", "PUT"]
    strike: Money
    expiration: date
    status: ContractStatusT = "RESEARCHING"
    target_entry: NonNegMoney | None = None
    invalidation: Note = None
    alert_conditions: Note = None
    notes: Note = None


class ContractUpdate(Strict):
    version: StrictInt
    status: ContractStatusT | None = None
    target_entry: NonNegMoney | None = None
    invalidation: Note = None
    alert_conditions: Note = None
    notes: Note = None
    account_id: str | None = None


class ContractOut(BaseModel):
    id: str
    account_id: str | None
    underlying: str
    option_right: str
    strike: str
    expiration: date
    days_to_expiration: int
    occ_symbol: str
    status: str
    target_entry: str | None
    invalidation: str | None
    alert_conditions: str | None
    notes: str | None
    position_id: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


class ArchiveIn(Strict):
    version: StrictInt


# ------------------------------------------------------------------ misc
class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


WIDGETS = ("overview", "discipline", "progress", "monitor", "positions", "events", "contracts", "news")
Widget = Literal["overview", "discipline", "progress", "monitor", "positions", "events", "contracts", "news"]


class PreferencesIn(Strict):
    terminology: Literal["professional", "colony"] | None = None
    timezone: Annotated[str | None, Field(max_length=64)] = None
    active_account_id: str | None = None
    theme: Literal["default", "aurora", "nebula", "solar"] | None = None
    dashboard_layout: Annotated[list[Widget] | None, Field(max_length=8)] = None
    hidden_widgets: Annotated[list[Widget] | None, Field(max_length=8)] = None


class PreferencesOut(BaseModel):
    terminology: Literal["professional", "colony"]
    timezone: str
    active_account_id: str | None
    theme: str = "default"
    dashboard_layout: list[str] = list(WIDGETS)
    hidden_widgets: list[str] = []
