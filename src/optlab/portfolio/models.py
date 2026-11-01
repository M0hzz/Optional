"""SQLAlchemy 2 models for the portfolio database. Schema changes go through Alembic (migrations/).

Conventions
    ids          UUIDv7 strings (ids.py)
    money        integer columns ending in `_units`, in $0.0001 (money.py)
    quantities   integers (shares, contracts); fractional shares are not supported yet
    time         timezone-aware UTC in Python, stored as UTC (UTCDateTime)
    enums        stored as text with CHECK constraints, so a bad value can't be written
    archival     rows are archived (archived_at), not deleted; accounts with positions can't be deleted
    versioning   `version` increments on every update; edits must send the version they read
"""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, Float, ForeignKey, Index,
                        Integer, String, Text, TypeDecorator, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Accepts only timezone-aware datetimes; stores UTC; returns aware UTC."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: optlab stores timezone-aware UTC timestamps only")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime(), dict[str, Any]: JSON}


class Lifecycle(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class AssetType(str, enum.Enum):
    STOCK = "STOCK"
    OPTION = "OPTION"


class OptionRight(str, enum.Enum):
    CALL = "CALL"
    PUT = "PUT"


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class Bound(str, enum.Enum):
    BOUNDED = "bounded"
    UNBOUNDED = "unbounded"
    UNKNOWN = "unknown"


class ContractStatus(str, enum.Enum):
    RESEARCHING = "RESEARCHING"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    ENTRY_QUALIFIED = "ENTRY_QUALIFIED"
    ENTRY_CONDITION_LOST = "ENTRY_CONDITION_LOST"
    IN_POSITION = "IN_POSITION"
    EXPIRED = "EXPIRED"
    ARCHIVED = "ARCHIVED"


def _enum(e: type[enum.Enum], name: str):
    return Enum(e, name=name, native_enum=False, create_constraint=True, length=32,
                values_callable=lambda x: [m.value for m in x], validate_strings=True)


class Audited:
    """created/updated stamps, provenance and free-form metadata. Versioned tables add `version`
    (SQLAlchemy version_id_col: incremented on every UPDATE, and an UPDATE against a stale version fails)."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    source_timestamp: Mapped[datetime | None]
    metadata_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Account(Audited, Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    broker: Mapped[str | None] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None]
    snapshots: Mapped[list[AccountSnapshot]] = relationship(back_populates="account", order_by="AccountSnapshot.as_of")
    __table_args__ = (CheckConstraint("currency = 'USD'", name="ck_accounts_usd"),)
    version: Mapped[int] = mapped_column(Integer)
    __mapper_args__ = {"version_id_col": version}


class AccountSnapshot(Base):
    """Manually entered (Phase 1) or broker-synced (later) account values. Append-only."""
    __tablename__ = "account_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    as_of: Mapped[datetime]
    total_value_units: Mapped[int | None] = mapped_column(BigInteger)
    cash_units: Mapped[int | None] = mapped_column(BigInteger)
    buying_power_units: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    source_timestamp: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    account: Mapped[Account] = relationship(back_populates="snapshots")
    __table_args__ = (Index("ix_account_snapshots_account_asof", "account_id", "as_of"),)


class Position(Audited, Base):
    __tablename__ = "positions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(40))           # recognize.Strategy value, CUSTOM if not exact
    strategy_variant: Mapped[str | None] = mapped_column(String(40))
    strategy_exact: Mapped[bool] = mapped_column(Boolean, default=False)
    direction: Mapped[str] = mapped_column(String(16), default="unknown")
    lifecycle: Mapped[Lifecycle] = mapped_column(_enum(Lifecycle, "lifecycle"), default=Lifecycle.OPEN, index=True)
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    archived_at: Mapped[datetime | None]
    archive_reason: Mapped[str | None] = mapped_column(String(40))
    # Entry analytics, recorded from the legs at creation / edit (recognize.analyze)
    net_premium_units: Mapped[int | None] = mapped_column(BigInteger)      # + debit paid, − credit received
    fees_units: Mapped[int] = mapped_column(BigInteger, default=0)
    max_loss_state: Mapped[Bound] = mapped_column(_enum(Bound, "bound_loss"), default=Bound.UNKNOWN)
    max_loss_units: Mapped[int | None] = mapped_column(BigInteger)
    max_gain_state: Mapped[Bound] = mapped_column(_enum(Bound, "bound_gain"), default=Bound.UNKNOWN)
    max_gain_units: Mapped[int | None] = mapped_column(BigInteger)
    defined_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    realized_pnl_units: Mapped[int | None] = mapped_column(BigInteger)
    # Read by the portfolio monitor (optlab.monitoring) when True and the position is OPEN.
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-position monitor settings: {"rules": {"exit_when": [...], "action_when": [...], "watch_when": [...]} | None
    # (None = the account-wide defaults), "timeframe": "1d", "notify": true}
    monitor_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Phase 3: the plan this position came from, the risk check when it was recorded, and whether it was
    # recorded although it broke a risk rule (kept honestly, for review)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("trade_plans.id", ondelete="SET NULL", use_alter=True,
                                                           name="fk_positions_plan_id"), index=True)
    entry_risk_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    rule_violation: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    legs: Mapped[list[PositionLeg]] = relationship(back_populates="position", order_by="PositionLeg.leg_index",
                                                   cascade="all, delete-orphan")
    __table_args__ = (
        CheckConstraint("(lifecycle = 'OPEN' AND closed_at IS NULL) OR lifecycle != 'OPEN'", name="ck_positions_open"),
        CheckConstraint("(max_loss_state = 'bounded') = (max_loss_units IS NOT NULL)", name="ck_positions_loss"),
        CheckConstraint("(max_gain_state = 'bounded') = (max_gain_units IS NOT NULL)", name="ck_positions_gain"),
        CheckConstraint("fees_units >= 0", name="ck_positions_fees"),
    )
    version: Mapped[int] = mapped_column(Integer)
    __mapper_args__ = {"version_id_col": version}


class PositionLeg(Base):
    __tablename__ = "position_legs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"), index=True)
    leg_index: Mapped[int] = mapped_column(Integer)
    asset_type: Mapped[AssetType] = mapped_column(_enum(AssetType, "asset_type"))
    option_right: Mapped[OptionRight | None] = mapped_column(_enum(OptionRight, "option_right"))
    strike_units: Mapped[int | None] = mapped_column(BigInteger)
    expiration: Mapped[date | None] = mapped_column(Date, index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    side: Mapped[Side] = mapped_column(_enum(Side, "side"))
    multiplier: Mapped[int] = mapped_column(Integer)
    fill_price_units: Mapped[int | None] = mapped_column(BigInteger)
    fees_units: Mapped[int] = mapped_column(BigInteger, default=0)
    filled_at: Mapped[datetime | None]
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    occ_symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    close_price_units: Mapped[int | None] = mapped_column(BigInteger)
    close_fees_units: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    position: Mapped[Position] = relationship(back_populates="legs")
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_legs_qty"),
        CheckConstraint("fees_units >= 0 AND (close_fees_units IS NULL OR close_fees_units >= 0)", name="ck_legs_fees"),
        CheckConstraint("fill_price_units IS NULL OR fill_price_units >= 0", name="ck_legs_price"),
        CheckConstraint("(asset_type = 'STOCK' AND option_right IS NULL AND strike_units IS NULL AND expiration IS NULL "
                        "AND multiplier = 1) OR (asset_type = 'OPTION' AND option_right IS NOT NULL AND strike_units > 0 "
                        "AND expiration IS NOT NULL AND multiplier > 0)", name="ck_legs_shape"),
        Index("ix_position_legs_position_index", "position_id", "leg_index", unique=True),
    )


class WatchedContract(Audited, Base):
    __tablename__ = "watched_contracts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    option_right: Mapped[OptionRight] = mapped_column(_enum(OptionRight, "wc_option_right"))
    strike_units: Mapped[int] = mapped_column(BigInteger)
    expiration: Mapped[date] = mapped_column(Date, index=True)
    occ_symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[ContractStatus] = mapped_column(_enum(ContractStatus, "contract_status"),
                                                   default=ContractStatus.RESEARCHING, index=True)
    target_entry_units: Mapped[int | None] = mapped_column(BigInteger)
    invalidation: Mapped[str | None] = mapped_column(Text)
    alert_conditions: Mapped[str | None] = mapped_column(Text)     # entry conditions, one per line (monitoring/rules.py)
    notes: Mapped[str | None] = mapped_column(Text)
    position_id: Mapped[str | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"))
    archived_at: Mapped[datetime | None]
    __table_args__ = (CheckConstraint("strike_units > 0", name="ck_wc_strike"),
                      CheckConstraint("target_entry_units IS NULL OR target_entry_units >= 0", name="ck_wc_target"))
    version: Mapped[int] = mapped_column(Integer)
    __mapper_args__ = {"version_id_col": version}


class UserPreference(Base):
    __tablename__ = "user_preferences"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuditEvent(Base):
    """Before/after record of every change to fills, closed positions and other financial facts.
    Append-only: nothing in the application updates or deletes these rows."""
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id", "at"),)


class IdempotencyRecord(Base):
    """A repeated POST with the same Idempotency-Key returns the first response instead of creating twice."""
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# ====================================================================== Phase 2: monitoring and events
class PositionSnapshot(Base):
    """One monitoring pass's view of one position. Append-only; older ones may be thinned to one per hour
    (retention), except those marked `keep` (every status change)."""
    __tablename__ = "position_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"))
    run_id: Mapped[str | None] = mapped_column(String(36))
    taken_at: Mapped[datetime]
    raw_status: Mapped[str] = mapped_column(String(24))          # what this pass's data says
    status: Mapped[str] = mapped_column(String(24))              # confirmed status after this pass
    reasons_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    rules_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    valuation_status: Mapped[str] = mapped_column(String(16))    # AVAILABLE | PARTIAL | UNAVAILABLE
    value_units: Mapped[int | None] = mapped_column(BigInteger)
    exit_value_units: Mapped[int | None] = mapped_column(BigInteger)
    open_pnl_units: Mapped[int | None] = mapped_column(BigInteger)
    open_pnl_pct: Mapped[float | None]
    underlying_price_units: Mapped[int | None] = mapped_column(BigInteger)
    delta: Mapped[float | None]
    gamma: Mapped[float | None]
    theta: Mapped[float | None]
    vega: Mapped[float | None]
    metrics_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    legs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    freshness: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(64))
    delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime | None]
    source_timestamp: Mapped[datetime | None]
    quote_age_seconds: Mapped[float | None]
    keep: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (Index("ix_position_snapshots_position_taken", "position_id", "taken_at"),
                      Index("ix_position_snapshots_taken", "taken_at"))


class ContractSnapshot(Base):
    """A watched contract's quote at one monitoring pass (bid/ask history)."""
    __tablename__ = "contract_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contract_id: Mapped[str] = mapped_column(ForeignKey("watched_contracts.id", ondelete="CASCADE"))
    run_id: Mapped[str | None] = mapped_column(String(36))
    taken_at: Mapped[datetime]
    quote_status: Mapped[str] = mapped_column(String(16))        # FOUND | NOT_FOUND | UNAVAILABLE
    bid_units: Mapped[int | None] = mapped_column(BigInteger)
    ask_units: Mapped[int | None] = mapped_column(BigInteger)
    mid_units: Mapped[int | None] = mapped_column(BigInteger)
    last_units: Mapped[int | None] = mapped_column(BigInteger)
    underlying_price_units: Mapped[int | None] = mapped_column(BigInteger)
    iv: Mapped[float | None]
    delta: Mapped[float | None]
    volume: Mapped[float | None]
    open_interest: Mapped[float | None]
    spread_pct: Mapped[float | None]
    conditions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    freshness: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime | None]
    keep: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (Index("ix_contract_snapshots_contract_taken", "contract_id", "taken_at"),)


class MonitorState(Base):
    """Small mutable state the monitor keeps between passes (confirmation counters, which warnings already
    fired, whether an alert is armed). Kept apart from the versioned rows so a pass never conflicts with
    a user's edit."""
    __tablename__ = "monitor_states"
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)        # position | contract | alert_rule | warning | monitor
    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(120), primary_key=True, default="")
    status: Mapped[str | None] = mapped_column(String(32))
    since: Mapped[datetime | None]
    candidate: Mapped[str | None] = mapped_column(String(32))
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_event_at: Mapped[datetime | None]
    last_cleared_at: Mapped[datetime | None]
    checked_at: Mapped[datetime | None]
    snapshot_id: Mapped[str | None] = mapped_column(String(36))
    data_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Event(Base):
    """The command-center feed: status changes, alerts, warnings and record changes. Append-only except
    for acknowledgement and notification state. `seq` orders events across processes (the live stream
    resumes from it)."""
    __tablename__ = "events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id)
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow)
    type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(16))             # info | warning | critical
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(String(16))
    account_id: Mapped[str | None] = mapped_column(String(36))
    position_id: Mapped[str | None] = mapped_column(String(36))
    contract_id: Mapped[str | None] = mapped_column(String(36))
    alert_rule_id: Mapped[str | None] = mapped_column(String(36))
    snapshot_id: Mapped[str | None] = mapped_column(String(36))
    run_id: Mapped[str | None] = mapped_column(String(36))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    source_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    requires_ack: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_at: Mapped[datetime | None]
    ack_note: Mapped[str | None] = mapped_column(Text)
    notify_state: Mapped[str] = mapped_column(String(16), default="none")   # none | pending | sent | failed | suppressed | unavailable
    notify_detail: Mapped[str | None] = mapped_column(String(300))
    __table_args__ = (
        CheckConstraint("severity IN ('info', 'warning', 'critical')", name="ck_events_severity"),
        Index("ix_events_occurred", "occurred_at"), Index("ix_events_position", "position_id", "seq"),
        Index("ix_events_contract", "contract_id", "seq"), Index("ix_events_symbol", "symbol", "seq"),
        Index("ix_events_type", "type", "seq"), Index("ix_events_unacked", "requires_ack", "acknowledged_at"),
        Index("ix_events_dedupe", "dedupe_key", "occurred_at"),
        {"sqlite_autoincrement": True},
    )


class NotificationDelivery(Base):
    """One attempt to deliver an event on one channel (desktop = notify-send on the server)."""
    __tablename__ = "notification_deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_seq: Mapped[int] = mapped_column(ForeignKey("events.seq", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    attempted_at: Mapped[datetime] = mapped_column(default=utcnow)
    status: Mapped[str] = mapped_column(String(16))               # sent | failed | unavailable
    detail: Mapped[str | None] = mapped_column(String(300))


class AlertRule(Audited, Base):
    """A user-defined condition on a position, a watched contract or a symbol. Fires once when it becomes
    true (after `confirm_passes`), re-arms only after it has cleared and `rearm_minutes` have passed,
    and repeats every `repeat_minutes` while true when that is set."""
    __tablename__ = "alert_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    target_type: Mapped[str] = mapped_column(String(16))          # position | contract | symbol
    position_id: Mapped[str | None] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"), index=True)
    contract_id: Mapped[str | None] = mapped_column(ForeignKey("watched_contracts.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    condition: Mapped[str] = mapped_column(Text)
    timeframe: Mapped[str] = mapped_column(String(8), default="1d")
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    confirm_passes: Mapped[int] = mapped_column(Integer, default=1)
    rearm_minutes: Mapped[int] = mapped_column(Integer, default=30)
    repeat_minutes: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None]
    __table_args__ = (
        CheckConstraint("target_type IN ('position', 'contract', 'symbol')", name="ck_alert_rules_target"),
        CheckConstraint("(target_type = 'position') = (position_id IS NOT NULL)", name="ck_alert_rules_position"),
        CheckConstraint("(target_type = 'contract') = (contract_id IS NOT NULL)", name="ck_alert_rules_contract"),
        CheckConstraint("severity IN ('info', 'warning', 'critical')", name="ck_alert_rules_severity"),
        CheckConstraint("confirm_passes >= 1 AND rearm_minutes >= 0 AND repeat_minutes >= 0", name="ck_alert_rules_timing"),
    )
    version: Mapped[int] = mapped_column(Integer)
    __mapper_args__ = {"version_id_col": version}


class MonitorRun(Base):
    """One monitoring pass: when, how long, what it checked, what went wrong."""
    __tablename__ = "monitor_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    seconds: Mapped[float | None]
    trigger: Mapped[str] = mapped_column(String(16))              # schedule | manual
    mode: Mapped[str] = mapped_column(String(16))                 # live | synthetic
    provider: Mapped[str] = mapped_column(String(64))
    positions_checked: Mapped[int] = mapped_column(Integer, default=0)
    contracts_checked: Mapped[int] = mapped_column(Integer, default=0)
    rules_checked: Mapped[int] = mapped_column(Integer, default=0)
    events_emitted: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    errors_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    __table_args__ = (Index("ix_monitor_runs_started", "started_at"),)


# ====================================================================== Phase 3: trade plans and risk
class RiskPolicy(Base):
    """One revision of an account's risk policy. Changing the policy adds a revision and marks the previous
    one superseded, so every past risk check can be explained by the policy it used."""
    __tablename__ = "risk_policies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    superseded_at: Mapped[datetime | None]
    __table_args__ = (Index("ix_risk_policies_account_revision", "account_id", "revision", unique=True),)


PLAN_STATES = ("DRAFT", "ARMED", "WAITING", "QUALIFIED", "INVALIDATED", "CONVERTED", "SKIPPED", "EXPIRED", "ARCHIVED")


class TradePlan(Audited, Base):
    """A mission: the plan written before a position exists. Prices are per share in $0.0001 units, except
    planned_max_loss_units (dollars for the whole position). Kept unchanged after conversion."""
    __tablename__ = "trade_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(16), default="DRAFT", index=True)
    state_changed_at: Mapped[datetime] = mapped_column(default=utcnow)
    thesis: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str | None] = mapped_column(String(16))           # bullish | bearish | neutral | volatility
    intended_strategy: Mapped[str | None] = mapped_column(String(40))   # recognize.Strategy value
    market_regime: Mapped[str | None] = mapped_column(String(80))
    setup_type: Mapped[str | None] = mapped_column(String(80))
    trigger: Mapped[str | None] = mapped_column(Text)
    trigger_conditions: Mapped[str | None] = mapped_column(Text)        # one per line, checked by the monitor
    confirmation: Mapped[str | None] = mapped_column(Text)
    confirmation_conditions: Mapped[str | None] = mapped_column(Text)
    entry_zone_low_units: Mapped[int | None] = mapped_column(BigInteger)
    entry_zone_high_units: Mapped[int | None] = mapped_column(BigInteger)
    max_entry_debit_units: Mapped[int | None] = mapped_column(BigInteger)
    min_entry_credit_units: Mapped[int | None] = mapped_column(BigInteger)
    max_spread_pct: Mapped[float | None]
    min_volume: Mapped[int | None] = mapped_column(Integer)
    min_open_interest: Mapped[int | None] = mapped_column(Integer)
    target: Mapped[str | None] = mapped_column(Text)
    target_price_units: Mapped[int | None] = mapped_column(BigInteger)
    invalidation: Mapped[str | None] = mapped_column(Text)
    invalidation_conditions: Mapped[str | None] = mapped_column(Text)
    invalidation_price_units: Mapped[int | None] = mapped_column(BigInteger)
    planned_max_loss_units: Mapped[int | None] = mapped_column(BigInteger)
    planned_contracts: Mapped[int | None] = mapped_column(Integer)
    expiration_from: Mapped[date | None] = mapped_column(Date)
    expiration_to: Mapped[date | None] = mapped_column(Date)
    delta_min: Mapped[float | None]
    delta_max: Mapped[float | None]
    time_stop: Mapped[date | None] = mapped_column(Date)
    event_risks: Mapped[str | None] = mapped_column(Text)
    related_news: Mapped[str | None] = mapped_column(Text)
    chart_reference: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    legs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)    # planned legs (optional)
    expires_at: Mapped[datetime | None]
    outcome_reason: Mapped[str | None] = mapped_column(Text)            # why skipped / invalidated
    risk_json: Mapped[dict[str, Any]] = mapped_column(default=dict)     # the last risk check
    position_id: Mapped[str | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), index=True)
    converted_at: Mapped[datetime | None]
    comparison_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    archived_at: Mapped[datetime | None]
    __table_args__ = (
        CheckConstraint("state IN ('DRAFT','ARMED','WAITING','QUALIFIED','INVALIDATED','CONVERTED','SKIPPED','EXPIRED',"
                        "'ARCHIVED')", name="ck_trade_plans_state"),
        CheckConstraint("planned_contracts IS NULL OR planned_contracts > 0", name="ck_trade_plans_contracts"),
        CheckConstraint("planned_max_loss_units IS NULL OR planned_max_loss_units >= 0", name="ck_trade_plans_loss"),
        CheckConstraint("state != 'CONVERTED' OR converted_at IS NOT NULL",
                        name="ck_trade_plans_converted"),
        Index("ix_trade_plans_account_state", "account_id", "state"),
    )
    version: Mapped[int] = mapped_column(Integer)
    __mapper_args__ = {"version_id_col": version}


# ====================================================================== Phase 4: process, journal, reviews
JOURNAL_KINDS = ("daily_thesis", "eod_review", "decision", "note", "lesson")


class JournalEntry(Audited, Base):
    """Archives: notes, daily theses, end-of-day reviews and decisions, optionally about a position or a plan.
    Edits keep the earlier text in the audit log."""
    __tablename__ = "journal_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    entry_date: Mapped[date] = mapped_column(Date, index=True)       # the day it's about, in the display time zone
    title: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    position_id: Mapped[str | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), index=True)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("trade_plans.id", ondelete="SET NULL"), index=True)
    archived_at: Mapped[datetime | None]
    __table_args__ = (CheckConstraint("kind IN ('daily_thesis','eod_review','decision','note','lesson')",
                                      name="ck_journal_kind"),
                      Index("ix_journal_account_date", "account_id", "entry_date"))
    version: Mapped[int] = mapped_column(Integer)
    __mapper_args__ = {"version_id_col": version}


class TradeReview(Audited, Base):
    """Mission report: the review of a closed position. Process questions only; the realized P&L is shown
    next to it but never scored."""
    __tablename__ = "trade_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"), unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    followed_plan: Mapped[str | None] = mapped_column(String(16))      # yes | partly | no | no_plan
    exit_reason: Mapped[str | None] = mapped_column(String(24))        # target | stop | invalidation | time_stop | risk_rule | discretionary | expired | assigned | other
    exit_followed_plan: Mapped[str | None] = mapped_column(String(16))  # yes | partly | no | no_plan
    mistakes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    went_well: Mapped[str | None] = mapped_column(Text)
    went_wrong: Mapped[str | None] = mapped_column(Text)
    lessons: Mapped[str | None] = mapped_column(Text)
    rule_violation_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    would_take_again: Mapped[str | None] = mapped_column(String(8))     # yes | no | unsure
    completed_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(Integer)
    __mapper_args__ = {"version_id_col": version}


class ProcessAward(Base):
    """The points ledger. Append-only (database triggers refuse UPDATE and DELETE). Points come only from
    process facts, never from profit or the number of trades.

    entry_type
        award        a rule's points for a subject (+), once per code and subject (partial unique index)
        revocation   removes an earlier award or replacement (−, the same points), with the reason
        correction   adjusts an earlier entry by a delta (±), with the reason
        replacement  grants the points again after a revocation (+), when the evidence is restored
    Every adjustment references the entry it adjusts (`references_id`), says why, who or what caused it
    (`actor`) and links the audit record. Effective points = the sum of all entries.
    """
    __tablename__ = "process_awards"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    entry_type: Mapped[str] = mapped_column(String(16), default="award")
    code: Mapped[str] = mapped_column(String(40))
    rule_version: Mapped[str] = mapped_column(String(40), default="points-policy/v1")
    category: Mapped[str] = mapped_column(String(16))                   # discipline | research | risk_control | review
    points: Mapped[int] = mapped_column(Integer)
    subject_type: Mapped[str] = mapped_column(String(16))               # position | plan | review | day
    subject_id: Mapped[str] = mapped_column(String(36))
    source_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    day: Mapped[date] = mapped_column(Date)                              # account date
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(String(300))
    references_id: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    audit_id: Mapped[str | None] = mapped_column(String(36))
    awarded_at: Mapped[datetime] = mapped_column(default=utcnow)        # creation timestamp
    __table_args__ = (
        CheckConstraint("entry_type IN ('award', 'revocation', 'correction', 'replacement')", name="ck_process_awards_type"),
        CheckConstraint("(entry_type IN ('award', 'replacement') AND points >= 0) OR (entry_type = 'revocation' AND points <= 0)"
                        " OR entry_type = 'correction'", name="ck_process_awards_points"),
        CheckConstraint("entry_type = 'award' OR references_id IS NOT NULL", name="ck_process_awards_reference"),
        Index("ix_process_awards_unique", "account_id", "code", "subject_type", "subject_id", unique=True,
              sqlite_where=text("entry_type = 'award'")),
        Index("ix_process_awards_account_day", "account_id", "day"),
        Index("ix_process_awards_subject", "account_id", "code", "subject_type", "subject_id"),
    )


class DailyScore(Base):
    """A finished account day's process score: immutable (triggers refuse UPDATE and DELETE). When the
    evidence for a finished day changes, a new revision is added that references the one it supersedes and
    says why; earlier revisions stay. Scores computed under an older scoring version are never recomputed."""
    __tablename__ = "daily_scores"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    account_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    policy_code: Mapped[str] = mapped_column(String(40))                # process-score
    policy_version: Mapped[str] = mapped_column(String(16))             # v1
    revision: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[str | None] = mapped_column(String(36))
    correction_reason: Mapped[str | None] = mapped_column(Text)
    calculated_at: Mapped[datetime] = mapped_column(default=utcnow)
    backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    rest_day: Mapped[bool] = mapped_column(Boolean, default=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    components_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    numerator: Mapped[int] = mapped_column(Integer)
    denominator: Mapped[int] = mapped_column(Integer)
    percentage: Mapped[int | None] = mapped_column(Integer)
    points: Mapped[int] = mapped_column(Integer)
    source_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    __table_args__ = (Index("ix_daily_scores_revision", "account_id", "account_date", "revision", unique=True),
                      CheckConstraint("numerator >= 0 AND denominator >= numerator", name="ck_daily_scores_counts"))


class DailyObjective(Base):
    """A manually checked daily objective (the automatic ones are computed from the records), or a rest day."""
    __tablename__ = "daily_objectives"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    day: Mapped[date] = mapped_column(Date)
    code: Mapped[str] = mapped_column(String(40))
    done: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (Index("ix_daily_objectives_unique", "account_id", "day", "code", unique=True),)


class UserAchievement(Base):
    """An achievement, earned once (unique per account and code; triggers refuse UPDATE and DELETE), with the
    rule version and the evidence it was earned on."""
    __tablename__ = "user_achievements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(40))
    rule_version: Mapped[str] = mapped_column(String(40), default="achievement-rules/v1")
    earned_at: Mapped[datetime] = mapped_column(default=utcnow)
    detail_json: Mapped[dict[str, Any]] = mapped_column(default=dict)          # the evidence
    __table_args__ = (Index("ix_user_achievements_unique", "account_id", "code", unique=True),)


# ------------------------------------------------------------------ news and sentiment (Phase 5, migration 0006)
class NewsSource(Base):
    """A news or official-event provider (SEC EDGAR, Federal Reserve, BLS, BEA, company IR feeds, Finnhub):
    whether it's enabled, how much weight its items get, and the result of its last fetch."""
    __tablename__ = "news_sources"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))                    # filings | macro | central_bank | company | news
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    credibility: Mapped[float] = mapped_column(Float, default=0.5)   # 0..1, weight in the symbol features
    status: Mapped[str] = mapped_column(String(16), default="never")  # never | ok | error | disabled | unavailable
    status_detail: Mapped[str | None] = mapped_column(Text)
    last_fetch_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    items_last: Mapped[int] = mapped_column(Integer, default=0)
    new_last: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    __table_args__ = (CheckConstraint("credibility >= 0 AND credibility <= 1", name="ck_news_sources_credibility"),
                      CheckConstraint("status IN ('never', 'ok', 'error', 'disabled', 'unavailable')", name="ck_news_sources_status"))


class NewsCluster(Base):
    """One story: the articles that report the same thing (exact URL or near-identical headline, close in time)."""
    __tablename__ = "news_clusters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lead_article_id: Mapped[str | None] = mapped_column(String(36))
    first_seen_at: Mapped[datetime]
    first_published_at: Mapped[datetime | None]
    last_published_at: Mapped[datetime | None]
    article_count: Mapped[int] = mapped_column(Integer, default=1)
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    category: Mapped[str | None] = mapped_column(String(32))


class NewsArticle(Base):
    """A normalized item from a source. `published_at` is the source's publication time (None when the source
    gives none: collection time is never used in its place); `first_seen_at` is when optlab first saw it."""
    __tablename__ = "news_articles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_code: Mapped[str] = mapped_column(ForeignKey("news_sources.code", ondelete="RESTRICT"))
    external_id: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_origin: Mapped[str] = mapped_column(String(20), default="none")   # provider | opening_sentences | none
    published_at: Mapped[datetime | None]
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    raw_json: Mapped[dict[str, Any]] = mapped_column(default=dict)            # the provider's metadata, as received
    fingerprint: Mapped[str] = mapped_column(String(64))
    cluster_id: Mapped[str | None] = mapped_column(ForeignKey("news_clusters.id", ondelete="SET NULL"))
    is_cluster_lead: Mapped[bool] = mapped_column(Boolean, default=True)
    duplicate_of: Mapped[str | None] = mapped_column(String(36))              # exact duplicate (same URL)
    category: Mapped[str | None] = mapped_column(String(32))
    category_confidence: Mapped[float | None] = mapped_column(Float)
    classification_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    novelty: Mapped[float] = mapped_column(Float, default=1.0)
    __table_args__ = (Index("ix_news_articles_external", "source_code", "external_id", unique=True),
                      Index("ix_news_articles_published", "published_at"),
                      Index("ix_news_articles_seen", "first_seen_at"),
                      Index("ix_news_articles_url", "canonical_url"),
                      Index("ix_news_articles_cluster", "cluster_id"),
                      CheckConstraint("summary_origin IN ('provider', 'opening_sentences', 'none')", name="ck_news_articles_summary"))


class NewsEntity(Base):
    """A symbol an article is about, how it was matched and how relevant the article is to it (0..1)."""
    __tablename__ = "news_entities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(ForeignKey("news_articles.id", ondelete="CASCADE"))
    symbol: Mapped[str] = mapped_column(String(16))
    cik: Mapped[str | None] = mapped_column(String(10))
    method: Mapped[str] = mapped_column(String(16))      # filer | provider | exchange | cashtag | name | market
    relevance: Mapped[float] = mapped_column(Float)
    evidence: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_news_entities_unique", "article_id", "symbol", unique=True),
                      Index("ix_news_entities_symbol", "symbol"),
                      CheckConstraint("relevance >= 0 AND relevance <= 1", name="ck_news_entities_relevance"))


class NewsEvent(Base):
    """An event: a classified article (kind 'article') or a scheduled official release (kind 'scheduled':
    FOMC meetings, BLS and BEA release dates) used for event awareness."""
    __tablename__ = "news_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))
    external_key: Mapped[str] = mapped_column(String(300))
    source_code: Mapped[str] = mapped_column(ForeignKey("news_sources.code", ondelete="RESTRICT"))
    article_id: Mapped[str | None] = mapped_column(ForeignKey("news_articles.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime | None]                          # publication time, or the scheduled time
    importance: Mapped[str] = mapped_column(String(8), default="medium")
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    details_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (Index("ix_news_events_key", "source_code", "kind", "external_key", unique=True),
                      Index("ix_news_events_at", "at"),
                      CheckConstraint("kind IN ('article', 'scheduled')", name="ck_news_events_kind"),
                      CheckConstraint("importance IN ('high', 'medium', 'low')", name="ck_news_events_importance"))


class SentimentScore(Base):
    """Dictionary sentiment of one article under one model version, with the matched words that explain it."""
    __tablename__ = "sentiment_scores"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(ForeignKey("news_articles.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(String(32))                 # loughran-mcdonald
    model_version: Mapped[str] = mapped_column(String(80))         # dictionary file + scoring rules version
    label: Mapped[str] = mapped_column(String(10))                 # positive | negative | neutral | unknown
    score: Mapped[float | None] = mapped_column(Float)             # (positive − negative) ÷ (positive + negative)
    confidence: Mapped[float] = mapped_column(Float)
    word_count: Mapped[int] = mapped_column(Integer)
    positive: Mapped[int] = mapped_column(Integer, default=0)
    negative: Mapped[int] = mapped_column(Integer, default=0)
    uncertainty: Mapped[int] = mapped_column(Integer, default=0)
    litigious: Mapped[int] = mapped_column(Integer, default=0)
    constraining: Mapped[int] = mapped_column(Integer, default=0)
    terms_json: Mapped[dict[str, Any]] = mapped_column(default=dict)   # category -> matched words (and negated positives)
    computed_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (Index("ix_sentiment_scores_unique", "article_id", "model_version", unique=True),
                      CheckConstraint("label IN ('positive', 'negative', 'neutral', 'unknown')", name="ck_sentiment_label"),
                      CheckConstraint("score IS NULL OR (score >= -1 AND score <= 1)", name="ck_sentiment_score"))


class SymbolNewsFeature(Base):
    """The latest aggregated news features of a symbol (or `_MARKET` for macro and central-bank items)."""
    __tablename__ = "symbol_news_features"
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    as_of: Mapped[datetime]
    features_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    model_version: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
