"""Deterministic strategy recognition and entry analytics for a set of legs.

Recognition never guesses: a leg set gets a named strategy only when it matches that strategy's exact
shape (types, sides, ratios, strike order, expirations). Anything else is CUSTOM, still with its full
payoff analysis. Leg order never matters: legs are normalised first.

Analytics are exact (Decimal) and per position, in dollars:
    net premium  Σ signed quantity × multiplier × fill price;  + = debit paid, − = credit received
    P&L(S)       payoff at expiration(S) − net premium − opening fees
For a single expiration (or stock only) the payoff is piecewise linear in the underlying price S with
kinks at the strikes, so the maximum gain and loss are found exactly at S = 0, at each strike, or
are unbounded when the slope above the highest strike is non-zero. Breakevens are the exact zeros.
Closing fees are unknown at entry and are not included.

Calendars and diagonals (two expirations) can't be valued at the front expiration without a model,
so their maximum gain and breakevens are `unknown`; the maximum loss of a long calendar/diagonal is
bounded under the stated assumption.

Every bound is reported with a state: bounded (with a value), unbounded, or unknown. Unlimited or
unavailable values are never reported as zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from math import gcd

ZERO = Decimal(0)


class Strategy(str, Enum):
    LONG_STOCK = "LONG_STOCK"
    SHORT_STOCK = "SHORT_STOCK"
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    SHORT_CALL = "SHORT_CALL"                  # uncovered (naked) short call
    SHORT_PUT = "SHORT_PUT"                    # uncovered short put (no attested cash collateral)
    CASH_SECURED_PUT = "CASH_SECURED_PUT"
    COVERED_CALL = "COVERED_CALL"
    PROTECTIVE_PUT = "PROTECTIVE_PUT"
    COLLAR = "COLLAR"
    CALL_DEBIT_SPREAD = "CALL_DEBIT_SPREAD"    # bull call spread
    CALL_CREDIT_SPREAD = "CALL_CREDIT_SPREAD"  # bear call spread
    PUT_DEBIT_SPREAD = "PUT_DEBIT_SPREAD"      # bear put spread
    PUT_CREDIT_SPREAD = "PUT_CREDIT_SPREAD"    # bull put spread
    CALENDAR_SPREAD = "CALENDAR_SPREAD"        # long calendar: short front, long back, same strike
    DIAGONAL_SPREAD = "DIAGONAL_SPREAD"        # long diagonal: short front, long back, different strikes
    IRON_CONDOR = "IRON_CONDOR"                # short: put credit spread + call credit spread
    IRON_BUTTERFLY = "IRON_BUTTERFLY"          # short: the two short strikes are equal
    LONG_STRADDLE = "LONG_STRADDLE"
    SHORT_STRADDLE = "SHORT_STRADDLE"
    LONG_STRANGLE = "LONG_STRANGLE"
    SHORT_STRANGLE = "SHORT_STRANGLE"
    CUSTOM = "CUSTOM"


NAMES = {
    "LONG_STOCK": "Long stock", "SHORT_STOCK": "Short stock", "LONG_CALL": "Long call", "LONG_PUT": "Long put",
    "SHORT_CALL": "Short call (uncovered)", "SHORT_PUT": "Short put (uncovered)",
    "CASH_SECURED_PUT": "Cash-secured put", "COVERED_CALL": "Covered call", "PROTECTIVE_PUT": "Protective put",
    "COLLAR": "Collar", "CALL_DEBIT_SPREAD": "Call debit spread", "CALL_CREDIT_SPREAD": "Call credit spread",
    "PUT_DEBIT_SPREAD": "Put debit spread", "PUT_CREDIT_SPREAD": "Put credit spread",
    "CALENDAR_SPREAD": "Calendar spread", "DIAGONAL_SPREAD": "Diagonal spread", "IRON_CONDOR": "Iron condor",
    "IRON_BUTTERFLY": "Iron butterfly", "LONG_STRADDLE": "Long straddle", "SHORT_STRADDLE": "Short straddle",
    "LONG_STRANGLE": "Long strangle", "SHORT_STRANGLE": "Short strangle", "CUSTOM": "Custom",
}
DIRECTION = {
    "LONG_STOCK": "bullish", "SHORT_STOCK": "bearish", "LONG_CALL": "bullish", "LONG_PUT": "bearish",
    "SHORT_CALL": "bearish", "SHORT_PUT": "bullish", "CASH_SECURED_PUT": "bullish", "COVERED_CALL": "bullish",
    "PROTECTIVE_PUT": "bullish", "COLLAR": "bullish", "CALL_DEBIT_SPREAD": "bullish", "CALL_CREDIT_SPREAD": "bearish",
    "PUT_DEBIT_SPREAD": "bearish", "PUT_CREDIT_SPREAD": "bullish", "CALENDAR_SPREAD": "neutral",
    "DIAGONAL_SPREAD": "neutral", "IRON_CONDOR": "neutral", "IRON_BUTTERFLY": "neutral", "LONG_STRADDLE": "neutral",
    "SHORT_STRADDLE": "neutral", "LONG_STRANGLE": "neutral", "SHORT_STRANGLE": "neutral", "CUSTOM": "unknown",
}
EXPECT_DEBIT = {"LONG_CALL", "LONG_PUT", "CALL_DEBIT_SPREAD", "PUT_DEBIT_SPREAD", "CALENDAR_SPREAD", "DIAGONAL_SPREAD",
                "LONG_STRADDLE", "LONG_STRANGLE"}
EXPECT_CREDIT = {"SHORT_CALL", "SHORT_PUT", "CASH_SECURED_PUT", "CALL_CREDIT_SPREAD", "PUT_CREDIT_SPREAD",
                 "IRON_CONDOR", "IRON_BUTTERFLY", "SHORT_STRADDLE", "SHORT_STRANGLE"}
MARGIN_STRATEGIES = {"SHORT_CALL", "SHORT_PUT", "SHORT_STOCK", "SHORT_STRADDLE", "SHORT_STRANGLE"}


@dataclass(frozen=True)
class Leg:
    asset_type: str                 # STOCK | OPTION
    side: str                       # BUY | SELL
    quantity: int                   # shares or contracts, > 0
    right: str | None = None        # CALL | PUT for options
    strike: Decimal | None = None
    expiration: date | None = None
    price: Decimal | None = None    # fill price per share (options: per-share premium)
    fees: Decimal = ZERO            # total fees for the leg
    multiplier: int = 1             # 100 for standard options, 1 for stock

    # Checked accessors for code that runs after validate(): option legs always have these.
    @property
    def k(self) -> Decimal:
        assert self.strike is not None, "validated option legs have a strike"
        return self.strike

    @property
    def exp(self) -> date:
        assert self.expiration is not None, "validated option legs have an expiration"
        return self.expiration

    @property
    def px(self) -> Decimal:
        assert self.price is not None, "used only when every leg has a fill price"
        return self.price

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1

    @property
    def signed_qty(self) -> int:
        return self.sign * self.quantity

    @property
    def is_option(self) -> bool:
        return self.asset_type == "OPTION"

    def intrinsic(self, s: Decimal) -> Decimal:
        if not self.is_option:
            return s
        return max(ZERO, s - self.k) if self.right == "CALL" else max(ZERO, self.k - s)


@dataclass
class Bounded:
    state: str                      # bounded | unbounded | unknown
    value: Decimal | None = None    # dollars, only when bounded

    @classmethod
    def of(cls, v: Decimal) -> Bounded:
        return cls("bounded", v)


@dataclass
class Issue:
    code: str
    message: str
    leg: int | None = None


@dataclass
class Analysis:
    strategy: str
    name: str
    exact: bool
    variant: str | None
    direction: str
    structure: list[dict]
    units: int                                  # how many "units" of the normalised ratio (e.g. 3 condors)
    net_premium: Decimal | None                 # + debit, − credit, before fees
    premium_type: str                           # DEBIT | CREDIT | EVEN | UNKNOWN
    fees: Decimal
    cost_basis: Decimal | None                  # net premium + opening fees
    max_gain: Bounded
    max_loss: Bounded
    breakevens: list[Decimal] | None            # None = unknown
    breakeven_state: str                        # known | unknown | none
    defined_risk: bool
    buying_power: Bounded
    buying_power_basis: str
    expirations: list[date]
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    payoff_points: list[tuple[Decimal, Decimal]] = field(default_factory=list)


@dataclass
class Context:
    """Facts that legs alone can't tell: whether a short put's cash is set aside, the account's buying
    power, the next earnings date, and today's date."""
    cash_secured: bool = False
    buying_power: Decimal | None = None
    earnings_date: date | None = None
    today: date | None = None


# ------------------------------------------------------------------ validation
def validate(legs: list[Leg]) -> list[Issue]:
    errs = []
    if not legs:
        errs.append(Issue("NO_LEGS", "A position needs at least one leg."))
    if len(legs) > 12:
        errs.append(Issue("TOO_MANY_LEGS", "At most 12 legs per position."))
    for i, lg in enumerate(legs):
        if lg.asset_type not in ("STOCK", "OPTION"):
            errs.append(Issue("BAD_ASSET_TYPE", "Asset type must be STOCK or OPTION.", i))
            continue
        if lg.side not in ("BUY", "SELL"):
            errs.append(Issue("BAD_SIDE", "Side must be BUY or SELL.", i))
        if not isinstance(lg.quantity, int) or isinstance(lg.quantity, bool) or lg.quantity <= 0:
            errs.append(Issue("BAD_QUANTITY", "Quantity must be a whole number above zero.", i))
        if lg.price is not None and lg.price < 0:
            errs.append(Issue("NEGATIVE_PRICE", "Fill price can't be negative; use BUY/SELL for direction.", i))
        if lg.fees < 0:
            errs.append(Issue("NEGATIVE_FEES", "Fees can't be negative.", i))
        if lg.is_option:
            if lg.right not in ("CALL", "PUT"):
                errs.append(Issue("MISSING_RIGHT", "Option legs need CALL or PUT.", i))
            if lg.strike is None or lg.strike <= 0:
                errs.append(Issue("MISSING_STRIKE", "Option legs need a strike above zero.", i))
            if lg.expiration is None:
                errs.append(Issue("MISSING_EXPIRATION", "Option legs need an expiration date.", i))
            if lg.multiplier <= 0:
                errs.append(Issue("BAD_MULTIPLIER", "The contract multiplier must be above zero.", i))
        else:
            if lg.right is not None or lg.strike is not None or lg.expiration is not None:
                errs.append(Issue("STOCK_WITH_OPTION_FIELDS", "Stock legs have no right, strike or expiration.", i))
            if lg.multiplier != 1:
                errs.append(Issue("BAD_MULTIPLIER", "Stock legs have a multiplier of 1.", i))
    return errs


# ------------------------------------------------------------------ normalisation and matching
def _key(lg: Leg):
    return (lg.expiration or date.min, {"STOCK": 0, "PUT": 1, "CALL": 2}[lg.right or "STOCK"],
            lg.strike or ZERO, lg.side)


def normalise(legs: list[Leg]) -> tuple[list[Leg], int, list[dict]]:
    """Sort legs canonically (expiration, type, strike, side) and express quantities as a ratio of the
    greatest common divisor of the option quantities, so 3 iron condors read as 3 × (1, 1, 1, 1)."""
    ordered = sorted(legs, key=_key)
    units = 0
    for lg in ordered:
        if lg.is_option:
            units = gcd(units, lg.quantity)
    units = units or 1
    structure = [{"type": lg.right or "STOCK", "side": lg.side, "quantity": lg.quantity,
                  "per_unit": str((Decimal(lg.quantity) / units).normalize()),
                  "strike": None if lg.strike is None else str(lg.strike.normalize()),
                  "expiration": None if lg.expiration is None else lg.expiration.isoformat()} for lg in ordered]
    return ordered, units, structure


def _match(legs: list[Leg], ctx: Context) -> tuple[str, str | None, list[Issue]]:
    """Exact shape -> strategy. Returns (strategy, variant, notes)."""
    notes: list[Issue] = []
    stock = [lg for lg in legs if not lg.is_option]
    opts = [lg for lg in legs if lg.is_option]
    calls = [lg for lg in opts if lg.right == "CALL"]
    puts = [lg for lg in opts if lg.right == "PUT"]
    exps = {lg.exp for lg in opts}
    same_qty = len({lg.quantity for lg in opts}) <= 1
    n = len(opts)

    if stock and len(stock) > 1:
        return "CUSTOM", None, notes
    if stock and not opts:
        return ("LONG_STOCK" if stock[0].side == "BUY" else "SHORT_STOCK"), None, notes
    if stock:
        s = stock[0]
        q = opts[0].quantity if opts else 0
        exact_cover = s.side == "BUY" and same_qty and len(exps) == 1 and s.quantity == q * opts[0].multiplier
        if n == 1 and calls and calls[0].side == "SELL" and s.side == "BUY":
            if exact_cover:
                return "COVERED_CALL", None, notes
            if s.quantity > q * calls[0].multiplier:
                notes.append(Issue("EXTRA_SHARES", "More shares than the calls cover: recorded as custom."))
            else:
                notes.append(Issue("PARTIALLY_UNCOVERED", "Fewer shares than the short calls need: part of the short "
                                                          "call is uncovered."))
            return "CUSTOM", None, notes
        if n == 1 and puts and puts[0].side == "BUY" and exact_cover:
            return "PROTECTIVE_PUT", None, notes
        if (n == 2 and len(calls) == 1 and len(puts) == 1 and exact_cover and calls[0].side == "SELL"
                and puts[0].side == "BUY" and puts[0].k < calls[0].k):
            return "COLLAR", None, notes
        return "CUSTOM", None, notes

    if n == 1:
        lg = opts[0]
        if lg.side == "BUY":
            return ("LONG_CALL" if lg.right == "CALL" else "LONG_PUT"), None, notes
        if lg.right == "CALL":
            return "SHORT_CALL", None, notes
        if ctx.cash_secured:
            return "CASH_SECURED_PUT", None, notes
        notes.append(Issue("UNCOVERED_SHORT_PUT", "Short put without attested cash collateral: recorded as an uncovered "
                                                  "short put. Tick 'cash secured' if the cash is set aside."))
        return "SHORT_PUT", None, notes

    if not same_qty:
        notes.append(Issue("UNEQUAL_QUANTITIES", "Legs have different quantities (a ratio structure): recorded as custom."))
        return "CUSTOM", None, notes

    if n == 2:
        a, b = opts
        if a.right == b.right and a.side != b.side:
            if len(exps) == 1 and a.k != b.k:          # vertical
                lo, hi = (a, b) if a.k < b.k else (b, a)
                if a.right == "CALL":
                    return ("CALL_DEBIT_SPREAD" if lo.side == "BUY" else "CALL_CREDIT_SPREAD"), None, notes
                return ("PUT_DEBIT_SPREAD" if hi.side == "BUY" else "PUT_CREDIT_SPREAD"), None, notes
            if len(exps) == 2:                                    # calendar / diagonal
                front, back = (a, b) if a.exp < b.exp else (b, a)
                if back.side == "BUY" and front.side == "SELL":
                    return ("CALENDAR_SPREAD" if a.k == b.k else "DIAGONAL_SPREAD"), None, notes
                notes.append(Issue("REVERSE_CALENDAR", "Long the front month and short the back month (a reverse "
                                                       "calendar/diagonal): recorded as custom."))
                return "CUSTOM", None, notes
        if a.right != b.right and a.side == b.side and len(exps) == 1:
            c, p = (a, b) if a.right == "CALL" else (b, a)
            prefix = "LONG" if a.side == "BUY" else "SHORT"
            if c.k == p.k:
                return f"{prefix}_STRADDLE", None, notes
            if p.k < c.k:
                return f"{prefix}_STRANGLE", None, notes
        return "CUSTOM", None, notes

    if n == 4 and len(calls) == 2 and len(puts) == 2 and len(exps) == 1:
        sps = [lg for lg in puts if lg.side == "SELL"]
        lps = [lg for lg in puts if lg.side == "BUY"]
        scs = [lg for lg in calls if lg.side == "SELL"]
        lcs = [lg for lg in calls if lg.side == "BUY"]
        if len(sps) == len(lps) == len(scs) == len(lcs) == 1:
            sp, lp, sc, lc = sps[0], lps[0], scs[0], lcs[0]
            if lp.k < sp.k <= sc.k < lc.k:
                variant = "EQUAL_WIDTH" if (sp.k - lp.k) == (lc.k - sc.k) else "BROKEN_WING"
                return ("IRON_BUTTERFLY" if sp.k == sc.k else "IRON_CONDOR"), variant, notes
            if sp.k < lp.k and lc.k < sc.k:
                notes.append(Issue("REVERSE_IRON", "Long the inner strikes (a reverse iron condor/butterfly): "
                                                   "recorded as custom."))
    return "CUSTOM", None, notes


# ------------------------------------------------------------------ payoff
def pnl_at(legs: list[Leg], s: Decimal, premium: Decimal, fees: Decimal) -> Decimal:
    return Decimal(sum((lg.signed_qty * lg.multiplier * lg.intrinsic(s) for lg in legs), ZERO)) - premium - fees


def _payoff_analysis(legs: list[Leg], premium: Decimal, fees: Decimal):
    strikes = sorted({lg.k for lg in legs if lg.is_option})
    xs = [ZERO] + [k for k in strikes if k > 0]
    ys = [pnl_at(legs, x, premium, fees) for x in xs]
    slope_inf = sum((lg.signed_qty * lg.multiplier for lg in legs if not lg.is_option or lg.right == "CALL"), 0)
    hi_val, lo_val = max(ys), min(ys)
    max_gain = Bounded("unbounded") if slope_inf > 0 else Bounded.of(hi_val)
    if slope_inf < 0:
        max_loss = Bounded("unbounded")
    else:
        max_loss = Bounded.of(max(ZERO, -lo_val))
    bes: list[Decimal] = []
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if y0 == 0:
            bes.append(x0)
        elif (y0 < 0) != (y1 < 0) and y1 != 0:
            bes.append(x0 + (x1 - x0) * (-y0) / (y1 - y0))
    if ys[-1] == 0 and xs[-1] not in bes:
        bes.append(xs[-1])
    if slope_inf != 0 and ys[-1] != 0 and (ys[-1] < 0) == (slope_inf > 0):
        bes.append(xs[-1] - ys[-1] / slope_inf)
    bes = sorted({b.quantize(Decimal("0.0001")) for b in bes if b > 0})
    # chart points (display only): 80% of the lowest strike to 120% of the highest, plus every kink and
    # breakeven, so the interesting region fills the chart. Stock-only: 50%-150% of the share cost.
    if strikes:
        lo, hi = strikes[0] * Decimal("0.8"), strikes[-1] * Decimal("1.2")
    else:
        ref = next((lg.price for lg in legs if not lg.is_option and lg.price), Decimal(100))
        lo, hi = ref * Decimal("0.5"), ref * Decimal("1.5")
    grid = sorted({lo, hi, *strikes, *bes, *[lo + (hi - lo) * i / 20 for i in range(1, 20)]})
    pts = [(x.quantize(Decimal("0.0001")), pnl_at(legs, x, premium, fees)) for x in grid]
    return max_gain, max_loss, bes, pts


# ------------------------------------------------------------------ analysis
def analyze(legs: list[Leg], ctx: Context | None = None) -> Analysis:
    ctx = ctx or Context()
    errors = validate(legs)
    if errors:
        return Analysis("CUSTOM", NAMES["CUSTOM"], False, None, "unknown", [], 1, None, "UNKNOWN", ZERO, None,
                        Bounded("unknown"), Bounded("unknown"), None, "unknown", False, Bounded("unknown"),
                        "invalid legs", [], errors=errors)
    ordered, units, structure = normalise(legs)
    strategy, variant, notes = _match(ordered, ctx)
    warnings = [Issue(i.code, i.message) for i in notes]
    assumptions = ["Values are per position in dollars, from the recorded fill prices.",
                   "Opening fees are included; closing fees are unknown at entry and are not."]
    fees = Decimal(sum((lg.fees for lg in ordered), ZERO))
    opts = [lg for lg in ordered if lg.is_option]
    exps = sorted({lg.exp for lg in opts})
    missing = [i for i, lg in enumerate(legs) if lg.price is None]
    for i in missing:
        warnings.append(Issue("MISSING_FILL_PRICE", "No fill price: cost, maximum gain/loss and breakevens can't be "
                                                    "calculated.", i))
    premium = None if missing else Decimal(sum((lg.signed_qty * lg.multiplier * lg.px for lg in ordered), ZERO))
    if premium is None:
        ptype = "UNKNOWN"
    else:
        ptype = "DEBIT" if premium > 0 else "CREDIT" if premium < 0 else "EVEN"
        if strategy in EXPECT_DEBIT and premium <= 0:
            warnings.append(Issue("DEBIT_CREDIT_MISMATCH", f"A {NAMES[strategy].lower()} is normally opened for a debit, "
                                                           f"but these fills give a {ptype.lower()}. Check the prices."))
        if strategy in EXPECT_CREDIT and premium >= 0:
            warnings.append(Issue("DEBIT_CREDIT_MISMATCH", f"A {NAMES[strategy].lower()} is normally opened for a credit, "
                                                           f"but these fills give a {ptype.lower()}. Check the prices."))
    for lg in opts:
        if lg.multiplier != 100:
            warnings.append(Issue("NON_STANDARD_MULTIPLIER", f"Multiplier {lg.multiplier} (standard is 100): possibly an "
                                                             "adjusted contract after a split or merger."))
            break
    seen = set()
    for lg in opts:
        k = (lg.right, lg.k, lg.exp)
        if k in seen:
            warnings.append(Issue("DUPLICATE_CONTRACT", "The same contract appears in more than one leg; legs are kept "
                                                        "as entered and not netted."))
            break
        seen.add(k)
    today = ctx.today or date.today()
    if any(e < today for e in exps):
        warnings.append(Issue("EXPIRED_LEG", "At least one leg has already expired."))
    if ctx.earnings_date and exps and ctx.earnings_date <= max(exps) and ctx.earnings_date >= today:
        warnings.append(Issue("EARNINGS_BEFORE_EXPIRATION",
                              f"Earnings on {ctx.earnings_date:%Y-%m-%d} fall before expiration "
                              f"{max(exps):%Y-%m-%d}: expect a gap and an implied-volatility drop."))
    if len(exps) > 1 and strategy not in ("CALENDAR_SPREAD", "DIAGONAL_SPREAD"):
        warnings.append(Issue("MIXED_EXPIRATIONS", "Legs expire on different dates."))

    breakevens: list[Decimal] | None
    if premium is None:
        max_gain, max_loss, breakevens, pts = Bounded("unknown"), Bounded("unknown"), None, []
    elif len(exps) <= 1:
        max_gain, max_loss, breakevens, pts = _payoff_analysis(ordered, premium, fees)
    elif strategy in ("CALENDAR_SPREAD", "DIAGONAL_SPREAD"):
        front, back = sorted(opts, key=lambda lg: lg.exp)
        gap = (back.k - front.k) if back.right == "CALL" else (front.k - back.k)
        loss = premium + fees + max(ZERO, gap) * back.multiplier * back.quantity
        max_gain, max_loss, breakevens, pts = Bounded("unknown"), Bounded.of(max(ZERO, loss)), None, []
        assumptions.append("Maximum loss assumes the position is closed by the front expiration and the back-month "
                           "option is worth at least its intrinsic value then; maximum gain and breakevens depend on "
                           "implied volatility at the front expiration and are not calculated.")
    else:
        max_gain, max_loss, breakevens, pts = Bounded("unknown"), Bounded("unknown"), None, []
        assumptions.append("Several expirations: the payoff can't be calculated without a pricing model.")

    if max_loss.state == "unbounded":
        warnings.append(Issue("UNBOUNDED_LOSS", "The maximum loss is unlimited."))
    elif max_loss.state == "unknown" and premium is not None:
        warnings.append(Issue("UNKNOWN_LOSS", "The maximum loss can't be calculated for this structure."))
    defined = max_loss.state == "bounded" and strategy not in MARGIN_STRATEGIES
    if strategy == "CASH_SECURED_PUT":
        need = sum((lg.k * lg.multiplier * lg.quantity for lg in opts), ZERO)
        assumptions.append(f"Cash-secured: ${need:,.2f} of cash is set aside to buy the shares if assigned "
                           "(as declared; not verified against the broker).")
        if ctx.buying_power is not None and ctx.buying_power < need:
            warnings.append(Issue("CASH_SECURED_UNVERIFIED", f"Declared cash-secured, but the account's recorded buying "
                                                             f"power (${ctx.buying_power:,.2f}) is below the ${need:,.2f} "
                                                             "the put could require."))

    # buying-power effect (cash-account basis, an estimate)
    if premium is None:
        bp, basis = Bounded("unknown"), "fill prices missing"
    elif any(not lg.is_option for lg in ordered) and defined:
        bp, basis = Bounded.of(max(ZERO, premium + fees)), "cash account: share cost plus option debits, less credits"
    elif defined and max_loss.value is not None:          # defined risk implies a bounded maximum loss
        bp, basis = Bounded.of(max_loss.value), "defined risk: the maximum loss is set aside"
    else:
        bp, basis = Bounded("unknown"), "depends on the broker's margin rules (undefined or margin-based risk)"

    expirations = exps
    return Analysis(strategy, NAMES[strategy] + (" (broken wing)" if variant == "BROKEN_WING" else ""),
                    strategy != "CUSTOM", variant, DIRECTION[strategy], structure, units, premium, ptype, fees,
                    None if premium is None else premium + fees, max_gain, max_loss, breakevens,
                    "unknown" if breakevens is None else ("known" if breakevens else "none"), defined, bp, basis,
                    expirations, [], warnings, assumptions, pts)
