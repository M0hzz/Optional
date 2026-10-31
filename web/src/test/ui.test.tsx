import * as Tooltip from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Account, Analysis, Position } from "../api/types";
import { AnalysisPanel } from "../components/AnalysisPanel";
import { BoundedValue, FreshnessBadge, TermLabel, ToastProvider } from "../components/ui";
import { TermsContext } from "../lib/terms";
import { Dashboard } from "../views/Dashboard";

function wrap(ui: ReactNode, mode: "professional" | "colony" = "professional") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Tooltip.Provider><ToastProvider><TermsContext.Provider value={mode}>{ui}</TermsContext.Provider></ToastProvider></Tooltip.Provider>
    </QueryClientProvider>,
  );
}

const ACCOUNT: Account = {
  id: "01a19000-0000-7000-8000-000000000001", name: "Main", broker: null, currency: "USD", is_default: true, notes: null,
  archived_at: null, created_at: "2026-11-02T14:00:00Z", updated_at: "2026-11-02T14:00:00Z", version: 1,
  latest_snapshot: null,
};

const POSITION: Position = {
  id: "01a19000-0000-7000-8000-000000000002", account_id: ACCOUNT.id, name: "XYZ short call", underlying: "XYZ",
  strategy: "SHORT_CALL", strategy_name: "Short call (uncovered)", strategy_variant: null, strategy_exact: true,
  direction: "bearish", lifecycle: "OPEN", opened_at: "2026-11-02T14:00:00Z", closed_at: null, archived_at: null,
  archive_reason: null, net_premium: "-250.0000", premium_type: "CREDIT", fees: "0.0000",
  max_loss: { state: "unbounded", value: null }, max_gain: { state: "bounded", value: "250.0000" }, defined_risk: false,
  risk_acknowledged: true, realized_pnl: null, monitoring_enabled: false, nearest_expiration: "2026-12-18",
  days_to_expiration: 46, notes: null, source: "manual", created_at: "", updated_at: "", version: 1, legs: [],
};

type Route = [RegExp, () => Response | Promise<Response>];
function mockFetch(routes: Route[]) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    for (const [re, res] of routes) if (re.test(url)) return res();
    return new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "no mock for " + url } }), { status: 404 });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}
const json = (b: unknown, status = 200) => () => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
const OVERVIEW = { account_id: ACCOUNT.id, account_name: "Main", snapshot: null, open_positions: 0, defined_risk_positions: 0,
  unbounded_risk_positions: 0, unknown_risk_positions: 0, capital_at_risk: "0.0000", capital_at_risk_complete: true,
  capital_at_risk_pct_of_buying_power: null, realized_pnl_total: "0.0000", upcoming_expirations: [], strategies: {} };
const page = (items: unknown[]) => ({ items, total: items.length, page: 1, page_size: 200 });

afterEach(() => vi.unstubAllGlobals());

describe("risk and freshness are never shown as zero or as fresh", () => {
  it("shows unlimited and unknown bounds as words, not $0", () => {
    wrap(<><BoundedValue b={{ state: "unbounded", value: null }} kind="loss" /><BoundedValue b={{ state: "unknown", value: null }} kind="loss" /></>);
    expect(screen.getByText("Unlimited")).toBeInTheDocument();
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).toBeNull();
  });
  it("labels delayed and unavailable data with text, not only colour", () => {
    wrap(<><FreshnessBadge freshness="DELAYED" provider="cboe (OpenBB)" delayMinutes={15} /><FreshnessBadge freshness="UNAVAILABLE" /></>);
    expect(screen.getByText("Delayed")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByLabelText(/Data Delayed/)).toBeInTheDocument();
  });
  it("analysis with a missing fill price shows unavailable values", () => {
    const a: Analysis = {
      strategy: "LONG_CALL", strategy_name: "Long call", exact: true, confidence: "exact", variant: null, direction: "bullish",
      units: 1, structure: [], net_premium: null, premium_type: "UNKNOWN", fees: "0.0000", cost_basis: null,
      max_gain: { state: "unknown", value: null }, max_loss: { state: "unknown", value: null }, breakevens: null,
      breakeven_state: "unknown", defined_risk: false, buying_power_effect: { state: "unknown", value: null },
      buying_power_basis: "fill prices missing", expirations: ["2026-12-18"], errors: [],
      warnings: [{ code: "MISSING_FILL_PRICE", message: "No fill price", leg: 0 }], assumptions: [],
      requires_risk_acknowledgement: true, payoff: [], quotes: null,
    };
    wrap(<AnalysisPanel a={a} />);
    expect(screen.getAllByText("Unknown").length).toBeGreaterThanOrEqual(3);
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("No fill price")).toBeInTheDocument();
  });
});

describe("terminology", () => {
  it("keeps the professional term next to the colony label", () => {
    wrap(<TermLabel k="buyingPower" />, "colony");
    expect(screen.getByText("Treasury")).toBeInTheDocument();
    expect(screen.getByText(/Buying power/)).toBeInTheDocument();
  });
  it("professional mode shows the plain financial term", () => {
    wrap(<TermLabel k="buyingPower" />);
    expect(screen.getByText("Buying power")).toBeInTheDocument();
    expect(screen.queryByText("Treasury")).toBeNull();
  });
});

describe("dashboard states", () => {
  it("shows loading, then empty states", async () => {
    mockFetch([[/overview/, json(OVERVIEW)], [/\/positions/, json(page([]))], [/watched-contracts/, json(page([]))]]);
    wrap(<Dashboard account={ACCOUNT} />);
    expect(screen.getAllByRole("status").length).toBeGreaterThan(0);              // loading spinners first
    await waitFor(() => expect(screen.getByText(/No open positions yet/)).toBeInTheDocument());
    expect(screen.getByText(/No watched contracts/)).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThanOrEqual(3);   // account values not entered
  });
  it("shows an API error with a retry button", async () => {
    mockFetch([[/overview/, json(OVERVIEW)], [/\/positions/, json({ error: { code: "INTERNAL_ERROR", message: "Something went wrong on the server." } }, 500)],
               [/watched-contracts/, json(page([]))]]);
    wrap(<Dashboard account={ACCOUNT} />);
    await waitFor(() => expect(screen.getByText(/Something went wrong on the server/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
  it("lists positions with unlimited risk flagged", async () => {
    mockFetch([[/overview/, json(OVERVIEW)], [/\/positions/, json(page([POSITION]))], [/watched-contracts/, json(page([]))]]);
    wrap(<Dashboard account={ACCOUNT} />);
    await waitFor(() => expect(screen.getByText("XYZ short call")).toBeInTheDocument());
    expect(screen.getByText("Unlimited")).toBeInTheDocument();
    expect(screen.getByText(/undefined risk/)).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("Open");
  });
});
