// Strategy-analysis preview: recognised strategy, entry cost, max gain/loss, breakevens, buying-power
// effect, Greeks from quotes, warnings, assumptions and the expiration payoff.
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import type { Analysis } from "../api/types";
import { decimal, money, num, price } from "../lib/format";
import { BoundedValue, FreshnessBadge, Tip, Unavailable } from "./ui";

export function AnalysisPanel({ a, compact = false }: { a: Analysis; compact?: boolean }) {
  const cost = a.cost_basis;
  return (
    <div className="analysis" aria-label="Strategy analysis">
      <div className="analysis-head">
        <div>
          <div className="eyebrow">Recognised strategy</div>
          <div className="strategy-name">
            {a.strategy_name}
            <span className={`badge ${a.exact ? "ok" : "muted"}`}>{a.exact ? "✓ Exact match" : "◇ Custom (not a named strategy)"}</span>
            <span className={`badge ${a.defined_risk ? "ok" : "bad"}`}>{a.defined_risk ? "◆ Defined risk" : "◇ Not defined risk"}</span>
          </div>
          <div className="muted-text">{a.direction !== "unknown" ? `${cap(a.direction)} · ` : ""}{a.units > 1 ? `${a.units} units · ` : ""}{a.premium_type === "UNKNOWN" ? "premium unknown" : a.premium_type.toLowerCase()}</div>
        </div>
      </div>
      <dl className="metrics">
        <Metric label="Entry cost" help="Net premium paid (debit) or received (credit, shown negative), plus opening fees. What it cost to open, not what it is worth now.">
          {cost === null ? <Unavailable reason="A fill price is missing." /> : <span className="mono">{money(cost)}</span>}
        </Metric>
        <Metric label="Maximum loss" help="The most the position can lose if held to expiration, including opening fees (closing fees not included).">
          <BoundedValue b={a.max_loss} kind="loss" />
        </Metric>
        <Metric label="Maximum gain" help="The most the position can make if held to expiration, after opening fees.">
          <BoundedValue b={a.max_gain} kind="gain" />
        </Metric>
        <Metric label="Breakevens" help="Underlying prices at expiration where the position neither makes nor loses money.">
          {a.breakevens === null ? <Unavailable reason="Can't be calculated (missing prices, or several expirations)." />
            : a.breakevens.length === 0 ? <span className="muted-text">None</span>
            : <span className="mono">{a.breakevens.map((b) => price(b)).join(" · ")}</span>}
        </Metric>
        <Metric label="Buying-power effect" help={`Estimated cash or margin the position ties up. Basis: ${a.buying_power_basis}.`}>
          <BoundedValue b={a.buying_power_effect} kind="bp" />
        </Metric>
        <Metric label="Fees" help="Opening fees across all legs."><span className="mono">{money(a.fees)}</span></Metric>
      </dl>
      {a.quotes && (
        <div className="quotes-row">
          <FreshnessBadge freshness={a.quotes.freshness} provider={a.quotes.provider} fetchedAt={a.quotes.fetched_at}
                          ageSeconds={a.quotes.quote_age_seconds} delayMinutes={a.quotes.delay_minutes} />
          <span className="muted-text">Underlying {price(a.quotes.underlying_price) ?? "unavailable"}</span>
          {a.quotes.greeks ? (
            <Tip text={`Position Greeks from ${a.quotes.greeks_source === "provider" ? "exchange-computed" : "model-estimated"} per-contract Greeks × quantity × 100. Delta in shares; theta in $/day; vega in $ per 1 vol point.`}>
              <span className="mono" tabIndex={0}>Δ {num(a.quotes.greeks.delta, 1)} · Γ {num(a.quotes.greeks.gamma, 2)} · Θ {num(a.quotes.greeks.theta, 2)} · V {num(a.quotes.greeks.vega, 2)}</span>
            </Tip>
          ) : <Unavailable reason={a.quotes.error ?? "A leg has no quote or no Greeks."} />}
        </div>
      )}
      {a.errors.length > 0 && <IssueList title="Errors (must be fixed)" items={a.errors.map((e) => e.message)} kind="bad" />}
      {a.warnings.length > 0 && <IssueList title="Warnings" items={a.warnings.map((w) => w.message)} kind="warn" />}
      {!compact && a.payoff.length > 1 && <Payoff a={a} />}
      {!compact && a.assumptions.length > 0 && (
        <details className="assumptions"><summary>Assumptions</summary><ul>{a.assumptions.map((x, i) => <li key={i}>{x}</li>)}</ul></details>
      )}
    </div>
  );
}

function Metric({ label, help, children }: { label: string; help: string; children: React.ReactNode }) {
  return (
    <div className="metric">
      <dt><Tip text={help}><span className="term" tabIndex={0}>{label}<span className="info" aria-hidden>ⓘ</span></span></Tip></dt>
      <dd>{children}</dd>
    </div>
  );
}

function IssueList({ title, items, kind }: { title: string; items: string[]; kind: "bad" | "warn" }) {
  return (
    <div className={`issues ${kind}`} role={kind === "bad" ? "alert" : undefined}>
      <strong>{kind === "bad" ? "⛔ " : "⚠ "}{title}</strong>
      <ul>{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  );
}

function Payoff({ a }: { a: Analysis }) {
  // Display only: the API computed every point exactly; Number() is used just to place them on the chart.
  const data = a.payoff.map((p) => ({ price: Number(p.price), pnl: Number(p.pnl) }));
  return (
    <figure className="payoff" aria-label="Profit and loss at expiration">
      <figcaption>
        <Tip text="Profit or loss if held to expiration, by underlying price at expiration. Not the value before expiration: that depends on time and implied volatility.">
          <span className="term" tabIndex={0}>Payoff at expiration<span className="info" aria-hidden>ⓘ</span></span>
        </Tip>
      </figcaption>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
          <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" />
          <XAxis dataKey="price" type="number" domain={["dataMin", "dataMax"]} tick={{ fill: "var(--muted)", fontSize: 12 }}
                 tickFormatter={(v: number) => "$" + v.toLocaleString()} />
          <YAxis tick={{ fill: "var(--muted)", fontSize: 12 }} tickFormatter={(v: number) => "$" + v.toLocaleString()} width={70} />
          <ReferenceLine y={0} stroke="var(--muted)" />
          {(a.breakevens ?? []).map((b) => <ReferenceLine key={b} x={Number(b)} stroke="var(--accent)" strokeDasharray="4 4" />)}
          <RTooltip formatter={(v) => [money(String(v)) ?? "", "P&L at expiration"]}
                    labelFormatter={(v) => `Underlying ${money(String(v))}`}
                    contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text)" }} />
          <Line type="linear" dataKey="pnl" stroke="var(--accent)" dot={false} strokeWidth={2} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <p className="muted-text small">Dashed lines: breakevens. {a.max_loss.state === "unbounded" ? "The loss keeps growing beyond the chart." : ""} Values from {decimal(a.payoff[0]?.price, 0) ? "$" + decimal(a.payoff[0].price, 0) : ""} to ${decimal(a.payoff[a.payoff.length - 1]?.price, 0)} underlying.</p>
    </figure>
  );
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
