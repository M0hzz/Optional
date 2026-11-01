"""trade plans and risk policies

Phase 3: `risk_policies` (one revision per change, the old one marked superseded) and `trade_plans`
(missions, with their state, conditions, planned legs, last risk check and the planned-vs-actual
comparison). On `positions` it adds `plan_id` (the plan a position came from), `entry_risk_json` (the risk
check when it was recorded) and `rule_violation`. Nothing existing changes. downgrade() drops the plans
and policies (their data is lost): back up first.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01 02:42:55.226553
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('risk_policies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('settings_json', sa.JSON(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('superseded_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('risk_policies', schema=None) as batch_op:
        batch_op.create_index('ix_risk_policies_account_revision', ['account_id', 'revision'], unique=True)

    op.create_table('trade_plans',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('state_changed_at', sa.DateTime(), nullable=False),
    sa.Column('thesis', sa.Text(), nullable=True),
    sa.Column('direction', sa.String(length=16), nullable=True),
    sa.Column('intended_strategy', sa.String(length=40), nullable=True),
    sa.Column('market_regime', sa.String(length=80), nullable=True),
    sa.Column('setup_type', sa.String(length=80), nullable=True),
    sa.Column('trigger', sa.Text(), nullable=True),
    sa.Column('trigger_conditions', sa.Text(), nullable=True),
    sa.Column('confirmation', sa.Text(), nullable=True),
    sa.Column('confirmation_conditions', sa.Text(), nullable=True),
    sa.Column('entry_zone_low_units', sa.BigInteger(), nullable=True),
    sa.Column('entry_zone_high_units', sa.BigInteger(), nullable=True),
    sa.Column('max_entry_debit_units', sa.BigInteger(), nullable=True),
    sa.Column('min_entry_credit_units', sa.BigInteger(), nullable=True),
    sa.Column('max_spread_pct', sa.Double(), nullable=True),
    sa.Column('min_volume', sa.Integer(), nullable=True),
    sa.Column('min_open_interest', sa.Integer(), nullable=True),
    sa.Column('target', sa.Text(), nullable=True),
    sa.Column('target_price_units', sa.BigInteger(), nullable=True),
    sa.Column('invalidation', sa.Text(), nullable=True),
    sa.Column('invalidation_conditions', sa.Text(), nullable=True),
    sa.Column('invalidation_price_units', sa.BigInteger(), nullable=True),
    sa.Column('planned_max_loss_units', sa.BigInteger(), nullable=True),
    sa.Column('planned_contracts', sa.Integer(), nullable=True),
    sa.Column('expiration_from', sa.Date(), nullable=True),
    sa.Column('expiration_to', sa.Date(), nullable=True),
    sa.Column('delta_min', sa.Double(), nullable=True),
    sa.Column('delta_max', sa.Double(), nullable=True),
    sa.Column('time_stop', sa.Date(), nullable=True),
    sa.Column('event_risks', sa.Text(), nullable=True),
    sa.Column('related_news', sa.Text(), nullable=True),
    sa.Column('chart_reference', sa.String(length=500), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('legs_json', sa.JSON(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('outcome_reason', sa.Text(), nullable=True),
    sa.Column('risk_json', sa.JSON(), nullable=False),
    sa.Column('position_id', sa.String(length=36), nullable=True),
    sa.Column('converted_at', sa.DateTime(), nullable=True),
    sa.Column('comparison_json', sa.JSON(), nullable=False),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.CheckConstraint("state != 'CONVERTED' OR converted_at IS NOT NULL", name='ck_trade_plans_converted'),
    sa.CheckConstraint("state IN ('DRAFT','ARMED','WAITING','QUALIFIED','INVALIDATED','CONVERTED','SKIPPED','EXPIRED','ARCHIVED')", name='ck_trade_plans_state'),
    sa.CheckConstraint('planned_contracts IS NULL OR planned_contracts > 0', name='ck_trade_plans_contracts'),
    sa.CheckConstraint('planned_max_loss_units IS NULL OR planned_max_loss_units >= 0', name='ck_trade_plans_loss'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('trade_plans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trade_plans_account_id'), ['account_id'], unique=False)
        batch_op.create_index('ix_trade_plans_account_state', ['account_id', 'state'], unique=False)
        batch_op.create_index(batch_op.f('ix_trade_plans_position_id'), ['position_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_trade_plans_state'), ['state'], unique=False)
        batch_op.create_index(batch_op.f('ix_trade_plans_symbol'), ['symbol'], unique=False)

    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('entry_risk_json', sa.JSON(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('rule_violation', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.create_index(batch_op.f('ix_positions_plan_id'), ['plan_id'], unique=False)
        batch_op.create_foreign_key('fk_positions_plan_id', 'trade_plans', ['plan_id'], ['id'], ondelete='SET NULL', use_alter=True)



def downgrade() -> None:
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_positions_plan_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_positions_plan_id'))
        batch_op.drop_column('rule_violation')
        batch_op.drop_column('entry_risk_json')
        batch_op.drop_column('plan_id')

    with op.batch_alter_table('trade_plans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trade_plans_symbol'))
        batch_op.drop_index(batch_op.f('ix_trade_plans_state'))
        batch_op.drop_index(batch_op.f('ix_trade_plans_position_id'))
        batch_op.drop_index('ix_trade_plans_account_state')
        batch_op.drop_index(batch_op.f('ix_trade_plans_account_id'))

    op.drop_table('trade_plans')
    with op.batch_alter_table('risk_policies', schema=None) as batch_op:
        batch_op.drop_index('ix_risk_policies_account_revision')

    op.drop_table('risk_policies')
