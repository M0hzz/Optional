"""process journal reviews

Phase 4: `journal_entries` (archives), `trade_reviews` (mission reports), `process_awards` (the points
ledger: append-only, never negative), `daily_objectives` (manual check-offs and rest days) and
`user_achievements`. Adds tables only. downgrade() drops them (their data is lost): back up first.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01 13:52:03.583925
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('daily_objectives',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('done', sa.Boolean(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('daily_objectives', schema=None) as batch_op:
        batch_op.create_index('ix_daily_objectives_unique', ['account_id', 'day', 'code'], unique=True)

    op.create_table('process_awards',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('points', sa.Integer(), nullable=False),
    sa.Column('subject_type', sa.String(length=16), nullable=False),
    sa.Column('subject_id', sa.String(length=36), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('awarded_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('points >= 0', name='ck_process_awards_points'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('process_awards', schema=None) as batch_op:
        batch_op.create_index('ix_process_awards_account_day', ['account_id', 'day'], unique=False)
        batch_op.create_index(batch_op.f('ix_process_awards_account_id'), ['account_id'], unique=False)
        batch_op.create_index('ix_process_awards_unique', ['account_id', 'code', 'subject_type', 'subject_id'], unique=True)

    op.create_table('user_achievements',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('earned_at', sa.DateTime(), nullable=False),
    sa.Column('detail_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('user_achievements', schema=None) as batch_op:
        batch_op.create_index('ix_user_achievements_unique', ['account_id', 'code'], unique=True)

    op.create_table('trade_reviews',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('position_id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('followed_plan', sa.String(length=16), nullable=True),
    sa.Column('exit_reason', sa.String(length=24), nullable=True),
    sa.Column('exit_followed_plan', sa.String(length=16), nullable=True),
    sa.Column('mistakes_json', sa.JSON(), nullable=False),
    sa.Column('went_well', sa.Text(), nullable=True),
    sa.Column('went_wrong', sa.Text(), nullable=True),
    sa.Column('lessons', sa.Text(), nullable=True),
    sa.Column('rule_violation_acknowledged', sa.Boolean(), nullable=False),
    sa.Column('would_take_again', sa.String(length=8), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('position_id')
    )
    with op.batch_alter_table('trade_reviews', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trade_reviews_account_id'), ['account_id'], unique=False)

    op.create_table('journal_entries',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('entry_date', sa.Date(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('tags_json', sa.JSON(), nullable=False),
    sa.Column('position_id', sa.String(length=36), nullable=True),
    sa.Column('plan_id', sa.String(length=36), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_timestamp', sa.DateTime(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.CheckConstraint("kind IN ('daily_thesis','eod_review','decision','note','lesson')", name='ck_journal_kind'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['plan_id'], ['trade_plans.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.create_index('ix_journal_account_date', ['account_id', 'entry_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_journal_entries_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_journal_entries_entry_date'), ['entry_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_journal_entries_plan_id'), ['plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_journal_entries_position_id'), ['position_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_journal_entries_position_id'))
        batch_op.drop_index(batch_op.f('ix_journal_entries_plan_id'))
        batch_op.drop_index(batch_op.f('ix_journal_entries_entry_date'))
        batch_op.drop_index(batch_op.f('ix_journal_entries_account_id'))
        batch_op.drop_index('ix_journal_account_date')

    op.drop_table('journal_entries')
    with op.batch_alter_table('trade_reviews', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trade_reviews_account_id'))

    op.drop_table('trade_reviews')
    with op.batch_alter_table('user_achievements', schema=None) as batch_op:
        batch_op.drop_index('ix_user_achievements_unique')

    op.drop_table('user_achievements')
    with op.batch_alter_table('process_awards', schema=None) as batch_op:
        batch_op.drop_index('ix_process_awards_unique')
        batch_op.drop_index(batch_op.f('ix_process_awards_account_id'))
        batch_op.drop_index('ix_process_awards_account_day')

    op.drop_table('process_awards')
    with op.batch_alter_table('daily_objectives', schema=None) as batch_op:
        batch_op.drop_index('ix_daily_objectives_unique')

    op.drop_table('daily_objectives')
