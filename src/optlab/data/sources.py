"""Data sources. Each returns Polars frames that match the Store schema.

OpenBBSource    real data through the OpenBB Platform (needs `pip install openbb`)
SyntheticSource realistic fake prices and option chains, for offline development

Both expose the same two methods:
    prices(symbol, start) -> prices frame
    option_chain(symbol)  -> option_chains frame (one snapshot)
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta

import numpy as np
import pandas as pd
import polars as pl

from ..pricing.fast_bs import bs_price


class OpenBBSource:
    """Thin adapter over the OpenBB Platform.

    Free providers: yfinance (prices + chains), cboe (chains). Paid providers such
    as polygon, intrinio or tradier plug in by changing `provider`, after setting
    their API keys with `obb.user.credentials`.
    """

    def __init__(self, provider: str = "yfinance", chain_provider: str | None = None):
        try:
            from openbb import obb  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError("OpenBB is not installed. Run: pip install openbb") from exc
        self.obb = obb
        self.provider = provider
        self.chain_provider = chain_provider or provider

    def prices(self, symbol: str, start: str = "2018-01-01") -> pl.DataFrame:
        res = self.obb.equity.price.historical(symbol, start_date=start, provider=self.provider)
        df = res.to_df().reset_index()
        df.columns = [c.lower() for c in df.columns]
        df["symbol"] = symbol
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return pl.from_pandas(df[["symbol", "date", "open", "high", "low", "close", "volume"]])

    def option_chain(self, symbol: str) -> pl.DataFrame:
        res = self.obb.derivatives.options.chains(symbol, provider=self.chain_provider)
        df = res.to_df().reset_index(drop=True) if hasattr(res, "to_df") else pd.DataFrame(res.results)
        df.columns = [c.lower() for c in df.columns]
        rename = {
            "last_trade_price": "last", "last_price": "last",
            "implied_volatility": "implied_vol", "iv": "implied_vol",
            "type": "option_type", "call_put": "option_type",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        for col in ["bid", "ask", "last", "volume", "open_interest", "implied_vol", "underlying_price"]:
            if col not in df.columns:
                df[col] = np.nan
        if df["underlying_price"].isna().all():
            df["underlying_price"] = float(self.prices(symbol, str(date.today() - timedelta(days=10)))["close"][-1])
        df["option_type"] = df["option_type"].astype(str).str.lower().str[0].map({"c": "call", "p": "put"})
        df["expiration"] = pd.to_datetime(df["expiration"]).dt.date
        df["snapshot_date"] = date.today()
        df["symbol"] = symbol
        cols = ["snapshot_date", "symbol", "expiration", "strike", "option_type", "bid", "ask", "last",
                "volume", "open_interest", "implied_vol", "underlying_price"]
        return pl.from_pandas(df[cols]).with_columns(pl.col(cols[5:]).cast(pl.Float64))


# Rough per-symbol (start price, end price, long-run vol). The random path is
# bridged to the end price so levels stay in a familiar range. Unknown symbols
# get defaults. These numbers are illustrative, not market data.
_PROFILES = {
    "SPY": (270, 650, 0.17), "QQQ": (160, 580, 0.22), "IWM": (155, 240, 0.23),
    "AAPL": (42, 250, 0.30), "MSFT": (85, 500, 0.27), "NVDA": (5, 180, 0.50),
    "AMZN": (60, 230, 0.33), "JPM": (110, 300, 0.26),
}


def strike_step(spot: float) -> float:
    """Typical listed-strike increment for a given underlying price."""
    return 0.5 if spot < 25 else 1.0 if spot < 150 else 2.5 if spot < 400 else 5.0


def _seed(symbol: str) -> int:
    return int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16)


class SyntheticSource:
    """Fake but realistic: mean-reverting stochastic vol, crash jumps, and a
    volatility smile that steepens when markets fall. Deterministic per symbol."""

    def __init__(self, end: date | None = None, rate: float = 0.04):
        self.end = end or date.today()
        self.rate = rate

    def _path(self, symbol: str, start: str) -> pd.DataFrame:
        rng = np.random.default_rng(_seed(symbol))
        s0, s_end, theta = _PROFILES.get(symbol, (100, 180, 0.30))
        dates = pd.bdate_range(start, self.end)
        n, dt = len(dates), 1 / 252
        vol = np.empty(n)
        v = theta
        for i in range(n):
            # Ornstein-Uhlenbeck on log-vol with occasional spikes
            v = np.exp(np.log(v) + 4.0 * (np.log(theta) - np.log(v)) * dt + 0.6 * np.sqrt(dt) * rng.standard_normal())
            if rng.random() < 0.002:
                v *= 1.6
            vol[i] = np.clip(v, 0.06, 1.0)
        shocks = rng.standard_normal(n)
        jumps = (rng.random(n) < 0.003) * rng.normal(-0.06, 0.03, n)
        rets = vol * np.sqrt(dt) * shocks + jumps
        cum = np.cumsum(rets)
        # Brownian bridge: tilt the path so it ends at s_end, keeping its shape
        cum = cum - np.linspace(0, 1, n) * (cum[-1] - np.log(s_end / s0))
        close = s0 * np.exp(cum)
        intraday = np.abs(rng.normal(0, vol * np.sqrt(dt) * 0.6, n))
        open_ = close * np.exp(rng.normal(0, vol * np.sqrt(dt) * 0.3, n))
        high = np.maximum(open_, close) * (1 + intraday)
        low = np.minimum(open_, close) * (1 - intraday)
        volume = rng.lognormal(16, 0.4, n) * (1 + 3 * (vol / theta - 1).clip(0))
        return pd.DataFrame({"date": dates.date, "open": open_, "high": high, "low": low,
                             "close": close, "volume": volume, "vol": vol})

    def prices(self, symbol: str, start: str = "2018-01-01") -> pl.DataFrame:
        df = self._path(symbol, start).drop(columns="vol")
        df.insert(0, "symbol", symbol)
        return pl.from_pandas(df)

    def option_chain(self, symbol: str, start: str = "2018-01-01") -> pl.DataFrame:
        path = self._path(symbol, start)
        spot, rv = float(path["close"].iloc[-1]), float(path["vol"].iloc[-1])
        ret20 = float(path["close"].iloc[-1] / path["close"].iloc[-21] - 1)
        atm = rv * 1.12 + 0.01                      # implied usually above realized
        skew = 0.35 + max(-ret20, 0) * 2.0          # steeper after selloffs
        snap = self.end
        expiries = [snap + timedelta(days=d) for d in (7, 14, 21, 30, 45, 60, 90, 120, 180)]
        expiries = [e + timedelta(days=(4 - e.weekday()) % 7) for e in expiries]  # Fridays
        rows = []
        step = strike_step(spot)
        strikes = np.arange(max(np.floor(spot * 0.6 / step), 1) * step, spot * 1.4, step)
        for exp in sorted(set(expiries)):
            t = (exp - snap).days / 365
            term = 1 + 0.08 * np.log(max(t, 1 / 52) / 0.08)   # gentle upward term structure
            m = np.log(strikes / spot) / np.sqrt(max(t, 1 / 52))
            iv = np.clip(atm * term * (1 - skew * m + 0.6 * m**2), 0.05, 2.5)
            for typ in ("call", "put"):
                is_call = typ == "call"
                mid = bs_price(spot, strikes, t, self.rate, 0.0, iv, is_call)
                half = np.maximum(0.01, mid * 0.03) + 0.02
                dist = np.abs(np.log(strikes / spot))
                oi = (20000 * np.exp(-12 * dist) * np.exp(-t)).round()
                for k, p, h, v, o in zip(strikes, mid, half, iv, oi):
                    if p < 0.01:
                        continue
                    rows.append((snap, symbol, exp, float(k), typ, max(p - h, 0.0), p + h, p,
                                 float(o * 0.1), float(o), float(v), spot))
        cols = ["snapshot_date", "symbol", "expiration", "strike", "option_type", "bid", "ask", "last",
                "volume", "open_interest", "implied_vol", "underlying_price"]
        return pl.DataFrame(rows, schema=cols, orient="row")


def get_source(cfg: dict):
    name = cfg["data"]["source"]
    if name == "openbb":
        return OpenBBSource(cfg["data"].get("openbb_provider", "yfinance"))
    if name == "synthetic":
        return SyntheticSource(rate=cfg["market"]["risk_free_rate"])
    raise ValueError(f"Unknown data source: {name}")
