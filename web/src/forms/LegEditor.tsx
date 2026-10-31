// Multi-leg editor. Prices and strikes stay strings end to end so nothing is rounded in the browser.
import type { LegIn } from "../api/types";
import { moneyInputError } from "../lib/format";

export interface LegDraft {
  key: string; asset_type: "STOCK" | "OPTION"; side: "BUY" | "SELL"; quantity: string; option_right: "CALL" | "PUT";
  strike: string; expiration: string; fill_price: string; fees: string; filled_at: string; broker_order_id: string;
  notes: string;
}

let n = 0;
export const newLeg = (over: Partial<LegDraft> = {}): LegDraft => ({
  key: `leg-${++n}`, asset_type: "OPTION", side: "BUY", quantity: "1", option_right: "CALL", strike: "", expiration: "",
  fill_price: "", fees: "0", filled_at: "", broker_order_id: "", notes: "", ...over,
});

export function legErrors(l: LegDraft): Record<string, string> {
  const e: Record<string, string> = {};
  if (!/^\d+$/.test(l.quantity.trim()) || Number(l.quantity) <= 0) e.quantity = "Whole number above 0";
  if (l.asset_type === "OPTION") {
    if (!l.strike.trim()) e.strike = "Required"; else { const m = moneyInputError(l.strike); if (m) e.strike = m; }
    if (!l.expiration) e.expiration = "Required";
  }
  const p = moneyInputError(l.fill_price); if (p) e.fill_price = p;
  const f = moneyInputError(l.fees); if (f) e.fees = f;
  return e;
}

/** Local datetime-input value -> ISO with the browser's timezone offset (the API requires a timezone). */
export function localToIso(v: string): string | null {
  if (!v) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function toLegIn(l: LegDraft): LegIn {
  const opt = l.asset_type === "OPTION";
  return {
    asset_type: l.asset_type, side: l.side, quantity: Number(l.quantity),
    option_right: opt ? l.option_right : null, strike: opt ? l.strike.trim() || null : null,
    expiration: opt ? l.expiration || null : null, fill_price: l.fill_price.trim() || null,
    fees: l.fees.trim() || "0", filled_at: localToIso(l.filled_at), broker_order_id: l.broker_order_id.trim() || null,
    notes: l.notes.trim() || null,
  };
}

export function LegEditor({ legs, onChange, serverErrors }: {
  legs: LegDraft[]; onChange: (legs: LegDraft[]) => void; serverErrors?: Record<number, string[]>;
}) {
  const set = (i: number, patch: Partial<LegDraft>) => onChange(legs.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const remove = (i: number) => onChange(legs.filter((_, j) => j !== i));
  return (
    <fieldset className="legs">
      <legend>Legs <span className="muted-text small">(order doesn't matter; up to 12)</span></legend>
      {legs.map((l, i) => {
        const err = legErrors(l);
        const id = (f: string) => `${l.key}-${f}`;
        return (
          <div className="leg" key={l.key} role="group" aria-label={`Leg ${i + 1}`}>
            <div className="leg-num">#{i + 1}</div>
            <label htmlFor={id("asset")}>Type
              <select id={id("asset")} value={l.asset_type} onChange={(e) => set(i, { asset_type: e.target.value as LegDraft["asset_type"] })}>
                <option value="OPTION">Option</option><option value="STOCK">Stock</option>
              </select>
            </label>
            <label htmlFor={id("side")}>Side
              <select id={id("side")} value={l.side} onChange={(e) => set(i, { side: e.target.value as LegDraft["side"] })}>
                <option value="BUY">Buy</option><option value="SELL">Sell</option>
              </select>
            </label>
            <label htmlFor={id("qty")}>{l.asset_type === "STOCK" ? "Shares" : "Contracts"}
              <input id={id("qty")} inputMode="numeric" value={l.quantity} aria-invalid={!!err.quantity}
                     onChange={(e) => set(i, { quantity: e.target.value })} />
              {err.quantity && <span className="field-error">{err.quantity}</span>}
            </label>
            {l.asset_type === "OPTION" && <>
              <label htmlFor={id("right")}>Call/put
                <select id={id("right")} value={l.option_right} onChange={(e) => set(i, { option_right: e.target.value as LegDraft["option_right"] })}>
                  <option value="CALL">Call</option><option value="PUT">Put</option>
                </select>
              </label>
              <label htmlFor={id("strike")}>Strike
                <input id={id("strike")} inputMode="decimal" placeholder="100" value={l.strike} aria-invalid={!!err.strike}
                       onChange={(e) => set(i, { strike: e.target.value })} />
                {err.strike && <span className="field-error">{err.strike}</span>}
              </label>
              <label htmlFor={id("exp")}>Expiration
                <input id={id("exp")} type="date" value={l.expiration} aria-invalid={!!err.expiration}
                       onChange={(e) => set(i, { expiration: e.target.value })} />
                {err.expiration && <span className="field-error">{err.expiration}</span>}
              </label>
            </>}
            <label htmlFor={id("px")}>Fill price <span className="muted-text small">per share</span>
              <input id={id("px")} inputMode="decimal" placeholder="1.25" value={l.fill_price} aria-invalid={!!err.fill_price}
                     onChange={(e) => set(i, { fill_price: e.target.value })} />
              {err.fill_price && <span className="field-error">{err.fill_price}</span>}
            </label>
            <label htmlFor={id("fees")}>Fees
              <input id={id("fees")} inputMode="decimal" value={l.fees} aria-invalid={!!err.fees}
                     onChange={(e) => set(i, { fees: e.target.value })} />
              {err.fees && <span className="field-error">{err.fees}</span>}
            </label>
            <details className="leg-more">
              <summary>More</summary>
              <label htmlFor={id("at")}>Fill time
                <input id={id("at")} type="datetime-local" value={l.filled_at} onChange={(e) => set(i, { filled_at: e.target.value })} />
              </label>
              <label htmlFor={id("oid")}>Broker order ID
                <input id={id("oid")} maxLength={64} value={l.broker_order_id} onChange={(e) => set(i, { broker_order_id: e.target.value })} />
              </label>
              <label htmlFor={id("notes")}>Leg notes
                <input id={id("notes")} maxLength={500} value={l.notes} onChange={(e) => set(i, { notes: e.target.value })} />
              </label>
            </details>
            <button type="button" className="btn ghost small" onClick={() => remove(i)} disabled={legs.length <= 1}
                    aria-label={`Remove leg ${i + 1}`}>Remove</button>
            {serverErrors?.[i]?.map((m, k) => <div key={k} className="field-error leg-server">{m}</div>)}
          </div>
        );
      })}
      <button type="button" className="btn small" onClick={() => onChange([...legs, newLeg()])} disabled={legs.length >= 12}>
        + Add leg
      </button>
    </fieldset>
  );
}
