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
    if isinstance(body, ast.Call) and body.func.id == "between" and len(body.args) == 3:
        x, lo, hi = (_numeric(ev.node(a), index) for a in body.args)
        return CondEval(cond, "between", f"between {ast.unparse(body.args[1])} and {ast.unparse(body.args[2])}",
                        passed & evaluable, evaluable, x, nan.copy(), extra={"lo": lo, "hi": hi, "ops": ["<=", "<="]})
    if isinstance(body, ast.Call) and body.func.id in CROSS_FUNCS and len(body.args) == 2:
        a, b = _numeric(ev.node(body.args[0]), index), _numeric(ev.node(body.args[1]), index)
        word = "above" if body.func.id == "crosses_above" else "below"
        return CondEval(cond, "cross", f"crosses {word} {ast.unparse(body.args[1])}", passed & evaluable,
                        evaluable, a, b, extra={"dir": CROSS_FUNCS[body.func.id]})
    why = ("combines several rules with and/or/not" if isinstance(body, (ast.BoolOp, ast.UnaryOp))
           else "describes behaviour over several bars" if isinstance(body, ast.Call) and body.func.id in (
               "rising", "falling", "increasing", "decreasing", "stayed_above", "stayed_below",
               "crossed_above_within", "crossed_below_within", "touched")
           else "has no single measurable distance from passing")
    return CondEval(cond, "boolean", "is true", passed & evaluable, evaluable, nan.copy(), nan.copy(),
                    note=f"pass/fail only: it {why}")


def evaluate_sequence(strategy: Strategy, ev: Evaluator, index) -> CondEval:
    """Ordered steps. Step 1 starts a sequence at its bar; step k must then be true on a bar 0..within_bars
    bars after step k-1's bar (the same bar counts). The sequence passes on its completion bar: the
    first bar where the last step lands. Every path is considered, not just the earliest match per
    step, and only bars up to the completion bar are used, so there's no look-ahead. When several
    starts complete on the same bar, the latest start is reported (the tightest sequence)."""
    n = len(index)
    steps, settled = [], np.ones(n, bool)
    for st in strategy.sequence:
        v = ev(st.expression)
        ok = _settled(parse(st.expression).body, ev, index)
        settled &= ok
        steps.append((v.fillna(0).astype(bool).to_numpy() if isinstance(v, pd.Series) else np.full(n, bool(v))) & ok)
    complete = np.zeros(n, bool)
    start_at = np.full(n, -1)
    step_bars: dict[int, list[int]] = {}
    for i in np.flatnonzero(steps[0]):
        frontier = {int(i): [int(i)]}            # bar of the latest step -> bars of each step so far
        for k, st in enumerate(strategy.sequence[1:], start=1):
            nxt = {}
            for pos, path in sorted(frontier.items()):
                hits = np.flatnonzero(steps[k][pos:min(n, pos + st.within_bars + 1)]) + pos
                for j in hits:
                    nxt.setdefault(int(j), path + [int(j)])   # earliest path reaching j wins
            frontier = nxt
            if not frontier:
                break
        if frontier:
            done = min(frontier)                 # first completion for this start
            if not complete[done] or start_at[done] < i:
                complete[done], start_at[done], step_bars[done] = True, i, frontier[done]
    w = strategy.sequence_weight or (np.mean([c.weight for c in strategy.conditions]) if strategy.conditions else 1.0)
    cond = Condition("sequence", " -> ".join(st.id for st in strategy.sequence), True, float(w))
    nan = np.full(n, np.nan)
    e = CondEval(cond, "sequence", "completes in order", complete, settled, nan, nan.copy(),
                 note="pass/fail only: a sequence either completed on this bar or didn't")
    e.extra.update({"start": start_at, "steps": step_bars})
    return e


def evaluate_symbol(strategy: Strategy, bars: pd.DataFrame) -> list[CondEval]:
    names, funcs = bar_functions(bars, strategy.timeframe)
    ev = Evaluator(names, funcs, memo={})  # each study computed once, shared by all conditions
    evals = [evaluate_condition(c, ev, bars.index) for c in strategy.conditions]
    if strategy.sequence:
        evals.append(evaluate_sequence(strategy, ev, bars.index))
    return evals


def _near_credit(shortfall: np.ndarray, tol: float) -> np.ndarray:
    """Linear partial credit strictly inside the tolerance: 1 - shortfall / tolerance, capped at 0.99."""
    return np.minimum(NEAR_CAP, 1 - shortfall / tol)


def _gap(raw: np.ndarray, base: np.ndarray, ttype: str) -> np.ndarray:
    """Distance from passing in the tolerance's units: absolute, or % of the compared value."""
    raw = np.maximum(raw, 0.0)
    if ttype == "percent_distance":
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(np.abs(base) > 0, 100 * raw / np.abs(base), np.nan)
    return raw


def measure(e: CondEval) -> None:
    """Fill status / credit / shortfall for every bar.

    compare  a > b, a >= b: shortfall = b - a; a < b, a <= b: shortfall = a - b
    between  lo < x < hi: shortfall = lo - x below the range, x - hi above it
    cross    pass if it crosses on this bar; near ("recent") if it crossed k <= recent_bars bars
             ago and is still on the crossed side, credit 1 - k / (recent_bars + 1); near if it
             hasn't crossed and the gap is inside the tolerance
    boolean  pass/fail only
    percent_distance tolerances measure shortfall as % of the compared value (b, or the bound).
    Near = failing with 0 <= shortfall < tolerance (strictly inside). Equality counts as passing
    only where the rule says so (>= and <=); for > and < a tie is a near with 0.99 credit.
    """
    n = len(e.passed)
    tt, tol = e.cond.tolerance_type, e.cond.tolerance
    failing = e.evaluable & ~e.passed
    shortfall = np.full(n, np.nan)
    near = np.zeros(n, dtype=bool)
    credit = e.passed.astype(float)
    recent_k = np.zeros(n)
    if e.kind == "compare":
        op = e.extra["op"]
        raw = (e.target - e.observed) if op.startswith(">") else (e.observed - e.target)
        shortfall = _gap(raw, e.target, tt)
    elif e.kind == "between":
        lo, hi = e.extra["lo"], e.extra["hi"]
        below = e.observed < lo + (0 if e.extra["ops"][0] == "<=" else 1e-12)
        raw = np.where(below, lo - e.observed, e.observed - hi)
        shortfall = _gap(raw, np.where(below, lo, hi), tt)
    elif e.kind == "cross":
        above = e.extra["dir"] == ">"
        side = (e.observed > e.target) if above else (e.observed < e.target)
        R = e.cond.recent_bars
        crossed = e.passed
        for k in range(1, R + 1):  # crossed k bars ago, nothing nearer, still on that side
            hit = failing & side & (recent_k == 0) & np.r_[np.zeros(k, bool), crossed[:-k]] if k < n else np.zeros(n, bool)
            recent_k[hit] = k
        rec = recent_k > 0
        near |= rec
        credit = np.where(rec, np.minimum(NEAR_CAP, 1 - recent_k / (R + 1)), credit)
        raw = (e.target - e.observed) if above else (e.observed - e.target)
        not_yet = failing & ~side
        shortfall = np.where(not_yet, _gap(raw, e.target, tt), np.nan)
    if tt != "none" and e.kind in ("compare", "between", "cross"):
        with np.errstate(invalid="ignore"):
            inside = failing & ~near & np.isfinite(shortfall) & (shortfall < tol)
        credit = np.where(inside, _near_credit(np.nan_to_num(shortfall), tol), credit)
        near |= inside
    if tt != "none" and e.kind in ("boolean", "sequence"):
        e.note += "; its tolerance is ignored"
    e.status = np.where(~e.evaluable, "n/a", np.where(e.passed, "pass", np.where(near, "near", "fail"))).astype(object)
    e.credit = np.where(e.evaluable, credit, 0.0)
    e.shortfall = shortfall
    e.extra["recent_k"] = recent_k


def score(strategy: Strategy, evals: list[CondEval]) -> dict:
    """Total = 100 x sum(weight x credit) / sum(weight), per bar."""
    for e in evals:
        measure(e)
    weights = np.array([e.cond.weight for e in evals], dtype=float)
    credit = np.vstack([e.credit for e in evals]) if evals else np.zeros((0, 0))
    total = 100 * (weights[:, None] * credit).sum(0) / weights.sum() if evals else np.zeros(0)
    return {"score": total}


def _fmt(x) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:,.4g}" if abs(x) < 1e5 else f"{x:,.0f}"


def explain(evals: list[CondEval], i: int) -> list[dict]:
    """Condition-by-condition account of one bar, in plain English."""
    total_w = sum(e.cond.weight for e in evals)
    out = []
    for e in evals:
        c, st = e.cond, e.status[i]
        pts = 100 * e.credit[i] * c.weight / total_w
        full = 100 * c.weight / total_w
        lhs = ast.unparse(parse(c.expression).body.left) if e.kind in ("compare",) else (
            ast.unparse(parse(c.expression).body.comparators[0]) if e.kind == "between" else (
                ast.unparse(parse(c.expression).body.args[0]) if e.kind == "cross" else c.expression))
        unit = "%" if c.tolerance_type == "percent_distance" else ""
        tol_txt = f"{c.tolerance:g}{unit}" if c.tolerance_type != "none" else "none"
        obs = f"{lhs} = {_fmt(e.observed[i])}" if e.kind != "boolean" else c.expression
        tgt = "" if e.kind in ("boolean", "between") else f" vs {_fmt(e.target[i])}"
        if e.kind == "sequence":
            lhs, obs, tgt = c.expression, c.expression, ""
        if st == "n/a":
            text = "not evaluable at this bar (indicator warm-up or missing data)"
        elif e.kind == "sequence":
            text = ("completed on this bar" if st == "pass" else "didn't complete on this bar") + " (pass/fail only)"
        elif e.kind == "boolean":
            text = f"{'true' if st == 'pass' else 'false'} ({e.note})"
        elif st == "pass":
            text = f"{obs}{tgt}: rule {e.relation} holds"
        elif st == "near" and e.kind == "cross" and e.extra["recent_k"][i]:
            k = int(e.extra["recent_k"][i])
            text = f"{obs}{tgt}: crossed {k} bar{'s' if k > 1 else ''} ago and is still on that side (recent)"
        else:
            sf = e.shortfall[i]
            gap = f"{_fmt(sf)}{unit} from passing" if np.isfinite(sf) else "already past the level without a cross on this bar"
            inside = "inside" if st == "near" else "outside"
            text = f"{obs}{tgt}: rule {e.relation} fails by {gap}; {inside} the {tol_txt} tolerance" \
                if c.tolerance_type != "none" or not np.isfinite(sf) else f"{obs}{tgt}: rule {e.relation} fails by {gap}; no tolerance set"
        out.append({"id": c.id, "expression": c.expression, "required": c.required, "weight": c.weight,
                    "kind": e.kind, "status": st, "observed": e.observed[i], "compared_with": e.target[i],
                    "shortfall": e.shortfall[i], "tolerance": tol_txt, "credit": round(float(e.credit[i]), 4),
                    "points": round(pts, 2), "max_points": round(full, 2),
                    "explanation": f"{text} -> {st}, {pts:.1f} of {full:.1f} points"})
    return out


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
    for e in evals:
        if e.kind == "sequence":
            st = e.extra["start"][idx]
            rows["sequence_start"] = pd.DatetimeIndex([bars.index[j] if j >= 0 else pd.NaT for j in st])
    out = pd.DataFrame(rows)
    out["explanation"] = [_summary(evals, i) for i in idx]
    return out


def _summary(evals: list[CondEval], i: int) -> str:
    parts = []
    for e in evals:
        st = e.status[i]
        if st == "pass":
            continue
        detail = ""
        if e.kind in ("compare", "between", "cross") and np.isfinite(e.observed[i]):
            if e.kind == "cross" and e.extra["recent_k"][i]:
                detail = f" (crossed {int(e.extra['recent_k'][i])} bars ago)"
            else:
                detail = f" ({_fmt(e.observed[i])} vs {e.relation})"
        parts.append(f"{e.cond.id} {st}{detail}")
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
