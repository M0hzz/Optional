// The signal board and its alert log.
//
//   BOARD   one block per ticker, one line per timeframe, one square per indicator (ind.js BOARD and scores):
//           green = bullish, red = bearish, more opaque = stronger; white = neutral.
//   LOG     every time a square changes colour: when, which ticker, timeframe and indicator, what it was and is now,
//           and that line's squares at that moment. Saved in the browser (the last 300).
//   ALERTS  a toast on whatever page is open, an unread count on the SIGNALS tab, and optionally a browser
//           notification.
//
// Symbols come from the open tabs, and optionally from the home page's watched and chained symbols. The board
// refreshes with the rest of the page (every 15 s while it's open): intraday lines every refresh, daily and weekly
// every 10 minutes with today's price folded into the last bar. Colours seen when the page was last open are saved,
// so changes that happened while it was closed are logged when it opens again, marked as found on return.
import * as I from "./ind.js";
import * as V from "./vol.js";

const CFG_KEY = "optlab-sim-signals-v1";
const LOG_KEY = "optlab-sim-signal-log-v1";
const STATE_KEY = "optlab-sim-signal-state-v1";
const MAX_LOG = 1000;
const FILTER_KEY = "optlab-sim-signal-filter-v1";
const PERIODS = { "1h": ["LAST HOUR", 3600e3], today: ["TODAY", null], "7d": ["7 DAYS", 7 * 86400e3], all: ["ALL", Infinity] };
const TFS = ["5m", "15m", "1h", "1d", "1w"];
const ROW_LABEL = { "5m": "5 MIN", "15m": "15 MIN", "1h": "HOUR", "1d": "DAY", "1w": "WEEK" };
const INTRADAY = new Set(["5m", "15m", "1h"]);
const WORD = { 1: "GREEN", "-1": "RED", 0: "NEUTRAL" };
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const load = (k, d) => { try { return JSON.parse(localStorage.getItem(k) || "null") ?? d; } catch { return d; } };
const store = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window */ } };

// The log's filter: an empty list means "all". `turned` is which colours to show ("g", "r", "w"); `when` a PERIODS key.
export const NO_FILTER = { syms: [], tfs: [], keys: [], turned: ["g", "r"], when: "all" };
const COLOUR = { 1: "g", "-1": "r", 0: "w" };
export function filterLog(log, f, now = Date.now()) {
  const since = f.when === "today" ? new Date(now).setHours(0, 0, 0, 0) : now - (PERIODS[f.when]?.[1] ?? Infinity);
  return log.filter((e) => (!f.syms.length || f.syms.includes(e.sym)) && (!f.tfs.length || f.tfs.includes(e.tf))
    && (!f.keys.length || f.keys.includes(e.key)) && f.turned.includes(COLOUR[e.to]) && e.t >= since);
}

export function createSignals(app) {
  const { $, api } = app;
  let cfg = { rows: ["1d", "1w"], tabs: true, monitor: false, positionsOnly: false, notify: false, neutral: false, ...load(CFG_KEY, {}) };
  cfg.rows = cfg.rows.filter((t) => TFS.includes(t));
  if (!cfg.rows.length) cfg.rows = ["1d", "1w"];
  let log = load(LOG_KEY, []);
  let filter = { ...NO_FILTER, ...load(FILTER_KEY, {}) };
  filter.turned = filter.turned.filter((c) => c !== "w").concat(cfg.neutral ? ["w"] : []);   // NEUTRAL follows cfg.neutral
  const saveFilter = () => store(FILTER_KEY, filter);
  const EMPTY_TEXT = $("sb-log-empty").textContent;
  let states = load(STATE_KEY, {});           // "SYM|tf|key" -> -1 | 0 | 1, as last seen
  const firstLook = new Set();                // keys compared for the first time in this visit (changes = while away)
  const data = new Map();                     // "SYM|tf" -> {bars, at, sc, err}
  const quotes = new Map();
  let unread = load(LOG_KEY + "-unread", 0), justChanged = new Set(), busy = false;

  const save = () => store(CFG_KEY, cfg);
  const saveLog = () => { store(LOG_KEY, log.slice(0, MAX_LOG)); store(LOG_KEY + "-unread", unread); };

  // Which tickers the board follows, and whether each one has a position open on a tab.
  function symbols() {
    const out = new Map();
    if (cfg.tabs) for (const x of app.tabSymbols()) out.set(x.sym, (out.get(x.sym) || false) || x.position);
    if (cfg.monitor) for (const s of app.monitorSymbols()) if (!out.has(s)) out.set(s, false);
    let list = [...out].map(([sym, position]) => ({ sym, position }));
    if (cfg.positionsOnly) list = list.filter((x) => x.position);
    return list.slice(0, 20);
  }

  // The live price folded into the last bar, so the squares move between bar downloads: on intraday lines it is
  // the forming bar's close; on daily and weekly lines today's (or this week's) close.
  function withQuote(bars, tf, q) {
    if (!q || !bars.length) return bars.map((b) => b.c);
    if (INTRADAY.has(tf)) { const c = bars.map((b) => b.c); c[c.length - 1] = q.price; return c; }
    const closes = bars.map((b) => b.c), last = bars.at(-1).d;
    const same = tf === "1d" ? last === q.day : weekKey(last) === weekKey(q.day);
    if (same) closes[closes.length - 1] = q.price;
    else if (q.day > last) closes.push(q.price);
    return closes;
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const syms = symbols().map((x) => x.sym);
      if (syms.length) {
        const q = await api(`/api/quotes?symbols=${encodeURIComponent(syms.join(","))}`).catch(() => ({ quotes: {} }));
        for (const [s, v] of Object.entries(q.quotes)) if (!v.error) quotes.set(s, v);
        const now = Date.now(), jobs = [];
        for (const sym of syms) for (const tf of cfg.rows) {
          const key = `${sym}|${tf}`, d = data.get(key);
          if (d && now - d.at < (INTRADAY.has(tf) ? 25000 : 600000)) continue;
          jobs.push(api(`/api/bars/${encodeURIComponent(sym)}?tf=${tf}`)
            .then((b) => data.set(key, { bars: b.bars, at: Date.now() }))
            .catch((e) => data.set(key, { ...(d || {}), at: Date.now(), err: e.message })));
        }
        await Promise.all(jobs);
        diff(syms);
      }
    } finally { busy = false; }
    render();
  }

  // Score every line, compare each square's colour with the last one seen, and log what changed.
  function diff(syms) {
    const fresh = [], changed = new Set();
    for (const sym of syms) for (const tf of cfg.rows) {
      const d = data.get(`${sym}|${tf}`);
      if (!d?.bars?.length) continue;
      const closes = withQuote(d.bars, tf, quotes.get(sym));
      d.sc = I.scores(closes);
      const vs = V.barScore(d.bars, tf, app.clock(), app.volProfile(sym));     // volume isn't in the closes
      if (vs) d.sc.vol = { s: vs.s, text: vs.text };
      // today's daily bar is downloaded every 10 minutes; the 5-minute pace is fresher
      const pc = tf === "1d" ? app.volRead(sym)?.pace : null, q = quotes.get(sym);
      if (pc?.rvol != null && pc.label !== "last session" && q?.change_pct != null) {
        const dir = Math.sign(q.change_pct), w = V.word(pc.rvol).word.toLowerCase();
        d.sc.vol = { s: dir * Math.max(0, Math.min(1, (pc.rvol - 1) / 1.5)),
                     text: `${pc.rvol.toFixed(1)}× normal volume for this time of day (${w}), price ${q.change_pct >= 0 ? "up" : "down"} ${Math.abs(q.change_pct).toFixed(2)}% today` };
      }
      for (const [k] of I.BOARD) {
        const v = d.sc[k];
        if (!v) continue;
        const id = `${sym}|${tf}|${k}`, prev = states[id];
        const now = I.stateOf(v.s, prev ?? 0);
        if (prev !== undefined && prev !== now) {
          fresh.push({ t: Date.now(), sym, tf, key: k, from: prev, to: now, s: v.s, text: v.text, late: !firstLook.has(id),
                       row: Object.fromEntries(I.BOARD.map(([b]) => [b, d.sc[b] ? +d.sc[b].s.toFixed(2) : null])) });
          changed.add(id);
        }
        states[id] = now;
        firstLook.add(id);
      }
    }
    store(STATE_KEY, states);
    justChanged = changed;
    if (!fresh.length) return;
    log = [...fresh.reverse(), ...log].slice(0, MAX_LOG);
    const shown = fresh.filter((e) => cfg.neutral || e.to !== 0);
    if (!app.onSignals()) unread += shown.length;
    saveLog();
    toast(shown);
    notify(shown);
  }

  // ------------------------------------------------------------ squares
  function square(v, key, changedNow) {
    const sq = el("span", "sq");
    if (!v) { sq.classList.add("na"); sq.title = "not enough history"; return sq; }
    const st = I.stateOf(v.s, 0);
    sq.classList.add(st > 0 ? "g" : st < 0 ? "r" : "w");
    sq.style.setProperty("--a", (st === 0 ? 0.62 + 0.28 * (1 - Math.min(1, Math.abs(v.s) / 0.2)) : 0.3 + 0.7 * Math.min(1, Math.abs(v.s))).toFixed(2));
    if (v.mark) sq.append(el("i", "", v.mark));
    if (changedNow) sq.classList.add("pulse");
    sq.title = `${I.BOARD.find(([k]) => k === key)[1]}: ${v.text} (score ${v.s >= 0 ? "+" : ""}${v.s.toFixed(2)})`;
    return sq;
  }
  const squareOf = (s, key) => (s == null ? square(null, key) : square({ s, text: "at the time" }, key));

  function header(extra = 2) {
    const tr = el("tr", "sb-head");
    for (let i = 0; i < extra; i++) tr.append(el("th"));
    for (const [, label] of I.BOARD) tr.append(el("th", "", label));
    return tr;
  }

  function renderBoard() {
    const list = symbols(), box = $("sb-board");
    $("sb-empty").hidden = list.length > 0;
    if (!list.length) { box.replaceChildren(); return; }
    const table = el("table", "sb");
    table.append(header());
    for (const { sym, position } of list) {
      cfg.rows.forEach((tf, r) => {
        const tr = el("tr", r === 0 ? "sb-first" : "");
        if (r === 0) {
          const th = el("th", "sb-sym");
          th.rowSpan = cfg.rows.length;
          const b = el("button", "mon-sym", sym);
          b.title = "Open its tab";
          b.addEventListener("click", () => app.openSymbol(sym));
          th.append(b);
          const q = quotes.get(sym);
          if (q) { const c = el("span", "sb-chg", `${q.change_pct >= 0 ? "+" : "−"}${Math.abs(q.change_pct ?? 0).toFixed(2)}%`); app.tone(c, q.change_pct || 0); th.append(c); }
          if (position) th.append(el("span", "sb-pos", "POSITION"));
          tr.append(th);
        }
        tr.append(el("td", "sb-tf", ROW_LABEL[tf]));
        const d = data.get(`${sym}|${tf}`);
        for (const [k] of I.BOARD) {
          const td = el("td");
          td.append(d?.sc ? square(d.sc[k], k, justChanged.has(`${sym}|${tf}|${k}`)) : el("span", "sq na", d?.err ? "!" : "…"));
          if (d?.err && !d.sc) td.title = d.err;
          tr.append(td);
        }
        table.append(tr);
      });
    }
    box.replaceChildren(table);
  }

  // The filter chips: tickers and lines that appear in the log, every indicator, the colours and the period.
  function renderFilter() {
    const box = $("sb-filter");
    const toggleIn = (list, v) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
    const group = (label, items, isOn, onClick) => {
      const g = el("div", "toggles");
      g.append(el("span", "k", label));
      for (const [v, text, title] of items) {
        const b = el("button", "tg", text);
        b.setAttribute("aria-pressed", isOn(v));
        if (title) b.title = title;
        b.addEventListener("click", () => { onClick(v); saveFilter(); renderLog(); });
        g.append(b);
      }
      return g;
    };
    const syms = [...new Set([...log.map((e) => e.sym), ...filter.syms])].sort();
    const tfs = TFS.filter((t) => log.some((e) => e.tf === t) || filter.tfs.includes(t));
    const all = (key) => () => { filter[key] = []; };
    box.replaceChildren(
      group("TICKER", [["*", "ALL"], ...syms.map((s) => [s, s])], (v) => (v === "*" ? !filter.syms.length : filter.syms.includes(v)),
        (v) => (v === "*" ? all("syms")() : (filter.syms = toggleIn(filter.syms, v)))),
      group("LINE", [["*", "ALL"], ...tfs.map((t) => [t, ROW_LABEL[t]])], (v) => (v === "*" ? !filter.tfs.length : filter.tfs.includes(v)),
        (v) => (v === "*" ? all("tfs")() : (filter.tfs = toggleIn(filter.tfs, v)))),
      group("INDICATOR", [["*", "ALL"], ...I.BOARD], (v) => (v === "*" ? !filter.keys.length : filter.keys.includes(v)),
        (v) => (v === "*" ? all("keys")() : (filter.keys = toggleIn(filter.keys, v)))),
      group("TURNED", [["g", "GREEN"], ["r", "RED"], ["w", "NEUTRAL", "Also log turns to neutral as alerts (toasts and the count on the tab)"]],
        (v) => filter.turned.includes(v), (v) => {
          filter.turned = toggleIn(filter.turned, v);
          if (v === "w") { cfg.neutral = filter.turned.includes("w"); save(); }
        }),
      group("WHEN", Object.entries(PERIODS).map(([k, [t]]) => [k, t]), (v) => filter.when === v, (v) => { filter.when = v; }),
    );
  }

  function renderLog() {
    renderFilter();
    const matched = filterLog(log, filter), shown = matched.slice(0, 200);
    const narrowed = JSON.stringify({ ...filter, turned: filter.turned.filter((c) => c !== "w").sort() }) !== JSON.stringify(NO_FILTER);
    $("sb-log-count").textContent = log.length ? `SHOWING ${shown.length}${matched.length > shown.length ? " (newest)" : ""} OF ${matched.length} MATCHING · ${log.length} LOGGED` : "";
    $("sb-log-reset").hidden = !narrowed;
    $("sb-log-empty").hidden = shown.length > 0;
    $("sb-log-empty").textContent = log.length ? "Nothing in the log matches this filter." : EMPTY_TEXT;
    const table = el("table", "sb sb-log");
    if (shown.length) {
      const h = header(0);
      h.prepend(el("th", "", "TIME"), el("th", "", "TICKER"), el("th", "", "TF"), el("th", "", "WHAT CHANGED"));
      table.append(h);
    }
    for (const e of shown) {
      const tr = el("tr", e.to > 0 ? "lg-g" : e.to < 0 ? "lg-r" : "lg-w");
      const when = new Date(e.t);
      const td0 = el("td", "lg-t", when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
      if (when.toDateString() !== new Date().toDateString()) td0.textContent = when.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + td0.textContent;
      if (e.late) td0.append(el("small", "", "found on return"));
      const sym = el("button", "mon-sym", e.sym);
      sym.addEventListener("click", () => app.openSymbol(e.sym));
      const td1 = el("td"); td1.append(sym);
      const what = el("td", "lg-what");
      what.append(squareOf(e.from === 0 ? 0 : e.from * 0.6, e.key), el("span", "lg-arrow", "→"), square({ s: e.s, text: e.text }, e.key),
        el("b", "", `${I.BOARD.find(([k]) => k === e.key)[1]} turned ${WORD[e.to]}`), el("small", "", e.text));
      tr.append(td0, td1, el("td", "sb-tf", ROW_LABEL[e.tf]), what);
      for (const [k] of I.BOARD) { const td = el("td"); td.append(squareOf(e.row[k], k)); if (k === e.key) td.classList.add("lg-hit"); tr.append(td); }
      table.append(tr);
    }
    $("sb-log").replaceChildren(table);
  }

  function renderControls() {
    for (const b of $("sb-tfs").children) if (b.dataset.tf) b.setAttribute("aria-pressed", cfg.rows.includes(b.dataset.tf));
    for (const [id, k] of [["sb-src-tabs", "tabs"], ["sb-src-mon", "monitor"], ["sb-pos-only", "positionsOnly"]]) $(id).setAttribute("aria-pressed", !!cfg[k]);
    const perm = typeof Notification === "undefined" ? "unsupported" : Notification.permission;
    $("sb-notify").setAttribute("aria-pressed", cfg.notify && perm === "granted");
    $("sb-notify").title = perm === "unsupported" ? "This browser can't show notifications"
      : perm === "denied" ? "Notifications are blocked for this site in the browser's settings" : "Also show alerts as system notifications";
    $("tab-signals").querySelector("small").textContent = unread ? String(unread) : "";
    $("tab-signals").classList.toggle("has-unread", unread > 0);
  }

  function render() {
    renderControls();
    if (!app.onSignals()) return;
    renderBoard();
    renderLog();
  }

  // ------------------------------------------------------------ alerts
  function toast(list) {
    if (!list.length) return;
    const box = $("sb-toasts");
    for (const e of list.slice(0, 3)) {
      const t = el("button", `toast ${e.to > 0 ? "g" : e.to < 0 ? "r" : "w"}`);
      t.append(square({ s: e.s, text: e.text }, e.key), el("b", "", `${e.sym} · ${ROW_LABEL[e.tf]}`),
        el("span", "", `${I.BOARD.find(([k]) => k === e.key)[1]} turned ${WORD[e.to]}`));
      t.addEventListener("click", () => { t.remove(); app.showSignals(); });
      box.prepend(t);
      setTimeout(() => t.classList.add("out"), 9000);
      setTimeout(() => t.remove(), 9600);
    }
    if (list.length > 3) {
      const more = el("button", "toast w", `+${list.length - 3} more changes · open SIGNALS`);
      more.addEventListener("click", () => { more.remove(); app.showSignals(); });
      box.append(more);
      setTimeout(() => more.remove(), 9600);
    }
    while (box.children.length > 6) box.lastChild.remove();
  }

  function notify(list) {
    if (!cfg.notify || typeof Notification === "undefined" || Notification.permission !== "granted" || !list.length) return;
    const body = list.slice(0, 4).map((e) => `${e.sym} ${ROW_LABEL[e.tf]}: ${I.BOARD.find(([k]) => k === e.key)[1]} turned ${WORD[e.to]}`).join("\n")
      + (list.length > 4 ? `\n+${list.length - 4} more` : "");
    try { new Notification(list.length === 1 ? "optlab signal" : `optlab: ${list.length} signals`, { body, tag: "optlab-signals" }); } catch { /* not allowed here */ }
  }

  // ------------------------------------------------------------ wiring
  for (const b of $("sb-tfs").children) {
    if (!b.dataset.tf) continue;
    b.addEventListener("click", () => {
      const tf = b.dataset.tf;
      cfg.rows = cfg.rows.includes(tf) ? cfg.rows.filter((x) => x !== tf) : TFS.filter((x) => cfg.rows.includes(x) || x === tf);
      if (!cfg.rows.length) cfg.rows = [tf];
      save(); render(); refresh();
    });
  }
  $("sb-log-reset").addEventListener("click", () => { filter = { ...NO_FILTER, turned: ["g", "r", ...(cfg.neutral ? ["w"] : [])] }; saveFilter(); renderLog(); });
  for (const [id, k] of [["sb-src-tabs", "tabs"], ["sb-src-mon", "monitor"], ["sb-pos-only", "positionsOnly"]]) {
    $(id).addEventListener("click", () => { cfg[k] = !cfg[k]; save(); render(); refresh(); });
  }
  $("sb-notify").addEventListener("click", async () => {
    if (typeof Notification === "undefined") return;
    if (!cfg.notify && Notification.permission === "default") await Notification.requestPermission();
    cfg.notify = !cfg.notify && Notification.permission === "granted";
    save(); render();
  });
  $("sb-clear").addEventListener("click", () => { log = []; unread = 0; saveLog(); render(); });

  // Live quotes from the page's fast loop (every 3 s): rescore every line and log what changed colour.
  function applyQuotes(map) {
    const syms = symbols().map((x) => x.sym);
    for (const s of syms) if (map.has(s)) quotes.set(s, map.get(s));
    if (syms.some((s) => cfg.rows.some((tf) => data.get(`${s}|${tf}`)?.bars))) { diff(syms); render(); }
  }

  return {
    refresh, render, applyQuotes, symbols: () => symbols().map((x) => x.sym),
    seen() { unread = 0; saveLog(); renderControls(); },
  };
}

export function weekKey(iso) {
  const d = new Date(iso.slice(0, 10) + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));     // back to Monday
  return d.toISOString().slice(0, 10);
}

