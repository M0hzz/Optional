"""API models for trade plans (missions) and risk policies."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator

from ..portfolio.schemas import UTC, LegIn, NonNegMoney, Note, Strict, Symbol
from .policy import PolicySettings

PlanState = Literal["DRAFT", "ARMED", "WAITING", "QUALIFIED", "INVALIDATED", "CONVERTED", "SKIPPED", "EXPIRED", "ARCHIVED"]
Direction = Literal["bullish", "bearish", "neutral", "volatility"]
Short = Annotated[str | None, Field(max_length=80)]


def _url(v: str | None) -> str | None:
    """Chart references are links or plain notes; only http(s) links are kept as links (no javascript: etc.)."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*:", v, re.I) and not re.match(r"^https?://", v, re.I):
        raise ValueError("only http:// or https:// links (or plain text)")
    return v[:500]


Url = Annotated[str | None, AfterValidator(_url)]


class PlanFields(Strict):
    title: Annotated[str | None, Field(max_length=160)] = None
    thesis: Note = None
    direction: Direction | None = None
    intended_strategy: Annotated[str | None, Field(max_length=40)] = None
    market_regime: Short = None
    setup_type: Short = None
    trigger: Note = None
    trigger_conditions: Note = None
    confirmation: Note = None
    confirmation_conditions: Note = None
    entry_zone_low: NonNegMoney | None = None
    entry_zone_high: NonNegMoney | None = None
    max_entry_debit: NonNegMoney | None = None
    min_entry_credit: NonNegMoney | None = None
    max_spread_pct: Annotated[float | None, Field(gt=0, le=500)] = None
    min_volume: Annotated[StrictInt | None, Field(ge=0)] = None
    min_open_interest: Annotated[StrictInt | None, Field(ge=0)] = None
    target: Note = None
    target_price: NonNegMoney | None = None
    invalidation: Note = None
    invalidation_conditions: Note = None
    invalidation_price: NonNegMoney | None = None
    planned_max_loss: NonNegMoney | None = None
    planned_contracts: Annotated[StrictInt | None, Field(gt=0, le=10_000)] = None
    expiration_from: date | None = None
    expiration_to: date | None = None
    delta_min: Annotated[float | None, Field(ge=0, le=1)] = None
    delta_max: Annotated[float | None, Field(ge=0, le=1)] = None
    time_stop: date | None = None
    event_risks: Note = None
    related_news: Note = None
    chart_reference: Url = None
    notes: Note = None
    legs: Annotated[list[LegIn] | None, Field(max_length=12)] = None     # planned legs, fill_price = planned price
    expires_at: UTC | None = None

    @model_validator(mode="after")
    def _ranges(self):
        if self.entry_zone_low is not None and self.entry_zone_high is not None and self.entry_zone_low > self.entry_zone_high:
            raise ValueError("entry zone: low is above high")
        if self.expiration_from and self.expiration_to and self.expiration_from > self.expiration_to:
            raise ValueError("expiration range: from is after to")
        if self.delta_min is not None and self.delta_max is not None and self.delta_min > self.delta_max:
            raise ValueError("delta range: min is above max")
        if self.max_entry_debit is not None and self.min_entry_credit is not None:
            raise ValueError("give a maximum entry debit or a minimum entry credit, not both")
        return self


class PlanCreate(PlanFields):
    account_id: str
    symbol: Symbol


class PlanUpdate(PlanFields):
    version: StrictInt


class PlanTransition(Strict):
    version: StrictInt
    to: PlanState
    reason: Annotated[str | None, Field(max_length=2000)] = None


class PlanConvert(Strict):
    version: StrictInt
    legs: Annotated[list[LegIn], Field(min_length=1, max_length=12)]
    opened_at: UTC | None = None
    name: Annotated[str | None, Field(max_length=160)] = None
    cash_secured: bool = False
    acknowledge_risk: bool = False
    acknowledge_rule_violation: bool = False
    underlying_price_at_entry: NonNegMoney | None = None
    notes: Note = None


class PlanOut(BaseModel):
    id: str
    account_id: str
    symbol: str
    title: str
    state: PlanState
    state_changed_at: datetime
    fields: dict[str, Any]
    legs: list[dict]
    expires_at: datetime | None
    outcome_reason: str | None
    risk: dict[str, Any] | None
    validation: dict[str, Any] | None = None
    position_id: str | None
    converted_at: datetime | None
    comparison: dict[str, Any] | None
    allowed_transitions: list[str]
    monitor: dict[str, Any] | None = None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


class PolicyIn(Strict):
    settings: PolicySettings
    notes: Note = None
