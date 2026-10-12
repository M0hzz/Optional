"""A simulated market day for rehearsing with the live monitor (scripts/simulate_day.py).

`ReplayFeed` serves the same bars / chains / earnings interface as LiveFeed, but from
a scripted synthetic session that a virtual clock walks through minute by minute:

    09:30-10:30  steady rally (+0.8%)
    10:10-10:25  momentum burst in one stock on heavy volume
    10:30-13:30  quiet drift
    13:30-14:15  sharp market selloff (about -2.6%), implied vol jumps
    14:15-16:00  partial recovery

Daily history comes from SyntheticSource; the session before the replay day is
generated too, so intraday studies (EMA, VWAP, RSI) have their warm-up. Option chains
are priced with Black-Scholes at the current minute's price and implied vol.
Everything is deterministic for a given seed. None of it is market data.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta

import numpy as np
import pandas as pd

from ..data.sources import SyntheticSource, strike_step
from ..pricing.fast_bs import bs_price
from .feed import finish_chain

MINUTES = 390
CHAIN_EVERY = 2
BETA = {"SPY": 1.0, "QQQ": 1.2, "IWM": 1.3, "DIA": 0.9}
BURST = "NVDA"


def previous_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def market_path(rng: np.random.Generator) -> np.ndarray:
    """Scripted per-minute market returns for the replay session."""
    drift = np.zeros(MINUTES)
    drift[0:60] = 0.008 / 60          # 09:30-10:30 rally
    drift[60:240] = 0.0005 / 180      # quiet
    drift[240:285] = -0.026 / 45      # 13:30-14:15 selloff
    drift[285:] = 0.009 / 105         # recovery
    return drift + rng.normal(0, 0.00012, MINUTES)


def synthetic_chain(symbol: str, spot: float, atm: float, today: date, rate: float, oi_seed: int) -> pd.DataFrame:
    """Calls and puts on Fridays 1 week to 6 months out, with a put skew."""
    expiries = sorted({e + timedelta(days=(4 - e.weekday()) % 7)
                       for e in (today + timedelta(days=d) for d in (5, 12, 19, 26, 33, 40, 47, 61, 89, 180))})
    step = strike_step(spot)
    strikes = np.arange(np.floor(spot * 0.7 / step) * step, spot * 1.3, step)
    rows = []
    for exp in expiries:
        t = max((exp - today).days, 1) / 365
        m = np.log(strikes / spot) / np.sqrt(max(t, 1 / 52))
        iv = np.clip(atm * (1 - 0.35 * m + 0.6 * m ** 2), 0.05, 2.5)
        oi = (20000 * np.exp(-12 * np.abs(np.log(strikes / spot))) * np.exp(-t) * (1 + (oi_seed % 7) / 10)).round()
        for typ in ("call", "put"):
            mid = bs_price(spot, strikes, t, rate, 0.0, iv, typ == "call")
            half = np.maximum(0.01, mid * 0.025) + 0.02
            for k, p, h, v, o in zip(strikes, mid, half, iv, oi):
                if p < 0.02:
                    continue
                rows.append((f"{symbol}{exp:%y%m%d}{typ[0].upper()}{int(round(k * 1000)):08d}", exp, float(k), typ,
                             max(p - h, 0.0), p + h, p, float(o * 0.1), float(o), float(v), spot))
    raw = pd.DataFrame(rows, columns=["contract", "expiration", "strike", "type", "bid", "ask", "last", "volume",
                                      "open_interest", "iv", "underlying_price"])
    return finish_chain(raw, symbol, rate, today=today)


class ReplayFeed:
    def __init__(self, day: date, symbols: list[str], rate: float = 0.04, seed: int = 7):
        self.day, self.rate, self.minute, self._chains = day, rate, 0, {}
        self.prev = previous_weekday(day)
        self.src = SyntheticSource(end=self.prev, rate=rate)
        rng = np.random.default_rng(seed)
        mkt_prev, mkt = rng.normal(0, 0.0005, MINUTES), market_path(rng)
        self.market_move = np.cumsum(mkt)
        start = (self.prev - timedelta(days=760)).isoformat()
        self.daily, self.intraday, self.base_iv = {}, {}, {}
        for i, s in enumerate(dict.fromkeys(symbols)):
            d = self.src.prices(s, start).to_pandas().set_index("date")
            d.index = pd.to_datetime(d.index)
            self.daily[s] = d[["open", "high", "low", "close", "volume"]]
            r = np.log(d["close"]).diff().tail(20).std() * np.sqrt(252)
            cap = 0.22 if s in BETA else 0.6  # index ETFs carry less vol than single stocks
            self.base_iv[s] = float(np.clip(r * 1.15 + 0.02, 0.12, cap))
            self.intraday[s] = self._session_pair(s, mkt_prev, mkt, np.random.default_rng(seed + 101 * (i + 1)))

    def _session_pair(self, s, mkt_prev, mkt, rng):
        beta = BETA.get(s, 1.1)
        vol_min = self.base_iv[s] / np.sqrt(252 * MINUTES) * (0.25 if s in BETA else 0.6)
        last = self.daily[s]
        prev_close, avg_vol = float(last["close"].iloc[-1]), float(last["volume"].tail(20).mean())
        u_shape = 1 + 1.5 * (np.linspace(-1, 1, MINUTES) ** 2)
        frames = []
        for day, m, anchor in ((self.prev, mkt_prev, "end"), (self.day, mkt, "start")):
            r = beta * m + rng.normal(0, vol_min, MINUTES)
            vol = avg_vol / MINUTES * u_shape / u_shape.mean() * rng.lognormal(0, 0.3, MINUTES)
            if day == self.day:
                vol[240:300] *= 2.5
                if s == BURST:
                    r[40:55] += 0.02 / 15
                    vol[40:55] *= 4
            path = np.exp(np.cumsum(r))
            # yesterday's minutes end at yesterday's close; today opens with a small gap
            close = prev_close * path / path[-1] if anchor == "end" else prev_close * (1 + rng.normal(0, 0.002)) * path
            noise = np.abs(rng.normal(0, vol_min, MINUTES)) * close
            open_ = np.r_[close[0] / (1 + r[0]), close[:-1]]
            idx = pd.date_range(datetime.combine(day, dtime(9, 30)), periods=MINUTES, freq="1min")
            frames.append(pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + noise,
                                        "low": np.minimum(open_, close) - noise, "close": close, "volume": vol},
                                       index=idx))
        return pd.concat(frames)

    # ------------------------------------------------------------ clock
    def set_minute(self, i: int) -> None:
        self.minute = i

    def now(self) -> datetime:
        return datetime.combine(self.day, dtime(9, 30)) + timedelta(minutes=self.minute)

    def spot(self, s: str) -> float:
        return float(self._today_minutes(s)["close"].iloc[-1])

    def _today_minutes(self, s):
        m = self.intraday[s]
        return m[m.index.date == self.day].iloc[: self.minute + 1]

    # ------------------------------------------------------- feed API
    def bars(self, symbols, timeframe="1d", lookback_days=None):
        out = {}
        for s in symbols:
            if s not in self.intraday:
                continue
            now = self._today_minutes(s)
            if timeframe in ("1d", "1wk", "1mo"):
                today_bar = pd.DataFrame({"open": [now["open"].iloc[0]], "high": [now["high"].max()],
                                          "low": [now["low"].min()], "close": [now["close"].iloc[-1]],
                                          "volume": [now["volume"].sum()]}, index=[pd.Timestamp(self.day)])
                out[s] = pd.concat([self.daily[s], today_bar])
                continue
            m = self.intraday[s]
            m = pd.concat([m[m.index.date == self.prev], now])
            rule = {"1m": "1min", "2m": "2min", "5m": "5min", "15m": "15min", "30m": "30min",
                    "60m": "60min", "1h": "60min"}.get(timeframe, "1min")
            out[s] = m.resample(rule, label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        return out

    def chain(self, symbol, source=None):
        # repriced every CHAIN_EVERY simulated minutes, like the live feed's refresh (CBOE is delayed anyway)
        key = (symbol, self.minute // CHAIN_EVERY)
        if key not in self._chains:
            drop = max(-self.market_move[self.minute], 0)  # implied vol rises as the market falls
            atm = self.base_iv[symbol] * (1 + 6 * drop)
            self._chains[key] = synthetic_chain(symbol, self.spot(symbol), atm, self.day, self.rate, len(symbol))
        return self._chains[key]

    def chains(self, symbols, source=None):
        return {s: self.chain(s) for s in symbols if s in self.intraday}, {}

    def earnings(self, symbols):
        out = {}
        for s in symbols:
            f = self.src.fundamentals(s)
            out[s] = None if f.is_empty() else f["next_earnings"][0]
        return out


def demo_positions(feed: ReplayFeed) -> list[dict]:
    """Three positions priced off the opening chain, set up so the day moves them."""
    def pick(sym, typ, dte, delta):
        ch = feed.chain(sym)
        ch = ch[(ch["type"] == typ) & ch["abs_delta"].notna()]
        exp = ch.iloc[(ch["dte"] - dte).abs().argsort()]["expiration"].iloc[0]
        ch = ch[ch["expiration"] == exp]
        return ch.iloc[(ch["abs_delta"] - delta).abs().argsort()].iloc[0]

    spy = pick("SPY", "put", 33, 0.20)
    iwm_short = pick("IWM", "put", 12, 0.25)
    iwm_ch = feed.chain("IWM")
    iwm_long = iwm_ch[(iwm_ch["type"] == "put") & (iwm_ch["expiration"] == iwm_short["expiration"])
                      & (iwm_ch["strike"] <= iwm_short["strike"] - 5)].iloc[-1]
    aapl = pick("AAPL", "call", 12, 0.20)
    leg = lambda r, q, px: {"type": r["type"], "strike": float(r["strike"]), "expiration": str(r["expiration"]),  # noqa: E731
                            "qty": q, "price": round(float(px), 2)}
    return [
        {"name": f"SPY {spy['strike']:g} put, sold last week", "symbol": "SPY",
         "legs": [leg(spy, -1, spy["mid"] * 1.85)]},
        {"name": f"IWM {iwm_short['strike']:g}/{iwm_long['strike']:g} put spread", "symbol": "IWM",
         "legs": [leg(iwm_short, -1, iwm_short["mid"]), leg(iwm_long, 1, iwm_long["mid"])],
         "exit_when": ["pnl_pct >= 50", "pnl_pct <= -100", "short_strike_otm_pct < 0"],
         "watch_when": ["short_strike_otm_pct < 2", "pnl_pct <= -40"]},
        {"name": f"AAPL covered call {aapl['strike']:g}", "symbol": "AAPL",
         "legs": [{"type": "stock", "qty": 100, "price": round(feed.spot("AAPL") * 0.92, 2)},
                  leg(aapl, -1, aapl["mid"])],
         "exit_when": ["pnl_pct >= 15"], "watch_when": ["short_strike_otm_pct < 1"]},
    ]
