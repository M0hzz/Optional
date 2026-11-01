"""/api/v1 news routes (Phase 5): the intel feed, symbol news features, the macro calendar and source status.
News is a confirmation and risk-awareness layer: nothing here suggests, places or blocks an order."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, or_, select

from ..news import features as nf
from ..news.classify import CATEGORIES, WHY
from ..news.pipeline import NOT_A_SIGNAL
from ..news.providers import BY_CODE
from ..news.sentiment import LM_LICENSE, LM_PAGE, default_model
from ..news.settings import load as load_news_settings
from ..news.symbols import MARKET
from ..portfolio.models import (Lifecycle, NewsArticle, NewsCluster, NewsEntity, NewsEvent, NewsSource, Position,
                                SentimentScore, SymbolNewsFeature, TradePlan, WatchedContract)
from ..portfolio.schemas import Strict
from ..portfolio.service import DomainError
from .deps import Read, Write

router = APIRouter()


def _held(s) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in s.scalars(select(Position).where(Position.lifecycle == Lifecycle.OPEN)):
        out.setdefault(p.underlying, []).append({"kind": "position", "id": p.id, "name": p.name})
    for pl in s.scalars(select(TradePlan).where(TradePlan.state.in_(["DRAFT", "ARMED", "WAITING", "QUALIFIED"]))):
        out.setdefault(pl.symbol, []).append({"kind": "plan", "id": pl.id, "name": pl.title})
    for c in s.scalars(select(WatchedContract).where(WatchedContract.archived_at.is_(None))):
        out.setdefault(c.underlying, []).append({"kind": "contract", "id": c.id, "name": c.occ_symbol})
    return out


def _why(a: NewsArticle, ents: list[NewsEntity], held: dict[str, list[dict]]) -> dict:
    parts = [WHY.get(a.category or "other", "")]
    yours = []
    for e in ents:
        for h in held.get(e.symbol, []):
            yours.append({**h, "symbol": e.symbol})
    if yours:
        kinds = {"position": "open position", "plan": "plan", "contract": "watched contract"}
        parts.append("It concerns " + "; ".join(f"your {kinds[y['kind']]} {y['name']}" for y in yours[:4])
                     + (" and more" if len(yours) > 4 else "") + ".")
    if any(e.symbol == MARKET for e in ents):
        parts.append("It affects the whole market, including index and ETF options.")
    return {"text": " ".join(p for p in parts if p), "yours": yours, "note": NOT_A_SIGNAL}


def article_out(s, a: NewsArticle, sources: dict[str, NewsSource], held: dict, ents: list[NewsEntity],
                sc: SentimentScore | None, cl: NewsCluster | None, model_note: str | None) -> dict:
    src = sources.get(a.source_code)
    cls = a.classification_json or {}
    return {
        "id": a.id, "title": a.title, "url": a.url,
        "source": {"code": a.source_code, "name": src.name if src else a.source_code, "kind": src.kind if src else None,
                   "credibility": src.credibility if src else None},
        "publisher": (a.raw_json or {}).get("publisher"),
        "published_at": a.published_at, "first_seen_at": a.first_seen_at, "publication_time_known": a.published_at is not None,
        "summary": a.summary, "summary_origin": a.summary_origin,
        "symbols": [{"symbol": e.symbol, "method": e.method, "relevance": e.relevance, "evidence": e.evidence} for e in
                    sorted(ents, key=lambda x: -x.relevance)],
        "category": a.category, "category_label": CATEGORIES.get(a.category or "other", "Other"),
        "category_confidence": a.category_confidence,
        "classification": {k: cls.get(k) for k in ("version", "rule", "matched", "secondary", "importance")},
        "sentiment": None if sc is None else {
            "label": sc.label, "score": sc.score, "confidence": sc.confidence, "model": sc.model, "model_version": sc.model_version,
            "counts": {"positive": sc.positive, "negative": sc.negative, "uncertainty": sc.uncertainty,
                       "litigious": sc.litigious, "constraining": sc.constraining, "words": sc.word_count},
            "terms": {k: v for k, v in (sc.terms_json or {}).items() if k not in ("explanation",)},
            "explanation": (sc.terms_json or {}).get("explanation")},
        "sentiment_unavailable": None if sc is not None else (model_note or "Not scored yet."),
        "novelty": a.novelty,
        "cluster": None if cl is None else {"id": cl.id, "article_count": cl.article_count, "source_count": cl.source_count,
                                            "is_lead": a.is_cluster_lead, "duplicate_of": a.duplicate_of,
                                            "duplicates": max(0, cl.article_count - 1)},
        "why_it_may_matter": _why(a, ents, held),
    }


def _bundle(s, rows: list[NewsArticle]) -> list[dict]:
    if not rows:
        return []
    ids = [a.id for a in rows]
    sources = {r.code: r for r in s.scalars(select(NewsSource))}
    ents: dict[str, list[NewsEntity]] = {}
    for e in s.scalars(select(NewsEntity).where(NewsEntity.article_id.in_(ids))):
        ents.setdefault(e.article_id, []).append(e)
    model, note = default_model(load_news_settings().lexicon_path)
    scores: dict[str, SentimentScore] = {}
    q = select(SentimentScore).where(SentimentScore.article_id.in_(ids))
    for sc in s.scalars(q.order_by(SentimentScore.computed_at)):
        if model is None or sc.model_version == model.version or sc.article_id not in scores:
            scores[sc.article_id] = sc
    clusters = {c.id: c for c in s.scalars(select(NewsCluster).where(NewsCluster.id.in_({a.cluster_id for a in rows if a.cluster_id})))}
    held = _held(s)
    return [article_out(s, a, sources, held, ents.get(a.id, []), scores.get(a.id), clusters.get(a.cluster_id or ""), note)
            for a in rows]


# ------------------------------------------------------------------ the feed
@router.get("/news", tags=["news"])
def list_news(s: Read, symbol: Annotated[list[str] | None, Query()] = None, category: Annotated[list[str] | None, Query()] = None,
              source: Annotated[list[str] | None, Query()] = None, held: bool = False, since: datetime | None = None,
              until: datetime | None = None, q: Annotated[str | None, Query(max_length=100)] = None,
              min_relevance: Annotated[float, Query(ge=0, le=1)] = 0.5, include_duplicates: bool = False,
              limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0):
    """Newest first (by publication time; items without one by first-seen time). One story per cluster
    unless include_duplicates. `held=true`: only symbols you hold, plan or watch, plus market-wide items of
    medium or high importance."""
    stmt = select(NewsArticle)
    syms = {x.upper() for x in symbol or []}
    if held:
        syms |= set(_held(s))
    if syms or held:
        cond: Any = NewsArticle.id.in_(select(NewsEntity.article_id).where(NewsEntity.symbol.in_(syms),
                                                                      NewsEntity.relevance >= min_relevance))
        if held:                  # plus market-wide items that matter (not routine low-importance releases)
            cond = cond | (NewsArticle.id.in_(select(NewsEntity.article_id).where(NewsEntity.symbol == MARKET))
                           & NewsArticle.id.in_(select(NewsEvent.article_id).where(NewsEvent.kind == "article",
                                                                                   NewsEvent.importance != "low")))
        stmt = stmt.where(cond)
    if category:
        stmt = stmt.where(NewsArticle.category.in_(category))
    if source:
        stmt = stmt.where(NewsArticle.source_code.in_(source))
    when = func.coalesce(NewsArticle.published_at, NewsArticle.first_seen_at)
    if since:
        stmt = stmt.where(when >= since)
    if until:
        stmt = stmt.where(when <= until)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(NewsArticle.title.ilike(like), NewsArticle.summary.ilike(like)))
    if not include_duplicates:
        stmt = stmt.where(NewsArticle.is_cluster_lead.is_(True))
    total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(s.scalars(stmt.order_by(when.desc(), NewsArticle.id.desc()).offset(offset).limit(limit)))
    return {"items": _bundle(s, rows), "total": total, "limit": limit, "offset": offset, "note": NOT_A_SIGNAL}


@router.get("/news/articles/{article_id}", tags=["news"])
def get_article(article_id: str, s: Read):
    a = s.get(NewsArticle, article_id)
    if a is None:
        raise DomainError("NOT_FOUND", f"News item {article_id} does not exist.", 404)
    out = _bundle(s, [a])[0]
    members = list(s.scalars(select(NewsArticle).where(NewsArticle.cluster_id == a.cluster_id, NewsArticle.id != a.id)
                             .order_by(NewsArticle.first_seen_at))) if a.cluster_id else []
    out["same_story"] = [{"id": m.id, "title": m.title, "source_code": m.source_code, "url": m.url, "published_at": m.published_at,
                          "first_seen_at": m.first_seen_at, "duplicate_of": m.duplicate_of} for m in members]
    out["raw"] = a.raw_json
    return out


# ------------------------------------------------------------------ symbol features
@router.get("/news/features/{symbol}", tags=["news"])
def symbol_features(symbol: str, s: Read):
    sym = symbol.upper()
    row = s.get(SymbolNewsFeature, sym)
    market = s.get(SymbolNewsFeature, MARKET)
    names, why = nf.rule_values(row.features_json if row else None, market.features_json if market else None)
    return {"symbol": sym, "as_of": row.as_of if row else None, "features": row.features_json if row else None,
            "rule_values": {k: (None if v is None else ("n/a" if isinstance(v, float) and v != v else v)) for k, v in names.items()},
            "why_missing": why, "meanings": nf.RULE_NAMES, "version": nf.VERSION, "note": NOT_A_SIGNAL}


@router.get("/news/features", tags=["news"])
def many_features(s: Read, symbols: Annotated[str, Query(max_length=500)]):
    syms = [x.strip().upper() for x in symbols.split(",") if x.strip()][:50]
    rows = {r.symbol: r for r in s.scalars(select(SymbolNewsFeature).where(SymbolNewsFeature.symbol.in_(syms)))}
    return {sym: {"as_of": rows[sym].as_of, "features": rows[sym].features_json} if sym in rows else None for sym in syms}


# ------------------------------------------------------------------ calendar
@router.get("/news/calendar", tags=["news"])
def calendar(s: Read, days: Annotated[int, Query(ge=1, le=90)] = 14, importance: Annotated[list[str] | None, Query()] = None):
    """Scheduled official releases (FOMC statements, BLS and BEA releases) from now on."""
    now = datetime.now(timezone.utc)
    stmt = select(NewsEvent).where(NewsEvent.kind == "scheduled", NewsEvent.at >= now - timedelta(hours=2),
                                   NewsEvent.at <= now + timedelta(days=days))
    if importance:
        stmt = stmt.where(NewsEvent.importance.in_(importance))
    rows = s.scalars(stmt.order_by(NewsEvent.at))
    return [{"id": e.id, "title": e.title, "at": e.at, "source_code": e.source_code, "category": e.category,
             "category_label": CATEGORIES.get(e.category, e.category), "importance": e.importance, "details": e.details_json}
            for e in rows]


# ------------------------------------------------------------------ sources and status
def _lexicon() -> dict:
    st = load_news_settings()
    model, note = default_model(st.lexicon_path)
    return {"installed": model is not None, "model_version": model.version if model else None, "note": note,
            "license": LM_LICENSE, "page": LM_PAGE,
            "install": "python scripts/news.py install-lexicon --accept-license"}


@router.get("/news/sources", tags=["news"])
def sources(request: Request, s: Read):
    st = load_news_settings()
    rows = {r.code: r for r in s.scalars(select(NewsSource))}
    counts = dict(s.execute(select(NewsArticle.source_code, func.count()).group_by(NewsArticle.source_code)).all())
    out = []
    for code, p in BY_CODE.items():
        r = rows.get(code)
        ok, why = p.available(st)
        out.append({"code": code, "name": p.name, "kind": p.kind, "enabled": r.enabled if r else code not in st.disabled_sources,
                    "configured": ok, "configuration_note": why, "credibility": r.credibility if r else st.credibility.get(code),
                    "status": r.status if r else "never", "status_detail": r.status_detail if r else None,
                    "last_fetch_at": r.last_fetch_at if r else None, "last_success_at": r.last_success_at if r else None,
                    "items_last": r.items_last if r else 0, "new_last": r.new_last if r else 0, "articles": counts.get(code, 0)})
    runner = getattr(request.app.state, "news_runner", None)
    return {"sources": out, "lexicon": _lexicon(),
            "runner": runner.status() if runner else {"state": "off", "detail": "This API was started with OPTLAB_NEWS=off."},
            "interval_minutes": st.interval_minutes, "identity_configured": st.identity_ok, "note": NOT_A_SIGNAL}


class SourceUpdate(Strict):
    enabled: bool


@router.patch("/news/sources/{code}", tags=["news"])
def update_source(code: str, body: SourceUpdate, s: Write):
    if code not in BY_CODE:
        raise DomainError("NOT_FOUND", f"No news source {code}.", 404)
    from ..news.pipeline import ensure_sources

    rows = ensure_sources(s, load_news_settings())
    r = rows[code]
    r.enabled = body.enabled
    if not body.enabled:
        r.status, r.status_detail = "disabled", "Switched off."
    elif r.status == "disabled":
        r.status, r.status_detail = "never", None
    s.flush()
    return {"code": code, "enabled": r.enabled, "status": r.status}


@router.post("/news/refresh", tags=["news"])
def refresh(request: Request):
    """Fetch all enabled sources now (in this process if it runs the news ingest, or if nobody does)."""
    from ..news.runner import NewsLock

    runner = getattr(request.app.state, "news_runner", None)
    if runner is not None and runner.lock.held:
        return runner.run_now().as_dict()
    lk = NewsLock(request.app.state.db.url)
    if not lk.try_acquire():
        raise DomainError("NEWS_ELSEWHERE", f"Another process fetches news for this database (pid {lk.holder() or '?'}).", 409)
    try:
        from ..news.pipeline import ingest

        st = load_news_settings()
        return ingest(request.app.state.db, st, get=getattr(request.app.state, "news_get", None)).as_dict()
    finally:
        lk.release()
