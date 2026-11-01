"""Position status: what one monitoring pass's data says about one open position.

Precedence (highest first):
    STALE_DATA           quotes unavailable or cached past their limit, a leg can't be valued, or a rule
                         can't be evaluated. Never shown as a safe HOLD.
    EXIT_CONDITION_MET   one of the position's exit rules holds
    ACTION_REQUIRED      an action rule holds, or a built-in check: an option leg expires today or has
                         expired, or a short option is in the money with `assignment_risk_days` or fewer left
    WATCH                a watch rule holds
    HOLD                 none of the above
    CLOSED               the position isn't open (set by the close / archive operations, not by a pass)

Every status carries the reasons: each rule that holds, with the values it used (and the previous pass's
value when it changed), or each piece of data that is missing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from ..live.scanner import NO_EARNINGS
from ..portfolio.models import Position
from ..portfolio.money import from_units
from ..portfolio.quotes import CACHED, UNAVAILABLE, ChainSnapshot
from ..portfolio.valuation import PositionValuation
from .rules import Context, RuleResult, explain
from .settings import MonitorSettings, RuleSet

STALE, EXIT, ACTION, WATCH, HOLD, CLOSED = ("STALE_DATA", "EXIT_CONDITION_MET", "ACTION_REQUIRED", "WATCH", "HOLD",
                                            "CLOSED")
PRECEDENCE = [STALE, EXIT, ACTION, WATCH, HOLD, CLOSED]
LABELS = {STALE: "STALE DATA", EXIT: "EXIT CONDITION MET", ACTION: "ACTION REQUIRED", WATCH: "WATCH", HOLD: "HOLD",
          CLOSED: "CLOSED"}
SEVERITY = {STALE: "warning", EXIT: "critical", ACTION: "critical", WATCH: "warning", HOLD: "info", CLOSED: "info"}
URGENT = {EXIT, ACTION}
NAN = float("nan")


def rank(status: str) -> int:
    return PRECEDENCE.index(status)


def rules_for(p: Position, settings: MonitorSettings) -> tuple[RuleSet, str, bool]:
    """(rules, timeframe, uses_defaults) for a position: its own when set, else the account-wide defaults."""
    cfg = p.monitor_json or {}
    own = cfg.get("rules")
    rules = RuleSet.model_validate(own) if own is not None else settings.default_rules
    return rules, str(cfg.get("timeframe") or settings.timeframe), own is None


def _f(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


@dataclass
class PositionCheck:
    raw_status: str
    reasons: list[str]
    rules: list[RuleResult]
    metrics: dict[str, Any]
    why_missing: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return f"{LABELS[self.raw_status]}: " + "; ".join(self.reasons)


def metrics_for(p: Position, v: PositionValuation, earnings: date | None, today: date) -> tuple[dict, dict]:
    """The names a position's rules can use, and why each missing one is missing.
    None = applies but unavailable; NaN = doesn't apply to this position (comparisons are False)."""
    why: dict[str, str] = {}
    price = _f(v.underlying_price)
    if price is None:
        why["price"] = "no underlying price in the chain snapshot"
    opts = [lg for lg in p.legs if lg.asset_type.value == "OPTION"]
    dte = min(((lg.expiration - today).days for lg in opts if lg.expiration), default=NO_EARNINGS)
    shorts = [lg for lg in opts if lg.side.value == "SELL"]
    short_otm: float | None = NAN
    short_itm: bool | None = False
    if shorts:
        if price is None:
            short_otm, short_itm = None, None
            why["short_strike_otm_pct"] = why["short_itm"] = why["price"]
        else:
            dists = []
            for lg in shorts:
                strike = float(from_units(lg.strike_units or 0))
                call = lg.option_right is not None and lg.option_right.value == "CALL"
                dists.append(100 * ((strike - price) if call else (price - strike)) / price)
            short_otm = min(dists)
            short_itm = short_otm < 0
    dte_earn = (earnings - today).days if earnings else NO_EARNINGS
    quotes = {lv.leg_index: lv.quote for lv in v.legs}
    spreads, ois = [], []
    for lg in opts:
        q = quotes.get(lg.leg_index)
        spreads.append(None if q is None else q.spread_pct)
        ois.append(None if q is None else q.open_interest)
    max_spread: float | None = NAN if not opts else (None if any(x is None for x in spreads) else max(spreads))  # type: ignore[type-var]
    min_oi: float | None = NAN if not opts else (None if any(x is None for x in ois) else min(ois))  # type: ignore[type-var]
    if max_spread is None:
        why["max_spread_pct"] = "a leg has no two-sided quote"
    if min_oi is None:
        why["min_open_interest"] = "a leg has no open interest in the chain"
    pnl = _f(v.open_pnl)
    max_loss = from_units(p.max_loss_units) if p.max_loss_state.value == "bounded" else None
    loss_pct: float | None = NAN
    if max_loss is not None and max_loss > 0:
        loss_pct = None if pnl is None else max(0.0, -pnl) / float(max_loss) * 100
    for k in ("pnl", "pnl_pct", "value"):
        why[k] = "the position can't be fully valued (see the leg quotes)"
    g = v.greeks
    for k in ("position_delta", "position_gamma", "theta_day", "position_vega"):
        why[k] = "a leg has no Greeks from the provider"
    if loss_pct is None:
        why["loss_pct_of_max"] = why["pnl"]
    names = {
        "pnl": pnl, "pnl_pct": v.open_pnl_pct, "value": _f(v.value), "entry": _f(from_units(p.net_premium_units)),
        "dte": dte, "short_strike_otm_pct": short_otm, "position_delta": g.get("delta"),
        "position_gamma": g.get("gamma"), "theta_day": g.get("theta"), "position_vega": g.get("vega"),
        "days_to_earnings": dte_earn, "earnings_before_expiry": bool(earnings) and 0 <= dte_earn <= dte,
        "price": price, "max_spread_pct": max_spread, "min_open_interest": min_oi, "loss_pct_of_max": loss_pct,
        "short_itm": short_itm,
    }
    if names["entry"] is None:
        why["entry"] = "a leg has no fill price"
    return names, {k: w for k, w in why.items() if names.get(k) is None}


def system_rules(p: Position, price: float | None, today: date, settings: MonitorSettings) -> list[RuleResult]:
    """Built-in ACTION checks that don't depend on user rules."""
    out: list[RuleResult] = []
    for lg in p.legs:
        if lg.asset_type.value != "OPTION" or lg.expiration is None:
            continue
        d = (lg.expiration - today).days
        name = lg.occ_symbol or f"leg {lg.leg_index + 1}"
        if settings.expiry_action and d <= 0:
            msg = (f"{name} expires today: close it, roll it, or let it expire deliberately" if d == 0 else
                   f"{name} expired on {lg.expiration:%Y-%m-%d}: record how it ended by closing the position "
                   "(expired worthless, assigned or exercised)")
            out.append(RuleResult("system", "expiration", True, {"dte": d}, msg))
        if lg.side.value == "SELL" and price is not None and 0 <= d <= settings.assignment_risk_days:
            strike = float(from_units(lg.strike_units or 0))
            call = lg.option_right is not None and lg.option_right.value == "CALL"
            itm = 100 * ((price - strike) if call else (strike - price)) / price
            if itm > 0:
                out.append(RuleResult("system", "assignment_risk", True, {"dte": d, "itm_pct": round(itm, 2)},
                                      f"short {name} is {itm:.1f}% in the money with {d} day{'s' if d != 1 else ''} "
                                      "left: assignment risk"))
    return out


def check_position(p: Position, v: PositionValuation, snap: ChainSnapshot, settings: MonitorSettings,
                   earnings: date | None, today: date, bars=None, bars_error: str | None = None,
                   previous: dict | None = None, max_age: float = 300,
                   now=None, news: tuple[dict, dict] | None = None) -> PositionCheck:
    names, why = metrics_for(p, v, earnings, today)
    rules, timeframe, _ = rules_for(p, settings)
    ctx = Context("position", names, why, bars, bars_error, timeframe, snap.rows, news)
    results = ([ctx.evaluate(r, "exit") for r in rules.exit_when] + [ctx.evaluate(r, "action") for r in rules.action_when]
               + [ctx.evaluate(r, "watch") for r in rules.watch_when])
    results += system_rules(p, names["price"], today, settings)
    stale: list[str] = []
    fresh = snap.freshness(max_age, now)
    if fresh == UNAVAILABLE:
        stale.append(f"option quotes unavailable ({snap.error or 'empty chain'})")
    elif fresh == CACHED:
        age = snap.age_seconds(now)
        stale.append(f"option quotes are an older snapshot ({snap.error})" if snap.error else
                     f"option quotes are {int((age or 0) // 60)} min old (limit {int(max_age // 60)} min)")
    no_chain = fresh == UNAVAILABLE
    if v.status != "AVAILABLE" and not no_chain:          # with no chain at all, every leg fails the same way
        stale += v.reasons or ["the position can't be fully valued"]
    unknown = [r for r in results if r.result is None]
    data_gaps = [r for r in unknown if r.error is None and ("pnl" in r.values or "price" in r.values
                                                           or "unavailable" in (r.reason or "") or no_chain
                                                           or any(r.values.get(k) is None for k in r.values))]
    if no_chain or v.status != "AVAILABLE":
        if data_gaps:
            stale.append(f"{len(data_gaps)} rule{'s' if len(data_gaps) != 1 else ''} can't be checked without that data: "
                         + ", ".join(f"`{r.rule}`" for r in data_gaps))
        unknown = [r for r in unknown if r not in data_gaps]
    for r in unknown:
        stale.append(f"rule `{r.rule}`: {r.reason}")
    hit = {k: [r for r in results if r.result and r.kind == k] for k in ("exit", "action", "watch", "system")}
    if stale:
        status, reasons = STALE, list(dict.fromkeys(stale))
        also = hit["exit"] + hit["action"] + hit["system"]
        reasons += [f"also: {explain(r, previous)}" for r in also]
    elif hit["exit"]:
        status, reasons = EXIT, [explain(r, previous) for r in hit["exit"]]
    elif hit["action"] or hit["system"]:
        status, reasons = ACTION, [explain(r, previous) for r in hit["action"] + hit["system"]]
    elif hit["watch"]:
        status, reasons = WATCH, [explain(r, previous) for r in hit["watch"]]
    else:
        n = len([r for r in results if r.kind != "system"])
        status, reasons = HOLD, [f"no exit, action or watch rule holds ({n} rule{'s' if n != 1 else ''} checked)"]
    return PositionCheck(status, reasons, results, names, why)


def headline(status: str, symbol: str, reasons: list[str]) -> str:
    """'WATCH: IWM — short_strike_otm_pct < 3 (short_strike_otm_pct = 1.8%, was 2.4%)'"""
    return f"{LABELS[status]}: {symbol} — " + "; ".join(reasons)


def finite(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
