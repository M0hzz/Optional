"""Risk policies: the account's own limits, plus small-account limits that apply automatically while the
account value is below a threshold.

Nothing is hard-coded: every limit is a setting with a default, and a limit left empty is not checked.
Money is kept as exact decimal strings (never floats); percentages, Greeks and counts are numbers.

Small-account mode (on by default, below $1,000 of account value) applies, on top of the account's
policy, whichever is stricter of the two for each limit. Its defaults come from the overlay's small-account
profile: defined risk only, one-lot positions, $100 maximum loss and $300 maximum capital per position,
10% of the account at risk per position, tight spreads (10%) and open interest of at least 100, no naked
short options, no margin, and no averaging down without a new written plan.

A policy is changed by adding a revision (risk_policies); earlier revisions stay, so a past risk check can
always be explained by the policy it used.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..portfolio.models import RiskPolicy, utcnow
from ..portfolio.schemas import NonNegMoney

Pct = Annotated[float, Field(ge=0, le=100)]
Count = Annotated[int, Field(ge=0, le=10_000)]
AVERAGING = ("allow", "warn", "require_plan")
DEFAULT_GROUPS = {
    "US index ETFs and indexes": ["SPY", "QQQ", "IWM", "DIA", "SPX", "XSP", "NDX", "RUT", "VOO", "IVV"],
    "Mega-cap tech": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO"],
}


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SmallAccountLimits(_M):
    max_capital_per_position: NonNegMoney | None = Decimal("300")
    max_loss_per_position: NonNegMoney | None = Decimal("100")
    max_risk_pct_per_position: Pct | None = 10
    max_contracts_per_position: Count | None = 1
    max_spread_pct: Annotated[float, Field(gt=0, le=500)] | None = 10
    min_open_interest: Count | None = 100
    defined_risk_only: bool = True
    allow_naked_short_options: bool = False
    allow_margin: bool = False
    averaging_down: Literal["allow", "warn", "require_plan"] = "require_plan"


class PolicySettings(_M):
    # per position
    max_capital_per_position: NonNegMoney | None = None
    max_loss_per_position: NonNegMoney | None = None
    max_risk_pct_per_position: Pct | None = None
    max_contracts_per_position: Count | None = None
    defined_risk_only: bool = False
    allow_naked_short_options: bool = True
    allow_margin: bool = True
    # account-wide
    max_total_open_risk: NonNegMoney | None = None
    max_total_open_risk_pct: Pct | None = None
    max_daily_realized_loss: NonNegMoney | None = None
    max_weekly_realized_loss: NonNegMoney | None = None
    max_open_positions: Count | None = None
    max_positions_per_underlying: Count | None = None
    max_positions_per_group: Count | None = None
    correlation_groups: dict[Annotated[str, Field(max_length=60)], list[str]] = Field(default_factory=lambda: dict(DEFAULT_GROUPS))
    max_net_delta: Annotated[float, Field(ge=0)] | None = None        # absolute, in shares
    max_negative_theta: Annotated[float, Field(ge=0)] | None = None   # $ per day of decay you accept
    # contract quality and timing
    min_open_interest: Count | None = None
    max_spread_pct: Annotated[float, Field(gt=0, le=500)] | None = None
    min_dte: Count | None = None
    max_dte: Count | None = None
    earnings: Literal["off", "warn", "block"] = "warn"
    averaging_down: Literal["allow", "warn", "require_plan"] = "warn"
    warn_at_pct: Annotated[float, Field(ge=1, le=100)] = 80          # WARNING from this share of a limit
    # small account
    small_account_mode: bool = True
    small_account_threshold: NonNegMoney = Decimal("1000")
    small_account: SmallAccountLimits = SmallAccountLimits()

    @field_validator("correlation_groups")
    @classmethod
    def _groups(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        if len(v) > 50:
            raise ValueError("at most 50 groups")
        return {k.strip(): sorted({s.strip().upper() for s in syms if s.strip()}) for k, syms in v.items() if k.strip()}


# ------------------------------------------------------------------ effective policy
STRICTER_MIN = ("max_capital_per_position", "max_loss_per_position", "max_risk_pct_per_position",
                "max_contracts_per_position", "max_spread_pct")
STRICTER_MAX = ("min_open_interest",)


def effective(p: PolicySettings, account_value: Decimal | None) -> dict[str, Any]:
    """The limits in force and where each comes from ("policy" or "small account")."""
    out: dict[str, Any] = {k: getattr(p, k) for k in PolicySettings.model_fields if k != "small_account"}
    source = {k: "policy" for k in out}
    active, why = False, "Small-account mode is off."
    if p.small_account_mode:
        if account_value is None:
            why = ("Small-account mode can't tell: no account value entered. Enter the account value so the "
                   "right limits apply.")
        elif account_value < p.small_account_threshold:
            active = True
            why = f"Small-account mode is on: account value ${account_value:,.2f} is below ${p.small_account_threshold:,.2f}."
        else:
            why = f"Small-account mode is off: account value ${account_value:,.2f} is at least ${p.small_account_threshold:,.2f}."
    if active:
        sa = p.small_account
        for k in STRICTER_MIN:
            a, b = out[k], getattr(sa, k)
            if b is not None and (a is None or b < a):
                out[k], source[k] = b, "small account"
        for k in STRICTER_MAX:
            a, b = out[k], getattr(sa, k)
            if b is not None and (a is None or b > a):
                out[k], source[k] = b, "small account"
        for k in ("defined_risk_only",):
            if sa.defined_risk_only and not out[k]:
                out[k], source[k] = True, "small account"
        for k in ("allow_naked_short_options", "allow_margin"):
            if not getattr(sa, k) and out[k]:
                out[k], source[k] = False, "small account"
        if AVERAGING.index(sa.averaging_down) > AVERAGING.index(out["averaging_down"]):
            out["averaging_down"], source["averaging_down"] = sa.averaging_down, "small account"
    return {"limits": out, "source": source, "small_account_active": active, "small_account_reason": why}


# ------------------------------------------------------------------ storage (revisions)
def current(s: Session, account_id: str) -> RiskPolicy | None:
    return s.scalar(select(RiskPolicy).where(RiskPolicy.account_id == account_id, RiskPolicy.superseded_at.is_(None))
                    .order_by(RiskPolicy.revision.desc()).limit(1))


def load(s: Session, account_id: str) -> tuple[PolicySettings, RiskPolicy | None]:
    row = current(s, account_id)
    if row is None:
        return PolicySettings(), None
    return PolicySettings.model_validate(row.settings_json), row


def save(s: Session, account_id: str, settings: PolicySettings, notes: str | None = None) -> RiskPolicy:
    old = current(s, account_id)
    now = utcnow()
    if old is not None:
        old.superseded_at = now
        s.flush()
    row = RiskPolicy(account_id=account_id, revision=(old.revision + 1) if old else 1,
                     settings_json=settings.model_dump(mode="json"), notes=notes, created_at=now)
    s.add(row)
    s.flush()
    return row


def history(s: Session, account_id: str) -> list[RiskPolicy]:
    return list(s.scalars(select(RiskPolicy).where(RiskPolicy.account_id == account_id)
                          .order_by(RiskPolicy.revision.desc())))
