// Display formatting only. Money arrives from the API as exact decimal strings ("−302.6000") and is
// formatted here with string/BigInt arithmetic, never through binary floating point.

const SCALE = 4;

function toUnits(s: string): bigint {
  const neg = s.trim().startsWith("-");
  const [i, f = ""] = s.trim().replace(/^[-+]/, "").split(".");
  const units = BigInt((i || "0") + (f + "0".repeat(SCALE)).slice(0, SCALE));
  return neg ? -units : units;
}

/** Round to `dp` decimals (half to even) and group thousands. "-1234.5650" -> "-1,234.56". */
export function decimal(s: string | null | undefined, dp = 2): string | null {
  if (s === null || s === undefined || s === "") return null;
  let u = toUnits(s);
  const neg = u < 0n;
  if (neg) u = -u;
  const step = 10n ** BigInt(SCALE - dp);
  let q = u / step;
  const r = u % step;
  const half = step / 2n;
  if (step > 1n && (r > half || (r === half && q % 2n === 1n))) q += 1n;   // dp = 4 drops nothing: no rounding
  const str = q.toString().padStart(dp + 1, "0");
  const intPart = str.slice(0, str.length - dp).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const frac = dp > 0 ? "." + str.slice(str.length - dp) : "";
  const zero = q === 0n;
  return (neg && !zero ? "−" : "") + intPart + frac;
}

/** "$1,234.56", "−$302.60". */
export function money(s: string | null | undefined, dp = 2): string | null {
  const d = decimal(s, dp);
  if (d === null) return null;
  return d.startsWith("−") ? "−$" + d.slice(1) : "$" + d;
}

/** Signed dollars for P&L: "+$12.00" / "−$3.40". */
export function pnl(s: string | null | undefined): string | null {
  const m = money(s);
  if (m === null) return null;
  return m.startsWith("−") || m === "$0.00" ? m : "+" + m;
}

/** Option/stock price: 2 decimals, or up to 4 when the value needs them. */
export function price(s: string | null | undefined): string | null {
  if (s === null || s === undefined) return null;
  let four = decimal(s, 4)!;
  while (four.endsWith("0") && four.length - four.indexOf(".") - 1 > 2) four = four.slice(0, -1);   // keep ≥ 2 decimals
  return four.startsWith("−") ? "−$" + four.slice(1) : "$" + four;
}

export function sign(s: string | null | undefined): -1 | 0 | 1 {
  if (!s) return 0;
  const u = toUnits(s);
  return u > 0n ? 1 : u < 0n ? -1 : 0;
}

export function num(x: number | null | undefined, dp = 2): string | null {
  if (x === null || x === undefined || !Number.isFinite(x)) return null;
  return x.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

export function pct(x: number | null | undefined, dp = 1): string | null {
  const n = num(x, dp);
  return n === null ? null : n + "%";
}

const TZ = "America/New_York";

export function dateTime(iso: string | null | undefined, tz = TZ): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return d.toLocaleString("en-US", { timeZone: tz, year: "numeric", month: "short", day: "numeric",
                                     hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
}

export function date(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", { timeZone: "UTC", year: "numeric", month: "short", day: "numeric" });
}

export function age(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null;
  if (seconds < 60) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${(seconds / 3600).toFixed(1)} h ago`;
}

/** Validate a money input the way the API does: digits, optional sign, at most 4 decimals. */
export function moneyInputError(s: string, allowNegative = false): string | null {
  const v = s.trim();
  if (v === "") return null;
  const re = allowNegative ? /^-?\d+(\.\d{1,4})?$/ : /^\d+(\.\d{1,4})?$/;
  if (!re.test(v)) {
    if (/^-?\d+\.\d{5,}$/.test(v)) return "At most 4 decimal places (optlab doesn't round inputs).";
    return allowNegative ? "Enter a number like 1.25" : "Enter a positive number like 1.25";
  }
  return null;
}
