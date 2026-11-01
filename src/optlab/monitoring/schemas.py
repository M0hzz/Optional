"""API models for monitoring: events, alert rules, per-position monitor settings."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StrictInt, model_validator

from ..portfolio.schemas import Note, Strict, Symbol
from .settings import RuleSet, Timeframe

TargetT = Literal["position", "contract", "symbol"]
SeverityT = Literal["info", "warning", "critical"]


class EventOut(BaseModel):
    id: str
    seq: int
    occurred_at: datetime
    type: str
    severity: SeverityT
    title: str
    message: str
    symbol: str | None
    account_id: str | None
    position_id: str | None
    contract_id: str | None
    alert_rule_id: str | None
    snapshot_id: str | None
    requires_ack: bool
    acknowledged_at: datetime | None
    ack_note: str | None
    notify: bool
    notify_state: str
    notify_detail: str | None
    source: dict[str, Any]


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    page: int
    page_size: int
    latest_seq: int


class AckIn(Strict):
    note: Note = None


class BulkAckIn(Strict):
    ids: Annotated[list[str] | None, Field(max_length=500)] = None
    up_to_seq: StrictInt | None = None           # every event with seq <= this (optionally for one position)
    position_id: str | None = None
    note: Note = None

    @model_validator(mode="after")
    def _one(self):
        if (self.ids is None) == (self.up_to_seq is None):
            raise ValueError("give either ids or up_to_seq")
        return self


class AlertRuleIn(Strict):
    name: Annotated[str, Field(min_length=1, max_length=160)]
    target_type: TargetT
    position_id: str | None = None
    contract_id: str | None = None
    symbol: Symbol | None = None
    account_id: str | None = None
    condition: Annotated[str, Field(min_length=1, max_length=300)]
    timeframe: Timeframe = "1d"
    severity: SeverityT = "warning"
    enabled: bool = True
    notify: bool = True
    confirm_passes: Annotated[StrictInt, Field(ge=1, le=20)] = 1
    rearm_minutes: Annotated[StrictInt, Field(ge=0, le=10080)] = 30
    repeat_minutes: Annotated[StrictInt, Field(ge=0, le=1440)] = 0
    notes: Note = None

    @model_validator(mode="after")
    def _target(self):
        need = {"position": self.position_id, "contract": self.contract_id, "symbol": self.symbol}
        if not need[self.target_type]:
            raise ValueError(f"a {self.target_type} alert needs {self.target_type}_id" if self.target_type != "symbol"
                             else "a symbol alert needs symbol")
        return self


class AlertRuleUpdate(Strict):
    version: StrictInt
    name: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    condition: Annotated[str | None, Field(min_length=1, max_length=300)] = None
    timeframe: Timeframe | None = None
    severity: SeverityT | None = None
    enabled: bool | None = None
    notify: bool | None = None
    confirm_passes: Annotated[StrictInt | None, Field(ge=1, le=20)] = None
    rearm_minutes: Annotated[StrictInt | None, Field(ge=0, le=10080)] = None
    repeat_minutes: Annotated[StrictInt | None, Field(ge=0, le=1440)] = None


class AlertRuleOut(BaseModel):
    id: str
    name: str
    target_type: TargetT
    position_id: str | None
    contract_id: str | None
    symbol: str | None
    account_id: str | None
    condition: str
    timeframe: str
    severity: SeverityT
    enabled: bool
    notify: bool
    confirm_passes: int
    rearm_minutes: int
    repeat_minutes: int
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
    state: dict[str, Any] | None = None


class PositionMonitorIn(Strict):
    version: StrictInt
    monitoring_enabled: bool | None = None
    use_defaults: bool | None = None            # True: follow the account-wide default rules
    rules: RuleSet | None = None                # this position's own rules (implies use_defaults False)
    timeframe: Timeframe | None = None
    notify: bool | None = None


class ValidateIn(Strict):
    condition: Annotated[str, Field(min_length=1, max_length=300)]
    target: TargetT = "position"


class YamlCommitIn(Strict):
    acknowledge_risk: bool = False
