"""Phase 2 API tests: manual pass, monitor status and settings, positions carrying their monitor status,
the event feed and acknowledgements, alert rules, per-position rules, snapshots and timeline, the stale
overlay, scans.yaml import, the YAML monitor skip, and the live stream."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from conftest import opt
from sqlalchemy import update

from optlab.portfolio.models import MonitorState

API = "/api/v1"


def make(client, account, legs, **kw):
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ", "legs": legs, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def put_spread(client, account):
    return make(client, account, [opt("SELL", "PUT", 95, "2.00"), opt("BUY", "PUT", 90, "1.00")])


def set_rules(client, pid, version, exit_when=(), watch_when=(), action_when=()):
    r = client.put(f"{API}/positions/{pid}/monitor", json={"version": version, "rules": {
        "exit_when": list(exit_when), "watch_when": list(watch_when), "action_when": list(action_when)}})
    assert r.status_code == 200, r.text
    return r.json()


def test_new_position_is_stale_until_checked_then_has_a_status(client, account):
    p = put_spread(client, account)
    assert p["monitor"]["status"] == "STALE_DATA" and "No monitoring pass" in p["monitor"]["reasons"][0]
    run = client.post(f"{API}/monitor/run").json()
    assert run["ok"] and run["positions"] == 1
    got = client.get(f"{API}/positions/{p['id']}").json()["monitor"]
    assert got["label"] in ("HOLD", "WATCH") and got["snapshot"]["open_pnl"] == "31.0000"
    assert got["snapshot"]["freshness"] == "DELAYED" and got["snapshot"]["label"] == "ESTIMATED"
    lst = client.get(f"{API}/positions", params={"account_id": account["id"]}).json()["items"]
    assert lst[0]["monitor"]["status"] == got["status"]


def test_monitor_status_and_overview(client, account):
    put_spread(client, account)
    client.post(f"{API}/monitor/run")
    st = client.get(f"{API}/monitor/status").json()
    assert st["last_run"]["ok"] and st["runner"]["state"] == "off" and "every 60 s" in st["schedule"]
    ov = client.get(f"{API}/accounts/{account['id']}/overview").json()["monitoring"]
    assert ov["open_pnl"] == "31.0000" and ov["open_pnl_complete"] and ov["greeks"]["delta"] == 10.0


def test_stale_overlay_when_the_last_check_is_old(client, account, db_url):
    from optlab.portfolio.db import Database

    p = put_spread(client, account)
    client.post(f"{API}/monitor/run")
    db = Database(db_url)
    with db.session() as s:
        s.execute(update(MonitorState).where(MonitorState.subject_id == p["id"])
                  .values(checked_at=datetime.now(timezone.utc) - timedelta(hours=3)))
    db.dispose()
    m = client.get(f"{API}/positions/{p['id']}").json()["monitor"]
    assert m["status"] == "STALE_DATA" and m["stale_overlay"] and "Last checked 3.0 h ago" in m["reasons"][0]
    assert m["stored_status"] in ("HOLD", "WATCH")


def test_position_rules_events_ack_and_timeline(client, account):
    p = put_spread(client, account)
    m = set_rules(client, p["id"], p["version"], exit_when=["pnl_pct >= 20"])
    assert m["rules_config"]["use_defaults"] is False
    client.post(f"{API}/monitor/run")
    ev = client.get(f"{API}/events", params={"position_id": p["id"], "unacknowledged": True}).json()
    assert ev["total"] == 1
    e = ev["items"][0]
    assert e["type"] == "POSITION_STATUS_CHANGED" and e["severity"] == "critical" and "pnl_pct >= 20" in e["message"]
    acked = client.post(f"{API}/events/{e['id']}/acknowledge", json={"note": "closing tomorrow"}).json()
    assert acked["acknowledged_at"] and acked["ack_note"] == "closing tomorrow"
    assert client.get(f"{API}/events", params={"unacknowledged": True}).json()["total"] == 0
    tl = client.get(f"{API}/positions/{p['id']}/timeline").json()
    kinds = [(x["kind"], x.get("event", {}).get("type") or x.get("audit", {}).get("action")) for x in tl]
    assert ("event", "POSITION_STATUS_CHANGED") in kinds and ("event", "POSITION_OPENED") in kinds
    assert ("audit", "monitor") in kinds and ("audit", "create") in kinds
    snaps = client.get(f"{API}/positions/{p['id']}/snapshots").json()
    assert len(snaps) == 1 and snaps[0]["status"] == "EXIT_CONDITION_MET"


def test_bulk_acknowledge(client, account):
    for _ in range(2):
        p = put_spread(client, account)
        set_rules(client, p["id"], p["version"], exit_when=["pnl_pct >= 20"])
    client.post(f"{API}/monitor/run")
    seq = client.get(f"{API}/events").json()["latest_seq"]
    assert client.post(f"{API}/events/acknowledge", json={"up_to_seq": seq}).json() == {"acknowledged": 2}
    assert client.post(f"{API}/events/acknowledge", json={}).status_code == 422


def test_rule_validation_and_version_conflicts(client, account):
    p = put_spread(client, account)
    r = client.put(f"{API}/positions/{p['id']}/monitor", json={"version": p["version"],
                                                                "rules": {"exit_when": ["pnl_pc >= 5"]}})
    assert r.status_code == 422 and r.json()["error"]["field_errors"][0]["field"] == "exit_when[0]"
    assert "did you mean pnl_pct" in r.json()["error"]["field_errors"][0]["message"]
    r = client.put(f"{API}/positions/{p['id']}/monitor", json={"version": p["version"] + 5, "notify": False})
    assert r.status_code == 409
    off = client.put(f"{API}/positions/{p['id']}/monitor", json={"version": p["version"], "monitoring_enabled": False})
    assert off.json()["status"] == "NOT_MONITORED"
    assert client.post(f"{API}/monitor/run").json()["positions"] == 0
    v = client.post(f"{API}/monitor/validate", json={"condition": "bid > 1", "target": "contract"}).json()
    assert v == {"valid": True, "error": None}


def test_alert_rules_crud(client, account):
    p = put_spread(client, account)
    bad = client.post(f"{API}/alert-rules", json={"name": "x", "target_type": "position", "position_id": p["id"],
                                                  "condition": "bid > 1"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "BAD_CONDITION"
    missing = client.post(f"{API}/alert-rules", json={"name": "x", "target_type": "symbol", "condition": "price > 1"})
    assert missing.status_code == 422
    r = client.post(f"{API}/alert-rules", json={"name": "Above 99", "target_type": "symbol", "symbol": "xyz",
                                               "condition": "price > 99", "severity": "critical"}).json()
    assert r["symbol"] == "XYZ" and r["account_id"] is None
    client.post(f"{API}/monitor/run")
    fired = client.get(f"{API}/events", params={"type": ["ALERT_TRIGGERED"]}).json()["items"]
    assert len(fired) == 1 and fired[0]["severity"] == "critical"
    lst = client.get(f"{API}/alert-rules").json()
    assert lst[0]["state"]["active"] is True
    up = client.patch(f"{API}/alert-rules/{r['id']}", json={"version": r["version"], "enabled": False}).json()
    assert up["enabled"] is False
    arch = client.post(f"{API}/alert-rules/{r['id']}/archive", json={"version": up["version"]})
    assert arch.status_code == 200 and client.get(f"{API}/alert-rules").json() == []


def test_monitor_settings_round_trip_and_validation(client):
    st = client.get(f"{API}/monitor/settings").json()
    st["confirm_passes"] = 1
    st["default_rules"]["exit_when"] = ["pnl_pct >= 40"]
    assert client.put(f"{API}/monitor/settings", json=st).json()["confirm_passes"] == 1
    st["default_rules"]["watch_when"] = ["nonsense_name > 3"]
    assert client.put(f"{API}/monitor/settings", json=st).status_code == 422
    st["default_rules"]["watch_when"] = []
    st["schedule"]["start"] = "25:00"
    assert client.put(f"{API}/monitor/settings", json=st).status_code == 422
    voc = client.get(f"{API}/monitor/vocabulary").json()
    assert any(x["name"] == "pnl_pct" for x in voc["targets"]["position"]) and "rsi" in voc["bar_functions"]


def test_manual_run_refused_while_another_process_holds_the_monitor(client, db_url):
    from optlab.monitoring.runner import MonitorLock

    lk = MonitorLock(db_url)
    assert lk.try_acquire()
    try:
        r = client.post(f"{API}/monitor/run")
        assert r.status_code == 409 and r.json()["error"]["code"] == "MONITOR_ELSEWHERE"
    finally:
        lk.release()


def test_scans_yaml_import_and_the_yaml_monitor_skips_it(client, account, tmp_path, monkeypatch, db_url):
    from optlab.monitoring import legacy

    f = tmp_path / "scans.yaml"
    f.write_text("""
position_defaults: {exit_when: [pnl_pct >= 60], watch_when: [dte <= 10]}
positions:
  - name: XYZ spread
    symbol: xyz
    legs:
      - {type: put, strike: 95, expiration: 2026-12-18, qty: -1, price: 2.00}
      - {type: put, strike: 90, expiration: 2026-12-18, qty: 1, price: 1.00}
  - name: Naked call
    symbol: XYZ
    legs: [{type: call, strike: 110, expiration: 2026-12-18, qty: -1, price: 0.5}]
  - name: Bad
    symbol: XYZ
    legs: [{type: put, strike: 95, expiration: 2026-12-18, qty: -1, price: 2.12345}]
""")
    monkeypatch.setattr(legacy, "SCANS_YAML", f)
    rows = client.post(f"{API}/imports/scans-yaml/preview", params={"account_id": account["id"]}).json()["rows"]
    assert [r["status"] for r in rows] == ["new", "new", "invalid"]
    assert rows[1]["needs_risk_ack"] and "more than 4 decimal places" in rows[2]["errors"][0]
    out = client.post(f"{API}/imports/scans-yaml/commit", params={"account_id": account["id"]}, json={},
                      headers={"Idempotency-Key": "imp-1"}).json()
    assert [r["status"] for r in out["rows"]] == ["imported", "skipped", "invalid"]
    pid = out["imported"][0]["id"]
    m = client.get(f"{API}/positions/{pid}/monitor").json()["rules_config"]
    assert m["rules"]["exit_when"] == ["pnl_pct >= 60"] and m["rules"]["watch_when"] == ["dte <= 10"]
    again = client.post(f"{API}/imports/scans-yaml/preview", params={"account_id": account["id"]}).json()["rows"]
    assert again[0]["status"] == "duplicate"
    keep, moved = legacy.split_imported(legacy.read_yaml(f)[0], db_url)
    assert moved == ["XYZ spread"] and [p["name"] for p in keep] == ["Naked call", "Bad"]
    out2 = client.post(f"{API}/imports/scans-yaml/commit", params={"account_id": account["id"]},
                       json={"acknowledge_risk": True}).json()
    assert [r["status"] for r in out2["rows"]] == ["duplicate", "imported", "invalid"]


def test_yaml_monitor_skip_is_empty_without_a_database(tmp_path):
    from optlab.monitoring.legacy import split_imported

    pos = [{"name": "a", "symbol": "SPY", "legs": []}]
    assert split_imported(pos, f"sqlite:///{tmp_path / 'none.sqlite'}") == (pos, [])


def test_stream_sends_new_events_and_resumes_from_last_event_id(client, account):
    p = put_spread(client, account)
    set_rules(client, p["id"], p["version"], exit_when=["pnl_pct >= 20"])
    with client.stream("GET", f"{API}/stream", params={"after": 0, "max_seconds": 0.6}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: hello" in body
    seqs = [int(line[4:]) for line in body.splitlines() if line.startswith("id: ")]
    assert len(seqs) >= 2                                   # POSITION_OPENED and MONITORING_CHANGED
    client.post(f"{API}/monitor/run")
    with client.stream("GET", f"{API}/stream", params={"max_seconds": 0.6},
                       headers={"Last-Event-ID": str(seqs[-1])}) as r:
        body = "".join(r.iter_text())
    data = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ") and '"seq"' in line]
    assert [d["type"] for d in data] == ["POSITION_STATUS_CHANGED"]      # nothing before the Last-Event-ID


def test_contract_monitor_and_snapshots(client, account):
    c = client.post(f"{API}/watched-contracts", json={"account_id": account["id"], "underlying": "XYZ",
                                                     "option_right": "PUT", "strike": "95", "expiration": "2026-12-18",
                                                     "alert_conditions": "bid > 1\noi >= 100"}).json()
    client.post(f"{API}/monitor/run")
    m = client.get(f"{API}/watched-contracts/{c['id']}/monitor").json()
    assert [x["result"] for x in m["conditions"]] == [True, True] and m["automated"] is False
    snaps = client.get(f"{API}/watched-contracts/{c['id']}/snapshots").json()
    assert len(snaps) == 1 and snaps[0]["bid"] is not None
    bad = client.patch(f"{API}/watched-contracts/{c['id']}", json={"version": c["version"], "alert_conditions": "x >"})
    assert bad.status_code == 422
