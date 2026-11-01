"""Phase 5: news and sentiment. Parsing, normalization, symbol mapping, de-duplication, classification,
Loughran–McDonald sentiment (with a small fixture dictionary, not the real one), symbol features without
look-ahead, provider isolation, idempotent ingest, command-center events, news-confirmed rules, the API, and
the rule that news never produces a BUY or SELL instruction. No network: every fetch reads a fixture."""
from __future__ import annotations

import math
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import opt
from sqlalchemy import select

from optlab.news import classify, feeds, normalize, pipeline, providers, sentiment
from optlab.news import features as nf
from optlab.news.http import FetchError
from optlab.news.settings import NewsSettings
from optlab.news.symbols import MARKET, SymbolMap, map_entities
from optlab.portfolio.db import Database
from optlab.portfolio.models import Event, NewsArticle, NewsEntity, NewsEvent, NewsSource, SentimentScore, SymbolNewsFeature

FIX = Path(__file__).parent / "fixtures" / "news"
NOW = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
API = "/api/v1"


def lex_model():
    return sentiment.LoughranMcDonald(sentiment.Lexicon.from_csv((FIX / "lexicon.csv").read_bytes(), "test-lexicon.csv"))


class Getter:
    """Serves fixture files by URL; records every call (URL and headers)."""

    def __init__(self, extra: dict[str, bytes | Exception] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.map: dict[str, bytes | Exception] = {
            "getcurrent": (FIX / "sec_current.xml").read_bytes(), "CIK0000000101": (FIX / "sec_submissions_xyz.json").read_bytes(),
            "company_tickers": (FIX / "tickers.json").read_bytes(), "press_all": (FIX / "fed.xml").read_bytes(),
            "fomccalendars": (FIX / "fomc.htm").read_bytes(), "bls.gov/feed/cpi": (FIX / "bls_cpi.rss").read_bytes(),
            "bls.ics": (FIX / "bls.ics").read_bytes(), "apps.bea.gov": (FIX / "bea.xml").read_bytes(),
            "ir.example.com/feed": (FIX / "ir_xyz.xml").read_bytes(), **(extra or {})}

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        for k, v in self.map.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v
        if "data.sec.gov/submissions" in url:
            return b'{"name": "x", "filings": {"recent": {"form": [], "filingDate": []}}}'
        raise FetchError(f"HTTP 404 from {url.split('?')[0]}")


@pytest.fixture
def st(tmp_path):
    cache = tmp_path / "newscache"
    cache.mkdir()
    shutil.copy(FIX / "tickers.json", cache / "sec_company_tickers.json")
    return NewsSettings(identity="optlab test admin@example.org", bls_feeds=["cpi"], cache_dir=cache,
                        ir_feeds={"XYZ": ["https://ir.example.com/feed.xml"]}, extra_symbols=["ACME", "XYZ"])


@pytest.fixture
def db(db_url, client):              # the client fixture has migrated the database
    d = Database(db_url)
    yield d
    d.dispose()


def run(db, st, get=None, now=NOW, model="lex", **kw):
    m = lex_model() if model == "lex" else model
    return pipeline.ingest(db, st, get=get or Getter(), now=now, model=m, model_note=None if m else "not installed", **kw)


def position(client, account, underlying="XYZ"):
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": underlying,
                                              "legs": [opt("SELL", "PUT", 95, "1.10"), opt("BUY", "PUT", 90, "0.15")]})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ parsing and normalization
def test_feed_times_are_parsed_or_left_unknown():
    assert feeds.parse_time("Wed, 28 Oct 2026 18:00:00 GMT") == datetime(2026, 10, 28, 18, tzinfo=timezone.utc)
    assert feeds.parse_time("2026-10-14T08:30:00-04:00") == datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)
    assert feeds.parse_time("2026-10-14T07:50:31.2-04:00") is not None
    assert feeds.parse_time(None) is None and feeds.parse_time("soon") is None
    assert feeds.parse_time("2026-10-14T08:30:00") is None             # no zone: unknown, never guessed
    items = feeds.parse((FIX / "fed.xml").read_bytes())
    assert items[0].title == "Federal Reserve issues FOMC statement" and items[0].categories == ["Monetary Policy"]
    cal = feeds.parse_ics((FIX / "bls.ics").read_bytes())
    cpi = next(e for e in cal if e.summary == "Consumer Price Index")
    assert cpi.start == datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc)      # 8:30 ET in November (EST)


def test_feed_parser_refuses_entity_expansion():
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>'
    with pytest.raises(Exception):
        feeds.parse(bomb)


def test_text_and_urls_are_cleaned():
    assert normalize.clean_text("<p>Strong <b>gains</b>.</p><script>alert(1)</script>") == "Strong gains ."
    assert normalize.safe_url("javascript:alert(1)") is None and normalize.safe_url("ftp://x/y") is None
    assert normalize.canonical_url("HTTPS://IR.Example.com/news/q3/?utm_source=rss&id=7#top") == "https://ir.example.com/news/q3?id=7"
    assert normalize.opening_sentences("First sentence here. Second one. Third one.") == "First sentence here. Second one."
    assert normalize.numbers("CPI rises 0.6% in September 2026") == frozenset({"0.6", "2026"})


# ------------------------------------------------------------------ sentiment
def test_loughran_mcdonald_scoring_is_explainable_and_deterministic():
    m = lex_model()
    r = m.score("Example Widgets reports strong gains but a lawsuit may follow")
    assert r.counts["positive"] == 2 and r.counts["negative"] == 1 and r.counts["litigious"] == 1 and r.counts["uncertainty"] == 1
    assert r.score == round((2 - 1) / 3, 4) and r.label == "positive"
    assert r.terms["positive"] == ["STRONG", "GAINS"] and "lawsuit" in r.explanation().lower()
    assert m.score("Example Widgets reports strong gains but a lawsuit may follow") == r      # same input, same result
    neg = m.score("The company did not report any gain this quarter")
    assert neg.negated == ["GAIN"] and neg.counts["negative"] == 1 and neg.label == "negative"
    assert m.score("Able to deliver").counts["positive"] == 0                        # removed in 2020: not a positive word
    assert m.score("Gains").label == "unknown" and m.score("Gains").score is None    # too little text
    neutral = m.score("The board met on Tuesday to discuss the agenda")
    assert neutral.label == "neutral" and neutral.score is None and neutral.confidence == 0
    assert m.version.startswith("test-lexicon@") and m.version.endswith("/lm-rules/v1")


def test_missing_dictionary_is_reported_not_guessed(tmp_path):
    model, note = sentiment.default_model(tmp_path / "missing.csv")
    assert model is None and "isn't installed" in note and "install-lexicon" in note
    bad = tmp_path / "bad.csv"
    bad.write_text("Word,Foo\nGAIN,1\n")
    model, note = sentiment.default_model(bad)
    assert model is None and "can't be read" in note


# ------------------------------------------------------------------ classification and mapping
def test_classification_names_its_rule():
    assert classify.classify_filing("8-K", ["2.02", "9.01"]).category == "earnings"
    assert classify.classify_filing("S-3", []).category == "offering_dilution"
    assert classify.classify_filing("8-K", ["5.02"]).category == "management_change"
    c = classify.classify_text("Example Widgets prices public offering of common stock", "dilution expected")
    assert c.category == "offering_dilution" and "public offering" in c.matched["offering_dilution"] and c.confidence >= 0.75
    assert classify.classify_text("Board meets on Tuesday", None).category == "other"
    assert classify.classify_fed(["Monetary Policy"], "Federal Reserve issues FOMC statement").importance == "high"
    assert set(classify.WHY) == set(classify.CATEGORIES)


def test_symbol_mapping_methods_and_relevance():
    smap = SymbolMap.from_json((FIX / "tickers.json").read_bytes())
    e = {x.symbol: x for x in map_entities("Example Widgets (NASDAQ: XYZ) beats", "Also mentions $ACME and $FAKE", smap,
                                           {"XYZ", "GLOBX"})}
    assert e["XYZ"].method == "exchange" and e["XYZ"].relevance == 0.9 and "name" not in e["XYZ"].method
    assert "headline" in e["XYZ"].evidence                                    # the name match is kept as evidence too
    assert e["ACME"].method == "cashtag" and "FAKE" not in e                  # unknown tickers are ignored
    assert {x.symbol for x in map_entities("Filing", None, smap, set(), filer_cik="0000000101")} == {"XYZ"}
    tagged = map_entities("Unrelated headline", None, smap, set(), provider_symbols=["ACME"])[0]
    assert tagged.method == "provider" and tagged.relevance == 0.6
    assert map_entities("CPI rises", None, smap, set(), market=True)[0].symbol == MARKET
    assert map_entities("globex holdings expands", None, smap, {"GLOBX"}) == []     # lower-case common words aren't names
    assert map_entities("Globex expands overseas", None, smap, {"GLOBX"})[0].relevance == 0.7


# ------------------------------------------------------------------ ingest
def test_ingest_end_to_end_is_idempotent_and_keeps_publication_time(db, st):
    g = Getter()
    r = run(db, st, g)
    by = {x.code: x for x in r.sources}
    assert by["sec_edgar"].status == "ok" and by["sec_edgar"].new == 2           # the 8-K and the S-3 of XYZ (Form 4 skipped)
    assert by["federal_reserve"].new == 2 and by["bls"].new == 2 and by["bea"].new == 1 and by["company_ir"].new == 2
    assert by["finnhub"].status == "unavailable" and "FINNHUB_API_KEY" in by["finnhub"].detail
    assert by["federal_reserve"].scheduled == 3 and by["bls"].scheduled == 2 and by["bea"].scheduled == 1
    again = run(db, st, Getter(), now=NOW + timedelta(minutes=15))
    assert again.new_articles == 0
    with db.session() as s:
        arts = list(s.scalars(select(NewsArticle)))
        assert len(arts) == r.new_articles
        undated = next(a for a in arts if a.external_id == "ir-2")
        assert undated.published_at is None and undated.first_seen_at == NOW           # never the collection time
        assert "alert" not in (undated.summary or "") and undated.summary_origin == "provider"
        sec = next(a for a in arts if a.external_id == "0000101-26-000001")
        assert sec.category == "earnings" and sec.published_at == datetime(2026, 10, 30, 21, 29, 14, tzinfo=timezone.utc)
        assert {e.symbol for e in s.scalars(select(NewsEntity).where(NewsEntity.article_id == sec.id))} == {"XYZ"}
        assert not any(a.external_id.startswith("0000999") for a in arts)               # filers you don't follow are skipped
        assert all(a.raw_json is not None for a in arts)
        assert s.scalar(select(SentimentScore).where(SentimentScore.article_id == sec.id)) is not None
        bls = next(a for a in arts if a.external_id == "cpi-2026_10_14")
        assert bls.summary_origin == "opening_sentences" and bls.summary == (
            "In September, the Consumer Price Index rose 0.6 percent, seasonally adjusted. "
            "The index for all items less food and energy rose 0.3 percent.")          # the first two sentences, verbatim
    assert all("admin@example.org" in h["User-Agent"] for _, h in g.calls)


def test_one_failing_source_doesnt_stop_the_others(db, st):
    g = Getter({"press_all": FetchError("HTTP 503 from https://www.federalreserve.gov/feeds/press_all.xml")})
    r = run(db, st, g)
    by = {x.code: x for x in r.sources}
    assert by["federal_reserve"].status == "error" and "503" in by["federal_reserve"].detail
    assert by["bls"].status == "ok" and by["sec_edgar"].status == "ok"
    with db.session() as s:
        fed = s.get(NewsSource, "federal_reserve")
        assert fed.status == "error" and fed.last_success_at is None
        f = s.get(SymbolNewsFeature, MARKET).features_json
        assert f["status"] == "incomplete" and "Federal Reserve" in f["sources_down"]


def test_sec_requires_an_identity_and_finnhub_a_key_in_a_header(db, st):
    st.identity = "Your Name your.email@example.com"
    st.finnhub_key = "secret-key-123"
    g = Getter({"finnhub.io": b'[{"id": 7, "headline": "Acme Corp unveils new widget", "summary": "Acme unveils.", '
                              b'"url": "https://news.example.com/a", "datetime": 1761998400, "related": "ACME", "source": "Wire"}]'})
    r = run(db, st, g)
    by = {x.code: x for x in r.sources}
    assert by["sec_edgar"].status == "unavailable" and "edgar.identity" in by["sec_edgar"].detail
    assert not any("sec.gov" in u for u, _ in g.calls)
    fh = [(u, h) for u, h in g.calls if "finnhub.io" in u]
    assert fh and all("secret-key-123" not in u for u, _ in fh) and all(h["X-Finnhub-Token"] == "secret-key-123" for _, h in fh)
    assert by["finnhub"].status == "ok" and by["finnhub"].new == 1
    bad = run(db, st, Getter({"finnhub.io": FetchError("HTTP 401 from https://finnhub.io/api/v1/company-news")}),
              now=NOW + timedelta(minutes=20))
    fb = next(x for x in bad.sources if x.code == "finnhub")
    assert fb.status == "error" and "secret-key-123" not in (fb.detail or "")


def test_disabled_sources_are_not_fetched(db, st, client):
    assert client.patch(f"{API}/news/sources/bea", json={"enabled": False}).status_code == 200
    g = Getter()
    r = run(db, st, g)
    assert next(x for x in r.sources if x.code == "bea").status == "disabled"
    assert not any("bea.gov" in u for u, _ in g.calls)


# ------------------------------------------------------------------ de-duplication
def test_duplicates_are_clustered_but_different_releases_are_not(db, st):
    wire = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>w</title>'
            b'<item><title>Example Widgets prices a public offering of common stock</title>'
            b'<link>https://ir.example.com/news/offering</link><guid>w-1</guid><pubDate>Fri, 30 Oct 2026 21:05:00 GMT</pubDate></item>'
            b'<item><title>Example Widgets prices public offering of its common stock</title><link>https://wire.example.com/b</link>'
            b'<guid>w-2</guid><description>(NASDAQ: XYZ)</description><pubDate>Fri, 30 Oct 2026 21:10:00 GMT</pubDate></item>'
            b'</channel></rss>')
    st.ir_feeds = {"XYZ": ["https://ir.example.com/feed.xml"], "ACME": ["https://wire.example.com/feed.xml"]}
    run(db, st, Getter({"wire.example.com/feed": wire}))
    with db.session() as s:
        arts = {a.external_id: a for a in s.scalars(select(NewsArticle))}
        a, b, near = arts["ir-1"], arts["w-1"], arts["w-2"]
        lead, same_url = (a, b) if a.is_cluster_lead else (b, a)                # whichever was stored first leads
        assert lead.is_cluster_lead and not same_url.is_cluster_lead
        assert same_url.duplicate_of == lead.id and same_url.cluster_id == lead.cluster_id     # same canonical URL
        assert near.cluster_id == lead.cluster_id and not near.is_cluster_lead and near.novelty < 1 and near.duplicate_of is None
        cpi_sep, cpi_aug = arts["cpi-2026_10_14"], arts["cpi-2026_09_11"]
        assert cpi_sep.cluster_id != cpi_aug.cluster_id                     # different months: different stories


def test_api_shows_one_story_per_cluster_unless_asked(db, st, client):
    run(db, st)
    run(db, st, Getter({"ir.example.com/feed": (FIX / "ir_xyz.xml").read_bytes().replace(b"ir-1", b"ir-1-repost")}),
        now=NOW + timedelta(minutes=5))
    lead_only = client.get(f"{API}/news", params={"symbol": "XYZ"}).json()
    every = client.get(f"{API}/news", params={"symbol": "XYZ", "include_duplicates": True}).json()
    assert every["total"] == lead_only["total"] + 1
    item = next(a for a in lead_only["items"] if "offering" in a["title"])
    assert item["cluster"]["duplicates"] == 1
    detail = client.get(f"{API}/news/articles/{item['id']}").json()
    assert len(detail["same_story"]) == 1 and detail["raw"]["symbol"] == "XYZ"


# ------------------------------------------------------------------ features
def item(i, minutes_ago, *, score=0.5, label="positive", cluster=None, seen_ago=None, rel=1.0, cred=1.0, cat="other",
         published=True, conf=1.0):
    pub = NOW - timedelta(minutes=minutes_ago)
    return nf.Item(f"a{i}", cluster or f"c{i}", "src", cred, rel, pub if published else None,
                   NOW - timedelta(minutes=seen_ago if seen_ago is not None else minutes_ago), True, cat, score, label, conf)


def test_features_windows_weights_and_no_look_ahead():
    items = [item(1, 5, score=1.0), item(2, 50, score=-1.0, label="negative"), item(3, 600, score=0.5),
             item(4, 3, score=-1.0, label="negative", seen_ago=-10),                  # seen after "now": not known yet
             item(5, 2, score=0.9, cluster="c1"),                                     # a duplicate of story 1
             item(6, 1, score=-1.0, label="negative", rel=0.3),                       # not relevant enough
             item(7, 1, published=False)]                                             # no publication time
    f = nf.compute(items, NOW)
    assert f["news_volume_15m"] == 1 and f["news_volume_1h"] == 2 and f["news_volume_1d"] == 3
    assert f["news_sentiment_15m"] == 0.9            # story 1 counted once, by its most heavily weighted (newest) article
    assert -1 < f["news_sentiment_1h"] < 1 and f["news_sentiment_1h"] > 0   # the newer positive story weighs more
    assert f["undated_count"] == 1 and f["minutes_since_news"] == 2.0
    assert f["bullish_article_count"] == 2 and f["bearish_article_count"] == 1 and f["conflicting"]
    assert f["news_velocity"] == 336.0                # 2 stories this hour ÷ (1 story in the previous 7 days ÷ 168 hours)
    assert nf.compute([item(1, 5)], NOW)["news_velocity"] is None          # no baseline: undefined, not zero
    assert nf.compute(items, NOW - timedelta(hours=2))["news_volume_1d"] == 1   # as of two hours ago: only story 3


def test_rule_values_distinguish_unknown_from_not_applicable():
    names, why = nf.rule_values(None, None)
    assert names["news_sentiment_1d"] is None and "unavailable" in why["news_sentiment_1d"]
    quiet = nf.compute([], NOW)
    names, why = nf.rule_values(quiet, quiet)
    assert math.isnan(names["news_sentiment_1d"]) and names["news_volume_1h"] == 0 and names["news_risk_event_1d"] is False
    unscored = nf.compute([item(1, 5, score=None, label=None)], NOW, model_available=False)
    names, why = nf.rule_values(unscored, quiet)
    assert names["news_sentiment_1h"] is None and "dictionary" in why["news_sentiment_1h"]


# ------------------------------------------------------------------ news-confirmed rules
def test_news_can_confirm_a_rule_but_never_be_the_whole_rule():
    from optlab.monitoring.rules import NEWS_ONLY, Context, validate

    assert validate("news_sentiment_1d > 0.2", "symbol") == NEWS_ONLY
    assert validate("news_risk_event_1d and news_volume_1h > 2", "position") == NEWS_ONLY
    assert validate("price > 100 and news_sentiment_1d > 0.2", "symbol") is None
    f = nf.compute([item(1, 30, score=0.8)], NOW)
    ctx = Context("symbol", {"price": 101.0}, {}, news=nf.rule_values(f, f))
    assert ctx.evaluate("price > 100 and news_sentiment_1d > 0.2", "alert").result is True
    assert ctx.evaluate("price > 100 and news_sentiment_15m > 0.2", "alert").result is False      # NaN: doesn't apply
    assert ctx.evaluate("news_sentiment_1d > 0.2", "alert").error == NEWS_ONLY
    unknown = Context("symbol", {"price": 101.0}, {}, news=nf.rule_values(None, None))
    r = unknown.evaluate("price > 100 and news_sentiment_1d > 0.2", "alert")
    assert r.result is None and "news is unavailable" in r.reason


def test_monitor_vocabulary_lists_news_names(client):
    v = client.get(f"{API}/monitor/vocabulary").json()
    assert {x["name"] for x in v["news"]} == set(nf.RULE_NAMES) and "never a trade signal" in v["news_rule"]
    r = client.post(f"{API}/monitor/validate", json={"condition": "news_sentiment_1d < -0.5", "target": "position"}).json()
    assert not r["valid"] and "can't be the whole condition" in r["error"]


def test_engine_rule_context_reads_stored_features(db, st, client, account):
    position(client, account)
    run(db, st)
    with db.session() as s:
        vals = nf.load_rule_values(s, {"XYZ", "NEW"})
    names, why = vals["XYZ"]
    assert names["news_risk_event_1d"] in (True, False) and names["news_volume_1h"] >= 0
    assert vals["NEW"][0]["news_sentiment_1d"] is None and "next news run" in vals["NEW"][1]["news_sentiment_1d"]


# ------------------------------------------------------------------ events and the no-BUY/SELL rule
def test_news_events_for_held_symbols_once_and_informational(db, st, client, account):
    position(client, account)
    run(db, st, now=datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc))
    run(db, st, now=datetime(2026, 10, 30, 22, 15, tzinfo=timezone.utc))
    with db.session() as s:
        ev = list(s.scalars(select(Event).where(Event.type == "NEWS")))
    xyz = [e for e in ev if e.symbol == "XYZ"]
    assert xyz and len({e.dedupe_key for e in xyz}) == len(xyz)              # one event per story, never repeated
    off = next(e for e in xyz if (e.source_json or {}).get("category") == "offering_dilution")
    assert off.severity == "warning" and pipeline.NOT_A_SIGNAL in off.message
    market = [e for e in ev if e.symbol is None]
    assert any("GDP" in e.title for e in market)                            # a high-importance macro release
    assert not any("approval of application" in e.title for e in market)    # low-importance Fed items aren't announced
    for e in ev:
        generated = e.message.replace(e.title, "")
        assert not re.search(r"\b(buy|sell|short|go long)\b", generated, re.I), e.message


def test_no_generated_text_is_a_buy_or_sell_instruction():
    texts = list(classify.WHY.values()) + list(nf.RULE_NAMES.values()) + [pipeline.NOT_A_SIGNAL]
    from optlab.monitoring.rules import NEWS_ONLY

    texts.append(NEWS_ONLY)
    for t in texts:
        assert not re.search(r"\b(buy|sell|go long|go short|enter now|exit now)\b", t, re.I), t
    import io
    import tokenize

    for p in Path(pipeline.__file__).parent.glob("*.py"):                  # the code itself (not its strings or comments)
        toks = tokenize.generate_tokens(io.StringIO(p.read_text()).readline)
        code = " ".join(tk.string for tk in toks if tk.type not in (tokenize.STRING, tokenize.COMMENT))
        assert not re.search(r"\b(BUY|SELL)\b", code), p.name


# ------------------------------------------------------------------ API
def test_news_api_feed_features_calendar_and_sources(db, st, client, account, monkeypatch):
    position(client, account)
    run(db, st)
    feed = client.get(f"{API}/news", params={"held": True}).json()
    assert feed["total"] > 0 and "never" in feed["note"]
    titles = [x["title"] for x in feed["items"]]
    assert any("FOMC" in x for x in titles) and not any("approval of application" in x for x in titles)  # routine Fed items left out
    a = feed["items"][0]
    assert {"published_at", "first_seen_at", "symbols", "category_label", "sentiment", "novelty", "cluster",
            "why_it_may_matter", "source"} <= set(a)
    off = next(x for x in client.get(f"{API}/news", params={"symbol": "XYZ", "category": "offering_dilution"}).json()["items"])
    assert any(y["kind"] == "position" for y in off["why_it_may_matter"]["yours"])
    assert off["sentiment"]["terms"]["negative"] == ["DILUTION"] and off["sentiment"]["model_version"].startswith("test-lexicon@")
    f = client.get(f"{API}/news/features/XYZ").json()
    assert f["features"]["version"] == "features-v1" and "news_sentiment_1d" in f["rule_values"]
    monkeypatch.setattr("optlab.api.news_routes.datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    cal = client.get(f"{API}/news/calendar", params={"days": 60}).json()
    titles = [c["title"] for c in cal]
    assert "Consumer Price Index" in titles and any("FOMC" in t for t in titles) and titles == sorted(titles, key=lambda t: titles.index(t))
    assert [c["at"] for c in cal] == sorted(c["at"] for c in cal)
    src = client.get(f"{API}/news/sources").json()
    assert {x["code"] for x in src["sources"]} == set(providers.BY_CODE)
    assert src["runner"]["state"] == "off" and "academic" in src["lexicon"]["license"]
    assert client.patch(f"{API}/news/sources/nope", json={"enabled": False}).status_code == 404
    assert client.get(f"{API}/news/articles/nope").status_code == 404


def test_refresh_endpoint_runs_an_ingest_and_respects_the_lock(client, db_url):
    from optlab.news.runner import NewsLock

    client.app.state.news_get = Getter()
    r = client.post(f"{API}/news/refresh")
    assert r.status_code == 200 and {x["code"] for x in r.json()["sources"]} == set(providers.BY_CODE)
    lk = NewsLock(db_url)
    assert lk.try_acquire()
    try:
        r = client.post(f"{API}/news/refresh")
        assert r.status_code == 409 and r.json()["error"]["code"] == "NEWS_ELSEWHERE"
    finally:
        lk.release()


def test_rescoring_after_installing_the_dictionary(db, st):
    run(db, st, model=None)
    with db.session() as s:
        assert s.scalar(select(SentimentScore)) is None
    r = run(db, st, now=NOW + timedelta(minutes=15))
    assert r.new_articles == 0 and r.rescored > 0
    assert run(db, st, now=NOW + timedelta(minutes=30)).rescored == 0


def test_scheduled_releases_are_upserted_not_duplicated(db, st):
    run(db, st)
    run(db, st, now=NOW + timedelta(days=2))                    # the calendars are fetched again after a day
    with db.session() as s:
        keys = [e.external_key for e in s.scalars(select(NewsEvent).where(NewsEvent.kind == "scheduled"))]
    assert len(keys) == len(set(keys))
    fomc = providers.FederalReserve.parse_calendar((FIX / "fomc.htm").read_text())
    assert [x.external_key for x in fomc] == ["fomc-2026-10-28", "fomc-2026-12-09", "fomc-2027-02-01"]
    assert fomc[1].details["projections"] and fomc[0].at == datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)


def test_sec_keeps_the_latest_few_filings_of_each_form(db, st):
    import json as _json

    n = 8
    sub = {"name": "Example Widgets Inc.", "filings": {"recent": {
        "accessionNumber": [f"0000101-26-1000{i}" for i in range(n)], "filingDate": ["2026-10-29"] * n,
        "acceptanceDateTime": [f"2026-10-29T2{i % 4}:00:00.000Z" for i in range(n)], "form": ["8-K"] * n,
        "items": ["8.01"] * n, "primaryDocument": [f"d{i}.htm" for i in range(n)]}}}
    r = run(db, st, Getter({"CIK0000000101": _json.dumps(sub).encode(), "getcurrent": b'<feed xmlns="http://www.w3.org/2005/Atom"/>'}))
    sec = next(x for x in r.sources if x.code == "sec_edgar")
    assert sec.new == providers.SEC_PER_FORM_CAP and "skipped 3 more 8-K" in sec.detail
    assert "424B2" not in NewsSettings().sec_forms
