"""score history and auditable ledger

Phase 4 completion: finished days get immutable, versioned score records (`daily_scores`); the points
ledger (`process_awards`) gains entry types (award / revocation / correction / replacement), rule versions,
evidence, source ids, account time zone, backfill flag and references to the entries it adjusts;
achievements record their rule version. Database triggers make `daily_scores`, `process_awards` and
`user_achievements` append-only (UPDATE and DELETE are refused).

Existing awards are kept as `award` entries of points-policy/v1 with evidence {"legacy": true} (they were
created before evidence was recorded) and the account time zone from the preferences.
downgrade() removes the triggers, the score table and the new columns (adjustment entries are lost).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01 15:23:53.765369
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


TRIGGERS = [
    ("daily_scores", "finished daily scores are immutable; add a revision instead"),
    ("process_awards", "the points ledger is append-only; add a revocation or correction instead"),
    ("user_achievements", "achievements are append-only"),
]


def upgrade() -> None:
    op.create_table('daily_scores',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('account_date', sa.Date(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('policy_code', sa.String(length=40), nullable=False),
    sa.Column('policy_version', sa.String(length=16), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('supersedes_id', sa.String(length=36), nullable=True),
    sa.Column('correction_reason', sa.Text(), nullable=True),
    sa.Column('calculated_at', sa.DateTime(), nullable=False),
    sa.Column('backfill', sa.Boolean(), nullable=False),
    sa.Column('rest_day', sa.Boolean(), nullable=False),
    sa.Column('input_snapshot', sa.JSON(), nullable=False),
    sa.Column('components_json', sa.JSON(), nullable=False),
    sa.Column('numerator', sa.Integer(), nullable=False),
    sa.Column('denominator', sa.Integer(), nullable=False),
    sa.Column('percentage', sa.Integer(), nullable=True),
    sa.Column('points', sa.Integer(), nullable=False),
    sa.Column('source_ids_json', sa.JSON(), nullable=False),
    sa.CheckConstraint('numerator >= 0 AND denominator >= numerator', name='ck_daily_scores_counts'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('daily_scores', schema=None) as batch_op:
        batch_op.create_index('ix_daily_scores_revision', ['account_id', 'account_date', 'revision'], unique=True)

    with op.batch_alter_table('process_awards', schema=None) as batch_op:
        batch_op.add_column(sa.Column('entry_type', sa.String(length=16), nullable=False, server_default='award'))
        batch_op.add_column(sa.Column('rule_version', sa.String(length=40), nullable=False, server_default='points-policy/v1'))
        batch_op.add_column(sa.Column('source_ids_json', sa.JSON(), nullable=False, server_default='[]'))
        batch_op.add_column(sa.Column('evidence_json', sa.JSON(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('timezone', sa.String(length=64), nullable=False, server_default='America/New_York'))
        batch_op.add_column(sa.Column('backfill', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('references_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('reason', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('actor', sa.String(length=64), nullable=False, server_default='system'))
        batch_op.add_column(sa.Column('audit_id', sa.String(length=36), nullable=True))
        batch_op.drop_constraint('ck_process_awards_points', type_='check')
        batch_op.drop_index('ix_process_awards_unique')
        batch_op.create_check_constraint('ck_process_awards_type',
                                         "entry_type IN ('award', 'revocation', 'correction', 'replacement')")
        batch_op.create_check_constraint('ck_process_awards_points',
                                         "(entry_type IN ('award', 'replacement') AND points >= 0) OR "
                                         "(entry_type = 'revocation' AND points <= 0) OR entry_type = 'correction'")
        batch_op.create_check_constraint('ck_process_awards_reference', "entry_type = 'award' OR references_id IS NOT NULL")
        batch_op.create_index('ix_process_awards_unique', ['account_id', 'code', 'subject_type', 'subject_id'], unique=True,
                              sqlite_where=sa.text("entry_type = 'award'"))
        batch_op.create_index('ix_process_awards_subject', ['account_id', 'code', 'subject_type', 'subject_id'], unique=False)

    with op.batch_alter_table('user_achievements', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rule_version', sa.String(length=40), nullable=False,
                                      server_default='achievement-rules/v1'))

    # existing awards: legacy evidence, and the time zone the user chose (if any)
    op.execute("""UPDATE process_awards SET evidence_json = '{"legacy": true}'""")
    op.execute("""UPDATE process_awards SET timezone = COALESCE((SELECT json_extract(value_json, '$.value')
                  FROM user_preferences WHERE key = 'timezone'), 'America/New_York')""")
    for table, msg in TRIGGERS:
        op.execute(f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, '{msg}'); END")
        op.execute(f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, '{msg}'); END")


def downgrade() -> None:
    for table, _ in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
    op.execute("DELETE FROM process_awards WHERE entry_type != 'award'")
    with op.batch_alter_table('user_achievements', schema=None) as batch_op:
        batch_op.drop_column('rule_version')

    with op.batch_alter_table('process_awards', schema=None) as batch_op:
        batch_op.drop_index('ix_process_awards_subject')
        batch_op.drop_index('ix_process_awards_unique')
        batch_op.drop_constraint('ck_process_awards_reference', type_='check')
        batch_op.drop_constraint('ck_process_awards_points', type_='check')
        batch_op.drop_constraint('ck_process_awards_type', type_='check')
        batch_op.create_check_constraint('ck_process_awards_points', 'points >= 0')
        batch_op.create_index('ix_process_awards_unique', ['account_id', 'code', 'subject_type', 'subject_id'], unique=True)
        batch_op.drop_column('audit_id')
        batch_op.drop_column('actor')
        batch_op.drop_column('reason')
        batch_op.drop_column('references_id')
        batch_op.drop_column('backfill')
        batch_op.drop_column('timezone')
        batch_op.drop_column('evidence_json')
        batch_op.drop_column('source_ids_json')
        batch_op.drop_column('rule_version')
        batch_op.drop_column('entry_type')

    with op.batch_alter_table('daily_scores', schema=None) as batch_op:
        batch_op.drop_index('ix_daily_scores_revision')

    op.drop_table('daily_scores')
