"""Shared fixtures for the portfolio / API tests: a fresh migrated SQLite file per test, a deterministic
option chain, and a fake delayed quote provider. Nothing here touches the network or real market data."""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

EXP = date(2026, 12, 18)
EXP2 = date(2027, 1, 15)
SPOT = 100.0


def fixture_chain(symbol: str = "XYZ", spot: float = SPOT) -> pd.DataFrame:
    """Calls and puts at strikes 80..120 step 5 for two expirations. Deterministic prices."""
    rows = []
    for exp, t in ((EXP, 0.13), (EXP2, 0.2)):
        for k in range(80, 125, 5):
            for typ in ("call", "put"):
                intrinsic = max(0.0, spot - k) if typ == "call" else max(0.0, k - spot)
                time_val = round(3.0 * np.exp(-abs(k - spot) / 15) * (1 + t), 2)
                mid = round(intrinsic + time_val, 2)
                bid, ask = round(mid - 0.05, 2), round(mid + 0.05, 2)
                delta = (0.5 + (spot - k) / 50) if typ == "call" else -(0.5 - (spot - k) / 50)
                rows.append({"symbol": symbol, "contract": f"{symbol}{exp:%y%m%d}{typ[0].upper()}{k * 1000:08d}",
                             "expiration": exp, "dte": (exp - date(2026, 11, 2)).days, "strike": float(k), "type": typ,
                             "bid": bid, "ask": ask, "mid": mid, "last": mid, "volume": 120.0, "open_interest": 900.0,
                             "iv": 0.25, "delta": round(max(-1, min(1, delta)), 4), "gamma": 0.03, "theta": -0.02,
                             "vega": 0.11, "underlying_price": spot})
    return pd.DataFrame(rows)


class FakeProvider:
    """Behaves like Cboe through OpenBB: 15 minutes delayed, exchange Greeks. `fail` simulates an outage."""
    name, delay_minutes, synthetic, greeks_source = "cboe (OpenBB)", 15, False, "provider"

    def __init__(self, chain=None):
        self.chain = chain if chain is not None else fixture_chain()
        self.calls = 0
        self.fail = False

    def fetch(self, symbol):
        self.calls += 1
        if self.fail:
            raise ConnectionError("provider unavailable (simulated outage)")
        if symbol == "NOOPT":
            raise ValueError("no options listed for NOOPT")
        df = self.chain.copy()
        df["symbol"] = symbol
        return df, datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


class FakeBars:
    """Daily bars drifting from 120 down to 100 (RSI low): no network."""
    name = "fake"

    def __init__(self):
        self.calls = 0

    def get(self, symbols, timeframe):
        self.calls += 1
        idx = pd.date_range("2026-08-01", periods=60, freq="D")
        df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": np.linspace(120, 100, 60), "volume": 1.0},
                          index=idx)
        return {s: df for s in symbols}, None


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'optlab.sqlite'}"


@pytest.fixture
def database(db_url):
    from optlab.portfolio.db import Database, upgrade

    upgrade(db_url)
    d = Database(db_url)
    yield d
    d.dispose()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def quotes(provider):
    from optlab.portfolio.quotes import QuoteService

    return QuoteService(provider, mode="live")


@pytest.fixture
def client(db_url, quotes):
    from fastapi.testclient import TestClient

    from optlab.api.main import create_app
    from optlab.portfolio.db import Database

    app = create_app(Database(db_url), quotes=quotes, web_dist=None, monitor=False,
                     engine_options={"notifier": None, "earnings": lambda s: None, "bars": FakeBars()})
    app.state.stream_poll = 0.05
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def account(client):
    a = client.post("/api/v1/accounts", json={"name": "Main"}).json()
    client.post(f"/api/v1/accounts/{a['id']}/snapshots",
                json={"total_value": "1000.00", "cash": "1000.00", "buying_power": "1000.00"})
    return a


def opt(side, right, strike, price, qty=1, exp=EXP, fees="0"):
    return {"asset_type": "OPTION", "side": side, "quantity": qty, "option_right": right, "strike": str(strike),
            "expiration": exp.isoformat(), "fill_price": price, "fees": fees}


def stock(side, qty, price):
    return {"asset_type": "STOCK", "side": side, "quantity": qty, "fill_price": price}
