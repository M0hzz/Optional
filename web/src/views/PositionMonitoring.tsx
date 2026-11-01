// The monitoring parts of a position's page: current status with every reason and rule result, the rules
// (own or defaults), history charts from the snapshots, the timeline, and this position's alert rules.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { ApiError, api } from "../api/client";
import type { Position, PositionMonitor, RuleSet, SnapshotRow, TimelineItem } from "../api/types";
import { Empty, ErrorState, FieldErrors, FreshnessBadge, Loading, Modal, Pnl, SeverityBadge, StatusBadge, TermLabel, Tip,
         Unavailable, statusInfo, useToast } from "../components/ui";
import { RulesEditor } from "../forms/RulesEditor";
import { dateTime, money, num, pct, price, shortTime } from "../lib/format";
import { useRefetchInterval } from "../lib/live";
import { AlertRulesList, EventFeed, RuleTable } from "./MonitorWidgets";

export function MonitoringSection({ p }: { p: Position }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const m = useQuery<PositionMonitor, ApiError>({ queryKey: ["position-monitor", p.id], queryFn: () => api.positionMonitor(p.id), refetchInterval: useRefetchInterval() });
  const toggle = useMutation<PositionMonitor, ApiError, { monitoring_enabled?: boolean; notify?: boolean }>({
    mutationFn: (b) => api.setPositionMonitor(p.id, { version: m.data!.version, ...b }),
    onSuccess: (r) => { qc.setQueryData(["position-monitor", p.id], r); ["position", "positions", "overview", "events", "timeline"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })); },
    onError: (e) => toast(e.message, "bad"),
  });
  if (p.lifecycle !== "OPEN") return null;
  if (m.isPending) return <section className="panel"><Loading what="Loading monitoring" /></section>;
  if (m.isError) return <section className="panel"><ErrorState error={m.error} retry={() => m.refetch()} /></section>;
  const d = m.data;
  const sn = d.snapshot;
  const cfg = d.rules_config;
  const st = statusInfo(d.status);
  return (
    <section className="panel" aria-labelledby="mon-pos-h">
      <div className="panel-head">
        <h3 id="mon-pos-h"><TermLabel k="status" /></h3>
        <div className="panel-tools">
          <label className="check small"><input type="checkbox" checked={d.enabled} disabled={toggle.isPending} onChange={(e) => toggle.mutate({ monitoring_enabled: e.target.checked })} /> Monitor this position</label>
          <label className="check small"><input type="checkbox" checked={cfg.notify} disabled={toggle.isPending || !d.enabled} onChange={(e) => toggle.mutate({ notify: e.target.checked })} /> Notify</label>
          <button className="btn small" onClick={() => setEditing(true)}>Edit rules</button>
        </div>
      </div>
      <div className={`status-card status-${st.cls}`}>
        <div className="status-line"><StatusBadge m={d} compact />
          {d.since && d.status === d.stored_status && <span className="muted-text small">since {dateTime(d.since)}</span>}
          {d.checked_at && <span className="muted-text small">· checked {dateTime(d.checked_at)}</span>}
          {sn && <FreshnessBadge freshness={d.stale_overlay ? "CACHED" : sn.freshness} provider={sn.provider} fetchedAt={sn.fetched_at} ageSeconds={d.age_seconds} delayMinutes={sn.delay_minutes} />}
        </div>
        <ul className="reasons">{d.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        {d.pending && <p className="small warn-text">Changing to {d.pending.label}: seen on {d.pending.passes} of {d.pending.needed} passes needed to confirm.</p>}
      </div>
      {sn && (
        <dl className="metrics">
          <div className="metric"><dt><Tip text="Value at mid prices, from the latest monitoring pass."><span className="term" tabIndex={0}>Estimated value<span className="info" aria-hidden>ⓘ</span></span></Tip></dt>
            <dd>{sn.value !== null ? <span className="mono">{money(sn.value)}</span> : <Unavailable reason="A leg had no usable quote." />}</dd></div>
          <div className="metric"><dt><Tip text="At natural prices: sell longs at the bid, buy back shorts at the ask."><span className="term" tabIndex={0}>Exit value<span className="info" aria-hidden>ⓘ</span></span></Tip></dt>
            <dd>{sn.exit_value !== null ? <span className="mono">{money(sn.exit_value)}</span> : <Unavailable />}</dd></div>
          <div className="metric"><dt>Open P&amp;L (estimate)</dt><dd><Pnl value={sn.open_pnl} /> {sn.open_pnl_pct !== null && <span className="muted-text small">{pct(sn.open_pnl_pct)}</span>}</dd></div>
          <div className="metric"><dt>Underlying</dt><dd className="mono">{price(sn.underlying_price) ?? <Unavailable />}</dd></div>
          <div className="metric"><dt>Greeks</dt><dd className="mono">{sn.greeks.delta === null ? <Unavailable reason="A leg has no Greeks." /> : `Δ ${num(sn.greeks.delta, 1)} · Γ ${num(sn.greeks.gamma, 2)} · Θ ${num(sn.greeks.theta, 2)} · V ${num(sn.greeks.vega, 2)}`}</dd></div>
        </dl>
      )}
      <details open={d.status !== "HOLD"}>
        <summary className="small">Rules checked on the last pass ({cfg.use_defaults ? "default rules" : "this position's own rules"}, {cfg.timeframe} bars)</summary>
        {d.rules && d.rules.length > 0 ? <RuleTable rules={d.rules} /> : <p className="muted-text small">No pass has run yet.</p>}
      </details>
      <Modal open={editing} onOpenChange={setEditing} title={`Rules for ${p.name}`} wide>
        <RulesForm p={p} m={d} onDone={() => setEditing(false)} />
      </Modal>
    </section>
  );
}

function RulesForm({ p, m, onDone }: { p: Position; m: PositionMonitor; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [useDefaults, setUseDefaults] = useState(m.rules_config.use_defaults);
  const [rules, setRules] = useState<RuleSet>(m.rules_config.rules);
  const [tf, setTf] = useState(m.rules_config.timeframe);
  const [bad, setBad] = useState(false);
  const onInvalid = useCallback((b: boolean) => setBad(b), []);
  const save = useMutation<PositionMonitor, ApiError>({
    mutationFn: () => api.setPositionMonitor(p.id, useDefaults ? { version: m.version, use_defaults: true, timeframe: tf }
      : { version: m.version, timeframe: tf, rules: { exit_when: rules.exit_when.filter((x) => x.trim()), action_when: rules.action_when.filter((x) => x.trim()), watch_when: rules.watch_when.filter((x) => x.trim()) } }),
    onSuccess: (r) => { qc.setQueryData(["position-monitor", p.id], r); ["position", "positions", "timeline"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })); toast("Rules saved; they apply from the next pass"); onDone(); },
  });
  return (
    <form className="form" noValidate onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <label className="check"><input type="checkbox" checked={useDefaults} onChange={(e) => { setUseDefaults(e.target.checked); if (!e.target.checked && m.rules_config.use_defaults) setRules(m.rules_config.defaults); }} /> Use the default rules (Monitor → Settings)</label>
      <label>Bars for studies<select value={tf} onChange={(e) => setTf(e.target.value)}>{["1m", "5m", "15m", "30m", "1h", "1d", "1wk"].map((t) => <option key={t}>{t}</option>)}</select></label>
      {useDefaults ? (
        <div className="small"><p className="muted-text">Default rules:</p>
          <ul className="mono">{[...m.rules_config.defaults.exit_when.map((x) => "exit: " + x), ...m.rules_config.defaults.action_when.map((x) => "action: " + x), ...m.rules_config.defaults.watch_when.map((x) => "watch: " + x)].map((x) => <li key={x}>{x}</li>)}</ul></div>
      ) : <RulesEditor value={rules} onChange={setRules} onInvalid={onInvalid} />}
      <FieldErrors error={save.error} />
      <div className="actions"><button className="btn primary" type="submit" disabled={(bad && !useDefaults) || save.isPending}>{save.isPending ? "Saving…" : "Save rules"}</button></div>
    </form>
  );
}

const SERIES = {
  open_pnl: { label: "Open P&L ($)", get: (r: SnapshotRow) => (r.open_pnl === null ? null : Number(r.open_pnl)), fmt: (v: number) => "$" + v.toLocaleString() },
  open_pnl_pct: { label: "Open P&L (%)", get: (r: SnapshotRow) => r.open_pnl_pct, fmt: (v: number) => v.toFixed(0) + "%" },
  delta: { label: "Delta (shares)", get: (r: SnapshotRow) => r.delta, fmt: (v: number) => v.toFixed(0) },
  theta: { label: "Theta ($/day)", get: (r: SnapshotRow) => r.theta, fmt: (v: number) => "$" + v.toFixed(2) },
  underlying: { label: "Underlying", get: (r: SnapshotRow) => (r.underlying_price === null ? null : Number(r.underlying_price)), fmt: (v: number) => v.toFixed(2) },
} as const;

export function HistoryChart({ p }: { p: Position }) {
  const [key, setKey] = useState<keyof typeof SERIES>("open_pnl");
  const q = useQuery<SnapshotRow[], ApiError>({ queryKey: ["snapshots", p.id], queryFn: () => api.snapshots(p.id), refetchInterval: useRefetchInterval(60_000) });
  const s = SERIES[key];
  const data = useMemo(() => (q.data ?? []).map((r) => ({ t: Date.parse(r.taken_at), v: s.get(r), status: r.status, fresh: r.freshness })), [q.data, s]);
  const changes = useMemo(() => (q.data ?? []).filter((r, i, a) => i > 0 && r.status !== a[i - 1].status), [q.data]);
  return (
    <section className="panel" aria-labelledby="hist-h">
      <div className="panel-head"><h3 id="hist-h">History</h3>
        <div className="panel-tools"><select aria-label="Series" value={key} onChange={(e) => setKey(e.target.value as keyof typeof SERIES)}>
          {Object.entries(SERIES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select></div></div>
      {q.isPending ? <Loading /> : q.isError ? <ErrorState error={q.error} /> : data.length < 2 ? <Empty>The chart appears after two monitoring passes.</Empty> : (
        <>
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
              <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" />
              <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tick={{ fill: "var(--muted)", fontSize: 12 }}
                     tickFormatter={(t: number) => shortTime(t)} />
              <YAxis tick={{ fill: "var(--muted)", fontSize: 12 }} tickFormatter={s.fmt} width={70} />
              {key.startsWith("open_pnl") && <ReferenceLine y={0} stroke="var(--muted)" />}
              {changes.map((c) => <ReferenceLine key={c.taken_at} x={Date.parse(c.taken_at)} stroke="var(--warn)" strokeDasharray="2 4" />)}
              <RTooltip contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text)" }}
                labelFormatter={(t) => dateTime(new Date(Number(t)).toISOString())}
                formatter={(v, _n, item) => [v === null || v === undefined ? "unavailable" : s.fmt(Number(v)), `${s.label} · ${statusInfo(String((item as { payload?: { status?: string } })?.payload?.status ?? "")).text}`]} />
              <Line type="linear" dataKey="v" stroke="var(--accent)" dot={false} strokeWidth={2} isAnimationActive={false} connectNulls={false} />
            </LineChart>
          </ResponsiveContainer>
          <p className="muted-text small">One point per monitoring pass (estimated from delayed quotes; gaps are passes without a value). Dashed lines mark status changes.</p>
        </>
      )}
    </section>
  );
}

const AUDIT_LABEL: Record<string, string> = { create: "Recorded", edit: "Edited", correct: "Corrected", close: "Closed", archive: "Archived", monitor: "Monitoring settings" };

export function Timeline({ p }: { p: Position }) {
  const q = useQuery<TimelineItem[], ApiError>({ queryKey: ["timeline", p.id], queryFn: () => api.timeline(p.id), refetchInterval: useRefetchInterval() });
  return (
    <section className="panel" aria-labelledby="tl-h">
      <h3 id="tl-h">Timeline</h3>
      {q.isPending ? <Loading /> : q.isError ? <ErrorState error={q.error} /> : q.data.length === 0 ? <Empty>Nothing yet.</Empty> : (
        <ol className="timeline">{q.data.map((x) => x.kind === "event" ? (
          <li key={x.event.id}><span className="tl-time">{dateTime(x.at)}</span> <SeverityBadge s={x.event.severity} /> <strong>{x.event.title}</strong>
            <div className="small">{x.event.message}</div></li>
        ) : (
          <li key={x.audit.id}><span className="tl-time">{dateTime(x.at)}</span> <span className="badge muted">✎ Record</span> <strong>{AUDIT_LABEL[x.audit.action] ?? x.audit.action}</strong>
            {x.audit.reason && <div className="small">Reason: {x.audit.reason}</div>}</li>
        ))}</ol>
      )}
    </section>
  );
}

export function PositionAlerts({ p }: { p: Position }) {
  return (
    <section className="panel" aria-labelledby="pa-h">
      <details>
        <summary><h3 id="pa-h" className="inline"><TermLabel k="alertRules" /></h3></summary>
        <AlertRulesList positionId={p.id} accountId={p.account_id} />
      </details>
      <EventFeed positionId={p.id} title={false} />
    </section>
  );
}
