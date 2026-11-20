import test from "node:test";
import assert from "node:assert/strict";
import * as V from "../src/optlab/web/vol.js";

// A U-shaped day: heavy at the open and close, light at midday. `scale` multiplies every bar.
const pad = (n) => String(n).padStart(2, "0");
function day(date, scale = 1, upTo = 960) {
  const out = [];
  for (let m = 570; m < upTo; m += 5) {
    const k = (m - 570) / 5, u = 1 + 3 * ((k - 38.5) / 38.5) ** 2;
    out.push({ d: `${date}T${pad(m / 60 | 0)}:${pad(m % 60)}`, o: 100, h: 101, l: 99, c: 100 + (scale > 1 ? 0.5 : 0), v: Math.round(1000 * u * scale) });
  }
  return out;
}
const history = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"].flatMap((d) => day(d));
const clock = (minute, d = "2026-11-16") => ({ day: d, minute, weekday: true, open: minute >= 570 && minute < 960 });

test("the profile is the average day, and only full earlier days count", () => {
  const p = V.profile([...history, ...day("2026-11-16", 5, 600)], "2026-11-16");
  assert.equal(p.days, 5);
  assert.ok(p.slot[0] > 3 * p.slot[38], "heavier at the open than at midday");
  assert.equal(V.normalBy(p, 960), p.day);
  assert.equal(V.normalBy(p, 570), 0);
  assert.ok(Math.abs(V.normalBy(p, 572.5) - p.slot[0] / 2) < 1e-9);
  assert.equal(V.profile(day("2026-11-13"), "2026-11-16"), null, "one day isn't enough");
  assert.equal(V.profile([...history.slice(0, 200)], "2026-11-16"), null, "partial days don't count");
});

test("a normal morning reads 1x by time of day, not as a quiet day", () => {
  const bars = [...history, ...day("2026-11-16", 1, 600)];          // 09:30-10:00, normal
  const r = V.pace(bars, clock(600));
  assert.ok(Math.abs(r.rvol - 1) < 0.02, r.rvol);
  assert.equal(r.label, "so far today");
  assert.ok(r.volume / r.p.day < 0.2, "only a small share of a day by 10:00");
  const heavy = V.pace([...history, ...day("2026-11-16", 2.5, 600)], clock(600));
  assert.ok(Math.abs(heavy.rvol - 2.5) < 0.05);
  assert.equal(V.word(heavy.rvol).word, "HEAVY");
  assert.equal(V.word(0.5).word, "QUIET");
  assert.equal(V.pace(bars, clock(572)).rvol, null, "nothing to say in the first 5 minutes");
  const news = [...history, ...day("2026-11-14", 3), ...day("2026-11-16", 1, 600)];   // one 3x day in the history
  assert.ok(Math.abs(V.pace(news, clock(600)).rvol - 1) < 0.02, "one news day doesn't make a normal day look quiet");
});

test("before the open, the last session is compared with a whole day", () => {
  const bars = [...history, ...day("2026-11-16", 1.5)];
  const r = V.pace(bars, { day: "2026-11-17", minute: 500, weekday: true, open: false });
  assert.equal(r.label, "last session");
  assert.ok(Math.abs(r.rvol - 1.5) < 0.02, r.rvol);
});

test("the last 15 minutes against the same 15 minutes on other days", () => {
  const today = [...day("2026-11-16", 1, 600), ...day("2026-11-16", 4, 615).slice(6)];   // 10:00-10:15 at 4x
  const bars = [...history, ...today];
  const r = V.recent(V.profile(bars, "2026-11-16"), bars, clock(616));
  assert.equal(r.from, "10:00");
  assert.equal(r.to, "10:10");
  assert.ok(Math.abs(r.rvol - 4) < 0.05);
});

test("the board's VOL square: heavy buying green, heavy selling red, ordinary white", () => {
  const today = day("2026-11-16", 3, 610);
  const bars = [...history, ...today];
  const up = V.barScore(bars, "5m", clock(612));                  // 10:05 bar is done, 10:10 not started
  assert.ok(up.s > 0.9 && Math.abs(up.rvol - 3) < 0.05, JSON.stringify(up));
  const down = bars.map((b, i) => (i === bars.length - 1 ? { ...b, c: 99 } : b));
  assert.ok(V.barScore(down, "5m", clock(612)).s < -0.9);
  const forming = V.barScore(bars, "5m", clock(607));             // 10:05 still forming: judge 10:00
  assert.match(forming.text, /3\.0× normal/);
  const plain = V.barScore([...history, ...day("2026-11-16", 1, 610)], "5m", clock(612));
  assert.equal(plain.s, 0);
  // a daily bar still forming is held to the normal volume by now
  const daily = Array.from({ length: 25 }, (_, i) => ({ d: `2026-10-${pad(i + 1)}`, o: 100, h: 101, l: 99, c: 100, v: 1000 }));
  daily.push({ d: "2026-11-16", o: 100, h: 101, l: 99, c: 101, v: 500 });
  const half = V.barScore(daily, "1d", clock(765));              // 12:45, half the session by the clock
  assert.ok(Math.abs(half.rvol - 1) < 0.05, half.rvol);
  assert.match(half.text, /so far/);
});
