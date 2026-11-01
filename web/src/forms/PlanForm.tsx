// Write or edit a trade plan (mission). Every field is optional while drafting; the checklist on the plan
// page says what's needed before it can be armed. Conditions are checked by the server as you type.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { LegIn, Plan } from "../api/types";
import { FieldErrors, useToast } from "../components/ui";
import { moneyInputError } from "../lib/format";
import { useTerms } from "../lib/terms";
import { LegEditor, legErrors, newLeg, toLegIn, type LegDraft } from "./LegEditor";
import { useConditionCheck } from "./RulesEditor";

export const STRATEGIES: [string, string][] = [
  ["LONG_CALL", "Long call"], ["LONG_PUT", "Long put"], ["CALL_DEBIT_SPREAD", "Call debit spread"], ["PUT_DEBIT_SPREAD", "Put debit spread"],
  ["PUT_CREDIT_SPREAD", "Put credit spread"], ["CALL_CREDIT_SPREAD", "Call credit spread"], ["IRON_CONDOR", "Iron condor"],
  ["IRON_BUTTERFLY", "Iron butterfly"], ["CASH_SECURED_PUT", "Cash-secured put"], ["COVERED_CALL", "Covered call"],
  ["CALENDAR_SPREAD", "Calendar spread"], ["DIAGONAL_SPREAD", "Diagonal spread"], ["LONG_STRADDLE", "Long straddle"],
  ["LONG_STRANGLE", "Long strangle"], ["SHORT_STRADDLE", "Short straddle (undefined risk)"], ["SHORT_STRANGLE", "Short strangle (undefined risk)"],
  ["SHORT_PUT", "Short put (uncovered)"], ["SHORT_CALL", "Short call (uncovered)"], ["PROTECTIVE_PUT", "Protective put"],
  ["COLLAR", "Collar"], ["LONG_STOCK", "Long stock"], ["SHORT_STOCK", "Short stock"], ["CUSTOM", "Custom"],
];

const strip = (s: string | null | undefined) => (s ? s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "") : "");
const toLocal = (iso: string | null) => (iso ? new Date(iso).toISOString().slice(0, 16) : "");

type F = Record<string, string>;
const TEXT = ["title", "thesis", "direction", "intended_strategy", "market_regime", "setup_type", "trigger", "trigger_conditions",
  "confirmation", "confirmation_conditions", "target", "invalidation", "invalidation_conditions", "event_risks", "related_news",
  "chart_reference", "notes", "expiration_from", "expiration_to", "time_stop"];
const MONEY = ["entry_zone_low", "entry_zone_high", "max_entry_debit", "min_entry_credit", "target_price", "invalidation_price", "planned_max_loss"];
const NUM = ["max_spread_pct", "min_volume", "min_open_interest", "planned_contracts", "delta_min", "delta_max"];

function initial(p?: Plan, prefill?: Partial<F>): F {
  const f: F = { symbol: p?.symbol ?? "", expires_at: toLocal(p?.fields.expires_at ?? null) };
  for (const k of [...TEXT, ...MONEY, ...NUM]) {
    const v = (p?.fields as unknown as Record<string, unknown> | undefined)?.[k];
    f[k] = v === null || v === undefined ? "" : MONEY.includes(k) ? strip(String(v)) : String(v);
  }
  if (p) f.title = p.title;
  return { ...f, ...(prefill as F | undefined) };
}

function Cond({ label, value, onChange, help }: { label: string; value: string; onChange: (v: string) => void; help: string }) {
  const lines = value.split(/\n|;/).map((x) => x.trim()).filter((x) => x && !x.startsWith("#"));
  const first = lines.find(Boolean) ?? "";
  const err = useConditionCheck(lines.length ? lines.join(" and ") : first, "symbol");
  return (
    <label>{label} <span className="muted-text small">{help}</span>
      <textarea rows={2} className="mono" value={value} maxLength={2000} onChange={(e) => onChange(e.target.value)} aria-invalid={!!err} />
      {err && <span className="field-error">{err}</span>}
    </label>
  );
}

export function PlanForm({ accountId, existing, prefill, prefillLegs, onDone }: {
  accountId: string; existing?: Plan; prefill?: Partial<F>; prefillLegs?: LegIn[]; onDone: (p: Plan) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { t } = useTerms();
  const [f, setF] = useState<F>(() => initial(existing, prefill));
  const startLegs = existing?.legs.length ? existing.legs : prefillLegs ?? [];
  const [withLegs, setWithLegs] = useState(startLegs.length > 0);
  const [legs, setLegs] = useState<LegDraft[]>(() => startLegs.length ? startLegs.map((l) => newLeg({
    asset_type: l.asset_type, side: l.side, quantity: String(l.quantity), option_right: l.option_right ?? "CALL",
    strike: strip(l.strike ?? ""), expiration: l.expiration ?? "", fill_price: strip(l.fill_price ?? ""), fees: strip(l.fees ?? "0") || "0",
  })) : [newLeg()]);
  const set = (k: string, v: string) => setF((x) => ({ ...x, [k]: v }));
  const errs: Record<string, string | null> = {};
  for (const k of MONEY) errs[k] = moneyInputError(f[k]);
  for (const k of NUM) errs[k] = f[k].trim() && !Number.isFinite(Number(f[k])) ? "A number" : null;
  if (!existing) errs.symbol = /^[A-Za-z][A-Za-z0-9.-]{0,9}$/.test(f.symbol.trim()) ? null : "Ticker like SPY";
  const legBad = withLegs && legs.some((l) => Object.keys(legErrors(l)).length > 0);

  const body = () => {
    const b: Record<string, unknown> = {};
    for (const k of TEXT) b[k] = f[k].trim() || null;
    for (const k of MONEY) b[k] = f[k].trim() || null;
    for (const k of NUM) b[k] = f[k].trim() ? (["min_volume", "min_open_interest", "planned_contracts"].includes(k) ? parseInt(f[k], 10) : Number(f[k])) : null;
    b.expires_at = f.expires_at ? new Date(f.expires_at).toISOString() : null;
    b.legs = withLegs ? legs.map(toLegIn) : null;
    if (!b.title) delete b.title;
    return b;
  };
  const save = useMutation<Plan, ApiError>({
    mutationFn: () => existing ? api.updatePlan(existing.id, { version: existing.version, ...body() })
                               : api.createPlan({ account_id: accountId, symbol: f.symbol.trim().toUpperCase(), ...body() }),
    onSuccess: (p) => { qc.invalidateQueries({ queryKey: ["plans"] }); qc.setQueryData(["plan", p.id], p);
                        toast(`${existing ? "Saved" : "Drafted"} ${t("plan").toLowerCase()}: ${p.title}`); onDone(p); },
  });
  const input = (k: string, label: string, extra: Record<string, unknown> = {}) => (
    <label>{label}<input value={f[k]} onChange={(e) => set(k, e.target.value)} aria-invalid={!!errs[k]} {...extra} />
      {errs[k] && f[k] && <span className="field-error">{errs[k]}</span>}</label>
  );
  return (
    <form className="form" noValidate onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <fieldset><legend>Idea</legend>
        <div className="grid-4">
          {input("symbol", "Symbol", { disabled: !!existing, placeholder: "SPY" })}
          {input("title", "Title", { maxLength: 160, placeholder: "Auto: symbol + strategy" })}
          <label>Direction<select value={f.direction} onChange={(e) => set("direction", e.target.value)}>
            <option value="">—</option><option value="bullish">Bullish</option><option value="bearish">Bearish</option>
            <option value="neutral">Neutral</option><option value="volatility">Volatility</option></select></label>
          <label>Intended strategy<select value={f.intended_strategy} onChange={(e) => set("intended_strategy", e.target.value)}>
            <option value="">—</option>{STRATEGIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
          {input("setup_type", "Setup type", { maxLength: 80, placeholder: "Pullback to support" })}
          {input("market_regime", "Market regime", { maxLength: 80, placeholder: "Weak bullish trend" })}
        </div>
        <label>Thesis <span className="muted-text small">what you expect, and why</span>
          <textarea rows={2} value={f.thesis} maxLength={5000} onChange={(e) => set("thesis", e.target.value)} /></label>
      </fieldset>
      <fieldset><legend>Entry</legend>
        <label>Trigger <span className="muted-text small">in words</span><input value={f.trigger} maxLength={5000} onChange={(e) => set("trigger", e.target.value)} /></label>
        <Cond label="Trigger conditions" value={f.trigger_conditions} onChange={(v) => set("trigger_conditions", v)}
              help="checked by the monitor, one per line, e.g. price > 650 or crosses_above(close, sma(20))" />
        <label>Confirmation <span className="muted-text small">in words</span><input value={f.confirmation} maxLength={5000} onChange={(e) => set("confirmation", e.target.value)} /></label>
        <Cond label="Confirmation conditions" value={f.confirmation_conditions} onChange={(v) => set("confirmation_conditions", v)}
              help="all must hold after the trigger for the plan to qualify, e.g. rel_volume(20) > 1.2" />
        <div className="grid-4">
          {input("entry_zone_low", "Entry zone low (underlying)", { inputMode: "decimal" })}
          {input("entry_zone_high", "Entry zone high (underlying)", { inputMode: "decimal" })}
          {input("max_entry_debit", "Maximum entry debit (per share)", { inputMode: "decimal" })}
          {input("min_entry_credit", "Minimum entry credit (per share)", { inputMode: "decimal" })}
          {input("max_spread_pct", "Maximum bid/ask spread %", { inputMode: "decimal" })}
          {input("min_volume", "Minimum volume", { inputMode: "numeric" })}
          {input("min_open_interest", "Minimum open interest", { inputMode: "numeric" })}
          {input("delta_min", "Delta from (absolute)", { inputMode: "decimal", placeholder: "0.20" })}
          {input("delta_max", "Delta to (absolute)", { inputMode: "decimal", placeholder: "0.35" })}
        </div>
      </fieldset>
      <fieldset><legend>Risk, size and exit</legend>
        <div className="grid-4">
          {input("planned_max_loss", "Planned maximum loss ($, whole position)", { inputMode: "decimal" })}
          {input("planned_contracts", "Planned size (contracts)", { inputMode: "numeric" })}
          <label>Expirations from<input type="date" value={f.expiration_from} onChange={(e) => set("expiration_from", e.target.value)} /></label>
          <label>Expirations to<input type="date" value={f.expiration_to} onChange={(e) => set("expiration_to", e.target.value)} /></label>
          {input("target_price", "Target price (underlying)", { inputMode: "decimal" })}
          {input("invalidation_price", "Invalidation price (underlying)", { inputMode: "decimal" })}
          <label>Time stop <span className="muted-text small">exit by</span><input type="date" value={f.time_stop} onChange={(e) => set("time_stop", e.target.value)} /></label>
          <label>Plan valid until<input type="datetime-local" value={f.expires_at} onChange={(e) => set("expires_at", e.target.value)} /></label>
        </div>
        <label>Target <span className="muted-text small">in words</span><input value={f.target} maxLength={5000} onChange={(e) => set("target", e.target.value)} /></label>
        <label>Invalidation <span className="muted-text small">what proves the idea wrong</span><input value={f.invalidation} maxLength={5000} onChange={(e) => set("invalidation", e.target.value)} /></label>
        <Cond label="Invalidation conditions" value={f.invalidation_conditions} onChange={(v) => set("invalidation_conditions", v)}
              help="any one makes the monitor invalidate the plan; the invalidation price is checked too (below it for bullish, above for bearish)" />
      </fieldset>
      <fieldset><legend>Context</legend>
        <div className="grid-2">
          <label>Event risks<textarea rows={2} value={f.event_risks} maxLength={5000} onChange={(e) => set("event_risks", e.target.value)} /></label>
          <label>Related news<textarea rows={2} value={f.related_news} maxLength={5000} onChange={(e) => set("related_news", e.target.value)} /></label>
        </div>
        <div className="grid-2">
          <label>Chart or screenshot link <span className="muted-text small">https:// only</span><input value={f.chart_reference} maxLength={500} onChange={(e) => set("chart_reference", e.target.value)} /></label>
          <label>Notes<textarea rows={2} value={f.notes} maxLength={5000} onChange={(e) => set("notes", e.target.value)} /></label>
        </div>
      </fieldset>
      <fieldset><legend>Planned legs <span className="muted-text small">(optional: exact contracts and the prices you plan to get)</span></legend>
        <label className="check"><input type="checkbox" checked={withLegs} onChange={(e) => setWithLegs(e.target.checked)} /> Plan the exact legs (lets the risk check use quotes, capital and liquidity)</label>
        {withLegs && <LegEditor legs={legs} onChange={setLegs} serverErrors={{}} />}
      </fieldset>
      <FieldErrors error={save.error} />
      <div className="actions"><button className="btn primary" type="submit" disabled={Object.values(errs).some(Boolean) || legBad || save.isPending}>
        {save.isPending ? "Saving…" : existing ? "Save draft" : `Draft ${t("plan").toLowerCase()}`}</button></div>
    </form>
  );
}
