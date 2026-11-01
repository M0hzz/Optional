// Thin typed client for /api/v1. Errors arrive as {error: {code, message, field_errors, details}}.
import type {
  Account, AlertRule, Analysis, AuditEntry, Contract, ContractMonitor, ContractQuote, DataSources, EventItem, EventPage,
  ImportReport, JournalEntryT, LegIn, MissionReport, MonitorRunInfo, PendingReview, Progress, Review, DayScore, Plan, PolicyPayload, PolicySettings, RiskDashboard, RiskReport, MonitorSettings, MonitorStatus, Overview, Page, Position, PositionMonitor,
  AnalysisDraft, AnalysisRun, CalendarItem, LedgerEntry, MatchWindow, NewsArticle, NewsFeatures, NewsPage, NewsSources, PointsSummary, Preferences, RuleSet, ScoreHistoryDay, Versions, Snapshot, SnapshotRow, TimelineItem, Valuation, Vocabulary, YamlRow,
} from "./types";

export interface FieldError { field: string; code: string; message: string }

export class ApiError extends Error {
  status: number; code: string; fieldErrors: FieldError[]; details: Record<string, unknown>;
  constructor(status: number, code: string, message: string, fieldErrors: FieldError[] = [],
              details: Record<string, unknown> = {}) {
    super(message);
    this.status = status; this.code = code; this.fieldErrors = fieldErrors; this.details = details;
  }
}

const BASE = "/api/v1";

async function request<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      method,
      headers: body instanceof FormData ? headers : { "Content-Type": "application/json", ...headers },
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK", "Can't reach the optlab API. Is it running?");
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!res.ok) {
    const e = (data as { error?: { code: string; message: string; field_errors: FieldError[]; details: Record<string, unknown> } })?.error;
    throw new ApiError(res.status, e?.code ?? "HTTP_" + res.status, e?.message ?? res.statusText, e?.field_errors ?? [],
                       e?.details ?? {});
  }
  return data as T;
}

export const q = (params: Record<string, string | number | boolean | string[] | undefined | null>) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x)); else u.append(k, String(v));
  }
  const s = u.toString();
  return s ? "?" + s : "";
};

export const api = {
  accounts: () => request<Account[]>("GET", "/accounts"),
  createAccount: (b: { name: string; broker?: string | null }) => request<Account>("POST", "/accounts", b),
  overview: (id: string) => request<Overview>("GET", `/accounts/${id}/overview`),
  addSnapshot: (id: string, b: { total_value?: string | null; cash?: string | null; buying_power?: string | null; notes?: string | null }) =>
    request<Snapshot>("POST", `/accounts/${id}/snapshots`, b),
  positions: (p: { account_id?: string; lifecycle?: string[]; underlying?: string; strategy?: string; page?: number; page_size?: number; sort?: string }) =>
    request<Page<Position>>("GET", "/positions" + q(p)),
  position: (id: string) => request<Position>("GET", `/positions/${id}`),
  analyze: (b: { underlying: string; legs: LegIn[]; account_id?: string | null; cash_secured?: boolean; include_quotes?: boolean }) =>
    request<Analysis>("POST", "/positions/analyze", b),
  positionAnalysis: (id: string) => request<Analysis>("GET", `/positions/${id}/analysis`),
  createPosition: (b: { account_id: string; underlying: string; name?: string | null; opened_at?: string | null; legs: LegIn[]; cash_secured: boolean; acknowledge_risk: boolean; acknowledge_rule_violation?: boolean; notes?: string | null }, idempotencyKey: string) =>
    request<Position>("POST", "/positions", b, { "Idempotency-Key": idempotencyKey }),
  updatePosition: (id: string, b: Record<string, unknown>) => request<Position>("PATCH", `/positions/${id}`, b),
  closePosition: (id: string, b: { version: number; closed_at?: string | null; legs: { leg_id: string; close_price?: string | null; close_fees?: string; expired_worthless?: boolean }[]; notes?: string | null }) =>
    request<Position>("POST", `/positions/${id}/close`, b),
  archivePosition: (id: string, b: { version: number; reason: "closed" | "entered_in_error" }) =>
    request<Position>("POST", `/positions/${id}/archive`, b),
  valuation: (id: string) => request<Valuation>("GET", `/positions/${id}/valuation`),
  audit: (id: string) => request<AuditEntry[]>("GET", `/positions/${id}/audit`),
  contracts: (p: { account_id?: string; status?: string[]; include_archived?: boolean; page_size?: number }) =>
    request<Page<Contract>>("GET", "/watched-contracts" + q(p)),
  createContract: (b: Record<string, unknown>) => request<Contract>("POST", "/watched-contracts", b),
  updateContract: (id: string, b: Record<string, unknown>) => request<Contract>("PATCH", `/watched-contracts/${id}`, b),
  archiveContract: (id: string, version: number) => request<Contract>("POST", `/watched-contracts/${id}/archive`, { version }),
  contractQuote: (id: string) => request<ContractQuote>("GET", `/watched-contracts/${id}/quote`),
  preferences: () => request<Preferences>("GET", "/preferences"),
  setPreferences: (b: Partial<Preferences>) => request<Preferences>("PUT", "/preferences", b),
  dataSources: () => request<DataSources>("GET", "/data-sources"),
  importPreview: (accountId: string, file: File) => {
    const f = new FormData(); f.append("file", file);
    return request<ImportReport>("POST", `/imports/positions/preview${q({ account_id: accountId })}`, f);
  },
  // ---------------------------------------------------------------- process (Phase 4)
  progress: (accountId: string) => request<Progress>("GET", `/accounts/${accountId}/progress`),
  scoreHistory: (accountId: string, days = 30) => request<ScoreHistoryDay[]>("GET", `/accounts/${accountId}/process-score/history` + q({ days })),
  scoreRevisions: (accountId: string, day: string) => request<DayScore[]>("GET", `/accounts/${accountId}/scores/${day}`),
  ledger: (accountId: string, p: { subject_id?: string; entry_type?: string; limit?: number } = {}) =>
    request<{ summary: PointsSummary; versions: Versions; entries: LedgerEntry[] }>("GET", `/accounts/${accountId}/ledger` + q(p)),
  revokeEntry: (accountId: string, entryId: string, reason: string) => request<LedgerEntry>("POST", `/accounts/${accountId}/ledger/${entryId}/revoke`, { reason }),
  correctEntry: (accountId: string, entryId: string, reason: string, points: number) =>
    request<LedgerEntry>("POST", `/accounts/${accountId}/ledger/${entryId}/correct`, { reason, points }),
  setObjective: (accountId: string, code: string, b: { day?: string; done: boolean; note?: string | null }) =>
    request<DayScore>("POST", `/accounts/${accountId}/objectives/${code}`, b),
  journal: (p: { account_id: string; kind?: string[]; position_id?: string; plan_id?: string; include_archived?: boolean; limit?: number }) => request<JournalEntryT[]>("GET", "/journal" + q(p)),
  createJournal: (b: Record<string, unknown>) => request<JournalEntryT>("POST", "/journal", b),
  updateJournal: (id: string, b: Record<string, unknown>) => request<JournalEntryT>("PATCH", `/journal/${id}`, b),
  archiveJournal: (id: string, version: number) => request<JournalEntryT>("POST", `/journal/${id}/archive`, { version }),
  restoreJournal: (id: string, version: number) => request<JournalEntryT>("POST", `/journal/${id}/restore`, { version }),
  pendingReviews: (accountId: string) => request<PendingReview[]>("GET", `/accounts/${accountId}/reviews/pending`),
  reviews: (accountId: string) => request<(Review & { position: { id: string; name: string; underlying: string; strategy_name: string; closed_at: string | null; plan_id: string | null; rule_violation: boolean } })[]>("GET", `/accounts/${accountId}/reviews`),
  saveReview: (positionId: string, b: Record<string, unknown>) => request<Review>("PUT", `/positions/${positionId}/review`, b),
  report: (positionId: string) => request<MissionReport>("GET", `/positions/${positionId}/report`),
  // ---------------------------------------------------------------- plans and risk (Phase 3)
  plans: (p: { account_id?: string; state?: string[]; symbol?: string; page_size?: number }) => request<Page<Plan>>("GET", "/plans" + q(p)),
  plan: (id: string) => request<Plan>("GET", `/plans/${id}`),
  createPlan: (b: Record<string, unknown>) => request<Plan>("POST", "/plans", b),
  updatePlan: (id: string, b: Record<string, unknown>) => request<Plan>("PATCH", `/plans/${id}`, b),
  transitionPlan: (id: string, b: { version: number; to: string; reason?: string | null }) => request<Plan>("POST", `/plans/${id}/transition`, b),
  assessPlan: (id: string) => request<Plan>("POST", `/plans/${id}/assess`),
  duplicatePlan: (id: string) => request<Plan>("POST", `/plans/${id}/duplicate`),
  convertPlan: (id: string, b: Record<string, unknown>) => request<{ plan: Plan; position: Position; risk: RiskReport }>("POST", `/plans/${id}/convert`, b),
  planTimeline: (id: string) => request<TimelineItem[]>("GET", `/plans/${id}/timeline`),
  riskPolicy: (accountId: string) => request<PolicyPayload>("GET", `/accounts/${accountId}/risk-policy`),
  saveRiskPolicy: (accountId: string, settings: PolicySettings, notes?: string | null) =>
    request<PolicyPayload>("PUT", `/accounts/${accountId}/risk-policy`, { settings, notes: notes ?? null }),
  riskPolicyHistory: (accountId: string) => request<{ id: string; revision: number; created_at: string; superseded_at: string | null; notes: string | null; settings: PolicySettings }[]>("GET", `/accounts/${accountId}/risk-policy/history`),
  riskDashboard: (accountId: string) => request<RiskDashboard>("GET", `/accounts/${accountId}/risk`),
  evaluateRisk: (b: { account_id: string; underlying: string; legs: LegIn[]; cash_secured?: boolean; plan_id?: string | null }) =>
    request<RiskReport>("POST", "/risk/evaluate", b),
  // ---------------------------------------------------------------- monitoring (Phase 2)
  monitorStatus: () => request<MonitorStatus>("GET", "/monitor/status"),
  runMonitor: () => request<{ run_id: string; ok: boolean; seconds: number; positions: number; contracts: number; rules: number; events: number; notified: number; errors: string[] }>("POST", "/monitor/run"),
  monitorSettings: () => request<MonitorSettings>("GET", "/monitor/settings"),
  saveMonitorSettings: (b: MonitorSettings) => request<MonitorSettings>("PUT", "/monitor/settings", b),
  monitorRuns: (limit = 20) => request<MonitorRunInfo[]>("GET", "/monitor/runs" + q({ limit })),
  vocabulary: () => request<Vocabulary>("GET", "/monitor/vocabulary"),
  validateCondition: (condition: string, target: "position" | "contract" | "symbol") =>
    request<{ valid: boolean; error: string | null }>("POST", "/monitor/validate", { condition, target }),
  events: (p: { account_id?: string; position_id?: string; contract_id?: string; symbol?: string; type?: string[];
                severity?: string[]; unacknowledged?: boolean; page?: number; page_size?: number }) =>
    request<EventPage>("GET", "/events" + q(p)),
  ackEvent: (id: string, note?: string | null) => request<EventItem>("POST", `/events/${id}/acknowledge`, { note: note ?? null }),
  ackEvents: (b: { ids?: string[]; up_to_seq?: number; position_id?: string }) =>
    request<{ acknowledged: number }>("POST", "/events/acknowledge", b),
  alertRules: (p: { position_id?: string; contract_id?: string; account_id?: string }) =>
    request<AlertRule[]>("GET", "/alert-rules" + q(p)),
  createAlertRule: (b: Record<string, unknown>) => request<AlertRule>("POST", "/alert-rules", b),
  updateAlertRule: (id: string, b: Record<string, unknown>) => request<AlertRule>("PATCH", `/alert-rules/${id}`, b),
  archiveAlertRule: (id: string, version: number) => request<AlertRule>("POST", `/alert-rules/${id}/archive`, { version }),
  positionMonitor: (id: string) => request<PositionMonitor>("GET", `/positions/${id}/monitor`),
  setPositionMonitor: (id: string, b: { version: number; monitoring_enabled?: boolean; use_defaults?: boolean; rules?: RuleSet; timeframe?: string; notify?: boolean }) =>
    request<PositionMonitor>("PUT", `/positions/${id}/monitor`, b),
  snapshots: (id: string, limit = 2000) => request<SnapshotRow[]>("GET", `/positions/${id}/snapshots` + q({ limit })),
  timeline: (id: string) => request<TimelineItem[]>("GET", `/positions/${id}/timeline`),
  contractMonitor: (id: string) => request<ContractMonitor>("GET", `/watched-contracts/${id}/monitor`),
  // ---------------------------------------------------------------- news (Phase 5)
  news: (p: { symbol?: string[]; category?: string[]; source?: string[]; held?: boolean; q?: string; include_duplicates?: boolean; limit?: number; offset?: number }) =>
    request<NewsPage>("GET", "/news" + q(p)),
  newsArticle: (id: string) => request<NewsArticle>("GET", `/news/articles/${id}`),
  newsFeatures: (symbol: string) => request<NewsFeatures>("GET", `/news/features/${encodeURIComponent(symbol)}`),
  newsCalendar: (days = 14, importance?: string[]) => request<CalendarItem[]>("GET", "/news/calendar" + q({ days, importance })),
  newsSources: () => request<NewsSources>("GET", "/news/sources"),
  setNewsSource: (code: string, enabled: boolean) => request<{ code: string; enabled: boolean; status: string }>("PATCH", `/news/sources/${code}`, { enabled }),
  refreshNews: () => request<Record<string, unknown>>("POST", "/news/refresh"),
  // ---------------------------------------------------------------- historical analysis (Phase 6)
  analysisDraft: (p: { subject_type: string; subject_id?: string; symbol?: string }) => request<AnalysisDraft>("GET", "/analysis/draft" + q(p)),
  createAnalysis: (b: Record<string, unknown>) => request<{ id: string; status: string; strategy_version: string; source: string }>("POST", "/analysis/runs", b),
  startAnalysis: (id: string) => request<{ id: string; status: string }>("POST", `/analysis/runs/${id}/start`),
  analysisRuns: (p: { subject_type?: string; subject_id?: string; limit?: number } = {}) => request<AnalysisRun[]>("GET", "/analysis/runs" + q(p)),
  analysisRun: (id: string) => request<AnalysisRun>("GET", `/analysis/runs/${id}`),
  matchWindow: (id: string, n: number) => request<MatchWindow>("GET", `/analysis/runs/${id}/matches/${n}/bars`),
  yamlPreview: (accountId: string) => request<{ path: string; rows: YamlRow[] }>("POST", "/imports/scans-yaml/preview" + q({ account_id: accountId })),
  yamlCommit: (accountId: string, acknowledgeRisk: boolean, key: string) =>
    request<{ imported: { id: string; name: string }[]; rows: YamlRow[] }>(
      "POST", "/imports/scans-yaml/commit" + q({ account_id: accountId }), { acknowledge_risk: acknowledgeRisk }, { "Idempotency-Key": key }),
  importCommit: (accountId: string, file: File, mode: "all_or_nothing" | "valid_only", key: string) => {
    const f = new FormData(); f.append("file", file);
    return request<{ imported: Position[]; report: ImportReport }>(
      "POST", `/imports/positions/commit${q({ account_id: accountId, mode })}`, f, { "Idempotency-Key": key });
  },
};
