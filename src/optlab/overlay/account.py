"""Small-account calculations. Every number is a fixed formula on natural prices
(buy at the ask, sell at the bid), per one contract unless stated.

    allocatable_capital = min(settled_cash − reserve_cash, total_buying_power − reserve_cash)
    risk_limit          = max_account_risk_pct % × total_buying_power

    long option      capital = ask × multiplier                          max loss = capital
    debit spread     net_debit = long ask − short bid                    capital = max loss = net_debit × multiplier
    credit spread    net_credit = short bid − long ask                   max loss = (width − net_credit) × multiplier
                     capital = max loss
    cash-secured put capital = strike × multiplier − premium received    max loss = capital (stock to zero)
    covered call     capital = 100 shares × price − premium (0 for the shares if owned_shares records them)
    naked short call undefined (unlimited) risk

Classification, first failing check wins (every failing check is listed in `reasons`):
    INSUFFICIENT DATA, UNDEFINED RISK, EXCEEDS CAPITAL LIMIT (capital > allocatable),
    ASSIGNMENT OBLIGATION EXCEEDS CAPACITY (short-leg obligation > settled cash, or buying power
    in a margin account, when require_assignment_capacity), EXCEEDS ACCOUNT-RISK LIMIT
    (max loss > risk_limit), EXCEEDS MAXIMUM LOSS (max loss > max_loss_per_idea),
    FITS BUYING POWER BUT EXCEEDS PREFERRED RISK (capital > max_capital_per_idea, or more
    contracts than max_contracts), else FITS CONFIGURED LIMITS.
Limits are inclusive: exactly at a limit fits.

Affordability says nothing about setup quality; it is reported separately, never as green.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

CLASSES = ("FITS CONFIGURED LIMITS", "FITS BUYING POWER BUT EXCEEDS PREFERRED RISK", "EXCEEDS CAPITAL LIMIT",
           "EXCEEDS MAXIMUM LOSS", "EXCEEDS ACCOUNT-RISK LIMIT", "ASSIGNMENT OBLIGATION EXCEEDS CAPACITY",
           "UNDEFINED RISK", "INSUFFICIENT DATA")
STATUS_OF = {"FITS CONFIGURED LIMITS": "FITS_ACCOUNT", "FITS BUYING POWER BUT EXCEEDS PREFERRED RISK": "FITS_BP_EXCEEDS_RISK",
             "INSUFFICIENT DATA": "INSUFFICIENT_DATA"}


@dataclass
class Profile:
    total_buying_power: float = 1000
    settled_cash: float = 1000
    reserve_cash: float = 300
    max_capital_per_idea: float = 300
    max_loss_per_idea: float = 100
    max_account_risk_pct: float = 10
    max_contracts: int = 1
    contract_multiplier: int = 100
    account_type: str = "cash"
    defined_risk_only: bool = True
    naked_options_allowed: bool = False
    unlimited_risk_allowed: bool = False
    require_assignment_capacity: bool = True
    owned_shares: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, d: dict) -> "Profile":
        keys = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in keys})

    @property
    def allocatable(self) -> float:
        return max(0.0, min(self.settled_cash - self.reserve_cash, self.total_buying_power - self.reserve_cash))

    @property
    def risk_limit(self) -> float:
        return self.max_account_risk_pct / 100 * self.total_buying_power

    @property
    def assignment_capacity(self) -> float:
        return self.settled_cash if self.account_type == "cash" else self.total_buying_power


@dataclass
class Fit:
    strategy: str
    classification: str
    capital_required: float | None
    max_loss: float | None
    max_profit: float | None          # None = unlimited
    assignment_obligation: float
    defined_risk: bool
    account_risk_pct: float | None
    allocatable_used_pct: float | None
    buying_power_remaining: float | None
    contracts: int
    reasons: list[str]
    formula: str
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return STATUS_OF.get(self.classification, "DOES_NOT_FIT_ACCOUNT")

    def as_dict(self) -> dict:
        return asdict(self) | {"status": self.status}


def _bad(*xs) -> bool:
    return any(x is None or (isinstance(x, float) and not math.isfinite(x)) for x in xs)


def classify(p: Profile, strategy: str, capital, max_loss, assignment: float = 0.0, defined: bool = True,
             max_profit=None, contracts: int = 1, formula: str = "", notes: list | None = None) -> Fit:
    reasons: list[str] = []
    notes = list(notes or [])
    if not defined:
        reasons.append("maximum loss is unlimited (a short call not covered by shares or a long call)")
        return Fit(strategy, "UNDEFINED RISK", capital, None, max_profit, assignment, False, None, None, None, contracts,
                   reasons, formula, notes)
    if _bad(capital, max_loss):
        reasons.append("a price needed for the calculation is missing (no bid or ask)")
        return Fit(strategy, "INSUFFICIENT DATA", None, None, None, assignment, True, None, None, None, contracts,
                   reasons, formula, notes)
    capital, max_loss = float(capital) * contracts, float(max_loss) * contracts
    assignment = float(assignment) * contracts
    eps = 1e-9
    order = []
    if capital > p.allocatable + eps:
        order.append("EXCEEDS CAPITAL LIMIT")
        reasons.append(f"capital ${capital:,.2f} exceeds allocatable ${p.allocatable:,.2f} "
                       f"(buying power ${p.total_buying_power:,.0f} − reserve ${p.reserve_cash:,.0f})")
    if p.require_assignment_capacity and assignment > p.assignment_capacity + eps:
        order.append("ASSIGNMENT OBLIGATION EXCEEDS CAPACITY")
        reasons.append(f"assignment could require ${assignment:,.0f}, more than the "
                       f"{'settled cash' if p.account_type == 'cash' else 'buying power'} of ${p.assignment_capacity:,.0f}")
    if max_loss > p.risk_limit + eps:
        order.append("EXCEEDS ACCOUNT-RISK LIMIT")
        reasons.append(f"maximum loss ${max_loss:,.2f} is {100 * max_loss / p.total_buying_power:.1f}% of the account; "
                       f"the limit is {p.max_account_risk_pct:g}% (${p.risk_limit:,.2f})")
    if max_loss > p.max_loss_per_idea + eps:
        order.append("EXCEEDS MAXIMUM LOSS")
        reasons.append(f"maximum loss ${max_loss:,.2f} exceeds the ${p.max_loss_per_idea:,.2f} per-idea limit")
    if capital > p.max_capital_per_idea + eps:
        order.append("FITS BUYING POWER BUT EXCEEDS PREFERRED RISK")
        reasons.append(f"capital ${capital:,.2f} exceeds the preferred ${p.max_capital_per_idea:,.2f} per idea")
    if contracts > p.max_contracts:
        order.append("FITS BUYING POWER BUT EXCEEDS PREFERRED RISK")
        reasons.append(f"{contracts} contracts exceeds max_contracts {p.max_contracts}")
    rank = {c: i for i, c in enumerate(("EXCEEDS CAPITAL LIMIT", "ASSIGNMENT OBLIGATION EXCEEDS CAPACITY",
                                        "EXCEEDS ACCOUNT-RISK LIMIT", "EXCEEDS MAXIMUM LOSS",
                                        "FITS BUYING POWER BUT EXCEEDS PREFERRED RISK"))}
    cls = min(order, key=rank.get) if order else "FITS CONFIGURED LIMITS"
    tbp = p.total_buying_power or float("nan")
    max_profit = None if max_profit is None else round(float(max_profit) * contracts, 2)
    return Fit(strategy, cls, round(capital, 2), round(max_loss, 2), max_profit, round(assignment, 2), True,
               round(100 * max_loss / tbp, 2), round(100 * capital / p.allocatable, 2) if p.allocatable else None,
               round(p.allocatable - capital, 2), contracts, reasons, formula, notes)


# ----------------------------------------------------------------- structures (one contract each)
def long_option(p: Profile, ask, strike=None, option_type="call") -> Fit:
    m = p.contract_multiplier
    cap = None if _bad(ask) or ask <= 0 else ask * m
    mp = None if option_type == "call" else (None if cap is None or strike is None else max(strike * m - cap, 0.0))
    notes = []
    if strike is not None and cap is not None:
        notes.append(f"Exercising would need ${strike * m:,.0f} for calls (buy 100 shares) or 100 shares for puts; "
                     "most long options are sold before expiry, but exercise needs that capacity.")
    return classify(p, f"Long {option_type}", cap, cap, 0.0, True, mp,
                    formula="capital = ask × multiplier; maximum loss = capital", notes=notes)


def debit_spread(p: Profile, long_ask, short_bid, width, short_strike=None, option_type="call") -> Fit:
    m = p.contract_multiplier
    if _bad(long_ask, short_bid) or long_ask <= 0:
        return classify(p, f"{option_type.capitalize()} debit spread", None, None, formula="needs both quotes")
    net = long_ask - short_bid
    obligation = (short_strike or 0) * m
    return classify(p, f"{option_type.capitalize()} debit spread", net * m, net * m, obligation, True,
                    max((width - net) * m, 0.0),
                    formula="net debit = long ask − short bid; capital = maximum loss = net debit × multiplier",
                    notes=_spread_notes(short_strike, m, option_type))


def credit_spread(p: Profile, short_bid, long_ask, width, short_strike=None, option_type="put") -> Fit:
    m = p.contract_multiplier
    if _bad(long_ask, short_bid) or short_bid <= 0:
        return classify(p, f"{option_type.capitalize()} credit spread", None, None, formula="needs both quotes")
    credit = short_bid - long_ask
    loss = (width - credit) * m
    notes = _spread_notes(short_strike, m, option_type)
    if credit <= 0:
        notes.append("No credit at natural prices: the spread costs money to open at bid/ask.")
    return classify(p, f"{option_type.capitalize()} credit spread", loss, loss, (short_strike or 0) * m, True,
                    max(credit * m, 0.0),
                    formula="net credit = short bid − long ask; maximum loss = (width − net credit) × multiplier; "
                            "capital = maximum loss", notes=notes)


def cash_secured_put(p: Profile, strike, bid) -> Fit:
    m = p.contract_multiplier
    if _bad(strike, bid):
        return classify(p, "Cash-secured put", None, None)
    cap = strike * m - bid * m
    return classify(p, "Cash-secured put", cap, cap, strike * m, True, bid * m,
                    formula="capital = strike × multiplier − premium received; maximum loss = capital (stock to zero)",
                    notes=[f"Assignment buys 100 shares for ${strike * m:,.0f}."])


def covered_call(p: Profile, symbol: str, spot, call_bid, strike) -> Fit:
    m = p.contract_multiplier
    owned = int((p.owned_shares or {}).get(symbol, 0)) >= m
    if _bad(spot, call_bid, strike):
        return classify(p, "Covered call", None, None)
    shares = 0.0 if owned else spot * m
    loss = spot * m - call_bid * m
    notes = ["Uses the 100 shares already recorded in owned_shares." if owned else
             f"Includes buying 100 shares for ${spot * m:,.0f} (record existing shares in owned_shares)."]
    return classify(p, "Covered call", shares - call_bid * m if not owned else 0.0, loss, 0.0, True,
                    max((strike - spot + call_bid) * m, 0.0),
                    formula="capital = 100 shares × price − premium (shares excluded if owned); maximum loss = "
                            "share cost − premium", notes=notes)


def naked_call(p: Profile, bid=None) -> Fit:
    return classify(p, "Naked short call", None, None, defined=False, formula="unlimited upside loss")


def _spread_notes(short_strike, m, option_type) -> list[str]:
    if not short_strike:
        return []
    side = "buy" if option_type == "put" else "deliver (sell short)"
    return [f"If the short {option_type} is assigned you must {side} 100 shares at ${short_strike:,.2f} "
            f"(${short_strike * m:,.0f}) until the long leg is exercised or sold.",
            "The long leg's protection holds only while the spread stays intact; closing or losing it leaves the "
            "short leg's risk undefined (calls) or strike-sized (puts)."]


def legs_fit(p: Profile, legs, spot: float, symbol: str = "", name: str = "Strategy") -> Fit:
    """Fit for any leg list (pricing.engine.Leg). Capital = maximum loss at expiry (worst case on a wide grid);
    shares recorded in owned_shares are not charged."""
    from ..strategies import max_loss
    from ..pricing.engine import payoff_at_expiry

    m = p.contract_multiplier
    calls = sum(lg.quantity for lg in legs if lg.option_type == "call")
    stock = sum(lg.quantity for lg in legs if lg.option_type == "stock")
    if calls + stock < 0:
        return classify(p, name, None, None, defined=False, formula="net short calls: unlimited upside loss")
    worst = max_loss(legs)
    owned = int((p.owned_shares or {}).get(symbol, 0)) >= m * max(stock, 0) and stock > 0
    capital = worst - (spot * m * stock if owned else 0.0)
    strikes = [lg.strike for lg in legs if lg.option_type != "stock"] or [spot]
    grid = np.linspace(0, max(strikes) * 3, 3001)
    best = float(payoff_at_expiry(legs, grid).max())
    unlimited_up = calls + stock > 0
    obligation = sum(abs(lg.quantity) * lg.strike * m for lg in legs if lg.option_type == "put" and lg.quantity < 0)
    obligation += sum(abs(lg.quantity) * lg.strike * m for lg in legs if lg.option_type == "call" and lg.quantity < 0) \
        if stock <= 0 else 0.0
    notes = [f"Short {lg.option_type} {lg.strike:g}: assignment obligation ${abs(lg.quantity) * lg.strike * m:,.0f}"
             for lg in legs if lg.option_type != "stock" and lg.quantity < 0]
    return classify(p, name, max(capital, 0.0), worst, obligation, True, None if unlimited_up else best,
                    formula="capital = maximum loss at expiry (natural entry prices); assignment = Σ short strike × "
                            "multiplier", notes=notes)
