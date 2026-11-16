// The CYCLES tab: a live tree of 15-minute scenarios, up to three cycles deep (tracker.js does the maths).
//
//   NOW      the cycle under way. Its five scenarios update with every live quote (3 s); as the price commits to a
//            direction the others fade (under 10%) and die (under 2%, once a fifth of the cycle has gone). A dead
//            scenario and everything under it leaves the tree and goes into the stored stack at the top.
//   NEXT     the cycle after, assuming the scenario picked in NOW happened (its odds add up to 100% on their own).
//   THEN     the one after that, assuming the scenario picked in NEXT happened.
// When a cycle ends, what happened is recorded, the scenarios it didn't take are stored, and the tree moves on.
// Outside regular hours the tree is for the next session's first cycles, built from the last close.
import * as T from "./tracker.js";
import * as LV from "./levels.js";

const KEY = "optlab-sim-cycles-v1";
const BARS_EVERY = 120000;                       // the 60-day 5-minute history is refetched every 2 minutes
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const load = (d) => { try { return JSON.parse(localStorage.getItem(KEY) || "null") ?? d; } catch { return d; } };
const pct = (v) => `${Math.round(100 * v)}%`;
const hm = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

// New York time now: {day: "YYYY-MM-DD", minute (of the day, with seconds as a fraction), open (regular hours)}.
export function marketClock(now = new Date()) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
                                                hour: "2-digit", minute: "2-digit", second: "2-digit", weekday: "short", hour12: false });
  const p = Object.fromEntries(f.formatToParts(now).map((x) => [x.type, x.value]));
  const minute = (+p.hour % 24) * 60 + +p.minute + +p.second / 60;
  const weekday = !["Sat", "Sun"].includes(p.weekday);
  return { day: `${p.year}-${p.month}-${p.day}`, minute, open: weekday && minute >= T.OPEN_MIN && minute < T.CLOSE_MIN };
}

export function createCycles(app) {
  const { $, api } = app;
  const saved = load({});
  let cfg = { sym: saved.sym || null, depth: saved.depth || 3, archiveOpen: false };
  const store = saved.tickers || {};             // SYM -> {cycle, archive, record, nextPred}
  const H = new Map();                           // SYM -> {H, bars, lv, at, score, cal, err, loading}
  let live = new Map(), sel = [null, null], dying = new Set(), lastTick = 0;

  const save = () => {
    try {
      for (const t of Object.values(store)) { t.archive = (t.archive || []).slice(0, 120); t.record = (t.record || []).slice(0, 300); }
      localStorage.setItem(KEY, JSON.stringify({ sym: cfg.sym, depth: cfg.depth, tickers: store }));
    } catch { /* private window */ }
  };
  const tick = (sym) => (store[sym] ||= { cycle: null, archive: [], record: [], nextPred: null });

  // the tickers on offer: open tabs first, then the home page's watched and chained symbols
  function symbols() {
    const list = [...new Set([...app.tabSymbols(), ...app.monitorSymbols()])];
    if (!cfg.sym || !list.includes(cfg.sym)) cfg.sym = list[0] || cfg.sym || "SPY";
    return list.length ? list : [cfg.sym];
  }

  async function ensure(sym) {
    const h = H.get(sym) || {};
    H.set(sym, h);
    if (h.loading || (h.H && Date.now() - h.at < BARS_EVERY)) return h;
    h.loading = true;
    try {
      const b = await api(`/api/bars/${encodeURIComponent(sym)}?tf=5mx`);
      h.bars = b.bars;
      h.H = T.prepare(b.bars);
      h.at = Date.now();
      h.err = null;
      h.lv = LV.findLevels(b.bars, 156, b.bars.at(-1).c, 4, LV.TF_TOL["5m"]);   // the last two sessions' levels
      const day = b.bars.at(-1).d.slice(0, 10);
      if (h.scoreDay !== day) {                  // the replay takes about a second: once a day, after the page draws
        h.scoreDay = day;
        setTimeout(() => {
          const cache = new Map(), bars = h.bars;
          const levelsAt = (i) => { const d = bars[i].d.slice(0, 10); if (!cache.has(d)) cache.set(d, LV.findLevels(bars.slice(0, i), 156, bars[i - 1].c, 4, LV.TF_TOL["5m"])); return cache.get(d); };
          h.score = T.replay(h.H, levelsAt);
          h.cal = T.calibrator(h.score.calibration);
          if (sym === cfg.sym) render();
        }, 50);
      }
    } catch (e) { h.err = e.message; h.at = Date.now(); }
    h.loading = false;
    return h;
  }

  // ------------------------------------------------------------ the live cycle
  // Where the cycle under way started: the close of the 5-minute bar just before it, or the first live price seen.
  function startOf(h, clock, idx) {
    const startMin = T.OPEN_MIN + 15 * idx, want = `${clock.day}T${hm(startMin - 5)}`;
    let i = -1;
    for (let k = h.H.n - 1; k >= 0 && k >= h.H.n - 100; k--) if (h.bars[k].d === want) { i = k; break; }
    return { startMin, i };
  }

  function step(sym) {
    const h = H.get(sym), st = tick(sym), q = live.get(sym);
    if (!h?.H || h.H.n < 700) return null;
    const clock = marketClock(), cal = h.cal || ((p) => p);
    const price = q?.price ?? h.H.c[h.H.n - 1];
    // a cycle whose 15 minutes are over (including the last one before the close) is scored before anything else
    if (st.cycle && !st.cycle.resolved && st.cycle.lastX != null
        && (!clock.open || st.cycle.id !== `${clock.day}#${Math.floor((clock.minute - T.OPEN_MIN) / 15)}`)) resolve(sym, st.cycle);
    if (!clock.open) {                           // closed: the next session's first cycle, from the last close
      const s = { ...T.liveState(h.H, h.H.n - 1, price), minute: T.OPEN_MIN + 5 };
      s.hi = s.lo = price;
      const node = T.cycle(h.H, s, h.lv);
      return { closed: true, node: { ...node, p: cal(node.p) }, raw: node, e: 0, x: 0, label: "NEXT SESSION · 09:30–09:45", startMin: T.OPEN_MIN };
    }
    const idx = Math.floor((clock.minute - T.OPEN_MIN) / 15), id = `${clock.day}#${idx}`;
    const { startMin, i } = startOf(h, clock, idx);
    let cyc = st.cycle;
    if (!cyc || cyc.id !== id) {
      const s = i >= 0 ? T.liveState(h.H, i) : { ...T.liveState(h.H, h.H.n - 1, price), minute: startMin };
      cyc = st.cycle = { id, startMin, price: s.price, state: s, t: T.thresholds(s.price, s.sig, h.lv), dead: {}, hist: [],
                         predicted: st.nextPred && st.nextPred.forId === id ? st.nextPred.p : null };
      st.nextPred = null;
      sel = [null, null];
      save();
    }
    if (cyc.state.price == null || !(cyc.state.sig > 0)) return null;
    const e = Math.min(1, (clock.minute - startMin) / 15), x = Math.log(price / cyc.price) / cyc.state.sig;
    const f = T.stateFeature(cyc.state);
    if (!cyc.samples) cyc.samples = f ? T.lookalikes(h.H, f) : [];
    const o = T.odds(cyc.samples, x, e, cyc.t);
    const node = { state: cyc.state, t: cyc.t, samples: cyc.samples, p: cal(o.p), end: o.end };
    cyc.lastX = x;
    cyc.lastP = node.p;
    if (!cyc.hist.length || Date.now() - cyc.hist.at(-1).at > 10000) { cyc.hist.push({ at: Date.now(), e, p: node.p.map((v) => +v.toFixed(3)) }); if (cyc.hist.length > 120) cyc.hist.shift(); }
    // pruning: under 10% fades, under 2% (after the first fifth of the cycle) dies and is stored; it comes back
    // only if it climbs over 10% again
    T.SCEN.forEach((sc, k) => {
      const v = node.p[k], d = cyc.dead[k];
      if (!d && e >= 0.2 && v < 0.02) {
        cyc.dead[k] = { at: Date.now(), p: v };
        dying.add(`${sym}|${k}`);
        setTimeout(() => { dying.delete(`${sym}|${k}`); if (sym === cfg.sym) render(); }, 900);
        st.archive.unshift({ at: Date.now(), cycle: `${hm(startMin)}–${hm(startMin + 15)}`, path: [sc.name], kind: "pruned", p: v,
                             note: `died ${Math.round(e * 15)} min in, at ${(100 * v).toFixed(1)}%`,
                             hist: cyc.hist.map((h2) => h2.p[k]) });
        save();
      } else if (d && v > 0.1) {
        delete cyc.dead[k];
        st.archive.unshift({ at: Date.now(), cycle: `${hm(startMin)}–${hm(startMin + 15)}`, path: [sc.name], kind: "revived", p: v,
                             note: `came back to ${pct(v)}`, hist: [] });
      }
    });
    return { node, raw: { ...node, p: o.p }, e, x, cyc, label: `${hm(startMin)}–${hm(startMin + 15)} ET`, startMin, price };
  }

  // A cycle has ended: record what happened, store the scenarios it didn't take.
  function resolve(sym, cyc) {
    if (cyc.resolved) return;
    cyc.resolved = true;
    const st = tick(sym), won = T.bucketOf(cyc.lastX, cyc.t);
    const first = cyc.hist[0]?.p, mid = cyc.hist.find((p) => p.e >= 0.5)?.p;
    st.record.unshift({ cycle: cyc.id, label: `${hm(cyc.startMin)}–${hm(cyc.startMin + 15)}`, won, start: first, mid,
                        predicted: cyc.predicted });
    T.SCEN.forEach((sc, k) => {
      if (k === won || cyc.dead[k]) return;
      st.archive.unshift({ at: Date.now(), cycle: `${hm(cyc.startMin)}–${hm(cyc.startMin + 15)}`, path: [sc.name], kind: "ended",
                           p: cyc.lastP?.[k] ?? null, note: `didn't happen: ${T.SCEN[won].name} did`, hist: cyc.hist.map((h2) => h2.p[k]) });
    });
    save();
  }

  // ------------------------------------------------------------ drawing
  function spark(values, cls) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 60 16");
    svg.setAttribute("class", `spark ${cls}`);
    if (values.length > 1) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
      path.setAttribute("points", values.map((v, i) => `${(60 * i / (values.length - 1)).toFixed(1)},${(15 - 14 * v).toFixed(1)}`).join(" "));
      svg.append(path);
    }
    return svg;
  }

  function card(sc, k, p, { layer, selected, faded, dyingNow, path, predicted, hist, onPick }) {
    const c = el("button", `cnode d${sc.dir < 0 ? "n" : sc.dir > 0 ? "p" : "0"}${selected ? " sel" : ""}${faded ? " fade" : ""}${dyingNow ? " dying" : ""}`);
    c.style.setProperty("--p", p.toFixed(3));
    const top = el("span", "cn-top");
    top.append(el("b", "cn-name", sc.name), el("span", "cn-p", pct(p)));
    c.append(top, el("i", "cn-bar"));
    const sub = el("span", "cn-sub");
    if (layer > 0) sub.append(el("span", "", `whole path ${(100 * path).toFixed(path < 0.1 ? 1 : 0)}%`));
    if (predicted != null) sub.append(el("span", "", `predicted ${pct(predicted)} last cycle`));
    if (hist && hist.length > 1) sub.append(spark(hist, sc.dir > 0 ? "up" : sc.dir < 0 ? "down" : "flat"));
    if (sub.childNodes.length) c.append(sub);
    c.title = layer === 0 ? `${sc.name}: ${pct(p)} chance this cycle ends here` : `${sc.name}: ${pct(p)} if the cycle before ends as picked; ${(100 * path).toFixed(1)}% for the whole path from now`;
    c.addEventListener("click", onPick);
    return c;
  }

  function render() {
    const syms = symbols();
    $("cy-syms").replaceChildren(...syms.map((s) => {
      const b = el("button", "tg", s);
      b.setAttribute("aria-pressed", s === cfg.sym);
      b.addEventListener("click", () => { cfg.sym = s; sel = [null, null]; save(); refresh(); });
      return b;
    }));
    for (const b of $("cy-depth").children) if (b.dataset.d) b.setAttribute("aria-pressed", +b.dataset.d === cfg.depth);
    if (!app.onCycles()) return;
    const sym = cfg.sym, h = H.get(sym), st = tick(sym);
    const r = h?.H ? step(sym) : null;
    $("cy-status").replaceChildren();
    if (!r) {
      $("cy-status").textContent = h?.err ? h.err : h?.H && h.H.n < 700 ? `Not enough 5-minute history for ${sym} yet.` : `Loading ${sym}'s 60-day 5-minute history…`;
      $("cy-tree").replaceChildren();
      renderArchive(st); renderScore(h, st);
      return;
    }
    // status line
    const status = $("cy-status");
    const left = r.closed ? null : Math.max(0, 15 - (marketClock().minute - r.startMin));
    status.append(el("b", "", `${sym} · ${r.closed ? "MARKET CLOSED" : "CYCLE " + r.label}`));
    if (!r.closed) {
      status.append(el("span", "cy-clock", `${Math.floor(left)}:${String(Math.floor((left % 1) * 60)).padStart(2, "0")} LEFT`));
      const prog = el("span", "cy-prog"); prog.style.setProperty("--e", r.e.toFixed(3)); status.append(prog);
      status.append(el("span", "", `${app.money(r.price)} · ${r.x >= 0 ? "+" : "−"}${Math.abs(r.x).toFixed(2)}σ from the start (1σ = ${app.money(r.cyc.price * r.cyc.state.sig)})`));
    } else status.append(el("span", "", `the tree below is for ${r.label.split(" · ")[1]} of the next session, from the last close`));
    const t = r.node.t;  // the cycle's levels
    status.append(el("span", "cy-lv", `R ${t.res ? t.res.toFixed(2) : "—"} · S ${t.sup ? t.sup.toFixed(2) : "—"}`));

    // the tree
    const cols = [];
    const startMin = r.startMin;
    const layer0 = r.node, dead = r.closed ? {} : r.cyc.dead;
    const alive = T.SCEN.map((_, k) => k).filter((k) => !dead[k]);
    if (sel[0] == null || dead[sel[0]]) sel[0] = alive.reduce((a, k) => (layer0.p[k] > layer0.p[a] ? k : a), alive[0]);
    const hist = r.closed ? [] : r.cyc.hist;
    const col0 = el("div", "ccol");
    col0.append(el("div", "ccol-head", r.closed ? `FIRST CYCLE · 09:30–09:45` : `NOW · ${hm(startMin)}–${hm(startMin + 15)}`));
    for (const k of T.SCEN.map((_, i) => i)) {
      if (dead[k] && !dying.has(`${sym}|${k}`)) continue;
      col0.append(card(T.SCEN[k], k, layer0.p[k], { layer: 0, selected: sel[0] === k, faded: layer0.p[k] < 0.1, dyingNow: dying.has(`${sym}|${k}`),
        path: layer0.p[k], predicted: r.cyc?.predicted?.[k], hist: hist.map((x) => x.p[k]), onPick: () => { sel = [k, null]; render(); } }));
    }
    cols.push(col0);
    let parentPath = layer0.p[sel[0]], parentK = sel[0], lastNode = null;
    const cal = h.cal || ((p) => p);
    for (let layer = 1; layer < cfg.depth; layer++) {
      const raw = T.children(h.H, layer === 1 ? r.raw : lastNode, parentK, h.lv);
      const node = { ...raw, p: cal(raw.p) };
      if (layer === 1 && !r.closed) st.nextPred = { forId: `${r.cyc.id.split("#")[0]}#${+r.cyc.id.split("#")[1] + 1}`, p: node.p };
      const s = layer;
      if (sel[s] == null) sel[s] = node.p.indexOf(Math.max(...node.p));
      const m = startMin + 15 * layer;
      const col = el("div", "ccol");
      col.append(el("div", "ccol-head", `${layer === 1 ? "NEXT" : "THEN"} · ${hm(m)}–${hm(m + 15)}`),
                 el("div", "ccol-if", `if ${T.SCEN[parentK].name} ${layer === 1 ? "happens now" : "happens next"}`));
      T.SCEN.forEach((sc, k) => {
        col.append(card(sc, k, node.p[k], { layer, selected: sel[s] === k, faded: node.p[k] < 0.05, path: parentPath * node.p[k],
          onPick: () => { sel[s] = k; if (s === 1) sel[2] = null; render(); } }));
      });
      cols.push(col);
      lastNode = raw;
      parentPath *= node.p[sel[s]];
      parentK = sel[s];
    }
    $("cy-tree").replaceChildren(...cols);
    $("cy-tree").style.setProperty("--cols", cfg.depth);
    renderArchive(st);
    renderScore(h, st);
    lastTick = Date.now();
  }

  function renderArchive(st) {
    const list = st.archive || [];
    $("cy-arch-count").textContent = String(list.length);
    $("cy-arch").hidden = !cfg.archiveOpen;
    $("cy-arch-toggle").setAttribute("aria-expanded", cfg.archiveOpen);
    $("cy-arch").replaceChildren(...list.slice(0, 60).map((a) => {
      const row = el("div", `arch ${a.kind}`);
      row.append(el("span", "arch-t", new Date(a.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
                 el("span", "arch-c", a.cycle), el("b", "", a.path.join(" → ")), el("span", "arch-n", a.note));
      if (a.hist?.length > 1) row.append(spark(a.hist, "flat"));
      return row;
    }));
    if (!list.length) $("cy-arch").append(el("p", "hint", "Nothing stored yet. Scenarios that die during a cycle, and the ones a finished cycle didn't take, are kept here."));
  }

  function renderScore(h, st) {
    const box = $("cy-score");
    box.replaceChildren();
    const sc = h?.score;
    if (!sc) { box.append(el("p", "hint", "Scoring the model on the last 60 days…")); return; }
    const skill = sc.skill * 100;
    box.append(el("p", "cy-verdict", `Tested on ${sc.n.toLocaleString()} past cycles of ${cfg.sym}: the top scenario was right ${pct(sc.topHit)} of the time at the start of a cycle and ${pct(sc.topHit10)} ten minutes in. `
      + (skill > 0.5 ? `That is ${skill.toFixed(1)}% better than always quoting ${cfg.sym}'s average odds: a small edge, as expected 15 minutes out.`
        : `That is no better than always quoting ${cfg.sym}'s average odds: treat the early odds as a base rate, not a forecast.`)));
    const table = el("table", "sb");
    const hr = el("tr", "sb-head");
    hr.append(el("th", "", "WHEN IT SAID"), el("th", "", "IT HAPPENED"), el("th", "", "TIMES"));
    table.append(hr);
    for (const b of sc.calibration) {
      const tr = el("tr");
      tr.append(el("td", "", b.range), el("td", "", b.happened == null ? "—" : pct(b.happened)), el("td", "", b.n.toLocaleString()));
      table.append(tr);
    }
    box.append(table, el("p", "hint", "Odds shown are corrected by this record (if \"68%\" used to happen 76% of the time, 76% is shown)."));
    const rec = st.record || [];
    if (rec.length) {
      const hit = rec.filter((x) => x.start && x.start.indexOf(Math.max(...x.start)) === x.won).length;
      box.append(el("p", "cy-verdict", `Live here: ${rec.length} cycles watched; the top scenario at the start was right ${hit} times (${pct(hit / rec.length)}).`));
      const list = el("div", "cy-rec");
      for (const x of rec.slice(0, 12)) {
        const row = el("div", "");
        row.append(el("span", "arch-c", x.label), el("b", "", T.SCEN[x.won].name),
                   el("span", "arch-n", x.start ? `it had ${pct(x.start[x.won])} at the start${x.mid ? `, ${pct(x.mid[x.won])} halfway` : ""}` : ""));
        list.append(row);
      }
      box.append(list);
    }
  }

  async function refresh() {
    const sym = cfg.sym || symbols()[0];
    if (sym) await ensure(sym);
    render();
  }

  $("cy-arch-toggle").addEventListener("click", () => { cfg.archiveOpen = !cfg.archiveOpen; render(); });
  for (const b of $("cy-depth").children) if (b.dataset.d) b.addEventListener("click", () => { cfg.depth = +b.dataset.d; save(); render(); });

  return {
    refresh, render,
    applyQuotes(map) { live = map; if (cfg.sym && H.get(cfg.sym)?.H) { if (app.onCycles()) render(); else step(cfg.sym); } },
    symbols: () => (cfg.sym ? [cfg.sym] : []),
    lastTick: () => lastTick,
  };
}
