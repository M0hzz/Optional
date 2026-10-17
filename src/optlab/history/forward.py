"""Forward behaviour after signal: what price did after each historical match.

This is NOT a trading backtest. It assumes no entry, fill, position size, cost or option
price. For each event it measures, from the CLOSE of the signal bar (the last price known
when the signal was complete):

    forward return   close at the horizon / signal close - 1
    max favourable   highest high over the bars after the signal, up to the horizon, vs the signal close
    max adverse      lowest low over the same bars vs the signal close
    realized vol     standard deviation of bar-to-bar log returns over those bars, annualized

Horizons: "N" = N bars later. "Nd" = N trading sessions later: for intraday bars, the last
bar of that session at or before the signal's time of day; for daily bars, N bars. A
horizon that runs past the last available bar is marked incomplete and left out of the
statistics. Signals can be thinned with `dedupe_bars` (keep a signal only if at least that
many bars passed since the last kept one, per symbol) to limit overlapping windows.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..live.indicators import BARS_PER_YEAR
from .bars import EXCHANGE_TZ, INTRADAY


def horizon_end(bars: pd.DataFrame, i: int, horizon: str, timeframe: str) -> int | None:
    """Index of the bar that ends `horizon` for a signal at bar i; None if it runs past the data."""
    n = len(bars)
    if not horizon.endswith("d"):
        j = i + int(horizon)
        return j if j < n else None
    days = int(horizon[:-1])
    if timeframe not in INTRADAY:
        if timeframe == "1wk":
            return None
        j = i + days
        return j if j < n else None
    local = bars.index.tz_convert(EXCHANGE_TZ)
    dates = local.normalize()
    sessions = dates.unique()
    k = sessions.get_loc(dates[i]) + days
    if k >= len(sessions):
        return None
    target = sessions[k]
    on_day = np.flatnonzero(dates == target)
    if target == sessions[-1] and local[-1].time() < local[i].time():
        return None  # the session that holds the horizon isn't complete in the data yet
    tod = local[i].time()
    at_or_before = [j for j in on_day if local[j].time() <= tod]
    return int(at_or_before[-1] if at_or_before else on_day[0])


def dedupe(events: pd.DataFrame, bars_by_symbol: dict, min_bars: int) -> pd.Series:
    """True for events kept after thinning: per symbol, at least `min_bars` bars since the last kept one."""
    keep = pd.Series(True, index=events.index)
    if min_bars <= 0 or events.empty:
        return keep
    for sym, g in events.sort_values("timestamp").groupby("symbol"):
        pos = bars_by_symbol[sym].index.get_indexer(g["timestamp"])
        last = -10**9
        for idx, p in zip(g.index, pos):
            if p - last >= min_bars:
                last = p
            else:
                keep[idx] = False
    return keep


def forward_behavior(events: pd.DataFrame, bars_by_symbol: dict, horizons: list[str], timeframe: str,
                     dedupe_bars: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(per-event table, summary table). Events need symbol, timestamp and match_type columns."""
    if events.empty:
        return pd.DataFrame(), pd.DataFrame()
    kept = dedupe(events, bars_by_symbol, dedupe_bars)
    per_year = BARS_PER_YEAR.get(timeframe, 252)
    rows = []
    for idx, ev in events[kept].iterrows():
        bars = bars_by_symbol[ev["symbol"]]
        i = bars.index.get_loc(ev["timestamp"])
        c0 = float(bars["close"].iloc[i])
        for h in horizons:
            j = horizon_end(bars, i, h, timeframe)
            row = {"symbol": ev["symbol"], "timestamp": ev["timestamp"], "match_type": ev["match_type"],
                   "horizon": h, "complete": j is not None}
            if j is not None and j > i:
                seg = bars.iloc[i + 1:j + 1]
                closes = bars["close"].iloc[i:j + 1].to_numpy(float)
                lr = np.diff(np.log(closes))
                row.update({
                    "bars_ahead": j - i, "end_time": bars.index[j],
                    "forward_return_pct": 100 * (closes[-1] / c0 - 1),
                    "max_favorable_pct": 100 * (seg["high"].max() / c0 - 1),
                    "max_adverse_pct": 100 * (seg["low"].min() / c0 - 1),
                    "realized_vol_pct": 100 * lr.std(ddof=1) * np.sqrt(per_year) if len(lr) > 1 else np.nan,
                })
            elif j is not None:
                row["complete"] = False   # e.g. a 1d horizon from the last bar of a session that has no later bar
            rows.append(row)
    per_event = pd.DataFrame(rows)
    return per_event, summarize(per_event, horizons, int((~kept).sum()))


def summarize(per_event: pd.DataFrame, horizons: list[str], deduped: int = 0) -> pd.DataFrame:
    out = []
    for h in horizons:
        for group in ("all", "exact", "near"):
            g = per_event[per_event["horizon"] == h]
            if group != "all":
                g = g[g["match_type"] == group]
            done = g[g["complete"]] if not g.empty else g
            r = done["forward_return_pct"] if "forward_return_pct" in done else pd.Series(dtype=float)
            out.append({
                "horizon": h, "group": group, "signals": len(done), "incomplete": int(len(g) - len(done)),
                "mean_return_pct": r.mean(), "median_return_pct": r.median(),
                "positive_pct": 100 * (r > 0).mean() if len(r) else np.nan,
                "negative_pct": 100 * (r < 0).mean() if len(r) else np.nan,
                "mean_max_favorable_pct": done["max_favorable_pct"].mean() if len(done) else np.nan,
                "mean_max_adverse_pct": done["max_adverse_pct"].mean() if len(done) else np.nan,
                "median_realized_vol_pct": done["realized_vol_pct"].median() if len(done) else np.nan,
            })
    s = pd.DataFrame(out)
    s.attrs["deduped"] = deduped
    return s
