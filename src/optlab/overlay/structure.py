"""Strategy-builder and simulator overlay: the structure's risks, its fit to the account and stress scenarios.

All payoffs use the strategy lab's legs and its pricing model (Black-Scholes, European), so these are
MODELED values; where a stored chain has the contract, the model is compared with the quote.

    breakevens         prices where the expiry payoff crosses zero (linear interpolation on a 0.2%-step grid)
    expected move      spot × IV × √(DTE ÷ 365)
    ATR move           ATR(14) × √(DTE)
    historical move    share of past DTE-day windows (daily closes) whose |move| reached the breakeven distance
    leg removal        each leg dropped in turn; undefined when the remaining legs are net short calls
"""
from __future__ import annotations


import numpy as np
import pandas as pd

from ..pricing.engine import Leg, entry_cost, payoff_at_expiry, price_position
from ..strategies import max_loss
from .account import Profile, legs_fit


def _net_short_calls(legs) -> bool:
    return sum(lg.quantity for lg in legs if lg.option_type == "call") + sum(
        lg.quantity for lg in legs if lg.option_type == "stock") < 0


def breakevens(legs, spot: float) -> list[float]:
    grid = np.linspace(spot * 0.3, spot * 2.0, 8501)
    pay = payoff_at_expiry(legs, grid)
    out = []
    sg = np.sign(np.round(pay, 9))
    for i in range(len(grid) - 1):
        if sg[i] == 0 and (i == 0 or sg[i - 1] != 0):
            out.append(float(grid[i]))                        # lands exactly on a grid point
        elif sg[i] * sg[i + 1] < 0:
            x0, x1, y0, y1 = grid[i], grid[i + 1], pay[i], pay[i + 1]
            out.append(float(x0 - y0 * (x1 - x0) / (y1 - y0)))
    return out


def hist_move_share(close: pd.Series, days: int, move_pct: float) -> float:
    """Share (%) of past `days`-session windows whose absolute close-to-close move was ≥ move_pct."""
    c = close.dropna().astype(float)
    if len(c) <= days + 20 or days < 1:
        return float("nan")
    m = (c.shift(-days) / c - 1).dropna().abs() * 100
    return float(100 * (m >= move_pct).mean())


def analyze(legs: list[Leg], spot: float, rate: float, dte: int, iv: float, p: Profile, symbol: str,
            close: pd.Series | None, atr_val: float | None, th: dict, outlook: dict | None = None,
            quotes: pd.DataFrame | None = None, name: str = "Strategy") -> dict:
    m = 100
    cost = entry_cost(legs)
    undefined = _net_short_calls(legs)
    worst = None if undefined else max_loss(legs)
    grid = np.linspace(0, max([lg.strike for lg in legs if lg.option_type != "stock"] or [spot]) * 3, 3001)
    pay = payoff_at_expiry(legs, grid)
    best = None if (sum(lg.quantity for lg in legs if lg.option_type in ("call", "stock")) > 0) else float(pay.max())
    bes = breakevens(legs, spot)
    fit = legs_fit(p, legs, spot, symbol, name)
    leg_rows = [{"action": "Buy" if lg.quantity > 0 else "Sell", "quantity": abs(lg.quantity), "type": lg.option_type.capitalize(),
                 "strike": lg.strike if lg.option_type != "stock" else None, "dte": lg.days if lg.option_type != "stock" else None,
                 "model_price": lg.entry_price} for lg in legs]
    risks = []
    if undefined:
        risks.append(("UNDEFINED", "Undefined loss: the position is net short calls."))
    removal = []
    for k, lg in enumerate(legs):
        rest = legs[:k] + legs[k + 1:]
        if not rest:
            continue
        und = _net_short_calls(rest)
        removal.append({"removed": f"{'Long' if lg.quantity > 0 else 'Short'} {lg.option_type} {lg.strike:g}"
                        if lg.option_type != "stock" else "Shares",
                        "max_loss_after": None if und else max_loss(rest), "undefined_after": und})
        if und and not undefined:
            risks.append(("LEG_REMOVAL", f"Removing the {removal[-1]['removed']} leaves undefined (unlimited) risk."))
    if len({lg.days for lg in legs if lg.option_type != "stock"}) > 1:
        risks.append(("EXPIRY_MISMATCH", "Legs expire on different dates."))
    shorts = [lg for lg in legs if lg.option_type != "stock" and lg.quantity < 0]
    for lg in shorts:
        dist = 100 * abs(spot - lg.strike) / spot
        itm = (lg.option_type == "call" and spot > lg.strike) or (lg.option_type == "put" and spot < lg.strike)
        if itm:
            risks.append(("EXERCISE", f"Short {lg.option_type} {lg.strike:g} is in the money: early assignment is possible "
                                      "(American-style options)."))
        if dist <= float(th["pin_risk_pct"]) and lg.days <= 7:
            risks.append(("PIN", f"Pin risk: price is within {dist:.1f}% of the short {lg.strike:g} strike with "
                                 f"{lg.days:g} days left."))
    if fit.assignment_obligation:
        risks.append(("ASSIGNMENT", f"Assignment exposure ${fit.assignment_obligation:,.0f} vs capacity ${p.assignment_capacity:,.0f}."
                      + (" The account could not support the shares." if fit.assignment_obligation > p.assignment_capacity else "")))
    if fit.status == "DOES_NOT_FIT_ACCOUNT":
        risks.append(("ACCOUNT", f"Outside the account limits ({len(fit.reasons)} reason{'s' if len(fit.reasons) != 1 else ''} "
                                 "listed under the metrics)."))
    exp_move = spot * iv * np.sqrt(max(dte, 1) / 365)
    atr_move = atr_val * np.sqrt(max(dte, 1)) if atr_val else None
    be_rows = []
    for b in bes:
        dist = 100 * (b / spot - 1)
        be_rows.append({"breakeven": b, "distance_pct": dist, "vs_expected_move": abs(b - spot) / exp_move if exp_move else np.nan,
                        "atr_multiples": abs(b - spot) / atr_move if atr_move else np.nan,
                        "hist_reach_pct": hist_move_share(close, dte, abs(dist)) if close is not None else np.nan})
        if exp_move and abs(b - spot) > 2 * exp_move and cost > 0:
            risks.append(("BREAKEVEN", f"Breakeven {b:,.2f} is more than 2 expected moves away ({abs(b - spot) / exp_move:.1f}×)."))
    prof = grid[pay > 0]
    if len(prof) and best is not None:
        width = prof.max() - prof.min()
        if atr_val and width < atr_val:
            risks.append(("NARROW", f"The profitable range at expiry is only {width:,.2f} wide, less than one ATR ({atr_val:,.2f})."))
    # slippage: crossing a 5% spread on every leg (or the quoted spreads when available)
    slip = sum(abs(lg.quantity) * m * (lg.entry_price or 0) * 0.05 for lg in legs if lg.option_type != "stock")
    if best and best > 0 and slip > 0.2 * best:
        risks.append(("SLIPPAGE", f"Crossing a 5% bid/ask spread on each leg costs about ${slip:,.0f}, more than 20% of the "
                                  f"${best:,.0f} maximum profit."))
    quote_rows = []
    if quotes is not None and not quotes.empty:
        for lg in legs:
            if lg.option_type == "stock":
                continue
            q = quotes[quotes["type"].str.lower() == lg.option_type]
            if q.empty:
                continue
            q = q.iloc[((q["strike"] - lg.strike).abs() + (q["dte"] - lg.days).abs() / 10).argsort()[:1]]
            r = q.iloc[0]
            quote_rows.append({"leg": f"{lg.option_type} {lg.strike:g}", "quoted_contract": f"{r['strike']:g} {r['expiration']}",
                               "bid": r["bid"], "ask": r["ask"], "model": lg.entry_price,
                               "model_vs_mid_pct": 100 * (lg.entry_price / ((r["bid"] + r["ask"]) / 2) - 1)
                               if r["bid"] + r["ask"] > 0 else np.nan,
                               "liquidity": r.get("liquidity", ""), "flags": r.get("flags", "")})
            if r.get("liquidity") == "POOR_LIQUIDITY":
                risks.append(("ILLIQUID", f"The {lg.option_type} {lg.strike:g} leg's nearest quote has poor liquidity "
                                          f"({r.get('flags', '')})."))
    g = price_position(legs, spot, rate, style="european")
    fc = []
    if outlook:
        d = outlook.get("direction")
        if d in ("Bullish", "Bearish"):
            ok = (g["delta"] > 0) == (d == "Bullish")
            fc.append({"check": "Direction", "status": "pass" if ok else "fail",
                       "detail": f"{d} outlook; position delta {g['delta']:+,.1f} shares"})
        lo, hi = outlook.get("range", (None, None))
        if lo and hi and hi > lo:
            xs = np.linspace(lo, hi, 201)
            share = 100 * (payoff_at_expiry(legs, xs) > 0).mean()
            fc.append({"check": "Expected range", "status": "pass" if share >= 50 else "fail",
                       "detail": f"{share:.0f}% of {lo:,.2f}–{hi:,.2f} is profitable at expiry"})
        hold = outlook.get("holding_days")
        if hold:
            fc.append({"check": "Holding period", "status": "pass" if hold <= dte else "fail",
                       "detail": f"plan to hold {hold} days; options expire in {dte}"})
        vo = outlook.get("volatility")
        if vo in ("Rising", "Falling"):
            ok = (g["vega"] > 0) == (vo == "Rising")
            fc.append({"check": "Volatility outlook", "status": "pass" if ok else "fail",
                       "detail": f"{vo.lower()} volatility; vega {g['vega']:+,.0f} $/pt"})
    return {"legs": pd.DataFrame(leg_rows), "net": cost, "max_loss": worst, "max_profit": best, "breakevens": pd.DataFrame(be_rows),
            "expected_move": exp_move, "atr_move": atr_move, "defined": not undefined, "fit": fit, "risks": risks,
            "removal": pd.DataFrame(removal), "quotes": pd.DataFrame(quote_rows), "greeks": g, "forecast": pd.DataFrame(fc),
            "width": _width(legs)}


def _width(legs) -> float | None:
    out = []
    for t in ("call", "put"):
        ks = sorted({lg.strike for lg in legs if lg.option_type == t})
        if len(ks) >= 2:
            out.append(ks[-1] - ks[0])
    return max(out) if out else None


def stress(legs: list[Leg], spot: float, rate: float, dte: int, iv: float, atr_val: float | None, p: Profile,
           max_loss_val: float | None, bes: list[float], slippage_pct: float = 5.0) -> pd.DataFrame:
    """Configured stress scenarios. P&L = model value after the scenario − entry cost (dollars, one unit).
    IV changes are relative (−10% means IV × 0.9), applied as the same shift to every leg."""
    cost = entry_cost(legs)
    a = atr_val or spot * 0.02
    base_vol = float(np.mean([lg.vol for lg in legs if lg.option_type != "stock"] or [iv]))
    rows = []

    def add(name, ds=0.0, ivp=0.0, days=0.0, extra=0.0):
        s = max(spot + ds, 0.01)
        days = min(days, dte)
        v = price_position(legs, s, rate, style="european", vol_shift=base_vol * ivp, days_forward=days)
        pnl = v["value"] - cost - extra
        crossed = any((spot - b) * (s - b) < 0 for b in bes)
        rows.append({"scenario": name, "price": s, "days_passed": days, "pnl": pnl,
                     "pct_of_account": 100 * pnl / p.total_buying_power, "account_after": p.total_buying_power + pnl,
                     "crosses_breakeven": crossed, "delta": v["delta"], "gamma": v["gamma"], "theta": v["theta"]})

    for k in (-2, -1, -0.5, 0.5, 1, 2):
        add(f"Underlying {k:+g} ATR now", ds=k * a)
    for ivp in (-0.25, -0.10, 0.10, 0.25):
        add(f"IV {ivp:+.0%}", ivp=ivp)
    for d in (1, 3, 7):
        add(f"{d} day{'s' if d > 1 else ''} pass", days=d)
    slip = sum(abs(lg.quantity) * 100 * (lg.entry_price or 0) * slippage_pct / 100 for lg in legs if lg.option_type != "stock")
    add(f"Worse-than-mid entry ({slippage_pct:g}% of each premium)", extra=slip)
    add(f"Conservative exit tomorrow ({slippage_pct:g}% each way)", days=1, extra=2 * slip)
    if dte > 5:
        add("Expiration week (5 days left)", days=dte - 5)
        add("Expiration week, underlying −1 ATR", ds=-a, days=dte - 5)
    add("At expiration, price unchanged", days=dte)
    if max_loss_val:
        for f in (0.25, 0.5, 1.0):
            rows.append({"scenario": f"{f:.0%} of maximum loss", "price": np.nan, "days_passed": np.nan,
                         "pnl": -f * max_loss_val, "pct_of_account": -100 * f * max_loss_val / p.total_buying_power,
                         "account_after": p.total_buying_power - f * max_loss_val, "crosses_breakeven": None,
                         "delta": np.nan, "gamma": np.nan, "theta": np.nan})
    return pd.DataFrame(rows)


def greek_path(legs, spot, rate, dte) -> dict:
    """Theta and gamma now vs with 7 and 2 days left: shows theta acceleration and expiry gamma."""
    out = {}
    for left in (dte, 7, 2):
        if left <= dte:
            v = price_position(legs, spot, rate, style="european", days_forward=dte - left)
            out[left] = {"theta": v["theta"], "gamma": v["gamma"]}
    return out


def assignment_scenario(legs, spot: float, p: Profile) -> list[str]:
    out = []
    for lg in legs:
        if lg.option_type == "stock" or lg.quantity >= 0:
            continue
        need = abs(lg.quantity) * 100 * lg.strike
        if lg.option_type == "put":
            out.append(f"Short put {lg.strike:g} assigned: buy {abs(lg.quantity) * 100} shares for ${need:,.0f}; "
                       f"capacity ${p.assignment_capacity:,.0f} → {'covered' if need <= p.assignment_capacity else 'NOT covered'}.")
        else:
            covered = sum(x.quantity for x in legs if x.option_type in ("stock",)) > 0
            out.append(f"Short call {lg.strike:g} assigned: deliver {abs(lg.quantity) * 100} shares"
                       + (" from the shares held." if covered else f" you don't own (a short-stock position worth about "
                          f"${abs(lg.quantity) * 100 * spot:,.0f}); a cash account can't hold that."))
    return out
