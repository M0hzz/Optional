"""Deterministic overlay: synthetic series with known answers."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from optlab.overlay import config as oc  # noqa: E402
from optlab.overlay.config import OverlayConfigError, defaults, validate  # noqa: E402
from optlab.overlay.engine import scan  # noqa: E402
from optlab.overlay.quality import bar_health, blocked, completed_bars  # noqa: E402
from optlab.overlay.rules import evaluate_rule, make_evaluator, timeframe_alignment, to_weekly  # noqa: E402
from optlab.overlay.status import BANNED_WORDS, STATUSES  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 3, 2, 18, 0, tzinfo=ET)   # Monday evening: Monday's session is complete


def bars_from(close, volume=None, end="2026-03-02", spread=0.01) -> pd.DataFrame:
    close = np.asarray(close, float)
    idx = pd.bdate_range(end=end, periods=len(close))
    opn = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": opn, "high": np.maximum(opn, close) * (1 + spread),
                         "low": np.minimum(opn, close) * (1 - spread), "close": close,
                         "volume": np.full(len(close), 1e6) if volume is None else np.asarray(volume, float)}, index=idx)


def cfg_with(rules, **over):
    raw = defaults()
    raw["rules"] = rules
    for k, v in over.items():
        raw[k] = {**raw[k], **v} if isinstance(v, dict) else v
    return validate(raw)


def run(rule_dict, bars, th=None, bench=None):
    cfg = cfg_with([rule_dict], thresholds=th or {})
    rule = cfg.rules[0]
    ev = make_evaluator(bars, "1d", bench)
    return evaluate_rule(rule, bars, ev, "TEST", cfg.thresholds, {})


def simple(expr="close > 100", tol=None, required=True, **extra):
    c = {"id": "level", "expression": expr, "required": required, "weight": 1}
    if tol:
        c["tolerance"] = tol
    return {"id": "r", "name": "R", "conditions": [c], "confirmation": {"minimum_score": 100}, **extra}


# ---------------------------------------------------------------- statuses
def test_exact_match_confirmed_and_newly_confirmed():
    r = run(simple(), bars_from([99] * 30 + [101]))
    assert r.status == "CONFIRMED" and r.score == 100 and r.transition == "new_confirmed" and r.bars_since == 0
    assert "Confirmed on the 2026-03-02 close" in r.explanation


def test_near_match_score_and_missing_requirement():
    rule = {"id": "r", "name": "R", "confirmation": {"minimum_score": 100}, "conditions": [
        {"id": "a", "expression": "close > 100", "weight": 1, "tolerance": {"type": "percent_distance", "value": 2}},
        {"id": "b", "expression": "close > 1", "weight": 1}]}
    r = run(rule, bars_from([99] * 31))
    # shortfall = (100 - 99) / 100 = 1% of the 2% tolerance -> credit 0.5; score = 100 * (0.5 + 1) / 2
    assert r.status == "NEAR_CONFIRMATION" and r.score == 75.0
    assert any("close is 99; the configured requirement is > 100" in m for m in r.missing)


def test_failed_condition_not_present():
    r = run(simple(tol={"type": "percent_distance", "value": 2}), bars_from([50] * 31))
    assert r.status == "NOT_PRESENT" and r.score == 0 and r.failed == 1


def test_near_to_confirmed_transition():
    rule = simple(tol={"type": "percent_distance", "value": 2})
    rule["conditions"].append({"id": "b", "expression": "close > 1", "weight": 1})
    r = run(rule, bars_from([99] * 30 + [101]))
    assert r.prev_status == "NEAR_CONFIRMATION" and r.status == "CONFIRMED" and r.transition == "near_to_confirmed"
    assert r.prev_score == 75.0 and r.score == 100


def test_invalidated_after_confirmation_lost():
    r = run(simple(), bars_from([99] * 28 + [101, 102, 98]))
    assert r.status == "INVALIDATED" and r.bars_since == 1 and r.transition == "invalidated"
    assert r.reason_code == "confirmation_lost"


def test_invalidation_condition():
    r = run(simple("close > 100", invalidation=["close < 105"]), bars_from([99] * 30 + [101]))
    assert r.status == "INVALIDATED" and r.reason_code == "invalidation_condition"


def test_stale_signal():
    r = run(simple(), bars_from([99] * 20 + [101] * 8), th={"stale_after_bars": 5})
    assert r.status == "STALE" and r.bars_since == 7 and "freshness limit is 5 bars" in r.explanation


def test_overextended_signal():
    closes = [99] * 30 + [101, 103, 106, 110]
    r = run(simple(), bars_from(closes, spread=0.002), th={"overextended_atr_multiple": 2.0})
    assert r.status == "OVEREXTENDED" and r.atr_move > 2


def test_insufficient_data_during_warmup():
    r = run(simple("close > sma(50)"), bars_from(np.linspace(90, 110, 20)))
    assert r.status == "INSUFFICIENT_DATA" and r.unavailable == ["level"]
    assert "Nothing is assumed" in r.explanation


def test_missing_benchmark_marks_condition_unavailable_not_substituted():
    rule = {"id": "r", "name": "R", "confirmation": {"minimum_score": 50}, "conditions": [
        {"id": "up", "expression": "close > 1", "weight": 1},
        {"id": "rs", "expression": "rs(20) > 0", "required": False, "weight": 1}]}
    r = run(rule, bars_from(np.linspace(90, 110, 40)), th={"stale_after_bars": 100})     # no benchmark supplied
    assert r.unavailable == ["rs"] and r.score == 50 and r.status == "CONFIRMED"


def test_relative_strength_against_benchmark():
    sym, bench = bars_from(np.linspace(100, 130, 40)), bars_from(np.linspace(100, 105, 40))
    r = run({"id": "r", "name": "R", "conditions": [{"id": "rs", "expression": "rs(20) > 5"}]}, sym, bench=bench)
    assert r.base_status == "CONFIRMED"


def test_completed_bar_handling():
    b = bars_from([1, 2, 3], end="2026-03-02")
    kept, dropped = completed_bars(b, datetime(2026, 3, 2, 11, 0, tzinfo=ET))
    assert dropped and len(kept) == 2
    kept, dropped = completed_bars(b, datetime(2026, 3, 2, 16, 5, tzinfo=ET))
    assert not dropped and len(kept) == 3


def test_no_look_ahead():
    rng = np.random.default_rng(3)
    b = bars_from(100 + np.cumsum(rng.normal(0, 1, 300)))
    rule = {"id": "r", "name": "R", "conditions": [
        {"id": "a", "expression": "close > ema(21)", "tolerance": {"type": "percent_distance", "value": 1}},
        {"id": "b", "expression": "rsi(14) > 50", "required": False, "tolerance": {"type": "absolute", "value": 5}}]}
    full = run(rule, b)
    for cut in (150, 200, 260):
        part = run(rule, b.iloc[:cut])
        assert list(part.states) == list(full.states[:cut])
        assert np.allclose(part.scores, full.scores[:cut], equal_nan=True)


def test_multi_timeframe_alignment():
    b = bars_from(np.linspace(50, 150, 400))
    a = timeframe_alignment(b, to_weekly(b, b.index[-1].date()))
    assert a == {"higher": "Up", "intermediate": "Up", "lower": "Up", "agreement": "Aligned up"}
    d = bars_from(np.linspace(150, 50, 400))
    assert timeframe_alignment(d, to_weekly(d, d.index[-1].date()))["agreement"] == "Aligned down"


def test_relative_volume_threshold():
    vol = [1e6] * 30 + [1.6e6]
    rule = {"id": "r", "name": "R", "conditions": [{"id": "v", "expression": "rel_volume(20) >= 1.5"}]}
    assert run(rule, bars_from([100] * 31, vol)).status == "CONFIRMED"
    assert run(rule, bars_from([100] * 31, [1e6] * 30 + [1.4e6])).status != "CONFIRMED"


# ---------------------------------------------------------------- data health
TH = defaults()["thresholds"]


def test_duplicate_and_missing_bars():
    b = bars_from(np.linspace(10, 20, 30))
    b = pd.concat([b, b.iloc[[-1]]])
    clean, checks = bar_health("X", b.sort_index(), NOW, TH, "test")
    dup = next(c for c in checks if c.id == "duplicates")
    assert not dup.passed and dup.effect == "downgrade" and not clean.index.duplicated().any()
    gap = bars_from(np.linspace(10, 20, 30)).drop(index=pd.Timestamp("2026-02-10"))
    _, checks = bar_health("X", gap, NOW, TH, "test")
    miss = next(c for c in checks if c.id == "missing")
    assert not miss.passed and "1 NYSE session" in miss.detail


def test_out_of_order_bars_block():
    b = bars_from(np.linspace(10, 20, 30)).iloc[::-1]
    _, checks = bar_health("X", b, NOW, TH, "test")
    assert blocked(checks, "technical rules")


def test_stale_data_downgrades():
    b = bars_from(np.linspace(10, 20, 30), end="2026-02-20")
    _, checks = bar_health("X", b, NOW, TH, "test")
    fresh = next(c for c in checks if c.id == "fresh")
    assert not fresh.passed and fresh.effect == "downgrade"


def test_unadjusted_split_blocks_history():
    b = bars_from([200] * 20 + [100] * 20)
    _, checks = bar_health("X", b, NOW, TH, "test")
    assert blocked(checks, "historical evidence") and not blocked(checks, "technical rules")


def test_blocked_symbol_gives_data_error_result():
    cfg = cfg_with([simple()])
    sc = scan({"X": bars_from(np.linspace(10, 20, 30)).iloc[::-1]}, cfg, NOW)
    assert sc.results[0].status == "DATA_ERROR" and "Blocked by data health" in sc.results[0].explanation


# ---------------------------------------------------------------- config
def test_yaml_validation_reports_field_paths():
    raw = defaults()
    raw["rules"] = [{"id": "a", "name": "A", "conditions": [
        {"expression": "rsii(14) > 50"}, {"expression": "close > 1", "tolerance": {"type": "absolut", "value": 1}}]},
        {"id": "a", "name": "dup", "conditions": [{"expression": "close > 1"}]}]
    raw["thresholds"] = {**raw["thresholds"], "near_match_score": 120}
    raw["account_profile"] = {**raw["account_profile"], "reserve_cash": 5000}
    with pytest.raises(OverlayConfigError) as exc:
        validate(raw)
    msg = "\n".join(exc.value.problems)
    assert "overlay.rules[0].conditions[0].expression: unknown function rsii(); did you mean rsi" in msg
    assert "overlay.rules[0].conditions[1].tolerance.type" in msg
    assert "overlay.rules[1].id: duplicate rule id 'a'" in msg
    assert "overlay.thresholds.near_match_score: must be at most 100" in msg
    assert "overlay.account_profile.reserve_cash" in msg


def test_defaults_validate_and_version_is_stable():
    a, b = validate(defaults()), validate(defaults())
    assert a.version == b.version and len(a.rules) >= 8


def test_user_overrides_merge_and_save(tmp_path):
    p = tmp_path / "overlay.yaml"
    oc.save_user_overrides({"thresholds": {"near_match_score": 70}}, p)
    cfg = oc.load_overlay_config(p)
    assert cfg.thresholds["near_match_score"] == 70 and cfg.thresholds["watch_score"] == 50
    with pytest.raises(OverlayConfigError):
        oc.save_user_overrides({"thresholds": {"near_match_score": "lots"}}, p)
    assert oc.load_overlay_config(p).thresholds["near_match_score"] == 70   # bad change never written


def test_enable_disable_controls():
    b = {"X": bars_from([99] * 30 + [101])}
    assert scan(b, cfg_with([simple()], enabled=False), NOW).results == []
    off = simple()
    off["enabled"] = False
    assert scan(b, cfg_with([off]), NOW).results == []
    cfg = cfg_with([simple()], tabs={"scanner": False})
    assert not cfg.tab_on("scanner") and cfg.tab_on("macro")


def test_scan_is_deterministic():
    rng = np.random.default_rng(1)
    frames = {s: bars_from(100 + np.cumsum(rng.normal(0, 1, 260))) for s in ("SPY", "AAA")}
    cfg = validate(defaults())
    a, b = scan(frames, cfg, NOW), scan(frames, cfg, NOW)
    dump = lambda sc: json.dumps([r.as_dict() for r in sc.results], default=str, sort_keys=True)  # noqa: E731
    assert dump(a) == dump(b)


def test_conflicting_rules_flagged():
    up = {"id": "up", "name": "Up", "direction": "bullish", "conditions": [{"expression": "close > 1"}]}
    dn = {"id": "dn", "name": "Down", "direction": "bearish", "conditions": [{"expression": "close > 2"}]}
    sc = scan({"X": bars_from(np.linspace(10, 20, 30))}, cfg_with([up, dn]), NOW)
    assert any(r.status == "CONFLICTING" and r.symbol == "X" for r in sc.results)


def test_no_banned_words_in_labels_or_meanings():
    text = " ".join(f"{s.label} {s.meaning}" for s in STATUSES.values()).lower()
    for w in BANNED_WORDS:
        assert w not in text


# ---------------------------------------------------------------- phase 2
from optlab.overlay.alerts import TransitionTracker, format_alert, transition_text  # noqa: E402
from optlab.overlay.market import overview  # noqa: E402
from optlab.overlay.quality import intraday_health  # noqa: E402

DEFAULT_RULES = {r["id"]: r for r in defaults()["rules"]}


def test_confirmed_breakout_and_failed_breakout():
    base = [100 + 0.2 * np.sin(i) for i in range(60)]
    vol = [1e6] * 60 + [2e6]
    up = bars_from(base + [104], vol, spread=0.002)
    r = run(DEFAULT_RULES["confirmed_breakout"], up, th={"stale_after_bars": 50})
    assert r.base_status == "CONFIRMED", r.explanation
    fail = bars_from(base + [100], vol, spread=0.002)
    fail.iloc[-1, fail.columns.get_loc("high")] = 104          # traded above the 20-day high, closed back inside
    f = run(DEFAULT_RULES["failed_breakout"], fail)
    assert f.base_status == "CONFIRMED" and run(DEFAULT_RULES["confirmed_breakout"], fail).base_status != "CONFIRMED"


def test_vwap_reclaim_and_rejection():
    rule = {"id": "reclaim", "name": "Reclaim", "conditions": [
        {"id": "was_below", "expression": "ago(close, 1) < ago(rolling_vwap(20), 1)"},
        {"id": "closes_above", "expression": "close > rolling_vwap(20)"}]}
    closes = [100] * 25 + [97, 96, 95, 101]
    assert run(rule, bars_from(closes)).status == "CONFIRMED"
    rej = {"id": "reject", "name": "Reject", "conditions": [
        {"id": "tested", "expression": "high >= rolling_vwap(20)"}, {"id": "closed_below", "expression": "close < rolling_vwap(20)"}]}
    b = bars_from([100] * 25 + [97, 96, 95, 95.5])
    b.iloc[-1, b.columns.get_loc("high")] = 101
    assert run(rej, b).status == "CONFIRMED"


def test_compression_then_expansion():
    rng = np.random.default_rng(5)
    calm = 100 + np.cumsum(rng.normal(0, 1.0, 150))
    tight = calm[-1] + np.cumsum(rng.normal(0, 0.05, 40))
    comp = run(DEFAULT_RULES["compression"], bars_from(np.r_[calm, tight], spread=0.001), th={"stale_after_bars": 99})
    assert comp.base_status in ("CONFIRMED", "NEAR_CONFIRMATION") and comp.direction == "neutral"
    burst = tight[-1] + np.cumsum(np.full(6, 2.5))
    vol = [1e6] * 190 + [3e6] * 6
    exp = run(DEFAULT_RULES["compression_exit"], bars_from(np.r_[calm, tight, burst], vol, spread=0.001),
              th={"stale_after_bars": 99, "overextended_atr_multiple": 99})
    assert exp.base_status == "CONFIRMED", exp.explanation


def test_breadth_divergence():
    n = 260
    idx_up = bars_from(np.r_[np.linspace(100, 120, n - 1), 125])          # index makes a new 20-bar high
    weak = {f"S{i}": bars_from(np.r_[np.linspace(100, 140, n - 30), np.linspace(140, 100, 30)]) for i in range(5)}
    frames = {"SPY": idx_up, **weak}
    m = overview(frames, cfg_with([simple()]), None)
    name, verdict, items = m["verdicts"]["breadth_divergence"]
    assert items[0]["status"] == "pass" and items[1]["status"] == "pass" and verdict in ("CONFIRMED", "NEAR_CONFIRMATION")
    assert m["breadth_low_sample"] and m["breadth"]["n"] == 5


def test_market_overview_never_fabricates_missing_data():
    m = overview({"SPY": bars_from(np.linspace(100, 120, 300))}, cfg_with([simple()]), None)
    assert "DIA" in m["missing_indexes"] and not m["sectors"]["available"] and m["vix"] is None
    assert "No economic-calendar source" in m["events_note"]


def test_alert_dedupe_cooldown_and_transition_text(tmp_path):
    rule = {"id": "vol", "name": "Volume", "conditions": [
        {"id": "rv", "expression": "rel_volume(20) >= 1.5", "tolerance": {"type": "absolute", "value": 0.2}},
        {"id": "up", "expression": "close > 1"}]}
    b = bars_from([100] * 31, [1e6] * 29 + [1.46e6, 1.62e6])
    r = run(rule, b)
    assert r.transition == "near_to_confirmed"
    text = transition_text(r)
    assert text.startswith("CONFIRMED at the 2026-03-02 close.") and "now meets the >= 1.5 requirement" in text
    tr = TransitionTracker(tmp_path / "s.json", cooldown_minutes=30)
    assert len(tr.alerts([r], NOW)) == 1
    assert tr.alerts([r], NOW) == []                               # same bar again: deduplicated
    tr2 = TransitionTracker(tmp_path / "s.json", cooldown_minutes=30)
    assert tr2.alerts([r], NOW) == []                              # survives a restart
    later = run(rule, bars_from([100] * 32, [1e6] * 30 + [1.46e6, 1.62e6], end="2026-03-03"))
    assert TransitionTracker(tmp_path / "s.json", 30).alerts([later], NOW) == []   # inside the cooldown
    assert TransitionTracker(tmp_path / "s.json", 30).alerts([later], NOW.replace(hour=23)) == []  # that bar stays suppressed
    third = run(rule, bars_from([100] * 33, [1e6] * 31 + [1.46e6, 1.62e6], end="2026-03-04"))
    assert len(TransitionTracker(tmp_path / "s.json", 30).alerts([third], NOW.replace(hour=23))) == 1   # cooldown over
    body = format_alert(r)
    for section in ("STATUS:", "RULE:", "OBSERVED CONDITIONS:", "MISSING CONFIRMATION:", "MARKET CONTEXT:",
                    "HISTORICAL EVIDENCE:", "ACCOUNT FIT:", "DATA QUALITY:", "EVENT FLAGS:", "INVALIDATION:"):
        assert section in body


def test_signal_stacking_counts_groups_once(tmp_path):
    rules = [{"id": a, "name": a, "group": g, "conditions": [{"expression": "close > 100"}]}
             for a, g in (("ema_align", "ema"), ("ema_cross", "ema"), ("breakout", "level"))]
    sc = scan({"X": bars_from([99] * 30 + [101])}, cfg_with(rules), NOW)
    alerts = TransitionTracker(tmp_path / "s.json", 0, stacking_bars=3).alerts(sc.results, NOW)
    stack = [a for a in alerts if a.rule == "stacking"]
    assert len(stack) == 1 and "2 independent rule groups" in stack[0].body


def test_intraday_completed_bars_and_latency():
    idx = pd.date_range("2026-03-02 09:30", periods=12, freq="5min")
    b = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    kept, checks = intraday_health("X", b, "5m", datetime(2026, 3, 2, 10, 22, tzinfo=ET), "test")
    assert kept.index[-1] == pd.Timestamp("2026-03-02 10:15")      # the 10:20 bar is still forming
    assert next(c for c in checks if c.id == "latency").passed
    _, late = intraday_health("X", b, "5m", datetime(2026, 3, 2, 11, 30, tzinfo=ET), "test")
    assert not next(c for c in late if c.id == "latency").passed


def test_intraday_rules_skipped_without_intraday_data():
    r = simple()
    r["timeframe"] = "5m"
    sc = scan({"X": bars_from([99] * 30 + [101])}, cfg_with([r]), NOW)
    assert sc.results == [] and "live monitor" in sc.skipped[0][1]


# ---------------------------------------------------------------- phase 3: small account and options
from optlab.overlay import account as acc  # noqa: E402
from optlab.overlay.options import anomalies, contract_flags, deterioration, structures  # noqa: E402
from optlab.pricing.engine import Leg  # noqa: E402

P = acc.Profile()            # the default $1,000 small-account profile


def test_allocatable_capital_and_reserve():
    assert P.allocatable == 700 and P.risk_limit == 100
    assert acc.Profile(settled_cash=600).allocatable == 300           # unsettled cash isn't allocatable


def test_case1_fifty_dollar_option_fails():
    f = acc.long_option(P, 50.0, 500, "call")
    assert f.capital_required == 5000 and f.max_loss == 5000 and f.classification == "EXCEEDS CAPITAL LIMIT"
    assert f.status == "DOES_NOT_FIT_ACCOUNT"


def test_case2_fifty_cent_option():
    f = acc.long_option(P, 0.50, 20, "call")
    assert f.capital_required == 50 and f.max_loss == 50 and f.classification == "FITS CONFIGURED LIMITS"
    assert f.status == "FITS_ACCOUNT" and f.account_risk_pct == 5.0 and f.buying_power_remaining == 650


def test_case3_debit_spread():
    f = acc.debit_spread(acc.Profile(require_assignment_capacity=False), 0.90, 0.25, 1.0, 21, "call")
    assert f.capital_required == 65 and f.max_loss == 65 and f.max_profit == 35
    assert f.classification == "FITS CONFIGURED LIMITS"


def test_case4_credit_spread():
    f = acc.credit_spread(acc.Profile(require_assignment_capacity=False), 0.55, 0.25, 1.0, 9, "put")
    assert f.max_loss == 70 and f.max_profit == 30 and f.classification == "FITS CONFIGURED LIMITS"


def test_case5_cash_secured_put_over_1000():
    f = acc.cash_secured_put(P, 50, 1.20)
    assert f.capital_required == 4880 and f.assignment_obligation == 5000
    assert f.classification == "EXCEEDS CAPITAL LIMIT"
    assert any("assignment could require $5,000" in r for r in f.reasons)


def test_case6_covered_call_needs_shares():
    f = acc.covered_call(P, "AAPL", 200, 2.0, 210)
    assert f.capital_required == 19800 and f.classification == "EXCEEDS CAPITAL LIMIT"
    owned = acc.covered_call(acc.Profile(owned_shares={"AAPL": 100}), "AAPL", 200, 2.0, 210)
    assert owned.capital_required == 0 and "already recorded" in owned.notes[0]


def test_case7_naked_call_unlimited():
    f = acc.naked_call(P)
    assert f.classification == "UNDEFINED RISK" and not f.defined_risk and f.max_loss is None
    legs = [Leg("call", 100, 30, -1, 0.3, entry_price=2.0)]
    assert acc.legs_fit(P, legs, 100).classification == "UNDEFINED RISK"


def test_case8_fits_buying_power_but_exceeds_preferred_risk():
    p = acc.Profile(max_capital_per_idea=150, max_loss_per_idea=250, max_account_risk_pct=30,
                    require_assignment_capacity=False)
    f = acc.debit_spread(p, 2.40, 0.40, 5.0, 105, "call")
    assert f.capital_required == 200 and f.classification == "FITS BUYING POWER BUT EXCEEDS PREFERRED RISK"
    assert f.status == "FITS_BP_EXCEEDS_RISK"


def test_case9_reserved_cash_reduces_buying_power():
    f = acc.long_option(acc.Profile(reserve_cash=950, max_capital_per_idea=1000), 0.60, 10, "call")
    assert P.allocatable == 700 and f.classification == "EXCEEDS CAPITAL LIMIT" and "allocatable $50.00" in f.reasons[0]


def test_case10_spread_assignment_exceeds_account():
    f = acc.credit_spread(P, 0.55, 0.25, 1.0, 50, "put")
    assert f.max_loss == 70 and f.assignment_obligation == 5000
    assert f.classification == "ASSIGNMENT OBLIGATION EXCEEDS CAPACITY"
    assert any("must buy 100 shares at $50.00" in n for n in f.notes)


def test_case11_missing_historical_option_quotes():
    from optlab.overlay.history_ev import options_history_status
    st_, msg = options_history_status(signal_dates=[pd.Timestamp("2025-06-02")], snapshot_dates=[])
    assert st_ == "INSUFFICIENT_DATA" and "timestamped option quotes are unavailable" in msg


def test_case12_boundary_exactly_1000():
    p = acc.Profile(reserve_cash=0, max_capital_per_idea=1000, max_loss_per_idea=1000, max_account_risk_pct=100)
    assert acc.long_option(p, 10.00, 50, "call").classification == "FITS CONFIGURED LIMITS"
    assert acc.long_option(p, 10.01, 50, "call").classification == "EXCEEDS CAPITAL LIMIT"


def test_affordability_never_marks_quality_green():
    from optlab.overlay.status import STATUSES as S
    assert S["FITS_ACCOUNT"].color != "green" and S["CONFIRMED"].color == "green"


def _chain():
    rows = []
    for k, cb, ca, pb, pa in ((95, 6.0, 6.2, 0.9, 1.0), (100, 2.4, 2.5, 2.3, 2.45), (105, 0.0, 0.3, 6.0, 6.3),
                              (110, 0.5, 0.4, 10.0, 10.4)):
        for t, b, a, d in (("call", cb, ca, 0.6), ("put", pb, pa, -0.4)):
            rows.append({"contract": f"X{k}{t[0]}", "expiration": date(2026, 4, 17), "dte": 30, "strike": k, "type": t,
                         "bid": b, "ask": a, "mid": (b + a) / 2, "last": a, "volume": 50, "open_interest": 500,
                         "iv": 0.3, "delta": d, "gamma": 0.05, "theta": -0.03, "vega": 0.1, "underlying_price": 100.0})
    return pd.DataFrame(rows)


def test_contract_quality_flags():
    df = contract_flags(_chain(), TH, P)
    zero = df[df["contract"] == "X105c"].iloc[0]
    crossed = df[df["contract"] == "X110c"].iloc[0]
    assert "zero bid" in zero["flags"] and zero["liquidity"] == "POOR_LIQUIDITY"
    assert "crossed market" in crossed["flags"]
    good = df[df["contract"] == "X100c"].iloc[0]
    assert good["liquidity"] == "USABLE" and good["cost_per_contract"] == 250
    stale = contract_flags(_chain(), TH, P, quotes_fresh=False)
    assert (stale["liquidity"] == "POOR_LIQUIDITY").all() and stale["flags"].str.contains("stale quote").all()
    nog = _chain().assign(delta=np.nan)
    assert contract_flags(nog, TH, P)["flags"].str.contains("missing Greeks").all()
    wide = contract_flags(_chain().assign(ask=lambda d: d["bid"] * 1.5 + 0.1), TH, P)
    assert wide["flags"].str.contains("wide spread").all()


def test_earnings_before_expiration_flag():
    df = contract_flags(_chain(), TH, P, earnings=date(2026, 4, 1))
    assert df["flags"].str.contains("earnings before expiration").all()


def test_anomalies_and_structure_validation():
    ch = _chain()
    ch.loc[ch["contract"] == "X105c", ["bid", "ask"]] = [3.0, 3.1]     # 105 call priced above the 100 call
    reasons = anomalies(ch)
    assert reasons[ch["contract"] == "X105c"].iloc[0] == "price out of order with the neighbouring strike"
    df = contract_flags(ch, TH, P)
    s = structures(df, acc.Profile(require_assignment_capacity=False), TH, [date(2026, 4, 17)], 1, 20, "X")
    flagged = s[s["validation"] != ""]
    assert not flagged.empty and set(s["strategy"]) >= {"Long call", "Bull call debit spread", "Bull put credit spread"}
    d = s[(s["strategy"] == "Bull call debit spread") & (s["strikes"] == "95/100")].iloc[0]
    assert d["net_natural"] == pytest.approx(6.2 - 2.4) and d["max_loss"] == pytest.approx(380)


def test_liquidity_deterioration():
    a = contract_flags(_chain(), TH, P)
    ch = _chain()
    ch.loc[ch["contract"] == "X100c", ["bid", "ask"]] = [0.0, 2.6]
    d = deterioration(a, contract_flags(ch, TH, P), TH)
    assert "X100c" in set(d["contract"]) and "bid disappeared" in d.set_index("contract").loc["X100c", "change"]
