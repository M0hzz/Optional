"""Overlay settings: built-in defaults (defaults.yaml) deep-merged with overlay.yaml, then validated.

Validation reports every problem at once with its exact field path
(`overlay.rules[2].conditions[0].tolerance.value: must be > 0`), the same way strategy
files are checked.
"""
from __future__ import annotations

import ast
import copy
import difflib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ..config import PROJECT_ROOT
from ..history.strategy import TOLERANCE_TYPES, Condition, _num, _unknown_keys
from ..live.expr import ExprError, parse
from ..live.indicators import bar_functions
from .status import STATUSES

DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")
USER_PATH = PROJECT_ROOT / "overlay.yaml"
TAB_KEYS = ("scanner", "chain_pricer", "strategy_lab", "backtest", "risk", "insiders", "company", "macro",
            "option_chain", "monitor", "signal_explorer")
DIRECTIONS = ("bullish", "bearish", "neutral")
RULE_TIMEFRAMES = ("1d", "1wk")   # built from the stored daily bars, so every tab sees the same data
# names and functions the overlay adds to the expression language (see rules.overlay_extras)
OVERLAY_NAMES = {"bench_close", "rs_line", "sector_close"}
OVERLAY_FUNCS = {"rs", "sector_rs"}


class OverlayConfigError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Overlay settings problems:\n  - " + "\n  - ".join(problems))


@dataclass
class Rule:
    id: str
    name: str
    conditions: list[Condition]
    direction: str = "neutral"
    group: str = ""
    kind: str = "technical"           # technical | volatility
    enabled: bool = True
    tab_scope: list[str] = field(default_factory=list)
    timeframe: str = "1d"
    symbols: list[str] = field(default_factory=list)   # empty = every symbol with data
    minimum_score: float | None = None   # None = thresholds.confirmed_match_score
    minimum_pass_count: int = 0
    required_must_pass: bool = True
    invalidation: list[str] = field(default_factory=list)
    reference_average: str = "ema(21)"
    description: str = ""

    @property
    def version(self) -> str:
        blob = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:10]

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "direction": self.direction, "group": self.group, "kind": self.kind,
                "enabled": self.enabled, "tab_scope": self.tab_scope, "timeframe": self.timeframe,
                "symbols": self.symbols, "invalidation": self.invalidation, "reference_average": self.reference_average,
                "confirmation": {"minimum_score": self.minimum_score, "minimum_pass_count": self.minimum_pass_count,
                                 "required_conditions_must_pass": self.required_must_pass},
                "conditions": [{"id": c.id, "expression": c.expression, "required": c.required, "weight": c.weight,
                                **({"tolerance": {"type": c.tolerance_type, "value": c.tolerance}}
                                   if c.tolerance_type != "none" else {})} for c in self.conditions]}


@dataclass
class OverlayConfig:
    raw: dict                          # the merged `overlay:` mapping
    rules: list[Rule]

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", True))

    @property
    def thresholds(self) -> dict:
        return self.raw["thresholds"]

    @property
    def account(self) -> dict:
        return self.raw["account_profile"]

    @property
    def market(self) -> dict:
        return self.raw["market"]

    @property
    def labels(self) -> dict:
        return self.raw.get("labels") or {}

    def tab_on(self, key: str) -> bool:
        return self.enabled and bool((self.raw.get("tabs") or {}).get(key, True))

    @property
    def version(self) -> str:
        """Hash of every setting and rule: equal versions mean identical configuration."""
        blob = json.dumps(self.raw, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


# ----------------------------------------------------------------- loading
def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("labels", "owned_shares"):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)     # lists (rules, indexes) replace the default
    return out


def defaults() -> dict:
    return yaml.safe_load(DEFAULTS_PATH.read_text())["overlay"]


def load_overlay_config(path: str | Path | None = None, overrides: dict | None = None) -> OverlayConfig:
    """Defaults <- overlay.yaml (if present) <- `overrides` (e.g. unsaved settings from the dashboard)."""
    merged = defaults()
    p = Path(path) if path else USER_PATH
    if p.exists():
        try:
            user = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as exc:
            raise OverlayConfigError([f"{p.name}: not valid YAML ({exc})"]) from None
        if not isinstance(user, dict):
            raise OverlayConfigError([f"{p.name}: must be a mapping with an `overlay:` key"])
        merged = _deep_merge(merged, user.get("overlay", user))
    if overrides:
        merged = _deep_merge(merged, overrides)
    return validate(merged)


def save_user_overrides(changes: dict, path: str | Path | None = None) -> Path:
    """Write `changes` (a partial overlay mapping) into overlay.yaml, keeping what is already there."""
    p = Path(path) if path else USER_PATH
    current = {}
    if p.exists():
        current = (yaml.safe_load(p.read_text()) or {}).get("overlay", {})
    merged = _deep_merge(current, changes)
    validate(_deep_merge(defaults(), merged))      # never write settings that don't validate
    header = ("# Your overlay settings. Values here override src/optlab/overlay/defaults.yaml.\n"
              "# Written by the dashboard's Overlay tab; you can also edit it by hand.\n")
    p.write_text(header + yaml.safe_dump({"overlay": merged}, sort_keys=False, allow_unicode=True))
    return p


# --------------------------------------------------------------- validation
def _known() -> tuple[set, set]:
    idx = pd.date_range("2024-01-02", periods=3, freq="B")
    dummy = pd.DataFrame({k: np.ones(3) for k in ("open", "high", "low", "close", "volume")}, index=idx)
    names, funcs = bar_functions(dummy)
    return set(names) | OVERLAY_NAMES, set(funcs) | OVERLAY_FUNCS


def check_expression(text, where: str) -> list[str]:
    try:
        tree = parse(str(text))
    except ExprError as exc:
        return [f"{where}: {exc}"]
    names, funcs = _known()
    out = []
    calls = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.func.id not in funcs:
            close = difflib.get_close_matches(node.func.id, funcs, n=3)
            out.append(f"{where}: unknown function {node.func.id}()" + (f"; did you mean {', '.join(close)}?" if close else ""))
        elif isinstance(node, ast.Name) and id(node) not in calls and node.id not in names:
            close = difflib.get_close_matches(node.id, names | funcs, n=3)
            out.append(f"{where}: unknown name {node.id!r}" + (f"; did you mean {', '.join(close)}?" if close else ""))
    return out


THRESHOLD_LIMITS = {  # name: (minimum, maximum or None, whole number?)
    "watch_score": (0, 100, False), "near_match_score": (0, 100, False), "confirmed_match_score": (0, 100, False),
    "stale_after_bars": (1, None, True), "overextended_atr_multiple": (0.1, None, False),
    "minimum_historical_samples": (1, None, True), "maximum_bid_ask_pct": (0.1, None, False),
    "minimum_open_interest": (0, None, False), "minimum_option_volume": (0, None, False),
    "maximum_quote_age_seconds": (1, None, False), "stale_data_sessions": (1, None, True),
    "breadth_minimum_symbols": (1, None, True), "high_theta_pct": (0.1, None, False), "far_otm_delta": (0, 1, False),
    "expiration_warning_days": (0, None, True), "compression_percentile": (0, 100, False),
    "expansion_percentile": (0, 100, False), "pin_risk_pct": (0, None, False), "outlier_share": (0, 1, False),
    "parameter_sensitivity_drop": (0, 1, False),
}
ACCOUNT_NUMBERS = ("total_buying_power", "settled_cash", "reserve_cash", "max_capital_per_idea", "max_loss_per_idea",
                   "max_account_risk_pct", "max_contracts", "contract_multiplier")
ACCOUNT_FLAGS = ("small_account_mode", "defined_risk_only", "naked_options_allowed", "unlimited_risk_allowed",
                 "require_assignment_capacity")
TOP = {"enabled", "completed_bars_only", "compact", "console_items", "tabs", "labels", "account_profile", "thresholds",
       "market", "similarity", "confluence", "alerts", "rules"}


def validate(o: dict) -> OverlayConfig:
    p: list[str] = []
    pre = "overlay."
    _unknown_keys(o, TOP, pre, p)
    for k in ("enabled", "completed_bars_only", "compact"):
        if not isinstance(o.get(k), bool):
            p.append(f"{pre}{k}: must be true or false")
    _num(o.get("console_items"), f"{pre}console_items", p, lo=1, integer=True)
    tabs = o.get("tabs") or {}
    _unknown_keys(tabs, set(TAB_KEYS), f"{pre}tabs.", p)
    for k, v in (o.get("labels") or {}).items():
        if k not in STATUSES:
            close = difflib.get_close_matches(str(k), STATUSES, n=1)
            p.append(f"{pre}labels.{k}: unknown status" + (f"; did you mean {close[0]}?" if close else ""))
        elif not str(v).strip():
            p.append(f"{pre}labels.{k}: label can't be empty")

    a = o.get("account_profile") or {}
    _unknown_keys(a, set(ACCOUNT_NUMBERS) | set(ACCOUNT_FLAGS) | {"account_type", "owned_shares"}, f"{pre}account_profile.", p)
    for k in ACCOUNT_NUMBERS:
        _num(a.get(k), f"{pre}account_profile.{k}", p, lo=1 if k in ("max_contracts", "contract_multiplier") else 0,
             integer=k in ("max_contracts", "contract_multiplier"))
    for k in ACCOUNT_FLAGS:
        if not isinstance(a.get(k), bool):
            p.append(f"{pre}account_profile.{k}: must be true or false")
    if a.get("account_type") not in ("cash", "margin"):
        p.append(f"{pre}account_profile.account_type: must be cash or margin")
    try:
        if float(a["reserve_cash"]) > float(a["total_buying_power"]):
            p.append(f"{pre}account_profile.reserve_cash: is more than total_buying_power, so nothing is allocatable")
        if float(a["max_account_risk_pct"]) > 100:
            p.append(f"{pre}account_profile.max_account_risk_pct: must be between 0 and 100")
    except (KeyError, TypeError, ValueError):
        pass
    for s, n in (a.get("owned_shares") or {}).items():
        _num(n, f"{pre}account_profile.owned_shares.{s}", p, lo=0, integer=True)

    t = o.get("thresholds") or {}
    _unknown_keys(t, set(THRESHOLD_LIMITS), f"{pre}thresholds.", p)
    for k, (lo, hi, whole) in THRESHOLD_LIMITS.items():
        v = _num(t.get(k), f"{pre}thresholds.{k}", p, lo=lo, integer=whole)
        if v is not None and hi is not None and v > hi:
            p.append(f"{pre}thresholds.{k}: must be at most {hi} (got {v:g})")
    try:
        if not float(t["watch_score"]) <= float(t["near_match_score"]) <= float(t["confirmed_match_score"]):
            p.append(f"{pre}thresholds: need watch_score <= near_match_score <= confirmed_match_score")
    except (KeyError, TypeError, ValueError):
        pass

    m = o.get("market") or {}
    _unknown_keys(m, {"indexes", "benchmark", "breadth_ma", "breadth_bullish_pct", "breadth_bearish_pct", "new_high_bars",
                      "divergence_bars", "sector_etfs", "symbol_sectors"}, f"{pre}market.", p)
    if not isinstance(m.get("indexes"), list) or not m.get("indexes"):
        p.append(f"{pre}market.indexes: list at least one index ETF, e.g. [SPY]")
    if not m.get("benchmark"):
        p.append(f"{pre}market.benchmark: required, e.g. SPY")
    for k in ("breadth_ma", "new_high_bars", "divergence_bars"):
        _num(m.get(k), f"{pre}market.{k}", p, lo=2, integer=True)
    for k in ("breadth_bullish_pct", "breadth_bearish_pct"):
        _num(m.get(k), f"{pre}market.{k}", p, lo=0)

    sim = o.get("similarity") or {}
    _unknown_keys(sim, {"top", "features"}, f"{pre}similarity.", p)
    _num(sim.get("top"), f"{pre}similarity.top", p, lo=1, integer=True)
    for expr, spec in (sim.get("features") or {}).items():
        w = f"{pre}similarity.features.{expr}"
        p += check_expression(expr, w)
        if not isinstance(spec, dict):
            p.append(f"{w}: use {{scale: 10, weight: 1}}")
            continue
        _unknown_keys(spec, {"scale", "weight"}, w + ".", p)
        sc = _num(spec.get("scale"), f"{w}.scale", p, lo=0)
        if sc == 0:
            p.append(f"{w}.scale: must be > 0")
        _num(spec.get("weight", 1), f"{w}.weight", p, lo=0)

    c = o.get("confluence") or {}
    _unknown_keys(c, {"show_composite", "weights"}, f"{pre}confluence.", p)
    _unknown_keys(c.get("weights") or {}, {"market", "sector", "technical", "historical", "liquidity", "data_quality"},
                  f"{pre}confluence.weights.", p)
    for k, v in (c.get("weights") or {}).items():
        _num(v, f"{pre}confluence.weights.{k}", p, lo=0)

    al = o.get("alerts") or {}
    _unknown_keys(al, {"cooldown_minutes", "stacking_bars", "desktop"}, f"{pre}alerts.", p)
    _num(al.get("cooldown_minutes", 0), f"{pre}alerts.cooldown_minutes", p, lo=0)
    _num(al.get("stacking_bars", 3), f"{pre}alerts.stacking_bars", p, lo=0, integer=True)

    rules, seen = [], set()
    for i, r in enumerate(o.get("rules") or []):
        rule = _parse_rule(r, f"{pre}rules[{i}]", p, seen)
        if rule:
            rules.append(rule)
    if p:
        raise OverlayConfigError(p)
    return OverlayConfig(o, rules)


def _parse_rule(r, w: str, p: list, seen: set) -> Rule | None:
    if not isinstance(r, dict):
        p.append(f"{w}: each rule must be a mapping with id, name and conditions")
        return None
    _unknown_keys(r, {"id", "name", "description", "enabled", "direction", "group", "kind", "tab_scope", "timeframe",
                      "symbols", "conditions", "confirmation", "invalidation", "reference_average"}, w + ".", p)
    rid = str(r.get("id") or "").strip()
    if not rid:
        p.append(f"{w}.id: required")
    elif rid in seen:
        p.append(f"{w}.id: duplicate rule id {rid!r}")
    seen.add(rid)
    direction = str(r.get("direction", "neutral"))
    if direction not in DIRECTIONS:
        p.append(f"{w}.direction: must be one of {', '.join(DIRECTIONS)}")
    kind = str(r.get("kind", "technical"))
    if kind not in ("technical", "volatility"):
        p.append(f"{w}.kind: must be technical or volatility")
    tf = str(r.get("timeframe", "1d"))
    if tf not in RULE_TIMEFRAMES:
        p.append(f"{w}.timeframe: must be one of {', '.join(RULE_TIMEFRAMES)} (overlay rules run on the stored "
                 "daily bars; use the Signal explorer for intraday searches)")
    scope = r.get("tab_scope") or []
    for s in scope:
        if s not in TAB_KEYS:
            p.append(f"{w}.tab_scope: unknown tab {s!r} (use {', '.join(TAB_KEYS)})")
    conds, ids = [], set()
    for j, c in enumerate(r.get("conditions") or []):
        cw = f"{w}.conditions[{j}]"
        if not isinstance(c, dict):
            c = {"expression": c}
        _unknown_keys(c, {"id", "expression", "required", "weight", "tolerance"}, cw + ".", p)
        cid = str(c.get("id") or f"c{j + 1}")
        if cid in ids:
            p.append(f"{cw}.id: duplicate condition id {cid!r}")
        ids.add(cid)
        if not c.get("expression"):
            p.append(f"{cw}.expression: required")
            continue
        p += check_expression(c["expression"], f"{cw}.expression")
        weight = _num(c.get("weight", 1), f"{cw}.weight", p, lo=0)
        if weight == 0:
            p.append(f"{cw}.weight: must be > 0")
        tol = c.get("tolerance") or {}
        if not isinstance(tol, dict):
            p.append(f"{cw}.tolerance: use {{type: absolute, value: 0.2}}")
            tol = {}
        _unknown_keys(tol, {"type", "value"}, f"{cw}.tolerance.", p)
        ttype = str(tol.get("type", "none" if not tol else "absolute"))
        if ttype not in TOLERANCE_TYPES:
            p.append(f"{cw}.tolerance.type: must be one of {', '.join(TOLERANCE_TYPES)}")
        tval = _num(tol.get("value", 0), f"{cw}.tolerance.value", p, lo=0) if ttype != "none" else 0.0
        if ttype in ("absolute", "percent_distance") and not tval:
            p.append(f"{cw}.tolerance.value: must be > 0 for {ttype} tolerance")
        conds.append(Condition(cid, str(c["expression"]), bool(c.get("required", True)), weight or 1.0, ttype, tval or 0.0))
    if not conds:
        p.append(f"{w}.conditions: add at least one condition")
    conf = r.get("confirmation") or {}
    _unknown_keys(conf, {"minimum_score", "minimum_pass_count", "required_conditions_must_pass"}, f"{w}.confirmation.", p)
    ms = _num(conf["minimum_score"], f"{w}.confirmation.minimum_score", p, lo=0) if "minimum_score" in conf else None
    if ms is not None and ms > 100:
        p.append(f"{w}.confirmation.minimum_score: must be between 0 and 100")
    mp = _num(conf.get("minimum_pass_count", 0), f"{w}.confirmation.minimum_pass_count", p, lo=0, integer=True)
    if mp and mp > len(conds):
        p.append(f"{w}.confirmation.minimum_pass_count: {mp} is more than the {len(conds)} conditions")
    inval = r.get("invalidation") or []
    if isinstance(inval, str):
        inval = [inval]
    for j, e in enumerate(inval):
        p += check_expression(e, f"{w}.invalidation[{j}]")
    ref = str(r.get("reference_average", "ema(21)"))
    p += check_expression(ref, f"{w}.reference_average")
    if not rid or not conds:
        return None
    return Rule(rid, str(r.get("name") or rid), conds, direction, str(r.get("group") or rid), kind,
                bool(r.get("enabled", True)), list(scope), tf, [str(s).upper() for s in r.get("symbols") or []],
                ms, mp or 0, bool(conf.get("required_conditions_must_pass", True)),
                [str(e) for e in inval], ref, str(r.get("description", "")))
