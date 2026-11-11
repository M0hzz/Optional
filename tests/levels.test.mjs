import test from "node:test";
import assert from "node:assert/strict";
import * as L from "../src/optlab/web/levels.js";

// A stock that swings between about 100 and 110 four times, then sits at 105.
function range(n = 160) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const c = 105 + 5 * Math.sin((2 * Math.PI * i) / 40);
    out.push({ d: `d${i}`, o: c, h: c + 0.3, l: c - 0.3, c, v: 1000 });
  }
  return out;
}

test("swing points are local extremes over k days each side", () => {
  const b = [1, 2, 5, 2, 1, 0, 1, 3, 1].map((c) => ({ h: c, l: c, c }));
  const s = L.swings(b, 2);
  assert.deepEqual(s.filter((p) => p.kind === "high").map((p) => p.i), [2]);
  assert.deepEqual(s.filter((p) => p.kind === "low").map((p) => p.i), [5]);
});

test("a range-bound stock gets one resistance at the tops and one support at the bottoms", () => {
  const lv = L.findLevels(range(), 160, 105);
  const r = lv.resistance[0], s = lv.support[0];
  assert.ok(Math.abs(r.price - 110.3) < 0.2 && r.touches >= 3, JSON.stringify(r));
  assert.ok(Math.abs(s.price - 99.7) < 0.2 && s.touches >= 3, JSON.stringify(s));
  assert.ok(lv.resistance.every((x) => x.price > 105) && lv.support.every((x) => x.price < 105));
});

test("levels sort nearest first, and the range sets how far back they come from", () => {
  const b = range(160);
  for (let i = 0; i < 40; i++) b.push({ d: "x", o: 120, h: 121 + (i === 20 ? 4 : 0), l: 119, c: 120, v: 1 });
  for (let i = 0; i < 30; i++) b.push({ d: "y", o: 112, h: 112.5, l: 111.5, c: 112, v: 1 });
  const long = L.findLevels(b, 230, 112);
  assert.ok(long.resistance[0].price < long.resistance.at(-1).price);
  assert.ok(long.resistance.some((x) => x.price > 124) && long.support.some((x) => x.price < 111));
  const short = L.findLevels(b, 21, 112);
  assert.ok(short.resistance.every((x) => x.price < 113) || short.resistance.length === 0);
  assert.ok(short.support.every((x) => x.price > 110));
  assert.ok(L.findLevels(b, 2000, 112).k <= 10 && L.findLevels(b, 21, 112).k === 2);
});

test("edge cases", () => {
  assert.deepEqual(L.findLevels([], 63, 100).resistance, []);
  const lv = L.findLevels(range(), 160, 200);         // price above everything: no resistance
  assert.equal(lv.resistance.length, 0);
  assert.ok(lv.support.length > 0);
  assert.equal(L.RANGES["1Y"], 252);
});
