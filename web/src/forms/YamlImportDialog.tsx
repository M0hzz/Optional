import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { YamlRow } from "../api/types";
import { Empty, ErrorState, FieldErrors, Loading, useToast } from "../components/ui";

const ST: Record<YamlRow["status"], { cls: string; text: string }> = {
  new: { cls: "info", text: "◉ New" }, duplicate: { cls: "muted", text: "= Already imported" }, invalid: { cls: "bad", text: "⚠ Invalid" },
  imported: { cls: "ok", text: "✓ Imported" }, skipped: { cls: "warn", text: "⊘ Skipped" },
};

export function YamlImportDialog({ accountId, onDone }: { accountId: string; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [ack, setAck] = useState(false);
  const [key] = useState(() => crypto.randomUUID());
  const prev = useQuery<{ path: string; rows: YamlRow[] }, ApiError>({ queryKey: ["yaml-preview", accountId], queryFn: () => api.yamlPreview(accountId) });
  const commit = useMutation<{ imported: { id: string; name: string }[]; rows: YamlRow[] }, ApiError>({
    mutationFn: () => api.yamlCommit(accountId, ack, key),
    onSuccess: (r) => { ["positions", "overview", "events", "yaml-preview"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
                        toast(`Imported ${r.imported.length} position(s) from scans.yaml`); onDone(); },
  });
  if (prev.isPending) return <Loading what="Reading scans.yaml" />;
  if (prev.isError) return <ErrorState error={prev.error} retry={() => prev.refetch()} />;
  const rows = commit.data?.rows ?? prev.data.rows;
  const fresh = rows.filter((r) => r.status === "new");
  const needAck = fresh.some((r) => r.needs_risk_ack);
  return (
    <div className="form">
      <p className="small">Copies the <code>positions:</code> of the project's scans.yaml into this account, once, with their exit and watch rules. scans.yaml isn't changed; the YAML monitor skips imported positions from then on, so nothing alerts twice. The YAML has no opening date or fees: they import as now and $0. Edit them afterwards if you know them.</p>
      {rows.length === 0 ? <Empty>scans.yaml has no positions.</Empty> : (
        <table className="table compact">
          <thead><tr><th>Position</th><th>Strategy</th><th>Max loss</th><th>Rules</th><th>Status</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.index}><td>{r.name} <span className="muted-text mono small">{r.symbol}</span></td><td>{r.strategy ?? "—"}</td>
              <td>{r.max_loss ? (r.max_loss.state === "bounded" ? `$${Number(r.max_loss.value).toFixed(2)}` : r.max_loss.state) : "—"}</td>
              <td className="mono small">{[...r.rules.exit_when.map((x) => "exit: " + x), ...r.rules.watch_when.map((x) => "watch: " + x)].join("; ")}</td>
              <td><span className={`badge ${ST[r.status].cls}`}>{ST[r.status].text}</span>
                {[...r.errors, ...r.warnings].length > 0 && <ul className="small">{[...r.errors, ...r.warnings].map((e, i) => <li key={i}>{e}</li>)}</ul>}</td></tr>))}</tbody>
        </table>
      )}
      {needAck && <label className="check"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I understand some of these have unlimited or unknown maximum loss: import them too</label>}
      <FieldErrors error={commit.error} />
      <div className="actions">
        <button className="btn" onClick={onDone}>Cancel</button>
        <button className="btn primary" disabled={fresh.length === 0 || commit.isPending || !!commit.data} onClick={() => commit.mutate()}>
          {commit.isPending ? "Importing…" : `Import ${fresh.length} new position(s)`}</button>
      </div>
    </div>
  );
}
