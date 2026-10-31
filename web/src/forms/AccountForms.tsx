import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Account, Snapshot } from "../api/types";
import { FieldErrors, useToast } from "../components/ui";
import { moneyInputError } from "../lib/format";
import { TermLabel } from "../components/ui";

export function CreateAccountForm({ onDone }: { onDone: (a: Account) => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [broker, setBroker] = useState("");
  const m = useMutation<Account, ApiError>({
    mutationFn: () => api.createAccount({ name: name.trim(), broker: broker.trim() || null }),
    onSuccess: (a) => { qc.invalidateQueries({ queryKey: ["accounts"] }); onDone(a); },
  });
  return (
    <form className="form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) m.mutate(); }}>
      <div className="grid-2">
        <label>Account name<input value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder="e.g. Brokerage cash account" autoFocus /></label>
        <label>Broker <span className="muted-text small">optional</span><input value={broker} maxLength={120} onChange={(e) => setBroker(e.target.value)} /></label>
      </div>
      <FieldErrors error={m.error} />
      <div className="actions"><button className="btn primary" disabled={!name.trim() || m.isPending}>Create account</button></div>
    </form>
  );
}

export function SnapshotForm({ account, onDone }: { account: Account; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const s = account.latest_snapshot;
  const strip = (v: string | null | undefined) => (v ? v.replace(/\.?0+$/, "") : "");
  const [f, setF] = useState({ total_value: strip(s?.total_value), cash: strip(s?.cash), buying_power: strip(s?.buying_power), notes: "" });
  const errs = { total_value: moneyInputError(f.total_value, true), cash: moneyInputError(f.cash, true), buying_power: moneyInputError(f.buying_power, true) };
  const m = useMutation<Snapshot, ApiError>({
    mutationFn: () => api.addSnapshot(account.id, { total_value: f.total_value.trim() || null, cash: f.cash.trim() || null,
                                                     buying_power: f.buying_power.trim() || null, notes: f.notes.trim() || null }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["accounts"] }); qc.invalidateQueries({ queryKey: ["overview"] }); toast("Account values recorded"); onDone(); },
  });
  return (
    <form className="form" onSubmit={(e) => { e.preventDefault(); m.mutate(); }}>
      <p className="muted-text">Copy these from your broker. They're recorded with today's date; earlier values are kept.</p>
      <div className="grid-3">
        <label><TermLabel k="totalValue" /><input inputMode="decimal" value={f.total_value} onChange={(e) => setF({ ...f, total_value: e.target.value })} aria-invalid={!!errs.total_value} />
          {errs.total_value && <span className="field-error">{errs.total_value}</span>}</label>
        <label><TermLabel k="cash" /><input inputMode="decimal" value={f.cash} onChange={(e) => setF({ ...f, cash: e.target.value })} aria-invalid={!!errs.cash} />
          {errs.cash && <span className="field-error">{errs.cash}</span>}</label>
        <label><TermLabel k="buyingPower" /><input inputMode="decimal" value={f.buying_power} onChange={(e) => setF({ ...f, buying_power: e.target.value })} aria-invalid={!!errs.buying_power} />
          {errs.buying_power && <span className="field-error">{errs.buying_power}</span>}</label>
      </div>
      <label>Notes<input value={f.notes} maxLength={500} onChange={(e) => setF({ ...f, notes: e.target.value })} /></label>
      <FieldErrors error={m.error} />
      <div className="actions"><button className="btn primary" disabled={Object.values(errs).some(Boolean) || m.isPending}>Record values</button></div>
    </form>
  );
}
