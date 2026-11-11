// How two symbols move in relation to each other, from their daily bars. Pure functions; tested with node --test.
//
// For an anchor A and a linked symbol B, over the days both traded:
//   correlation   of their daily returns over the last 60 days: +1 = move together, 0 = unrelated, -1 = opposite.
//   beta          how much A moves per 1% move in B (slope of A's daily returns on B's, 60 days).
//   strength      A's return minus B's return over 5 and 20 days, in percentage points: + means A is leading.
//   gap today     A's change today minus beta x B's change today: how far A is from where B's move "puts" it.
// and a plain-language read with a flag when they diverge.

export function align(a, b) {
  const byDay = new Map(b.map((x) => [x.d, x.c]));
  const A = [], B = [], days = [];
  for (const x of a) {
    const y = byDay.get(x.d);
    if (y != null) { A.push(x.c); B.push(y); days.push(x.d); }
  }
  return { A, B, days };
}

const rets = (x) => x.slice(1).map((v, i) => Math.log(v / x[i]));
const mean = (x) => x.reduce((s, v) => s + v, 0) / x.length;

export function corrBeta(A, B, n = 60) {
  const ra = rets(A).slice(-n), rb = rets(B).slice(-n);
  if (ra.length < 20) return { corr: null, beta: null };
  const ma = mean(ra), mb = mean(rb);
  let sab = 0, saa = 0, sbb = 0;
  for (let i = 0; i < ra.length; i++) { sab += (ra[i] - ma) * (rb[i] - mb); saa += (ra[i] - ma) ** 2; sbb += (rb[i] - mb) ** 2; }
  if (saa === 0 || sbb === 0) return { corr: null, beta: null };
  return { corr: sab / Math.sqrt(saa * sbb), beta: sab / sbb };
}

const retPct = (x, n) => (x.length > n ? 100 * (x[x.length - 1] / x[x.length - 1 - n] - 1) : null);

// a, b: {bars, changePct (today, %), verdict ("UPTREND" | "DOWNTREND" | "MIXED" | undefined)}
export function relation(a, b) {
  const { A, B } = align(a.bars, b.bars);
  if (A.length < 25) return null;
  const { corr, beta } = corrBeta(A, B);
  const s5 = retPct(A, 5) != null ? retPct(A, 5) - retPct(B, 5) : null;
  const s20 = retPct(A, 20) != null ? retPct(A, 20) - retPct(B, 20) : null;
  const gap = a.changePct != null && b.changePct != null && beta != null ? a.changePct - beta * b.changePct : null;
  const flags = [];
  if (a.verdict && b.verdict && a.verdict !== "MIXED" && b.verdict !== "MIXED" && a.verdict !== b.verdict) flags.push("TRENDS DISAGREE");
  if (gap != null && Math.abs(gap) >= 1.5) flags.push(gap > 0 ? "STRONGER THAN ITS LINK TODAY" : "WEAKER THAN ITS LINK TODAY");
  if (corr != null && corr < 0.3) flags.push("LINK IS WEAK LATELY");
  const lead = s20 == null ? "" : Math.abs(s20) < 1 ? "moving in step over 20 days" : s20 > 0 ? `leading by ${s20.toFixed(1)} pts over 20 days` : `lagging by ${Math.abs(s20).toFixed(1)} pts over 20 days`;
  return { corr, beta, s5, s20, gap, flags, lead, days: A.length };
}

export const corrWord = (c) => (c == null ? "—" : c >= 0.8 ? "very tight" : c >= 0.6 ? "tight" : c >= 0.3 ? "loose" : c > -0.3 ? "unrelated" : "opposite");
