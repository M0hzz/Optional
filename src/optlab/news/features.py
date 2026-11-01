"""Symbol-level news features (features-v1), computed by a pure function of the stored articles and a time.

No look-ahead: at time T only items whose publication time AND first-seen time are at or before T count, so
a replay never uses news before optlab could have known it. Items without a publication time are counted
as `undated` and stay out of the time windows.

One story counts once: articles in the same cluster (duplicates) count as one story, represented by its
most heavily weighted article. Items with relevance below 0.5 to the symbol are ignored.

    weight            source credibility × relevance × max(sentiment confidence, 0.1) × time decay
    time decay        0.5 ^ (age ÷ half-life), half-life = half the window (7.5 min, 30 min, 12 h)
    news_sentiment_W  weighted mean sentiment score of the scored stories in the window, −1..+1
                      (None when no story in the window has a score)
    news_volume_W     stories in the window
    news_velocity     stories in the last hour ÷ the average stories per hour over the 7 days before it
                      (None without a baseline)
    news_novelty      share of the last day's articles that are new stories rather than repeats
    news_confidence   mean sentiment confidence of the last day's scored stories
    minutes_since_news   since the latest dated story (None if none in 30 days)
    bullish/bearish_article_count   last day's stories labelled positive / negative
    source_count      distinct sources in the last day
    event_type        the category of the most recent story in the last day
    risk_event_1d     a story in the last day in a category that can move the price or volatility
    conflicting       positive and negative stories in the same day
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .classify import RISK

VERSION = "features-v1"
WINDOWS = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "1d": timedelta(days=1)}
MIN_RELEVANCE = 0.5


@dataclass
class Item:
    article_id: str
    cluster_id: str | None
    source_code: str
    credibility: float
    relevance: float
    published_at: datetime | None
    first_seen_at: datetime
    is_lead: bool
    category: str | None
    score: float | None = None
    label: str | None = None
    confidence: float = 0.0
    importance: str = "medium"


def _weight(it: Item, age: timedelta, window: timedelta) -> float:
    decay = 0.5 ** (age / (window / 2))
    return it.credibility * it.relevance * max(it.confidence, 0.1) * decay


def compute(items: list[Item], as_of: datetime, *, sources_down: list[str] | None = None,
            sources_ok: int = 1, model_available: bool = True) -> dict:
    known = [i for i in items if i.first_seen_at <= as_of and i.relevance >= MIN_RELEVANCE]
    undated = [i for i in known if i.published_at is None]
    dated = [i for i in known if i.published_at is not None and i.published_at <= as_of]
    out: dict = {"version": VERSION, "as_of": as_of.isoformat(), "undated_count": len(undated)}

    def stories(window: timedelta) -> dict[str, Item]:
        by: dict[str, tuple[float, Item]] = {}
        for i in dated:
            age = as_of - i.published_at                                # type: ignore[operator]
            if age > window:
                continue
            key = i.cluster_id or i.article_id
            w = _weight(i, age, window)
            if key not in by or w > by[key][0]:
                by[key] = (w, i)
        return {k: v[1] for k, v in by.items()}

    for name, win in WINDOWS.items():
        st = stories(win)
        scored = [(float(i.score), _weight(i, as_of - i.published_at, win)) for i in st.values()  # type: ignore[operator]
                  if i.score is not None]
        tot = sum(w for _, w in scored)
        out[f"news_sentiment_{name}"] = round(sum(sc * w for sc, w in scored) / tot, 4) if tot > 0 else None
        out[f"news_volume_{name}"] = len(st)
        out[f"scored_{name}"] = len(scored)
    day = stories(WINDOWS["1d"])
    hour = out["news_volume_1h"]
    base_start, base_end = as_of - timedelta(days=7, hours=1), as_of - timedelta(hours=1)
    base: set[str] = set()
    for i in dated:
        if i.published_at is not None and base_start < i.published_at <= base_end:
            base.add(i.cluster_id or i.article_id)
    out["news_velocity"] = round(hour / (len(base) / (7 * 24)), 3) if base else None
    day_articles = [i for i in dated if as_of - i.published_at <= WINDOWS["1d"]]                  # type: ignore[operator]
    out["news_novelty"] = round(sum(1 for i in day_articles if i.is_lead) / len(day_articles), 3) if day_articles else None
    confs = [i.confidence for i in day.values() if i.score is not None]
    out["news_confidence"] = round(sum(confs) / len(confs), 3) if confs else None
    recent = [i for i in dated if as_of - i.published_at <= timedelta(days=30)]                    # type: ignore[operator]
    latest = max(recent, key=lambda i: i.published_at, default=None)                                # type: ignore[arg-type,return-value]
    out["minutes_since_news"] = None if latest is None else round((as_of - latest.published_at).total_seconds() / 60, 1)  # type: ignore[operator]
    out["bullish_article_count"] = sum(1 for i in day.values() if i.label == "positive")
    out["bearish_article_count"] = sum(1 for i in day.values() if i.label == "negative")
    out["source_count"] = len({i.source_code for i in day.values()})
    last_day = max(day.values(), key=lambda i: i.published_at, default=None)                       # type: ignore[arg-type,return-value]
    out["event_type"] = last_day.category if last_day else None
    out["risk_event_1d"] = any(i.category in RISK for i in day.values())
    out["high_importance_1d"] = any(i.importance == "high" for i in day.values())
    out["conflicting"] = out["bullish_article_count"] > 0 and out["bearish_article_count"] > 0
    out["unscored_1d"] = sum(1 for i in day.values() if i.score is None)
    down = sorted(sources_down or [])
    out["sources_down"] = down
    out["status"] = "unavailable" if sources_ok == 0 else ("incomplete" if down else "ok")
    out["model_available"] = model_available
    return out


# ------------------------------------------------------------------ values for monitoring rules
RULE_NAMES = {
    "news_sentiment_15m": "news sentiment, last 15 minutes (−1..+1; not applicable without scored news)",
    "news_sentiment_1h": "news sentiment, last hour (−1..+1)",
    "news_sentiment_1d": "news sentiment, last day (−1..+1)",
    "news_volume_15m": "news stories in the last 15 minutes",
    "news_volume_1h": "news stories in the last hour",
    "news_velocity": "stories in the last hour ÷ the hourly average of the previous 7 days",
    "news_novelty": "share of the last day's articles that are new stories (0..1)",
    "news_confidence": "mean sentiment confidence of the last day's stories (0..1)",
    "minutes_since_news": "minutes since the latest story",
    "bullish_article_count": "stories with positive sentiment in the last day",
    "bearish_article_count": "stories with negative sentiment in the last day",
    "news_source_count": "distinct news sources in the last day",
    "news_risk_event_1d": "true if a story in the last day is earnings, guidance, an offering, a deal, a lawsuit, "
                          "a regulatory action, a management change or a macro / Fed event",
    "market_news_risk_1d": "true if a high-importance Federal Reserve or macro release (FOMC statement, CPI, jobs, GDP, "
                           "PCE...) came out in the last day",
}
NUMERIC_NA = ("news_sentiment_15m", "news_sentiment_1h", "news_sentiment_1d", "news_velocity", "news_novelty",
              "news_confidence", "minutes_since_news")


def rule_values(f: dict | None, market: dict | None) -> tuple[dict, dict]:
    """(names, why missing) for the monitoring rule context.

    None (unknown → the rule can't be checked) when the news data itself is unavailable; NaN (doesn't apply →
    comparisons are false) when news is available but there's nothing to measure, e.g. no story today."""
    names: dict = {}
    why: dict = {}
    if not f or f.get("status") == "unavailable":
        reason = "news is unavailable (no source has been fetched successfully)" if not f else \
            "news is unavailable: " + ", ".join(f.get("sources_down") or ["every source failed"])
        for k in RULE_NAMES:
            names[k], why[k] = None, reason
        if market and market.get("status") != "unavailable":
            names["market_news_risk_1d"] = bool(market.get("high_importance_1d"))
            why.pop("market_news_risk_1d", None)
        return names, why
    for k in NUMERIC_NA:
        v = f.get(k)
        if v is None and k.startswith("news_sentiment") and not f.get("model_available") and f.get(f"news_volume_{k.rsplit('_', 1)[1]}"):
            names[k], why[k] = None, "news sentiment is unavailable (the Loughran–McDonald dictionary isn't installed)"
        else:
            names[k] = math.nan if v is None else float(v)
    names["news_volume_15m"] = int(f.get("news_volume_15m") or 0)
    names["news_volume_1h"] = int(f.get("news_volume_1h") or 0)
    names["bullish_article_count"] = int(f.get("bullish_article_count") or 0)
    names["bearish_article_count"] = int(f.get("bearish_article_count") or 0)
    names["news_source_count"] = int(f.get("source_count") or 0)
    names["news_risk_event_1d"] = bool(f.get("risk_event_1d"))
    if market and market.get("status") != "unavailable":
        names["market_news_risk_1d"] = bool(market.get("high_importance_1d"))
    else:
        names["market_news_risk_1d"], why["market_news_risk_1d"] = None, "macro and Fed news is unavailable"
    return names, why


def load_rule_values(s, symbols) -> dict[str, tuple[dict, dict]]:
    """Rule-context news values for each symbol, from the stored features (no network, no recomputation)."""
    from sqlalchemy import select

    from ..portfolio.models import SymbolNewsFeature
    from .symbols import MARKET

    want = {x.upper() for x in symbols if x} | {MARKET}
    rows = {r.symbol: r.features_json for r in s.scalars(select(SymbolNewsFeature).where(SymbolNewsFeature.symbol.in_(want)))}
    market = rows.get(MARKET)
    out = {}
    for sym in want - {MARKET}:
        f = rows.get(sym)
        if f is None and market is not None and market.get("status") != "unavailable":
            names, why = rule_values({"status": "unavailable", "sources_down": [f"no news features for {sym} yet "
                                                                                 "(the next news run computes them)"]}, market)
        else:
            names, why = rule_values(f, market)
        out[sym] = (names, why)
    return out
