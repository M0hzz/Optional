"""monitoring and events

Phase 2: position and contract snapshots, monitor state, the event feed, notification deliveries, alert
rules and monitor runs. On `positions` it adds `monitor_json` (per-position rules), lifts the Phase 1
constraint that kept `monitoring_enabled` at 0, and switches monitoring on for positions that are OPEN.

Nothing existing is deleted. downgrade() drops the Phase 2 tables (their snapshots, events and alert
rules are lost), turns monitoring off again and restores the Phase 1 constraint. Back up first.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01 00:48:27.855018
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('events',
    sa.Column('seq', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('occurred_at', sa.DateTime(), nullable=False),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('symbol', sa.String(length=16), nullable=True),
    sa.Column('account_id', sa.String(length=36), nullable=True),
    sa.Column('position_id', sa.String(length=36), nullable=True),
    sa.Column('contract_id', sa.String(length=36), nullable=True),
    sa.Column('alert_rule_id', sa.String(length=36), nullable=True),
    sa.Column('snapshot_id', sa.String(length=36), nullable=True),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('dedupe_key', sa.String(length=200), nullable=True),
    sa.Column('source_json', sa.JSON(), nullable=False),
    sa.Column('requires_ack', sa.Boolean(), nullable=False),
    sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
    sa.Column('ack_note', sa.Text(), nullable=True),
    sa.Column('notify_state', sa.String(length=16), nullable=False),
    sa.Column('notify_detail', sa.String(length=300), nullable=True),
    sa.CheckConstraint("severity IN ('info', 'warning', 'critical')", name='ck_events_severity'),
    sa.PrimaryKeyConstraint('seq'),
    sa.UniqueConstraint('id'),
    sqlite_autoincrement=True
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index('ix_events_contract', ['contract_id', 'seq'], unique=False)
        batch_op.create_index('ix_events_dedupe', ['dedupe_key', 'occurred_at'], unique=False)
        batch_op.create_index('ix_events_occurred', ['occurred_at'], unique=False)
        batch_op.create_index('ix_events_position', ['position_id', 'seq'], unique=False)
        batch_op.create_index('ix_events_symbol', ['symbol', 'seq'], unique=False)
        batch_op.create_index('ix_events_type', ['type', 'seq'], unique=False)
        batch_op.create_index('ix_events_unacked', ['requires_ack', 'acknowledged_at'], unique=False)

    op.create_table('monitor_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('seconds', sa.Double(), nullable=True),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('positions_checked', sa.Integer(), nullable=False),
    sa.Column('contracts_checked', sa.Integer(), nullable=False),
    sa.Column('rules_checked', sa.Integer(), nullable=False),
    sa.Column('events_emitted', sa.Integer(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('errors_json', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('monitor_runs', schema=None) as batch_op:
        batch_op.create_index('ix_monitor_runs_started', ['started_at'], unique=False)

    op.create_table('monitor_states',
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('subject_id', sa.String(length=64), nullable=False),
    sa.Column('key', sa.String(length=120), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=True),
    sa.Column('since', sa.DateTime(), nullable=True),
    sa.Column('candidate', sa.String(length=32), nullable=True),
    sa.Column('candidate_count', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('last_event_at', sa.DateTime(), nullable=True),
    sa.Column('last_cleared_at', sa.DateTime(), nullable=True),
    sa.Column('checked_at', sa.DateTime(), nullable=True),
    sa.Column('snapshot_id', sa.String(length=36), nullable=True),
    sa.Column('data_json', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('kind', 'subject_id', 'key')
    )
    op.create_table('notification_deliveries',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('event_seq', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('attempted_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('detail', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['event_seq'], ['events.seq'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('notification_deliveries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_deliveries_event_seq'), ['event_seq'], unique=False)

    op.create_table('position_snapshots',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('position_id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('taken_at', sa.DateTime(), nullable=False),
    sa.Column('raw_status', sa.String(length=24), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('reasons_json', sa.JSON(), nullable=False),
    sa.Column('rules_json', sa.JSON(), nullable=False),
    sa.Column('valuation_status', sa.String(length=16), nullable=False),
    sa.Column('value_units', sa.BigInteger(), nullable=True),
    sa.Column('exit_value_units', sa.BigInteger(), nullable=True),
    sa.Column('open_pnl_units', sa.BigInteger(), nullable=True),
    sa.Column('open_pnl_pct', sa.Double(), nullable=True),
    sa.Column('underlying_price_units', sa.BigInteger(), nullable=True),
    sa.Column('delta', sa.Double(), nullable=True),
    sa.Column('gamma', sa.Double(), nullable=True),
    sa.Column('theta', sa.Double(), nullable=True),
    sa.Column('vega', sa.Double(), nullable=True),
    sa.Column('metrics_json', sa.JSON(), nullable=False),
    sa.Column('legs_json', sa.JSON(), nullable=False),
    sa.Column('freshness', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('delay_minutes', sa.Integer(), nullable=False),
    sa.Column('fetched_at', sa.DateTime(), nullable=True),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('quote_age_seconds', sa.Double(), nullable=True),
    sa.Column('keep', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('position_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_position_snapshots_position_taken', ['position_id', 'taken_at'], unique=False)
        batch_op.create_index('ix_position_snapshots_taken', ['taken_at'], unique=False)

    op.create_table('alert_rules',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=True),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('target_type', sa.String(length=16), nullable=False),
    sa.Column('position_id', sa.String(length=36), nullable=True),
    sa.Column('contract_id', sa.String(length=36), nullable=True),
    sa.Column('symbol', sa.String(length=16), nullable=True),
    sa.Column('condition', sa.Text(), nullable=False),
    sa.Column('timeframe', sa.String(length=8), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('notify', sa.Boolean(), nullable=False),
    sa.Column('confirm_passes', sa.Integer(), nullable=False),
    sa.Column('rearm_minutes', sa.Integer(), nullable=False),
    sa.Column('repeat_minutes', sa.Integer(), nullable=False),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.CheckConstraint("(target_type = 'contract') = (contract_id IS NOT NULL)", name='ck_alert_rules_contract'),
    sa.CheckConstraint("(target_type = 'position') = (position_id IS NOT NULL)", name='ck_alert_rules_position'),
    sa.CheckConstraint("severity IN ('info', 'warning', 'critical')", name='ck_alert_rules_severity'),
    sa.CheckConstraint("target_type IN ('position', 'contract', 'symbol')", name='ck_alert_rules_target'),
    sa.CheckConstraint('confirm_passes >= 1 AND rearm_minutes >= 0 AND repeat_minutes >= 0', name='ck_alert_rules_timing'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['contract_id'], ['watched_contracts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alert_rules', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alert_rules_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_rules_contract_id'), ['contract_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_rules_position_id'), ['position_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_rules_symbol'), ['symbol'], unique=False)

    op.create_table('contract_snapshots',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('contract_id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('taken_at', sa.DateTime(), nullable=False),
    sa.Column('quote_status', sa.String(length=16), nullable=False),
    sa.Column('bid_units', sa.BigInteger(), nullable=True),
    sa.Column('ask_units', sa.BigInteger(), nullable=True),
    sa.Column('mid_units', sa.BigInteger(), nullable=True),
    sa.Column('last_units', sa.BigInteger(), nullable=True),
    sa.Column('underlying_price_units', sa.BigInteger(), nullable=True),
    sa.Column('iv', sa.Double(), nullable=True),
    sa.Column('delta', sa.Double(), nullable=True),
    sa.Column('volume', sa.Double(), nullable=True),
    sa.Column('open_interest', sa.Double(), nullable=True),
    sa.Column('spread_pct', sa.Double(), nullable=True),
    sa.Column('conditions_json', sa.JSON(), nullable=False),
    sa.Column('freshness', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('fetched_at', sa.DateTime(), nullable=True),
    sa.Column('keep', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['contract_id'], ['watched_contracts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('contract_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_contract_snapshots_contract_taken', ['contract_id', 'taken_at'], unique=False)

    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('monitor_json', sa.JSON(), nullable=False, server_default='{}'))
        batch_op.drop_constraint('ck_positions_not_monitored_phase1', type_='check')
    op.execute("UPDATE positions SET monitoring_enabled = 1 WHERE lifecycle = 'OPEN'")


def downgrade() -> None:
    op.execute("UPDATE positions SET monitoring_enabled = 0")
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.drop_column('monitor_json')
        batch_op.create_check_constraint('ck_positions_not_monitored_phase1', 'monitoring_enabled = 0')

    with op.batch_alter_table('contract_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_contract_snapshots_contract_taken')

    op.drop_table('contract_snapshots')
    with op.batch_alter_table('alert_rules', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alert_rules_symbol'))
        batch_op.drop_index(batch_op.f('ix_alert_rules_position_id'))
        batch_op.drop_index(batch_op.f('ix_alert_rules_contract_id'))
        batch_op.drop_index(batch_op.f('ix_alert_rules_account_id'))

    op.drop_table('alert_rules')
    with op.batch_alter_table('position_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_position_snapshots_taken')
        batch_op.drop_index('ix_position_snapshots_position_taken')

    op.drop_table('position_snapshots')
    with op.batch_alter_table('notification_deliveries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_deliveries_event_seq'))

    op.drop_table('notification_deliveries')
    op.drop_table('monitor_states')
    with op.batch_alter_table('monitor_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_monitor_runs_started')

    op.drop_table('monitor_runs')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index('ix_events_unacked')
        batch_op.drop_index('ix_events_type')
        batch_op.drop_index('ix_events_symbol')
        batch_op.drop_index('ix_events_position')
        batch_op.drop_index('ix_events_occurred')
        batch_op.drop_index('ix_events_dedupe')
        batch_op.drop_index('ix_events_contract')

    op.drop_table('events')
