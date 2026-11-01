"""Phase 3 tests: risk policies (effective limits, small-account mode, revisions), every risk check's
PASS / WARNING / BLOCKED / UNKNOWN, trade plans (validation, state machine, risk gate, conversion and the
planned-versus-actual comparison), positions breaking the policy, the monitor moving plans, risk-limit
events and the risk dashboard. Deterministic: fake delayed provider, no network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from conftest import EXP, opt, stock

from optlab.planning import policy as pol
from optlab.planning import riskcheck as rc

API = "/api/v1"
TODAY = date(2026, 11, 2)


# ------------------------------------------------------------------ policy and checks (units)
def port(value="5000", cash="4000", positions=(), today_pnl="0", week_pnl="0"):
    return rc.Portfolio(None if value is None else Decimal(value), None if cash is None else Decimal(cash), None,
                        list(positions), Decimal(today_pnl), Decimal(week_pnl), TODAY)


def cand(**kw):
    base = dict(underlying="XYZ", strategy="PUT_CREDIT_SPREAD", max_loss_state="bounded", max_loss=Decimal("400"),
                capital=Decimal("400"), defined_risk=True, contracts=1, expirations=[EXP], earnings=None,
                leg_quotes=[{"occ_symbol": "XYZ261218P00095000", "open_interest": 900.0, "spread_pct": 4.0}])
    base.update(kw)
    return rc.Candidate(**base)


def run(settings: pol.PolicySettings, c, p=None):
    p = p or port()
    return rc.evaluate(pol.effective(settings, p.account_value), p, c)


def by(rep, code):
    return next(x for x in rep.checks if x.code == code)


def test_default_policy_passes_ordinary_positions():
    rep = run(pol.PolicySettings(), cand())
    assert rep.overall == rc.PASS and not rep.small_account_active


def test_max_loss_over_the_limit_is_blocked_with_its_reason():
    rep = run(pol.PolicySettings(max_loss_per_position="300"), cand())
    c = by(rep, "MAX_LOSS")
    assert rep.overall == rc.BLOCKED and c.result == rc.BLOCKED
    assert c.reason == "Maximum loss $400.00 is over the limit of $300.00." and c.limit == "$300.00"


def test_warning_near_a_limit_and_pass_well_within():
    assert by(run(pol.PolicySettings(max_loss_per_position="450"), cand()), "MAX_LOSS").result == rc.WARNING
    assert by(run(pol.PolicySettings(max_loss_per_position="1000"), cand()), "MAX_LOSS").result == rc.PASS


def test_small_account_mode_applies_below_the_threshold_and_says_why():
    s = pol.PolicySettings()
    small = run(s, cand(), port(value="800", cash="800"))
    assert small.small_account_active and "below $1,000.00" in small.small_account_reason
    assert by(small, "MAX_LOSS").result == rc.BLOCKED and by(small, "MAX_LOSS").source == "small account"
    assert by(small, "CAPITAL").result == rc.BLOCKED
    big = run(s, cand(), port(value="5000"))
    assert not big.small_account_active and big.overall == rc.PASS
    unknown = pol.effective(s, None)
    assert not unknown["small_account_active"] and "no account value" in unknown["small_account_reason"]


def test_small_account_takes_the_stricter_value_only():
    s = pol.PolicySettings(max_loss_per_position="50", allow_margin=True)
    eff = pol.effective(s, Decimal("500"))
    assert eff["limits"]["max_loss_per_position"] == Decimal("50") and eff["source"]["max_loss_per_position"] == "policy"
    assert eff["limits"]["allow_margin"] is False and eff["source"]["allow_margin"] == "small account"
    assert eff["limits"]["averaging_down"] == "require_plan"


def test_undefined_risk_naked_shorts_and_unknown_loss():
    unb = cand(strategy="SHORT_CALL", max_loss_state="unbounded", max_loss=None, capital=None)
    assert by(run(pol.PolicySettings(), unb), "DEFINED_RISK").result == rc.WARNING
    rep = run(pol.PolicySettings(defined_risk_only=True, allow_naked_short_options=False), unb)
    assert by(rep, "DEFINED_RISK").result == rc.BLOCKED and by(rep, "NAKED_SHORT").result == rc.BLOCKED
    unk = run(pol.PolicySettings(), cand(max_loss_state="unknown", max_loss=None))
    assert by(unk, "DEFINED_RISK").result == rc.UNKNOWN and unk.overall == rc.UNKNOWN


def test_missing_account_value_makes_percent_checks_unknown_never_pass():
    rep = run(pol.PolicySettings(max_risk_pct_per_position=5, small_account_mode=False), cand(), port(value=None))
    assert by(rep, "RISK_PCT").result == rc.UNKNOWN and "no account value" in by(rep, "RISK_PCT").reason
    assert by(run(pol.PolicySettings(max_risk_pct_per_position=5), cand()), "RISK_PCT").result == rc.BLOCKED   # 8%


def test_margin_daily_and_weekly_loss_limits():
    s = pol.PolicySettings(allow_margin=False, max_daily_realized_loss="200", max_weekly_realized_loss="500")
    rep = run(s, cand(capital=Decimal("4500")), port(today_pnl="-200", week_pnl="-420"))
    assert by(rep, "NO_MARGIN").result == rc.BLOCKED and "would use margin" in by(rep, "NO_MARGIN").reason
    assert by(rep, "DAILY_LOSS").result == rc.BLOCKED and "no new risk" in by(rep, "DAILY_LOSS").reason
    assert by(rep, "WEEKLY_LOSS").result == rc.WARNING


def test_open_risk_counts_groups_and_greeks():
    held = [rc.OpenPos("a", "SPY spread", "SPY", "bounded", Decimal("500"), Decimal("-20"), 30.0, -4.0),
            rc.OpenPos("b", "QQQ call", "QQQ", "unbounded", None, None, None, None)]
    s = pol.PolicySettings(max_total_open_risk="800", max_open_positions=2, max_positions_per_group=2,
                           max_net_delta=100, averaging_down="require_plan")
    rep = run(s, cand(underlying="SPY"), port(positions=held))
    assert by(rep, "TOTAL_OPEN_RISK").result == rc.BLOCKED and "unlimited or unknown risk" in by(rep, "TOTAL_OPEN_RISK").reason
    assert by(rep, "OPEN_POSITIONS").result == rc.BLOCKED
    assert by(rep, "PER_GROUP").result == rc.BLOCKED and "US index ETFs" in by(rep, "PER_GROUP").label
    assert by(rep, "NET_DELTA").result == rc.UNKNOWN                     # QQQ has no recent Greeks
    assert by(rep, "AVERAGING_DOWN").result == rc.BLOCKED and "new written plan" in by(rep, "AVERAGING_DOWN").reason
    planned = run(s, cand(underlying="SPY", has_plan=True), port(positions=held))
    assert by(planned, "AVERAGING_DOWN").result == rc.PASS


def test_dte_earnings_and_liquidity():
    s = pol.PolicySettings(min_dte=50, max_dte=60, earnings="block", min_open_interest=1000, max_spread_pct=3)
    rep = run(s, cand(earnings=date(2026, 11, 20)))
    assert by(rep, "MIN_DTE").result == rc.BLOCKED and by(rep, "MAX_DTE").result == rc.PASS
    assert by(rep, "EARNINGS").result == rc.BLOCKED
    assert by(rep, "OPEN_INTEREST").result == rc.BLOCKED and by(rep, "SPREAD").result == rc.BLOCKED
    noq = run(s, cand(leg_quotes=None))
    assert by(noq, "OPEN_INTEREST").result == rc.UNKNOWN and "no quotes" in by(noq, "OPEN_INTEREST").reason


def test_overall_precedence():
    C = rc.Check
    assert rc.overall([C("a", "", rc.PASS, ""), C("b", "", rc.WARNING, "")]) == rc.WARNING
    assert rc.overall([C("a", "", rc.UNKNOWN, ""), C("b", "", rc.WARNING, "")]) == rc.UNKNOWN
    assert rc.overall([C("a", "", rc.UNKNOWN, ""), C("b", "", rc.BLOCKED, "")]) == rc.BLOCKED
    assert rc.overall([]) == rc.PASS


def test_policy_validation():
    with pytest.raises(ValueError):
        pol.PolicySettings(max_loss_per_position="1.23456")
    with pytest.raises(ValueError):
        pol.PolicySettings(max_risk_pct_per_position=150)
    s = pol.PolicySettings(correlation_groups={" Banks ": ["jpm", "bac", ""]})
    assert s.correlation_groups == {"Banks": ["BAC", "JPM"]}


# ------------------------------------------------------------------ API: policy, positions
def test_policy_revisions_and_history(client, account):
    g = client.get(f"{API}/accounts/{account['id']}/risk-policy").json()
    assert g["is_default"] and g["revision"] is None and g["effective"]["small_account_active"] is False
    body = {"settings": {**g["settings"], "max_loss_per_position": "250"}, "notes": "tighter"}
    r1 = client.put(f"{API}/accounts/{account['id']}/risk-policy", json=body).json()
    r2 = client.put(f"{API}/accounts/{account['id']}/risk-policy", json={**body, "notes": "again"}).json()
    assert (r1["revision"], r2["revision"]) == (1, 2) and r2["settings"]["max_loss_per_position"] == "250.0000"
    h = client.get(f"{API}/accounts/{account['id']}/risk-policy/history").json()
    assert [x["revision"] for x in h] == [2, 1] and h[1]["superseded_at"] is not None and h[0]["superseded_at"] is None
    bad = client.put(f"{API}/accounts/{account['id']}/risk-policy", json={"settings": {"max_loss_per_position": "1.12345"}})
    assert bad.status_code == 422


def set_policy(client, account, **kw):
    g = client.get(f"{API}/accounts/{account['id']}/risk-policy").json()
    r = client.put(f"{API}/accounts/{account['id']}/risk-policy", json={"settings": {**g["settings"], **kw}})
    assert r.status_code == 200, r.text


def test_recording_a_position_that_breaks_the_policy_needs_acknowledgement(client, account):
    set_policy(client, account, max_loss_per_position="100", small_account_mode=False)
    legs = [opt("SELL", "PUT", 95, "2.00"), opt("BUY", "PUT", 90, "1.00")]          # max loss $400
    ev = client.post(f"{API}/risk/evaluate", json={"account_id": account["id"], "underlying": "XYZ", "legs": legs}).json()
    assert ev["overall"] == "BLOCKED" and any(c["code"] == "MAX_LOSS" and c["result"] == "BLOCKED" for c in ev["checks"])
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ", "legs": legs})
    assert r.status_code == 422 and r.json()["error"]["code"] == "RULE_VIOLATION"
    assert r.json()["error"]["details"]["risk"]["overall"] == "BLOCKED"
    ok = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ", "legs": legs,
                                                "acknowledge_rule_violation": True}).json()
    assert ok["rule_violation"] is True and ok["entry_risk"]["overall"] == "BLOCKED"
    events = client.get(f"{API}/events", params={"type": ["RULE_VIOLATION"]}).json()["items"]
    assert len(events) == 1 and "Maximum loss per position" in events[0]["message"]


def test_positions_within_policy_keep_their_entry_check(client, account):
    p = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ",
                                              "legs": [opt("BUY", "CALL", 100, "3.00")]}).json()
    assert p["rule_violation"] is False and p["entry_risk"]["overall"] in ("PASS", "WARNING")


# ------------------------------------------------------------------ API: plans
def plan_body(account, **kw):
    b = {"account_id": account["id"], "symbol": "XYZ", "title": "XYZ bull put", "thesis": "Holding support at 95",
         "direction": "bullish", "intended_strategy": "PUT_CREDIT_SPREAD", "trigger": "Bounce off 95",
         "trigger_conditions": "price > 99", "confirmation_conditions": "price < 101", "invalidation": "Close below 92",
         "invalidation_price": "92", "planned_max_loss": "450", "planned_contracts": 1, "min_entry_credit": "0.90",
         "expiration_from": "2026-12-01", "expiration_to": "2027-01-31", "target": "50% of the credit",
         "legs": [opt("SELL", "PUT", 95, "1.00"), opt("BUY", "PUT", 90, "0.10")]}
    b.update(kw)
    return b


def test_plan_lifecycle_edit_arm_and_rules(client, account):
    r = client.post(f"{API}/plans", json=plan_body(account, thesis=None))
    assert r.status_code == 201
    p = r.json()
    assert p["state"] == "DRAFT" and not p["validation"]["ready"]
    assert {"Thesis"} <= {e["item"] for e in p["validation"]["errors"]}
    arm = client.post(f"{API}/plans/{p['id']}/transition", json={"version": p["version"], "to": "ARMED"})
    assert arm.status_code == 422 and arm.json()["error"]["code"] == "PLAN_INCOMPLETE"
    p = client.patch(f"{API}/plans/{p['id']}", json={"version": p["version"], "thesis": "Support at 95"}).json()
    assert p["validation"]["ready"]
    p = client.post(f"{API}/plans/{p['id']}/transition", json={"version": p["version"], "to": "ARMED"}).json()
    assert p["state"] == "ARMED" and p["risk"]["overall"] in ("PASS", "WARNING", "UNKNOWN")
    edit = client.patch(f"{API}/plans/{p['id']}", json={"version": p["version"], "notes": "x"})
    assert edit.status_code == 409 and edit.json()["error"]["code"] == "PLAN_NOT_DRAFT"
    skip = client.post(f"{API}/plans/{p['id']}/transition", json={"version": p["version"], "to": "SKIPPED"})
    assert skip.status_code == 422 and skip.json()["error"]["code"] == "REASON_REQUIRED"
    p = client.post(f"{API}/plans/{p['id']}/transition", json={"version": p["version"], "to": "SKIPPED",
                                                              "reason": "Volatility spiked"}).json()
    assert p["state"] == "SKIPPED" and p["outcome_reason"] == "Volatility spiked" and p["allowed_transitions"] == ["ARCHIVED"]
    back = client.post(f"{API}/plans/{p['id']}/transition", json={"version": p["version"], "to": "ARMED"})
    assert back.status_code == 409 and back.json()["error"]["code"] == "BAD_TRANSITION"
    copy = client.post(f"{API}/plans/{p['id']}/duplicate").json()
    assert copy["state"] == "DRAFT" and copy["fields"]["thesis"] == "Support at 95"
    tl = client.get(f"{API}/plans/{p['id']}/timeline").json()
    assert any(x["kind"] == "event" and x["event"]["type"] == "PLAN_STATE_CHANGED" for x in tl)


def test_plan_exceeding_max_loss_is_blocked_with_its_reason(client, account):
    """Acceptance check agreed for Phase 3."""
    set_policy(client, account, max_loss_per_position="200", small_account_mode=False)
    p = client.post(f"{API}/plans", json=plan_body(account)).json()          # legs risk $410
    r = client.post(f"{API}/plans/{p['id']}/transition", json={"version": p["version"], "to": "ARMED"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "RISK_BLOCKED"
    risk = r.json()["error"]["details"]["risk"]
    c = next(x for x in risk["checks"] if x["code"] == "MAX_LOSS")
    assert risk["overall"] == "BLOCKED" and c["result"] == "BLOCKED"
    assert c["reason"] == "Maximum loss $410.00 is over the limit of $200.00."
    assessed = client.post(f"{API}/plans/{p['id']}/assess").json()
    assert assessed["risk"]["overall"] == "BLOCKED" and assessed["state"] == "DRAFT"


def test_conditions_and_links_are_validated(client, account):
    bad = client.post(f"{API}/plans", json=plan_body(account, trigger_conditions="prise > 3"))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "BAD_CONDITION"
    js = client.post(f"{API}/plans", json=plan_body(account, chart_reference="javascript:alert(1)"))
    assert js.status_code == 422
    ok = client.post(f"{API}/plans", json=plan_body(account, chart_reference="https://example.com/chart.png"))
    assert ok.status_code == 201
    inverted = client.post(f"{API}/plans", json=plan_body(account, entry_zone_low="10", entry_zone_high="5"))
    assert inverted.status_code == 422


def arm(client, account, **kw):
    p = client.post(f"{API}/plans", json=plan_body(account, **kw)).json()
    r = client.post(f"{API}/plans/{p['id']}/transition", json={"version": p["version"], "to": "ARMED"})
    assert r.status_code == 200, r.text
    return r.json()


def test_convert_keeps_the_plan_and_compares_planned_with_actual(client, account):
    p = arm(client, account)
    conv = client.post(f"{API}/plans/{p['id']}/convert", json={
        "version": p["version"], "legs": [opt("SELL", "PUT", 95, "1.10", qty=2), opt("BUY", "PUT", 90, "0.15", qty=2)],
        "underlying_price_at_entry": "100"})
    assert conv.status_code == 200, conv.text
    out = conv.json()
    plan, position = out["plan"], out["position"]
    assert plan["state"] == "CONVERTED" and plan["position_id"] == position["id"] and position["plan_id"] == plan["id"]
    items = {i["aspect"]: i for i in plan["comparison"]["items"]}
    assert items["Strategy"]["result"] == "as_planned"
    assert items["Size"]["result"] == "deviated" and items["Size"]["actual"] == "2 contract(s)"
    assert items["Maximum loss"]["result"] == "deviated"                     # 2 lots risk $810 > $450
    assert items["Entry price"]["result"] == "as_planned" and items["Entry price"]["actual"] == "credit $0.95"
    assert items["Timing"]["result"] == "deviated"                            # converted while ARMED
    assert plan["comparison"]["plan_snapshot"]["thesis"] == "Holding support at 95"
    again = client.post(f"{API}/plans/{p['id']}/convert", json={"version": plan["version"], "legs": [opt("BUY", "CALL", 100, "1")]})
    assert again.status_code == 409


def test_convert_that_breaks_the_policy_needs_acknowledgement(client, account):
    p = arm(client, account)
    set_policy(client, account, max_contracts_per_position=1, small_account_mode=False)
    legs = [opt("SELL", "PUT", 95, "1.10", qty=3), opt("BUY", "PUT", 90, "0.15", qty=3)]
    r = client.post(f"{API}/plans/{p['id']}/convert", json={"version": p["version"], "legs": legs})
    assert r.status_code == 422 and r.json()["error"]["code"] == "RULE_VIOLATION"
    ok = client.post(f"{API}/plans/{p['id']}/convert", json={"version": p["version"], "legs": legs,
                                                             "acknowledge_rule_violation": True}).json()
    assert ok["position"]["rule_violation"] is True


def test_monitor_moves_plans_through_trigger_confirmation_and_invalidation(client, account, provider, quotes):
    quotes.ttl = 0                                                           # refetch every pass
    st = client.get(f"{API}/monitor/settings").json()
    client.put(f"{API}/monitor/settings", json={**st, "confirm_passes": 1})
    p = arm(client, account)                                                 # trigger price > 99, confirm price < 101
    client.post(f"{API}/monitor/run")
    p = client.get(f"{API}/plans/{p['id']}").json()
    assert p["state"] == "QUALIFIED" and p["monitor"]["trigger"][0]["result"] is True
    provider.chain.loc[:, "underlying_price"] = 102.0                       # confirmation lost
    client.post(f"{API}/monitor/run")
    assert client.get(f"{API}/plans/{p['id']}").json()["state"] == "WAITING"
    provider.chain.loc[:, "underlying_price"] = 98.0                        # trigger lost
    client.post(f"{API}/monitor/run")
    assert client.get(f"{API}/plans/{p['id']}").json()["state"] == "ARMED"
    provider.chain.loc[:, "underlying_price"] = 91.0                        # below the invalidation price
    client.post(f"{API}/monitor/run")
    p = client.get(f"{API}/plans/{p['id']}").json()
    assert p["state"] == "INVALIDATED"
    ev = client.get(f"{API}/events", params={"type": ["PLAN_STATE_CHANGED"]}).json()["items"]
    assert any("price <= 92" in e["message"] and e["severity"] == "warning" for e in ev)


def test_monitor_expires_plans(client, account):
    past = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    p = client.post(f"{API}/plans", json=plan_body(account, expires_at=past)).json()
    import time

    time.sleep(2.2)
    client.post(f"{API}/monitor/run")
    assert client.get(f"{API}/plans/{p['id']}").json()["state"] == "EXPIRED"


def test_risk_dashboard_and_limit_events(client, account):
    client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ",
                                          "legs": [opt("SELL", "PUT", 95, "2.00"), opt("BUY", "PUT", 90, "1.00")]})
    set_policy(client, account, max_total_open_risk="420", small_account_mode=False)
    d = client.get(f"{API}/accounts/{account['id']}/risk").json()
    c = next(x for x in d["utilization"]["checks"] if x["code"] == "TOTAL_OPEN_RISK")
    assert c["result"] == "WARNING" and d["open_risk"] == "400.0000" and d["positions"][0]["name"]
    client.post(f"{API}/monitor/run")
    client.post(f"{API}/monitor/run")
    ev = client.get(f"{API}/events", params={"type": ["RISK_LIMIT"]}).json()["items"]
    assert len(ev) == 1 and ev[0]["title"] == "Close to a limit: Total open risk"
    ov = client.get(f"{API}/accounts/{account['id']}/overview").json()["risk"]
    assert ov["overall"] == "WARNING" and ov["realized_today"] == "0.0000"


def test_daily_loss_counts_positions_closed_today(client, account):
    p = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ",
                                              "legs": [stock("BUY", 10, "100")]}).json()
    client.post(f"{API}/positions/{p['id']}/close", json={"version": p["version"], "legs": [
        {"leg_id": p["legs"][0]["id"], "close_price": "90"}]})
    set_policy(client, account, max_daily_realized_loss="80", small_account_mode=False)
    d = client.get(f"{API}/accounts/{account['id']}/risk").json()
    assert d["realized_today"] == "-100.0000"
    c = next(x for x in d["utilization"]["checks"] if x["code"] == "DAILY_LOSS")
    assert c["result"] == "BLOCKED" and d["utilization"]["overall"] == "BLOCKED"
