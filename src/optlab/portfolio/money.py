"""Exact money handling.

Storage   every persisted money value is an integer count of $0.0001 ("units"): $1.2345 -> 12345.
Inputs    user-entered prices, fees and account values are parsed with Decimal and must have at most
          four decimal places. More precision is REJECTED (MoneyPrecisionError), never rounded.
Computed  values the system derives (a per-share value times 100, an average, a provider quote that
          arrives as a binary float) are rounded once, to the nearest $0.0001, half to even
          (banker's rounding), at the persistence or API boundary: `quantize_computed`.
Provider  quotes arrive as floats from pandas. They are converted through their shortest decimal
          string (Decimal(str(x))), so 1.2 becomes exactly 1.2, then rounded as computed values.
Floats    never cross into money: `to_units` refuses float input so a binary float can't slip in.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import overload

SCALE = 10_000
QUANTUM = Decimal("0.0001")
MAX_ABS_UNITS = 10**17          # $10 trillion: anything larger is a typo, and stays well inside int64


class MoneyPrecisionError(ValueError):
    """A money input has more than four decimal places, or isn't a finite number."""


def parse_decimal(value: str | int | Decimal, field: str = "value") -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyPrecisionError(f"{field}: send money as a decimal string or integer, not a binary float")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        raise MoneyPrecisionError(f"{field}: {value!r} is not a number") from None
    if not d.is_finite():
        raise MoneyPrecisionError(f"{field}: {value!r} is not a finite number")
    return d


def to_units(value: str | int | Decimal, field: str = "value") -> int:
    """User input -> integer units. Rejects more than 4 decimal places."""
    d = parse_decimal(value, field)
    if d != d.quantize(QUANTUM):
        raise MoneyPrecisionError(f"{field}: {value} has more than 4 decimal places; "
                                  "optlab stores money to $0.0001 and does not round inputs")
    units = int(d * SCALE)
    if abs(units) > MAX_ABS_UNITS:
        raise MoneyPrecisionError(f"{field}: {value} is out of range")
    return units


def quantize_computed(d: Decimal) -> Decimal:
    """Round a derived value to $0.0001, half to even."""
    return d.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def computed_to_units(d: Decimal) -> int:
    return int(quantize_computed(d) * SCALE)


@overload
def from_units(units: int) -> Decimal: ...
@overload
def from_units(units: None) -> None: ...
@overload
def from_units(units: int | None) -> Decimal | None: ...
def from_units(units: int | None) -> Decimal | None:
    if units is None:
        return None
    return (Decimal(units) / SCALE).quantize(QUANTUM)


def from_provider(x) -> Decimal | None:
    """A provider float (or None/NaN) -> Decimal rounded to $0.0001; None when missing."""
    if x is None:
        return None
    try:
        if x != x:                     # NaN
            return None
    except TypeError:
        return None
    try:
        return quantize_computed(Decimal(str(x)))
    except InvalidOperation:
        return None


@overload
def fmt(d: Decimal) -> str: ...
@overload
def fmt(d: None) -> None: ...
@overload
def fmt(d: Decimal | None) -> str | None: ...
def fmt(d: Decimal | None) -> str | None:
    """Canonical API string: always 4 decimal places ("1.2000"), or None."""
    return None if d is None else f"{quantize_computed(d):.4f}"
