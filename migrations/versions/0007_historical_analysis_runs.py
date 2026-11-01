"""historical analysis runs

Phase 6: one table, `analysis_runs`, holding each historical analysis opened from a plan, position, alert
rule, watched contract or event: the exact strategy and its version, the live-only clauses left out (and why),
the data source, the status and the results. No existing table or row changes; downgrade() drops the table.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01 19:38:34.312384
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('analysis_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('subject_type', sa.String(length=16), nullable=False),
    sa.Column('subject_id', sa.String(length=36), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('strategy_json', sa.JSON(), nullable=False),
    sa.Column('strategy_version', sa.String(length=16), nullable=False),
    sa.Column('excluded_json', sa.JSON(), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=True),
    sa.Column('focus_horizon', sa.String(length=8), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('seconds', sa.Float(), nullable=True),
    sa.Column('result_json', sa.JSON(), nullable=False),
    sa.Column('matches_json', sa.JSON(), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'running', 'done', 'error')", name='ck_analysis_runs_status'),
    sa.CheckConstraint("subject_type IN ('plan', 'position', 'alert_rule', 'contract', 'event', 'manual')", name='ck_analysis_runs_subject'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('analysis_runs', schema=None) as batch_op:
        batch_op.create_index('ix_analysis_runs_subject', ['subject_type', 'subject_id', 'created_at'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('analysis_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_analysis_runs_subject')

    op.drop_table('analysis_runs')
