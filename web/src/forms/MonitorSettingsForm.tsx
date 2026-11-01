import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { ApiError, api } from "../api/client";
import type { MonitorSettings, Severity } from "../api/types";
import { ErrorState, FieldErrors, Loading, useToast } from "../components/ui";
import { RulesEditor } from "./RulesEditor";

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

export function MonitorSettingsForm({ onDone }: { onDone: () => void }) {
  const q = useQuery<MonitorSettings, ApiError>({ queryKey: ["monitor-settings"], queryFn: api.monitorSettings });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} retry={() => q.refetch()} />;
  return <Editor initial={q.data} onDone={onDone} />;
}

function Editor({ initial, onDone }: { initial: MonitorSettings; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [f, setF] = useState<MonitorSettings>(initial);
  const [badRules, setBadRules] = useState(false);
  const [muted, setMuted] = useState(initial.notifications.muted_symbols.join(", "));
  const [warnDays, setWarnDays] = useState(initial.expiration_warning_days.join(", "));
  const set = (p: Partial<MonitorSettings>) => setF((x) => ({ ...x, ...p }));
  const sch = (p: Partial<MonitorSettings["schedule"]>) => setF((x) => ({ ...x, schedule: { ...x.schedule, ...p } }));
  const nt = (p: Partial<MonitorSettings["notifications"]>) => setF((x) => ({ ...x, notifications: { ...x.notifications, ...p } }));
  const onInvalid = useCallback((b: boolean) => setBadRules(b), []);
  const save = useMutation<MonitorSettings, ApiError>({
    mutationFn: () => api.saveMonitorSettings({
      ...f, default_rules: { exit_when: f.default_rules.exit_when.filter((x) => x.trim()), action_when: f.default_rules.action_when.filter((x) => x.trim()),
                             watch_when: f.default_rules.watch_when.filter((x) => x.trim()) },
      notifications: { ...f.notifications, muted_symbols: muted.split(/[,\s]+/).filter(Boolean) },
      expiration_warning_days: warnDays.split(/[,\s]+/).filter(Boolean).map(Number).filter((n) => Number.isInteger(n) && n >= 0),
    }),
    onSuccess: (s) => { qc.setQueryData(["monitor-settings"], s); qc.invalidateQueries({ queryKey: ["monitor-status"] }); qc.invalidateQueries({ queryKey: ["position-monitor"] }); toast("Monitor settings saved"); onDone(); },
  });
  const n = (v: string, min: number) => Math.max(min, Number.isFinite(+v) ? Math.round(+v) : min);
  return (
    <form className="form" noValidate onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <label className="check"><input type="checkbox" checked={f.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> Monitoring on</label>
      <fieldset><legend>Schedule</legend>
        <div className="grid-4">
          <label>Start<input value={f.schedule.start} onChange={(e) => sch({ start: e.target.value })} placeholder="09:30" /></label>
          <label>End<input value={f.schedule.end} onChange={(e) => sch({ end: e.target.value })} placeholder="16:00" /></label>
          <label>Time zone<input value={f.schedule.timezone} onChange={(e) => sch({ timezone: e.target.value })} /></label>
          <label>Every (seconds)<input type="number" min={15} max={3600} value={f.schedule.every_seconds} onChange={(e) => sch({ every_seconds: n(e.target.value, 15) })} /></label>
        </div>
        <div className="days">{DAYS.map((d) => (
          <label key={d} className="check"><input type="checkbox" checked={f.schedule.days.includes(d)}
            onChange={(e) => sch({ days: e.target.checked ? DAYS.filter((x) => x === d || f.schedule.days.includes(x)) : f.schedule.days.filter((x) => x !== d) })} />{d}</label>))}
          <label className="check"><input type="checkbox" checked={f.schedule.skip_market_holidays} onChange={(e) => sch({ skip_market_holidays: e.target.checked })} /> Skip US market holidays and stop at early closes</label>
        </div>
      </fieldset>
      <fieldset><legend>Status changes and repeats</legend>
        <div className="grid-4">
          <label>Confirm a new status after (passes)<input type="number" min={1} max={20} value={f.confirm_passes} onChange={(e) => set({ confirm_passes: n(e.target.value, 1) })} /></label>
          <label>Repeat Exit / Action every (min, 0 = no)<input type="number" min={0} max={1440} value={f.repeat_urgent_minutes} onChange={(e) => set({ repeat_urgent_minutes: n(e.target.value, 0) })} /></label>
          <label>Re-arm warnings after (min)<input type="number" min={0} max={1440} value={f.rearm_minutes} onChange={(e) => set({ rearm_minutes: n(e.target.value, 0) })} /></label>
          <label>Stale after (seconds without a check)<input type="number" min={60} max={86400} value={f.stale_after_seconds} onChange={(e) => set({ stale_after_seconds: n(e.target.value, 60) })} /></label>
        </div>
        <p className="muted-text small">Stale data always takes effect at once; any other new status must be seen on this many passes in a row.</p>
      </fieldset>
      <fieldset><legend>Default rules for positions that don't have their own</legend>
        <div className="grid-4">
          <label>Bars for studies<select value={f.timeframe} onChange={(e) => set({ timeframe: e.target.value })}>{["1m", "5m", "15m", "30m", "1h", "1d", "1wk"].map((t) => <option key={t}>{t}</option>)}</select></label>
          <label className="check"><input type="checkbox" checked={f.completed_bars_only} onChange={(e) => set({ completed_bars_only: e.target.checked })} /> Completed bars only</label>
          <label className="check"><input type="checkbox" checked={f.expiry_action} onChange={(e) => set({ expiry_action: e.target.checked })} /> Action required on expiration day</label>
          <label>Assignment-risk window (days)<input type="number" min={0} max={60} value={f.assignment_risk_days} onChange={(e) => set({ assignment_risk_days: n(e.target.value, 0) })} /></label>
          <label>Expiration warnings (days before)<input value={warnDays} onChange={(e) => setWarnDays(e.target.value)} placeholder="7, 1" /></label>
          <label>Liquidity warning above spread %<input type="number" min={1} max={500} value={f.liquidity_max_spread_pct} onChange={(e) => set({ liquidity_max_spread_pct: Math.max(1, +e.target.value || 1) })} /></label>
          <label>Keep every snapshot for (days)<input type="number" min={1} max={3650} value={f.retention_full_days} onChange={(e) => set({ retention_full_days: n(e.target.value, 1) })} /></label>
        </div>
        <RulesEditor value={f.default_rules} onChange={(r) => set({ default_rules: r })} onInvalid={onInvalid} />
      </fieldset>
      <fieldset><legend>Notifications</legend>
        <div className="grid-4">
          <label className="check"><input type="checkbox" checked={f.notifications.desktop} onChange={(e) => nt({ desktop: e.target.checked })} /> Desktop (notify-send on the server)</label>
          <label>Notify from<select value={f.notifications.min_severity} onChange={(e) => nt({ min_severity: e.target.value as Severity })}>
            <option value="warning">Warning and critical</option><option value="critical">Critical only</option></select></label>
          <label className="check"><input type="checkbox" checked={f.notifications.quiet_hours} onChange={(e) => nt({ quiet_hours: e.target.checked })} /> Quiet hours</label>
          <label className="check"><input type="checkbox" checked={f.notifications.critical_during_quiet_hours} onChange={(e) => nt({ critical_during_quiet_hours: e.target.checked })} /> Critical still notifies in quiet hours</label>
          <label>Quiet from<input value={f.notifications.quiet_start} onChange={(e) => nt({ quiet_start: e.target.value })} /></label>
          <label>Quiet until<input value={f.notifications.quiet_end} onChange={(e) => nt({ quiet_end: e.target.value })} /></label>
          <label>Muted symbols<input value={muted} onChange={(e) => setMuted(e.target.value)} placeholder="SPY, QQQ" /></label>
        </div>
        <p className="muted-text small">Muted and quiet-hour events still appear in the feed and still need acknowledging; they just don't pop up. Browser notifications are switched on per browser in the Monitor panel.</p>
      </fieldset>
      <FieldErrors error={save.error} />
      <div className="actions"><button className="btn primary" type="submit" disabled={badRules || save.isPending}>{save.isPending ? "Saving…" : "Save settings"}</button></div>
    </form>
  );
}
