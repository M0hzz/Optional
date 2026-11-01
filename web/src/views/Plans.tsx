// Trade plans (missions): the list, one plan's page with its state machine, checklist, risk check, monitor
// conditions, conversion into a position and the planned-versus-actual comparison.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Account, Plan, PlanState, RuleResult, TimelineItem } from "../api/types";
import { RiskBadge, RiskPanel } from "../components/RiskReport";
import { ConfirmDialog, Empty, ErrorState, FieldErrors, Loading, Modal, SeverityBadge, TermLabel, Tip, useToast } from "../components/ui";
import { PlanForm, STRATEGIES } from "../forms/PlanForm";
import { PositionForm } from "../forms/PositionForm";
import { date, dateTime, money } from "../lib/format";
import { useRefetchInterval } from "../lib/live";
import { navigate } from "../lib/router";
import { useTerms } from "../lib/terms";
import { RuleTable } from "./MonitorWidgets";

export const PLAN_STATE: Record<PlanState, { icon: string; text: string; cls: string; help: string }> = {
  DRAFT: { icon: "✎", text: "Draft", cls: "muted", help: "Being written. Arm it when it's complete and passes the risk check." },
  ARMED: { icon: "◎", text: "Armed", cls: "info", help: "Complete. The monitor watches the trigger." },
  WAITING: { icon: "◔", text: "Waiting", cls: "warn", help: "The trigger holds; waiting for confirmation." },
  QUALIFIED: { icon: "✓", text: "Qualified", cls: "ok", help: "Trigger and confirmation hold: entering now follows the plan." },
  INVALIDATED: { icon: "✕", text: "Invalidated", cls: "bad", help: "The invalidation happened. Kept for the record." },
  CONVERTED: { icon: "◉", text: "Converted", cls: "info", help: "Became a position; compare what you did with the plan." },
  SKIPPED: { icon: "⤼", text: "Skipped", cls: "muted", help: "You decided not to take it. Skipping is a legitimate outcome." },
  EXPIRED: { icon: "⌛", text: "Expired", cls: "muted", help: "Its validity ended before it was used." },
  ARCHIVED: { icon: "▣", text: "Archived", cls: "muted", help: "Put away; read-only." },
};

export function PlanBadge({ s }: { s: PlanState }) {
  const x = PLAN_STATE[s];
  return <Tip text={x.help}><span className={`badge ${x.cls}`} tabIndex={0}><span aria-hidden>{x.icon}</span> {x.text}</span></Tip>;
}

const strategyName = (v: string | null) => STRATEGIES.find(([k]) => k === v)?.[1] ?? v ?? "—";
const GROUPS: { label: string; states: PlanState[] }[] = [
  { label: "Active", states: ["DRAFT", "ARMED", "WAITING", "QUALIFIED"] },
  { label: "Finished", states: ["CONVERTED", "SKIPPED", "INVALIDATED", "EXPIRED"] },
  { label: "Archived", states: ["ARCHIVED"] },
];

export function PlansView({ account }: { account: Account }) {
  const { t } = useTerms();
  const [group, setGroup] = useState(0);
  const [adding, setAdding] = useState(false);
  const list = useQuery({ queryKey: ["plans", account.id, group],
                          queryFn: () => api.plans({ account_id: account.id, state: GROUPS[group].states, page_size: 200 }),
                          refetchInterval: useRefetchInterval() });
  return (
    <section className="panel" aria-labelledby="plans-h">
      <div className="panel-head">
        <h2 id="plans-h"><TermLabel k="plans" /></h2>
        <div className="panel-tools">
          <div role="tablist" className="tabs">{GROUPS.map((g, i) => (
            <button key={g.label} role="tab" aria-selected={group === i} className={`btn small ${group === i ? "primary" : ""}`} onClick={() => setGroup(i)}>{g.label}</button>))}</div>
          <button className="btn primary small" onClick={() => setAdding(true)}>+ New {t("plan").toLowerCase()}</button>
        </div>
      </div>
      <p className="muted-text small">Write the plan before the trade: thesis, trigger, confirmation, invalidation, risk and size. Arm it when it's complete;
        the monitor watches it and tells you when it qualifies. Converting it records the position and compares what you did with what you planned.</p>
      {list.isPending ? <Loading what="Loading plans" /> : list.isError ? <ErrorState error={list.error} retry={() => list.refetch()} />
        : list.data.items.length === 0 ? <Empty>{group === 0 ? <>No active plans. <button className="btn small primary" onClick={() => setAdding(true)}>Draft one</button></> : "Nothing here."}</Empty> : (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>State</th><th>Plan</th><th>Symbol</th><th>Strategy</th><th>Planned max loss</th><th>Risk check</th><th>Valid until</th><th>Updated</th></tr></thead>
          <tbody>{list.data.items.map((p) => (
            <tr key={p.id} className="clickable" onClick={() => navigate(`/plans/${p.id}`)}>
              <td><PlanBadge s={p.state} /></td>
              <td><a href={`#/plans/${p.id}`} onClick={(e) => e.stopPropagation()}>{p.title}</a>
                {p.comparison && <div className="muted-text small">{p.comparison.as_planned} of {p.comparison.compared} as planned</div>}
                {p.outcome_reason && <div className="muted-text small clip">{p.outcome_reason}</div>}</td>
              <td className="mono">{p.symbol}</td><td>{strategyName(p.fields.intended_strategy)}</td>
              <td className="mono">{money(p.fields.planned_max_loss) ?? "—"}</td>
              <td>{p.risk ? <RiskBadge r={p.risk.overall} /> : <span className="muted-text small">not checked</span>}</td>
              <td className="small">{p.expires_at ? dateTime(p.expires_at) : "—"}</td><td className="small">{dateTime(p.updated_at)}</td>
            </tr>))}</tbody>
        </table></div>
      )}
      <Modal open={adding} onOpenChange={setAdding} title={`New ${t("plan").toLowerCase()}`} wide>
        <PlanForm accountId={account.id} onDone={(p) => { setAdding(false); navigate(`/plans/${p.id}`); }} />
      </Modal>
    </section>
  );
}

// ------------------------------------------------------------------ one plan
const ACTION: Partial<Record<PlanState, { label: string; danger?: boolean; reason?: boolean; help: string }>> = {
  ARMED: { label: "Arm", help: "Complete and risk-checked: the monitor starts watching the trigger." },
  DRAFT: { label: "Back to draft", help: "Stop watching it so you can edit it; arm it again afterwards." },
  WAITING: { label: "Mark waiting", help: "The trigger happened; wait for confirmation." },
  QUALIFIED: { label: "Mark qualified", help: "Trigger and confirmation hold, by your judgement." },
  INVALIDATED: { label: "Invalidate…", danger: true, reason: true, help: "The idea was proven wrong." },
  SKIPPED: { label: "Skip…", reason: true, help: "Decide not to take it. Skipping a setup that doesn't qualify is good process." },
  EXPIRED: { label: "Mark expired", help: "Its time has passed." },
  ARCHIVED: { label: "Archive", help: "Put it away, read-only." },
};

export function PlanDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { t } = useTerms();
  const q = useQuery<Plan, ApiError>({ queryKey: ["plan", id], queryFn: () => api.plan(id), refetchInterval: useRefetchInterval() });
  const [editing, setEditing] = useState(false);
  const [converting, setConverting] = useState(false);
  const [pending, setPending] = useState<PlanState | null>(null);
  const [reason, setReason] = useState("");
  const done = (p: Plan) => { qc.setQueryData(["plan", id], p); qc.invalidateQueries({ queryKey: ["plans"] }); qc.invalidateQueries({ queryKey: ["plan-timeline", id] }); };
  const move = useMutation<Plan, ApiError, PlanState>({
    mutationFn: (to) => api.transitionPlan(id, { version: q.data!.version, to, reason: reason.trim() || null }),
    onSuccess: (p) => { done(p); setPending(null); setReason(""); toast(`Plan ${PLAN_STATE[p.state].text.toLowerCase()}`); },
  });
  const assess = useMutation<Plan, ApiError>({ mutationFn: () => api.assessPlan(id), onSuccess: (p) => { done(p); toast(`Risk check: ${p.risk?.overall ?? "—"}`); } });
  const dup = useMutation<Plan, ApiError>({ mutationFn: () => api.duplicatePlan(id), onSuccess: (p) => { qc.invalidateQueries({ queryKey: ["plans"] }); navigate(`/plans/${p.id}`); } });
  if (q.isPending) return <Loading what="Loading plan" />;
  if (q.isError) return <ErrorState error={q.error} retry={() => q.refetch()} />;
  const p = q.data;
  const f = p.fields;
  const acts = p.allowed_transitions.filter((s): s is PlanState => s !== "CONVERTED" && !!ACTION[s as PlanState]);
  const riskErr = move.error?.code === "RISK_BLOCKED" ? (move.error.details.risk as Plan["risk"]) : null;
  const fact = (label: string, v: React.ReactNode) => <div><dt>{label}</dt><dd>{v ?? "—"}</dd></div>;
  return (
    <div className="detail">
      <button className="btn ghost small" onClick={() => navigate("/plans")}>← All {t("plans").toLowerCase()}</button>
      <section className="panel">
        <div className="panel-head">
          <h2>{p.title} <PlanBadge s={p.state} /></h2>
          <div className="panel-tools">
            {p.state === "DRAFT" && <button className="btn small" onClick={() => setEditing(true)}>Edit</button>}
            {p.allowed_transitions.includes("CONVERTED") && <Tip text="Record the position you entered; the plan is kept and compared with it.">
              <button className="btn small primary" onClick={() => setConverting(true)}>Convert to {t("position").toLowerCase()}…</button></Tip>}
            {acts.map((s) => (
              <Tip key={s} text={ACTION[s]!.help}>
                <button className={`btn small ${s === "ARMED" ? "primary" : ACTION[s]!.danger ? "ghost" : ""}`}
                        onClick={() => (ACTION[s]!.reason || s === "ARCHIVED" ? setPending(s) : move.mutate(s))} disabled={move.isPending}>{ACTION[s]!.label}</button>
              </Tip>))}
            <button className="btn small ghost" onClick={() => dup.mutate()}>Duplicate</button>
          </div>
        </div>
        {move.error && pending === null && (riskErr ? <RiskPanel report={riskErr} title="Can't arm: the risk check is blocked" /> : <FieldErrors error={move.error} />)}
        {p.outcome_reason && <p className="small"><strong>{p.state === "SKIPPED" ? "Why skipped" : "Why invalidated"}:</strong> {p.outcome_reason}</p>}
        {p.position_id && <p className="small">Position: <a href={`#/positions/${p.position_id}`}>open the position</a></p>}
        <dl className="facts">
          {fact("Symbol", <span className="mono">{p.symbol}</span>)}{fact("Direction", f.direction)}{fact("Strategy", strategyName(f.intended_strategy))}
          {fact("Setup", f.setup_type)}{fact("Market regime", f.market_regime)}
          {fact("Planned maximum loss", money(f.planned_max_loss))}{fact("Planned size", f.planned_contracts ? `${f.planned_contracts} contract(s)` : null)}
          {fact("Entry zone", f.entry_zone_low || f.entry_zone_high ? `${money(f.entry_zone_low) ?? "…"} – ${money(f.entry_zone_high) ?? "…"}` : null)}
          {fact("Entry price limit", f.max_entry_debit ? `debit ≤ ${money(f.max_entry_debit)}` : f.min_entry_credit ? `credit ≥ ${money(f.min_entry_credit)}` : null)}
          {fact("Expirations", f.expiration_from || f.expiration_to ? `${date(f.expiration_from) ?? "…"} – ${date(f.expiration_to) ?? "…"}` : null)}
          {fact("Delta", f.delta_min !== null || f.delta_max !== null ? `${f.delta_min ?? "…"} – ${f.delta_max ?? "…"}` : null)}
          {fact("Liquidity", [f.max_spread_pct !== null && `spread ≤ ${f.max_spread_pct}%`, f.min_open_interest !== null && `OI ≥ ${f.min_open_interest}`, f.min_volume !== null && `volume ≥ ${f.min_volume}`].filter(Boolean).join(" · ") || null)}
          {fact("Target", [f.target, f.target_price && money(f.target_price)].filter(Boolean).join(" · ") || null)}
          {fact("Time stop", date(f.time_stop))}{fact("Valid until", p.expires_at ? dateTime(p.expires_at) : null)}
        </dl>
        <div className="grid-2 text-blocks">
          <div><h4>Thesis</h4><p className="pre">{f.thesis ?? "—"}</p></div>
          <div><h4>Trigger</h4><p className="pre">{f.trigger ?? "—"}</p>{f.trigger_conditions && <pre className="mono small">{f.trigger_conditions}</pre>}</div>
          <div><h4>Confirmation</h4><p className="pre">{f.confirmation ?? "—"}</p>{f.confirmation_conditions && <pre className="mono small">{f.confirmation_conditions}</pre>}</div>
          <div><h4>Invalidation</h4><p className="pre">{[f.invalidation, f.invalidation_price && `price ${f.direction === "bearish" ? "≥" : "≤"} ${money(f.invalidation_price)}`].filter(Boolean).join(" · ") || "—"}</p>
            {f.invalidation_conditions && <pre className="mono small">{f.invalidation_conditions}</pre>}</div>
          {(f.event_risks || f.related_news) && <div><h4>Events and news</h4><p className="pre">{[f.event_risks, f.related_news].filter(Boolean).join("\n\n")}</p></div>}
          {(f.notes || f.chart_reference) && <div><h4>Notes</h4><p className="pre">{f.notes}</p>
            {f.chart_reference && (/^https?:\/\//i.test(f.chart_reference) ? <a href={f.chart_reference} target="_blank" rel="noreferrer noopener">Chart ↗</a> : <span>{f.chart_reference}</span>)}</div>}
        </div>
        {p.legs.length > 0 && (
          <table className="table compact"><caption className="muted-text small">Planned legs (prices are planned, not fills)</caption>
            <thead><tr><th>Side</th><th>Qty</th><th>Contract</th><th>Planned price</th></tr></thead>
            <tbody>{p.legs.map((l, i) => <tr key={i}><td>{l.side === "BUY" ? "Buy" : "Sell"}</td><td>{l.quantity}</td>
              <td className="mono">{l.asset_type === "STOCK" ? "Shares" : `${money(l.strike)} ${l.option_right?.toLowerCase()} ${date(l.expiration)}`}</td>
              <td className="mono">{money(l.fill_price) ?? "—"}</td></tr>)}</tbody></table>
        )}
      </section>

      {p.validation && (
        <section className="panel" aria-labelledby="chk-h">
          <h3 id="chk-h">Checklist {p.validation.ready ? <span className="badge ok">✓ Ready to arm</span> : <span className="badge warn">✎ Incomplete</span>}</h3>
          <ul className="checklist">{p.validation.checklist.map((c, i) => (
            <li key={i} className={c.ok ? "ok" : c.level}><span aria-hidden>{c.ok ? "✓" : c.level === "error" ? "✕" : "⚠"}</span> <strong>{c.item}</strong>
              {!c.ok && <span className="small"> — {c.detail}{c.level === "warning" ? " (recommended)" : ""}</span>}</li>))}</ul>
        </section>
      )}

      <section className="panel" aria-labelledby="risk-h">
        <div className="panel-head"><h3 id="risk-h"><TermLabel k="risk" /></h3>
          {!["CONVERTED", "SKIPPED", "INVALIDATED", "EXPIRED", "ARCHIVED"].includes(p.state) &&
            <button className="btn small" onClick={() => assess.mutate()} disabled={assess.isPending}>{assess.isPending ? "Checking…" : "Check risk now"}</button>}</div>
        {p.risk ? <RiskPanel report={p.risk} title={`Checked ${p.risk.evaluated_at ? dateTime(p.risk.evaluated_at) : ""}`} />
          : <p className="muted-text small">Not checked yet. Arming checks it; so does "Check risk now".</p>}
        <FieldErrors error={assess.error} />
      </section>

      {p.monitor && ["ARMED", "WAITING", "QUALIFIED"].includes(p.state) && (
        <section className="panel" aria-labelledby="pm-h">
          <h3 id="pm-h">Monitor <span className="muted-text small">{p.monitor.checked_at ? `checked ${dateTime(p.monitor.checked_at)}` : "not checked yet"}</span></h3>
          {p.monitor.pending && <p className="small warn-text">Changing to {PLAN_STATE[p.monitor.pending.state as PlanState]?.text ?? p.monitor.pending.state}: {p.monitor.pending.passes} pass(es) so far.</p>}
          {p.monitor.note && <p className="small">{p.monitor.note}</p>}
          {(["trigger", "confirmation", "invalidation"] as const).map((k) => (p.monitor?.[k]?.length ?? 0) > 0 && (
            <div key={k}><h4>{k[0].toUpperCase() + k.slice(1)}</h4><RuleTable rules={p.monitor![k] as RuleResult[]} /></div>))}
        </section>
      )}

      {p.comparison && (
        <section className="panel" aria-labelledby="cmp-h">
          <h3 id="cmp-h">Planned versus actual <span className="muted-text small">{p.comparison.as_planned} of {p.comparison.compared} as planned · converted while {PLAN_STATE[p.comparison.converted_from as PlanState]?.text.toLowerCase()}</span></h3>
          <table className="table compact"><thead><tr><th>Aspect</th><th>Planned</th><th>Actual</th><th>Result</th></tr></thead>
            <tbody>{p.comparison.items.map((c, i) => (
              <tr key={i}><td>{c.aspect}</td><td className="small">{c.planned}</td><td className="small">{c.actual}</td>
                <td>{c.result === "as_planned" ? <span className="badge ok">✓ As planned</span> : c.result === "deviated" ? <span className="badge warn">↯ Deviated</span> : <span className="badge muted">— Not compared</span>}
                  {c.note && <div className="small muted-text">{c.note}</div>}</td></tr>))}</tbody></table>
        </section>
      )}

      <PlanTimeline id={p.id} />

      <Modal open={editing} onOpenChange={setEditing} title={`Edit ${p.title}`} wide><PlanForm accountId={p.account_id} existing={p} onDone={() => setEditing(false)} /></Modal>
      <Modal open={converting} onOpenChange={setConverting} title={`Convert ${p.title} into a ${t("position").toLowerCase()}`} wide>
        <p className="small muted-text">Enter the fills you actually got. The plan stays as written and is compared with them.
          {p.state !== "QUALIFIED" && <strong className="warn-text"> The plan hasn't qualified yet: entering now is recorded as before confirmation.</strong>}</p>
        <PositionForm accountId={p.account_id} plan={p} onDone={(pos) => { setConverting(false); qc.invalidateQueries({ queryKey: ["plan", id] }); navigate(`/positions/${pos.id}`); }} />
      </Modal>
      <ConfirmDialog open={pending !== null} onOpenChange={(o) => { if (!o) { setPending(null); move.reset(); } }}
        title={pending ? `${ACTION[pending]?.label.replace("…", "")} this plan?` : ""}
        body={<>{pending && <p>{ACTION[pending]?.help}</p>}
          {pending && ACTION[pending]?.reason && <label>Reason <span className="muted-text small">required; it's part of the record</span>
            <textarea rows={2} value={reason} maxLength={2000} onChange={(e) => setReason(e.target.value)} autoFocus /></label>}
          <FieldErrors error={move.error} /></>}
        confirmLabel={pending ? ACTION[pending]!.label.replace("…", "") : ""} danger={pending === "INVALIDATED"} busy={move.isPending}
        onConfirm={() => pending && move.mutate(pending)} />
    </div>
  );
}

function PlanTimeline({ id }: { id: string }) {
  const q = useQuery<TimelineItem[], ApiError>({ queryKey: ["plan-timeline", id], queryFn: () => api.planTimeline(id) });
  return (
    <section className="panel" aria-labelledby="ptl-h">
      <h3 id="ptl-h">Timeline</h3>
      {q.isPending ? <Loading /> : q.isError ? <ErrorState error={q.error} /> : q.data.length === 0 ? <Empty>Nothing yet.</Empty> : (
        <ol className="timeline">{q.data.map((x) => x.kind === "event" ? (
          <li key={x.event.id}><span className="tl-time">{dateTime(x.at)}</span> <SeverityBadge s={x.event.severity} /> <strong>{x.event.title}</strong><div className="small">{x.event.message}</div></li>
        ) : (
          <li key={x.audit.id}><span className="tl-time">{dateTime(x.at)}</span> <span className="badge muted">✎ Record</span> <strong>{x.audit.action === "state" ? "State change" : x.audit.action === "create" ? "Drafted" : "Edited"}</strong>
            {x.audit.reason && <div className="small">Reason: {x.audit.reason}</div>}</li>))}</ol>
      )}
    </section>
  );
}
