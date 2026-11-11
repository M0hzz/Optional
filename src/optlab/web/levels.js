// Support and resistance levels from daily bars. Pure functions; tested with node --test.
//
// The rule, start to finish ("day" means one bar: the same rule runs on 5-minute, hourly or weekly bars):
//   1. Look at the last `lookback` days only (the RANGE you pick: 1M = 21 days ... 5Y = 1260).
//   2. Swing points: a day whose high is the highest of the k days on each side is a swing high; likewise a
//      swing low. k grows with the range (2 for a month, up to 10 for years), so a long range finds major turns
//      and a short range finds the recent ones. The range's own highest high and lowest low always count.
//   3. Swing points within a tolerance of each other (half a 14-day ATR, at least 0.4% of the price) are the
//      same level: its price is their average and each point is a "touch".
//   4. Levels above today's price are resistance, below are support, nearest first. A level is stronger the
//      more touches it has.

export const RANGES = { "1M": 21, "3M": 63, "6M": 126, "1Y": 252, "2Y": 504, "5Y": 1260 };

// The ranges offered on each timeframe, as a number of that timeframe's bars, and the one it starts on.
// (5m: 78 bars a session; 15m: 26; 1h: 7; 1w: 52 a year.)
export const TF_RANGES = {
  "5m": { set: { "1D": 78, "2D": 156, "5D": 390, "10D": 780 }, start: "2D" },
  "15m": { set: { "2D": 52, "5D": 130, "10D": 260, "1M": 546 }, start: "5D" },
  "1h": { set: { "5D": 35, "1M": 147, "3M": 441, "6M": 882 }, start: "1M" },
  "1d": { set: RANGES, start: "6M" },
  "1w": { set: { "6M": 26, "1Y": 52, "2Y": 104, "5Y": 260 }, start: "1Y" },
};
export const TF_LABEL = { "5m": "5 MIN", "15m": "15 MIN", "1h": "1 HOUR", "1d": "DAILY", "1w": "WEEKLY" };
export const rangeBars = (tf, label) => { const r = TF_RANGES[tf] || TF_RANGES["1d"]; return r.set[label] ?? r.set[r.start]; };

export function atr(bars, n = 14) {
  const b = bars.slice(-(n + 1));
  if (b.length < 2) return 0;
  let s = 0;
  for (let i = 1; i < b.length; i++) s += Math.max(b[i].h - b[i].l, Math.abs(b[i].h - b[i - 1].c), Math.abs(b[i].l - b[i - 1].c));
  return s / (b.length - 1);
}

export function swings(bars, k) {
  const out = [];
  for (let i = k; i < bars.length - k; i++) {
    let hi = true, lo = true;
    for (let j = i - k; j <= i + k; j++) {
      if (j === i) continue;
      if (bars[j].h > bars[i].h) hi = false;
      if (bars[j].l < bars[i].l) lo = false;
    }
    if (hi) out.push({ p: bars[i].h, i, kind: "high" });
    if (lo) out.push({ p: bars[i].l, i, kind: "low" });
  }
  return out;
}

// minTolPct: the least two swing points must differ by (as a fraction of the price) to be separate levels.
// Daily bars use 0.4%; intraday charts pass less (TF_TOL) so levels a few cents apart stay separate.
export const TF_TOL = { "5m": 0.001, "15m": 0.0015, "1h": 0.0025, "1d": 0.004, "1w": 0.006 };

export function findLevels(bars, lookback, price, max = 4, minTolPct = 0.004) {
  const sub = bars.slice(-Math.max(lookback, 10));
  const n = sub.length;
  if (n < 5 || !(price > 0)) return { resistance: [], support: [], tol: 0, k: 0 };
  const k = Math.min(10, Math.max(2, Math.round(n / 40)));
  const pts = swings(sub, k);
  let hiI = 0, loI = 0;
  sub.forEach((b, i) => { if (b.h > sub[hiI].h) hiI = i; if (b.l < sub[loI].l) loI = i; });
  if (!pts.some((p) => p.i === hiI && p.kind === "high")) pts.push({ p: sub[hiI].h, i: hiI, kind: "high" });
  if (!pts.some((p) => p.i === loI && p.kind === "low")) pts.push({ p: sub[loI].l, i: loI, kind: "low" });
  const tol = Math.max(0.5 * atr(sub), price * minTolPct);
  pts.sort((a, b) => a.p - b.p);
  const groups = [];
  for (const p of pts) {
    const g = groups[groups.length - 1];
    if (g && p.p - g.pts[g.pts.length - 1].p <= tol && p.p - g.pts[0].p <= 2 * tol) g.pts.push(p);
    else groups.push({ pts: [p] });
  }
  const levels = groups.map((g) => {
    const last = Math.max(...g.pts.map((p) => p.i));
    return { price: Math.round(g.pts.reduce((s, p) => s + p.p, 0) / g.pts.length * 100) / 100,
             touches: g.pts.length, daysAgo: n - 1 - last };
  });
  const gap = price * minTolPct * 0.375;                       // a level right at the price is neither yet
  const resistance = levels.filter((l) => l.price > price + gap).sort((a, b) => a.price - b.price).slice(0, max);
  const support = levels.filter((l) => l.price < price - gap).sort((a, b) => b.price - a.price).slice(0, max);
  return { resistance, support, tol, k };
}
