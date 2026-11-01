"""On-demand valuation of a position from the shared quote service (Phase 1: an estimate when asked,
not continuous monitoring; nothing here assigns HOLD/WATCH statuses).

    leg mark        options: mid of bid and ask (crossed or missing quotes -> unavailable)
                    stock:   the chain's underlying price
    leg value       signed quantity × multiplier × mark      (+ asset you own, − obligation you owe)
    position value  Σ leg values, only when every leg has a mark; otherwise unavailable, never partial
    open P&L        position value − net premium paid at entry − opening fees
    exit value      the same at natural prices (sell longs at the bid, buy back shorts at the ask)
    Greeks          Σ signed quantity × multiplier × per-share Greek; stock adds its share count to delta.
                    A Greek is unavailable if any option leg lacks it.
All results are labelled ESTIMATED with the quote provider, delay class, fetch time and age.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .money import from_units, quantize_computed
from .quotes import ESTIMATED, ContractQuote, QuoteService

GREEKS = ("delta", "gamma", "theta", "vega")


@dataclass
class LegValuation:
    leg_index: int
    occ_symbol: str | None
    status: str                      # FOUND | NOT_FOUND | UNAVAILABLE
    reason: str | None
    mark: Decimal | None
    exit_price: Decimal | None
    value: Decimal | None
    bid: Decimal | None = None
    ask: Decimal | None = None
    iv: float | None = None
    greeks: dict = field(default_factory=dict)
    quality: list[str] = field(default_factory=list)
    nearest: list[str] = field(default_factory=list)
    quote: ContractQuote | None = None          # the full quote (open interest, spread, ...) for the monitor


@dataclass
class PositionValuation:
    label: str
    status: str                      # AVAILABLE | PARTIAL | UNAVAILABLE
    reasons: list[str]
    value: Decimal | None
    exit_value: Decimal | None
    open_pnl: Decimal | None
    open_pnl_pct: float | None
    underlying_price: Decimal | None
    greeks: dict
    legs: list[LegValuation]
    provider: str
    freshness: str
    delay_minutes: int
    fetched_at: datetime | None
    source_timestamp: datetime | None
    quote_age_seconds: float | None
    greeks_source: str
    valued_at: datetime


def value_position(position, quotes: QuoteService) -> PositionValuation:
    snap = quotes.chain(position.underlying)
    now = quotes.wall() if hasattr(quotes, "wall") else datetime.now(timezone.utc)
    legs, reasons = [], []
    under = snap.underlying_price()
    for lg in position.legs:
        if lg.asset_type.value == "STOCK":
            mark = under
            st = "FOUND" if mark is not None else "UNAVAILABLE"
            why = None if mark is not None else (snap.error or "no underlying price in the chain snapshot")
            sign = 1 if lg.side.value == "BUY" else -1
            lv = LegValuation(lg.leg_index, None, st, why, mark, mark,
                              None if mark is None else sign * lg.quantity * mark,
                              greeks={"delta": float(sign * lg.quantity), "gamma": 0.0, "theta": 0.0, "vega": 0.0})
        else:
            q, _ = quotes.contract(position.underlying, lg.expiration, lg.option_right.value,
                                   from_units(lg.strike_units), snap)
            sign = 1 if lg.side.value == "BUY" else -1
            n = sign * lg.quantity * lg.multiplier
            exit_px = (q.bid if sign > 0 else q.ask) if q.status == "FOUND" else None
            why = q.reason
            if q.status == "FOUND" and q.mid is None:
                why = "no usable mid price (" + ", ".join(q.quality) + ")"
            g = {k: (None if getattr(q, k) is None else getattr(q, k) * n) for k in GREEKS}
            status = "UNAVAILABLE" if q.status == "FOUND" and q.mid is None else q.status
            lv = LegValuation(lg.leg_index, q.occ_symbol, status, why, q.mid, exit_px,
                              None if q.mid is None else n * q.mid, q.bid, q.ask, q.iv, g, q.quality, q.nearest, q)
        if lv.value is None:
            reasons.append(f"Leg {lg.leg_index + 1}: {lv.reason or 'no quote'}")
        legs.append(lv)
    complete = all(lv.value is not None for lv in legs)
    value = quantize_computed(sum((lv.value for lv in legs if lv.value is not None), Decimal(0))) if complete else None
    exit_ok = complete and all(lv.exit_price is not None for lv in legs)
    exit_value = None
    if exit_ok:
        exit_value = Decimal(0)
        for lg, lv in zip(position.legs, legs):
            sign = 1 if lg.side.value == "BUY" else -1
            exit_value += sign * lg.quantity * (lg.multiplier or 1) * lv.exit_price
        exit_value = quantize_computed(exit_value)
    premium = from_units(position.net_premium_units)
    fees = from_units(position.fees_units) or Decimal(0)
    pnl = None if value is None or premium is None else quantize_computed(value - premium - fees)
    basis = None if premium is None else abs(premium) + fees
    pnl_pct = float(pnl / basis * 100) if pnl is not None and basis else None
    greeks = {}
    for k in GREEKS:
        vals = [lv.greeks.get(k) for lv in legs]
        greeks[k] = None if any(v is None for v in vals) else round(sum(v for v in vals if v is not None), 6)
    status = "AVAILABLE" if complete else ("UNAVAILABLE" if not any(lv.value is not None for lv in legs) else "PARTIAL")
    return PositionValuation(ESTIMATED, status, reasons, value, exit_value, pnl, pnl_pct, under, greeks, legs,
                             snap.provider, snap.freshness(quotes.max_age, now), snap.delay_minutes, snap.fetched_at,
                             snap.source_timestamp, snap.age_seconds(now), snap.greeks_source, now)
