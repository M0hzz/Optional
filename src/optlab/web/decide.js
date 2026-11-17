// GO / HOLD / CUT: the deciding is done once, by rules set when you buy; the app only says which rule applies.
// Pure functions; tested with node --test.
//
// What the rules rest on (see the README for the papers):
//   * Profit is counted at the BID (what you can sell for), after fees. Spreads on cheap short-dated options average
//     ~12%, and they were most of what retail lost on same-day options.
//   * Far-out-of-the-money, near-expiry options are lottery tickets that do worse than other options: never a GO.
//   * Buying before earnings and holding through them loses 5-14% on average: never held overnight through
//     earnings unless you say so.
//   * Exits set in advance (a target and a stop) undo the habit of selling winners early and riding losers.
import { bsPrice, normCdf } from "./sim.js";

export const DEFAULT_RULES = { target: 50, stop: 40, mode: "eod", swingDays: 2, holdEarnings: false, fees: 0 };
export const OPEN_MIN = 570, CLOSE_MIN = 960, LAST_CALL = 945;     // 09:30, 16:00, and 15:45 for selling a day trade
const TRADING_YEAR_HOURS = 252 * 6.5;
export const REACH = 0.3;                                         // the least chance of the target that still counts as a GO

// New York time: {day: "YYYY-MM-DD", minute (fractional), weekday, open (regular hours, Mon-Fri)}.
export function marketClock(now = new Date()) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
                                                hour: "2-digit", minute: "2-digit", second: "2-digit", weekday: "short", hour12: false });
  const p = Object.fromEntries(f.formatToParts(now).map((x) => [x.type, x.value]));
  const minute = (+p.hour % 24) * 60 + +p.minute + +p.second / 60;
  const weekday = !["Sat", "Sun"].includes(p.weekday);
  return { day: `${p.year}-${p.month}-${p.day}`, minute, weekday, open: weekday && minute >= OPEN_MIN && minute < CLOSE_MIN };
}

// What you'd net if you sold now: the bid, less what you paid, less fees both ways.
export const netNow = ({ entry, bid, qty, fees = 0 }) => (bid - entry) * 100 * qty - 2 * fees * qty;

// The plan's horizon: hours of trading left before you sell, and calendar days the option loses meanwhile.
export function horizon(rules, clock, dte) {
  if (rules.mode === "swing") {
    const days = Math.max(dte - rules.swingDays, 0);
    return { hours: Math.max(days, 0.25) * 6.5, days, label: `by ${rules.swingDays} days before expiry` };
  }
  const today = clock.open && clock.minute < LAST_CALL;          // after 15:45, a new day trade is tomorrow's
  const left = today ? (LAST_CALL - clock.minute) / 60 : 6.25;
  return { hours: left, days: left / 24, label: today ? "by 15:45 today" : "by 15:45 next session" };
}

// The chance the stock reaches the price where the trade nets `target` (and where it hits the stop) at some point
// before the horizon: the model price matched to the option's current mid, its implied volatility as the stock's
// expected volatility, and the probability a driftless random walk touches a level, 2 x (1 - N(distance / spread)).
// Time decay is charged in full (the levels are worked out at the end of the horizon), so it errs on the low side.
export function targetOdds({ spot, strike, isCall, bid, ask, dte, iv, qty = 1, rules = DEFAULT_RULES, hz }) {
  if (!(spot > 0 && ask > 0 && iv > 0)) return null;
  const mid = bid > 0 ? (bid + ask) / 2 : ask, half = bid > 0 ? (ask - bid) / 2 : 0;
  const T0 = Math.max(dte, 0.5) / 365, T1 = Math.max(dte - hz.days, 0.05) / 365;
  const off = mid - bsPrice(spot, strike, T0, iv, isCall);
  const midAt = (S, T) => bsPrice(S, strike, T, iv, isCall) + off;
  const per = 100 * qty;
  const wantMid = ask + (rules.target + 2 * rules.fees * qty) / per + half;          // sell at the bid = mid - half
  const stopMid = ask - (rules.stop - 2 * rules.fees * qty) / per + half;
  const solve = (want, T) => {                                                         // the stock price giving that mid
    let lo = spot * 0.3, hi = spot * 2.5;
    const f = (S) => midAt(S, T) - want;
    if (isCall ? f(hi) < 0 : f(lo) < 0) return null;                                   // out of reach
    if (isCall ? f(lo) > 0 : f(hi) > 0) return isCall ? lo : hi;
    for (let i = 0; i < 80; i++) {
      const m = (lo + hi) / 2;
      if ((f(m) > 0) === isCall) hi = m; else lo = m;
    }
    return (lo + hi) / 2;
  };
  const sd = iv * Math.sqrt(Math.max(hz.hours, 0.05) / TRADING_YEAR_HOURS);
  const touch = (S) => (S == null ? 0 : Math.min(1, 2 * (1 - normCdf(Math.abs(Math.log(S / spot)) / sd))));
  const sTarget = solve(wantMid, T1), sStop = solve(Math.max(stopMid, 0.0001), T0);
  return {
    pTarget: touch(sTarget), pStop: stopMid <= 0 ? 0 : touch(sStop), sTarget, sStop,
    move: sTarget == null ? null : 100 * (sTarget / spot - 1),
  };
}

export function isLottery({ delta, bid, ask, dte }) {
  const spreadPct = bid > 0 ? 100 * (ask - bid) / ((ask + bid) / 2) : Infinity;
  const why = [];
  if (delta != null && Math.abs(delta) < 0.2) why.push("far out of the money (|delta| under 0.20)");
  if (spreadPct > 15) why.push(`wide spread (${Number.isFinite(spreadPct) ? spreadPct.toFixed(0) + "%" : "no bid"})`);
  if (dte < 1) why.push("expires today");
  return { lottery: why.length > 0, why, spreadPct };
}

// Does an earnings report fall while you'd still hold? earnings: {date: "YYYY-MM-DD", when: "bmo" | "amc" | "unknown"}.
export function earningsInPlan(earnings, rules, clock, expiration) {
  if (!earnings?.date) return null;
  const { date, when } = earnings;
  if (date < clock.day) return null;
  if (rules.mode !== "swing") {
    // a day trade sells by 15:45: only a report during today's session matters (rare)
    return date === clock.day && when === "unknown" ? { date, when, text: "reports today (time not given)" } : null;
  }
  const last = expiration ? shiftDays(expiration, -rules.swingDays) : null;
  if (last && date > last) return null;
  if (date === clock.day && when === "bmo" && clock.minute >= OPEN_MIN) return null;   // already out this morning
  return { date, when, text: `reports ${date === clock.day ? "today" : date}${when === "amc" ? " after the close" : when === "bmo" ? " before the open" : ""}` };
}

function shiftDays(iso, n) {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

// The one word for an open position.
//   TAKE PROFIT  net at the bid >= target          CUT          net <= -stop, or earnings would be held
//   TIME'S UP    day trade after 15:45, or a swing at its last day       HOLD   none of these
export function status({ net, rules, clock, dte, earn }) {
  if (net >= rules.target) return { code: "TAKE", word: "TAKE PROFIT", text: `+$${Math.round(net)} net hits your +$${rules.target} target: sell.` };
  if (net <= -rules.stop) return { code: "CUT", word: "CUT", text: `−$${Math.round(-net)} net hits your −$${rules.stop} stop: sell.` };
  if (rules.mode === "swing" && earn && !rules.holdEarnings)
    return { code: "CUT", word: "CUT", text: `Earnings: it ${earn.text}. Your plan doesn't hold through earnings: sell before the close.` };
  if (rules.mode !== "swing" && clock.weekday && clock.minute >= LAST_CALL && clock.minute < CLOSE_MIN + 60)
    return { code: "TIME", word: "TIME'S UP", text: "Day trade: sell before the close." };
  if (rules.mode === "swing" && dte <= rules.swingDays)
    return { code: "TIME", word: "TIME'S UP", text: `${dte} day${dte === 1 ? "" : "s"} to expiry: your plan sells ${rules.swingDays} days before. Sell.` };
  const toGo = rules.target - net;
  return { code: "HOLD", word: "HOLD", text: `$${Math.round(toGo)} more to your target, $${Math.round(rules.stop + net)} of room to your stop.` };
}

// GO or WAIT for buying one contract: every check has to pass. The first one that fails is the reason to wait.
export function goCheck({ isCall, cost, budget, odds, lot, trend, resistance, support, earn, rules, clock, dte, cooldownLeft = 0 }) {
  const checks = [];
  const add = (ok, label, why) => checks.push({ ok, label, why });
  const dir = isCall ? "up" : "down";
  add(cost <= budget, "Fits your budget", `costs $${Math.round(cost)}, over your $${budget}`);
  add(!lot.lottery, "Not a lottery ticket", lot.why.join(", "));
  const trendOk = trend ? (isCall ? trend.verdict !== "DOWNTREND" && trend.short === "up" : trend.verdict !== "UPTREND" && trend.short === "down") : false;
  add(trendOk, `Trend points ${dir}`, trend ? `trend ${trend.verdict.toLowerCase()}, short-term ${trend.short}` : "no trend read yet");
  const room = odds?.sTarget == null ? false : isCall ? !(resistance && resistance < odds.sTarget) : !(support && support > odds.sTarget);
  add(room, `Room to reach +$${rules.target}`, odds?.sTarget == null ? `+$${rules.target} isn't reachable before your exit`
    : `needs ${odds.sTarget.toFixed(2)}, past the ${isCall ? "resistance at " + resistance?.toFixed(2) : "support at " + support?.toFixed(2)}`);
  // A fair random walk reaches a $40 stop more often than a $50 target, always: the odds can't be the edge (the trend
  // is your edge). This only asks that the target is within reach before you have to sell.
  add(!!odds && odds.pTarget >= REACH, `+$${rules.target} within reach`,
    odds ? `${Math.round(odds.pTarget * 100)}% chance of +$${rules.target} before your exit (needs ${Math.round(REACH * 100)}%)` : "no price yet");
  const timeOk = rules.mode === "swing" ? dte > rules.swingDays + 1 : !clock.open || clock.minute <= LAST_CALL - 45;
  add(timeOk, rules.mode === "swing" ? "Time for a swing" : "Time left today",
    rules.mode === "swing" ? `${dte} days to expiry: too close to your ${rules.swingDays}-day exit` : "under 45 minutes before your 15:45 exit");
  add(!earn || rules.holdEarnings, "No earnings in the plan", earn ? `it ${earn.text}` : "");
  add(cooldownLeft <= 0, "Not right after a cut", `${Math.ceil(cooldownLeft)} min of cool-down left after your last CUT`);
  const fail = checks.find((c) => !c.ok);
  return { go: !fail, checks, reason: fail ? `${fail.label}: ${fail.why}` : "every check passes" };
}
