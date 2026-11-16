// The cycle tracker's model. Pure functions; tested with node --test.
//
// A cycle is a 15-minute window of the regular session (09:30-09:45, 09:45-10:00, ...). Where the price ends a
// cycle, measured from where it started in the ticker's own typical 15-minute move (sigma), puts it in one of five
// scenarios:
//
//   BREAKOUT UP    at or above the nearest resistance (or +1.5 sigma when no resistance is within 3 sigma)
//   DRIFT UP       +0.5 sigma or more, short of that
//   CHOP           within +-0.5 sigma
//   DRIFT DOWN     -0.5 sigma or more, short of a breakdown
//   BREAKDOWN      at or below the nearest support (or -1.5 sigma)
//
// Starting odds: the 5-minute history (60 days) is searched for the moments most like the cycle's start (short
// trend, RSI, room to the day's high and low, volume, time of day), and how each of those went over its next 15
// minutes is counted, pulled toward the ticker's overall record so a handful of lookalikes can't claim certainty.
//
// Live: with part of the cycle gone and the price already moved x sigma, each lookalike's remaining move is scaled
// to the time left and added to x. As time runs out the spread shrinks, so the odds polarise on their own: one
// scenario rises toward 100% and the ones the price can no longer reach die.
//
// Later cycles are conditional: each assumes its parent happened. The parent's typical ending (the average end
// among the lookalikes that landed in it) is appended to the history as three hypothetical 5-minute bars, and the
// next cycle's odds are worked out from that state, with its own levels. Siblings therefore always add up to 100%.

import * as I from "./ind.js";

export const SCEN = [
  { id: "bu", name: "BREAKOUT UP", dir: 2 },
  { id: "du", name: "DRIFT UP", dir: 1 },
  { id: "ch", name: "CHOP", dir: 0 },
  { id: "dd", name: "DRIFT DOWN", dir: -1 },
  { id: "bd", name: "BREAKDOWN", dir: -2 },
];
export const BARS = 3;                 // 5-minute bars per cycle
export const OPEN_MIN = 570;           // 09:30
export const CLOSE_MIN = 960;          // 16:00
const K = 120, PRIOR = 40;             // lookalikes counted, and how many "average" moments they're blended with
const W = [1, 0.7, 0.6, 0.6, 0.4, 0.8];

export const minuteOf = (d) => +d.slice(11, 13) * 60 + +d.slice(14, 16);
export const dayOf = (d) => d.slice(0, 10);

function normCdf(x) {
  const t = 1 / (1 + 0.2316419 * Math.abs(x)), d = 0.3989422804014327 * Math.exp(-x * x / 2);
  const p = d * t * (0.31938153 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  return x >= 0 ? 1 - p : p;
}

// ------------------------------------------------------------------ preparing the history
// bars: 5-minute bars {d: "YYYY-MM-DDTHH:MM", o, h, l, c, v}, regular hours, oldest first.
export function prepare(bars) {
  const n = bars.length, c = bars.map((b) => b.c);
  const ind = { ema9: I.ema(c, 9), ema21: I.ema(c, 21), rsi: I.rsi(c) };
  // sigma: the standard deviation of 15-minute log returns over the previous ~5 sessions, at each bar
  const sig = new Float64Array(n), r3 = new Float64Array(n);
  for (let i = BARS; i < n; i++) r3[i] = Math.log(c[i] / c[i - BARS]);
  let s1 = 0, s2 = 0, m = 0;
  const WIN = 390;
  for (let i = BARS; i < n; i++) {
    s1 += r3[i]; s2 += r3[i] * r3[i]; m++;
    if (i - WIN >= BARS) { s1 -= r3[i - WIN]; s2 -= r3[i - WIN] ** 2; m--; }
    const v = m > 20 ? (s2 - s1 * s1 / m) / (m - 1) : NaN;
    sig[i] = v > 0 ? Math.sqrt(v) : NaN;
  }
  for (let i = 0; i < n; i++) if (!(sig[i] > 0)) sig[i] = NaN;
  // the day's range so far and 20-bar average volume, per bar
  const hiD = new Float64Array(n), loD = new Float64Array(n), avgV = new Float64Array(n);
  let vs = 0;
  for (let i = 0; i < n; i++) {
    const newDay = i === 0 || dayOf(bars[i].d) !== dayOf(bars[i - 1].d);
    hiD[i] = newDay ? bars[i].h : Math.max(hiD[i - 1], bars[i].h);
    loD[i] = newDay ? bars[i].l : Math.min(loD[i - 1], bars[i].l);
    vs += bars[i].v; if (i >= 20) vs -= bars[i - 20].v;
    avgV[i] = vs / Math.min(i + 1, 20);
  }
  const feats = new Float64Array(n * 6), out = new Float64Array(n).fill(NaN), ok = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const f = featureAt(c[i], ind.ema9[i], ind.ema21[i], ind.rsi[i], hiD[i], loD[i], bars[i].v, avgV[i], minuteOf(bars[i].d) + 5, sig[i]);
    if (!f) continue;
    feats.set(f, i * 6);
    // the outcome: the 15-minute move that followed, in sigma, if it stayed inside the same session
    if (i + BARS < n && dayOf(bars[i + BARS].d) === dayOf(bars[i].d)) {
      out[i] = Math.log(c[i + BARS] / c[i]) / sig[i];
      ok[i] = 1;
    }
  }
  return { bars, n, c, ind, sig, hiD, loD, avgV, feats, out, ok };
}

// The six numbers that describe a moment. `minute` is the minute of the day the moment ends at.
export function featureAt(price, e9, e21, rsi, hi, lo, vol, avgVol, minute, sig) {
  if (e9 == null || e21 == null || rsi == null || !(sig > 0)) return null;
  const u = price * sig, clamp = (x, a) => Math.max(-a, Math.min(a, x));
  return [
    clamp((e9 - e21) / u, 3),
    (rsi - 50) / 25,
    Math.min((price - lo) / u, 6) / 3,
    Math.min((hi - price) / u, 6) / 3,
    clamp(Math.log(Math.max(vol, 1) / Math.max(avgVol, 1)), 2) / 2,
    (minute - OPEN_MIN) / (CLOSE_MIN - OPEN_MIN),
  ];
}

// The moments most like `f` among those before `upto` (so a replay never peeks ahead), with the overall record
// as a prior: [{r, w}] where r is the 15-minute move in sigma.
export function lookalikes(H, f, upto = H.n) {
  const lim = Math.max(0, upto - BARS), cand = [];
  for (let j = 0; j < lim; j++) {
    if (!H.ok[j] || j + BARS >= upto) continue;
    let d = 0;
    for (let k = 0; k < 6; k++) { const x = H.feats[j * 6 + k] - f[k]; d += W[k] * x * x; }
    cand.push([d, j]);
  }
  if (!cand.length) return [];
  const all = cand.length;
  cand.sort((a, b) => a[0] - b[0]);
  const near = cand.slice(0, K), out = near.map(([, j]) => ({ r: H.out[j], w: 1 }));
  const step = Math.max(1, Math.floor(all / 200)), wu = PRIOR / Math.ceil(all / step);
  for (let i = 0; i < all; i += step) out.push({ r: H.out[cand[i][1]], w: wu });
  return out;
}

// Where the five scenarios start and stop, in sigma from the cycle's starting price.
export function thresholds(price, sig, levels) {
  const u = price * sig;
  // a level is resistance or support by where it sits now: once broken, an old resistance is the support
  const all = [...(levels?.resistance || []), ...(levels?.support || [])].map((l) => l.price);
  const above = all.filter((v) => v > price), below = all.filter((v) => v < price);
  const res = above.length ? { price: Math.min(...above) } : null, sup = below.length ? { price: Math.max(...below) } : null;
  const up = res && (res.price - price) / u <= 3 ? (res.price - price) / u : 1.5;
  const dn = sup && (price - sup.price) / u <= 3 ? (price - sup.price) / u : 1.5;
  return { up: Math.max(up, 0.75), dn: Math.max(dn, 0.75), res: res?.price ?? null, sup: sup?.price ?? null };
}

const edges = (t) => [-Infinity, -t.dn, -0.5, 0.5, t.up, Infinity];   // BREAKDOWN .. BREAKOUT, low to high

export function bucketOf(x, t) {
  if (x >= t.up) return 0;
  if (x >= 0.5) return 1;
  if (x > -0.5) return 2;
  if (x > -t.dn) return 3;
  return 4;
}

// Odds of the five scenarios (in SCEN order) with `e` of the cycle gone (0..1) and the price already `x` sigma
// from the start, and each scenario's typical ending (in sigma) for the cycle after it.
export function odds(samples, x, e, t) {
  const left = Math.max(0, 1 - e), sq = Math.sqrt(left);
  let wsum = 0, mu = 0;
  for (const s of samples) { wsum += s.w; mu += s.w * s.r; }
  mu = wsum ? mu / wsum : 0;
  const bw = 0.25 * sq + 0.02, E = edges(t);
  const p = [0, 0, 0, 0, 0], endSum = [0, 0, 0, 0, 0], endW = [0, 0, 0, 0, 0];
  for (const s of samples) {
    const fin = x + mu * left + (s.r - mu) * sq;
    for (let b = 0; b < 5; b++) {             // b runs BREAKDOWN..BREAKOUT on the number line
      const lo = E[b], hi = E[b + 1];
      const m = (hi === Infinity ? 1 : normCdf((hi - fin) / bw)) - (lo === -Infinity ? 0 : normCdf((lo - fin) / bw));
      if (m <= 0) continue;
      const k = 4 - b;
      p[k] += s.w * m;
      endSum[k] += s.w * m * fin;
      endW[k] += s.w * m;
    }
  }
  const tot = p.reduce((a, b) => a + b, 0) || 1;
  const centre = [t.up + 0.4, (0.5 + t.up) / 2, 0, -(0.5 + t.dn) / 2, -t.dn - 0.4];
  return { p: p.map((v) => v / tot), end: endW.map((w, k) => (w > 1e-9 ? endSum[k] / w : centre[k])) };
}

// ------------------------------------------------------------------ the tree
// The state a cycle starts from: the real one now, or a hypothetical one after an assumed parent.
// {price, e9, e21, rsiG, rsiL, rsi, hi, lo, minute, sig, avgVol}
export function liveState(H, i, price = H.c[i]) {
  return { price, e9: H.ind.ema9[i], e21: H.ind.ema21[i], rsi: H.ind.rsi[i], ...rsiCarry(H.c, i),
           hi: Math.max(H.hiD[i], price), lo: Math.min(H.loD[i], price), minute: minuteOf(H.bars[i].d) + 5, sig: H.sig[i],
           vol: H.bars[i].v, avgVol: H.avgV[i] };
}

// Wilder's averages at bar i, so hypothetical bars can carry the RSI on.
function rsiCarry(c, i, n = 14) {
  if (i < n) return { rsiG: null, rsiL: null };
  let g = 0, l = 0;
  for (let k = 1; k <= n; k++) { const d = c[k] - c[k - 1]; if (d > 0) g += d; else l -= d; }
  g /= n; l /= n;
  for (let k = n + 1; k <= i; k++) { const d = c[k] - c[k - 1]; g = (g * (n - 1) + Math.max(d, 0)) / n; l = (l * (n - 1) + Math.max(-d, 0)) / n; }
  return { rsiG: g, rsiL: l };
}

// The state after a hypothetical cycle that ends `endSig` sigma from `s.price`: three bars walked in a straight line.
export function advance(s, endSig) {
  const end = s.price * Math.exp(endSig * s.sig), k9 = 2 / 10, k21 = 2 / 22;
  let { e9, e21, rsiG, rsiL, hi, lo } = s, prev = s.price;
  for (let b = 1; b <= BARS; b++) {
    const p = s.price + (end - s.price) * b / BARS, d = p - prev;
    e9 = p * k9 + e9 * (1 - k9);
    e21 = p * k21 + e21 * (1 - k21);
    if (rsiG != null) { rsiG = (rsiG * 13 + Math.max(d, 0)) / 14; rsiL = (rsiL * 13 + Math.max(-d, 0)) / 14; }
    hi = Math.max(hi, p); lo = Math.min(lo, p); prev = p;
  }
  const rsi = rsiG == null ? s.rsi : rsiL === 0 ? (rsiG === 0 ? 50 : 100) : 100 - 100 / (1 + rsiG / rsiL);
  let minute = s.minute + 15;
  if (minute > CLOSE_MIN) { minute = OPEN_MIN + 5; hi = end; lo = end; }    // past the close: the next session's open
  return { ...s, price: end, e9, e21, rsiG, rsiL, rsi, hi, lo, minute, vol: s.avgVol };
}

export const stateFeature = (s) => featureAt(s.price, s.e9, s.e21, s.rsi, s.hi, s.lo, s.vol, s.avgVol, s.minute, s.sig);

// One cycle's node: its odds, thresholds and lookalikes, from a start state; x and e for the live cycle.
export function cycle(H, s, levels, x = 0, e = 0) {
  const f = stateFeature(s);
  const samples = f ? lookalikes(H, f) : [];
  const t = thresholds(s.price, s.sig, levels);
  return { state: s, t, samples, ...odds(samples, x, e, t) };
}

// The children of a node: the next cycle, assuming scenario k of this one happened.
export function children(H, node, k, levels) {
  return cycle(H, advance(node.state, node.end[k]), levels);
}

// ------------------------------------------------------------------ the scoreboard
// Replay the history: at every cycle start, odds from only what came before, against what actually happened.
// Also the odds 5 and 10 minutes in, from the price then. Scores against always quoting the ticker's average
// odds up to that day (Brier score: lower is better; skill > 0 means better than average odds).
export function replay(H, levelsAt = () => null, minPast = 600) {
  const rec = [];
  for (let i = minPast; i + BARS < H.n; i++) {
    if (!H.ok[i - 1] && !(H.sig[i - 1] > 0)) continue;
    const start = i - 1, m = minuteOf(H.bars[i].d);
    if ((m - OPEN_MIN) % 15 !== 0 || m >= CLOSE_MIN) continue;
    if (dayOf(H.bars[i + BARS - 1].d) !== dayOf(H.bars[i].d) || dayOf(H.bars[start].d) !== dayOf(H.bars[i].d)) continue;
    const s = liveState(H, start), f = stateFeature(s);
    if (!f) continue;
    const samples = lookalikes(H, f, i);
    if (samples.length < 50) continue;
    const t = thresholds(s.price, s.sig, levelsAt(i));
    const endX = Math.log(H.c[start + BARS] / s.price) / s.sig;
    const won = bucketOf(endX, t);
    const at = (b) => odds(samples, Math.log(H.c[start + b] / s.price) / s.sig, b / BARS, t).p;
    const base = [0, 0, 0, 0, 0];
    let nb = 0;
    for (let j = 0; j < i - BARS; j++) if (H.ok[j]) { base[bucketOf(H.out[j], t)]++; nb++; }
    rec.push({ i, won, p0: odds(samples, 0, 0, t).p, p5: at(1), p10: at(2), base: base.map((v) => v / nb) });
  }
  const brier = (p, w) => p.reduce((a, v, k) => a + (v - (k === w ? 1 : 0)) ** 2, 0);
  const mean = (f) => rec.reduce((a, r) => a + f(r), 0) / (rec.length || 1);
  const bModel = mean((r) => brier(r.p0, r.won)), bBase = mean((r) => brier(r.base, r.won));
  const bins = [0.2, 0.4, 0.6, 0.8, 1.01].map((hi, k) => ({ lo: k * 0.2, hi, n: 0, said: 0, hit: 0 }));
  for (const r of rec) for (const p of [r.p0, r.p5, r.p10]) p.forEach((v, k) => {
    const b = bins.find((x) => v >= x.lo && v < x.hi);
    b.n++; b.said += v; b.hit += k === r.won ? 1 : 0;
  });
  return {
    n: rec.length, brier: bModel, brierBase: bBase, skill: bBase ? 1 - bModel / bBase : 0,
    topHit: mean((r) => (r.p0.indexOf(Math.max(...r.p0)) === r.won ? 1 : 0)),
    topHit10: mean((r) => (r.p10.indexOf(Math.max(...r.p10)) === r.won ? 1 : 0)),
    calibration: bins.map((b) => ({ range: `${Math.round(b.lo * 100)}-${Math.min(100, Math.round(b.hi * 100))}%`, n: b.n,
                                     said: b.n ? b.said / b.n : null, happened: b.n ? b.hit / b.n : null })),
  };
}

// A correction learned from the replay: if, in the past, "68%" happened 76% of the time, quote 76%. Piecewise
// linear through the replay's (said, happened) points, pinned at 0 and 100%, then renormalised so the five
// still add up to 100%. Bins with too few cycles are skipped.
export function calibrator(calibration, minN = 150) {
  const pts = [[0, 0], ...calibration.filter((b) => b.n >= minN && b.said != null).map((b) => [b.said, b.happened]), [1, 1]]
    .sort((a, b) => a[0] - b[0]);
  for (let i = 1; i < pts.length; i++) pts[i][1] = Math.max(pts[i][1], pts[i - 1][1]);    // keep it increasing
  const f = (v) => {
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
      if (v <= x1) return x1 === x0 ? y1 : y0 + (y1 - y0) * (v - x0) / (x1 - x0);
    }
    return 1;
  };
  return (p) => {
    const q = p.map(f), s = q.reduce((a, b) => a + b, 0) || 1;
    return q.map((v) => v / s);
  };
}
