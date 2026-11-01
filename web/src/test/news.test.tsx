import * as Tooltip from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NewsArticle, NewsSources } from "../api/types";
import { ToastProvider } from "../components/ui";
import { TermsContext } from "../lib/terms";
import { NewsCard, NewsSourcesPanel, NewsWidget, SymbolNews } from "../views/News";

function wrap(ui: ReactNode, mode: "professional" | "colony" = "professional") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><Tooltip.Provider><ToastProvider><TermsContext.Provider value={mode}>{ui}</TermsContext.Provider></ToastProvider></Tooltip.Provider></QueryClientProvider>);
}
type Route = [RegExp, () => Response];
function mockFetch(routes: Route[]) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    for (const [re, res] of routes) if (re.test(url)) return res();
    return new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "no mock for " + url } }), { status: 404 });
  }));
}
const json = (b: unknown) => () => new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } });
afterEach(() => vi.unstubAllGlobals());

const ART: NewsArticle = {
  id: "a1", title: "Example Widgets prices public offering of common stock", url: "https://ir.example.com/news/offering", publisher: null,
  source: { code: "company_ir", name: "Company investor-relations feeds", kind: "company", credibility: 0.85 },
  published_at: "2026-10-30T21:00:00Z", first_seen_at: "2026-10-30T21:05:00Z", publication_time_known: true,
  summary: "Example Widgets announced the pricing of an underwritten public offering.", summary_origin: "provider",
  symbols: [{ symbol: "XYZ", method: "company", relevance: 0.95, evidence: "the company's own feed" }],
  category: "offering_dilution", category_label: "Offering / dilution", category_confidence: 0.85,
  classification: { version: "classify-rules/v1", rule: "keyword rule 'offering_dilution'", matched: { offering_dilution: ["public offering"] }, secondary: [], importance: "high" },
  sentiment: { label: "negative", score: -1, confidence: 0.333, model: "loughran-mcdonald", model_version: "lm-1993-2025@abc/lm-rules/v1",
               counts: { positive: 0, negative: 1, uncertainty: 1, litigious: 0, constraining: 0, words: 20 }, terms: { negative: ["DILUTION"] },
               explanation: "0 positive and 1 negative Loughran–McDonald words in 20 (negative: dilution)" },
  sentiment_unavailable: null, novelty: 1,
  cluster: { id: "c1", article_count: 3, source_count: 2, is_lead: true, duplicate_of: null, duplicates: 2 },
  why_it_may_matter: { text: "New shares or convertible debt can dilute existing holders. It concerns your open position XYZ put credit spread.",
                       yours: [{ kind: "position", id: "p1", name: "XYZ put credit spread", symbol: "XYZ" }], note: "For awareness only." },
};
const SOURCES: NewsSources = {
  sources: [{ code: "sec_edgar", name: "SEC EDGAR", kind: "filings", enabled: true, configured: false, configuration_note: "Set edgar.identity", credibility: 0.95,
              status: "unavailable", status_detail: "Set edgar.identity", last_fetch_at: null, last_success_at: null, items_last: 0, new_last: 0, articles: 0 },
            { code: "bls", name: "Bureau of Labor Statistics", kind: "macro", enabled: true, configured: true, configuration_note: null, credibility: 0.95,
              status: "error", status_detail: "HTTP 503 from https://www.bls.gov/feed/cpi.rss", last_fetch_at: "2026-11-01T16:00:00Z", last_success_at: null, items_last: 0, new_last: 0, articles: 4 }],
  interval_minutes: 15, identity_configured: false, note: "never",
  lexicon: { installed: false, model_version: null, note: "not installed", license: "Free for academic research.", page: "https://sraf.nd.edu", install: "python scripts/news.py install-lexicon --accept-license" },
  runner: { state: "waiting", detail: null },
};

describe("news", () => {
  it("a news card shows source, both times, symbols, category, sentiment, duplicates and why it may matter", () => {
    wrap(<ol><NewsCard a={ART} /></ol>);
    expect(screen.getByRole("link", { name: ART.title })).toHaveAttribute("href", ART.url);
    expect(screen.getByRole("link", { name: ART.title })).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(screen.getByText(/published/)).toBeInTheDocument();
    expect(screen.getByText(/first seen/)).toBeInTheDocument();
    expect(screen.getByText("XYZ")).toBeInTheDocument();
    expect(screen.getByText("Offering / dilution")).toBeInTheDocument();
    expect(screen.getByLabelText("Sentiment Negative")).toBeInTheDocument();
    expect(screen.getByText("+2 similar")).toBeInTheDocument();
    expect(screen.getByText(/the source's summary/)).toBeInTheDocument();
    expect(screen.getByText(/It concerns your open position/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Why these scores" }));
    expect(screen.getByText(/negative: dilution/)).toBeInTheDocument();
    expect(screen.getByText(/first report of this story/)).toBeInTheDocument();
    expect(screen.queryByText(/\b(buy|sell)\b/i)).toBeNull();
  });
  it("an unsafe link is shown as text, an unknown publication time says so, and missing sentiment is labelled", () => {
    const a = { ...ART, url: "javascript:alert(1)", published_at: null, publication_time_known: false, sentiment: null,
                sentiment_unavailable: "The dictionary isn't installed." };
    wrap(<ol><NewsCard a={a} /></ol>);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/publication time not given by the source/)).toBeInTheDocument();
    expect(screen.getByText("sentiment n/a")).toBeInTheDocument();
  });
  it("the sources panel explains a missing dictionary, a missing SEC identity and a failing source", async () => {
    mockFetch([[/news\/sources/, json(SOURCES)]]);
    wrap(<NewsSourcesPanel />);
    await waitFor(() => expect(screen.getByText(/Sentiment is off/)).toBeInTheDocument());
    expect(screen.getByText(/install-lexicon --accept-license/)).toBeInTheDocument();
    expect(screen.getByText(/SEC EDGAR is off/)).toBeInTheDocument();
    expect(screen.getByText("not configured")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "SEC EDGAR enabled" })).toBeChecked();
  });
  it("the dashboard widget lists held-symbol news and high-importance releases in colony terms", async () => {
    mockFetch([[/\/news\?/, json({ items: [ART], total: 1, limit: 6, offset: 0, note: "" })],
               [/news\/calendar/, json([{ id: "e1", title: "Consumer Price Index", at: "2026-11-10T13:30:00Z", source_code: "bls", category: "macro_release",
                                         category_label: "Macro release", importance: "high", details: {} }])]]);
    wrap(<NewsWidget account={{ id: "acc", name: "Main" } as never} />, "colony");
    await waitFor(() => expect(screen.getByText(ART.title)).toBeInTheDocument());
    expect(screen.getByText("Intel feed")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Consumer Price Index/)).toBeInTheDocument());
    expect(screen.getAllByText(/never suggests, places or blocks an order/).length).toBeGreaterThan(0);
  });
  it("symbol news shows the features used by monitoring rules", async () => {
    mockFetch([[/news\/features\/XYZ/, json({ symbol: "XYZ", as_of: "2026-11-01T16:00:00Z", version: "features-v1", note: "",
      features: { news_sentiment_1d: -0.42, news_sentiment_1h: null, scored_1d: 3, news_volume_1h: 1, news_volume_1d: 3, bullish_article_count: 0,
                  bearish_article_count: 2, conflicting: false, minutes_since_news: 35, risk_event_1d: true, event_type: "offering_dilution", status: "ok", sources_down: [] },
      rule_values: {}, why_missing: {}, meanings: {} })], [/\/news\?/, json({ items: [], total: 0, limit: 5, offset: 0, note: "" })]]);
    wrap(<SymbolNews symbol="XYZ" />);
    await waitFor(() => expect(screen.getByLabelText("News features for XYZ")).toBeInTheDocument());
    expect(screen.getByText("-0.42")).toBeInTheDocument();
    expect(screen.getByText(/yes \(offering_dilution\)/)).toBeInTheDocument();
    expect(screen.getByText(/never on their own/)).toBeInTheDocument();
  });
});
