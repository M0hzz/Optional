"""Phase 4 completion: persisted, versioned score snapshots; the append-only points ledger with revocations,
corrections and replacements; explicit UNKNOWN handling; source-change handling; review policy results;
idempotency; time-zone boundaries; terminology independence; and P&L independence."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import opt
from sqlalchemy import func, select, update
from test_process import armed_plan, complete_day, convert, journal, progress, qualify, set_policy, today_et

from optlab.portfolio.db import Database, sqlite_path
from optlab.portfolio.models import Account, DailyScore, Position, ProcessAward, UserAchievement
from optlab.process import engine as pe

API = "/api/v1"
TZ = "America/New_York"


# ------------------------------------------------------------------ helpers
def backdate_account(db_url, days=10):
    db = Database(db_url)
    with db.session() as s:
        s.execute(update(Account).values(created_at=datetime.now(timezone.utc) - timedelta(days=days)))
    db.dispose()


def rows(db_url, model, **where):
    db = Database(db_url)
    with db.session() as s:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        out = [{c.name: getattr(r, c.name) for c in model.__table__.columns} for r in s.scalars(q)]
    db.dispose()
    return out


def ledger(client, account, **params):
    return client.get(f"{API}/accounts/{account['id']}/ledger", params=params).json()


def revisions(client, account, day):
    return client.get(f"{API}/accounts/{account['id']}/scores/{day.isoformat()}").json()


def item(score, code, subject=None):
    return next(i for i in score["items"] if i["code"] == code and (subject is None or i["subject_id"] == subject))


def close(client, pos, prices):
    r = client.post(f"{API}/positions/{pos['id']}/close", json={"version": pos["version"], "legs": [
        {"leg_id": lg["id"], "close_price": prices[lg["side"]]} for lg in pos["legs"]]})
    assert r.status_code == 200, r.text
    return r.json()


def breaking_position(client, account):
    """A position recorded against the policy (max loss 100, the spread risks 400), with the override acknowledged."""
    set_policy(client, account, max_loss_per_position="100", small_account_mode=False)
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ", "acknowledge_rule_violation": True,
                                              "legs": [opt("SELL", "PUT", 95, "2.00"), opt("BUY", "PUT", 90, "1.00")]})
    assert r.status_code == 201, r.text
    return r.json()


def snap(**kw):
    """A minimal score snapshot for the pure evaluator."""
    base = {"version": "process-score/v1", "day": "2026-11-02", "rest_day": False,
            "manual": {c: True for c in sorted(pe.MANUAL)}, "open_position_ids": [], "expiring_position_ids": [],
            "daily_thesis_ids": ["j1"], "eod_review_ids": ["j2"], "policy": {"id": "p", "revision": 1, "daily_loss_limit": "100"},
            "entries": [], "reviews_due": [], "display": {}}
    base.update(kw)
    return base


def entry(pid="pos1", risk="PASS", imported=False, planned=True):
    return {"position_id": pid, "plan_id": "plan1" if planned else None, "planned_before_entry": planned,
            "plan_has_invalidation": planned, "plan_max_loss_defined": planned, "entry_risk": risk, "imported": imported,
            "converted_from": "QUALIFIED" if planned else None}


# ------------------------------------------------------------------ idempotency
def test_replaying_the_sync_and_loading_progress_repeatedly_never_duplicates_points(client, account, db_url):
    convert(client, qualify(client, armed_plan(client, account)))
    first = progress(client, account)
    n = len(rows(db_url, ProcessAward))
    for _ in range(4):                                             # load progress again and again
        again = progress(client, account)
        assert again["points"] == first["points"] and again["new"]["ledger"] == {"award": 0, "revocation": 0, "replacement": 0}
    db = Database(db_url)
    with db.session() as s:                                        # replay the sync directly (reprocessing the same events)
        for _ in range(3):
            res = pe.refresh(s, account["id"], TZ)
            assert res["ledger"] == {"award": 0, "revocation": 0, "replacement": 0}
            assert res["days"] == {"stored": 0, "revised": 0} and res["achievements"] == []
    db.dispose()
    assert len(rows(db_url, ProcessAward)) == n


def test_backfill_is_idempotent_and_flagged(client, account, db_url):
    backdate_account(db_url, 6)
    t = today_et()
    d = t - timedelta(days=4)
    journal(client, account, "daily_thesis", d)
    progress(client, account)
    scores = rows(db_url, DailyScore)
    assert {r["account_date"] for r in scores} == {t - timedelta(days=i) for i in range(1, 7)}      # every finished day
    assert all(r["revision"] == 1 for r in scores)
    assert next(r for r in scores if r["account_date"] == d)["backfill"] is True                 # calculated days later
    award = next(r for r in rows(db_url, ProcessAward) if r["code"] == "DAILY_THESIS")
    assert award["backfill"] is True and award["day"] == d and award["timezone"] == TZ
    for _ in range(3):
        progress(client, account)
    assert len(rows(db_url, DailyScore)) == len(scores) and len(rows(db_url, ProcessAward)) == len(rows(db_url, ProcessAward))
    assert sorted(r["id"] for r in rows(db_url, DailyScore)) == sorted(r["id"] for r in scores)


# ------------------------------------------------------------------ immutability
def test_ledger_scores_and_achievements_are_append_only_in_the_database(client, account, db_url):
    backdate_account(db_url, 3)
    armed_plan(client, account)
    journal(client, account, "daily_thesis")
    progress(client, account)
    con = sqlite3.connect(sqlite_path(db_url))
    for table in ("process_awards", "daily_scores", "user_achievements"):
        assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, table
        with pytest.raises(sqlite3.IntegrityError, match="append-only|immutable"):
            con.execute(f"UPDATE {table} SET account_id = account_id")
        with pytest.raises(sqlite3.IntegrityError, match="append-only|immutable"):
            con.execute(f"DELETE FROM {table}")
    con.close()


# ------------------------------------------------------------------ source changes: archive, restore, edits
def test_archiving_scored_evidence_revokes_with_a_reason_and_revises_the_day(client, account, db_url):
    backdate_account(db_url, 3)
    y = today_et() - timedelta(days=1)
    e = journal(client, account, "daily_thesis", y)
    p0 = progress(client, account)
    [rev1] = revisions(client, account, y)
    assert item(rev1, "DAILY_THESIS")["state"] == "MET" and rev1["points"] == 5 and e["id"] in rev1["source_ids"]

    client.post(f"{API}/journal/{e['id']}/archive", json={"version": e["version"]})
    led = ledger(client, account, subject_id=y.isoformat())["entries"]
    rev = next(x for x in led if x["entry_type"] == "revocation")
    award = next(x for x in led if x["entry_type"] == "award")
    assert rev["references_id"] == award["id"] and rev["points"] == -5 and "archived" in rev["reason"] and e["id"] in rev["reason"]
    assert rev["actor"] == "system:process-sync" and rev["audit_id"] and rev["evidence"]["original_evidence"] == award["evidence"]
    revs = revisions(client, account, y)
    assert [r["revision"] for r in revs] == [1, 2]
    assert revs[0] == rev1                                                    # the original revision is untouched
    assert revs[1]["supersedes_id"] == rev1["id"] and item(revs[1], "DAILY_THESIS")["state"] == "VIOLATED"
    assert "Write a daily market thesis: MET → VIOLATED" in revs[1]["correction_reason"] and "points for the day 5 → 0" in revs[1]["correction_reason"]
    p1 = progress(client, account)
    assert p1["points"]["effective"] == p0["points"]["effective"] - 5
    assert p1["points"]["lifetime_awarded"] == p0["points"]["lifetime_awarded"] and p1["points"]["revoked_or_corrected"] == -5
    from optlab.portfolio.models import AuditEvent

    [a] = rows(db_url, AuditEvent, id=rev["audit_id"])                     # the linked audit record
    assert a["entity_type"] == "process_award" and a["entity_id"] == rev["id"] and a["action"] == "revocation"
    assert a["reason"] == rev["reason"] and a["after_json"]["references"] == award["id"]
    assert [x["action"] for x in rows(db_url, AuditEvent, entity_type="journal", entity_id=e["id"])] == ["archive"]


def test_restoring_evidence_is_deterministic(client, account, db_url):
    backdate_account(db_url, 3)
    y = today_et() - timedelta(days=1)
    e = journal(client, account, "daily_thesis", y)
    base = progress(client, account)["points"]["effective"]
    e = client.post(f"{API}/journal/{e['id']}/archive", json={"version": e["version"]}).json()
    e = client.post(f"{API}/journal/{e['id']}/restore", json={"version": e["version"]}).json()
    assert e["archived_at"] is None
    led = sorted(ledger(client, account, subject_id=y.isoformat())["entries"], key=lambda x: x["awarded_at"])
    assert [x["entry_type"] for x in led if x["code"] == "DAILY_THESIS"] == ["award", "revocation", "replacement"]
    repl = led[-1] if led[-1]["code"] == "DAILY_THESIS" else next(x for x in led if x["entry_type"] == "replacement")
    assert repl["references_id"] == next(x["id"] for x in led if x["entry_type"] == "revocation") and repl["points"] == 5
    assert progress(client, account)["points"]["effective"] == base
    revs = revisions(client, account, y)
    assert [r["revision"] for r in revs] == [1, 2, 3] and item(revs[2], "DAILY_THESIS")["state"] == "MET"
    assert revs[2]["supersedes_id"] == revs[1]["id"]
    assert pe.evidence_key(revs[2]["snapshot"]) == pe.evidence_key(revs[0]["snapshot"])     # same evidence as the original
    n = len(rows(db_url, ProcessAward))
    progress(client, account)
    assert len(rows(db_url, ProcessAward)) == n and len(revisions(client, account, y)) == 3
    assert client.post(f"{API}/journal/{e['id']}/restore", json={"version": e["version"]}).status_code == 409


def test_ordinary_note_edits_dont_create_revisions_or_ledger_entries(client, account, db_url):
    backdate_account(db_url, 3)
    y = today_et() - timedelta(days=1)
    e = journal(client, account, "daily_thesis", y)
    progress(client, account)
    n = len(rows(db_url, ProcessAward))
    r = client.patch(f"{API}/journal/{e['id']}", json={"version": e["version"], "body": "Reworded thesis", "tags": ["spx"]})
    assert r.status_code == 200
    progress(client, account)
    assert len(rows(db_url, ProcessAward)) == n and len(revisions(client, account, y)) == 1


def test_archiving_a_position_as_entered_in_error_revokes_its_points(client, account):
    pos = convert(client, qualify(client, armed_plan(client, account)))
    progress(client, account)
    r = client.post(f"{API}/positions/{pos['id']}/archive", json={"version": pos["version"], "reason": "entered_in_error"})
    assert r.status_code == 200, r.text
    led = ledger(client, account, subject_id=pos["id"])["entries"]
    awards = [x for x in led if x["entry_type"] == "award"]
    revs = [x for x in led if x["entry_type"] == "revocation"]
    assert awards and len(revs) == len(awards) and all("entered in error" in x["reason"] for x in revs)
    assert sum(x["points"] for x in led) == 0


def test_editing_a_review_doesnt_duplicate_its_award_and_keeps_previous_versions(client, account):
    pos = convert(client, qualify(client, armed_plan(client, account)))
    close(client, pos, {"SELL": "0.20", "BUY": "0.05"})
    r1 = client.put(f"{API}/positions/{pos['id']}/review", json={
        "followed_plan": "yes", "exit_reason": "target", "exit_followed_plan": "yes", "lessons": "First lesson", "complete": True}).json()
    progress(client, account)
    r2 = client.put(f"{API}/positions/{pos['id']}/review", json={
        "version": r1["version"], "followed_plan": "yes", "exit_reason": "target", "exit_followed_plan": "yes",
        "lessons": "Better wording of the lesson", "complete": True})
    assert r2.status_code == 200, r2.text
    progress(client, account)
    led = ledger(client, account, subject_id=r1["id"])["entries"]
    assert sorted(x["code"] for x in led) == ["FOLLOWED_EXIT", "REVIEW_DONE"] and all(x["entry_type"] == "award" for x in led)
    hist = client.get(f"{API}/positions/{pos['id']}/review/history").json()
    assert [h["action"] for h in hist] == ["create", "edit"]
    assert hist[1]["before"]["lessons"] == "First lesson" and hist[1]["after"]["lessons"] == "Better wording of the lesson"
    # changing a scored answer revokes only that award, with the reason
    r3 = client.put(f"{API}/positions/{pos['id']}/review", json={
        "version": r2.json()["version"], "followed_plan": "yes", "exit_reason": "discretionary", "exit_followed_plan": "no",
        "lessons": "Better wording of the lesson", "complete": True})
    assert r3.status_code == 200
    led = ledger(client, account, subject_id=r1["id"])["entries"]
    rev = [x for x in led if x["entry_type"] == "revocation"]
    assert len(rev) == 1 and rev[0]["code"] == "FOLLOWED_EXIT" and "'no'" in rev[0]["reason"]


# ------------------------------------------------------------------ manual corrections and revocations
def test_manual_revocations_and_corrections_stay_visible(client, account, db_url):
    convert(client, qualify(client, armed_plan(client, account)))
    p = progress(client, account)
    award = next(a for a in p["recent_awards"] if a["code"] == "WITHIN_RISK")
    url = f"{API}/accounts/{account['id']}/ledger/{award['id']}"
    assert client.post(f"{url}/revoke", json={"reason": ""}).status_code == 422
    rv = client.post(f"{url}/revoke", json={"reason": "Duplicate: the same fill was imported twice"})
    assert rv.status_code == 201 and rv.json()["actor"] == "user:local" and rv.json()["points"] == -award["points"]
    assert client.post(f"{url}/revoke", json={"reason": "again"}).status_code == 409
    for _ in range(2):                                     # the sync never undoes a person's revocation
        progress(client, account)
    led = ledger(client, account, subject_id=award["subject_id"])
    mine = [x for x in led["entries"] if x["code"] == "WITHIN_RISK"]
    assert sorted(x["entry_type"] for x in mine) == ["award", "revocation"]
    plan_award = next(a for a in p["recent_awards"] if a["code"] == "PLAN_BEFORE_ENTRY")
    c = client.post(f"{API}/accounts/{account['id']}/ledger/{plan_award['id']}/correct",
                    json={"reason": "Policy says 5 for this case", "points": -5})
    assert c.status_code == 201 and c.json()["entry_type"] == "correction" and c.json()["references_id"] == plan_award["id"]
    assert client.post(f"{API}/accounts/{account['id']}/ledger/{plan_award['id']}/correct",
                       json={"reason": "too much", "points": -50}).status_code == 422
    s = ledger(client, account)["summary"]
    assert s["revoked_or_corrected"] == -award["points"] - 5
    assert s["effective"] == s["lifetime_awarded"] + s["revoked_or_corrected"]
    assert s["entries"]["revocation"] == 1 and s["entries"]["correction"] == 1
    assert progress(client, account)["points"]["effective"] == s["effective"]
    assert {r["id"] for r in rows(db_url, ProcessAward)} >= {award["id"], plan_award["id"]}       # originals kept


# ------------------------------------------------------------------ achievements
def test_achievements_are_awarded_once_with_evidence_and_rule_version(client, account, db_url):
    plan = armed_plan(client, account)
    for _ in range(3):
        progress(client, account)
    armed_plan(client, account)
    progress(client, account)
    got = [a for a in rows(db_url, UserAchievement) if a["code"] == "FIRST_PLAN"]
    assert len(got) == 1
    assert got[0]["rule_version"] == pe.ACHIEVEMENT_VERSION == "achievement-rules/v1"
    assert plan["id"] in got[0]["detail_json"]["armed_plan_ids"] and got[0]["detail_json"]["goal"] == 1
    ach = next(a for a in progress(client, account)["achievements"] if a["code"] == "FIRST_PLAN")
    assert ach["rule_version"] == "achievement-rules/v1" and plan["id"] in ach["evidence"]["armed_plan_ids"]


# ------------------------------------------------------------------ versions and reproducibility
def test_changing_the_scoring_version_never_modifies_old_scores(client, account, db_url, monkeypatch):
    backdate_account(db_url, 3)
    y = today_et() - timedelta(days=1)
    e = journal(client, account, "daily_thesis", y)
    progress(client, account)
    [old] = revisions(client, account, y)
    monkeypatch.setattr(pe, "SCORE_VERSION", "v2")
    client.post(f"{API}/journal/{e['id']}/archive", json={"version": e["version"]})        # evidence changes under v2
    progress(client, account)
    assert revisions(client, account, y) == [old] and old["version"] == "process-score/v1"


def test_recalculating_identical_inputs_gives_identical_results(client, account, db_url):
    backdate_account(db_url, 4)
    journal(client, account, "daily_thesis", today_et() - timedelta(days=2))
    convert(client, qualify(client, armed_plan(client, account)))
    progress(client, account)
    for r in rows(db_url, DailyScore):
        ev = pe.evaluate(r["input_snapshot"])
        assert ev["components"] == r["components_json"] and (ev["numerator"], ev["denominator"], ev["percentage"]) == \
            (r["numerator"], r["denominator"], r["percentage"])
    s1 = pe.evaluate(snap(entries=[entry()]))
    assert pe.evaluate(pe.snapshot_copy(snap(entries=[entry()]))) == s1


def test_versions_are_reported(client, account):
    p = progress(client, account)
    assert p["versions"] == {"process_score": "process-score/v1", "points_policy": "points-policy/v1",
                             "achievement_rules": "achievement-rules/v1"}
    assert all(a["rule_version"] == "points-policy/v1" for a in p["recent_awards"])


# ------------------------------------------------------------------ UNKNOWN and NOT_APPLICABLE
def test_unknown_never_earns_credit_and_is_distinct_from_not_applicable():
    known = pe.evaluate(snap(entries=[entry(risk="PASS")]))
    unknown = pe.evaluate(snap(entries=[entry(risk="UNKNOWN")]))
    missing = pe.evaluate(snap(entries=[entry(risk=None)]))
    imported = pe.evaluate(snap(entries=[entry(risk=None, imported=True)]))
    st = {name: next(c for c in ev["components"] if c["code"] == "WITHIN_POLICY")
          for name, ev in (("known", known), ("unknown", unknown), ("missing", missing), ("imported", imported))}
    assert st["known"]["state"] == "MET"
    assert st["unknown"]["state"] == st["missing"]["state"] == "UNKNOWN"
    assert st["imported"]["state"] == "NOT_APPLICABLE"
    assert unknown["numerator"] == known["numerator"] - 1 and unknown["denominator"] == known["denominator"]   # counted, no credit
    assert imported["denominator"] == known["denominator"] - 1 and imported["numerator"] == known["numerator"] - 1   # excluded
    assert unknown["percentage"] < 100 and imported["percentage"] == 100 and unknown["unknown"] == 1 and imported["unknown"] == 0
    assert "missing data" in st["unknown"]["detail"] and "no risk check" in st["missing"]["detail"]


def test_to_do_is_today_only_and_never_stored(client, account, db_url):
    backdate_account(db_url, 3)
    today = client.get(f"{API}/accounts/{account['id']}/process-score").json()
    assert item(today, "CHECK_DATA")["state"] == "TO_DO" and today["in_progress"]
    progress(client, account)
    for r in rows(db_url, DailyScore):
        assert all(c["state"] in ("MET", "VIOLATED", "UNKNOWN", "NOT_APPLICABLE") for c in r["components_json"])
    y = revisions(client, account, today_et() - timedelta(days=1))[0]
    assert item(y, "CHECK_DATA")["state"] == "VIOLATED"


# ------------------------------------------------------------------ reviews: policy results, unplanned
def test_review_lists_the_exact_policies_that_were_overridden(client, account):
    pos = breaking_position(client, account)
    pos = close(client, pos, {"SELL": "1.00", "BUY": "1.00"})
    rev = client.put(f"{API}/positions/{pos['id']}/review", json={
        "exit_reason": "discretionary", "exit_followed_plan": "no_plan", "lessons": "Size down.",
        "rule_violation_acknowledged": True, "complete": True}).json()
    pr = rev["policy_results"]
    assert pr["overall"] == "BLOCKED" and pr["override_recorded"] and pr["unplanned"] and pr["policy_revision"] is not None
    v = {c["code"]: c for c in pr["violated"]}
    assert "MAX_LOSS" in v
    m = v["MAX_LOSS"]
    assert m["name"] == "Maximum loss per position" and m["result"] == "BLOCKED" and m["value"] and m["limit"]
    assert m["explanation"] and m["override_recorded"] is True and m["policy_revision"] == pr["policy_revision"]
    assert all(set(c) >= {"code", "name", "result", "value", "limit", "explanation", "policy_revision", "override_recorded"}
               for c in pr["checks"])
    assert {c["result"] for c in pr["checks"]} <= {"PASS", "WARNING", "BLOCKED", "UNKNOWN"}
    assert "MAX_LOSS" in rev["acknowledged_rules"]
    rep = client.get(f"{API}/positions/{pos['id']}/report").json()
    assert rep["policy_results"]["violated"][0]["code"] in v and rep["review"]["policy_results"] == pr


def test_unplanned_positions_are_identified(client, account):
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ",
                                              "legs": [opt("SELL", "PUT", 95, "1.10"), opt("BUY", "PUT", 90, "0.15")]})
    pos = r.json()
    planned = convert(client, qualify(client, armed_plan(client, account)))
    sc = progress(client, account)["today"]
    assert item(sc, "PLANNED", pos["id"])["state"] == "VIOLATED"
    assert "unplanned position" in item(sc, "PLANNED", pos["id"])["detail"]
    assert item(sc, "PLANNED", planned["id"])["state"] == "MET"
    assert client.get(f"{API}/positions/{pos['id']}/policy-results").json()["unplanned"] is True
    assert client.get(f"{API}/positions/{planned['id']}/policy-results").json()["unplanned"] is False


# ------------------------------------------------------------------ time zones
def test_the_account_time_zone_controls_daily_and_weekly_boundaries(client, account, db_url):
    pos = convert(client, qualify(client, armed_plan(client, account)))
    opened = datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc)            # Sun 22:30 in New York, Mon 12:30 in Tokyo
    db = Database(db_url)
    with db.session() as s:
        s.execute(update(Position).where(Position.id == pos["id"]).values(opened_at=opened))
    with db.session() as s:
        now = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
        ny, tk = pe.load_facts(s, account["id"], "America/New_York", now), pe.load_facts(s, account["id"], "Asia/Tokyo", now)
        assert ny.today == date(2026, 11, 1) and tk.today == date(2026, 11, 2)
        assert [e["position_id"] for e in pe.day_snapshot(ny, date(2026, 11, 1))["entries"]] == [pos["id"]]
        assert pe.day_snapshot(ny, date(2026, 11, 2))["entries"] == []
        assert [e["position_id"] for e in pe.day_snapshot(tk, date(2026, 11, 2))["entries"]] == [pos["id"]]
        # weekly boundary: in New York the entry falls on Sunday (not a trading day: never breaks a streak),
        # in Tokyo on Monday (a new trading week)
        assert ny.today.weekday() == 6 and ny.today.weekday() not in ny.schedule_days
        assert tk.today.weekday() == 0 and tk.today.weekday() in tk.schedule_days
        assert pe.streaks(ny, {ny.today - timedelta(days=1): 0, ny.today: 0}, set())["current"] == 0
        cands_ny = {c.code: c.day for c in pe._candidates(ny, {})}
        cands_tk = {c.code: c.day for c in pe._candidates(tk, {})}
        assert cands_ny["PLAN_BEFORE_ENTRY"] == date(2026, 11, 1) and cands_tk["PLAN_BEFORE_ENTRY"] == date(2026, 11, 2)
    db.dispose()
    assert client.put(f"{API}/preferences", json={"timezone": "Asia/Tokyo"}).status_code == 200
    sc = client.get(f"{API}/accounts/{account['id']}/process-score").json()
    assert sc["timezone"] == "Asia/Tokyo"
    assert sc["day"] == datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()


# ------------------------------------------------------------------ terminology
def test_professional_and_colony_modes_give_identical_results_and_permissions(client, account):
    def run():
        return {"progress": {k: v for k, v in progress(client, account).items() if k != "new"},
                "score": client.get(f"{API}/accounts/{account['id']}/process-score").json(),
                "ledger": ledger(client, account),
                "auto": client.post(f"{API}/accounts/{account['id']}/objectives/DAILY_THESIS", json={}).status_code,
                "theme": client.put(f"{API}/preferences", json={"theme": "solar"}).status_code}
    convert(client, qualify(client, armed_plan(client, account)))
    journal(client, account, "daily_thesis")
    assert client.put(f"{API}/preferences", json={"terminology": "professional"}).status_code == 200
    pro = run()
    assert client.put(f"{API}/preferences", json={"terminology": "colony"}).status_code == 200
    colony = run()
    assert pro == colony and pro["auto"] == 422 and pro["theme"] == 409
    import inspect

    from optlab.api import process_routes
    from optlab.process import service as ps
    for mod in (pe, ps, process_routes):
        assert "terminology" not in inspect.getsource(mod)


# ------------------------------------------------------------------ process, never profit
def test_a_disciplined_losing_trade_outscores_a_rule_breaking_profitable_one(client, account):
    good = convert(client, qualify(client, armed_plan(client, account)))
    good = close(client, good, {"SELL": "4.00", "BUY": "0.05"})                  # a loss
    client.put(f"{API}/positions/{good['id']}/review", json={
        "followed_plan": "yes", "exit_reason": "stop", "exit_followed_plan": "yes", "lessons": "Stop held.", "complete": True})
    bad = breaking_position(client, account)
    bad = close(client, bad, {"SELL": "0.05", "BUY": "0.01"})                    # a profit
    client.put(f"{API}/positions/{bad['id']}/review", json={
        "exit_reason": "discretionary", "exit_followed_plan": "no_plan", "lessons": "Got lucky.", "complete": True,
        "rule_violation_acknowledged": True})
    g = client.get(f"{API}/positions/{good['id']}/report").json()
    b = client.get(f"{API}/positions/{bad['id']}/report").json()
    assert int(g["position"]["realized_pnl"].replace(".", "").replace("-", "")) and g["position"]["realized_pnl"].startswith("-")
    assert not b["position"]["realized_pnl"].startswith("-")
    assert g["points_net"] > b["points_net"]
    sc = progress(client, account)["today"]
    met = {pid: sum(1 for i in sc["items"] if i["subject_id"] == pid and i["state"] == "MET") for pid in (good["id"], bad["id"])}
    assert met[good["id"]] > met[bad["id"]]


def test_changing_only_pnl_changes_nothing_in_progress(client, account, db_url):
    backdate_account(db_url, 3)
    pos = convert(client, qualify(client, armed_plan(client, account)))
    close(client, pos, {"SELL": "3.00", "BUY": "0.10"})
    client.put(f"{API}/positions/{pos['id']}/review", json={
        "followed_plan": "yes", "exit_reason": "stop", "exit_followed_plan": "yes", "lessons": "Stop worked.", "complete": True})
    before = progress(client, account)
    n_scores, n_awards = len(rows(db_url, DailyScore)), len(rows(db_url, ProcessAward))
    db = Database(db_url)
    with db.session() as s:
        s.execute(update(Position).where(Position.id == pos["id"]).values(realized_pnl_units=10**12))
    db.dispose()
    after = progress(client, account)
    for k in ("points", "level", "streak", "achievements", "recent_awards", "today"):
        assert after[k] == before[k], k
    assert len(rows(db_url, DailyScore)) == n_scores and len(rows(db_url, ProcessAward)) == n_awards


def test_a_finished_no_trade_day_is_stored_at_100(client, account, db_url):
    backdate_account(db_url, 3)
    set_policy(client, account, max_daily_realized_loss="100")
    db = Database(db_url)
    from optlab.portfolio.models import RiskPolicy

    with db.session() as s:
        s.execute(update(RiskPolicy).values(created_at=datetime.now(timezone.utc) - timedelta(days=3)))
    db.dispose()
    y = today_et() - timedelta(days=1)
    complete_day(client, account, y)                      # each record added to the finished day is a new revision
    revs = revisions(client, account, y)
    r = revs[-1]
    assert r["score"] == 100 and r["met"] == r["applicable"] == 5 and r["unknown"] == 0
    assert [x["revision"] for x in revs] == list(range(1, len(revs) + 1)) and revs[0]["score"] < 100
    assert all(b["supersedes_id"] == a["id"] and b["correction_reason"] for a, b in zip(revs, revs[1:]))
    assert any(a["code"] == "ALL_OBJECTIVES" and a["subject_id"] == y.isoformat()
               for a in ledger(client, account)["entries"])


def test_skipped_setup_cap_is_enforced(client, account):
    for _ in range(5):
        a = armed_plan(client, account)
        client.post(f"{API}/plans/{a['id']}/transition", json={"version": a["version"], "to": "SKIPPED", "reason": "No volume"})
    skips = [x for x in ledger(client, account)["entries"] if x["code"] == "SKIPPED_SETUP"]
    if not skips:
        progress(client, account)
        skips = [x for x in ledger(client, account)["entries"] if x["code"] == "SKIPPED_SETUP"]
    assert len(skips) == pe.DAILY_CAP


def test_archiving_a_skipped_plan_keeps_its_points_with_an_explanation(client, account):
    a = armed_plan(client, account)
    a = client.post(f"{API}/plans/{a['id']}/transition", json={"version": a["version"], "to": "SKIPPED", "reason": "x"}).json()
    progress(client, account)
    r = client.post(f"{API}/plans/{a['id']}/transition", json={"version": a["version"], "to": "ARCHIVED"})
    assert r.status_code == 200, r.text
    progress(client, account)
    led = ledger(client, account, subject_id=a["id"])["entries"]
    assert [x["entry_type"] for x in led] == ["award"] and "archived after its outcome" in led[0]["note"]


# ------------------------------------------------------------------ migration 0005 keeps existing awards
def test_migration_0005_keeps_existing_awards_as_legacy_entries(db_url):
    from optlab.portfolio import db as dbm

    dbm.upgrade(db_url, "0004")
    from optlab.portfolio.models import UserPreference

    db = Database(db_url)
    with db.session() as s:                                  # these tables are the same at 0004 and 0005
        s.add(Account(id="a1", name="A"))
        s.add(UserPreference(key="timezone", value_json={"value": "Asia/Tokyo"}))
    db.dispose()
    con = sqlite3.connect(sqlite_path(db_url))
    con.execute("INSERT INTO process_awards (id, account_id, code, category, points, subject_type, subject_id, day, description, "
                "awarded_at) VALUES ('x1', 'a1', 'DAILY_THESIS', 'research', 5, 'day', '2026-10-02', '2026-10-02', 'd', "
                "'2026-10-02 12:00:00')")
    con.commit()
    con.close()
    assert dbm.upgrade(db_url, "0005") == "0005"
    db = Database(db_url)
    with db.session() as s:
        a = s.get(ProcessAward, "x1")
        assert a.entry_type == "award" and a.evidence_json == {"legacy": True} and a.timezone == "Asia/Tokyo"
        assert a.rule_version == "points-policy/v1" and s.scalar(select(func.count()).select_from(DailyScore)) == 0
    db.dispose()
