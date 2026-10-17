"""Historical-search strategies: YAML in, a validated Strategy out.

Every problem is reported with the exact field path (`conditions[2].tolerance.type`),
all at once, so a strategy file can be fixed in one pass.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd
import yaml

from ..live.expr import ExprError, parse
from ..live.indicators import bar_functions
from .bars import TIMEFRAMES

TOLERANCE_TYPES = ("absolute", "percent_distance", "none")
SOURCES = ("yahoo", "archive", "synthetic")
SORT_KEYS = ("timestamp", "score", "symbol")
# live-only names: no timestamped history exists for them
NOT_HISTORICAL = {
    "atm_iv": "implied volatility", "skew": "option skew", "put_call_volume": "option volume",
    "put_call_oi": "option open interest", "option_volume": "option volume",
    "days_to_earnings": "earnings dates",
}
SYMBOL_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-]{0,9}$")
TOP_KEYS = {"name", "description", "symbols", "timeframe", "range", "warmup_bars", "data", "conditions",
            "sequence", "sequence_weight", "matching", "output", "forward"}


class _TextDates(yaml.SafeLoader):
    """SafeLoader that leaves dates as text, so a bad date is reported against its field
    (range.start) instead of crashing inside the YAML parser."""


_TextDates.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}


def load_yaml(text: str):
    return yaml.load(text, Loader=_TextDates)


class StrategyError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Strategy problems:\n  - " + "\n  - ".join(problems))


@dataclass
class Condition:
    id: str
    expression: str
    required: bool = True
    weight: float = 1.0
    tolerance_type: str = "none"
    tolerance: float = 0.0
    recent_bars: int = 0


@dataclass
class Step:
    id: str
    expression: str
    within_bars: int | None = None


@dataclass
class Strategy:
    name: str
    symbols: list[str]
    timeframe: str
    start: str
    end: str
    timezone: str = "America/New_York"
    market_hours_only: bool = True
    warmup_bars: int = 0
    source: str = "yahoo"
    conditions: list[Condition] = field(default_factory=list)
    sequence: list[Step] = field(default_factory=list)
    minimum_score: float = 100.0
    minimum_pass_count: int = 0
    include_exact: bool = True
    include_near: bool = False
    required_mode: str = "strict"
    sort_by: str = "timestamp"
    order: str = "ascending"
    limit: int = 1000
    horizons: list[str] = field(default_factory=lambda: ["1", "3", "5", "10"])
    dedupe_bars: int = 0
    description: str = ""
    sequence_weight: float = 0.0  # 0 = weighted like an average condition

    @property
    def version(self) -> str:
        """Short hash of every setting: two searches with the same version used the same strategy."""
        blob = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def expressions(self) -> list[str]:
        return [c.expression for c in self.conditions] + [s.expression for s in self.sequence]

    def to_dict(self) -> dict:
        conds = []
        for c in self.conditions:
            d = {"id": c.id, "expression": c.expression, "required": c.required, "weight": c.weight}
            if c.tolerance_type != "none":
                d["tolerance"] = {"type": c.tolerance_type, "value": c.tolerance}
            if c.recent_bars:
                d["recent_bars"] = c.recent_bars
            conds.append(d)
        out = {"name": self.name, "description": self.description, "symbols": self.symbols,
               "timeframe": self.timeframe,
               "range": {"start": self.start, "end": self.end, "timezone": self.timezone,
                         "market_hours_only": self.market_hours_only},
               "warmup_bars": self.warmup_bars, "data": {"source": self.source}, "conditions": conds,
               "matching": {"minimum_score": self.minimum_score, "minimum_pass_count": self.minimum_pass_count,
                            "include_exact": self.include_exact, "include_near": self.include_near,
                            "required_mode": self.required_mode},
               "output": {"sort_by": self.sort_by, "order": self.order, "limit": self.limit},
               "forward": {"horizons": self.horizons, "dedupe_bars": self.dedupe_bars}}
        if self.sequence:
            if self.sequence_weight:
                out["sequence_weight"] = self.sequence_weight
            out["sequence"] = [{"id": s.id, "expression": s.expression,
                                **({"within_bars": s.within_bars} if s.within_bars is not None else {})}
                               for s in self.sequence]
        return {"historical_signal_search": out}

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)


def _known_names() -> tuple[set, set]:
    idx = pd.date_range("2024-01-02", periods=3, freq="B")
    dummy = pd.DataFrame({k: np.ones(3) for k in ("open", "high", "low", "close", "volume")}, index=idx)
    names, funcs = bar_functions(dummy)
    return set(names), set(funcs)


def check_expression(text: str, where: str) -> list[str]:
    """Static check: it parses, and every name and function exists and has history."""
    try:
        tree = parse(str(text))
    except ExprError as exc:
        return [f"{where}: {exc}"]
    names, funcs = _known_names()
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func.id
            if f not in funcs:
                close = difflib.get_close_matches(f, funcs, n=3)
                out.append(f"{where}: unknown function {f}()" + (f"; did you mean {', '.join(close)}?" if close else ""))
        elif isinstance(node, ast.Name) and not _is_call_name(tree, node):
            if node.id in NOT_HISTORICAL:
                out.append(f"{where}: {node.id} needs historical {NOT_HISTORICAL[node.id]}, which optlab doesn't "
                           "have as timestamped history; historical searches support price and volume studies only.")
            elif node.id not in names:
                close = difflib.get_close_matches(node.id, names | funcs, n=3)
                out.append(f"{where}: unknown name {node.id!r}" + (f"; did you mean {', '.join(close)}?" if close else ""))
    return out


def _is_call_name(tree, name_node) -> bool:
    return any(isinstance(n, ast.Call) and n.func is name_node for n in ast.walk(tree))


def _date_str(v, where, problems) -> str | None:
    if isinstance(v, (date, datetime)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    try:
        pd.Timestamp(str(v))
        return str(v).strip()
    except (ValueError, TypeError, OverflowError):
        problems.append(f"{where}: {v!r} is not a date or date-time (use 2026-01-31 or '2026-01-31 10:30')")
        return None


def _num(v, where, problems, lo=None, integer=False):
    try:
        x = float(v)
    except (TypeError, ValueError):
        problems.append(f"{where}: must be a number (got {v!r})")
        return None
    if integer and not x.is_integer():
        problems.append(f"{where}: must be a whole number (got {v!r})")
        return None
    if lo is not None and x < lo:
        problems.append(f"{where}: must be >= {lo} (got {v!r})")
        return None
    return int(x) if integer else x


def _unknown_keys(d: dict, allowed: set, where: str, problems: list) -> None:
    for k in d:
        if k not in allowed:
            close = difflib.get_close_matches(str(k), allowed, n=1)
            problems.append(f"{where}{k}: unknown setting" + (f"; did you mean {close[0]}?" if close else ""))


def parse_strategy(data: dict | str) -> Strategy:
    """Validate a strategy (a dict, or YAML text). Raises StrategyError listing every problem."""
    if isinstance(data, str):
        try:
            data = load_yaml(data) or {}
        except yaml.YAMLError as exc:
            raise StrategyError([f"YAML: {exc}"]) from None
    if not isinstance(data, dict):
        raise StrategyError(["The strategy must be a YAML mapping (key: value)."])
    d = data.get("historical_signal_search", data)
    p: list[str] = []
    _unknown_keys(d, TOP_KEYS, "", p)

    name = str(d.get("name") or "").strip()
    if not name:
        p.append("name: required")
    syms = d.get("symbols")
    syms = [syms] if isinstance(syms, str) else syms
    if not syms:
        p.append("symbols: list at least one symbol, e.g. [NVDA]")
        syms = []
    syms = [str(s).strip().upper() for s in syms]
    for i, s in enumerate(syms):
        if not SYMBOL_RE.match(s):
            p.append(f"symbols[{i}]: {s!r} is not a valid ticker")
    tf = str(d.get("timeframe", ""))
    if tf not in TIMEFRAMES:
        p.append(f"timeframe: must be one of {', '.join(TIMEFRAMES)} (got {d.get('timeframe')!r})")

    rng = d.get("range") or {}
    _unknown_keys(rng, {"start", "end", "timezone", "market_hours_only"}, "range.", p)
    start = _date_str(rng.get("start"), "range.start", p) if rng.get("start") is not None else None
    end = _date_str(rng.get("end"), "range.end", p) if rng.get("end") is not None else None
    if start is None and "start" not in rng:
        p.append("range.start: required")
    if end is None and "end" not in rng:
        p.append("range.end: required")
    tz = str(rng.get("timezone", "America/New_York"))
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception:
        p.append(f"range.timezone: unknown timezone {tz!r} (use names like America/New_York or UTC)")
    if start and end:
        try:
            if pd.Timestamp(start) > pd.Timestamp(end):
                p.append(f"range: start {start} is after end {end}")
        except ValueError:
            pass

    data_cfg = d.get("data") or {}
    _unknown_keys(data_cfg, {"source"}, "data.", p)
    source = str(data_cfg.get("source", "yahoo"))
    if source not in SOURCES:
        p.append(f"data.source: must be one of {', '.join(SOURCES)} (got {source!r})")

    conds, seen = [], set()
    for i, c in enumerate(d.get("conditions") or []):
        w = f"conditions[{i}]"
        if not isinstance(c, dict):
            c = {"expression": c}
        _unknown_keys(c, {"id", "expression", "required", "weight", "tolerance", "recent_bars"}, w + ".", p)
        cid = str(c.get("id") or f"c{i + 1}")
        if cid in seen:
            p.append(f"{w}.id: duplicate id {cid!r}")
        seen.add(cid)
        expr = c.get("expression")
        if not expr:
            p.append(f"{w}.expression: required")
            continue
        p += check_expression(expr, f"{w}.expression ({cid})")
        weight = _num(c.get("weight", 1), f"{w}.weight", p, lo=0)
        if weight == 0:
            p.append(f"{w}.weight: must be > 0")
        tol = c.get("tolerance") or {}
        if not isinstance(tol, dict):
            p.append(f"{w}.tolerance: use {{type: absolute, value: 0.2}}")
            tol = {}
        _unknown_keys(tol, {"type", "value"}, f"{w}.tolerance.", p)
        ttype = str(tol.get("type", "none" if not tol else "absolute"))
        if ttype not in TOLERANCE_TYPES:
            p.append(f"{w}.tolerance.type: must be one of {', '.join(TOLERANCE_TYPES)} (got {ttype!r})")
        tval = _num(tol.get("value", 0), f"{w}.tolerance.value", p, lo=0) if ttype != "none" else 0.0
        if ttype in ("absolute", "percent_distance") and not tval:
            p.append(f"{w}.tolerance.value: must be > 0 for {ttype} tolerance")
        recent = _num(c.get("recent_bars", 0), f"{w}.recent_bars", p, lo=0, integer=True)
        conds.append(Condition(cid, str(expr), bool(c.get("required", True)), weight or 1.0, ttype,
                               tval or 0.0, recent or 0))

    steps = []
    for i, st in enumerate(d.get("sequence") or []):
        w = f"sequence[{i}]"
        if not isinstance(st, dict) or not st.get("expression"):
            p.append(f"{w}: needs an expression")
            continue
        _unknown_keys(st, {"id", "expression", "within_bars"}, w + ".", p)
        p += check_expression(st["expression"], f"{w}.expression")
        wb = st.get("within_bars")
        if i == 0 and wb is not None:
            p.append(f"{w}.within_bars: the first step starts the sequence; within_bars applies to later steps")
        if i > 0 and wb is None:
            p.append(f"{w}.within_bars: required for every step after the first (bars allowed after the previous step)")
        elif i > 0:
            wb = _num(wb, f"{w}.within_bars", p, lo=0, integer=True)
        steps.append(Step(str(st.get("id") or f"step{i + 1}"), str(st["expression"]), wb if i > 0 else None))
    if not conds and not steps:
        p.append("conditions: add at least one condition (or a sequence)")

    m = d.get("matching") or {}
    _unknown_keys(m, {"minimum_score", "minimum_pass_count", "include_exact", "include_near", "required_mode"},
                  "matching.", p)
    min_score = _num(m.get("minimum_score", 100 if not m.get("include_near") else 75), "matching.minimum_score", p, lo=0)
    if min_score is not None and min_score > 100:
        p.append("matching.minimum_score: must be between 0 and 100")
    min_pass = _num(m.get("minimum_pass_count", 0), "matching.minimum_pass_count", p, lo=0, integer=True)
    if min_pass and min_pass > len(conds) + (1 if steps else 0):
        p.append(f"matching.minimum_pass_count: {min_pass} is more than the {len(conds)} conditions")
    req_mode = str(m.get("required_mode", "strict"))
    if req_mode not in ("strict", "tolerant"):
        p.append("matching.required_mode: must be strict or tolerant")
    inc_exact, inc_near = bool(m.get("include_exact", True)), bool(m.get("include_near", False))
    if not inc_exact and not inc_near:
        p.append("matching: include_exact and include_near are both false, so nothing can match")

    o = d.get("output") or {}
    _unknown_keys(o, {"sort_by", "order", "limit"}, "output.", p)
    sort_by = str(o.get("sort_by", "timestamp"))
    if sort_by not in SORT_KEYS:
        p.append(f"output.sort_by: must be one of {', '.join(SORT_KEYS)}")
    order = str(o.get("order", "ascending"))
    if order not in ("ascending", "descending"):
        p.append("output.order: must be ascending or descending")
    limit = _num(o.get("limit", 1000), "output.limit", p, lo=1, integer=True)

    fw = d.get("forward") or {}
    _unknown_keys(fw, {"horizons", "dedupe_bars"}, "forward.", p)
    horizons = [str(h) for h in fw.get("horizons", ["1", "3", "5", "10"])]
    for i, h in enumerate(horizons):
        if not re.fullmatch(r"\d+d?", h) or h.rstrip("d") == "0":
            p.append(f"forward.horizons[{i}]: use a bar count like 5 or trading days like 1d (got {h!r})")
    dedupe = _num(fw.get("dedupe_bars", 0), "forward.dedupe_bars", p, lo=0, integer=True)
    warm = _num(d.get("warmup_bars", 0), "warmup_bars", p, lo=0, integer=True)
    seq_w = _num(d.get("sequence_weight", 0), "sequence_weight", p, lo=0)
    if d.get("sequence_weight") is not None and not steps:
        p.append("sequence_weight: set only with a sequence")

    if p:
        raise StrategyError(p)
    s = Strategy(name, syms, tf, start, end, tz, bool(rng.get("market_hours_only", True)), warm or 0, source,
                 conds, steps, min_score, min_pass or 0, inc_exact, inc_near, req_mode, sort_by, order, limit,
                 horizons, dedupe or 0, str(d.get("description", "")), seq_w or 0.0)
    if not s.warmup_bars:
        s.warmup_bars = auto_warmup(s.expressions())
    return s


def auto_warmup(expressions: list[str]) -> int:
    """Bars of history before the range so studies are settled: 3x the longest window (EMAs and
    Wilder smoothing need about that), at least 50."""
    longest = 1
    for e in expressions:
        for call in ast.walk(parse(e)):
            if not isinstance(call, ast.Call):
                continue  # thresholds like the 55 in `rsi(14) >= 55` are not windows
            for arg in list(call.args) + [k.value for k in call.keywords]:
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float))
                        and not isinstance(arg.value, bool) and float(arg.value).is_integer() and 1 <= arg.value <= 1000):
                    longest = max(longest, int(arg.value))
    return int(max(50, 3 * longest))
