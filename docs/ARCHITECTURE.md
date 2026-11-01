# optlab architecture

optlab is two applications over one project folder:

| | Dashboard (new, primary) | Research Lab (original) |
|---|---|---|
| What | Accounts, positions, legs, watched contracts, strategy analysis, position monitoring, alerts and events | Watchlist, market, option chains, backtests, Signal explorer, overlay |
| Stack | FastAPI + SQLAlchemy 2 + Alembic, React 19 + TypeScript + Vite | Streamlit |
| Data | `data/optlab.sqlite` (SQLite, WAL) | `data/market.duckdb`, `data/history.duckdb` (DuckDB) |
| Port | 127.0.0.1:8600 | 127.0.0.1:8517 (systemd) / 8518 (Docker) |
| Code | `src/optlab/portfolio`, `src/optlab/monitoring`, `src/optlab/api`, `web/` | `app/`, `src/optlab/{data,live,overlay,history,...}` |

Nothing in either application places orders or connects to a broker's order entry.

```
 browser ──HTTPS (Tailscale)──▶ :8600  FastAPI ──┬─ /api/v1/*  routes.py ─▶ portfolio.service ─▶ SQLite (optlab.sqlite)
                                                  │                         │
                                                  │                         ├─ recognize.py  (strategy + exact payoff maths)
                                                  │                         └─ quotes.py     (shared chain snapshots) ─▶ Cboe via OpenBB
                                                  └─ /*  web/dist (React)                                (DuckDB: earnings dates, read-only)
 browser ◀─SSE /api/v1/stream── events + finished passes (live updates)
                    API process: background thread  monitoring.runner ─▶ monitoring.engine ─▶ quotes.py (same snapshots)
                                                                         └─▶ snapshots, state, events ─▶ SQLite
                                                                         └─▶ notify-send (desktop)
 browser ──HTTPS :8443 (Tailscale)──▶ :8517  Streamlit Research Lab ─▶ DuckDB
 systemd: optlab-monitor (scans.yaml scans + YAML positions not imported), optlab-ingest timer (DuckDB, unchanged)
```

## Backend layers (`src/optlab/portfolio`, `src/optlab/api`)

| Module | Responsibility |
|---|---|
| `money.py` | Exact money: persisted as integer $0.0001 units; inputs with more than 4 decimals rejected; computed values rounded half-even once |
| `ids.py` | UUIDv7 primary keys (RFC 9562; internal implementation, tested) |
| `occ.py` | OCC option symbols (build/parse) |
| `models.py` | SQLAlchemy models, constraints, optimistic-concurrency `version` |
| `db.py` | Engine (WAL, foreign keys, busy timeout, BEGIN IMMEDIATE for writes), forward migrations under a file lock, online backup, single-instance lock |
| `recognize.py` | Deterministic strategy recognition and exact analytics (max gain/loss, breakevens, buying-power effect) |
| `quotes.py` | One shared quote/chain snapshot service: TTL cache, single flight per symbol, provider + fetch timestamps, freshness labels, synthetic isolation |
| `valuation.py` | On-demand position valuation from the quote service (labelled ESTIMATED) |
| `service.py` | Domain operations: accounts, positions (create, edit, close, archive), contracts, preferences, audit, idempotency |
| `csv_import.py` | CSV preview/commit (atomic) and export (formula-safe) |
| `schemas.py` | Typed request/response models (pydantic v2) |
| `api/main.py`, `routes.py`, `deps.py`, `serialize.py` | FastAPI app factory, structured errors, CORS, static frontend, one transaction per request |

All financial calculations happen on the server; the browser formats strings for display.

## Monitoring layer (`src/optlab/monitoring`, Phase 2)

| Module | Responsibility |
|---|---|
| `settings.py` | Monitor settings (schedule, confirmation, re-arm, repeat, stale limit, default rules, notifications), stored in `user_preferences`; seeded from scans.yaml until first saved |
| `calendar.py` | Monitoring window: weekly schedule in its time zone (DST via zoneinfo), US market holidays and early closes for 2026–2027 |
| `rules.py` | Conditions in the scans.yaml expression language; three-valued results (true / false / unknown) with the values used; completed-bar handling; validation per target (position / contract / symbol) |
| `status.py` | Position metrics, built-in action checks, status precedence STALE > EXIT > ACTION > WATCH > HOLD and the reason strings |
| `transitions.py` | Pure state machines: status confirmation (stale immediate), urgent reminders; alert fire / clear / re-arm / repeat |
| `events.py` | Writing events, the notification policy (severity, quiet hours, mutes), desktop delivery and its log |
| `engine.py` | One pass: read (short transaction) → fetch chains, bars, earnings (no lock held) → evaluate → write snapshots, state and events (one transaction) → notify |
| `runner.py` | The scheduler thread and the per-database monitor lock (`optlab.monitor.lock`): one monitor per database across processes and containers |
| `read.py` | Effective status for the API: closed, not monitored, never checked, and the stale overlay when the last check is older than the limit |
| `legacy.py` | scans.yaml import (with fingerprints) and the skip list the YAML monitor reads |
| `api/monitor_routes.py` | `/monitor/*`, `/events`, `/alert-rules`, position snapshots / timeline / rules, scans.yaml import, `/stream` (SSE) |

Each pass fetches one chain per underlying through the API's `QuoteService`, so the monitor and the
dashboard share snapshots and never fetch the same chain twice within its TTL. Price bars are fetched only
for rules that use studies. The monitor's per-pass state lives in `monitor_states`, not on the versioned
rows, so a pass never causes an edit conflict (except a watched contract whose status the monitor changes,
which is a real change). The live stream polls the `events` table by its sequence number, so it works
whichever process wrote the events, and a reconnecting browser resumes with `Last-Event-ID` without gaps.

## Database (Phase 1, migration `0001`)

```mermaid
erDiagram
    accounts ||--o{ account_snapshots : "values over time"
    accounts ||--o{ positions : "RESTRICT delete"
    accounts |o--o{ watched_contracts : "optional (research-only allowed)"
    positions ||--|{ position_legs : "1..12 legs, CASCADE"
    positions |o--o{ watched_contracts : "position_id SET NULL"
    accounts {
        string id PK "UUIDv7"
        string name
        bool is_default
        datetime archived_at
        int version
    }
    account_snapshots {
        string id PK
        string account_id FK
        datetime as_of
        bigint total_value_units
        bigint cash_units
        bigint buying_power_units
    }
    positions {
        string id PK
        string account_id FK
        string underlying
        string strategy
        string lifecycle "OPEN|CLOSED|ARCHIVED"
        bigint net_premium_units "+debit/-credit"
        string max_loss_state "bounded|unbounded|unknown"
        bigint max_loss_units
        bigint realized_pnl_units
        bool monitoring_enabled "CHECK = 0 in Phase 1"
        int version
    }
    position_legs {
        string id PK
        string position_id FK
        string asset_type "STOCK|OPTION"
        string option_right "CALL|PUT"
        bigint strike_units
        date expiration
        int quantity "> 0"
        string side "BUY|SELL"
        bigint fill_price_units
        bigint fees_units
        string occ_symbol
        bigint close_price_units
    }
    watched_contracts {
        string id PK
        string account_id FK "nullable"
        string occ_symbol
        string status
        bigint target_entry_units
        int version
    }
    audit_events {
        string id PK
        string entity_type
        string entity_id
        string action
        json before_json
        json after_json
    }
    idempotency_keys { string key PK }
    user_preferences { string key PK }
```

Every table that records facts carries `created_at`, `updated_at`, `source`, `source_timestamp` and
`metadata_json`; editable ones carry `version`. Indexes: account, underlying, lifecycle, expiration,
OCC symbol, status, `(entity_type, entity_id, at)` on the audit log, and every foreign key.

## Planning layer (`src/optlab/planning`, Phase 3)

| Module | Responsibility |
|---|---|
| `policy.py` | Policy settings (money as exact decimal strings), small-account limits merged by strictness (`effective`), revisions in `risk_policies` |
| `riskcheck.py` | Candidate (legs or plan) and portfolio state → checks with PASS / WARNING / BLOCKED / UNKNOWN, value, limit and source; account utilization for the dashboard and events |
| `plans.py` | Plan fields, checklist validation, the state machine, risk assessment, conversion with the planned-versus-actual comparison, duplication |
| `monitor.py` | Called by the monitoring pass: trigger / confirmation / invalidation / expiry for plans; risk-limit events |
| `schemas.py` | API models for plans and policies |
| `api/plan_routes.py` | `/plans`, `/accounts/{id}/risk-policy`, `/accounts/{id}/risk`, `/risk/evaluate` |

Recording a position (`POST /positions`, CSV excluded) and converting a plan run the same check; the result
is stored on the position (`entry_risk_json`, `rule_violation`). The portfolio state comes from the
database (latest account values, open positions, positions closed today and this week) and the monitor's
latest fresh snapshots (open P&L, Greeks), never from a fresh network call.

## Database (Phase 3, migration `0003`)

| Table / column | Contents |
|---|---|
| `risk_policies` | One row per revision per account: settings JSON, notes, created, superseded (unique account + revision) |
| `trade_plans` | Versioned plans: fields (prices in units), condition text, planned legs JSON, state and when it changed, expiry, outcome reason, last risk check, linked position, conversion time, comparison JSON, archived |
| `positions.plan_id` | The plan it came from (`SET NULL` if the plan is removed) |
| `positions.entry_risk_json`, `rule_violation` | The risk check when it was recorded; whether it was recorded against the policy |

Plan events carry the plan id in their source data; a plan's timeline merges them with its audit log.

## Database (Phase 2, migration `0002`)

| Table | Contents |
|---|---|
| `position_snapshots` | One row per position per pass: raw and confirmed status, reasons, every rule result, value / exit value / open P&L (units), P&L %, Greeks, metrics, per-leg quotes, provider, freshness, fetch and source times; `keep` marks status changes (never thinned) |
| `contract_snapshots` | A watched contract's quote per pass (bid/ask/mid/last in units, IV, delta, OI, spread) and its condition results |
| `monitor_states` | Per subject (position, contract, alert rule, warning, monitor): status, since, candidate and count, armed/active, last event, last cleared, last check, last snapshot |
| `events` | The feed: integer `seq` (stream order) + UUID, time, type, severity, title, message, symbol and links (account, position, contract, rule, snapshot, run), source data, dedupe key, acknowledgement, notification state |
| `notification_deliveries` | Each desktop delivery attempt and its result |
| `alert_rules` | Versioned user rules: target (position / contract / symbol), condition, timeframe, severity, confirm / re-arm / repeat, notify, archived |
| `monitor_runs` | Each pass: start, duration, trigger (schedule / manual), provider, counts, errors |

```mermaid
erDiagram
    positions ||--o{ position_snapshots : "one per pass"
    watched_contracts ||--o{ contract_snapshots : "one per pass"
    positions ||--o{ alert_rules : "target"
    watched_contracts ||--o{ alert_rules : "target"
    events ||--o{ notification_deliveries : "desktop attempts"
    monitor_runs ||--o{ position_snapshots : "run_id"
    monitor_runs ||--o{ events : "run_id"
    positions ||--o{ events : "position_id"
    position_snapshots ||--o| events : "snapshot_id"
    monitor_states }o--|| positions : "kind=position"
```

`positions` gains `monitor_json` (own rules, timeframe, notify) and loses the Phase 1 constraint on
`monitoring_enabled`; the migration switches monitoring on for positions that are OPEN. Indexes cover
`(position_id, taken_at)`, `taken_at`, events by position / contract / symbol / type / time / unacknowledged
/ dedupe key, and every foreign key. `downgrade` drops the Phase 2 tables and restores the constraint.

## Request flow (create a position)

1. `POST /api/v1/positions` with an `Idempotency-Key`. A repeat with the same key returns the first response.
2. The schema validates types, decimal precision and timezones; a write session opens with `BEGIN IMMEDIATE`.
3. `recognize.analyze` classifies the legs and computes the risk; errors become `422` field errors; an
   unbounded/unknown maximum loss needs `acknowledge_risk`.
4. The position, its legs and an `audit_events` row are written; the transaction commits.

## Phase boundaries

- Phase 2 (this): monitoring, snapshots, statuses, alert rules, events, notifications, live updates,
  scans.yaml import. The YAML monitor keeps running the scans and the YAML positions you haven't imported.
- Phase 3: trade plans, risk policies and checks, plan conversion and comparison, risk dashboard.
- Not yet: process scoring and achievements (Phase 4; the plan comparisons and rule-violation flags are its inputs), news
  (Phase 5), historical-signal drill-downs (Phase 6). Scans still live in scans.yaml and the Research Lab.
