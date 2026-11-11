// Trend indicators on daily closes. Pure functions: no page, no network. Tested with node --test.
//
// The set is the handful most traders use to read a trend, each with its usual settings:
//   EMA 9 / EMA 21   short-term trend: fast average above slow = up. A cross is a change of trend.
//   SMA 50           medium-term trend: price above it = up.
//   SMA 200          long-term trend: price above it = up (needs 200 days of history).
//   MACD 12/26/9     momentum: histogram above 0 = up, and growing = strengthening.
//   RSI 14           stretch: above 70 = overbought (ran hard), below 30 = oversold (fell hard).
//   Bollinger 20/2   range: where the price sits in its normal band, and a squeeze when the band is tight.
// Values are null until there is enough history for them.

export function sma(x, n) {
  const out = new Array(x.length).fill(null);
  let sum = 0;
  for (let i = 0; i < x.length; i++) {
    sum += x[i];
    if (i >= n) sum -= x[i - n];
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

// Seeded with the simple average of the first n values, then exponential with k = 2 / (n + 1).
export function ema(x, n) {
  const out = new Array(x.length).fill(null), k = 2 / (n + 1);
  let prev = null;
  for (let i = 0; i < x.length; i++) {
    if (x[i] == null) continue;
    if (prev == null) {
      if (i >= n - 1 && x.slice(i - n + 1, i + 1).every((v) => v != null)) {
        prev = x.slice(i - n + 1, i + 1).reduce((a, b) => a + b, 0) / n;
        out[i] = prev;
      }
      continue;
    }
    prev = x[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

// Wilder's RSI: average gain and loss over n changes, smoothed by (n - 1) / n.
export function rsi(x, n = 14) {
  const out = new Array(x.length).fill(null);
  if (x.length <= n) return out;
  let gain = 0, loss = 0;
  for (let i = 1; i <= n; i++) {
    const d = x[i] - x[i - 1];
    if (d > 0) gain += d; else loss -= d;
  }
  gain /= n; loss /= n;
  const val = () => (loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss));
  out[n] = val();
  for (let i = n + 1; i < x.length; i++) {
    const d = x[i] - x[i - 1];
    gain = (gain * (n - 1) + Math.max(d, 0)) / n;
    loss = (loss * (n - 1) + Math.max(-d, 0)) / n;
    out[i] = val();
  }
  return out;
}

export function macd(x, fast = 12, slow = 26, signal = 9) {
  const f = ema(x, fast), s = ema(x, slow);
  const line = x.map((_, i) => (f[i] == null || s[i] == null ? null : f[i] - s[i]));
  const sig = ema(line, signal);
  return { line, signal: sig, hist: line.map((v, i) => (v == null || sig[i] == null ? null : v - sig[i])) };
}

export function bollinger(x, n = 20, k = 2) {
  const mid = sma(x, n), up = new Array(x.length).fill(null), lo = new Array(x.length).fill(null);
  for (let i = n - 1; i < x.length; i++) {
    let v = 0;
    for (let j = i - n + 1; j <= i; j++) v += (x[j] - mid[i]) ** 2;
    const sd = Math.sqrt(v / n);
    up[i] = mid[i] + k * sd;
    lo[i] = mid[i] - k * sd;
  }
  return { mid, up, lo };
}

export function compute(closes) {
  return { ema9: ema(closes, 9), ema21: ema(closes, 21), sma50: sma(closes, 50), sma200: sma(closes, 200),
           rsi: rsi(closes), macd: macd(closes), bb: bollinger(closes) };
}

const last = (a) => a[a.length - 1];

// The readout at the last close: one light per indicator ("up", "down" or "flat"), each with its value and a
// plain-language line, and a verdict from the four trend lights (short, medium, long, momentum).
export function readout(closes, ind = compute(closes)) {
  const i = closes.length - 1, c = closes[i], out = {};
  const e9 = ind.ema9[i], e21 = ind.ema21[i];
  if (e9 != null && e21 != null) {
    let since = 0, flipped = false;
    for (let j = i - 1; j >= 0 && ind.ema9[j] != null && ind.ema21[j] != null; j--, since++) {
      if ((ind.ema9[j] > ind.ema21[j]) !== (e9 > e21)) { flipped = true; break; }
    }
    out.short = { tone: e9 > e21 ? "up" : "down", value: `${e9 > e21 ? "9 > 21" : "9 < 21"}`,
                  text: flipped ? `crossed ${since === 0 ? "today" : since + "d ago"}` : `for ${since}+ days` };
  }
  const s50 = ind.sma50[i];
  if (s50 != null) out.medium = { tone: c > s50 ? "up" : "down", value: s50.toFixed(2), text: `price ${pctOff(c, s50)} ${c > s50 ? "above" : "below"}` };
  const s200 = ind.sma200[i];
  if (s200 != null) out.long = { tone: c > s200 ? "up" : "down", value: s200.toFixed(2), text: `price ${pctOff(c, s200)} ${c > s200 ? "above" : "below"}` };
  const h = ind.macd.hist[i], hp = ind.macd.hist[i - 1];
  if (h != null && hp != null) {
    const dead = Math.abs(h) < c * 1e-4;             // a histogram this close to 0 is no momentum either way
    out.momentum = { tone: dead ? "flat" : h > 0 ? "up" : "down", value: h.toFixed(3),
                     text: dead ? "no momentum either way" : `${h > 0 ? "up" : "down"} momentum, ${Math.abs(h) > Math.abs(hp) ? "strengthening" : "fading"}` };
  }
  const r = ind.rsi[i];
  if (r != null) out.rsi = { tone: r >= 70 ? "hot" : r <= 30 ? "cold" : "flat", value: r.toFixed(0),
                             text: r >= 70 ? "overbought: ran hard" : r <= 30 ? "oversold: fell hard" : r >= 50 ? "neutral, leaning up" : "neutral, leaning down" };
  const up = ind.bb.up[i], lo = ind.bb.lo[i], mid = ind.bb.mid[i];
  if (up != null) {
    const pb = (c - lo) / (up - lo), widths = [];
    for (let j = Math.max(19, i - 119); j <= i; j++) if (ind.bb.up[j] != null) widths.push((ind.bb.up[j] - ind.bb.lo[j]) / ind.bb.mid[j]);
    const w = (up - lo) / mid, rank = widths.filter((x) => x < w).length / widths.length;
    out.bands = { tone: pb > 1 ? "hot" : pb < 0 ? "cold" : "flat", value: `${Math.round(pb * 100)}%`,
                  text: (pb > 1 ? "above the band" : pb < 0 ? "below the band" : pb > 0.5 ? "upper half of the band" : "lower half of the band")
                        + (widths.length >= 40 && rank <= 0.2 ? " · SQUEEZE" : "") };
  }
  const votes = ["short", "medium", "long", "momentum"].filter((k) => out[k]);
  const ups = votes.filter((k) => out[k].tone === "up").length, downs = votes.filter((k) => out[k].tone === "down").length;
  out.verdict = { tone: ups >= 3 && ups > downs ? "up" : downs >= 3 && downs > ups ? "down" : "flat",
                  value: ups >= 3 && ups > downs ? "UPTREND" : downs >= 3 && downs > ups ? "DOWNTREND" : "MIXED",
                  text: `${ups} of ${votes.length} trend signals up` };
  return out;
}

const pctOff = (a, b) => `${Math.abs(100 * (a / b - 1)).toFixed(1)}%`;

// Signals that fire on each close from `from` on: trend crosses, RSI extremes, MACD flips, level breaks.
// levels: {resistance, support}. Returns [{i, dir: 1 | -1 | 0, label}].
export function events(closes, from, levels = {}, ind = compute(closes)) {
  const out = [];
  const cross = (a, b, i) => (a[i - 1] == null || b[i - 1] == null || a[i] == null || b[i] == null ? 0
    : a[i - 1] <= b[i - 1] && a[i] > b[i] ? 1 : a[i - 1] >= b[i - 1] && a[i] < b[i] ? -1 : 0);
  const flat = (v) => closes.map(() => v);
  const R = levels.resistance ? flat(levels.resistance) : null, F = levels.support ? flat(levels.support) : null;
  for (let i = Math.max(from, 1); i < closes.length; i++) {
    let d;
    if (R && (d = cross(closes, R, i))) out.push({ i, dir: d, label: d > 0 ? "BREAKS RESISTANCE" : "FALLS BACK UNDER RESISTANCE" });
    if (F && (d = cross(closes, F, i))) out.push({ i, dir: d, label: d > 0 ? "BACK ABOVE SUPPORT" : "BREAKS SUPPORT" });
    if ((d = cross(ind.ema9, ind.ema21, i))) out.push({ i, dir: d, label: d > 0 ? "EMA 9 CROSSES ABOVE 21" : "EMA 9 CROSSES BELOW 21" });
    if ((d = cross(closes, ind.sma50, i))) out.push({ i, dir: d, label: d > 0 ? "RECLAIMS SMA 50" : "LOSES SMA 50" });
    if ((d = cross(closes, ind.sma200, i))) out.push({ i, dir: d, label: d > 0 ? "RECLAIMS SMA 200" : "LOSES SMA 200" });
    const r0 = ind.rsi[i - 1], r1 = ind.rsi[i];
    if (r0 != null && r1 != null) {
      if (r0 < 70 && r1 >= 70) out.push({ i, dir: 0, label: "RSI OVERBOUGHT (70+)" });
      if (r0 > 30 && r1 <= 30) out.push({ i, dir: 0, label: "RSI OVERSOLD (30-)" });
    }
    const h0 = ind.macd.hist[i - 1], h1 = ind.macd.hist[i];
    const tol = closes[i] * 1e-4, band = (h) => (h > tol ? 1 : h < -tol ? -1 : 0);   // near 0 counts as neither
    if (h0 != null && h1 != null && band(h1) !== 0 && band(h1) !== band(h0)) out.push({ i, dir: Math.sign(h1), label: h1 > 0 ? "MACD TURNS UP" : "MACD TURNS DOWN" });
  }
  return out;
}
