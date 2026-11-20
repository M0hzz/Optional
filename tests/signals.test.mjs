import test from "node:test";
import assert from "node:assert/strict";
import { filterLog, NO_FILTER } from "../src/optlab/web/signals.js";

const now = new Date("2026-11-20T15:00:00").getTime();
const log = [
  { t: now - 60e3, sym: "SPY", tf: "5m", key: "rsi", to: 1 },
  { t: now - 2 * 3600e3, sym: "NVDA", tf: "1d", key: "vol", to: -1 },
  { t: now - 3 * 86400e3, sym: "SPY", tf: "1w", key: "trend", to: -1 },
  { t: now - 30 * 86400e3, sym: "QQQ", tf: "1d", key: "short", to: 0 },
];

test("the alert log filter: empty lists mean all, and every chosen field has to match", () => {
  assert.equal(filterLog(log, NO_FILTER, now).length, 3, "neutral turns hidden by default");
  assert.equal(filterLog(log, { ...NO_FILTER, turned: ["g", "r", "w"] }, now).length, 4);
  assert.deepEqual(filterLog(log, { ...NO_FILTER, syms: ["SPY"] }, now).map((e) => e.tf), ["5m", "1w"]);
  assert.deepEqual(filterLog(log, { ...NO_FILTER, syms: ["SPY", "NVDA"], turned: ["r"] }, now).map((e) => e.sym), ["NVDA", "SPY"]);
  assert.equal(filterLog(log, { ...NO_FILTER, tfs: ["1d"], keys: ["vol"] }, now).length, 1);
  assert.equal(filterLog(log, { ...NO_FILTER, when: "1h" }, now).length, 1);
  assert.equal(filterLog(log, { ...NO_FILTER, when: "today" }, now).length, 2);
  assert.equal(filterLog(log, { ...NO_FILTER, when: "7d" }, now).length, 3);
});
