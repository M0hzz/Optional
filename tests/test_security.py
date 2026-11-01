"""Phase 7 security checks: DNS-rebinding host allowlist, cross-site origin refusal, body limits, security
headers, no stack traces in responses, safe static files, expression evaluation that can't hang, and secrets
kept out of URLs and error text."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from optlab.api.security import host_ok

API = "/api/v1"


@pytest.fixture
def app_client(db_url, quotes, tmp_path):
    from optlab.api.main import create_app
    from optlab.portfolio.db import Database

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>t</title>")
    (tmp_path / "secret.txt").write_text("top secret")
    app = create_app(Database(db_url), quotes=quotes, web_dist=dist, monitor=False, news=False,
                     allowed_hosts=["localhost", "127.0.0.1", "::1", "*.ts.net"])
    with TestClient(app, base_url="http://127.0.0.1:8600", raise_server_exceptions=False) as c:
        yield c


def test_host_allowlist_matching():
    allowed = ["localhost", "127.0.0.1", "::1", "*.ts.net"]
    assert host_ok("127.0.0.1:8600", allowed) and host_ok("[::1]:8600", allowed) and host_ok("LOCALHOST", allowed)
    assert host_ok("mybox.tail1234.ts.net", allowed) and host_ok("mybox.tail1234.ts.net:443", allowed)
    for bad in ("evil.example", "ts.net", "evilts.net", "ts.net.evil.com", "127.0.0.2", ""):
        assert not host_ok(bad, allowed), bad
    assert host_ok("anything", ["*"])


def test_dns_rebinding_hosts_are_refused(app_client):
    r = app_client.get(f"{API}/accounts", headers={"Host": "evil.example"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "HOST_NOT_ALLOWED"
    assert app_client.get(f"{API}/health", headers={"Host": "mybox.tail1234.ts.net"}).status_code == 200


def test_cross_site_changes_are_refused_and_same_site_ones_allowed(app_client):
    ok = {"name": "Main"}
    assert app_client.post(f"{API}/accounts", json=ok, headers={"Origin": "http://127.0.0.1:8600"}).status_code == 201
    assert app_client.post(f"{API}/accounts", json=ok).status_code == 201                      # curl / scripts
    for h in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
              {"Origin": "http://localhost.evil.example"}):
        r = app_client.post(f"{API}/accounts", json=ok, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] in ("ORIGIN_NOT_ALLOWED", "CROSS_SITE_REQUEST"), h
    # through tailscale serve: the browser's origin is the ts.net name, the API sees a loopback Host
    assert app_client.post(f"{API}/accounts", json=ok, headers={"Origin": "https://mybox.tail1234.ts.net"}).status_code == 201
    assert app_client.post(f"{API}/accounts", json=ok, headers={"Host": "mybox.tail1234.ts.net",
                                                                "Origin": "https://mybox.tail1234.ts.net"}).status_code == 201
    assert app_client.post(f"{API}/accounts", json=ok, headers={"Host": "mybox.tail1234.ts.net",
                                                                "Origin": "https://other.tail9999.ts.net"}).status_code == 403
    assert app_client.get(f"{API}/accounts", headers={"Origin": "https://evil.example"}).status_code == 200  # reads: CORS decides
    r = app_client.post(f"{API}/accounts", content='{"name": "x"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 422                                          # simple (no-preflight) bodies aren't accepted


def test_cors_never_allows_arbitrary_origins(app_client):
    r = app_client.options(f"{API}/accounts", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                                                       "Access-Control-Request-Headers": "content-type"})
    assert "access-control-allow-origin" not in r.headers


def test_oversized_bodies_are_refused_without_reading_them(app_client):
    r = app_client.post(f"{API}/journal", content=b'{"x":"' + b"a" * 3_000_000 + b'"}', headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "BODY_TOO_LARGE"

    def chunks():
        for _ in range(40):
            yield b"a" * 100_000
    r = app_client.post(f"{API}/journal", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 413)


def test_security_headers(app_client):
    page = app_client.get("/")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    for r in (page, app_client.get(f"{API}/health")):
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer" and "camera=()" in r.headers["permissions-policy"]


def test_static_files_cant_escape_the_build_folder(app_client):
    for p in ("/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
        r = app_client.get(p)
        assert "top secret" not in r.text, p


def test_errors_never_include_stack_traces(app_client, monkeypatch):
    import optlab.portfolio.service as svc

    def boom(*a, **k):
        raise RuntimeError("database password=hunter2 exploded")
    monkeypatch.setattr(svc, "list_accounts", boom)
    r = app_client.get(f"{API}/accounts")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in r.text and "Traceback" not in r.text and r.json()["error"]["request_id"]


def test_expressions_cant_hang_the_server():
    from optlab.live.expr import Evaluator, ExprError, parse

    ev = Evaluator({"close": 2.0}, {})
    t = time.time()
    assert ev("2 ** 2 ** 40 > 1") is True and ev("9 ** 9 ** 9 > 0") is True
    assert ev("(-8) ** 0.5") != ev("(-8) ** 0.5")                         # NaN, not a complex number
    assert time.time() - t < 1
    for bad in ("close.__class__", "[x for x in range(9)]", "lambda: 1", "close if close else 1", "x[0]"):
        with pytest.raises(ExprError):
            parse(bad)
    with pytest.raises(ExprError):
        ev("__import__('os')")                     # a call parses, but only the study functions can be called


def test_rule_endpoints_reject_hanging_and_unsafe_conditions(client):
    r = client.post(f"{API}/monitor/validate", json={"condition": "__import__('os').system('id')", "target": "symbol"})
    assert r.status_code == 200 and not r.json()["valid"]


def test_secrets_stay_out_of_urls_and_messages():
    from optlab.news.http import FetchError, http_get

    try:
        http_get("http://127.0.0.1:9/x?token=secret-123", {"User-Agent": "t"}, timeout=0.5)
    except FetchError as exc:
        assert "secret-123" not in str(exc)
