import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ApiError, api } from "../api/client";
import type { Account, Contract, ContractQuote, Overview, Position, Valuation } from "../api/types";
import { BoundedValue, ConfirmDialog, Empty, ErrorState, FreshnessBadge, LifecycleBadge, Loading, Modal, Pnl, TermLabel,
         Tip, Unavailable, useToast } from "../components/ui";
import { SnapshotForm } from "../forms/AccountForms";
import { CONTRACT_STATUSES, ContractForm } from "../forms/ContractForm";
import { ImportDialog } from "../forms/ImportDialog";
import { PositionForm } from "../forms/PositionForm";
import { date, dateTime, money, num, pct, price } from "../lib/format";
import { navigate } from "../lib/router";
import { useTerms } from "../lib/terms";

export function Dashboard({ account }: { account: Account }) {
  return (
    <div className="dashboard">
      <AccountOverview account={account} />
      <PositionsWidget account={account} />
      <WatchedWidget account={account} />
    </div>
  );
}

// ------------------------------------------------------------------ account overview
function AccountOverview({ account }: { account: Account }) {
  const [edit, setEdit] = useState(false);
  const ov = useQuery<Overview, ApiError>({ queryKey: ["overview", account.id], queryFn: () => api.overview(account.id) });
  const s = account.latest_snapshot;
  return (
    <section className="panel" aria-labelledby="ov-h">
      <div className="panel-head">
        <h2 id="ov-h"><TermLabel k="account" /> <span className="muted-text">·</span> {account.name}</h2>
        <div className="panel-tools">
          <span className="muted-text small">{s ? `Values entered manually ${dateTime(s.as_of)}` : "No account values entered yet"}</span>
          <button className="btn small" onClick={() => setEdit(true)}>{s ? "Update values" : "Enter values"}</button>
        </div>
      </div>
      {ov.isPending ? <Loading /> : ov.isError ? <ErrorState error={ov.error} retry={() => ov.refetch()} /> : (
        <div className="cards">
          <Card label={<TermLabel k="totalValue" />}>{s?.total_value ? money(s.total_value) : <Unavailable reason="Not entered." />}</Card>
          <Card label={<TermLabel k="cash" />}>{s?.cash ? money(s.cash) : <Unavailable reason="Not entered." />}</Card>
          <Card label={<TermLabel k="buyingPower" />}>{s?.buying_power ? money(s.buying_power) : <Unavailable reason="Not entered." />}</Card>
          <Card label={<TermLabel k="capitalAtRisk" />}
                sub={ov.data.capital_at_risk_complete ? (ov.data.capital_at_risk_pct_of_buying_power !== null ? `${pct(ov.data.capital_at_risk_pct_of_buying_power)} of buying power` : "")
                  : `Excludes ${ov.data.unbounded_risk_positions + ov.data.unknown_risk_positions} position(s) with unlimited or unknown risk`}
                warn={!ov.data.capital_at_risk_complete}>
            {money(ov.data.capital_at_risk)}{!ov.data.capital_at_risk_complete && " +"}
          </Card>
          <Card label={<TermLabel k="openPositions" />} sub={`${ov.data.defined_risk_positions} defined risk · ${ov.data.unbounded_risk_positions} unlimited · ${ov.data.unknown_risk_positions} unknown`}>
            {ov.data.open_positions}
          </Card>
          <Card label={<Tip text="Sum of realized profit and loss of closed and archived positions, after fees."><span className="term" tabIndex={0}>Realized P&amp;L<span className="info" aria-hidden>ⓘ</span></span></Tip>}>
            <Pnl value={ov.data.realized_pnl_total} />
          </Card>
          <div className="card wide">
            <div className="card-label">Upcoming expirations</div>
            {ov.data.upcoming_expirations.length === 0 ? <span className="muted-text">None</span> : (
              <ul className="exp-list">{ov.data.upcoming_expirations.slice(0, 5).map((e) => (
                <li key={e.expiration}><span className={`badge ${e.days <= 7 ? "warn" : "muted"}`}>{e.days < 0 ? "expired" : `${e.days} d`}</span> {date(e.expiration)} · {e.positions.join(", ")}</li>))}</ul>
            )}
          </div>
        </div>
      )}
      <Modal open={edit} onOpenChange={setEdit} title="Account values">
        <SnapshotForm account={account} onDone={() => setEdit(false)} />
      </Modal>
    </section>
  );
}

function Card({ label, children, sub, warn }: { label: React.ReactNode; children: React.ReactNode; sub?: string; warn?: boolean }) {
  return (
    <div className={`card ${warn ? "card-warn" : ""}`}>
      <div className="card-label">{label}</div>
      <div className="card-value">{children}</div>
      {sub && <div className="card-sub">{warn ? "⚠ " : ""}{sub}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ positions
type SortKey = "name" | "underlying" | "strategy" | "opened_at" | "max_loss" | "dte" | "lifecycle";

function PositionsWidget({ account }: { account: Account }) {
  const { t } = useTerms();
  const [life, setLife] = useState<"OPEN" | "CLOSED" | "ARCHIVED" | "ALL">("OPEN");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "dte", dir: 1 });
  const [adding, setAdding] = useState(false);
  const [importing, setImporting] = useState(false);
  const [estimates, setEstimates] = useState(false);
  const list = useQuery({
    queryKey: ["positions", account.id, life],
    queryFn: () => api.positions({ account_id: account.id, lifecycle: life === "ALL" ? undefined : [life], page_size: 200 }),
  });
  const rows = useMemo(() => {
    const q = search.trim().toUpperCase();
    const items = (list.data?.items ?? []).filter((p) => !q || p.underlying.includes(q) || p.name.toUpperCase().includes(q) || p.strategy_name.toUpperCase().includes(q));
    const val = (p: Position): string | number => sort.key === "dte" ? (p.days_to_expiration ?? 99999)
      : sort.key === "max_loss" ? (p.max_loss.state === "bounded" ? Number(p.max_loss.value) : p.max_loss.state === "unbounded" ? Infinity : -1)
      : sort.key === "strategy" ? p.strategy_name : (p[sort.key] ?? "") as string;
    return [...items].sort((a, b) => (val(a) < val(b) ? -1 : val(a) > val(b) ? 1 : 0) * sort.dir);
  }, [list.data, search, sort]);
  const open = rows.filter((p) => p.lifecycle === "OPEN");
  const vals = useQueries({
    queries: open.map((p) => ({ queryKey: ["valuation", p.id], queryFn: () => api.valuation(p.id), enabled: estimates, staleTime: 60_000 })),
  });
  const valById = Object.fromEntries(open.map((p, i) => [p.id, vals[i]]));
  const header = (key: SortKey, label: string) => (
    <th aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      <button className="th-btn" onClick={() => setSort({ key, dir: sort.key === key ? (sort.dir === 1 ? -1 : 1) : 1 })}>
        {label} {sort.key === key ? (sort.dir === 1 ? "▲" : "▼") : ""}
      </button>
    </th>
  );
  return (
    <section className="panel" aria-labelledby="pos-h">
      <div className="panel-head">
        <h2 id="pos-h"><TermLabel k="positions" /></h2>
        <div className="panel-tools">
          <select aria-label="Show" value={life} onChange={(e) => setLife(e.target.value as typeof life)}>
            <option value="OPEN">Open</option><option value="CLOSED">Closed</option><option value="ARCHIVED">Archived</option><option value="ALL">All</option>
          </select>
          <input aria-label="Filter positions" placeholder="Filter: symbol, name, strategy" value={search} onChange={(e) => setSearch(e.target.value)} />
          <Tip text="Fetch an on-demand estimate of each open position's value from the shared quote snapshot. Quotes are delayed; nothing is monitored continuously until Phase 2.">
            <button className="btn small" onClick={() => setEstimates(true)} disabled={estimates || open.length === 0}>{estimates ? "Estimates shown" : "Show estimates"}</button>
          </Tip>
          <button className="btn small" onClick={() => setImporting(true)}>Import CSV</button>
          <a className="btn small" href="/api/v1/positions/export.csv" download>Export CSV</a>
          <button className="btn primary small" onClick={() => setAdding(true)}>+ Add {t("position").toLowerCase()}</button>
        </div>
      </div>
      {list.isPending ? <Loading what="Loading positions" /> : list.isError ? <ErrorState error={list.error} retry={() => list.refetch()} />
        : rows.length === 0 ? (
          <Empty>{search ? "No positions match the filter." : life === "OPEN" ? <>No open positions yet. <button className="btn small primary" onClick={() => setAdding(true)}>Add your first position</button></> : "Nothing here."}</Empty>
        ) : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr>
              {header("name", "Position")}{header("underlying", "Symbol")}{header("strategy", "Strategy")}
              <th>Entry</th>{header("max_loss", "Max loss")}<th>Max gain</th>{header("dte", "Expiry")}
              {estimates && <th>Estimate</th>}{header("lifecycle", "Status")}
            </tr></thead>
            <tbody>{rows.map((p) => (
              <tr key={p.id} className="clickable" onClick={() => navigate(`/positions/${p.id}`)}>
                <td><a href={`#/positions/${p.id}`} onClick={(e) => e.stopPropagation()}>{p.name}</a>
                    {!p.defined_risk && <span className="badge bad small-badge" title="Not defined risk">◇ undefined risk</span>}</td>
                <td className="mono">{p.underlying}</td>
                <td>{p.strategy_name}{!p.strategy_exact && <span className="muted-text small"> (custom)</span>}</td>
                <td className="mono">{p.net_premium === null ? <Unavailable /> : <>{money(p.net_premium?.replace("-", ""))} <span className="muted-text small">{p.premium_type.toLowerCase()}</span></>}</td>
                <td><BoundedValue b={p.max_loss} kind="loss" /></td>
                <td><BoundedValue b={p.max_gain} kind="gain" /></td>
                <td>{p.nearest_expiration ? <>{date(p.nearest_expiration)} <span className={`badge ${p.days_to_expiration !== null && p.days_to_expiration <= 7 ? "warn" : "muted"}`}>{p.days_to_expiration! < 0 ? "expired" : `${p.days_to_expiration} d`}</span></> : "—"}</td>
                {estimates && <td>{p.lifecycle !== "OPEN" ? <span className="muted-text">—</span> : <EstimateCell q={valById[p.id]} />}</td>}
                <td>{p.lifecycle === "CLOSED" ? <>{<LifecycleBadge lifecycle="CLOSED" />} <Pnl value={p.realized_pnl} /></> : <LifecycleBadge lifecycle={p.lifecycle} />}</td>
              </tr>))}</tbody>
          </table>
          <p className="muted-text small">{rows.length} of {list.data!.total} shown. Statuses are lifecycle only (open, closed, archived); live HOLD / WATCH monitoring arrives in Phase 2.</p>
        </div>
      )}
      <Modal open={adding} onOpenChange={setAdding} title={`Add ${t("position").toLowerCase()}`} wide>
        <PositionForm accountId={account.id} onDone={(p) => { setAdding(false); navigate(`/positions/${p.id}`); }} />
      </Modal>
      <Modal open={importing} onOpenChange={setImporting} title="Import positions from CSV" wide>
        <ImportDialog accountId={account.id} onDone={() => setImporting(false)} />
      </Modal>
    </section>
  );
}

function EstimateCell({ q }: { q?: { isPending: boolean; isError: boolean; data?: Valuation } }) {
  if (!q || q.isPending) return <span className="muted-text">…</span>;
  if (q.isError || !q.data) return <Unavailable reason="The valuation request failed." />;
  const v = q.data;
  return (
    <span className="estimate">
      {v.open_pnl !== null ? <Pnl value={v.open_pnl} /> : <Unavailable reason={v.reasons.join(" ") || "No quote."} />}{" "}
      <FreshnessBadge freshness={v.freshness} provider={v.provider} fetchedAt={v.fetched_at} ageSeconds={v.quote_age_seconds} delayMinutes={v.delay_minutes} />
    </span>
  );
}

// ------------------------------------------------------------------ watched contracts
function WatchedWidget({ account }: { account: Account }) {
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Contract | null>(null);
  const [archiving, setArchiving] = useState<Contract | null>(null);
  const [quoted, setQuoted] = useState<Record<string, boolean>>({});
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({ queryKey: ["contracts", account.id], queryFn: () => api.contracts({ account_id: account.id, page_size: 200 }) });
  const archive = useMutation<Contract, ApiError, Contract>({
    mutationFn: (c) => api.archiveContract(c.id, c.version),
    onSuccess: (c) => { qc.invalidateQueries({ queryKey: ["contracts"] }); setArchiving(null); toast(`Archived ${c.occ_symbol}`); },
  });
  return (
    <section className="panel" aria-labelledby="wc-h">
      <div className="panel-head">
        <h2 id="wc-h"><TermLabel k="watched" /></h2>
        <div className="panel-tools"><button className="btn primary small" onClick={() => setAdding(true)}>+ Watch a contract</button></div>
      </div>
      {list.isPending ? <Loading what="Loading contracts" /> : list.isError ? <ErrorState error={list.error} retry={() => list.refetch()} />
        : list.data.items.length === 0 ? <Empty>No watched contracts. Add one you're researching to see its quote and track your plan for it.</Empty> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Contract</th><th>Expiry</th><th>Status</th><th>Target entry</th><th>Invalidation</th><th>Quote</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{list.data.items.map((c) => {
              const st = CONTRACT_STATUSES.find((s) => s.value === c.status)!;
              return (
                <tr key={c.id}>
                  <td><span className="mono">{c.underlying} {price(c.strike)} {c.option_right.toLowerCase()}</span><div className="muted-text small mono">{c.occ_symbol}{c.account_id === null ? " · research only" : ""}</div></td>
                  <td>{date(c.expiration)} <span className={`badge ${c.days_to_expiration <= 7 ? "warn" : "muted"}`}>{c.days_to_expiration < 0 ? "expired" : `${c.days_to_expiration} d`}</span></td>
                  <td><span className="badge info"><span aria-hidden>{st.icon}</span> {st.label}</span></td>
                  <td className="mono">{price(c.target_entry) ?? "—"}</td>
                  <td className="clip" title={c.invalidation ?? ""}>{c.invalidation ?? "—"}</td>
                  <td>{quoted[c.id] ? <ContractQuoteCell id={c.id} /> : <button className="btn small" onClick={() => setQuoted({ ...quoted, [c.id]: true })}>Get quote</button>}</td>
                  <td className="row-actions">
                    <button className="btn ghost small" onClick={() => setEditing(c)}>Edit</button>
                    <button className="btn ghost small" onClick={() => setArchiving(c)}>Archive</button>
                  </td>
                </tr>);
            })}</tbody>
          </table>
        </div>
      )}
      <Modal open={adding} onOpenChange={setAdding} title="Watch a contract" wide><ContractForm accountId={account.id} onDone={() => setAdding(false)} /></Modal>
      <Modal open={!!editing} onOpenChange={(o) => !o && setEditing(null)} title={`Edit ${editing?.occ_symbol ?? ""}`} wide>
        {editing && <ContractForm accountId={account.id} existing={editing} onDone={() => setEditing(null)} />}
      </Modal>
      <ConfirmDialog open={!!archiving} onOpenChange={(o) => !o && setArchiving(null)} title="Archive this contract?"
                     body={<p>{archiving?.occ_symbol} will move to the archive. Archived contracts are kept, read-only.</p>}
                     confirmLabel="Archive" danger busy={archive.isPending} onConfirm={() => archiving && archive.mutate(archiving)} />
    </section>
  );
}

function ContractQuoteCell({ id }: { id: string }) {
  const q = useQuery<ContractQuote, ApiError>({ queryKey: ["contract-quote", id], queryFn: () => api.contractQuote(id), staleTime: 30_000 });
  if (q.isPending) return <span className="muted-text">…</span>;
  if (q.isError) return <Unavailable reason={q.error.message} />;
  const d = q.data;
  if (d.status !== "FOUND") return <Unavailable reason={[d.reason, d.nearest.length ? `Nearest: ${d.nearest.join(", ")}` : ""].filter(Boolean).join(" ")} />;
  return (
    <span className="quote-cell">
      <Tip text={`Bid ${price(d.bid)} / ask ${price(d.ask)} · spread ${price(d.spread)} (${pct(d.spread_pct)}) · volume ${num(d.volume, 0)} · OI ${num(d.open_interest, 0)} · IV ${d.iv !== null ? pct(d.iv * 100) : "—"} · Δ ${num(d.delta, 2)} Γ ${num(d.gamma, 3)} Θ ${num(d.theta, 3)} V ${num(d.vega, 3)} · intrinsic ${price(d.intrinsic)} · extrinsic ${price(d.extrinsic)} · breakeven ${price(d.breakeven)} · underlying ${price(d.underlying_price)}${d.quality.length ? " · ⚠ " + d.quality.join(", ") : ""}`}>
        <span className="mono" tabIndex={0}>{price(d.bid) ?? "—"} × {price(d.ask) ?? "—"}{d.quality.length ? " ⚠" : ""}</span>
      </Tip>{" "}
      <FreshnessBadge freshness={d.freshness} provider={d.provider} fetchedAt={d.fetched_at} ageSeconds={d.quote_age_seconds} delayMinutes={d.delay_minutes} />
    </span>
  );
}
