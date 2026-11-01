"""Risk checks: a candidate (a position you're about to record, or a plan) against the account's effective
policy and its current state.

Every check returns PASS, WARNING, BLOCKED or UNKNOWN with a plain-language reason, the value and the limit.
    BLOCKED   breaks a limit (or a limit would be broken once the candidate is added)
    WARNING   within `warn_at_pct` of a limit, or a rule set to warn (earnings, averaging down, unlimited risk)
    UNKNOWN   the check needs data that isn't available (no account value, no quotes, no Greeks); never PASS
    PASS      within the limit
Overall: BLOCKED if any check is BLOCKED, else UNKNOWN if any is UNKNOWN, else WARNING if any, else PASS.

BLOCKED means the candidate breaks *your* rules. optlab can't and doesn't stop a broker order; recording a
position that breaks a rule is allowed, but only with an explicit acknowledgement, and it's flagged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..portfolio import recognize as rz
from ..portfolio.models import Lifecycle, Position
from ..portfolio.money import from_units

PASS, WARNING, BLOCKED, UNKNOWN = "PASS", "WARNING", "BLOCKED", "UNKNOWN"
NAKED = {"SHORT_CALL", "SHORT_PUT", "SHORT_STRADDLE", "SHORT_STRANGLE"}
UNDEFINED_STRATEGIES = {"SHORT_CALL", "SHORT_STRADDLE", "SHORT_STRANGLE", "SHORT_STOCK"}


def usd(d: Decimal | float | None) -> str:
    if d is None:
        return "unknown"
    d = Decimal(str(d))
    return f"{'−' if d < 0 else ''}${abs(d):,.2f}"


@dataclass
class Check:
    code: str
    label: str
    result: str
    reason: str
    value: str | None = None
    limit: str | None = None
    source: str | None = None          # policy | small account


@dataclass
class Report:
    overall: str
    checks: list[Check]
    policy_revision: int | None
    policy_id: str | None
    small_account_active: bool
    small_account_reason: str
    evaluated_at: datetime
    summary: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["evaluated_at"] = self.evaluated_at.isoformat()
        return d


@dataclass
class OpenPos:
    id: str
    name: str
    underlying: str
    max_loss_state: str
    max_loss: Decimal | None
    open_pnl: Decimal | None          # from the latest fresh snapshot
    delta: float | None
    theta: float | None


@dataclass
class Portfolio:
    account_value: Decimal | None
    cash: Decimal | None
    buying_power: Decimal | None
    positions: list[OpenPos]
    realized_today: Decimal
    realized_week: Decimal
    today: date


@dataclass
class Candidate:
    underlying: str
    strategy: str | None                 # recognize.Strategy value
    max_loss_state: str                  # bounded | unbounded | unknown
    max_loss: Decimal | None
    capital: Decimal | None              # buying-power effect
    defined_risk: bool | None
    contracts: int | None                # largest option-leg quantity
    expirations: list[date] = field(default_factory=list)
    earnings: date | None = None
    leg_quotes: list[dict] | None = None   # [{occ, open_interest, spread_pct}] or None when not fetched
    delta: float | None = None
    theta: float | None = None
    has_plan: bool = False
    greeks_known: bool = False
    label: str = "This position"


def candidate_from_analysis(underlying: str, a: rz.Analysis, legs: list[rz.Leg], earnings: date | None,
                            quotes: dict | None = None, has_plan: bool = False) -> Candidate:
    opts = [lg for lg in legs if lg.asset_type == "OPTION"]
    c = Candidate(underlying=underlying, strategy=a.strategy, max_loss_state=a.max_loss.state,
                  max_loss=a.max_loss.value, capital=a.buying_power.value if a.buying_power.state == "bounded" else None,
                  defined_risk=a.defined_risk, contracts=max((lg.quantity for lg in opts), default=None),
                  expirations=list(a.expirations), earnings=earnings, has_plan=has_plan)
    if quotes is not None:
        c.leg_quotes = quotes.get("legs")
        g = quotes.get("greeks")
        if g:
            c.delta, c.theta, c.greeks_known = g.get("delta"), g.get("theta"), True
    return c


# ------------------------------------------------------------------ the checks
class _Run:
    def __init__(self, eff: dict, port: Portfolio):
        self.L, self.src, self.port = eff["limits"], eff["source"], port
        self.out: list[Check] = []
        self.warn = float(self.L["warn_at_pct"]) / 100

    def add(self, code, label, result, reason, value=None, limit=None, key=None):
        self.out.append(Check(code, label, result, reason, value, limit, self.src.get(key or code) if (key or code) in self.src else None))

    def cap(self, code: str, label: str, value: Decimal | float | None, key: str, what: str, fmt=usd, unknown_why=""):
        """value must not exceed the limit L[key]."""
        lim = self.L.get(key)
        if lim is None:
            return
        lim_d = Decimal(str(lim))
        if value is None:
            self.add(code, label, UNKNOWN, f"Can't check {what}: {unknown_why or 'not known'}.", None, fmt(lim_d), key)
            return
        v = Decimal(str(value))
        if v > lim_d:
            self.add(code, label, BLOCKED, f"{what.capitalize()} {fmt(v)} is over the limit of {fmt(lim_d)}.", fmt(v), fmt(lim_d), key)
        elif lim_d > 0 and v >= lim_d * Decimal(str(self.warn)):
            self.add(code, label, WARNING, f"{what.capitalize()} {fmt(v)} is {float(v / lim_d * 100):.0f}% of the limit "
                                           f"of {fmt(lim_d)}.", fmt(v), fmt(lim_d), key)
        else:
            self.add(code, label, PASS, f"{what.capitalize()} {fmt(v)} is within the limit of {fmt(lim_d)}.", fmt(v), fmt(lim_d), key)


def _pct(x) -> str:
    return f"{float(x):.1f}%"


def _num(x) -> str:
    return f"{float(x):,.1f}"


def _int(x) -> str:
    return f"{int(x)}"


def _groups_of(sym: str, groups: dict[str, list[str]]) -> list[str]:
    return [g for g, syms in groups.items() if sym.upper() in syms]


def account_checks(r: _Run, add_positions: int = 0, add_risk: Decimal | None = Decimal(0), cand: Candidate | None = None) -> None:
    """Limits on the account as a whole (with the candidate added when given)."""
    p, L = r.port, r.L
    # realized losses: no new risk once a loss limit is reached
    for code, label, key, realized, period in (("DAILY_LOSS", "Daily realized loss", "max_daily_realized_loss", p.realized_today, "today"),
                                              ("WEEKLY_LOSS", "Weekly realized loss", "max_weekly_realized_loss", p.realized_week, "this week")):
        lim = L.get(key)
        if lim is None:
            continue
        loss = max(Decimal(0), -realized)
        lim_d = Decimal(str(lim))
        if loss >= lim_d:
            r.add(code, label, BLOCKED, f"Realized loss {period} is {usd(loss)}, at or over the limit of {usd(lim_d)}: "
                                        "no new risk until the period resets.", usd(loss), usd(lim_d), key)
        elif lim_d > 0 and loss >= lim_d * Decimal(str(r.warn)):
            r.add(code, label, WARNING, f"Realized loss {period} is {usd(loss)} ({float(loss / lim_d * 100):.0f}% of the "
                                        f"{usd(lim_d)} limit).", usd(loss), usd(lim_d), key)
        else:
            r.add(code, label, PASS, f"Realized loss {period} is {usd(loss)} (limit {usd(lim_d)}).", usd(loss), usd(lim_d), key)
    # open risk
    bounded = [x.max_loss for x in p.positions if x.max_loss_state == "bounded" and x.max_loss is not None]
    open_unbounded = [x for x in p.positions if x.max_loss_state != "bounded"]
    total = sum(bounded, Decimal(0)) + (add_risk or Decimal(0)) if add_risk is not None else None
    extra = (f" (plus {len(open_unbounded)} position(s) with unlimited or unknown risk, not included)"
             if open_unbounded else "")
    if L.get("max_total_open_risk") is not None:
        r.cap("TOTAL_OPEN_RISK", "Total open risk", total, "max_total_open_risk", "total maximum loss of open positions"
              + (" with this one" if cand else "") + extra, unknown_why="this position's maximum loss isn't known")
    if L.get("max_total_open_risk_pct") is not None:
        pct = None if total is None or not p.account_value else total / p.account_value * 100
        why = "no account value entered" if not p.account_value else "this position's maximum loss isn't known"
        r.cap("TOTAL_OPEN_RISK_PCT", "Total open risk, % of account", pct, "max_total_open_risk_pct",
              "open risk as a share of the account" + extra, fmt=_pct, unknown_why=why)
    n = len(p.positions) + add_positions
    r.cap("OPEN_POSITIONS", "Open positions", n, "max_open_positions", "the number of open positions" + (" with this one" if cand else ""), fmt=_int)
    if cand is not None:
        same = sum(1 for x in p.positions if x.underlying == cand.underlying) + 1
        r.cap("PER_UNDERLYING", f"Positions in {cand.underlying}", same, "max_positions_per_underlying",
              f"the number of {cand.underlying} positions with this one", fmt=_int)
        for g in _groups_of(cand.underlying, L["correlation_groups"]):
            members = set(L["correlation_groups"][g])
            cnt = sum(1 for x in p.positions if x.underlying in members) + 1
            r.cap("PER_GROUP", f"Positions in {g}", cnt, "max_positions_per_group", f"positions in the group {g} with this one",
                  fmt=_int)
    # Greeks (from the latest fresh monitoring snapshots)
    for code, label, key, attr, what in (("NET_DELTA", "Net delta", "max_net_delta", "delta", "absolute net delta (shares)"),
                                         ("NEGATIVE_THETA", "Daily theta decay", "max_negative_theta", "theta", "daily theta decay")):
        if L.get(key) is None:
            continue
        vals = [getattr(x, attr) for x in p.positions]
        if any(v is None for v in vals) or (cand is not None and not cand.greeks_known):
            r.add(code, label, UNKNOWN, f"Can't check {what}: "
                  + ("a position has no recent Greeks (check the monitor)" if any(v is None for v in vals) else
                     "this position's Greeks need quotes"), None, _num(L[key]), key)
            continue
        tot = sum(v for v in vals if v is not None) + ((getattr(cand, attr) or 0) if cand else 0)
        val = abs(tot) if attr == "delta" else max(0.0, -tot)
        r.cap(code, label, val, key, what + (" with this one" if cand else ""), fmt=_num)


def position_checks(r: _Run, c: Candidate) -> None:
    L, p = r.L, r.port
    # defined risk and naked shorts
    if c.max_loss_state == "bounded":
        r.add("DEFINED_RISK", "Defined risk", PASS, f"Maximum loss is known: {usd(c.max_loss)}.", usd(c.max_loss), None,
              "defined_risk_only")
    elif c.max_loss_state == "unbounded":
        res = BLOCKED if L["defined_risk_only"] else WARNING
        r.add("DEFINED_RISK", "Defined risk", res, "Maximum loss is unlimited." + (" Your policy allows defined-risk "
              "positions only." if res == BLOCKED else " Your policy allows it, but nothing caps the loss."),
              "unlimited", "defined risk only" if L["defined_risk_only"] else None, "defined_risk_only")
    else:
        r.add("DEFINED_RISK", "Defined risk", UNKNOWN, "Maximum loss can't be calculated (a fill price or a planned "
              "maximum loss is missing).", None, None, "defined_risk_only")
    if c.strategy in NAKED and not L["allow_naked_short_options"]:
        r.add("NAKED_SHORT", "Naked short options", BLOCKED, f"{rz.NAMES.get(c.strategy, c.strategy)}: your policy "
              "doesn't allow uncovered short options.", rz.NAMES.get(c.strategy, c.strategy), "not allowed",
              "allow_naked_short_options")
    # size
    if c.max_loss_state == "bounded" or L.get("max_loss_per_position") is not None:
        r.cap("MAX_LOSS", "Maximum loss per position", c.max_loss if c.max_loss_state == "bounded" else None,
              "max_loss_per_position", "maximum loss", unknown_why="maximum loss is unlimited or unknown")
    if L.get("max_risk_pct_per_position") is not None:
        pct = (c.max_loss / p.account_value * 100) if c.max_loss is not None and c.max_loss_state == "bounded" and p.account_value else None
        why = "no account value entered" if not p.account_value else "maximum loss is unlimited or unknown"
        r.cap("RISK_PCT", "Risk, % of account", pct, "max_risk_pct_per_position", "maximum loss as a share of the account",
              fmt=_pct, unknown_why=why)
    r.cap("CAPITAL", "Capital per position", c.capital, "max_capital_per_position", "capital required (buying-power effect)",
          unknown_why="the buying-power effect isn't known (missing fill prices, or the broker's margin rules decide it)")
    if c.contracts is not None:
        r.cap("CONTRACTS", "Contracts per position", c.contracts, "max_contracts_per_position", "contracts per leg", fmt=_int)
    if not L["allow_margin"]:
        if c.capital is None or p.cash is None:
            r.add("NO_MARGIN", "No margin", UNKNOWN, "Can't check that this fits in cash: "
                  + ("enter the account's cash" if p.cash is None else "capital required isn't known") + ".",
                  None, usd(p.cash) if p.cash is not None else None, "allow_margin")
        elif c.capital > p.cash:
            r.add("NO_MARGIN", "No margin", BLOCKED, f"Needs {usd(c.capital)} but cash is {usd(p.cash)}: it would use "
                  "margin, which your policy doesn't allow.", usd(c.capital), usd(p.cash), "allow_margin")
        else:
            r.add("NO_MARGIN", "No margin", PASS, f"Needs {usd(c.capital)} of {usd(p.cash)} cash.", usd(c.capital),
                  usd(p.cash), "allow_margin")
    # time
    if c.expirations and (L.get("min_dte") is not None or L.get("max_dte") is not None):
        dtes = [(e - p.today).days for e in c.expirations]
        if L.get("min_dte") is not None:
            v = min(dtes)
            res = BLOCKED if v < L["min_dte"] else PASS
            r.add("MIN_DTE", "Minimum days to expiration", res, f"Nearest expiration is in {v} days (minimum {L['min_dte']}).",
                  _int(v), _int(L["min_dte"]), "min_dte")
        if L.get("max_dte") is not None:
            v = max(dtes)
            res = BLOCKED if v > L["max_dte"] else PASS
            r.add("MAX_DTE", "Maximum days to expiration", res, f"Furthest expiration is in {v} days (maximum {L['max_dte']}).",
                  _int(v), _int(L["max_dte"]), "max_dte")
    if L["earnings"] != "off" and c.expirations:
        last = max(c.expirations)
        if c.earnings is None:
            r.add("EARNINGS", "Earnings before expiration", PASS, f"No earnings date known for {c.underlying} (ETFs and "
                  "indexes have none; for stocks, check the date yourself).", None, None, "earnings")
        elif p.today <= c.earnings <= last:
            res = BLOCKED if L["earnings"] == "block" else WARNING
            r.add("EARNINGS", "Earnings before expiration", res, f"{c.underlying} reports on {c.earnings:%Y-%m-%d}, before "
                  f"the expiration on {last:%Y-%m-%d}.", f"{c.earnings:%Y-%m-%d}", L["earnings"], "earnings")
        else:
            r.add("EARNINGS", "Earnings before expiration", PASS, f"Next earnings ({c.earnings:%Y-%m-%d}) are after the "
                  "expiration.", f"{c.earnings:%Y-%m-%d}", None, "earnings")
    # liquidity (needs quotes)
    for code, label, key, attr, cmp, what, fmt in (("OPEN_INTEREST", "Open interest", "min_open_interest", "open_interest", "min",
                                                    "open interest", _int),
                                                   ("SPREAD", "Bid/ask spread", "max_spread_pct", "spread_pct", "max",
                                                    "bid/ask spread", _pct)):
        lim = L.get(key)
        if lim is None:
            continue
        if c.leg_quotes is None:
            r.add(code, label, UNKNOWN, f"Can't check the {what}: no quotes for the legs yet.", None, fmt(lim), key)
            continue
        vals: list[tuple[str, Any]] = [(str(q["occ_symbol"]), q.get(attr)) for q in c.leg_quotes if q.get("occ_symbol")]
        missing = [o for o, v in vals if v is None]
        bad = [(o, v) for o, v in vals if v is not None and (v < lim if cmp == "min" else v > lim)]
        if bad:
            r.add(code, label, BLOCKED, "; ".join(f"{o}: {what} {fmt(v)}" for o, v in bad)
                  + f" ({'minimum' if cmp == 'min' else 'maximum'} {fmt(lim)}).", fmt(bad[0][1]), fmt(lim), key)
        elif missing:
            r.add(code, label, UNKNOWN, f"No {what} for {', '.join(missing)}.", None, fmt(lim), key)
        elif vals:
            nums = [v for _, v in vals if v is not None]
            worst = min(nums) if cmp == "min" else max(nums)
            r.add(code, label, PASS, f"Every leg's {what} is within the limit (worst {fmt(worst)}).", fmt(worst), fmt(lim), key)
    # averaging down
    if L["averaging_down"] != "allow":
        losing = [x for x in p.positions if x.underlying == c.underlying and x.open_pnl is not None and x.open_pnl < 0]
        if losing:
            names = ", ".join(f"{x.name} ({usd(x.open_pnl)})" for x in losing)
            if L["averaging_down"] == "require_plan" and not c.has_plan:
                r.add("AVERAGING_DOWN", "Averaging down", BLOCKED, f"You already hold losing {c.underlying}: {names}. Adding "
                      "to it needs a new written plan.", names, "plan required", "averaging_down")
            else:
                r.add("AVERAGING_DOWN", "Averaging down", WARNING if not c.has_plan else PASS,
                      f"You already hold losing {c.underlying}: {names}." + (" Documented in a plan." if c.has_plan else ""),
                      names, L["averaging_down"], "averaging_down")


def overall(checks: list[Check]) -> str:
    rs = {c.result for c in checks}
    return BLOCKED if BLOCKED in rs else UNKNOWN if UNKNOWN in rs else WARNING if WARNING in rs else PASS


def evaluate(eff: dict, port: Portfolio, cand: Candidate, policy_row=None, now: datetime | None = None) -> Report:
    r = _Run(eff, port)
    position_checks(r, cand)
    add_risk = cand.max_loss if cand.max_loss_state == "bounded" else None
    account_checks(r, 1, add_risk, cand)
    o = overall(r.out)
    bad = [c for c in r.out if c.result in (BLOCKED, UNKNOWN, WARNING)]
    summary = ({PASS: "Within every limit of your risk policy.",
                WARNING: "Within your limits, with warnings.",
                UNKNOWN: "Some limits can't be checked yet.",
                BLOCKED: "Breaks your risk policy."}[o]
               + ("" if not bad else " " + " ".join(f"{c.label}: {c.reason}" for c in bad if c.result == o)[:600]))
    from ..portfolio.models import utcnow

    return Report(o, r.out, policy_row.revision if policy_row else None, policy_row.id if policy_row else None,
                  eff["small_account_active"], eff["small_account_reason"], now or utcnow(), summary)


def utilization(eff: dict, port: Portfolio, policy_row=None, now: datetime | None = None) -> Report:
    """The account against its limits right now (no candidate): the risk dashboard."""
    r = _Run(eff, port)
    account_checks(r)
    from ..portfolio.models import utcnow

    o = overall(r.out)
    return Report(o, r.out, policy_row.revision if policy_row else None, policy_row.id if policy_row else None,
                  eff["small_account_active"], eff["small_account_reason"], now or utcnow(),
                  {PASS: "The account is within its limits.", WARNING: "Close to a limit.",
                   UNKNOWN: "Some limits can't be checked.", BLOCKED: "A limit is reached: no new risk."}[o])


# ------------------------------------------------------------------ the account's state
def period_starts(now: datetime, tz: str) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo(tz))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    return day, week


def portfolio_state(s: Session, account_id: str, now: datetime, tz: str = "America/New_York",
                    exclude_position: str | None = None) -> Portfolio:
    from ..monitoring import settings as msettings
    from ..monitoring.read import position_views
    from ..portfolio.service import latest_snapshot

    snap = latest_snapshot(s, account_id)
    opens = list(s.scalars(select(Position).options(selectinload(Position.legs))
                           .where(Position.account_id == account_id, Position.lifecycle == Lifecycle.OPEN)))
    opens = [p for p in opens if p.id != exclude_position]
    views = position_views(s, opens, msettings.load(s), now) if opens else {}
    out = []
    for p in opens:
        v = views[p.id]
        sn = v["snapshot"] if not v["stale_overlay"] and v["status"] not in ("NOT_MONITORED",) else None
        out.append(OpenPos(p.id, p.name, p.underlying, p.max_loss_state.value, from_units(p.max_loss_units),
                           Decimal(sn["open_pnl"]) if sn and sn["open_pnl"] is not None else None,
                           sn["greeks"]["delta"] if sn else None, sn["greeks"]["theta"] if sn else None))
    day, week = period_starts(now, tz)
    closed = list(s.scalars(select(Position).where(Position.account_id == account_id, Position.closed_at.is_not(None),
                                                   Position.closed_at >= week, Position.realized_pnl_units.is_not(None))))
    rt = sum((from_units(p.realized_pnl_units) for p in closed if p.closed_at and p.closed_at >= day
              and p.realized_pnl_units is not None), Decimal(0))
    rw = sum((from_units(p.realized_pnl_units) for p in closed if p.realized_pnl_units is not None), Decimal(0))
    return Portfolio(from_units(snap.total_value_units) if snap else None, from_units(snap.cash_units) if snap else None,
                     from_units(snap.buying_power_units) if snap else None, out, rt, rw,
                     now.astimezone(ZoneInfo(tz)).date())


def as_jsonable(x: Any) -> Any:
    return x.as_dict() if hasattr(x, "as_dict") else x
