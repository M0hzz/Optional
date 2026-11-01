"""The ingest run: fetch every enabled provider (outside any database transaction), then store, map,
de-duplicate, classify, score, aggregate and announce, in one write transaction. Idempotent: an item already
stored (same source and external id) is skipped, so re-running a fetch adds nothing.

De-duplication (dedupe-v1)
    exact     the same canonical URL as a stored article (any source): marked `duplicate_of` it
    near      a stored article first seen within 72 hours, published within 48 hours of this one (when both
              have a time), with the same figures in the headline, and either
                - the same headline fingerprint (same significant words), or
                - from a different source, headline similarity ≥ 0.5 with a company symbol in common, or ≥ 0.7
                  when both are market-wide (official sources use formulaic titles, so the bar is higher)
              joins that article's cluster (story).
    Duplicates join the story's cluster; novelty = 1 ÷ (1 + articles already in the cluster).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..portfolio.models import (AlertRule, Lifecycle, NewsArticle, NewsCluster, NewsEntity, NewsEvent, NewsSource,
                                Position, SentimentScore, SymbolNewsFeature, TradePlan, WatchedContract)
from . import features as feat
from .classify import CATEGORIES, RISK, WHY, classify_text
from .http import FetchError, Get, http_get
from .normalize import canonical_url, clean_text, fingerprint, numbers, opening_sentences, safe_url, similarity
from .providers import ALL, Ctx, Fetched, Provider, RawItem
from .sentiment import LoughranMcDonald, default_model
from .settings import NewsSettings
from .symbols import MARKET, SymbolMap, map_entities

log = logging.getLogger("optlab.news")
DEDUPE_HOURS, SIMILAR_COMPANY, SIMILAR_MARKET = 72, 0.5, 0.7
FEATURE_LOOKBACK = timedelta(days=8)
NOT_A_SIGNAL = "For awareness only: news never places, suggests or blocks an order."


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SourceResult:
    code: str
    status: str
    detail: str | None = None
    items: int = 0
    new: int = 0
    duplicates: int = 0
    scheduled: int = 0


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: datetime | None = None
    sources: list[SourceResult] = field(default_factory=list)
    new_articles: int = 0
    rescored: int = 0
    features: int = 0
    events: int = 0
    model: str | None = None
    model_note: str | None = None

    def as_dict(self) -> dict:
        return {"started_at": self.started_at, "finished_at": self.finished_at, "new_articles": self.new_articles,
                "rescored": self.rescored, "features": self.features, "events": self.events, "model": self.model,
                "model_note": self.model_note, "sources": [s.__dict__ for s in self.sources]}


# ------------------------------------------------------------------ sources and followed symbols
def ensure_sources(s: Session, st: NewsSettings, providers: list[Provider] = ALL) -> dict[str, NewsSource]:
    rows = {r.code: r for r in s.scalars(select(NewsSource))}
    for p in providers:
        if p.code not in rows:
            rows[p.code] = NewsSource(code=p.code, name=p.name, kind=p.kind, enabled=p.code not in st.disabled_sources,
                                      credibility=st.credibility.get(p.code, 0.5))
            s.add(rows[p.code])
        else:
            r = rows[p.code]
            r.name, r.kind = p.name, p.kind
            if p.code in st.credibility and abs(r.credibility - st.credibility[p.code]) > 1e-9:
                r.credibility = st.credibility[p.code]
    s.flush()
    return rows


def followed_symbols(s: Session, st: NewsSettings) -> tuple[set[str], set[str]]:
    """(followed, held). Held: open positions, live and draft plans, watched contracts, symbol alert rules.
    Followed: held plus the config universe and news.symbols."""
    held: set = set()
    held |= {u for (u,) in s.execute(select(Position.underlying).where(Position.lifecycle == Lifecycle.OPEN))}
    held |= {u for (u,) in s.execute(select(TradePlan.symbol).where(TradePlan.state.in_(["DRAFT", "ARMED", "WAITING", "QUALIFIED"])))}
    held |= {u for (u,) in s.execute(select(WatchedContract.underlying).where(WatchedContract.archived_at.is_(None)))}
    held |= {u for (u,) in s.execute(select(AlertRule.symbol).where(AlertRule.symbol.is_not(None), AlertRule.archived_at.is_(None)))}
    try:
        from ..config import load_config

        universe = {str(x).upper() for x in load_config().get("universe", [])}
    except (OSError, KeyError, TypeError):
        universe = set()
    held = {str(h).upper() for h in held if h}
    return held | universe | set(st.extra_symbols), held


def symbol_map(st: NewsSettings) -> SymbolMap:
    m, _ = SymbolMap.cached(st.cache_dir / "sec_company_tickers.json", max_age_days=3650)
    return m or SymbolMap()


# ------------------------------------------------------------------ the run
def fetch_all(st: NewsSettings, rows: dict[str, NewsSource], followed: set[str], smap: SymbolMap, now: datetime,
              get: Get | None = None, providers: list[Provider] = ALL, state: dict | None = None) -> dict[str, tuple[str, Any]]:
    """Network only. code -> ('ok', Fetched) | ('error' | 'unavailable' | 'disabled', message)."""
    state = state if state is not None else {}
    getter: Get = get or (lambda url, headers: http_get(url, headers, st.timeout_seconds))
    out: dict[str, tuple[str, Any]] = {}
    for p in providers:
        row = rows.get(p.code)
        if row is not None and not row.enabled:
            out[p.code] = ("disabled", "Switched off.")
            continue
        ok, why = p.available(st)
        if not ok:
            out[p.code] = ("unavailable", why)
            continue
        ctx = Ctx(st, getter, followed, smap, now, state.setdefault(p.code, {}))
        try:
            got = p.fetch(ctx)
            smap = ctx.smap if len(ctx.smap) else smap
            out[p.code] = ("ok", got)
        except (FetchError, ValueError, KeyError, TypeError) as exc:
            out[p.code] = ("error", str(exc)[:500] or type(exc).__name__)
        except Exception as exc:                          # a provider bug must not stop the others
            log.exception("news provider %s crashed", p.code)
            out[p.code] = ("error", f"{type(exc).__name__} in the {p.name} provider")
    out["_smap"] = ("ok", smap)
    return out


def ingest(db, st: NewsSettings, *, now: datetime | None = None, get: Get | None = None,
           providers: list[Provider] = ALL, model: LoughranMcDonald | None = None, model_note: str | None = None,
           state: dict | None = None, emit_events: bool = True) -> RunSummary:
    now = now or utcnow()
    summ = RunSummary(started_at=now)
    if model is None and model_note is None:
        model, model_note = default_model(st.lexicon_path)
    summ.model, summ.model_note = (model.version if model else None), model_note
    with db.session(write=True) as s:
        rows = ensure_sources(s, st, providers)
        followed, held = followed_symbols(s, st)
        for r in rows.values():
            s.expunge(r)
    smap = symbol_map(st)
    fetched = fetch_all(st, rows, followed, smap, now, get, providers, state)
    smap = fetched.pop("_smap")[1]
    with db.session(write=True) as s:
        rows = {r.code: r for r in s.scalars(select(NewsSource))}
        new_ids: list[str] = []
        for p in providers:
            status, payload = fetched[p.code]
            row = rows[p.code]
            res = SourceResult(p.code, status)
            row.last_fetch_at = now if status in ("ok", "error") else row.last_fetch_at
            if status == "ok":
                got: Fetched = payload
                res.items = len(got.items)
                for it in got.items:
                    a, dup = store_item(s, row, it, smap, followed, now, model)
                    if a is not None:
                        res.new += 1
                        res.duplicates += int(dup)
                        new_ids.append(a.id)
                res.scheduled = store_scheduled(s, row, got.scheduled)
                row.status, row.last_success_at = "ok", now
                row.status_detail = ("; ".join(got.notes)[:1000] if got.notes else None)
                row.items_last, row.new_last = res.items, res.new
            else:
                row.status, row.status_detail = status, payload
                row.items_last = row.new_last = 0
            res.detail = row.status_detail
            summ.sources.append(res)
        s.flush()
        summ.new_articles = len(new_ids)
        if model is not None:
            summ.rescored = rescore(s, model)
        summ.features, shifts = refresh_features(s, followed | {MARKET}, now, model is not None)
        if emit_events:
            summ.events = announce(s, new_ids, held, shifts, now)
    summ.finished_at = utcnow()
    return summ


# ------------------------------------------------------------------ storing one item
def store_item(s: Session, src: NewsSource, it: RawItem, smap: SymbolMap, followed: set[str], now: datetime,
               model: LoughranMcDonald | None) -> tuple[NewsArticle | None, bool]:
    ext = (it.external_id or "").strip()[:300]
    title = clean_text(it.title, 500)
    if not ext or not title:
        return None, False
    if s.scalar(select(NewsArticle.id).where(NewsArticle.source_code == src.code, NewsArticle.external_id == ext)):
        return None, False                                      # already stored: idempotent
    summary, origin = (clean_text(it.summary), it.summary_origin) if it.summary else (None, "none")
    if summary and origin == "opening_sentences":
        summary = opening_sentences(summary)
    if not summary:
        origin = "none"
    url = safe_url(it.url)
    canon = canonical_url(url)
    ents = map_entities(title, summary, smap, followed, filer_cik=it.filer_cik, provider_symbols=it.provider_symbols,
                        company_symbol=it.company_symbol, market=it.market)
    cls = it.classification or classify_text(title, summary)
    a = NewsArticle(source_code=src.code, external_id=ext, url=url, canonical_url=canon, title=title, summary=summary,
                    summary_origin=origin, published_at=it.published_at, first_seen_at=now, raw_json=_jsonable(it.raw),
                    fingerprint=fingerprint(title), category=cls.category, category_confidence=cls.confidence,
                    classification_json=cls.as_dict())
    s.add(a)
    s.flush()
    for e in ents:
        s.add(NewsEntity(article_id=a.id, symbol=e.symbol, cik=e.cik, method=e.method, relevance=round(e.relevance, 3),
                         evidence=e.evidence[:500]))
    dup = _cluster(s, a, {e.symbol for e in ents}, src.code)
    s.add(NewsEvent(kind="article", external_key=a.id, source_code=src.code, article_id=a.id, category=cls.category,
                    title=title, at=it.published_at, importance=cls.importance, symbols_json=sorted({e.symbol for e in ents}),
                    details_json={"rule": cls.rule, "confidence": cls.confidence}))
    if model is not None:
        _score(s, a, model)
    s.flush()
    return a, dup


def _cluster(s: Session, a: NewsArticle, symbols: set[str], source: str) -> bool:
    since = a.first_seen_at - timedelta(hours=DEDUPE_HOURS)
    exact = None
    if a.canonical_url:
        exact = s.scalar(select(NewsArticle).where(NewsArticle.canonical_url == a.canonical_url, NewsArticle.id != a.id)
                         .order_by(NewsArticle.first_seen_at).limit(1))
    match = exact
    if match is None:
        cands = list(s.scalars(select(NewsArticle).where(NewsArticle.first_seen_at >= since, NewsArticle.id != a.id,
                                                         NewsArticle.cluster_id.is_not(None))
                               .order_by(NewsArticle.first_seen_at.desc()).limit(2000)))
        if cands:
            ent: dict[str, set[str]] = {}
            for aid, sym in s.execute(select(NewsEntity.article_id, NewsEntity.symbol)
                                      .where(NewsEntity.article_id.in_([c.id for c in cands]))):
                ent.setdefault(aid, set()).add(sym)
            best = 0.0
            figs = numbers(a.title)
            for c in cands:
                if a.published_at and c.published_at and abs(a.published_at - c.published_at) > timedelta(hours=48):
                    continue
                if numbers(c.title) != figs:
                    continue
                same_fp = c.fingerprint == a.fingerprint
                theirs = ent.get(c.id, set())
                common = symbols & theirs
                if not common and (symbols or theirs):
                    continue
                if same_fp:
                    sim = 1.0
                elif c.source_code == source:
                    continue
                else:
                    sim = similarity(a.title, c.title)
                    need = SIMILAR_COMPANY if common - {MARKET} else SIMILAR_MARKET
                    if sim < need:
                        continue
                if sim > best:
                    best, match = sim, c
    if match is None or match.cluster_id is None:
        cl = NewsCluster(first_seen_at=a.first_seen_at, first_published_at=a.published_at, last_published_at=a.published_at,
                         article_count=1, source_count=1, symbols_json=sorted(symbols), category=a.category, lead_article_id=a.id)
        s.add(cl)
        s.flush()
        a.cluster_id, a.is_cluster_lead, a.novelty = cl.id, True, 1.0
        return False
    found = s.get(NewsCluster, match.cluster_id)
    assert found is not None
    cl = found
    a.cluster_id, a.is_cluster_lead = cl.id, False
    a.novelty = round(1 / (1 + cl.article_count), 3)
    if exact is not None:
        a.duplicate_of = exact.id
    cl.article_count += 1
    srcs = {x for (x,) in s.execute(select(NewsArticle.source_code).where(NewsArticle.cluster_id == cl.id))} | {source}
    cl.source_count = len(srcs)
    cl.symbols_json = sorted(set(cl.symbols_json or []) | symbols)
    if a.published_at and (cl.last_published_at is None or a.published_at > cl.last_published_at):
        cl.last_published_at = a.published_at
    if a.published_at and (cl.first_published_at is None or a.published_at < cl.first_published_at):
        cl.first_published_at = a.published_at
    return True


def _score(s: Session, a: NewsArticle, model: LoughranMcDonald) -> SentimentScore:
    r = model.score(f"{a.title}. {a.summary or ''}")
    row = SentimentScore(article_id=a.id, model=r.model, model_version=r.model_version, label=r.label, score=r.score,
                         confidence=r.confidence, word_count=r.word_count, positive=r.counts["positive"],
                         negative=r.counts["negative"], uncertainty=r.counts["uncertainty"], litigious=r.counts["litigious"],
                         constraining=r.counts["constraining"],
                         terms_json={**{k: v for k, v in r.terms.items()}, "negated": r.negated, "explanation": r.explanation()})
    s.add(row)
    return row


def rescore(s: Session, model: LoughranMcDonald, limit: int = 5000) -> int:
    """Score the articles that have no score under the current model version (e.g. the dictionary was
    installed after they arrived). Earlier versions' scores are kept."""
    done = select(SentimentScore.article_id).where(SentimentScore.model_version == model.version)
    rows = list(s.scalars(select(NewsArticle).where(NewsArticle.id.not_in(done)).order_by(NewsArticle.first_seen_at.desc()).limit(limit)))
    for a in rows:
        _score(s, a, model)
    s.flush()
    return len(rows)


def store_scheduled(s: Session, src: NewsSource, items) -> int:
    n = 0
    for sc in items:
        row = s.scalar(select(NewsEvent).where(NewsEvent.source_code == src.code, NewsEvent.kind == "scheduled",
                                               NewsEvent.external_key == sc.external_key[:300]))
        if row is None:
            s.add(NewsEvent(kind="scheduled", external_key=sc.external_key[:300], source_code=src.code, category=sc.category,
                            title=sc.title[:300], at=sc.at, importance=sc.importance, symbols_json=[MARKET],
                            details_json=_jsonable(sc.details)))
            n += 1
        elif row.at != sc.at or row.title != sc.title[:300]:
            row.at, row.title, row.details_json = sc.at, sc.title[:300], {**_jsonable(sc.details), "rescheduled": True}
    s.flush()
    return n


# ------------------------------------------------------------------ features
def items_for(s: Session, symbol: str, since: datetime, model_version: str | None = None) -> list[feat.Item]:
    q = (select(NewsEntity, NewsArticle, NewsSource.credibility).join(NewsArticle, NewsEntity.article_id == NewsArticle.id)
         .join(NewsSource, NewsSource.code == NewsArticle.source_code)
         .where(NewsEntity.symbol == symbol, NewsArticle.first_seen_at >= since - timedelta(days=30)))
    rows = list(s.execute(q))
    ids = [a.id for _, a, _ in rows]
    scores: dict[str, SentimentScore] = {}
    if ids:
        sq = select(SentimentScore).where(SentimentScore.article_id.in_(ids))
        if model_version:
            sq = sq.where(SentimentScore.model_version == model_version)
        for row in s.scalars(sq.order_by(SentimentScore.computed_at)):
            scores[row.article_id] = row
    imp = {e.article_id: e.importance for e in s.scalars(select(NewsEvent).where(NewsEvent.article_id.in_(ids)))} if ids else {}
    out = []
    for e, a, cred in rows:
        sc = scores.get(a.id)
        out.append(feat.Item(a.id, a.cluster_id, a.source_code, float(cred), float(e.relevance), a.published_at, a.first_seen_at,
                             a.is_cluster_lead, a.category, sc.score if sc else None, sc.label if sc else None,
                             sc.confidence if sc else 0.0, imp.get(a.id, "medium")))
    return out


def source_health(s: Session) -> tuple[list[str], int]:
    rows = list(s.scalars(select(NewsSource).where(NewsSource.enabled.is_(True))))
    down = [r.name for r in rows if r.status == "error"]
    ok = sum(1 for r in rows if r.last_success_at is not None)
    return down, ok


def refresh_features(s: Session, symbols: set[str], now: datetime, model_available: bool) -> tuple[int, list[dict]]:
    down, ok = source_health(s)
    shifts = []
    n = 0
    for sym in sorted(symbols):
        items = items_for(s, sym, now - FEATURE_LOOKBACK)
        f = feat.compute(items, now, sources_down=down, sources_ok=ok, model_available=model_available)
        row = s.get(SymbolNewsFeature, sym)
        prev = (row.features_json or {}) if row else {}
        a, b = prev.get("news_sentiment_1d"), f.get("news_sentiment_1d")
        if a is not None and b is not None and _label(a) != _label(b) and _label(b) != "neutral" and f.get("scored_1d", 0) >= 2:
            shifts.append({"symbol": sym, "from": a, "to": b})
        if row is None:
            s.add(SymbolNewsFeature(symbol=sym, as_of=now, features_json=f))
        else:
            row.as_of, row.features_json = now, f
        n += 1
    s.flush()
    return n, shifts


# ------------------------------------------------------------------ command-center events
def _label(x: float) -> str:
    return "positive" if x >= 0.25 else "negative" if x <= -0.25 else "neutral"


def announce(s: Session, new_ids: list[str], held: set[str], shifts: list[dict], now: datetime) -> int:
    """NEWS events for new stories about symbols you hold or plan, and for high-importance macro / Fed items;
    SENTIMENT_SHIFT when a held symbol's day sentiment changes label. Informational only."""
    from ..monitoring import settings as msettings
    from ..monitoring.events import emit

    st = msettings.load(s)
    n = 0
    for aid in new_ids:
        a = s.get(NewsArticle, aid)
        if a is None or not a.is_cluster_lead:
            continue
        if a.published_at is not None and now - a.published_at > timedelta(days=2):
            continue                                         # old items found on a first fetch aren't news
        ents = {e.symbol: e for e in s.scalars(select(NewsEntity).where(NewsEntity.article_id == aid))}
        cls = a.classification_json or {}
        sc = s.scalar(select(SentimentScore).where(SentimentScore.article_id == aid).order_by(SentimentScore.computed_at.desc()).limit(1))
        sent = f"sentiment {sc.label}" + (f" ({sc.score:+.2f})" if sc and sc.score is not None else "") if sc else "sentiment not scored"
        src = s.get(NewsSource, a.source_code)
        mine = sorted(sym for sym, e in ents.items() if sym in held and e.relevance >= 0.6)
        if mine:
            sev = "warning" if a.category in RISK else "info"
            for sym in mine[:3]:
                emit(s, "NEWS", sev, f"{sym}: {a.title}"[:300],
                     f"{src.name if src else a.source_code} · {CATEGORIES.get(a.category or 'other', 'Other')} · {sent}. "
                     f"{WHY.get(a.category or 'other', '')} {NOT_A_SIGNAL}", settings=st, symbol=sym,
                     source={"article_id": a.id, "cluster_id": a.cluster_id, "category": a.category, "url": a.url},
                     dedupe_key=f"news:{a.cluster_id}:{sym}", dedupe_seconds=7 * 86400, now=now)
                n += 1
        elif MARKET in ents and cls.get("importance") == "high":
            emit(s, "NEWS", "info", a.title[:300],
                 f"{src.name if src else a.source_code} · {CATEGORIES.get(a.category or 'other', 'Other')}. "
                 f"{WHY.get(a.category or 'other', '')} {NOT_A_SIGNAL}", settings=st,
                 source={"article_id": a.id, "cluster_id": a.cluster_id, "category": a.category, "url": a.url},
                 dedupe_key=f"news:{a.cluster_id}:market", dedupe_seconds=7 * 86400, now=now)
            n += 1
    for sh in shifts:
        if sh["symbol"] not in held:
            continue
        emit(s, "SENTIMENT_SHIFT", "info", f"{sh['symbol']}: news sentiment turned {_label(sh['to'])}",
             f"Day sentiment {sh['from']:+.2f} → {sh['to']:+.2f} (Loughran–McDonald, weighted by source, relevance and age). "
             f"{NOT_A_SIGNAL}", settings=st, symbol=sh["symbol"], source=sh,
             dedupe_key=f"sentiment:{sh['symbol']}:{_label(sh['to'])}", dedupe_seconds=6 * 3600, now=now)
        n += 1
    return n


def _jsonable(d: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in (d or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
        elif isinstance(v, (list, tuple)):
            out[str(k)] = [x if isinstance(x, (str, int, float, bool)) or x is None else str(x) for x in v]
        elif isinstance(v, dict):
            out[str(k)] = _jsonable(v)
        else:
            out[str(k)] = str(v)
    return out
