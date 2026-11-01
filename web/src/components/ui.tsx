// Shared building blocks. Every state is shown with text and an icon, never colour alone.
import * as Dialog from "@radix-ui/react-dialog";
import * as Tooltip from "@radix-ui/react-tooltip";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import type { Bounded, Freshness, Lifecycle, MonitorView, Severity } from "../api/types";
import { age, dateTime, money, pnl, sign } from "../lib/format";
import { useTerms, type TermKey } from "../lib/terms";

export function Tip({ text, children }: { text: string; children: ReactNode }) {
  return (
    <Tooltip.Root delayDuration={200}>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className="tooltip" sideOffset={6} collisionPadding={8}>
          {text}
          <Tooltip.Arrow className="tooltip-arrow" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

/** A label in the current terminology, with the financial meaning in a tooltip (and the professional
 *  term beside it in Colony mode). */
export function TermLabel({ k }: { k: TermKey }) {
  const { t, meaning, secondary } = useTerms();
  const sec = secondary(k);
  return (
    <Tip text={meaning(k)}>
      <span className="term" tabIndex={0}>
        {t(k)}
        {sec && <span className="term-secondary"> ({sec})</span>}
        <span className="info" aria-hidden>ⓘ</span>
      </span>
    </Tip>
  );
}

const FRESH: Record<Freshness, { icon: string; text: string; cls: string; help: string }> = {
  LIVE: { icon: "●", text: "Live", cls: "ok", help: "Real-time quote." },
  DELAYED: { icon: "◷", text: "Delayed", cls: "warn", help: "Exchange data delayed by the provider (Cboe: about 15 minutes)." },
  CACHED: { icon: "⟲", text: "Cached", cls: "warn", help: "An older snapshot: the latest refresh failed or it is past the freshness limit." },
  ESTIMATED: { icon: "≈", text: "Estimated", cls: "info", help: "Calculated from quotes; not a broker value." },
  SYNTHETIC: { icon: "⚗", text: "Synthetic", cls: "bad", help: "Offline demo data (data.source: synthetic). Not market prices." },
  UNAVAILABLE: { icon: "⊘", text: "Unavailable", cls: "bad", help: "No data. Nothing is filled in." },
};

export function FreshnessBadge({ freshness, provider, fetchedAt, ageSeconds, delayMinutes }: {
  freshness: Freshness; provider?: string; fetchedAt?: string | null; ageSeconds?: number | null; delayMinutes?: number;
}) {
  const f = FRESH[freshness];
  const detail = [f.help, provider && `Source: ${provider}.`, delayMinutes ? `Provider delay about ${delayMinutes} min.` : null,
                  fetchedAt && `Fetched ${dateTime(fetchedAt)} (${age(ageSeconds)}).`].filter(Boolean).join(" ");
  return (
    <Tip text={detail}>
      <span className={`badge ${f.cls}`} tabIndex={0} aria-label={`Data ${f.text}. ${detail}`}>
        <span aria-hidden>{f.icon}</span> {f.text}
      </span>
    </Tip>
  );
}

const LIFE: Record<Lifecycle, { icon: string; text: string; cls: string }> = {
  OPEN: { icon: "◉", text: "Open", cls: "info" },
  CLOSED: { icon: "✓", text: "Closed", cls: "muted" },
  ARCHIVED: { icon: "▣", text: "Archived", cls: "muted" },
};

export function LifecycleBadge({ lifecycle }: { lifecycle: Lifecycle }) {
  const l = LIFE[lifecycle];
  return <span className={`badge ${l.cls}`}><span aria-hidden>{l.icon}</span> {l.text}</span>;
}

const STATUS: Record<string, { icon: string; text: string; cls: string; help: string }> = {
  STALE_DATA: { icon: "⌛", text: "Stale data", cls: "bad", help: "The data is missing, old or incomplete, so the position's state is unknown. Never treated as safe." },
  EXIT_CONDITION_MET: { icon: "⛔", text: "Exit condition met", cls: "bad", help: "One of the position's exit rules holds." },
  ACTION_REQUIRED: { icon: "❗", text: "Action required", cls: "bad", help: "Something needs a decision: an action rule holds, a leg expires today or has expired, or a short option is in the money close to expiration." },
  WATCH: { icon: "◔", text: "Watch", cls: "warn", help: "A watch rule holds: look at the position." },
  HOLD: { icon: "●", text: "Hold", cls: "ok", help: "No exit, action or watch rule holds on current data." },
  CLOSED: { icon: "✓", text: "Closed", cls: "muted", help: "The position isn't open." },
  NOT_MONITORED: { icon: "○", text: "Not monitored", cls: "muted", help: "Monitoring is off for this position: nothing checks it." },
};

export const statusInfo = (s: string) => STATUS[s] ?? { icon: "?", text: s, cls: "muted", help: "" };

/** A monitoring status with icon and text; the reasons are in the tooltip. */
export function StatusBadge({ m, compact }: { m: MonitorView | null | undefined; compact?: boolean }) {
  if (!m) return <span className="badge muted">… Unknown</span>;
  const st = statusInfo(m.status);
  const lines = [st.help, ...m.reasons.map((r) => "• " + r)];
  if (m.pending) lines.push(`Changing to ${m.pending.label}: seen on ${m.pending.passes} of the ${m.pending.needed} passes needed to confirm.`);
  if (m.checked_at) lines.push(`Checked ${dateTime(m.checked_at)}.`);
  return (
    <Tip text={lines.filter(Boolean).join("\n")}>
      <span className={`badge ${st.cls} status-badge`} tabIndex={0} aria-label={`Status ${st.text}. ${m.reasons.join(" ")}`}>
        <span aria-hidden>{st.icon}</span> {st.text}{!compact && m.pending ? <span className="pending"> → {statusInfo(m.pending.status).text}?</span> : null}
      </span>
    </Tip>
  );
}

const SEV: Record<Severity, { icon: string; text: string; cls: string }> = {
  critical: { icon: "⛔", text: "Critical", cls: "bad" },
  warning: { icon: "⚠", text: "Warning", cls: "warn" },
  info: { icon: "ℹ", text: "Info", cls: "muted" },
};

export function SeverityBadge({ s }: { s: Severity }) {
  const x = SEV[s];
  return <span className={`badge ${x.cls}`}><span aria-hidden>{x.icon}</span> {x.text}</span>;
}

/** A maximum gain/loss: dollars when bounded, "Unlimited" or "Unknown" otherwise (never $0). */
export function BoundedValue({ b, kind }: { b: Bounded; kind: "gain" | "loss" | "bp" }) {
  if (b.state === "bounded") return <span className="mono">{money(b.value)}</span>;
  if (b.state === "unbounded")
    return <span className={`badge ${kind === "gain" ? "info" : "bad"}`}><span aria-hidden>∞</span> Unlimited</span>;
  return (
    <Tip text={kind === "bp" ? "Depends on the broker's margin rules." : "Can't be calculated from the information recorded."}>
      <span className="badge muted" tabIndex={0}><span aria-hidden>?</span> Unknown</span>
    </Tip>
  );
}

export function Pnl({ value }: { value: string | null }) {
  if (value === null) return <Unavailable />;
  const s = sign(value);
  return <span className={`mono ${s > 0 ? "pos" : s < 0 ? "neg" : ""}`}>{s > 0 ? "▲ " : s < 0 ? "▼ " : ""}{pnl(value)}</span>;
}

export function Unavailable({ reason }: { reason?: string }) {
  const el = <span className="unavailable" tabIndex={reason ? 0 : undefined}><span aria-hidden>⊘</span> Unavailable</span>;
  return reason ? <Tip text={reason}>{el}</Tip> : el;
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="state" role="status" aria-live="polite"><span className="spinner" aria-hidden /> {what}…</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="state empty">{children}</div>;
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const msg = error instanceof ApiError ? error.message : error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div className="state error" role="alert">
      <span aria-hidden>⚠</span> {msg}
      {retry && <button className="btn small" onClick={retry}>Try again</button>}
    </div>
  );
}

export function FieldErrors({ error }: { error: unknown }) {
  if (!(error instanceof ApiError)) return error ? <ErrorState error={error} /> : null;
  return (
    <div className="state error" role="alert">
      <strong>{error.message}</strong>
      {error.fieldErrors.length > 0 && (
        <ul>{error.fieldErrors.map((f, i) => <li key={i}><code>{f.field || "form"}</code>: {f.message}</li>)}</ul>
      )}
    </div>
  );
}

export function Modal({ open, onOpenChange, title, description, children, wide }: {
  open: boolean; onOpenChange: (o: boolean) => void; title: string; description?: string; children: ReactNode; wide?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="overlay" />
        <Dialog.Content className={`dialog ${wide ? "wide" : ""}`} aria-describedby={description ? undefined : undefined}>
          <div className="dialog-head">
            <Dialog.Title>{title}</Dialog.Title>
            <Dialog.Close className="btn ghost" aria-label="Close">✕</Dialog.Close>
          </div>
          {description ? <Dialog.Description className="muted-text">{description}</Dialog.Description>
                       : <Dialog.Description className="sr-only">{title}</Dialog.Description>}
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function ConfirmDialog({ open, onOpenChange, title, body, confirmLabel, danger, onConfirm, busy }: {
  open: boolean; onOpenChange: (o: boolean) => void; title: string; body: ReactNode; confirmLabel: string;
  danger?: boolean; onConfirm: () => void; busy?: boolean;
}) {
  return (
    <Modal open={open} onOpenChange={onOpenChange} title={title}>
      <div className="dialog-body">{body}</div>
      <div className="actions">
        <button className="btn" onClick={() => onOpenChange(false)}>Cancel</button>
        <button className={`btn ${danger ? "danger" : "primary"}`} onClick={onConfirm} disabled={busy}>
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

// ------------------------------------------------------------------ toasts (ordinary feedback)
interface ToastMsg { id: number; text: string; kind: "ok" | "bad" }
const ToastCtx = createContext<(text: string, kind?: "ok" | "bad") => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastMsg[]>([]);
  const push = useCallback((text: string, kind: "ok" | "bad" = "ok") => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x, { id, text, kind }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), 5000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.kind === "ok" ? "✓ " : "⚠ "}{t.text}</div>)}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);
