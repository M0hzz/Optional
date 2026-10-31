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

from sqlalchemy import (JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Integer,
                        String, Text, TypeDecorator)
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
    # Phase 2 switches positions on for the live monitor; until then nothing reads them for monitoring.
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    legs: Mapped[list[PositionLeg]] = relationship(back_populates="position", order_by="PositionLeg.leg_index",
                                                   cascade="all, delete-orphan")
    __table_args__ = (
        CheckConstraint("(lifecycle = 'OPEN' AND closed_at IS NULL) OR lifecycle != 'OPEN'", name="ck_positions_open"),
        CheckConstraint("(max_loss_state = 'bounded') = (max_loss_units IS NOT NULL)", name="ck_positions_loss"),
        CheckConstraint("(max_gain_state = 'bounded') = (max_gain_units IS NOT NULL)", name="ck_positions_gain"),
        CheckConstraint("fees_units >= 0", name="ck_positions_fees"),
        CheckConstraint("monitoring_enabled = 0", name="ck_positions_not_monitored_phase1"),
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
    alert_conditions: Mapped[str | None] = mapped_column(Text)     # stored only; evaluated from Phase 2
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
