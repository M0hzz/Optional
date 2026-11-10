from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from optlab.live.expr import Evaluator, ExprError, truthy
from optlab.live.feed import SyntheticFeed, finish_chain, standard_only
from optlab.live.indicators import bar_functions
from optlab.live.monitor import AlertTracker, Schedule
from optlab.live.scanner import (filter_contracts, load_scan_config, max_loss_of, run_pass, run_positions,
                                  size_contracts, validate)

ROOT = Path(__file__).resolve().parents[1]
END = date(2024, 12, 31)
NY = ZoneInfo("America/New_York")


def bars_from(close, start="2024-01-02", freq="B"):
    close = np.asarray(close, float)
    idx = pd.date_range(start, periods=len(close), freq=freq)
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close,
                         "volume": np.full(len(close), 1000.0)}, index=idx)


def ev_for(bars, timeframe="1d"):
    return Evaluator(*bar_functions(bars, timeframe))


# --------------------------------------------------------------- expressions
def test_indicators_match_definitions():
    rng = np.random.default_rng(0)
    b = bars_from(100 + rng.normal(0, 1, 300).cumsum())
    ev = ev_for(b)
    assert ev("sma(20)").iloc[-1] == pytest.approx(b["close"].tail(20).mean())
    r = ev("rsi(14)").dropna()
    assert r.between(0, 100).all()
    assert ev_for(bars_from(np.arange(1, 60)))("rsi(14)").iloc[-1] == pytest.approx(100)  # only up days
    assert ev("hv(20)").iloc[-1] > 0 and ev("atr(14)").iloc[-1] > 0
    assert ev("bb_lower() < bb_mid() < bb_upper()").iloc[-1]


def test_crossovers_and_boolean_logic():
    b = bars_from([1, 1, 1, 1, 5])
    ev = ev_for(b)
    assert truthy(ev("crosses_above(close, 3)"))
    assert not truthy(ev("crosses_below(close, 3)"))
    assert truthy(ev("close > 3 and not close > 10"))
    assert truthy(ev("close < 0 or 2 < close < 6"))
    assert ev("ago(close, 1)").iloc[-1] == 1
    arr = Evaluator({"x": np.array([1.0, 5.0, np.nan])})("x > 2 or x < 0")
    assert arr.tolist() == [False, True, False]


def test_vwap_resets_each_session():
    idx = pd.to_datetime(["2024-01-02 09:30", "2024-01-02 09:31", "2024-01-03 09:30"])
    b = pd.DataFrame({"open": [10, 20, 30], "high": [10, 20, 30], "low": [10, 20, 30], "close": [10, 20, 30],
                      "volume": [1, 1, 1]}, index=idx, dtype=float)
    v = ev_for(b, "1m")("vwap()")
    assert v.tolist() == pytest.approx([10, 15, 30])


@pytest.mark.parametrize("bad", ["__import__('os')", "close.real", "[1, 2]", "lambda: 1", "exec('x')", "close["])
def test_unsafe_or_invalid_expressions_are_rejected(bad):
    with pytest.raises(ExprError):
        ev_for(bars_from(range(1, 30)))(bad)


def test_unknown_name_suggests_fix():
    with pytest.raises(ExprError, match="did you mean close"):
        ev_for(bars_from(range(1, 30)))("clos > 1")


# ---------------------------------------------------------------- chains
def test_standard_only_drops_adjusted_contracts():
    df = pd.DataFrame({"contract": ["AAPL261120P00200000", "AAPL1261120P00200000", "SPXW261120P05000000"]})
    assert standard_only(df, "AAPL")["contract"].tolist() == ["AAPL261120P00200000"]
    assert len(standard_only(df.iloc[[2]], "SPX")) == 1


def test_finish_chain_derived_columns():
    raw = pd.DataFrame({"contract": ["X1", "X2"], "expiration": ["2025-01-31", "2025-01-31"], "strike": [90.0, 110.0],
                        "type": ["put", "call"], "bid": [1.0, 2.0], "ask": [1.2, 2.2], "last": [1.1, 2.1],
                        "volume": [5, 6], "open_interest": [10, 20], "iv": [0.3, 0.0], "underlying_price": [100.0, 100.0]})
    out = finish_chain(raw, "X", today=date(2025, 1, 1))
    put, call = out[out["type"] == "put"].iloc[0], out[out["type"] == "call"].iloc[0]
    assert put["otm_pct"] == pytest.approx(10) and call["otm_pct"] == pytest.approx(10)
    assert put["yield_pct"] == pytest.approx(100 * 1.1 / 90)
    assert put["delta"] < 0 < call["delta"] or np.isnan(call["delta"])  # greeks computed where IV known
    assert np.isnan(call["iv"])                                           # CBOE's 0 means "unknown"


# ---------------------------------------------------------------- scanning
@pytest.fixture(scope="module")
def feed():
    return SyntheticFeed(end=END)


def scan_cfg(**over):
    cfg = {"watchlists": {"w": ["SPY", "QQQ", "AAPL", "MSFT"]}, "positions": [], "scans": [{
        "name": "puts", "watchlist": "w", "timeframe": "1d", "stock": ["close > 0"],
        "options": {"type": "put", "dte": [20, 60], "delta": [0.15, 0.35], "min_open_interest": 50,
                    "conditions": ["bid >= 0.5", "iv > 0"], "top": 4}}]}
    cfg.update(over)
    return cfg


def test_option_scan_respects_every_filter(feed):
    res = run_pass(scan_cfg(), feed, today=END)
    assert not res.errors and not res.hits.empty
    h = res.hits
    assert (h["type"] == "put").all() and h["dte"].between(20, 60).all()
    assert h["delta"].abs().between(0.15, 0.35).all() and (h["open_interest"] >= 50).all()
    assert (h["bid"] >= 0.5).all() and (h.groupby("symbol").size() <= 4).all()
    assert h.groupby("symbol")["annualized_yield"].apply(lambda s: s.is_monotonic_decreasing).all()


def _sized(feed, trade_size, **opt):
    cfg = scan_cfg(trade_size=trade_size)
    cfg["scans"][0]["options"] = {"type": "put", "dte": [20, 60], "delta": [0.15, 0.35], "top": 50, **opt}
    assert not validate({**cfg, "watchlists": cfg["watchlists"]})
    res = run_pass(cfg, feed, today=END)
    assert not res.errors
    return res.hits


@pytest.mark.parametrize("size", [80, 250, 1000])
def test_trade_size_keeps_only_what_one_trade_can_afford(feed, size):
    # far out-of-the-money puts too: at $80 nothing nearer the money is that cheap on these underlyings
    buy = _sized(feed, size, side="buy", sort="capital asc", delta=[0.01, 0.35])
    assert not buy.empty and (buy["capital"] <= size).all() and (buy["capital"] == buy["ask"] * 100).all()
    assert (buy["contracts"] == (size // buy["capital"])).all() and (buy["contracts"] >= 1).all()
    assert buy["structure"].str.startswith("Buy ").all() and (buy["net"] > 0).all()

    # a cash-secured put on these underlyings ties up thousands: nothing fits a small trade
    assert _sized(feed, size, side="sell").empty

    spread = _sized(feed, size, side="sell", max_width=5)
    assert (spread["capital"] <= size).all() and (spread["net"] < 0).all()
    assert spread.empty == (size < 1000)        # these chains have $5 strikes: a $5 spread risks over $400
    if not spread.empty:
        width = spread["strike"] - spread["other_strike"]
        assert ((width > 0) & (width <= 5)).all()                       # the long put is below the short one
        assert np.allclose(spread["max_loss"], (width + spread["net"]) * 100)
        assert np.allclose(spread["return_on_risk"], 100 * -spread["net"] * 100 / spread["max_loss"])

    debit = _sized(feed, size, side="buy", max_width=5)
    assert (debit["capital"] <= size).all() and (debit.empty or np.allclose(debit["capital"], debit["net"] * 100))


def test_an_80_dollar_trade_gets_a_one_dollar_wide_spread():
    strikes = [95.0, 96.0, 97.0, 98.0, 99.0]
    bids = [0.20, 0.30, 0.45, 0.65, 0.90]
    raw = pd.DataFrame({"contract": [f"X{int(k)}" for k in strikes], "expiration": "2025-01-31", "strike": strikes,
                        "type": "put", "bid": bids, "ask": [b + 0.05 for b in bids], "last": bids, "volume": 50,
                        "open_interest": 500, "iv": 0.3, "underlying_price": 100.0})
    ch = finish_chain(raw, "X", today=date(2025, 1, 1))
    opt = {"type": "put", "side": "sell", "max_width": 3, "top": 99, "sort": "strike"}
    got = size_contracts(ch, ch, opt, 80).set_index("strike")
    # 99 put: $1 wide risks (1 - 0.20) x 100 = 80, which fits; $2 wide would risk 160
    assert got.loc[99.0, "other_strike"] == 98.0 and got.loc[99.0, "capital"] == pytest.approx(80)
    assert got.loc[99.0, "structure"] == "Sell 99/98 put spread" and got.loc[99.0, "contracts"] == 1
    assert got.loc[99.0, "return_on_risk"] == pytest.approx(25)
    assert 98.0 not in got.index and 95.0 not in got.index       # 98/97 risks $85; 95 has nothing below it
    wide = size_contracts(ch, ch, opt, 1000).set_index("strike")
    assert wide.loc[99.0, "other_strike"] == 96.0 and wide.loc[99.0, "contracts"] == 4   # widest that fits: risks $245
    longs = size_contracts(ch, ch, {"side": "buy"}, 80)
    assert sorted(longs["strike"]) == [95.0, 96.0, 97.0, 98.0] and (longs["capital"] <= 80).all()


def test_trade_size_is_optional_and_checked(feed):
    plain = run_pass(scan_cfg(), feed, today=END).hits               # no trade_size, no side: as before
    assert not plain.empty and plain["capital"].isna().all() and plain["structure"].isna().all()
    big = _sized(feed, 10_000_000, side="sell")
    assert not big.empty and np.allclose(big["capital"], (big["strike"] - big["bid"]) * 100)
    ch = feed.chain("SPY")
    opt = {"type": "put", "dte": [20, 60], "side": "sell", "max_width": 5, "conditions": ["capital <= 120"], "top": 99}
    names, funcs = bar_functions(feed.bars(["SPY"], "1d")["SPY"], "1d")
    picked = filter_contracts(ch, opt, Evaluator(names, funcs), {})
    assert (picked["capital"] <= 120).all()                              # usable in conditions without trade_size
    msgs = " | ".join(validate({**scan_cfg(trade_size=-5)}))
    assert "trade_size" in msgs and "options.side is needed" in msgs
    msgs = " | ".join(validate(scan_cfg(scans=[{"name": "s", "symbols": ["SPY"], "options": {"side": "hold", "max_width": 0}}])))
    assert "must be buy or sell" in msgs and "max_width" in msgs


def test_default_rules_fit_small_bought_trades_and_sold_premium(feed):
    ch = feed.chain("SPY")
    row = ch[(ch["type"] == "call") & ch["dte"].between(25, 60) & (ch["mid"] > 0.5)].iloc[0]
    leg = {"type": "call", "strike": float(row["strike"]), "expiration": str(row["expiration"])}

    def status(qty, mult, legs=None):
        pos = {"name": "p", "symbol": "SPY", "legs": legs or [{**leg, "qty": qty, "price": float(row["mid"]) * mult}]}
        return run_positions({"positions": [pos]}, feed, today=END)[0].iloc[0]

    lost_half = status(1, 2.2)                         # bought at 2.2x today's price: down ~55% of the cost
    assert lost_half["status"] == "EXIT" and "loss_pct_of_max >= 50" in lost_half["reasons"]
    assert lost_half["pnl_pct"] == pytest.approx(-54.5, abs=0.5)
    held = status(1, 1.1)                              # a small loss on a bought option with weeks left: no exit
    assert held["status"] != "EXIT" and "dte" not in held["reasons"]
    naked = status(-1, 0.3)                            # sold for 0.3x today's price: loss is over 2x the credit
    assert naked["status"] == "EXIT" and "pnl_pct <= -200" in naked["reasons"]
    assert "loss_pct_of_max" not in naked["reasons"]   # a naked call has no maximum loss

    assert max_loss_of([{**leg, "qty": 1, "price": 0.8}]) == pytest.approx(80)          # an $80 option
    spread = [{"type": "put", "strike": 100, "expiration": "2026-01-16", "qty": -1, "price": 0.45},
              {"type": "put", "strike": 99, "expiration": "2026-01-16", "qty": 1, "price": 0.25}]
    assert max_loss_of(spread) == pytest.approx(80)                                      # $1 wide, 0.20 credit
    assert np.isnan(max_loss_of([{**leg, "qty": -1, "price": 3}]))
    assert np.isnan(max_loss_of(spread[:1] + [{**spread[1], "expiration": "2026-02-20"}]))


def test_stock_only_scan_reports_study_values(feed):
    cfg = scan_cfg(scans=[{"name": "trend", "symbols": ["SPY", "AAPL"], "stock": ["rsi(14) >= 0", "atm_iv > 0"]}])
    res = run_pass(cfg, feed, today=END)
    assert set(res.hits["symbol"]) == {"SPY", "AAPL"}
    assert res.hits["details"].str.contains("rsi\\(14\\)=").all() and res.hits["details"].str.contains("atm_iv=").all()
    assert res.hits["contract"].isna().all()


def _short_put(feed, entry_mult):
    ch = feed.chain("SPY")
    row = ch[(ch["type"] == "put") & ch["dte"].between(25, 60)].iloc[len(ch) // 10]
    return row, {"name": "p", "symbol": "SPY", "legs": [{"type": "put", "strike": float(row["strike"]),
                 "expiration": str(row["expiration"]), "qty": -1, "price": float(row["mid"] * entry_mult)}]}


def test_position_status_follows_conditions(feed):
    _, pos = _short_put(feed, 2.5)                     # sold at 2.5x today's price: ~60% profit
    out, _ = run_positions({"positions": [pos], "position_defaults": {"exit_when": ["pnl_pct >= 50"],
                                                                         "watch_when": []}}, feed, today=END)
    r = out.iloc[0]
    assert r["status"] == "EXIT" and r["pnl_pct"] == pytest.approx(60, abs=0.5) and "pnl_pct >= 50" in r["reasons"]
    _, pos = _short_put(feed, 1.0)
    out, _ = run_positions({"positions": [{**pos, "exit_when": ["pnl_pct >= 50"], "watch_when": ["rsi(14) >= 0"]}]},
                           feed, today=END)
    assert out.iloc[0]["status"] == "WATCH"


def test_position_with_unknown_contract_gives_hint(feed):
    row, pos = _short_put(feed, 1.0)
    pos["legs"][0]["expiration"] = str(row["expiration"] - timedelta(days=1))
    out, _ = run_positions({"positions": [pos]}, feed, today=END)
    assert out.iloc[0]["status"] == "ERROR" and "nearest expirations" in out.iloc[0]["reasons"]


def test_config_validation_and_shipped_file():
    cfg = load_scan_config(ROOT / "scans.yaml")              # the file users edit must stay valid
    assert cfg["scans"] and len(cfg["watchlists"]["liquid"]) > 50
    bad = {"watchlists": {}, "positions": [{"symbol": "X", "legs": [{"type": "put", "qty": -1, "price": 1}]}],
           "scans": [{"name": "s", "watchlist": "nope", "stock": ["rsi(14) <"]}]}
    msgs = " | ".join(validate(bad))
    assert "unknown watchlist" in msgs and "Can't parse" in msgs and "strike and expiration" in msgs


# ---------------------------------------------------------- schedule/alerts
def test_schedule_window_and_next_start():
    s = Schedule.from_config({"start": "09:30", "end": "16:00"})
    fri_open, fri_late = datetime(2026, 10, 9, 10, 0, tzinfo=NY), datetime(2026, 10, 9, 16, 0, tzinfo=NY)
    assert s.is_open(fri_open) and not s.is_open(fri_late)
    assert s.next_start(fri_late) == datetime(2026, 10, 12, 9, 30, tzinfo=NY)          # skips the weekend
    assert s.next_tick(datetime(2026, 10, 9, 10, 0, 15, tzinfo=NY)).second == 0
    assert Schedule.from_config({"start": 570}).start.hour == 9                      # unquoted 09:30 in YAML


def test_alerts_fire_on_changes_only(tmp_path):
    t = AlertTracker(tmp_path / "s.json", repeat_exit_minutes=15, rearm_minutes=30, confirm_passes=1)
    hits = pd.DataFrame([{"scan": "a", "symbol": "SPY", "price": 1.0, "contract": None, "details": ""}])
    pos = pd.DataFrame([{"position": "p", "status": "HOLD", "reasons": "", "pnl": 1.0, "pnl_pct": 1.0}])
    now = datetime(2026, 10, 9, 10, 0, tzinfo=NY)
    assert len(t.alerts(hits, pos, now)) == 1                       # new hit; HOLD on first sight is quiet
    assert t.alerts(hits, pos, now + timedelta(minutes=1)) == []    # nothing changed
    pos.loc[0, "status"] = "EXIT"
    assert "HOLD → EXIT" in t.alerts(hits, pos, now + timedelta(minutes=2))[0][0]
    assert t.alerts(hits, pos, now + timedelta(minutes=7)) == []    # within the repeat window
    assert "still" in t.alerts(hits, pos, now + timedelta(minutes=18))[0][0]


def test_alerts_ignore_flapping(tmp_path):
    t = AlertTracker(tmp_path / "s.json", rearm_minutes=30, confirm_passes=3)
    hit = pd.DataFrame([{"scan": "a", "symbol": "SPY", "price": 1.0, "contract": None, "details": ""}])
    none = hit.iloc[0:0]
    pos = lambda st: pd.DataFrame([{"position": "p", "status": st, "reasons": "", "pnl": 0.0, "pnl_pct": 0.0}])  # noqa: E731
    t0 = datetime(2026, 10, 9, 10, 0, tzinfo=NY)
    at = lambda m: t0 + timedelta(minutes=m)  # noqa: E731
    assert len(t.alerts(hit, pos("HOLD"), at(0))) == 1
    t.alerts(none, pos("WATCH"), at(1))                           # drops out, status wobbles for one pass
    assert t.alerts(hit, pos("HOLD"), at(2)) == []                # back within 30 min, status back: silence
    for m in (3, 4):
        assert t.alerts(none, pos("WATCH"), at(m)) == []          # WATCH not yet confirmed
    assert "HOLD → WATCH" in t.alerts(none, pos("WATCH"), at(5))[0][0]   # third pass in a row
    assert len(t.alerts(hit, pos("WATCH"), at(40))) == 1          # out of the scan > 30 min: re-armed


def test_replay_day_tells_its_story():
    from optlab.live.replay import ReplayFeed, demo_positions

    feed = ReplayFeed(date(2026, 10, 12), ["SPY", "IWM", "AAPL", "NVDA"])
    spy = [feed.set_minute(m) or feed.spot("SPY") for m in (0, 60, 285)]
    assert spy[1] > spy[0] > spy[2] and spy[2] / spy[0] < 0.985     # rally, then the selloff
    positions = demo_positions(feed)
    feed.set_minute(0)
    first, _ = run_positions({"positions": positions}, feed, today=feed.day)
    assert (first["status"] != "ERROR").all()
    assert feed.bars(["SPY"], "5m")["SPY"].index[-1].date() == feed.day
