"""API integration tests: accounts, positions lifecycle, idempotency, conflicts, CSV, quotes, isolation."""
from __future__ import annotations

import threading
from decimal import Decimal as D

import pytest
from conftest import EXP, EXP2, FakeProvider, opt, stock

API = "/api/v1"
IC = [opt("BUY", "PUT", 90, "0.50", fees="0.65"), opt("SELL", "PUT", 95, "1.50", fees="0.65"),
      opt("SELL", "CALL", 105, "1.50", fees="0.65"), opt("BUY", "CALL", 110, "0.50", fees="0.65")]


def make(client, account, legs, **kw):
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ", "legs": legs, **kw})
    assert r.status_code == 201, r.json()
    return r.json()


# ------------------------------------------------------------------ system
def test_health_openapi_and_structured_404(client):
    h = client.get(f"{API}/health").json()
    assert h["status"] == "ok" and h["database"]["revision"] == h["database"]["head"] == "0001"
    assert h["database"]["journal_mode"] == "wal"
    spec = client.get(f"{API}/openapi.json").json()
    assert "/api/v1/positions/analyze" in spec["paths"] and "/api/v1/positions/{position_id}/close" in spec["paths"]
    r = client.get(f"{API}/positions/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND" and r.headers["x-request-id"]


def test_unhandled_errors_never_leak_internals(client, monkeypatch):
    from optlab.portfolio import service

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /home/user/x.py")
    monkeypatch.setattr(service, "list_accounts", boom)
    r = client.get(f"{API}/accounts")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in r.text and "Traceback" not in r.text


def test_cors_is_restricted(client, monkeypatch):
    r = client.options(f"{API}/accounts", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers
    ok = client.options(f"{API}/accounts", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    from optlab.api.main import create_app

    monkeypatch.setenv("OPTLAB_CORS_ORIGINS", "*")
    with pytest.raises(ValueError):
        create_app(web_dist=None, migrate=False, lock=False)


# ------------------------------------------------------------------ accounts
def test_account_rules(client, account):
    acc = client.get(f"{API}/accounts/{account['id']}").json()
    assert acc["is_default"] and acc["latest_snapshot"]["buying_power"] == "1000.0000"
    r = client.post(f"{API}/accounts/{account['id']}/snapshots", json={"cash": 10.5})            # binary float
    assert r.status_code == 422 and "binary float" in r.json()["error"]["field_errors"][0]["message"]
    p = make(client, account, [opt("BUY", "CALL", 100, "2.50")])
    r = client.delete(f"{API}/accounts/{account['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ACCOUNT_NOT_EMPTY"
    r = client.post(f"{API}/accounts/{account['id']}/archive", json={"version": acc["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ACCOUNT_HAS_OPEN_POSITIONS"
    empty = client.post(f"{API}/accounts", json={"name": "Spare"}).json()
    assert not empty["is_default"]
    assert client.delete(f"{API}/accounts/{empty['id']}").status_code == 204
    _ = p


def test_overview_counts_entry_risk_without_fabricating(client, account):
    make(client, account, IC)
    make(client, account, [opt("SELL", "CALL", 100, "2.50")], acknowledge_risk=True)           # unbounded
    o = client.get(f"{API}/accounts/{account['id']}/overview").json()
    assert o["open_positions"] == 2 and o["unbounded_risk_positions"] == 1
    assert o["capital_at_risk"] == "302.6000" and o["capital_at_risk_complete"] is False
    assert o["upcoming_expirations"][0]["expiration"] == EXP.isoformat()


# ------------------------------------------------------------------ positions
def test_analyze_persists_nothing_and_reports_quotes(client, account):
    r = client.post(f"{API}/positions/analyze", json={"underlying": "XYZ", "legs": IC, "account_id": account["id"]})
    a = r.json()
    assert r.status_code == 200 and a["strategy"] == "IRON_CONDOR" and a["confidence"] == "exact"
    assert a["max_loss"] == {"state": "bounded", "value": "302.6000"} and a["breakevens"] == ["93.0260", "106.9740"]   # fees narrow the range
    assert a["buying_power_effect"]["value"] == "302.6000"
    q = a["quotes"]
    assert q["freshness"] == "DELAYED" and q["delay_minutes"] == 15 and q["greeks"] is not None
    assert all(leg["status"] == "FOUND" for leg in q["legs"])
    assert client.get(f"{API}/positions").json()["total"] == 0


def test_create_requires_acknowledgement_for_unbounded_or_unknown_risk(client, account):
    body = {"account_id": account["id"], "underlying": "XYZ", "legs": [opt("SELL", "CALL", 100, "2.50")]}
    r = client.post(f"{API}/positions", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "RISK_ACK_REQUIRED"
    unknown = {**body, "legs": [{**opt("BUY", "CALL", 100, "1"), "fill_price": None}]}
    assert client.post(f"{API}/positions", json=unknown).json()["error"]["details"]["max_loss_state"] == "unknown"
    p = make(client, account, body["legs"], acknowledge_risk=True)
    assert p["max_loss"] == {"state": "unbounded", "value": None} and p["risk_acknowledged"] and not p["defined_risk"]
    assert p["monitoring_enabled"] is False


def test_invalid_legs_give_field_errors(client, account):
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ",
                                                "legs": [{"asset_type": "OPTION", "side": "BUY", "quantity": 1}]})
    err = r.json()["error"]
    assert r.status_code == 422 and err["code"] == "INVALID_LEGS"
    assert {f["code"] for f in err["field_errors"]} >= {"MISSING_RIGHT", "MISSING_STRIKE", "MISSING_EXPIRATION"}
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ",
                                                "legs": [{**opt("BUY", "CALL", 100, "2.5"), "quantity": 1.5}]})
    assert r.status_code == 422 and r.json()["error"]["field_errors"][0]["field"] == "legs.0.quantity"
    r = client.post(f"{API}/positions", json={"account_id": account["id"], "underlying": "XYZ", "legs": IC,
                                                "surprise": 1})
    assert r.status_code == 422                                                   # unknown fields are rejected


def test_idempotent_create(client, account):
    body = {"account_id": account["id"], "underlying": "XYZ", "legs": IC}
    a = client.post(f"{API}/positions", json=body, headers={"Idempotency-Key": "abc"})
    b = client.post(f"{API}/positions", json=body, headers={"Idempotency-Key": "abc"})
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert b.headers["idempotent-replay"] == "true"
    assert client.get(f"{API}/positions").json()["total"] == 1
    c = client.post(f"{API}/positions", json={**body, "legs": IC[:2]}, headers={"Idempotency-Key": "abc"})
    assert c.status_code == 409 and c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_close_computes_exact_realized_pnl_and_archive_rules(client, account):
    p = make(client, account, IC)
    legs = {(lg["option_right"], lg["strike"]): lg["id"] for lg in p["legs"]}
    r = client.post(f"{API}/positions/{p['id']}/archive", json={"version": p["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CLOSE_FIRST"
    close = {"version": p["version"], "closed_at": "2026-11-20T20:00:00Z", "legs": [
        {"leg_id": legs[("PUT", "90.0000")], "expired_worthless": True},
        {"leg_id": legs[("PUT", "95.0000")], "close_price": "0.20", "close_fees": "0.65"},
        {"leg_id": legs[("CALL", "105.0000")], "close_price": "0.30", "close_fees": "0.65"},
        {"leg_id": legs[("CALL", "110.0000")], "expired_worthless": True}]}
    r = client.post(f"{API}/positions/{p['id']}/close", json={**close, "legs": close["legs"][:3]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_CLOSE"
    c = client.post(f"{API}/positions/{p['id']}/close", json=close).json()
    # credit 200 − buybacks 50 − opening fees 2.60 − closing fees 1.30
    assert c["lifecycle"] == "CLOSED" and c["realized_pnl"] == "146.1000"
    assert client.get(f"{API}/positions/{p['id']}/valuation").json()["status"] == "UNAVAILABLE"
    a = client.post(f"{API}/positions/{p['id']}/archive", json={"version": c["version"]}).json()
    assert a["lifecycle"] == "ARCHIVED" and a["realized_pnl"] == "146.1000"
    r = client.patch(f"{API}/positions/{p['id']}", json={"version": a["version"], "name": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "POSITION_ARCHIVED"
    trail = client.get(f"{API}/positions/{p['id']}/audit").json()
    assert [e["action"] for e in trail] == ["create", "close", "archive"]


def test_entered_in_error_archive_from_open(client, account):
    p = make(client, account, [opt("BUY", "CALL", 100, "2.50")])
    a = client.post(f"{API}/positions/{p['id']}/archive", json={"version": p["version"], "reason": "entered_in_error"})
    assert a.json()["lifecycle"] == "ARCHIVED" and a.json()["archive_reason"] == "entered_in_error"
    assert a.json()["realized_pnl"] is None


def test_version_conflicts_and_audited_corrections(client, account):
    p = make(client, account, [opt("BUY", "CALL", 100, "2.50")])
    ok = client.patch(f"{API}/positions/{p['id']}", json={"version": p["version"], "notes": "<script>x</script>\x00"})
    assert ok.json()["notes"] == "<script>x</script>" and ok.json()["version"] == p["version"] + 1  # stored as text
    stale = client.patch(f"{API}/positions/{p['id']}", json={"version": p["version"], "notes": "again"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    edited = client.patch(f"{API}/positions/{p['id']}", json={
        "version": ok.json()["version"], "legs": [opt("BUY", "CALL", 100, "2.40")]}).json()
    assert edited["net_premium"] == "240.0000"
    lid = edited["legs"][0]["id"]
    closed = client.post(f"{API}/positions/{p['id']}/close", json={
        "version": edited["version"], "legs": [{"leg_id": lid, "close_price": "3.00"}]}).json()
    assert closed["realized_pnl"] == "60.0000"
    r = client.patch(f"{API}/positions/{p['id']}", json={"version": closed["version"], "cash_secured": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "CORRECTION_REASON_REQUIRED"
    trail = client.get(f"{API}/positions/{p['id']}/audit").json()
    edit = [e for e in trail if e["action"] == "edit" and e["before"]["legs"][0]["fill_price_units"] == 25000
            and e["after"]["legs"][0]["fill_price_units"] != 25000]
    assert edit and edit[0]["after"]["legs"][0]["fill_price_units"] == 24000        # original fill kept


def test_list_filters_sort_and_pagination(client, account):
    for k in (95, 100, 105):
        make(client, account, [opt("BUY", "CALL", k, "1.00")], name=f"c{k}")
    make(client, account, IC)
    page = client.get(f"{API}/positions", params={"page_size": 2, "page": 2, "sort": "name"}).json()
    assert page["total"] == 4 and [p["name"] for p in page["items"]] == ["c95", "XYZ iron condor"]   # case-insensitive
    assert client.get(f"{API}/positions", params={"strategy": "IRON_CONDOR"}).json()["total"] == 1
    assert client.get(f"{API}/positions", params={"lifecycle": "CLOSED"}).json()["total"] == 0
    assert client.get(f"{API}/positions", params={"sort": "bogus"}).status_code == 422
    assert client.get(f"{API}/positions", params={"page_size": 500}).status_code == 422


def test_covered_call_and_cash_secured_put_need_their_cover(client, account):
    cc = make(client, account, [stock("BUY", 100, "98"), opt("SELL", "CALL", 105, "1.00")])
    assert cc["strategy"] == "COVERED_CALL" and cc["defined_risk"]
    csp = make(client, account, [opt("SELL", "PUT", 95, "1.00")], cash_secured=True)
    assert csp["strategy"] == "CASH_SECURED_PUT" and csp["max_loss"]["value"] == "9400.0000"
    a = client.post(f"{API}/positions/analyze", json={"underlying": "XYZ", "legs": [opt("SELL", "PUT", 95, "1.00")],
                                                       "account_id": account["id"], "cash_secured": True,
                                                       "include_quotes": False}).json()
    assert "CASH_SECURED_UNVERIFIED" in [w["code"] for w in a["warnings"]]        # $1,000 can't secure a $95 put


# ------------------------------------------------------------------ quotes and valuation
def test_valuation_is_an_exact_labelled_estimate(client, account):
    p = make(client, account, [opt("BUY", "CALL", 100, "2.50", fees="0.65")])
    v = client.get(f"{API}/positions/{p['id']}/valuation").json()
    assert v["label"] == "ESTIMATED" and v["freshness"] == "DELAYED" and v["provider"] == "cboe (OpenBB)"
    assert v["status"] == "AVAILABLE" and v["fetched_at"] and v["quote_age_seconds"] is not None
    mark = D(v["legs"][0]["mark"])
    assert D(v["value"]) == mark * 100 and D(v["open_pnl"]) == mark * 100 - D("250") - D("0.65")
    assert v["greeks"]["delta"] == pytest.approx(50.0)


def test_missing_contract_is_unavailable_with_suggestions(client, account):
    p = make(client, account, [opt("BUY", "CALL", 101, "1.00")])
    v = client.get(f"{API}/positions/{p['id']}/valuation").json()
    assert v["status"] == "UNAVAILABLE" and v["value"] is None and v["open_pnl"] is None
    leg = v["legs"][0]
    assert leg["status"] == "NOT_FOUND" and "strike 100" in leg["nearest"]
    assert v["reasons"] and v["greeks"]["delta"] is None


def test_partial_quotes_never_give_a_partial_value(client, account):
    p = make(client, account, [opt("BUY", "CALL", 100, "3.00"), opt("SELL", "CALL", 101, "1.00")])
    v = client.get(f"{API}/positions/{p['id']}/valuation").json()
    assert v["status"] == "PARTIAL" and v["value"] is None


def test_provider_outage_serves_cached_or_unavailable(provider, quotes):
    snap = quotes.chain("XYZ")
    assert snap.freshness(quotes.max_age) == "DELAYED" and snap.source_timestamp is not None
    provider.fail = True
    again = quotes.chain("XYZ", force=True)
    assert again.freshness(quotes.max_age) == "CACHED" and "refresh failed" in again.error
    assert again.fetched_at == snap.fetched_at
    none = quotes.chain("ABC")
    assert none.freshness(quotes.max_age) == "UNAVAILABLE" and none.rows is None
    q, _ = quotes.contract("ABC", EXP, "CALL", D(100))
    assert q.status == "UNAVAILABLE" and q.mid is None


def test_chain_snapshots_are_shared_and_single_flight(provider, quotes):
    import time

    real = provider.fetch

    def slow(symbol):
        time.sleep(0.2)
        return real(symbol)
    provider.fetch = slow
    ts = [threading.Thread(target=quotes.chain, args=("XYZ",)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    quotes.contract("XYZ", EXP, "PUT", D(95))
    quotes.contract("XYZ", EXP2, "CALL", D(110))
    assert provider.calls == 1


def test_quote_quality_flags():
    from optlab.portfolio.quotes import QuoteService

    from conftest import fixture_chain

    ch = fixture_chain()
    ch.loc[(ch["strike"] == 100) & (ch["type"] == "call"), ["bid", "ask"]] = [2.0, 1.5]      # crossed
    ch.loc[(ch["strike"] == 95) & (ch["type"] == "call"), ["delta", "iv"]] = [float("nan"), 0.0]
    qs = QuoteService(FakeProvider(ch), mode="live")
    crossed, _ = qs.contract("XYZ", EXP, "CALL", D(100))
    assert crossed.mid is None and "crossed market (bid above ask)" in crossed.quality
    nog, _ = qs.contract("XYZ", EXP, "CALL", D(95))
    assert nog.delta is None and "missing Greeks" in nog.quality and nog.mid is not None


def test_synthetic_data_is_isolated_from_live_mode():
    from optlab.portfolio.quotes import QuoteService, SyntheticProvider

    with pytest.raises(ValueError, match="synthetic provider"):
        QuoteService(SyntheticProvider(), mode="live")
    with pytest.raises(ValueError):
        QuoteService(FakeProvider(), mode="synthetic")
    s = QuoteService(SyntheticProvider(), mode="synthetic").chain("SPY")
    assert s.freshness(300) == "SYNTHETIC"


def test_portfolio_positions_are_invisible_to_the_monitor(client, account):
    make(client, account, IC)
    from optlab.config import PROJECT_ROOT
    from optlab.live.scanner import load_scan_config

    cfg = load_scan_config(PROJECT_ROOT / "scans.yaml")
    assert all(p.get("symbol") != "XYZ" for p in cfg["positions"])               # the monitor reads scans.yaml only


# ------------------------------------------------------------------ watched contracts and preferences
def test_watched_contracts(client, account):
    body = {"underlying": "xyz", "option_right": "PUT", "strike": "95", "expiration": EXP.isoformat(),
            "target_entry": "1.20", "invalidation": "close below 92"}
    research = client.post(f"{API}/watched-contracts", json=body).json()               # account-independent
    assert research["account_id"] is None and research["occ_symbol"] == "XYZ261218P00095000"
    mine = client.post(f"{API}/watched-contracts", json={**body, "account_id": account["id"], "strike": "100"}).json()
    q = client.get(f"{API}/watched-contracts/{mine['id']}/quote").json()
    assert q["status"] == "FOUND" and q["freshness"] == "DELAYED" and q["mid"] is not None
    upd = client.patch(f"{API}/watched-contracts/{mine['id']}", json={"version": mine["version"],
                                                                         "status": "ENTRY_QUALIFIED"}).json()
    assert upd["status"] == "ENTRY_QUALIFIED"
    assert client.patch(f"{API}/watched-contracts/{mine['id']}", json={"version": mine["version"],
                                                                        "notes": "x"}).status_code == 409
    old = client.post(f"{API}/watched-contracts", json={**body, "expiration": "2025-01-17"}).json()
    assert old["status"] == "EXPIRED"
    arch = client.post(f"{API}/watched-contracts/{research['id']}/archive", json={"version": research["version"]}).json()
    assert arch["status"] == "ARCHIVED"
    lst = client.get(f"{API}/watched-contracts").json()
    assert {c["id"] for c in lst["items"]} == {mine["id"], old["id"]}
    bad = client.post(f"{API}/watched-contracts", json={**body, "strike": "95.00005"})
    assert bad.status_code == 422


def test_preferences(client, account):
    p = client.get(f"{API}/preferences").json()
    assert p == {"terminology": "professional", "timezone": "America/New_York", "active_account_id": None}
    p = client.put(f"{API}/preferences", json={"terminology": "colony", "active_account_id": account["id"]}).json()
    assert p["terminology"] == "colony" and p["active_account_id"] == account["id"]
    assert client.put(f"{API}/preferences", json={"terminology": "pirate"}).status_code == 422
    assert client.put(f"{API}/preferences", json={"timezone": "Mars/Base"}).status_code == 422


# ------------------------------------------------------------------ CSV
HEADER = "position_ref,underlying,asset_type,side,quantity,option_right,strike,expiration,fill_price,fees,opened_at\n"


def _ic_rows(ref="IC-1", opened="2026-11-02T15:00:00Z"):
    return "".join(f"{ref},XYZ,OPTION,{s},1,{r},{k},{EXP.isoformat()},{px},0.65,{opened}\n"
                   for s, r, k, px in (("BUY", "PUT", 90, "0.50"), ("SELL", "PUT", 95, "1.50"),
                                       ("SELL", "CALL", 105, "1.50"), ("BUY", "CALL", 110, "0.50")))


def _upload(client, path, content, account, **params):
    return client.post(f"{API}/imports/positions/{path}", params={"account_id": account["id"], **params},
                       files={"file": ("p.csv", content, "text/csv")})


def test_csv_preview_then_commit(client, account):
    content = HEADER + _ic_rows() + "LC-1,XYZ,OPTION,BUY,2,CALL,100,2026-12-18,2.50,1.30,\n"
    pv = _upload(client, "preview", content, account).json()
    assert pv["ok"] and [g["strategy"] for g in pv["positions"]] == ["Iron condor", "Long call"]
    assert client.get(f"{API}/positions").json()["total"] == 0                      # preview writes nothing
    r = _upload(client, "commit", content, account)
    assert r.status_code == 200 and len(r.json()["imported"]) == 2
    assert {p["source"] for p in r.json()["imported"]} == {"csv"}
    again = _upload(client, "preview", content, account).json()                      # duplicate of what's stored
    assert not again["ok"] and "Already in the database" in again["positions"][0]["errors"][0]


def test_csv_invalid_rows_and_all_or_nothing(client, account):
    bad = (HEADER + _ic_rows("IC-1") + "BAD,XYZ,OPTION,BUY,1,CALL,100,2026-12-18,1.123456,0,\n"
           + "DUP,XYZ,OPTION,BUY,1,CALL,100,2026-12-18,1.00,0,\nDUP,XYZ,OPTION,BUY,1,CALL,100,2026-12-18,1.00,0,\n")
    pv = _upload(client, "preview", bad, account).json()
    by_ref = {g["position_ref"]: g for g in pv["positions"]}
    assert by_ref["IC-1"]["valid"] and not by_ref["BAD"]["valid"] and not by_ref["DUP"]["valid"]
    assert any("more than 4 decimal places" in e for r in pv["rows"] for e in r["errors"])
    assert any("duplicates row" in e for e in by_ref["DUP"]["errors"])
    r = _upload(client, "commit", bad, account)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IMPORT_INVALID"
    assert client.get(f"{API}/positions").json()["total"] == 0                       # nothing written
    r = _upload(client, "commit", bad, account, mode="valid_only")
    assert [p["name"] for p in r.json()["imported"]] == ["XYZ iron condor"]           # never a partial position


def test_csv_import_is_atomic_when_a_write_fails(client, account, monkeypatch):
    from optlab.portfolio import service

    real, calls = service.create_position, {"n": 0}

    def flaky(s, data, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full (simulated)")
        return real(s, data, *a, **k)
    monkeypatch.setattr(service, "create_position", flaky)
    content = HEADER + _ic_rows("A") + _ic_rows("B", "2026-11-03T15:00:00Z")
    r = _upload(client, "commit", content, account)
    assert r.status_code == 500
    assert client.get(f"{API}/positions").json()["total"] == 0                       # the first one rolled back too


def test_csv_file_level_validation_and_idempotency(client, account):
    assert not _upload(client, "preview", "a,b\n1,2\n", account).json()["ok"]
    assert _upload(client, "preview", b"\xff\xfe\x00bad", account).json()["file_errors"][0].startswith("The file must")
    big = HEADER + "x" * 1_100_000
    assert _upload(client, "preview", big, account).status_code == 413
    content = HEADER + _ic_rows()
    a = client.post(f"{API}/imports/positions/commit", params={"account_id": account["id"]},
                    files={"file": ("p.csv", content, "text/csv")}, headers={"Idempotency-Key": "imp-1"})
    b = client.post(f"{API}/imports/positions/commit", params={"account_id": account["id"]},
                    files={"file": ("p.csv", content, "text/csv")}, headers={"Idempotency-Key": "imp-1"})
    assert a.status_code == 200 and b.headers["idempotent-replay"] == "true"
    assert client.get(f"{API}/positions").json()["total"] == 1


def test_csv_export_roundtrip_and_formula_escaping(client, account):
    make(client, account, IC, name="=HYPERLINK(\"http://x\")", notes="@SUM(1)")
    out = client.get(f"{API}/positions/export.csv").text
    assert "'=HYPERLINK" in out and out.splitlines()[0].startswith("position_ref,position_name,underlying")
    # the export (minus the escaped name/notes) re-imports as an exact duplicate
    rows = out.splitlines()
    pv = _upload(client, "preview", "\n".join(rows) + "\n", account).json()
    assert pv["positions"][0]["duplicate_of"] is not None

