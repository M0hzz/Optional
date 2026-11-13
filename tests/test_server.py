"""The data side: CBOE chain parsing, levels from bars, the cache, and the three routes."""
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from optlab import market  # noqa: E402
from optlab.api import create_app, host_ok  # noqa: E402

TODAY = date(2026, 11, 10)


def opt(sym, bid, ask, last=0.0, when=None, **kw):
    return {"option": sym, "bid": bid, "ask": ask, "iv": 0.2, "open_interest": 10.0, "volume": 5.0, "delta": 0.4,
            "theta": -0.02, "last_trade_price": last, "last_trade_time": when, **kw}


PAYLOAD = {"timestamp": "2026-11-10 16:00:00", "data": {
    "symbol": "IWM", "current_price": 278.8, "prev_day_close": 282.5, "price_change": -3.7,
    "price_change_percent": -1.31, "iv30": 19.8, "options": [
        opt("B1IWM261117C00280000", 1.0, 1.1),                       # adjusted contract: left out
        opt("IWM261117C00280000", 2.9, 3.0, 2.95, "2026-11-10T15:59:00"),
        opt("IWM261117P00280000", 3.9, 4.1, delta=-0.6),
        opt("IWM261117C00285000", 0.0, 0.0, 0.5, "2026-11-09T10:00:00"),   # no quote: the last trade stands in
        opt("IWM261117C00290000", 0.0, 0.0),                        # never traded, no quote: no price
        opt("IWM261106C00280000", 1.0, 1.1),                         # already expired: left out
        opt("IWM261218C00280000", 6.0, 6.2)]}}


def bars(n=70, start=100.0):
    out = []
    for i in range(n):
        c = start + i * 0.5 + (3 if i % 7 == 0 else 0)
        out.append({"d": f"d{i}", "o": c - 0.2, "h": c + 1, "l": c - 1, "c": c, "v": 1000 + 10 * i})
    return out


def test_chain_parsing():
    c = market.parse_chain(PAYLOAD, TODAY)
    assert c["symbol"] == "IWM" and c["price"] == 278.8 and c["iv30"] == pytest.approx(0.198)
    assert [(e["date"], e["dte"]) for e in c["expirations"]] == [("2026-11-17", 7), ("2026-12-18", 38)]
    calls = c["expirations"][0]["calls"]
    assert [r["strike"] for r in calls] == [280.0, 285.0, 290.0]
    assert calls[0]["mid"] == 2.95 and calls[1]["mid"] == 0.5 and calls[2]["mid"] is None
    assert c["expirations"][0]["puts"][0]["delta"] == -0.6
    with pytest.raises(market.MarketError):
        market.parse_chain({"data": {"symbol": "X", "current_price": 5, "options": []}}, TODAY)


def test_symbols_and_fetch_retry():
    assert market.clean_symbol("brk-b") == "BRK.B" and market.clean_symbol("^spx") == "SPX"
    with pytest.raises(market.MarketError):
        market.clean_symbol("  ")

    class R:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return PAYLOAD
    seen = []

    def flaky(url):
        seen.append(url)
        return R(429 if len(seen) < 3 else 200)
    assert market.fetch_cboe("iwm", get=flaky, pause=0) is PAYLOAD and len(seen) == 3
    index = []

    def only_index(url):
        index.append(url)
        return R(200 if "/_SPX." in url else 403)
    assert market.fetch_cboe("SPX", get=only_index, pause=0) is PAYLOAD and len(index) == 2
    with pytest.raises(market.MarketError, match="Check the ticker"):
        market.fetch_cboe("NOPE", get=lambda url: R(403), pause=0)
    with pytest.raises(market.MarketError, match="busy"):
        market.fetch_cboe("IWM", get=lambda url: R(429), pause=0, retries=1)


def test_levels_follow_their_rules():
    b = bars()
    lv = market.levels(b, price=b[-1]["c"])
    assert lv["resistance"] == max(x["h"] for x in b[-21:-1]) and lv["support"] == max(
        min(x["l"] for x in b[-21:-1]), min(x["l"] for x in b[-61:-1]))
    assert lv["atr"] > 0 and 0 < lv["hv"] < 2 and lv["rel_volume"] == pytest.approx(b[-1]["v"] / lv["avg_volume"], abs=0.01)
    assert market.levels(b, price=999)["resistance"] is None and market.levels(b, price=1)["support"] is None


def test_cache_serves_the_last_good_answer_when_a_refresh_fails():
    now = [0.0]
    cache, calls = market.Cache(10, clock=lambda: now[0]), []

    def make():
        calls.append(1)
        if len(calls) == 2:
            raise market.MarketError("down")
        return len(calls)
    assert cache.get("a", make) == (1, False) and cache.get("a", make) == (1, False) and len(calls) == 1
    now[0] = 11
    assert cache.get("a", make) == (1, True)                 # refresh failed: stale, not an error
    assert cache.get("a", make) == (3, False)
    with pytest.raises(market.MarketError):
        market.Cache(10).get("b", lambda: (_ for _ in ()).throw(market.MarketError("down")))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(market, "date", type("D", (date,), {"today": classmethod(lambda cls: TODAY)}))
    app = create_app(fetch_chain=lambda s: PAYLOAD, fetch_bars=lambda s, tf="1d": bars())
    return TestClient(app, base_url="http://127.0.0.1:8600", raise_server_exceptions=False)


def test_routes(client):
    s = client.get("/api/chain/iwm").json()
    assert s["symbol"] == "IWM" and s["expirations"][0] == {"date": "2026-11-17", "dte": 7, "count": 4} and "calls" not in s
    e = client.get("/api/chain/IWM/2026-11-17").json()
    assert e["price"] == 278.8 and len(e["calls"]) == 3 and e["calls"][0]["occ"] == "IWM261117C00280000"
    r = client.get("/api/chain/IWM/2030-01-01")
    assert r.status_code == 502 and "no options expiring" in r.json()["error"]
    b = client.get("/api/bars/IWM").json()
    assert len(b["bars"]) == 70 and b["levels"]["atr"] > 0
    assert client.get("/api/nope").status_code == 404
    page = client.get("/")
    assert page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/static/sim.js").status_code == 200 and client.get("/static/../market.py").status_code == 404


def test_timeframes(client):
    b = bars(70)
    for i, x in enumerate(b):                                  # real dates: 14 weeks of weekdays from Mon 2026-08-03
        x["d"] = (date(2026, 8, 3) + __import__("datetime").timedelta(days=i + 2 * (i // 5))).isoformat()
    w = market.weekly(b)
    assert len(w) == 14 and w[0]["d"] == "2026-08-03" and w[1]["d"] == "2026-08-10"
    assert w[0]["o"] == b[0]["o"] and w[0]["c"] == b[4]["c"] and w[0]["h"] == max(x["h"] for x in b[:5])
    assert w[0]["l"] == min(x["l"] for x in b[:5]) and w[0]["v"] == sum(x["v"] for x in b[:5])

    class R:
        status_code = 200

        def __init__(self, n):
            self.n = n

        def json(self):
            t0 = 1794407400                                    # 2026-11-11 14:30 UTC = 09:30 New York
            ts = [t0 + 300 * i for i in range(self.n)]
            q = {k: [100.0 + i for i in range(self.n)] for k in ("open", "high", "low", "close")}
            q["volume"] = [10] * self.n
            q["close"][3] = None                               # a hole in the data is skipped
            ts.append(ts[-1] + 60)                             # Yahoo's off-grid "price right now" row
            for k in q:
                q[k].append(q[k][-1])
            return {"chart": {"result": [{"meta": {"exchangeTimezoneName": "America/New_York"}, "timestamp": ts,
                                          "indicators": {"quote": [q]}}]}}
    seen = []
    got = market.fetch_bars("iwm", "5m", get=lambda url: (seen.append(url), R(40))[1])
    assert "interval=5m" in seen[0] and "range=10d" in seen[0]
    assert got[0]["d"] == "2026-11-11T09:30" and got[1]["d"] == "2026-11-11T09:35" and len(got) == 39
    with pytest.raises(market.MarketError, match="Timeframe"):
        market.fetch_bars("iwm", "3m")

    calls = []
    app = create_app(fetch_chain=lambda s: PAYLOAD, fetch_bars=lambda s, tf="1d": (calls.append(tf), b)[1])
    c = TestClient(app, base_url="http://127.0.0.1:8600")
    wk = c.get("/api/bars/IWM?tf=1w").json()
    assert wk["tf"] == "1w" and len(wk["bars"]) == 14 and wk["levels"]["atr"] > 0 and calls == ["1d"]   # built from daily
    assert c.get("/api/bars/IWM?tf=5m").json()["tf"] == "5m" and calls == ["1d", "5m"]
    assert c.get("/api/bars/IWM").json()["tf"] == "1d" and calls == ["1d", "5m"]                         # cached
    assert c.get("/api/bars/IWM?tf=2h").status_code == 502


def test_quotes(client):
    class R:
        status_code = 200

        def json(self):
            day = 86400
            t = 1794407400                                    # 2026-11-11 14:30 UTC
            return {"chart": {"result": [{"meta": {"regularMarketPrice": 176.94, "regularMarketTime": t + 3600,
                                                   "regularMarketVolume": 1000, "shortName": "NVIDIA Corporation"},
                                          "timestamp": [t - 2 * day, t - day, t],
                                          "indicators": {"quote": [{"close": [178.67, 168.89, 176.94]}]}}]}}
    q = market.fetch_quote("nvda", get=lambda url: R())
    assert q["symbol"] == "NVDA" and q["price"] == 176.94 and q["prev_close"] == 168.89
    assert q["change_pct"] == pytest.approx(4.77, abs=0.01) and q["day"] == "2026-11-11" and q["name"].startswith("NVIDIA")
    seen = []
    market.fetch_quote("nasdaq", get=lambda url: (seen.append(url), R())[1])
    assert "%5EIXIC" in seen[0] or "^IXIC" in seen[0]

    def fake(sym):
        if sym == "BAD":
            raise market.MarketError("No quote for BAD. Check the ticker.")
        return {"symbol": sym, "price": 1.0, "change_pct": 0.5}
    app = create_app(fetch_chain=lambda s: PAYLOAD, fetch_bars=lambda s, tf="1d": bars(), fetch_quote=fake)
    c = TestClient(app, base_url="http://127.0.0.1:8600")
    out = c.get("/api/quotes?symbols=spy,BAD,brk-b,,").json()["quotes"]
    assert out["SPY"]["price"] == 1.0 and out["BRK.B"]["stale"] is False and "Check the ticker" in out["BAD"]["error"]
    assert c.get("/api/quotes").json() == {"quotes": {}}


def test_only_known_hosts_and_no_stack_traces(client):
    assert client.get("/api/chain/IWM", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/chain/IWM", headers={"Host": "box.tail1234.ts.net"}).status_code == 200
    assert host_ok("[::1]:8600", ["::1"]) and not host_ok("ts.net", ["*.ts.net"]) and not host_ok("evilts.net", ["*.ts.net"])
    boom = TestClient(create_app(fetch_chain=lambda s: 1 / 0, fetch_bars=lambda s, tf="1d": bars()),
                      base_url="http://127.0.0.1:8600", raise_server_exceptions=False)
    r = boom.get("/api/chain/IWM")
    assert r.status_code == 500 and "division" not in r.text and "Traceback" not in r.text


def test_version_changes_when_the_page_files_do(client, tmp_path, monkeypatch):
    import optlab.api as api_mod

    v1 = client.get("/api/version").json()["version"]
    assert client.get("/api/version").json()["version"] == v1 and len(v1) == 25
    web = tmp_path / "web"
    web.mkdir()
    (web / "app.js").write_text("one")
    monkeypatch.setattr(api_mod, "PKG", tmp_path)
    monkeypatch.setattr(api_mod, "WEB", web)
    a = client.get("/api/version").json()["version"]
    (web / "app.js").write_text("two, longer")
    b = client.get("/api/version").json()["version"]
    assert a != b and a.split("-")[0] == b.split("-")[0]          # only the page part moved
