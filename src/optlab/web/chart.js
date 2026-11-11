// The interactive price chart: candles, volume and RSI or MACD in three stacked panes that share one x axis.
//
//   wheel / pinch / + -   zoom around the cursor         drag      pan
//   hover / touch         crosshair with the bar's values double-click or RESET   back to the default view
//
// x is measured in "slots": one per day. Real days are 0 .. histLen-1; the simulated future continues from
// histLen-1 (today) as fractional slots, so a step 2.5 days ahead sits at histLen - 1 + 2.5.
// While both real and simulated days are in view, the simulated ones get at least 30% of the width, so a
// 5-day scenario stays readable next to years of history.
const SIM_SHARE = 0.3;

const C = {
  bg: "#050816", grid: "#151d40", axis: "#6f7ea8", text: "#dfe9ff",
  up: "#26d07c", down: "#ff4d6d", upSim: "rgba(38, 208, 124, 0.55)", downSim: "rgba(255, 77, 109, 0.55)",
  res: "#ff8a3d", sup: "#3db8ff", strike: "#ffc531", be: "#eaf6ff",
  ema9: "#ffe066", ema21: "#ff7ae6", sma50: "#9db4ff", sma200: "#c9cfdf", bb: "#7f8fb8",
  story: "#7dff5a", roll: "#ffffff", band: "rgba(125, 255, 90, 0.10)",
  vol: "rgba(111, 126, 168, 0.55)", volSim: "rgba(125, 255, 90, 0.35)",
};
export const COLORS = C;
const FONT = "11px ui-monospace, Menlo, Consolas, monospace";

export class PriceChart {
  constructor(canvas, { onViewChange } = {}) {
    this.cv = canvas;
    this.g = canvas.getContext("2d");
    this.m = null;                    // the model (set by setModel)
    this.view = null;                 // {a, b}: the slots shown
    this.hover = null;                // {x, y} in CSS pixels
    this.onViewChange = onViewChange;
    this.pointers = new Map();
    this._wire();
  }

  // m: {bars, histLen, futureDays, closes, ind, overlays, osc, levels, strike, breakeven, band, story, roll,
  //     simBars, simVol, lever, markers, defaultBars}
  setModel(m, view) {
    const prevLast = this.m ? this.lastSlot() : null;
    this.m = m;
    if (view) this.view = { ...view };
    if (!this.view) this.resetView();
    else if (prevLast != null && this.lastSlot() !== prevLast && this.view.b >= prevLast - 0.5) {
      // the simulated days grew or shrank while you were looking at the right edge: follow them
      this.view = { a: this.view.a, b: this.lastSlot() + 0.8 };
      this.onViewChange?.(this.view);
    }
    this.draw();
  }

  lastSlot() { return this.m.histLen - 1 + Math.max(this.m.futureDays, 0); }

  defaultView() {
    const end = this.lastSlot() + 0.8, show = Math.min(this.m.defaultBars, this.m.histLen) + Math.max(this.m.futureDays, 0) + 1;
    return { a: end - show, b: end };
  }

  resetView() {
    this.view = this.defaultView();
    this.onViewChange?.(this.view);
  }

  zoom(f, atX = null) {
    const { a, b } = this.view, w = b - a;
    const anchor = atX == null ? (a + b) / 2 : this.invX(atX);
    const nw = Math.min(Math.max(w * f, 8), this.m.histLen + this.m.futureDays + 20);
    const k = (anchor - a) / w;
    this.view = { a: anchor - k * nw, b: anchor - k * nw + nw };
    this._clamp();
    this.onViewChange?.(this.view);
    this.draw();
  }

  pan(dSlots) {
    this.view = { a: this.view.a + dSlots, b: this.view.b + dSlots };
    this._clamp();
    this.onViewChange?.(this.view);
    this.draw();
  }

  _clamp() {
    const w = this.view.b - this.view.a, lo = -2, hi = this.lastSlot() + 3;
    if (this.view.a < lo) this.view = { a: lo, b: lo + w };
    if (this.view.b > hi) this.view = { a: hi - w, b: hi };
  }

  _wire() {
    const cv = this.cv;
    cv.addEventListener("wheel", (e) => {
      if (!this.m) return;
      e.preventDefault();
      const r = cv.getBoundingClientRect();
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) this.pan(e.deltaX / (this.L.x1 - this.L.x0) * (this.view.b - this.view.a));
      else this.zoom(Math.exp(e.deltaY * 0.0015), e.clientX - r.left);
    }, { passive: false });
    cv.addEventListener("pointerdown", (e) => {
      cv.setPointerCapture(e.pointerId);
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, moved: false });
    });
    cv.addEventListener("pointermove", (e) => {
      if (!this.m) return;
      const r = cv.getBoundingClientRect();
      const p = this.pointers.get(e.pointerId);
      if (p && this.pointers.size === 1 && (e.pointerType === "mouse" ? e.buttons & 1 : true)) {
        const dx = e.clientX - p.x;
        if (Math.abs(e.clientX - p.x0) > 4) p.moved = true;
        if (p.moved && dx) this.pan(this.invX(p.x - r.left) - this.invX(e.clientX - r.left));
        p.x = e.clientX; p.y = e.clientY;
      } else if (p && this.pointers.size === 2) {            // pinch
        const [q1, q2] = [...this.pointers.values()];
        const before = Math.abs(q1.x - q2.x);
        p.x = e.clientX; p.y = e.clientY;
        const after = Math.abs(q1.x - q2.x);
        if (before > 10 && after > 10) this.zoom(before / after, (q1.x + q2.x) / 2 - r.left);
      }
      this.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
      this.draw();
    });
    const up = (e) => { this.pointers.delete(e.pointerId); if (e.pointerType !== "mouse") { this.hover = null; this.draw(); } };
    cv.addEventListener("pointerup", up);
    cv.addEventListener("pointercancel", up);
    cv.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") { this.hover = null; this.draw(); } });
    cv.addEventListener("dblclick", () => { if (this.m) { this.resetView(); this.draw(); } });
  }

  // ------------------------------------------------------------ drawing
  draw() {
    const m = this.m;
    if (!m) return;
    const cv = this.cv, dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const g = this.g;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = C.bg;
    g.fillRect(0, 0, w, h);
    g.font = FONT;
    g.textBaseline = "alphabetic";

    const x0 = 8, x1 = w - 62, axisH = 18;
    const usable = h - axisH - 8;
    const volH = m.showVolume ? Math.round(usable * 0.14) : 0, oscH = Math.round(usable * 0.2), gap = 8;
    const priceH = usable - volH - oscH - gap * (m.showVolume ? 2 : 1);
    const P = { top: 6, bot: 6 + priceH }, V = { top: P.bot + gap, bot: P.bot + gap + volH }, O = { top: (m.showVolume ? V.bot : P.bot) + gap, bot: (m.showVolume ? V.bot : P.bot) + gap + oscH };
    this.L = { x0, x1, P, V, O, w, h };
    const { a, b } = this.view;
    const hiIdx = m.histLen - 1;
    // piecewise x: real days on the left of `split`, simulated days on the right
    let split = x0 + (hiIdx - a) / (b - a) * (x1 - x0);
    const both = m.futureDays > 0 && a < hiIdx && b > hiIdx;
    if (both) split = Math.min(split, x1 - SIM_SHARE * (x1 - x0));
    const X = both
      ? (s) => (s <= hiIdx ? x0 + (s - a) / (hiIdx - a) * (split - x0) : split + (s - hiIdx) / (b - hiIdx) * (x1 - split))
      : (s) => x0 + (s - a) / (b - a) * (x1 - x0);
    this.X = X;
    this.invX = both
      ? (px) => (px <= split ? a + (px - x0) / (split - x0) * (hiIdx - a) : hiIdx + (px - split) / (x1 - split) * (b - hiIdx))
      : (px) => a + (px - x0) / (x1 - x0) * (b - a);
    const slotW = both ? (split - x0) / (hiIdx - a) : (x1 - x0) / (b - a);
    const simSlotW = both ? (x1 - split) / (b - hiIdx) : slotW;
    const visible = (s) => s >= a - 1 && s <= b + 1;
    const ia = Math.max(0, Math.floor(a)), ib = Math.min(m.histLen - 1, Math.ceil(b));

    // ---------------- y range of the price pane: what's on screen
    const vals = [];
    for (let i = ia; i <= ib; i++) vals.push(m.bars[i].h, m.bars[i].l);
    for (const sb of m.simBars) if (visible(sb.s)) vals.push(sb.h, sb.l);
    if (m.story) for (const p of m.story) if (visible(p.s)) vals.push(p.p);
    if (m.band) m.band.forEach((p) => { if (visible(p.s)) vals.push(p.lo, p.hi); });
    if (m.lever && visible(m.lever.s)) vals.push(m.lever.p);
    const overlays = this._overlays();
    for (const [series] of overlays) {
      for (let i = Math.max(ia, 0); i <= Math.min(Math.ceil(b), series.length - 1); i++) if (series[i] != null) vals.push(series[i]);
    }
    if (!vals.length) vals.push(m.bars[hiIdx].c);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.06 || hi * 0.01;
    lo -= pad; hi += pad;
    const Y = (v) => P.top + (P.bot - P.top) * (1 - (v - lo) / (hi - lo));

    g.save();
    g.beginPath(); g.rect(x0, 0, x1 - x0, h); g.clip();
    // grid and the line between real and simulated
    g.strokeStyle = C.grid; g.lineWidth = 1;
    const ticks = niceTicks(lo, hi, 5);
    for (const t of ticks) { g.beginPath(); g.moveTo(x0, Y(t)); g.lineTo(x1, Y(t)); g.stroke(); }
    if (m.futureDays > 0 && visible(hiIdx)) {
      g.fillStyle = "rgba(125, 255, 90, 0.035)";
      g.fillRect(X(hiIdx), P.top, x1 - X(hiIdx), O.bot - P.top);
      g.strokeStyle = "rgba(125, 255, 90, 0.35)"; g.setLineDash([3, 4]);
      g.beginPath(); g.moveTo(X(hiIdx), P.top); g.lineTo(X(hiIdx), O.bot); g.stroke(); g.setLineDash([]);
      g.fillStyle = C.story; g.fillText("SIMULATION ▶", X(hiIdx) + 6, P.top + 12);
    }

    // levels
    for (const lv of m.levels) {
      if (lv.price < lo || lv.price > hi) continue;
      const color = lv.kind === "R" ? C.res : C.sup;
      g.strokeStyle = color; g.globalAlpha = lv.active ? 0.95 : 0.45; g.lineWidth = lv.active ? 1.6 : 1;
      g.setLineDash(lv.active ? [] : [6, 5]);
      g.beginPath(); g.moveTo(x0, Y(lv.price)); g.lineTo(x1, Y(lv.price)); g.stroke();
      g.setLineDash([]); g.globalAlpha = 1;
    }
    for (const [v, color, dash] of [[m.strike, C.strike, [2, 3]], [m.breakeven, C.be, [8, 4]]]) {
      if (v == null || v < lo || v > hi) continue;
      g.strokeStyle = color; g.globalAlpha = 0.7; g.setLineDash(dash); g.lineWidth = 1;
      g.beginPath(); g.moveTo(x0, Y(v)); g.lineTo(x1, Y(v)); g.stroke(); g.setLineDash([]); g.globalAlpha = 1;
    }

    // the band of 8 in 10 rolls
    if (m.band && m.band.length > 1) {
      g.fillStyle = C.band; g.beginPath();
      m.band.forEach((p, i) => (i ? g.lineTo(X(p.s), Y(p.hi)) : g.moveTo(X(p.s), Y(p.hi))));
      for (let i = m.band.length - 1; i >= 0; i--) g.lineTo(X(m.band[i].s), Y(m.band[i].lo));
      g.closePath(); g.fill();
    }
    // Bollinger fill under the lines
    if (m.overlays.bb && m.ind) {
      const { up, lo: bl } = m.ind.bb;
      const s0 = Math.max(Math.floor(a), 0), s1 = Math.min(Math.ceil(b), up.length - 1);
      g.fillStyle = "rgba(127, 143, 184, 0.07)"; g.beginPath();
      let first = true;
      for (let i = s0; i <= s1; i++) if (up[i] != null) { first ? g.moveTo(X(this._slot(i)), Y(up[i])) : g.lineTo(X(this._slot(i)), Y(up[i])); first = false; }
      for (let i = s1; i >= s0; i--) if (bl[i] != null) g.lineTo(X(this._slot(i)), Y(bl[i]));
      g.closePath(); g.fill();
    }

    // candles
    const bwHist = Math.max(1, Math.min(slotW * 0.68, 14)), bw = bwHist, bwSim = Math.max(1, Math.min(simSlotW * 0.68, 22));
    const candle = (s, o, hh, ll, c, sim) => {
      const upc = c >= o, col = sim ? (upc ? C.upSim : C.downSim) : (upc ? C.up : C.down);
      const x = X(s), bw = sim ? bwSim : bwHist;
      g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 1;
      g.beginPath(); g.moveTo(Math.round(x) + 0.5, Y(hh)); g.lineTo(Math.round(x) + 0.5, Y(ll)); g.stroke();
      const yT = Y(Math.max(o, c)), yB = Y(Math.min(o, c));
      if (sim) { g.strokeRect(x - bw / 2, yT, bw, Math.max(yB - yT, 1)); }
      else g.fillRect(x - bw / 2, yT, bw, Math.max(yB - yT, 1));
    };
    for (let i = ia; i <= ib; i++) { const k = m.bars[i]; candle(i, k.o, k.h, k.l, k.c, false); }
    for (const sb of m.simBars) if (visible(sb.s)) candle(sb.s, sb.o, sb.h, sb.l, sb.c, true);

    // indicator lines
    for (const [series, color, width] of overlays) {
      g.strokeStyle = color; g.lineWidth = width; g.beginPath();
      let on = false;
      const s0 = Math.max(Math.floor(a) - 1, 0), s1 = Math.min(Math.ceil(b) + 1, series.length - 1);
      for (let i = s0; i <= s1; i++) {
        const v = series[i];
        if (v == null) { on = false; continue; }
        const px = X(this._slot(i)), py = Y(v);
        if (on) g.lineTo(px, py); else { g.moveTo(px, py); on = true; }
      }
      g.stroke();
    }
    // the scenario's story and the roll being played
    const path = (pts, color, width, glow) => {
      if (!pts || pts.length < 2) return;
      g.strokeStyle = color; g.lineWidth = width; g.shadowColor = color; g.shadowBlur = glow;
      g.beginPath(); pts.forEach((p, i) => (i ? g.lineTo(X(p.s), Y(p.p)) : g.moveTo(X(p.s), Y(p.p)))); g.stroke();
      g.shadowBlur = 0;
    };
    path(m.story, C.story, m.roll ? 1.2 : 2, m.roll ? 0 : 8);
    path(m.roll, C.roll, 1.6, 6);
    for (const e of m.markers) {
      const s = this._slot(e.i), v = m.closes[e.i];
      if (!visible(s) || v == null) continue;
      const px = X(s), py = Y(v);
      g.fillStyle = e.dir > 0 ? C.up : e.dir < 0 ? C.down : C.strike;
      g.beginPath();
      if (e.dir >= 0) { g.moveTo(px, py - 16); g.lineTo(px - 6, py - 7); g.lineTo(px + 6, py - 7); }
      else { g.moveTo(px, py + 16); g.lineTo(px - 6, py + 7); g.lineTo(px + 6, py + 7); }
      g.closePath(); g.fill();
    }
    if (m.lever && visible(m.lever.s)) {
      g.fillStyle = C.text; g.shadowColor = C.text; g.shadowBlur = 12;
      g.beginPath(); g.arc(X(m.lever.s), Y(m.lever.p), 5, 0, Math.PI * 2); g.fill(); g.shadowBlur = 0;
    }

    // volume
    if (m.showVolume) {
      const vs = [];
      for (let i = ia; i <= ib; i++) vs.push(m.bars[i].v);
      for (const sv of m.simVol) if (visible(sv.s)) vs.push(sv.v);
      const vmax = Math.max(1, ...vs);
      const VY = (v) => V.bot - (V.bot - V.top) * (v / vmax);
      for (let i = ia; i <= ib; i++) {
        const k = m.bars[i];
        g.fillStyle = k.c >= k.o ? "rgba(38, 208, 124, 0.45)" : "rgba(255, 77, 109, 0.45)";
        g.fillRect(X(i) - bw / 2, VY(k.v), bw, V.bot - VY(k.v));
      }
      for (const sv of m.simVol) if (visible(sv.s)) { g.fillStyle = C.volSim; g.fillRect(X(sv.s) - bwSim / 2, VY(sv.v), bwSim, V.bot - VY(sv.v)); }
      if (m.avgVolume) {
        g.strokeStyle = "rgba(255, 197, 49, 0.6)"; g.setLineDash([4, 4]);
        g.beginPath(); g.moveTo(x0, VY(m.avgVolume)); g.lineTo(x1, VY(m.avgVolume)); g.stroke(); g.setLineDash([]);
      }
    }

    // oscillator
    const ind = m.ind;
    if (ind) {
      const s0 = Math.max(Math.floor(a) - 1, 0), s1 = Math.min(Math.ceil(b) + 1, m.closes.length - 1);
      if (m.osc === "rsi") {
        const OY = (v) => O.bot - (O.bot - O.top) * (v / 100);
        g.fillStyle = "rgba(255, 77, 109, 0.07)"; g.fillRect(x0, OY(100), x1 - x0, OY(70) - OY(100));
        g.fillStyle = "rgba(38, 208, 124, 0.07)"; g.fillRect(x0, OY(30), x1 - x0, OY(0) - OY(30));
        g.strokeStyle = C.grid;
        for (const v of [30, 50, 70]) { g.beginPath(); g.moveTo(x0, OY(v)); g.lineTo(x1, OY(v)); g.stroke(); }
        this._line(g, ind.rsi, s0, s1, X, OY, "#c9b8ff", 1.4);
        this.OY = OY;
      } else {
        const { line, signal, hist } = ind.macd;
        let amax = 0;
        for (let i = s0; i <= s1; i++) for (const s of [line, signal, hist]) if (s[i] != null) amax = Math.max(amax, Math.abs(s[i]));
        amax = amax * 1.15 || 1;
        const OY = (v) => O.top + (O.bot - O.top) * (1 - (v + amax) / (2 * amax));
        g.strokeStyle = C.grid; g.beginPath(); g.moveTo(x0, OY(0)); g.lineTo(x1, OY(0)); g.stroke();
        for (let i = s0; i <= s1; i++) {
          if (hist[i] == null) continue;
          const sim = i > hiIdx, w2 = sim ? bwSim : bw;
          g.fillStyle = hist[i] >= 0 ? (sim ? C.upSim : "rgba(38, 208, 124, 0.6)") : (sim ? C.downSim : "rgba(255, 77, 109, 0.6)");
          g.fillRect(X(this._slot(i)) - w2 / 2, Math.min(OY(0), OY(hist[i])), w2, Math.abs(OY(hist[i]) - OY(0)));
        }
        this._line(g, line, s0, s1, X, OY, "#5fd8ff", 1.4);
        this._line(g, signal, s0, s1, X, OY, "#ff9d5c", 1.2);
        this.OY = OY;
      }
    }
    g.restore();

    // pane labels, right axis
    g.fillStyle = C.axis;
    for (const t of ticks) g.fillText(fmt(t), x1 + 6, Y(t) + 4);
    g.fillText(m.osc === "rsi" ? "RSI 14" : "MACD 12/26/9", x0 + 4, O.top + 12);
    if (m.osc === "rsi" && this.OY) { g.fillText("70", x1 + 6, this.OY(70) + 4); g.fillText("30", x1 + 6, this.OY(30) + 4); }
    if (m.showVolume) g.fillText("VOLUME" + (m.avgVolume ? " · dashed: 20-day average" : ""), x0 + 4, V.top + 12);
    // level tags on the axis
    for (const lv of m.levels) {
      if (lv.price < lo || lv.price > hi) continue;
      this._tag(g, x1, Y(lv.price), fmt(lv.price), lv.kind === "R" ? C.res : C.sup, lv.active);
    }
    if (m.strike != null && m.strike > lo && m.strike < hi) this._tag(g, x1, Y(m.strike), `K ${fmt(m.strike)}`, C.strike, false);
    // legend (top left)
    let lx = x0 + 6;
    const legend = [...overlays.filter((o) => o[3]).map((o) => [o[3], o[1]])];
    if (m.levels.length) legend.push(["RES", C.res], ["SUP", C.sup]);
    for (const [label, color] of legend) { g.fillStyle = color; g.fillText(label, lx, P.top + 12 + (m.futureDays > 0 ? 14 : 0)); lx += g.measureText(label).width + 10; }
    // x axis: dates for real days, D+n for simulated ones
    g.fillStyle = C.axis;
    const step = Math.max(1, Math.ceil(70 / slotW));
    let lastLabelX = -1e9;
    for (let s = Math.ceil(a); s <= Math.floor(b); s++) {
      let label = null;
      if (s >= 0 && s <= hiIdx) {
        const d = m.bars[s].d, prev = s > 0 ? m.bars[s - 1].d : null;
        const monthStart = prev && d.slice(0, 7) !== prev.slice(0, 7);
        if (step <= 25 ? (s % step === 0) : monthStart) label = step > 25 && d.slice(5, 7) === "01" ? d.slice(0, 4) : shortDate(d);
      } else if (s > hiIdx && s <= this.lastSlot()) label = `D+${s - hiIdx}`;
      if (!label) continue;
      const px = X(s);
      if (px - lastLabelX < 60) continue;
      lastLabelX = px;
      g.fillText(label, px - g.measureText(label).width / 2, h - 5);
    }

    if (this.hover) this._crosshair(g, X, Y, lo, hi, a, b);
  }

  _slot(i) { return i; }

  _overlays() {
    const m = this.m, ind = m.ind, O = m.overlays, out = [];
    if (!ind) return out;
    if (O.ema) out.push([ind.ema9, C.ema9, 1.3, "EMA 9"], [ind.ema21, C.ema21, 1.3, "EMA 21"]);
    if (O.sma50) out.push([ind.sma50, C.sma50, 1.6, "SMA 50"]);
    if (O.sma200) out.push([ind.sma200, C.sma200, 1.6, "SMA 200"]);
    if (O.bb) out.push([ind.bb.up, C.bb, 1, "BOLLINGER"], [ind.bb.lo, C.bb, 1, null]);
    return out;
  }

  _line(g, s, s0, s1, X, Y, color, width) {
    g.strokeStyle = color; g.lineWidth = width; g.beginPath();
    let on = false;
    for (let i = s0; i <= s1; i++) {
      if (s[i] == null) { on = false; continue; }
      if (on) g.lineTo(X(i), Y(s[i])); else { g.moveTo(X(i), Y(s[i])); on = true; }
    }
    g.stroke();
  }

  _tag(g, x, y, text, color, strong) {
    const w = g.measureText(text).width + 8;
    g.fillStyle = strong ? color : C.bg;
    g.strokeStyle = color;
    g.fillRect(x + 2, y - 8, w, 15);
    g.strokeRect(x + 2, y - 8, w, 15);
    g.fillStyle = strong ? C.bg : color;
    g.fillText(text, x + 6, y + 4);
  }

  // The bar under the cursor and everything known about it.
  _crosshair(g, X, Y, lo, hi, a, b) {
    const m = this.m, { x0, x1, P, O } = this.L, { x, y } = this.hover;
    if (x < x0 || x > x1) return;
    const s = Math.round(this.invX(x));
    const hiIdx = m.histLen - 1;
    if (s < 0 || s > this.lastSlot()) return;
    const sx = X(s);
    g.strokeStyle = "rgba(223, 233, 255, 0.35)"; g.setLineDash([3, 3]); g.lineWidth = 1;
    g.beginPath(); g.moveTo(sx, P.top); g.lineTo(sx, O.bot); g.stroke();
    if (y >= P.top && y <= P.bot) {
      g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
      const v = lo + (hi - lo) * (1 - (y - P.top) / (P.bot - P.top));
      this._tag(g, x1, y, fmt(v), C.text, true);
    }
    g.setLineDash([]);
    const rows = [];
    let bar = null, title;
    if (s <= hiIdx) { bar = m.bars[s]; title = longDate(bar.d); }
    else { bar = m.simBars.find((q) => q.s === s); title = `SIMULATED DAY +${s - hiIdx}`; }
    if (bar) rows.push(["O", fmt(bar.o)], ["H", fmt(bar.h)], ["L", fmt(bar.l)], ["C", fmt(bar.c)]);
    else if (s > hiIdx && m.closes[s] != null) rows.push(["C (story)", fmt(m.closes[s])]);
    if (bar && s > 0) {
      const prev = s <= hiIdx ? m.bars[s - 1].c : (s - 1 === hiIdx ? m.bars[hiIdx].c : m.simBars.find((q) => q.s === s - 1)?.c);
      if (prev) rows.push(["CHG", `${bar.c >= prev ? "+" : ""}${(100 * (bar.c / prev - 1)).toFixed(2)}%`]);
    }
    const vol = s <= hiIdx ? m.bars[s].v : m.simVol.find((q) => q.s === s)?.v;
    if (vol != null) rows.push(["VOL", compact(vol) + (m.avgVolume ? ` (${(vol / m.avgVolume).toFixed(1)}×)` : "")]);
    const ind = m.ind;
    if (ind && s < m.closes.length) {
      const add = (label, v, d = 2) => { if (v != null) rows.push([label, v.toFixed(d)]); };
      if (m.overlays.ema) { add("EMA 9", ind.ema9[s]); add("EMA 21", ind.ema21[s]); }
      if (m.overlays.sma50) add("SMA 50", ind.sma50[s]);
      if (m.overlays.sma200) add("SMA 200", ind.sma200[s]);
      add("RSI", ind.rsi[s], 0);
      add("MACD H", ind.macd.hist[s], 3);
    }
    for (const e of m.markers) if (e.i === s) rows.push(["▶", e.label]);
    // box
    g.font = FONT;
    const lw = Math.max(...rows.map((r) => g.measureText(r[0]).width), 0) + 10;
    const bwid = Math.max(g.measureText(title).width, ...rows.map((r) => lw + g.measureText(r[1]).width)) + 16;
    const bh = 20 + rows.length * 14;
    let bx = sx + 14, by = P.top + 6;
    if (bx + bwid > x1) bx = sx - 14 - bwid;
    g.fillStyle = "rgba(5, 8, 22, 0.92)"; g.strokeStyle = "#2c3a7a";
    g.fillRect(bx, by, bwid, bh); g.strokeRect(bx, by, bwid, bh);
    g.fillStyle = s > hiIdx ? C.story : C.text;
    g.fillText(title, bx + 8, by + 14);
    rows.forEach(([k, v], i) => {
      g.fillStyle = C.axis; g.fillText(k, bx + 8, by + 30 + i * 14);
      g.fillStyle = C.text; g.fillText(v, bx + 8 + lw, by + 30 + i * 14);
    });
  }
}

function niceTicks(lo, hi, n) {
  const span = hi - lo, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(+v.toFixed(10));
  return out;
}
const fmt = (v) => (Math.abs(v) >= 1000 ? v.toFixed(0) : Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2));
const compact = (v) => (v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "K" : String(Math.round(v)));
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const shortDate = (d) => `${MON.at(+d.slice(5, 7) - 1)} ${+d.slice(8, 10)}`;
const longDate = (d) => `${shortDate(d)} ${d.slice(0, 4)}`;
