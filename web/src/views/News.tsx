// Phase 5: the news / intel feed, the dashboard widget, a symbol's news panel and the macro calendar.
// News is a confirmation and risk-awareness layer: nothing here suggests or places an order.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Account, CalendarItem, NewsArticle, NewsFeatures, NewsSources } from "../api/types";
import { Empty, ErrorState, Loading, TermLabel, Tip, useToast } from "../components/ui";
import { dateTime } from "../lib/format";
import { useRefetchInterval } from "../lib/live";

const CATS: [string, string][] = [["earnings", "Earnings"], ["guidance", "Guidance"], ["sec_filing", "SEC filing"], ["offering_dilution", "Offering / dilution"],
  ["buyback", "Buyback"], ["dividend", "Dividend"], ["merger_acquisition", "Merger / acquisition"], ["analyst_action", "Analyst action"],
  ["product", "Product announcement"], ["regulatory", "Regulatory action"], ["lawsuit", "Lawsuit"], ["management_change", "Management change"],
  ["macro_release", "Macro release"], ["fed_event", "Federal Reserve event"], ["industry", "Industry event"], ["other", "Other"]];
const SENT: Record<string, { cls: string; icon: string; text: string }> = {
  positive: { cls: "ok", icon: "▲", text: "Positive" }, negative: { cls: "bad", icon: "▼", text: "Negative" },
  neutral: { cls: "muted", icon: "■", text: "Neutral" }, unknown: { cls: "muted", icon: "?", text: "Unknown" },
};
const NOTE = "News is a confirmation and risk-awareness layer: it never suggests, places or blocks an order.";
const pct = (x: number | null | undefined) => (x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`);
const signed = (x: number | null | undefined) => (x === null || x === undefined ? "—" : `${x > 0 ? "+" : ""}${x.toFixed(2)}`);
const isHttp = (u: string | null) => !!u && /^https?:\/\//i.test(u);
const sym = (s: string) => (s === "_MARKET" ? "Market" : s);

export function SentimentBadge({ a }: { a: NewsArticle }) {
  if (!a.sentiment) return <Tip text={a.sentiment_unavailable ?? "Not scored."}><span className="badge muted" tabIndex={0}>sentiment n/a</span></Tip>;
  const s = SENT[a.sentiment.label];
  return (
    <Tip text={`${a.sentiment.explanation ?? ""} · confidence ${pct(a.sentiment.confidence)} · ${a.sentiment.model_version}`}>
      <span className={`badge ${s.cls}`} tabIndex={0} aria-label={`Sentiment ${s.text}`}><span aria-hidden>{s.icon}</span> {s.text} {a.sentiment.score !== null && signed(a.sentiment.score)}</span>
    </Tip>
  );
}

export function NewsCard({ a, compact }: { a: NewsArticle; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const relevance = a.symbols.length ? Math.max(...a.symbols.map((x) => x.relevance)) : null;
  return (
    <li className="news-item">
      <div className="event-head">
        <span className="badge info">{a.category_label}</span>
        <SentimentBadge a={a} />
        {a.classification.importance === "high" && <span className="badge warn">high importance</span>}
        {a.cluster && a.cluster.duplicates > 0 && <Tip text={`The same story from ${a.cluster.source_count} source(s), ${a.cluster.article_count} articles: shown once.`}>
          <span className="badge muted" tabIndex={0}>+{a.cluster.duplicates} similar</span></Tip>}
      </div>
      <div className="news-title">{isHttp(a.url) ? <a href={a.url!} target="_blank" rel="noopener noreferrer nofollow">{a.title}</a> : a.title}</div>
      <div className="muted-text small">
        {a.source.name}{a.publisher ? ` (${a.publisher})` : ""} · {a.publication_time_known ? `published ${dateTime(a.published_at)}` : "publication time not given by the source"}
        {" · "}first seen {dateTime(a.first_seen_at)}
        {a.symbols.length > 0 && <> · {a.symbols.slice(0, 6).map((x) => (
          <Tip key={x.symbol} text={`Relevance ${pct(x.relevance)} (${x.method}): ${x.evidence ?? ""}`}><span className="chip" tabIndex={0}>{sym(x.symbol)}</span></Tip>))}</>}
      </div>
      {!compact && a.summary && <p className="small">{a.summary} <span className="muted-text">({a.summary_origin === "provider" ? "the source's summary" : "the item's opening sentences"})</span></p>}
      <p className="small why">{a.why_it_may_matter.text}</p>
      {!compact && (
        <button className="btn ghost small" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide details" : "Why these scores"}</button>)}
      {open && (
        <dl className="facts small">
          <div><dt>Category</dt><dd>{a.category_label} · confidence {pct(a.category_confidence)} · {a.classification.rule}
            {Object.keys(a.classification.matched ?? {}).length > 0 && <> · matched {Object.entries(a.classification.matched).map(([k, v]) => `${k}: ${v.join(", ")}`).join("; ")}</>}</dd></div>
          <div><dt>Sentiment</dt><dd>{a.sentiment ? <>{a.sentiment.explanation} · confidence {pct(a.sentiment.confidence)} · {a.sentiment.model_version}</> : a.sentiment_unavailable}</dd></div>
          <div><dt>Relevance</dt><dd>{relevance === null ? "no symbol" : a.symbols.map((x) => `${sym(x.symbol)} ${pct(x.relevance)} (${x.method})`).join(", ")}</dd></div>
          <div><dt>Novelty</dt><dd>{pct(a.novelty)} {a.cluster?.is_lead ? "(first report of this story)" : "(a later report of a story already seen)"}</dd></div>
          <div><dt>Source weight</dt><dd>{pct(a.source.credibility)}</dd></div>
        </dl>)}
    </li>
  );
}

function LexiconBanner({ src }: { src: NewsSources }) {
  if (src.lexicon.installed) return null;
  return (
    <div className="banner warn small" role="status">
      Sentiment is off: the Loughran–McDonald dictionary isn't installed. {src.lexicon.license} To install it, run <code>{src.lexicon.install}</code>.
      Items are still collected, de-duplicated and classified; they're scored when the dictionary is installed.
    </div>
  );
}

const STATUS_CLS: Record<string, string> = { ok: "ok", error: "bad", unavailable: "warn", disabled: "muted", never: "muted" };

export function NewsSourcesPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery<NewsSources, ApiError>({ queryKey: ["news-sources"], queryFn: api.newsSources, refetchInterval: useRefetchInterval(60_000) });
  const toggle = useMutation<unknown, ApiError, { code: string; enabled: boolean }>({
    mutationFn: ({ code, enabled }) => api.setNewsSource(code, enabled), onSuccess: () => qc.invalidateQueries({ queryKey: ["news-sources"] }),
    onError: (e) => toast(e.message, "bad"),
  });
  const refresh = useMutation<Record<string, unknown>, ApiError>({
    mutationFn: api.refreshNews,
    onSuccess: (r) => { ["news", "news-sources", "news-calendar", "news-features", "events"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })); toast(`News refreshed: ${String(r.new_articles ?? 0)} new`); },
    onError: (e) => toast(e.message, "bad"),
  });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} />;
  const d = q.data;
  return (
    <div>
      <LexiconBanner src={d} />
      {!d.identity_configured && <div className="banner warn small" role="status">SEC EDGAR is off: set <code>edgar.identity</code> in config.yaml to your name and email (the SEC requires it on every request).</div>}
      <ul className="plain source-list">{d.sources.map((x) => (
        <li key={x.code}>
          <div className="source-row">
            <label className="check"><input type="checkbox" aria-label={`${x.name} enabled`} checked={x.enabled} disabled={toggle.isPending}
              onChange={(e) => toggle.mutate({ code: x.code, enabled: e.target.checked })} /> <strong>{x.name}</strong></label>
            <Tip text={x.configuration_note ?? x.status_detail ?? x.status}><span tabIndex={0} className={`badge ${STATUS_CLS[x.configured || x.status === "disabled" ? x.status : "unavailable"]}`}>
              {x.configured ? x.status : "not configured"}</span></Tip>
          </div>
          <div className="muted-text small">last success {dateTime(x.last_success_at) ?? "never"} · {x.articles} items · weight {pct(x.credibility)}</div>
          {x.status === "error" && <div className="small bad-text">{x.status_detail}</div>}
          {!x.configured && x.configuration_note && <div className="small muted-text">{x.configuration_note}</div>}
        </li>))}</ul>
      <div className="actions"><span className="muted-text small">Fetched every {d.interval_minutes} minutes · {d.runner.state}{d.runner.detail ? ` (${d.runner.detail})` : ""}</span>
        <button className="btn small" disabled={refresh.isPending} onClick={() => refresh.mutate()}>{refresh.isPending ? "Fetching…" : "Refresh now"}</button></div>
    </div>
  );
}

export function MacroCalendar({ days = 14, compact }: { days?: number; compact?: boolean }) {
  const q = useQuery<CalendarItem[], ApiError>({ queryKey: ["news-calendar", days], queryFn: () => api.newsCalendar(days) });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} />;
  const rows = compact ? q.data.filter((x) => x.importance === "high").slice(0, 5) : q.data;
  if (!rows.length) return <Empty>No scheduled releases in the next {days} days (or the calendars haven't been fetched yet).</Empty>;
  return (
    <ul className="plain small calendar">{rows.map((e) => (
      <li key={e.id}><span className="tl-time">{dateTime(e.at)}</span> {e.importance === "high" && <span className="badge warn">high</span>} {e.title}
        <span className="muted-text"> · {e.source_code === "federal_reserve" ? "Federal Reserve" : e.source_code.toUpperCase()}{typeof e.details.time === "string" ? ` · ${e.details.time}` : ""}</span></li>))}</ul>
  );
}

// ------------------------------------------------------------------ the feed
export function NewsView({ account }: { account: Account }) {
  const [symbol, setSymbol] = useState("");
  const [category, setCategory] = useState("");
  const [held, setHeld] = useState(false);
  const [dups, setDups] = useState(false);
  const [text, setText] = useState("");
  const [limit, setLimit] = useState(30);
  const params = { symbol: symbol.trim() ? [symbol.trim().toUpperCase()] : undefined, category: category ? [category] : undefined,
                   held, include_duplicates: dups, q: text.trim() || undefined, limit };
  const q = useQuery({ queryKey: ["news", account.id, params], queryFn: () => api.news(params), refetchInterval: useRefetchInterval(120_000) });
  return (
    <div className="dashboard news-layout">
      <section className="panel" aria-labelledby="news-h">
        <div className="panel-head"><h2 id="news-h"><TermLabel k="news" /></h2>
          <div className="panel-tools">
            <input aria-label="Symbol" placeholder="Symbol" value={symbol} maxLength={12} onChange={(e) => setSymbol(e.target.value)} style={{ width: "7em" }} />
            <select aria-label="Category" value={category} onChange={(e) => setCategory(e.target.value)}><option value="">All categories</option>
              {CATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
            <input aria-label="Search headlines" placeholder="Search" value={text} maxLength={100} onChange={(e) => setText(e.target.value)} />
            <label className="check small"><input type="checkbox" checked={held} onChange={(e) => setHeld(e.target.checked)} /> Only what I hold, plan or watch</label>
            <label className="check small"><input type="checkbox" checked={dups} onChange={(e) => setDups(e.target.checked)} /> Show duplicates</label>
          </div></div>
        <p className="muted-text small">{NOTE} Sentiment uses the Loughran–McDonald financial dictionary (deterministic; the matched words are shown). Summaries are the source's own or the item's opening sentences, never generated.</p>
        {q.isPending ? <Loading /> : q.isError ? <ErrorState error={q.error} retry={() => q.refetch()} /> : q.data.items.length === 0 ? (
          <Empty>No news yet for these filters. Sources are fetched every few minutes; see their status on the right.</Empty>) : (
          <>
            <ol className="news-list">{q.data.items.map((a) => <NewsCard key={a.id} a={a} />)}</ol>
            {q.data.total > q.data.items.length && <button className="btn" onClick={() => setLimit(limit + 30)}>Show more ({q.data.total - q.data.items.length} more)</button>}
          </>)}
      </section>
      <aside className="news-side">
        <section className="panel"><h3>Scheduled releases</h3><MacroCalendar days={21} /></section>
        <section className="panel"><h3>Sources</h3><NewsSourcesPanel /></section>
      </aside>
    </div>
  );
}

// ------------------------------------------------------------------ dashboard widget
export function NewsWidget({ account }: { account: Account }) {
  const q = useQuery({ queryKey: ["news", account.id, "held"], queryFn: () => api.news({ held: true, limit: 6 }), refetchInterval: useRefetchInterval(120_000) });
  return (
    <section className="panel" aria-labelledby="newsw-h">
      <div className="panel-head"><h2 id="newsw-h"><TermLabel k="news" /></h2><div className="panel-tools"><a className="btn small ghost" href="#/news">All news</a></div></div>
      {q.isPending ? <Loading /> : q.isError ? <ErrorState error={q.error} /> : q.data.items.length === 0 ? <Empty>No recent news about what you hold, plan or watch.</Empty> : (
        <ol className="news-list compact">{q.data.items.map((a) => <NewsCard key={a.id} a={a} compact />)}</ol>)}
      <h3 className="small">Next high-importance releases</h3>
      <MacroCalendar days={10} compact />
      <p className="muted-text small">{NOTE}</p>
    </section>
  );
}

// ------------------------------------------------------------------ one symbol (position detail, plans)
export function SymbolNews({ symbol }: { symbol: string }) {
  const f = useQuery<NewsFeatures, ApiError>({ queryKey: ["news-features", symbol], queryFn: () => api.newsFeatures(symbol) });
  const n = useQuery({ queryKey: ["news", "symbol", symbol], queryFn: () => api.news({ symbol: [symbol], limit: 5 }) });
  const ft = f.data?.features;
  const v = (k: string) => (ft ? ft[k] : null) as number | null;
  return (
    <section className="panel" aria-labelledby={`sn-${symbol}`}>
      <h3 id={`sn-${symbol}`}>News: {symbol}</h3>
      {f.isPending ? <Loading /> : f.isError ? <ErrorState error={f.error} /> : !ft ? <p className="muted-text small">No news features for {symbol} yet: they're computed on the next news run.</p> : (
        <dl className="facts small" aria-label={`News features for ${symbol}`}>
          <div><dt>Sentiment, last day</dt><dd>{signed(v("news_sentiment_1d"))} <span className="muted-text">({String(ft.scored_1d ?? 0)} scored)</span></dd></div>
          <div><dt>Sentiment, last hour</dt><dd>{signed(v("news_sentiment_1h"))}</dd></div>
          <div><dt>Stories, last hour / day</dt><dd>{String(ft.news_volume_1h ?? 0)} / {String(ft.news_volume_1d ?? 0)}</dd></div>
          <div><dt>Positive / negative</dt><dd>{String(ft.bullish_article_count ?? 0)} / {String(ft.bearish_article_count ?? 0)}{ft.conflicting ? " (conflicting)" : ""}</dd></div>
          <div><dt>Since the latest story</dt><dd>{v("minutes_since_news") === null ? "none in 30 days" : `${Math.round(v("minutes_since_news")!)} min`}</dd></div>
          <div><dt>Risk event today</dt><dd>{ft.risk_event_1d ? `yes (${String(ft.event_type ?? "")})` : "no"}</dd></div>
          {ft.status !== "ok" && <div><dt>Data</dt><dd className="warn-text">{String(ft.status)}: {(ft.sources_down as string[] | undefined)?.join(", ")}</dd></div>}
        </dl>)}
      {n.data && n.data.items.length > 0 && <ol className="news-list compact">{n.data.items.map((a) => <NewsCard key={a.id} a={a} compact />)}</ol>}
      <p className="muted-text small">{NOTE} Monitoring rules can use these values to confirm a condition (e.g. <code>price &gt; sma(50) and news_sentiment_1d &gt; 0</code>), never on their own.</p>
    </section>
  );
}
