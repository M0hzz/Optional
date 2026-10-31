// Thin typed client for /api/v1. Errors arrive as {error: {code, message, field_errors, details}}.
import type {
  Account, Analysis, AuditEntry, Contract, ContractQuote, DataSources, ImportReport, LegIn, Overview, Page,
  Position, Preferences, Snapshot, Valuation,
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

const q = (params: Record<string, string | number | boolean | string[] | undefined | null>) => {
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
  createPosition: (b: { account_id: string; underlying: string; name?: string | null; opened_at?: string | null; legs: LegIn[]; cash_secured: boolean; acknowledge_risk: boolean; notes?: string | null }, idempotencyKey: string) =>
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
  importCommit: (accountId: string, file: File, mode: "all_or_nothing" | "valid_only", key: string) => {
    const f = new FormData(); f.append("file", file);
    return request<{ imported: Position[]; report: ImportReport }>(
      "POST", `/imports/positions/commit${q({ account_id: accountId, mode })}`, f, { "Idempotency-Key": key });
  },
};
