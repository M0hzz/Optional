"""Tastytrade chains against a fake API: login, token renewal, paging the instrument list, batching quotes, and
working out implied volatility and delta from the quotes."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from optlab import market, tasty
from optlab.api import create_app

TODAY = date(2026, 11, 20)
SPOT = 600.0


class R:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


def chain_items(exp="2026-11-27", strikes=range(500, 701, 5), root="SPY", camel=False):
    out = []
    for k in strikes:
        for kind in "CP":
            sym = f"{root:<6}{exp[2:4]}{exp[5:7]}{exp[8:10]}{kind}{int(k * 1000):08d}"
            item = {"symbol": sym, "strike-price": f"{k:.1f}", "expiration-date": exp, "option-type": kind,
                    "root-symbol": root, "active": True}
            if camel:
                item = {"symbol": sym, "strikePrice": f"{k:.1f}", "expirationDate": exp, "optionType": kind,
                        "rootSymbol": root, "active": True}
            out.append(item)
    return out


class FakeAPI:
    """Answers like Tastytrade: quotes priced by Black-Scholes at 25% volatility."""

    def __init__(self, items, pages=1, expire_once=False):
        self.items, self.pages, self.expire_once = items, pages, expire_once
        self.logins, self.gets = [], []

    def post(self, url, json=None, timeout=None, headers=None):
        assert url.endswith("/oauth/token") and headers["User-Agent"].startswith("optlab/")
        self.logins.append(json)
        if json["refresh_token"] != "good":
            return R(401)
        return R(200, {"access_token": f"tok{len(self.logins)}", "expires_in": 900})

    def get(self, url, params=None, timeout=None, headers=None):
        assert headers["Authorization"].startswith("Bearer tok") and headers["User-Agent"]
        self.gets.append((url, params))
        if self.expire_once:
            self.expire_once = False
            return R(401)
        if "/option-chains/" in url:
            page = (params or {}).get("page-offset", 0)
            per = len(self.items) // self.pages + 1
            return R(200, {"data": {"items": self.items[page * per:(page + 1) * per]},
                           "pagination": {"total-pages": self.pages, "page-offset": page}})
        if url.endswith("/market-data/by-type"):
            assert len(params) <= tasty.BATCH
            items = []
            for kind, sym in params:
                if kind in ("equity[]", "index[]"):
                    items.append({"symbol": sym, "last": f"{SPOT}", "prevClose": "590.0"})
                else:
                    k, call = int(sym[-8:]) / 1000, sym[-9] == "C"
                    p = tasty.bs_price(SPOT, k, 7 / 365, 0.25, call)
                    items.append({"symbol": sym, "bid": f"{max(p - 0.05, 0):.2f}", "ask": f"{p + 0.05:.2f}", "volume": "12"})
            return R(200, {"data": {"items": items}})
        return R(404)


def client(api):
    return tasty.Tasty({"client_secret": "s", "refresh_token": "good"}, session=api, sleep=lambda s: None)


def test_credentials_from_env_or_file(tmp_path):
    assert tasty.load_credentials(tmp_path / "none.json", env={}) is None
    env = {"TASTYTRADE_CLIENT_SECRET": "a", "TASTYTRADE_REFRESH_TOKEN": "b"}
    assert tasty.load_credentials(tmp_path / "none.json", env=env)["refresh_token"] == "b"
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"client_secret": "x", "refresh_token": "y"}))
    assert tasty.load_credentials(f, env={}) == {"client_secret": "x", "refresh_token": "y", "client_id": None}
    f.write_text(json.dumps({"client_secret": "x"}))
    with pytest.raises(market.MarketError, match="needs both"):
        tasty.load_credentials(f, env={})
    f.write_text("not json")
    with pytest.raises(market.MarketError, match="Can't read"):
        tasty.load_credentials(f, env={})


def test_login_once_renew_on_401_and_a_clear_error_for_a_bad_token():
    api = FakeAPI(chain_items(), expire_once=True)
    t = client(api)
    t.instruments("SPY")
    assert len(api.logins) == 2                              # the 401 forced one new login, then it worked
    t.quotes(equity=["SPY"])
    assert len(api.logins) == 2                              # the token is reused
    bad = tasty.Tasty({"client_secret": "s", "refresh_token": "bad"}, session=FakeAPI([]))
    with pytest.raises(market.MarketError, match="refused the login"):
        bad.instruments("SPY")


def test_instruments_page_through_and_read_either_spelling():
    items = chain_items() + chain_items(camel=True, exp="2026-12-04")
    rows = client(FakeAPI(items, pages=3)).instruments("SPY")
    assert len(rows) == len(items)
    assert rows[0]["occ"] == "SPY261127C00500000" and rows[0]["strike"] == 500 and rows[0]["call"]
    # SPX: the AM monthly and the PM SPXW on the same day and strike -> one row, the PM one
    both = chain_items(root="SPX", strikes=[6000]) + chain_items(root="SPXW", strikes=[6000])
    rows = client(FakeAPI(both)).instruments("SPX")
    assert [r["root"] for r in rows] == ["SPXW", "SPXW"]
    with pytest.raises(market.MarketError, match="no options"):
        client(FakeAPI([])).instruments("NOPE")


def test_quotes_come_in_batches_of_100():
    api = FakeAPI([])
    syms = [f"SPY   261127C{k * 1000:08d}" for k in range(1, 251)]
    q = client(api).quotes(options=syms)
    assert len(q) == 250 and sum(1 for u, _ in api.gets if u.endswith("by-type")) == 3


def test_summary_and_expiration_match_what_the_page_reads():
    api = FakeAPI(chain_items() + chain_items(exp="2026-12-04"))
    t = client(api)
    s = tasty.summary(t, "spy", today=TODAY)
    assert s["price"] == SPOT and s["change_pct"] == pytest.approx(1.69, abs=0.01) and s["source"] == "tastytrade"
    assert [(e["date"], e["dte"], e["count"]) for e in s["expirations"]] == [("2026-11-27", 7, 82), ("2026-12-04", 14, 82)]
    e = tasty.expiration(t, "SPY", "2026-11-27", today=TODAY)
    strikes = [r["strike"] for r in e["calls"]]
    assert strikes == sorted(strikes) and len(strikes) == 41         # 500-700: all within NEAR of the price
    atm = next(r for r in e["calls"] if r["strike"] == 600)
    assert atm["iv"] == pytest.approx(0.25, abs=0.01)        # worked back out of the quote
    assert 0.45 < atm["delta"] < 0.6 and atm["volume"] == 12 and atm["oi"] is None
    put = next(r for r in e["puts"] if r["strike"] == 600)
    assert -0.55 < put["delta"] < -0.4
    with pytest.raises(market.MarketError, match="no options expiring"):
        tasty.expiration(t, "SPY", "2027-01-15", today=TODAY)


def test_server_uses_tastytrade_when_set_up():
    t = client(FakeAPI(chain_items()))
    app = create_app(fetch_chain=lambda s: (_ for _ in ()).throw(AssertionError("CBOE must not be asked")),
                     fetch_bars=lambda s, tf="1d": [], tasty=t)
    c = TestClient(app, base_url="http://localhost")
    assert c.get("/api/source").json() == {"options": "tastytrade", "error": None}
    s = c.get("/api/chain/SPY").json()
    assert s["source"] == "tastytrade" and s["expirations"][0]["date"] == "2026-11-27"
    e = c.get("/api/chain/SPY/2026-11-27").json()
    assert e["source"] == "tastytrade" and e["calls"] and not e["stale"]
    assert c.get("/api/chain/SPY/2030-01-01").status_code == 502
    off = TestClient(create_app(fetch_chain=lambda s: {}, fetch_bars=lambda s, tf="1d": []), base_url="http://localhost")
    assert off.get("/api/source").json()["options"] == "cboe"
