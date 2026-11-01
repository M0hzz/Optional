"""Unit tests: UUIDv7, exact money, OCC symbols, strategy recognition and payoff analytics."""
from __future__ import annotations

import itertools
import uuid
from datetime import date
from decimal import Decimal as D

import pytest

from optlab.portfolio import money
from optlab.portfolio.ids import new_id, timestamp_ms, uuid7
from optlab.portfolio.occ import OCCError, build, parse
from optlab.portfolio.recognize import Context, Leg, analyze

E1, E2 = date(2026, 12, 18), date(2027, 1, 15)
TODAY = date(2026, 11, 2)


# ------------------------------------------------------------------ UUIDv7
def test_uuid7_layout_order_and_uniqueness():
    ids = [uuid7() for _ in range(20_000)]
    assert all(u.version == 7 and u.variant == uuid.RFC_4122 for u in ids)
    assert ids == sorted(ids, key=lambda u: u.int)          # strictly increasing within a process
    assert len(set(ids)) == len(ids)
    assert abs(timestamp_ms(new_id()) - timestamp_ms(uuid7())) < 1000


def test_uuid7_counter_handles_a_clock_that_stands_still_or_steps_back():
    a = uuid7(now_ms=1_800_000_000_000)
    b = uuid7(now_ms=1_800_000_000_000)          # same millisecond
    c = uuid7(now_ms=1_799_999_999_000)          # clock stepped back a second
    assert a.int < b.int < c.int
    assert timestamp_ms(c) >= timestamp_ms(a)


# ------------------------------------------------------------------ money
@pytest.mark.parametrize("value,units", [("1.2345", 12345), ("0", 0), ("-2.5", -25000), (7, 70000), ("1e-4", 1),
                                         (D("100.10"), 1001000), ("  3.0001 ", 30001)])
def test_money_to_units_is_exact(value, units):
    assert money.to_units(value) == units


@pytest.mark.parametrize("value", ["1.23456", "0.00001", "1e-5", "nan", "inf", "abc", 1.25, True])
def test_money_rejects_extra_precision_floats_and_non_numbers(value):
    with pytest.raises(money.MoneyPrecisionError):
        money.to_units(value)


def test_money_computed_rounding_is_half_even_and_provider_floats_are_clean():
    assert money.quantize_computed(D("0.00005")) == D("0.0000")
    assert money.quantize_computed(D("0.00015")) == D("0.0002")
    assert money.quantize_computed(D("-1.23455")) == D("-1.2346")
    assert money.from_provider(1.2) == D("1.2000") and money.from_provider(0.1 + 0.2) == D("0.3000")
    assert money.from_provider(float("nan")) is None and money.from_provider(None) is None
    assert money.from_units(12345) == D("1.2345") and money.from_units(None) is None
    assert money.fmt(D("2")) == "2.0000"
    with pytest.raises(money.MoneyPrecisionError):
        money.to_units("1" + "0" * 14)                      # $100 trillion: out of range


# ------------------------------------------------------------------ OCC
def test_occ_roundtrip_and_validation():
    s = build("aapl", E1, "CALL", D("250"))
    assert s == "AAPL261218C00250000"
    assert parse(s) == ("AAPL", E1, "CALL", D("250"))
    assert parse("SPXW  261218P05000500")[3] == D("5000.5")
    assert build("SPY", E1, "PUT", D("432.5")) == "SPY261218P00432500"
    for bad in (lambda: build("SPY", E1, "PUT", D("1.0005")), lambda: build("TOOLONGROOT", E1, "PUT", D(1)),
                lambda: parse("AAPL"), lambda: build("SPY", E1, "X", D(1))):
        with pytest.raises(OCCError):
            bad()


# ------------------------------------------------------------------ recognition
def o(side, right, k, px, q=1, e=E1, fees="0"):
    return Leg("OPTION", side, q, right, D(str(k)), e, None if px is None else D(px), D(fees), 100)


def st(side, q, px):
    return Leg("STOCK", side, q, None, None, None, D(px), D("0"), 1)


CTX = Context(today=TODAY)
# name -> (legs, context, strategy, variant, max_gain(state, value), max_loss(state, value), breakevens, defined)
MATRIX = {
    "long stock": ([st("BUY", 100, "50")], CTX, "LONG_STOCK", None, ("unbounded", None), ("bounded", "5000"), ["50"], True),
    "short stock": ([st("SELL", 100, "50")], CTX, "SHORT_STOCK", None, ("bounded", "5000"), ("unbounded", None), ["50"], False),
    "long call": ([o("BUY", "CALL", 100, "2.50")], CTX, "LONG_CALL", None, ("unbounded", None), ("bounded", "250"), ["102.5"], True),
    "long put": ([o("BUY", "PUT", 100, "2.00")], CTX, "LONG_PUT", None, ("bounded", "9800"), ("bounded", "200"), ["98"], True),
    "short call (uncovered)": ([o("SELL", "CALL", 100, "2.50")], CTX, "SHORT_CALL", None, ("bounded", "250"), ("unbounded", None), ["102.5"], False),
    "short put (uncovered)": ([o("SELL", "PUT", 100, "2.00")], CTX, "SHORT_PUT", None, ("bounded", "200"), ("bounded", "9800"), ["98"], False),
    "cash-secured put": ([o("SELL", "PUT", 100, "2.00")], Context(cash_secured=True, today=TODAY), "CASH_SECURED_PUT", None,
                         ("bounded", "200"), ("bounded", "9800"), ["98"], True),
    "covered call": ([st("BUY", 100, "98"), o("SELL", "CALL", 105, "1.00")], CTX, "COVERED_CALL", None,
                     ("bounded", "800"), ("bounded", "9700"), ["97"], True),
    "protective put": ([st("BUY", 100, "100"), o("BUY", "PUT", 95, "2.00")], CTX, "PROTECTIVE_PUT", None,
                       ("unbounded", None), ("bounded", "700"), ["102"], True),
    "collar": ([st("BUY", 100, "100"), o("BUY", "PUT", 95, "1"), o("SELL", "CALL", 110, "1")], CTX, "COLLAR", None,
               ("bounded", "1000"), ("bounded", "500"), ["100"], True),
    "call debit spread": ([o("BUY", "CALL", 100, "3.00"), o("SELL", "CALL", 105, "1.00")], CTX, "CALL_DEBIT_SPREAD", None,
                          ("bounded", "300"), ("bounded", "200"), ["102"], True),
    "call credit spread": ([o("SELL", "CALL", 100, "3.00"), o("BUY", "CALL", 105, "1.00")], CTX, "CALL_CREDIT_SPREAD", None,
                           ("bounded", "200"), ("bounded", "300"), ["102"], True),
    "put debit spread": ([o("BUY", "PUT", 105, "3.00"), o("SELL", "PUT", 100, "1.00")], CTX, "PUT_DEBIT_SPREAD", None,
                         ("bounded", "300"), ("bounded", "200"), ["103"], True),
    "put credit spread": ([o("SELL", "PUT", 105, "3.00"), o("BUY", "PUT", 100, "1.00")], CTX, "PUT_CREDIT_SPREAD", None,
                          ("bounded", "200"), ("bounded", "300"), ["103"], True),
    "calendar": ([o("SELL", "CALL", 100, "2.00"), o("BUY", "CALL", 100, "3.00", e=E2)], CTX, "CALENDAR_SPREAD", None,
                 ("unknown", None), ("bounded", "100"), None, True),
    "diagonal": ([o("SELL", "CALL", 105, "1.00"), o("BUY", "CALL", 100, "4.00", e=E2)], CTX, "DIAGONAL_SPREAD", None,
                 ("unknown", None), ("bounded", "300"), None, True),
    "iron condor": ([o("BUY", "PUT", 90, "0.50"), o("SELL", "PUT", 95, "1.50"), o("SELL", "CALL", 105, "1.50"),
                     o("BUY", "CALL", 110, "0.50")], CTX, "IRON_CONDOR", "EQUAL_WIDTH", ("bounded", "200"),
                    ("bounded", "300"), ["93", "107"], True),
    "broken-wing iron condor": ([o("BUY", "PUT", 85, "0.30"), o("SELL", "PUT", 95, "1.50"), o("SELL", "CALL", 105, "1.50"),
                                 o("BUY", "CALL", 110, "0.50")], CTX, "IRON_CONDOR", "BROKEN_WING", ("bounded", "220"),
                                ("bounded", "780"), ["92.8", "107.2"], True),
    "iron butterfly": ([o("BUY", "PUT", 90, "0.50"), o("SELL", "PUT", 100, "4.00"), o("SELL", "CALL", 100, "4.00"),
                        o("BUY", "CALL", 110, "0.50")], CTX, "IRON_BUTTERFLY", "EQUAL_WIDTH", ("bounded", "700"),
                       ("bounded", "300"), ["93", "107"], True),
    "long straddle": ([o("BUY", "CALL", 100, "3"), o("BUY", "PUT", 100, "3")], CTX, "LONG_STRADDLE", None,
                      ("unbounded", None), ("bounded", "600"), ["94", "106"], True),
    "short straddle": ([o("SELL", "CALL", 100, "3"), o("SELL", "PUT", 100, "3")], CTX, "SHORT_STRADDLE", None,
                       ("bounded", "600"), ("unbounded", None), ["94", "106"], False),
    "long strangle": ([o("BUY", "CALL", 110, "1"), o("BUY", "PUT", 90, "1")], CTX, "LONG_STRANGLE", None,
                      ("unbounded", None), ("bounded", "200"), ["88", "112"], True),
    "short strangle": ([o("SELL", "CALL", 110, "1"), o("SELL", "PUT", 90, "1")], CTX, "SHORT_STRANGLE", None,
                       ("bounded", "200"), ("unbounded", None), ["88", "112"], False),
}


@pytest.mark.parametrize("name", list(MATRIX))
def test_every_supported_strategy(name):
    legs, ctx, strat, variant, gain, loss, bes, defined = MATRIX[name]
    a = analyze(legs, ctx)
    assert (a.strategy, a.variant, a.exact) == (strat, variant, True)
    assert (a.max_gain.state, a.max_loss.state) == (gain[0], loss[0])
    assert a.max_gain.value == (None if gain[1] is None else D(gain[1]))
    assert a.max_loss.value == (None if loss[1] is None else D(loss[1]))
    assert a.breakevens == (None if bes is None else [D(b) for b in bes])
    assert a.defined_risk is defined
    assert not a.errors


@pytest.mark.parametrize("name", ["iron condor", "collar", "covered call", "calendar", "broken-wing iron condor"])
def test_recognition_is_leg_order_independent(name):
    legs, ctx, strat, *_ = MATRIX[name]
    results = {(analyze(list(p), ctx).strategy, analyze(list(p), ctx).max_loss.value)
               for p in itertools.permutations(legs)}
    assert len(results) == 1 and next(iter(results))[0] == strat


@pytest.mark.parametrize("legs,code", [
    ([o("BUY", "CALL", 100, "3.00"), o("SELL", "CALL", 105, "1.00", q=2)], "UNEQUAL_QUANTITIES"),        # ratio spread
    ([o("BUY", "CALL", 100, "3.00"), o("SELL", "CALL", 100, "2.00", e=E2)], "REVERSE_CALENDAR"),
    ([o("SELL", "PUT", 90, "0.50"), o("BUY", "PUT", 95, "1.50"), o("BUY", "CALL", 105, "1.50"),
      o("SELL", "CALL", 110, "0.50")], "REVERSE_IRON"),
    ([st("BUY", 150, "98"), o("SELL", "CALL", 105, "1.00")], "EXTRA_SHARES"),
    ([st("BUY", 50, "98"), o("SELL", "CALL", 105, "1.00")], "PARTIALLY_UNCOVERED"),
])
def test_ambiguous_or_irregular_structures_become_custom(legs, code):
    a = analyze(legs, CTX)
    assert a.strategy == "CUSTOM" and not a.exact and a.direction == "unknown"
    assert code in [w.code for w in a.warnings]


def test_custom_structures_still_get_exact_risk():
    butterfly = [o("BUY", "CALL", 95, "6"), o("SELL", "CALL", 100, "3", q=2), o("BUY", "CALL", 105, "1")]
    a = analyze(butterfly, CTX)
    assert a.strategy == "CUSTOM" and a.max_loss.state == "bounded" and a.max_loss.value == D("100")
    assert a.max_gain.value == D("400") and a.breakevens == [D("96"), D("104")] and a.defined_risk
    guts = [o("BUY", "CALL", 95, "6"), o("BUY", "PUT", 105, "6")]                          # put strike above call
    assert analyze(guts, CTX).strategy == "CUSTOM"
    partial = analyze([st("BUY", 50, "98"), o("SELL", "CALL", 105, "1.00")], CTX)
    assert partial.max_loss.state == "unbounded" and not partial.defined_risk               # half the call is naked


def test_unknown_risk_is_never_zero():
    a = analyze([o("BUY", "CALL", 100, None)], CTX)                                          # no fill price
    assert a.max_loss.state == "unknown" and a.max_loss.value is None and a.breakevens is None
    assert a.premium_type == "UNKNOWN" and a.buying_power.state == "unknown" and not a.defined_risk
    multi = analyze([o("BUY", "CALL", 100, "2"), o("BUY", "PUT", 100, "2", e=E2)], CTX)     # two expirations, custom
    assert multi.strategy == "CUSTOM" and multi.max_loss.state == "unknown" and multi.max_gain.state == "unknown"
    assert "MIXED_EXPIRATIONS" in [w.code for w in multi.warnings]


def test_debit_credit_distinction_and_mismatch_warning():
    a = analyze([o("BUY", "CALL", 100, "3.00"), o("SELL", "CALL", 105, "1.00")], CTX)
    assert a.premium_type == "DEBIT" and a.net_premium == D("200")
    b = analyze([o("SELL", "PUT", 105, "3.00"), o("BUY", "PUT", 100, "1.00")], CTX)
    assert b.premium_type == "CREDIT" and b.net_premium == D("-200")
    bad = analyze([o("BUY", "CALL", 100, "1.00"), o("SELL", "CALL", 105, "3.00")], CTX)     # debit spread entered as a credit
    assert bad.strategy == "CALL_DEBIT_SPREAD" and "DEBIT_CREDIT_MISMATCH" in [w.code for w in bad.warnings]


def test_fees_units_breakeven_on_a_strike_and_cash_secured_check():
    a = analyze([o("SELL", "PUT", 95, "1.50", q=3, fees="1.95"), o("BUY", "PUT", 90, "0.50", q=3, fees="1.95")], CTX)
    assert a.units == 3 and a.fees == D("3.90")
    assert a.max_gain.value == D("296.10") and a.max_loss.value == D("1203.90")
    zero_at_strike = analyze([o("SELL", "PUT", 100, "0"), o("BUY", "PUT", 95, "0")], CTX)
    assert zero_at_strike.breakevens == [D("100")]
    csp = analyze([o("SELL", "PUT", 50, "1.00")], Context(cash_secured=True, buying_power=D("1000"), today=TODAY))
    assert "CASH_SECURED_UNVERIFIED" in [w.code for w in csp.warnings]


def test_validation_errors_and_warnings():
    a = analyze([Leg("OPTION", "BUY", 0, None, None, None, D("1"), D("0"), 100)], CTX)
    codes = {e.code for e in a.errors}
    assert {"BAD_QUANTITY", "MISSING_RIGHT", "MISSING_STRIKE", "MISSING_EXPIRATION"} <= codes
    assert analyze([Leg("STOCK", "BUY", 10, "CALL", D("5"), None, D("1"), D("0"), 1)], CTX).errors[0].code == \
        "STOCK_WITH_OPTION_FIELDS"
    assert analyze([], CTX).errors[0].code == "NO_LEGS"
    expired = analyze([o("BUY", "CALL", 100, "1", e=date(2026, 1, 16))], CTX)
    assert "EXPIRED_LEG" in [w.code for w in expired.warnings]
    earn = analyze([o("BUY", "CALL", 100, "1")], Context(earnings_date=date(2026, 12, 1), today=TODAY))
    assert "EARNINGS_BEFORE_EXPIRATION" in [w.code for w in earn.warnings]
    dup = analyze([o("BUY", "CALL", 100, "1"), o("BUY", "CALL", 100, "1")], CTX)
    assert "DUPLICATE_CONTRACT" in [w.code for w in dup.warnings]
