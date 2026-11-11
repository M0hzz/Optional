// The simulator's maths. Pure functions: no page, no network. Tested with `node --test tests/`.
//
// What it models, and how (every rule is fixed and written here, nothing is fitted or predicted):
//   option value   Black-Scholes with the contract's own implied volatility, solved from its market price so
//                  the model matches the market before you touch a lever.
//   price path     a scripted story for each scenario (waypoints such as "reach resistance, break it, follow
//                  through") plus random noise around it.
//   volume         doesn't choose a direction. It scales how far a move goes: the size of moves and of the
//                  noise grows with the square root of relative volume, and the surge fades (half-life 2 days).
//   volatility     implied volatility rises when price falls (3% per 1% drop), eases when it rises (1.5% per
//                  1% gain), and gets a small lift from a volume surge.
// A simulation shows what would follow from those assumptions. It is not a forecast.

export const RATE = 0.04;
export const STEPS_PER_DAY = 8;

export function normCdf(x) {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const d = 0.3989422804014327 * Math.exp(-x * x / 2);
  const p = d * t * (0.31938153 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  return x >= 0 ? 1 - p : p;
}

// Value of one share's worth of a call or put. T in years; at or past expiration it's the intrinsic value.
export function bsPrice(S, K, T, iv, isCall, r = RATE) {
  const intrinsic = Math.max(isCall ? S - K : K - S, 0);
  if (!(T > 0) || !(iv > 0) || !(S > 0)) return intrinsic;
  const sq = iv * Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r + iv * iv / 2) * T) / sq;
  const d2 = d1 - sq;
  const disc = K * Math.exp(-r * T);
  return isCall ? S * normCdf(d1) - disc * normCdf(d2) : disc * normCdf(-d2) - S * normCdf(-d1);
}

// The volatility at which the model gives `price` (bisection). null when no volatility can.
export function impliedVol(price, S, K, T, isCall) {
  if (!(price > 0) || !(T > 0)) return null;
  let lo = 0.005, hi = 6;
  if (price <= bsPrice(S, K, T, lo, isCall) || price >= bsPrice(S, K, T, hi, isCall)) return null;
  for (let i = 0; i < 60; i++) {
    const mid = (lo + hi) / 2;
    if (bsPrice(S, K, T, mid, isCall) > price) hi = mid; else lo = mid;
  }
  return (lo + hi) / 2;
}

// A contract ready to simulate: {strike, isCall, dte, iv, entry, qty}. iv comes from the market price when it
// can be solved, else from the chain's own figure.
export function makeContract(row, isCall, dte, spot, qty = 1) {
  const mark = row.mid ?? row.ask;
  const solved = impliedVol(mark, spot, row.strike, Math.max(dte, 0.5) / 365, isCall);
  return { occ: row.occ, strike: row.strike, isCall, dte, iv: solved ?? row.iv ?? 0.3, entry: row.ask > 0 ? row.ask : mark, qty };
}

// Value per share after `day` days at price S, with implied volatility multiplied by ivMult.
export function valueAt(c, S, day, ivMult = 1) {
  return bsPrice(S, c.strike, Math.max(c.dte - day, 0) / 365, c.iv * ivMult, c.isCall);
}

export const pnlAt = (c, S, day, ivMult = 1) => (valueAt(c, S, day, ivMult) - c.entry) * 100 * c.qty;

// The stock price at expiration where the trade breaks even.
export const breakeven = (c) => (c.isCall ? c.strike + c.entry : c.strike - c.entry);

export const SCENARIOS = [
  { id: "breakout", name: "RESISTANCE BREAKS", dir: 1, blurb: "Climbs to resistance, breaks through, follows through." },
  { id: "reject", name: "REJECTED AT RESISTANCE", dir: -1, blurb: "Climbs to resistance, fails, rolls over." },
  { id: "breakdown", name: "SUPPORT BREAKS", dir: -1, blurb: "Slides to support, breaks it, keeps falling." },
  { id: "bounce", name: "BOUNCE OFF SUPPORT", dir: 1, blurb: "Slides to support, holds, recovers." },
  { id: "surge_up", name: "VOLUME SPIKE ▲", dir: 1, blurb: "Buyers pile in now. Fast move up, then it slows." },
  { id: "surge_down", name: "VOLUME SPIKE ▼", dir: -1, blurb: "Sellers pile in now. Fast move down, then it slows." },
  { id: "chop", name: "CHOP", dir: 0, blurb: "Goes nowhere. Time decay does the damage." },
  { id: "crush", name: "IV CRUSH", dir: 0, blurb: "Price holds, implied volatility drops 35% in a day (after earnings)." },
];

// Relative volume `day` days after a surge of size V: back to normal with a 2-day half-life.
export const volumeAt = (V, day) => 1 + (V - 1) * Math.pow(0.5, day / 2);

const ease = (x) => x * x * (3 - 2 * x);

function through(points, day) {
  if (day <= points[0][0]) return points[0][1];
  for (let i = 1; i < points.length; i++) {
    const [d0, p0] = points[i - 1], [d1, p1] = points[i];
    if (day <= d1) return p0 + (p1 - p0) * ease((day - d0) / Math.max(d1 - d0, 1e-9));
  }
  return points[points.length - 1][1];
}

// The scripted story of a scenario: [day, price] waypoints.
// ctx: {price, resistance, support, sigmaDay (daily move as a fraction), volume (relative, 1 = normal), days}
export function waypoints(id, ctx) {
  const S = ctx.price, H = ctx.days, a = S * ctx.sigmaDay, m = Math.sqrt(ctx.volume);
  const R = ctx.resistance && ctx.resistance > S ? ctx.resistance : S + a;
  const F = ctx.support && ctx.support < S ? ctx.support : S - a;
  const run = (frac) => 0.5 * a * m * Math.sqrt(frac * H);          // follow-through grows with the square root of time
  const k = Math.min(1, Math.sqrt(H));                              // under a day, the first jump shrinks the same way
  const pop = 0.4 * a * m * k, burst = a * m * k;
  switch (id) {
    case "breakout": return [[0, S], [0.25 * H, R], [0.4 * H, R + pop], [H, R + pop + run(0.6)]];
    case "reject": return [[0, S], [0.3 * H, R - 0.05 * a], [H, R - 0.05 * a - run(0.7)]];
    case "breakdown": return [[0, S], [0.25 * H, F], [0.4 * H, F - pop], [H, F - pop - run(0.6)]];
    case "bounce": return [[0, S], [0.3 * H, F + 0.05 * a], [H, F + 0.05 * a + run(0.7)]];
    case "surge_up": return [[0, S], [0.15 * H, S + burst], [H, S + burst + run(0.85)]];
    case "surge_down": return [[0, S], [0.15 * H, S - burst], [H, S - burst - run(0.85)]];
    default: return [[0, S], [H, S]];
  }
}

// What the scenario itself does to implied volatility by `day` (on top of the price and volume rules).
export function scenarioIv(id, day) {
  if (id === "crush") return 1 - 0.35 * Math.min(day, 1);
  if (id === "chop") return Math.max(1 - 0.01 * day, 0.85);
  return 1;
}

// Implied-volatility multiplier from the rules at the top of this file.
export function ivRule(ret, relVolume, scenarioMult = 1) {
  const fromPrice = 1 + (ret < 0 ? -3 : -1.5) * ret;
  const fromVolume = 1 + 0.1 * (Math.sqrt(Math.max(relVolume, 0.01)) - 1);
  return Math.min(Math.max(fromPrice * fromVolume * scenarioMult, 0.4), 3);
}

export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function gaussian(rng) {
  let u = 0;
  while (u === 0) u = rng();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rng());
}

// One possible future: arrays over time steps. rng = null gives the story with no noise.
// ctx.spd = steps per day: 8 by default; an intraday chart passes its bars per session (78 for 5-minute bars),
// with ctx.days a fraction of a day, so each step is one bar.
export function simulatePath(id, ctx, rng = null) {
  const spd = ctx.spd || STEPS_PER_DAY;
  const n = Math.max(1, Math.round(ctx.days * spd)), wp = waypoints(id, ctx);
  const noiseScale = id === "chop" ? 0.6 : 0.8;                   // the story leads; the noise is most of a normal day
  const price = new Array(n + 1), ivMult = new Array(n + 1), day = new Array(n + 1);
  let drift = 0;
  for (let i = 0; i <= n; i++) {
    const d = i / spd, v = volumeAt(ctx.volume, d);
    if (rng && i > 0) drift += ctx.sigmaDay * Math.sqrt(v / spd) * noiseScale * gaussian(rng);
    day[i] = d;
    price[i] = Math.max(through(wp, d) * Math.exp(drift), 0.01);
    ivMult[i] = ivRule(price[i] / ctx.price - 1, v, scenarioIv(id, d));
  }
  return { day, price, ivMult };
}

const pct = (sorted, q) => sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))))];

// Many futures under one scenario: the price band at each step and the spread of results at the end.
export function monteCarlo(id, ctx, contract, runs = 300, seed = 1) {
  const rng = mulberry32(seed), n = Math.max(1, Math.round(ctx.days * (ctx.spd || STEPS_PER_DAY)));
  const cols = Array.from({ length: n + 1 }, () => new Float64Array(runs));
  const ends = new Float64Array(runs);
  for (let r = 0; r < runs; r++) {
    const p = simulatePath(id, ctx, rng);
    for (let i = 0; i <= n; i++) cols[i][r] = p.price[i];
    ends[r] = contract ? pnlAt(contract, p.price[n], ctx.days, p.ivMult[n]) : 0;
  }
  const lo = new Array(n + 1), hi = new Array(n + 1);
  for (let i = 0; i <= n; i++) {
    const s = cols[i].slice().sort();
    lo[i] = pct(s, 0.1);
    hi[i] = pct(s, 0.9);
  }
  const e = ends.slice().sort();
  let wins = 0;
  for (const x of e) if (x > 0) wins++;
  return { lo, hi, chance: wins / runs, p10: pct(e, 0.1), p50: pct(e, 0.5), p90: pct(e, 0.9), runs };
}
