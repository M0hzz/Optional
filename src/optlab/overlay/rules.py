"""Technical rule engine for the overlay.

A rule is a list of conditions in the same expression language as scans.yaml and the
Signal explorer, and is scored by the same code (history.explorer), so a rule's verdict
here matches what a historical search with the same conditions reports.

Per bar, on completed bars only:

    score       = 100 × Σ(weight × credit) / Σ(weight)
                  credit: pass 1; near 1 − shortfall / tolerance (at most 0.99); fail 0;
                  a condition that can't be evaluated earns 0 and is listed as unavailable
    CONFIRMED   every required condition passes (or, with required_conditions_must_pass:
                false, is at least near), score ≥ minimum_score (rule's confirmation, else
                thresholds.confirmed_match_score) and passes ≥ minimum_pass_count
    NEAR        not confirmed, score ≥ near_match_score, every required condition passes or is near
    WATCH       score ≥ watch_score
    NOT_PRESENT otherwise
    INSUFFICIENT_DATA  a required condition can't be evaluated on the latest bar

On the latest bar a confirmed rule is further labelled
    INVALIDATED   an invalidation condition is true
    STALE         the confirmed run started more than stale_after_bars bars ago
    OVEREXTENDED  |close − trigger close| / ATR(14) at the trigger, or |close − reference
                  average| / ATR(14), exceeds overextended_atr_multiple
and a rule that was confirmed within the last stale_after_bars bars but no longer is
becomes INVALIDATED.

Every study is causal (rolling windows, EMAs, positive shifts), so the verdict on a bar
uses only that bar and earlier ones: no look-ahead.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..history.explorer import CondEval, _fmt, evaluate_condition, explain, measure
from ..live.expr import Evaluator, ExprError, parse
from ..live.indicators import bar_functions, ema, sma
from .config import Rule

SCORE_FORMULA = ("score = 100 × Σ(weight × credit) / Σ(weight); credit = 1 pass, 1 − shortfall ÷ tolerance "
                 "(max 0.99) near, 0 fail or unavailable")
BASE = ("CONFIRMED", "NEAR_CONFIRMATION", "WATCH", "NOT_PRESENT", "INSUFFICIENT_DATA")


def indicator_version() -> str:
    """Hash of the expression engine and indicator code: a change here changes every result."""
    root = Path(__file__).resolve().parents[1]
    blob = b"".join((root / f).read_bytes() for f in ("live/expr.py", "live/indicators.py", "history/explorer.py",
                                                       "overlay/rules.py"))
    return hashlib.sha256(blob).hexdigest()[:10]


# ------------------------------------------------------------------ data helpers
def to_weekly(bars: pd.DataFrame, last_session) -> pd.DataFrame:
    """Weekly bars (week ending Friday) from daily bars; a week not yet finished is dropped."""
    if bars.empty:
        return bars
    wk = bars.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    wk = wk.dropna(subset=["close"])
    if len(wk) and wk.index[-1].date() > last_session:
        wk = wk.iloc[:-1]
    return wk


def overlay_extras(bars: pd.DataFrame, bench: pd.DataFrame | None, sector: pd.DataFrame | None) -> tuple[dict, dict]:
    """Names and functions the overlay adds: benchmark and sector series aligned on this symbol's
    dates. Dates the benchmark lacks stay NaN (never filled), so conditions using them are unavailable."""
    c = bars["close"].astype(float)
    nan = pd.Series(np.nan, index=bars.index)
    b = bench["close"].astype(float).reindex(bars.index) if bench is not None and not bench.empty else nan
    s = sector["close"].astype(float).reindex(bars.index) if sector is not None and not sector.empty else nan

    def _rs(other):
        def f(n=20):
            n = int(n)
            return 100 * (c / c.shift(n) - 1) - 100 * (other / other.shift(n) - 1)
        return f

    names = {"bench_close": b, "rs_line": c / b, "sector_close": s}
    funcs = {"rs": _rs(b), "sector_rs": _rs(s)}
    return names, funcs


def make_evaluator(bars: pd.DataFrame, timeframe: str, bench=None, sector=None) -> Evaluator:
    names, funcs = bar_functions(bars, timeframe)
    xn, xf = overlay_extras(bars, bench, sector)
    return Evaluator({**names, **xn}, {**funcs, **xf}, memo={})  # one evaluator per series: studies computed once


# ------------------------------------------------------------------ results
@dataclass
class RuleResult:
    rule_id: str
    rule_name: str
    symbol: str
    timeframe: str
    timestamp: pd.Timestamp | None
    status: str
    base_status: str
    score: float
    prev_score: float
    prev_status: str
    passed: int
    near: int
    failed: int
    unavailable: list[str]
    n_conditions: int
    minimum_score: float
    minimum_pass_count: int
    direction: str
    group: str
    kind: str
    tab_scope: list[str]
    conditions: list[dict]
    missing: list[str]
    explanation: str
    transition: str | None = None
    bars_since: int | None = None
    trigger_time: pd.Timestamp | None = None
    move_since_pct: float | None = None
    atr_move: float | None = None
    ref_distance_atr: float | None = None
    close: float | None = None
    data_source: str = ""
    data_timestamp: str = ""
    data_flags: list[str] = field(default_factory=list)   # STALE_DATA, DATA_ERROR ...
    reason_code: str = ""
    versions: dict = field(default_factory=dict)
    formula: str = SCORE_FORMULA
    states: np.ndarray | None = field(default=None, repr=False)
    scores: np.ndarray | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("states"), d.pop("scores")
        for k in ("timestamp", "trigger_time"):
            d[k] = str(d[k]) if d[k] is not None else None
        return d


def _lhs_op(e: CondEval) -> tuple[str, str]:
    body = parse(e.cond.expression).body
    if e.kind == "compare":
        return ast.unparse(body.left), e.extra["op"]
    if e.kind == "cross":
        return ast.unparse(body.args[0]), "crosses " + ("above" if e.extra["dir"] == ">" else "below")
    return e.cond.expression, ""


def _needed(e: CondEval, i: int) -> str:
    """Fixed sentence: what this condition still needs on bar i."""
    st, c = e.status[i], e.cond
    if st == "n/a":
        return f"{c.id}: unavailable ({c.expression} has no value yet: warm-up or missing data)"
    lhs, op = _lhs_op(e)
    if e.kind == "compare":
        return f"{c.id}: {lhs} is {_fmt(e.observed[i])}; the configured requirement is {op} {_fmt(e.target[i])}"
    if e.kind == "between":
        return f"{c.id}: {lhs} is {_fmt(e.observed[i])}; the configured requirement is {e.relation}"
    if e.kind == "cross":
        return f"{c.id}: {lhs} is {_fmt(e.observed[i])} vs {_fmt(e.target[i])}; needs to {op} it on a completed bar"
    return f"{c.id}: needs `{c.expression}` to be true on a completed bar"


def _states(rule: Rule, evals: list[CondEval], score: np.ndarray, th: dict) -> np.ndarray:
    n = len(score)
    status = np.vstack([e.status for e in evals])
    req = np.array([e.cond.required for e in evals])
    req_eval = np.vstack([e.evaluable for e in evals])[req].all(0) if req.any() else np.ones(n, bool)
    req_pass = (status[req] == "pass").all(0) if req.any() else np.ones(n, bool)
    req_near = np.isin(status[req], ["pass", "near"]).all(0) if req.any() else np.ones(n, bool)
    n_pass = (status == "pass").sum(0)
    min_score = rule.minimum_score if rule.minimum_score is not None else float(th["confirmed_match_score"])
    confirmed = (req_pass if rule.required_must_pass else req_near) & (score >= min_score - 1e-9) & (
        n_pass >= rule.minimum_pass_count)
    near = ~confirmed & req_near & (score >= float(th["near_match_score"]) - 1e-9)
    watch = score >= float(th["watch_score"]) - 1e-9
    out = np.where(confirmed, "CONFIRMED", np.where(near, "NEAR_CONFIRMATION", np.where(watch, "WATCH", "NOT_PRESENT")))
    return np.where(req_eval, out, "INSUFFICIENT_DATA").astype(object)


def evaluate_rule(rule: Rule, bars: pd.DataFrame, ev: Evaluator, symbol: str, th: dict, versions: dict,
                  source: str = "") -> RuleResult:
    n = len(bars)
    evals = [evaluate_condition(c, ev, bars.index) for c in rule.conditions]
    for e in evals:
        measure(e)
    w = np.array([e.cond.weight for e in evals], float)
    score = 100 * (w[:, None] * np.vstack([e.credit for e in evals])).sum(0) / w.sum()
    states = _states(rule, evals, score, th)
    i = n - 1
    min_score = rule.minimum_score if rule.minimum_score is not None else float(th["confirmed_match_score"])
    status_i = np.array([e.status[i] for e in evals])
    n_pass, n_near = int((status_i == "pass").sum()), int((status_i == "near").sum())
    unavailable = [e.cond.id for e in evals if e.status[i] == "n/a"]
    rows = explain(evals, i)
    for r, e in zip(rows, evals):
        r["lhs"] = _lhs_op(e)[0]
        r["prev_status"] = e.status[i - 1] if n > 1 else None
        r["prev_observed"] = float(e.observed[i - 1]) if n > 1 else float("nan")
        r["required_value"] = (f"{_lhs_op(e)[1]} {_fmt(e.target[i])}" if e.kind == "compare" else e.relation)
        r["needed"] = "" if e.status[i] == "pass" else _needed(e, i)
    base = states[i]
    status, reason = base, base.lower()
    c = bars["close"].astype(float).to_numpy()
    a = np.asarray(ev("atr(14)"), float)          # memoised: computed once per symbol, shared by every rule
    try:
        ref = np.asarray(ev(rule.reference_average), float) if rule.reference_average else np.full(n, np.nan)
    except ExprError:
        ref = np.full(n, np.nan)
    inval_hits = []
    for expr in rule.invalidation:
        try:
            v = ev(expr)
            if bool(np.asarray(v.fillna(False) if isinstance(v, pd.Series) else v).reshape(-1)[-1]):
                inval_hits.append(expr)
        except ExprError:
            pass
    stale_n = int(th["stale_after_bars"])
    mult = float(th["overextended_atr_multiple"])
    bars_since = trig = move = atr_move = ref_dist = None
    if base == "CONFIRMED":
        j = i
        while j > 0 and states[j - 1] == "CONFIRMED":
            j -= 1
        bars_since, trig = i - j, bars.index[j]
        move = 100 * (c[i] / c[j] - 1)
        atr_move = (c[i] - c[j]) / a[j] if np.isfinite(a[j]) and a[j] > 0 else None
        ref_dist = (c[i] - ref[i]) / a[i] if np.isfinite(ref[i]) and np.isfinite(a[i]) and a[i] > 0 else None
        if inval_hits:
            status, reason = "INVALIDATED", "invalidation_condition"
        elif bars_since > stale_n:
            status, reason = "STALE", "stale_signal"
        elif (atr_move is not None and abs(atr_move) > mult) or (ref_dist is not None and abs(ref_dist) > mult):
            status, reason = "OVEREXTENDED", "overextended"
    elif base != "INSUFFICIENT_DATA":
        for k in range(1, min(stale_n, i) + 1):
            if states[i - k] == "CONFIRMED":
                status, reason, bars_since, trig = "INVALIDATED", "confirmation_lost", k, bars.index[i - k]
                break
    prev = states[i - 1] if n > 1 else "NOT_PRESENT"
    transition = None
    if base == "CONFIRMED" and prev == "NEAR_CONFIRMATION":
        transition = "near_to_confirmed"
    elif base == "CONFIRMED" and prev != "CONFIRMED":
        transition = "new_confirmed"
    elif prev == "CONFIRMED" and base != "CONFIRMED":
        transition = "invalidated"
    elif base == "NEAR_CONFIRMATION" and prev in ("WATCH", "NOT_PRESENT"):
        transition = "new_near"
    missing = [r["needed"] for r in rows if r["needed"]]
    if base != "CONFIRMED":
        if n_pass < rule.minimum_pass_count:
            missing.append(f"at least {rule.minimum_pass_count} passing conditions (now {n_pass})")
        if score[i] < min_score:
            missing.append(f"a score of {min_score:g} (now {score[i]:.0f})")
    res = RuleResult(rule.id, rule.name, symbol, rule.timeframe, bars.index[i] if n else None, status, base,
                     round(float(score[i]), 1), round(float(score[i - 1]), 1) if n > 1 else float("nan"), prev,
                     n_pass, n_near, len(evals) - n_pass - n_near - len(unavailable), unavailable, len(evals),
                     min_score, rule.minimum_pass_count, rule.direction, rule.group, rule.kind, rule.tab_scope, rows,
                     missing, "", transition, bars_since, trig, move, atr_move, ref_dist, float(c[i]),
                     source, str(pd.Timestamp(bars.index[i]).date()) if n else "", [], reason, versions,
                     states=states, scores=score)
    res.explanation = explanation(res, th, inval_hits, rule)
    return res


def explanation(r: RuleResult, th: dict, inval_hits: list[str], rule: Rule) -> str:
    """Fixed templates filled with calculated values. No generated text."""
    when = f"{pd.Timestamp(r.timestamp):%Y-%m-%d}" if r.timestamp is not None else "the latest bar"
    head = f"{r.passed} of {r.n_conditions} conditions pass, score {r.score:.0f}"
    need = ("; ".join(r.missing[:4]) + ("" if len(r.missing) <= 4 else f"; and {len(r.missing) - 4} more")) if r.missing else ""
    if r.status == "INSUFFICIENT_DATA":
        return (f"Insufficient data on {when}: {', '.join(r.unavailable)} can't be evaluated (warm-up or missing data). "
                "Nothing is assumed in its place.")
    if r.status == "CONFIRMED":
        age = "on this bar" if not r.bars_since else f"{r.bars_since} bar(s) ago"
        return f"Confirmed on the {when} close: {head} (needs {r.minimum_score:g}). First confirmed {age}."
    if r.status == "STALE":
        return (f"Confirmed, but stale: the trigger was {r.bars_since} bars ago ({pd.Timestamp(r.trigger_time):%Y-%m-%d}); "
                f"the freshness limit is {th['stale_after_bars']} bars. Price has moved {r.move_since_pct:+.1f}%"
                + (f" ({r.atr_move:+.1f} ATR)" if r.atr_move is not None else "") + " since.")
    if r.status == "OVEREXTENDED":
        parts = []
        if r.atr_move is not None:
            parts.append(f"{r.atr_move:+.1f} ATR from the trigger close")
        if r.ref_distance_atr is not None:
            parts.append(f"{r.ref_distance_atr:+.1f} ATR from {rule.reference_average}")
        return (f"Confirmed, but overextended: price is {' and '.join(parts)}; the limit is "
                f"{th['overextended_atr_multiple']:g} ATR.")
    if r.status == "INVALIDATED":
        if inval_hits:
            return f"Invalidated on {when}: invalidation condition {' / '.join(inval_hits)} is true."
        return (f"Invalidated on {when}: confirmed {r.bars_since} bar(s) earlier, now {head}. "
                + (f"Failing: {need}." if need else ""))
    if r.status == "NEAR_CONFIRMATION":
        return f"Near confirmation: {head}. Confirmation requires: {need}."
    if r.status == "WATCH":
        return f"Watch: {head}. Still needed: {need}."
    return f"Not present: {head}."


# ------------------------------------------------------------------ alignment
def trend_state(close: pd.Series, fast: pd.Series, slow: pd.Series) -> str:
    if not len(close) or not np.isfinite([close.iloc[-1], fast.iloc[-1], slow.iloc[-1]]).all():
        return "Unavailable"
    if close.iloc[-1] > slow.iloc[-1] and fast.iloc[-1] > slow.iloc[-1]:
        return "Up"
    if close.iloc[-1] < slow.iloc[-1] and fast.iloc[-1] < slow.iloc[-1]:
        return "Down"
    return "Mixed"


def timeframe_alignment(daily: pd.DataFrame, weekly: pd.DataFrame) -> dict:
    """Three separate states. Higher: weekly close and 5-week EMA vs the 10-week SMA. Intermediate:
    daily close and 21-day EMA vs the 50-day SMA. Lower (trigger): daily close and 9-day EMA vs 21-day EMA."""
    wc, dc = weekly["close"].astype(float), daily["close"].astype(float)
    hi = trend_state(wc, ema(wc, 5), sma(wc, 10)) if len(wc) >= 10 else "Unavailable"
    mid = trend_state(dc, ema(dc, 21), sma(dc, 50))
    lo = trend_state(dc, ema(dc, 9), ema(dc, 21))
    states = {hi, mid, lo}
    agree = (f"Aligned {hi.lower()}" if len(states) == 1 and hi in ("Up", "Down")
             else "Unavailable" if "Unavailable" in states else "Not aligned")
    return {"higher": hi, "intermediate": mid, "lower": lo, "agreement": agree}
