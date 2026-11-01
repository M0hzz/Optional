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
import { RiskPanel } from "../components/RiskReport";
import { PlanDetail, PlansView } from "../views/Plans";
import { DisciplinePanel, ProgressPanel } from "../views/Process";
import type { Progress } from "../api/types";
import type { Plan, RiskReport } from "../api/types";

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
  risk_acknowledged: true, realized_pnl: null, monitoring_enabled: true, monitor: null, plan_id: null, entry_risk: null, rule_violation: false, nearest_expiration: "2026-12-18",
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
                last_checked_at: null, label: "ESTIMATED" },
  risk: { overall: "PASS" as const, summary: "The account is within its limits.", checks: [], realized_today: "0.0000",
          realized_week: "0.0000", small_account_active: false, small_account_reason: "Small-account mode is off.", policy_revision: null } };
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

const BLOCKED: RiskReport = {
  overall: "BLOCKED", policy_revision: 2, small_account_active: true, small_account_reason: "Small-account mode is on: account value $800.00 is below $1,000.00.",
  summary: "Breaks your risk policy.", checks: [
    { code: "MAX_LOSS", label: "Maximum loss per position", result: "BLOCKED", reason: "Maximum loss $410.00 is over the limit of $100.00.", value: "$410.00", limit: "$100.00", source: "small account" },
    { code: "OPEN_INTEREST", label: "Open interest", result: "UNKNOWN", reason: "Can't check the open interest: no quotes for the legs yet.", value: null, limit: "100", source: "small account" },
    { code: "DEFINED_RISK", label: "Defined risk", result: "PASS", reason: "Maximum loss is known: $410.00.", value: "$410.00", limit: null, source: "policy" }],
};

const PLAN: Plan = {
  id: "01a19000-0000-7000-8000-0000000000aa", account_id: ACCOUNT.id, symbol: "XYZ", title: "XYZ bull put", state: "DRAFT",
  state_changed_at: "2026-11-02T14:00:00Z", fields: {
    thesis: "Support at 95", direction: "bullish", intended_strategy: "PUT_CREDIT_SPREAD", market_regime: null, setup_type: null,
    trigger: "Bounce", trigger_conditions: "price > 99", confirmation: null, confirmation_conditions: null, entry_zone_low: null,
    entry_zone_high: null, max_entry_debit: null, min_entry_credit: "0.9000", max_spread_pct: null, min_volume: null, min_open_interest: null,
    target: null, target_price: null, invalidation: null, invalidation_conditions: null, invalidation_price: "92.0000",
    planned_max_loss: "450.0000", planned_contracts: 1, expiration_from: null, expiration_to: null, delta_min: null, delta_max: null,
    time_stop: null, event_risks: null, related_news: null, chart_reference: "javascript:alert(1)", notes: null, expires_at: null },
  legs: [], expires_at: null, outcome_reason: null, risk: BLOCKED,
  validation: { ready: false, errors: [{ item: "Planned size", ok: false, level: "error", detail: "How many contracts." }], warnings: [],
                checklist: [{ item: "Thesis", ok: true, level: "error", detail: "" }, { item: "Planned size", ok: false, level: "error", detail: "How many contracts." }] },
  position_id: null, converted_at: null, comparison: null, allowed_transitions: ["ARCHIVED", "ARMED", "SKIPPED"], monitor: null,
  archived_at: null, created_at: "2026-11-02T14:00:00Z", updated_at: "2026-11-02T14:00:00Z", version: 1,
};

describe("risk checks", () => {
  it("show the overall result and each finding with its reason, blocked first, in words", () => {
    wrap(<RiskPanel report={BLOCKED} />);
    expect(screen.getAllByText("Blocked").length).toBeGreaterThan(0);
    expect(screen.getByText("Maximum loss $410.00 is over the limit of $100.00.")).toBeInTheDocument();
    expect(screen.getByText(/no quotes for the legs yet/)).toBeInTheDocument();
    expect(screen.getByText(/below \$1,000.00/)).toBeInTheDocument();
    const rows = screen.getAllByRole("row").map((r) => r.textContent ?? "");
    expect(rows[1]).toContain("Maximum loss per position");          // BLOCKED sorts before UNKNOWN and PASS
  });
});

describe("plans", () => {
  it("lists plans with their state and risk", async () => {
    mockFetch([[/\/plans/, json({ ...page([{ ...PLAN, state: "ARMED", validation: null }]) })]]);
    wrap(<PlansView account={ACCOUNT} />);
    await waitFor(() => expect(screen.getByText("XYZ bull put")).toBeInTheDocument());
    expect(screen.getByRole("table")).toHaveTextContent("Armed");
    expect(screen.getByRole("table")).toHaveTextContent("Blocked");
  });
  it("shows the empty state", async () => {
    mockFetch([[/\/plans/, json(page([]))]]);
    wrap(<PlansView account={ACCOUNT} />);
    await waitFor(() => expect(screen.getByText(/No active plans/)).toBeInTheDocument());
  });
  it("a draft shows its checklist and only the allowed actions, and never renders a script link", async () => {
    mockFetch([[/\/plans\/[^/]+\/timeline/, json([])], [/\/plans\//, json(PLAN)]]);
    wrap(<PlanDetail id={PLAN.id} />);
    await waitFor(() => expect(screen.getByText("Checklist")).toBeInTheDocument());
    expect(screen.getByText(/How many contracts/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Arm" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Convert/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Chart/ })).toBeNull();          // javascript: is shown as text, not a link
  });
});

const PROGRESS: Progress = {
  points: { total: 65, by_category: { discipline: 30, research: 5, risk_control: 20, review: 10 } },
  level: { level: 2, colony_name: "Outpost", points_for_level: 50, points_for_next: 150, progress: 0.15 },
  streak: { current: 3, best: 5, threshold: 80 },
  achievements: [{ code: "FIRST_PLAN", name: "First mission briefed", description: "Arm your first plan.", goal: 1, progress: 1, earned_at: "2026-11-02T15:00:00Z" },
                 { code: "REVIEWS_TEN", name: "Ten mission reports", description: "Complete ten reviews.", goal: 10, progress: 3, earned_at: null }],
  recent_awards: [{ code: "DAILY_THESIS", category: "research", points: 5, description: "Wrote the daily market thesis: 2026-11-02", day: "2026-11-02", awarded_at: "2026-11-02T15:00:00Z" }],
  themes: [{ id: "default", unlock_level: 1, unlocked: true }, { id: "aurora", unlock_level: 3, unlocked: false }],
  new: { awards: 0, achievements: [] },
  today: { day: "2026-11-02", rest_day: false, score: 100, met: 5, applicable: 5, in_progress: true, items: [
    { code: "REVIEW_POSITIONS", label: "Review open positions", group: "objective", status: "na", detail: "no open positions", kind: "manual", subject_id: null },
    { code: "CHECK_DATA", label: "Confirm data-source status", group: "objective", status: "met", detail: "", kind: "manual", subject_id: null },
    { code: "DAILY_THESIS", label: "Write a daily market thesis", group: "objective", status: "met", detail: "written", kind: "auto", subject_id: null },
    { code: "INVALIDATION_BEFORE_ENTRY", label: "Record invalidation before entry", group: "objective", status: "na", detail: "no positions opened", kind: "auto", subject_id: null },
  ] },
};

describe("process and progression", () => {
  it("a day without trades shows 100% with the trade items not applicable, and offers a rest day", async () => {
    mockFetch([[/progress/, json(PROGRESS)]]);
    wrap(<DisciplinePanel account={ACCOUNT} />);
    await waitFor(() => expect(screen.getByText("100%")).toBeInTheDocument());
    expect(screen.getByText(/no positions opened/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Confirm data-source status/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Rest day" })).toBeInTheDocument();
    expect(screen.getByText(/Profit, size and the number of trades never count/)).toBeInTheDocument();
  });
  it("progress shows level, streak, points by kind and achievements with their progress", async () => {
    mockFetch([[/progress/, json(PROGRESS)], [/process-score\/history/, json([])]]);
    wrap(<ProgressPanel account={ACCOUNT} />, "colony");
    await waitFor(() => expect(screen.getByText("Outpost")).toBeInTheDocument());
    expect(screen.getByRole("progressbar", { name: /next level/ })).toHaveAttribute("aria-valuenow", "15");
    expect(screen.getByText("Ten mission reports")).toBeInTheDocument();
    expect(screen.getByText("3/10")).toBeInTheDocument();
    expect(screen.queryByText(/profit/i)).toBeNull();
  });
  it("the dashboard leaves out hidden widgets", async () => {
    mockFetch([[/preferences/, json({ terminology: "professional", timezone: "America/New_York", active_account_id: null, theme: "default",
                                      dashboard_layout: ["positions", "overview"], hidden_widgets: ["discipline", "progress", "monitor", "events", "contracts"] })],
               [/overview/, json(OVERVIEW)], [/\/positions/, json(page([]))]]);
    wrap(<Dashboard account={ACCOUNT} />);
    await waitFor(() => expect(screen.getByText(/No open positions yet/)).toBeInTheDocument());
    expect(screen.queryByText("Daily discipline")).toBeNull();
    expect(screen.queryByText("Watched contracts")).toBeNull();
  });
});
