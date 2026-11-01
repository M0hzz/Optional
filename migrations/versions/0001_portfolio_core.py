"""portfolio core: accounts, positions, legs, watched contracts

Creates the Phase 1 portfolio tables. Money columns end in _units ($0.0001 integers); timestamps are
stored as UTC. positions.monitoring_enabled is constrained to 0 (ck_positions_not_monitored_phase1)
so nothing can mark a portfolio position as live-monitored before the Phase 2 migration lifts it.

downgrade() drops every portfolio table and its data. Back up first (docs/DEPLOY.md).

Revision ID: 0001
Revises: 
Create Date: 2026-10-31 21:29:09.154430
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('broker', sa.String(length=120), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.CheckConstraint("currency = 'USD'", name='ck_accounts_usd'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('audit_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('entity_type', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('before_json', sa.JSON(), nullable=True),
    sa.Column('after_json', sa.JSON(), nullable=True),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index('ix_audit_entity', ['entity_type', 'entity_id', 'at'], unique=False)

    op.create_table('idempotency_keys',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('scope', sa.String(length=64), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=False),
    sa.Column('response_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('user_preferences',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value_json', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('account_snapshots',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('as_of', sa.DateTime(), nullable=False),
    sa.Column('total_value_units', sa.BigInteger(), nullable=True),
    sa.Column('cash_units', sa.BigInteger(), nullable=True),
    sa.Column('buying_power_units', sa.BigInteger(), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('account_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_account_snapshots_account_asof', ['account_id', 'as_of'], unique=False)
        batch_op.create_index(batch_op.f('ix_account_snapshots_account_id'), ['account_id'], unique=False)

    op.create_table('positions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('underlying', sa.String(length=16), nullable=False),
    sa.Column('strategy', sa.String(length=40), nullable=False),
    sa.Column('strategy_variant', sa.String(length=40), nullable=True),
    sa.Column('strategy_exact', sa.Boolean(), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=False),
    sa.Column('lifecycle', sa.Enum('OPEN', 'CLOSED', 'ARCHIVED', name='lifecycle', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('opened_at', sa.DateTime(), nullable=False),
    sa.Column('closed_at', sa.DateTime(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('archive_reason', sa.String(length=40), nullable=True),
    sa.Column('net_premium_units', sa.BigInteger(), nullable=True),
    sa.Column('fees_units', sa.BigInteger(), nullable=False),
    sa.Column('max_loss_state', sa.Enum('bounded', 'unbounded', 'unknown', name='bound_loss', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('max_loss_units', sa.BigInteger(), nullable=True),
    sa.Column('max_gain_state', sa.Enum('bounded', 'unbounded', 'unknown', name='bound_gain', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('max_gain_units', sa.BigInteger(), nullable=True),
    sa.Column('defined_risk', sa.Boolean(), nullable=False),
    sa.Column('risk_acknowledged', sa.Boolean(), nullable=False),
    sa.Column('realized_pnl_units', sa.BigInteger(), nullable=True),
    sa.Column('monitoring_enabled', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.CheckConstraint("(lifecycle = 'OPEN' AND closed_at IS NULL) OR lifecycle != 'OPEN'", name='ck_positions_open'),
    sa.CheckConstraint("(max_gain_state = 'bounded') = (max_gain_units IS NOT NULL)", name='ck_positions_gain'),
    sa.CheckConstraint("(max_loss_state = 'bounded') = (max_loss_units IS NOT NULL)", name='ck_positions_loss'),
    sa.CheckConstraint('fees_units >= 0', name='ck_positions_fees'),
    sa.CheckConstraint('monitoring_enabled = 0', name='ck_positions_not_monitored_phase1'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_positions_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_positions_lifecycle'), ['lifecycle'], unique=False)
        batch_op.create_index(batch_op.f('ix_positions_underlying'), ['underlying'], unique=False)

    op.create_table('position_legs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('position_id', sa.String(length=36), nullable=False),
    sa.Column('leg_index', sa.Integer(), nullable=False),
    sa.Column('asset_type', sa.Enum('STOCK', 'OPTION', name='asset_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('option_right', sa.Enum('CALL', 'PUT', name='option_right', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('strike_units', sa.BigInteger(), nullable=True),
    sa.Column('expiration', sa.Date(), nullable=True),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('side', sa.Enum('BUY', 'SELL', name='side', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('multiplier', sa.Integer(), nullable=False),
    sa.Column('fill_price_units', sa.BigInteger(), nullable=True),
    sa.Column('fees_units', sa.BigInteger(), nullable=False),
    sa.Column('filled_at', sa.DateTime(), nullable=True),
    sa.Column('broker_order_id', sa.String(length=64), nullable=True),
    sa.Column('occ_symbol', sa.String(length=32), nullable=True),
    sa.Column('close_price_units', sa.BigInteger(), nullable=True),
    sa.Column('close_fees_units', sa.BigInteger(), nullable=True),
    sa.Column('closed_at', sa.DateTime(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("(asset_type = 'STOCK' AND option_right IS NULL AND strike_units IS NULL AND expiration IS NULL AND multiplier = 1) OR (asset_type = 'OPTION' AND option_right IS NOT NULL AND strike_units > 0 AND expiration IS NOT NULL AND multiplier > 0)", name='ck_legs_shape'),
    sa.CheckConstraint('fees_units >= 0 AND (close_fees_units IS NULL OR close_fees_units >= 0)', name='ck_legs_fees'),
    sa.CheckConstraint('fill_price_units IS NULL OR fill_price_units >= 0', name='ck_legs_price'),
    sa.CheckConstraint('quantity > 0', name='ck_legs_qty'),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('position_legs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_position_legs_expiration'), ['expiration'], unique=False)
        batch_op.create_index(batch_op.f('ix_position_legs_occ_symbol'), ['occ_symbol'], unique=False)
        batch_op.create_index(batch_op.f('ix_position_legs_position_id'), ['position_id'], unique=False)
        batch_op.create_index('ix_position_legs_position_index', ['position_id', 'leg_index'], unique=True)

    op.create_table('watched_contracts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=True),
    sa.Column('underlying', sa.String(length=16), nullable=False),
    sa.Column('option_right', sa.Enum('CALL', 'PUT', name='wc_option_right', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('strike_units', sa.BigInteger(), nullable=False),
    sa.Column('expiration', sa.Date(), nullable=False),
    sa.Column('occ_symbol', sa.String(length=32), nullable=False),
    sa.Column('status', sa.Enum('RESEARCHING', 'WAITING_FOR_CONFIRMATION', 'ENTRY_QUALIFIED', 'ENTRY_CONDITION_LOST', 'IN_POSITION', 'EXPIRED', 'ARCHIVED', name='contract_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('target_entry_units', sa.BigInteger(), nullable=True),
    sa.Column('invalidation', sa.Text(), nullable=True),
    sa.Column('alert_conditions', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('position_id', sa.String(length=36), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.CheckConstraint('strike_units > 0', name='ck_wc_strike'),
    sa.CheckConstraint('target_entry_units IS NULL OR target_entry_units >= 0', name='ck_wc_target'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('watched_contracts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_watched_contracts_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_watched_contracts_expiration'), ['expiration'], unique=False)
        batch_op.create_index(batch_op.f('ix_watched_contracts_occ_symbol'), ['occ_symbol'], unique=False)
        batch_op.create_index(batch_op.f('ix_watched_contracts_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_watched_contracts_underlying'), ['underlying'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('watched_contracts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_watched_contracts_underlying'))
        batch_op.drop_index(batch_op.f('ix_watched_contracts_status'))
        batch_op.drop_index(batch_op.f('ix_watched_contracts_occ_symbol'))
        batch_op.drop_index(batch_op.f('ix_watched_contracts_expiration'))
        batch_op.drop_index(batch_op.f('ix_watched_contracts_account_id'))

    op.drop_table('watched_contracts')
    with op.batch_alter_table('position_legs', schema=None) as batch_op:
        batch_op.drop_index('ix_position_legs_position_index')
        batch_op.drop_index(batch_op.f('ix_position_legs_position_id'))
        batch_op.drop_index(batch_op.f('ix_position_legs_occ_symbol'))
        batch_op.drop_index(batch_op.f('ix_position_legs_expiration'))

    op.drop_table('position_legs')
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_positions_underlying'))
        batch_op.drop_index(batch_op.f('ix_positions_lifecycle'))
        batch_op.drop_index(batch_op.f('ix_positions_account_id'))

    op.drop_table('positions')
    with op.batch_alter_table('account_snapshots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_account_snapshots_account_id'))
        batch_op.drop_index('ix_account_snapshots_account_asof')

    op.drop_table('account_snapshots')
    op.drop_table('user_preferences')
    op.drop_table('idempotency_keys')
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_entity')

    op.drop_table('audit_events')
    op.drop_table('accounts')
