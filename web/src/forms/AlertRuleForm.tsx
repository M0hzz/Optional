import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { AlertRule, Severity } from "../api/types";
import { FieldErrors, useToast } from "../components/ui";
import { ConditionInput, Vocab } from "./RulesEditor";

type Target = "position" | "contract" | "symbol";
const PRESETS: Record<Target, { label: string; cond: string }[]> = {
  symbol: [{ label: "Price below…", cond: "price < 0" }, { label: "Price above…", cond: "price > 0" },
           { label: "IV rich", cond: "atm_iv > 0.4" }, { label: "RSI oversold", cond: "rsi(14) < 30" }],
  position: [{ label: "P&L % at least…", cond: "pnl_pct >= 50" }, { label: "Loss of max loss", cond: "loss_pct_of_max >= 50" },
             { label: "Near short strike", cond: "short_strike_otm_pct < 2" }, { label: "Days left", cond: "dte <= 7" }],
  contract: [{ label: "Ask at target", cond: "ask <= target_entry" }, { label: "Wide spread", cond: "spread_pct > 15" },
             { label: "Delta reached", cond: "abs_delta >= 0.30" }],
};

export function AlertRuleForm({ accountId, positionId, contractId, existing, onDone }: {
  accountId?: string; positionId?: string; contractId?: string; existing?: AlertRule; onDone: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const fixed: Target | null = existing ? existing.target_type : positionId ? "position" : contractId ? "contract" : null;
  const [f, setF] = useState({
    name: existing?.name ?? "", target: (fixed ?? "symbol") as Target, symbol: existing?.symbol ?? "",
    position_id: existing?.position_id ?? positionId ?? "", contract_id: existing?.contract_id ?? contractId ?? "",
    condition: existing?.condition ?? "", timeframe: existing?.timeframe ?? "1d", severity: (existing?.severity ?? "warning") as Severity,
    notify: existing?.notify ?? true, confirm_passes: existing?.confirm_passes ?? 1, rearm_minutes: existing?.rearm_minutes ?? 30,
    repeat_minutes: existing?.repeat_minutes ?? 0,
  });
  const [condErr, setCondErr] = useState<string | null>(null);
  const set = (p: Partial<typeof f>) => setF({ ...f, ...p });
  const positions = useQuery({ queryKey: ["positions", accountId, "OPEN"], queryFn: () => api.positions({ account_id: accountId, lifecycle: ["OPEN"], page_size: 200 }), enabled: !fixed && !!accountId });
  const contracts = useQuery({ queryKey: ["contracts", accountId], queryFn: () => api.contracts({ account_id: accountId, page_size: 200 }), enabled: !fixed && !!accountId });
  const save = useMutation<AlertRule, ApiError>({
    mutationFn: () => existing
      ? api.updateAlertRule(existing.id, { version: existing.version, name: f.name, condition: f.condition, timeframe: f.timeframe,
          severity: f.severity, notify: f.notify, confirm_passes: f.confirm_passes, rearm_minutes: f.rearm_minutes, repeat_minutes: f.repeat_minutes })
      : api.createAlertRule({ name: f.name, target_type: f.target, account_id: accountId ?? null,
          symbol: f.target === "symbol" ? f.symbol.trim() : null, position_id: f.target === "position" ? f.position_id : null,
          contract_id: f.target === "contract" ? f.contract_id : null, condition: f.condition, timeframe: f.timeframe,
          severity: f.severity, notify: f.notify, confirm_passes: f.confirm_passes, rearm_minutes: f.rearm_minutes, repeat_minutes: f.repeat_minutes }),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["alert-rules"] }); toast(`${existing ? "Saved" : "Added"} ${r.name}`); onDone(); },
  });
  const targetMissing = !existing && (f.target === "symbol" ? !/^[A-Za-z][A-Za-z0-9.-]{0,9}$/.test(f.symbol.trim())
    : f.target === "position" ? !f.position_id : !f.contract_id);
  return (
    <form className="form" noValidate onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <div className="grid-3">
        <label>Name<input value={f.name} maxLength={160} onChange={(e) => set({ name: e.target.value })} /></label>
        <label>On<select value={f.target} disabled={!!fixed} onChange={(e) => set({ target: e.target.value as Target, condition: "" })}>
          <option value="symbol">A symbol</option><option value="position">A position</option><option value="contract">A watched contract</option></select></label>
        {f.target === "symbol" && <label>Symbol<input value={f.symbol} disabled={!!existing} onChange={(e) => set({ symbol: e.target.value })} /></label>}
        {f.target === "position" && !fixed && <label>Position<select value={f.position_id} onChange={(e) => set({ position_id: e.target.value })}>
          <option value="">Choose…</option>{positions.data?.items.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}
        {f.target === "contract" && !fixed && <label>Contract<select value={f.contract_id} onChange={(e) => set({ contract_id: e.target.value })}>
          <option value="">Choose…</option>{contracts.data?.items.map((c) => <option key={c.id} value={c.id}>{c.occ_symbol}</option>)}</select></label>}
      </div>
      <div className="presets small">Start from: {PRESETS[f.target].map((p) => (
        <button type="button" key={p.label} className="btn ghost small" onClick={() => set({ condition: p.cond, name: f.name || p.label.replace("…", "") })}>{p.label}</button>))}</div>
      <ConditionInput value={f.condition} onChange={(v) => set({ condition: v })} target={f.target} label="Condition" onError={setCondErr} />
      <div className="grid-4">
        <label>Severity<select value={f.severity} onChange={(e) => set({ severity: e.target.value as Severity })}>
          <option value="info">Info (feed only)</option><option value="warning">Warning</option><option value="critical">Critical</option></select></label>
        <label>Bars for studies<select value={f.timeframe} onChange={(e) => set({ timeframe: e.target.value })}>
          {["1m", "5m", "15m", "30m", "1h", "1d", "1wk"].map((t) => <option key={t}>{t}</option>)}</select></label>
        <label>Confirm passes<input type="number" min={1} max={20} value={f.confirm_passes} onChange={(e) => set({ confirm_passes: Math.max(1, +e.target.value || 1) })} /></label>
        <label>Re-arm after (min)<input type="number" min={0} max={10080} value={f.rearm_minutes} onChange={(e) => set({ rearm_minutes: Math.max(0, +e.target.value || 0) })} /></label>
        <label>Repeat every (min, 0 = no)<input type="number" min={0} max={1440} value={f.repeat_minutes} onChange={(e) => set({ repeat_minutes: Math.max(0, +e.target.value || 0) })} /></label>
        <label className="check"><input type="checkbox" checked={f.notify} onChange={(e) => set({ notify: e.target.checked })} /> Notify</label>
      </div>
      <Vocab target={f.target} />
      <FieldErrors error={save.error} />
      <div className="actions"><button className="btn primary" type="submit" disabled={!f.name.trim() || !f.condition.trim() || !!condErr || targetMissing || save.isPending}>
        {save.isPending ? "Saving…" : existing ? "Save" : "Add alert rule"}</button></div>
    </form>
  );
}
