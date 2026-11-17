import test from "node:test";
import assert from "node:assert/strict";
import * as D from "../src/optlab/web/decide.js";
import { bsPrice } from "../src/optlab/web/sim.js";

const near = (a, b, tol) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);
const R = { ...D.DEFAULT_RULES };
const open = { day: "2026-11-17", minute: 600, weekday: true, open: true };          // 10:00 New York
const quote = (S, K, dte, iv, isCall, spread = 0.06) => { const m = bsPrice(S, K, dte / 365, iv, isCall); return { bid: m - spread / 2, ask: m + spread / 2 }; };

test("net is at the bid, after fees both ways", () => {
  assert.equal(D.netNow({ entry: 2.0, bid: 2.6, qty: 1 }), 60.00000000000001);
  near(D.netNow({ entry: 2.0, bid: 2.6, qty: 2, fees: 0.65 }), 120 - 2.6, 1e-9);
});

test("New York clock", () => {
  const c = D.marketClock(new Date("2026-11-17T15:00:00Z"));                         // 10:00 EST
  assert.deepEqual([c.day, c.minute, c.open], ["2026-11-17", 600, true]);
  assert.equal(D.marketClock(new Date("2026-07-15T13:00:00Z")).minute, 540);          // 09:00 EDT
  assert.equal(D.marketClock(new Date("2026-11-21T16:00:00Z")).open, false);          // Saturday
});

test("target odds: a farther target is less likely, more time makes both more likely", () => {
  const S = 280, q = quote(S, 282, 7, 0.22, true);
  const hz = D.horizon(R, open, 7);
  near(hz.hours, 5.75, 1e-9);
  const o = D.targetOdds({ spot: S, strike: 282, isCall: true, ...q, dte: 7, iv: 0.22, rules: R, hz });
  assert.ok(o.sTarget > S && o.sStop < S && o.move > 0);
  // at sTarget the option, after a day's decay, is worth the entry + $50 + half the spread
  const m = bsPrice(o.sTarget, 282, (7 - hz.days) / 365, 0.22, true) + ((q.bid + q.ask) / 2 - bsPrice(S, 282, 7 / 365, 0.22, true));
  near((m - 0.03 - q.ask) * 100, 50, 0.01);
  assert.ok(o.pTarget > 0 && o.pTarget < 1 && o.pStop > 0 && o.pStop < 1);
  const far = D.targetOdds({ spot: S, strike: 282, isCall: true, ...q, dte: 7, iv: 0.22, rules: { ...R, target: 150 }, hz });
  assert.ok(far.pTarget < o.pTarget && far.sTarget > o.sTarget);
  const swing = D.targetOdds({ spot: S, strike: 282, isCall: true, ...q, dte: 7, iv: 0.22, rules: { ...R, mode: "swing" }, hz: D.horizon({ ...R, mode: "swing" }, open, 7) });
  assert.ok(swing.pStop > o.pStop);
  const put = D.targetOdds({ spot: S, strike: 278, isCall: false, ...quote(S, 278, 7, 0.22, false), dte: 7, iv: 0.22, rules: R, hz });
  assert.ok(put.sTarget < S && put.sStop > S);
  // a $0.05 option can't lose $40: no stop to hit
  assert.equal(D.targetOdds({ spot: S, strike: 300, isCall: true, bid: 0.04, ask: 0.05, dte: 7, iv: 0.22, rules: R, hz }).pStop, 0);
});

test("lottery tickets", () => {
  assert.equal(D.isLottery({ delta: 0.45, bid: 2.9, ask: 3.0, dte: 7 }).lottery, false);
  assert.deepEqual(D.isLottery({ delta: 0.08, bid: 0.04, ask: 0.06, dte: 0 }).why.length, 3);
  assert.equal(D.isLottery({ delta: 0.5, bid: 0, ask: 1, dte: 5 }).lottery, true);
});

test("earnings only matter while you'd hold", () => {
  const amcToday = { date: "2026-11-17", when: "amc" };
  assert.equal(D.earningsInPlan(amcToday, R, open, "2026-11-27"), null);              // a day trade is out by 15:45
  assert.match(D.earningsInPlan(amcToday, { ...R, mode: "swing" }, open, "2026-11-27").text, /today after the close/);
  assert.equal(D.earningsInPlan({ date: "2026-11-17", when: "bmo" }, { ...R, mode: "swing" }, open, "2026-11-27"), null);
  assert.equal(D.earningsInPlan({ date: "2026-11-26", when: "bmo" }, { ...R, mode: "swing" }, open, "2026-11-27"), null);  // after your exit
  assert.ok(D.earningsInPlan({ date: "2026-11-24", when: "bmo" }, { ...R, mode: "swing" }, open, "2026-11-27"));
});

test("status: one word, by the rules", () => {
  const base = { rules: R, clock: open, dte: 7, earn: null };
  assert.equal(D.status({ ...base, net: 52 }).word, "TAKE PROFIT");
  assert.equal(D.status({ ...base, net: -41 }).word, "CUT");
  const hold = D.status({ ...base, net: 12 });
  assert.equal(hold.word, "HOLD");
  assert.match(hold.text, /\$38 more to your target, \$52 of room/);
  assert.equal(D.status({ ...base, net: 5, clock: { ...open, minute: 950 } }).word, "TIME'S UP");
  const sw = { ...R, mode: "swing" };
  assert.equal(D.status({ ...base, rules: sw, net: 5, clock: { ...open, minute: 950 } }).word, "HOLD");   // a swing holds overnight
  assert.equal(D.status({ ...base, rules: sw, net: 5, dte: 2 }).word, "TIME'S UP");
  assert.equal(D.status({ ...base, rules: sw, net: 5, earn: { text: "reports today after the close" } }).word, "CUT");
  assert.equal(D.status({ ...base, rules: { ...sw, holdEarnings: true }, net: 5, earn: { text: "x" } }).word, "HOLD");
  assert.equal(D.status({ ...base, net: 60, earn: null, clock: { ...open, minute: 950 } }).word, "TAKE PROFIT");      // target first
});

test("GO only when every check passes; the first failure is the reason", () => {
  const ok = { isCall: true, cost: 150, budget: 300, odds: { pTarget: 0.4, pStop: 0.3, sTarget: 283 }, lot: { lottery: false, why: [] },
               trend: { verdict: "UPTREND", short: "up" }, resistance: 286, support: 275, earn: null, rules: R, clock: open, dte: 7 };
  assert.equal(D.goCheck(ok).go, true);
  assert.match(D.goCheck({ ...ok, resistance: 282 }).reason, /^Room to reach \+\$50: needs 283\.00, past the resistance at 282\.00/);
  assert.match(D.goCheck({ ...ok, trend: { verdict: "DOWNTREND", short: "down" } }).reason, /^Trend points up/);
  assert.equal(D.goCheck({ ...ok, isCall: false, trend: { verdict: "DOWNTREND", short: "down" }, odds: { pTarget: 0.4, pStop: 0.3, sTarget: 277 } }).go, true);
  assert.match(D.goCheck({ ...ok, odds: { pTarget: 0.25, pStop: 0.4, sTarget: 283 } }).reason, /^\+\$50 within reach/);
  assert.match(D.goCheck({ ...ok, clock: { ...open, minute: 920 } }).reason, /^Time left today/);
  assert.match(D.goCheck({ ...ok, cooldownLeft: 9 }).reason, /^Not right after a cut: 9 min/);
  assert.match(D.goCheck({ ...ok, cost: 420 }).reason, /^Fits your budget/);
  assert.match(D.goCheck({ ...ok, lot: { lottery: true, why: ["expires today"] } }).reason, /^Not a lottery ticket: expires today/);
});
