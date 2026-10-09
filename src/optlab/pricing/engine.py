"""QuantLib options engine: fair value, Greeks, implied volatility, scenario P&L.

Listed US equity options are American; index options (SPX, NDX, RUT) are
European. Pass style="american" or "european" accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import QuantLib as ql

DAY_COUNT = ql.Actual365Fixed()
CALENDAR = ql.UnitedStates(ql.UnitedStates.NYSE)
MULTIPLIER = 100


def _qd(d: date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


@dataclass
class OptionSpec:
    spot: float
    strike: float
    days: float                 # calendar days to expiry
    vol: float                  # annualized, e.g. 0.25
    option_type: str = "put"    # "call" | "put"
    rate: float = 0.04
    div: float = 0.0
    style: str = "american"     # "american" | "european"
    valuation: date = field(default_factory=date.today)


def _build(spec: OptionSpec, spot_q: ql.SimpleQuote, vol_q: ql.SimpleQuote):
    today = _qd(spec.valuation)
    ql.Settings.instance().evaluationDate = today
    expiry = today + max(int(round(spec.days)), 1)
    payoff = ql.PlainVanillaPayoff(ql.Option.Call if spec.option_type == "call" else ql.Option.Put, spec.strike)
    exercise = ql.AmericanExercise(today, expiry) if spec.style == "american" else ql.EuropeanExercise(expiry)
    option = ql.VanillaOption(payoff, exercise)

    rate_ts = ql.YieldTermStructureHandle(ql.FlatForward(today, spec.rate, DAY_COUNT))
    div_ts = ql.YieldTermStructureHandle(ql.FlatForward(today, spec.div, DAY_COUNT))
    vol_ts = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(today, CALENDAR, ql.QuoteHandle(vol_q), DAY_COUNT))
    process = ql.BlackScholesMertonProcess(ql.QuoteHandle(spot_q), div_ts, rate_ts, vol_ts)

    if spec.style == "american":
        engine = ql.FdBlackScholesVanillaEngine(process, 200, 200)
    else:
        engine = ql.AnalyticEuropeanEngine(process)
    option.setPricingEngine(engine)
    return option, process


def price(spec: OptionSpec) -> dict:
    """Fair value and Greeks for one option (per share, not per contract).

    vega is per 1 vol point (0.01), theta per calendar day, rho per 1% rate.
    """
    spot_q, vol_q = ql.SimpleQuote(spec.spot), ql.SimpleQuote(spec.vol)
    option, _ = _build(spec, spot_q, vol_q)
    npv = option.NPV()

    if spec.style == "european":
        return {
            "price": npv,
            "delta": option.delta(),
            "gamma": option.gamma(),
            "vega": option.vega() / 100.0,
            "theta": option.thetaPerDay(),
            "rho": option.rho() / 100.0,
        }

    # American: FD engine gives delta/gamma/theta; vega and rho by bumping.
    delta, gamma = option.delta(), option.gamma()
    vol_q.setValue(spec.vol + 0.01)
    vega = option.NPV() - npv
    vol_q.setValue(spec.vol)

    def _npv(**changes) -> float:
        bumped = OptionSpec(**{**spec.__dict__, **changes})
        return _build(bumped, ql.SimpleQuote(spec.spot), ql.SimpleQuote(spec.vol))[0].NPV()

    rho = _npv(rate=spec.rate + 0.01) - npv
    # theta: reprice with one fewer day to expiry (FD theta is unreliable near expiry)
    theta = (_npv(days=spec.days - 1) - npv) if spec.days > 1.5 else -npv + max(
        (spec.spot - spec.strike) if spec.option_type == "call" else (spec.strike - spec.spot), 0.0)
    return {"price": npv, "delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "rho": rho}


def implied_vol(market_price: float, spec: OptionSpec, lo: float = 0.005, hi: float = 5.0) -> float:
    """Solve for the vol that reproduces market_price. Returns nan if no solution."""
    spot_q, vol_q = ql.SimpleQuote(spec.spot), ql.SimpleQuote(spec.vol or 0.3)
    option, process = _build(spec, spot_q, vol_q)
    try:
        return option.impliedVolatility(market_price, process, 1e-6, 500, lo, hi)
    except RuntimeError:
        return float("nan")


# --------------------------------------------------------------------- positions
@dataclass
class Leg:
    option_type: str     # "call" | "put" | "stock"
    strike: float        # ignored for stock
    days: float          # days to expiry at entry; ignored for stock
    quantity: int        # +long / -short, in contracts (stock: shares/100 "lots")
    vol: float = 0.25
    entry_price: float | None = None   # per share; filled by price_position if None


def price_position(legs: list[Leg], spot: float, rate: float, div: float = 0.0,
                   style: str = "american", vol_shift: float = 0.0, days_forward: float = 0.0) -> dict:
    """Value and aggregate Greeks of a multi-leg position, in dollars."""
    total = {"value": 0.0, "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    for leg in legs:
        q = leg.quantity * MULTIPLIER
        if leg.option_type == "stock":
            total["value"] += q * spot
            total["delta"] += q
            continue
        d = max(leg.days - days_forward, 0.0)
        if d <= 0:
            intrinsic = max(spot - leg.strike, 0) if leg.option_type == "call" else max(leg.strike - spot, 0)
            total["value"] += q * intrinsic
            total["delta"] += q * (1.0 if (leg.option_type == "call" and spot > leg.strike) else
                                   -1.0 if (leg.option_type == "put" and spot < leg.strike) else 0.0)
            continue
        g = price(OptionSpec(spot, leg.strike, d, max(leg.vol + vol_shift, 0.01), leg.option_type, rate, div, style))
        total["value"] += q * g["price"]
        for k in ("delta", "gamma", "vega", "theta"):
            total[k] += q * g[k]
    total["dollar_delta"] = total["delta"] * spot
    return total


def fill_entry_prices(legs: list[Leg], spot: float, rate: float, div: float = 0.0, style: str = "american") -> list[Leg]:
    for leg in legs:
        if leg.entry_price is None:
            if leg.option_type == "stock":
                leg.entry_price = spot
            else:
                leg.entry_price = price(OptionSpec(spot, leg.strike, leg.days, leg.vol, leg.option_type, rate, div, style))["price"]
    return legs


def entry_cost(legs: list[Leg]) -> float:
    """Net debit (+) or credit (-) to open, in dollars."""
    return sum(l.quantity * MULTIPLIER * (l.entry_price or 0.0) for l in legs)


def payoff_at_expiry(legs: list[Leg], spots: np.ndarray) -> np.ndarray:
    """P&L at expiration across a vector of underlying prices, in dollars."""
    pnl = np.zeros_like(spots, dtype=float)
    for leg in legs:
        q = leg.quantity * MULTIPLIER
        if leg.option_type == "stock":
            pnl += q * (spots - leg.entry_price)
        elif leg.option_type == "call":
            pnl += q * (np.maximum(spots - leg.strike, 0) - leg.entry_price)
        else:
            pnl += q * (np.maximum(leg.strike - spots, 0) - leg.entry_price)
    return pnl


def scenario_grid(legs: list[Leg], spot: float, rate: float, div: float = 0.0,
                  spot_moves=(-0.15, -0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10),
                  vol_shifts=(-0.10, -0.05, 0.0, 0.05, 0.10, 0.20),
                  days_forward: float = 0.0, style: str = "european") -> dict:
    """P&L matrix (vol_shift x spot_move) versus entry, in dollars.

    Defaults to European pricing for speed; pass style="american" for precision.
    """
    fill_entry_prices(legs, spot, rate, div, style)
    cost = entry_cost(legs)
    grid = np.zeros((len(vol_shifts), len(spot_moves)))
    for i, dv in enumerate(vol_shifts):
        for j, mv in enumerate(spot_moves):
            v = price_position(legs, spot * (1 + mv), rate, div, style, dv, days_forward)["value"]
            grid[i, j] = v - cost
    return {"spot_moves": list(spot_moves), "vol_shifts": list(vol_shifts), "pnl": grid}


def expiry_date(days: int, start: date | None = None) -> date:
    return (start or date.today()) + timedelta(days=days)
