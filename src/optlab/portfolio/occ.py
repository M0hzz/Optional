"""OCC option symbols: ROOT (1-6 chars) + YYMMDD + C/P + strike × 1000 as 8 digits.

    AAPL  261218C00250000   ->  AAPL, 2026-12-18, CALL, 250.000

The padded 21-character form (root padded with spaces to 6) is the OCC standard; providers often
drop the padding. `build` returns the compact form (no spaces); `parse` accepts either.
Strikes must be multiples of $0.001; anything finer can't be represented and is rejected.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

_RE = re.compile(r"^([A-Z0-9.]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


class OCCError(ValueError):
    pass


def build(root: str, expiration: date, right: str, strike: Decimal) -> str:
    root = root.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.]{1,6}", root):
        raise OCCError(f"option root {root!r} must be 1-6 letters, digits or dots")
    milli = strike * 1000
    if milli != milli.to_integral_value() or strike <= 0 or milli >= 10**8:
        raise OCCError(f"strike {strike} can't be written in an OCC symbol (positive, at most $0.001 precision, "
                       "below $100,000)")
    r = {"CALL": "C", "PUT": "P"}.get(right.upper())
    if r is None:
        raise OCCError(f"right must be CALL or PUT, not {right!r}")
    return f"{root}{expiration:%y%m%d}{r}{int(milli):08d}"


def parse(symbol: str) -> tuple[str, date, str, Decimal]:
    m = _RE.match(symbol.strip().upper())
    if not m:
        raise OCCError(f"{symbol!r} is not an OCC option symbol (ROOT + YYMMDD + C/P + 8-digit strike)")
    root, yy, mm, dd, cp, k = m.groups()
    try:
        exp = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        raise OCCError(f"{symbol!r} has an invalid expiration date") from None
    return root, exp, "CALL" if cp == "C" else "PUT", (Decimal(k) / 1000).normalize()
