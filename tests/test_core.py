import math
from datetime import date

import numpy as np
import pandas as pd
import polars as pl
import pytest

from optlab.backtest import BTParams, simulate, stats
from optlab.data import Store
from optlab.data.chain import atm_iv
from optlab.data.sources import SyntheticSource
from optlab.filings import insider_signal, synthetic_insider_trades
from optlab.pricing import (Leg, OptionSpec, bs_delta, bs_price, implied_vol, payoff_at_expiry, price,
                            strike_for_delta)
from optlab.pricing.engine import fill_entry_prices
from optlab.research import build_features, rank_candidates
from optlab.risk import RiskLimits, allocate, check_limits, size_position
from optlab.strategies import STRATEGIES, max_loss


# ------------------------------------------------------------------ pricing
def test_quantlib_matches_closed_form():
    ql = price(OptionSpec(100, 105, 45, 0.3, "call", 0.04, 0.0, "european"))
    bs = float(bs_price(100, 105, 45 / 365, 0.04, 0.0, 0.3, True))
    assert ql["price"] == pytest.approx(bs, rel=1e-3)
    assert ql["delta"] == pytest.approx(float(bs_delta(100, 105, 45 / 365, 0.04, 0, 0.3, True)), abs=1e-3)


def test_put_call_parity():
    c = price(OptionSpec(100, 100, 60, 0.25, "call", 0.04, 0, "european"))["price"]
    p = price(OptionSpec(100, 100, 60, 0.25, "put", 0.04, 0, "european"))["price"]
    assert c - p == pytest.approx(100 - 100 * math.exp(-0.04 * 60 / 365), abs=1e-3)


def test_american_put_worth_at_least_european():
    am = price(OptionSpec(100, 120, 180, 0.25, "put", 0.05, 0, "american"))
    eu = price(OptionSpec(100, 120, 180, 0.25, "put", 0.05, 0, "european"))
    assert am["price"] >= eu["price"] - 1e-6
    assert am["theta"] < 0 and am["vega"] > 0


@pytest.mark.parametrize("style", ["european", "american"])
def test_implied_vol_roundtrip(style):
    spec = OptionSpec(100, 95, 30, 0.37, "put", 0.04, 0, style)
    assert implied_vol(price(spec)["price"], spec) == pytest.approx(0.37, abs=1e-3)


def test_strike_for_delta_inverts_delta():
    k = float(strike_for_delta(100, 0.1, 0.04, 0, 0.25, 0.25, False))
    assert float(bs_delta(100, k, 0.1, 0.04, 0, 0.25, False)) == pytest.approx(-0.25, abs=1e-6)


# --------------------------------------------------------------- strategies
def test_put_spread_max_loss_is_width_minus_credit():
    legs = fill_entry_prices(STRATEGIES["Put credit spread"](100, 0.25), 100, 0.04, style="european")
    width = legs[0].strike - legs[1].strike
    credit = legs[0].entry_price - legs[1].entry_price
    assert max_loss(legs) == pytest.approx((width - credit) * 100, rel=1e-6)


def test_covered_call_caps_upside():
    legs = fill_entry_prices(STRATEGIES["Covered call"](100, 0.25), 100, 0.04, style="european")
    pnl = payoff_at_expiry(legs, np.array([150.0, 200.0]))
    assert pnl[0] == pytest.approx(pnl[1])


# --------------------------------------------------------------- data store
def test_store_upsert_is_idempotent(tmp_path):
    src = SyntheticSource(end=date(2024, 6, 28))
    with Store(tmp_path / "t.duckdb") as s:
        df = src.prices("SPY", "2023-01-01")
        s.upsert("prices", df)
        s.upsert("prices", df)
        assert s.prices("SPY").height == df.height
        s.upsert("option_chains", src.option_chain("SPY", "2023-01-01"))
        iv = atm_iv(s.latest_chain("SPY"))
        assert 0.05 < iv < 1.5


# ----------------------------------------------------------------- backtest
@pytest.fixture(scope="module")
def spy_close():
    df = SyntheticSource(end=date(2024, 12, 31)).prices("SPY", "2019-01-01").to_pandas()
    return pd.Series(df["close"].values, index=pd.to_datetime(df["date"]))


@pytest.mark.parametrize("strategy", ["put_write", "covered_call", "put_spread", "iron_condor"])
def test_simulate_runs(spy_close, strategy):
    rets, log = simulate(spy_close, BTParams(strategy=strategy))
    assert len(log) > 10
    assert np.isfinite(rets).all()
    s = stats(rets)
    assert -100 < s["max_drawdown_%"].iloc[0] <= 0


def test_costs_reduce_returns(spy_close):
    cheap = simulate(spy_close, BTParams(slippage_pct=0, commission=0))[0].sum()
    dear = simulate(spy_close, BTParams(slippage_pct=10, commission=2))[0].sum()
    assert dear < cheap


# --------------------------------------------------------------------- risk
def test_size_position_respects_both_limits():
    lim = RiskLimits(equity=100_000, max_risk_per_trade_pct=2, max_underlying_pct=25)
    assert size_position(400, 500, lim)["contracts"] == 5           # 2,000 / 400
    out = size_position(100, 10_000, lim)                           # concentration binds
    assert out["contracts"] == 2 and out["limited_by"] == "underlying concentration"


def test_check_limits_flags_breach():
    pos = pd.DataFrame([{"symbol": "SPY", "dollar_delta": 80_000, "vega": 50, "capital": 10_000}])
    chk = check_limits(pos, RiskLimits(equity=100_000))
    assert not chk.ok and any("delta" in b.lower() for b in chk.breaches)


def test_allocate_weights_sum_to_one(spy_close):
    R = pd.DataFrame({s: simulate(spy_close, BTParams(strategy=s))[0]
                      for s in ["put_write", "put_spread", "iron_condor"]})
    w = allocate(R, "min_cvar", max_weight=0.6)
    assert w.sum() == pytest.approx(1.0, abs=1e-4) and (w <= 0.6 + 1e-6).all()


# ---------------------------------------------------------- research/filings
def test_ranking_and_insiders():
    src = SyntheticSource(end=date(2024, 12, 31))
    prices = pl.concat([src.prices(s, "2022-01-01") for s in ["SPY", "QQQ", "AAPL"]])
    ranked = rank_candidates(build_features(prices.select(["symbol", "date", "close"])))
    assert ranked.height == 3 and ranked["score"].is_finite().all()
    sig = insider_signal(synthetic_insider_trades(["AAPL", "MSFT"]))
    assert sig["score"].is_between(-1, 1).all()


# ------------------------------------------------- fundamentals/macro/filings
def test_fundamentals_and_macro_roundtrip(tmp_path):
    from optlab.data import risk_free_rate

    src = SyntheticSource(end=date(2024, 6, 28), rate=0.045)
    cfg = {"market": {"risk_free_rate": 0.04, "rate_series": "treasury_3m"}}
    with Store(tmp_path / "t.duckdb") as s:
        assert risk_free_rate(s, cfg) == 0.04                      # empty macro table -> config fallback
        assert src.fundamentals("SPY").is_empty()                  # ETFs have no fundamentals
        s.upsert("fundamentals", src.fundamentals("AAPL"))
        s.upsert("fundamentals", src.fundamentals("AAPL"))
        f = s.latest_fundamentals()
        assert f.height == 1 and f["next_earnings"][0] > date(2024, 6, 28)
        macro = src.macro("2023-01-01")
        s.upsert("macro", macro)
        s.upsert("macro", macro)
        assert s.macro().height == macro.height
        assert set(macro["series"]) == {"treasury_1m", "treasury_3m", "treasury_1y", "treasury_2y", "treasury_10y", "vix"}
        rate = risk_free_rate(s, cfg)
        assert rate == s.latest_macro("treasury_3m") and 0 < rate < 0.15


def test_financial_changes_and_flags():
    from optlab.filings import financial_changes, financial_flags

    rows = [("XYZ", "revenue", date(2023, 6, 30), 100.0, "Q2"), ("XYZ", "revenue", date(2024, 3, 31), 95.0, "Q1"),
            ("XYZ", "revenue", date(2024, 6, 30), 80.0, "Q2"),
            ("XYZ", "net_income", date(2023, 6, 30), 10.0, "Q2"), ("XYZ", "net_income", date(2024, 6, 30), -2.0, "Q2"),
            ("XYZ", "long_term_debt", date(2024, 6, 30), 50.0, "Q2")]          # no year-ago value
    fin = pl.DataFrame(rows, schema=["symbol", "metric", "period_end", "value", "fiscal_period"], orient="row")
    ch = financial_changes(fin)
    rev = ch.filter(pl.col("metric") == "revenue").row(0, named=True)
    assert rev["year_ago"] == 100.0 and rev["yoy_pct"] == pytest.approx(-20.0)   # vs a year ago, not last quarter
    assert ch.filter(pl.col("metric") == "long_term_debt")["yoy_pct"][0] is None
    flags = financial_flags(ch).row(0, named=True)["financial_flags"]
    assert "revenue -20% YoY" in flags and "net income turned negative" in flags


def test_filing_flags_only_recent_red_flags():
    from optlab.filings import filing_flags, synthetic_filings, synthetic_financials

    fil = pl.DataFrame([("XYZ", date(2024, 6, 1), "8-K", "a1", "4.02,9.01", "", ""),
                        ("XYZ", date(2024, 6, 2), "8-K", "a2", "2.02,9.01", "", ""),     # earnings: not a flag
                        ("XYZ", date(2023, 1, 1), "8-K", "a3", "4.01", "", "")],         # too old
                       schema=["symbol", "filing_date", "form", "accession_no", "items", "description", "url"],
                       orient="row")
    out = filing_flags(fil, as_of=date(2024, 6, 30)).row(0, named=True)
    assert out["n_filing_flags"] == 1 and "Prior financials unreliable" in out["filing_flags"]
    syms = ["AAPL", "SPY"]
    assert set(synthetic_filings(syms)["symbol"]) == {"AAPL"}
    assert synthetic_financials(syms)["metric"].n_unique() == 8
