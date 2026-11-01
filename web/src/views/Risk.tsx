// Risk dashboard (Colony defense): the account against its policy now, open positions against the current
// policy, and the policy itself (editable; every save is a new revision, earlier checks keep theirs).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Account, PolicyPayload, PolicySettings, RiskDashboard } from "../api/types";
import { RiskBadge, RiskChecks } from "../components/RiskReport";
import { BoundedValue, Empty, ErrorState, FieldErrors, Loading, Modal, Pnl, TermLabel, Tip, useToast } from "../components/ui";
import { dateTime, money, moneyInputError } from "../lib/format";
import { useRefetchInterval } from "../lib/live";

export function RiskView({ account }: { account: Account }) {
  const [editing, setEditing] = useState(false);
  const d = useQuery<RiskDashboard, ApiError>({ queryKey: ["risk", account.id], queryFn: () => api.riskDashboard(account.id), refetchInterval: useRefetchInterval() });
  const pol = useQuery<PolicyPayload, ApiError>({ queryKey: ["risk-policy", account.id], queryFn: () => api.riskPolicy(account.id) });
  return (
    <div className="dashboard">
      <section className="panel" aria-labelledby="risk-h">
        <div className="panel-head">
          <h2 id="risk-h"><TermLabel k="risk" /> {d.data && <RiskBadge r={d.data.utilization.overall} />}</h2>
          <div className="panel-tools">
            <span className="muted-text small">{pol.data ? (pol.data.is_default ? "Default policy (not saved yet)" : `Policy revision ${pol.data.revision}, ${dateTime(pol.data.created_at)}`) : ""}</span>
            <button className="btn small primary" onClick={() => setEditing(true)} disabled={!pol.data}>Edit policy</button>
          </div>
        </div>
        {d.isPending ? <Loading what="Checking the account" /> : d.isError ? <ErrorState error={d.error} retry={() => d.refetch()} /> : (
          <>
            <p className="small">{d.data.utilization.summary} <span className="muted-text">BLOCKED means a rule is broken: optlab doesn't stop orders, it tells you.</span></p>
            <p className={`small ${d.data.small_account_active ? "warn-text" : "muted-text"}`}>{d.data.small_account_reason}</p>
            <div className="cards">
              <div className="card"><div className="card-label">Account value</div><div className="card-value">{money(d.data.account_value) ?? "—"}</div><div className="card-sub">cash {money(d.data.cash) ?? "—"}</div></div>
              <div className="card"><div className="card-label"><Tip text="Sum of the maximum loss of open positions whose loss is capped."><span className="term" tabIndex={0}>Open risk<span className="info" aria-hidden>ⓘ</span></span></Tip></div>
                <div className="card-value">{money(d.data.open_risk)}</div>{d.data.open_unbounded > 0 && <div className="card-sub">⚠ plus {d.data.open_unbounded} with unlimited or unknown risk</div>}</div>
              <div className="card"><div className="card-label">Realized today</div><div className="card-value"><Pnl value={d.data.realized_today} /></div></div>
              <div className="card"><div className="card-label">Realized this week</div><div className="card-value"><Pnl value={d.data.realized_week} /></div></div>
              <div className="card"><div className="card-label">Live plans</div><div className="card-value">{d.data.live_plans}</div></div>
              <div className={`card ${d.data.rule_violations ? "card-warn" : ""}`}><div className="card-label">Recorded rule breaks</div><div className="card-value">{d.data.rule_violations}</div><div className="card-sub">open positions recorded against the policy</div></div>
            </div>
            <h3>Account limits</h3>
            {d.data.utilization.checks.length === 0 ? <Empty>No account-wide limits are set. Edit the policy to add some.</Empty> : <RiskChecks checks={d.data.utilization.checks} all />}
            <h3>Open positions against the current policy</h3>
            {d.data.positions.length === 0 ? <Empty>No open positions.</Empty> : (
              <table className="table compact"><thead><tr><th>Position</th><th>Max loss</th><th>Now</th><th>At entry</th><th>Findings</th></tr></thead>
                <tbody>{d.data.positions.map((p) => (
                  <tr key={p.id}><td><a href={`#/positions/${p.id}`}>{p.name}</a>{p.plan_id && <span className="muted-text small"> · <a href={`#/plans/${p.plan_id}`}>planned</a></span>}
                    {p.rule_violation && <div><span className="badge bad">⛔ recorded against the policy</span></div>}</td>
                    <td><BoundedValue b={p.max_loss} kind="loss" /></td><td><RiskBadge r={p.overall} /></td>
                    <td>{p.entry_risk ? <RiskBadge r={p.entry_risk} /> : <span className="muted-text small">not checked (recorded before Phase 3)</span>}</td>
                    <td className="small">{p.checks.map((c) => `${c.label}: ${c.reason}`).join(" ") || "—"}</td></tr>))}</tbody></table>
            )}
          </>
        )}
      </section>
      {pol.data && <PolicySummary p={pol.data} />}
      <Modal open={editing} onOpenChange={setEditing} title="Risk policy" wide>
        {pol.data && <PolicyForm account={account} payload={pol.data} onDone={() => setEditing(false)} />}
      </Modal>
    </div>
  );
}

const LIMITS: [keyof PolicySettings, string, "money" | "pct" | "int" | "num"][] = [
  ["max_capital_per_position", "Maximum capital per position", "money"], ["max_loss_per_position", "Maximum loss per position", "money"],
  ["max_risk_pct_per_position", "Maximum risk per position, % of account", "pct"], ["max_contracts_per_position", "Maximum contracts per leg", "int"],
  ["max_total_open_risk", "Maximum total open risk", "money"], ["max_total_open_risk_pct", "Maximum total open risk, % of account", "pct"],
  ["max_daily_realized_loss", "Maximum realized loss per day", "money"], ["max_weekly_realized_loss", "Maximum realized loss per week", "money"],
  ["max_open_positions", "Maximum open positions", "int"], ["max_positions_per_underlying", "Maximum positions per underlying", "int"],
  ["max_positions_per_group", "Maximum positions per correlated group", "int"], ["max_net_delta", "Maximum net delta (shares, absolute)", "num"],
  ["max_negative_theta", "Maximum theta decay ($ per day)", "num"], ["min_open_interest", "Minimum open interest per leg", "int"],
  ["max_spread_pct", "Maximum bid/ask spread %", "pct"], ["min_dte", "Minimum days to expiration", "int"], ["max_dte", "Maximum days to expiration", "int"],
];
const fmtLimit = (v: unknown, kind: string) => v === null || v === undefined || v === "" ? "—" : kind === "money" ? money(String(v)) : kind === "pct" ? `${v}%` : String(v);

function PolicySummary({ p }: { p: PolicyPayload }) {
  const e = p.effective;
  return (
    <section className="panel" aria-labelledby="pol-h">
      <h3 id="pol-h">Limits in force</h3>
      <p className="muted-text small">{e.small_account_reason} Empty limits aren't checked.</p>
      <table className="table compact"><thead><tr><th>Limit</th><th>In force</th><th>From</th></tr></thead>
        <tbody>
          {LIMITS.map(([k, label, kind]) => <tr key={k}><td>{label}</td><td className="mono">{fmtLimit(e.limits[k], kind)}</td><td className="small muted-text">{e.limits[k] === null ? "" : e.source[k]}</td></tr>)}
          <tr><td>Defined-risk positions only</td><td>{e.limits.defined_risk_only ? "Yes" : "No"}</td><td className="small muted-text">{e.source.defined_risk_only}</td></tr>
          <tr><td>Naked short options allowed</td><td>{e.limits.allow_naked_short_options ? "Yes" : "No"}</td><td className="small muted-text">{e.source.allow_naked_short_options}</td></tr>
          <tr><td>Margin allowed</td><td>{e.limits.allow_margin ? "Yes" : "No (must fit in cash)"}</td><td className="small muted-text">{e.source.allow_margin}</td></tr>
          <tr><td>Earnings before expiration</td><td>{e.limits.earnings}</td><td className="small muted-text">{e.source.earnings}</td></tr>
          <tr><td>Adding to a losing position</td><td>{{ allow: "allowed", warn: "warn", require_plan: "needs a new plan" }[e.limits.averaging_down]}</td><td className="small muted-text">{e.source.averaging_down}</td></tr>
          <tr><td>Warn at</td><td>{e.limits.warn_at_pct}% of a limit</td><td /></tr>
        </tbody></table>
      <details className="small"><summary>Correlated groups</summary>
        <ul>{Object.entries(e.limits.correlation_groups).map(([g, s]) => <li key={g}><strong>{g}</strong>: {s.join(", ")}</li>)}</ul></details>
    </section>
  );
}

function PolicyForm({ account, payload, onDone }: { account: Account; payload: PolicyPayload; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const toStr = (v: unknown) => (v === null || v === undefined ? "" : String(v).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, ""));
  const [s, setS] = useState<PolicySettings>(payload.settings);
  const [txt, setTxt] = useState<Record<string, string>>(() => Object.fromEntries(LIMITS.map(([k]) => [k, toStr(payload.settings[k])])));
  const [sa, setSa] = useState<Record<string, string>>(() => ({
    max_capital_per_position: toStr(payload.settings.small_account.max_capital_per_position),
    max_loss_per_position: toStr(payload.settings.small_account.max_loss_per_position),
    max_risk_pct_per_position: toStr(payload.settings.small_account.max_risk_pct_per_position),
    max_contracts_per_position: toStr(payload.settings.small_account.max_contracts_per_position),
    max_spread_pct: toStr(payload.settings.small_account.max_spread_pct), min_open_interest: toStr(payload.settings.small_account.min_open_interest),
    threshold: toStr(payload.settings.small_account_threshold),
  }));
  const [groups, setGroups] = useState(Object.entries(payload.settings.correlation_groups).map(([g, v]) => `${g}: ${v.join(", ")}`).join("\n"));
  const [notes, setNotes] = useState("");
  const parse = (v: string, kind: string): unknown => (v.trim() === "" ? null : kind === "money" ? v.trim() : kind === "int" ? parseInt(v, 10) : Number(v));
  const errs: Record<string, string | null> = {};
  for (const [k, , kind] of LIMITS) errs[k] = kind === "money" ? moneyInputError(txt[k]) : txt[k].trim() && !Number.isFinite(Number(txt[k])) ? "A number" : null;
  const save = useMutation<PolicyPayload, ApiError>({
    mutationFn: () => {
      const out: Record<string, unknown> = { ...s };
      for (const [k, , kind] of LIMITS) out[k] = parse(txt[k], kind);
      out.small_account_threshold = sa.threshold.trim() || "0";
      out.small_account = { ...s.small_account, max_capital_per_position: parse(sa.max_capital_per_position, "money"), max_loss_per_position: parse(sa.max_loss_per_position, "money"),
        max_risk_pct_per_position: parse(sa.max_risk_pct_per_position, "pct"), max_contracts_per_position: parse(sa.max_contracts_per_position, "int"),
        max_spread_pct: parse(sa.max_spread_pct, "pct"), min_open_interest: parse(sa.min_open_interest, "int") };
      out.correlation_groups = Object.fromEntries(groups.split("\n").map((l) => l.split(":")).filter((x) => x.length === 2 && x[0].trim())
        .map(([g, v]) => [g.trim(), v.split(/[,\s]+/).filter(Boolean)]));
      return api.saveRiskPolicy(account.id, out as unknown as PolicySettings, notes.trim() || null);
    },
    onSuccess: (p) => { qc.setQueryData(["risk-policy", account.id], p); ["risk", "overview", "plans", "plan"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
                        toast(`Risk policy saved (revision ${p.revision})`); onDone(); },
  });
  const bool = (k: "defined_risk_only" | "allow_naked_short_options" | "allow_margin", label: string) => (
    <label className="check"><input type="checkbox" checked={s[k]} onChange={(e) => setS({ ...s, [k]: e.target.checked })} /> {label}</label>);
  return (
    <form className="form" noValidate onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <p className="muted-text small">Leave a limit empty to not check it. Saving adds a new revision; earlier risk checks keep the revision they used.</p>
      <fieldset><legend>Limits</legend>
        <div className="grid-3">{LIMITS.map(([k, label, kind]) => (
          <label key={k}>{label}<input inputMode="decimal" value={txt[k]} onChange={(e) => setTxt({ ...txt, [k]: e.target.value })} aria-invalid={!!errs[k]} placeholder={kind === "money" ? "$" : ""} />
            {errs[k] && <span className="field-error">{errs[k]}</span>}</label>))}</div>
        <div className="grid-3">
          {bool("defined_risk_only", "Defined-risk positions only")}{bool("allow_naked_short_options", "Allow naked short options")}{bool("allow_margin", "Allow margin")}
          <label>Earnings before expiration<select value={s.earnings} onChange={(e) => setS({ ...s, earnings: e.target.value as PolicySettings["earnings"] })}>
            <option value="off">Don't check</option><option value="warn">Warn</option><option value="block">Block</option></select></label>
          <label>Adding to a losing position<select value={s.averaging_down} onChange={(e) => setS({ ...s, averaging_down: e.target.value as PolicySettings["averaging_down"] })}>
            <option value="allow">Allow</option><option value="warn">Warn</option><option value="require_plan">Needs a new plan</option></select></label>
          <label>Warn at (% of a limit)<input type="number" min={1} max={100} value={s.warn_at_pct} onChange={(e) => setS({ ...s, warn_at_pct: Math.max(1, Math.min(100, +e.target.value || 80)) })} /></label>
        </div>
      </fieldset>
      <fieldset><legend>Small-account mode</legend>
        <div className="grid-3">
          <label className="check"><input type="checkbox" checked={s.small_account_mode} onChange={(e) => setS({ ...s, small_account_mode: e.target.checked })} /> On below this account value</label>
          <label>Threshold ($)<input inputMode="decimal" value={sa.threshold} onChange={(e) => setSa({ ...sa, threshold: e.target.value })} /></label>
        </div>
        <p className="muted-text small">While on, each of these applies where it's stricter than the limits above.</p>
        <div className="grid-3">
          {([["max_capital_per_position", "Maximum capital per position ($)"], ["max_loss_per_position", "Maximum loss per position ($)"],
             ["max_risk_pct_per_position", "Maximum risk per position, %"], ["max_contracts_per_position", "Maximum contracts per leg"],
             ["max_spread_pct", "Maximum spread %"], ["min_open_interest", "Minimum open interest"]] as const).map(([k, label]) => (
            <label key={k}>{label}<input inputMode="decimal" value={sa[k]} onChange={(e) => setSa({ ...sa, [k]: e.target.value })} /></label>))}
          {(["defined_risk_only", "allow_naked_short_options", "allow_margin"] as const).map((k) => (
            <label key={k} className="check"><input type="checkbox" checked={s.small_account[k]} onChange={(e) => setS({ ...s, small_account: { ...s.small_account, [k]: e.target.checked } })} />
              {{ defined_risk_only: "Defined risk only", allow_naked_short_options: "Allow naked short options", allow_margin: "Allow margin" }[k]}</label>))}
          <label>Adding to a losing position<select value={s.small_account.averaging_down} onChange={(e) => setS({ ...s, small_account: { ...s.small_account, averaging_down: e.target.value as PolicySettings["averaging_down"] } })}>
            <option value="allow">Allow</option><option value="warn">Warn</option><option value="require_plan">Needs a new plan</option></select></label>
        </div>
      </fieldset>
      <label>Correlated groups <span className="muted-text small">one per line: name: SYMBOL, SYMBOL…</span>
        <textarea rows={3} className="mono" value={groups} onChange={(e) => setGroups(e.target.value)} /></label>
      <label>What changed and why <span className="muted-text small">optional, kept with the revision</span><input value={notes} maxLength={2000} onChange={(e) => setNotes(e.target.value)} /></label>
      <FieldErrors error={save.error} />
      <div className="actions"><button className="btn primary" type="submit" disabled={Object.values(errs).some(Boolean) || save.isPending}>{save.isPending ? "Saving…" : "Save as a new revision"}</button></div>
    </form>
  );
}
