// Editors for monitoring conditions. Conditions use the same expression language as scans.yaml and are
// checked by the server as you type (POST /monitor/validate), so a typo never reaches the monitor.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { RuleSet, Vocabulary } from "../api/types";

type Target = "position" | "contract" | "symbol";

export function useConditionCheck(text: string, target: Target): string | null {
  const [res, setRes] = useState<{ key: string; err: string | null } | null>(null);
  const t = text.trim();
  const key = `${target}|${t}`;
  useEffect(() => {
    if (!t) return;
    let live = true;
    const h = window.setTimeout(() => {
      api.validateCondition(t, target).then((r) => live && setRes({ key, err: r.valid ? null : r.error }))
        .catch(() => live && setRes({ key, err: null }));
    }, 300);
    return () => { live = false; window.clearTimeout(h); };
  }, [t, target, key]);
  return t && res?.key === key ? res.err : null;     // only the verdict for exactly this text
}

export function ConditionInput({ value, onChange, target, label, onError }: {
  value: string; onChange: (v: string) => void; target: Target; label: string; onError?: (e: string | null) => void;
}) {
  const err = useConditionCheck(value, target);
  useEffect(() => { onError?.(err); }, [err, onError]);
  return (
    <label>{label}
      <input className="mono" value={value} maxLength={300} onChange={(e) => onChange(e.target.value)} aria-invalid={!!err}
             placeholder={target === "contract" ? "ask <= target_entry" : target === "symbol" ? "price < 640" : "pnl_pct >= 50"} />
      {err && <span className="field-error">{err}</span>}
    </label>
  );
}

function ListEditor({ title, help, items, onChange, onErrors }: {
  title: string; help: string; items: string[]; onChange: (x: string[]) => void; onErrors: (n: number) => void;
}) {
  const [errs, setErrs] = useState<Record<number, string | null>>({});
  useEffect(() => { onErrors(Object.values(errs).filter(Boolean).length); }, [errs, onErrors]);
  return (
    <fieldset className="rule-list">
      <legend>{title}</legend>
      <p className="muted-text small">{help}</p>
      {items.map((x, i) => (
        <div key={i} className="rule-row">
          <ConditionInput value={x} target="position" label={`Rule ${i + 1}`}
            onChange={(v) => onChange(items.map((y, j) => (j === i ? v : y)))}
            onError={(e) => setErrs((m) => (m[i] === e ? m : { ...m, [i]: e }))} />
          <button type="button" className="btn ghost small" aria-label={`Remove rule ${i + 1}`}
            onClick={() => { onChange(items.filter((_, j) => j !== i)); setErrs({}); }}>✕</button>
        </div>
      ))}
      <button type="button" className="btn small ghost" onClick={() => onChange([...items, ""])} disabled={items.length >= 20}>+ Add rule</button>
    </fieldset>
  );
}

export function RulesEditor({ value, onChange, onInvalid }: { value: RuleSet; onChange: (r: RuleSet) => void; onInvalid: (bad: boolean) => void }) {
  const [bad, setBad] = useState({ exit: 0, action: 0, watch: 0 });
  useEffect(() => { onInvalid(bad.exit + bad.action + bad.watch > 0); }, [bad, onInvalid]);
  return (
    <div className="rules-editor">
      <ListEditor title="Exit when (any) → Exit condition met" help="For example pnl_pct >= 50 (up half), loss_pct_of_max >= 50 (lost half of the most it can lose), entry < 0 and dte <= 21 (sold premium near expiry)."
        items={value.exit_when} onChange={(x) => onChange({ ...value, exit_when: x })} onErrors={(n) => setBad((b) => (b.exit === n ? b : { ...b, exit: n }))} />
      <ListEditor title="Action required when (any)" help="Your own action checks. Built-in: an option leg expiring today or expired, and short options in the money near expiration."
        items={value.action_when} onChange={(x) => onChange({ ...value, action_when: x })} onErrors={(n) => setBad((b) => (b.action === n ? b : { ...b, action: n }))} />
      <ListEditor title="Watch when (any)" help="For example short_strike_otm_pct < 3, earnings_before_expiry, rsi(14) < 30."
        items={value.watch_when} onChange={(x) => onChange({ ...value, watch_when: x })} onErrors={(n) => setBad((b) => (b.watch === n ? b : { ...b, watch: n }))} />
      <Vocab target="position" />
    </div>
  );
}

export function Vocab({ target }: { target: Target }) {
  const v = useQuery<Vocabulary>({ queryKey: ["vocabulary"], queryFn: api.vocabulary, staleTime: Infinity });
  if (!v.data) return null;
  return (
    <details className="small vocab">
      <summary>Names you can use</summary>
      <dl>{v.data.targets[target].map((x) => <div key={x.name}><dt className="mono">{x.name}</dt><dd>{x.meaning}</dd></div>)}
        {v.data.chain.map((x) => <div key={x.name}><dt className="mono">{x.name}</dt><dd>{x.meaning}</dd></div>)}</dl>
      <p>Price-bar studies (on the underlying, timeframe from the settings): <span className="mono">{v.data.bar_functions.join(", ")}</span>; bars: <span className="mono">{v.data.bar_names.join(", ")}</span>.
        Combine with <code>and</code> / <code>or</code> / <code>not</code>. A value that isn't available makes the rule <em>unknown</em>, and the position shows Stale data rather than Hold.</p>
    </details>
  );
}
