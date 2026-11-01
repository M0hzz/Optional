// Mirrors src/optlab/portfolio/schemas.py. Money is a decimal string with 4 places ("1.2500").

export type Money = string;
export type BoundState = "bounded" | "unbounded" | "unknown";
export interface Bounded { state: BoundState; value: Money | null }
export type Lifecycle = "OPEN" | "CLOSED" | "ARCHIVED";
export type Freshness = "LIVE" | "DELAYED" | "CACHED" | "SYNTHETIC" | "UNAVAILABLE" | "ESTIMATED";

export interface Snapshot {
  id: string; as_of: string; total_value: Money | null; cash: Money | null; buying_power: Money | null;
  source: string; notes: string | null;
}

export interface Account {
  id: string; name: string; broker: string | null; currency: string; is_default: boolean; notes: string | null;
  archived_at: string | null; created_at: string; updated_at: string; version: number;
  latest_snapshot: Snapshot | null;
}

export interface Overview {
  account_id: string; account_name: string; snapshot: Snapshot | null; open_positions: number;
  defined_risk_positions: number; unbounded_risk_positions: number; unknown_risk_positions: number;
  capital_at_risk: Money; capital_at_risk_complete: boolean; capital_at_risk_pct_of_buying_power: number | null;
  realized_pnl_total: Money; upcoming_expirations: { expiration: string; days: number; positions: string[] }[];
  strategies: Record<string, number>;
  monitoring: { statuses: Record<string, number>; attention: number; open_pnl: Money | null; open_pnl_positions: number;
                open_pnl_complete: boolean; greeks: Record<string, number> | null; last_checked_at: string | null;
                label: string };
}

export interface LegIn {
  asset_type: "STOCK" | "OPTION"; side: "BUY" | "SELL"; quantity: number;
  option_right?: "CALL" | "PUT" | null; strike?: Money | null; expiration?: string | null; multiplier?: number | null;
  fill_price?: Money | null; fees?: Money; filled_at?: string | null; broker_order_id?: string | null;
  notes?: string | null;
}

export interface Leg extends Required<Omit<LegIn, "multiplier">> {
  id: string; leg_index: number; multiplier: number; occ_symbol: string | null; close_price: Money | null;
  close_fees: Money | null; closed_at: string | null;
}

export interface Position {
  id: string; account_id: string; name: string; underlying: string; strategy: string; strategy_name: string;
  strategy_variant: string | null; strategy_exact: boolean; direction: string; lifecycle: Lifecycle;
  opened_at: string; closed_at: string | null; archived_at: string | null; archive_reason: string | null;
  net_premium: Money | null; premium_type: "DEBIT" | "CREDIT" | "EVEN" | "UNKNOWN"; fees: Money;
  max_loss: Bounded; max_gain: Bounded; defined_risk: boolean; risk_acknowledged: boolean;
  realized_pnl: Money | null; monitoring_enabled: boolean; nearest_expiration: string | null;
  days_to_expiration: number | null; notes: string | null; source: string; created_at: string; updated_at: string;
  version: number; legs: Leg[]; monitor: MonitorView | null;
}

// ------------------------------------------------------------------ monitoring (Phase 2)
export type MonitorStatusValue = "STALE_DATA" | "EXIT_CONDITION_MET" | "ACTION_REQUIRED" | "WATCH" | "HOLD" | "CLOSED" | "NOT_MONITORED";

export interface SnapshotSummary {
  id: string; taken_at: string; raw_status: string; status: string; valuation_status: string; value: Money | null;
  exit_value: Money | null; open_pnl: Money | null; open_pnl_pct: number | null; underlying_price: Money | null;
  greeks: { delta: number | null; gamma: number | null; theta: number | null; vega: number | null };
  freshness: Freshness; provider: string; delay_minutes: number; fetched_at: string | null;
  quote_age_seconds: number | null; label: string;
}

export interface RuleResult {
  kind: "exit" | "action" | "watch" | "system" | "alert" | "condition"; rule: string; result: boolean | null;
  values: Record<string, number | boolean | string | null>; reason: string | null; error: string | null;
}

export interface MonitorView {
  enabled: boolean; status: MonitorStatusValue; label: string; stored_status: string | null; reasons: string[];
  since: string | null; checked_at: string | null; age_seconds: number | null; stale_overlay: boolean;
  pending: { status: string; label: string; passes: number; needed: number } | null;
  raw_status: string | null; raw_reasons: string[]; snapshot: SnapshotSummary | null;
  rules?: RuleResult[]; metrics?: Record<string, number | boolean | string | null>;
}

export interface RuleSet { exit_when: string[]; action_when: string[]; watch_when: string[] }

export interface PositionMonitor extends MonitorView {
  rules_config: { use_defaults: boolean; rules: RuleSet; timeframe: string; notify: boolean; defaults: RuleSet };
  confirm_passes: number; version: number;
}

export type Severity = "info" | "warning" | "critical";

export interface EventItem {
  id: string; seq: number; occurred_at: string; type: string; severity: Severity; title: string; message: string;
  symbol: string | null; account_id: string | null; position_id: string | null; contract_id: string | null;
  alert_rule_id: string | null; snapshot_id: string | null; requires_ack: boolean; acknowledged_at: string | null;
  ack_note: string | null; notify: boolean; notify_state: string; notify_detail: string | null;
  source: Record<string, unknown>;
}
export interface EventPage extends Page<EventItem> { latest_seq: number }

export interface AlertRule {
  id: string; name: string; target_type: "position" | "contract" | "symbol"; position_id: string | null;
  contract_id: string | null; symbol: string | null; account_id: string | null; condition: string; timeframe: string;
  severity: Severity; enabled: boolean; notify: boolean; confirm_passes: number; rearm_minutes: number;
  repeat_minutes: number; archived_at: string | null; created_at: string; updated_at: string; version: number;
  state: { active: boolean; checked_at: string | null; last_fired_at: string | null; last_cleared_at: string | null;
           passes_true: number; last: RuleResult | null } | null;
}

export interface MonitorRunInfo {
  id: string; started_at: string; finished_at: string | null; seconds: number | null; trigger: string; ok: boolean;
  errors: string[]; positions: number; contracts: number; rules: number; events: number; mode: string; provider: string;
}

export interface MonitorStatus {
  enabled: boolean; schedule: string; window_open: boolean; why_closed: string | null; next_window_open: string | null;
  runner: { state: string; detail: string | null; next_pass_at: string | null; in_process: boolean;
            lock_holder_pid: string | null; running_since: string | null };
  last_run: MonitorRunInfo | null; last_successful_run_at: string | null; stale_after_seconds: number;
  unacknowledged: { warning: number; critical: number }; latest_seq: number;
  quotes: DataSources["quotes"]; note: string | null;
}

export interface MonitorSettings {
  enabled: boolean;
  schedule: { start: string; end: string; timezone: string; days: string[]; every_seconds: number; skip_market_holidays: boolean };
  confirm_passes: number; repeat_urgent_minutes: number; rearm_minutes: number; stale_after_seconds: number;
  default_rules: RuleSet; timeframe: string; completed_bars_only: boolean; assignment_risk_days: number;
  expiry_action: boolean; expiration_warning_days: number[]; liquidity_max_spread_pct: number;
  retention_full_days: number;
  notifications: { desktop: boolean; min_severity: Severity; quiet_hours: boolean; quiet_start: string;
                   quiet_end: string; critical_during_quiet_hours: boolean; muted_symbols: string[] };
}

export interface SnapshotRow {
  taken_at: string; status: string; raw_status: string; open_pnl: Money | null; open_pnl_pct: number | null;
  value: Money | null; underlying_price: Money | null; delta: number | null; gamma: number | null;
  theta: number | null; vega: number | null; freshness: Freshness; valuation_status: string;
}

export type TimelineItem =
  | { kind: "event"; at: string; event: EventItem }
  | { kind: "audit"; at: string; audit: { id: string; action: string; reason: string | null; before: unknown; after: unknown } };

export interface ContractMonitor {
  checked_at: string | null; quote_status: string | null; conditions: RuleResult[]; freshness: Freshness | null;
  reason: string | null; pending: { status: string; passes: number; needed: number } | null; automated: boolean;
}

export interface Vocabulary {
  targets: Record<"position" | "contract" | "symbol", { name: string; meaning: string }[]>;
  chain: { name: string; meaning: string }[]; bar_names: string[]; bar_functions: string[];
  statuses: Record<string, string>;
}

export interface YamlRow {
  index: number; name: string; symbol: string; fingerprint: string;
  status: "new" | "duplicate" | "invalid" | "imported" | "skipped"; errors: string[]; warnings: string[];
  strategy: string | null; max_loss: { state: string; value: string | null } | null; needs_risk_ack: boolean;
  rules: RuleSet; timeframe: string;
}

export interface Issue { code: string; message: string; leg: number | null }

export interface QuoteBlock {
  freshness: Freshness; provider: string; delay_minutes: number; fetched_at: string | null;
  quote_age_seconds: number | null; error: string | null; underlying_price: Money | null;
  greeks: Record<string, number> | null; greeks_source: string;
  legs: { leg: number; status: string; reason?: string | null; mid: Money | null; bid?: Money | null;
          ask?: Money | null; iv?: number | null; delta?: number | null; quality?: string[]; nearest?: string[] }[];
}

export interface Analysis {
  strategy: string; strategy_name: string; exact: boolean; confidence: "exact" | "custom"; variant: string | null;
  direction: string; units: number; structure: Record<string, unknown>[]; net_premium: Money | null;
  premium_type: string; fees: Money; cost_basis: Money | null; max_gain: Bounded; max_loss: Bounded;
  breakevens: Money[] | null; breakeven_state: "known" | "unknown" | "none"; defined_risk: boolean;
  buying_power_effect: Bounded; buying_power_basis: string; expirations: string[]; errors: Issue[];
  warnings: Issue[]; assumptions: string[]; requires_risk_acknowledgement: boolean;
  payoff: { price: Money; pnl: Money }[]; quotes: QuoteBlock | null;
}

export interface LegValuation {
  leg_index: number; occ_symbol: string | null; status: string; reason: string | null; mark: Money | null;
  exit_price: Money | null; value: Money | null; bid: Money | null; ask: Money | null; iv: number | null;
  greeks: Record<string, number | null>; quality: string[]; nearest: string[];
}

export interface Valuation {
  label: string; freshness: Freshness; provider: string; delay_minutes: number; fetched_at: string | null;
  source_timestamp: string | null; quote_age_seconds: number | null; greeks_source: string; valued_at: string;
  status: "AVAILABLE" | "PARTIAL" | "UNAVAILABLE"; reasons: string[]; value: Money | null; exit_value: Money | null;
  open_pnl: Money | null; open_pnl_pct: number | null; underlying_price: Money | null;
  greeks: Record<string, number | null>; legs: LegValuation[];
}

export type ContractStatus = "RESEARCHING" | "WAITING_FOR_CONFIRMATION" | "ENTRY_QUALIFIED" | "ENTRY_CONDITION_LOST" |
  "IN_POSITION" | "EXPIRED" | "ARCHIVED";

export interface Contract {
  id: string; account_id: string | null; underlying: string; option_right: "CALL" | "PUT"; strike: Money;
  expiration: string; days_to_expiration: number; occ_symbol: string; status: ContractStatus;
  target_entry: Money | null; invalidation: string | null; alert_conditions: string | null; notes: string | null;
  position_id: string | null; archived_at: string | null; created_at: string; updated_at: string; version: number;
}

export interface ContractQuote {
  freshness: Freshness; provider: string; delay_minutes: number; fetched_at: string | null;
  quote_age_seconds: number | null; status: string; reason: string | null; nearest: string[]; bid: Money | null;
  ask: Money | null; mid: Money | null; last: Money | null; spread: Money | null; spread_pct: number | null;
  volume: number | null; open_interest: number | null; iv: number | null; delta: number | null; gamma: number | null;
  theta: number | null; vega: number | null; underlying_price: Money | null; intrinsic: Money | null;
  extrinsic: Money | null; breakeven: Money | null; quality: string[]; greeks_source: string;
}

export interface Page<T> { items: T[]; total: number; page: number; page_size: number }
export interface Preferences { terminology: "professional" | "colony"; timezone: string; active_account_id: string | null }
export interface DataSources {
  quotes: { mode: string; provider: string; delay_minutes: number; synthetic: boolean; greeks_source: string };
  research_lab_url: string | null; notes: string[];
}
export interface ImportReport {
  ok: boolean; file_errors: string[];
  rows: { row: number; position_ref: string; errors: string[] }[];
  positions: { position_ref: string; rows: number[]; underlying: string | null; valid: boolean; errors: string[];
               warnings: string[]; strategy: string | null; max_loss: Money | null; max_loss_state: string | null;
               duplicate_of: string | null; legs: number }[];
}
export interface AuditEntry { id: string; action: string; reason: string | null; at: string;
                              before: Record<string, unknown> | null; after: Record<string, unknown> | null }
