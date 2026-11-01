"""Forward statistics, distributions, breakdowns, coverage and bias checks for one historical search.

Definitions (stats-v1)
    forward return      close at the horizon ÷ close of the signal bar − 1 (the signal bar's close is the last
                        price known when the signal was complete; nothing earlier or on the same bar is used)
    favourable / adverse   for a bullish (or unspecified) direction: highest high / lowest low after the signal,
                        up to the horizon; for a bearish direction they swap (a fall is favourable)
    win                 bullish: return > 0; bearish: return < 0; no direction: not defined ("up" / "down" only)
    regime labels       measured at the signal bar from that bar and earlier only:
                          trend       close above or below its 200-bar average
                          market      SPY close above or below its 50-bar average (same timeframe)
                          volatility  14-bar ATR's percentile over the previous 252 bars: low (< 33), middle, high (> 67)
                          time of day intraday only, exchange time: open 9:30–10:30, midday 10:30–14:30, close 14:30–16:00
    low sample          fewer than 20 signals: shown, flagged, never hidden
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from ..history.bars import EXCHANGE_TZ, INTRADAY
from ..history.explorer import evaluate_symbol
from ..live.indicators import atr, sma

VERSION = "stats-v1"
LOW_SAMPLE = 20
SMALL_SAMPLE_WARNING = 30


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(v) else round(v, 4)


def signed(per_event: pd.DataFrame, direction: str | None) -> pd.DataFrame:
    """Add favourable / adverse columns for the direction, and a win flag where a direction is given."""
    d = per_event.copy()
    if d.empty:
        return d
    if direction == "bearish":
        d["favorable_pct"], d["adverse_pct"] = -d.get("max_adverse_pct"), -d.get("max_favorable_pct")
        d["win"] = d.get("forward_return_pct") < 0
    else:
        d["favorable_pct"], d["adverse_pct"] = d.get("max_favorable_pct"), d.get("max_adverse_pct")
        d["win"] = (d.get("forward_return_pct") > 0) if direction == "bullish" else np.nan
    return d


def horizon_stats(d: pd.DataFrame, horizon: str, direction: str | None, group: str = "all") -> dict:
    g = d[(d["horizon"] == horizon)] if not d.empty else d
    if group != "all" and not g.empty:
        g = g[g["match_type"] == group]
    done = g[g["complete"]] if not g.empty else g
    r = done["forward_return_pct"].astype(float) if len(done) else pd.Series(dtype=float)
    out = {"horizon": horizon, "group": group, "signals": int(len(done)), "incomplete": int(len(g) - len(done)),
           "mean_return_pct": _f(r.mean()), "median_return_pct": _f(r.median()),
           "p25_return_pct": _f(r.quantile(0.25)) if len(r) else None, "p75_return_pct": _f(r.quantile(0.75)) if len(r) else None,
           "best_pct": _f(r.max()), "worst_pct": _f(r.min()),
           "up_pct": _f(100 * (r > 0).mean()) if len(r) else None, "down_pct": _f(100 * (r < 0).mean()) if len(r) else None,
           "mean_favorable_pct": _f(done["favorable_pct"].mean()) if len(done) else None,
           "mean_adverse_pct": _f(done["adverse_pct"].mean()) if len(done) else None,
           "median_realized_vol_pct": _f(done["realized_vol_pct"].median()) if len(done) and "realized_vol_pct" in done else None,
           "low_sample": len(done) < LOW_SAMPLE}
    if direction in ("bullish", "bearish") and len(done):
        w = done["win"].astype(bool)
        wins, losses = r[w], r[~w]
        out.update({"win_pct": _f(100 * w.mean()), "wins": int(w.sum()), "losses": int((~w).sum()),
                    "mean_win_pct": _f(wins.mean()), "mean_loss_pct": _f(losses.mean())})
    return out


def distribution(d: pd.DataFrame, horizon: str, direction: str | None, bins: int = 12) -> dict:
    g = d[(d["horizon"] == horizon) & d["complete"]] if not d.empty else d
    r = g["forward_return_pct"].astype(float).dropna().to_numpy() if len(g) else np.array([])
    if not len(r):
        return {"horizon": horizon, "bins": [], "signals": 0}
    lo, hi = np.percentile(r, 1), np.percentile(r, 99)
    if lo == hi:
        lo, hi = lo - 0.5, hi + 0.5
    edges = np.linspace(lo, hi, bins + 1)
    clipped = np.clip(r, lo, hi)
    counts, _ = np.histogram(clipped, edges)
    out = []
    for i, c in enumerate(counts):
        a, b = float(edges[i]), float(edges[i + 1])
        mid = (a + b) / 2
        kind = None
        if direction == "bullish":
            kind = "win" if mid > 0 else "loss"
        elif direction == "bearish":
            kind = "win" if mid < 0 else "loss"
        out.append({"from_pct": round(a, 3), "to_pct": round(b, 3), "count": int(c), "kind": kind,
                    "open_low": i == 0 and bool((r < lo).any()), "open_high": i == bins - 1 and bool((r > hi).any())})
    return {"horizon": horizon, "signals": int(len(r)), "bins": out,
            "note": "The lowest and highest 1% are counted in the end bins." if len(r) >= 20 else None}


# ------------------------------------------------------------------ regimes (from the signal bar and earlier only)
def regime_labels(bars: pd.DataFrame, positions: np.ndarray, bench: pd.DataFrame | None, timeframe: str) -> pd.DataFrame:
    c = bars["close"].astype(float)
    a = atr(bars["high"].astype(float), bars["low"].astype(float), c, 14)
    a_rank = a.rolling(252, min_periods=60).rank(pct=True)          # trailing window: past bars only
    s200 = sma(c, 200)
    out = pd.DataFrame(index=positions)
    ci, si, ri = c.iloc[positions].to_numpy(), s200.iloc[positions].to_numpy(), a_rank.iloc[positions].to_numpy()
    out["trend"] = np.where(np.isnan(si), "unavailable (under 200 bars)", np.where(ci > si, "above 200-bar average",
                                                                                     "below 200-bar average"))
    out["volatility"] = np.where(np.isnan(ri), "unavailable (under 60 bars)",
                                 np.where(ri < 1 / 3, "low ATR percentile", np.where(ri > 2 / 3, "high ATR percentile",
                                                                                   "middle ATR percentile")))
    if bench is not None and not bench.empty:
        bc = bench["close"].astype(float)
        b50 = sma(bc, 50)
        # the market's state as of each signal: the last benchmark bar at or before the signal's time
        at = bars.index[positions]
        bi = bc.index.searchsorted(at, side="right") - 1
        bv = np.where(bi >= 0, bc.to_numpy()[np.clip(bi, 0, None)], np.nan)
        b5 = np.where(bi >= 0, b50.to_numpy()[np.clip(bi, 0, None)], np.nan)
        out["market"] = np.where(np.isnan(b5), "unavailable", np.where(bv > b5, "SPY above 50-bar average",
                                                                       "SPY below 50-bar average"))
    if timeframe in INTRADAY:
        t = bars.index[positions].tz_convert(EXCHANGE_TZ)
        mins = t.hour * 60 + t.minute
        out["time_of_day"] = np.where(mins < 10 * 60 + 30, "open (9:30–10:30)",
                                      np.where(mins < 14 * 60 + 30, "midday (10:30–14:30)", "close (14:30–16:00)"))
    return out


def breakdown(d: pd.DataFrame, labels: pd.DataFrame, horizon: str, direction: str | None) -> list[dict]:
    """Rows: dimension, subset, signals, median, mean, up/down or win %, low-sample flag."""
    g = d[(d["horizon"] == horizon) & d["complete"]] if not d.empty else d
    if g.empty or labels.empty:
        return []
    rows = []
    for dim in labels.columns:
        for subset, idx in labels.groupby(dim).groups.items():
            keys = set(idx)
            sub = g[g["key"].isin(keys)]
            r = sub["forward_return_pct"].astype(float)
            row = {"dimension": dim, "subset": subset, "signals": int(len(sub)), "median_return_pct": _f(r.median()),
                   "mean_return_pct": _f(r.mean()), "up_pct": _f(100 * (r > 0).mean()) if len(r) else None,
                   "low_sample": len(sub) < LOW_SAMPLE, "keys": sorted(str(k) for k in keys)[:2000]}
            if direction in ("bullish", "bearish") and len(sub):
                row["win_pct"] = _f(100 * sub["win"].astype(bool).mean())
            rows.append(row)
    return rows


# ------------------------------------------------------------------ bias checks
def truncation_test(strategy, bars_by_symbol: dict, results: pd.DataFrame, evals_by_symbol: dict, samples: int = 12) -> dict:
    """Re-evaluate the conditions at sampled signals using ONLY bars up to the signal and compare the verdicts:
    any difference would mean the full-history evaluation looked ahead."""
    if results.empty:
        return {"checked": 0, "mismatches": 0, "details": []}
    pick = results.sort_values("timestamp").iloc[np.unique(np.linspace(0, len(results) - 1, min(samples, len(results))).astype(int))]
    checked, bad, details = 0, 0, []
    for _, row in pick.iterrows():
        bars = bars_by_symbol[row["symbol"]]
        i = int(bars.index.get_loc(row["timestamp"]))
        full = evals_by_symbol[row["symbol"]]
        cut = evaluate_symbol(strategy, bars.iloc[: i + 1])
        for e_full, e_cut in zip(full, cut):
            same_pass = bool(e_full.passed[i]) == bool(e_cut.passed[-1])
            a, b = float(e_full.observed[i]), float(e_cut.observed[-1])
            same_val = (math.isnan(a) and math.isnan(b)) or (math.isfinite(a) and math.isfinite(b) and abs(a - b) <= 1e-9 * max(1, abs(a)))
            if not (same_pass and same_val):
                bad += 1
                details.append(f"{row['symbol']} {row['timestamp']}: {e_full.cond.id} differs with later bars removed")
        checked += 1
    return {"checked": checked, "mismatches": bad, "details": details[:10]}


def bias_checks(*, strategy, search, per_event: pd.DataFrame, truncation: dict, deduped: int, longest_bars: int,
                focus_signals: int, prior_versions: int, source: str) -> list[dict]:
    def chk(code, title, status, detail):
        return {"code": code, "title": title, "status": status, "detail": detail}

    out = [
        chk("NO_LOOKAHEAD", "Conditions use only past and current bars",
            "pass" if truncation["mismatches"] == 0 else "fail",
            f"Truncation test: {truncation['checked']} signal(s) re-evaluated with every later bar removed; "
            f"{truncation['mismatches']} difference(s)." + (" " + "; ".join(truncation["details"]) if truncation["details"] else "")),
        chk("SIGNAL_CLOSE", "Forward returns start at the signal bar's close", "pass",
            "No price from before the signal was complete is used, and nothing is assumed about fills."),
        chk("COMPLETED_BARS", "Only completed bars", "pass",
            f"{sum(r.incomplete_bars_dropped for r in search.reports.values())} bar(s) still in progress were dropped."),
        chk("INCOMPLETE_HORIZONS", "Horizons past the end of the data are left out",
            "pass", f"{int((~per_event['complete']).sum()) if len(per_event) else 0} signal-horizon pair(s) ran past the data "
                    "and aren't in the statistics."),
        chk("OVERLAP", "Overlapping signals thinned",
            "pass" if strategy.dedupe_bars >= longest_bars else "warn",
            f"A signal is kept only if {strategy.dedupe_bars} bar(s) passed since the last kept one ({deduped} thinned); "
            + ("windows don't overlap." if strategy.dedupe_bars >= longest_bars else
               f"the longest horizon is {longest_bars} bars, so some forward windows overlap and aren't independent.")),
        chk("SAMPLE_SIZE", "Enough signals", "pass" if focus_signals >= SMALL_SAMPLE_WARNING else "warn",
            f"{focus_signals} completed signal(s) at the focus horizon"
            + ("." if focus_signals >= SMALL_SAMPLE_WARNING else f": under {SMALL_SAMPLE_WARNING}, the statistics are unstable.")),
        chk("NEAR_SEPARATE", "Near matches reported separately", "pass",
            "Exact and near matches have their own rows; near matches are looser by construction."),
        chk("SURVIVORSHIP", "Survivorship", "warn",
            "The symbols were chosen today: companies that were delisted, merged or went bankrupt aren't in the search, "
            "which can flatter the results."),
        chk("ADJUSTED_PRICES", "Split- and dividend-adjusted prices",
            "pass" if all(r.adjusted for r in search.reports.values()) else "warn",
            "Past prices are adjusted, so they differ from the prices quoted at the time; percentages are unaffected."),
        chk("DATA_SNOOPING", "Many variations tried", "warn" if prior_versions >= 5 else "pass",
            f"{prior_versions} other version(s) of this analysis were run for the same object in the last 30 days"
            + (": trying many variations until one looks good makes the best one look better than it is." if prior_versions >= 5 else ".")),
    ]
    if source == "synthetic":
        out.insert(0, chk("SYNTHETIC", "Synthetic data", "fail",
                          "These bars are generated demo data (synthetic mode), not market history: the results say nothing about real markets."))
    return out


def coverage(search) -> list[dict]:
    rows = []
    for sym, r in search.reports.items():
        rows.append({"symbol": sym, "source": r.source, "first_bar": r.first_bar, "last_bar": r.last_bar,
                     "bars_in_range": r.bars_in_range, "missing_bars": r.missing_bars,
                     "warmup_requested": r.warmup_bars_requested, "warmup_available": r.warmup_bars_available,
                     "incomplete_bars_dropped": r.incomplete_bars_dropped, "adjusted": r.adjusted, "warnings": list(r.warnings)})
    return rows


def jsonable(v):
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return _f(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if v is pd.NaT:
        return None
    return v
