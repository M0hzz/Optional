"""Historical analysis runs: validate the form, search, compute the statistics, store, and serve drill-downs.

A run is created `queued`, executed in a background thread of the API (`running`), and ends `done` or
`error`. A finished run is never recomputed: running again (even unchanged) creates a new run, so what you
saw is always what's stored, with its strategy version and data source.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import numpy as np
import pandas as pd
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..history.bars import load_history
from ..history.explorer import run_search
from ..history.forward import forward_behavior
from ..history.strategy import StrategyError, Strategy, parse_strategy
from ..portfolio.models import AnalysisRun, utcnow
from ..portfolio.schemas import Strict
from ..portfolio.service import DomainError
from . import stats
from .drafts import SUBJECTS, horizon_bars

log = logging.getLogger("optlab.analysis")
MAX_SYMBOLS, MAX_MATCHES_STORED = 10, 2000
SPAN_LIMITS = {"1m": 30, "5m": 60, "15m": 60, "30m": 60, "1h": 730, "1d": 365 * 40, "1wk": 365 * 60}
BENCHMARK = "SPY"


class ConditionIn(Strict):
    id: Annotated[str, Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_]+$")]
    expression: Annotated[str, Field(min_length=1, max_length=400)]
    origin: Annotated[str | None, Field(max_length=40)] = None
    required: bool = True
    weight: Annotated[float, Field(gt=0, le=100)] = 1.0
    tolerance_type: Literal["none", "absolute", "percent_distance"] = "none"
    tolerance: Annotated[float, Field(ge=0, le=1_000_000)] = 0.0


class AnalysisIn(Strict):
    subject_type: Literal["plan", "position", "alert_rule", "contract", "event", "manual"] = "manual"
    subject_id: str | None = None
    name: Annotated[str, Field(min_length=1, max_length=120)]
    symbols: Annotated[list[Annotated[str, Field(max_length=12)]], Field(min_length=1, max_length=MAX_SYMBOLS)]
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "1d", "1wk"] = "1d"
    start: Annotated[str, Field(max_length=10)]
    end: Annotated[str, Field(max_length=10)]
    conditions: Annotated[list[ConditionIn], Field(min_length=1, max_length=20)]
    excluded: Annotated[list[dict], Field(max_length=50)] = []
    include_near: bool = True
    minimum_score: Annotated[float, Field(ge=0, le=100)] = 80.0
    required_mode: Literal["strict", "tolerant"] = "tolerant"
    horizons: Annotated[list[Annotated[str, Field(pattern=r"^\d{1,3}d?$")]], Field(min_length=1, max_length=8)]
    dedupe_bars: Annotated[int, Field(ge=0, le=10_000)] = 0
    direction: Literal["bullish", "bearish"] | None = None
    focus_horizon: Annotated[str | None, Field(pattern=r"^\d{1,3}d?$")] = None


def build_strategy(data: AnalysisIn, source: str) -> Strategy:
    """The explorer strategy for the form, validated (every problem reported at once)."""
    problems = []
    try:
        s0, s1 = pd.Timestamp(data.start), pd.Timestamp(data.end)
        if (s1 - s0).days > SPAN_LIMITS[data.timeframe]:
            problems.append({"field": "start", "code": "RANGE_TOO_LONG",
                             "message": f"{data.timeframe} bars are limited to {SPAN_LIMITS[data.timeframe]} days of history."})
    except ValueError:
        problems.append({"field": "start", "code": "BAD_DATE", "message": "Use YYYY-MM-DD dates."})
    if data.focus_horizon and data.focus_horizon not in data.horizons:
        problems.append({"field": "focus_horizon", "code": "NOT_A_HORIZON", "message": "The focus horizon must be one of the horizons."})
    spec = {"name": data.name, "symbols": data.symbols, "timeframe": data.timeframe,
            "range": {"start": data.start, "end": data.end}, "data": {"source": source},
            "conditions": [{"id": c.id, "expression": c.expression, "required": c.required, "weight": c.weight,
                            **({"tolerance": {"type": c.tolerance_type, "value": c.tolerance}} if c.tolerance_type != "none" else {})}
                           for c in data.conditions],
            "matching": {"minimum_score": data.minimum_score, "include_exact": True, "include_near": data.include_near,
                         "required_mode": data.required_mode},
            "output": {"sort_by": "timestamp", "order": "ascending", "limit": 5000},
            "forward": {"horizons": data.horizons, "dedupe_bars": data.dedupe_bars}}
    try:
        st = parse_strategy(spec)
    except StrategyError as exc:
        problems += [{"field": p.split(":")[0], "code": "INVALID", "message": p} for p in exc.problems]
        st = None
    if problems or st is None:
        raise DomainError("ANALYSIS_INVALID", "Fix these to run the analysis.", 422, field_errors=problems)
    if not st.warmup_bars:
        from ..history.strategy import auto_warmup

        st.warmup_bars = max(auto_warmup(st.expressions()), 260)        # 200-bar trend + ATR percentile labels
    return st


# ------------------------------------------------------------------ execution (pure: no database)
def execute(strategy: Strategy, *, direction: str | None, focus_horizon: str, bars_override: dict | None = None,
            bench_override: pd.DataFrame | None = None, prior_versions: int = 0, entry_at: str | None = None,
            now=None) -> tuple[dict, list[dict]]:
    t0 = time.time()
    search = run_search(strategy, now=now, bars_override=bars_override)
    res = search.results
    per_event, _summary = forward_behavior(res, search.bars, strategy.horizons, strategy.timeframe, strategy.dedupe_bars)
    deduped = int(_summary.attrs.get("deduped", 0)) if len(_summary) else 0
    if not per_event.empty:
        per_event["key"] = per_event["symbol"] + "|" + per_event["timestamp"].map(lambda x: pd.Timestamp(x).isoformat())
    d = stats.signed(per_event, direction)

    # regime labels at each signal, from that bar and earlier only
    bench = bench_override
    if bench is None:
        if BENCHMARK in search.bars:
            bench = search.bars[BENCHMARK]
        elif bars_override is None:
            try:
                bench, _, _ = load_history(BENCHMARK, strategy.timeframe, strategy.start, strategy.end, strategy.timezone,
                                           strategy.market_hours_only, 60, strategy.source, now=now)
            except Exception as exc:                       # the breakdown just loses its market column
                log.info("benchmark unavailable: %s", exc)
                bench = None
    frames = []
    for sym, g in (res.groupby("symbol") if not res.empty else []):
        bars = search.bars[sym]
        pos = bars.index.get_indexer(g["timestamp"])
        lab = stats.regime_labels(bars, pos, bench, strategy.timeframe)
        lab.index = [f"{sym}|{pd.Timestamp(t).isoformat()}" for t in g["timestamp"]]
        frames.append(lab)
    labels = pd.concat(frames) if frames else pd.DataFrame()

    hstats = [stats.horizon_stats(d, h, direction, grp) for h in strategy.horizons for grp in ("all", "exact", "near")] if len(d) else []
    focus = next((x for x in hstats if x["horizon"] == focus_horizon and x["group"] == "all"), None)
    trunc = stats.truncation_test(strategy, search.bars, res, search.evals)
    longest = max(horizon_bars(h, strategy.timeframe) for h in strategy.horizons)
    bias = stats.bias_checks(strategy=strategy, search=search, per_event=per_event if len(per_event) else pd.DataFrame({"complete": []}),
                             truncation=trunc, deduped=deduped, longest_bars=longest,
                             focus_signals=focus["signals"] if focus else 0, prior_versions=prior_versions, source=strategy.source)
    cov = stats.coverage(search)
    warnings = list(search.errors) + [f"{c['symbol']}: {w}" for c in cov for w in c["warnings"]]
    if res.empty:
        warnings.append("No bar matched these conditions in the range.")

    kept = set(per_event["key"]) if len(per_event) else set()
    fwd: dict[str, dict] = {}
    for r in d.itertuples() if len(d) else []:
        if r.complete:
            fwd.setdefault(r.key, {})[r.horizon] = {"return_pct": stats._f(r.forward_return_pct),
                                                    "favorable_pct": stats._f(r.favorable_pct),
                                                    "adverse_pct": stats._f(r.adverse_pct)}
    matches = []
    entry_day = pd.Timestamp(entry_at).tz_convert(strategy.timezone).normalize() if entry_at else None
    cond_ids = [c.id for c in strategy.conditions]
    for i, row in enumerate(res.head(MAX_MATCHES_STORED).itertuples(index=False)):
        rd = row._asdict()
        ts = pd.Timestamp(rd["timestamp"])
        key = f"{rd['symbol']}|{ts.isoformat()}"
        lab = labels.loc[key].to_dict() if key in labels.index else {}
        matches.append({"n": i, "key": key, "symbol": rd["symbol"], "timestamp": ts.isoformat(), "match_type": rd["match_type"],
                        "score": stats._f(rd["score"]), "passed": int(rd["passed"]), "near": int(rd["near"]), "failed": int(rd["failed"]),
                        "conditions": {c: {"status": rd.get(c), "value": stats._f(rd.get(f"{c}_value")),
                                           "score": stats._f(rd.get(f"{c}_score"))} for c in cond_ids},
                        "explanation": rd.get("explanation"), "thinned": key not in kept, "forward": fwd.get(key, {}),
                        "labels": lab,
                        "is_entry": bool(entry_day is not None and ts.tz_convert(strategy.timezone).normalize() == entry_day
                                         and rd["symbol"] in strategy.symbols)})
    result = {
        "version": stats.VERSION, "seconds": round(time.time() - t0, 2),
        "counts": {"matches": int(search.total_matches), "exact": int((res["match_type"] == "exact").sum()) if len(res) else 0,
                   "near": int((res["match_type"] == "near").sum()) if len(res) else 0, "thinned": deduped,
                   "bars_evaluated": int(search.bars_evaluated), "not_evaluable": int(search.not_evaluable),
                   "stored": len(matches)},
        "horizons": hstats, "distributions": [stats.distribution(d, h, direction) for h in strategy.horizons] if len(d) else [],
        "breakdowns": stats.breakdown(d, labels, focus_horizon, direction) if len(d) else [],
        "bias": bias, "coverage": cov, "warnings": warnings, "truncation": trunc,
        "definitions": stats.__doc__,
    }
    return stats.jsonable(result), stats.jsonable(matches)


# ------------------------------------------------------------------ runs
def prior_versions(s: Session, subject_type: str, subject_id: str | None, version: str) -> int:
    since = utcnow() - timedelta(days=30)
    q = select(func.count(func.distinct(AnalysisRun.strategy_version))).where(
        AnalysisRun.subject_type == subject_type, AnalysisRun.created_at >= since, AnalysisRun.strategy_version != version)
    q = q.where(AnalysisRun.subject_id == subject_id) if subject_id else q.where(AnalysisRun.subject_id.is_(None))
    return int(s.scalar(q) or 0)


def create_run(s: Session, data: AnalysisIn, source: str) -> AnalysisRun:
    if data.subject_type not in SUBJECTS:
        raise DomainError("BAD_SUBJECT", "Unknown subject type.", 422)
    st = build_strategy(data, source)
    focus = data.focus_horizon or data.horizons[min(1, len(data.horizons) - 1)]
    run = AnalysisRun(subject_type=data.subject_type, subject_id=data.subject_id, title=data.name,
                      strategy_json={**st.to_dict(), "origins": {c.id: c.origin for c in data.conditions}},
                      strategy_version=st.version, excluded_json=data.excluded, direction=data.direction,
                      focus_horizon=focus, source=source, status="queued")
    s.add(run)
    s.flush()
    return run


def _entry_at(s: Session, run: AnalysisRun) -> str | None:
    if run.subject_type != "position" or not run.subject_id:
        return None
    from ..portfolio.models import Position

    p = s.get(Position, run.subject_id)
    return p.opened_at.isoformat() if p else None


def run_job(db, run_id: str, *, bars_override: dict | None = None, bench_override=None, now=None) -> None:
    """Execute a stored run and save the outcome. Never raises: errors are stored on the run."""
    with db.session(write=True) as s:
        run = s.get(AnalysisRun, run_id)
        if run is None or run.status not in ("queued",):
            return
        run.status, run.started_at = "running", utcnow()
        spec = dict(run.strategy_json)
        origins = spec.pop("origins", {})
        strategy = parse_strategy(spec)
        strategy.warmup_bars = max(strategy.warmup_bars, 260)
        prior = prior_versions(s, run.subject_type, run.subject_id, run.strategy_version)
        entry = _entry_at(s, run)
        direction, focus = run.direction, run.focus_horizon
    t0 = time.time()
    try:
        result, matches = execute(strategy, direction=direction, focus_horizon=focus, bars_override=bars_override,
                                  bench_override=bench_override, prior_versions=prior, entry_at=entry, now=now)
        result["origins"] = origins
        status, error = "done", None
    except Exception as exc:                               # stored and shown; never a stack trace in the API
        log.exception("analysis %s failed", run_id)
        result, matches, status, error = {}, [], "error", f"{type(exc).__name__}: {str(exc)[:300]}"
    with db.session(write=True) as s:
        run = s.get(AnalysisRun, run_id)
        if run is None:
            return
        run.status, run.error, run.result_json, run.matches_json = status, error, result, matches
        run.finished_at, run.seconds = utcnow(), round(time.time() - t0, 2)


class Jobs:
    """One worker thread per API process: analyses run one at a time, off the request thread."""

    def __init__(self, db):
        self.db = db
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="optlab-analysis")
        self.lock = threading.Lock()

    def submit(self, run_id: str) -> None:
        self.pool.submit(run_job, self.db, run_id)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def run_out(r: AnalysisRun, full: bool = True) -> dict:
    out = {"id": r.id, "subject_type": r.subject_type, "subject_id": r.subject_id, "title": r.title,
           "strategy_version": r.strategy_version, "direction": r.direction, "focus_horizon": r.focus_horizon,
           "source": r.source, "status": r.status, "error": r.error, "created_at": r.created_at, "started_at": r.started_at,
           "finished_at": r.finished_at, "seconds": r.seconds,
           "counts": (r.result_json or {}).get("counts"),
           "strategy": (r.strategy_json or {}).get("historical_signal_search")}
    if full:
        out.update({"excluded": r.excluded_json, "result": r.result_json, "matches": r.matches_json})
    return out


def match_window(run: AnalysisRun, n: int, before: int = 60, now=None, bars_override: dict | None = None) -> dict:
    """Bars around one match (for the drill-down chart): `before` bars before the signal and the longest horizon
    after it, with the signal and each horizon's end marked. Bars are reloaded from the same source."""
    m = next((x for x in run.matches_json or [] if x["n"] == n), None)
    if m is None:
        raise DomainError("NOT_FOUND", f"Match {n} isn't in this run.", 404)
    spec = dict(run.strategy_json)
    spec.pop("origins", None)
    st = parse_strategy(spec)
    if bars_override is not None:
        bars = bars_override[m["symbol"]]
    else:
        bars, _, _ = load_history(m["symbol"], st.timeframe, st.start, st.end, st.timezone, st.market_hours_only,
                                  max(st.warmup_bars, 260), st.source, now=now)
    ts = pd.Timestamp(m["timestamp"])
    try:
        i = int(bars.index.get_loc(ts))
    except KeyError:
        raise DomainError("BARS_UNAVAILABLE", "The bars for this match aren't available any more.", 409) from None
    longest = max(horizon_bars(h, st.timeframe) for h in st.horizons)
    a, b = max(0, i - before), min(len(bars), i + longest + 1)
    w = bars.iloc[a:b]
    rows = [{"t": t.isoformat(), "o": stats._f(r.open), "h": stats._f(r.high), "l": stats._f(r.low), "c": stats._f(r.close),
             "v": stats._f(r.volume)} for t, r in zip(w.index, w.itertuples())]
    from ..history.forward import horizon_end

    ends = {}
    for h in st.horizons:
        j = horizon_end(bars, i, h, st.timeframe)
        ends[h] = None if j is None or j >= b else j - a
    return {"match": m, "bars": rows, "signal_index": i - a, "horizon_ends": ends, "timeframe": st.timeframe,
            "note": "Prices are split- and dividend-adjusted."}


__all__ = ["AnalysisIn", "Jobs", "build_strategy", "create_run", "execute", "match_window", "run_job", "run_out",
           "datetime", "timezone", "np"]
