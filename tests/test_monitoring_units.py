"""Phase 2 unit tests: confirmation / re-arm state machines, status precedence and reasons, rule evaluation
(including unknown results), the market calendar, notification policy and the scans.yaml fingerprint.
Deterministic: no network, no real clock."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from optlab.monitoring import legacy
from optlab.monitoring.calendar import Window
from optlab.monitoring.events import in_quiet_hours, notify_policy
from optlab.monitoring.rules import Context, completed_only, explain, split_conditions, validate
from optlab.monitoring.settings import MonitorSettings, NotificationSettings, RuleSet, ScheduleSettings
from optlab.monitoring.status import ACTION, EXIT, HOLD, STALE, WATCH, check_position, rank
from optlab.monitoring.transitions import step_alert, step_status
from optlab.portfolio.quotes import ChainSnapshot
from optlab.portfolio.valuation import PositionValuation

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


@dataclass
class St:
    status: str | None = None
    since: datetime | None = None
    candidate: str | None = None
    candidate_count: int = 0
    active: bool = False
    last_event_at: datetime | None = None
    last_cleared_at: datetime | None = None


# ------------------------------------------------------------------ status state machine
def test_first_status_is_adopted_and_only_non_hold_produces_an_event():
    st = St()
    assert step_status(st, HOLD, T0, 3, 15) is None and st.status == HOLD
    st2 = St()
    ch = step_status(st2, WATCH, T0, 3, 15)
    assert ch and ch.kind == "initial" and ch.to_status == WATCH


def test_a_change_needs_confirm_passes_and_produces_exactly_one_event():
    st = St(status=HOLD)
    out = [step_status(st, EXIT, T0 + timedelta(minutes=i), 3, 15) for i in range(6)]
    assert [o is not None for o in out] == [False, False, True, False, False, False]
    assert out[2].from_status == HOLD and out[2].to_status == EXIT and st.status == EXIT


def test_flapping_resets_the_confirmation_count():
    st = St(status=HOLD)
    for raw in (WATCH, WATCH, HOLD, WATCH, WATCH):
        assert step_status(st, raw, T0, 3, 0) is None
    assert st.status == HOLD and st.candidate == WATCH and st.candidate_count == 2


def test_stale_data_is_immediate_never_a_lingering_hold():
    st = St(status=HOLD)
    ch = step_status(st, STALE, T0, 3, 15)
    assert ch and ch.to_status == STALE and st.status == STALE
    assert step_status(st, HOLD, T0, 3, 15) is None and st.status == STALE     # recovery is confirmed


def test_urgent_statuses_repeat_on_their_interval_only():
    st = St(status=EXIT, last_event_at=T0)
    assert step_status(st, EXIT, T0 + timedelta(minutes=14), 3, 15) is None
    r = step_status(st, EXIT, T0 + timedelta(minutes=15), 3, 15)
    assert r and r.kind == "reminder"
    assert step_status(St(status=WATCH, last_event_at=T0), WATCH, T0 + timedelta(hours=2), 3, 15) is None
    assert step_status(St(status=EXIT, last_event_at=T0), EXIT, T0 + timedelta(hours=2), 3, 0) is None


# ------------------------------------------------------------------ alert state machine
def test_alert_fires_once_rearms_after_clearing_and_waiting():
    st, t = St(), T0
    acts = []
    for res in (True, True, True, False, True, None, True):
        acts.append(step_alert(st, res, t, 1, 30, 0))
        t += timedelta(minutes=5)
    # fire, still true (nothing), nothing, clear, true again but only 20 min after the alert, unknown,
    # 30 min after the alert: re-armed
    assert acts == ["fire", None, None, "clear", None, None, "fire"]


def test_alert_confirm_passes_and_repeat():
    st = St()
    assert [step_alert(st, True, T0 + timedelta(minutes=i), 2, 0, 10) for i in range(3)] == [None, "fire", None]
    assert step_alert(st, True, T0 + timedelta(minutes=11), 2, 0, 10) == "repeat"


# ------------------------------------------------------------------ precedence and reasons
def _leg(i, side, right, strike, exp=date(2026, 12, 18)):
    return SimpleNamespace(leg_index=i, asset_type=SimpleNamespace(value="OPTION"), side=SimpleNamespace(value=side),
                           option_right=SimpleNamespace(value=right), strike_units=int(strike * 10000),
                           expiration=exp, occ_symbol=f"XYZ{exp:%y%m%d}{right[0]}{int(strike * 1000):08d}")


def _position(legs, rules=None):
    return SimpleNamespace(legs=legs, monitor_json={"rules": rules} if rules is not None else {},
                           net_premium_units=-1_000_000, max_loss_state=SimpleNamespace(value="bounded"),
                           max_loss_units=4_000_000, underlying="XYZ")


def _quote(spread=5.0, oi=900.0):
    return SimpleNamespace(spread_pct=spread, open_interest=oi)


def _valuation(pnl="31", pct=31.0, price="100", status="AVAILABLE", legs=2, delta=10.0):
    lv = [SimpleNamespace(leg_index=i, quote=_quote(), quality=[]) for i in range(legs)]
    return PositionValuation("ESTIMATED", status, [] if status == "AVAILABLE" else ["Leg 1: not in chain"],
                             Decimal("-69"), None, Decimal(pnl) if pnl else None, pct, Decimal(price),
                             {"delta": delta, "gamma": 0.0, "theta": 1.5, "vega": -2.0}, lv, "cboe", "DELAYED", 15, T0,
                             None, 5.0, "provider", T0)


def _snap(error=None, rows=True, age=0):
    df = pd.DataFrame({"x": [1]}) if rows else None
    return ChainSnapshot("XYZ", "cboe", 15, False, "provider", T0 - timedelta(seconds=age), None, df, error)


SPREAD = [_leg(0, "SELL", "PUT", 95), _leg(1, "BUY", "PUT", 90)]
TODAY = date(2026, 11, 2)


def test_hold_when_nothing_holds_and_reasons_say_so():
    st = MonitorSettings()
    chk = check_position(_position(SPREAD), _valuation(), _snap(), st, None, TODAY, max_age=1e9)
    assert chk.raw_status == HOLD and "no exit, action or watch rule holds" in chk.reasons[0]


def test_exit_beats_watch_and_explains_values():
    rules = {"exit_when": ["pnl_pct >= 30"], "action_when": [], "watch_when": ["short_strike_otm_pct < 10"]}
    chk = check_position(_position(SPREAD, rules), _valuation(), _snap(), MonitorSettings(), None, TODAY, max_age=1e9,
                         previous={"pnl_pct": 22.0})
    assert chk.raw_status == EXIT
    assert chk.reasons == ["pnl_pct >= 30 (pnl_pct = +31.0%, was +22.0%)"]


def test_watch_reason_names_the_values():
    rules = {"exit_when": [], "action_when": [], "watch_when": ["short_strike_otm_pct < 10", "dte <= 60"]}
    chk = check_position(_position(SPREAD, rules), _valuation(), _snap(), MonitorSettings(), None, TODAY, max_age=1e9)
    assert chk.raw_status == WATCH
    assert chk.reasons == ["short_strike_otm_pct < 10 (short_strike_otm_pct = 5.0%)", "dte <= 60 (dte = 46)"]


def test_stale_quotes_win_over_an_exit_and_keep_it_as_context():
    rules = {"exit_when": ["pnl_pct >= 30"], "action_when": [], "watch_when": []}
    chk = check_position(_position(SPREAD, rules), _valuation(), _snap(error="refresh failed: timeout"),
                         MonitorSettings(), None, TODAY, max_age=1e9)
    assert chk.raw_status == STALE
    assert "older snapshot" in chk.reasons[0] and any(r.startswith("also: pnl_pct >= 30") for r in chk.reasons)


def test_quotes_older_than_the_limit_are_stale():
    chk = check_position(_position(SPREAD), _valuation(), _snap(age=900), MonitorSettings(), None, TODAY, max_age=300,
                         now=T0)
    assert chk.raw_status == STALE


def test_unvaluable_position_and_unknown_rule_are_stale_never_hold():
    v = _valuation(pnl=None, pct=None, status="PARTIAL")
    chk = check_position(_position(SPREAD), v, _snap(), MonitorSettings(), None, TODAY, max_age=1e9)
    assert chk.raw_status == STALE and "Leg 1: not in chain" in chk.reasons
    assert any("pnl_pct" in r and "Can't evaluate" in r for r in chk.reasons)


def test_rule_needing_bars_without_bars_is_stale():
    rules = {"exit_when": [], "action_when": [], "watch_when": ["rsi(14) < 30"]}
    chk = check_position(_position(SPREAD, rules), _valuation(), _snap(), MonitorSettings(), None, TODAY,
                         bars=None, bars_error="network down", max_age=1e9)
    assert chk.raw_status == STALE and "network down" in " ".join(chk.reasons)


def test_not_applicable_values_dont_make_a_long_call_stale():
    legs = [_leg(0, "BUY", "CALL", 100)]
    rules = {"exit_when": [], "action_when": [], "watch_when": ["short_strike_otm_pct < 3"]}
    chk = check_position(_position(legs, rules), _valuation(legs=1), _snap(), MonitorSettings(), None, TODAY, max_age=1e9)
    assert chk.raw_status == HOLD


def test_built_in_action_rules_expiry_and_assignment_risk():
    near = date(2026, 11, 4)
    legs = [_leg(0, "SELL", "PUT", 105, near), _leg(1, "BUY", "PUT", 95, near)]
    chk = check_position(_position(legs, {"exit_when": [], "action_when": [], "watch_when": []}), _valuation(),
                         _snap(), MonitorSettings(), None, TODAY, max_age=1e9)
    assert chk.raw_status == ACTION and "5.0% in the money with 2 days left: assignment risk" in chk.reasons[0]
    today = [_leg(0, "BUY", "CALL", 100, TODAY)]
    chk = check_position(_position(today, {"exit_when": [], "action_when": [], "watch_when": []}), _valuation(legs=1),
                         _snap(), MonitorSettings(), None, TODAY, max_age=1e9)
    assert chk.raw_status == ACTION and "expires today" in chk.reasons[0]


def test_earnings_before_expiry_metric():
    rules = {"exit_when": [], "action_when": [], "watch_when": ["earnings_before_expiry"]}
    chk = check_position(_position(SPREAD, rules), _valuation(), _snap(), MonitorSettings(), date(2026, 11, 20), TODAY,
                         max_age=1e9)
    assert chk.raw_status == WATCH and chk.metrics["days_to_earnings"] == 18


def test_precedence_order():
    assert [rank(x) for x in (STALE, EXIT, ACTION, WATCH, HOLD)] == [0, 1, 2, 3, 4]


# ------------------------------------------------------------------ rules
def test_validate_names_per_target():
    assert validate("pnl_pct >= 50 and rsi(14) < 30", "position") is None
    assert validate("ask <= target_entry and spread_pct < 10", "contract") is None
    assert "did you mean pnl_pct" in validate("pnl_pc >= 50", "position")
    assert validate("bid > 1", "position") is not None              # contract name on a position
    assert "not allowed" in validate("__import__('os')", "position") or validate("x.y > 1", "position")


def test_split_conditions_lines_semicolons_and_comments():
    assert split_conditions("bid > 1\n# note\nask < 2; iv > 0.3\n") == ["bid > 1", "ask < 2", "iv > 0.3"]


def test_context_bars_and_values():
    idx = pd.date_range("2026-01-01", periods=60, freq="D")
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": np.linspace(100, 70, 60), "volume": 1.0},
                        index=idx)
    ctx = Context("symbol", {"price": 70.0}, {}, bars, None, "1d")
    r = ctx.evaluate("rsi(14) < 30 and price < 80", "alert")
    assert r.result is True and "rsi(14)" in r.values
    short = Context("symbol", {"price": 70.0}, {}, bars.iloc[:5], None, "1d").evaluate("sma(50) > 1", "alert")
    assert short.result is None and "not enough" in short.reason
    assert explain(r).startswith("rsi(14) < 30 and price < 80 (")


def test_completed_only_drops_the_bar_in_progress():
    idx = pd.to_datetime(["2026-11-02 10:00", "2026-11-02 10:05"])
    df = pd.DataFrame({"close": [1, 2]}, index=idx)
    now = datetime(2026, 11, 2, 10, 7)
    assert len(completed_only(df, "5m", now)) == 1
    assert len(completed_only(df, "5m", datetime(2026, 11, 2, 10, 10))) == 2
    day = pd.DataFrame({"close": [1, 2]}, index=pd.to_datetime(["2026-10-30", "2026-11-02"]))
    assert len(completed_only(day, "1d", datetime(2026, 11, 2, 11, 0))) == 1
    assert len(completed_only(day, "1d", datetime(2026, 11, 2, 16, 1))) == 2


# ------------------------------------------------------------------ calendar
def test_window_holidays_early_close_and_weekends():
    w = Window(ScheduleSettings())
    et = lambda *a: datetime(*a, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))  # noqa: E731
    assert w.is_open(et(2026, 11, 2, 10, 0)) and not w.is_open(et(2026, 11, 2, 16, 0))
    assert not w.is_open(et(2026, 11, 26, 11, 0)) and "Thanksgiving" in w.why_closed(et(2026, 11, 26, 11, 0))
    assert w.is_open(et(2026, 11, 27, 12, 59)) and not w.is_open(et(2026, 11, 27, 13, 0))      # early close
    assert w.next_open(et(2026, 11, 6, 17, 0)) == et(2026, 11, 9, 9, 30)                        # Friday -> Monday
    assert w.next_open(et(2026, 11, 25, 17, 0)) == et(2026, 11, 27, 9, 30)                      # skips Thanksgiving
    off = Window(ScheduleSettings(skip_market_holidays=False))
    assert off.is_open(et(2026, 11, 26, 11, 0))


def test_window_handles_daylight_saving():
    w = Window(ScheduleSettings())
    # 2026-11-02 is after the switch back (EST, UTC-5): 09:30 ET = 14:30 UTC
    assert w.is_open(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc))
    assert not w.is_open(datetime(2026, 11, 2, 13, 45, tzinfo=timezone.utc))
    # 2026-10-30 is still EDT (UTC-4): 09:30 ET = 13:30 UTC
    assert w.is_open(datetime(2026, 10, 30, 13, 45, tzinfo=timezone.utc))


def test_next_tick_is_clock_aligned():
    w = Window(ScheduleSettings(every_seconds=60))
    assert w.next_tick(datetime(2026, 11, 2, 15, 0, 17, tzinfo=timezone.utc)) == datetime(2026, 11, 2, 15, 1,
                                                                                         tzinfo=timezone.utc)


# ------------------------------------------------------------------ notification policy
def test_notify_policy():
    st = MonitorSettings(notifications=NotificationSettings(min_severity="warning", muted_symbols=["spy"]))
    assert notify_policy(st, "info", "XYZ", T0) == ("none", None)
    assert notify_policy(st, "warning", "XYZ", T0) == ("pending", None)
    assert notify_policy(st, "warning", "SPY", T0)[0] == "suppressed"
    assert notify_policy(st, "critical", "XYZ", T0, muted="off for this position") == ("suppressed", "off for this position")
    crit = MonitorSettings(notifications=NotificationSettings(min_severity="critical"))
    assert notify_policy(crit, "warning", "XYZ", T0)[0] == "none"


def test_quiet_hours_across_midnight_and_critical_override():
    st = MonitorSettings(notifications=NotificationSettings(quiet_hours=True, quiet_start="22:00", quiet_end="07:00"))
    night = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)      # 23:00 ET
    day = datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc)
    assert in_quiet_hours(st, night) and not in_quiet_hours(st, day)
    assert notify_policy(st, "warning", "XYZ", night)[0] == "suppressed"
    assert notify_policy(st, "critical", "XYZ", night)[0] == "pending"


def test_settings_validation():
    with pytest.raises(ValueError):
        ScheduleSettings(start="9:30")
    with pytest.raises(ValueError):
        ScheduleSettings(timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        MonitorSettings(confirm_passes=0)
    assert RuleSet().exit_when == []


def test_settings_seeded_from_scans_yaml(tmp_path):
    from optlab.monitoring.settings import from_scans_yaml

    f = tmp_path / "scans.yaml"
    f.write_text("schedule: {start: 570, end: '15:45', every_seconds: 30}\nalerts: {confirm_passes: 2, "
                 "repeat_exit_minutes: 5, desktop: false}\nposition_defaults: {exit_when: ['pnl_pct >= 40']}\n")
    st = from_scans_yaml(f)
    assert (st.schedule.start, st.schedule.end, st.schedule.every_seconds) == ("09:30", "15:45", 30)
    assert st.confirm_passes == 2 and st.repeat_urgent_minutes == 5 and not st.notifications.desktop
    assert st.default_rules.exit_when == ["pnl_pct >= 40"]
    assert from_scans_yaml(tmp_path / "missing.yaml") == MonitorSettings()


# ------------------------------------------------------------------ scans.yaml fingerprint
def test_fingerprint_ignores_leg_order_name_and_rules():
    a = {"name": "A", "symbol": "spy", "legs": [{"type": "put", "strike": 640, "expiration": date(2026, 11, 20), "qty": -1, "price": 6.4},
                                                {"type": "put", "strike": 620, "expiration": date(2026, 11, 20), "qty": 1, "price": 3.9}]}
    b = {"name": "B", "symbol": "SPY", "exit_when": ["dte < 5"], "legs": list(reversed(a["legs"]))}
    assert legacy.fingerprint(a) == legacy.fingerprint(b)
    c = {**a, "legs": [{**a["legs"][0], "price": 6.5}, a["legs"][1]]}
    assert legacy.fingerprint(a) != legacy.fingerprint(c)
