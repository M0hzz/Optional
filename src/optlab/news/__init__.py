"""News and official-event sources (Phase 5): a confirmation and risk-awareness layer, never a trade signal.

Pipeline (pipeline.ingest):
    fetch (providers.py) → normalize (normalize.py) → store raw metadata → map symbols (symbols.py) →
    deduplicate (dedupe.py) → classify the event (classify.py) → score sentiment (sentiment.py: Loughran–McDonald
    dictionary, deterministic, explainable) → confidence, relevance, novelty, source weight, time decay →
    aggregate symbol features (features.py) → scans (monitoring rules) and dashboard widgets.

Rules this package keeps:
    - No generative AI. Summaries are the provider's own, or the item's cleaned opening sentences.
    - `published_at` is the source's publication time; when a source gives none it stays empty. The time
      optlab first saw an item is `first_seen_at`, and it is never used as the publication time.
    - No output of this package is a BUY or SELL instruction. News can confirm a monitoring condition, but a
      condition can't consist of news alone (monitoring/rules.py).
    - Providers fail independently: one unavailable source is recorded and shown; the others still run.
"""
