// Risk check results: overall badge and the list of checks. Every result is text + icon, never colour alone.
import type { RiskCheck, RiskReport as Report, RiskResult } from "../api/types";
import { Tip } from "./ui";

const R: Record<RiskResult, { icon: string; text: string; cls: string; help: string }> = {
  PASS: { icon: "✓", text: "Pass", cls: "ok", help: "Within the limit." },
  WARNING: { icon: "⚠", text: "Warning", cls: "warn", help: "Close to a limit, or a rule set to warn." },
  BLOCKED: { icon: "⛔", text: "Blocked", cls: "bad", help: "Breaks your risk policy. optlab doesn't stop broker orders; it records that the rule was broken." },
  UNKNOWN: { icon: "?", text: "Unknown", cls: "muted", help: "Can't be checked: data is missing (account values, quotes or Greeks). Never treated as a pass." },
};

export function RiskBadge({ r, label }: { r: RiskResult; label?: string }) {
  const x = R[r];
  return (
    <Tip text={x.help}>
      <span className={`badge ${x.cls}`} tabIndex={0} aria-label={`Risk ${x.text}`}><span aria-hidden>{x.icon}</span> {label ?? x.text}</span>
    </Tip>
  );
}

export function RiskChecks({ checks, all = false }: { checks: RiskCheck[]; all?: boolean }) {
  const order: RiskResult[] = ["BLOCKED", "UNKNOWN", "WARNING", "PASS"];
  const rows = [...checks].filter((c) => all || c.result !== "PASS").sort((a, b) => order.indexOf(a.result) - order.indexOf(b.result));
  if (rows.length === 0) return <p className="muted-text small">Every check passes.</p>;
  return (
    <table className="table compact">
      <thead><tr><th>Check</th><th>Result</th><th>Why</th><th>Value</th><th>Limit</th></tr></thead>
      <tbody>{rows.map((c, i) => (
        <tr key={`${c.code}-${i}`}>
          <td>{c.label}{c.source === "small account" && <span className="muted-text small"> (small account)</span>}</td>
          <td><RiskBadge r={c.result} /></td><td className="small">{c.reason}</td>
          <td className="mono small">{c.value ?? "—"}</td><td className="mono small">{c.limit ?? "—"}</td>
        </tr>))}</tbody>
    </table>
  );
}

export function RiskPanel({ report, title = "Risk check", compact }: { report: Report | null | undefined; title?: string; compact?: boolean }) {
  if (!report) return null;
  return (
    <div className={`risk-panel risk-${report.overall.toLowerCase()}`}>
      <div className="risk-head">
        <strong>{title}</strong> <RiskBadge r={report.overall} />
        <span className="muted-text small">{report.policy_revision ? `policy revision ${report.policy_revision}` : "default policy"}
          {report.small_account_active ? " · small-account limits apply" : ""}</span>
      </div>
      {!compact && <p className="small">{report.summary}</p>}
      {report.small_account_reason && <p className="muted-text small">{report.small_account_reason}</p>}
      <RiskChecks checks={report.checks} all={!compact} />
    </div>
  );
}
