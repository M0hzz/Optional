"""One shared quote / option-chain snapshot service for the API (and, from Phase 2, the monitor).

Every request for a symbol's chain goes through `QuoteService.chain(symbol)`:
    * a snapshot is reused for `ttl_seconds` (default 60) across all callers;
    * concurrent requests for the same symbol wait for the one fetch in flight (single flight);
    * each snapshot records the provider, its delay class, the provider's own timestamp when it gives
      one (source_timestamp) and when optlab fetched it (fetched_at), separately;
    * if a refresh fails and an earlier snapshot exists, that snapshot is returned labelled CACHED with
      the error; with no earlier snapshot the result is UNAVAILABLE. Nothing is ever filled in.

Freshness labels
    LIVE         real-time provider, fetched within max_age_seconds (no real-time provider is configured yet)
    DELAYED      delayed provider (Cboe through OpenBB: about 15 minutes), fetched within max_age_seconds
    CACHED       an older snapshot (the refresh failed, or it's past max_age_seconds)
    SYNTHETIC    offline demo data (data.source: synthetic only)
    UNAVAILABLE  no data
    (ESTIMATED is applied by valuation to values derived from quotes.)

Synthetic isolation: in live mode the service refuses a synthetic provider, and in synthetic mode it
refuses a live one, so demo prices can never appear as market data. DuckDB is only touched to read an
earnings date, through a connection opened for that one read.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

import numpy as np
import pandas as pd

from .money import from_provider

LIVE, DELAYED, CACHED, SYNTHETIC, UNAVAILABLE, ESTIMATED = "LIVE", "DELAYED", "CACHED", "SYNTHETIC", "UNAVAILABLE", "ESTIMATED"


class ChainProvider(Protocol):
    name: str
    delay_minutes: int          # 0 = real time
    synthetic: bool
    greeks_source: str          # "provider" (exchange-computed) or "model"

    def fetch(self, symbol: str) -> tuple[pd.DataFrame, datetime | None]:
        """Chain in optlab.live.feed CHAIN_COLUMNS layout, and the provider's timestamp if it gives one."""
        ...


class CboeProvider:
    name, delay_minutes, synthetic, greeks_source = "cboe (OpenBB)", 15, False, "provider"

    def __init__(self, rate: float = 0.04):
        self.rate, self.feed = rate, None

    def fetch(self, symbol):
        if self.feed is None:                        # OpenBB is slow to import: only on first use
            from ..live.feed import LiveFeed

            self.feed = LiveFeed(chain_source="cboe", rate=self.rate, chain_ttl=0)   # caching is QuoteService's job
        return self.feed.chain(symbol), None


class SyntheticProvider:
    name, delay_minutes, synthetic, greeks_source = "synthetic", 0, True, "model"

    def __init__(self, rate: float = 0.04):
        from ..live.feed import SyntheticFeed

        self.feed = SyntheticFeed(rate=rate)

    def fetch(self, symbol):
        return self.feed.chain(symbol), None


@dataclass
class ChainSnapshot:
    symbol: str
    provider: str
    delay_minutes: int
    synthetic: bool
    greeks_source: str
    fetched_at: datetime | None
    source_timestamp: datetime | None
    rows: pd.DataFrame | None
    error: str | None = None
    from_cache: bool = False

    def age_seconds(self, now: datetime | None = None) -> float | None:
        if self.fetched_at is None:
            return None
        return ((now or datetime.now(timezone.utc)) - self.fetched_at).total_seconds()

    def freshness(self, max_age: float, now: datetime | None = None) -> str:
        if self.rows is None or self.rows.empty:
            return UNAVAILABLE
        if self.synthetic:
            return SYNTHETIC
        if self.error or (self.age_seconds(now) or 0) > max_age:
            return CACHED
        return LIVE if self.delay_minutes == 0 else DELAYED

    def underlying_price(self) -> Decimal | None:
        if self.rows is None or self.rows.empty or "underlying_price" not in self.rows:
            return None
        v = self.rows["underlying_price"].dropna()
        return from_provider(float(v.iloc[0])) if len(v) else None


def _num(x) -> float | None:
    try:
        return None if x is None or not np.isfinite(float(x)) else float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class ContractQuote:
    """One contract's quote. Prices are Decimal (rounded to $0.0001 from the provider's floats);
    Greeks and IV stay floats (they are not money). Missing values are None, never zero."""
    status: str                          # FOUND | NOT_FOUND | UNAVAILABLE
    reason: str | None
    occ_symbol: str | None
    bid: Decimal | None = None
    ask: Decimal | None = None
    mid: Decimal | None = None
    last: Decimal | None = None
    volume: float | None = None
    open_interest: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    underlying_price: Decimal | None = None
    spread: Decimal | None = None
    spread_pct: float | None = None
    intrinsic: Decimal | None = None
    extrinsic: Decimal | None = None
    breakeven: Decimal | None = None
    quality: list[str] = field(default_factory=list)
    nearest: list[str] = field(default_factory=list)


class QuoteService:
    def __init__(self, provider: ChainProvider, mode: str = "live", ttl_seconds: float = 60, max_age_seconds: float = 300,
                 clock=time.monotonic):
        if mode not in ("live", "synthetic"):
            raise ValueError("mode must be live or synthetic")
        if mode == "live" and provider.synthetic:
            raise ValueError("A synthetic provider can't be used in live mode: demo prices would look like market data.")
        if mode == "synthetic" and not provider.synthetic:
            raise ValueError("Synthetic mode only uses the synthetic provider.")
        self.provider, self.mode, self.ttl, self.max_age, self.clock = provider, mode, ttl_seconds, max_age_seconds, clock
        self._snaps: dict[str, tuple[float, ChainSnapshot]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.fetch_count = 0

    def _lock(self, symbol: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(symbol, threading.Lock())

    def chain(self, symbol: str, force: bool = False) -> ChainSnapshot:
        symbol = symbol.strip().upper()
        with self._lock(symbol):                     # single flight per symbol
            hit = self._snaps.get(symbol)
            if hit and not force and self.clock() - hit[0] < self.ttl and hit[1].error is None:
                return _cached(hit[1])
            p = self.provider
            try:
                self.fetch_count += 1
                rows, src_ts = p.fetch(symbol)
                snap = ChainSnapshot(symbol, p.name, p.delay_minutes, p.synthetic, p.greeks_source,
                                     datetime.now(timezone.utc), src_ts, rows)
                self._snaps[symbol] = (self.clock(), snap)
                return snap
            except Exception as exc:                 # outage, rate limit, symbol without options...
                msg = (str(exc).strip().splitlines() or [type(exc).__name__])[-1][:200]
                if hit:
                    old = hit[1]
                    return ChainSnapshot(old.symbol, old.provider, old.delay_minutes, old.synthetic, old.greeks_source,
                                         old.fetched_at, old.source_timestamp, old.rows, f"refresh failed: {msg}", True)
                return ChainSnapshot(symbol, p.name, p.delay_minutes, p.synthetic, p.greeks_source, None, None, None,
                                     msg)

    def status(self) -> dict:
        p = self.provider
        return {"mode": self.mode, "provider": p.name, "delay_minutes": p.delay_minutes, "synthetic": p.synthetic,
                "greeks_source": p.greeks_source, "ttl_seconds": self.ttl, "max_age_seconds": self.max_age,
                "cached_symbols": sorted(self._snaps), "fetches": self.fetch_count}

    def contract(self, symbol: str, expiration: date, right: str, strike: Decimal,
                 snap: ChainSnapshot | None = None) -> tuple[ContractQuote, ChainSnapshot]:
        from .occ import build

        snap = snap or self.chain(symbol)
        occ = build(symbol, expiration, right, strike)
        if snap.rows is None or snap.rows.empty:
            return ContractQuote("UNAVAILABLE", snap.error or "no chain data", occ), snap
        df = snap.rows
        typ = right.lower()
        m = df[(pd.to_datetime(df["expiration"]).dt.date == expiration) & (df["type"] == typ)
               & np.isclose(df["strike"].astype(float), float(strike))]
        if m.empty:
            same = df[(pd.to_datetime(df["expiration"]).dt.date == expiration) & (df["type"] == typ)]
            if same.empty:
                exps = sorted(pd.to_datetime(df["expiration"]).dt.date.unique(), key=lambda e: abs((e - expiration).days))[:3]
                near = [f"expiration {e:%Y-%m-%d}" for e in exps]
                why = f"No {typ}s expiring {expiration:%Y-%m-%d} in the chain"
            else:
                ks = sorted(same["strike"].astype(float), key=lambda k: abs(k - float(strike)))[:3]
                near = [f"strike {k:g}" for k in ks]
                why = f"No {strike.normalize()} {typ} on {expiration:%Y-%m-%d} in the chain"
            return ContractQuote("NOT_FOUND", why + " (the contract may be adjusted, expired, or the symbol changed).",
                                 occ, nearest=near), snap
        r = m.iloc[0]
        bid, ask, last = from_provider(_num(r["bid"])), from_provider(_num(r["ask"])), from_provider(_num(r["last"]))
        q = ContractQuote("FOUND", None, occ, bid, ask, None, last, _num(r["volume"]), _num(r["open_interest"]),
                          _num(r["iv"]), _num(r["delta"]), _num(r["gamma"]), _num(r["theta"]), _num(r["vega"]),
                          snap.underlying_price())
        if bid is None or ask is None:
            q.quality.append("missing bid or ask")
        elif ask < bid:
            q.quality.append("crossed market (bid above ask)")
        else:
            if bid == 0:
                q.quality.append("zero bid")
            if ask > 0:
                q.mid = (bid + ask) / 2
                q.spread = ask - bid
                q.spread_pct = float(q.spread / q.mid * 100) if q.mid > 0 else None
        if q.delta is None:
            q.quality.append("missing Greeks")
        if q.iv is None or q.iv <= 0:
            q.quality.append("missing or invalid implied volatility")
        u = q.underlying_price
        if u is not None:
            q.intrinsic = max(Decimal(0), (u - strike) if right == "CALL" else (strike - u))
            if q.mid is not None:
                q.extrinsic = q.mid - q.intrinsic
                q.breakeven = strike + q.mid if right == "CALL" else strike - q.mid
        return q, snap


def _cached(s: ChainSnapshot) -> ChainSnapshot:
    return ChainSnapshot(s.symbol, s.provider, s.delay_minutes, s.synthetic, s.greeks_source, s.fetched_at,
                         s.source_timestamp, s.rows, s.error, True)


def default_service() -> QuoteService:
    """Provider from OPTLAB_QUOTE_SOURCE (live | synthetic) if set, else config.yaml's data.source:
    synthetic -> synthetic mode, anything else -> live (Cboe, delayed)."""
    import os

    from ..config import load_config

    cfg = load_config()
    rate = float(cfg["market"].get("risk_free_rate", 0.04))
    source = os.environ.get("OPTLAB_QUOTE_SOURCE") or ("synthetic" if cfg["data"]["source"] == "synthetic" else "live")
    if source not in ("live", "synthetic"):
        raise ValueError("OPTLAB_QUOTE_SOURCE must be live or synthetic")
    if source == "synthetic":
        return QuoteService(SyntheticProvider(rate), mode="synthetic")
    return QuoteService(CboeProvider(rate), mode="live")


def earnings_date(symbol: str) -> date | None:
    """Next earnings date from the market database, read through a short-lived read-only connection."""
    from ..config import load_config
    from ..data import Store

    try:
        with Store(load_config()["data"]["db_path"], read_only=True, wait_seconds=5) as s:
            if not s.has_table("fundamentals"):
                return None
            df = s.query("SELECT next_earnings FROM fundamentals WHERE symbol = ? ORDER BY as_of DESC LIMIT 1",
                         [symbol.upper()])
            v = df["next_earnings"][0] if df.height else None
            return v if isinstance(v, date) else None
    except Exception:
        return None
