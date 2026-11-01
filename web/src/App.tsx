import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Suspense, lazy, useEffect } from "react";
import { api } from "./api/client";
import type { Account, DataSources, EventItem, MonitorStatus, Preferences, Progress, SubjectType, ThemeId } from "./api/types";
import { Empty, ErrorState, Loading, Tip, useToast } from "./components/ui";
import { LiveProvider, useLiveState, useRefetchInterval } from "./lib/live";
import { CreateAccountForm } from "./forms/AccountForms";
import { useRoute, navigate } from "./lib/router";
import { TermsContext, TERMS, type Mode } from "./lib/terms";
import { Dashboard } from "./views/Dashboard";

// Route views load on first visit (smaller first download); the dashboard is in the main bundle.
const PlansView = lazy(() => import("./views/Plans").then((m) => ({ default: m.PlansView })));
const PlanDetail = lazy(() => import("./views/Plans").then((m) => ({ default: m.PlanDetail })));
const DebriefView = lazy(() => import("./views/Process").then((m) => ({ default: m.DebriefView })));
const JournalView = lazy(() => import("./views/Process").then((m) => ({ default: m.JournalView })));
const AcademyView = lazy(() => import("./views/Process").then((m) => ({ default: m.AcademyView })));
const MissionReportView = lazy(() => import("./views/Process").then((m) => ({ default: m.MissionReportView })));
const PositionDetail = lazy(() => import("./views/PositionDetail").then((m) => ({ default: m.PositionDetail })));
const RiskView = lazy(() => import("./views/Risk").then((m) => ({ default: m.RiskView })));
const NewsView = lazy(() => import("./views/News").then((m) => ({ default: m.NewsView })));
const AnalysisList = lazy(() => import("./views/Analysis").then((m) => ({ default: m.AnalysisList })));
const AnalysisNew = lazy(() => import("./views/Analysis").then((m) => ({ default: m.AnalysisNew })));
const AnalysisRunView = lazy(() => import("./views/Analysis").then((m) => ({ default: m.AnalysisRunView })));
const ManualNew = lazy(() => import("./views/Analysis").then((m) => ({ default: m.ManualNew })));

function researchUrl(ds?: DataSources): string {
  if (ds?.research_lab_url) return ds.research_lab_url;
  const { protocol, hostname } = window.location;
  return protocol === "https:" ? `https://${hostname}:8443/` : `http://${hostname}:8517/`;
}

export function App() {
  const toast = useToast();
  const onEvent = (e: EventItem) => { if (e.severity !== "info") toast(e.title, "bad"); };
  return <LiveProvider onEvent={onEvent}><Shell /></LiveProvider>;
}

function ThemePicker({ accountId, current, onPick }: { accountId: string; current: ThemeId; onPick: (t: ThemeId) => void }) {
  const prog = useQuery<Progress>({ queryKey: ["progress", accountId], queryFn: () => api.progress(accountId), staleTime: 60_000 });
  const themes = prog.data?.themes ?? [{ id: "default" as ThemeId, unlock_level: 1, unlocked: true }];
  return (
    <label className="small">Theme <span className="muted-text">(cosmetic; unlocks with level)</span>
      <select value={current} onChange={(e) => onPick(e.target.value as ThemeId)}>
        {themes.map((t) => <option key={t.id} value={t.id} disabled={!t.unlocked}>{t.id[0].toUpperCase() + t.id.slice(1)}{t.unlocked ? "" : ` (level ${t.unlock_level})`}</option>)}
      </select></label>
  );
}

function AlertCount() {
  const live = useLiveState();
  const st = useQuery<MonitorStatus>({ queryKey: ["monitor-status"], queryFn: api.monitorStatus, refetchInterval: useRefetchInterval(30_000) || 60_000 });
  const n = st.data ? st.data.unacknowledged.critical + st.data.unacknowledged.warning : 0;
  return (
    <div className="sidebar-alerts small">
      <a href="#/" onClick={() => setTimeout(() => document.getElementById("ev-h-all")?.scrollIntoView({ behavior: "smooth" }), 50)}>
        {n > 0 ? <span className={`badge ${st.data!.unacknowledged.critical ? "bad" : "warn"}`}>⚑ {n} to acknowledge</span>
               : <span className="badge muted">✓ Nothing to acknowledge</span>}
      </a>
      <span className={live === "live" ? "muted-text" : "warn-text"}>{live === "live" ? "● live" : live === "connecting" ? "◌ connecting" : "◌ polling"}</span>
    </div>
  );
}

function Shell() {
  const qc = useQueryClient();
  const route = useRoute();
  const prefs = useQuery<Preferences>({ queryKey: ["prefs"], queryFn: api.preferences });
  const accounts = useQuery<Account[]>({ queryKey: ["accounts"], queryFn: api.accounts });
  const sources = useQuery<DataSources>({ queryKey: ["sources"], queryFn: api.dataSources, staleTime: 300_000 });
  // Display preferences (labels, active account) update at once and roll back if the server refuses;
  // financial state is never updated this way.
  const setPrefs = useMutation({
    mutationFn: (p: Partial<Preferences>) => api.setPreferences(p),
    onMutate: (p) => {
      const prev = qc.getQueryData<Preferences>(["prefs"]);
      if (prev) qc.setQueryData(["prefs"], { ...prev, ...p });
      return { prev };
    },
    onError: (_e, _p, ctx) => ctx?.prev && qc.setQueryData(["prefs"], ctx.prev),
    onSuccess: (p) => qc.setQueryData(["prefs"], p),
  });
  const mode: Mode = prefs.data?.terminology ?? "professional";
  const list = accounts.data ?? [];
  const active = list.find((a) => a.id === prefs.data?.active_account_id) ?? list.find((a) => a.is_default) ?? list[0];
  useEffect(() => { document.title = mode === "colony" ? "optlab · Command center" : "optlab"; }, [mode]);
  const posMatch = /^\/positions\/([0-9a-f-]{36})$/.exec(route);
  const planMatch = /^\/plans\/([0-9a-f-]{36})$/.exec(route);
  const reportMatch = /^\/reports\/([0-9a-f-]{36})$/.exec(route);
  const anManual = /^\/analysis\/new\/manual\/([A-Za-z0-9.^%-]{1,20})$/.exec(route);
  const anNew = /^\/analysis\/new\/(plan|position|alert_rule|contract|event)\/([0-9a-f-]{36})$/.exec(route);
  const anRun = /^\/analysis\/([0-9a-f-]{36})$/.exec(route);
  const section = posMatch ? "dash" : planMatch || route === "/plans" ? "plans" : route === "/risk" ? "risk"
    : reportMatch || route === "/debrief" ? "debrief" : route === "/journal" ? "journal" : route === "/news" ? "news" : route.startsWith("/analysis") ? "analysis" : route === "/learn" ? "learn" : "dash";
  const theme = prefs.data?.theme ?? "default";
  useEffect(() => { document.documentElement.dataset.colorTheme = theme; }, [theme]);
  const nav = (key: "dashboard" | "plans" | "risk" | "debrief" | "journal" | "news" | "analysis" | "academy", href: string, sec: string) => (
    <a href={href} className={section === sec ? "active" : ""}>{mode === "colony" ? TERMS[key].colony : TERMS[key].pro}</a>);

  return (
    <TermsContext.Provider value={mode}>
      <a className="skip-link" href="#main" onClick={(e) => { e.preventDefault(); document.getElementById("main")?.focus(); }}>Skip to main content</a>
      <div className="shell">
        <aside className="sidebar" aria-label="Navigation">
          <div className="brand"><span aria-hidden>◈</span> optlab</div>
          <nav>
            {nav("dashboard", "#/", "dash")}{nav("plans", "#/plans", "plans")}{nav("risk", "#/risk", "risk")}
            {nav("debrief", "#/debrief", "debrief")}{nav("journal", "#/journal", "journal")}{nav("news", "#/news", "news")}{nav("analysis", "#/analysis", "analysis")}{nav("academy", "#/learn", "learn")}
            <a href={researchUrl(sources.data)} target="_blank" rel="noreferrer">
              <Tip text={TERMS.research.meaning}><span>Research Lab ↗</span></Tip>
            </a>
            <a href="/api/docs" target="_blank" rel="noreferrer">API docs ↗</a>
          </nav>
          <AlertCount />
          <div className="sidebar-section">
            {list.length > 1 && (
              <label>Account
                <select value={active?.id ?? ""} onChange={(e) => setPrefs.mutate({ active_account_id: e.target.value })}>
                  {list.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              </label>
            )}
            <fieldset className="mode-switch">
              <legend>Terminology</legend>
              {(["professional", "colony"] as const).map((m) => (
                <label key={m} className="check">
                  <input type="radio" name="terminology" checked={mode === m} onChange={() => setPrefs.mutate({ terminology: m })} />
                  {m === "professional" ? "Professional" : "Colony"}
                </label>
              ))}
              <p className="muted-text small">Labels only. Financial terms stay in the tooltips.</p>
            </fieldset>
            {active && <ThemePicker accountId={active.id} current={theme} onPick={(t) => setPrefs.mutate({ theme: t })} />}
          </div>
          <div className="sidebar-foot">
            {sources.data && (
              <p className={`small ${sources.data.quotes.synthetic ? "warn-text" : "muted-text"}`}>
                Quotes: {sources.data.quotes.provider}{sources.data.quotes.synthetic ? " (synthetic demo data)" : sources.data.quotes.delay_minutes ? `, delayed ~${sources.data.quotes.delay_minutes} min` : ""}
              </p>
            )}
            <p className="muted-text small">Informational only: optlab never places orders.</p>
          </div>
        </aside>
        <main className="main" id="main" tabIndex={-1}>
          <Suspense fallback={<Loading what="Loading" />}>
          {accounts.isPending || prefs.isPending ? <Loading what="Loading" />
            : accounts.isError ? <ErrorState error={accounts.error} retry={() => accounts.refetch()} />
            : !active ? (
              <section className="panel">
                <h2>Welcome to optlab</h2>
                <Empty>Create your account to start recording positions. You can add more accounts later.</Empty>
                <CreateAccountForm onDone={(a) => { setPrefs.mutate({ active_account_id: a.id }); navigate("/"); }} />
              </section>
            ) : posMatch ? <PositionDetail id={posMatch[1]} />
              : planMatch ? <PlanDetail id={planMatch[1]} />
              : route === "/plans" ? <PlansView account={active} />
              : route === "/risk" ? <RiskView account={active} />
              : reportMatch ? <MissionReportView id={reportMatch[1]} />
              : route === "/debrief" ? <DebriefView account={active} />
              : route === "/journal" ? <JournalView account={active} />
              : route === "/news" ? <NewsView account={active} />
              : route === "/analysis" ? <AnalysisList />
              : anManual ? <ManualNew symbol={decodeURIComponent(anManual[1])} />
              : anNew ? <AnalysisNew type={anNew[1] as SubjectType} id={anNew[2]} />
              : anRun ? <AnalysisRunView id={anRun[1]} />
              : route === "/learn" ? <AcademyView />
              : <Dashboard account={active} />}
          </Suspense>
        </main>
      </div>
    </TermsContext.Provider>
  );
}
