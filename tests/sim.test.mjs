import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/optlab/web/sim.js";

const near = (a, b, tol) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);

test("Black-Scholes matches a textbook value and put-call parity", () => {
  near(S.bsPrice(100, 100, 1, 0.2, true, 0.05), 10.4506, 0.002);
  near(S.bsPrice(100, 100, 1, 0.2, false, 0.05), 5.5735, 0.002);
  const c = S.bsPrice(278, 280, 30 / 365, 0.22, true), p = S.bsPrice(278, 280, 30 / 365, 0.22, false);
  near(c - p, 278 - 280 * Math.exp(-S.RATE * 30 / 365), 0.002);
  assert.equal(S.bsPrice(105, 100, 0, 0.2, true), 5);                 // at expiration: intrinsic value
  assert.equal(S.bsPrice(105, 100, 0, 0.2, false), 0);
});

test("implied volatility recovers the volatility that made the price", () => {
  for (const iv of [0.12, 0.35, 0.9]) near(S.impliedVol(S.bsPrice(50, 52, 0.1, iv, true), 50, 52, 0.1, true), iv, 1e-4);
  assert.equal(S.impliedVol(1, 60, 50, 0.1, true), null);              // below intrinsic value: no volatility fits
});

test("a contract starts at the market: zero levers means value = mid, loss = the spread", () => {
  const row = { occ: "X", strike: 280, bid: 2.9, ask: 3.0, mid: 2.95, iv: 0.19 };
  const c = S.makeContract(row, true, 21, 278.8, 2);
  near(S.valueAt(c, 278.8, 0, 1), 2.95, 0.001);
  near(S.pnlAt(c, 278.8, 0, 1), (2.95 - 3.0) * 100 * 2, 0.2);
  assert.equal(S.breakeven(c), 283);
  assert.ok(S.pnlAt(c, 290, 0) > 0 && S.pnlAt(c, 270, 0) < 0);       // a call gains when the price rises
  assert.ok(S.pnlAt(c, 278.8, 10) < S.pnlAt(c, 278.8, 0));           // time passing costs money
  assert.ok(S.pnlAt(c, 278.8, 0, 0.65) < S.pnlAt(c, 278.8, 0, 1));   // so does falling implied volatility
  near(S.pnlAt(c, 200, 21), -3.0 * 100 * 2, 1e-9);                   // the most it can lose is what it cost
});

const ctx = { price: 100, resistance: 104, support: 97, sigmaDay: 0.012, volume: 1, days: 10 };

test("each scenario's story goes where its name says", () => {
  const end = (id, c = ctx) => S.simulatePath(id, c).price.at(-1);
  const top = (id) => Math.max(...S.simulatePath(id, ctx).price);
  assert.ok(end("breakout") > 104 && end("surge_up") > 100 && end("bounce") > 97);
  assert.ok(end("breakdown") < 97 && end("surge_down") < 100);
  assert.ok(top("reject") < 104 && top("reject") > 103.5 && end("reject") < top("reject"));
  assert.equal(end("chop"), 100);
  assert.equal(end("crush"), 100);
  near(S.simulatePath("crush", ctx).ivMult.at(-1), 0.65, 1e-9);
  // no resistance overhead (at new highs): the breakout is from one daily move up
  assert.ok(end("breakout", { ...ctx, resistance: null }) > 101.2);
});

test("volume scales the size of the move, not its direction", () => {
  const end = (id, volume) => S.simulatePath(id, { ...ctx, volume }).price.at(-1);
  assert.ok(end("breakout", 4) > end("breakout", 1) && end("breakdown", 4) < end("breakdown", 1));
  near((end("surge_up", 4) - 100) / (end("surge_up", 1) - 100), 2, 1e-6);   // 4x volume: sqrt(4) = 2x the move
  assert.equal(S.volumeAt(3, 0), 3);
  near(S.volumeAt(3, 2), 2, 1e-12);                                          // the surge halves every 2 days
  assert.equal(S.volumeAt(1, 5), 1);
});

test("implied volatility rises when price falls and eases when it rises", () => {
  near(S.ivRule(-0.05, 1), 1.15, 1e-12);
  near(S.ivRule(0.05, 1), 0.925, 1e-12);
  assert.ok(S.ivRule(0, 4) > 1 && S.ivRule(-0.9, 1) <= 3 && S.ivRule(0.9, 1) >= 0.4);
});

test("Monte Carlo is repeatable, brackets the story, and agrees with the direction", () => {
  const call = S.makeContract({ occ: "C", strike: 102, bid: 0.9, ask: 1.0, mid: 0.95, iv: 0.2 }, true, 30, 100);
  const put = S.makeContract({ occ: "P", strike: 98, bid: 0.9, ask: 1.0, mid: 0.95, iv: 0.2 }, false, 30, 100);
  const a = S.monteCarlo("breakout", ctx, call, 300, 7), b = S.monteCarlo("breakout", ctx, call, 300, 7);
  assert.deepEqual([a.chance, a.p10, a.p50, a.p90], [b.chance, b.p10, b.p50, b.p90]);
  const story = S.simulatePath("breakout", ctx).price;
  for (let i = 8; i < story.length; i += 8) assert.ok(a.lo[i] < story[i] && story[i] < a.hi[i]);
  assert.ok(a.p10 <= a.p50 && a.p50 <= a.p90 && a.p10 >= -100.0001);
  assert.ok(a.chance > 0.7 && S.monteCarlo("breakout", ctx, put, 300, 7).chance < 0.3);
  assert.ok(S.monteCarlo("chop", ctx, call, 300, 7).p50 < 0);          // going nowhere loses to time decay
  const wide = S.monteCarlo("breakout", { ...ctx, volume: 4 }, call, 300, 7);
  assert.ok(wide.hi.at(-1) - wide.lo.at(-1) > a.hi.at(-1) - a.lo.at(-1));
});
