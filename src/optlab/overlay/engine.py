"""Run the overlay over the stored daily bars: data health first, then every enabled rule on
every symbol, then the cross-symbol pieces (conflicts, timeframe alignment, market overview).

`scan()` is pure: the same bars, settings and clock give the same OverlayScan.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .config import OverlayConfig
from .quality import Check, bar_health, blocked, completed_bars, downgraded, last_completed_session
from .rules import RuleResult, evaluate_rule, indicator_version, make_evaluator, timeframe_alignment, to_weekly
from .status import STATUSES

ACTIVE = ("CONFIRMED", "STALE", "OVEREXTENDED")          # states in which a confirmation still stands
TAB_TITLES = {"scanner": "Scanner", "chain_pricer": "Chain & pricer", "strategy_lab": "Strategy lab",
              "backtest": "Backtest", "risk": "Risk & sizing", "insiders": "Insiders", "company": "Company",
              "macro": "Macro", "option_chain": "Option chain", "monitor": "Monitor",
              "signal_explorer": "Signal explorer", "overlay": "Overlay"}
TRANSITION_TEXT = {"near_to_confirmed": "near → confirmed", "new_confirmed": "newly confirmed",
                   "invalidated": "confirmation lost", "new_near": "newly near"}
# order in which the console lists things worth looking at
PRIORITY = {"near_to_confirmed": 0, "new_confirmed": 1, "invalidated": 2, "new_near": 3}
STATUS_PRIORITY = {"DATA_ERROR": 0, "CONFIRMED": 1, "INVALIDATED": 2, "NEAR_CONFIRMATION": 3, "CONFLICTING": 4,
                   "OVEREXTENDED": 5, "STALE": 6, "INSUFFICIENT_DATA": 7, "WATCH": 8, "NOT_PRESENT": 9}


@dataclass
class OverlayScan:
    config: OverlayConfig
    now: datetime
    source: str
    results: list[RuleResult]
    checks: list[Check]
    alignment: dict[str, dict]
    bars: dict[str, pd.DataFrame] = field(repr=False, default_factory=dict)
    market: dict | None = None
    seconds: float = 0.0
    n_symbols: int = 0
    n_bars: int = 0
    n_rules: int = 0
    versions: dict = field(default_factory=dict)

    def for_tab(self, key: str) -> list[RuleResult]:
        return [r for r in self.results if key in r.tab_scope or r.rule_id == "conflict"]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.effect != "none"]

    def highlights(self, statuses=None, symbols=None, min_severity: int = 0, limit: int | None = None) -> list[RuleResult]:
        """Results worth looking at, most important first: state changes on the latest bar, then
        confirmations, invalidations, near confirmations, conflicts, then caveated signals."""
        pool = [r for r in self.results if r.status not in ("NOT_PRESENT", "WATCH") or r.transition]
        if statuses:
            pool = [r for r in pool if r.status in statuses]
        if symbols:
            pool = [r for r in pool if r.symbol in symbols]
        pool = [r for r in pool if STATUSES[r.status].severity >= min_severity]
        pool.sort(key=lambda r: (PRIORITY.get(r.transition, 9), STATUS_PRIORITY.get(r.status, 9), -r.score, r.symbol))
        return pool[:limit] if limit else pool


def frames_from_prices(prices: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Long table (symbol, date, open, high, low, close, volume) -> {symbol: bars indexed by date}."""
    out = {}
    for sym, g in prices.groupby("symbol", sort=True):
        g = g.sort_values("date")
        out[sym] = g.set_index(pd.DatetimeIndex(pd.to_datetime(g["date"]), name="date"))[
            ["open", "high", "low", "close", "volume"]].astype(float)
    return out


def _error_result(rule, symbol: str, check: Check, versions: dict) -> RuleResult:
    return RuleResult(rule.id, rule.name, symbol, rule.timeframe, None, "DATA_ERROR", "DATA_ERROR", 0.0, float("nan"),
                      "DATA_ERROR", 0, 0, 0, [], len(rule.conditions), rule.minimum_score or 0, rule.minimum_pass_count,
                      rule.direction, rule.group, rule.kind, rule.tab_scope, [], [],
                      f"Blocked by data health: {check.name.lower()} failed ({check.detail}) {check.action}".strip(),
                      reason_code=f"blocked:{check.id}", versions=versions, data_flags=["DATA_ERROR"])


def scan(frames: dict[str, pd.DataFrame], cfg: OverlayConfig, now: datetime, source: str = "") -> OverlayScan:
    t0 = time.perf_counter()
    th = cfg.thresholds
    versions = {"config": cfg.version, "indicators": indicator_version()}
    out = OverlayScan(cfg, now, source, [], [], {}, versions=versions)
    if not cfg.enabled:
        return out
    last_sess = last_completed_session(now)
    clean: dict[str, pd.DataFrame] = {}
    checks_by: dict[str, list[Check]] = {}
    for sym, raw in frames.items():
        bars, dropped = completed_bars(raw, now) if cfg.raw.get("completed_bars_only", True) else (raw, False)
        bars, checks = bar_health(sym, bars, now, th, source, dropped)
        checks_by[sym], clean[sym] = checks, bars
        out.checks += checks
    out.bars = clean
    bench_sym = cfg.market["benchmark"]
    bench = clean.get(bench_sym)
    sector_of = cfg.market.get("symbol_sectors") or {}
    rules = [r for r in cfg.rules if r.enabled]
    for sym, bars in clean.items():
        checks = checks_by[sym]
        mine = [r for r in rules if not r.symbols or sym in r.symbols]
        stop = blocked(checks, "technical rules")
        if stop:
            out.results += [_error_result(r, sym, stop[0], {**versions, "rule": r.version}) for r in mine]
            continue
        flags = (["STALE_DATA"] if any(c.id == "fresh" for c in downgraded(checks, "technical rules")) else []) + (
            ["DOWNGRADED"] if any(c.id != "fresh" for c in downgraded(checks, "technical rules")) else [])
        sector = clean.get(sector_of.get(sym, ""))
        evs = {}
        weekly = None
        for rule in mine:
            if rule.timeframe == "1wk":
                if weekly is None:
                    weekly = to_weekly(bars, last_sess)
                b, bb = weekly, to_weekly(bench, last_sess) if bench is not None else None
                sb = to_weekly(sector, last_sess) if sector is not None else None
            else:
                b, bb, sb = bars, bench, sector
            if len(b) < 2:
                continue
            ev = evs.setdefault(rule.timeframe, make_evaluator(b, rule.timeframe, bb, sb))
            res = evaluate_rule(rule, b, ev, sym, th, {**versions, "rule": rule.version}, source)
            res.data_flags = list(flags)
            out.results.append(res)
            out.n_bars += len(b)
        out.alignment[sym] = timeframe_alignment(bars, to_weekly(bars, last_sess)) if len(bars) > 60 else {
            "higher": "Unavailable", "intermediate": "Unavailable", "lower": "Unavailable", "agreement": "Unavailable"}
    out.results += conflicts(out.results, versions)
    out.n_symbols, out.n_rules = len(clean), len(rules)
    out.seconds = time.perf_counter() - t0
    return out


def conflicts(results: list[RuleResult], versions: dict) -> list[RuleResult]:
    """A symbol with a bullish and a bearish rule both standing on the same timeframe is CONFLICTING."""
    out = []
    by = {}
    for r in results:
        if r.status in ACTIVE and r.direction in ("bullish", "bearish"):
            by.setdefault((r.symbol, r.timeframe), {}).setdefault(r.direction, []).append(r)
    for (sym, tf), d in by.items():
        if "bullish" in d and "bearish" in d:
            bull, bear = ", ".join(r.rule_name for r in d["bullish"]), ", ".join(r.rule_name for r in d["bearish"])
            ref = d["bullish"][0]
            out.append(RuleResult("conflict", "Conflicting evidence", sym, tf, ref.timestamp, "CONFLICTING", "CONFLICTING",
                                  0.0, float("nan"), "", 0, 0, 0, [], 0, 0, 0, "neutral", "conflict", "technical",
                                  ["scanner", "monitor"], [], [],
                                  f"Conflicting: bullish {bull} and bearish {bear} are both confirmed on {sym} ({tf}).",
                                  reason_code="conflict", versions=versions, data_timestamp=ref.data_timestamp))
    return out


def results_frame(results: list[RuleResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append({"symbol": r.symbol, "rule": r.rule_name, "status": r.status, "score": r.score,
                     "prev_score": r.prev_score, "passed": r.passed, "near": r.near, "failed": r.failed,
                     "unavailable": len(r.unavailable), "conditions": r.n_conditions,
                     "change": TRANSITION_TEXT.get(r.transition, ""), "bars_since": r.bars_since,
                     "move_since_pct": r.move_since_pct, "atr_move": r.atr_move, "ref_distance_atr": r.ref_distance_atr,
                     "direction": r.direction, "timeframe": r.timeframe, "bar": r.data_timestamp,
                     "data": ", ".join(r.data_flags), "explanation": r.explanation, "rule_id": r.rule_id})
    return pd.DataFrame(rows)
