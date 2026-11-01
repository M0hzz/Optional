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
  risk: { overall: RiskResult; summary: string; checks: RiskCheck[]; realized_today: Money; realized_week: Money;
          small_account_active: boolean; small_account_reason: string; policy_revision: number | null };
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
  plan_id: string | null; entry_risk: RiskReport | null; rule_violation: boolean;
}

// ------------------------------------------------------------------ plans and risk (Phase 3)
export type RiskResult = "PASS" | "WARNING" | "BLOCKED" | "UNKNOWN";
export interface RiskCheck { code: string; label: string; result: RiskResult; reason: string; value: string | null; limit: string | null; source: string | null }
export interface RiskReport {
  overall: RiskResult; checks: RiskCheck[]; policy_revision: number | null; policy_id?: string | null;
  small_account_active: boolean; small_account_reason: string; evaluated_at?: string; summary: string;
}

export type PlanState = "DRAFT" | "ARMED" | "WAITING" | "QUALIFIED" | "INVALIDATED" | "CONVERTED" | "SKIPPED" | "EXPIRED" | "ARCHIVED";
export interface PlanFieldsT {
  thesis: string | null; direction: "bullish" | "bearish" | "neutral" | "volatility" | null; intended_strategy: string | null;
  market_regime: string | null; setup_type: string | null; trigger: string | null; trigger_conditions: string | null;
  confirmation: string | null; confirmation_conditions: string | null; entry_zone_low: Money | null; entry_zone_high: Money | null;
  max_entry_debit: Money | null; min_entry_credit: Money | null; max_spread_pct: number | null; min_volume: number | null;
  min_open_interest: number | null; target: string | null; target_price: Money | null; invalidation: string | null;
  invalidation_conditions: string | null; invalidation_price: Money | null; planned_max_loss: Money | null;
  planned_contracts: number | null; expiration_from: string | null; expiration_to: string | null; delta_min: number | null;
  delta_max: number | null; time_stop: string | null; event_risks: string | null; related_news: string | null;
  chart_reference: string | null; notes: string | null; expires_at: string | null;
}
export interface ChecklistItem { item: string; ok: boolean; level: "error" | "warning"; detail: string }
export interface ComparisonItem { aspect: string; planned: string; actual: string; result: "as_planned" | "deviated" | "not_compared"; note: string }
export interface Plan {
  id: string; account_id: string; symbol: string; title: string; state: PlanState; state_changed_at: string;
  fields: PlanFieldsT; legs: LegIn[]; expires_at: string | null; outcome_reason: string | null; risk: RiskReport | null;
  validation: { ready: boolean; errors: ChecklistItem[]; warnings: ChecklistItem[]; checklist: ChecklistItem[] } | null;
  position_id: string | null; converted_at: string | null;
  comparison: { items: ComparisonItem[]; as_planned: number; deviated: number; compared: number; converted_from: string;
                plan_snapshot: Record<string, unknown> } | null;
  allowed_transitions: PlanState[];
  monitor: { checked_at: string | null; trigger?: RuleResult[]; confirmation?: RuleResult[]; invalidation?: RuleResult[];
             pending: { state: string; passes: number } | null; note?: string } | null;
  archived_at: string | null; created_at: string; updated_at: string; version: number;
}

export interface PolicySettings {
  max_capital_per_position: Money | null; max_loss_per_position: Money | null; max_risk_pct_per_position: number | null;
  max_contracts_per_position: number | null; defined_risk_only: boolean; allow_naked_short_options: boolean; allow_margin: boolean;
  max_total_open_risk: Money | null; max_total_open_risk_pct: number | null; max_daily_realized_loss: Money | null;
  max_weekly_realized_loss: Money | null; max_open_positions: number | null; max_positions_per_underlying: number | null;
  max_positions_per_group: number | null; correlation_groups: Record<string, string[]>; max_net_delta: number | null;
  max_negative_theta: number | null; min_open_interest: number | null; max_spread_pct: number | null; min_dte: number | null;
  max_dte: number | null; earnings: "off" | "warn" | "block"; averaging_down: "allow" | "warn" | "require_plan";
  warn_at_pct: number; small_account_mode: boolean; small_account_threshold: Money;
  small_account: { max_capital_per_position: Money | null; max_loss_per_position: Money | null; max_risk_pct_per_position: number | null;
                   max_contracts_per_position: number | null; max_spread_pct: number | null; min_open_interest: number | null;
                   defined_risk_only: boolean; allow_naked_short_options: boolean; allow_margin: boolean;
                   averaging_down: "allow" | "warn" | "require_plan" };
}
export interface PolicyPayload {
  settings: PolicySettings; revision: number | null; id: string | null; created_at: string | null; notes: string | null;
  is_default: boolean; defaults: PolicySettings;
  effective: { limits: PolicySettings; source: Record<string, string>; small_account_active: boolean; small_account_reason: string };
}
export interface RiskDashboard {
  utilization: RiskReport; policy: { revision: number | null; is_default: boolean }; small_account_active: boolean;
  small_account_reason: string; account_value: Money | null; cash: Money | null; open_risk: Money; open_unbounded: number;
  realized_today: Money; realized_week: Money; live_plans: number; rule_violations: number;
  positions: { id: string; name: string; underlying: string; overall: RiskResult; checks: RiskCheck[]; entry_risk: RiskResult | null;
               rule_violation: boolean; plan_id: string | null; max_loss: Bounded }[];
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
export type WidgetId = "overview" | "discipline" | "progress" | "monitor" | "positions" | "events" | "contracts";
export type ThemeId = "default" | "aurora" | "nebula" | "solar";
export interface Preferences {
  terminology: "professional" | "colony"; timezone: string; active_account_id: string | null;
  theme: ThemeId; dashboard_layout: WidgetId[]; hidden_widgets: WidgetId[];
}

// ------------------------------------------------------------------ process and progression (Phase 4)
// MET earns credit · VIOLATED counts, no credit · UNKNOWN counts, no credit, disclosed · NOT_APPLICABLE excluded ·
// TO_DO: today only (an unfinished actionable item), never stored for a finished day
export type ScoreState = "MET" | "VIOLATED" | "UNKNOWN" | "NOT_APPLICABLE" | "TO_DO";
export interface ScoreItem { code: string; label: string; group: "objective" | "trade" | "review"; state: ScoreState; detail: string; kind: "auto" | "manual"; subject_id: string | null }
export interface DayScore {
  day: string; timezone: string; version: string; rest_day: boolean; score: number | null; met: number; applicable: number; unknown: number;
  in_progress: boolean; items: ScoreItem[]; points: number | null; stored: boolean;
  // stored (finished) days only
  id?: string; revision?: number; supersedes_id?: string | null; correction_reason?: string | null; calculated_at?: string; backfill?: boolean;
  source_ids?: string[];
}
export interface ScoreHistoryDay { day: string; score: number | null; rest_day: boolean; met: number; applicable: number; unknown: number; stored: boolean; revision: number | null; version: string; in_progress: boolean; scheduled: boolean }
export type LedgerType = "award" | "revocation" | "correction" | "replacement";
export interface LedgerEntry {
  id: string; entry_type: LedgerType; code: string; rule_version: string; category: string; points: number; subject_type: string; subject_id: string;
  source_ids: string[]; evidence: Record<string, unknown>; day: string; timezone: string; backfill: boolean; description: string;
  references_id: string | null; reason: string | null; actor: string; audit_id: string | null; awarded_at: string; note?: string | null;
}
export interface PointsSummary {
  lifetime_awarded: number; revoked_or_corrected: number; effective: number; total: number;
  by_category: Record<"discipline" | "research" | "risk_control" | "review", number>; entries: Record<LedgerType, number>;
}
export interface Versions { process_score: string; points_policy: string; achievement_rules: string }
export interface Achievement { code: string; name: string; description: string; goal: number; progress: number; earned_at: string | null; rule_version: string; evidence: Record<string, unknown> | null }
export interface Progress {
  versions: Versions; points: PointsSummary;
  level: { level: number; colony_name: string; points_for_level: number; points_for_next: number; progress: number };
  streak: { current: number; best: number; threshold: number };
  achievements: Achievement[]; recent_awards: LedgerEntry[]; themes: { id: ThemeId; unlock_level: number; unlocked: boolean }[];
  new: { ledger: Partial<Record<LedgerType, number>>; achievements: string[] }; today: DayScore;
}
export interface PolicyResult { code: string; name: string; result: "PASS" | "WARNING" | "BLOCKED" | "UNKNOWN"; value: string | null; limit: string | null; explanation: string; source: string | null; policy_revision: number | null; override_recorded: boolean }
export interface PolicyResults {
  overall: "PASS" | "WARNING" | "BLOCKED" | "UNKNOWN"; policy_revision: number | null; policy_id: string | null; evaluated_at: string | null;
  override_recorded: boolean; unplanned: boolean; plan: { id: string; title: string; created_after_entry: boolean } | null;
  violated: PolicyResult[]; unknown: PolicyResult[]; checks: PolicyResult[];
}
export type JournalKind = "daily_thesis" | "eod_review" | "decision" | "note" | "lesson";
export interface JournalEntryT {
  id: string; account_id: string; kind: JournalKind; entry_date: string; title: string | null; body: string; tags: string[];
  position_id: string | null; plan_id: string | null; archived_at: string | null; created_at: string; updated_at: string; version: number;
}
export interface Review {
  id: string; position_id: string; followed_plan: string | null; exit_reason: string | null; exit_followed_plan: string | null;
  mistakes: string[]; went_well: string | null; went_wrong: string | null; lessons: string | null;
  rule_violation_acknowledged: boolean; would_take_again: string | null; completed_at: string | null; version: number;
  policy_results?: PolicyResults | null; acknowledged_rules?: string[];
}
export interface ReviewVersion { at: string; action: string; before: Record<string, unknown> | null; after: Record<string, unknown> | null }
export interface PendingReview { position_id: string; name: string; underlying: string; closed_at: string | null; days_waiting: number | null; overdue: boolean; rule_violation: boolean; started: boolean; plan_id: string | null }
export interface MissionReport {
  position: Position; plan: Plan | null; review: Review | null; journal: JournalEntryT[]; timeline: TimelineItem[];
  points: LedgerEntry[]; points_net: number; policy_results: PolicyResults; review_history: ReviewVersion[]; pnl_note: string;
}
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
