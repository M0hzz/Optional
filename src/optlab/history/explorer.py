"""Historical Signal Explorer: when did a set of conditions match, or nearly match?

For each symbol: load bars (with warm-up), evaluate every condition once over the whole
series with the same expression engine scans.yaml uses, then classify each completed bar
inside the range:

    exact   every condition passes
    near    not exact, but required conditions are satisfied (strict: all pass;
            tolerant: each passes or is near), score >= minimum_score and at least
            minimum_pass_count conditions pass

Score = 100 x sum(weight x credit) / sum(weight). Credit: pass 1, near 1 - shortfall /
tolerance (capped at 0.99, so a near never earns full weight), fail 0. How shortfall is
measured depends on the condition's shape; see `measure`.

No look-ahead: every study is causal (rolling windows, EMAs, positive shifts only), and a
bar's verdict uses only bars up to and including it. Bars where any study is still
warming up (NaN) are "not evaluable" and never match.

Information and education only: this describes what happened, it recommends nothing.
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..live.expr import Evaluator, ExprError, parse
from ..live.indicators import bar_functions
from .bars import DataReport, load_history
from .strategy import Condition, Strategy

CROSS_FUNCS = {"crosses_above": ">", "crosses_below": "<"}
NEAR_CAP = 0.99
_OPS = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<="}


@dataclass
class CondEval:
    """One condition evaluated at every bar of one symbol."""
    cond: Condition
    kind: str                     # compare | between | cross | boolean
    relation: str                 # human form of the rule, e.g. ">= 1.5" or "crosses above ema(21)"
    passed: np.ndarray
    evaluable: np.ndarray
    observed: np.ndarray          # left-hand value (NaN for boolean conditions)
    target: np.ndarray            # what it is compared with (NaN when not measurable)
    status: np.ndarray = None     # pass | near | fail | n/a
    credit: np.ndarray = None
    shortfall: np.ndarray = None  # distance from passing, in the tolerance's units
    note: str = ""                # why a condition is pass/fail only, if it is
    extra: dict = field(default_factory=dict)


def _numeric(x, index) -> np.ndarray:
    if isinstance(x, pd.Series):
        return x.astype(float).to_numpy()
    if isinstance(x, np.ndarray):
        return x.astype(float)
    return np.full(len(index), float(x))


def _settled(node, ev: Evaluator, index) -> np.ndarray:
    """True where every study inside the expression has a value (past its warm-up)."""
    ok = np.ones(len(index), dtype=bool)
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Call, ast.Name)) and not (isinstance(sub, ast.Name) and sub.id in ev.funcs):
            try:
                v = ev.node(sub)
            except ExprError:
                continue
            if isinstance(v, pd.Series) and v.dtype != bool:
                ok &= v.notna().to_numpy()
    return ok


def evaluate_condition(cond: Condition, ev: Evaluator, index) -> CondEval:
    tree = parse(cond.expression)
    body = tree.body
    passed_raw = ev(cond.expression)
    passed = (passed_raw.fillna(0).astype(bool).to_numpy() if isinstance(passed_raw, pd.Series)
              else np.full(len(index), bool(passed_raw)))
    evaluable = _settled(body, ev, index)
    nan = np.full(len(index), np.nan)
    if isinstance(body, ast.Compare) and len(body.ops) == 1 and type(body.ops[0]) in _OPS:
        left, right = _numeric(ev.node(body.left), index), _numeric(ev.node(body.comparators[0]), index)
        return CondEval(cond, "compare", f"{_OPS[type(body.ops[0])]} {ast.unparse(body.comparators[0])}",
                        passed & evaluable, evaluable, left, right, extra={"op": _OPS[type(body.ops[0])]})
    if (isinstance(body, ast.Compare) and len(body.ops) == 2 and all(type(o) in _OPS for o in body.ops)
            and {_OPS[type(o)][0] for o in body.ops} in ({"<"}, {">"})):
        lo_node, x_node, hi_node = body.left, body.comparators[0], body.comparators[1]
        ops = [_OPS[type(o)] for o in body.ops]
        if ops[0].startswith(">"):  # hi > x > lo  ->  lo < x < hi
            lo_node, hi_node, ops = hi_node, lo_node, ["<" + o[1:] for o in reversed(ops)]
        lo, x, hi = (_numeric(ev.node(n), index) for n in (lo_node, x_node, hi_node))
        return CondEval(cond, "between", f"between {ast.unparse(lo_node)} and {ast.unparse(hi_node)}",
                        passed & evaluable, evaluable, x, nan.copy(), extra={"lo": lo, "hi": hi, "ops": ops})
    if isinstance(body, ast.Call) and body.func.id in CROSS_FUNCS and len(body.args) == 2:
        a, b = _numeric(ev.node(body.args[0]), index), _numeric(ev.node(body.args[1]), index)
        word = "above" if body.func.id == "crosses_above" else "below"
        return CondEval(cond, "cross", f"crosses {word} {ast.unparse(body.args[1])}", passed & evaluable,
                        evaluable, a, b, extra={"dir": CROSS_FUNCS[body.func.id]})
    why = ("combines several rules with and/or/not" if isinstance(body, (ast.BoolOp, ast.UnaryOp))
           else "has no single measurable distance from passing")
    return CondEval(cond, "boolean", "is true", passed & evaluable, evaluable, nan.copy(), nan.copy(),
                    note=f"pass/fail only: it {why}")


def evaluate_symbol(strategy: Strategy, bars: pd.DataFrame) -> list[CondEval]:
    names, funcs = bar_functions(bars, strategy.timeframe)
    ev = Evaluator(names, funcs, memo={})  # each study computed once, shared by all conditions
    return [evaluate_condition(c, ev, bars.index) for c in strategy.conditions]


def score(strategy: Strategy, evals: list[CondEval]) -> dict:
    """Phase 1: pass/fail per condition; status and credit arrays for every bar."""
    for e in evals:
        e.status = np.where(~e.evaluable, "n/a", np.where(e.passed, "pass", "fail")).astype(object)
        e.credit = e.passed.astype(float)
        e.shortfall = np.full(len(e.passed), np.nan)
    weights = np.array([e.cond.weight for e in evals], dtype=float)
    credit = np.vstack([e.credit for e in evals]) if evals else np.zeros((0, 0))
    total = 100 * (weights[:, None] * credit).sum(0) / weights.sum() if evals else np.zeros(0)
    return {"score": total}


@dataclass
class SearchResult:
    strategy: Strategy
    results: pd.DataFrame
    reports: dict[str, DataReport]
    bars: dict[str, pd.DataFrame]
    evals: dict[str, list]
    seconds: float = 0.0
    bars_evaluated: int = 0
    not_evaluable: int = 0
    errors: list[str] = field(default_factory=list)


def classify(strategy: Strategy, symbol: str, bars: pd.DataFrame, in_range: np.ndarray, evals: list[CondEval],
             scores: dict) -> pd.DataFrame:
    n = len(bars)
    if not evals or not n:
        return pd.DataFrame()
    status = np.vstack([e.status for e in evals])
    evaluable = np.vstack([e.evaluable for e in evals]).all(0)
    n_pass, n_near = (status == "pass").sum(0), (status == "near").sum(0)
    n_fail = len(evals) - n_pass - n_near
    req = np.array([e.cond.required for e in evals])
    req_ok_strict = (status[req] == "pass").all(0) if req.any() else np.ones(n, bool)
    req_ok_tol = np.isin(status[req], ["pass", "near"]).all(0) if req.any() else np.ones(n, bool)
    req_ok = req_ok_strict if strategy.required_mode == "strict" else req_ok_tol
    exact = evaluable & (n_pass == len(evals))
    near = (evaluable & ~exact & req_ok & (scores["score"] >= strategy.minimum_score)
            & (n_pass >= strategy.minimum_pass_count))
    keep = in_range & ((exact & strategy.include_exact) | (near & strategy.include_near))
    idx = np.flatnonzero(keep)
    rows = {
        "symbol": symbol, "timestamp": bars.index[idx], "timeframe": strategy.timeframe,
        "match_type": np.where(exact[idx], "exact", "near"), "score": np.round(scores["score"][idx], 1),
        "passed": n_pass[idx], "near": n_near[idx], "failed": n_fail[idx],
        "required": np.where(req_ok_strict[idx], "all pass", np.where(req_ok_tol[idx], "near", "failed")),
    }
    for e in evals:
        rows[e.cond.id] = e.status[idx]
        rows[f"{e.cond.id}_value"] = np.round(e.observed[idx], 4)
        rows[f"{e.cond.id}_score"] = np.round(100 * e.credit[idx] * e.cond.weight / sum(x.cond.weight for x in evals), 1)
    out = pd.DataFrame(rows)
    out["explanation"] = [_summary(evals, i) for i in idx]
    return out


def _summary(evals: list[CondEval], i: int) -> str:
    parts = [f"{e.cond.id} {e.status[i]}" for e in evals if e.status[i] != "pass"]
    return "all conditions pass" if not parts else "; ".join(parts)


def run_search(strategy: Strategy, archive_path=None, now=None, progress=None,
               bars_override: dict[str, pd.DataFrame] | None = None) -> SearchResult:
    """Search every symbol. `progress(i, n, symbol)` is called before each symbol.
    `bars_override` supplies bars directly (tests, or bars loaded elsewhere): {symbol: frame}."""
    t0 = time.time()
    res = SearchResult(strategy, pd.DataFrame(), {}, {}, {})
    frames = []
    for i, sym in enumerate(strategy.symbols):
        if progress:
            progress(i, len(strategy.symbols), sym)
        try:
            if bars_override is not None:
                bars = bars_override[sym]
                in_range = _in_range(bars, strategy)
                report = DataReport(sym, strategy.timeframe, "provided", strategy.timezone, strategy.start,
                                    strategy.end, bars_in_range=int(in_range.sum()))
            else:
                kw = {"archive_path": archive_path} if archive_path else {}
                bars, in_range_s, report = load_history(
                    sym, strategy.timeframe, strategy.start, strategy.end, strategy.timezone,
                    strategy.market_hours_only, strategy.warmup_bars, strategy.source, now=now, **kw)
                in_range = in_range_s.to_numpy() if len(in_range_s) else np.zeros(0, bool)
        except Exception as exc:
            res.errors.append(f"{sym}: couldn't load bars ({type(exc).__name__}: {exc})")
            continue
        res.reports[sym], res.bars[sym] = report, bars
        if bars.empty or not in_range.any():
            continue
        try:
            evals = evaluate_symbol(strategy, bars)
        except ExprError as exc:
            res.errors.append(f"{sym}: {exc}")
            continue
        scores = score(strategy, evals)
        res.evals[sym] = evals
        evaluable = np.vstack([e.evaluable for e in evals]).all(0) if evals else np.zeros(len(bars), bool)
        res.bars_evaluated += int((in_range & evaluable).sum())
        res.not_evaluable += int((in_range & ~evaluable).sum())
        frames.append(classify(strategy, sym, bars, in_range, evals, scores))
    out = pd.concat([f for f in frames if not f.empty], ignore_index=True) if any(
        not f.empty for f in frames) else pd.DataFrame()
    res.results = sort_limit(out, strategy)
    res.seconds = time.time() - t0
    return res


def _in_range(bars: pd.DataFrame, strategy: Strategy) -> np.ndarray:
    from .bars import to_local

    t0, t1 = to_local(strategy.start, strategy.timezone), to_local(strategy.end, strategy.timezone, end_of_day=True)
    return np.asarray((bars.index >= t0) & (bars.index <= t1))


def sort_limit(df: pd.DataFrame, strategy: Strategy) -> pd.DataFrame:
    if df.empty:
        return df
    key = {"timestamp": ["timestamp", "symbol"], "score": ["score", "timestamp"], "symbol": ["symbol", "timestamp"]}
    asc = strategy.order == "ascending"
    df = df.sort_values(key[strategy.sort_by], ascending=[asc, True] if strategy.sort_by != "timestamp" else asc,
                        kind="stable")
    return df.head(strategy.limit).reset_index(drop=True)


def to_csv(df: pd.DataFrame) -> str:
    out = df.copy()
    if "timestamp" in out:
        out["timestamp"] = out["timestamp"].astype(str)  # keeps the UTC offset, so the timezone is unambiguous
    return out.to_csv(index=False)
