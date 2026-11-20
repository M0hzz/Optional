import * as S from "./sim.js";
import * as I from "./ind.js";
import * as LV from "./levels.js";
import { PriceChart } from "./chart.js";
import { createHome } from "./home.js";
import { createSignals } from "./signals.js";
import * as DC from "./decide.js";
import * as V from "./vol.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 15000;
const SAVE_KEY = "optlab-sim-tabs-v1";
const OLD_KEY = "optlab-sim-pick";
const MAX_TABS = 8;
const KEEP = ["id", "sym", "side", "budget", "pick", "exp", "horizon", "seed", "scenario", "lever", "levelR", "levelS", "overlays", "osc",
              "range", "autoR", "autoS", "tf", "hbars"];

// How a tab's timeframe maps onto the simulation: steps per day (spd) and per chart bar (spb). Intraday
// timeframes run one step per bar and measure a scenario's length in bars (hbars), not days.
const TF_UNIT = {
  "5m": { spd: 78, spb: 1, intraday: true, min: 5, hStep: 6, hDef: 24, hMax: 156, word: "BAR" },
  "15m": { spd: 26, spb: 1, intraday: true, min: 15, hStep: 2, hDef: 8, hMax: 52, word: "BAR" },
  "1h": { spd: 6.5, spb: 1, intraday: true, min: 60, hStep: 1, hDef: 7, hMax: 33, word: "BAR" },
  "1d": { spd: 8, spb: 8, word: "DAY" },
  "1w": { spd: 8, spb: 56, word: "WEEK" },
};
const unit = (t = st) => TF_UNIT[t.tf] || TF_UNIT["1d"];
const horizonDays = (t = st) => (unit(t).intraday ? t.hbars * unit(t).min / 390 : t.horizon);
const hours = (bars, u) => { const m = bars * u.min; return m % 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}` : `${m / 60}h`; };
// "+2d", or on an intraday chart "+1h30" of trading time
const ahead = (day, t = st) => {
  const u = unit(t);
  if (!u.intraday) return `+${day.toFixed(day % 1 ? 1 : 0)}d`;
  const m = Math.round(day * 390);
  return m >= 390 * 2 ? `+${(day).toFixed(1)}d` : `+${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
};

// ---------------------------------------------------------------- tabs: each one is a whole simulation
let tabs = [];
let st = null;                 // the tab on screen
let playing = null;            // the animation frame of PLAY (one at a time)
let pollTimer = null;
let view = "tab";              // "home" (the overview of everything) or "tab" (one simulation)
let home = null;
// GO / HOLD / CUT: your default rules (each position keeps the rules it was bought with), the record of trades you
// closed, and the earnings calendar.
const RULES_KEY = "optlab-sim-rules-v1", RECORD_KEY = "optlab-sim-record-v1";
const readJSON = (k, d) => { try { return JSON.parse(localStorage.getItem(k) || "null") ?? d; } catch { return d; } };
const writeJSON = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window */ } };
let rules = { ...DC.DEFAULT_RULES, ...readJSON(RULES_KEY, {}) };
let record = readJSON(RECORD_KEY, []);
const earnings = new Map();
const COOLDOWN_MIN = 15;
const LIVE_MS = 3000;          // live stock quotes: every 3 s while the page is visible
const live = new Map();        // SYM -> latest live quote
let lastLive = 0, liveBusy = false, liveFailed = false;
let signals = null;
let chart = null;              // the interactive chart (one canvas, redrawn for whichever tab is on screen)

function newTab(from = {}) {
  return {
    id: Math.random().toString(36).slice(2, 9), sym: null, side: "calls", budget: 300, pick: null, exp: null,
    horizon: 5, seed: 1, scenario: null, lever: { price: null, day: 0, iv: 0, volume: 1, touched: false },
    levelR: "", levelS: "", autoR: true, autoS: true, range: "6M", tf: "1d", hbars: 24,
    overlays: { ema: true, sma50: true, sma200: false, bb: false, vol: true }, osc: "rsi",
    ...structuredClone(Object.fromEntries(KEEP.filter((k) => k in from).map((k) => [k, from[k]]))),
    // live data, never saved
    summary: null, expData: null, pickData: null, contract: null, row: null, spot: null, levels: {}, bars: [],
    story: null, mc: null, roll: null, livePnl: null, loading: false, lv: null, view: null,
  };
}

function saveTabs() {
  try {
    localStorage.setItem(SAVE_KEY, JSON.stringify({
      active: view === "home" ? -1 : view === "signals" ? -2 : tabs.indexOf(st), tabs: tabs.map((t) => Object.fromEntries(KEEP.map((k) => [k, t[k]]))),
    }));
  } catch { /* a private window: tabs just aren't remembered */ }
}

function addTab(from) {
  if (tabs.length >= MAX_TABS) return say(`Up to ${MAX_TABS} tabs. Close one first.`, true);
  const t = newTab(from);
  if (from) t.id = Math.random().toString(36).slice(2, 9);
  tabs.push(t);
  if (from && from.sym) {           // a copy starts with the original's data, then refreshes
    Object.assign(t, { summary: from.summary, expData: from.expData, pickData: from.pickData, spot: from.spot,
                       levels: from.levels, bars: from.bars, roll: null, story: null, mc: null });
    buildContract(t);
  }
  switchTo(tabs.length - 1);
  if (!from) $("sym").focus();
}

function closeTab(i) {
  stopPlay();
  tabs.splice(i, 1);
  if (!tabs.length) tabs.push(newTab());
  switchTo(Math.min(tabs.indexOf(st) >= 0 ? tabs.indexOf(st) : i, tabs.length - 1));
}

function switchTo(i) {
  stopPlay();
  view = "tab";
  st = tabs[i];
  saveTabs();
  renderTabs();
  renderTab();
  home?.render();
}

function showSignals() {
  stopPlay();
  view = "signals";
  saveTabs();
  say("");
  renderTabs();
  renderTab();
  signals.seen();
  signals.render();
  home.render();
}

function showHome() {
  stopPlay();
  view = "home";
  saveTabs();
  say("");
  renderTabs();
  renderTab();
  home.render();
}

// Open the tab for a symbol (the monitor and the dock call this), making one if there is none.
function openSymbol(sym) {
  const i = tabs.findIndex((t) => t.sym === sym);
  if (i >= 0) return switchTo(i);
  let j = tabs.findIndex((t) => !t.sym);
  if (j < 0) {
    if (tabs.length >= MAX_TABS) { say(`Up to ${MAX_TABS} tabs. Close one first.`, true); return; }
    tabs.push(newTab());
    j = tabs.length - 1;
  }
  switchTo(j);
  loadSymbol(st, sym, false);
}

// What the home page shows for each tab that holds a contract.
function positions() {
  return tabs.map((t, index) => ({ t, index })).filter(({ t }) => t.pick && t.contract).map(({ t, index }) => {
    const c = t.contract, p = t.pick, L = t.lever, row = t.row;
    const simPrice = L.touched && L.price != null ? L.price : t.spot;
    const mark = t.estMark ?? (row ? row.mid ?? row.ask : p.entry);
    const closes = histCloses(t);
    const sc = S.SCENARIOS.find((x) => x.id === t.scenario);
    const moved = L.touched && (L.day > 0 || L.iv !== 0 || Math.abs(simPrice - t.spot) > 1e-9);
    return {
      index, name: `${p.symbol} ${p.strike % 1 ? p.strike.toFixed(2) : p.strike} ${p.isCall ? "CALL" : "PUT"}`, isCall: p.isCall,
      symbol: p.symbol, expires: `exp ${dateLabel(p.expiration)} (${c.dte}d)`, qty: p.qty, entry: p.entry, cost: p.entry * 100 * p.qty,
      livePnl: t.livePnl, livePct: 100 * (mark - p.entry) / p.entry,
      simPnl: S.pnlAt(c, simPrice, L.day, 1 + L.iv / 100),
      simNote: !moved ? "levers at now" : `${sc ? sc.name.toLowerCase() + " · " : ""}${money(simPrice)} at ${ahead(L.day, t)}`,
      spot: t.spot, changePct: t.summary?.change_pct ?? null, strike: c.strike, breakeven: S.breakeven(c),
      read: closes.length >= 30 ? I.readout(closes) : null, lv: t.lv,
      decision: t.decision, netNow: t.netNow, earn: t.earn,
    };
  });
}

const shortName = (t) => {
  if (!t.sym) return "NEW";
  if (!t.pick) return t.sym;
  const k = t.pick.strike % 1 ? t.pick.strike.toFixed(1) : t.pick.strike;
  return `${t.sym} ${k}${t.pick.isCall ? "C" : "P"}`;
};

function renderTabs() {
  const box = $("tabs");
  $("tab-home").classList.toggle("on", view === "home");
  $("tab-home").setAttribute("aria-selected", view === "home");
  $("tab-signals").classList.toggle("on", view === "signals");
  $("tab-signals").setAttribute("aria-selected", view === "signals");
  box.replaceChildren(...tabs.map((t, i) => {
    const el = document.createElement("div");
    el.className = `tab${t === st && view === "tab" ? " on" : ""}${t.pick ? (t.pick.isCall ? " call" : " put") : ""}`;
    el.innerHTML = `<button class="tab-main" role="tab"><b></b><small></small></button><button class="tab-x" aria-label="Close tab">×</button>`;
    const [main, x] = el.children;
    main.setAttribute("aria-selected", t === st && view === "tab");
    main.firstChild.textContent = shortName(t);
    const sub = main.lastChild;
    if (t.loading) sub.textContent = "loading…";
    else if (t.decision && t.netNow != null) {
      sub.textContent = `${t.decision.word} ${signed(t.netNow, 0)}`;
      sub.className = `dw ${t.decision.code}`;
    } else if (t.livePnl != null) { sub.textContent = signed(t.livePnl, 0); tone(sub, t.livePnl); }
    else sub.textContent = t.scenario ? S.SCENARIOS.find((s) => s.id === t.scenario)?.name.toLowerCase() ?? "" : t.sym ? "pick a contract" : "empty";
    main.addEventListener("click", () => switchTo(i));
    x.addEventListener("click", (e) => { e.stopPropagation(); closeTab(i); });
    return el;
  }));
  $("tab-add").disabled = tabs.length >= MAX_TABS;
}

// ---------------------------------------------------------------- formatting
const money = (v, dp = 2) => (v == null || !isFinite(v) ? "—" : `$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp })}`);
const signed = (v, dp = 2) => (v == null || !isFinite(v) ? "—" : `${v < 0 ? "−" : "+"}${money(v, dp)}`);
const pct = (v, dp = 0) => (v == null || !isFinite(v) ? "" : `${v < 0 ? "−" : "+"}${Math.abs(v).toFixed(dp)}%`);
const dateLabel = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
const tone = (el, v) => { el.classList.toggle("up", v > 0.005); el.classList.toggle("down", v < -0.005); };

const tweens = new Map();
function glide(el, to, fmt) {                      // numbers roll to their new value instead of jumping
  const t = tweens.get(el) || { cur: to, raf: 0 };
  tweens.set(el, t);
  cancelAnimationFrame(t.raf);
  const from = isFinite(t.cur) ? t.cur : to, start = performance.now();
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const tick = (now) => {
    const k = reduce ? 1 : Math.min((now - start) / 260, 1);
    t.cur = from + (to - from) * (1 - Math.pow(1 - k, 3));
    el.textContent = fmt(t.cur);
    if (k < 1) t.raf = requestAnimationFrame(tick);
  };
  t.raf = requestAnimationFrame(tick);
}

function say(text, bad = false) {
  const m = $("msg");
  m.hidden = !text;
  m.textContent = text || "";
  m.classList.toggle("bad", bad);
}

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `The server answered ${r.status}.`);
  return body;
}

// ---------------------------------------------------------------- data, per tab (safe to run for a tab that isn't on screen)
async function loadSymbol(t, sym, keepPick = false) {
  if (t === st && view === "tab") say(`Loading ${sym.toUpperCase()}…`);
  t.loading = true;
  try {
    const summary = await api(`/api/chain/${encodeURIComponent(sym)}`);
    const changed = t.sym !== summary.symbol;
    t.sym = summary.symbol;
    t.summary = summary;
    t.spot = summary.price;
    if (!keepPick || changed && t.pick && t.pick.symbol !== t.sym) clearPick(t);
    if (changed) { t.levelR = ""; t.levelS = ""; t.autoR = t.autoS = true; t.bars = []; t.levels = {}; t.view = null; }
    const bars = loadBars(t);
    const want = t.pick ? t.pick.expiration : t.exp;
    const first = summary.expirations.find((e) => e.date === want) || summary.expirations.find((e) => e.dte >= 7) || summary.expirations[0];
    await loadExp(t, first.date, false);
    await bars;
    if (t === st && view === "tab") say("");
  } catch (e) {
    if (t === st && view === "tab") say(e.message, true);
  }
  t.loading = false;
  saveTabs();
  renderTabs();
  if (t === st) renderTab();
  home?.render();
}

// The tab's bars on its timeframe. `levels` (volatility, average volume) always describes the daily bars.
async function loadBars(t) {
  const sym = t.sym, tf = t.tf;
  try {
    const b = await api(`/api/bars/${encodeURIComponent(sym)}?tf=${tf}`);
    if (t.sym !== sym || t.tf !== tf) return;                // the tab moved on while this was loading
    t.bars = b.bars;
    t.levels = b.levels;
    t.barsAt = Date.now();
    computeLevels(t);
  } catch (e) {
    if (t === st && view === "tab") say(e.message, true);
  }
}

async function loadExp(t, date, render = true) {
  try {
    const d = await api(`/api/chain/${encodeURIComponent(t.sym)}/${date}`);
    t.exp = date;
    t.expData = d;
    t.spot = live.get(t.sym)?.price ?? d.price;
    if (t.pick && t.pick.expiration === date) t.pickData = d;
    buildContract(t);
    if (render && t === st) { renderExps(); renderLadder(); onQuote(); }
  } catch (e) {
    if (t === st) say(e.message, true);
  }
}

// Every 15 s: refresh each tab's quote (the server shares one fetch per ticker), so every tab's live P&L moves.
// Every 15 s while the page is visible: refresh each tab's quote (the server shares one fetch per ticker), so every
// tab's live P&L moves, then the home page. Coming back to the page refreshes at once.
let lastPoll = 0, polling = false, pollFailed = false;
async function pollNow() {
  if (polling) return;
  polling = true;
  let failed = false;
  try {
    await Promise.all(tabs.filter((t) => t.sym && !t.loading).map(async (t) => {
      const date = t.pick ? t.pick.expiration : t.exp;
      if (!date) return;
      try {
        const d = await api(`/api/chain/${encodeURIComponent(t.sym)}/${date}`);
        const q = live.get(t.sym);
        if (!q) {                                  // no live quote yet: the delayed chain's price stands in
          t.spot = d.price;
          t.summary = { ...t.summary, price: d.price, change_pct: d.change_pct, stale: d.stale };
        }
        if (date === t.exp) t.expData = d;
        if (t.pick) t.pickData = d;
        buildContract(t);
        // intraday bars move all day: refresh them with every poll; daily and weekly every 10 minutes
        if (Date.now() - (t.barsAt || 0) > (unit(t).intraday ? 10000 : 600000)) await loadBars(t);
        computeLevels(t);
      } catch { failed = true; if (t.summary) t.summary = { ...t.summary, stale: true }; }
    }));
    renderTabs();
    if (view === "tab" && st.sym) {
      showLive(st.summary);
      if (!$("pick").hidden) renderLadder();
      onQuote();
    }
    await home.refresh();
    await signals.refresh();
    await loadEarnings();
  } catch { failed = true; }
  polling = false;
  pollFailed = failed;
  lastPoll = Date.now();
  renderFresh();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!document.hidden) await pollNow();
    schedulePoll();
  }, POLL_MS);
}

// Earnings dates for every ticker on screen (the server keeps the calendar; this is cheap).
async function loadEarnings() {
  const syms = [...new Set([...tabs.filter((t) => t.sym).map((t) => t.sym), ...home.symbols()])];
  if (!syms.length) return;
  try {
    const r = await api(`/api/earnings?symbols=${encodeURIComponent(syms.join(","))}`);
    for (const s of syms) { if (r.earnings[s]) earnings.set(s, r.earnings[s]); else if (!r.loading) earnings.delete(s); }
  } catch { /* the calendar is optional: decisions still work without it */ }
}

// Where option quotes come from: Tastytrade (live) when a login is set up on the server, else CBOE (15 min late).
let optSource = { options: "cboe", error: null };
const optLive = () => optSource.options === "tastytrade";
async function loadSource() {
  try { optSource = await api("/api/source"); } catch { /* keep the default */ }
  const note = $("live-note");
  note.textContent = optLive() ? "LIVE · OPTIONS LIVE (TASTYTRADE)" : "LIVE · OPTIONS 15 MIN DELAYED";
  note.classList.toggle("warn", !!optSource.error);
  note.title = (optLive()
    ? "Stock prices and option quotes are live (option quotes from Tastytrade, refreshed every 15 s; P&L moves with the stock in between)."
    : "The stock price is live. Option quotes come from CBOE 15 minutes late; the P&L shown is a live estimate from the stock's price now.")
    + (optSource.error ? ` Tastytrade login problem: ${optSource.error}` : "");
}

// Relative volume for every ticker on screen: 5-minute bars, refreshed once a minute per ticker.
const volume = V.createVolume((u) => api(u));
const volRead = (sym) => volume.read(sym, DC.marketClock());

// A "VOL 1.8x" chip with a meter (tab header, home cards, dock): normal for this time of day = 1.0.
function volChip(sym, small = false) {
  const v = V.describe(volRead(sym)), b = document.createElement("span");
  b.className = `vchip ${v?.cls || ""}${small ? " small" : ""}`;
  if (!v) { b.textContent = "VOL …"; b.title = "Loading the last two weeks of 5-minute volume…"; return b; }
  const m = document.createElement("i"); m.style.setProperty("--f", v.fill.toFixed(2));
  const t = document.createElement("b"); t.textContent = v.text;
  const w = document.createElement("em"); w.textContent = v.word;
  b.append(t, m, w);
  if (v.recentText) { const r = document.createElement("small"); r.textContent = v.recentText; if (v.recentHot) r.className = "hot"; b.append(r); }
  b.title = v.title;
  return b;
}

// The fast loop: live stock quotes for every ticker on every page, in one request. Tab headers, the options'
// live estimates, the home page, the dock and the signal board all move with it.
async function livePoll() {
  if (liveBusy || document.hidden) return;
  const syms = [...new Set([...tabs.filter((t) => t.sym).map((t) => t.sym), ...home.symbols(), ...signals.symbols()])];
  if (!syms.length) { lastLive = Date.now(); return renderFresh(); }
  liveBusy = true;
  try {
    const r = await api(`/api/quotes?symbols=${encodeURIComponent(syms.slice(0, 40).join(","))}`);
    for (const [sym, q] of Object.entries(r.quotes)) if (!q.error) live.set(sym, q);
    volume.refresh(syms.slice(0, 40));                // not awaited: at most once a minute per ticker
    for (const t of tabs) {
      const q = t.sym && live.get(t.sym);
      if (!q) continue;
      t.spot = q.price;
      t.summary = { ...(t.summary || { symbol: t.sym }), price: q.price, change_pct: q.change_pct, stale: q.stale };
      buildContract(t);
    }
    renderTabs();
    if (view === "tab" && st.sym && st.summary) {
      showLive(st.summary);
      if (!$("pick").hidden) renderLadder();
      onQuote();
    }
    home.applyQuotes(live);
    signals.applyQuotes(live);
    liveFailed = false;
    lastLive = Date.now();
  } catch { liveFailed = true; }
  liveBusy = false;
  renderFresh();
}

// "UPDATED 4s AGO" in the header, ticking every second; amber when updates stop arriving.
function renderFresh() {
  const el = $("fresh");
  const last = Math.max(lastLive, lastPoll);
  if (!last) { el.textContent = "CONNECTING…"; return; }
  const ago = Math.round((Date.now() - last) / 1000);
  const late = ago > 15 || !navigator.onLine;
  el.textContent = !navigator.onLine ? "OFFLINE" : `LIVE · ${ago < 60 ? ago + "s" : Math.floor(ago / 60) + "m"} AGO`;
  el.classList.toggle("late", late || liveFailed || pollFailed);
  el.title = `Stock prices, trends, the home page and signals refresh every ${LIVE_MS / 1000} s; option quotes and candles every `
    + `${POLL_MS / 1000} s (option quotes ${optLive() ? "live from Tastytrade" : "are 15 minutes old at the source"}).${liveFailed || pollFailed ? " The last refresh missed some data; it retries." : ""}`;
}

// A new version of the app on the server: reload into it (tabs and settings are saved in the browser), but never in
// the middle of a PLAY.
let version = null, reloading = false;
async function checkVersion() {
  if (reloading) return;
  try {
    const v = (await api("/api/version")).version;
    if (version == null) { version = v; return; }
    if (v === version) return;
    if (playing) { setTimeout(checkVersion, 5000); return; }
    reloading = true;
    saveTabs();
    say("New version installed. Reloading…");
    setTimeout(() => location.reload(), 1200);
  } catch { /* the server is restarting: try again next minute */ }
}

function refreshNow() {
  if (document.hidden) return;
  checkVersion();
  livePoll();
  if (Date.now() - lastPoll > 5000) { clearTimeout(pollTimer); pollNow().then(schedulePoll); }
}

function buildContract(t) {
  const p = t.pick, d = t.pickData;
  if (!p || !d || d.date !== p.expiration) return;
  const row = d[p.isCall ? "calls" : "puts"].find((r) => r.occ === p.occ);
  if (!row) return;
  t.row = row;
  const c = S.makeContract(row, p.isCall, d.dte, d.price, p.qty);
  c.entry = p.entry;
  t.contract = c;
  // The option quote is 15 minutes old; the stock price isn't. Move the quoted price by what the model says the
  // stock's move since then is worth, at the option's own implied volatility (estMark). P&L uses that estimate.
  const quoted = row.mid ?? row.ask;
  const est = S.liveEstimate(quoted, d.price, t.spot ?? d.price, c.strike, d.dte, c.iv, c.isCall);
  t.quoteMark = quoted;
  t.estMark = est;
  t.quotePnl = (quoted - p.entry) * 100 * p.qty;
  t.livePnl = (est - p.entry) * 100 * p.qty;
  // what you could sell for: the bid, moved the same way, and what that nets after fees
  const R = rulesOf(t), clock = DC.marketClock();
  t.bidEst = row.bid > 0 ? S.liveEstimate(row.bid, d.price, t.spot ?? d.price, c.strike, d.dte, c.iv, c.isCall) : 0;
  t.netNow = DC.netNow({ entry: p.entry, bid: t.bidEst, qty: p.qty, fees: R.fees });
  t.earn = DC.earningsInPlan(earnings.get(t.sym), R, clock, p.expiration);
  const was = t.decision?.code;
  t.decision = DC.status({ net: t.netNow, rules: R, clock, dte: d.dte, earn: t.earn });
  if (was && was !== t.decision.code) decisionAlert(t);
}

const rulesOf = (t) => ({ ...DC.DEFAULT_RULES, ...rules, ...(t.pick?.rules || {}) });

// A position's word changed: say so wherever you are.
function decisionAlert(t) {
  const d = t.decision, p = t.pick;
  const title = `${p.symbol} ${p.strike}${p.isCall ? "C" : "P"}: ${d.word}`;
  const box = $("sb-toasts"), el = document.createElement("button");
  el.className = `toast ${d.code === "TAKE" || d.code === "HOLD" ? "g" : "r"}`;
  el.innerHTML = "<b></b><span></span>";
  el.firstChild.textContent = title;
  el.lastChild.textContent = d.text;
  el.addEventListener("click", () => { el.remove(); switchTo(tabs.indexOf(t)); });
  box.prepend(el);
  setTimeout(() => el.classList.add("out"), 15000);
  setTimeout(() => el.remove(), 15600);
  if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    try { new Notification(title, { body: d.text, tag: `optlab-${t.id}` }); } catch { /* not allowed here */ }
  }
}

// Support and resistance over the tab's RANGE; the nearest of each drives the scenarios unless you picked another.
function computeLevels(t) {
  if (!t.bars.length || !t.spot) return;
  t.lv = LV.findLevels(t.bars, LV.rangeBars(t.tf, t.range), t.spot, 4, LV.TF_TOL[t.tf]);
  if (t.autoR) t.levelR = t.lv.resistance[0] ? String(t.lv.resistance[0].price) : "";
  if (t.autoS) t.levelS = t.lv.support[0] ? String(t.lv.support[0].price) : "";
}

function clearPick(t) {
  Object.assign(t, { pick: null, contract: null, row: null, pickData: null, scenario: null, mc: null, story: null, roll: null, livePnl: null });
}

// ---------------------------------------------------------------- rendering the tab on screen
function renderTab() {
  $("home").hidden = view !== "home";
  $("signals").hidden = view !== "signals";
  if (view !== "tab") {
    for (const id of ["empty", "pick", "hud", "deck", "live"]) $(id).hidden = true;
    $("sym").value = "";
    return;
  }
  $("sym").value = st.sym || "";
  if (st.summary) showLive(st.summary); else $("live").hidden = true;
  $("empty").hidden = !!st.sym || st.loading;
  $("pick").hidden = !st.sym || !!st.pick;
  $("hud").hidden = $("deck").hidden = !st.pick || !st.contract;
  $("side-call").setAttribute("aria-pressed", st.side === "calls");
  $("side-put").setAttribute("aria-pressed", st.side === "puts");
  $("budget").value = st.budget;
  $("lv-r").value = st.levelR;
  $("lv-s").value = st.levelS;
  $("l-vol").value = st.lever.volume;
  for (const b of $("overlays").children) b.setAttribute("aria-pressed", !!st.overlays[b.dataset.o]);
  renderRanges();
  $("sig-pick-src").textContent = `TREND ON ${LV.TF_LABEL[st.tf] || "DAILY"} BARS`;
  if (chart) chart.view = st.view ? { ...st.view } : null;
  for (const b of $("osc-pick").children) b.setAttribute("aria-pressed", b.dataset.o === st.osc);
  if (st.summary) renderExps();
  renderLadder();
  renderSignals("sig-pick", histCloses(st), "NOW", st.levels.rel_volume);
  onQuote();
}

function showLive(d) {
  if (!d) return;
  $("live").hidden = false;
  $("live-sym").textContent = d.symbol;
  glide($("live-px"), d.price, (v) => money(v));
  const chg = $("live-chg");
  chg.textContent = pct(d.change_pct, 2);
  tone(chg, d.change_pct || 0);
  $("dot").classList.toggle("stale", !!d.stale);
  $("live-vol").replaceChildren(volChip(d.symbol));
}

function renderExps() {
  if (!st.summary?.expirations) return;            // only a live price so far: the chain didn't load
  $("exps").replaceChildren(...st.summary.expirations.slice(0, 24).map((e) => {
    const b = document.createElement("button");
    b.className = "chip";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", e.date === st.exp);
    b.innerHTML = `<b></b><small></small>`;
    b.firstChild.textContent = dateLabel(e.date);
    b.lastChild.textContent = e.dte === 0 ? "today" : `${e.dte}d`;
    b.addEventListener("click", () => loadExp(st, e.date));
    return b;
  }));
}

function renderLadder() {
  const d = st.expData;
  $("budget-out").textContent = money(st.budget, 0);
  if (!d || $("pick").hidden) return;
  const rows = d[st.side].filter((r) => r.ask > 0);
  if (!rows.length) { $("ladder").replaceChildren(); $("ladder-hint").textContent = "No quotes for this expiration."; return; }
  const px = st.spot ?? d.price;
  const atm = rows.reduce((best, r, i) => (Math.abs(r.strike - px) < Math.abs(rows[best].strike - px) ? i : best), 0);
  const view = rows.slice(Math.max(0, atm - 12), atm + 13);
  const isCall = st.side === "calls";
  const ctx0 = goContext(d, px, isCall);
  let affordable = 0, best = null;
  const els = view.map((r) => {
    const cost = r.ask * 100, itm = isCall ? r.strike < px : r.strike > px, over = cost > st.budget;
    if (!over) affordable++;
    const v = judge(r, d, px, isCall, ctx0);
    if (!over && !v.lot.lottery && (!best || (v.go.go && !best.v.go.go) || (v.go.go === best.v.go.go && (v.odds?.pTarget ?? 0) > (best.v.odds?.pTarget ?? 0)))) best = { r, v };
    const b = document.createElement("button");
    b.className = `row ${isCall ? "call" : "put"}${over ? " over" : ""}${v.lot.lottery ? " lottery" : ""}`;
    b.innerHTML = `<span class="cells"><span class="strike"></span><span class="cost"></span><span class="odds"></span><span class="verdict"></span></span>`;
    const [strike, costEl, oddsEl, verdict] = b.firstChild.children;
    strike.textContent = r.strike % 1 ? r.strike.toFixed(2) : r.strike;
    if (itm) strike.insertAdjacentHTML("beforeend", "<small>IN THE MONEY</small>");
    costEl.textContent = money(cost, 0);
    oddsEl.innerHTML = "<b></b><small></small>";
    oddsEl.firstChild.textContent = v.odds ? `${Math.round(100 * v.odds.pTarget)}%` : "—";
    oddsEl.lastChild.textContent = v.odds ? `stop ${Math.round(100 * v.odds.pStop)}%` : "";
    verdict.textContent = v.lot.lottery ? "LOTTERY" : v.go.go ? "GO" : "WAIT";
    verdict.className = `verdict ${v.lot.lottery ? "lot" : v.go.go ? "go" : "wait"}`;
    b.title = v.lot.lottery ? `Lottery ticket: ${v.lot.why.join(", ")}` : `${v.go.go ? "GO" : "WAIT"}: ${v.go.reason}`
      + (v.odds?.sTarget ? `\n+$${rulesOf(st).target} needs ${st.sym} at ${v.odds.sTarget.toFixed(2)} (${pct(v.odds.move, 1)})` : "");
    b.dataset.occ = r.occ;
    b.addEventListener("click", () => choose(r, isCall));
    return b;
  });
  renderGoCard(best, d, isCall);
  const mark = document.createElement("div");
  mark.className = "spot-line";
  mark.textContent = `${st.sym} is at ${money(px)} now${optLive() ? "" : " · option prices are 15 min old"}`;
  const at = view.findIndex((r) => r.strike > px);
  els.splice(at < 0 ? els.length : at, 0, mark);
  const box = $("ladder"), key = `${st.id}|${st.sym}|${st.exp}|${st.side}`, keep = box.dataset.for === key ? box.scrollTop : null;
  box.replaceChildren(...els);
  box.dataset.for = key;
  box.scrollTop = keep ?? Math.max(0, mark.offsetTop - box.clientHeight / 2 + 20);   // open at the stock's price
  if (best) box.querySelector(`[data-occ="${best.r.occ}"]`)?.classList.add("best");
  $("ladder-hint").textContent = affordable ? `${affordable} of these fit your budget (dimmed ones cost more). "Chance" is how likely the stock reaches the price where one contract nets +$${rulesOf(st).target} at the bid before your exit; "stop" is the same for your −$${rulesOf(st).stop} stop. Hover a row for its reason.`
    : "Nothing here fits that budget. Raise it, or pick a nearer expiration.";
}

// What every row on the ladder is judged against: the trend on this tab's timeframe, the nearest levels, earnings,
// the time, and the cool-down after a CUT.
function goContext(d, px, isCall) {
  const closes = histCloses(st), rd = closes.length >= 30 ? I.readout(closes) : null;
  const clock = DC.marketClock(), R = { ...DC.DEFAULT_RULES, ...rules };
  const lastCut = record.find((x) => x.word === "CUT");
  const cooldownLeft = lastCut ? COOLDOWN_MIN - (Date.now() - lastCut.at) / 60000 : 0;
  return {
    clock, R, hz: DC.horizon(R, clock, d.dte),
    trend: rd ? { verdict: rd.verdict.value, short: rd.short?.tone || "flat" } : null,
    resistance: st.lv?.resistance.find((l) => l.price > px)?.price ?? null,
    support: st.lv?.support.find((l) => l.price < px)?.price ?? null,
    earn: DC.earningsInPlan(earnings.get(st.sym), R, clock, st.exp), cooldownLeft, isCall,
  };
}

function judge(r, d, px, isCall, c) {
  const mid = r.mid ?? r.ask;
  const iv = S.impliedVol(mid, d.price, r.strike, Math.max(d.dte, 0.5) / 365, isCall) ?? r.iv ?? 0.3;
  const shift = S.liveEstimate(mid, d.price, px, r.strike, d.dte, iv, isCall) - mid;     // the 15-minute-old quote, moved
  const bid = r.bid > 0 ? Math.max(r.bid + shift, 0.01) : 0, ask = Math.max(r.ask + shift, 0.01);
  const odds = DC.targetOdds({ spot: px, strike: r.strike, isCall, bid, ask, dte: d.dte, iv, qty: 1, rules: c.R, hz: c.hz });
  const lot = DC.isLottery({ delta: r.delta, bid: r.bid, ask: r.ask, dte: d.dte });
  const go = DC.goCheck({ isCall, cost: r.ask * 100, budget: st.budget, odds, lot, trend: c.trend, resistance: c.resistance,
                          support: c.support, earn: c.earn, rules: c.R, clock: c.clock, dte: d.dte, cooldownLeft: c.cooldownLeft });
  return { odds, lot, go };
}

// The card above the ladder: the best contract for your rules, and GO or WAIT with every check.
function renderGoCard(best, d, isCall) {
  const card = $("go-card"), R = { ...DC.DEFAULT_RULES, ...rules };
  if (!document.activeElement?.closest?.("#go-rules")) {
    $("gr-target").value = R.target;
    $("gr-stop").value = R.stop;
    $("gr-mode").textContent = R.mode === "swing" ? "SWING" : "DAY TRADE";
    $("gr-mode").setAttribute("aria-pressed", R.mode === "swing");
    $("gr-fees").value = R.fees;
  }
  if (!best) {
    card.className = "go-card WAIT";
    $("go-word").textContent = "WAIT";
    const ok = d[st.side].filter((r) => r.ask > 0 && !DC.isLottery({ delta: r.delta, bid: r.bid, ask: r.ask, dte: d.dte }).lottery)
      .sort((a, b) => a.ask - b.ask)[0];
    $("go-what").textContent = `Nothing on this expiration fits: every ${isCall ? "call" : "put"} under $${st.budget} is a lottery ticket.`
      + (ok ? ` The cheapest that isn't costs ${money(ok.ask * 100, 0)} (${ok.strike}). Try another expiration, ${isCall ? "puts" : "calls"}, or a cheaper stock.` : "");
    $("go-checks").replaceChildren();
    $("go-buy").hidden = true;
    return;
  }
  const { r, v } = best;
  card.className = `go-card ${v.go.go ? "GO" : "WAIT"}`;
  $("go-word").textContent = v.go.go ? "GO" : "WAIT";
  $("go-what").textContent = `${st.sym} ${r.strike % 1 ? r.strike.toFixed(2) : r.strike} ${isCall ? "CALL" : "PUT"} · ${dateLabel(d.date)} · ${money(r.ask * 100, 0)}`
    + (v.odds ? ` · ${Math.round(100 * v.odds.pTarget)}% chance of +$${R.target}, ${Math.round(100 * v.odds.pStop)}% of the stop` : "")
    + (v.go.go ? "" : ` · waiting on: ${v.go.reason}`);
  $("go-checks").replaceChildren(...v.go.checks.map((c) => {
    const li = document.createElement("li");
    li.className = c.ok ? "ok" : "no";
    li.textContent = `${c.ok ? "✓" : "✗"} ${c.label}`;
    if (!c.ok) li.title = c.why;
    return li;
  }));
  $("go-buy").hidden = false;
  $("go-buy").textContent = v.go.go ? "I BOUGHT IT" : "BUY ANYWAY (I BOUGHT IT)";
  $("go-buy").onclick = () => choose(r, isCall);
}

function choose(r, isCall) {
  st.pick = { symbol: st.sym, expiration: st.exp, isCall, occ: r.occ, strike: r.strike, entry: r.ask, qty: 1, rules: { ...rules },
              boughtAt: Date.now() };
  st.pickData = st.expData;
  buildContract(st);
  resetLevers(false);
  saveTabs();
  renderTabs();
  renderTab();
  $("hud").scrollIntoView({ behavior: "smooth", block: "start" });
}

// The position's one word, how far it is from the target and the stop, and its rules.
function renderDecision() {
  const p = st.pick, d = st.decision;
  if (!p || !d) return;
  const R = rulesOf(st), box = $("decide");
  box.className = `decide ${d.code}`;
  $("dc-word").textContent = d.word;
  $("dc-text").textContent = d.text;
  const span = R.target + R.stop, at = Math.min(1, Math.max(0, (st.netNow + R.stop) / span));
  $("dc-bar").style.setProperty("--x", `${(100 * at).toFixed(1)}%`);
  $("dc-bar").style.setProperty("--zero", `${(100 * R.stop / span).toFixed(1)}%`);
  $("dc-lo").textContent = `STOP −$${R.stop}`;
  $("dc-hi").textContent = `TARGET +$${R.target}`;
  $("dc-net").textContent = `If you sold now: ${signed(st.netNow, 0)} net (at the bid${R.fees ? ", after fees" : ""}, estimated)`;
  tone($("dc-net"), st.netNow);
  $("dc-earn").textContent = st.earn ? `Earnings: ${st.sym} ${st.earn.text}.` : "";
  $("dc-earn").hidden = !st.earn;
  if (document.activeElement?.closest?.("#dc-rules")) return;     // don't overwrite what's being typed
  $("dc-target").value = R.target;
  $("dc-stop").value = R.stop;
  $("dc-mode").textContent = R.mode === "swing" ? "SWING" : "DAY TRADE";
  $("dc-mode").setAttribute("aria-pressed", R.mode === "swing");
  $("dc-hold").checked = !!R.holdEarnings;
  $("dc-hold-wrap").hidden = R.mode !== "swing";
  if (document.activeElement !== $("dc-sold")) $("dc-sold").value = st.bidEst ? st.bidEst.toFixed(2) : "";
}

function onQuote() {
  const p = st.pick, c = st.contract, row = st.row;
  if (!p || !c || !row) return;
  $("hud").hidden = $("deck").hidden = false;
  $("hud-name").textContent = `${p.symbol} ${p.strike % 1 ? p.strike.toFixed(2) : p.strike} ${p.isCall ? "CALL" : "PUT"}`;
  $("hud-name").className = `hud-name ${p.isCall ? "call" : "put"}`;
  $("hud-sub").textContent = `expires ${dateLabel(p.expiration)} · ${c.dte === 0 ? "today" : c.dte + " days left"} · bought at ${money(p.entry)}`;
  $("qty").textContent = p.qty;
  $("hud-cost").textContent = money(p.entry * 100 * p.qty, 0);
  $("hud-be").textContent = money(S.breakeven(c));
  const mark = st.estMark ?? row.mid ?? row.ask;
  glide($("hud-val"), mark * 100 * p.qty, (v) => money(v, 0));
  glide($("hud-pnl"), st.livePnl, (v) => signed(v, 0));
  tone($("hud-pnl"), st.livePnl);
  $("hud-pnl-pct").textContent = pct(100 * (mark - p.entry) / p.entry, 1);
  $("hud-quote").textContent = `${optLive() ? "last quote" : "delayed quote"}: ${money(st.quoteMark * 100 * p.qty, 0)} (${signed(st.quotePnl, 0)})`;
  renderDecision();
  if (!playing) refreshSim(false);
}

// ---------------------------------------------------------------- levers and scenarios
function sigmaDay() {
  const yearly = st.levels.hv || st.summary?.iv30 || st.contract?.iv || 0.3;
  return yearly / Math.sqrt(365);
}

function ctx() {
  const r = parseFloat(st.levelR), s = parseFloat(st.levelS);
  return { price: st.spot, resistance: isFinite(r) ? r : null, support: isFinite(s) ? s : null,
           sigmaDay: sigmaDay(), volume: st.lever.volume, days: horizonDays(), spd: unit().spd };
}

function resetLevers(paintNow = true) {
  stopPlay();
  st.lever = { price: st.spot, day: 0, iv: 0, volume: 1, touched: false };
  st.scenario = null; st.mc = null; st.story = null; st.roll = null;
  st.horizon = Math.max(1, Math.min(st.pickData?.dte || 5, 5));
  $("l-vol").value = 1;
  saveTabs();
  if (paintNow) { renderTabs(); refreshSim(true); }
}

function refreshSim(rebuild) {
  const c = st.contract;
  if (!c || $("deck").hidden) return;
  const L = st.lever;
  if (!L.touched || L.price == null) L.price = st.spot;
  const u = unit();
  const span = u.intraday ? Math.max(0.04, 6 * sigmaDay() * Math.sqrt(Math.max(horizonDays(), 1)))
    : Math.max(0.12, 5 * sigmaDay() * Math.sqrt(Math.max(st.horizon, 5)));
  const lp = $("l-price");
  lp.min = (st.spot * (1 - span)).toFixed(2);
  lp.max = (st.spot * (1 + span)).toFixed(2);
  lp.value = L.price;
  $("l-day").max = u.intraday ? Math.min(Math.max(c.dte, 1), 3) : Math.max(c.dte, 1);
  $("l-day").step = u.intraday ? String(Math.round(1e5 / u.spd) / 1e5) : "0.125";
  $("l-day").value = L.day;
  $("l-iv").value = L.iv;
  st.horizon = Math.max(1, Math.min(st.horizon, Math.max(c.dte, 1), 45));
  if (u.intraday) st.hbars = Math.max(u.hStep, Math.min(st.hbars || u.hDef, u.hMax));
  $("horizon").textContent = u.intraday ? hours(st.hbars, u) : st.horizon;
  $("horizon-unit").textContent = u.intraday ? `(${st.hbars} BARS)` : "DAYS";
  if (rebuild || (st.scenario && !st.story)) rebuildScenario();
  paint();
}

function rebuildScenario() {
  if (!st.scenario || !st.contract) { st.story = null; st.mc = null; return; }
  const x = ctx();
  st.story = S.simulatePath(st.scenario, x);
  st.mc = S.monteCarlo(st.scenario, x, st.contract, 300, st.seed);
}

// ---------------------------------------------------------------- indicators on real history + the simulated future
function histCloses(t) {
  const c = t.bars.map((b) => b.c);
  if (c.length && t.spot) c[c.length - 1] = t.spot;     // today's bar is still forming: use the live price
  return c;
}

// The simulated closes, one per chart bar, up to `step`.
function barCloses(path, step) {
  const out = [], spb = unit().spb;
  for (let k = spb; k <= step && k < path.price.length; k += spb) out.push(path.price[k]);
  return out;
}

// History plus the simulated bars up to where the levers are: what the indicators would read then.
function closesAtLever() {
  const hist = histCloses(st), path = st.roll || st.story, L = st.lever, u = unit();
  const step = st.roll ? st.roll.upto : Math.round(L.day * u.spd);
  const fut = path ? barCloses(path, step) : [];
  const moved = L.touched && (Math.abs(L.price - st.spot) > 1e-9 || L.day > 0);
  if (moved && !(path && step % u.spb === 0 && fut.length && fut.at(-1) === L.price)) fut.push(L.price);
  return { closes: hist.concat(fut), histLen: hist.length, simulated: fut.length > 0 };
}

function renderSignals(id, closes, when, relVol) {
  const box = $(id);
  if (closes.length < 30) { box.replaceChildren(); return; }
  const r = I.readout(closes);
  const cards = [
    ["verdict", "TREND", r.verdict],
    ["short", "SHORT · EMA 9/21", r.short],
    ["medium", "MEDIUM · SMA 50", r.medium],
    ["long", "LONG · SMA 200", r.long],
    ["momentum", "MOMENTUM · MACD", r.momentum],
    ["rsi", "RSI 14", r.rsi],
    ["bands", "BOLLINGER", r.bands],
  ];
  if (relVol != null) cards.push(["vol", "VOLUME", { tone: relVol >= 1.5 ? "hot" : relVol <= 0.6 ? "cold" : "flat", value: `${relVol.toFixed(1)}×`, text: relVol >= 1.5 ? "heavy: moves carry further" : relVol <= 0.6 ? "light: moves fade" : "normal" }]);
  box.replaceChildren(...cards.filter(([, , v]) => v).map(([key, label, v]) => {
    const el = document.createElement("div");
    el.className = `sig ${v.tone}${key === "verdict" ? " verdict" : ""}`;
    el.innerHTML = `<span class="k"></span><span class="v"></span><span class="k2"></span>`;
    el.children[0].textContent = label;
    el.children[1].textContent = v.value;
    el.children[2].textContent = v.text;
    return el;
  }));
  const head = $(id === "sig-pick" ? "sig-pick-when" : "sig-sim-when");
  if (head) head.textContent = when;
}

// ---------------------------------------------------------------- the main redraw
function paint() {
  const c = st.contract, L = st.lever;
  if (!c) return;
  const ivMult = 1 + L.iv / 100;
  const val = S.valueAt(c, L.price, L.day, ivMult), pnl = S.pnlAt(c, L.price, L.day, ivMult);
  $("l-price-out").textContent = `${money(L.price)} (${pct(100 * (L.price / st.spot - 1), 1)})`;
  $("l-day-out").textContent = L.day === 0 ? "now" : ahead(L.day);
  $("l-iv-out").textContent = `${(c.iv * ivMult * 100).toFixed(0)}% (${pct(L.iv)})`;
  $("l-vol-out").textContent = `${L.volume.toFixed(1)}× normal`;
  const el = $("sim-pnl");
  glide(el, pnl, (v) => signed(v, 0));
  tone(el, pnl);
  $("sim-pct").textContent = `${pct(100 * (val - c.entry) / c.entry, 0)} on ${money(c.entry * 100 * c.qty, 0)}`;
  glide($("sim-val"), val * 100 * c.qty, (v) => money(v, 0));
  $("sim-px").textContent = money(L.price);
  $("sim-left").textContent = Math.max(c.dte - L.day, 0).toFixed(L.day % 1 ? 1 : 0);
  $("lever-hint").textContent = st.scenario ? "Volume changes how far the scenario runs. Watch the band widen."
    : "Drag a lever and watch the P&L and the signals. Volume needs a scenario below: it sets how far a move goes, not which way.";
  for (const b of $("scenarios").children) b.setAttribute("aria-pressed", b.dataset.id === st.scenario);
  $("play").disabled = $("reroll").disabled = !st.scenario;
  const sc = S.SCENARIOS.find((s) => s.id === st.scenario);
  $("story").textContent = sc ? `${sc.blurb} Bright line: the story. Band: 8 of 10 rolls land inside.` : "Pick a scenario to project a path, then press PLAY to watch one roll of it.";
  const o = st.mc;
  $("odds").hidden = $("odds-title").hidden = !o;
  if (o) {
    $("o-chance").textContent = `${Math.round(o.chance * 100)}%`;
    $("o-meter").style.setProperty("--w", `${Math.round(o.chance * 100)}%`);
    for (const [id, v] of [["o-p10", o.p10], ["o-p50", o.p50], ["o-p90", o.p90]]) { $(id).textContent = signed(v, 0); tone($(id), v); }
  }
  const at = closesAtLever();
  const simVol = st.scenario ? S.volumeAt(L.volume, L.day) : null;
  renderSignals("sig-sim", at.closes, `SIGNALS · ${LV.TF_LABEL[st.tf] || "DAILY"} · ${at.simulated ? "SIMULATED " + ahead(L.day) : "NOW"}`,
                simVol ?? st.levels.rel_volume);
  const view = chartSeries();
  renderEvents(view);
  renderLevelChips();
  drawChart(view);
  drawPayoff();
}

// What the charts show: the last 30 real days and the simulated days, with indicators over all of it.
function chartSeries() {
  const hist = histCloses(st), path = st.roll || st.story;
  const fut = path ? barCloses(path, st.roll ? st.roll.upto : path.price.length - 1) : [];
  const all = hist.concat(fut);
  const ev = path && hist.length > 30 ? I.events(all, hist.length, ctx()) : [];
  return { hist, fut, all, ind: all.length >= 30 ? I.compute(all) : null, ev, from: Math.max(0, hist.length - 30) };
}

function renderEvents(view) {
  const box = $("events");
  if (!view.ev.length) {
    box.replaceChildren();
    box.hidden = !st.scenario;
    if (st.scenario && view.hist.length > 30) box.textContent = st.roll ? "No signals fired yet." : "No signals fire on this path.";
    return;
  }
  box.hidden = false;
  const MAX_SHOWN = 8, more = view.ev.length - MAX_SHOWN;
  box.replaceChildren(...view.ev.slice(0, MAX_SHOWN).map((e) => {
    const el = document.createElement("span");
    el.className = `ev ${e.dir > 0 ? "up" : e.dir < 0 ? "down" : "flat"}`;
    el.textContent = `${unit().word} ${e.i - (view.hist.length - 1)} ${e.dir > 0 ? "▲" : e.dir < 0 ? "▼" : "●"} ${e.label}`;
    return el;
  }));
  if (more > 0) {
    const el = document.createElement("span");
    el.textContent = `+${more} more: hover the chart markers`;
    box.append(el);
  }
}

// ---------------------------------------------------------------- play
function play() {
  if (!st.scenario || !st.contract) return;
  stopPlay();
  const t = st;
  const rng = S.mulberry32((t.seed * 7919 + Date.now()) >>> 0);
  const roll = S.simulatePath(t.scenario, ctx(), rng);
  const mine = { ...roll, upto: 0 };
  t.roll = mine;
  const n = roll.price.length - 1, dur = Math.min(4000 + (unit(t).intraday ? Math.min(t.hbars, 80) * 90 : t.horizon * 900), 14000), t0 = performance.now();
  $("play").textContent = "■ STOP";
  $("deck").classList.add("playing");
  const frame = (now) => {
    if (st !== t || t.roll !== mine) return;      // the tab changed or the roll was replaced: this play is over
    const k = Math.min((now - t0) / dur, 1), i = Math.round(k * n);
    t.roll.upto = i;
    t.lever.touched = true;
    t.lever.price = roll.price[i];
    t.lever.day = Math.min(roll.day[i], t.contract.dte);
    t.lever.iv = Math.round((roll.ivMult[i] - 1) * 100);
    $("l-price").value = t.lever.price;
    $("l-day").value = t.lever.day;
    $("l-iv").value = t.lever.iv;
    paint();
    if (k < 1) playing = requestAnimationFrame(frame);
    else {
      stopPlay();
      saveTabs();
      const p = $("sim-pnl");
      p.classList.remove("pop");
      void p.offsetWidth;
      p.classList.add("pop");
    }
  };
  playing = requestAnimationFrame(frame);
}

function stopPlay() {
  if (playing) cancelAnimationFrame(playing);
  playing = null;
  $("play").innerHTML = "&#9654; PLAY";
  $("deck").classList.remove("playing");
}

// ---------------------------------------------------------------- charts
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function canvas(id) {
  const cv = $(id), dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  g.font = "11px ui-monospace, Menlo, Consolas, monospace";
  return { g, w, h };
}

function hline(g, y, x0, x1, color, label, dash = [], at = 0) {
  g.save();
  g.strokeStyle = color; g.fillStyle = color; g.lineWidth = 1; g.setLineDash(dash);
  g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
  g.setLineDash([]);
  if (label) g.fillText(label, x0 + 4 + at, y - 4);
  g.restore();
}

// The interactive chart's model: real candles, the simulated days as hollow candles, indicators over both.
function simCandles(path, upto) {
  const out = [], per = unit().spb;
  for (let d = 1; (d - 1) * per < upto; d++) {
    const a = (d - 1) * per, b = Math.min(d * per, upto);
    let h = -Infinity, l = Infinity;
    for (let i = a; i <= b; i++) { h = Math.max(h, path.price[i]); l = Math.min(l, path.price[i]); }
    out.push({ d, o: path.price[a], h, l, c: path.price[b] });
  }
  return out;
}

function drawChart(view) {
  if (!chart) chart = new PriceChart($("future"), { onViewChange: (v) => { st.view = { ...v }; } });
  const bars = st.bars.map((b) => ({ ...b }));
  if (!bars.length) return;
  const u = unit(), hiIdx = bars.length - 1, last = bars[hiIdx];
  last.c = st.spot; last.h = Math.max(last.h, st.spot); last.l = Math.min(last.l, st.spot);
  const story = st.story, roll = st.roll, L = st.lever;
  const toPts = (p, upto) => p.price.slice(0, upto + 1).map((v, i) => ({ s: hiIdx + i / u.spb, p: v }));
  const futureBars = st.scenario && story ? Math.ceil((story.price.length - 1) / u.spb) : Math.ceil(L.day * u.spd / u.spb);
  const done = bars.slice(-21, -1).map((b) => b.v).filter((v) => v > 0);
  const avg = done.length ? done.reduce((a, b) => a + b, 0) / done.length : null;      // 20-bar average volume
  const simVol = [];
  if (st.scenario && avg) for (let k = 1; k <= futureBars; k++) simVol.push({ s: hiIdx + k, v: avg * S.volumeAt(L.volume, (k - 1) * u.spb / u.spd) });
  const r = parseFloat(st.levelR), sp = parseFloat(st.levelS);
  const levels = [];
  for (const [list, kind, active] of [[st.lv?.resistance || [], "R", r], [st.lv?.support || [], "S", sp]]) {
    let found = false;
    for (const x of list) {
      const on = Math.abs(x.price - active) < 1e-6;
      found ||= on;
      levels.push({ price: x.price, kind, active: on, touches: x.touches });
    }
    if (!found && isFinite(active)) levels.push({ price: active, kind, active: true, touches: 0 });
  }
  chart.setModel({
    bars, histLen: bars.length, futureBars, closes: view.all, ind: view.ind, overlays: st.overlays, osc: st.osc,
    showVolume: st.overlays.vol !== false, avgVolume: avg, levels, intraday: !!u.intraday, barWord: u.word,
    futureLabel: (k) => (u.intraday ? `+${hours(k, u)}` : u.word === "WEEK" ? `W+${k}` : `D+${k}`),
    strike: st.contract?.strike ?? null, breakeven: st.contract ? S.breakeven(st.contract) : null,
    band: story && st.mc ? st.mc.lo.map((lo, i) => ({ s: hiIdx + i / u.spb, lo, hi: st.mc.hi[i] })) : null,
    story: story ? toPts(story, story.price.length - 1) : null,
    roll: roll ? toPts(roll, roll.upto) : null,
    simBars: roll ? simCandles(roll, roll.upto).map((c) => ({ ...c, s: hiIdx + c.d })) : [],
    simVol, lever: { s: hiIdx + L.day * u.spd / u.spb, p: L.price }, markers: view.ev,
    defaultBars: LV.rangeBars(st.tf, st.range),
  }, st.view);
  if (!st.view) st.view = { ...chart.view };
}

// The RANGE buttons belong to the timeframe: a 5-minute chart offers days, a weekly one offers years.
function renderRanges() {
  const box = $("ranges"), set = (LV.TF_RANGES[st.tf] || LV.TF_RANGES["1d"]).set;
  if (!(st.range in set)) st.range = (LV.TF_RANGES[st.tf] || LV.TF_RANGES["1d"]).start;
  const label = document.createElement("span");
  label.className = "k";
  label.textContent = "RANGE";
  box.replaceChildren(label, ...Object.keys(set).map((r) => {
    const b = document.createElement("button");
    b.className = "tg";
    b.textContent = r;
    b.setAttribute("aria-pressed", r === st.range);
    b.addEventListener("click", () => {
      stopPlay();
      st.range = r;
      computeLevels(st);
      st.view = null;
      if (chart) chart.view = null;
      st.roll = null; rebuildScenario(); saveTabs(); renderRanges(); paint();
    });
    return b;
  }));
  for (const b of $("tfs").children) if (b.dataset.tf) b.setAttribute("aria-pressed", b.dataset.tf === st.tf);
}

async function setTimeframe(tf) {
  if (tf === st.tf) return;
  stopPlay();
  const t = st;
  t.tf = tf;
  t.range = LV.TF_RANGES[tf].start;
  t.hbars = TF_UNIT[tf].hDef || t.hbars;
  t.view = null; t.roll = null; t.story = null; t.mc = null;
  t.lever = { price: t.spot, day: 0, iv: 0, volume: t.lever.volume, touched: false };
  if (chart) chart.view = null;
  saveTabs();
  renderRanges();
  await loadBars(t);
  if (t !== st) return;
  rebuildScenario();
  renderTab();
}

function renderLevelChips() {
  const box = (id, list, kind) => {
    const active = parseFloat(kind === "R" ? st.levelR : st.levelS), auto = kind === "R" ? st.autoR : st.autoS;
    const el = $(id);
    el.replaceChildren(...list.map((x, i) => {
      const b = document.createElement("button");
      b.className = `lvl ${kind === "R" ? "res" : "sup"}`;
      b.setAttribute("aria-pressed", Math.abs(x.price - active) < 1e-6);
      b.innerHTML = `<b></b><small></small>`;
      b.firstChild.textContent = `${kind}${i + 1} ${x.price.toFixed(2)}`;
      b.lastChild.textContent = `${x.touches} touch${x.touches === 1 ? "" : "es"} · ${pct(100 * (x.price / st.spot - 1), 1)}`;
      b.title = `Use ${x.price.toFixed(2)} as the ${kind === "R" ? "resistance" : "support"} the scenarios aim at`;
      b.addEventListener("click", () => {
        if (kind === "R") { st.levelR = String(x.price); st.autoR = i === 0; } else { st.levelS = String(x.price); st.autoS = i === 0; }
        stopPlay(); st.roll = null; rebuildScenario(); saveTabs(); renderTab();
      });
      return b;
    }));
    if (!list.length) el.textContent = kind === "R" ? "None above: at a new high for this range." : "None below: at a new low for this range.";
    $(kind === "R" ? "lv-r" : "lv-s").value = kind === "R" ? st.levelR : st.levelS;
    $(kind === "R" ? "lv-r-auto" : "lv-s-auto").hidden = auto;
  };
  box("lv-r-list", st.lv?.resistance || [], "R");
  box("lv-s-list", st.lv?.support || [], "S");
}

function drawPayoff() {
  const c = st.contract;
  if (!c) return;
  const { g, w, h } = canvas("payoff");
  const lp = $("l-price"), a = parseFloat(lp.min), b = parseFloat(lp.max), n = 80;
  // while a tab is still loading its price the slider has no range yet: nothing to draw
  if (!isFinite(a) || !isFinite(b) || b <= a || !isFinite(st.lever.price) || !w || !h) return;
  const ivMult = 1 + st.lever.iv / 100;
  const now = [], exp = [];
  for (let i = 0; i <= n; i++) {
    const s = a + (b - a) * i / n;
    now.push(S.pnlAt(c, s, st.lever.day, ivMult));
    exp.push(S.pnlAt(c, s, c.dte, 1));
  }
  const cost = c.entry * 100 * c.qty;
  const lo = -cost * 1.15, hi = Math.max(cost * 1.5, ...now, st.mc ? st.mc.p90 : 0) * 1.1;
  const pad = { l: 8, r: 58, t: 12, b: 18 };
  const X = (i) => pad.l + (w - pad.l - pad.r) * i / n;
  const Y = (v) => pad.t + (h - pad.t - pad.b) * (1 - (Math.min(Math.max(v, lo), hi) - lo) / (hi - lo));
  hline(g, Y(0), pad.l, w - pad.r, css("--grid"), "", []);
  g.fillStyle = css("--dim");
  g.fillText("$0", w - pad.r + 6, Y(0) + 4);
  if (Y(-cost) - Y(0) > 14) g.fillText(`−${money(cost, 0)}`, w - pad.r + 6, Y(-cost) + 4);
  g.fillText(money(a, 0), pad.l, h - 4);
  g.fillText(money(b, 0), w - pad.r - 30, h - 4);
  g.fillText(w > 520 ? "P&L AT EACH STOCK PRICE · dashed: at expiration" : "P&L BY PRICE", pad.l + 60, h - 4);
  g.strokeStyle = css("--dim"); g.setLineDash([4, 4]); g.lineWidth = 1.5;
  g.beginPath(); exp.forEach((v, i) => (i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)))); g.stroke();
  g.setLineDash([]);
  const grad = g.createLinearGradient(0, Y(hi), 0, Y(lo));
  grad.addColorStop(0, css("--lime")); grad.addColorStop((hi - 0) / (hi - lo), css("--lime"));
  grad.addColorStop(Math.min((hi - 0) / (hi - lo) + 0.001, 1), css("--red")); grad.addColorStop(1, css("--red"));
  g.strokeStyle = grad; g.lineWidth = 2.5;
  g.beginPath(); now.forEach((v, i) => (i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)))); g.stroke();
  const k = (st.lever.price - a) / (b - a) * n, v = S.pnlAt(c, st.lever.price, st.lever.day, ivMult);
  g.fillStyle = v >= 0 ? css("--lime") : css("--red"); g.shadowColor = g.fillStyle; g.shadowBlur = 12;
  g.beginPath(); g.arc(X(Math.min(Math.max(k, 0), n)), Y(v), 6, 0, Math.PI * 2); g.fill();
  g.shadowBlur = 0;
}

// ---------------------------------------------------------------- wiring
function wire() {
  $("load").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("sym").value.trim();
    if (!v) return;
    stopPlay();
    if (view === "home") {                       // from the home page a ticker opens in its own tab
      let j = tabs.findIndex((t) => !t.sym);
      if (j < 0 && tabs.length < MAX_TABS) { tabs.push(newTab()); j = tabs.length - 1; }
      if (j < 0) return say(`Up to ${MAX_TABS} tabs. Close one first.`, true);
      switchTo(j);
    }
    loadSymbol(st, v, false);
  });
  $("tab-add").addEventListener("click", () => addTab());
  // your default rules (for the next trade)
  const setRule = (k, v) => { rules = { ...rules, [k]: v }; writeJSON(RULES_KEY, rules); renderLadder(); };
  $("gr-target").addEventListener("change", (e) => setRule("target", Math.max(5, Math.round(+e.target.value || 50))));
  $("gr-stop").addEventListener("change", (e) => setRule("stop", Math.max(5, Math.round(+e.target.value || 40))));
  $("gr-fees").addEventListener("change", (e) => setRule("fees", Math.max(0, +e.target.value || 0)));
  $("gr-mode").addEventListener("click", () => setRule("mode", rules.mode === "swing" ? "eod" : "swing"));
  // this position's rules
  const setMine = (k, v) => {
    if (!st.pick) return;
    st.pick.rules = { ...rulesOf(st), [k]: v };
    buildContract(st); saveTabs(); renderTabs(); onQuote(); home.render();
  };
  $("dc-target").addEventListener("change", (e) => setMine("target", Math.max(5, Math.round(+e.target.value || 50))));
  $("dc-stop").addEventListener("change", (e) => setMine("stop", Math.max(5, Math.round(+e.target.value || 40))));
  $("dc-mode").addEventListener("click", () => setMine("mode", rulesOf(st).mode === "swing" ? "eod" : "swing"));
  $("dc-hold").addEventListener("change", (e) => setMine("holdEarnings", e.target.checked));
  $("dc-record").addEventListener("click", () => {
    const p = st.pick, R = rulesOf(st), exit = parseFloat($("dc-sold").value);
    if (!p || !(exit >= 0)) return say("Type the price you sold at (per share, like 2.35).", true);
    const net = DC.netNow({ entry: p.entry, bid: exit, qty: p.qty, fees: R.fees });
    record = [{ at: Date.now(), sym: p.symbol, name: `${p.symbol} ${p.strike}${p.isCall ? "C" : "P"} ${dateLabel(p.expiration)}`,
                qty: p.qty, entry: p.entry, exit, net, word: st.decision?.word ?? "", mode: R.mode,
                followed: !!st.decision && st.decision.code !== "HOLD" }, ...record].slice(0, 500);
    writeJSON(RECORD_KEY, record);
    say(`Recorded: ${signed(net, 0)} net.`);
    stopPlay(); clearPick(st); saveTabs(); renderTabs(); renderTab(); home.render();
  });
  $("tab-home").addEventListener("click", showHome);
  $("tab-signals").addEventListener("click", showSignals);
  $("copy").addEventListener("click", () => addTab(st));
  for (const [id, side] of [["side-call", "calls"], ["side-put", "puts"]]) {
    $(id).addEventListener("click", () => {
      st.side = side;
      $("side-call").setAttribute("aria-pressed", side === "calls");
      $("side-put").setAttribute("aria-pressed", side === "puts");
      saveTabs();
      renderLadder();
    });
  }
  $("budget").addEventListener("input", (e) => { st.budget = +e.target.value; renderLadder(); });
  $("budget").addEventListener("change", saveTabs);
  $("change").addEventListener("click", () => {
    stopPlay();
    clearPick(st);
    saveTabs();
    renderTabs();
    renderTab();
  });
  const qty = (d) => {
    if (!st.pick) return;
    st.pick.qty = Math.max(1, Math.min(50, st.pick.qty + d));
    buildContract(st);
    saveTabs();
    renderTabs();
    onQuote();
    refreshSim(true);
  };
  $("qty-up").addEventListener("click", () => qty(1));
  $("qty-dn").addEventListener("click", () => qty(-1));

  const lever = (id, key, after) => {
    $(id).addEventListener("input", (e) => {
      stopPlay();
      st.lever[key] = +e.target.value;
      st.lever.touched = true;
      if (after) after();
      paint();
    });
    $(id).addEventListener("change", saveTabs);
  };
  lever("l-price", "price");
  lever("l-day", "day");
  lever("l-iv", "iv");
  lever("l-vol", "volume", rebuildScenario);
  for (const [id, key] of [["lv-r", "levelR"], ["lv-s", "levelS"]]) {
    $(id).addEventListener("change", (e) => {
      st[key] = e.target.value;
      st[key === "levelR" ? "autoR" : "autoS"] = false;
      stopPlay(); st.roll = null; rebuildScenario(); saveTabs(); paint();
    });
  }

  $("scenarios").replaceChildren(...S.SCENARIOS.map((s) => {
    const b = document.createElement("button");
    b.className = `btn scen ${s.dir > 0 ? "bull" : s.dir < 0 ? "bear" : "flat"}`;
    b.dataset.id = s.id;
    b.textContent = s.name;
    b.addEventListener("click", () => {
      stopPlay();
      st.scenario = st.scenario === s.id ? null : s.id;
      st.roll = null;
      if (st.scenario && st.lever.volume === 1 && s.id.startsWith("surge")) { st.lever.volume = 3; $("l-vol").value = 3; }
      rebuildScenario();
      saveTabs();
      renderTabs();
      paint();
    });
    return b;
  }));
  const horizon = (d) => {
    stopPlay();
    const u = unit();
    if (u.intraday) st.hbars = Math.max(u.hStep, Math.min(st.hbars + d * u.hStep, u.hMax)); else st.horizon += d;
    st.roll = null; refreshSim(true); saveTabs();
  };
  $("h-up").addEventListener("click", () => horizon(1));
  $("h-dn").addEventListener("click", () => horizon(-1));
  $("play").addEventListener("click", () => (playing ? stopPlay() : play()));
  $("reroll").addEventListener("click", () => { st.seed++; rebuildScenario(); play(); });
  $("reset").addEventListener("click", () => resetLevers());
  for (const b of $("overlays").children) {
    b.addEventListener("click", () => {
      st.overlays[b.dataset.o] = !st.overlays[b.dataset.o];
      b.setAttribute("aria-pressed", st.overlays[b.dataset.o]);
      saveTabs();
      paint();
    });
  }
  for (const [id, key, flag] of [["lv-r-auto", "levelR", "autoR"], ["lv-s-auto", "levelS", "autoS"]]) {
    $(id).addEventListener("click", () => { stopPlay(); st[flag] = true; computeLevels(st); st.roll = null; rebuildScenario(); saveTabs(); paint(); });
  }
  for (const b of $("tfs").children) if (b.dataset.tf) b.addEventListener("click", () => setTimeframe(b.dataset.tf));
  $("zoom-in").addEventListener("click", () => chart?.zoom(1 / 1.4));
  $("zoom-out").addEventListener("click", () => chart?.zoom(1.4));
  $("zoom-reset").addEventListener("click", () => { if (chart) { chart.resetView(); chart.draw(); } });
  for (const b of $("osc-pick").children) {
    b.addEventListener("click", () => {
      st.osc = b.dataset.o;
      for (const x of $("osc-pick").children) x.setAttribute("aria-pressed", x === b);
      saveTabs();
      paint();
    });
  }
  addEventListener("resize", () => { if (st.contract) paint(); });
  document.addEventListener("visibilitychange", refreshNow);
  addEventListener("focus", refreshNow);
  addEventListener("online", refreshNow);
  addEventListener("offline", renderFresh);
}

// ---------------------------------------------------------------- start
function restore() {
  const today = new Date().toISOString().slice(0, 10);
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(SAVE_KEY) || "null"); } catch { /* ignore */ }
  if (!saved) {                                      // the single pick an earlier version remembered
    try {
      const old = JSON.parse(localStorage.getItem(OLD_KEY) || "null");
      if (old && old.symbol) saved = { active: 0, tabs: [{ sym: old.symbol, pick: old, side: old.isCall ? "calls" : "puts" }] };
    } catch { /* ignore */ }
  }
  tabs = (saved?.tabs || []).slice(0, MAX_TABS).map((t) => {
    const n = newTab(t);
    if (n.pick && n.pick.expiration < today) { n.pick = null; n.scenario = null; }
    if (!TF_UNIT[n.tf]) n.tf = "1d";
    return n;
  });
  if (!tabs.length) tabs.push(newTab());
  st = tabs[Math.min(Math.max(saved?.active ?? 0, 0), tabs.length - 1)];
  view = saved ? ({ "-1": "home", "-2": "signals", "-3": "home" }[saved.active] || "tab") : "home";      // a first visit starts on the home page
  for (const t of tabs) if (t.sym) t.loading = true;
  renderTabs();
  renderTab();
  for (const t of tabs) if (t.sym) loadSymbol(t, t.sym, true);
  if (view === "tab" && !st.sym) $("sym").focus();
}

wire();
home = createHome({
  $, api, money, signed, pct, tone, positions, openTab: switchTo, openSymbol, record: () => record, volChip, volRead,
  clearRecord: () => { record = []; writeJSON(RECORD_KEY, record); },
  currentSymbol: () => (view === "tab" ? st.sym : null),
  currentSide: () => ({ bars: st.bars, changePct: st.summary?.change_pct ?? null,
                        verdict: st.bars.length >= 30 ? I.readout(histCloses(st)).verdict.value : undefined }),
});
signals = createSignals({
  $, api, tone, openSymbol, showSignals, onSignals: () => view === "signals", volRead,
  volProfile: (s) => volume.profile(s, DC.marketClock()), clock: () => DC.marketClock(),
  tabSymbols: () => tabs.filter((t) => t.sym).map((t) => ({ sym: t.sym, position: !!t.pick })),
  monitorSymbols: () => home.symbols(),
});
try { localStorage.removeItem("optlab-sim-cycles-v1"); } catch { /* the scenario tree's saved state, no longer used */ }
loadSource();
restore();
signals.refresh();
home.refresh().then(() => { lastPoll = Date.now(); renderFresh(); });
schedulePoll();
checkVersion();
setInterval(renderFresh, 1000);
setInterval(livePoll, LIVE_MS);
livePoll();
setInterval(() => { if (!document.hidden) checkVersion(); }, 60000);
