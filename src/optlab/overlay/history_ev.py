"""Historical evidence for an overlay rule: how often it matched before, what price did afterwards,
how stable that was, and which past bars look most like today. Descriptive statistics only:
nothing here predicts, and nothing is called a cause.

Events      a bar where the rule's state became CONFIRMED (exact) or NEAR (near) after not being
            in that state on the bar before, so one long run counts once.
Outcomes    for horizon h bars after event bar i (close-to-close, completed bars only):
            return = close[i+h] / close[i] − 1
            favorable excursion = max(high[i+1..i+h]) / close[i] − 1 (bullish; lows for bearish)
            adverse excursion   = min(low[i+1..i+h])  / close[i] − 1 (bullish; highs for bearish)
            Returns are signed for the rule's direction: a bearish rule "works" when price falls.
Stability   events split into three equal time periods; stable when all three medians share a sign.
Similarity  normalized_distance_f = |current_f − historical_f| / scale_f
            similarity = 100 × max(0, 1 − Σ(w_f × d_f) / Σ(w_f))
            with the features, scales and weights in overlay.yaml → similarity.
Sensitivity each numeric threshold on the right of a comparison is moved one step down and up
            (step = 1 for |value| ≥ 10, else 10% of the value) and the events re-counted.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..live.expr import Evaluator, ExprError, parse
from ..live.indicators import atr, sma
from .rules import evaluate_rule, make_evaluator

OPTIONS_LIMITATION = ("Technical signal available. Historical options affordability cannot be verified because "
                      "timestamped option quotes are unavailable.")
HORIZONS = (1, 3, 5, 10, 20)


def options_history_status(signal_dates, snapshot_dates) -> tuple[str, str]:
    """Historical option affordability can only use a chain stored on the signal's own date; the current chain is
    never applied to past signals."""
    snaps = {pd.Timestamp(d).date() for d in snapshot_dates or []}
    hits = [d for d in signal_dates if pd.Timestamp(d).date() in snaps]
    if not hits:
        return "INSUFFICIENT_DATA", OPTIONS_LIMITATION
    return "INFO", (f"Stored option chains exist for {len(hits)} of {len(signal_dates)} signal dates; only those "
                    "dates can show historical affordability.")


def events_from_states(states: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Indices of new exact (CONFIRMED) and new near (NEAR_CONFIRMATION) states."""
    s = np.asarray(states, dtype=object)
    prev = np.r_[["NONE"], s[:-1]]
    exact = np.flatnonzero((s == "CONFIRMED") & (prev != "CONFIRMED"))
    near = np.flatnonzero((s == "NEAR_CONFIRMATION") & (prev != "NEAR_CONFIRMATION") & (prev != "CONFIRMED"))
    return exact, near


def outcomes(bars: pd.DataFrame, idx: np.ndarray, horizons=HORIZONS, direction: str = "bullish") -> pd.DataFrame:
    c, h, lo = (bars[k].to_numpy(float) for k in ("close", "high", "low"))
    sign = -1.0 if direction == "bearish" else 1.0
    rows = []
    n = len(c)
    for i in idx:
        for hz in horizons:
            j = i + hz
            row = {"bar": int(i), "time": bars.index[i], "horizon": hz, "complete": j < n}
            if j < n:
                seg_h, seg_l = h[i + 1:j + 1], lo[i + 1:j + 1]
                up, dn = seg_h.max() / c[i] - 1, seg_l.min() / c[i] - 1
                fav, adv = (up, dn) if sign > 0 else (-dn, -up)
                t_up, t_dn = int(np.argmax(seg_h)) + 1, int(np.argmin(seg_l)) + 1
                t_fav, t_adv = (t_up, t_dn) if sign > 0 else (t_dn, t_up)
                row.update({"return_pct": 100 * sign * (c[j] / c[i] - 1), "favorable_pct": 100 * fav,
                            "adverse_pct": 100 * adv, "bars_to_favorable": t_fav, "bars_to_adverse": t_adv,
                            "first": "favorable" if t_fav < t_adv else "adverse" if t_adv < t_fav else "same bar"})
            rows.append(row)
    return pd.DataFrame(rows)


def summarize(out: pd.DataFrame, min_samples: int) -> pd.DataFrame:
    rows = []
    for hz, g in out.groupby("horizon") if not out.empty else []:
        d = g[g["complete"]]
        r = d["return_pct"] if "return_pct" in d else pd.Series(dtype=float)
        ds = d.sort_values("time")
        cuts = [0, len(ds) // 3, 2 * len(ds) // 3, len(ds)]
        thirds = [ds.iloc[cuts[k]:cuts[k + 1]] for k in range(3)] if len(ds) >= 3 else []
        meds = [float(t["return_pct"].median()) for t in thirds if len(t)]
        stable = bool(len(meds) == 3 and (all(m > 0 for m in meds) or all(m < 0 for m in meds)))
        rows.append({"horizon": hz, "events": len(d), "incomplete": int((~g["complete"]).sum()),
                     "positive_pct": 100 * (r > 0).mean() if len(r) else np.nan, "median_pct": r.median(),
                     "mean_pct": r.mean(), "favorable_pct": d["favorable_pct"].mean() if len(d) else np.nan,
                     "adverse_pct": d["adverse_pct"].mean() if len(d) else np.nan,
                     "favorable_first_pct": 100 * (d["first"] == "favorable").mean() if len(d) else np.nan,
                     "bars_to_favorable": d["bars_to_favorable"].median() if len(d) else np.nan,
                     "bars_to_adverse": d["bars_to_adverse"].median() if len(d) else np.nan,
                     "period_medians": ", ".join(f"{m:+.2f}%" for m in meds) or "—", "stable": stable,
                     "low_sample": len(d) < min_samples})
    return pd.DataFrame(rows)


def regimes(bars: pd.DataFrame, bench: pd.DataFrame | None, idx: np.ndarray) -> pd.DataFrame:
    """Regime labels at each event bar (only measures the data supports)."""
    c = bars["close"].astype(float)
    a = atr(bars["high"].astype(float), bars["low"].astype(float), c, 14)
    a_rank = a.rolling(252, min_periods=60).rank(pct=True)
    s200 = sma(c, 200)
    out = pd.DataFrame(index=idx)
    out["Long-term trend"] = np.where(c.iloc[idx] > s200.iloc[idx], "Above 200-day MA",
                                      np.where(s200.iloc[idx].isna(), "Unavailable", "Below 200-day MA"))
    out["Volatility"] = np.where(a_rank.iloc[idx] >= 0.5, "High ATR percentile",
                                 np.where(a_rank.iloc[idx].isna(), "Unavailable", "Low ATR percentile"))
    if bench is not None and not bench.empty:
        bc = bench["close"].astype(float).reindex(bars.index)
        b50 = sma(bc, 50)
        out["Benchmark trend"] = np.where(bc.iloc[idx] > b50.iloc[idx], "Benchmark above 50-day",
                                          np.where(b50.iloc[idx].isna(), "Unavailable", "Benchmark below 50-day"))
    return out


def regime_table(out: pd.DataFrame, labels: pd.DataFrame, horizon: int, min_samples: int) -> pd.DataFrame:
    d = out[(out["horizon"] == horizon) & out["complete"]].set_index("bar")
    rows = []
    for col in labels.columns:
        for val, grp in labels.groupby(col):
            r = d.loc[d.index.intersection(grp.index), "return_pct"]
            rows.append({"regime": col, "subset": val, "events": len(r),
                         "positive_pct": 100 * (r > 0).mean() if len(r) else np.nan,
                         "median_pct": r.median(), "low_sample": len(r) < min_samples})
    return pd.DataFrame(rows)


def feature_matrix(ev: Evaluator, features: dict, index) -> pd.DataFrame:
    cols = {}
    for expr in features:
        try:
            v = ev(expr)
            cols[expr] = v.astype(float) if isinstance(v, pd.Series) else pd.Series(float(v), index=index)
        except ExprError:
            cols[expr] = pd.Series(np.nan, index=index)
    return pd.DataFrame(cols, index=index)


def similarity(current: pd.Series, hist: pd.DataFrame, features: dict) -> pd.Series:
    w = np.array([float(features[f].get("weight", 1)) for f in hist.columns])
    sc = np.array([float(features[f]["scale"]) for f in hist.columns])
    d = (hist - current).abs() / sc
    ok = d.notna().all(axis=1) & current.notna().all()
    score = 100 * np.maximum(0, 1 - (d.fillna(0).to_numpy() * w).sum(1) / w.sum())
    return pd.Series(np.where(ok, score, np.nan), index=hist.index)


def failure_clusters(bars: pd.DataFrame, ev: Evaluator, out: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """How often simple measurable descriptors were present at events with weak outcomes (bottom quartile)
    versus all events. Descriptive frequencies, not causes."""
    d = out[(out["horizon"] == horizon) & out["complete"]]
    if len(d) < 8:
        return pd.DataFrame()
    cut = d["return_pct"].quantile(0.25)
    weak = set(d.loc[d["return_pct"] <= cut, "bar"])
    desc = {"RSI(14) above 70": "rsi(14) > 70", "RSI(14) below 30": "rsi(14) < 30",
            "Relative volume ≥ 1.5": "rel_volume(20) >= 1.5", "More than 2 ATR above the 21-day EMA":
            "close - ema(21) > 2 * atr(14)", "Below the 200-day MA": "close < sma(200)",
            "Gap of more than 1%": "abs(open / ago(close, 1) - 1) > 0.01", "Bollinger width in top 20%":
            "pct_rank(bb_width(20), 252) >= 80"}
    rows = []
    for name, expr in desc.items():
        try:
            v = ev(expr)
        except ExprError:
            continue
        v = np.asarray(v.fillna(False) if isinstance(v, pd.Series) else v, bool)
        allb = d["bar"].to_numpy()
        f_all = 100 * v[allb].mean()
        f_weak = 100 * v[list(weak)].mean() if weak else np.nan
        rows.append({"descriptor": name, "in_weak_outcomes_pct": f_weak, "in_all_events_pct": f_all,
                     "difference_pts": f_weak - f_all})
    return pd.DataFrame(rows).sort_values("difference_pts", ascending=False)


def _thresholds(rule) -> list[tuple[int, float]]:
    out = []
    for k, c in enumerate(rule.conditions):
        body = parse(c.expression).body
        if isinstance(body, ast.Compare) and len(body.comparators) == 1:
            rhs = body.comparators[0]
            if isinstance(rhs, ast.Constant) and isinstance(rhs.value, (int, float)) and not isinstance(rhs.value, bool):
                out.append((k, float(rhs.value)))
    return out


def sensitivity(rule, bars: pd.DataFrame, bench, th: dict) -> pd.DataFrame:
    """Re-count confirmations with each comparison threshold moved one step either way."""
    rows = []
    base = evaluate_rule(rule, bars, make_evaluator(bars, rule.timeframe, bench), "S", th, {})
    n0 = len(events_from_states(base.states)[0])
    for k, val in _thresholds(rule):
        step = 1.0 if abs(val) >= 10 else max(abs(val) * 0.1, 0.01)
        for v in (val - step, val + step):
            r2 = copy.deepcopy(rule)
            # parse() results are cached and shared: edit a copy, never the cached tree
            body = copy.deepcopy(parse(r2.conditions[k].expression).body)
            body.comparators[0] = ast.Constant(round(v, 6))
            r2.conditions[k].expression = ast.unparse(body)
            res = evaluate_rule(r2, bars, make_evaluator(bars, rule.timeframe, bench), "S", th, {})
            n = len(events_from_states(res.states)[0])
            rows.append({"condition": rule.conditions[k].id, "threshold": val, "tested": round(v, 6),
                         "events": n, "base_events": n0,
                         "change_pct": 100 * (n / n0 - 1) if n0 else np.nan})
    df = pd.DataFrame(rows)
    if not df.empty:
        drop = float(th["parameter_sensitivity_drop"])
        df["sensitive"] = df["change_pct"].abs() > 100 * drop
    return df


@dataclass
class Evidence:
    rule_id: str
    symbol: str
    exact: int
    near: int
    summary_exact: pd.DataFrame
    summary_near: pd.DataFrame
    per_event: pd.DataFrame
    regimes: pd.DataFrame
    analogues: pd.DataFrame
    clusters: pd.DataFrame
    sensitivity: pd.DataFrame
    current_vs_history: pd.DataFrame
    status: str
    notes: list[str] = field(default_factory=list)


def evidence(result, rule, bars: pd.DataFrame, bench: pd.DataFrame | None, cfg, horizon: int = 5,
             blocked_reason: str | None = None, with_sensitivity: bool = True) -> Evidence:
    th = cfg.thresholds
    mins = int(th["minimum_historical_samples"])
    empty = pd.DataFrame()
    if blocked_reason:
        return Evidence(rule.id, result.symbol, 0, 0, empty, empty, empty, empty, empty, empty, empty, empty,
                        "DATA_ERROR", [blocked_reason])
    ex, nr = events_from_states(result.states)
    i_now = len(bars) - 1
    ex, nr = ex[ex < i_now], nr[nr < i_now]          # the current bar is not part of its own history
    out_ex = outcomes(bars, ex, HORIZONS, rule.direction).assign(match="exact")
    out_nr = outcomes(bars, nr, HORIZONS, rule.direction).assign(match="near")
    s_ex, s_nr = summarize(out_ex, mins), summarize(out_nr, mins)
    ev = make_evaluator(bars, rule.timeframe, bench)
    lab = regimes(bars, bench, ex) if len(ex) else pd.DataFrame()
    reg = regime_table(out_ex, lab, horizon, mins) if len(ex) else empty
    feats = cfg.raw["similarity"]["features"]
    fm = feature_matrix(ev, feats, bars.index)
    pool = np.r_[ex, nr]
    ana = empty
    if len(pool):
        sim = similarity(fm.iloc[-1], fm.iloc[pool], feats)
        top = sim.sort_values(ascending=False).head(int(cfg.raw["similarity"]["top"]))
        d_all = pd.concat([out_ex, out_nr])
        d = d_all[(d_all["horizon"] == horizon) & d_all["complete"]].set_index("bar")
        pos = [bars.index.get_loc(t) for t in top.index]
        exs = set(ex.tolist())
        ana = pd.DataFrame({"date": list(top.index), "similarity": top.values,
                            "match": ["exact" if p in exs else "near" for p in pos]})
        ana[f"return_{horizon}_pct"] = [d["return_pct"].get(p, np.nan) for p in pos]
        for f in feats:
            ana[f] = fm[f].iloc[pos].to_numpy()
    clusters = failure_clusters(bars, ev, out_ex, horizon) if len(ex) else empty
    sens = sensitivity(rule, bars, bench, th) if with_sensitivity else empty
    cur = []
    for c in result.conditions:
        name = c["id"]
        try:
            body = parse(c["expression"]).body
            col = ev(ast.unparse(body.left)) if isinstance(body, ast.Compare) else None
        except ExprError:
            col = None
        if isinstance(col, pd.Series) and len(ex):
            hist = col.iloc[ex].dropna()
            now = col.iloc[-1]
            cur.append({"condition": name, "current": float(now), "exact_median": float(hist.median()) if len(hist) else np.nan,
                        "percentile_among_exact": float(100 * (hist <= now).mean()) if len(hist) else np.nan})
    n5 = s_ex.loc[s_ex["horizon"] == horizon, "events"] if not s_ex.empty else pd.Series(dtype=int)
    n5 = int(n5.iloc[0]) if len(n5) else 0
    status = "LOW_SAMPLE_SIZE" if n5 < mins else "INFO"
    notes = []
    if n5 < mins:
        notes.append(f"Low sample size: {n5} exact matches with a complete {horizon}-bar outcome; the minimum is {mins}. "
                     "Percentages from so few events can swing widely.")
    return Evidence(rule.id, result.symbol, len(ex), len(nr), s_ex, s_nr, pd.concat([out_ex, out_nr]), reg, ana,
                    clusters, sens, pd.DataFrame(cur), status, notes)
