import test from "node:test";
import assert from "node:assert/strict";
import * as T from "../src/optlab/web/tracker.js";
import { mulberry32, gaussian } from "../src/optlab/web/sim.js";

const near = (a, b, tol) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);
// 5-minute bars of a random walk, regular hours, `days` sessions; `drift` per bar in sigma units
function walk(days = 30, drift = 0, seed = 3) {
  const rng = mulberry32(seed), out = [];
  let p = 100;
  for (let d = 0; d < days; d++) {
    const day = new Date(Date.UTC(2026, 8, 1 + d)).toISOString().slice(0, 10);
    for (let k = 0; k < 78; k++) {
      const m = 570 + 5 * k, o = p;
      p *= Math.exp(0.001 * (gaussian(rng) + drift));
      out.push({ d: `${day}T${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`,
                 o, h: Math.max(o, p) * 1.0003, l: Math.min(o, p) * 0.9997, c: p, v: 1000 + 100 * ((k * 7) % 5) });
    }
  }
  return out;
}

test("history: sigma, features and outcomes, never across a session break", () => {
  const H = T.prepare(walk(20));
  const i = H.n - 100;
  assert.ok(H.sig[i] > 0.0012 && H.sig[i] < 0.0024, String(H.sig[i]));      // ~0.001 * sqrt(3)
  assert.equal(H.feats.length, H.n * 6);
  const lastOfDay = 77 + 78 * 10;
  assert.equal(H.ok[lastOfDay], 0);                                          // its next 15 minutes are tomorrow
  assert.equal(H.ok[lastOfDay - 3], 1);
  near(H.out[lastOfDay - 3], Math.log(H.c[lastOfDay] / H.c[lastOfDay - 3]) / H.sig[lastOfDay - 3], 1e-12);
});

test("thresholds use a level within 3 sigma, otherwise 1.5 sigma, never under 0.75", () => {
  const lv = { resistance: [{ price: 100.4 }, { price: 103 }], support: [{ price: 90 }] };
  const t = T.thresholds(100, 0.002, lv);                                    // 1 sigma = $0.20
  near(t.up, 2, 1e-9);
  assert.equal(t.dn, 1.5);
  assert.equal(T.thresholds(100, 0.002, { resistance: [{ price: 100.05 }] }).up, 0.75);
  assert.deepEqual([T.bucketOf(2.1, t), T.bucketOf(1, t), T.bucketOf(0, t), T.bucketOf(-1, t), T.bucketOf(-1.6, t)], [0, 1, 2, 3, 4]);
});

test("odds add to 100%, start spread out, and polarise as the cycle runs out", () => {
  const H = T.prepare(walk(30));
  const s = T.liveState(H, H.n - 40), t = T.thresholds(s.price, s.sig, null);
  const samples = T.lookalikes(H, T.stateFeature(s));
  assert.ok(samples.length > 100);
  const p0 = T.odds(samples, 0, 0, t).p;
  near(p0.reduce((a, b) => a + b), 1, 1e-9);
  assert.ok(Math.max(...p0) < 0.6, JSON.stringify(p0));                     // a random walk: no early certainty
  // the price has already run to +1.8 sigma: breakout climbs as the time left shrinks
  const series = [0, 1 / 3, 2 / 3, 0.99].map((e) => T.odds(samples, 1.8, e, t).p[0]);
  for (let k = 1; k < series.length; k++) assert.ok(series[k] > series[k - 1], JSON.stringify(series));
  assert.ok(series.at(-1) > 0.95);
  const done = T.odds(samples, -0.1, 1, t).p;                                // the cycle is over: chop, for certain
  assert.ok(done[2] > 0.99);
  assert.ok(done[0] < 1e-6 && done[4] < 1e-6);                                // the far scenarios have died
});

test("children: each layer assumes its parent happened, and adds up to 100% on its own", () => {
  const H = T.prepare(walk(30));
  const lv = { resistance: [{ price: 0 }], support: [] };
  const s = T.liveState(H, H.n - 40);
  const levels = { resistance: [{ price: s.price * 1.003 }, { price: s.price * 1.01 }], support: [{ price: s.price * 0.996 }] };
  const root = T.cycle(H, s, levels);
  const up = T.children(H, root, 0, levels), down = T.children(H, root, 4, levels);
  near(up.p.reduce((a, b) => a + b), 1, 1e-9);
  near(down.p.reduce((a, b) => a + b), 1, 1e-9);
  assert.ok(up.state.price > s.price * 1.003 && down.state.price < s.price * 0.996);
  assert.equal(up.t.res, levels.resistance[1].price);                         // above the old resistance: aims at the next
  assert.equal(up.t.sup, levels.resistance[0].price);                         // and the broken one is now support
  assert.equal(up.state.minute, s.minute + 15);
  assert.ok(up.state.e9 > s.e9 && down.state.e9 < s.e9);
  const g = T.children(H, up, 2, levels);
  near(g.p.reduce((a, b) => a + b), 1, 1e-9);
  void lv;
});

test("advance past the close starts the next session", () => {
  const H = T.prepare(walk(20));
  const s = { ...T.liveState(H, H.n - 2), minute: 955 };
  assert.equal(T.advance(s, 0.5).minute, 575);
});

test("replay: scores itself against plain average odds without peeking ahead", () => {
  const H = T.prepare(walk(25));
  const r = T.replay(H);
  assert.ok(r.n > 200, String(r.n));
  assert.ok(r.brier > 0 && r.brierBase > 0);
  assert.ok(Math.abs(r.skill) < 0.15, String(r.skill));                       // a random walk has nothing to find
  assert.ok(r.topHit10 > r.topHit);                                           // 10 minutes in it knows more
  const cal = r.calibration.filter((b) => b.n > 100);
  for (const b of cal) assert.ok(Math.abs(b.said - b.happened) < 0.15, JSON.stringify(b));
});

test("calibrator: quotes what used to happen, still adds to 100%", () => {
  const cal = T.calibrator([{ n: 1000, said: 0.07, happened: 0.06 }, { n: 1000, said: 0.68, happened: 0.76 }, { n: 1000, said: 0.86, happened: 0.93 },
                            { n: 10, said: 0.5, happened: 0.9 }]);            // too few cycles: ignored
  const p = cal([0.68, 0.2, 0.07, 0.03, 0.02]);
  near(p.reduce((a, b) => a + b), 1, 1e-12);
  assert.ok(p[0] > 0.68 && p[4] < 0.02);
  const same = T.calibrator([])([0.5, 0.3, 0.2, 0, 0]);                       // nothing learned: unchanged
  same.forEach((v, k) => near(v, [0.5, 0.3, 0.2, 0, 0][k], 1e-12));
});
