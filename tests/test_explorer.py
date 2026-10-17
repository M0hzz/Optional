"""Historical Signal Explorer. Deterministic data with hand-checked expected timestamps and scores."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from optlab.history.bars import load_history, synthetic_bars
from optlab.history.explorer import run_search, to_csv
from optlab.history.strategy import StrategyError, auto_warmup, parse_strategy

NY = "America/New_York"


def daily(closes, start="2026-01-05", volume=None):
    """Daily bars on consecutive business days, timestamped like the loader (midnight, exchange tz)."""
    c = np.asarray(closes, float)
    idx = pd.bdate_range(start, periods=len(c)).tz_localize(NY)
    v = np.full(len(c), 1000.0) if volume is None else np.asarray(volume, float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": v}, index=idx)


def strat(conditions, **kw):
    body = {"name": "t", "symbols": ["XYZ"], "timeframe": kw.pop("timeframe", "1d"),
            "range": {"start": kw.pop("start", "2026-01-01"), "end": kw.pop("end", "2026-12-31")},
            "conditions": conditions, **kw}
    return parse_strategy({"historical_signal_search": body})


def search(s, bars):
    return run_search(s, bars_override={"XYZ": bars})


# ------------------------------------------------------------ validation
def test_validation_names_each_bad_field():
    bad = {"name": "x", "symbols": ["OK", "bad ticker"], "timeframe": "7m",
           "range": {"start": "2026-13-01", "end": "2026-01-01", "timezone": "Mars/Base"},
           "conditions": [{"id": "a", "expression": "emma(9) > 1", "tolerance": {"type": "pct", "value": 1}},
                          {"id": "a", "expression": "atm_iv > 0.3"},
                          {"expression": "rsi(14) >", "wieght": 2}],
           "matching": {"minimum_score": 120}}
    with pytest.raises(StrategyError) as e:
        parse_strategy(bad)
    msg = "\n".join(e.value.problems)
    for needle in ("symbols[1]", "timeframe: must be one of", "range.start:", "range.timezone:",
                   "unknown function emma(); did you mean ema?", "conditions[0].tolerance.type",
                   "conditions[1].id: duplicate", "atm_iv needs historical implied volatility",
                   "conditions[2].wieght: unknown setting; did you mean weight?", "Can't parse condition",
                   "matching.minimum_score"):
        assert needle in msg, needle


def test_yaml_text_and_bad_yaml_dates():
    s = parse_strategy("historical_signal_search:\n  name: y\n  symbols: [spy]\n  timeframe: 1d\n"
                       "  range: {start: 2026-01-02, end: 2026-02-01}\n  conditions: [close > 1]\n")
    assert s.symbols == ["SPY"] and s.start == "2026-01-02" and s.conditions[0].id == "c1"
    with pytest.raises(StrategyError, match="range.end"):
        parse_strategy("name: y\nsymbols: [SPY]\ntimeframe: 1d\nrange: {start: 2026-01-02, end: 2026-02-30}\n"
                       "conditions: [close > 1]\n")


def test_auto_warmup_counts_windows_not_thresholds():
    assert auto_warmup(["rsi(14) >= 55"]) == 50          # 3 x 14 = 42 -> floor of 50
    assert auto_warmup(["close > sma(200)"]) == 600


# ---------------------------------------------------------- exact matches
def test_exact_matches_known_timestamps():
    bars = daily([100, 101, 106, 104, 107, 110, 103])
    res = search(strat(["close > 105"]), bars)
    got = res.results["timestamp"].dt.strftime("%Y-%m-%d").tolist()
    assert got == ["2026-01-07", "2026-01-09", "2026-01-12"]
    assert (res.results["match_type"] == "exact").all() and (res.results["score"] == 100).all()


def test_all_conditions_must_pass_for_exact():
    bars = daily([100, 106, 107, 104, 109], volume=[1, 1, 5, 5, 1])
    res = search(strat(["close > 105", "volume > 2"]), bars)
    assert res.results["timestamp"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-07"]


def test_warmup_bars_are_not_evaluable_and_never_match():
    bars = daily(np.arange(1, 11))                           # rising: close > sma(5) whenever defined
    res = search(strat(["close > sma(5)"]), bars)
    assert res.results["timestamp"].iloc[0].strftime("%Y-%m-%d") == bars.index[4].strftime("%Y-%m-%d")
    assert res.not_evaluable == 4 and res.bars_evaluated == 6


def test_nan_bars_are_skipped_not_failed():
    bars = daily([100, 110, np.nan, 111, 112])
    res = search(strat(["close > 105"]), bars)
    assert len(res.results) == 3 and res.not_evaluable == 1


def test_range_limits_and_crossover_on_the_bar():
    bars = daily([1, 1, 1, 5, 5, 1, 1, 6])
    s = strat(["crosses_above(close, 3)"], start="2026-01-09", end="2026-01-31")   # start is inclusive
    got = search(s, bars).results["timestamp"].dt.strftime("%Y-%m-%d").tolist()
    assert got == ["2026-01-14"]                             # the Jan 8 cross is before the range


def test_no_look_ahead_prefix_invariance():
    """A bar's verdict must not change when later bars are added."""
    rng = np.random.default_rng(1)
    c = 100 + rng.normal(0, 1, 120).cumsum()
    bars = daily(c, volume=rng.lognormal(7, 0.3, 120))
    conds = ["ema(9) > ema(21)", "rsi(14) > 50", "close > vwap()", "macd_hist() > 0", "adx() > 20",
             "bb_pct() < 0.8", "stoch_k() > 30", "crosses_above(close, sma(10))", "change_pct(5) > 0",
             "rel_volume(10) > 1", "close > ago(highest(10), 1)", "williams_r() > -50", "cci() > 0",
             "atr_pct() < 3", "hv(10) < 0.5", "obv() > ago(obv(), 3)"]
    s = strat([{"id": f"k{i}", "expression": e} for i, e in enumerate(conds)])
    full = search(s, bars).evals["XYZ"]
    for cut in (60, 90, 119):
        part = search(s, bars.iloc[:cut]).evals["XYZ"]
        for a, b in zip(full, part):
            assert (a.status[:cut] == b.status).all(), a.cond.expression


def test_sorting_limit_and_csv_keeps_timezone():
    bars = daily([106, 109, 107, 108])
    s = strat(["close > 105"], output={"sort_by": "timestamp", "order": "descending", "limit": 2})
    df = search(s, bars).results
    assert df["timestamp"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-08", "2026-01-07"]
    csv = to_csv(df)
    assert "2026-01-08 00:00:00-05:00" in csv and csv.splitlines()[0].startswith("symbol,timestamp")


# ------------------------------------------------------------------ data
def test_market_hours_dst_and_timezone():
    bars, mask, rep = load_history("XYZ", "5m", "2026-03-06", "2026-03-09", source="synthetic",
                                   warmup_bars=0, now="2026-10-01")
    inr = bars[mask.to_numpy()]
    first_each_day = inr.groupby(inr.index.date).head(1).index
    assert [t.strftime("%H:%M%z") for t in first_each_day] == ["09:30-0500", "09:30-0400"]  # DST on Mar 8
    assert rep.holidays_in_range == 0 and rep.missing_bars == 0
    utc, m2, _ = load_history("XYZ", "5m", "2026-03-06", "2026-03-09", tz="UTC", source="synthetic",
                              warmup_bars=0, now="2026-10-01")
    assert str(utc.index.tz) == "UTC" and utc[m2.to_numpy()].index[0].strftime("%H:%M") == "14:30"


def test_incomplete_bars_are_dropped():
    bars, mask, rep = load_history("XYZ", "5m", "2026-03-10", "2026-03-10", source="synthetic", warmup_bars=0,
                                   now="2026-03-10 10:02:00-04:00")
    assert bars.index[-1].strftime("%H:%M") == "09:55"       # the 10:00 bar is still open at 10:02
    assert rep.incomplete_bars_dropped > 0


def test_holidays_counted_not_missing():
    _, _, rep = load_history("XYZ", "1d", "2026-11-23", "2026-11-30", source="synthetic", warmup_bars=0,
                             now="2026-12-15")
    assert rep.holidays_in_range == 1 and rep.missing_bars == 0      # Thanksgiving


def test_synthetic_bars_do_not_depend_on_range():
    a = synthetic_bars("XYZ", "15m", date(2026, 4, 6), date(2026, 4, 10))
    b = synthetic_bars("XYZ", "15m", date(2026, 3, 2), date(2026, 5, 1))
    assert len(a) == 5 * 26 and (a["close"] == b.loc[a.index, "close"]).all()


# ------------------------------------------------------ near-match scoring
def _one_bar_eval(expression, tol=None, recent=0, closes=(100,), volume=None):
    bars = daily(list(closes), volume=volume)
    c = {"id": "k", "expression": expression, "weight": 30}
    if tol:
        c["tolerance"] = {"type": tol[0], "value": tol[1]}
    if recent:
        c["recent_bars"] = recent
    s = strat([c], matching={"include_near": True, "minimum_score": 0})
    res = search(s, bars)
    return res.evals["XYZ"][0], res


def test_spec_example_rel_volume_near():
    from optlab.history.explorer import explain

    vol = [100] * 20 + [142]          # 20-bar average of the window ending here = (19*100 + 142) / 20 = 102.1
    e, _ = _one_bar_eval("volume / 100 >= 1.5", tol=("absolute", 0.20), closes=[100] * 21, volume=vol)
    assert e.status[-1] == "near" and e.observed[-1] == pytest.approx(1.42)
    assert e.shortfall[-1] == pytest.approx(0.08) and e.credit[-1] == pytest.approx(0.6)
    ex = explain([e], 20)[0]
    assert ex["points"] == pytest.approx(60.0) and "inside the 0.2 tolerance" in ex["explanation"]


def test_tolerance_boundaries():
    def st(close, tol, expr="close >= 100"):
        e, _ = _one_bar_eval(expr, tol=("absolute", tol), closes=[close])
        return e.status[-1], round(float(e.credit[-1]), 4)
    assert st(100, 1) == ("pass", 1.0)                       # >= counts equality
    assert st(99.5, 1) == ("near", 0.5)
    assert st(99.0, 1) == ("fail", 0.0)                      # shortfall == tolerance: outside (strict)
    assert st(100, 1, "close > 100") == ("near", 0.99)       # a tie fails a strict >; capped below full
    e, _ = _one_bar_eval("close >= 100", closes=[99.9])      # no tolerance: just fail
    assert e.status[-1] == "fail"


def test_percent_distance_tolerance():
    e, _ = _one_bar_eval("close > 200", tol=("percent_distance", 0.5), closes=[199.4])
    assert e.shortfall[-1] == pytest.approx(0.3) and e.status[-1] == "near"   # 100 * 0.6 / 200
    assert e.credit[-1] == pytest.approx(1 - 0.3 / 0.5)


def test_between_measures_distance_to_broken_bound():
    e, _ = _one_bar_eval("40 < close < 60", tol=("absolute", 5), closes=[62])
    assert e.kind == "between" and e.shortfall[-1] == pytest.approx(2) and e.status[-1] == "near"
    e, _ = _one_bar_eval("40 < close < 60", tol=("absolute", 5), closes=[33])
    assert e.shortfall[-1] == pytest.approx(7) and e.status[-1] == "fail"


def test_crossover_exact_recent_and_near():
    closes = [1, 1, 5, 5, 5, 5, 2.8]
    e, _ = _one_bar_eval("crosses_above(close, 3)", tol=("absolute", 0.5), recent=2, closes=closes)
    assert e.status.tolist() == ["fail", "fail", "pass", "near", "near", "fail", "near"]
    assert e.extra["recent_k"][3] == 1 and e.credit[3] == pytest.approx(1 - 1 / 3)
    assert e.credit[4] == pytest.approx(1 - 2 / 3)           # two bars ago
    assert e.status[5] == "fail"                             # crossed 3 bars ago: outside recent_bars
    assert e.shortfall[6] == pytest.approx(0.2)              # not crossed, 0.2 below: near by gap


def test_boolean_conditions_are_pass_fail_only():
    e, _ = _one_bar_eval("close > 1 and close < 0", tol=("absolute", 5), closes=[3])
    assert e.kind == "boolean" and e.status[-1] == "fail" and "pass/fail only" in e.note


def test_weighted_score_required_modes_and_pass_count():
    bars = daily([100, 99.5, 100, 99.5], volume=[10, 10, 1, 1])
    conds = [{"id": "price", "expression": "close >= 100", "weight": 75, "tolerance": {"type": "absolute", "value": 1}},
             {"id": "vol", "expression": "volume >= 5", "weight": 25, "required": False}]
    strict = search(strat(conds, matching={"include_near": True, "minimum_score": 0}), bars).results
    assert strict["match_type"].tolist() == ["exact", "near"]                     # bar 3: required fails strict
    assert strict["score"].tolist() == [100.0, 75.0]
    tol = search(strat(conds, matching={"include_near": True, "minimum_score": 0, "required_mode": "tolerant"}),
                 bars).results
    assert tol["score"].tolist() == [100.0, 62.5, 75.0, 37.5]                    # 75*0.5 + 25 = 62.5
    assert tol["match_type"].tolist() == ["exact", "near", "near", "near"]
    high = search(strat(conds, matching={"include_near": True, "minimum_score": 60, "required_mode": "tolerant",
                                         "minimum_pass_count": 1}), bars).results
    assert high["score"].tolist() == [100.0, 62.5, 75.0]
    only_near = search(strat(conds, matching={"include_near": True, "include_exact": False, "minimum_score": 0}),
                       bars).results
    assert (only_near["match_type"] == "near").all()


def test_sort_by_score():
    bars = daily([100, 99.5, 99.8, 100])
    c = [{"id": "p", "expression": "close >= 100", "tolerance": {"type": "absolute", "value": 1}}]
    s = strat(c, matching={"include_near": True, "minimum_score": 0, "required_mode": "tolerant"},
              output={"sort_by": "score", "order": "descending"})
    assert search(s, bars).results["score"].tolist() == [100.0, 100.0, 80.0, 50.0]


# ------------------------------------------------- behaviour and sequences
SERIES = [1, 2, 3, 3, 4, 5, 6, 5, 4, 3, 3, 2]


@pytest.mark.parametrize("expr,expected", [
    ("rising(close, bars=2)", "..1..11....."),           # strict: the flat 3 -> 3 breaks it
    ("increasing(close, bars=3)", "...1111....."),       # flat steps allowed, net rise required
    ("falling(close, bars=2)", "........11.."),
    ("decreasing(close, bars=3)", ".........111"),
    ("stayed_above(close, 3.5, bars=3)", "......111..."),
    ("stayed_below(close, 3.5, bars=2)", ".111......11"),    # close < 3.5 on bars 0-3 and 9-11
    ("crossed_above_within(close, 3.5, bars=3)", "....111....."),
    ("crossed_below_within(close, 4.5, bars=2)", "........11.."),
    ("touched(close, 3.1, tolerance_pct=5)", "..11.....11."),  # |3 - 3.1| = 0.1 <= 0.155
    ("between(close, 3, 4)", "..111...111."),            # both ends count
    ("rising(close, 2)", "..1..11....."),                # positional bars works too
])
def test_behaviour_functions(expr, expected):
    from optlab.live.expr import Evaluator
    from optlab.live.indicators import bar_functions

    ev = Evaluator(*bar_functions(daily(SERIES)))
    assert "".join("1" if v else "." for v in ev(expr).tolist()) == expected


def test_behaviour_functions_reject_lookahead_and_warm_up_is_false():
    from optlab.live.expr import Evaluator, ExprError
    from optlab.live.indicators import bar_functions

    ev = Evaluator(*bar_functions(daily(SERIES)))
    with pytest.raises(ExprError, match="whole number of bars"):
        ev("rising(close, bars=0)")
    assert not ev("stayed_above(close, sma(5), bars=3)").iloc[:6].any()      # sma(5) warm-up -> False


def test_behaviour_functions_are_pass_fail_only_in_scoring():
    e, _ = _one_bar_eval("rising(close, bars=2)", tol=("absolute", 1), closes=[1, 2, 2])
    assert e.kind == "boolean" and "behaviour over several bars" in e.note and e.status[-1] == "fail"


def test_between_function_is_measurable():
    e, _ = _one_bar_eval("between(close, 40, 60)", tol=("absolute", 5), closes=[62])
    assert e.kind == "between" and e.shortfall[-1] == pytest.approx(2) and e.status[-1] == "near"


def _seq(steps, closes, volume=None, **kw):
    s = strat([], sequence=steps, **kw)
    return search(s, daily(closes, volume=volume))


def test_sequence_completes_within_windows():
    steps = [{"id": "up", "expression": "crosses_above(close, 10)"},
             {"id": "high", "expression": "close > 12", "within_bars": 2},
             {"id": "vol", "expression": "volume > 5", "within_bars": 1}]
    closes = [9, 11, 11, 13, 13, 9, 11, 11, 11, 13]
    vol = [1, 1, 1, 1, 9, 1, 1, 1, 1, 9]
    res = _seq(steps, closes, vol)
    got = res.results[["timestamp", "sequence_start"]].map(lambda t: t.strftime("%m-%d")).values.tolist()
    # start bar 1 (cross), close > 12 on bar 3 (2 bars later), volume on bar 4 (1 later): completes bar 4.
    # start bar 6: close > 12 first on bar 9 (3 bars later) -> too late, no completion.
    assert got == [["01-09", "01-06"]]


def test_sequence_same_bar_counts_and_all_paths_are_considered():
    steps = [{"id": "a", "expression": "close >= 10"},
             {"id": "b", "expression": "close >= 20", "within_bars": 3},
             {"id": "c", "expression": "volume > 5", "within_bars": 1}]
    # a on bar 0; b on bars 1 and 3; c only on bar 4 -> only the later b (bar 3) leads to completion
    res = _seq(steps, [10, 20, 0, 20, 0], [1, 1, 1, 1, 9])
    assert res.results["timestamp"].dt.strftime("%m-%d").tolist() == ["01-09"]
    same = _seq([{"id": "a", "expression": "close >= 10"}, {"id": "b", "expression": "volume > 5", "within_bars": 0}],
                [10, 10], [1, 9])
    assert same.results["timestamp"].dt.strftime("%m-%d").tolist() == ["01-06"]   # a and b on the same bar


def test_sequence_has_no_look_ahead():
    steps = [{"id": "a", "expression": "crosses_above(close, 10)"},
             {"id": "b", "expression": "close > 12", "within_bars": 3}]
    closes = [9, 11, 11, 13, 9, 11, 13, 13]
    full = _seq(steps, closes).evals["XYZ"][0]
    for cut in range(2, len(closes)):
        part = _seq(steps, closes[:cut]).evals["XYZ"][0]
        assert (part.passed == full.passed[:cut]).all()


def test_sequence_validation():
    with pytest.raises(StrategyError, match=r"sequence\[1\].within_bars: required"):
        strat([], sequence=[{"expression": "close > 1"}, {"expression": "close > 2"}])
    with pytest.raises(StrategyError, match=r"sequence\[0\].within_bars"):
        strat([], sequence=[{"expression": "close > 1", "within_bars": 2}])
