import * as S from "./sim.js";
import * as I from "./ind.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 15000;
const SAVE_KEY = "optlab-sim-tabs-v1";
const OLD_KEY = "optlab-sim-pick";
const MAX_TABS = 8;
const KEEP = ["id", "sym", "side", "budget", "pick", "exp", "horizon", "seed", "scenario", "lever", "levelR", "levelS", "overlays", "osc"];

// ---------------------------------------------------------------- tabs: each one is a whole simulation
let tabs = [];
let st = null;                 // the tab on screen
let playing = null;            // the animation frame of PLAY (one at a time)
let pollTimer = null;

function newTab(from = {}) {
  return {
    id: Math.random().toString(36).slice(2, 9), sym: null, side: "calls", budget: 500, pick: null, exp: null,
    horizon: 5, seed: 1, scenario: null, lever: { price: null, day: 0, iv: 0, volume: 1, touched: false },
    levelR: "", levelS: "", overlays: { ema: true, sma50: true, sma200: false, bb: false }, osc: "rsi",
    ...structuredClone(Object.fromEntries(KEEP.filter((k) => k in from).map((k) => [k, from[k]]))),
    // live data, never saved
    summary: null, expData: null, pickData: null, contract: null, row: null, spot: null, levels: {}, bars: [],
    story: null, mc: null, roll: null, livePnl: null, loading: false,
  };
}

function saveTabs() {
  try {
    localStorage.setItem(SAVE_KEY, JSON.stringify({
      active: tabs.indexOf(st), tabs: tabs.map((t) => Object.fromEntries(KEEP.map((k) => [k, t[k]]))),
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
  st = tabs[i];
  saveTabs();
  renderTabs();
  renderTab();
}

const shortName = (t) => {
  if (!t.sym) return "NEW";
  if (!t.pick) return t.sym;
  const k = t.pick.strike % 1 ? t.pick.strike.toFixed(1) : t.pick.strike;
  return `${t.sym} ${k}${t.pick.isCall ? "C" : "P"}`;
};

function renderTabs() {
  const box = $("tabs");
  box.replaceChildren(...tabs.map((t, i) => {
    const el = document.createElement("div");
    el.className = `tab${t === st ? " on" : ""}${t.pick ? (t.pick.isCall ? " call" : " put") : ""}`;
    el.innerHTML = `<button class="tab-main" role="tab"><b></b><small></small></button><button class="tab-x" aria-label="Close tab">×</button>`;
    const [main, x] = el.children;
    main.setAttribute("aria-selected", t === st);
    main.firstChild.textContent = shortName(t);
    const sub = main.lastChild;
    if (t.livePnl != null) { sub.textContent = signed(t.livePnl, 0); tone(sub, t.livePnl); }
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
  if (t === st) say(`Loading ${sym.toUpperCase()}…`);
  t.loading = true;
  try {
    const summary = await api(`/api/chain/${encodeURIComponent(sym)}`);
    const changed = t.sym !== summary.symbol;
    t.sym = summary.symbol;
    t.summary = summary;
    t.spot = summary.price;
    if (!keepPick || changed && t.pick && t.pick.symbol !== t.sym) clearPick(t);
    if (changed) { t.levelR = ""; t.levelS = ""; t.bars = []; t.levels = {}; }
    const bars = api(`/api/bars/${encodeURIComponent(t.sym)}`).then((b) => {
      t.bars = b.bars;
      t.levels = b.levels;
      if (t.levelR === "" && b.levels.resistance != null) t.levelR = String(b.levels.resistance);
      if (t.levelS === "" && b.levels.support != null) t.levelS = String(b.levels.support);
    }).catch(() => {});
    const want = t.pick ? t.pick.expiration : t.exp;
    const first = summary.expirations.find((e) => e.date === want) || summary.expirations.find((e) => e.dte >= 7) || summary.expirations[0];
    await loadExp(t, first.date, false);
    await bars;
    if (t === st) say("");
  } catch (e) {
    if (t === st) say(e.message, true);
  }
  t.loading = false;
  saveTabs();
  renderTabs();
  if (t === st) renderTab();
}

async function loadExp(t, date, render = true) {
  try {
    const d = await api(`/api/chain/${encodeURIComponent(t.sym)}/${date}`);
    t.exp = date;
    t.expData = d;
    t.spot = d.price;
    if (t.pick && t.pick.expiration === date) t.pickData = d;
    buildContract(t);
    if (render && t === st) { renderExps(); renderLadder(); onQuote(); }
  } catch (e) {
    if (t === st) say(e.message, true);
  }
}

// Every 15 s: refresh each tab's quote (the server shares one fetch per ticker), so every tab's live P&L moves.
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!document.hidden) {
      await Promise.all(tabs.filter((t) => t.sym && !t.loading).map(async (t) => {
        const date = t.pick ? t.pick.expiration : t.exp;
        if (!date) return;
        try {
          const d = await api(`/api/chain/${encodeURIComponent(t.sym)}/${date}`);
          t.spot = d.price;
          t.summary = { ...t.summary, price: d.price, change_pct: d.change_pct, stale: d.stale };
          if (date === t.exp) t.expData = d;
          if (t.pick) t.pickData = d;
          buildContract(t);
        } catch { if (t.summary) t.summary = { ...t.summary, stale: true }; }
      }));
      renderTabs();
      if (st.sym) {
        showLive(st.summary);
        if (!$("pick").hidden) renderLadder();
        onQuote();
      }
    }
    schedulePoll();
  }, POLL_MS);
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
  t.livePnl = ((row.mid ?? row.ask) - p.entry) * 100 * p.qty;
}

function clearPick(t) {
  Object.assign(t, { pick: null, contract: null, row: null, pickData: null, scenario: null, mc: null, story: null, roll: null, livePnl: null });
}

// ---------------------------------------------------------------- rendering the tab on screen
function renderTab() {
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
}

function renderExps() {
  if (!st.summary) return;
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
  const atm = rows.reduce((best, r, i) => (Math.abs(r.strike - d.price) < Math.abs(rows[best].strike - d.price) ? i : best), 0);
  const view = rows.slice(Math.max(0, atm - 12), atm + 13);
  const maxVol = Math.max(1, ...view.map((r) => r.volume));
  const isCall = st.side === "calls";
  let affordable = 0;
  const els = view.map((r) => {
    const cost = r.ask * 100, itm = isCall ? r.strike < d.price : r.strike > d.price, over = cost > st.budget;
    if (!over) affordable++;
    const b = document.createElement("button");
    b.className = `row ${isCall ? "call" : "put"}${over ? " over" : ""}`;
    b.innerHTML = `<span class="cells"><span class="strike"></span><span class="cost"></span><span class="delta"></span><span class="bar"><i></i><em></em></span></span>`;
    const [strike, costEl, delta, bar] = b.firstChild.children;
    strike.textContent = r.strike % 1 ? r.strike.toFixed(2) : r.strike;
    if (itm) strike.insertAdjacentHTML("beforeend", "<small>IN THE MONEY</small>");
    costEl.textContent = money(cost, 0);
    delta.textContent = r.delta == null ? "—" : `$${Math.abs(r.delta * 100).toFixed(0)}`;
    bar.firstChild.style.setProperty("--w", `${Math.round(100 * r.volume / maxVol)}%`);
    bar.lastChild.textContent = r.volume.toLocaleString("en-US");
    b.addEventListener("click", () => choose(r, isCall));
    return b;
  });
  const mark = document.createElement("div");
  mark.className = "spot-line";
  mark.textContent = `${st.sym} is at ${money(d.price)}`;
  const at = view.findIndex((r) => r.strike > d.price);
  els.splice(at < 0 ? els.length : at, 0, mark);
  const box = $("ladder"), key = `${st.id}|${st.sym}|${st.exp}|${st.side}`, keep = box.dataset.for === key ? box.scrollTop : null;
  box.replaceChildren(...els);
  box.dataset.for = key;
  box.scrollTop = keep ?? Math.max(0, mark.offsetTop - box.clientHeight / 2 + 20);   // open at the stock's price
  $("ladder-hint").textContent = affordable ? `${affordable} of these fit your budget. Dimmed ones cost more. "Moves per $1" is how much one contract gains or loses when the stock moves $1.`
    : "Nothing here fits that budget. Raise it, or pick a nearer expiration.";
}

function choose(r, isCall) {
  st.pick = { symbol: st.sym, expiration: st.exp, isCall, occ: r.occ, strike: r.strike, entry: r.ask, qty: 1 };
  st.pickData = st.expData;
  buildContract(st);
  resetLevers(false);
  saveTabs();
  renderTabs();
  renderTab();
  $("hud").scrollIntoView({ behavior: "smooth", block: "start" });
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
  const mark = row.mid ?? row.ask;
  glide($("hud-val"), mark * 100 * p.qty, (v) => money(v, 0));
  glide($("hud-pnl"), st.livePnl, (v) => signed(v, 0));
  tone($("hud-pnl"), st.livePnl);
  $("hud-pnl-pct").textContent = pct(100 * (mark - p.entry) / p.entry, 1);
  refreshSim(false);
}

// ---------------------------------------------------------------- levers and scenarios
function sigmaDay() {
  const yearly = st.levels.hv || st.summary?.iv30 || st.contract?.iv || 0.3;
  return yearly / Math.sqrt(365);
}

function ctx() {
  const r = parseFloat(st.levelR), s = parseFloat(st.levelS);
  return { price: st.spot, resistance: isFinite(r) ? r : null, support: isFinite(s) ? s : null,
           sigmaDay: sigmaDay(), volume: st.lever.volume, days: st.horizon };
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
  const span = Math.max(0.12, 5 * sigmaDay() * Math.sqrt(Math.max(st.horizon, 5)));
  const lp = $("l-price");
  lp.min = (st.spot * (1 - span)).toFixed(2);
  lp.max = (st.spot * (1 + span)).toFixed(2);
  lp.value = L.price;
  $("l-day").max = Math.max(c.dte, 1);
  $("l-day").value = L.day;
  $("l-iv").value = L.iv;
  st.horizon = Math.max(1, Math.min(st.horizon, Math.max(c.dte, 1), 45));
  $("horizon").textContent = st.horizon;
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

// The simulated closes, one per day, up to `step` (STEPS_PER_DAY steps a day).
function dailyCloses(path, step) {
  const out = [];
  for (let s = S.STEPS_PER_DAY; s <= step && s < path.price.length; s += S.STEPS_PER_DAY) out.push(path.price[s]);
  return out;
}

// History plus the simulated days up to where the levers are: what the indicators would read then.
function closesAtLever() {
  const hist = histCloses(st), path = st.roll || st.story, L = st.lever;
  const step = st.roll ? st.roll.upto : Math.round(L.day * S.STEPS_PER_DAY);
  const fut = path ? dailyCloses(path, step) : [];
  const moved = L.touched && (Math.abs(L.price - st.spot) > 1e-9 || L.day > 0);
  if (moved && !(path && step % S.STEPS_PER_DAY === 0 && fut.length && fut.at(-1) === L.price)) fut.push(L.price);
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
  $("l-day-out").textContent = L.day === 0 ? "now" : `+${L.day.toFixed(L.day % 1 ? 1 : 0)}d`;
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
  renderSignals("sig-sim", at.closes, at.simulated ? `SIGNALS · SIMULATED +${L.day.toFixed(L.day % 1 ? 1 : 0)}d` : "SIGNALS · NOW",
                simVol ?? st.levels.rel_volume);
  const view = chartSeries();
  renderEvents(view);
  drawFuture(view);
  drawOsc(view);
  drawPayoff();
}

// What the charts show: the last 30 real days and the simulated days, with indicators over all of it.
function chartSeries() {
  const hist = histCloses(st), path = st.roll || st.story;
  const fut = path ? dailyCloses(path, st.roll ? st.roll.upto : path.price.length - 1) : [];
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
  box.replaceChildren(...view.ev.map((e) => {
    const el = document.createElement("span");
    el.className = `ev ${e.dir > 0 ? "up" : e.dir < 0 ? "down" : "flat"}`;
    el.textContent = `DAY ${e.i - (view.hist.length - 1)} ${e.dir > 0 ? "▲" : e.dir < 0 ? "▼" : "●"} ${e.label}`;
    return el;
  }));
}

// ---------------------------------------------------------------- play
function play() {
  if (!st.scenario || !st.contract) return;
  stopPlay();
  const t = st;
  const rng = S.mulberry32((t.seed * 7919 + Date.now()) >>> 0);
  const roll = S.simulatePath(t.scenario, ctx(), rng);
  t.roll = { ...roll, upto: 0 };
  const n = roll.price.length - 1, dur = Math.min(4000 + t.horizon * 900, 14000), t0 = performance.now();
  $("play").textContent = "■ STOP";
  $("deck").classList.add("playing");
  const frame = (now) => {
    if (st !== t) return;
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

// x positions shared by the price chart and the oscillator: 30 real days on the left, the simulation on the right.
function layout(view, w, pad) {
  const story = st.story, fut = story ? story.price.length : 0;
  const split = story ? pad.l + (w - pad.l - pad.r) * 0.36 : w - pad.r;
  const nh = view.hist.length - view.from;
  const XH = (i) => pad.l + (split - pad.l) * (nh > 1 ? (i - view.from) / (nh - 1) : 1);
  const XF = (step) => split + (w - pad.r - split) * (fut > 1 ? step / (fut - 1) : 0);
  const X = (i) => (i < view.hist.length ? XH(i) : XF((i - (view.hist.length - 1)) * S.STEPS_PER_DAY));
  return { split, XH, XF, X, fut };
}

function polyline(g, X, Y, series, from, to, color, width = 1.2, dash = []) {
  g.save();
  g.strokeStyle = color; g.lineWidth = width; g.setLineDash(dash);
  g.beginPath();
  let started = false;
  for (let i = from; i <= to; i++) {
    const v = series[i];
    if (v == null) { started = false; continue; }
    if (!started) { g.moveTo(X(i), Y(v)); started = true; } else g.lineTo(X(i), Y(v));
  }
  g.stroke();
  g.restore();
}

function drawFuture(view) {
  const c = st.contract;
  if (!c) return;
  const { g, w, h } = canvas("future");
  const story = st.story, mc = st.mc, roll = st.roll, x = ctx(), ind = view.ind, O = st.overlays;
  const pad = { l: 8, r: 58, t: 14, b: 18 };
  const { split, XF, X, fut } = layout(view, w, pad);
  const last = view.all.length - 1, hi0 = view.hist.length - 1;
  const vals = [...view.hist.slice(view.from), st.spot, st.lever.price];
  if (story) vals.push(...story.price, ...mc.lo, ...mc.hi);
  if (roll) vals.push(...roll.price.slice(0, roll.upto + 1));
  const near = (v) => v != null && Math.abs(v / st.spot - 1) < 0.15;
  const ovl = [];
  if (ind) {
    if (O.ema) ovl.push([ind.ema9, css("--cyan"), "EMA 9"], [ind.ema21, css("--mag"), "EMA 21"]);
    if (O.sma50) ovl.push([ind.sma50, css("--amber"), "SMA 50"]);
    if (O.sma200) ovl.push([ind.sma200, "#b9c6ff", "SMA 200"]);
    if (O.bb) ovl.push([ind.bb.up, css("--dim"), "BB"], [ind.bb.lo, css("--dim"), ""]);
    for (const [s] of ovl) for (let i = view.from; i <= last; i++) if (near(s[i])) vals.push(s[i]);
  }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const lines = [[x.resistance, css("--mag"), "RESISTANCE"], [x.support, css("--cyan"), "SUPPORT"],
                 [c.strike, css("--amber"), "STRIKE"], [S.breakeven(c), css("--ink"), "BREAKEVEN"]];
  for (const [v] of lines) if (near(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  const m = (hi - lo) * 0.08 || 1;
  lo -= m; hi += m;
  const Y = (v) => pad.t + (h - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));

  g.strokeStyle = css("--grid"); g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, y = Y(v);
    g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
    g.fillStyle = css("--dim"); g.fillText(v.toFixed(v > 100 ? 0 : 2), w - pad.r + 6, y + 4);
  }
  lines.forEach(([v, color, label], i) => {        // labels step sideways so close levels stay readable
    if (v && v > lo && v < hi) hline(g, Y(v), pad.l, w - pad.r, color, label, label === "BREAKEVEN" ? [4, 4] : [], i * 84);
  });

  if (story) {                                       // the band of 8 in 10 rolls, behind everything
    g.fillStyle = css("--band");
    g.beginPath();
    mc.hi.forEach((v, i) => (i ? g.lineTo(XF(i), Y(v)) : g.moveTo(XF(i), Y(v))));
    for (let i = mc.lo.length - 1; i >= 0; i--) g.lineTo(XF(i), Y(mc.lo[i]));
    g.closePath(); g.fill();
  }
  if (ind && O.bb) {                                 // Bollinger band fill
    g.fillStyle = "rgba(127, 143, 184, 0.08)";
    g.beginPath();
    let first = true;
    for (let i = view.from; i <= last; i++) if (ind.bb.up[i] != null) { first ? g.moveTo(X(i), Y(ind.bb.up[i])) : g.lineTo(X(i), Y(ind.bb.up[i])); first = false; }
    for (let i = last; i >= view.from; i--) if (ind.bb.lo[i] != null) g.lineTo(X(i), Y(ind.bb.lo[i]));
    g.closePath(); g.fill();
  }
  for (const [s, color] of ovl) polyline(g, X, Y, s, view.from, last, color, 1.2);

  polyline(g, X, Y, view.hist, view.from, hi0, css("--dim"), 1.5);
  if (story) {
    g.strokeStyle = css("--lime"); g.lineWidth = 2; g.shadowColor = css("--lime"); g.shadowBlur = 8;
    g.beginPath();
    story.price.forEach((v, i) => (i ? g.lineTo(XF(i), Y(v)) : g.moveTo(XF(i), Y(v))));
    g.stroke();
    g.shadowBlur = 0;
    g.fillStyle = css("--dim");
    g.fillText("NOW", split - 12, h - 4);
    g.fillText(`+${st.horizon}d`, w - pad.r - 24, h - 4);
    g.strokeStyle = css("--grid"); g.setLineDash([2, 4]);
    g.beginPath(); g.moveTo(split, pad.t); g.lineTo(split, h - pad.b); g.stroke(); g.setLineDash([]);
  }
  if (roll && story) {
    g.strokeStyle = css("--ink"); g.lineWidth = 2; g.shadowColor = css("--ink"); g.shadowBlur = 10;
    g.beginPath();
    for (let i = 0; i <= roll.upto; i++) (i ? g.lineTo(XF(i), Y(roll.price[i])) : g.moveTo(XF(i), Y(roll.price[i])));
    g.stroke();
    g.shadowBlur = 0;
  }
  for (const e of view.ev) {                         // where each signal fires on the simulated path
    const px = X(e.i), py = Y(view.all[e.i]);
    g.fillStyle = e.dir > 0 ? css("--lime") : e.dir < 0 ? css("--red") : css("--amber");
    g.beginPath();
    if (e.dir >= 0) { g.moveTo(px, py - 14); g.lineTo(px - 5, py - 6); g.lineTo(px + 5, py - 6); }
    if (e.dir < 0) { g.moveTo(px, py + 14); g.lineTo(px - 5, py + 6); g.lineTo(px + 5, py + 6); }
    g.closePath(); g.fill();
  }
  let lx = pad.l + 4;                                 // a small key for the overlays
  for (const [, color, label] of ovl) {
    if (!label) continue;
    g.fillStyle = color; g.fillText(label, lx, pad.t + 2);
    lx += g.measureText(label).width + 12;
  }
  const mx = story ? XF(Math.min(Math.round(st.lever.day * S.STEPS_PER_DAY), fut - 1)) : w - pad.r;
  const my = Y(Math.min(Math.max(st.lever.price, lo), hi));
  g.fillStyle = css("--ink"); g.shadowColor = css("--ink"); g.shadowBlur = 12;
  g.beginPath(); g.arc(mx, my, 5, 0, Math.PI * 2); g.fill();
  g.shadowBlur = 0;
}

function drawOsc(view) {
  const { g, w, h } = canvas("osc");
  const ind = view.ind;
  if (!ind) return;
  const pad = { l: 8, r: 58, t: 10, b: 14 };
  const { X } = layout(view, w, pad), last = view.all.length - 1;
  if (st.osc === "rsi") {
    const Y = (v) => pad.t + (h - pad.t - pad.b) * (1 - v / 100);
    g.fillStyle = "rgba(255, 77, 109, 0.08)"; g.fillRect(pad.l, Y(100), w - pad.l - pad.r, Y(70) - Y(100));
    g.fillStyle = "rgba(125, 255, 90, 0.08)"; g.fillRect(pad.l, Y(30), w - pad.l - pad.r, Y(0) - Y(30));
    for (const [v, label] of [[70, "70 OVERBOUGHT"], [50, ""], [30, "30 OVERSOLD"]]) hline(g, Y(v), pad.l, w - pad.r, css("--grid"), "", [3, 4]);
    g.fillStyle = css("--dim");
    g.fillText("70", w - pad.r + 6, Y(70) + 4); g.fillText("30", w - pad.r + 6, Y(30) + 4);
    g.fillText("RSI 14", pad.l + 4, pad.t + 2);
    polyline(g, X, Y, ind.rsi, view.from, view.hist.length - 1, css("--ink"), 1.5);
    if (last >= view.hist.length) polyline(g, X, Y, ind.rsi, view.hist.length - 1, last, css("--lime"), 2);
    const r = ind.rsi[last];
    if (r != null) { g.fillStyle = r >= 70 ? css("--red") : r <= 30 ? css("--lime") : css("--ink"); g.fillText(r.toFixed(0), w - pad.r + 30, Y(r) + 4); }
  } else {
    const { line, signal, hist } = ind.macd;
    const vals = [];
    for (let i = view.from; i <= last; i++) for (const s of [line, signal, hist]) if (s[i] != null) vals.push(s[i]);
    if (!vals.length) return;
    const a = Math.max(...vals.map(Math.abs)) * 1.15 || 1;
    const Y = (v) => pad.t + (h - pad.t - pad.b) * (1 - (v + a) / (2 * a));
    hline(g, Y(0), pad.l, w - pad.r, css("--grid"), "");
    const bw = Math.max(2, (X(view.from + 1) - X(view.from)) * 0.6);
    for (let i = view.from; i <= last; i++) {
      if (hist[i] == null) continue;
      g.fillStyle = hist[i] >= 0 ? (i >= view.hist.length ? css("--lime") : "rgba(125, 255, 90, 0.5)") : (i >= view.hist.length ? css("--red") : "rgba(255, 77, 109, 0.5)");
      g.fillRect(X(i) - bw / 2, Math.min(Y(0), Y(hist[i])), bw, Math.abs(Y(hist[i]) - Y(0)));
    }
    polyline(g, X, Y, line, view.from, last, css("--cyan"), 1.4);
    polyline(g, X, Y, signal, view.from, last, css("--mag"), 1.2);
    g.fillStyle = css("--dim");
    g.fillText("MACD 12/26/9 · bars: histogram", pad.l + 4, pad.t + 2);
  }
}

function drawPayoff() {
  const c = st.contract;
  if (!c) return;
  const { g, w, h } = canvas("payoff");
  const lp = $("l-price"), a = parseFloat(lp.min), b = parseFloat(lp.max), n = 80;
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
    if (v) { stopPlay(); loadSymbol(st, v, false); }
  });
  $("tab-add").addEventListener("click", () => addTab());
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
    $(id).addEventListener("change", (e) => { st[key] = e.target.value; st.roll = null; rebuildScenario(); saveTabs(); paint(); });
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
  const horizon = (d) => { st.horizon += d; st.roll = null; refreshSim(true); saveTabs(); };
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
  for (const b of $("osc-pick").children) {
    b.addEventListener("click", () => {
      st.osc = b.dataset.o;
      for (const x of $("osc-pick").children) x.setAttribute("aria-pressed", x === b);
      saveTabs();
      paint();
    });
  }
  addEventListener("resize", () => { if (st.contract) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) schedulePoll(); });
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
    return n;
  });
  if (!tabs.length) tabs.push(newTab());
  st = tabs[Math.min(Math.max(saved?.active ?? 0, 0), tabs.length - 1)];
  for (const t of tabs) if (t.sym) t.loading = true;
  renderTabs();
  renderTab();
  for (const t of tabs) if (t.sym) loadSymbol(t, t.sym, true);
  if (!st.sym) $("sym").focus();
}

wire();
restore();
schedulePoll();
