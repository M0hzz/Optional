"""Rule conditions for positions, watched contracts and symbol alerts.

Conditions use the scans.yaml expression language (optlab.live.expr): `pnl_pct >= 50`, `dte <= 21`,
`rsi(14) < 30 and price < 640`. Each evaluates to one of three results:

    True     the condition holds on this pass
    False    it doesn't
    None     it can't be evaluated: a value it uses is unavailable (no quote, no Greeks, not enough bars).
             Unknown is never treated as False: the position's status becomes STALE DATA.

A value that doesn't apply (the short-strike distance of a position with no short legs) is NaN, as in the
existing monitor: comparisons with it are False, so the rule simply doesn't fire. A value that applies but
is missing is None, and makes the rule unknown.

Every result keeps the values it used, so a status always says exactly why it was assigned.
"""
from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from ..live.expr import Evaluator, ExprError, last, names_used, parse
from ..live.indicators import bar_functions
from ..live.scanner import CHAIN_NAMES, NO_EARNINGS

POSITION_NAMES = {
    "pnl": "open P&L in dollars (estimated value − entry cost − opening fees)",
    "pnl_pct": "open P&L as a % of the entry cost (debit paid or credit received, plus fees)",
    "value": "estimated position value in dollars (mid prices)",
    "entry": "entry cost in dollars (+ debit paid, − credit received)",
    "dte": "days to the nearest option expiration (9999 for stock only)",
    "short_strike_otm_pct": "% the underlying is from the nearest short strike (negative = in the money)",
    "position_delta": "position delta in shares",
    "position_gamma": "position gamma (delta change per $1)",
    "theta_day": "position theta in dollars per day",
    "position_vega": "position vega in dollars per 1 vol point",
    "days_to_earnings": "days to the next earnings date (9999 if none known)",
    "earnings_before_expiry": "true if earnings fall on or before the nearest expiration",
    "price": "underlying price",
    "max_spread_pct": "widest bid/ask spread among the option legs, % of mid",
    "min_open_interest": "smallest open interest among the option legs",
    "loss_pct_of_max": "current loss as a % of the maximum loss (bounded risk only; 0 when profitable)",
    "short_itm": "true if any short option leg is in the money",
}
CONTRACT_NAMES = {
    "bid": "bid", "ask": "ask", "mid": "mid of bid and ask", "last": "last trade", "volume": "volume today",
    "open_interest": "open interest", "oi": "open interest", "iv": "implied volatility (0.25 = 25%)",
    "delta": "delta", "abs_delta": "absolute delta", "gamma": "gamma", "theta": "theta per share per day",
    "vega": "vega per share", "spread": "ask − bid", "spread_pct": "bid/ask spread, % of mid",
    "dte": "days to expiration", "strike": "strike", "price": "underlying price",
    "underlying_price": "underlying price", "intrinsic": "intrinsic value", "extrinsic": "extrinsic value",
    "breakeven": "breakeven at expiration for a buyer", "otm_pct": "% out of the money (negative = in the money)",
    "target_entry": "your target entry price", "days_to_earnings": "days to the next earnings date (9999 if none)",
}
SYMBOL_NAMES = {"price": "underlying price (from the option chain snapshot)",
                "days_to_earnings": "days to the next earnings date (9999 if none known)"}
CHAIN_DESCR = {"atm_iv": "~30-day at-the-money implied volatility", "skew": "25-delta put IV − call IV",
               "put_call_volume": "put/call volume ratio", "put_call_oi": "put/call open-interest ratio",
               "option_volume": "total option volume"}
TARGET_NAMES = {"position": POSITION_NAMES, "contract": CONTRACT_NAMES, "symbol": SYMBOL_NAMES}


@lru_cache(maxsize=1)
def bar_vocabulary() -> tuple[frozenset[str], frozenset[str]]:
    """Names and functions available on price bars (close, volume, sma(), rsi(), ...)."""
    idx = pd.date_range("2026-01-01", periods=30, freq="D")
    df = pd.DataFrame({k: np.linspace(1, 2, 30) for k in ("open", "high", "low", "close", "volume")}, index=idx)
    names, funcs = bar_functions(df, "1d")
    return frozenset(names), frozenset(funcs)


def allowed_names(target: str) -> set[str]:
    bn, bf = bar_vocabulary()
    return set(TARGET_NAMES[target]) | set(CHAIN_NAMES) | set(bn) | set(bf)


def validate(text: str, target: str) -> str | None:
    """None if the condition is valid for this target, else a message for the user."""
    try:
        used = names_used(text)
    except ExprError as exc:
        return str(exc)
    unknown = sorted(used - allowed_names(target))
    if unknown:
        import difflib

        hints = [m for u in unknown for m in difflib.get_close_matches(u, allowed_names(target), n=2)]
        return (f"Unknown name{'s' if len(unknown) > 1 else ''} {', '.join(unknown)} in {text!r}"
                + (f"; did you mean {', '.join(dict.fromkeys(hints))}?" if hints else "."))
    return None


def split_conditions(text: str | None) -> list[str]:
    """Watched-contract alert conditions are stored as text: one per line (or separated by ';')."""
    if not text:
        return []
    return [c.strip() for part in text.splitlines() for c in part.split(";") if c.strip() and not c.strip().startswith("#")]


# ------------------------------------------------------------------ bars
_STEP = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
         "30m": timedelta(minutes=30), "1h": timedelta(hours=1)}


def completed_only(bars: pd.DataFrame, timeframe: str, now_market: datetime) -> pd.DataFrame:
    """Drop the last bar if its period hasn't ended yet. Bar times are exchange-local (America/New_York)
    and naive, as optlab.live.feed returns them. `now_market` is aware, in the market time zone."""
    if bars is None or bars.empty:
        return bars
    t = pd.Timestamp(bars.index[-1])
    now = pd.Timestamp(now_market.replace(tzinfo=None))
    if timeframe in _STEP:
        done = t + _STEP[timeframe] <= now
    elif timeframe == "1d":
        done = t.date() < now.date() or now.time() >= datetime.strptime("16:00", "%H:%M").time()
    elif timeframe == "1wk":
        done = t + pd.Timedelta(days=7) <= now
    else:
        done = True
    return bars if done else bars.iloc[:-1]


# ------------------------------------------------------------------ evaluation
@dataclass
class RuleResult:
    kind: str                          # exit | action | watch | alert | condition | system
    rule: str
    result: bool | None
    values: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None          # why unknown, or why a system rule fired
    error: str | None = None           # the condition itself is invalid

    def as_dict(self) -> dict:
        return {"kind": self.kind, "rule": self.rule, "result": self.result, "values": _jsonable(self.values),
                "reason": self.reason, "error": self.error}


def _jsonable(d: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, (bool, np.bool_)):
            out[k] = bool(v)
        elif isinstance(v, (int, np.integer)):
            out[k] = int(v)
        elif isinstance(v, (float, np.floating)):
            out[k] = "n/a" if not math.isfinite(float(v)) else round(float(v), 6)
        else:
            out[k] = None if v is None else str(v)
    return out


def _missing(v) -> bool:
    if v is None:
        return True
    try:
        return isinstance(v, (float, np.floating)) and not math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


class Context:
    """The values one subject (a position, contract or symbol) exposes to its conditions on one pass."""

    def __init__(self, target: str, names: dict[str, Any], why_missing: dict[str, str] | None = None,
                 bars: pd.DataFrame | None = None, bars_error: str | None = None, timeframe: str = "1d",
                 chain: pd.DataFrame | None = None):
        self.target, self.timeframe, self.chain = target, timeframe, chain
        self.names = {**{k: None for k in CHAIN_NAMES}, **names}
        self.why = dict(why_missing or {})
        self._stats_done = False
        self.bars, self.bars_error = bars, bars_error
        self._series: Evaluator | None = None
        if bars is not None and not bars.empty:
            n, f = bar_functions(bars, timeframe)
            self._series = Evaluator(n, f, memo={})

    def _chain_stats(self) -> None:
        """atm_iv, skew, put/call ratios: computed only when a rule uses them."""
        from ..live.scanner import chain_stats

        self._stats_done = True
        if self.chain is None or self.chain.empty:
            self.why.update({k: "no option chain" for k in CHAIN_NAMES})
            return
        try:
            stats = chain_stats(self.chain)
        except Exception as exc:
            self.why.update({k: f"chain statistics failed ({type(exc).__name__})" for k in CHAIN_NAMES})
            return
        for k, v in stats.items():
            self.names[k] = None if _missing(v) else float(v)
            if self.names[k] is None:
                self.why[k] = "not enough option data in the chain"

    def evaluate(self, text: str, kind: str) -> RuleResult:
        err = validate(text, self.target)
        if err:
            return RuleResult(kind, text, None, error=err, reason=err)
        used = names_used(text)
        if used & set(CHAIN_NAMES) and not self._stats_done:
            self._chain_stats()
        bn, bf = bar_vocabulary()
        values: dict[str, Any] = {}
        for n in sorted(used & set(self.names)):
            values[n] = self.names[n]
        missing = [n for n in values if values[n] is None]
        if missing:
            why = "; ".join(f"{n}: {self.why.get(n, 'unavailable')}" for n in missing)
            return RuleResult(kind, text, None, values, f"Can't evaluate: {why}.")
        needs_bars = bool((used - set(self.names)) & (bn | bf))
        if needs_bars and self._series is None:
            return RuleResult(kind, text, None, values,
                              f"Can't evaluate: needs {self.timeframe} price bars ({self.bars_error or 'none available'}).")
        ev = Evaluator({k: v for k, v in self.names.items() if v is not None}, {}, series_eval=self._series)
        try:
            for node in ast.walk(parse(text)):
                if needs_bars and isinstance(node, ast.Call) and node.func.id in bf:  # type: ignore[attr-defined]
                    txt = ast.unparse(node)
                    v = last(ev(txt))
                    if node.func.id not in ("crosses_above", "crosses_below"):      # type: ignore[attr-defined]
                        values[txt] = v
                        if _missing(v):
                            return RuleResult(kind, text, None, values,
                                              f"Can't evaluate: not enough {self.timeframe} bars for {txt}.")
                elif needs_bars and isinstance(node, ast.Name) and node.id in bn and node.id not in self.names:
                    values[node.id] = last(ev(node.id))
            v = last(ev(text))
        except ExprError as exc:
            return RuleResult(kind, text, None, values, str(exc), error=str(exc))
        except (TypeError, ValueError, ZeroDivisionError, ArithmeticError) as exc:
            return RuleResult(kind, text, None, values, f"Can't evaluate: {type(exc).__name__}: {exc}.")
        if _missing(v):
            return RuleResult(kind, text, None, values, "Can't evaluate: the result is undefined (missing data).")
        return RuleResult(kind, text, bool(v), values)


# ------------------------------------------------------------------ explanations
def fmt_value(name: str, v) -> str:
    if v is None:
        return "unavailable"
    if v == "n/a" or (isinstance(v, (float, np.floating)) and not math.isfinite(float(v))):
        return "not applicable"
    if isinstance(v, (bool, np.bool_)):
        return "yes" if v else "no"
    if name in ("dte", "days_to_earnings") and isinstance(v, (int, float, np.integer, np.floating)):
        return "none known" if v >= NO_EARNINGS else f"{int(v)}"
    if isinstance(v, (int, np.integer)):
        return f"{int(v):,}"
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if name.endswith("_pct") or name == "loss_pct_of_max":
            return f"{f:+.1f}%" if name == "pnl_pct" else f"{f:.1f}%"
        if name in ("pnl", "value", "entry", "theta_day", "position_vega"):
            return f"{'−' if f < 0 else '+' if name in ('pnl', 'theta_day') and f > 0 else ''}${abs(f):,.2f}"
        return f"{f:,.4g}"
    return str(v)


def explain(r: RuleResult, previous: dict | None = None) -> str:
    """'short_strike_otm_pct < 3 (short_strike_otm_pct = 1.8%)', with the previous pass's value when it changed."""
    if r.kind == "system":
        return r.reason or r.rule
    parts = []
    for k, v in r.values.items():
        s = f"{k} = {fmt_value(k, v)}"
        if previous and k in previous and previous[k] is not None and previous[k] != v and not isinstance(v, bool):
            s += f", was {fmt_value(k, previous[k])}"
        parts.append(s)
    return r.rule + (f" ({'; '.join(parts)})" if parts else "")
