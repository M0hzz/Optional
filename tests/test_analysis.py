"""Phase 6: historical analysis. Translating live conditions, drafts from dashboard objects, forward statistics,
distributions and breakdowns, the bias checks (no look-ahead, completed bars, incomplete horizons, overlap,
sample size, survivorship, snooping), stored runs, and the drill-down window. Synthetic or hand-made bars only."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from conftest import opt

from optlab.analysis import drafts, stats
from optlab.analysis import service as an
from optlab.analysis.translate import split_clauses, translate
from optlab.history.explorer import evaluate_symbol, run_search
from optlab.portfolio.db import Database
from optlab.portfolio.service import DomainError

API = "/api/v1"
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def form(**kw):
    base = dict(name="test", symbols=["XYZ"], timeframe="1d", start="2019-01-01", end="2026-10-30",
                conditions=[{"id": "c1", "expression": "close > sma(50)", "tolerance_type": "percent_distance", "tolerance": 2},
                            {"id": "c2", "expression": "rsi(14) < 45", "tolerance_type": "percent_distance", "tolerance": 2}],
                horizons=["1", "5", "10", "20"], dedupe_bars=20, direction="bullish", focus_horizon="5")
    base.update(kw)
    return an.AnalysisIn(**base)


def daily(closes, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=len(closes), tz="America/New_York")
    c = np.asarray(closes, float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)


# ------------------------------------------------------------------ translation
def test_live_conditions_are_split_and_live_only_clauses_left_out_with_reasons():
    assert split_clauses("price > sma(50) and rsi(14) < 40 and (volume > 1 or close > 2)") == \
        ["price > sma(50)", "rsi(14) < 40", "volume > 1 or close > 2"]
    inc, exc = translate([("rule", "price > sma(50) and pnl_pct >= 50"), ("entry", "iv < 0.3 and rsi(14) < 30"),
                          ("x", "news_sentiment_1d > 0 and close > 10"), ("y", "days_to_earnings > 10"), ("z", "close >")])
    assert [c["expression"] for c in inc] == ["price > sma(50)", "rsi(14) < 30", "close > 10"]
    why = {e["expression"]: e["reason"] for e in exc}
    assert "position values" in why["pnl_pct >= 50"] and "option quote history" in why["iv < 0.3"]
    assert "news history" in why["news_sentiment_1d > 0"] and "earnings dates" in why["days_to_earnings > 10"]
    assert "parsed" in why["close >"]


# ------------------------------------------------------------------ drafts
def test_drafts_from_plans_positions_rules_contracts_and_events(client, account, db_url):
    p = client.post(f"{API}/plans", json={"account_id": account["id"], "symbol": "XYZ", "thesis": "t", "direction": "bullish",
                                          "intended_strategy": "PUT_CREDIT_SPREAD", "trigger": "Bounce",
                                          "trigger_conditions": "price > sma(50) and atm_iv > 0.2", "confirmation_conditions": "rsi(14) < 40",
                                          "invalidation": "below 92", "invalidation_conditions": "price < 92",
                                          "planned_max_loss": "450", "planned_contracts": 1, "expiration_from": "2026-12-01",
                                          "expiration_to": "2027-01-31"}).json()
    d = client.get(f"{API}/analysis/draft", params={"subject_type": "plan", "subject_id": p["id"]}).json()
    assert [c["expression"] for c in d["conditions"]] == ["price > sma(50)", "rsi(14) < 40"]
    assert {e["origin"] for e in d["excluded"]} == {"trigger", "invalidation"} and d["direction"] == "bullish"
    assert d["conditions"][0]["tolerance_type"] == "percent_distance" and d["symbols"] == ["XYZ"] and d["timeframe"] == "1d"
    pos = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "ACME",
                                                "legs": [opt("SELL", "PUT", 95, "1.10"), opt("BUY", "PUT", 90, "0.15")]}).json()
    dp = client.get(f"{API}/analysis/draft", params={"subject_type": "position", "subject_id": pos["id"]}).json()
    assert dp["symbols"] == ["ACME"] and "no plan" in dp["notes"][0] and dp["entry"]["opened_at"]
    rule = client.post(f"{API}/alert-rules", json={"name": "dip", "target_type": "position", "position_id": pos["id"],
                                                   "condition": "rsi(14) < 30 and pnl_pct < -20"}).json()
    dr = client.get(f"{API}/analysis/draft", params={"subject_type": "alert_rule", "subject_id": rule["id"]}).json()
    assert dr["symbols"] == ["ACME"] and [c["expression"] for c in dr["conditions"]] == ["rsi(14) < 30"]
    c = client.post(f"{API}/watched-contracts", json={"account_id": account["id"], "underlying": "XYZ", "option_right": "PUT",
                                                      "strike": "95", "expiration": "2026-12-18",
                                                      "alert_conditions": "price < sma(20)\niv > 0.4"}).json()
    dc = client.get(f"{API}/analysis/draft", params={"subject_type": "contract", "subject_id": c["id"]}).json()
    assert dc["direction"] == "bearish" and dc["excluded"][0]["expression"] == "iv > 0.4"
    assert client.get(f"{API}/analysis/draft", params={"subject_type": "plan", "subject_id": "nope"}).status_code == 404
    assert client.get(f"{API}/analysis/draft", params={"subject_type": "spaceship"}).status_code == 422
    dm = client.get(f"{API}/analysis/draft", params={"subject_type": "manual", "symbol": "qqq"}).json()
    assert dm["symbols"] == ["QQQ"] and dm["conditions"] == []


# ------------------------------------------------------------------ validation
def test_the_form_is_validated_with_every_problem_reported():
    with pytest.raises(DomainError) as e:
        an.build_strategy(form(timeframe="5m", start="2025-01-01", end="2026-10-30",
                               conditions=[{"id": "c1", "expression": "close > smaa(5)"}], focus_horizon="7"), "synthetic")
    fields = {f["field"] for f in e.value.field_errors}
    assert {"start", "focus_horizon"} <= fields and any("smaa" in f["message"] for f in e.value.field_errors)


# ------------------------------------------------------------------ statistics
def test_forward_returns_are_measured_from_the_signal_close():
    closes = [100.0] * 60 + [100, 102, 104, 103, 110, 111]
    bars = {"T": daily(closes)}
    st = an.build_strategy(form(symbols=["T"], conditions=[{"id": "c1", "expression": "close > 101"}], horizons=["1", "3"],
                                dedupe_bars=0, focus_horizon="1", start="2024-01-02", end="2024-12-31"), "synthetic")
    res, matches = an.execute(st, direction="bullish", focus_horizon="1", bars_override=bars, bench_override=None)
    first = matches[0]
    assert first["timestamp"].startswith(str(bars["T"].index[61].date()))           # the close of 102
    assert first["forward"]["1"]["return_pct"] == pytest.approx(100 * (104 / 102 - 1), abs=1e-3)
    assert first["forward"]["3"]["return_pct"] == pytest.approx(100 * (110 / 102 - 1), abs=1e-3)
    assert first["forward"]["3"]["favorable_pct"] == pytest.approx(100 * (110 * 1.01 / 102 - 1), abs=1e-3)
    last = matches[-1]
    assert last["forward"] == {}                                     # the last bar has no future: incomplete, left out
    h1 = next(h for h in res["horizons"] if h["horizon"] == "3" and h["group"] == "all")
    assert h1["incomplete"] >= 1 and h1["signals"] == len(matches) - h1["incomplete"]


def test_bearish_direction_swaps_favourable_and_wins():
    closes = [100.0] * 60 + [102, 98, 95, 99, 100, 100, 100]
    bars = {"T": daily(closes)}
    st = an.build_strategy(form(symbols=["T"], conditions=[{"id": "c1", "expression": "close > 101"}], horizons=["2"],
                                dedupe_bars=0, focus_horizon="2", start="2024-01-02", end="2024-12-31"), "synthetic")
    res, matches = an.execute(st, direction="bearish", focus_horizon="2", bars_override=bars)
    m = matches[0]
    assert m["forward"]["2"]["return_pct"] < 0 and m["forward"]["2"]["favorable_pct"] > 0      # a fall is favourable
    h = next(x for x in res["horizons"] if x["group"] == "all")
    assert h["win_pct"] == 100.0 and h["wins"] == 1


def test_synthetic_run_statistics_breakdowns_and_bias_checks():
    st = an.build_strategy(form(symbols=["XYZ", "SPY"]), "synthetic")
    res, matches = an.execute(st, direction="bullish", focus_horizon="5", now=NOW)
    assert res["counts"]["matches"] == len(matches) > 0 and res["counts"]["exact"] + res["counts"]["near"] == len(matches)
    for dist in res["distributions"]:
        h = next(x for x in res["horizons"] if x["horizon"] == dist["horizon"] and x["group"] == "all")
        assert sum(b["count"] for b in dist["bins"]) == dist["signals"] == h["signals"]
        assert all(b["kind"] in ("win", "loss") for b in dist["bins"])
    dims = {b["dimension"] for b in res["breakdowns"]}
    assert {"trend", "volatility", "market"} <= dims and "time_of_day" not in dims          # daily bars: no time of day
    assert all(b["low_sample"] == (b["signals"] < stats.LOW_SAMPLE) for b in res["breakdowns"])
    bias = {b["code"]: b for b in res["bias"]}
    assert bias["SYNTHETIC"]["status"] == "fail" and bias["NO_LOOKAHEAD"]["status"] == "pass"
    assert res["truncation"]["checked"] > 0 and res["truncation"]["mismatches"] == 0
    assert bias["OVERLAP"]["status"] == "pass" and bias["SURVIVORSHIP"]["status"] == "warn"
    assert res["coverage"][0]["bars_in_range"] > 1500
    assert all(set(m["labels"]) >= {"trend", "volatility"} for m in matches)


def test_intraday_runs_break_down_by_time_of_day():
    st = an.build_strategy(form(timeframe="1h", start="2026-06-01", end="2026-10-30", horizons=["1", "3", "1d"],
                                dedupe_bars=7, focus_horizon="3",
                                conditions=[{"id": "c1", "expression": "close > sma(20)"}, {"id": "c2", "expression": "rsi(14) > 55"}]),
                           "synthetic")
    res, matches = an.execute(st, direction=None, focus_horizon="3", now=NOW)
    tod = {b["subset"] for b in res["breakdowns"] if b["dimension"] == "time_of_day"}
    assert tod and tod <= {"open (9:30–10:30)", "midday (10:30–14:30)", "close (14:30–16:00)"}
    assert "win_pct" not in next(h for h in res["horizons"] if h["group"] == "all")        # no direction: no wins


# ------------------------------------------------------------------ look-ahead
def test_changing_bars_after_a_signal_never_changes_the_signal():
    st = an.build_strategy(form(), "synthetic")
    base = run_search(st, now=NOW)
    bars = base.bars["XYZ"].copy()
    i = len(bars) // 2
    altered = bars.copy(deep=True)
    cols = ["open", "high", "low", "close"]
    altered.loc[altered.index[i + 1:], cols] = altered.loc[altered.index[i + 1:], cols].to_numpy() * 3.0   # wreck every later bar
    before = evaluate_symbol(st, bars)
    after = evaluate_symbol(st, altered)
    for a, b in zip(before, after):
        assert (a.passed[: i + 1] == b.passed[: i + 1]).all() and np.allclose(a.observed[: i + 1], b.observed[: i + 1], equal_nan=True)
        assert not (a.passed[i + 1:] == b.passed[i + 1:]).all() or not np.allclose(a.observed[i + 1:], b.observed[i + 1:], equal_nan=True)


def test_the_truncation_test_catches_a_look_ahead():
    st = an.build_strategy(form(), "synthetic")
    search = run_search(st, now=NOW)
    assert stats.truncation_test(st, search.bars, search.results, search.evals)["mismatches"] == 0
    row = search.results.iloc[0]
    i = search.bars[row["symbol"]].index.get_loc(row["timestamp"])
    e = search.evals[row["symbol"]][0]
    obs = e.observed.copy()
    obs[i] += 1.0                                                       # pretend the full history changed the past
    e.observed = obs
    t = stats.truncation_test(st, search.bars, search.results, search.evals, samples=len(search.results))
    assert t["mismatches"] >= 1 and "differs with later bars removed" in t["details"][0]


# ------------------------------------------------------------------ runs through the API
@pytest.fixture
def sync(client):
    client.app.state.analysis_source = "synthetic"
    client.app.state.analysis_sync = True
    client.app.state.analysis_overrides = {"now": NOW}
    return client


def test_live_mode_never_uses_synthetic_bars(client):
    from optlab.api.analysis_routes import data_source

    assert client.app.state.quotes.mode != "synthetic" and data_source(client.app) == "yahoo"


def test_run_lifecycle_storage_drilldown_and_snooping_warning(sync, account):
    body = form().model_dump()
    body.update(subject_type="manual")
    r = sync.post(f"{API}/analysis/runs", json=body)
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["source"] == "synthetic"
    rid = r.json()["id"]
    assert sync.post(f"{API}/analysis/runs/{rid}/start").status_code == 200
    run = sync.get(f"{API}/analysis/runs/{rid}").json()
    assert run["status"] == "done" and run["result"]["counts"]["matches"] == len(run["matches"]) > 0
    assert run["strategy"]["conditions"][0]["expression"] == "close > sma(50)" and run["strategy_version"]
    again = sync.post(f"{API}/analysis/runs/{rid}/start").json()
    assert again["status"] == "done"                                      # a finished run is never recomputed
    w = sync.get(f"{API}/analysis/runs/{rid}/matches/0/bars").json()
    assert w["bars"][w["signal_index"]]["t"] == run["matches"][0]["timestamp"]
    assert all(v is None or v > w["signal_index"] for v in w["horizon_ends"].values())
    assert sync.get(f"{API}/analysis/runs/{rid}/matches/99999/bars").status_code == 404
    for k in range(6):                                                    # six variations of the same idea
        b = dict(body, conditions=[{"id": "c1", "expression": f"close > sma({20 + k})"}])
        sync.post(f"{API}/analysis/runs/{sync.post(f'{API}/analysis/runs', json=b).json()['id']}/start")
    last = sync.get(f"{API}/analysis/runs").json()[0]
    full = sync.get(f"{API}/analysis/runs/{last['id']}").json()
    snoop = next(b for b in full["result"]["bias"] if b["code"] == "DATA_SNOOPING")
    assert snoop["status"] == "warn" and "6 other version(s)" in snoop["detail"]      # the first run + 5 earlier variations
    assert len(sync.get(f"{API}/analysis/runs", params={"subject_type": "manual"}).json()) == 7


def test_invalid_forms_are_refused_and_failures_are_stored_without_traces(sync, monkeypatch):
    bad = form().model_dump()
    bad["conditions"] = [{"id": "c1", "expression": "close >> 2"}]
    r = sync.post(f"{API}/analysis/runs", json=bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ANALYSIS_INVALID"
    rid = sync.post(f"{API}/analysis/runs", json=form().model_dump()).json()["id"]

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(an, "execute", boom)
    sync.post(f"{API}/analysis/runs/{rid}/start")
    run = sync.get(f"{API}/analysis/runs/{rid}").json()
    assert run["status"] == "error" and run["error"] == "RuntimeError: disk on fire" and "Traceback" not in run["error"]


def test_position_runs_mark_the_entry_bar(sync, account, db_url):
    pos = sync.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ",
                                              "legs": [opt("SELL", "PUT", 95, "1.10"), opt("BUY", "PUT", 90, "0.15")]}).json()
    st = an.build_strategy(form(), "synthetic")
    first = run_search(st, now=NOW).results.iloc[0]
    db = Database(db_url)
    from sqlalchemy import update

    from optlab.portfolio.models import Position
    with db.session() as s:
        s.execute(update(Position).where(Position.id == pos["id"]).values(
            opened_at=pd.Timestamp(first["timestamp"]).tz_convert("UTC").to_pydatetime().replace(hour=15)))
    db.dispose()
    body = dict(form().model_dump(), subject_type="position", subject_id=pos["id"])
    rid = sync.post(f"{API}/analysis/runs", json=body).json()["id"]
    sync.post(f"{API}/analysis/runs/{rid}/start")
    run = sync.get(f"{API}/analysis/runs/{rid}").json()
    assert sum(m["is_entry"] for m in run["matches"]) == 1 and run["matches"][0]["is_entry"]
    assert sync.get(f"{API}/analysis/runs", params={"subject_type": "position", "subject_id": pos["id"]}).json()[0]["id"] == rid


def test_descriptive_text_never_recommends():
    import re

    texts = [stats.__doc__ or "", drafts.__doc__ or ""]
    st = an.build_strategy(form(), "synthetic")
    res, _ = an.execute(st, direction="bullish", focus_horizon="5", now=NOW)
    texts += [b["detail"] for b in res["bias"]] + [b["title"] for b in res["bias"]]
    for t in texts:
        assert not re.search(r"\b(buy|sell|you should|recommend(ed)?|guarantee)\b", t, re.I), t
