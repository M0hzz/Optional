import * as S from "./sim.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 15000;
const KEY = "optlab-sim-pick";

const st = {
  sym: null, summary: null, exp: null, expData: null, side: "calls", budget: 500,
  pick: null,            // {symbol, expiration, isCall, occ, strike, entry, qty}
  contract: null,        // the simulator's view of the pick, rebuilt from each live quote
  row: null, spot: null, levels: {}, bars: [],
  lever: { price: null, day: 0, iv: 0, volume: 1, touched: false },
  scenario: null, horizon: 5, seed: 1, mc: null, story: null, roll: null, playing: null, pollTimer: null,
};

// ---------------------------------------------------------------- formatting
const money = (v, dp = 2) => (v == null || !isFinite(v) ? "—" : `$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp })}`);
const signed = (v, dp = 2) => (v == null || !isFinite(v) ? "—" : `${v < 0 ? "−" : "+"}${money(v, dp)}`);
const pct = (v, dp = 0) => (v == null || !isFinite(v) ? "" : `${v < 0 ? "−" : "+"}${Math.abs(v).toFixed(dp)}%`);
const dateLabel = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
const tone = (el, v) => { el.classList.toggle("up", v > 0.005); el.classList.toggle("down", v < -0.005); };

// Numbers glide to their new value instead of jumping.
const tweens = new Map();
function glide(el, to, fmt) {
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

// ---------------------------------------------------------------- loading a ticker
async function loadSymbol(sym, keepPick = false) {
  say(`Loading ${sym.toUpperCase()}…`);
  clearTimeout(st.pollTimer);
  try {
    const summary = await api(`/api/chain/${encodeURIComponent(sym)}`);
    st.sym = summary.symbol;
    st.summary = summary;
    st.spot = summary.price;
    $("sym").value = st.sym;
    if (!keepPick) clearPick();
    showLive(summary);
    api(`/api/bars/${encodeURIComponent(st.sym)}`).then((b) => {
      st.bars = b.bars;
      st.levels = b.levels;
      $("lv-r").value = b.levels.resistance ?? "";
      $("lv-s").value = b.levels.support ?? "";
      refreshSim(true);
    }).catch(() => { st.bars = []; st.levels = {}; refreshSim(true); });
    const want = keepPick && st.pick ? st.pick.expiration : null;
    const first = summary.expirations.find((e) => e.date === want) || summary.expirations.find((e) => e.dte >= 7) || summary.expirations[0];
    renderExps();
    $("pick").hidden = !!st.pick;              // visible before the ladder is drawn, so it can open at the stock's price
    await loadExp(first.date);
    say("");
  } catch (e) {
    say(e.message, true);
  }
}

function showLive(d) {
  $("live").hidden = false;
  $("live-sym").textContent = d.symbol;
  glide($("live-px"), d.price, (v) => money(v));
  const chg = $("live-chg");
  chg.textContent = pct(d.change_pct, 2);
  tone(chg, d.change_pct || 0);
  $("dot").classList.toggle("stale", !!d.stale);
}

function renderExps() {
  const box = $("exps");
  box.replaceChildren(...st.summary.expirations.slice(0, 24).map((e) => {
    const b = document.createElement("button");
    b.className = "chip";
    b.setAttribute("role", "tab");
    b.dataset.date = e.date;
    b.innerHTML = `<b></b><small></small>`;
    b.firstChild.textContent = dateLabel(e.date);
    b.lastChild.textContent = e.dte === 0 ? "today" : `${e.dte}d`;
    b.addEventListener("click", () => loadExp(e.date));
    return b;
  }));
}

async function loadExp(date) {
  try {
    const d = await api(`/api/chain/${encodeURIComponent(st.sym)}/${date}`);
    st.exp = date;
    st.expData = d;
    st.spot = d.price;
    for (const c of $("exps").children) c.setAttribute("aria-selected", c.dataset.date === date);
    renderLadder();
    onQuote();
  } catch (e) {
    say(e.message, true);
  }
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(st.pollTimer);
  st.pollTimer = setTimeout(async () => {
    if (document.hidden || !st.sym) return schedulePoll();
    try {
      const date = st.pick ? st.pick.expiration : st.exp;
      const d = await api(`/api/chain/${encodeURIComponent(st.sym)}/${date}`);
      st.spot = d.price;
      showLive({ symbol: st.sym, price: d.price, change_pct: d.change_pct, stale: d.stale });
      if (date === st.exp) { st.expData = d; if (!$("pick").hidden) renderLadder(); }
      if (st.pick) st.pickData = d;
      onQuote();
    } catch { $("dot").classList.add("stale"); }
    schedulePoll();
  }, POLL_MS);
}

// ---------------------------------------------------------------- the ladder
function renderLadder() {
  const d = st.expData;
  if (!d) return;
  $("budget-out").textContent = money(st.budget, 0);
  const rows = d[st.side].filter((r) => r.ask > 0);
  const atm = rows.reduce((best, r, i) => (Math.abs(r.strike - d.price) < Math.abs(rows[best].strike - d.price) ? i : best), 0);
  const view = rows.slice(Math.max(0, atm - 12), atm + 13);
  const maxVol = Math.max(1, ...view.map((r) => r.volume));
  const isCall = st.side === "calls";
  let affordable = 0;
  const els = view.map((r) => {
    const cost = r.ask * 100, itm = isCall ? r.strike < d.price : r.strike > d.price, over = cost > st.budget;
    if (!over) affordable++;
    const b = document.createElement("button");
    b.className = `row ${isCall ? "call" : "put"}${itm ? " itm" : ""}${over ? " over" : ""}`;
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
  const box = $("ladder"), keep = box.dataset.for === `${st.sym}|${st.exp}|${st.side}` ? box.scrollTop : null;
  box.replaceChildren(...els);
  box.dataset.for = `${st.sym}|${st.exp}|${st.side}`;
  box.scrollTop = keep ?? Math.max(0, mark.offsetTop - box.clientHeight / 2 + 20);   // open at the stock's price
  $("ladder-hint").textContent = affordable ? `${affordable} of these fit your budget. Dimmed ones cost more. "Moves per $1" is how much one contract gains or loses when the stock moves $1.`
    : "Nothing here fits that budget. Raise it, or pick a nearer expiration.";
}

function choose(r, isCall) {
  st.pick = { symbol: st.sym, expiration: st.exp, isCall, occ: r.occ, strike: r.strike, entry: r.ask, qty: 1 };
  st.pickData = st.expData;
  save();
  $("pick").hidden = true;
  resetLevers();
  onQuote();
  $("hud").scrollIntoView({ behavior: "smooth", block: "start" });
}

function clearPick() {
  st.pick = null; st.contract = null; st.row = null; st.scenario = null; st.mc = null; st.story = null; st.roll = null;
  stopPlay();
  save();
  $("hud").hidden = true;
  $("deck").hidden = true;
}

const save = () => { try { st.pick ? localStorage.setItem(KEY, JSON.stringify(st.pick)) : localStorage.removeItem(KEY); } catch { /* private window */ } };

// ---------------------------------------------------------------- live quote -> HUD and simulator baseline
function onQuote() {
  const p = st.pick, d = st.pickData;
  if (!p || !d || d.date !== p.expiration) return;
  const row = d[p.isCall ? "calls" : "puts"].find((r) => r.occ === p.occ);
  if (!row) return;
  st.row = row;
  const c = S.makeContract(row, p.isCall, d.dte, d.price, p.qty);
  c.entry = p.entry;
  st.contract = c;
  $("hud").hidden = false;
  $("deck").hidden = false;
  $("hud-name").textContent = `${p.symbol} ${p.strike % 1 ? p.strike.toFixed(2) : p.strike} ${p.isCall ? "CALL" : "PUT"}`;
  $("hud-name").className = `hud-name ${p.isCall ? "call" : "put"}`;
  $("hud-sub").textContent = `expires ${dateLabel(p.expiration)} · ${d.dte === 0 ? "today" : d.dte + " days left"} · bought at ${money(p.entry)}`;
  $("qty").textContent = p.qty;
  $("hud-cost").textContent = money(p.entry * 100 * p.qty, 0);
  $("hud-be").textContent = money(S.breakeven(c));
  const mark = row.mid ?? row.ask, pnl = (mark - p.entry) * 100 * p.qty;
  glide($("hud-val"), mark * 100 * p.qty, (v) => money(v, 0));
  glide($("hud-pnl"), pnl, (v) => signed(v, 0));
  tone($("hud-pnl"), pnl);
  $("hud-pnl-pct").textContent = pct(100 * (mark - p.entry) / p.entry, 1);
  refreshSim(false);
}

// ---------------------------------------------------------------- levers
function sigmaDay() {
  const yearly = st.levels.hv || st.summary?.iv30 || st.contract?.iv || 0.3;
  return yearly / Math.sqrt(365);
}

function ctx() {
  const r = parseFloat($("lv-r").value), s = parseFloat($("lv-s").value);
  return { price: st.spot, resistance: isFinite(r) ? r : null, support: isFinite(s) ? s : null,
           sigmaDay: sigmaDay(), volume: st.lever.volume, days: st.horizon };
}

function resetLevers() {
  stopPlay();
  st.lever = { price: st.spot, day: 0, iv: 0, volume: 1, touched: false };
  st.scenario = null; st.mc = null; st.story = null; st.roll = null;
  st.horizon = Math.max(1, Math.min(st.pickData?.dte || 5, 5));
  $("l-vol").value = 1;
  refreshSim(true);
}

function refreshSim(rebuild) {
  const c = st.contract;
  if (!c) return;
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
  if (rebuild || !st.story) rebuildScenario();
  paint();
}

function rebuildScenario() {
  if (!st.scenario || !st.contract) { st.story = null; st.mc = null; return; }
  const x = ctx();
  st.story = S.simulatePath(st.scenario, x);
  st.mc = S.monteCarlo(st.scenario, x, st.contract, 300, st.seed);
}

function paint() {
  const c = st.contract, L = st.lever;
  if (!c) return;
  const ivMult = 1 + L.iv / 100;
  const val = S.valueAt(c, L.price, L.day, ivMult), pnl = S.pnlAt(c, L.price, L.day, ivMult);
  const move = 100 * (L.price / st.spot - 1);
  $("l-price-out").textContent = `${money(L.price)} (${pct(move, 1)})`;
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
    : "Drag a lever and watch the P&L. Volume needs a scenario below: it sets how far a move goes, not which way.";
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
  drawFuture();
  drawPayoff();
}

// ---------------------------------------------------------------- play
function play() {
  if (!st.scenario || !st.contract) return;
  stopPlay();
  const rng = S.mulberry32((st.seed * 7919 + Date.now()) >>> 0);
  const roll = S.simulatePath(st.scenario, ctx(), rng);
  st.roll = { ...roll, upto: 0 };
  const n = roll.price.length - 1, dur = Math.min(4000 + st.horizon * 900, 14000), t0 = performance.now();
  $("play").textContent = "■ STOP";
  $("deck").classList.add("playing");
  const frame = (now) => {
    const k = Math.min((now - t0) / dur, 1), i = Math.round(k * n);
    st.roll.upto = i;
    st.lever.touched = true;
    st.lever.price = roll.price[i];
    st.lever.day = Math.min(roll.day[i], st.contract.dte);
    st.lever.iv = Math.round((roll.ivMult[i] - 1) * 100);
    $("l-price").value = st.lever.price;
    $("l-day").value = st.lever.day;
    $("l-iv").value = st.lever.iv;
    paint();
    if (k < 1) st.playing = requestAnimationFrame(frame);
    else {
      stopPlay();
      const p = $("sim-pnl");
      p.classList.remove("pop");
      void p.offsetWidth;
      p.classList.add("pop");
    }
  };
  st.playing = requestAnimationFrame(frame);
}

function stopPlay() {
  if (st.playing) cancelAnimationFrame(st.playing);
  st.playing = null;
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
  g.fillText(label, x0 + 4 + at, y - 4);
  g.restore();
}

function drawFuture() {
  const c = st.contract;
  if (!c) return;
  const { g, w, h } = canvas("future");
  const hist = st.bars.slice(-30).map((b) => b.c);
  if (hist.length) hist[hist.length - 1] = st.spot;
  const story = st.story, mc = st.mc, roll = st.roll, x = ctx();
  const fut = story ? story.price.length : 0;
  const pad = { l: 8, r: 58, t: 14, b: 18 };
  const split = story ? pad.l + (w - pad.l - pad.r) * 0.36 : w - pad.r;
  const vals = [...hist, st.spot, st.lever.price];
  if (story) vals.push(...story.price, ...mc.lo, ...mc.hi);
  if (roll) vals.push(...roll.price.slice(0, roll.upto + 1));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const lines = [[x.resistance, css("--mag"), "RESISTANCE"], [x.support, css("--cyan"), "SUPPORT"],
                 [c.strike, css("--amber"), "STRIKE"], [S.breakeven(c), css("--ink"), "BREAKEVEN"]];
  for (const [v] of lines) if (v && Math.abs(v / st.spot - 1) < 0.2) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  const m = (hi - lo) * 0.08 || 1;
  lo -= m; hi += m;
  const Y = (v) => pad.t + (h - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  const XH = (i) => pad.l + (split - pad.l) * (hist.length > 1 ? i / (hist.length - 1) : 1);
  const XF = (i) => split + (w - pad.r - split) * (fut > 1 ? i / (fut - 1) : 0);

  g.strokeStyle = css("--grid"); g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, y = Y(v);
    g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
    g.fillStyle = css("--dim"); g.fillText(v.toFixed(v > 100 ? 0 : 2), w - pad.r + 6, y + 4);
  }
  lines.forEach(([v, color, label], i) => {        // labels step sideways so close levels stay readable
    if (v && v > lo && v < hi) hline(g, Y(v), pad.l, w - pad.r, color, label, label === "BREAKEVEN" ? [4, 4] : [], i * 84);
  });

  if (hist.length > 1) {
    g.strokeStyle = css("--dim"); g.lineWidth = 1.5;
    g.beginPath();
    hist.forEach((v, i) => (i ? g.lineTo(XH(i), Y(v)) : g.moveTo(XH(i), Y(v))));
    g.stroke();
  }
  if (story) {
    g.fillStyle = css("--band");
    g.beginPath();
    mc.hi.forEach((v, i) => (i ? g.lineTo(XF(i), Y(v)) : g.moveTo(XF(i), Y(v))));
    for (let i = mc.lo.length - 1; i >= 0; i--) g.lineTo(XF(i), Y(mc.lo[i]));
    g.closePath(); g.fill();
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
  // the lever's position: a marker at (day, price)
  const mx = story ? XF(Math.min(Math.round(st.lever.day * S.STEPS_PER_DAY), fut - 1)) : w - pad.r;
  const my = Y(Math.min(Math.max(st.lever.price, lo), hi));
  g.fillStyle = css("--ink"); g.shadowColor = css("--ink"); g.shadowBlur = 12;
  g.beginPath(); g.arc(mx, my, 5, 0, Math.PI * 2); g.fill();
  g.shadowBlur = 0;
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
  $("load").addEventListener("submit", (e) => { e.preventDefault(); const v = $("sym").value.trim(); if (v) loadSymbol(v); });
  for (const [id, side] of [["side-call", "calls"], ["side-put", "puts"]]) {
    $(id).addEventListener("click", () => {
      st.side = side;
      $("side-call").setAttribute("aria-pressed", side === "calls");
      $("side-put").setAttribute("aria-pressed", side === "puts");
      renderLadder();
    });
  }
  $("budget").addEventListener("input", (e) => { st.budget = +e.target.value; renderLadder(); });
  $("change").addEventListener("click", () => { clearPick(); $("pick").hidden = false; renderLadder(); });
  const qty = (d) => { if (!st.pick) return; st.pick.qty = Math.max(1, Math.min(50, st.pick.qty + d)); save(); onQuote(); refreshSim(true); };
  $("qty-up").addEventListener("click", () => qty(1));
  $("qty-dn").addEventListener("click", () => qty(-1));

  const lever = (id, key, after) => $(id).addEventListener("input", (e) => {
    stopPlay();
    st.lever[key] = +e.target.value;
    st.lever.touched = true;
    if (after) after();
    paint();
  });
  lever("l-price", "price");
  lever("l-day", "day");
  lever("l-iv", "iv");
  lever("l-vol", "volume", rebuildScenario);
  for (const id of ["lv-r", "lv-s"]) $(id).addEventListener("change", () => { rebuildScenario(); paint(); });

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
      paint();
    });
    return b;
  }));
  const horizon = (d) => { st.horizon += d; st.roll = null; refreshSim(true); };
  $("h-up").addEventListener("click", () => horizon(1));
  $("h-dn").addEventListener("click", () => horizon(-1));
  $("play").addEventListener("click", () => (st.playing ? stopPlay() : play()));
  $("reroll").addEventListener("click", () => { st.seed++; rebuildScenario(); play(); });
  $("reset").addEventListener("click", resetLevers);
  addEventListener("resize", () => paint());
  document.addEventListener("visibilitychange", () => { if (!document.hidden && st.sym) schedulePoll(); });
}

wire();
try {
  const saved = JSON.parse(localStorage.getItem(KEY) || "null");
  if (saved && saved.symbol && saved.occ && new Date(saved.expiration + "T23:59:59") >= new Date()) {
    st.pick = saved;
    st.side = saved.isCall ? "calls" : "puts";
    loadSymbol(saved.symbol, true).then(() => { st.pickData = st.expData; resetLevers(); onQuote(); });
  } else {
    $("sym").focus();
  }
} catch { $("sym").focus(); }
