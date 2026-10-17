"""Live market data for the monitor and the option-chain viewer.

Option chains come through OpenBB:
    cboe      every expiration and strike in one request, with exchange-computed Greeks
              (free, 15-minute delayed; the default)
    yfinance  slower (one request per expiration); Greeks are computed here
Bars (1m ... 1d) come from Yahoo through the yfinance library (which OpenBB's yfinance
provider wraps), called directly so ~100 tickers come back in one batched request.

Both are cached for a short time so a scan every minute doesn't refetch daily
history it already has. `SyntheticFeed` serves the same frames offline.

Every chain is returned in one standard pandas layout (CHAIN_COLUMNS), restricted to
standard contracts: weeklies, monthlies, quarterlies and LEAPS are kept; adjusted
contracts left over from splits and mergers (roots like AAPL1) are dropped.
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..pricing.fast_bs import bs_greeks

CHAIN_COLUMNS = [
    "symbol", "contract", "expiration", "dte", "strike", "type", "bid", "ask", "mid", "last", "volume",
    "open_interest", "iv", "delta", "gamma", "theta", "vega", "underlying_price", "spread_pct", "otm_pct",
    "moneyness", "intrinsic", "extrinsic", "breakeven", "yield_pct", "annualized_yield", "prob_otm", "abs_delta",
]
# Index options trade under the index name at CBOE, but Yahoo prices the index as ^NAME.
INDEX_BARS = {"SPX": "^SPX", "NDX": "^NDX", "RUT": "^RUT", "VIX": "^VIX", "XSP": "^XSP", "DJX": "^DJI"}
LOOKBACK_DAYS = {"1m": 5, "2m": 30, "5m": 30, "15m": 30, "30m": 30, "60m": 180, "1h": 180, "90m": 30,
                 "1d": 730, "1wk": 1825, "1mo": 3650}
DAILY = {"1d", "1wk", "1mo"}


def finish_chain(df: pd.DataFrame, symbol: str, rate: float = 0.04, today: date | None = None) -> pd.DataFrame:
    """Normalize a raw chain to CHAIN_COLUMNS and add the derived columns conditions can use."""
    today = today or date.today()
    df = df.copy()
    df["symbol"] = symbol
    df["expiration"] = pd.to_datetime(df["expiration"]).dt.date
    df["dte"] = [(e - today).days for e in df["expiration"]]
    for c in ("bid", "ask", "last", "volume", "open_interest", "iv", "delta", "gamma", "theta", "vega"):
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df else np.nan
    df.loc[df["iv"] <= 0, "iv"] = np.nan  # CBOE sends 0 when it can't solve IV (deep ITM, no quote)
    df["volume"] = df["volume"].fillna(0)
    df["open_interest"] = df["open_interest"].fillna(0)
    s = float(df["underlying_price"].dropna().iloc[0]) if df["underlying_price"].notna().any() else np.nan
    df["underlying_price"] = s
    is_call = (df["type"] == "call").to_numpy()
    two_sided = (df["bid"] > 0) & (df["ask"] > 0)
    df["mid"] = np.where(two_sided, (df["bid"] + df["ask"]) / 2, df["last"])
    if df["delta"].isna().all() and df["iv"].notna().any():  # e.g. yfinance: compute Greeks ourselves
        t = np.maximum(df["dte"].to_numpy(float), 0.5) / 365
        g = bs_greeks(s, df["strike"].to_numpy(float), t, rate, 0.0, df["iv"].fillna(0).clip(lower=1e-4).to_numpy(), is_call)
        for k in ("delta", "gamma", "theta", "vega"):
            df[k] = np.where(df["iv"].notna(), g[k], np.nan)
    df["spread_pct"] = np.where(two_sided, 100 * (df["ask"] - df["bid"]) / df["mid"], np.nan)
    df["moneyness"] = df["strike"] / s
    df["otm_pct"] = np.where(is_call, 100 * (df["strike"] - s) / s, 100 * (s - df["strike"]) / s)
    df["intrinsic"] = np.where(is_call, np.maximum(s - df["strike"], 0), np.maximum(df["strike"] - s, 0))
    df["extrinsic"] = df["mid"] - df["intrinsic"]
    df["breakeven"] = np.where(is_call, df["strike"] + df["mid"], df["strike"] - df["mid"])
    df["yield_pct"] = 100 * df["mid"] / np.where(is_call, s, df["strike"])   # premium vs capital at risk
    df["annualized_yield"] = df["yield_pct"] * 365 / np.maximum(df["dte"], 1)
    df["abs_delta"] = df["delta"].abs()
    df["prob_otm"] = 100 * (1 - df["abs_delta"])
    df = df[df["dte"] >= 0]
    return df[CHAIN_COLUMNS].sort_values(["expiration", "strike", "type"]).reset_index(drop=True)


def standard_only(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Drop adjusted contracts. OCC symbols are ROOT + YYMMDD + C/P + 8-digit strike."""
    root = df["contract"].astype(str).str[:-15]
    base = re.sub(r"[^A-Z]", "", symbol.upper())
    keep = root.str.fullmatch(f"{base}W?")
    return df[keep] if keep.any() else df


class LiveFeed:
    """Real data via OpenBB. Thread-safe caches; one instance per process."""

    def __init__(self, chain_source: str = "cboe", rate: float = 0.04, bar_ttl: dict | None = None,
                 chain_ttl: float = 50, workers: int = 16):
        from openbb import obb  # slow import, only when live data is needed

        self.obb, self.chain_source, self.rate = obb, chain_source, rate
        self.bar_ttl = {"1wk": 3600, "1mo": 3600, **(bar_ttl or {})}  # others: refetched each pass
        self.chain_ttl, self.workers = chain_ttl, workers
        self._bars: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._chains: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._earnings: dict[str, tuple[date, date | None]] = {}
        self._lock = threading.Lock()

    def earnings(self, symbols: list[str]) -> dict[str, date | None]:
        """Next earnings date per symbol (None for ETFs/indices), refreshed once a day."""
        from ..data.sources import next_earnings_date

        todo = [s for s in symbols if self._earnings.get(s, (None,))[0] != date.today()]
        if todo:
            with ThreadPoolExecutor(self.workers) as ex:
                for s, d in zip(todo, ex.map(next_earnings_date, todo)):
                    self._earnings[s] = (date.today(), d)
        return {s: self._earnings[s][1] for s in symbols}

    # ------------------------------------------------------------------ bars
    def bars(self, symbols: list[str], timeframe: str = "1d", lookback_days: int | None = None) -> dict[str, pd.DataFrame]:
        ttl = self.bar_ttl.get(timeframe, 50)
        now = time.time()
        stale = [s for s in symbols if now - self._bars.get((s, timeframe), (0, None))[0] > ttl]
        for i in range(0, len(stale), 100):  # Yahoo copes with ~100 tickers per request
            self._fetch_bars(stale[i:i + 100], timeframe, lookback_days or LOOKBACK_DAYS.get(timeframe, 730))
        return {s: self._bars[(s, timeframe)][1] for s in symbols if (s, timeframe) in self._bars}

    def _fetch_bars(self, symbols, timeframe, lookback):
        # yfinance directly, not through OpenBB: OpenBB's yfinance provider wraps the same
        # library but requests tickers one by one, ~4x slower for a once-a-minute scan.
        import logging

        import yfinance as yf

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        tickers = {INDEX_BARS.get(s, s): s for s in symbols}
        start = (date.today() - timedelta(days=lookback)).isoformat()
        try:
            df = yf.download(list(tickers), start=start, interval=timeframe, group_by="ticker", auto_adjust=True,
                             progress=False, threads=True, multi_level_index=True)
        except Exception:
            return
        if df is None or df.empty:
            return
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:  # intraday bars arrive tz-aware; keep exchange-local wall time
            df.index = idx.tz_convert("America/New_York").tz_localize(None)
        stamp = time.time()
        with self._lock:
            for tick, sym in tickers.items():
                if tick not in df.columns.get_level_values(0):
                    continue
                g = df[tick].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(how="all")
                g = g.dropna(subset=["close"])
                if not g.empty:
                    self._bars[(sym, timeframe)] = (stamp, g)

    # ---------------------------------------------------------------- chains
    def chain(self, symbol: str, source: str | None = None) -> pd.DataFrame:
        source = source or self.chain_source
        hit = self._chains.get((symbol, source))
        if hit and time.time() - hit[0] < self.chain_ttl:
            return hit[1]
        raw = self.obb.derivatives.options.chains(symbol, provider=source).to_df().reset_index(drop=True)
        raw = raw.rename(columns={"contract_symbol": "contract", "option_type": "type", "last_trade_price": "last",
                                  "implied_volatility": "iv"})
        df = finish_chain(standard_only(raw, symbol), symbol, self.rate)
        with self._lock:
            self._chains[(symbol, source)] = (time.time(), df)
        return df

    def chains(self, symbols: list[str], source: str | None = None) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        """Fetch many chains in parallel. Returns (chains, errors)."""
        out, errors = {}, {}

        def one(s):
            try:
                out[s] = self.chain(s, source)
            except Exception as exc:  # no options listed, network hiccup, ...
                errors[s] = str(exc).strip().splitlines()[-1][:200] if str(exc).strip() else type(exc).__name__
        with ThreadPoolExecutor(self.workers) as ex:
            list(ex.map(one, symbols))
        return out, errors


class SyntheticFeed:
    """Offline stand-in with the same interface, built on SyntheticSource."""

    def __init__(self, end: date | None = None, rate: float = 0.04):
        from ..data.sources import SyntheticSource

        self.src, self.rate, self.end = SyntheticSource(end=end, rate=rate), rate, end or date.today()

    def bars(self, symbols, timeframe="1d", lookback_days=None):
        start = (self.end - timedelta(days=lookback_days or 730)).isoformat()
        out = {}
        for s in symbols:
            df = self.src.prices(s, start).to_pandas().set_index("date")
            df.index = pd.to_datetime(df.index)
            out[s] = df[["open", "high", "low", "close", "volume"]]
        return out

    def chain(self, symbol, source=None):
        raw = self.src.option_chain(symbol).to_pandas()
        raw = raw.rename(columns={"option_type": "type", "implied_vol": "iv"})
        raw["contract"] = [f"{symbol}{e:%y%m%d}{t[0].upper()}{int(round(k * 1000)):08d}"
                           for e, t, k in zip(raw["expiration"], raw["type"], raw["strike"])]
        return finish_chain(raw, symbol, self.rate, today=self.end)

    def chains(self, symbols, source=None):
        return {s: self.chain(s) for s in symbols}, {}

    def earnings(self, symbols):
        out = {}
        for s in symbols:
            f = self.src.fundamentals(s)
            out[s] = None if f.is_empty() else f["next_earnings"][0]
        return out


def make_feed(source: str, rate: float = 0.04, chain_source: str = "cboe", chain_refresh: float = 50):
    if source == "synthetic":
        return SyntheticFeed(rate=rate)
    return LiveFeed(chain_source=chain_source, rate=rate, chain_ttl=chain_refresh)
