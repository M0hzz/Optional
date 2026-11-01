// Phase 6: historical analysis. Open it from a plan, position, alert rule, watched contract or event; edit the
// draft; run; read the forward statistics, distributions, breakdowns and bias checks; drill down to each match.
// Descriptive statistics only: nothing here predicts or recommends.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { ApiError, api } from "../api/client";
import type { AnalysisCondition, AnalysisDraft, AnalysisMatch, AnalysisRun, Breakdown, HorizonStats, MatchWindow, SubjectType } from "../api/types";
import { Empty, ErrorState, FieldErrors, Loading, Modal, Tip, useToast } from "../components/ui";
import { date, dateTime } from "../lib/format";
import { navigate } from "../lib/router";

const NOTE = "Descriptive statistics of what price did after similar bars in the past. Not a forecast or a recommendation.";
const pc = (x: number | null | undefined, dp = 2) => (x === null || x === undefined ? "—" : `${x > 0 ? "+" : ""}${x.toFixed(dp)}%`);
const pp = (x: number | null | undefined) => (x === null || x === undefined ? "—" : `${x.toFixed(0)}%`);
const when = (ts: string | null | undefined, tf?: string) => (tf === "1d" || tf === "1wk" ? date(ts) : dateTime(ts));
const hzLabel = (h: string) => (h.endsWith("d") ? `${h.slice(0, -1)} session${h === "1d" ? "" : "s"}` : `${h} bar${h === "1" ? "" : "s"}`);
const BIAS_CLS = { pass: "ok", warn: "warn", fail: "bad" } as const;
const ST_ICON: Record<string, string> = { pass: "✓", near: "≈", fail: "✕", "n/a": "–" };
const DIM: Record<string, string> = { trend: "Trend (200-bar average)", market: "Market (SPY vs 50-bar average)", volatility: "Volatility (ATR percentile)", time_of_day: "Time of day" };

export function analysisHref(type: SubjectType, id?: string | null) {
  return `#/analysis/new/${type}${id ? `/${id}` : ""}`;
}

export function AnalysisLink({ type, id, small = true }: { type: SubjectType; id: string; small?: boolean }) {
  return (
    <Tip text="Open the Historical Signal Explorer with these conditions: when did they match before, and what did price do afterwards?">
      <a className={`btn ghost ${small ? "small" : ""}`} href={analysisHref(type, id)}>Historical analysis</a>
    </Tip>
  );
}

// ------------------------------------------------------------------ the form
type Form = Omit<AnalysisDraft, "subject" | "notes" | "source" | "entry"> & { symbolsText: string; horizonsText: string };

export function AnalysisNew({ type, id }: { type: SubjectType; id?: string }) {
  const q = useQuery<AnalysisDraft, ApiError>({ queryKey: ["analysis-draft", type, id], queryFn: () => api.analysisDraft({ subject_type: type, subject_id: id }) });
  if (q.isPending) return <Loading what="Preparing the analysis" />;
  if (q.isError) return <ErrorState error={q.error} />;
  return <AnalysisForm draft={q.data} />;
}

export function AnalysisForm({ draft }: { draft: AnalysisDraft }) {
  const toast = useToast();
  const [f, setF] = useState<Form>({ ...draft, symbolsText: draft.symbols.join(", "), horizonsText: draft.horizons.join(", ") });
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((x) => ({ ...x, [k]: v }));
  const setCond = (i: number, patch: Partial<AnalysisCondition>) => set("conditions", f.conditions.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const horizons = f.horizonsText.split(",").map((x) => x.trim()).filter(Boolean);
  const run = useMutation<{ id: string }, ApiError>({
    mutationFn: async () => {
      const r = await api.createAnalysis({
        subject_type: draft.subject.type, subject_id: draft.subject.id, name: f.name, timeframe: f.timeframe, start: f.start, end: f.end,
        symbols: f.symbolsText.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean), conditions: f.conditions.map(({ origin, ...c }) => ({ ...c, origin: origin ?? null })),
        excluded: f.excluded, include_near: f.include_near, minimum_score: f.minimum_score, required_mode: f.required_mode, horizons,
        dedupe_bars: f.dedupe_bars, direction: f.direction, focus_horizon: horizons.includes(f.focus_horizon) ? f.focus_horizon : horizons[0],
      });
      await api.startAnalysis(r.id);
      return r;
    },
    onSuccess: (r) => navigate(`/analysis/${r.id}`), onError: (e) => toast(e.message, "bad"),
  });
  return (
    <div className="detail">
      <button className="btn ghost small" onClick={() => navigate("/analysis")}>← All analyses</button>
      <section className="panel" aria-labelledby="an-new-h">
        <h2 id="an-new-h">Historical analysis{draft.subject.type !== "manual" && <>: {draft.subject.link ? <a href={draft.subject.link}>{draft.subject.label}</a> : draft.subject.label}</>}</h2>
        <p className="muted-text small">{NOTE}</p>
        {draft.source === "synthetic" && <div className="banner warn small" role="status">Synthetic mode: the search uses generated demo bars, not market history.</div>}
        {draft.notes.map((n) => <p key={n} className="small">{n}</p>)}
        <form className="form" noValidate onSubmit={(e) => { e.preventDefault(); run.mutate(); }}>
          <div className="grid-4">
            <label>Name<input value={f.name} maxLength={120} onChange={(e) => set("name", e.target.value)} /></label>
            <label>Symbols <span className="muted-text small">up to 10</span><input value={f.symbolsText} onChange={(e) => set("symbolsText", e.target.value)} /></label>
            <label>Bars<select value={f.timeframe} onChange={(e) => set("timeframe", e.target.value)}>
              {["1m", "5m", "15m", "30m", "1h", "1d", "1wk"].map((t) => <option key={t} value={t}>{t}</option>)}</select></label>
            <label>Direction<select value={f.direction ?? ""} onChange={(e) => set("direction", (e.target.value || null) as Form["direction"])}>
              <option value="">none (up / down only)</option><option value="bullish">bullish (a rise is a win)</option><option value="bearish">bearish (a fall is a win)</option></select></label>
            <label>From<input type="date" value={f.start} onChange={(e) => set("start", e.target.value)} /></label>
            <label>To<input type="date" value={f.end} onChange={(e) => set("end", e.target.value)} /></label>
            <label>Horizons <Tip text="N = N bars after the signal; Nd = N trading sessions later (intraday bars)."><span className="info" tabIndex={0}>ⓘ</span></Tip>
              <input value={f.horizonsText} onChange={(e) => set("horizonsText", e.target.value)} /></label>
            <label>Focus horizon<select value={f.focus_horizon} onChange={(e) => set("focus_horizon", e.target.value)}>
              {horizons.map((h) => <option key={h} value={h}>{h}</option>)}</select></label>
            <label>Thin signals <Tip text="Keep a signal only if this many bars passed since the last kept one, so forward windows don't overlap."><span className="info" tabIndex={0}>ⓘ</span></Tip>
              <input type="number" min={0} value={f.dedupe_bars} onChange={(e) => set("dedupe_bars", Number(e.target.value))} /></label>
            <label className="check"><input type="checkbox" checked={f.include_near} onChange={(e) => set("include_near", e.target.checked)} /> Include near matches</label>
            <label>Minimum near score<input type="number" min={0} max={100} value={f.minimum_score} onChange={(e) => set("minimum_score", Number(e.target.value))} /></label>
          </div>
          <fieldset><legend>Conditions (all evaluated on completed bars, using only that bar and earlier ones)</legend>
            <table className="table compact"><thead><tr><th>Id</th><th>Condition</th><th>From</th><th>Required</th><th>Near tolerance</th><th>Weight</th><th /></tr></thead>
              <tbody>{f.conditions.map((c, i) => (
                <tr key={i}>
                  <td className="mono small">{c.id}</td>
                  <td><input className="mono" style={{ width: "100%", minWidth: "18em" }} aria-label={`Condition ${c.id}`} value={c.expression} onChange={(e) => setCond(i, { expression: e.target.value })} /></td>
                  <td className="small muted-text">{c.origin ?? "you"}</td>
                  <td><input type="checkbox" aria-label={`${c.id} required`} checked={c.required} onChange={(e) => setCond(i, { required: e.target.checked })} /></td>
                  <td><select aria-label={`${c.id} tolerance type`} value={c.tolerance_type} onChange={(e) => setCond(i, { tolerance_type: e.target.value as AnalysisCondition["tolerance_type"] })}>
                    <option value="none">none</option><option value="percent_distance">% distance</option><option value="absolute">absolute</option></select>
                    {c.tolerance_type !== "none" && <input type="number" min={0} step="any" aria-label={`${c.id} tolerance`} value={c.tolerance} style={{ width: "5em" }} onChange={(e) => setCond(i, { tolerance: Number(e.target.value) })} />}</td>
                  <td><input type="number" min={0.1} step="any" aria-label={`${c.id} weight`} value={c.weight} style={{ width: "4.5em" }} onChange={(e) => setCond(i, { weight: Number(e.target.value) })} /></td>
                  <td><button type="button" className="btn ghost small" onClick={() => set("conditions", f.conditions.filter((_, j) => j !== i))}>Remove</button></td>
                </tr>))}</tbody></table>
            <button type="button" className="btn small" onClick={() => set("conditions", [...f.conditions, { id: `c${f.conditions.length + 1}`, expression: "", required: true, weight: 1, tolerance_type: "none", tolerance: 0, origin: null }])}>+ Condition</button>
          </fieldset>
          {f.excluded.length > 0 && <ExcludedList items={f.excluded} />}
          <FieldErrors error={run.error} />
          <div className="actions"><button className="btn primary" type="submit" disabled={run.isPending || !f.conditions.length}>{run.isPending ? "Starting…" : "Run the analysis"}</button></div>
        </form>
      </section>
    </div>
  );
}

function ExcludedList({ items }: { items: { expression: string; origin: string; reason: string }[] }) {
  return (
    <div className="banner small" role="note">
      <strong>Left out of the historical search</strong> (the search is looser than the live rule):
      <ul className="plain">{items.map((x, i) => <li key={i}><code>{x.expression}</code> <span className="muted-text">({x.origin})</span>: {x.reason}</li>)}</ul>
    </div>
  );
}

// ------------------------------------------------------------------ results
export function AnalysisRunView({ id }: { id: string }) {
  const q = useQuery<AnalysisRun, ApiError>({
    queryKey: ["analysis-run", id], queryFn: () => api.analysisRun(id),
    refetchInterval: (query) => (query.state.data && ["queued", "running"].includes(query.state.data.status) ? 1500 : false),
  });
  if (q.isPending) return <Loading what="Loading the analysis" />;
  if (q.isError) return <ErrorState error={q.error} retry={() => q.refetch()} />;
  const r = q.data;
  const subjectHref = r.subject_type === "manual" ? null : analysisHref(r.subject_type, r.subject_id);
  return (
    <div className="detail">
      <button className="btn ghost small" onClick={() => navigate("/analysis")}>← All analyses</button>
      <section className="panel">
        <div className="panel-head"><h2>{r.title}</h2>
          <div className="panel-tools">{subjectHref && <a className="btn small" href={subjectHref}>Edit and run again</a>}</div></div>
        <p className="muted-text small">{NOTE}</p>
        <dl className="facts small">
          <div><dt>Status</dt><dd><span className={`badge ${r.status === "done" ? "ok" : r.status === "error" ? "bad" : "muted"}`}>{r.status}</span></dd></div>
          <div><dt>Data</dt><dd>{r.source === "synthetic" ? <span className="badge bad">synthetic demo bars</span> : r.source}</dd></div>
          <div><dt>Bars · range</dt><dd>{r.strategy?.timeframe} · {r.strategy?.range.start} → {r.strategy?.range.end}</dd></div>
          <div><dt>Symbols</dt><dd>{r.strategy?.symbols.join(", ")}</dd></div>
          <div><dt>Direction</dt><dd>{r.direction ?? "none"}</dd></div>
          <div><dt>Strategy version</dt><dd className="mono">{r.strategy_version}</dd></div>
          <div><dt>Run</dt><dd>{dateTime(r.created_at)}{r.seconds !== null ? ` · ${r.seconds}s` : ""}</dd></div>
        </dl>
        <ul className="plain small mono">{r.strategy?.conditions.map((c) => <li key={c.id}>{c.id}: {c.expression}{c.required ? "" : " (optional)"}{c.tolerance ? ` · near within ${c.tolerance.value}${c.tolerance.type === "percent_distance" ? "%" : ""}` : ""}</li>)}</ul>
        {r.excluded && r.excluded.length > 0 && <ExcludedList items={r.excluded} />}
      </section>
      {r.status === "queued" || r.status === "running" ? <section className="panel"><Loading what="Searching history" /></section>
        : r.status === "error" ? <section className="panel"><p className="bad-text">The analysis failed: {r.error}</p></section>
        : <Results r={r} />}
    </div>
  );
}

function Results({ r }: { r: AnalysisRun }) {
  const res = r.result as NonNullable<AnalysisRun["result"]> & { horizons: HorizonStats[] };
  const [hz, setHz] = useState(r.focus_horizon);
  const [filter, setFilter] = useState<Breakdown | null>(null);
  const [type, setType] = useState<"all" | "exact" | "near">("all");
  const [open, setOpen] = useState<AnalysisMatch | null>(null);
  const matches = useMemo(() => (r.matches ?? []).filter((m) => (type === "all" || m.match_type === type) && (!filter || filter.keys.includes(m.key))), [r.matches, type, filter]);
  if (!res || !("horizons" in res)) return <Empty>No results.</Empty>;
  const dist = res.distributions.find((d) => d.horizon === hz);
  const c = r.counts!;
  return (
    <>
      <section className="panel" aria-labelledby="bias-h">
        <h3 id="bias-h">Bias checks</h3>
        <ul className="plain bias-list">{res.bias.map((b) => (
          <li key={b.code}><span className={`badge ${BIAS_CLS[b.status]}`}>{b.status === "pass" ? "✓" : b.status === "warn" ? "!" : "✕"} {b.status}</span> <strong>{b.title}</strong>
            <div className="small muted-text">{b.detail}</div></li>))}</ul>
      </section>
      <section className="panel" aria-labelledby="sum-h">
        <h3 id="sum-h">Matches</h3>
        <div className="cards">
          <div className="card"><div className="card-label">Signals</div><div className="card-value">{c.matches}</div><div className="small muted-text">{c.exact} exact · {c.near} near</div></div>
          <div className="card"><div className="card-label">Thinned (overlapping)</div><div className="card-value">{c.thinned}</div></div>
          <div className="card"><div className="card-label">Bars searched</div><div className="card-value">{c.bars_evaluated.toLocaleString()}</div><div className="small muted-text">{c.not_evaluable} not evaluable (warming up)</div></div>
        </div>
        {res.warnings.length > 0 && <ul className="small warn-text">{res.warnings.slice(0, 8).map((w) => <li key={w}>{w}</li>)}</ul>}
      </section>
      <section className="panel" aria-labelledby="fwd-h">
        <h3 id="fwd-h">What price did afterwards</h3>
        <table className="table compact"><thead><tr><th>Horizon</th><th>Matches</th><th>Signals</th><th>Median</th><th>Mean</th><th>Middle half</th><th>Up</th>
          {r.direction && <th>Win</th>}<th>Favourable</th><th>Adverse</th></tr></thead>
          <tbody>{res.horizons.filter((h) => h.signals > 0 || h.group === "all").map((h) => (
            <tr key={`${h.horizon}-${h.group}`} className={h.horizon === r.focus_horizon && h.group === "all" ? "focus-row" : ""}>
              <td>{hzLabel(h.horizon)}</td><td>{h.group}</td>
              <td>{h.signals}{h.low_sample && <Tip text="Fewer than 20 signals: treat with caution."><span className="badge warn" tabIndex={0}>low sample</span></Tip>}
                {h.incomplete > 0 && <span className="muted-text small"> (+{h.incomplete} incomplete)</span>}</td>
              <td>{pc(h.median_return_pct)}</td><td>{pc(h.mean_return_pct)}</td><td className="small">{pc(h.p25_return_pct)} … {pc(h.p75_return_pct)}</td>
              <td>{pp(h.up_pct)}</td>{r.direction && <td>{pp(h.win_pct)}</td>}<td>{pc(h.mean_favorable_pct)}</td><td>{pc(h.mean_adverse_pct)}</td></tr>))}</tbody></table>
        <p className="muted-text small">Measured from the close of the signal bar. Favourable / adverse: the best and worst move after the signal within the horizon{r.direction === "bearish" ? " (for a bearish direction a fall is favourable)" : ""}.</p>
      </section>
      <section className="panel" aria-labelledby="dist-h">
        <div className="panel-head"><h3 id="dist-h">Distribution of returns</h3>
          <div className="panel-tools"><select aria-label="Horizon" value={hz} onChange={(e) => setHz(e.target.value)}>{res.distributions.map((d) => <option key={d.horizon} value={d.horizon}>{d.horizon}</option>)}</select></div></div>
        {!dist || dist.signals === 0 ? <Empty>No completed signals at this horizon.</Empty> : (
          <>
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={dist.bins.map((b) => ({ ...b, label: `${b.from_pct.toFixed(1)}…${b.to_pct.toFixed(1)}%` }))} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
                <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="label" tick={{ fill: "var(--muted)", fontSize: 11 }} interval={1} />
                <YAxis allowDecimals={false} tick={{ fill: "var(--muted)", fontSize: 12 }} width={36} />
                <RTooltip contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text)" }} formatter={(v) => [String(v), "signals"]} />
                <Bar dataKey="count" isAnimationActive={false}>
                  {dist.bins.map((b, i) => <Cell key={i} fill={b.kind === "win" ? "var(--ok)" : b.kind === "loss" ? "var(--bad)" : "var(--accent)"} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
            <p className="muted-text small">{dist.signals} signals at horizon {dist.horizon}. {r.direction ? "Green: wins, red: losses for the chosen direction." : "No direction chosen: shown without win/loss colours."} {dist.note ?? ""}</p>
          </>)}
      </section>
      <section className="panel" aria-labelledby="brk-h">
        <h3 id="brk-h">Breakdowns at horizon {r.focus_horizon} <span className="muted-text small">(click a row to see its matches)</span></h3>
        {res.breakdowns.length === 0 ? <Empty>No completed signals to break down.</Empty> : (
          <table className="table compact"><thead><tr><th>Dimension</th><th>Subset</th><th>Signals</th><th>Median</th><th>Mean</th><th>Up</th>{r.direction && <th>Win</th>}</tr></thead>
            <tbody>{res.breakdowns.map((b) => (
              <tr key={`${b.dimension}-${b.subset}`} className={`clickable ${filter === b ? "focus-row" : ""}`} onClick={() => setFilter(filter === b ? null : b)}>
                <td>{DIM[b.dimension] ?? b.dimension}</td><td>{b.subset}</td>
                <td>{b.signals}{b.low_sample && <span className="badge warn">low sample</span>}</td><td>{pc(b.median_return_pct)}</td><td>{pc(b.mean_return_pct)}</td>
                <td>{pp(b.up_pct)}</td>{r.direction && <td>{pp(b.win_pct)}</td>}</tr>))}</tbody></table>)}
        <p className="muted-text small">Each label is measured at the signal bar from that bar and earlier only.</p>
      </section>
      <section className="panel" aria-labelledby="m-h">
        <div className="panel-head"><h3 id="m-h">Matching bars {filter && <span className="badge info">{DIM[filter.dimension] ?? filter.dimension}: {filter.subset} <button className="link-btn" onClick={() => setFilter(null)} aria-label="Clear filter">✕</button></span>}</h3>
          <div className="panel-tools"><select aria-label="Match type" value={type} onChange={(e) => setType(e.target.value as typeof type)}>
            <option value="all">exact and near</option><option value="exact">exact only</option><option value="near">near only</option></select></div></div>
        {matches.length === 0 ? <Empty>No matches for this filter.</Empty> : (
          <table className="table compact"><thead><tr><th>Bar</th><th>Symbol</th><th>Match</th><th>Score</th><th>Conditions</th><th>{r.focus_horizon} later</th><th>Labels</th></tr></thead>
            <tbody>{matches.slice(0, 300).map((m) => (
              <tr key={m.n} className="clickable" onClick={() => setOpen(m)}>
                <td className="small">{when(m.timestamp, r.strategy?.timeframe)}{m.is_entry && <span className="badge info">your entry</span>}</td><td>{m.symbol}</td>
                <td><span className={`badge ${m.match_type === "exact" ? "ok" : "muted"}`}>{m.match_type}</span>{m.thinned && <Tip text="Thinned: too close to the previous kept signal; not in the statistics."><span className="badge muted" tabIndex={0}>thinned</span></Tip>}</td>
                <td>{m.score ?? "—"}</td>
                <td className="mono small">{Object.entries(m.conditions).map(([k, v]) => `${k}${ST_ICON[v.status] ?? "?"}`).join(" ")}</td>
                <td>{pc(m.forward[r.focus_horizon]?.return_pct)}</td>
                <td className="small muted-text">{Object.values(m.labels).join(" · ")}</td></tr>))}</tbody></table>)}
        {matches.length > 300 && <p className="muted-text small">Showing the first 300 of {matches.length}.</p>}
      </section>
      <section className="panel" aria-labelledby="cov-h">
        <h3 id="cov-h">Data coverage</h3>
        <table className="table compact"><thead><tr><th>Symbol</th><th>Source</th><th>First bar</th><th>Last bar</th><th>Bars</th><th>Missing</th><th>Warm-up</th><th>Warnings</th></tr></thead>
          <tbody>{res.coverage.map((cv) => (
            <tr key={cv.symbol}><td>{cv.symbol}</td><td>{cv.source}</td><td className="small">{when(cv.first_bar, r.strategy?.timeframe)}</td><td className="small">{when(cv.last_bar, r.strategy?.timeframe)}</td>
              <td>{cv.bars_in_range}</td><td>{cv.missing_bars}</td><td className="small">{cv.warmup_available}/{cv.warmup_requested}</td>
              <td className="small warn-text">{cv.warnings.join("; ")}</td></tr>))}</tbody></table>
      </section>
      <details className="panel"><summary>Definitions</summary><pre className="small pre">{res.definitions}</pre></details>
      <Modal open={!!open} onOpenChange={(o) => !o && setOpen(null)} title={open ? `${open.symbol} · ${when(open.timestamp, r.strategy?.timeframe)}` : ""} wide>
        {open && <MatchChart runId={r.id} m={open} horizons={r.strategy?.forward.horizons ?? []} />}
      </Modal>
    </>
  );
}

function MatchChart({ runId, m, horizons }: { runId: string; m: AnalysisMatch; horizons: string[] }) {
  const q = useQuery<MatchWindow, ApiError>({ queryKey: ["analysis-window", runId, m.n], queryFn: () => api.matchWindow(runId, m.n) });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} />;
  const w = q.data;
  const data = w.bars.map((b, i) => ({ i, t: b.t, c: b.c }));
  return (
    <div>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data} margin={{ top: 22, right: 16, bottom: 4, left: 8 }}>
          <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" />
          <XAxis dataKey="i" tickFormatter={(i: number) => (w.bars[i] ? dateTime(w.bars[i].t)?.split(",")[0] ?? "" : "")} tick={{ fill: "var(--muted)", fontSize: 11 }} />
          <YAxis domain={["auto", "auto"]} tick={{ fill: "var(--muted)", fontSize: 12 }} width={60} />
          <ReferenceLine x={w.signal_index} stroke="var(--accent)" label={{ value: "signal", fill: "var(--accent)", fontSize: 11, position: "top" }} />
          {Object.entries(w.horizon_ends).map(([h, j]) => j !== null && <ReferenceLine key={h} x={j} stroke="var(--muted)" strokeDasharray="3 3" label={{ value: h, fill: "var(--muted)", fontSize: 10, position: "insideTop" }} />)}
          <RTooltip contentStyle={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text)" }}
            labelFormatter={(i) => dateTime(w.bars[Number(i)]?.t)} formatter={(v) => [Number(v).toFixed(2), "close"]} />
          <Line type="linear" dataKey="c" stroke="var(--text)" dot={false} strokeWidth={1.5} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <table className="table compact"><thead><tr><th>Condition</th><th>Result</th><th>Value</th></tr></thead>
        <tbody>{Object.entries(m.conditions).map(([k, v]) => <tr key={k}><td className="mono">{k}</td><td>{ST_ICON[v.status]} {v.status}</td><td>{v.value ?? "—"}</td></tr>)}</tbody></table>
      <table className="table compact"><thead><tr><th>Horizon</th><th>Return</th><th>Favourable</th><th>Adverse</th></tr></thead>
        <tbody>{horizons.map((h) => <tr key={h}><td>{h}</td><td>{pc(m.forward[h]?.return_pct)}</td><td>{pc(m.forward[h]?.favorable_pct)}</td><td>{pc(m.forward[h]?.adverse_pct)}</td></tr>)}</tbody></table>
      <p className="muted-text small">{m.explanation} · {w.note} {Object.values(w.horizon_ends).some((x) => x === null) ? "Horizons past the end of the data aren't drawn." : ""}</p>
    </div>
  );
}

// ------------------------------------------------------------------ list
export function AnalysisList() {
  const q = useQuery({ queryKey: ["analysis-runs"], queryFn: () => api.analysisRuns({ limit: 100 }) });
  const [sym, setSym] = useState("SPY");
  return (
    <div className="dashboard">
      <section className="panel" aria-labelledby="al-h">
        <div className="panel-head"><h2 id="al-h">Historical analysis</h2>
          <div className="panel-tools"><input aria-label="Symbol" value={sym} maxLength={12} onChange={(e) => setSym(e.target.value.toUpperCase())} style={{ width: "6em" }} />
            <a className="btn small primary" href={`#/analysis/new/manual/${encodeURIComponent(sym)}`}>New analysis</a></div></div>
        <p className="muted-text small">{NOTE} Open one from a plan, a position, an alert rule, a watched contract or an event to start from its conditions.</p>
        {q.isPending ? <Loading /> : q.isError ? <ErrorState error={q.error} /> : q.data.length === 0 ? <Empty>No analyses yet.</Empty> : (
          <table className="table compact"><thead><tr><th>Analysis</th><th>From</th><th>Bars</th><th>Signals</th><th>Data</th><th>Status</th><th>Run</th></tr></thead>
            <tbody>{q.data.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => navigate(`/analysis/${r.id}`)}>
                <td><a href={`#/analysis/${r.id}`}>{r.title}</a></td><td className="small">{r.subject_type.replace("_", " ")}</td>
                <td className="small">{r.strategy?.symbols.join(", ")} · {r.strategy?.timeframe}</td><td>{r.counts?.matches ?? "—"}</td>
                <td className="small">{r.source}</td><td><span className={`badge ${r.status === "done" ? "ok" : r.status === "error" ? "bad" : "muted"}`}>{r.status}</span></td>
                <td className="small">{dateTime(r.created_at)}</td></tr>))}</tbody></table>)}
      </section>
    </div>
  );
}

export function ManualNew({ symbol }: { symbol: string }) {
  const q = useQuery<AnalysisDraft, ApiError>({ queryKey: ["analysis-draft", "manual", symbol], queryFn: () => api.analysisDraft({ subject_type: "manual", symbol }) });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} />;
  return <AnalysisForm draft={q.data} />;
}
