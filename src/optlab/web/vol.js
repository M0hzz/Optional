// Relative volume: how today's volume compares with what's normal BY THIS TIME OF DAY.
//
// Volume is heavy at the open and the close and light at midday, so a stock that has traded 30% of its daily average
// by 10:00 is running normally, not quietly. The yardstick is the 5-minute bars of the previous sessions (up to 10):
// the median volume traded by each time of day. Pure functions, tested with node --test; createVolume keeps
// the 5-minute bars each ticker needs, shared by every page.
//
//   pace     today's volume so far / the normal volume by now                  (1.0 = a normal day so far)
//   recent   the last 15 minutes / normal for those 15 minutes, and which way the price went meanwhile
//   barScore the last bar on any timeframe against normal for that bar, signed by the bar's direction (the board)

export const OPEN = 570, CLOSE = 960, SLOTS = 78;                 // 09:30, 16:00, 5-minute slots in a session
const minuteOf = (d) => +d.slice(11, 13) * 60 + +d.slice(14, 16);
const median = (a) => { const b = [...a].sort((x, y) => x - y), h = b.length >> 1; return b.length % 2 ? b[h] : (b[h - 1] + b[h]) / 2; };
const avg = (a) => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : null);

// The normal session, from 5-minute bars: slot[k] = average volume of the slot starting 09:30 + 5k, cum[k] =
// average volume traded by the end of slot k-1 (cum[0] = 0, cum[78] = a whole day). Only full previous sessions count.
export function profile(bars5m, today) {
  const days = new Map();
  for (const b of bars5m) {
    const day = b.d.slice(0, 10), m = minuteOf(b.d);
    if (day >= today || m < OPEN || m >= CLOSE) continue;
    if (!days.has(day)) days.set(day, new Array(SLOTS).fill(0));
    days.get(day)[(m - OPEN) / 5 | 0] += b.v;
  }
  const full = [...days.values()].filter((s) => s.filter((v) => v > 0).length >= 60).slice(-10);   // no half days
  if (full.length < 3) return null;
  // the median for each slot: one news day (3x normal) shouldn't make every ordinary day look quiet
  const slot = Array.from({ length: SLOTS }, (_, k) => median(full.map((s) => s[k])));
  const cum = [0];
  for (let k = 0; k < SLOTS; k++) cum.push(cum[k] + slot[k]);
  return { slot, cum, day: cum[SLOTS], days: full.length };
}

// Normal volume traded by a minute of the session (straight line within a 5-minute slot).
export function normalBy(p, minute) {
  if (minute <= OPEN) return 0;
  if (minute >= CLOSE) return p.day;
  const x = (minute - OPEN) / 5, k = Math.floor(x);
  return p.cum[k] + (p.cum[k + 1] - p.cum[k]) * (x - k);
}

// The share of a normal day's volume done by a minute: the clock for daily and weekly volume.
export const dayShare = (p, minute) => (p ? normalBy(p, minute) / p.day : Math.min(1, Math.max(0, (minute - OPEN) / (CLOSE - OPEN))));

// Today so far vs normal by now, both counted from 5-minute bars (they leave out the closing auction and some
// prints, ~15% of the day, so the quote's day volume would overstate every stock). Before the open, or on a weekend,
// the last session as a whole is compared with a whole normal day.
export function pace(bars5m, clock) {
  if (!bars5m?.length) return null;
  const live = clock.weekday && clock.minute >= OPEN && clock.minute < CLOSE;
  const day = live ? clock.day : bars5m.at(-1).d.slice(0, 10);
  const p = profile(bars5m, day);
  if (!p) return null;
  const todays = bars5m.filter((b) => b.d.slice(0, 10) === day && minuteOf(b.d) >= OPEN && minuteOf(b.d) < CLOSE);
  if (live && clock.minute < OPEN + 5) return { rvol: null, label: "opening", volume: 0, normal: 0, p };
  if (!todays.length) return null;
  const volume = todays.reduce((t, b) => t + b.v, 0);
  const upTo = live ? Math.min(clock.minute, minuteOf(todays.at(-1).d) + 5) : Math.max(minuteOf(todays.at(-1).d) + 5, OPEN + 5);
  const normal = normalBy(p, upTo);
  const label = live ? "so far today" : day === clock.day ? "today" : "last session";
  return { rvol: normal > 0 ? volume / normal : null, volume, normal, label, p };
}

// The last 15 finished minutes of today vs normal for those slots, and the price move over them.
export function recent(p, bars5m, clock, n = 3) {
  if (!p || !clock.weekday || clock.minute < OPEN + 5 * n) return null;
  const done = bars5m.filter((b) => b.d.slice(0, 10) === clock.day && minuteOf(b.d) + 5 <= clock.minute && minuteOf(b.d) >= OPEN);
  const last = done.slice(-n);
  if (last.length < n) return null;
  const normal = last.reduce((s, b) => s + p.slot[(minuteOf(b.d) - OPEN) / 5 | 0], 0);
  if (!(normal > 0)) return null;
  return { rvol: last.reduce((s, b) => s + b.v, 0) / normal, move: 100 * (last.at(-1).c / last[0].o - 1),
           from: last[0].d.slice(11), to: last.at(-1).d.slice(11) };
}

// A plain-words label for a relative volume.
export function word(rvol) {
  if (rvol == null) return { word: "—", cls: "" };
  if (rvol >= 2) return { word: "HEAVY", cls: "heavy" };
  if (rvol >= 1.3) return { word: "BUSY", cls: "busy" };
  if (rvol >= 0.7) return { word: "NORMAL", cls: "normal" };
  return { word: "QUIET", cls: "quiet" };
}

// The signal board's VOL square: the last bar's volume against normal for that bar, coloured by which way the
// bar went (heavy buying green, heavy selling red, ordinary volume white). Intraday bars are compared with the same
// time slot on earlier days; a daily or weekly bar still forming is compared with the normal volume by now.
const TF_MIN = { "5m": 5, "15m": 15, "1h": 60 };
export function barScore(bars, tf, clock, p) {
  if (!bars || bars.length < 22) return null;
  let i = bars.length - 1, normal;
  if (TF_MIN[tf]) {
    const end = (b) => minuteOf(b.d) + TF_MIN[tf];
    if (bars[i].d.slice(0, 10) === clock.day && clock.weekday && end(bars[i]) > clock.minute && clock.minute < CLOSE) i--;  // still forming
    const b = bars[i], hm = b.d.slice(11), day = b.d.slice(0, 10);
    normal = median(bars.filter((x) => x.d.slice(11) === hm && x.d.slice(0, 10) < day).slice(-20).map((x) => x.v));
  } else {
    const prior = bars.slice(Math.max(0, i - 20), i).map((x) => x.v);
    normal = avg(prior);
    const live = clock.weekday && clock.minute >= OPEN && clock.minute < CLOSE;
    if (tf === "1d" && bars[i].d === clock.day && live) normal *= dayShare(p, clock.minute);
    if (tf === "1w" && live && weekOf(bars[i].d) === weekOf(clock.day)) {
      const wd = new Date(clock.day + "T12:00:00Z").getUTCDay();               // 1 = Monday (holidays ignored)
      normal *=Math.min(1, (wd - 1 + dayShare(p, clock.minute)) / 5);
    }
  }
  const b = bars[i], prev = bars[i - 1];
  if (!(normal > 0) || !(b.v > 0)) return null;
  const rvol = b.v / normal;
  const ref = TF_MIN[tf] ? b.o : prev.c;
  const dir = b.c > ref ? 1 : b.c < ref ? -1 : 0;
  const s = dir * Math.max(0, Math.min(1, (rvol - 1) / 1.5));
  const w = word(rvol).word.toLowerCase();
  const forming = !TF_MIN[tf] && i === bars.length - 1 && (tf === "1d" ? b.d === clock.day : weekOf(b.d) === weekOf(clock.day));
  return { s, rvol, text: `${rvol.toFixed(1)}× normal volume (${w}) on a ${dir > 0 ? "rising" : dir < 0 ? "falling" : "flat"} ${forming ? "bar so far" : "bar"}` };
}

function weekOf(iso) {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
}

// Keeps each ticker's 5-minute bars (refreshed every minute; the server caches them too) and its normal profile.
export function createVolume(api) {
  const store = new Map();                      // sym -> {bars, at, busy}
  async function refresh(syms) {
    const now = Date.now();
    await Promise.all(syms.map(async (s) => {
      const d = store.get(s) || {};
      if (d.busy || (d.at && now - d.at < 60000)) return;
      d.busy = true; store.set(s, d);
      try {
        const r = await api(`/api/bars/${encodeURIComponent(s)}?tf=5m`);
        d.bars = r.bars;
        d.at = Date.now();
      } catch { d.at = Date.now(); } finally { d.busy = false; }
    }));
  }
  // {pace, recent, profile} for a ticker, or null until its bars arrive
  const read = (sym, clock) => {
    const d = store.get(sym);
    if (!d?.bars) return null;
    const pc = pace(d.bars, clock);
    if (!pc) return null;
    return { pace: pc, recent: recent(pc.p, d.bars, clock), profile: pc.p };
  };
  return { refresh, read, profile: (s, clock) => read(s, clock)?.profile ?? null };
}

const big = (v) => (v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "K" : String(Math.round(v)));

// What every page shows for a ticker: the multiple, its word and colour class, the last 15 minutes, and a tooltip.
export function describe(r) {
  const pc = r?.pace;
  if (!pc) return null;
  const w = word(pc.rvol), rc = r.recent;
  const recentText = rc ? `15m ${rc.rvol.toFixed(1)}× ${rc.move > 0.02 ? "▲" : rc.move < -0.02 ? "▼" : "•"}` : "";
  const title = pc.rvol == null ? "The first 5 minutes: too early to compare volume."
    : `${big(pc.volume)} traded ${pc.label} vs ${big(pc.normal)} normal by this time of day (the median of the last ${pc.p.days} sessions, `
      + `from 5-minute bars): ${pc.rvol.toFixed(2)}× normal, ${w.word.toLowerCase()}.`
      + (rc ? ` Last 15 minutes (${rc.from}–${rc.to}): ${rc.rvol.toFixed(1)}× normal for that time, price ${rc.move >= 0 ? "+" : "−"}${Math.abs(rc.move).toFixed(2)}%.` : "");
  return { rvol: pc.rvol, text: pc.rvol == null ? "VOL —" : `VOL ${pc.rvol.toFixed(1)}×`, word: w.word, cls: w.cls, recentText,
           recentHot: !!rc && rc.rvol >= 2, fill: pc.rvol == null ? 0 : Math.min(1, pc.rvol / 3), title };
}
