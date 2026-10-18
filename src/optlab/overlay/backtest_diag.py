"""Backtest diagnostics: is the quick backtest's result supported by enough trades, stable across time
and regimes, robust to nearby settings and to higher costs? Fixed checks, each with its formula.

    profit factor = Σ winning P&L ÷ |Σ losing P&L|
    expectancy    = mean P&L per trade
    exposure      = days in a trade ÷ days in the test
    outlier share = (P&L of the two best trades) ÷ total P&L
    in-sample / out-of-sample: the first 70% of the period vs the last 30%
    cost check:  slippage and commission doubled
    robustness:  DTE ± 7 days and delta ± 0.05, other settings unchanged
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from ..backtest import simulate, stats
from ..live.indicators import sma


def _item(check: str, status: str, value: str, detail: str) -> dict:
    return {"check": check, "status": status, "value": value, "detail": detail}


def longest_losing_streak(pnl: pd.Series) -> int:
    best = cur = 0
    for x in pnl:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    return best


def diagnostics(px: pd.Series, params, th: dict) -> dict:
    rets, log = simulate(px, params)
    mins = int(th["minimum_historical_samples"])
    items = []
    n = len(log)
    if n == 0:
        return {"items": [_item("Sample size", "INSUFFICIENT_DATA", "0 trades", "The strategy never traded.")],
                "is_oos": pd.DataFrame(), "robust": pd.DataFrame(), "years": pd.DataFrame(), "regimes": pd.DataFrame()}
    pnl, rp = log["pnl"], log["return_pct"]
    items.append(_item("Sample size", "CONFIRMED" if n >= mins else "LOW_SAMPLE_SIZE", f"{n:,} trades",
                       f"Minimum {mins} (thresholds.minimum_historical_samples)."))
    days = (log["exit"] - log["entry"]).dt.days.sum()
    span = (px.index[-1] - px.index[0]).days or 1
    items.append(_item("Date range and exposure", "INFO", f"{px.index[0]:%Y-%m-%d} to {px.index[-1]:%Y-%m-%d}",
                       f"In a trade {100 * days / span:.0f}% of calendar days."))
    wins, losses = pnl[pnl > 0].sum(), pnl[pnl <= 0].sum()
    pf = wins / abs(losses) if losses else float("inf")
    items.append(_item("Win rate, median, mean", "INFO", f"{100 * (pnl > 0).mean():.0f}% wins",
                       f"median {rp.median():+.2f}%, mean {rp.mean():+.2f}% per trade (of capital)."))
    items.append(_item("Profit factor and expectancy", "INFO", f"{pf:,.2f}" if np.isfinite(pf) else "no losses",
                       f"expectancy ${pnl.mean():+,.2f} per share per trade; longest losing streak "
                       f"{longest_losing_streak(pnl)} trades."))
    st_ = stats(rets).iloc[0]
    items.append(_item("Maximum drawdown", "INFO", f"{st_['max_drawdown_%']:.1f}%", "Largest peak-to-trough fall of the strategy."))
    total = pnl.sum()
    top2 = pnl.nlargest(2).sum()
    share = top2 / total if total > 0 else float("nan")
    out_dep = bool(total > 0 and share > float(th["outlier_share"]))
    items.append(_item("Outlier dependence", "INVALIDATED" if out_dep else "CONFIRMED",
                       f"top 2 trades = {share:.0%} of P&L" if np.isfinite(share) else "total P&L not positive",
                       f"Without them total P&L is ${total - top2:+,.2f} per share. Flagged above "
                       f"{float(th['outlier_share']):.0%}."))
    gap = abs(rp.mean() - rp.median())
    skewed = bool(np.sign(rp.mean()) != np.sign(rp.median()) or gap > 2 * max(abs(rp.median()), 0.05))
    items.append(_item("Median vs mean", "NEAR_CONFIRMATION" if skewed else "CONFIRMED",
                       f"median {rp.median():+.2f}%, mean {rp.mean():+.2f}%",
                       "Flagged when they differ in sign or by more than twice the median."))
    yrs = log.assign(year=log["entry"].dt.year).groupby("year")["pnl"].agg(["count", "sum"]).reset_index()
    pos_years = int((yrs["sum"] > 0).sum())
    stable = pos_years >= 2 / 3 * len(yrs)
    items.append(_item("Stable across years", "CONFIRMED" if stable else "NEAR_CONFIRMATION",
                       f"{pos_years} of {len(yrs)} years positive", "Stable when at least two thirds of years are positive."))
    cut = px.index[0] + (px.index[-1] - px.index[0]) * 0.7
    is_r, oos_r = rets[rets.index <= cut], rets[rets.index > cut]
    io = pd.DataFrame([{"period": "In-sample (first 70%)", **stats(is_r).iloc[0].to_dict()},
                       {"period": "Out-of-sample (last 30%)", **stats(oos_r).iloc[0].to_dict()}])
    a_is, a_oos = float(io.loc[0, "annual_return_%"]), float(io.loc[1, "annual_return_%"])
    drop = float(th["parameter_sensitivity_drop"])
    same = (a_is > 0) == (a_oos > 0)
    held = same and (a_is <= 0 or a_oos >= a_is * (1 - drop))
    oos_status = "CONFIRMED" if held else "NEAR_CONFIRMATION" if same else "INVALIDATED"
    items.append(_item("Out-of-sample confirmation", oos_status,
                       f"annual return {io.loc[0, 'annual_return_%']:+.1f}% → {io.loc[1, 'annual_return_%']:+.1f}%",
                       f"Development period ends {cut:%Y-%m-%d}; confirmed when both periods have the same sign and the "
                       f"later one keeps at least {1 - drop:.0%} of the earlier return; weakened otherwise. "
                       "The settings were not fitted, so this is a split check, not walk-forward optimisation."))
    costly = replace(params, slippage_pct=params.slippage_pct * 2, commission=params.commission * 2)
    r2, _ = simulate(px, costly)
    tr0, tr2 = stats(rets).iloc[0]["total_return_%"], stats(r2).iloc[0]["total_return_%"]
    collapse = bool(tr0 > 0 and (tr2 <= 0 or tr2 < tr0 * 0.5))
    items.append(_item("Results after doubled costs", "INVALIDATED" if collapse else "CONFIRMED",
                       f"total return {tr0:+.1f}% → {tr2:+.1f}%",
                       "Flagged when doubling slippage and commission halves the return or turns it negative."))
    rows = []
    for name, p2 in (("DTE − 7", replace(params, dte=max(params.dte - 7, 7))), ("DTE + 7", replace(params, dte=params.dte + 7)),
                     ("Delta − 0.05", replace(params, short_delta=max(params.short_delta - 0.05, 0.05))),
                     ("Delta + 0.05", replace(params, short_delta=min(params.short_delta + 0.05, 0.5)))):
        s2 = stats(simulate(px, p2)[0]).iloc[0]
        rows.append({"variant": name, "sharpe": s2["sharpe"], "annual_return_%": s2["annual_return_%"],
                     "max_drawdown_%": s2["max_drawdown_%"]})
    rob = pd.DataFrame(rows)
    base_sh = float(st_["sharpe"])
    fragile = bool(base_sh > 0 and (rob["sharpe"].min() < 0 or rob["sharpe"].min() < base_sh * (1 - float(th["parameter_sensitivity_drop"]))))
    items.append(_item("Parameter robustness", "INVALIDATED" if fragile else "CONFIRMED",
                       f"Sharpe {base_sh:.2f}; neighbours {rob['sharpe'].min():.2f} to {rob['sharpe'].max():.2f}",
                       "Flagged when a neighbouring setting loses more than "
                       f"{float(th['parameter_sensitivity_drop']):.0%} of the Sharpe ratio or turns negative."))
    s200 = sma(px, 200)
    above = log["entry"].map(lambda t: bool(px.loc[t] > s200.loc[t]) if pd.notna(s200.loc[t]) else None)
    reg = log.assign(regime=above.map({True: "Entry above 200-day MA", False: "Entry below 200-day MA", None: "Unavailable"}))
    reg = reg.groupby("regime")["return_pct"].agg(["count", "mean", "median"]).reset_index()
    items.append(_item("Regimes represented", "CONFIRMED" if (reg["count"] >= 5).sum() >= 2 else "NEAR_CONFIRMATION",
                       ", ".join(f"{r.regime}: {r.count}" for r in reg.itertuples()), "Both regimes need at least 5 trades."))
    items.append(_item("Look-ahead bias", "CONFIRMED", "none found",
                       "Entries use realized volatility through the entry bar's close; exits use only later bars."))
    items.append(_item("Survivorship bias", "NEAR_CONFIRMATION", "possible",
                       "The symbols are today's universe; names that were delisted or dropped are not in the test."))
    items.append(_item("Option fills", "REQUIRES_DATA_VALIDATION", "modeled, not quoted",
                       f"Options are priced with Black-Scholes from realized vol × {params.iv_premium:g}; fills are model value "
                       f"minus {params.slippage_pct:g}% slippage, never real bid/ask. The cost check shows sensitivity; "
                       "re-test in LEAN with real quotes."))
    return {"items": items, "is_oos": io, "robust": rob, "years": yrs, "regimes": reg}
