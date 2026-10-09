"""Strategy templates. Each builder returns a list of Legs sized at one unit.

Strikes are chosen by target delta, then rounded to a listed-strike increment.
"""
from __future__ import annotations

from ..pricing.engine import Leg
from ..pricing.fast_bs import strike_for_delta


def _round_strike(k: float, spot: float) -> float:
    from ..data.sources import strike_step

    step = strike_step(spot)
    return round(round(k / step) * step, 2)


def _k(spot, dte, vol, delta, is_call, rate):
    return _round_strike(float(strike_for_delta(spot, dte / 365, rate, 0.0, vol, delta, is_call)), spot)


def covered_call(spot, vol, dte=35, short_delta=0.25, rate=0.04, **_):
    return [Leg("stock", 0, 0, 1, entry_price=spot),
            Leg("call", _k(spot, dte, vol, short_delta, True, rate), dte, -1, vol)]


def cash_secured_put(spot, vol, dte=35, short_delta=0.25, rate=0.04, **_):
    return [Leg("put", _k(spot, dte, vol, short_delta, False, rate), dte, -1, vol)]


def put_credit_spread(spot, vol, dte=35, short_delta=0.25, spread_width_pct=5.0, rate=0.04, **_):
    short_k = _k(spot, dte, vol, short_delta, False, rate)
    long_k = _round_strike(short_k - spot * spread_width_pct / 100, spot)
    return [Leg("put", short_k, dte, -1, vol), Leg("put", long_k, dte, 1, vol)]


def call_credit_spread(spot, vol, dte=35, short_delta=0.25, spread_width_pct=5.0, rate=0.04, **_):
    short_k = _k(spot, dte, vol, short_delta, True, rate)
    long_k = _round_strike(short_k + spot * spread_width_pct / 100, spot)
    return [Leg("call", short_k, dte, -1, vol), Leg("call", long_k, dte, 1, vol)]


def iron_condor(spot, vol, dte=35, short_delta=0.16, spread_width_pct=5.0, rate=0.04, **_):
    return (put_credit_spread(spot, vol, dte, short_delta, spread_width_pct, rate)
            + call_credit_spread(spot, vol, dte, short_delta, spread_width_pct, rate))


def long_straddle(spot, vol, dte=35, rate=0.04, **_):
    k = _round_strike(spot, spot)
    return [Leg("call", k, dte, 1, vol), Leg("put", k, dte, 1, vol)]


STRATEGIES = {
    "Covered call": covered_call,
    "Cash-secured put": cash_secured_put,
    "Put credit spread": put_credit_spread,
    "Call credit spread": call_credit_spread,
    "Iron condor": iron_condor,
    "Long straddle": long_straddle,
}


def max_loss(legs: list[Leg]) -> float:
    """Worst-case loss per unit in dollars (positive number), checked on a wide price grid."""
    import numpy as np

    from ..pricing.engine import payoff_at_expiry

    strikes = [l.strike for l in legs if l.option_type != "stock"] or [100.0]
    hi = max(strikes) * 3
    grid = np.concatenate([np.linspace(0.0, hi, 3001), strikes])
    worst = float(payoff_at_expiry(legs, grid).min())
    return max(-worst, 0.0)
