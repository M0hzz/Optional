import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Contract, ContractStatus } from "../api/types";
import { FieldErrors, useToast } from "../components/ui";
import { moneyInputError } from "../lib/format";

export const CONTRACT_STATUSES: { value: ContractStatus; label: string; icon: string }[] = [
  { value: "RESEARCHING", label: "Researching", icon: "🔍" },
  { value: "WAITING_FOR_CONFIRMATION", label: "Waiting for confirmation", icon: "⏳" },
  { value: "ENTRY_QUALIFIED", label: "Entry qualified", icon: "✓" },
  { value: "ENTRY_CONDITION_LOST", label: "Entry condition lost", icon: "✕" },
  { value: "IN_POSITION", label: "In position", icon: "◉" },
  { value: "EXPIRED", label: "Expired", icon: "⌛" },
  { value: "ARCHIVED", label: "Archived", icon: "▣" },
];

export function ContractForm({ accountId, existing, onDone }: { accountId: string | null; existing?: Contract; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [f, setF] = useState({
    underlying: existing?.underlying ?? "", option_right: existing?.option_right ?? "CALL",
    strike: existing?.strike.replace(/\.?0+$/, "") ?? "", expiration: existing?.expiration ?? "",
    status: existing?.status ?? "RESEARCHING", target_entry: existing?.target_entry?.replace(/\.?0+$/, "") ?? "",
    invalidation: existing?.invalidation ?? "", alert_conditions: existing?.alert_conditions ?? "", notes: existing?.notes ?? "",
    research_only: existing ? existing.account_id === null : false,
  });
  const set = (p: Partial<typeof f>) => setF({ ...f, ...p });
  const errs: Record<string, string | null> = {
    strike: existing ? null : (!f.strike ? "Required" : moneyInputError(f.strike)),
    target_entry: moneyInputError(f.target_entry),
    expiration: existing ? null : (!f.expiration ? "Required" : null),
    underlying: existing ? null : (/^[A-Za-z][A-Za-z0-9.-]{0,9}$/.test(f.underlying.trim()) ? null : "Ticker like SPY"),
  };
  const save = useMutation<Contract, ApiError>({
    mutationFn: () => existing
      ? api.updateContract(existing.id, { version: existing.version, status: f.status === "ARCHIVED" ? undefined : f.status,
          target_entry: f.target_entry.trim() || null, invalidation: f.invalidation || null,
          alert_conditions: f.alert_conditions || null, notes: f.notes || null })
      : api.createContract({ account_id: f.research_only ? null : accountId, underlying: f.underlying.trim(), option_right: f.option_right,
          strike: f.strike.trim(), expiration: f.expiration, status: f.status, target_entry: f.target_entry.trim() || null,
          invalidation: f.invalidation || null, alert_conditions: f.alert_conditions || null, notes: f.notes || null }),
    onSuccess: (c) => { qc.invalidateQueries({ queryKey: ["contracts"] }); toast(`${existing ? "Updated" : "Watching"} ${c.occ_symbol}`); onDone(); },
  });
  return (
    <form className="form" onSubmit={(e) => { e.preventDefault(); save.mutate(); }} noValidate>
      <div className="grid-4">
        <label>Underlying<input value={f.underlying} disabled={!!existing} onChange={(e) => set({ underlying: e.target.value })} aria-invalid={!!errs.underlying} />
          {errs.underlying && f.underlying && <span className="field-error">{errs.underlying}</span>}</label>
        <label>Call/put<select value={f.option_right} disabled={!!existing} onChange={(e) => set({ option_right: e.target.value as "CALL" | "PUT" })}>
          <option value="CALL">Call</option><option value="PUT">Put</option></select></label>
        <label>Strike<input inputMode="decimal" value={f.strike} disabled={!!existing} onChange={(e) => set({ strike: e.target.value })} aria-invalid={!!errs.strike} />
          {errs.strike && f.strike && <span className="field-error">{errs.strike}</span>}</label>
        <label>Expiration<input type="date" value={f.expiration} disabled={!!existing} onChange={(e) => set({ expiration: e.target.value })} /></label>
      </div>
      <div className="grid-3">
        <label>Status<select value={f.status} onChange={(e) => set({ status: e.target.value as ContractStatus })}>
          {CONTRACT_STATUSES.filter((s) => s.value !== "ARCHIVED").map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}</select></label>
        <label>Target entry price <span className="muted-text small">per share</span>
          <input inputMode="decimal" value={f.target_entry} onChange={(e) => set({ target_entry: e.target.value })} aria-invalid={!!errs.target_entry} />
          {errs.target_entry && <span className="field-error">{errs.target_entry}</span>}</label>
        {!existing && <label className="check"><input type="checkbox" checked={f.research_only} onChange={(e) => set({ research_only: e.target.checked })} />Research only (not tied to an account)</label>}
      </div>
      <label>Invalidation <span className="muted-text small">what would make you drop the idea</span>
        <input value={f.invalidation} maxLength={2000} onChange={(e) => set({ invalidation: e.target.value })} /></label>
      <label>Entry conditions <span className="muted-text small">one per line, e.g. <code>ask &lt;= target_entry</code>, <code>oi &gt;= 500</code>, <code>spread_pct &lt; 10</code>. Checked every monitoring pass; while the status is Waiting / Entry qualified / Entry condition lost, it changes automatically when all of them hold (or stop holding).</span>
        <textarea rows={3} className="mono" value={f.alert_conditions} maxLength={2000} onChange={(e) => set({ alert_conditions: e.target.value })} /></label>
      <label>Notes<textarea rows={2} value={f.notes} maxLength={5000} onChange={(e) => set({ notes: e.target.value })} /></label>
      <FieldErrors error={save.error} />
      <div className="actions"><button className="btn primary" type="submit" disabled={Object.values(errs).some(Boolean) || save.isPending}>
        {save.isPending ? "Saving…" : existing ? "Save" : "Watch contract"}</button></div>
    </form>
  );
}
