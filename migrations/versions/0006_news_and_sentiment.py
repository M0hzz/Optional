"""news and sentiment

Phase 5: news and official-event sources (SEC EDGAR, Federal Reserve, BLS, BEA, company IR feeds, optional
Finnhub). New tables only; no existing table or row changes:

    news_sources            providers, enabled flag, credibility weight, last fetch status
    news_articles           normalized items: publication time (never collection time) and first-seen time
    news_entities           symbols an article is about, how they were matched, relevance
    news_clusters           duplicate stories grouped (same URL or near-identical headline)
    news_events             classified articles and scheduled official releases
    sentiment_scores        Loughran-McDonald dictionary sentiment with the matched words
    symbol_news_features    latest aggregated news features per symbol

downgrade() drops these tables (news history is lost; back up first).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01 16:02:13.509517
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('news_clusters',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('lead_article_id', sa.String(length=36), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('first_published_at', sa.DateTime(), nullable=True),
    sa.Column('last_published_at', sa.DateTime(), nullable=True),
    sa.Column('article_count', sa.Integer(), nullable=False),
    sa.Column('source_count', sa.Integer(), nullable=False),
    sa.Column('symbols_json', sa.JSON(), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('news_sources',
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('credibility', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('status_detail', sa.Text(), nullable=True),
    sa.Column('last_fetch_at', sa.DateTime(), nullable=True),
    sa.Column('last_success_at', sa.DateTime(), nullable=True),
    sa.Column('items_last', sa.Integer(), nullable=False),
    sa.Column('new_last', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('never', 'ok', 'error', 'disabled', 'unavailable')", name='ck_news_sources_status'),
    sa.CheckConstraint('credibility >= 0 AND credibility <= 1', name='ck_news_sources_credibility'),
    sa.PrimaryKeyConstraint('code')
    )
    op.create_table('symbol_news_features',
    sa.Column('symbol', sa.String(length=16), nullable=False),
    sa.Column('as_of', sa.DateTime(), nullable=False),
    sa.Column('features_json', sa.JSON(), nullable=False),
    sa.Column('model_version', sa.String(length=80), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('symbol')
    )
    op.create_table('news_articles',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('source_code', sa.String(length=32), nullable=False),
    sa.Column('external_id', sa.String(length=300), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('canonical_url', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('summary_origin', sa.String(length=20), nullable=False),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('raw_json', sa.JSON(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('cluster_id', sa.String(length=36), nullable=True),
    sa.Column('is_cluster_lead', sa.Boolean(), nullable=False),
    sa.Column('duplicate_of', sa.String(length=36), nullable=True),
    sa.Column('category', sa.String(length=32), nullable=True),
    sa.Column('category_confidence', sa.Float(), nullable=True),
    sa.Column('classification_json', sa.JSON(), nullable=False),
    sa.Column('novelty', sa.Float(), nullable=False),
    sa.CheckConstraint("summary_origin IN ('provider', 'opening_sentences', 'none')", name='ck_news_articles_summary'),
    sa.ForeignKeyConstraint(['cluster_id'], ['news_clusters.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_code'], ['news_sources.code'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('news_articles', schema=None) as batch_op:
        batch_op.create_index('ix_news_articles_cluster', ['cluster_id'], unique=False)
        batch_op.create_index('ix_news_articles_external', ['source_code', 'external_id'], unique=True)
        batch_op.create_index('ix_news_articles_published', ['published_at'], unique=False)
        batch_op.create_index('ix_news_articles_seen', ['first_seen_at'], unique=False)
        batch_op.create_index('ix_news_articles_url', ['canonical_url'], unique=False)

    op.create_table('news_entities',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('article_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=16), nullable=False),
    sa.Column('cik', sa.String(length=10), nullable=True),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('relevance', sa.Float(), nullable=False),
    sa.Column('evidence', sa.Text(), nullable=True),
    sa.CheckConstraint('relevance >= 0 AND relevance <= 1', name='ck_news_entities_relevance'),
    sa.ForeignKeyConstraint(['article_id'], ['news_articles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('news_entities', schema=None) as batch_op:
        batch_op.create_index('ix_news_entities_symbol', ['symbol'], unique=False)
        batch_op.create_index('ix_news_entities_unique', ['article_id', 'symbol'], unique=True)

    op.create_table('news_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('external_key', sa.String(length=300), nullable=False),
    sa.Column('source_code', sa.String(length=32), nullable=False),
    sa.Column('article_id', sa.String(length=36), nullable=True),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=True),
    sa.Column('importance', sa.String(length=8), nullable=False),
    sa.Column('symbols_json', sa.JSON(), nullable=False),
    sa.Column('details_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("importance IN ('high', 'medium', 'low')", name='ck_news_events_importance'),
    sa.CheckConstraint("kind IN ('article', 'scheduled')", name='ck_news_events_kind'),
    sa.ForeignKeyConstraint(['article_id'], ['news_articles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_code'], ['news_sources.code'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('news_events', schema=None) as batch_op:
        batch_op.create_index('ix_news_events_at', ['at'], unique=False)
        batch_op.create_index('ix_news_events_key', ['source_code', 'kind', 'external_key'], unique=True)

    op.create_table('sentiment_scores',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('article_id', sa.String(length=36), nullable=False),
    sa.Column('model', sa.String(length=32), nullable=False),
    sa.Column('model_version', sa.String(length=80), nullable=False),
    sa.Column('label', sa.String(length=10), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('word_count', sa.Integer(), nullable=False),
    sa.Column('positive', sa.Integer(), nullable=False),
    sa.Column('negative', sa.Integer(), nullable=False),
    sa.Column('uncertainty', sa.Integer(), nullable=False),
    sa.Column('litigious', sa.Integer(), nullable=False),
    sa.Column('constraining', sa.Integer(), nullable=False),
    sa.Column('terms_json', sa.JSON(), nullable=False),
    sa.Column('computed_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("label IN ('positive', 'negative', 'neutral', 'unknown')", name='ck_sentiment_label'),
    sa.CheckConstraint('score IS NULL OR (score >= -1 AND score <= 1)', name='ck_sentiment_score'),
    sa.ForeignKeyConstraint(['article_id'], ['news_articles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sentiment_scores', schema=None) as batch_op:
        batch_op.create_index('ix_sentiment_scores_unique', ['article_id', 'model_version'], unique=True)



def downgrade() -> None:
    with op.batch_alter_table('sentiment_scores', schema=None) as batch_op:
        batch_op.drop_index('ix_sentiment_scores_unique')

    op.drop_table('sentiment_scores')
    with op.batch_alter_table('news_events', schema=None) as batch_op:
        batch_op.drop_index('ix_news_events_key')
        batch_op.drop_index('ix_news_events_at')

    op.drop_table('news_events')
    with op.batch_alter_table('news_entities', schema=None) as batch_op:
        batch_op.drop_index('ix_news_entities_unique')
        batch_op.drop_index('ix_news_entities_symbol')

    op.drop_table('news_entities')
    with op.batch_alter_table('news_articles', schema=None) as batch_op:
        batch_op.drop_index('ix_news_articles_url')
        batch_op.drop_index('ix_news_articles_seen')
        batch_op.drop_index('ix_news_articles_published')
        batch_op.drop_index('ix_news_articles_external')
        batch_op.drop_index('ix_news_articles_cluster')

    op.drop_table('news_articles')
    op.drop_table('symbol_news_features')
    op.drop_table('news_sources')
    op.drop_table('news_clusters')
