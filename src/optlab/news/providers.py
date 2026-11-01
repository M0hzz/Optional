"""News and official-event providers. Each implements the same interface, so it can be enabled, disabled or
replaced without touching the pipeline:

    code, name, kind, default credibility
    available(settings) -> (bool, why not)
    fetch(ctx) -> Fetched(items, scheduled)

Providers only fetch and parse; storing, mapping, de-duplicating, classifying and scoring happen in
pipeline.py. A provider that fails raises; the pipeline records it and the other providers still run.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import feeds
from .classify import Classification, classify_fed, classify_filing, classify_macro
from .http import FetchError, Get
from .normalize import clean_text, opening_sentences
from .settings import SEC_PER_FORM_CAP, NewsSettings
from .symbols import SymbolMap

ET = ZoneInfo("America/New_York")


@dataclass
class RawItem:
    external_id: str
    title: str
    url: str | None
    published_at: datetime | None
    summary: str | None = None
    summary_origin: str = "none"                 # provider | opening_sentences | none
    raw: dict[str, Any] = field(default_factory=dict)
    filer_cik: str | None = None
    provider_symbols: list[str] = field(default_factory=list)
    company_symbol: str | None = None
    market: bool = False
    classification: Classification | None = None


@dataclass
class Scheduled:
    external_key: str
    title: str
    at: datetime
    category: str
    importance: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Fetched:
    items: list[RawItem] = field(default_factory=list)
    scheduled: list[Scheduled] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Ctx:
    settings: NewsSettings
    get: Get
    followed: set[str]
    smap: SymbolMap
    now: datetime
    state: dict[str, Any]                        # per-provider memory for the life of the process

    @property
    def headers(self) -> dict:
        return {"User-Agent": self.settings.user_agent, "Accept-Encoding": "gzip, deflate"}


class Provider:
    code = ""
    name = ""
    kind = "news"

    def available(self, st: NewsSettings) -> tuple[bool, str | None]:
        return True, None

    def fetch(self, ctx: Ctx) -> Fetched:            # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def due(ctx: Ctx, key: str, minutes: float) -> bool:
        last = ctx.state.get(key)
        return last is None or ctx.now - last >= timedelta(minutes=minutes)


def _summary(text: str | None, title: str) -> tuple[str | None, str]:
    s = clean_text(text)
    if not s or s.lower().strip(" .") == title.lower().strip(" ."):
        return None, "none"
    return s, "provider"


# ------------------------------------------------------------------ SEC EDGAR
class SecEdgar(Provider):
    code, name, kind = "sec_edgar", "SEC EDGAR", "filings"
    CURRENT = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&start=0&count=100&output=atom"
    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
    TICKERS = "https://www.sec.gov/files/company_tickers.json"
    TITLE = re.compile(r"^(?P<form>[\w\-/ ]+?) - (?P<name>.+?) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)")

    def available(self, st):
        if not st.identity_ok:
            return False, ("Set edgar.identity in config.yaml (or OPTLAB_SEC_IDENTITY) to your name and email: "
                           "the SEC requires it on every request.")
        return True, None

    def refresh_tickers(self, ctx: Ctx) -> SymbolMap:
        path = ctx.settings.cache_dir / "sec_company_tickers.json"
        cached, fresh = SymbolMap.cached(path)
        if cached is not None and fresh:
            return cached
        try:
            data = ctx.get(self.TICKERS, ctx.headers)
            m = SymbolMap.from_json(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return m
        except (FetchError, ValueError):
            if cached is not None:
                return cached
            raise

    def _item(self, form: str, name: str, cik: str, acc: str, items: list[str], url: str | None, at: datetime | None,
              filed: str | None) -> RawItem:
        from ..filings.company import describe_items

        labels = describe_items(",".join(items))
        title = f"{form}: {name}" + (f" — {labels}" if labels else "")
        summary = (f"Form {form} filed {filed or '?'} (accession {acc})."
                   + (" Items: " + "; ".join(f"{c} {describe_items(c) or ''}".strip() for c in items) + "." if items else ""))
        return RawItem(external_id=acc, title=title, url=url, published_at=at, summary=summary, summary_origin="provider",
                       raw={"form": form, "cik": cik, "company": name, "accession": acc, "items": items, "filed": filed},
                       filer_cik=cik, classification=classify_filing(form, items))

    def fetch(self, ctx: Ctx) -> Fetched:
        out = Fetched()
        ctx.smap = self.refresh_tickers(ctx)
        ciks: dict[str, str] = {}
        for t in ctx.followed:
            if (c := ctx.smap.cik(t)) is not None:
                ciks[c] = t
        for e in feeds.parse(ctx.get(self.CURRENT, ctx.headers))[: ctx.settings.max_items_per_source]:
            m = self.TITLE.match(e.title or "")
            if not m or m.group("cik") not in ciks:
                continue                                      # only companies you follow
            acc = re.search(r"accession-number=([\d-]+)", e.guid or "") or re.search(r"AccNo:\s*([\d-]+)", e.summary or "")
            if not acc:
                continue
            items = re.findall(r"Item (\d+\.\d+)", clean_text(e.summary) or "")
            filed = re.search(r"Filed:\s*(\d{4}-\d{2}-\d{2})", clean_text(e.summary) or "")
            out.items.append(self._item(m.group("form").strip(), m.group("name"), m.group("cik"), acc.group(1), items,
                                        e.link, e.published, filed.group(1) if filed else None))
        if self.due(ctx, "submissions", ctx.settings.sec_submissions_every_minutes):
            since = (ctx.now - timedelta(days=ctx.settings.sec_lookback_days)).date().isoformat()
            forms = set(ctx.settings.sec_forms)
            for cik, sym in sorted(ciks.items())[:60]:
                try:
                    d = json.loads(ctx.get(self.SUBMISSIONS.format(cik=cik), ctx.headers))
                except (FetchError, ValueError) as exc:
                    out.notes.append(f"{sym}: {exc}")
                    continue
                r = d.get("filings", {}).get("recent", {})
                per_form: dict[str, int] = {}
                for i, form in enumerate(r.get("form", [])):
                    if form not in forms or r["filingDate"][i] < since:
                        continue
                    per_form[form] = per_form.get(form, 0) + 1
                    if per_form[form] > SEC_PER_FORM_CAP:          # newest first: keep the latest few of each form
                        continue
                    acc = r["accessionNumber"][i]
                    doc = r.get("primaryDocument", [""] * (i + 1))[i]
                    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}" if doc else None
                    items = [x.strip() for x in (r.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
                    out.items.append(self._item(form, d.get("name", sym), cik, acc, items, url,
                                                feeds.parse_time(r.get("acceptanceDateTime", [None] * (i + 1))[i]),
                                                r["filingDate"][i]))
                capped = {f: n - SEC_PER_FORM_CAP for f, n in per_form.items() if n > SEC_PER_FORM_CAP}
                if capped:
                    out.notes.append(f"{sym}: kept the latest {SEC_PER_FORM_CAP} of each form; skipped "
                                     + ", ".join(f"{n} more {f}" for f, n in capped.items()))
            ctx.state["submissions"] = ctx.now
        return out


# ------------------------------------------------------------------ Federal Reserve
class FederalReserve(Provider):
    code, name, kind = "federal_reserve", "Federal Reserve", "central_bank"
    PRESS = "https://www.federalreserve.gov/feeds/press_all.xml"
    CALENDAR = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    _M = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    MONTHS = {**{m: i for i, m in enumerate(_M, 1)}, **{m[:3]: i for i, m in enumerate(_M, 1)}, "Sept": 9}

    def fetch(self, ctx: Ctx) -> Fetched:
        out = Fetched()
        for e in feeds.parse(ctx.get(self.PRESS, ctx.headers))[: ctx.settings.max_items_per_source]:
            s, origin = _summary(e.summary, e.title)
            out.items.append(RawItem(e.guid or e.link or e.title, clean_text(e.title, 500) or "", e.link, e.published, s, origin,
                                     {"categories": e.categories}, market=True, classification=classify_fed(e.categories, e.title)))
        if self.due(ctx, "calendar", 24 * 60):
            try:
                out.scheduled += self.parse_calendar(ctx.get(self.CALENDAR, ctx.headers).decode("utf-8", "replace"))
                ctx.state["calendar"] = ctx.now
            except FetchError as exc:
                out.notes.append(f"FOMC calendar: {exc}")
        return out

    @classmethod
    def parse_calendar(cls, page: str) -> list[Scheduled]:
        """FOMC meetings: the statement is released at 2:00 p.m. ET on the meeting's last day."""
        out = []
        for ym in re.finditer(r"(\d{4}) FOMC Meetings(.*?)(?=\d{4} FOMC Meetings|$)", page, re.S):
            year, body = int(ym.group(1)), ym.group(2)
            for m in re.finditer(r"fomc-meeting__month[^>]*><strong>([A-Za-z/]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<", body, re.S):
                months, days = m.group(1).split("/"), m.group(2).strip()
                nums = re.findall(r"\d+", days)
                if not nums or months[-1] not in cls.MONTHS:
                    continue
                last_day = int(nums[-1])
                month = cls.MONTHS[months[-1]]
                try:
                    d = date(year, month, last_day)
                except ValueError:
                    continue
                at = datetime.combine(d, time(14, 0), ET).astimezone(timezone.utc)
                sep = "*" in days
                out.append(Scheduled(f"fomc-{d.isoformat()}", "FOMC statement" + (" and economic projections" if sep else ""),
                                     at, "fed_event", "high", {"meeting": f"{m.group(1)} {days}", "time": "2:00 p.m. ET",
                                                               "projections": sep}))
        return out


# ------------------------------------------------------------------ BLS
class Bls(Provider):
    code, name, kind = "bls", "Bureau of Labor Statistics", "macro"
    FEED = "https://www.bls.gov/feed/{name}.rss"
    SCHEDULE = "https://www.bls.gov/schedule/news_release/bls.ics"
    HIGH = re.compile(r"consumer price index|employment situation|producer price index|employment cost index", re.I)

    def fetch(self, ctx: Ctx) -> Fetched:
        out = Fetched()
        errors = 0
        for name in ctx.settings.bls_feeds:
            try:
                entries = feeds.parse(ctx.get(self.FEED.format(name=name), ctx.headers))
            except (FetchError, ValueError) as exc:
                errors += 1
                out.notes.append(f"{name}: {exc}")
                continue
            for e in entries[:20]:
                title = clean_text(e.title, 500) or ""
                out.items.append(RawItem(e.guid or e.link or title, title, e.link, e.published,
                                         opening_sentences(e.summary), "opening_sentences" if e.summary else "none",
                                         {"feed": name}, market=True, classification=classify_macro("BLS", title)))
        if errors and errors == len(ctx.settings.bls_feeds):
            raise FetchError("every BLS feed failed: " + "; ".join(out.notes[:3]))
        if self.due(ctx, "schedule", 24 * 60):
            try:
                for ev in feeds.parse_ics(ctx.get(self.SCHEDULE, ctx.headers)):
                    if ctx.now - timedelta(days=1) <= ev.start <= ctx.now + timedelta(days=90):
                        out.scheduled.append(Scheduled(f"bls-{ev.uid}", ev.summary, ev.start, "macro_release",
                                                       "high" if self.HIGH.search(ev.summary) else "medium",
                                                       {"source": "BLS release schedule"}))
                ctx.state["schedule"] = ctx.now
            except (FetchError, ValueError) as exc:
                out.notes.append(f"schedule: {exc}")
        return out


# ------------------------------------------------------------------ BEA
class Bea(Provider):
    code, name, kind = "bea", "Bureau of Economic Analysis", "macro"
    FEED = "https://apps.bea.gov/rss/rss.xml"

    def fetch(self, ctx: Ctx) -> Fetched:
        out = Fetched()
        for e in feeds.parse(ctx.get(self.FEED, ctx.headers))[: ctx.settings.max_items_per_source]:
            title = clean_text(e.title, 500) or ""
            s, origin = _summary(e.summary, title)
            out.items.append(RawItem(e.guid or e.link or title, title, e.link, e.published, s, origin,
                                     {k: v for k, v in e.extra.items() if k in ("vintage", "NextReleaseDate", "dbId")},
                                     market=True, classification=classify_macro("BEA", title)))
            nxt = e.extra.get("NextReleaseDate")
            if nxt:
                try:
                    d = datetime.strptime(nxt.strip(), "%B %d, %Y").date()
                except ValueError:
                    continue
                name = re.sub(r",.*$", "", title).strip()
                out.scheduled.append(Scheduled(f"bea-{name}-{d.isoformat()}", f"{name} (next release)",
                                               datetime.combine(d, time(8, 30), ET).astimezone(timezone.utc), "macro_release",
                                               "high" if re.search(r"\b(GDP|personal income|PCE)\b", name, re.I) else "medium",
                                               {"time": "8:30 a.m. ET (BEA's usual release time; the feed gives the date only)"}))
        return out


# ------------------------------------------------------------------ company investor-relations feeds
class CompanyIR(Provider):
    code, name, kind = "company_ir", "Company investor-relations feeds", "company"

    def available(self, st):
        if not st.ir_feeds:
            return False, "No company feeds configured (news.company_ir in config.yaml: SYMBOL: feed URL)."
        return True, None

    def fetch(self, ctx: Ctx) -> Fetched:
        out = Fetched()
        failed = 0
        urls = [(sym, u) for sym, us in sorted(ctx.settings.ir_feeds.items()) for u in us]
        for sym, url in urls:
            try:
                entries = feeds.parse(ctx.get(url, ctx.headers))
            except (FetchError, ValueError) as exc:
                failed += 1
                out.notes.append(f"{sym}: {exc}")
                continue
            for e in entries[:50]:
                title = clean_text(e.title, 500) or ""
                s, origin = _summary(e.summary, title)
                out.items.append(RawItem(e.guid or e.link or title, title, e.link, e.published, s, origin,
                                         {"feed": url, "symbol": sym}, company_symbol=sym))
        if urls and failed == len(urls):
            raise FetchError("every company feed failed: " + "; ".join(out.notes[:3]))
        return out


# ------------------------------------------------------------------ Finnhub (optional; needs FINNHUB_API_KEY)
class Finnhub(Provider):
    code, name, kind = "finnhub", "Finnhub company news", "news"
    URL = "https://finnhub.io/api/v1/company-news?symbol={sym}&from={start}&to={end}"

    def available(self, st):
        if not st.finnhub_key:
            return False, "Disabled: set FINNHUB_API_KEY in the environment to use Finnhub (optional)."
        return True, None

    def fetch(self, ctx: Ctx) -> Fetched:
        out = Fetched()
        start = (ctx.now - timedelta(days=ctx.settings.finnhub_lookback_days)).date().isoformat()
        end = ctx.now.date().isoformat()
        headers = {**ctx.headers, "X-Finnhub-Token": ctx.settings.finnhub_key or ""}     # header, never the URL
        syms = sorted(s for s in ctx.followed if s != "_MARKET")[:30]
        for sym in syms:
            try:
                rows = json.loads(ctx.get(self.URL.format(sym=sym, start=start, end=end), headers))
            except (FetchError, ValueError) as exc:
                out.notes.append(f"{sym}: {exc}")
                if "401" in str(exc) or "403" in str(exc):
                    raise FetchError("Finnhub refused the API key (HTTP 401/403)") from None
                continue
            for r in rows[:50] if isinstance(rows, list) else []:
                title = clean_text(str(r.get("headline") or ""), 500) or ""
                if not title:
                    continue
                s, origin = _summary(r.get("summary"), title)
                ts = r.get("datetime")
                pub = datetime.fromtimestamp(int(ts), timezone.utc) if isinstance(ts, (int, float)) and ts > 0 else None
                out.items.append(RawItem(str(r.get("id") or r.get("url") or title), title, r.get("url"), pub, s, origin,
                                         {"publisher": r.get("source"), "category": r.get("category"), "related": r.get("related")},
                                         provider_symbols=[x for x in str(r.get("related") or sym).split(",") if x]))
        return out


ALL: list[Provider] = [SecEdgar(), FederalReserve(), Bls(), Bea(), CompanyIR(), Finnhub()]
BY_CODE = {p.code: p for p in ALL}
