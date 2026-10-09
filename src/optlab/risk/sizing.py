"""Position sizing, portfolio Greek limits, and skfolio allocation.

Three layers, each answering one question:
1. size_position      how many contracts can this trade be, given max loss?
2. check_limits       does the whole book stay inside delta/vega/concentration limits?
3. allocate           how should capital be split across strategy return streams?
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class RiskLimits:
    equity: float = 100_000
    max_risk_per_trade_pct: float = 2.0
    max_underlying_pct: float = 25.0
    max_portfolio_delta_pct: float = 50.0
    max_portfolio_vega_pct: float = 1.0
    max_drawdown_pct: float = 20.0

    @classmethod
    def from_config(cls, cfg: dict) -> "RiskLimits":
        return cls(**cfg["account"])


def size_position(max_loss_per_unit: float, capital_per_unit: float, limits: RiskLimits,
                  existing_underlying_capital: float = 0.0) -> dict:
    """Contracts allowed by (a) max loss per trade and (b) per-underlying concentration.

    For undefined-risk trades (cash-secured put, covered call) pass a stress loss as
    max_loss_per_unit, e.g. the loss on a 25% gap down, not the theoretical maximum.
    """
    risk_budget = limits.equity * limits.max_risk_per_trade_pct / 100
    by_loss = math.floor(risk_budget / max_loss_per_unit) if max_loss_per_unit > 0 else 10**6
    room = limits.equity * limits.max_underlying_pct / 100 - existing_underlying_capital
    by_conc = math.floor(max(room, 0) / capital_per_unit) if capital_per_unit > 0 else 10**6
    n = max(min(by_loss, by_conc), 0)
    return {
        "contracts": n,
        "limited_by": "max loss per trade" if by_loss <= by_conc else "underlying concentration",
        "risk_budget": risk_budget,
        "total_max_loss": n * max_loss_per_unit,
        "total_capital": n * capital_per_unit,
    }


def stress_loss(legs, spot: float, gap: float = -0.25) -> float:
    """Expiry loss per unit if the underlying gaps by `gap` (default -25%)."""
    from ..pricing.engine import payoff_at_expiry

    return float(max(-payoff_at_expiry(legs, np.array([spot * (1 + gap)]))[0], 0.0))


@dataclass
class BookCheck:
    ok: bool
    breaches: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def check_limits(positions: pd.DataFrame, limits: RiskLimits) -> BookCheck:
    """positions columns: symbol, dollar_delta, vega, capital (dollars, signed for Greeks)."""
    m = {
        "dollar_delta": float(positions["dollar_delta"].sum()),
        "vega": float(positions["vega"].sum()),
        "capital": float(positions["capital"].sum()),
    }
    m["delta_pct"] = 100 * abs(m["dollar_delta"]) / limits.equity
    m["vega_pct"] = 100 * abs(m["vega"]) / limits.equity
    by_sym = positions.groupby("symbol")["capital"].sum() / limits.equity * 100
    m["largest_underlying"] = by_sym.idxmax() if len(by_sym) else None
    m["largest_underlying_pct"] = float(by_sym.max()) if len(by_sym) else 0.0
    br = []
    if m["delta_pct"] > limits.max_portfolio_delta_pct:
        br.append(f"Dollar delta {m['delta_pct']:.0f}% of equity > {limits.max_portfolio_delta_pct:.0f}% limit")
    if m["vega_pct"] > limits.max_portfolio_vega_pct:
        br.append(f"Vega {m['vega_pct']:.2f}% of equity per vol point > {limits.max_portfolio_vega_pct:.2f}% limit")
    for sym, pct in by_sym.items():
        if pct > limits.max_underlying_pct:
            br.append(f"{sym} uses {pct:.0f}% of equity > {limits.max_underlying_pct:.0f}% limit")
    if m["capital"] > limits.equity:
        br.append(f"Capital in use ${m['capital']:,.0f} exceeds equity ${limits.equity:,.0f}")
    return BookCheck(ok=not br, breaches=br, metrics=m)


def allocate(returns: pd.DataFrame, method: str = "min_cvar", max_weight: float = 0.4) -> pd.Series:
    """Split capital across strategy return streams with skfolio.

    methods: min_cvar (minimize 95% CVaR), max_sharpe, risk_parity (equal CDaR
    contribution), hrp (hierarchical risk parity on drawdown).
    """
    from skfolio import RiskMeasure
    from skfolio.optimization import (HierarchicalRiskParity, MeanRisk, ObjectiveFunction,
                                      RiskBudgeting)

    X = returns.dropna(how="all").fillna(0.0)
    X = X.loc[:, X.std() > 0]
    if X.shape[1] == 1:
        return pd.Series([1.0], index=X.columns)
    if method == "min_cvar":
        model = MeanRisk(risk_measure=RiskMeasure.CVAR, max_weights=max_weight)
    elif method == "max_sharpe":
        model = MeanRisk(objective_function=ObjectiveFunction.MAXIMIZE_RATIO,
                         risk_measure=RiskMeasure.VARIANCE, max_weights=max_weight)
    elif method == "risk_parity":
        model = RiskBudgeting(risk_measure=RiskMeasure.CDAR)
    elif method == "hrp":
        model = HierarchicalRiskParity(risk_measure=RiskMeasure.CDAR)
    else:
        raise ValueError(method)
    model.fit(X)
    return pd.Series(model.weights_, index=X.columns, name="weight")


def portfolio_report(returns: pd.DataFrame, weights: pd.Series) -> dict:
    """Annualized return/vol, max drawdown and 95% CVaR (daily) of the weighted mix."""
    r = returns[weights.index].fillna(0.0) @ weights.values
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1
    tail = np.sort(r.values)[: max(int(len(r) * 0.05), 1)]
    return {
        "annual_return_%": float((eq.iloc[-1] ** (252 / len(r)) - 1) * 100),
        "annual_vol_%": float(r.std() * np.sqrt(252) * 100),
        "max_drawdown_%": float(dd.min() * 100),
        "cvar95_daily_%": float(tail.mean() * 100),
        "equity_curve": eq,
    }
