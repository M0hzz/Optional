import test from "node:test";
import assert from "node:assert/strict";
import * as R from "../src/optlab/web/rel.js";

const near = (a, b, tol) => assert.ok(Math.abs(a - b) <= tol, `${a} is not within ${tol} of ${b}`);
// B wanders; A moves `beta` times B's daily return plus its own drift.
function pair(n = 90, beta = 2, drift = 0) {
  const a = [], b = [];
  let pa = 100, pb = 50;
  for (let i = 0; i < n; i++) {
    const rb = 0.01 * Math.sin(i * 1.7) + 0.004 * Math.cos(i * 0.9);
    pb *= Math.exp(rb);
    pa *= Math.exp(beta * rb + drift);
    a.push({ d: `2026-01-${String(i).padStart(3, "0")}`, c: pa });
    b.push({ d: `2026-01-${String(i).padStart(3, "0")}`, c: pb });
  }
  return { a, b };
}

test("align keeps only the days both traded", () => {
  const { A, B, days } = R.align([{ d: "1", c: 1 }, { d: "2", c: 2 }, { d: "3", c: 3 }], [{ d: "2", c: 20 }, { d: "3", c: 30 }, { d: "4", c: 40 }]);
  assert.deepEqual([A, B, days], [[2, 3], [20, 30], ["2", "3"]]);
});

test("correlation and beta recover how the pair was built", () => {
  const { a, b } = pair(90, 2);
  const { A, B } = R.align(a, b);
  const { corr, beta } = R.corrBeta(A, B);
  near(corr, 1, 1e-9);
  near(beta, 2, 1e-9);
  const inv = pair(90, -1);
  near(R.corrBeta(inv.a.map((x) => x.c), inv.b.map((x) => x.c)).corr, -1, 1e-9);
  assert.equal(R.corrBeta([1, 2, 3], [1, 2, 3]).corr, null);          // too little history
});

test("relation: strength, today's gap and the flags", () => {
  const { a, b } = pair(90, 1, 0.003);                                  // A drifts up 0.3% a day beyond B
  const r = R.relation({ bars: a, changePct: 3.0, verdict: "UPTREND" }, { bars: b, changePct: 0.5, verdict: "DOWNTREND" });
  assert.ok(r.s20 > 5 && r.s5 > 1 && r.lead.startsWith("leading by"));
  near(r.gap, 3.0 - r.beta * 0.5, 1e-9);
  assert.deepEqual(r.flags, ["TRENDS DISAGREE", "STRONGER THAN ITS LINK TODAY"]);
  const calm = R.relation({ bars: a, changePct: 0.5, verdict: "UPTREND" }, { bars: b, changePct: 0.5, verdict: "MIXED" });
  assert.deepEqual(calm.flags, []);
  const same = pair(90, 1, 0);
  assert.equal(R.relation({ bars: same.a }, { bars: same.b }).lead, "moving in step over 20 days");
  assert.equal(R.relation({ bars: a.slice(0, 10) }, { bars: b.slice(0, 10) }), null);
  assert.equal(R.corrWord(0.85), "very tight");
  assert.equal(R.corrWord(0.1), "unrelated");
});
