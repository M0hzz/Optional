// The home page and the monitor dock.
//
//   POSITIONS   every tab that holds a contract: live P&L, the simulated P&L from whatever its levers and scenario
//               are set to, the underlying's trend and where its price sits between support and resistance.
//   MONITOR     symbols watched whether or not you hold anything on them: trend health, RSI, nearest levels.
//   CHAINS      an anchor symbol and the symbols linked to it, compared day by day (see rel.js).
//   DOCK        the same watched symbols as a strip pinned to the bottom of every tab; symbols linked to the
//               tab's own ticker come first, with how they relate to it.
//
// Quotes for the monitor come from a light route (no option chain), refreshed with every poll.
import * as I from "./ind.js";
import * as LV from "./levels.js";
import * as R from "./rel.js";

const KEY = "optlab-sim-watch-v1";
const MAX_SYMBOLS = 24;
const INTRADAY = new Set(["5m", "15m", "1h"]);
const clean = (s) => String(s || "").trim().toUpperCase().replace(/[-/ ]/g, ".").replace(/[^A-Z0-9.]/g, "").slice(0, 8);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

export function createHome(app) {
  const { $, api, money, signed, pct, tone } = app;
  let cfg = { watch: ["SPY", "QQQ", "NVDA"], chains: [{ anchor: "NVDA", links: ["QQQ", "SPY"] }], range: "6M", tf: "1d", dock: true };
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "null");
    if (saved && Array.isArray(saved.watch)) cfg = { ...cfg, ...saved };
    if (!LV.TF_RANGES[cfg.tf]) cfg.tf = "1d";
  } catch { /* defaults */ }
  const data = new Map();          // sym -> {quote, bars, barsAt, read, lv, err}
  let openCard = null;             // the dock symbol whose detail card is open

  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(cfg)); } catch { /* private window */ } };
  // everything whose trend the home page shows: watched symbols, chain members and the positions' underlyings
  const symbols = () => [...new Set([...cfg.watch, ...cfg.chains.flatMap((c) => [c.anchor, ...c.links]),
                                     ...app.positions().map((p) => p.symbol)])].slice(0, MAX_SYMBOLS);
  const unitWord = () => (cfg.tf === "1d" ? "days" : cfg.tf === "1w" ? "weeks" : "bars");
  const get = (sym) => { if (!data.has(sym)) data.set(sym, {}); return data.get(sym); };

  function compute(sym) {
    const d = get(sym);
    if (!d.bars || !d.bars.length) return;
    const price = d.quote?.price ?? d.bars.at(-1).c;
    const closes = d.bars.map((b) => b.c);
    closes[closes.length - 1] = price;
    d.price = price;
    d.read = closes.length >= 30 ? I.readout(closes) : null;
    d.lv = LV.findLevels(d.bars, LV.rangeBars(cfg.tf, cfg.range), price, 4, LV.TF_TOL[cfg.tf]);
  }

  async function refresh() {
    const syms = symbols();
    if (!syms.length) return render();
    const now = Date.now();
    const tf = cfg.tf, every = INTRADAY.has(tf) ? 25000 : 600000;
    const needBars = syms.filter((s) => !get(s).bars || get(s).tf !== tf || now - (get(s).barsAt || 0) > every);
    const jobs = needBars.map((s) => api(`/api/bars/${encodeURIComponent(s)}?tf=${tf}`).then((b) => {
      if (cfg.tf !== tf) return;
      Object.assign(get(s), { bars: b.bars, tf, avgVolume: b.levels.avg_volume, barsAt: Date.now(), err: null });
    }).catch((e) => { get(s).err = e.message; }));
    jobs.push(api(`/api/quotes?symbols=${encodeURIComponent(syms.join(","))}`).then((r) => {
      for (const [s, q] of Object.entries(r.quotes)) {
        if (q.error) get(s).err = q.error; else Object.assign(get(s), { quote: q, err: get(s).bars ? null : get(s).err });
      }
    }).catch(() => {}));
    await Promise.all(jobs);
    syms.forEach(compute);
    render();
  }

  // ------------------------------------------------------------ small pieces
  function trendChip(read) {
    const v = read?.verdict;
    const c = el("span", `chip-trend ${v ? v.tone : "flat"}`, v ? v.value : "…");
    if (v) c.title = v.text;
    return c;
  }

  function lights(read) {
    const box = el("span", "lights");
    for (const [key, label] of [["short", "S"], ["medium", "M"], ["long", "L"], ["momentum", "Mo"]]) {
      const v = read?.[key], dot = el("i", `light ${v ? "l-" + v.tone : "off"}`, label);
      dot.title = v ? `${{ short: "Short term (EMA 9/21)", medium: "Medium term (SMA 50)", long: "Long term (SMA 200)", momentum: "Momentum (MACD)" }[key]}: ${v.value}, ${v.text}` : "not enough history";
      box.append(dot);
    }
    return box;
  }

  // Where the price sits between the nearest support and resistance.
  function rangeBar(price, lv, marks = []) {
    const s = lv?.support[0]?.price, r = lv?.resistance[0]?.price;
    const wrap = el("div", "rbar");
    const lo = s ?? price * 0.95, hi = r ?? price * 1.05;
    const pos = (v) => `${Math.min(100, Math.max(0, 100 * (v - lo) / (hi - lo))).toFixed(1)}%`;
    const track = el("div", "rbar-track");
    const dot = el("i", "rbar-dot");
    dot.style.setProperty("--x", pos(price));
    track.append(dot);
    for (const m of marks) if (m.v > lo && m.v < hi) { const k = el("i", `rbar-mark ${m.cls}`); k.style.setProperty("--x", pos(m.v)); k.title = m.title; track.append(k); }
    const ends = el("div", "rbar-ends");
    const left = el("span", "sup", s ? `S ${s.toFixed(2)} (${pct(100 * (s / price - 1), 1)})` : "no support below");
    const right = el("span", "res", r ? `R ${r.toFixed(2)} (${pct(100 * (r / price - 1), 1)})` : "no resistance above");
    if (s && price / s - 1 < 0.01) left.classList.add("near");
    if (r && r / price - 1 < 0.01) right.classList.add("near");
    ends.append(left, right);
    wrap.append(track, ends);
    return wrap;
  }

  const chg = (v) => { const c = el("span", "chg", v == null ? "" : pct(v, 2)); tone(c, v || 0); return c; };

  // ------------------------------------------------------------ home: positions
  function renderPositions() {
    const rows = app.positions();
    const box = $("home-positions");
    $("home-pos-empty").hidden = rows.length > 0;
    let cost = 0, live = 0, sim = 0;
    box.replaceChildren(...rows.map((p) => {
      cost += p.cost; live += p.livePnl ?? 0; sim += p.simPnl ?? 0;
      const card = el("button", `pos ${p.isCall ? "call" : "put"}`);
      card.title = "Open this tab";
      const head = el("div", "pos-head");
      head.append(el("b", "pos-name", p.name), el("span", "pos-sub", `${p.expires} · ${p.qty} × ${money(p.entry)}`));
      const nums = el("div", "pos-nums");
      const a = el("div"); a.append(el("span", "k", "LIVE P&L"));
      const av = el("span", "v", signed(p.livePnl, 0)); tone(av, p.livePnl || 0); a.append(av, el("span", "k2", pct(p.livePct, 1)));
      const b = el("div"); b.append(el("span", "k", "SIMULATED"));
      const bv = el("span", "v", signed(p.simPnl, 0)); tone(bv, p.simPnl || 0); b.append(bv, el("span", "k2", p.simNote));
      const c = el("div"); c.append(el("span", "k", p.symbol), el("span", "v", money(p.spot)), chg(p.changePct));
      nums.append(a, b, c);
      const h = get(p.symbol);                      // the home page's timeframe, once its bars are in
      const read = h.read ?? p.read, lv = h.lv ?? p.lv;
      const tr = el("div", "pos-trend");
      tr.append(trendChip(read), lights(read));
      card.append(head, nums, tr, rangeBar(p.spot, lv, [{ v: p.strike, cls: "strike", title: `strike ${p.strike}` }, { v: p.breakeven, cls: "be", title: `breakeven ${p.breakeven.toFixed(2)}` }]));
      card.addEventListener("click", () => app.openTab(p.index));
      return card;
    }));
    const t = $("home-totals");
    t.hidden = rows.length === 0;
    if (rows.length) {
      t.replaceChildren();
      for (const [label, v, isMoney] of [["POSITIONS", rows.length, false], ["AT RISK", cost, true], ["LIVE P&L", live, null], ["SIMULATED P&L", sim, null]]) {
        const d = el("div", "tot"); d.append(el("span", "k", label));
        const val = el("span", "v", isMoney === false ? String(v) : isMoney ? money(v, 0) : signed(v, 0));
        if (isMoney === null) tone(val, v);
        d.append(val); t.append(d);
      }
    }
  }

  // ------------------------------------------------------------ home: monitor
  function monitorCard(sym) {
    const d = get(sym), card = el("div", "mon");
    const head = el("div", "mon-head");
    const open = el("button", "mon-sym", sym);
    open.title = "Open a tab for this symbol";
    open.addEventListener("click", () => app.openSymbol(sym));
    const x = el("button", "mon-x", "×");
    x.setAttribute("aria-label", `Stop watching ${sym}`);
    x.addEventListener("click", () => { cfg.watch = cfg.watch.filter((s) => s !== sym); save(); render(); });
    head.append(open, el("span", "mon-px", d.price != null ? money(d.price) : "…"), chg(d.quote?.change_pct), x);
    card.append(head);
    if (d.err && !d.bars) { card.append(el("p", "mon-err", d.err)); return card; }
    card.append(el("div", "mon-name", d.quote?.name || ""));
    const tr = el("div", "pos-trend");
    tr.append(trendChip(d.read), lights(d.read));
    card.append(tr);
    const stats = el("div", "mon-stats");
    const rsi = d.read?.rsi;
    const s1 = el("span", rsi ? `rsi ${rsi.tone}` : "rsi", rsi ? `RSI ${rsi.value}` : "RSI —");
    if (rsi) s1.title = rsi.text;
    const rv = d.quote?.volume && d.avgVolume ? d.quote.volume / d.avgVolume : null;
    const s2 = el("span", "", rv == null ? "" : `VOL ${rv.toFixed(1)}× avg so far`);
    const s3 = el("span", "", d.read?.bands?.text.includes("SQUEEZE") ? "SQUEEZE" : "");
    stats.append(s1, s2, s3);
    card.append(stats);
    if (d.price != null) card.append(rangeBar(d.price, d.lv));
    return card;
  }

  function renderMonitor() {
    $("home-monitor").replaceChildren(...cfg.watch.map(monitorCard));
    const set = LV.TF_RANGES[cfg.tf].set;
    if (!(cfg.range in set)) cfg.range = LV.TF_RANGES[cfg.tf].start;
    const lab = el("span", "k", "LEVELS FROM");
    $("home-ranges").replaceChildren(lab, ...Object.keys(set).map((r) => {
      const b = el("button", "tg", r);
      b.setAttribute("aria-pressed", r === cfg.range);
      b.addEventListener("click", () => { cfg.range = r; save(); symbols().forEach(compute); render(); });
      return b;
    }));
    for (const b of $("home-tfs").children) if (b.dataset.tf) b.setAttribute("aria-pressed", b.dataset.tf === cfg.tf);
  }

  // ------------------------------------------------------------ home: chains
  const side = (sym) => { const d = get(sym); return { bars: d.bars || [], changePct: d.quote?.change_pct ?? null, verdict: d.read?.verdict.value }; };

  function relationRow(anchor, link) {
    const d = get(link), row = el("div", "rel");
    const name = el("button", "mon-sym", link);
    name.addEventListener("click", () => app.openSymbol(link));
    row.append(name, el("span", "rel-px", d.price != null ? money(d.price) : "…"), chg(d.quote?.change_pct), trendChip(d.read));
    const r = d.bars && get(anchor).bars ? R.relation(side(anchor), side(link), unitWord()) : null;
    const U = unitWord().toUpperCase();
    const cells = el("div", "rel-cells");
    const cell = (k, v, title, cls = "") => { const c = el("div", `rel-cell ${cls}`); c.title = title; c.append(el("span", "k", k), el("span", "v", v)); cells.append(c); };
    if (r) {
      cell("MOVE TOGETHER", `${r.corr == null ? "—" : r.corr.toFixed(2)} ${R.corrWord(r.corr)}`, `Correlation of returns over the last 60 ${unitWord()}: 1 = together, 0 = unrelated, −1 = opposite`);
      cell(`${anchor} PER 1%`, r.beta == null ? "—" : `${r.beta.toFixed(2)}%`, `How much ${anchor} moves when ${link} moves 1% (beta, 60 days)`);
      cell(`5 ${U}`, r.s5 == null ? "—" : `${r.s5 >= 0 ? "+" : "−"}${Math.abs(r.s5).toFixed(1)} pts`, `${anchor}'s return over 5 ${unitWord()} minus ${link}'s`, r.s5 > 0 ? "up" : r.s5 < 0 ? "down" : "");
      cell(`20 ${U}`, r.s20 == null ? "—" : `${r.s20 >= 0 ? "+" : "−"}${Math.abs(r.s20).toFixed(1)} pts`, `${anchor}'s return over 20 ${unitWord()} minus ${link}'s`, r.s20 > 0 ? "up" : r.s20 < 0 ? "down" : "");
      cell("GAP TODAY", r.gap == null ? "—" : `${r.gap >= 0 ? "+" : "−"}${Math.abs(r.gap).toFixed(2)}%`, `${anchor}'s change today minus what ${link}'s move implies (beta × its change)`, Math.abs(r.gap ?? 0) >= 1.5 ? "hot" : "");
    }
    row.append(cells);
    const read = el("div", "rel-read");
    if (r) {
      read.append(el("span", "", `${anchor} is ${r.lead} vs ${link}.`));
      for (const f of r.flags) read.append(el("span", "flag", f));
    } else read.append(el("span", "", d.err || "loading…"));
    const x = el("button", "mon-x", "×");
    x.setAttribute("aria-label", `Unlink ${link} from ${anchor}`);
    x.addEventListener("click", () => { const c = cfg.chains.find((q) => q.anchor === anchor); c.links = c.links.filter((s) => s !== link); save(); render(); });
    row.append(read, x);
    return row;
  }

  function renderChains() {
    $("home-chains").replaceChildren(...cfg.chains.map((c) => {
      const d = get(c.anchor), card = el("div", "chain");
      const head = el("div", "chain-head");
      const name = el("button", "mon-sym big", c.anchor);
      name.addEventListener("click", () => app.openSymbol(c.anchor));
      const del = el("button", "tg", "REMOVE CHAIN");
      del.addEventListener("click", () => { cfg.chains = cfg.chains.filter((q) => q !== c); save(); render(); });
      head.append(el("span", "k", "ANCHOR"), name, el("span", "mon-px", d.price != null ? money(d.price) : "…"), chg(d.quote?.change_pct), trendChip(d.read), lights(d.read), del);
      card.append(head, ...c.links.map((l) => relationRow(c.anchor, l)));
      const form = el("form", "chain-add");
      const input = el("input"); input.placeholder = "LINK A SYMBOL"; input.maxLength = 8; input.setAttribute("aria-label", `Link a symbol to ${c.anchor}`);
      const btn = el("button", "tg", "+ LINK"); btn.type = "submit";
      form.append(input, btn);
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const s = clean(input.value);
        if (s && s !== c.anchor && !c.links.includes(s) && symbols().length < MAX_SYMBOLS) { c.links.push(s); save(); refresh(); }
        input.value = "";
      });
      card.append(form);
      return card;
    }));
    $("home-chain-empty").hidden = cfg.chains.length > 0;
  }

  // ------------------------------------------------------------ the dock (on every tab)
  function renderDock() {
    const dock = $("dock"), cur = app.currentSymbol();
    dock.hidden = !$("home").hidden;                 // the home page already shows all of it
    dock.classList.toggle("closed", !cfg.dock);
    $("dock-toggle").textContent = cfg.dock ? "MONITOR ▾" : "MONITOR ▴";
    const linked = new Set();
    for (const c of cfg.chains) {
      if (c.anchor === cur) c.links.forEach((l) => linked.add(l));
      else if (c.links.includes(cur)) linked.add(c.anchor);
    }
    const order = [...linked, ...symbols().filter((s) => !linked.has(s) && s !== cur)];
    const curSide = cur ? app.currentSide() : null;
    $("dock-chips").replaceChildren(...order.map((sym) => {
      const d = get(sym), chip = el("button", `dchip t-${d.read?.verdict.tone || "flat"}${linked.has(sym) ? " linked" : ""}${openCard === sym ? " on" : ""}`);
      const top = el("span", "dchip-top");
      top.append(el("b", "", sym), el("span", "", d.price != null ? money(d.price) : "…"), chg(d.quote?.change_pct));
      chip.append(top);
      let rel = null;
      if (linked.has(sym) && curSide && d.bars) rel = R.relation(curSide, side(sym));
      const sub = el("span", "dchip-sub", rel ? `⛓ ${cur} ${rel.lead.replace(/ over 20 \w+$/, "")}${rel.flags.length ? " · " + rel.flags[0] : ""}`
        : `${d.read?.verdict.value ?? ""}${d.read?.rsi ? " · RSI " + d.read.rsi.value : ""}`);
      if (rel?.flags.length) sub.classList.add("flagged");
      chip.append(sub);
      chip.addEventListener("click", () => { openCard = openCard === sym ? null : sym; renderDock(); });
      return chip;
    }));
    const card = $("dock-card");
    card.hidden = !openCard || !cfg.dock;
    if (!card.hidden) {
      const sym = openCard, d = get(sym);
      card.replaceChildren();
      const head = el("div", "mon-head");
      const open = el("button", "tg", "OPEN TAB"); open.addEventListener("click", () => { openCard = null; app.openSymbol(sym); });
      const close = el("button", "mon-x", "×"); close.setAttribute("aria-label", "Close"); close.addEventListener("click", () => { openCard = null; renderDock(); });
      head.append(el("b", "mon-sym big", sym), el("span", "mon-px", d.price != null ? money(d.price) : "…"), chg(d.quote?.change_pct), open, close);
      const tr = el("div", "pos-trend"); tr.append(trendChip(d.read), lights(d.read));
      card.append(head, el("div", "mon-name", d.quote?.name || d.err || ""), tr);
      if (d.price != null) card.append(rangeBar(d.price, d.lv));
      if (cur && cur !== sym && curSide && d.bars) {
        const r = R.relation(curSide, side(sym));
        if (r) {
          const p = el("p", "dock-rel");
          p.textContent = `${cur} vs ${sym}: moves together ${r.corr == null ? "—" : r.corr.toFixed(2)} (${R.corrWord(r.corr)}), ${cur} moves ${r.beta == null ? "—" : r.beta.toFixed(2)}% per 1%, ${r.lead}.`;
          card.append(p);
          for (const f of r.flags) card.append(el("span", "flag", f));
        }
      }
    }
  }

  function render() {
    renderDock();
    document.body.classList.toggle("has-dock", !$("dock").hidden);
    if ($("home").hidden) return;
    renderPositions();
    renderMonitor();
    renderChains();
  }

  // ------------------------------------------------------------ wiring
  $("home-watch-add").addEventListener("submit", (e) => {
    e.preventDefault();
    for (const raw of $("home-watch-input").value.split(/[ ,]+/)) {
      const s = clean(raw);
      if (s && !cfg.watch.includes(s) && symbols().length < MAX_SYMBOLS) cfg.watch.push(s);
    }
    $("home-watch-input").value = "";
    save(); refresh();
  });
  $("home-chain-add").addEventListener("submit", (e) => {
    e.preventDefault();
    const a = clean($("home-chain-anchor").value);
    const links = [...new Set($("home-chain-links").value.split(/[ ,]+/).map(clean).filter((s) => s && s !== a))];
    if (!a || !links.length) return;
    const have = cfg.chains.find((c) => c.anchor === a);
    if (have) have.links = [...new Set([...have.links, ...links])]; else cfg.chains.push({ anchor: a, links });
    $("home-chain-anchor").value = $("home-chain-links").value = "";
    save(); refresh();
  });
  for (const b of $("home-tfs").children) {
    if (!b.dataset.tf) continue;
    b.addEventListener("click", () => {
      if (cfg.tf === b.dataset.tf) return;
      cfg.tf = b.dataset.tf;
      cfg.range = LV.TF_RANGES[cfg.tf].start;
      for (const d of data.values()) { d.read = null; d.lv = null; }        // stale until the new bars arrive
      save(); render(); refresh();
    });
  }
  $("dock-toggle").addEventListener("click", () => { cfg.dock = !cfg.dock; openCard = null; save(); renderDock(); });

  return { refresh, render, symbols };
}
