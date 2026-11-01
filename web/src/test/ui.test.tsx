import * as Tooltip from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Account, Analysis, EventItem, MonitorStatus, MonitorView, Position } from "../api/types";
import { AnalysisPanel } from "../components/AnalysisPanel";
import { BoundedValue, FreshnessBadge, StatusBadge, TermLabel, ToastProvider } from "../components/ui";
import { TermsContext } from "../lib/terms";
import { Dashboard } from "../views/Dashboard";
import { EventFeed, MonitorPanel } from "../views/MonitorWidgets";

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
  risk_acknowledged: true, realized_pnl: null, monitoring_enabled: true, monitor: null, nearest_expiration: "2026-12-18",
  days_to_expiration: 46, notes: null, source: "manual", created_at: "", updated_at: "", version: 1, legs: [],
};

type Route = [RegExp, () => Response | Promise<Response>];
function mockFetch(routes: Route[]) {
  const fn = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
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
  capital_at_risk_pct_of_buying_power: null, realized_pnl_total: "0.0000", upcoming_expirations: [], strategies: {},
  monitoring: { statuses: {}, attention: 0, open_pnl: null, open_pnl_positions: 0, open_pnl_complete: true, greeks: null,
                last_checked_at: null, label: "ESTIMATED" } };
const page = (items: unknown[]) => ({ items, total: items.length, page: 1, page_size: 200 });

const MON: MonitorView = {
  enabled: true, status: "HOLD", label: "HOLD", stored_status: "HOLD", reasons: ["no exit, action or watch rule holds (3 rules checked)"],
  since: "2026-11-02T14:30:00Z", checked_at: "2026-11-02T15:00:00Z", age_seconds: 20, stale_overlay: false, pending: null,
  raw_status: "HOLD", raw_reasons: [], snapshot: {
    id: "s1", taken_at: "2026-11-02T15:00:00Z", raw_status: "HOLD", status: "HOLD", valuation_status: "AVAILABLE",
    value: "-69.0000", exit_value: "-75.0000", open_pnl: "31.0000", open_pnl_pct: 31, underlying_price: "100.0000",
    greeks: { delta: 10, gamma: 0, theta: 1.5, vega: -2 }, freshness: "DELAYED", provider: "cboe (OpenBB)", delay_minutes: 15,
    fetched_at: "2026-11-02T15:00:00Z", quote_age_seconds: 20, label: "ESTIMATED" },
};

const EVENT: EventItem = {
  id: "e1", seq: 7, occurred_at: "2026-11-02T15:01:00Z", type: "POSITION_STATUS_CHANGED", severity: "critical",
  title: "HOLD → EXIT CONDITION MET: XYZ spread", message: "EXIT CONDITION MET: pnl_pct >= 30 (pnl_pct = +31.0%).",
  symbol: "XYZ", account_id: ACCOUNT.id, position_id: POSITION.id, contract_id: null, alert_rule_id: null, snapshot_id: "s1",
  requires_ack: true, acknowledged_at: null, ack_note: null, notify: true, notify_state: "sent", notify_detail: null,
  source: { rules: [{ kind: "exit", rule: "pnl_pct >= 30", result: true, values: { pnl_pct: 31 }, reason: null, error: null }] },
};

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
    expect(screen.getAllByRole("button", { name: "Try again" }).length).toBeGreaterThan(0);
  });
  it("lists positions with unlimited risk flagged", async () => {
    const unchecked = { ...POSITION, monitor: { ...MON, status: "STALE_DATA" as const, label: "STALE DATA", snapshot: null,
                        reasons: ["No monitoring pass has checked this position yet."] } };
    mockFetch([[/overview/, json(OVERVIEW)], [/\/positions/, json(page([unchecked]))], [/watched-contracts/, json(page([]))]]);
    wrap(<Dashboard account={ACCOUNT} />);
    await waitFor(() => expect(screen.getByText("XYZ short call")).toBeInTheDocument());
    expect(screen.getByText("Unlimited")).toBeInTheDocument();
    expect(screen.getByText(/undefined risk/)).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("Stale data");                  // never a silent Hold
    expect(screen.getByRole("table")).toHaveTextContent("No monitoring pass has checked this position yet.");
  });
});

describe("monitoring statuses", () => {
  it("shows every status with an icon and text, with the reasons for screen readers", () => {
    wrap(<>
      <StatusBadge m={MON} />
      <StatusBadge m={{ ...MON, status: "STALE_DATA", label: "STALE DATA", reasons: ["option quotes unavailable (timeout)"] }} />
      <StatusBadge m={{ ...MON, status: "EXIT_CONDITION_MET", label: "EXIT CONDITION MET", pending: null }} />
      <StatusBadge m={{ ...MON, status: "WATCH", label: "WATCH", pending: { status: "EXIT_CONDITION_MET", label: "EXIT CONDITION MET", passes: 1, needed: 3 } }} />
    </>);
    expect(screen.getByText("Hold")).toBeInTheDocument();
    expect(screen.getByText("Stale data")).toBeInTheDocument();
    expect(screen.getByLabelText(/Status Stale data\. option quotes unavailable/)).toBeInTheDocument();
    expect(screen.getByText("Exit condition met")).toBeInTheDocument();
    expect(screen.getByText(/→ Exit condition met\?/)).toBeInTheDocument();         // pending change is visible
  });
  it("an old snapshot shows as cached, with its P&L, in the positions table", async () => {
    const old = { ...POSITION, monitor: { ...MON, status: "STALE_DATA" as const, label: "STALE DATA", stale_overlay: true,
                  reasons: ["Last checked 3.0 h ago, so the data is old. The last status was HOLD: ..."] } };
    mockFetch([[/overview/, json(OVERVIEW)], [/\/positions/, json(page([old]))], [/watched-contracts/, json(page([]))]]);
    wrap(<Dashboard account={ACCOUNT} />);
    await waitFor(() => expect(screen.getByText("XYZ short call")).toBeInTheDocument());
    const table = screen.getAllByRole("table")[0];
    expect(table).toHaveTextContent("Stale data");
    expect(table).toHaveTextContent("Cached");
    expect(table).toHaveTextContent("+$31.00");
  });
});

describe("event feed", () => {
  it("is empty when nothing needs acknowledging", async () => {
    mockFetch([[/\/events/, json({ ...page([]), latest_seq: 0 })]]);
    wrap(<EventFeed accountId={ACCOUNT.id} />);
    await waitFor(() => expect(screen.getByText("Nothing needs acknowledging.")).toBeInTheDocument());
  });
  it("lists events with severity text and acknowledges one", async () => {
    const f = mockFetch([[/\/events\/e1\/acknowledge/, json({ ...EVENT, acknowledged_at: "2026-11-02T15:02:00Z" })],
                         [/\/events/, json({ ...page([EVENT]), latest_seq: 7 })]]);
    wrap(<EventFeed accountId={ACCOUNT.id} />);
    await waitFor(() => expect(screen.getByText(EVENT.title)).toBeInTheDocument());
    expect(screen.getByRole("list")).toHaveTextContent("Critical");
    expect(screen.getByText(/desktop notification sent/)).toBeInTheDocument();
    screen.getByRole("button", { name: "Acknowledge" }).click();
    await waitFor(() => expect(f.mock.calls.some(([u, o]) => String(u).includes("/events/e1/acknowledge") && o?.method === "POST")).toBe(true));
  });
  it("shows an error state", async () => {
    mockFetch([[/\/events/, json({ error: { code: "INTERNAL_ERROR", message: "Something went wrong on the server." } }, 500)]]);
    wrap(<EventFeed accountId={ACCOUNT.id} />);
    await waitFor(() => expect(screen.getByText(/Something went wrong on the server/)).toBeInTheDocument());
  });
});

describe("monitor panel", () => {
  const STATUS: MonitorStatus = {
    enabled: true, schedule: "09:30–16:00 America/New_York, mon/tue/wed/thu/fri, every 60 s", window_open: false,
    why_closed: "After the monitoring window (16:00).", next_window_open: "2026-11-03T14:30:00Z",
    runner: { state: "off", detail: "This API was started with OPTLAB_MONITOR=off.", next_pass_at: null, in_process: false, lock_holder_pid: null, running_since: null },
    last_run: null, last_successful_run_at: null, stale_after_seconds: 300, unacknowledged: { warning: 0, critical: 0 }, latest_seq: 0,
    quotes: { mode: "live", provider: "cboe (OpenBB)", delay_minutes: 15, synthetic: false, greeks_source: "provider" },
    note: "Outside the monitoring window: After the monitoring window (16:00).",
  };
  it("says plainly when the monitor isn't running and nothing has been checked", async () => {
    mockFetch([[/monitor\/status/, json(STATUS)]]);
    wrap(<MonitorPanel accountId={ACCOUNT.id} />);
    await waitFor(() => expect(screen.getByText("Not running here")).toBeInTheDocument());
    expect(screen.getByText(/No pass yet/)).toBeInTheDocument();
    expect(screen.getByText(/delayed ~15 min/)).toBeInTheDocument();
    expect(screen.getByText(/Outside the monitoring window/)).toBeInTheDocument();
  });
});
