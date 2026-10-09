"""Quick options backtests, scored with vectorbt.

Historical option quotes are expensive, so this module reprices options from the
underlying's own history: implied vol = trailing realized vol x `iv_premium`, and
every leg is marked daily with Black-Scholes. A slippage haircut on premium and a
per-contract commission are charged on every open and close.

This is a screening tool: it tells you which strategy/DTE/delta combinations are
worth a closer look. It cannot see real skew, real spreads, early assignment or
dividends. Re-test anything promising in LEAN (integrations/lean) with real quotes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import vectorbt as vbt

from ..pricing.fast_bs import strike_for_delta

TRADING_DAYS = 252
_SQRT2 = math.sqrt(2.0)


def bs_price(s: float, k: float, t: float, r: float, q: float, vol: float, is_call: bool) -> float:
    """Scalar Black-Scholes; ~50x faster than NumPy for one value at a time."""
    if t <= 0 or vol <= 0:
        return max(s - k, 0.0) if is_call else max(k - s, 0.0)
    vs = vol * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * vol * vol) * t) / vs
    d2 = d1 - vs
    n = lambda x: 0.5 * (1.0 + math.erf(x / _SQRT2))  # noqa: E731
    if is_call:
        return s * math.exp(-q * t) * n(d1) - k * math.exp(-r * t) * n(d2)
    return k * math.exp(-r * t) * n(-d2) - s * math.exp(-q * t) * n(-d1)


@dataclass
class BTParams:
    strategy: str = "put_write"       # put_write | covered_call | put_spread | iron_condor
    dte: int = 35
    short_delta: float = 0.25
    width_pct: float = 5.0            # spread wing width, % of spot
    profit_take_pct: float = 50.0     # close early at this % of max profit (0 disables)
    stop_loss_mult: float = 0.0       # close if loss exceeds N x credit (0 disables)
    iv_premium: float = 1.15          # implied / realized vol ratio
    rv_window: int = 20
    slippage_pct: float = 3.0         # % of premium lost per fill
    commission: float = 0.65          # $ per contract per fill
    rate: float = 0.04
    # Share of the account committed to each trade's margin. Cash-secured puts and
    # covered calls use the whole account (1.0). Spreads risk only their width, so
    # committing the whole account to them would be all-in leverage; default 25%.
    capital_fraction: float | None = None
    collateral_yield: bool = True     # cash collateral earns `rate` (T-bills), as in CBOE PUT index


def _legs(p: BTParams, s: float, t: float, iv: float):
    """(is_call, strike, qty) tuples per one unit; stock handled separately."""
    put_k = float(strike_for_delta(s, t, p.rate, 0, iv, p.short_delta, False))
    call_k = float(strike_for_delta(s, t, p.rate, 0, iv, p.short_delta, True))
    w = s * p.width_pct / 100
    if p.strategy == "put_write":
        return [(False, put_k, -1)]
    if p.strategy == "covered_call":
        return [(True, call_k, -1)]
    if p.strategy == "put_spread":
        return [(False, put_k, -1), (False, put_k - w, 1)]
    if p.strategy == "iron_condor":
        return [(False, put_k, -1), (False, put_k - w, 1), (True, call_k, -1), (True, call_k + w, 1)]
    raise ValueError(p.strategy)


def _capital(p: BTParams, s: float, legs) -> float:
    if p.strategy == "put_write":
        return legs[0][1]
    if p.strategy == "covered_call":
        return s
    return s * p.width_pct / 100  # defined-risk spreads: margin = wing width


def simulate(close: pd.Series, p: BTParams) -> tuple[pd.Series, pd.DataFrame]:
    """Daily strategy returns and a trade log, for one unit rolled continuously."""
    close = close.dropna().astype(float)
    logret = np.log(close).diff()
    rv = (logret.rolling(p.rv_window).std() * np.sqrt(TRADING_DAYS)).bfill().clip(0.05, 2.0)
    iv = (rv * p.iv_premium).to_numpy()
    px = close.to_numpy()
    n = len(px)
    rets = np.zeros(n)
    trades = []
    i = p.rv_window
    hold_days = max(int(round(p.dte * TRADING_DAYS / 365)), 1)
    stock = p.strategy == "covered_call"
    frac = p.capital_fraction or (1.0 if p.strategy in ("put_write", "covered_call") else 0.25)

    while i < n - 1:
        s0 = px[i]
        legs = _legs(p, s0, p.dte / 365, iv[i])
        cap = _capital(p, s0, legs)
        acct = cap / frac                                  # account size backing one unit
        entry = sum(q * bs_price(s0, k, p.dte / 365, p.rate, 0, iv[i], c) for c, k, q in legs)
        credit = -entry                                    # >0 for premium sellers
        costs = abs(credit) * p.slippage_pct / 100 + p.commission * len(legs) / 100
        mtm_prev = entry
        end = min(i + hold_days, n - 1)
        exit_reason = "expiry"
        exit_cost = 0.0
        j = i
        for j in range(i + 1, end + 1):
            t = max((end - j) / TRADING_DAYS, 0.0)
            mtm = sum(q * bs_price(px[j], k, t, p.rate, 0, iv[j], c) for c, k, q in legs)
            day_pnl = mtm - mtm_prev + (px[j] - px[j - 1] if stock else 0.0)
            if j == i + 1:
                day_pnl -= costs
            mtm_prev = mtm
            profit = credit + mtm                          # option-leg P&L so far
            close_now = False
            if p.profit_take_pct and credit > 0 and profit >= credit * p.profit_take_pct / 100:
                close_now, exit_reason = True, "profit_take"
            elif p.stop_loss_mult and credit > 0 and -profit >= credit * p.stop_loss_mult:
                close_now, exit_reason = True, "stop_loss"
            if close_now and j < end:
                exit_cost = abs(mtm) * p.slippage_pct / 100 + p.commission * len(legs) / 100
                day_pnl -= exit_cost
            if p.collateral_yield:
                day_pnl += (acct - (cap if stock else 0.0)) * p.rate / TRADING_DAYS
            rets[j] = day_pnl / acct
            if close_now:
                break
        if exit_reason == "expiry" and end < i + hold_days:
            exit_reason = "open"
        trades.append({
            "entry": close.index[i], "exit": close.index[j], "spot_in": s0, "spot_out": px[j],
            "strikes": [round(k, 2) for _, k, _ in legs], "iv": round(iv[i], 4), "credit": credit,
            "pnl": (credit + mtm_prev) + ((px[j] - s0) if stock else 0.0) - costs - exit_cost,
            "capital": cap, "account": acct, "reason": exit_reason,
        })
        i = j  # re-enter the same day the previous trade closes
    out = pd.Series(rets, index=close.index, name=p.strategy)
    log = pd.DataFrame(trades)
    if not log.empty:
        log["return_pct"] = 100 * log["pnl"] / log["capital"]
    return out, log


def stats(returns: pd.Series | pd.DataFrame) -> pd.DataFrame:
    """Headline metrics via vectorbt's returns accessor."""
    acc = returns.vbt.returns(freq="1D", year_freq=f"{TRADING_DAYS} days")
    out = pd.DataFrame({
        "total_return_%": acc.total() * 100,
        "annual_return_%": acc.annualized() * 100,
        "annual_vol_%": acc.annualized_volatility() * 100,
        "sharpe": acc.sharpe_ratio(),
        "sortino": acc.sortino_ratio(),
        "max_drawdown_%": acc.max_drawdown() * 100,
        "calmar": acc.calmar_ratio(),
    }, index=[returns.name] if isinstance(returns, pd.Series) else None)
    return out


def sweep(close: pd.Series, base: BTParams, dtes=(14, 21, 30, 45, 60),
          deltas=(0.10, 0.16, 0.20, 0.25, 0.30, 0.40)) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Grid over DTE x delta. Returns (daily returns wide frame, stats frame)."""
    cols = {}
    for d in dtes:
        for de in deltas:
            p = BTParams(**{**base.__dict__, "dte": d, "short_delta": de})
            cols[(d, de)] = simulate(close, p)[0]
    wide = pd.DataFrame(cols)
    wide.columns = pd.MultiIndex.from_tuples(wide.columns, names=["dte", "delta"])
    st = stats(wide)
    st.index = wide.columns
    return wide, st


def buy_and_hold(close: pd.Series) -> pd.Series:
    pf = vbt.Portfolio.from_holding(close, freq="1D")
    return pf.returns().rename("buy_and_hold")
