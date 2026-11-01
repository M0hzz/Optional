// Add or edit a position. The analysis preview calls /positions/analyze (persists nothing) as you type.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import type { Analysis, Plan, Position, RiskReport } from "../api/types";
import { AnalysisPanel } from "../components/AnalysisPanel";
import { RiskPanel } from "../components/RiskReport";
import { FieldErrors, Loading, useToast } from "../components/ui";
import { useTerms } from "../lib/terms";
import { LegEditor, legErrors, localToIso, newLeg, toLegIn, type LegDraft } from "./LegEditor";

const TEMPLATES: Record<string, () => LegDraft[]> = {
  "Long call": () => [newLeg({ side: "BUY", option_right: "CALL" })],
  "Long put": () => [newLeg({ side: "BUY", option_right: "PUT" })],
  "Cash-secured put": () => [newLeg({ side: "SELL", option_right: "PUT" })],
  "Covered call": () => [newLeg({ asset_type: "STOCK", side: "BUY", quantity: "100" }), newLeg({ side: "SELL", option_right: "CALL" })],
  "Put credit spread": () => [newLeg({ side: "SELL", option_right: "PUT" }), newLeg({ side: "BUY", option_right: "PUT" })],
  "Call credit spread": () => [newLeg({ side: "SELL", option_right: "CALL" }), newLeg({ side: "BUY", option_right: "CALL" })],
  "Call debit spread": () => [newLeg({ side: "BUY", option_right: "CALL" }), newLeg({ side: "SELL", option_right: "CALL" })],
  "Put debit spread": () => [newLeg({ side: "BUY", option_right: "PUT" }), newLeg({ side: "SELL", option_right: "PUT" })],
  "Iron condor": () => [newLeg({ side: "BUY", option_right: "PUT" }), newLeg({ side: "SELL", option_right: "PUT" }),
                        newLeg({ side: "SELL", option_right: "CALL" }), newLeg({ side: "BUY", option_right: "CALL" })],
  "Straddle / strangle": () => [newLeg({ side: "BUY", option_right: "CALL" }), newLeg({ side: "BUY", option_right: "PUT" })],
  "Calendar / diagonal": () => [newLeg({ side: "SELL", option_right: "CALL" }), newLeg({ side: "BUY", option_right: "CALL" })],
  "Long stock": () => [newLeg({ asset_type: "STOCK", side: "BUY", quantity: "100" })],
  Custom: () => [newLeg()],
};

const isoToLocal = (iso: string | null) => (iso ? new Date(iso).toISOString().slice(0, 16) : "");

function fromPosition(p: Position): LegDraft[] {
  return p.legs.map((l) => newLeg({
    asset_type: l.asset_type, side: l.side, quantity: String(l.quantity), option_right: l.option_right ?? "CALL",
    strike: l.strike ? stripZeros(l.strike) : "", expiration: l.expiration ?? "",
    fill_price: l.fill_price ? stripZeros(l.fill_price) : "", fees: stripZeros(l.fees), filled_at: isoToLocal(l.filled_at),
    broker_order_id: l.broker_order_id ?? "", notes: l.notes ?? "",
  }));
}
const stripZeros = (s: string) => s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");

function fromPlan(p: Plan): LegDraft[] {
  if (!p.legs.length) return TEMPLATES["Long call"]();
  return p.legs.map((l) => newLeg({
    asset_type: l.asset_type, side: l.side, quantity: String(l.quantity), option_right: l.option_right ?? "CALL",
    strike: l.strike ? stripZeros(l.strike) : "", expiration: l.expiration ?? "", fill_price: l.fill_price ? stripZeros(l.fill_price) : "",
    fees: stripZeros(l.fees ?? "0"),
  }));
}

/** Record a position, edit one, or (with `plan`) convert a plan into the position you actually entered. */
export function PositionForm({ accountId, existing, plan, onDone }: { accountId: string; existing?: Position; plan?: Plan; onDone: (p: Position) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { t } = useTerms();
  const [underlying, setUnderlying] = useState(existing?.underlying ?? plan?.symbol ?? "");
  const [name, setName] = useState(existing?.name ?? plan?.title ?? "");
  const [ackRule, setAckRule] = useState(false);
  const [entryPrice, setEntryPrice] = useState("");
  const [openedAt, setOpenedAt] = useState(isoToLocal(existing?.opened_at ?? null));
  const [notes, setNotes] = useState(existing?.notes ?? "");
  const [cashSecured, setCashSecured] = useState(false);
  const [ack, setAck] = useState(false);
  const [legs, setLegs] = useState<LegDraft[]>(existing ? fromPosition(existing) : plan ? fromPlan(plan) : TEMPLATES["Put credit spread"]());
  const [correction, setCorrection] = useState("");
  const idemKey = useRef(crypto.randomUUID());          // one key per form session: a double submit can't create twice

  const valid = /^[A-Za-z][A-Za-z0-9.-]{0,9}$/.test(underlying.trim()) && legs.every((l) => Object.keys(legErrors(l)).length === 0);
  const payload = useMemo(() => ({ underlying: underlying.trim().toUpperCase(), legs: legs.map(toLegIn), account_id: accountId,
                                   cash_secured: cashSecured }), [underlying, legs, accountId, cashSecured]);
  const [debounced, setDebounced] = useState(payload);
  useEffect(() => { const h = setTimeout(() => setDebounced(payload), 450); return () => clearTimeout(h); }, [payload]);
  const preview = useQuery<Analysis, ApiError>({
    queryKey: ["analyze", debounced], queryFn: () => api.analyze({ ...debounced, include_quotes: true }),
    enabled: valid && debounced === payload, staleTime: 30_000, retry: false,
  });

  const risk = useQuery<RiskReport, ApiError>({
    queryKey: ["risk-eval", debounced, plan?.id],
    queryFn: () => api.evaluateRisk({ account_id: accountId, underlying: debounced.underlying, legs: debounced.legs,
                                      cash_secured: debounced.cash_secured, plan_id: plan?.id ?? null }),
    enabled: !existing && valid && debounced === payload, staleTime: 30_000, retry: false,
  });
  const save = useMutation<Position, ApiError>({
    mutationFn: () => plan
      ? api.convertPlan(plan.id, { version: plan.version, legs: payload.legs, opened_at: localToIso(openedAt), name: name.trim() || null,
                                   cash_secured: cashSecured, acknowledge_risk: ack, acknowledge_rule_violation: ackRule,
                                   underlying_price_at_entry: entryPrice.trim() || null, notes: notes.trim() || null }).then((r) => r.position)
      : existing
      ? api.updatePosition(existing.id, { version: existing.version, name: name.trim() || null, notes: notes.trim() || null,
                                          legs: payload.legs, cash_secured: cashSecured, acknowledge_risk: ack,
                                          correction_reason: correction.trim() || null })
      : api.createPosition({ account_id: accountId, underlying: payload.underlying, name: name.trim() || null,
                             opened_at: localToIso(openedAt), legs: payload.legs, cash_secured: cashSecured,
                             acknowledge_risk: ack, acknowledge_rule_violation: ackRule, notes: notes.trim() || null }, idemKey.current),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["overview"] });
      qc.invalidateQueries({ queryKey: ["position", p.id] });
      qc.invalidateQueries({ queryKey: ["plans"] });
      qc.invalidateQueries({ queryKey: ["plan"] });
      qc.invalidateQueries({ queryKey: ["risk"] });
      toast(existing ? "Position updated" : `${t("position")} recorded: ${p.name}`);
      onDone(p);
    },
  });

  const needsAck = preview.data?.requires_risk_acknowledgement ?? false;
  const ruleBroken = !existing && risk.data?.overall === "BLOCKED";
  const blocked = !valid || (preview.data?.errors.length ?? 0) > 0 || (needsAck && !ack) || (ruleBroken && !ackRule) || save.isPending;
  const serverLegErrors: Record<number, string[]> = {};
  save.error?.fieldErrors.forEach((f) => {
    const m = /^legs[.[](\d+)/.exec(f.field);
    if (m) (serverLegErrors[Number(m[1])] ??= []).push(f.message);
  });
  const hasShortPut = legs.some((l) => l.asset_type === "OPTION" && l.side === "SELL" && l.option_right === "PUT");

  return (
    <form className="form" onSubmit={(e) => { e.preventDefault(); if (!blocked) save.mutate(); }} noValidate>
      {!existing && (
        <div className="template-row" role="group" aria-label="Start from a template">
          <span className="muted-text small">Start from:</span>
          {Object.keys(TEMPLATES).map((k) => (
            <button type="button" key={k} className="chip" onClick={() => { setLegs(TEMPLATES[k]()); setAck(false); }}>{k}</button>
          ))}
        </div>
      )}
      <div className="grid-3">
        <label>Underlying symbol
          <input value={underlying} onChange={(e) => setUnderlying(e.target.value)} placeholder="SPY" disabled={!!existing || !!plan}
                 aria-invalid={!!underlying && !valid && !/^[A-Za-z]/.test(underlying)} autoFocus={!existing} />
        </label>
        <label>Name <span className="muted-text small">optional</span>
          <input value={name} maxLength={160} onChange={(e) => setName(e.target.value)} placeholder="Auto: symbol + strategy" />
        </label>
        {!existing && <label>Opened at <span className="muted-text small">default: now</span>
          <input type="datetime-local" value={openedAt} onChange={(e) => setOpenedAt(e.target.value)} />
        </label>}
        {plan && <label>Underlying price at entry <span className="muted-text small">optional; default: the current snapshot</span>
          <input inputMode="decimal" value={entryPrice} onChange={(e) => setEntryPrice(e.target.value)} />
        </label>}
      </div>
      <LegEditor legs={legs} onChange={(l) => { setLegs(l); setAck(false); }} serverErrors={serverLegErrors} />
      {hasShortPut && (
        <label className="check">
          <input type="checkbox" checked={cashSecured} onChange={(e) => setCashSecured(e.target.checked)} />
          Cash is set aside to buy the shares if assigned (cash-secured). Unticked, a short put is recorded as uncovered.
        </label>
      )}
      <label>Notes <span className="muted-text small">plain text</span>
        <textarea value={notes} maxLength={5000} rows={2} onChange={(e) => setNotes(e.target.value)} />
      </label>
      {existing?.lifecycle === "CLOSED" && (
        <label>Correction reason <span className="muted-text small">required for a closed position</span>
          <input value={correction} maxLength={500} onChange={(e) => setCorrection(e.target.value)} />
        </label>
      )}

      <section aria-label="Analysis preview" className="preview">
        <h3>Analysis preview <span className="muted-text small">(not saved)</span></h3>
        {!valid ? <p className="muted-text">Fill in the symbol and every leg to see the analysis.</p>
          : preview.isPending ? <Loading what="Analysing" />
          : preview.isError ? <FieldErrors error={preview.error} />
          : preview.data ? <AnalysisPanel a={preview.data} /> : null}
      </section>

      {!existing && valid && (
        <section aria-label="Risk check" className="preview">
          {risk.isPending ? <Loading what="Checking your risk policy" /> : risk.isError ? <FieldErrors error={risk.error} />
            : <RiskPanel report={risk.data} title="Risk policy check" />}
        </section>
      )}
      {ruleBroken && (
        <label className="check danger-box">
          <input type="checkbox" checked={ackRule} onChange={(e) => setAckRule(e.target.checked)} />
          <span>This breaks my risk policy. I'm recording it because it <strong>already happened at the broker</strong>; flag it for review.</span>
        </label>
      )}
      {needsAck && (
        <label className="check danger-box">
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          <span>I understand the maximum loss of this position is <strong>{preview.data?.max_loss.state === "unbounded" ? "unlimited" : "unknown"}</strong>,
            and it will be recorded that way (never as defined risk).</span>
        </label>
      )}
      <FieldErrors error={save.error} />
      <div className="actions">
        <button type="submit" className="btn primary" disabled={blocked}>
          {save.isPending ? "Saving…" : existing ? "Save changes" : plan ? `Convert to ${t("position").toLowerCase()}` : `Record ${t("position").toLowerCase()}`}
        </button>
      </div>
    </form>
  );
}
