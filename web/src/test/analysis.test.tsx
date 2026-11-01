import * as Tooltip from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnalysisDraft, AnalysisRun } from "../api/types";
import { ToastProvider } from "../components/ui";
import { TermsContext } from "../lib/terms";
import { AnalysisForm, AnalysisRunView } from "../views/Analysis";

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><Tooltip.Provider><ToastProvider><TermsContext.Provider value="professional">{ui}</TermsContext.Provider></ToastProvider></Tooltip.Provider></QueryClientProvider>);
}
const calls: { url: string; body: unknown }[] = [];
function mockFetch(routes: [RegExp, () => Response][]) {
  calls.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
    for (const [re, res] of routes) if (re.test(url)) return res();
    return new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "no mock for " + url } }), { status: 404 });
  }));
}
const json = (b: unknown, status = 200) => () => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
afterEach(() => vi.unstubAllGlobals());

const DRAFT: AnalysisDraft = {
  subject: { type: "plan", id: "p1", label: "XYZ pullback", link: "#/plans/p1" }, name: "Plan: XYZ pullback", symbols: ["XYZ"], timeframe: "1d",
  start: "2018-11-03", end: "2026-11-01", direction: "bullish", horizons: ["1", "5", "10", "20"], focus_horizon: "5", dedupe_bars: 20,
  include_near: true, minimum_score: 80, required_mode: "tolerant", source: "synthetic", notes: [],
  conditions: [{ id: "c1", expression: "price > sma(50)", origin: "trigger", required: true, weight: 1, tolerance_type: "percent_distance", tolerance: 2 }],
  excluded: [{ expression: "atm_iv < 0.4", origin: "trigger", reason: "needs historical implied volatility, which isn't stored" }],
};
const RUN: AnalysisRun = {
  id: "r1", subject_type: "plan", subject_id: "p1", title: "Plan: XYZ pullback", strategy_version: "abc123", direction: "bullish", focus_horizon: "5",
  source: "synthetic", status: "done", error: null, created_at: "2026-11-01T19:00:00Z", started_at: null, finished_at: null, seconds: 0.9,
  counts: { matches: 2, exact: 1, near: 1, thinned: 0, bars_evaluated: 2000, not_evaluable: 0, stored: 2 },
  strategy: { name: "x", symbols: ["XYZ"], timeframe: "1d", range: { start: "2018-11-03", end: "2026-11-01" },
    conditions: [{ id: "c1", expression: "price > sma(50)", required: true, weight: 1 }],
    matching: { minimum_score: 80, include_near: true, required_mode: "tolerant" }, forward: { horizons: ["1", "5"], dedupe_bars: 20 } },
  excluded: DRAFT.excluded,
  result: {
    version: "stats-v1", definitions: "defs", warnings: [], truncation: { checked: 2, mismatches: 0, details: [] },
    horizons: [{ horizon: "5", group: "all", signals: 2, incomplete: 0, mean_return_pct: 1, median_return_pct: 1, p25_return_pct: -1, p75_return_pct: 2,
      best_pct: 3, worst_pct: -1, up_pct: 50, down_pct: 50, mean_favorable_pct: 2, mean_adverse_pct: -1.5, median_realized_vol_pct: 20, low_sample: true, win_pct: 50, wins: 1, losses: 1 }],
    distributions: [{ horizon: "5", signals: 2, bins: [{ from_pct: -1, to_pct: 0, count: 1, kind: "loss", open_low: false, open_high: false },
                                                      { from_pct: 0, to_pct: 3, count: 1, kind: "win", open_low: false, open_high: false }] }],
    breakdowns: [{ dimension: "trend", subset: "above 200-bar average", signals: 1, median_return_pct: 3, mean_return_pct: 3, up_pct: 100, win_pct: 100, low_sample: true, keys: ["XYZ|a"] },
                 { dimension: "trend", subset: "below 200-bar average", signals: 1, median_return_pct: -1, mean_return_pct: -1, up_pct: 0, win_pct: 0, low_sample: true, keys: ["XYZ|b"] }],
    bias: [{ code: "SYNTHETIC", title: "Synthetic data", status: "fail", detail: "generated demo data" },
           { code: "NO_LOOKAHEAD", title: "Conditions use only past and current bars", status: "pass", detail: "Truncation test: 2 signal(s) re-evaluated" }],
    coverage: [{ symbol: "XYZ", source: "synthetic", first_bar: "2018-11-05", last_bar: "2026-10-30", bars_in_range: 2000, missing_bars: 0, warmup_requested: 260, warmup_available: 320, incomplete_bars_dropped: 0, adjusted: true, warnings: [] }],
  },
  matches: [
    { n: 0, key: "XYZ|a", symbol: "XYZ", timestamp: "2020-01-02T00:00:00-05:00", match_type: "exact", score: 100, passed: 1, near: 0, failed: 0, conditions: { c1: { status: "pass", value: 10, score: 100 } },
      explanation: "all conditions pass", thinned: false, forward: { "5": { return_pct: 3, favorable_pct: 4, adverse_pct: -1 } }, labels: { trend: "above 200-bar average" }, is_entry: true },
    { n: 1, key: "XYZ|b", symbol: "XYZ", timestamp: "2021-03-04T00:00:00-05:00", match_type: "near", score: 85, passed: 0, near: 1, failed: 0, conditions: { c1: { status: "near", value: 9, score: 85 } },
      explanation: "c1 near", thinned: false, forward: { "5": { return_pct: -1, favorable_pct: 1, adverse_pct: -2 } }, labels: { trend: "below 200-bar average" }, is_entry: false },
  ],
};

describe("historical analysis", () => {
  it("the draft lists the conditions, what was left out and why, and submits then starts the run", async () => {
    mockFetch([[/analysis\/runs\/r9\/start/, json({ id: "r9", status: "running" })], [/analysis\/runs$/, json({ id: "r9", status: "queued", strategy_version: "x", source: "synthetic" }, 202)]]);
    wrap(<AnalysisForm draft={DRAFT} />);
    expect(screen.getByLabelText("Condition c1")).toHaveValue("price > sma(50)");
    expect(screen.getByText(/Left out of the historical search/)).toBeInTheDocument();
    expect(screen.getByText(/historical implied volatility/)).toBeInTheDocument();
    expect(screen.getByText(/Synthetic mode/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run the analysis" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/analysis/runs/r9/start"))).toBe(true));
    const body = calls.find((c) => c.url.endsWith("/analysis/runs"))!.body as Record<string, unknown>;
    expect(body.subject_type).toBe("plan");
    expect(body.horizons).toEqual(["1", "5", "10", "20"]);
    expect(body.excluded).toEqual(DRAFT.excluded);
  });
  it("results show bias checks, low samples, the distribution and breakdown drill-down to matches", async () => {
    mockFetch([[/analysis\/runs\/r1$/, json(RUN)]]);
    wrap(<AnalysisRunView id="r1" />);
    await waitFor(() => expect(screen.getByText("Bias checks")).toBeInTheDocument());
    expect(screen.getByText("Synthetic data")).toBeInTheDocument();
    expect(screen.getByText(/Truncation test/)).toBeInTheDocument();
    expect(screen.getAllByText("low sample").length).toBeGreaterThan(0);
    expect(screen.getByText("your entry")).toBeInTheDocument();
    expect(screen.getByText(/Not a forecast or a recommendation/)).toBeInTheDocument();
    expect(screen.getAllByRole("row").filter((r) => /\+3\.00%|−1|-1\.00%/.test(r.textContent ?? "")).length).toBeGreaterThan(0);
    const brk = screen.getByText(/Breakdowns at horizon/).closest("section")!;
    fireEvent.click(within(brk).getByText("below 200-bar average"));
    await waitFor(() => expect(screen.getByLabelText("Clear filter")).toBeInTheDocument());
    const table = screen.getByText("Matching bars").closest("section")!;
    expect(table.textContent).toContain("near");
    expect(table.textContent).not.toContain("your entry");
    expect(screen.queryByText(/\b(buy|sell)\b/i)).toBeNull();
  });
  it("a failed run shows its error", async () => {
    mockFetch([[/analysis\/runs\/r2$/, json({ ...RUN, id: "r2", status: "error", error: "RuntimeError: no bars", result: {}, matches: [] })]]);
    wrap(<AnalysisRunView id="r2" />);
    await waitFor(() => expect(screen.getByText(/The analysis failed: RuntimeError: no bars/)).toBeInTheDocument());
  });
});
