// CSV import: preview first (writes nothing), then commit atomically.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import type { ImportReport } from "../api/types";
import { FieldErrors, useToast } from "../components/ui";
import { money } from "../lib/format";

const MAX = 1_000_000;

export function ImportDialog({ accountId, onDone }: { accountId: string; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<"all_or_nothing" | "valid_only">("all_or_nothing");
  const key = useRef(crypto.randomUUID());
  const preview = useMutation<ImportReport, ApiError, File>({ mutationFn: (f) => api.importPreview(accountId, f) });
  const commit = useMutation({
    mutationFn: () => api.importCommit(accountId, file!, mode, key.current),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["positions"] }); qc.invalidateQueries({ queryKey: ["overview"] });
                        toast(`Imported ${r.imported.length} position(s)`); onDone(); },
  });
  const rep = preview.data;
  const tooBig = file && file.size > MAX;
  return (
    <div className="form">
      <p className="muted-text">One row per leg; rows sharing <code>position_ref</code> form one position. The schema is in docs/PORTFOLIO.md
        (or export your positions to see it). Nothing is saved until you import.</p>
      <label>CSV file (UTF-8, at most 1 MB)
        <input type="file" accept=".csv,text/csv" onChange={(e) => { const f = e.target.files?.[0] ?? null; setFile(f); preview.reset(); commit.reset(); key.current = crypto.randomUUID(); if (f && f.size <= MAX) preview.mutate(f); }} />
      </label>
      {tooBig && <div className="state error" role="alert">⚠ The file is larger than 1 MB.</div>}
      <FieldErrors error={preview.error} />
      {preview.isPending && <div className="state">Checking…</div>}
      {rep && (
        <>
          {rep.file_errors.map((e, i) => <div key={i} className="state error" role="alert">⚠ {e}</div>)}
          <table className="table compact">
            <caption className="sr-only">Positions found in the file</caption>
            <thead><tr><th>Ref</th><th>Symbol</th><th>Legs</th><th>Strategy</th><th>Max loss</th><th>Result</th></tr></thead>
            <tbody>{rep.positions.map((g) => (
              <tr key={g.position_ref}>
                <td>{g.position_ref}</td><td>{g.underlying}</td><td>{g.legs}</td><td>{g.strategy ?? "—"}</td>
                <td className="mono">{g.max_loss_state === "bounded" ? money(g.max_loss) : g.max_loss_state ?? "—"}</td>
                <td>{g.valid ? <span className="badge ok">✓ Ready</span> : <span className="badge bad">✕ {g.errors.join(" ")}</span>}
                    {g.warnings.length > 0 && <div className="muted-text small">⚠ {g.warnings.join(" ")}</div>}</td>
              </tr>))}</tbody>
          </table>
          {rep.rows.some((r) => r.errors.length) && (
            <details open><summary>Row errors</summary><ul>{rep.rows.filter((r) => r.errors.length).map((r) => <li key={r.row}>Row {r.row}: {r.errors.join("; ")}</li>)}</ul></details>
          )}
          <fieldset className="radio-row"><legend>If some positions are invalid</legend>
            <label className="check"><input type="radio" checked={mode === "all_or_nothing"} onChange={() => setMode("all_or_nothing")} />Import nothing</label>
            <label className="check"><input type="radio" checked={mode === "valid_only"} onChange={() => setMode("valid_only")} />Import only the valid positions (never part of a position)</label>
          </fieldset>
          <FieldErrors error={commit.error} />
          <div className="actions">
            <button className="btn primary" disabled={!file || commit.isPending || rep.file_errors.length > 0 || (mode === "all_or_nothing" && !rep.ok) || !rep.positions.some((g) => g.valid)}
                    onClick={() => commit.mutate()}>{commit.isPending ? "Importing…" : `Import ${rep.positions.filter((g) => g.valid).length} position(s)`}</button>
          </div>
        </>
      )}
    </div>
  );
}
