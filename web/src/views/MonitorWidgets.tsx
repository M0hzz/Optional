// Monitor panel (status, schedule, last pass, "Run now", settings), the event feed with acknowledgement,
// and the alert-rule list. Everything here is informational: nothing places orders.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { analysisHref } from "../lib/analysisLinks";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { AlertRule, EventItem, MonitorStatus, RuleResult, Severity } from "../api/types";
import { ConfirmDialog, Empty, ErrorState, FieldErrors, Loading, Modal, SeverityBadge, TermLabel, Tip, useToast } from "../components/ui";
import { AlertRuleForm } from "../forms/AlertRuleForm";
import { MonitorSettingsForm } from "../forms/MonitorSettingsForm";
import { YamlImportDialog } from "../forms/YamlImportDialog";
import { dateTime } from "../lib/format";
import { browserNotificationsOn, notificationSupport, setBrowserNotifications, useLiveState, useRefetchInterval } from "../lib/live";
import { navigate } from "../lib/router";

const RUNNER: Record<string, { icon: string; text: string; cls: string }> = {
  running: { icon: "⟳", text: "Checking now", cls: "info" },
  waiting: { icon: "◷", text: "Running", cls: "ok" },
  disabled: { icon: "⏸", text: "Switched off", cls: "warn" },
  elsewhere: { icon: "↗", text: "Runs in another process", cls: "info" },
  off: { icon: "○", text: "Not running here", cls: "warn" },
  stopped: { icon: "○", text: "Stopped", cls: "bad" },
  error: { icon: "⚠", text: "Error", cls: "bad" },
};

export function MonitorPanel({ accountId }: { accountId: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const live = useLiveState();
  const interval = useRefetchInterval(15_000);
  const st = useQuery<MonitorStatus, ApiError>({ queryKey: ["monitor-status"], queryFn: api.monitorStatus, refetchInterval: interval || 60_000 });
  const [settings, setSettings] = useState(false);
  const [yaml, setYaml] = useState(false);
  const [rules, setRules] = useState(false);
  const run = useMutation({
    mutationFn: api.runMonitor,
    onSuccess: (r) => {
      for (const k of ["positions", "position", "overview", "monitor-status", "events", "snapshots", "position-monitor", "contract-monitor", "alert-rules", "timeline"])
        qc.invalidateQueries({ queryKey: [k] });
      toast(r.ok ? `Checked ${r.positions} position(s), ${r.contracts} contract(s), ${r.rules} alert rule(s) in ${r.seconds.toFixed(1)} s: ${r.events} new event(s)` : "The pass failed: " + r.errors.join("; "), r.ok ? "ok" : "bad");
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "The pass failed.", "bad"),
  });
  const confirmData = useMutation({
    mutationFn: () => api.setObjective(accountId, "CHECK_DATA", { done: true, note: d ? `${d.quotes.provider}; ${d.last_run ? `last pass ${d.last_run.ok ? "OK" : "failed"}` : "no pass yet"}` : null }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["progress"] }); toast("Data sources confirmed for today"); },
  });
  const d = st.data;
  const runner = d ? (d.runner.state === "waiting" && !d.window_open ? { icon: "◷", text: "Waiting for the monitoring window", cls: "info" }
                       : (RUNNER[d.runner.state] ?? RUNNER.off)) : null;
  return (
    <section className="panel" aria-labelledby="mon-h">
      <div className="panel-head">
        <h2 id="mon-h"><TermLabel k="monitor" /></h2>
        <div className="panel-tools">
          <span className={`badge ${live === "live" ? "ok" : "warn"}`} title={live === "live" ? "Updates arrive as they happen." : "Live updates are reconnecting; the dashboard polls every 30 s meanwhile."}>
            <span aria-hidden>{live === "live" ? "●" : "◌"}</span> {live === "live" ? "Live updates" : live === "connecting" ? "Connecting…" : "Polling"}
          </span>
          <Tip text="Run one monitoring pass now, whatever the time. Outside market hours quotes are the last session's.">
            <button className="btn small primary" onClick={() => run.mutate()} disabled={run.isPending}>{run.isPending ? "Checking…" : "Run now"}</button>
          </Tip>
          <Tip text="Confirms today's 'data sources checked' objective: you looked at the quote source, its delay and the last pass.">
            <button className="btn small" onClick={() => confirmData.mutate()} disabled={confirmData.isPending}>Confirm data sources</button></Tip>
          <button className="btn small" onClick={() => setRules(true)}>Alert rules</button>
          <button className="btn small" onClick={() => setYaml(true)}>Import scans.yaml</button>
          <button className="btn small" onClick={() => setSettings(true)}>Settings</button>
        </div>
      </div>
      {st.isPending ? <Loading what="Loading monitor status" /> : st.isError ? <ErrorState error={st.error} retry={() => st.refetch()} /> : d && runner && (
        <div className="monitor-grid">
          <div><div className="card-label">State</div>
            <span className={`badge ${runner.cls}`}><span aria-hidden>{runner.icon}</span> {d.enabled ? runner.text : "Switched off"}</span>
            {d.runner.detail && <div className="muted-text small">{d.runner.detail}</div>}
          </div>
          <div><div className="card-label">Schedule</div><div className="small">{d.schedule}</div>
            <div className="muted-text small">{d.window_open ? (d.runner.next_pass_at ? `Next pass ${dateTime(d.runner.next_pass_at)}` : "In the window now") : `${d.why_closed ?? ""} ${d.next_window_open ? "Next: " + dateTime(d.next_window_open) : ""}`}</div>
          </div>
          <div><div className="card-label">Last pass</div>
            {d.last_run ? <>
              <span className={`badge ${d.last_run.ok ? "ok" : "bad"}`}>{d.last_run.ok ? "✓ OK" : "⚠ Failed"}</span>{" "}
              <span className="small">{dateTime(d.last_run.started_at)} · {d.last_run.seconds?.toFixed(1) ?? "…"} s · {d.last_run.positions} positions · {d.last_run.events} events</span>
              {d.last_run.errors.length > 0 && <details className="small"><summary>{d.last_run.errors.length} problem(s)</summary><ul>{d.last_run.errors.map((e, i) => <li key={i}>{e}</li>)}</ul></details>}
            </> : <span className="muted-text small">No pass yet. Press Run now, or wait for the schedule.</span>}
          </div>
          <div><div className="card-label">Quotes</div>
            <div className="small">{d.quotes.provider}{d.quotes.synthetic ? " (synthetic demo data)" : d.quotes.delay_minutes ? `, delayed ~${d.quotes.delay_minutes} min` : ""}</div>
            <div className="muted-text small">Positions older than {Math.round(d.stale_after_seconds / 60)} min show Stale data.</div>
          </div>
          <BrowserNotifications />
          {d.note && <div className="wide-cell"><span className="badge warn">⚠</span> <span className="small">{d.note}</span></div>}
        </div>
      )}
      <Modal open={settings} onOpenChange={setSettings} title="Monitor settings" wide><MonitorSettingsForm onDone={() => setSettings(false)} /></Modal>
      <Modal open={yaml} onOpenChange={setYaml} title="Import positions from scans.yaml" wide><YamlImportDialog accountId={accountId} onDone={() => setYaml(false)} /></Modal>
      <Modal open={rules} onOpenChange={setRules} title="Alert rules" wide><AlertRulesList accountId={accountId} /></Modal>
    </section>
  );
}

function BrowserNotifications() {
  const [on, setOn] = useState(browserNotificationsOn());
  const sup = notificationSupport();
  const toggle = async () => {
    if (on) { setBrowserNotifications(false); setOn(false); return; }
    let perm = sup;
    if (sup === "default") perm = await Notification.requestPermission();
    if (perm === "granted") { setBrowserNotifications(true); setOn(true); }
  };
  const why = sup === "unsupported" ? "This browser doesn't support notifications." : sup === "insecure" ? "Browsers allow notifications only over HTTPS or on localhost (the Tailscale https:// link works)." : sup === "denied" ? "Notifications are blocked for this site in the browser's settings." : null;
  return (
    <div><div className="card-label">This browser</div>
      <label className="check small"><input type="checkbox" checked={on && sup === "granted"} disabled={!!why} onChange={toggle} /> Notifications for warnings and critical events</label>
      {why && <div className="muted-text small">{why}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ events
const TYPE_LABEL: Record<string, string> = {
  POSITION_STATUS_CHANGED: "Status change", POSITION_STATUS_REMINDER: "Reminder", ALERT_TRIGGERED: "Alert", ALERT_REMINDER: "Alert reminder",
  ALERT_CLEARED: "Alert cleared", EXPIRATION_WARNING: "Expiration", EARNINGS_WARNING: "Earnings", LIQUIDITY_WARNING: "Liquidity",
  CONTRACT_STATUS_CHANGED: "Contract", CONTRACT_ADDED: "Contract", CONTRACT_ARCHIVED: "Contract", MONITOR_ERROR: "Monitor",
  MONITOR_RECOVERED: "Monitor", POSITION_OPENED: "Recorded", POSITION_EDITED: "Edited", POSITION_CORRECTED: "Corrected",
  POSITION_CLOSED: "Closed", POSITION_ARCHIVED: "Archived", MONITORING_CHANGED: "Monitoring", MONITOR_SETTINGS_CHANGED: "Settings", IMPORT: "Import",
};

export function EventFeed({ accountId, positionId, title = true }: { accountId?: string; positionId?: string; title?: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [unacked, setUnacked] = useState(!positionId);
  const [sev, setSev] = useState<"all" | Severity>("all");
  const [size, setSize] = useState(25);
  const [confirmAll, setConfirmAll] = useState(false);
  const interval = useRefetchInterval();
  const ev = useQuery({
    queryKey: ["events", accountId, positionId, unacked, sev, size],
    queryFn: () => api.events({ account_id: accountId, position_id: positionId, unacknowledged: unacked || undefined,
                                severity: sev === "all" ? undefined : [sev], page_size: size }),
    refetchInterval: interval,
  });
  const done = () => { qc.invalidateQueries({ queryKey: ["events"] }); qc.invalidateQueries({ queryKey: ["monitor-status"] }); };
  const ack = useMutation<EventItem, ApiError, string>({ mutationFn: (id) => api.ackEvent(id), onSuccess: done });
  const ackAll = useMutation<{ acknowledged: number }, ApiError, void>({
    mutationFn: () => api.ackEvents({ up_to_seq: ev.data!.latest_seq, position_id: positionId }),
    onSuccess: (r) => { done(); setConfirmAll(false); toast(`Acknowledged ${r.acknowledged} event(s)`); },
  });
  const items = ev.data?.items ?? [];
  const open = items.filter((e) => e.requires_ack && !e.acknowledged_at).length;
  return (
    <section className="panel" aria-labelledby={`ev-h-${positionId ?? "all"}`}>
      <div className="panel-head">
        {title ? <h2 id={`ev-h-${positionId ?? "all"}`}><TermLabel k="events" /></h2> : <h3 id={`ev-h-${positionId ?? "all"}`}>Events</h3>}
        <div className="panel-tools">
          <label className="check small"><input type="checkbox" checked={unacked} onChange={(e) => setUnacked(e.target.checked)} /> Needs acknowledging only</label>
          <select aria-label="Severity" value={sev} onChange={(e) => setSev(e.target.value as typeof sev)}>
            <option value="all">All severities</option><option value="critical">Critical</option><option value="warning">Warning</option><option value="info">Info</option>
          </select>
          {open > 0 && <button className="btn small" onClick={() => setConfirmAll(true)}>Acknowledge all</button>}
        </div>
      </div>
      {ev.isPending ? <Loading what="Loading events" /> : ev.isError ? <ErrorState error={ev.error} retry={() => ev.refetch()} />
        : items.length === 0 ? <Empty>{unacked ? "Nothing needs acknowledging." : "No events yet."}</Empty> : (
        <>
          <ol className="events">{items.map((e) => <EventRow key={e.id} e={e} onAck={() => ack.mutate(e.id)} busy={ack.isPending && ack.variables === e.id} showPosition={!positionId} />)}</ol>
          {ev.data!.total > items.length && <button className="btn small ghost" onClick={() => setSize(size + 50)}>Show more ({ev.data!.total - items.length} older)</button>}
        </>
      )}
      <ConfirmDialog open={confirmAll} onOpenChange={setConfirmAll} title="Acknowledge all?"
        body={<><p>Marks every warning and critical event{positionId ? " of this position" : ""} as seen. Nothing else changes.</p><FieldErrors error={ackAll.error} /></>}
        confirmLabel="Acknowledge all" busy={ackAll.isPending} onConfirm={() => ackAll.mutate()} />
    </section>
  );
}

function notifyText(e: EventItem): string {
  switch (e.notify_state) {
    case "sent": return "desktop notification sent";
    case "failed": return "desktop notification failed" + (e.notify_detail ? `: ${e.notify_detail}` : "");
    case "unavailable": return "no desktop notifier on the server (shown in browsers)";
    case "browser": return "shown in open dashboards";
    case "suppressed": return "not notified: " + (e.notify_detail ?? "settings");
    case "pending": return "notifying…";
    default: return "";
  }
}

function EventRow({ e, onAck, busy, showPosition }: { e: EventItem; onAck: () => void; busy: boolean; showPosition: boolean }) {
  const rules = (e.source?.rules ?? (e.source?.rule ? [e.source.rule] : null)) as RuleResult[] | null;
  const needs = e.requires_ack && !e.acknowledged_at;
  return (
    <li className={`event sev-${e.severity} ${needs ? "unacked" : ""}`}>
      <div className="event-head">
        <SeverityBadge s={e.severity} />
        <span className="muted-text small">{TYPE_LABEL[e.type] ?? e.type}</span>
        <strong className="event-title">{e.title}</strong>
        <span className="muted-text small" title={e.occurred_at}>{dateTime(e.occurred_at)}</span>
        {needs ? <button className="btn small" onClick={onAck} disabled={busy}>{busy ? "…" : "Acknowledge"}</button>
          : e.acknowledged_at ? <span className="muted-text small">✓ acknowledged {dateTime(e.acknowledged_at)}</span> : null}
      </div>
      <p className="event-msg">{e.message}</p>
      <div className="event-meta small muted-text">
        {e.symbol && <span className="mono">{e.symbol}</span>}
        {showPosition && e.position_id && <a href={`#/positions/${e.position_id}`} onClick={(x) => { x.preventDefault(); navigate(`/positions/${e.position_id}`); }}>open position</a>}
        {notifyText(e) && <span>{notifyText(e)}</span>}
        {e.ack_note && <span>note: {e.ack_note}</span>}
        {(e.alert_rule_id || e.contract_id || typeof e.source?.plan_id === "string" || (e.position_id && e.type !== "NEWS")) &&
          <a href={analysisHref("event", e.id)}>historical analysis</a>}
      </div>
      {rules && rules.length > 0 && (
        <details className="small"><summary>Source data</summary><RuleTable rules={rules} /></details>
      )}
    </li>
  );
}

export function RuleTable({ rules }: { rules: RuleResult[] }) {
  const res = (r: RuleResult) => r.result === true ? <span className="badge warn">✓ holds</span> : r.result === false ? <span className="badge muted">✗ no</span>
    : <Tip text={r.reason ?? "unknown"}><span className="badge bad" tabIndex={0}>? unknown</span></Tip>;
  return (
    <table className="table compact">
      <thead><tr><th>Kind</th><th>Rule</th><th>Result</th><th>Values</th></tr></thead>
      <tbody>{rules.map((r, i) => (
        <tr key={i}><td>{r.kind}</td><td className="mono">{r.kind === "system" ? r.reason : r.rule}</td><td>{res(r)}</td>
          <td className="mono small">{Object.entries(r.values).map(([k, v]) => `${k}=${typeof v === "number" ? +v.toFixed(4) : v}`).join(", ") || "—"}{r.result === null && r.reason ? <div className="warn-text">{r.reason}</div> : null}</td></tr>))}</tbody>
    </table>
  );
}

// ------------------------------------------------------------------ alert rules
export function AlertRulesList({ accountId, positionId, contractId }: { accountId?: string; positionId?: string; contractId?: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<AlertRule | null>(null);
  const [archiving, setArchiving] = useState<AlertRule | null>(null);
  const list = useQuery({ queryKey: ["alert-rules", accountId, positionId, contractId], queryFn: () => api.alertRules({ account_id: accountId, position_id: positionId, contract_id: contractId }) });
  const toggle = useMutation<AlertRule, ApiError, AlertRule>({
    mutationFn: (r) => api.updateAlertRule(r.id, { version: r.version, enabled: !r.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alert-rules"] }),
    onError: (e) => toast(e.message, "bad"),
  });
  const archive = useMutation<AlertRule, ApiError, AlertRule>({
    mutationFn: (r) => api.archiveAlertRule(r.id, r.version),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["alert-rules"] }); setArchiving(null); toast(`Archived ${r.name}`); },
  });
  return (
    <div>
      <p className="muted-text small">An alert fires once when its condition becomes true (after the confirmation passes), re-arms only after the condition clears and the re-arm time has passed, and repeats only if you set a repeat interval.</p>
      <div className="actions-left"><button className="btn small primary" onClick={() => setAdding(true)}>+ Add alert rule</button></div>
      {list.isPending ? <Loading /> : list.isError ? <ErrorState error={list.error} /> : list.data.length === 0 ? <Empty>No alert rules.</Empty> : (
        <table className="table compact">
          <thead><tr><th>Name</th><th>On</th><th>Condition</th><th>Severity</th><th>State</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{list.data.map((r) => (
            <tr key={r.id}>
              <td>{r.name}</td>
              <td className="small">{r.target_type === "symbol" ? <span className="mono">{r.symbol}</span> : r.target_type}</td>
              <td className="mono small">{r.condition}{r.timeframe !== "1d" ? ` (${r.timeframe} bars)` : ""}</td>
              <td><SeverityBadge s={r.severity} /></td>
              <td className="small">{!r.enabled ? <span className="badge muted">⏸ off</span> : !r.state ? <span className="muted-text">not checked yet</span>
                : r.state.active ? <span className="badge warn">⚑ triggered {dateTime(r.state.last_fired_at)}</span>
                : r.state.last?.result === null ? <Tip text={r.state.last?.reason ?? ""}><span className="badge bad" tabIndex={0}>? unknown</span></Tip>
                : <span className="badge ok">● armed</span>}</td>
              <td className="row-actions">
                <button className="btn ghost small" onClick={() => toggle.mutate(r)}>{r.enabled ? "Pause" : "Resume"}</button>
                <a className="btn ghost small" href={analysisHref("alert_rule", r.id)}>History</a>
                <button className="btn ghost small" onClick={() => setEditing(r)}>Edit</button>
                <button className="btn ghost small" onClick={() => setArchiving(r)}>Archive</button>
              </td>
            </tr>))}</tbody>
        </table>
      )}
      <Modal open={adding} onOpenChange={setAdding} title="Add alert rule" wide>
        <AlertRuleForm accountId={accountId} positionId={positionId} contractId={contractId} onDone={() => setAdding(false)} />
      </Modal>
      <Modal open={!!editing} onOpenChange={(o) => !o && setEditing(null)} title={`Edit ${editing?.name ?? ""}`} wide>
        {editing && <AlertRuleForm existing={editing} onDone={() => setEditing(null)} />}
      </Modal>
      <ConfirmDialog open={!!archiving} onOpenChange={(o) => !o && setArchiving(null)} title="Archive this alert rule?"
        body={<p>{archiving?.name} stops being checked. Its past events stay in the feed.</p>} confirmLabel="Archive" danger
        busy={archive.isPending} onConfirm={() => archiving && archive.mutate(archiving)} />
    </div>
  );
}
