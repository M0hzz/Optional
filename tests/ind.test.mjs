import test from "node:test";
import assert from "node:assert/strict";
import * as I from "../src/optlab/web/ind.js";

const near = (a, b, tol) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);
const ramp = (n, start = 100, step = 1) => Array.from({ length: n }, (_, i) => start + i * step);

test("moving averages", () => {
  assert.deepEqual(I.sma([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  const e = I.ema([1, 2, 3, 4, 5], 3);
  assert.deepEqual(e.slice(0, 2), [null, null]);
  assert.equal(e[2], 2);                       // seeded with the average of the first 3
  assert.equal(e[3], 4 * 0.5 + 2 * 0.5);       // k = 2 / (3 + 1)
  assert.equal(I.ema([null, null, 1, 2, 3, 4], 3)[4], 2);   // a series with leading gaps (MACD's signal line)
});

test("RSI follows Wilder's definition", () => {
  assert.equal(I.rsi(ramp(30)).at(-1), 100);              // only gains
  assert.equal(I.rsi(ramp(30, 100, -1)).at(-1), 0);       // only losses
  const zig = Array.from({ length: 40 }, (_, i) => 100 + (i % 2));
  near(I.rsi(zig).at(-1), 50, 4);                          // equal ups and downs
  assert.equal(I.rsi(ramp(10))[9], null);
  // the classic Wilder example (14-period): first value 70.53
  const w = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28];
  near(I.rsi(w)[14], 70.53, 0.1);                         // (the published table rounds its averages)
});

test("MACD and Bollinger", () => {
  const m = I.macd(ramp(60));
  assert.ok(m.line.at(-1) > 0 && m.hist.at(-1) != null);
  assert.equal(m.line[24], null);
  near(m.hist.at(-1), 0, 1e-9);                           // a steady ramp: MACD settles on its signal line
  const b = I.bollinger(Array(30).fill(10));
  assert.equal(b.up.at(-1), 10);
  assert.equal(b.lo.at(-1), 10);
  const x = [...Array(19).fill(10), 12], bb = I.bollinger(x);
  near(bb.mid[19], 10.1, 1e-9);
  near(bb.up[19] - bb.mid[19], 2 * Math.sqrt((19 * 0.01 + 1.9 ** 2) / 20), 1e-9);
});

test("readout: a long climb is an uptrend, a long slide a downtrend", () => {
  const accel = Array.from({ length: 260 }, (_, i) => 50 + 0.002 * i * i);       // climbing faster and faster
  const upr = I.readout(accel);
  assert.equal(upr.verdict.value, "UPTREND");
  assert.equal(upr.verdict.text, "4 of 4 trend signals up");
  assert.ok(upr.short.tone === "up" && upr.long.tone === "up" && upr.momentum.tone === "up" && upr.rsi.tone === "hot");
  assert.ok(upr.short.text.startsWith("for "));                                  // never crossed down
  const steady = I.readout(ramp(260, 50, 0.5));                                  // a steady climb: trend up, no momentum
  assert.equal(steady.momentum.tone, "flat");
  assert.equal(steady.verdict.value, "UPTREND");
  const dn = I.readout(accel.map((v) => 600 - v));
  assert.equal(dn.verdict.value, "DOWNTREND");
  assert.equal(dn.rsi.tone, "cold");
  const short = I.readout(accel.slice(0, 80));            // under 200 days: no long-term light, 3 votes
  assert.equal(short.long, undefined);
  assert.equal(short.verdict.text, "3 of 3 trend signals up");
  const flat = I.readout(Array.from({ length: 120 }, (_, i) => 100 + Math.sin(i / 3)));
  assert.equal(flat.bands.text.includes("band"), true);
});

test("events fire where the cross happens, and only from `from` on", () => {
  const x = [...ramp(60, 120, -0.5), ...ramp(30, 90.5, 1)];  // falls, then climbs
  const ev = I.events(x, 60, { resistance: 100, support: 92 });
  const at = (label) => ev.find((e) => e.label === label);
  assert.ok(at("BREAKS RESISTANCE") && at("EMA 9 CROSSES ABOVE 21") && at("RECLAIMS SMA 50"));
  const v = [...ramp(60, 120, -0.5), ...Array.from({ length: 30 }, (_, i) => 90.5 + 0.08 * i * i)];
  assert.ok(I.events(v, 60).some((e) => e.label === "MACD TURNS UP"));
  const br = at("BREAKS RESISTANCE");
  assert.ok(x[br.i - 1] <= 100 && x[br.i] > 100 && br.dir === 1);
  assert.ok(ev.every((e) => e.i >= 60));
  assert.ok(I.events(x, 0, { support: 92 }).some((e) => e.label === "BREAKS SUPPORT"));
});
