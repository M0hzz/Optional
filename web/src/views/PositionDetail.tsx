import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Analysis, AuditEntry, Position, Valuation } from "../api/types";
import { AnalysisPanel } from "../components/AnalysisPanel";
import { BoundedValue, ConfirmDialog, ErrorState, FieldErrors, FreshnessBadge, LifecycleBadge, Loading, Modal, Pnl,
         TermLabel, Tip, Unavailable, useToast } from "../components/ui";
import { ClosePositionForm } from "../forms/ClosePositionForm";
import { PositionForm } from "../forms/PositionForm";
import { date, dateTime, money, num, pct, price } from "../lib/format";
import { useRefetchInterval } from "../lib/live";
import { navigate } from "../lib/router";
import { RiskBadge, RiskPanel } from "../components/RiskReport";
import { HistoryChart, MonitoringSection, PositionAlerts, Timeline } from "./PositionMonitoring";

export function PositionDetail({ id }: { id: string }) {
  const q = useQuery<Position, ApiError>({ queryKey: ["position", id], queryFn: () => api.position(id), refetchInterval: useRefetchInterval() });
  const analysis = useQuery<Analysis, ApiError>({ queryKey: ["position-analysis", id, q.data?.version], queryFn: () => api.positionAnalysis(id), enabled: !!q.data });
  const [editing, setEditing] = useState(false);
  const [closing, setClosing] = useState(false);
  const [archiving, setArchiving] = useState<null | "closed" | "entered_in_error">(null);
  const [showEstimate, setShowEstimate] = useState(false);
  const qc = useQueryClient();
  const toast = useToast();
  const archive = useMutation<Position, ApiError, "closed" | "entered_in_error">({
    mutationFn: (reason) => api.archivePosition(id, { version: q.data!.version, reason }),
    onSuccess: (p) => { qc.setQueryData(["position", id], p); qc.invalidateQueries({ queryKey: ["positions"] }); qc.invalidateQueries({ queryKey: ["overview"] }); setArchiving(null); toast(`Archived ${p.name}`); },
  });
  if (q.isPending) return <Loading what="Loading position" />;
  if (q.isError) return <ErrorState error={q.error} retry={() => q.refetch()} />;
  const p = q.data;
  return (
    <div className="detail">
      <button className="btn ghost small" onClick={() => navigate("/")}>← Back to dashboard</button>
      <section className="panel">
        <div className="panel-head">
          <h2>{p.name} <LifecycleBadge lifecycle={p.lifecycle} /></h2>
          <div className="panel-tools">
            {p.lifecycle !== "ARCHIVED" && <button className="btn small" onClick={() => setEditing(true)}>Edit</button>}
            {p.lifecycle === "OPEN" && <button className="btn small primary" onClick={() => setClosing(true)}>Close…</button>}
            {p.lifecycle === "CLOSED" && <button className="btn small" onClick={() => setArchiving("closed")}>Archive…</button>}
            {p.lifecycle === "OPEN" && <button className="btn small ghost" onClick={() => setArchiving("entered_in_error")}>Entered in error…</button>}
            {p.lifecycle !== "OPEN" && p.archive_reason !== "entered_in_error" && <a className="btn small primary" href={`#/reports/${p.id}`}>Mission report</a>}
          </div>
        </div>
        <dl className="facts">
          <div><dt>Symbol</dt><dd className="mono">{p.underlying}</dd></div>
          <div><dt>Strategy</dt><dd>{p.strategy_name} {!p.strategy_exact && <span className="muted-text small">(custom: no exact named match)</span>}</dd></div>
          <div><dt>Direction</dt><dd>{p.direction}</dd></div>
          <div><dt>Opened</dt><dd>{dateTime(p.opened_at)}</dd></div>
          {p.closed_at && <div><dt>{p.archive_reason === "entered_in_error" ? "Archived" : "Closed"}</dt><dd>{dateTime(p.closed_at)}</dd></div>}
          <div><dt><Tip text="Net premium at entry: paid (debit) or received (credit), before fees."><span className="term" tabIndex={0}>Entry premium<span className="info" aria-hidden>ⓘ</span></span></Tip></dt>
            <dd className="mono">{p.net_premium === null ? <Unavailable reason="A fill price is missing." /> : `${money(p.net_premium.replace("-", ""))} ${p.premium_type.toLowerCase()}`}</dd></div>
          <div><dt>Opening fees</dt><dd className="mono">{money(p.fees)}</dd></div>
          <div><dt><TermLabel k="maxLoss" /></dt><dd><BoundedValue b={p.max_loss} kind="loss" />{p.risk_acknowledged && <span className="muted-text small"> (acknowledged at entry)</span>}</dd></div>
          <div><dt>Maximum gain</dt><dd><BoundedValue b={p.max_gain} kind="gain" /></dd></div>
          <div><dt>Defined risk</dt><dd>{p.defined_risk ? <span className="badge ok">◆ Yes</span> : <span className="badge bad">◇ No</span>}</dd></div>
          {p.realized_pnl !== null && <div><dt>Realized P&amp;L</dt><dd><Pnl value={p.realized_pnl} /></dd></div>}
          <div><dt>Plan</dt><dd>{p.plan_id ? <a href={`#/plans/${p.plan_id}`}>◎ open the plan (and planned vs actual)</a> : <span className="muted-text">recorded without a plan</span>}</dd></div>
          <div><dt>Risk check at entry</dt><dd>{p.entry_risk ? <RiskBadge r={p.entry_risk.overall} /> : <span className="muted-text">not checked</span>}
            {p.rule_violation && <span className="badge bad"> ⛔ recorded against the policy</span>}</dd></div>
        </dl>
        {p.notes && <div className="notes"><strong>Notes</strong><p className="pre">{p.notes}</p></div>}
      </section>

      <section className="panel" aria-labelledby="legs-h">
        <h3 id="legs-h">Legs</h3>
        <div className="table-wrap"><table className="table">
          <thead><tr><th>#</th><th>Side</th><th>Qty</th><th>Contract</th><th>Fill</th><th>Fees</th><th>Filled</th><th>Closed at</th><th>Order ID</th></tr></thead>
          <tbody>{p.legs.map((l) => (
            <tr key={l.id}>
              <td>{l.leg_index + 1}</td><td>{l.side === "BUY" ? "Buy" : "Sell"}</td><td>{l.quantity}</td>
              <td>{l.asset_type === "STOCK" ? "Shares" : <><span className="mono">{price(l.strike)} {l.option_right?.toLowerCase()} {date(l.expiration)}</span><div className="muted-text small mono">{l.occ_symbol}{l.multiplier !== 100 ? ` · multiplier ${l.multiplier}` : ""}</div></>}</td>
              <td className="mono">{price(l.fill_price) ?? <Unavailable />}</td><td className="mono">{money(l.fees)}</td>
              <td>{dateTime(l.filled_at) ?? "—"}</td>
              <td className="mono">{l.close_price !== null ? `${price(l.close_price)} (fees ${money(l.close_fees)})` : "—"}</td>
              <td className="mono">{l.broker_order_id ?? "—"}</td>
            </tr>))}</tbody>
        </table></div>
      </section>

      <MonitoringSection p={p} />

      {p.lifecycle === "OPEN" && (
        <section className="panel" aria-labelledby="est-h">
          <div className="panel-head"><h3 id="est-h">Fresh estimate</h3>
            {!showEstimate && <button className="btn small" onClick={() => setShowEstimate(true)}>Get an estimate now</button>}</div>
          {showEstimate ? <EstimatePanel id={p.id} /> : <p className="muted-text">The values above are from the latest monitoring pass. Fetch a fresh estimate now (from the shared, delayed quote snapshot) without waiting for the next pass.</p>}
        </section>
      )}

      <HistoryChart p={p} />

      <section className="panel" aria-labelledby="an-h">
        <h3 id="an-h">Entry analysis</h3>
        {analysis.isPending ? <Loading /> : analysis.isError ? <ErrorState error={analysis.error} /> : <AnalysisPanel a={analysis.data} />}
      </section>

      <PositionAlerts p={p} />
      <Timeline p={p} />
      {p.entry_risk && p.entry_risk.overall !== "PASS" && (
        <section className="panel"><details><summary><h3 className="inline">Risk check when recorded</h3></summary><RiskPanel report={p.entry_risk} /></details></section>
      )}
      <AuditPanel id={p.id} version={p.version} />

      <Modal open={editing} onOpenChange={setEditing} title={`Edit ${p.name}`} wide>
        <PositionForm accountId={p.account_id} existing={p} onDone={() => setEditing(false)} />
      </Modal>
      <Modal open={closing} onOpenChange={setClosing} title={`Close ${p.name}`} wide>
        <ClosePositionForm position={p} onDone={() => setClosing(false)} />
      </Modal>
      <ConfirmDialog open={archiving !== null} onOpenChange={(o) => !o && setArchiving(null)}
        title={archiving === "entered_in_error" ? "Archive as entered in error?" : "Archive this position?"}
        body={<>{archiving === "entered_in_error" ? <p>Use this only if the position was recorded by mistake. It is kept in the archive (and the audit log) with no realized P&amp;L.</p>
                                                  : <p>The closed position moves to the archive, read-only. Its record and audit history are kept.</p>}
               <FieldErrors error={archive.error} /></>}
        confirmLabel="Archive" danger busy={archive.isPending} onConfirm={() => archiving && archive.mutate(archiving)} />
    </div>
  );
}

function EstimatePanel({ id }: { id: string }) {
  const v = useQuery<Valuation, ApiError>({ queryKey: ["valuation", id], queryFn: () => api.valuation(id), staleTime: 30_000 });
  if (v.isPending) return <Loading what="Fetching quotes" />;
  if (v.isError) return <ErrorState error={v.error} retry={() => v.refetch()} />;
  const d = v.data;
  return (
    <div>
      <div className="quotes-row">
        <span className="badge info" aria-label="On-demand estimate">≈ On-demand estimate</span>
        <FreshnessBadge freshness={d.freshness} provider={d.provider} fetchedAt={d.fetched_at} ageSeconds={d.quote_age_seconds} delayMinutes={d.delay_minutes} />
        <span className="muted-text small">Valued {dateTime(d.valued_at)}</span>
        <button className="btn small" onClick={() => v.refetch()} disabled={v.isFetching}>{v.isFetching ? "Refreshing…" : "Refresh"}</button>
      </div>
      {d.reasons.length > 0 && <div className="issues warn" role="status"><strong>⚠ Incomplete</strong><ul>{d.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul></div>}
      <dl className="metrics">
        <div className="metric"><dt><Tip text="Value at the mid price of each leg (long legs positive, short legs negative)."><span className="term" tabIndex={0}>Estimated value<span className="info" aria-hidden>ⓘ</span></span></Tip></dt>
          <dd>{d.value !== null ? <span className="mono">{money(d.value)}</span> : <Unavailable reason="At least one leg has no usable quote." />}</dd></div>
        <div className="metric"><dt><Tip text="Value at natural prices: what you'd likely get closing now (sell at the bid, buy back at the ask)."><span className="term" tabIndex={0}>Exit value<span className="info" aria-hidden>ⓘ</span></span></Tip></dt>
          <dd>{d.exit_value !== null ? <span className="mono">{money(d.exit_value)}</span> : <Unavailable />}</dd></div>
        <div className="metric"><dt>Open P&amp;L (estimate)</dt><dd><Pnl value={d.open_pnl} /> {d.open_pnl_pct !== null && <span className="muted-text small">{pct(d.open_pnl_pct)} of entry cost</span>}</dd></div>
        <div className="metric"><dt>Underlying</dt><dd className="mono">{price(d.underlying_price) ?? <Unavailable />}</dd></div>
        <div className="metric"><dt><Tip text={`Position Greeks (${d.greeks_source === "provider" ? "exchange-computed" : "model-estimated"}): delta in shares, theta $/day, vega $ per vol point.`}><span className="term" tabIndex={0}>Greeks<span className="info" aria-hidden>ⓘ</span></span></Tip></dt>
          <dd className="mono">{d.greeks.delta === null ? <Unavailable reason="A leg has no Greeks." /> : `Δ ${num(d.greeks.delta, 1)} · Γ ${num(d.greeks.gamma, 2)} · Θ ${num(d.greeks.theta, 2)} · V ${num(d.greeks.vega, 2)}`}</dd></div>
      </dl>
      <table className="table compact">
        <thead><tr><th>Leg</th><th>Status</th><th>Bid</th><th>Ask</th><th>Mark</th><th>IV</th><th>Quality</th></tr></thead>
        <tbody>{d.legs.map((l) => (
          <tr key={l.leg_index}><td className="mono">{l.occ_symbol ?? "Shares"}</td>
            <td>{l.status === "FOUND" ? <span className="badge ok">✓ Found</span> : <Tip text={[l.reason, l.nearest.length ? "Nearest: " + l.nearest.join(", ") : ""].filter(Boolean).join(" ")}><span className="badge bad" tabIndex={0}>⊘ {l.status === "NOT_FOUND" ? "Not in chain" : "Unavailable"}</span></Tip>}</td>
            <td className="mono">{price(l.bid) ?? "—"}</td><td className="mono">{price(l.ask) ?? "—"}</td><td className="mono">{price(l.mark) ?? "—"}</td>
            <td>{l.iv !== null ? pct(l.iv * 100) : "—"}</td><td>{l.quality.length ? "⚠ " + l.quality.join(", ") : "—"}</td></tr>))}</tbody>
      </table>
    </div>
  );
}

function AuditPanel({ id, version }: { id: string; version: number }) {
  const a = useQuery<AuditEntry[], ApiError>({ queryKey: ["audit", id, version], queryFn: () => api.audit(id) });
  return (
    <section className="panel" aria-labelledby="audit-h">
      <details>
        <summary><h3 id="audit-h" className="inline">History</h3> <span className="muted-text small">every change to fills, closing and archiving, with the values before and after</span></summary>
        {a.isPending ? <Loading /> : a.isError ? <ErrorState error={a.error} /> : (
          <ol className="audit">{a.data.map((e) => (
            <li key={e.id}><strong>{e.action}</strong> · {dateTime(e.at)}{e.reason ? ` · reason: ${e.reason}` : ""}</li>))}</ol>
        )}
      </details>
    </section>
  );
}
