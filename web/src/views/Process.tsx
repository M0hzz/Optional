// Phase 4: daily discipline panel, progress (points, level, streak, achievements), the debrief room with
// mission reports, the journal (archives) and the academy. Everything here rewards process, never profit.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Account, DayScore, JournalEntryT, JournalKind, LedgerEntry, MissionReport, PolicyResults, Progress, Review, ScoreState } from "../api/types";
import { RiskPanel } from "../components/RiskReport";
import { Empty, ErrorState, FieldErrors, Loading, Modal, Pnl, SeverityBadge, TermLabel, Tip, useToast } from "../components/ui";
import { date, dateTime } from "../lib/format";
import { useRefetchInterval } from "../lib/live";
import { navigate } from "../lib/router";
import { TERMS, useTerms } from "../lib/terms";
import { PlanBadge } from "./Plans";

export const ST: Record<ScoreState, { icon: string; cls: string; text: string }> = {
  MET: { icon: "✓", cls: "ok", text: "Met" }, VIOLATED: { icon: "✕", cls: "bad", text: "Not met" },
  UNKNOWN: { icon: "?", cls: "warn", text: "Unknown: counted, no credit" }, NOT_APPLICABLE: { icon: "–", cls: "muted", text: "Doesn't apply" },
  TO_DO: { icon: "○", cls: "muted", text: "To do" },
};
const ENTRY: Record<LedgerEntry["entry_type"], { cls: string; text: string }> = {
  award: { cls: "ok", text: "award" }, replacement: { cls: "info", text: "replacement" },
  revocation: { cls: "bad", text: "revocation" }, correction: { cls: "warn", text: "correction" },
};
const signed = (n: number) => (n > 0 ? `+${n}` : `${n}`);
const CAT: Record<string, string> = { discipline: "Discipline", research: "Research", risk_control: "Risk control", review: "Review" };

// ------------------------------------------------------------------ discipline panel
export function DisciplinePanel({ account }: { account: Account }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [writing, setWriting] = useState<JournalKind | null>(null);
  const prog = useQuery<Progress, ApiError>({ queryKey: ["progress", account.id], queryFn: () => api.progress(account.id), refetchInterval: useRefetchInterval(60_000) });
  const tick = useMutation<DayScore, ApiError, { code: string; done: boolean }>({
    mutationFn: ({ code, done }) => api.setObjective(account.id, code, { done }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["progress"] }), onError: (e) => toast(e.message, "bad"),
  });
  const d = prog.data?.today;
  return (
    <section className="panel" aria-labelledby="disc-h">
      <div className="panel-head">
        <h2 id="disc-h"><TermLabel k="discipline" /></h2>
        <div className="panel-tools">
          {d && <span className="process-score" aria-label={`Today's process score ${d.score ?? "not scored"}`}>
            {d.rest_day ? "Rest day" : d.score === null ? "—" : `${d.score}%`} <span className="muted-text small">{d.rest_day ? "not scored" : `${d.met} of ${d.applicable} so far`}</span></span>}
          {d && <Tip text="A rest day isn't scored and doesn't break your streak. Taking days off is part of discipline.">
            <button className="btn small ghost" onClick={() => tick.mutate({ code: "REST_DAY", done: !d.rest_day })}>{d.rest_day ? "Undo rest day" : "Rest day"}</button></Tip>}
        </div>
      </div>
      {prog.isPending ? <Loading /> : prog.isError ? <ErrorState error={prog.error} retry={() => prog.refetch()} /> : d && (
        <>
          <ul className="objectives">{d.items.map((i, n) => {
            const st = ST[i.state];
            return (
              <li key={`${i.code}-${n}`} className={`obj ${i.state.toLowerCase()}`}>
                {i.kind === "manual" && i.state !== "NOT_APPLICABLE" && !d.rest_day ? (
                  <label className="check"><input type="checkbox" checked={i.state === "MET"} disabled={tick.isPending}
                    onChange={(e) => tick.mutate({ code: i.code, done: e.target.checked })} /> {i.label}</label>
                ) : (
                  <span><span className={`badge ${st.cls}`} aria-label={st.text}><span aria-hidden>{st.icon}</span></span> {i.label}</span>
                )}
                {i.detail && <span className="muted-text small"> — {i.detail}</span>}
                {i.code === "DAILY_THESIS" && i.state !== "MET" && !d.rest_day && <button className="btn small ghost" onClick={() => setWriting("daily_thesis")}>Write</button>}
                {i.code === "EOD_REVIEW" && i.state !== "MET" && !d.rest_day && <button className="btn small ghost" onClick={() => setWriting("eod_review")}>Write</button>}
                {i.code === "MAX_DAILY_RISK" && i.state !== "MET" && !d.rest_day && <a className="btn small ghost" href="#/risk">Set it</a>}
              </li>);
          })}</ul>
          {d.unknown > 0 && <p className="small"><span className="badge warn">? {d.unknown} unknown</span> The data to judge {d.unknown === 1 ? "one item" : "some items"} is missing: counted, with no credit.</p>}
          <p className="muted-text small">Score = met ÷ items that apply (unknown items count without credit). Profit, size and the number of trades never count; a day without trades can reach 100%. Finished days are stored and never change silently. <span className="muted-text">{d.version} · {d.timezone}</span></p>
        </>
      )}
      <Modal open={writing !== null} onOpenChange={(o) => !o && setWriting(null)} title={writing === "daily_thesis" ? "Daily market thesis" : "End-of-day review"} wide>
        {writing && <JournalForm accountId={account.id} kind={writing} onDone={() => { setWriting(null); qc.invalidateQueries({ queryKey: ["progress"] }); }} />}
      </Modal>
    </section>
  );
}

// ------------------------------------------------------------------ progress panel
export function ProgressPanel({ account }: { account: Account }) {
  const { mode } = useTerms();
  const prog = useQuery<Progress, ApiError>({ queryKey: ["progress", account.id], queryFn: () => api.progress(account.id) });
  const hist = useQuery({ queryKey: ["score-history", account.id], queryFn: () => api.scoreHistory(account.id, 21) });
  const [day, setDay] = useState<string | null>(null);
  const [ledgerOpen, setLedgerOpen] = useState(false);
  const p = prog.data;
  return (
    <section className="panel" aria-labelledby="prog-h">
      <div className="panel-head"><h2 id="prog-h"><TermLabel k="progress" /></h2></div>
      {prog.isPending ? <Loading /> : prog.isError ? <ErrorState error={prog.error} /> : p && (
        <div className="progress-grid">
          <div>
            <div className="card-label">{mode === "colony" ? "Colony level" : "Level"}</div>
            <div className="level"><span className="level-num">{p.level.level}</span> {mode === "colony" && <span>{p.level.colony_name}</span>}</div>
            <div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(p.level.progress * 100)}
                 aria-label="Progress to the next level"><span style={{ width: `${Math.round(p.level.progress * 100)}%` }} /></div>
            <div className="muted-text small">{p.points.effective} points · next level at {p.level.points_for_next}</div>
          </div>
          <div><div className="card-label">Points</div>
            <dl className="facts tight small" aria-label="Points breakdown">
              <div><dt>Lifetime awarded</dt><dd>{p.points.lifetime_awarded}</dd></div>
              <div><dt>Revoked or corrected</dt><dd>{p.points.revoked_or_corrected}</dd></div>
              <div><dt>Current effective</dt><dd><strong>{p.points.effective}</strong></dd></div>
            </dl>
            <ul className="plain small">{Object.entries(p.points.by_category).map(([k, v]) => <li key={k}>{CAT[k]}: <strong>{v}</strong></li>)}</ul>
            <button className="btn small ghost" onClick={() => setLedgerOpen(true)}>Points ledger</button></div>
          <div><div className="card-label">Streak</div>
            <div className="level"><span className="level-num">{p.streak.current}</span> day{p.streak.current === 1 ? "" : "s"}</div>
            <div className="muted-text small">best {p.streak.best} · scored weekdays at {p.streak.threshold}%+ · rest days don't break it</div>
            {hist.data && <div className="spark" aria-label="Daily process scores, last three weeks">{hist.data.filter((h) => h.scheduled).map((h) => (
              <Tip key={h.day} text={`${date(h.day)}: ${h.rest_day ? "rest day" : h.score === null ? "not scored" : h.score + "%"}${h.revision && h.revision > 1 ? ` (revision ${h.revision})` : ""}${h.unknown ? ` · ${h.unknown} unknown` : ""}`}>
                <button type="button" aria-label={`Score for ${date(h.day)}`} onClick={() => !h.in_progress && setDay(h.day)}
                      className={`spark-bar ${h.rest_day ? "rest" : h.score === null ? "none" : h.score >= p.streak.threshold ? "good" : "low"}`}
                      style={{ height: `${h.score === null ? 12 : Math.max(8, h.score * 0.4)}px` }} /></Tip>))}</div>}
          </div>
          <div className="wide-cell"><div className="card-label">Achievements</div>
            <ul className="achievements">{p.achievements.map((a) => (
              <li key={a.code} className={a.earned_at ? "earned" : ""}>
                <Tip text={`${a.description}${a.earned_at ? ` Earned ${dateTime(a.earned_at)} under ${a.rule_version}.` : ` ${a.progress} of ${a.goal}.`}`}>
                  <span tabIndex={0}><span aria-hidden>{a.earned_at ? "★" : "☆"}</span> {a.name}{!a.earned_at && <span className="muted-text small"> {a.progress}/{a.goal}</span>}</span>
                </Tip></li>))}</ul></div>
          {p.recent_awards.length > 0 && (
            <details className="wide-cell small"><summary>Recent points</summary>
              <ul className="plain">{p.recent_awards.slice(0, 10).map((a) => <li key={a.id}><LedgerLine e={a} /></li>)}</ul>
            </details>)}
          <div className="wide-cell muted-text small">Rules: {p.versions.process_score} · {p.versions.points_policy} · {p.versions.achievement_rules}</div>
        </div>
      )}
      <Modal open={day !== null} onOpenChange={(o) => !o && setDay(null)} title={day ? `Score for ${date(day)}` : ""} wide>{day && <ScoreRevisions accountId={account.id} day={day} />}</Modal>
      <Modal open={ledgerOpen} onOpenChange={setLedgerOpen} title="Points ledger" wide>{ledgerOpen && <LedgerView accountId={account.id} />}</Modal>
    </section>
  );
}

export function LedgerLine({ e }: { e: LedgerEntry }) {
  return (
    <span>
      <strong className={e.points < 0 ? "neg" : undefined}>{signed(e.points)}</strong> <span className={`badge ${ENTRY[e.entry_type].cls}`}>{ENTRY[e.entry_type].text}</span>{" "}
      {CAT[e.category]} · {e.description} <span className="muted-text">({date(e.day)}{e.backfill ? ", backfilled" : ""})</span>
      {e.reason && <div className="small">Reason: {e.reason} <span className="muted-text">— {e.actor === "user:local" ? "you" : "automatic"}</span></div>}
      {e.note && <div className="muted-text small">{e.note}</div>}
    </span>
  );
}

function ItemsTable({ items }: { items: DayScore["items"] }) {
  return (
    <table className="table compact"><thead><tr><th>Item</th><th>Result</th><th>Detail</th></tr></thead>
      <tbody>{items.map((i, n) => <tr key={`${i.code}-${n}`}><td>{i.label}</td>
        <td><span className={`badge ${ST[i.state].cls}`}>{ST[i.state].icon} {ST[i.state].text}</span></td><td className="small">{i.detail}</td></tr>)}</tbody></table>
  );
}

export function ScoreRevisions({ accountId, day }: { accountId: string; day: string }) {
  const q = useQuery<DayScore[], ApiError>({ queryKey: ["score-revisions", accountId, day], queryFn: () => api.scoreRevisions(accountId, day) });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} />;
  if (q.data.length === 0) return <Empty>This day hasn't been stored yet. It is stored when you next open your progress after the day ends.</Empty>;
  const revs = [...q.data].reverse();
  return (
    <div>
      <p className="muted-text small">Finished days are stored and never overwritten. When a record behind a day changes, a new revision is added that says what changed; earlier revisions stay.</p>
      {revs.map((r, n) => (
        <details key={r.id} open={n === 0} className="revision">
          <summary><strong>Revision {r.revision}</strong> · {r.rest_day ? "rest day" : r.score === null ? "not scored" : `${r.score}%`} ({r.met} of {r.applicable}{r.unknown ? `, ${r.unknown} unknown` : ""}) · {signed(r.points ?? 0)} points
            <span className="muted-text small"> · {r.version} · {r.timezone} · calculated {dateTime(r.calculated_at ?? null)}{r.backfill ? " (backfilled)" : ""}{n > 0 ? " · superseded" : ""}</span></summary>
          {r.correction_reason && <p className="small">What changed: {r.correction_reason}</p>}
          <ItemsTable items={r.items} />
        </details>))}
    </div>
  );
}

export function LedgerView({ accountId }: { accountId: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [adjusting, setAdjusting] = useState<LedgerEntry | null>(null);
  const [reason, setReason] = useState("");
  const q = useQuery({ queryKey: ["ledger", accountId], queryFn: () => api.ledger(accountId, { limit: 300 }) });
  const revoke = useMutation<LedgerEntry, ApiError>({
    mutationFn: () => api.revokeEntry(accountId, adjusting!.id, reason),
    onSuccess: () => { ["ledger", "progress", "score-history", "score-revisions"].forEach((x) => qc.invalidateQueries({ queryKey: [x] })); toast("Points revoked (the original entry is kept)"); setAdjusting(null); setReason(""); },
  });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} />;
  const { summary: s, entries } = q.data;
  const active = (e: LedgerEntry) => e.entry_type === "award" || e.entry_type === "replacement";
  return (
    <div>
      <dl className="facts">
        <div><dt>Lifetime awarded</dt><dd>{s.lifetime_awarded}</dd></div><div><dt>Revoked or corrected</dt><dd>{s.revoked_or_corrected}</dd></div>
        <div><dt>Current effective</dt><dd><strong>{s.effective}</strong></dd></div>
        <div><dt>Entries</dt><dd className="small">{s.entries.award} awards · {s.entries.replacement} replacements · {s.entries.revocation} revocations · {s.entries.correction} corrections</dd></div>
      </dl>
      <p className="muted-text small">Append-only: nothing here is edited or deleted. Points earned for good process never decrease; an award that was wrong (a duplicate, or evidence archived as entered in error) is removed by a separate revocation that says why.</p>
      <ol className="journal">{entries.map((e) => (
        <li key={e.id}><LedgerLine e={e} />
          {active(e) && <button className="btn ghost small" onClick={() => setAdjusting(e)}>Revoke…</button>}</li>))}</ol>
      <Modal open={!!adjusting} onOpenChange={(o) => !o && setAdjusting(null)} title="Revoke points">
        {adjusting && <form className="form" onSubmit={(ev) => { ev.preventDefault(); revoke.mutate(); }}>
          <p className="small">{adjusting.description} ({signed(adjusting.points)})</p>
          <label>Reason <span className="muted-text small">required, kept with the revocation</span><input value={reason} maxLength={500} onChange={(ev) => setReason(ev.target.value)} /></label>
          <FieldErrors error={revoke.error} />
          <div className="actions"><button className="btn danger" type="submit" disabled={reason.trim().length < 3 || revoke.isPending}>Revoke</button></div>
        </form>}
      </Modal>
    </div>
  );
}

// ------------------------------------------------------------------ journal
const KIND_LABEL: Record<JournalKind, string> = { daily_thesis: "Daily thesis", eod_review: "End-of-day review", decision: "Decision", note: "Note", lesson: "Lesson" };
const PROMPT: Partial<Record<JournalKind, string>> = {
  daily_thesis: "What is the market doing (trend, volatility, scheduled events)? What would you trade today, and what would make you stand aside?",
  eod_review: "Did you follow your plans and limits today? What did you skip, and why? What will you do differently tomorrow?",
};

export function JournalForm({ accountId, kind, existing, positionId, planId, onDone }: {
  accountId: string; kind: JournalKind; existing?: JournalEntryT; positionId?: string; planId?: string; onDone: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [k, setK] = useState<JournalKind>(existing?.kind ?? kind);
  const [title, setTitle] = useState(existing?.title ?? "");
  const [body, setBody] = useState(existing?.body ?? "");
  const [tags, setTags] = useState(existing?.tags.join(", ") ?? "");
  const save = useMutation<JournalEntryT, ApiError>({
    mutationFn: () => existing ? api.updateJournal(existing.id, { version: existing.version, title: title.trim() || null, body, tags: tags.split(",").map((x) => x.trim()).filter(Boolean) })
      : api.createJournal({ account_id: accountId, kind: k, title: title.trim() || null, body, tags: tags.split(",").map((x) => x.trim()).filter(Boolean),
                            position_id: positionId ?? null, plan_id: planId ?? null }),
    onSuccess: () => { ["journal", "progress", "score-history", "report"].forEach((x) => qc.invalidateQueries({ queryKey: [x] })); toast(`${KIND_LABEL[k]} saved`); onDone(); },
  });
  return (
    <form className="form" noValidate onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <div className="grid-3">
        <label>Kind<select value={k} disabled={!!existing} onChange={(e) => setK(e.target.value as JournalKind)}>
          {Object.entries(KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Title <span className="muted-text small">optional</span><input value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} /></label>
        <label>Tags <span className="muted-text small">comma separated</span><input value={tags} onChange={(e) => setTags(e.target.value)} /></label>
      </div>
      <label>Text<textarea rows={8} value={body} maxLength={20000} placeholder={PROMPT[k]} onChange={(e) => setBody(e.target.value)} /></label>
      <FieldErrors error={save.error} />
      <div className="actions"><button className="btn primary" type="submit" disabled={!body.trim() || save.isPending}>{save.isPending ? "Saving…" : "Save"}</button></div>
    </form>
  );
}

export function JournalView({ account }: { account: Account }) {
  const [kind, setKind] = useState<JournalKind | "all">("all");
  const [adding, setAdding] = useState<JournalKind | null>(null);
  const [editing, setEditing] = useState<JournalEntryT | null>(null);
  const [archived, setArchived] = useState(false);
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({ queryKey: ["journal", account.id, kind, archived], queryFn: () => api.journal({ account_id: account.id, kind: kind === "all" ? undefined : [kind], include_archived: archived, limit: 200 }) });
  const arch = useMutation<JournalEntryT, ApiError, { e: JournalEntryT; restore: boolean }>({
    mutationFn: ({ e, restore }) => restore ? api.restoreJournal(e.id, e.version) : api.archiveJournal(e.id, e.version),
    onSuccess: (_r, v) => { ["journal", "progress", "score-history", "score-revisions", "ledger"].forEach((x) => qc.invalidateQueries({ queryKey: [x] }));
      toast(v.restore ? "Restored. Any points it earned come back as a replacement entry." : "Archived. Any points it earned are revoked, with the reason."); },
    onError: (e) => toast(e.message, "bad"),
  });
  return (
    <section className="panel" aria-labelledby="jr-h">
      <div className="panel-head"><h2 id="jr-h"><TermLabel k="journal" /></h2>
        <div className="panel-tools">
          <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            <option value="all">Everything</option>{Object.entries(KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          <label className="check small"><input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} /> Show archived</label>
          <button className="btn small" onClick={() => setAdding("daily_thesis")}>Daily thesis</button>
          <button className="btn small" onClick={() => setAdding("eod_review")}>End-of-day review</button>
          <button className="btn small primary" onClick={() => setAdding("note")}>+ Entry</button>
        </div></div>
      {list.isPending ? <Loading /> : list.isError ? <ErrorState error={list.error} /> : list.data.length === 0 ? <Empty>No entries yet. A daily thesis and an end-of-day review count towards the daily objectives.</Empty> : (
        <ol className="journal">{list.data.map((e) => (
          <li key={e.id}>
            <div className="event-head"><span className="badge info">{KIND_LABEL[e.kind]}</span>{e.archived_at && <span className="badge muted">archived</span>} <strong>{e.title ?? date(e.entry_date)}</strong>
              <span className="muted-text small">{date(e.entry_date)}{e.updated_at !== e.created_at ? " · edited" : ""}</span>
              {e.position_id && <a className="small" href={`#/reports/${e.position_id}`}>position</a>}{e.plan_id && <a className="small" href={`#/plans/${e.plan_id}`}>plan</a>}
              {e.archived_at ? <button className="btn ghost small" disabled={arch.isPending} onClick={() => arch.mutate({ e, restore: true })}>Restore</button> : <>
                <button className="btn ghost small" onClick={() => setEditing(e)}>Edit</button>
                <button className="btn ghost small" disabled={arch.isPending} onClick={() => arch.mutate({ e, restore: false })}>Archive</button></>}</div>
            <p className="pre small">{e.body}</p>{e.tags.length > 0 && <div className="muted-text small">{e.tags.map((t) => `#${t}`).join(" ")}</div>}
          </li>))}</ol>
      )}
      <Modal open={adding !== null} onOpenChange={(o) => !o && setAdding(null)} title="New journal entry" wide>{adding && <JournalForm accountId={account.id} kind={adding} onDone={() => setAdding(null)} />}</Modal>
      <Modal open={!!editing} onOpenChange={(o) => !o && setEditing(null)} title="Edit entry" wide>{editing && <JournalForm accountId={account.id} kind={editing.kind} existing={editing} onDone={() => setEditing(null)} />}</Modal>
    </section>
  );
}

// ------------------------------------------------------------------ debrief room
export function DebriefView({ account }: { account: Account }) {
  const pend = useQuery({ queryKey: ["reviews-pending", account.id], queryFn: () => api.pendingReviews(account.id) });
  const done = useQuery({ queryKey: ["reviews", account.id], queryFn: () => api.reviews(account.id) });
  return (
    <div className="dashboard">
      <section className="panel" aria-labelledby="deb-h">
        <h2 id="deb-h"><TermLabel k="debrief" /></h2>
        <p className="muted-text small">Every closed position gets a mission report: did you follow the plan, why did you exit, what did you learn. Positions recorded against your risk policy come first. Reviews earn points whatever the result of the trade.</p>
        <h3>Waiting for a review</h3>
        {pend.isPending ? <Loading /> : pend.isError ? <ErrorState error={pend.error} /> : pend.data.length === 0 ? <Empty>Nothing waiting.</Empty> : (
          <table className="table compact"><thead><tr><th>Position</th><th>Closed</th><th>Waiting</th><th /></tr></thead>
            <tbody>{pend.data.map((r) => (
              <tr key={r.position_id}><td>{r.name} {r.rule_violation && <span className="badge bad">⛔ recorded against the policy</span>}{r.started && <span className="badge muted">draft</span>}</td>
                <td className="small">{dateTime(r.closed_at)}</td>
                <td>{r.overdue ? <span className="badge warn">⚠ {r.days_waiting} days</span> : <span className="small">{r.days_waiting ?? 0} days</span>}</td>
                <td><button className="btn small primary" onClick={() => navigate(`/reports/${r.position_id}`)}>Write the report</button></td></tr>))}</tbody></table>
        )}
        <h3>Completed</h3>
        {done.isPending ? <Loading /> : done.isError ? <ErrorState error={done.error} /> : done.data.length === 0 ? <Empty>No reviews yet.</Empty> : (
          <table className="table compact"><thead><tr><th>Position</th><th>Followed the plan</th><th>Exit</th><th>Lesson</th><th>Completed</th></tr></thead>
            <tbody>{done.data.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => navigate(`/reports/${r.position_id}`)}><td><a href={`#/reports/${r.position_id}`}>{r.position.name}</a></td>
                <td>{r.followed_plan?.replace("_", " ")}</td><td>{r.exit_reason?.replace("_", " ")} ({r.exit_followed_plan?.replace("_", " ")})</td>
                <td className="small clip">{r.lessons}</td><td className="small">{dateTime(r.completed_at)}</td></tr>))}</tbody></table>
        )}
      </section>
    </div>
  );
}

const MISTAKES: [string, string][] = [["no_plan", "No plan"], ["chased_entry", "Chased the entry"], ["oversized", "Too big"], ["ignored_stop", "Ignored the stop"],
  ["moved_stop", "Moved the stop"], ["early_exit", "Exited early"], ["late_exit", "Exited late"], ["averaging_down", "Averaged down"],
  ["revenge_trade", "Revenge trade"], ["fomo", "Fear of missing out"], ["ignored_liquidity", "Ignored liquidity"], ["ignored_earnings", "Ignored earnings"], ["other", "Other"]];

function ReviewForm({ report, onDone }: { report: MissionReport; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const r = report.review;
  const p = report.position;
  const [f, setF] = useState({ followed_plan: r?.followed_plan ?? (p.plan_id ? "" : "no_plan"), exit_reason: r?.exit_reason ?? "", exit_followed_plan: r?.exit_followed_plan ?? (p.plan_id ? "" : "no_plan"),
                               went_well: r?.went_well ?? "", went_wrong: r?.went_wrong ?? "", lessons: r?.lessons ?? "", would_take_again: r?.would_take_again ?? "" });
  const [mistakes, setMistakes] = useState<string[]>(r?.mistakes ?? []);
  const [ack, setAck] = useState(r?.rule_violation_acknowledged ?? false);
  const set = (k: keyof typeof f, v: string) => setF((x) => ({ ...x, [k]: v }));
  const save = useMutation<Review, ApiError, boolean>({
    mutationFn: (complete) => api.saveReview(p.id, { version: r?.version ?? null, ...Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v || null])),
                                                      mistakes, rule_violation_acknowledged: ack, complete }),
    onSuccess: (rv) => { ["report", "reviews", "reviews-pending", "progress"].forEach((x) => qc.invalidateQueries({ queryKey: [x] })); toast(rv.completed_at ? "Mission report complete" : "Draft saved"); onDone(); },
  });
  const yn = (k: "followed_plan" | "exit_followed_plan", label: string) => (
    <label>{label}<select value={f[k]} onChange={(e) => set(k, e.target.value)}><option value="">—</option><option value="yes">Yes</option><option value="partly">Partly</option><option value="no">No</option><option value="no_plan">There was no plan</option></select></label>);
  return (
    <form className="form" noValidate onSubmit={(e) => { e.preventDefault(); save.mutate(true); }}>
      <div className="grid-4">
        {yn("followed_plan", "Did you follow the plan at entry?")}
        <label>Why did you exit?<select value={f.exit_reason} onChange={(e) => set("exit_reason", e.target.value)}><option value="">—</option>
          {[["target", "Target reached"], ["stop", "Stop / maximum loss"], ["invalidation", "Invalidation"], ["time_stop", "Time stop"], ["risk_rule", "A risk rule"],
            ["discretionary", "My judgement"], ["expired", "Expired"], ["assigned", "Assigned / exercised"], ["other", "Other"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        {yn("exit_followed_plan", "Was the exit the planned one?")}
        <label>Would you take it again?<select value={f.would_take_again} onChange={(e) => set("would_take_again", e.target.value)}><option value="">—</option><option value="yes">Yes</option><option value="no">No</option><option value="unsure">Unsure</option></select></label>
      </div>
      <fieldset><legend>Mistakes (honest is what counts)</legend><div className="chips">{MISTAKES.map(([v, l]) => (
        <label key={v} className="check"><input type="checkbox" checked={mistakes.includes(v)} onChange={(e) => setMistakes(e.target.checked ? [...mistakes, v] : mistakes.filter((x) => x !== v))} /> {l}</label>))}</div></fieldset>
      <div className="grid-2">
        <label>What went well<textarea rows={3} value={f.went_well} onChange={(e) => set("went_well", e.target.value)} /></label>
        <label>What went wrong<textarea rows={3} value={f.went_wrong} onChange={(e) => set("went_wrong", e.target.value)} /></label>
      </div>
      <label>Lesson <span className="muted-text small">required to complete</span><textarea rows={2} value={f.lessons} onChange={(e) => set("lessons", e.target.value)} /></label>
      {p.rule_violation && <label className="check danger-box"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <span>This position was recorded against my risk policy. I acknowledge which rules it broke (reviewing it honestly earns review points; the break itself earns none).</span></label>}
      <FieldErrors error={save.error} />
      <div className="actions"><button type="button" className="btn" onClick={() => save.mutate(false)} disabled={save.isPending}>Save draft</button>
        <button className="btn primary" type="submit" disabled={save.isPending}>{save.isPending ? "Saving…" : "Complete the report"}</button></div>
    </form>
  );
}

export function MissionReportView({ id }: { id: string }) {
  const { t } = useTerms();
  const [writing, setWriting] = useState(false);
  const [noting, setNoting] = useState(false);
  const q = useQuery<MissionReport, ApiError>({ queryKey: ["report", id], queryFn: () => api.report(id) });
  if (q.isPending) return <Loading what="Loading the report" />;
  if (q.isError) return <ErrorState error={q.error} retry={() => q.refetch()} />;
  const r = q.data;
  const p = r.position;
  const open = p.lifecycle === "OPEN";
  return (
    <div className="detail">
      <button className="btn ghost small" onClick={() => navigate("/debrief")}>← {t("debrief")}</button>
      <section className="panel">
        <div className="panel-head"><h2>Mission report: {p.name}</h2>
          <div className="panel-tools"><a className="btn small" href={`#/positions/${p.id}`}>Open the position</a>
            <button className="btn small" onClick={() => setNoting(true)}>+ Journal note</button>
            {!open && <button className="btn small primary" onClick={() => setWriting(true)}>{r.review?.completed_at ? "Edit the review" : r.review ? "Continue the review" : "Write the review"}</button>}</div></div>
        <dl className="facts">
          <div><dt>Strategy</dt><dd>{p.strategy_name}</dd></div><div><dt>Opened</dt><dd>{dateTime(p.opened_at)}</dd></div>
          <div><dt>Closed</dt><dd>{dateTime(p.closed_at) ?? "still open"}</dd></div>
          <div><dt><Tip text={r.pnl_note}><span className="term" tabIndex={0}>Realized P&amp;L<span className="info" aria-hidden>ⓘ</span></span></Tip></dt><dd><Pnl value={p.realized_pnl} /> <span className="muted-text small">for the record, never scored</span></dd></div>
          <div><dt>Plan</dt><dd>{r.plan ? <><a href={`#/plans/${r.plan.id}`}>{r.plan.title}</a> <PlanBadge s={r.plan.state} /></> : "none"}</dd></div>
          <div><dt>Process points</dt><dd>{r.points.length ? <>{signed(r.points_net)} net <span className="muted-text small">({r.points.length} ledger entr{r.points.length === 1 ? "y" : "ies"} below)</span></> : "none yet"}</dd></div>
          <div><dt>Planned</dt><dd>{r.policy_results.unplanned ? <span className="badge bad">✕ unplanned position</span> : <span className="badge ok">✓ plan written before entry</span>}</dd></div>
        </dl>
        {open && <p className="muted-text">Review the position after it's closed.</p>}
      </section>
      {r.review && (
        <section className="panel"><h3>Review {r.review.completed_at ? <span className="badge ok">✓ complete</span> : <span className="badge muted">draft</span>}</h3>
          <dl className="facts">
            <div><dt>Followed the plan</dt><dd>{r.review.followed_plan?.replace("_", " ") ?? "—"}</dd></div>
            <div><dt>Exit</dt><dd>{r.review.exit_reason?.replace("_", " ") ?? "—"} ({r.review.exit_followed_plan?.replace("_", " ") ?? "—"})</dd></div>
            <div><dt>Mistakes</dt><dd>{r.review.mistakes.map((m) => MISTAKES.find(([v]) => v === m)?.[1] ?? m).join(", ") || "none"}</dd></div>
            <div><dt>Would take again</dt><dd>{r.review.would_take_again ?? "—"}</dd></div>
          </dl>
          <div className="grid-2 text-blocks"><div><h4>Went well</h4><p className="pre">{r.review.went_well ?? "—"}</p></div><div><h4>Went wrong</h4><p className="pre">{r.review.went_wrong ?? "—"}</p></div>
            <div><h4>Lesson</h4><p className="pre">{r.review.lessons ?? "—"}</p></div></div>
        </section>)}
      {r.plan?.comparison && (
        <section className="panel"><h3>Planned versus actual <span className="muted-text small">{r.plan.comparison.as_planned} of {r.plan.comparison.compared} as planned</span></h3>
          <table className="table compact"><thead><tr><th>Aspect</th><th>Planned</th><th>Actual</th><th>Result</th></tr></thead>
            <tbody>{r.plan.comparison.items.map((c, i) => <tr key={i}><td>{c.aspect}</td><td className="small">{c.planned}</td><td className="small">{c.actual}</td>
              <td>{c.result === "as_planned" ? "✓ as planned" : c.result === "deviated" ? "↯ deviated" : "—"}</td></tr>)}</tbody></table></section>)}
      <section className="panel"><PolicyResultsTable pr={r.review?.policy_results ?? r.policy_results} reviewed={!!r.review?.policy_results} /></section>
      {p.entry_risk && <details className="panel"><summary>Full risk check when recorded</summary><RiskPanel report={p.entry_risk} title="Risk check when recorded" compact /></details>}
      {r.points.length > 0 && <section className="panel"><h3>Points ledger for this position</h3><ul className="plain">{r.points.map((e) => <li key={e.id}><LedgerLine e={e} /></li>)}</ul></section>}
      {r.review_history.length > 0 && <details className="panel"><summary>Review history ({r.review_history.length} saved version{r.review_history.length === 1 ? "" : "s"})</summary>
        <ol className="timeline">{r.review_history.map((h, i) => <li key={i}><span className="tl-time">{dateTime(h.at)}</span> <strong>{h.action === "create" ? "Started" : "Edited"}</strong>
          {h.before && <div className="small">Before: lesson “{String(h.before.lessons ?? "—")}”, exit {String(h.before.exit_reason ?? "—")} ({String(h.before.exit_followed_plan ?? "—")})</div>}
          {h.after && <div className="small">After: lesson “{String(h.after.lessons ?? "—")}”, exit {String(h.after.exit_reason ?? "—")} ({String(h.after.exit_followed_plan ?? "—")}){h.after.completed ? " · complete" : ""}</div>}</li>)}</ol></details>}
      {r.journal.length > 0 && <section className="panel"><h3>Journal</h3><ul className="plain">{r.journal.map((j) => <li key={j.id}><strong>{KIND_LABEL[j.kind]}</strong> · {date(j.entry_date)}: <span className="pre">{j.body}</span></li>)}</ul></section>}
      <section className="panel"><h3>Timeline</h3>
        <ol className="timeline">{r.timeline.map((x) => x.kind === "event" ? (
          <li key={x.event.id}><span className="tl-time">{dateTime(x.at)}</span> <SeverityBadge s={x.event.severity} /> <strong>{x.event.title}</strong><div className="small">{x.event.message}</div></li>
        ) : <li key={x.audit.id}><span className="tl-time">{dateTime(x.at)}</span> <span className="badge muted">✎ Record</span> <strong>{x.audit.action}</strong></li>)}</ol></section>
      <Modal open={writing} onOpenChange={setWriting} title={`Review ${p.name}`} wide><ReviewForm report={r} onDone={() => setWriting(false)} /></Modal>
      <Modal open={noting} onOpenChange={setNoting} title="Journal note" wide><JournalForm accountId={p.account_id} kind="lesson" positionId={p.id} onDone={() => setNoting(false)} /></Modal>
    </div>
  );
}

const RES: Record<string, string> = { PASS: "ok", WARNING: "warn", BLOCKED: "bad", UNKNOWN: "warn" };

export function PolicyResultsTable({ pr, reviewed }: { pr: PolicyResults; reviewed: boolean }) {
  return (
    <div>
      <h3>Policy results at entry <span className={`badge ${RES[pr.overall]}`}>{pr.overall}</span>
        <span className="muted-text small"> {pr.policy_revision ? `risk policy revision ${pr.policy_revision}` : "no policy revision recorded"}{reviewed ? " · as reviewed" : ""}</span></h3>
      {pr.override_recorded && <p className="small danger-box">An explicit override was recorded when this position was entered: it was recorded although it broke {pr.violated.length === 1 ? "this rule" : "these rules"}: {pr.violated.map((c) => c.name).join(", ")}.</p>}
      <table className="table compact"><thead><tr><th>Rule</th><th>Result</th><th>Observed</th><th>Limit</th><th>Explanation</th><th>Override</th></tr></thead>
        <tbody>{pr.checks.map((c) => <tr key={c.code}><td>{c.name} <span className="muted-text small">{c.code}</span></td>
          <td><span className={`badge ${RES[c.result]}`}>{c.result}</span></td><td className="small">{c.value ?? "—"}</td><td className="small">{c.limit ?? "—"}</td>
          <td className="small">{c.explanation}</td><td className="small">{c.override_recorded ? "recorded" : "—"}</td></tr>)}</tbody></table>
    </div>
  );
}

// ------------------------------------------------------------------ academy
export function AcademyView() {
  return (
    <div className="dashboard">
      <section className="panel" aria-labelledby="ac-h">
        <h2 id="ac-h"><TermLabel k="academy" /></h2>
        <p className="small">optlab is for research, planning, journaling and monitoring. It never places, changes or stops orders. Data is delayed unless labelled Live.</p>
        <h3>Terms</h3>
        <table className="table compact"><thead><tr><th>Professional</th><th>Colony</th><th>Meaning</th></tr></thead>
          <tbody>{Object.entries(TERMS).map(([k, v]) => <tr key={k}><td>{v.pro}</td><td>{v.colony}</td><td className="small">{v.meaning}</td></tr>)}</tbody></table>
        <h3>Position statuses</h3>
        <ul className="small">
          <li><strong>Stale data</strong>: quotes missing, old or incomplete, or a rule can't be checked. Never treated as safe.</li>
          <li><strong>Exit condition met</strong>: an exit rule holds. <strong>Action required</strong>: an action rule or a built-in check (expiration day, short option in the money near expiration).</li>
          <li><strong>Watch</strong>: a watch rule holds. <strong>Hold</strong>: nothing holds on current data. A new status needs a few passes in a row; stale data applies at once.</li>
        </ul>
        <h3>Risk results</h3>
        <ul className="small"><li><strong>Pass</strong> within the limit · <strong>Warning</strong> close to it · <strong>Blocked</strong> breaks your policy (optlab can't stop an order; it records and warns) · <strong>Unknown</strong> data is missing, never counted as a pass.</li></ul>
        <h3>Plans</h3>
        <ul className="small"><li>Draft → Armed (complete and risk-checked) → Waiting (trigger) → Qualified (confirmed) → Converted. Or Skipped (a good outcome when the setup doesn't qualify), Invalidated, Expired.</li></ul>
        <h3>News and official events</h3>
        <ul className="small">
          <li>Sources: SEC EDGAR filings (8-K items, offerings, quarterly and annual reports) for the companies you follow, Federal Reserve press releases and FOMC meeting dates, BLS and BEA releases and release schedules, company investor-relations feeds you add, and Finnhub if you set an API key.</li>
          <li>Each item keeps its source's publication time and the time optlab first saw it (never mixed up), the symbols it's about and how they were matched (relevance), its category with the rule that matched, and the source's weight.</li>
          <li>The same story from several sources is shown once. Novelty is high for the first report of a story and low for repeats.</li>
          <li>Sentiment comes from the Loughran–McDonald financial dictionary: positive and negative words are counted, and positive words after "not", "no", "never"… count as negative. The matched words are shown. There's no generated text: summaries are the source's own or its opening sentences.</li>
          <li>News is a confirmation and risk-awareness layer. It never suggests, places or blocks an order. A monitoring rule may use news values (e.g. <code>news_sentiment_1d</code>, <code>news_risk_event_1d</code>) only together with a price, chain or position condition.</li>
        </ul>
        <h3>Process score and points</h3>
        <ul className="small">
          <li>Daily process score: objectives met ÷ objectives that apply. Trades add items (planned before entry, maximum loss defined, within policy, waited for confirmation); with no trades they don't apply, so a quiet day can score 100%.</li>
          <li>Points: plan before entry 10, maximum loss defined 10, within the risk policy 10, liquid contracts 5, waited for confirmation 10, skipped an armed setup 10, review completed 10, followed the planned exit 10, honest review of a rule break 5, daily thesis 5, end-of-day review 5, every objective met 10.</li>
          <li>At most three positions and three skipped setups earn points per day: trading more, or skipping more, never earns more.</li>
          <li>Each item is <strong>Met</strong> (credit), <strong>Not met</strong> (counted, no credit), <strong>Unknown</strong> (the data to judge it is missing: counted, no credit, and shown) or <strong>Doesn't apply</strong> (left out). <strong>To do</strong> appears only for today.</li>
          <li>Finished days are stored with their evidence under a versioned rule set ({"process-score/v1"}). If a record behind a finished day changes, a new revision is added that says what changed; old revisions and old rule versions are never recomputed.</li>
          <li>Never counted: profit or loss, position size, number of trades, time on screen. Points earned for good process never go down, so hiding a losing trade can never pay. Breaking a rule earns nothing and asks for a review.</li>
          <li>The points ledger is append-only. If the evidence for an award goes away (a thesis archived, a position archived as entered in error) the points are revoked by a separate entry with the reason; restoring it adds a replacement. You can revoke a wrong award yourself, with a reason.</li>
          <li>Levels come from points; themes unlock with levels. Achievements reward disciplined behaviour only (planned trades, reviews, skipped setups, staying inside limits, clean records).</li>
        </ul>
      </section>
    </div>
  );
}
