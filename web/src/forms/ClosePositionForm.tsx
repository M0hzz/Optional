// Close a position: a closing fill (or "expired worthless") for every leg. Realized P&L is computed by the API.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Position } from "../api/types";
import { FieldErrors, useToast } from "../components/ui";
import { moneyInputError, price } from "../lib/format";
import { localToIso } from "./LegEditor";

export function ClosePositionForm({ position, onDone }: { position: Position; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [rows, setRows] = useState(position.legs.map((l) => ({ leg_id: l.id, close_price: "", close_fees: "0", expired: false })));
  const [closedAt, setClosedAt] = useState("");
  const [confirm, setConfirm] = useState(false);
  const set = (i: number, patch: Partial<(typeof rows)[number]>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const errs = rows.map((r) => (r.expired ? moneyInputError(r.close_fees) : (!r.close_price.trim() ? "Required" : moneyInputError(r.close_price) ?? moneyInputError(r.close_fees))));
  const close = useMutation<Position, ApiError>({
    mutationFn: () => api.closePosition(position.id, {
      version: position.version, closed_at: localToIso(closedAt),
      legs: rows.map((r) => ({ leg_id: r.leg_id, close_price: r.expired ? null : r.close_price.trim(), close_fees: r.close_fees.trim() || "0", expired_worthless: r.expired })),
    }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["positions"] }); qc.invalidateQueries({ queryKey: ["overview"] });
      qc.setQueryData(["position", p.id], p);
      toast(`Closed ${p.name}`); onDone();
    },
  });
  return (
    <form className="form" onSubmit={(e) => { e.preventDefault(); setConfirm(true); }}>
      <p className="muted-text">Enter the price each leg was closed at (per share), or tick "expired worthless". Fees are the closing fees.</p>
      <table className="table compact">
        <thead><tr><th>Leg</th><th>Opened at</th><th>Closing price</th><th>Closing fees</th><th>Expired worthless</th></tr></thead>
        <tbody>
          {position.legs.map((l, i) => (
            <tr key={l.id}>
              <td>{l.side === "BUY" ? "Long" : "Short"} {l.quantity} {l.asset_type === "STOCK" ? "shares" : `${price(l.strike)} ${l.option_right?.toLowerCase()} ${l.expiration}`}</td>
              <td className="mono">{price(l.fill_price) ?? "—"}</td>
              <td><input aria-label={`Closing price leg ${i + 1}`} inputMode="decimal" value={rows[i].close_price} disabled={rows[i].expired}
                         onChange={(e) => set(i, { close_price: e.target.value })} aria-invalid={!!errs[i]} />
                  {errs[i] && <span className="field-error">{errs[i]}</span>}</td>
              <td><input aria-label={`Closing fees leg ${i + 1}`} inputMode="decimal" value={rows[i].close_fees}
                         onChange={(e) => set(i, { close_fees: e.target.value })} /></td>
              <td>{l.asset_type === "OPTION" && <input type="checkbox" aria-label={`Leg ${i + 1} expired worthless`} checked={rows[i].expired}
                         onChange={(e) => set(i, { expired: e.target.checked })} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <label>Closed at <span className="muted-text small">default: now</span>
        <input type="datetime-local" value={closedAt} onChange={(e) => setClosedAt(e.target.value)} />
      </label>
      <FieldErrors error={close.error} />
      {confirm ? (
        <div className="confirm-inline" role="alertdialog" aria-label="Confirm close">
          <span>Close <strong>{position.name}</strong>? Closed positions can only be corrected with a reason, and the original record is kept.</span>
          <button type="button" className="btn" onClick={() => setConfirm(false)}>Cancel</button>
          <button type="button" className="btn danger" disabled={close.isPending} onClick={() => close.mutate()}>{close.isPending ? "Closing…" : "Yes, close it"}</button>
        </div>
      ) : (
        <div className="actions"><button type="submit" className="btn primary" disabled={errs.some(Boolean)}>Close position…</button></div>
      )}
    </form>
  );
}
