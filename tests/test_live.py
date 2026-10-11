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
from optlab.live.scanner import load_scan_config, run_pass, run_positions, validate

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
    t = AlertTracker(tmp_path / "s.json", repeat_exit_minutes=15)
    hits = pd.DataFrame([{"scan": "a", "symbol": "SPY", "price": 1.0, "contract": None, "details": ""}])
    pos = pd.DataFrame([{"position": "p", "status": "HOLD", "reasons": "", "pnl": 1.0, "pnl_pct": 1.0}])
    now = datetime(2026, 10, 9, 10, 0, tzinfo=NY)
    assert len(t.alerts(hits, pos, now)) == 1                       # new hit; HOLD on first sight is quiet
    assert t.alerts(hits, pos, now) == []                           # nothing changed
    pos.loc[0, "status"] = "EXIT"
    assert "HOLD → EXIT" in t.alerts(hits, pos, now)[0][0]
    assert t.alerts(hits, pos, now + timedelta(minutes=5)) == []    # within repeat window
    assert len(t.alerts(hits, pos, now + timedelta(minutes=16))) == 1
