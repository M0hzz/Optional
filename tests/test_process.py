"""Phase 4 tests: daily process score (a no-trade day can score 100), points that never depend on profit
(checked on the scoring code itself and on results), caps against trading or skipping more, reviews,
journal, objectives, rest days, streaks, levels, themes and achievements."""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

from conftest import opt
from sqlalchemy import update

from optlab.portfolio.db import Database
from optlab.portfolio.models import Position, RiskPolicy
from optlab.process import engine as pe

API = "/api/v1"


def today_et() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()


def set_policy(client, account, **kw):
    g = client.get(f"{API}/accounts/{account['id']}/risk-policy").json()
    assert client.put(f"{API}/accounts/{account['id']}/risk-policy", json={"settings": {**g["settings"], **kw}}).status_code == 200


def journal(client, account, kind, day=None, body="Market above its 20-day average; waiting for pullbacks."):
    r = client.post(f"{API}/journal", json={"account_id": account["id"], "kind": kind, "body": body,
                                            **({"entry_date": day.isoformat()} if day else {})})
    assert r.status_code == 201, r.text
    return r.json()


def tick(client, account, code, day=None, done=True):
    r = client.post(f"{API}/accounts/{account['id']}/objectives/{code}", json={"done": done, **({"day": day.isoformat()} if day else {})})
    assert r.status_code == 200, r.text
    return r.json()


def complete_day(client, account, day=None):
    journal(client, account, "daily_thesis", day)
    journal(client, account, "eod_review", day)
    tick(client, account, "CHECK_DATA", day)
    return tick(client, account, "REVIEW_EVENTS", day)


def plan_body(account, **kw):
    b = {"account_id": account["id"], "symbol": "XYZ", "thesis": "Support at 95", "direction": "bullish",
         "intended_strategy": "PUT_CREDIT_SPREAD", "trigger": "Bounce", "invalidation": "Close below 92",
         "planned_max_loss": "450", "planned_contracts": 1, "expiration_from": "2026-12-01", "expiration_to": "2027-01-31"}
    b.update(kw)
    return b


def armed_plan(client, account, **kw):
    p = client.post(f"{API}/plans", json=plan_body(account, **kw)).json()
    r = client.post(f"{API}/plans/{p['id']}/transition", json={"version": p["version"], "to": "ARMED"})
    assert r.status_code == 200, r.text
    return r.json()


def qualify(client, plan):
    r = client.post(f"{API}/plans/{plan['id']}/transition", json={"version": plan["version"], "to": "QUALIFIED"})
    assert r.status_code == 200, r.text
    return r.json()


def convert(client, plan, legs=None):
    r = client.post(f"{API}/plans/{plan['id']}/convert", json={"version": plan["version"], "legs": legs or [
        opt("SELL", "PUT", 95, "1.10"), opt("BUY", "PUT", 90, "0.15")]})
    assert r.status_code == 200, r.text
    return r.json()["position"]


def progress(client, account):
    return client.get(f"{API}/accounts/{account['id']}/progress").json()


# ------------------------------------------------------------------ the acceptance checks
def test_a_day_with_no_trades_can_score_full_marks(client, account):
    set_policy(client, account, max_daily_realized_loss="100")
    sc = complete_day(client, account)
    items = {i["code"]: i["state"] for i in sc["items"]}
    assert sc["score"] == 100 and sc["met"] == sc["applicable"] == 5
    assert items["REVIEW_POSITIONS"] == items["REVIEW_EXPIRATIONS"] == items["INVALIDATION_BEFORE_ENTRY"] == "NOT_APPLICABLE"
    assert not any(i["group"] == "trade" for i in sc["items"])
    p = progress(client, account)
    codes = {a["code"] for a in p["recent_awards"]}
    assert {"DAILY_THESIS", "EOD_REVIEW", "ALL_OBJECTIVES"} <= codes


def _code_without_docs(module) -> str:
    """The module's code with every docstring and comment removed (AST round trip)."""
    import ast

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree).lower()


def test_scoring_code_never_reads_profit_or_loss():
    code = _code_without_docs(pe)
    code = code.replace("max_daily_realized_loss", "").replace("maximum daily realized loss", "")   # a policy setting, not P&L
    for word in ("pnl", "realized", "profit", "net_premium", "exit_value", "quantity", "fill_price"):
        assert word not in code, word


def test_profit_or_loss_changes_nothing(client, account, db_url):
    plan = qualify(client, armed_plan(client, account))
    pos = convert(client, plan)
    r = client.post(f"{API}/positions/{pos['id']}/close", json={"version": pos["version"], "legs": [
        {"leg_id": lg["id"], "close_price": "3.00" if lg["side"] == "SELL" else "0.10"} for lg in pos["legs"]]})
    assert r.status_code == 200
    review = client.put(f"{API}/positions/{pos['id']}/review", json={
        "followed_plan": "yes", "exit_reason": "stop", "exit_followed_plan": "yes", "lessons": "Stop worked.", "complete": True})
    assert review.status_code == 200, review.text
    before = progress(client, account)
    score_before = client.get(f"{API}/accounts/{account['id']}/process-score").json()
    db = Database(db_url)
    with db.session() as s:                                  # turn a loss into a huge win
        s.execute(update(Position).where(Position.id == pos["id"]).values(realized_pnl_units=10**12))
    db.dispose()
    after = progress(client, account)
    assert after["points"] == before["points"] and after["level"] == before["level"]
    assert client.get(f"{API}/accounts/{account['id']}/process-score").json() == score_before


# ------------------------------------------------------------------ points
def test_planned_entry_earns_process_points_once(client, account):
    plan = qualify(client, armed_plan(client, account))
    convert(client, plan)
    p1 = progress(client, account)
    codes = {a["code"] for a in p1["recent_awards"]}
    assert {"PLAN_BEFORE_ENTRY", "MAX_LOSS_DEFINED", "WAITED_CONFIRMATION", "WITHIN_RISK"} <= codes
    assert progress(client, account)["points"] == p1["points"]           # idempotent
    assert p1["points"]["by_category"]["discipline"] == 20 and p1["points"]["by_category"]["risk_control"] >= 20


def test_unplanned_and_rule_breaking_trades_earn_nothing(client, account):
    set_policy(client, account, max_loss_per_position="100", small_account_mode=False)
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ", "acknowledge_rule_violation": True,
                                              "legs": [opt("SELL", "PUT", 95, "2.00"), opt("BUY", "PUT", 90, "1.00")]})
    assert r.status_code == 201
    p = progress(client, account)
    assert not [a for a in p["recent_awards"] if a["code"] in pe.POSITION_CODES]
    sc = p["today"]
    trade = {i["code"]: i["state"] for i in sc["items"] if i["group"] == "trade"}
    assert trade["PLANNED"] == "VIOLATED" and trade["WITHIN_POLICY"] == "VIOLATED"


def test_more_trades_in_a_day_never_earn_more(client, account):
    for _ in range(5):
        convert(client, qualify(client, armed_plan(client, account)))
    p = progress(client, account)
    plan_awards = [a for a in p["recent_awards"] if a["code"] == "PLAN_BEFORE_ENTRY"]
    assert len(plan_awards) == pe.DAILY_CAP


def test_skipping_an_armed_setup_earns_points_but_skipping_a_draft_doesnt(client, account):
    a = armed_plan(client, account)
    client.post(f"{API}/plans/{a['id']}/transition", json={"version": a["version"], "to": "SKIPPED", "reason": "No volume"})
    d = client.post(f"{API}/plans", json=plan_body(account)).json()
    client.post(f"{API}/plans/{d['id']}/transition", json={"version": d["version"], "to": "SKIPPED", "reason": "x"})
    skips = [x for x in progress(client, account)["recent_awards"] if x["code"] == "SKIPPED_SETUP"]
    assert len(skips) == 1 and "XYZ" in skips[0]["description"]


# ------------------------------------------------------------------ reviews
def test_reviews_rules_and_honest_record(client, account):
    set_policy(client, account, max_loss_per_position="100", small_account_mode=False)
    pos = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ", "acknowledge_rule_violation": True,
                                                "legs": [opt("SELL", "PUT", 95, "2.00"), opt("BUY", "PUT", 90, "1.00")]}).json()
    assert client.put(f"{API}/positions/{pos['id']}/review", json={"lessons": "x"}).status_code == 409    # still open
    pos = client.post(f"{API}/positions/{pos['id']}/close", json={"version": pos["version"], "legs": [
        {"leg_id": lg["id"], "close_price": "1.00"} for lg in pos["legs"]]}).json()
    pend = client.get(f"{API}/accounts/{account['id']}/reviews/pending").json()
    assert pend[0]["position_id"] == pos["id"] and pend[0]["rule_violation"]
    draft = client.put(f"{API}/positions/{pos['id']}/review", json={"went_wrong": "Too big"}).json()
    assert draft["completed_at"] is None and draft["followed_plan"] == "no_plan"
    inc = client.put(f"{API}/positions/{pos['id']}/review", json={"version": draft["version"], "complete": True})
    assert inc.status_code == 422 and {f["field"] for f in inc.json()["error"]["field_errors"]} >= {"exit_reason", "lessons",
                                                                                                    "rule_violation_acknowledged"}
    done = client.put(f"{API}/positions/{pos['id']}/review", json={
        "version": draft["version"], "exit_reason": "discretionary", "exit_followed_plan": "no_plan", "lessons": "Size down.",
        "mistakes": ["oversized", "no_plan"], "rule_violation_acknowledged": True, "complete": True}).json()
    assert done["completed_at"] and done["mistakes"] == ["no_plan", "oversized"]
    codes = {a["code"] for a in progress(client, account)["recent_awards"]}
    assert {"REVIEW_DONE", "HONEST_REVIEW"} <= codes and "FOLLOWED_EXIT" not in codes
    assert client.get(f"{API}/accounts/{account['id']}/reviews/pending").json() == []
    rep = client.get(f"{API}/positions/{pos['id']}/report").json()
    assert rep["review"]["lessons"] == "Size down." and "never counts" in rep["pnl_note"] and rep["points"]
    ach = {a["code"]: a for a in progress(client, account)["achievements"]}
    assert ach["HONEST_RECORD"]["earned_at"] is not None


# ------------------------------------------------------------------ objectives, rest days, journal
def test_objectives_validation_and_rest_day(client, account):
    r = client.post(f"{API}/accounts/{account['id']}/objectives/DAILY_THESIS", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "AUTOMATIC_OBJECTIVE"
    fut = client.post(f"{API}/accounts/{account['id']}/objectives/CHECK_DATA", json={"day": (today_et() + timedelta(days=1)).isoformat()})
    assert fut.status_code == 422
    rest = tick(client, account, "REST_DAY")
    assert rest["rest_day"] and rest["score"] is None and all(i["state"] == "NOT_APPLICABLE" for i in rest["items"])


def test_today_shows_pending_not_failed(client, account):
    sc = client.get(f"{API}/accounts/{account['id']}/process-score").json()
    assert sc["in_progress"] and {i["state"] for i in sc["items"] if i["code"] == "CHECK_DATA"} == {"TO_DO"}


def test_journal_edit_history_and_dates(client, account):
    e = journal(client, account, "note", body="First")
    up = client.patch(f"{API}/journal/{e['id']}", json={"version": e["version"], "body": "Second"}).json()
    assert up["body"] == "Second" and up["version"] == e["version"] + 1
    assert client.patch(f"{API}/journal/{e['id']}", json={"version": e["version"], "body": "x"}).status_code == 409
    fut = client.post(f"{API}/journal", json={"account_id": account["id"], "body": "x",
                                              "entry_date": (today_et() + timedelta(days=2)).isoformat()})
    assert fut.status_code == 422
    lst = client.get(f"{API}/journal", params={"account_id": account["id"]}).json()
    assert lst[0]["body"] == "Second"
    client.post(f"{API}/journal/{e['id']}/archive", json={"version": up["version"]})
    assert client.get(f"{API}/journal", params={"account_id": account["id"]}).json() == []


# ------------------------------------------------------------------ streaks, levels, themes, achievements
def test_streak_counts_scored_weekdays_and_rest_days_dont_break_it(client, account, db_url):
    set_policy(client, account, max_daily_realized_loss="100")
    db = Database(db_url)
    with db.session() as s:                                   # the policy and account existed a week ago
        s.execute(update(RiskPolicy).values(created_at=datetime.now(timezone.utc) - timedelta(days=8)))
        from optlab.portfolio.models import Account

        s.execute(update(Account).values(created_at=datetime.now(timezone.utc) - timedelta(days=8)))
    db.dispose()
    t = today_et()
    days = [t - timedelta(days=i) for i in range(1, 8)]
    weekdays = [d for d in days if d.weekday() < 5]
    for d in weekdays[1:]:
        complete_day(client, account, d)
    tick(client, account, "REST_DAY", weekdays[0])            # yesterday-ish: a rest day
    p = progress(client, account)
    assert p["streak"]["current"] == len(weekdays) - 1 and p["streak"]["best"] >= len(weekdays) - 1


def test_levels_and_theme_unlocks(client, account):
    assert pe.level_for(0)["level"] == 1 and pe.level_for(49)["level"] == 1 and pe.level_for(50)["level"] == 2
    assert pe.level_for(150)["level"] == 3 and pe.level_for(150)["colony_name"] == "Settlement"
    r = client.put(f"{API}/preferences", json={"theme": "solar", "active_account_id": account["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "THEME_LOCKED"
    assert client.put(f"{API}/preferences", json={"theme": "default"}).json()["theme"] == "default"
    lay = client.put(f"{API}/preferences", json={"dashboard_layout": ["positions", "overview"], "hidden_widgets": ["progress"]}).json()
    assert lay["dashboard_layout"] == ["positions", "overview"] and lay["hidden_widgets"] == ["progress"]


def test_first_plan_achievement_and_progress_payload(client, account):
    armed_plan(client, account)
    p = progress(client, account)
    ach = {a["code"]: a for a in p["achievements"]}
    assert ach["FIRST_PLAN"]["earned_at"] and ach["REVIEWS_TEN"]["earned_at"] is None and ach["REVIEWS_TEN"]["goal"] == 10
    assert p["level"]["level"] >= 1 and {t["id"] for t in p["themes"]} == set(pe.THEMES)
    assert not any(a["name"].lower().startswith(("most", "largest", "biggest")) for a in p["achievements"])
