"""Historical bars for the Historical Signal Explorer.

Three sources, one interface (`load_history`):

    yahoo      fetch from Yahoo Finance (via yfinance) into the local archive, then read
               the archive. Yahoo serves limited intraday history: about 7 days of 1m,
               60 days of 5m/15m/30m, 730 days of 1h, decades of 1d/1wk. Fetch regularly
               and the archive (data/history.duckdb) keeps everything already fetched,
               so longer intraday ranges build up over time.
    archive    read only what the archive already holds; no network.
    synthetic  deterministic fake bars for offline use and tests. Range-independent:
               the bar at a timestamp is the same whatever range you ask for.

Timestamps are stored in UTC and returned in the requested timezone; each is the bar's
START (a 5m bar stamped 10:35 covers 10:35-10:40). Only completed bars are returned:
a bar still in progress at `now` is dropped. Prices are split/dividend adjusted.
"""
from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd

EXCHANGE_TZ = "America/New_York"
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "1d", "1wk")
INTRADAY = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
YAHOO_MAX_DAYS = {"1m": 29, "5m": 59, "15m": 59, "30m": 59, "1h": 729}
YAHOO_CHUNK_DAYS = {"1m": 7, "5m": 59, "15m": 59, "30m": 59, "1h": 365}
SESSION_OPEN, SESSION_CLOSE = dtime(9, 30), dtime(16, 0)
DEFAULT_ARCHIVE = Path(__file__).resolve().parents[3] / "data" / "history.duckdb"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS bars (
        symbol VARCHAR, timeframe VARCHAR, ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE,
        close DOUBLE, volume DOUBLE, session VARCHAR, source VARCHAR, adjusted BOOLEAN,
        fetched_at TIMESTAMP, PRIMARY KEY (symbol, timeframe, ts))""",
    """CREATE TABLE IF NOT EXISTS signal_searches (
        search_id VARCHAR PRIMARY KEY, created_at TIMESTAMP, strategy_name VARCHAR,
        strategy_version VARCHAR, strategy_yaml VARCHAR, metadata VARCHAR, results INTEGER)""",
    """CREATE TABLE IF NOT EXISTS signal_results (
        search_id VARCHAR, symbol VARCHAR, ts VARCHAR, match_type VARCHAR, score DOUBLE, row_json VARCHAR)""",
]


def bars_per_session(timeframe: str) -> float:
    if timeframe in INTRADAY:
        return math.ceil(390 / INTRADAY[timeframe])
    return 1.0 if timeframe == "1d" else 0.2


def nyse_business_days(start: date, end: date) -> list[date]:
    import QuantLib as ql

    cal = ql.UnitedStates(ql.UnitedStates.NYSE)
    out, d = [], start
    while d <= end:
        if cal.isBusinessDay(ql.Date(d.day, d.month, d.year)):
            out.append(d)
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ archive
class Archive:
    """Local DuckDB store of fetched bars and saved searches. Open per operation:
    `with Archive() as a: ...` so the CLI and the dashboard don't hold the file locked."""

    def __init__(self, path: str | Path = DEFAULT_ARCHIVE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path))
        for ddl in SCHEMA:
            self.con.execute(ddl)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.con.close()

    def upsert_bars(self, symbol: str, timeframe: str, df: pd.DataFrame, source: str, adjusted: bool = True) -> int:
        if df.empty:
            return 0
        rows = df.reset_index().rename(columns={df.index.name or "index": "ts"})
        rows = rows.assign(symbol=symbol, timeframe=timeframe, source=source, adjusted=adjusted,
                           fetched_at=pd.Timestamp.utcnow().tz_localize(None))
        cols = ["symbol", "timeframe", "ts", "open", "high", "low", "close", "volume", "session", "source",
                "adjusted", "fetched_at"]
        self.con.register("_in", rows[cols])
        self.con.execute(f"INSERT OR REPLACE INTO bars ({', '.join(cols)}) SELECT {', '.join(cols)} FROM _in")
        self.con.unregister("_in")
        return len(rows)

    def read_bars(self, symbol: str, timeframe: str, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
        df = self.con.cursor().execute(
            "SELECT ts, open, high, low, close, volume, session, source, adjusted FROM bars "
            "WHERE symbol = ? AND timeframe = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            [symbol, timeframe, start_utc, end_utc]).df()
        return df.set_index("ts")

    def coverage(self, symbol: str, timeframe: str) -> tuple:
        return self.con.cursor().execute(
            "SELECT min(ts), max(ts), count(*) FROM bars WHERE symbol = ? AND timeframe = ?",
            [symbol, timeframe]).fetchone()


# -------------------------------------------------------------------- yahoo
def fetch_yahoo(symbol: str, timeframe: str, start: date, end: date, extended: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """Bars indexed by naive UTC timestamps (daily: the session date at 00:00). Returns notes on limits hit."""
    import logging

    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    notes = []
    if timeframe in YAHOO_MAX_DAYS:
        earliest = date.today() - timedelta(days=YAHOO_MAX_DAYS[timeframe])
        if start < earliest:
            notes.append(f"Yahoo only serves {timeframe} bars from about {earliest}; earlier bars come only "
                         "from what the archive already holds.")
            start = earliest
    if start > end:
        return pd.DataFrame(), notes
    chunk = YAHOO_CHUNK_DAYS.get(timeframe, 100_000)
    frames, d = [], start
    interval = "60m" if timeframe == "1h" else timeframe
    while d <= end:
        stop = min(d + timedelta(days=chunk), end + timedelta(days=1))
        df = yf.download(symbol, start=d.isoformat(), end=stop.isoformat(), interval=interval, auto_adjust=True,
                         prepost=extended, progress=False, threads=False, multi_level_index=False)
        if df is not None and not df.empty:
            frames.append(df)
        d = stop
    if not frames:
        return pd.DataFrame(), notes + [f"Yahoo returned no {timeframe} bars for {symbol} between {start} and {end}."]
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    idx = pd.DatetimeIndex(df.index)
    if timeframe in INTRADAY:
        local = idx.tz_convert(EXCHANGE_TZ) if idx.tz is not None else idx.tz_localize(EXCHANGE_TZ)
        t = local.time
        df["session"] = np.where([(SESSION_OPEN <= x < SESSION_CLOSE) for x in t], "regular", "extended")
        df.index = local.tz_convert("UTC").tz_localize(None)
    else:
        df.index = idx.tz_localize(None).normalize() if idx.tz is not None else idx.normalize()
        df["session"] = "regular"
    df.index.name = "ts"
    return df.dropna(subset=["close"]), notes


# ---------------------------------------------------------------- synthetic
_EPOCH, _HORIZON = date(2015, 1, 2), date(2035, 12, 31)


def _seed(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


@lru_cache(maxsize=64)
def _daily_walk(symbol: str) -> pd.DataFrame:
    """Session open/close over a fixed epoch..horizon, so a day's values never depend on the range asked for."""
    days = nyse_business_days(_EPOCH, _HORIZON)
    rng = np.random.default_rng(_seed("daily", symbol))
    n = len(days)
    vol = np.clip(0.18 + 0.12 * np.sin(np.arange(n) / 90) + rng.normal(0, 0.03, n), 0.08, 0.6) / np.sqrt(252)
    rets = 0.0003 + vol * rng.standard_normal(n)
    close = 100 * np.exp(np.cumsum(rets))
    gap = rng.normal(0, 0.3, n) * vol
    open_ = np.r_[100, close[:-1]] * np.exp(gap)
    return pd.DataFrame({"open": open_, "close": close, "vol": vol}, index=pd.DatetimeIndex(days))


def _session_minutes(symbol: str, day: date, open_: float, close: float, vol_day: float) -> pd.DataFrame:
    rng = np.random.default_rng(_seed("minutes", symbol, day))
    steps = rng.standard_normal(390) * vol_day / np.sqrt(390)
    walk = np.cumsum(steps)
    bridge = walk - np.linspace(0, 1, 390) * walk[-1]          # pinned to the day's close
    path = np.exp(np.log(open_) + np.linspace(0, 1, 390) * np.log(close / open_) + bridge)
    prev = np.r_[open_, path[:-1]]
    wick = np.abs(rng.normal(0, vol_day / np.sqrt(390) / 2, 390)) * path
    u = 1 + 1.5 * np.linspace(-1, 1, 390) ** 2
    vol = 2e4 * u * rng.lognormal(0, 0.35, 390)
    start = datetime.combine(day, SESSION_OPEN)
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(390)]).tz_localize(EXCHANGE_TZ)
    return pd.DataFrame({"open": prev, "high": np.maximum(prev, path) + wick, "low": np.minimum(prev, path) - wick,
                         "close": path, "volume": vol}, index=idx)


def synthetic_bars(symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
    """Regular-session synthetic bars, indexed by naive UTC timestamps like the archive."""
    walk = _daily_walk(symbol)
    walk = walk[(walk.index >= pd.Timestamp(start)) & (walk.index <= pd.Timestamp(end))]
    frames = [_session_minutes(symbol, d.date(), r.open, r.close, r.vol) for d, r in walk.iterrows()]
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "session"])
    m = pd.concat(frames)
    rule = {"1m": None, "5m": "5min", "15m": "15min", "30m": "30min", "1h": "60min", "1d": "D", "1wk": "W-FRI"}[timeframe]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if rule is None:
        out = m
    elif timeframe in INTRADAY:
        # session-anchored bins, like exchange bars: 09:30, 10:30, ... for 1h
        parts = [g.resample(rule, origin=g.index[0], label="left", closed="left").agg(agg)
                 for _, g in m.groupby(m.index.date)]
        out = pd.concat(parts).dropna()
    else:
        out = m.tz_localize(None).resample(rule).agg(agg).dropna()
        if timeframe == "1wk":  # Yahoo labels weeks by their Monday
            out.index = out.index - pd.Timedelta(days=4)
        out["session"] = "regular"
        out.index.name = "ts"
        return out
    out = out.copy()
    out["session"] = "regular"
    out.index = out.index.tz_convert("UTC").tz_localize(None)
    out.index.name = "ts"
    return out


# ------------------------------------------------------------------- loader
@dataclass
class DataReport:
    symbol: str
    timeframe: str
    source: str
    timezone: str
    requested_start: str
    requested_end: str
    first_bar: str | None = None
    last_bar: str | None = None
    bars_in_range: int = 0
    warmup_bars_requested: int = 0
    warmup_bars_available: int = 0
    sessions_in_range: int = 0
    holidays_in_range: int = 0
    sessions_before_data: int = 0
    missing_bars: int = 0
    incomplete_bars_dropped: int = 0
    adjusted: bool = True
    extended_hours_included: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def to_local(value, tz: str, end_of_day: bool = False) -> pd.Timestamp:
    """'2026-01-01' or '2026-01-01 10:30' (or a date/datetime) as a timestamp in `tz`."""
    ts = pd.Timestamp(value)
    date_only = isinstance(value, date) and not isinstance(value, datetime) or (
        isinstance(value, str) and len(value.strip()) <= 10)
    if ts.tzinfo is None:
        ts = ts.tz_localize(tz)
    else:
        ts = ts.tz_convert(tz)
    if end_of_day and date_only:
        ts = ts + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return ts


def _calendar_days_for(bars: int, timeframe: str) -> int:
    per_day = bars_per_session(timeframe) if timeframe != "1wk" else 0.2
    return int(math.ceil(bars / per_day * 7 / 5 * 1.25)) + 7


def load_history(symbol: str, timeframe: str, start, end, tz: str = EXCHANGE_TZ, market_hours_only: bool = True,
                 warmup_bars: int = 200, source: str = "yahoo", archive_path: str | Path = DEFAULT_ARCHIVE,
                 now: datetime | None = None) -> tuple[pd.DataFrame, pd.Series, DataReport]:
    """Bars for [start - warm-up, end] in `tz`, a mask of the bars inside [start, end], and a report."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    t0, t1 = to_local(start, tz), to_local(end, tz, end_of_day=True)
    rep = DataReport(symbol, timeframe, source, tz, str(t0), str(t1), warmup_bars_requested=int(warmup_bars),
                     extended_hours_included=not market_hours_only and timeframe in INTRADAY and source != "synthetic")
    now_ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz=tz)
    now_ts = now_ts.tz_localize(tz) if now_ts.tzinfo is None else now_ts.tz_convert(tz)
    fetch_from = (t0 - pd.Timedelta(days=_calendar_days_for(warmup_bars, timeframe))).date()
    fetch_to = min(t1.date(), now_ts.date())
    if source == "synthetic":
        raw = synthetic_bars(symbol, timeframe, fetch_from, fetch_to)
        if not market_hours_only and timeframe in INTRADAY:
            rep.warnings.append("Synthetic data has regular-session bars only.")
    elif source in ("yahoo", "archive"):
        with Archive(archive_path) as arc:
            if source == "yahoo":
                got, notes = fetch_yahoo(symbol, timeframe, fetch_from, fetch_to,
                                         extended=not market_hours_only and timeframe in INTRADAY)
                rep.warnings += notes
                arc.upsert_bars(symbol, timeframe, got, "yahoo")
            pad = pd.Timedelta(days=1)
            raw = arc.read_bars(symbol, timeframe, (pd.Timestamp(fetch_from) - pad).to_pydatetime(),
                                (pd.Timestamp(fetch_to) + 2 * pad).to_pydatetime())
            if not raw.empty and (raw["source"] == "synthetic").any():
                rep.warnings.append("Archive contains synthetic bars.")
            if not raw.empty:
                rep.adjusted = bool(raw["adjusted"].all())
    else:
        raise ValueError("source must be yahoo, archive or synthetic")

    if raw.empty:
        rep.warnings.append(f"No {timeframe} bars available for {symbol}.")
        return _empty_frame(tz), pd.Series(dtype=bool), rep

    df = raw.copy()
    if timeframe in INTRADAY:
        df.index = pd.DatetimeIndex(df.index).tz_localize("UTC").tz_convert(tz)
        if market_hours_only and "session" in df:
            df = df[df["session"] == "regular"]
        step = pd.Timedelta(minutes=INTRADAY[timeframe])
        bar_end = df.index + step
    else:
        # daily/weekly bars are session dates; they close at 16:00 exchange time on their last day
        days = pd.DatetimeIndex(df.index).normalize()
        df.index = days.tz_localize(tz)
        last_day = days + (pd.Timedelta(days=4) if timeframe == "1wk" else pd.Timedelta(0))
        bar_end = pd.DatetimeIndex(last_day + pd.Timedelta(hours=16)).tz_localize(EXCHANGE_TZ).tz_convert(tz)
    done = np.asarray(bar_end <= now_ts)
    rep.incomplete_bars_dropped = int((~done).sum())
    df = df[done][["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    in_range = pd.Series((df.index >= t0) & (df.index <= t1), index=df.index)
    rep.bars_in_range = int(in_range.sum())
    if rep.bars_in_range:
        first_in = df.index[in_range.to_numpy()][0]
        rep.warmup_bars_available = int((df.index < first_in).sum())
        rep.first_bar, rep.last_bar = str(first_in), str(df.index[in_range.to_numpy()][-1])
    else:
        rep.warnings.append(f"No completed {timeframe} bars for {symbol} between {t0} and {t1}.")
    if rep.warmup_bars_available < warmup_bars and rep.bars_in_range:
        rep.warnings.append(f"Only {rep.warmup_bars_available} warm-up bars before {rep.first_bar} "
                            f"({warmup_bars} requested); early bars may not be evaluable.")
    _count_missing(df[in_range.to_numpy()], timeframe, t0, min(t1, now_ts), rep)
    if rep.last_bar and pd.Timestamp(rep.last_bar) < min(t1, now_ts) - pd.Timedelta(days=4):
        rep.warnings.append(f"Data ends at {rep.last_bar}, before the requested end; later bars weren't searched.")
    return df, in_range, rep


def _empty_frame(tz):
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                        index=pd.DatetimeIndex([], tz=tz), dtype=float)


def _count_missing(df: pd.DataFrame, timeframe: str, t0: pd.Timestamp, t1: pd.Timestamp, rep: DataReport) -> None:
    """Sessions and holidays in range; sessions before the data starts; bars missing inside the data."""
    if timeframe == "1wk" or t1 < t0:
        return
    d0, d1 = t0.tz_convert(EXCHANGE_TZ).date(), t1.tz_convert(EXCHANGE_TZ).date()
    sessions = nyse_business_days(d0, d1)
    weekdays = sum(1 for i in range((d1 - d0).days + 1) if (d0 + timedelta(days=i)).weekday() < 5)
    rep.sessions_in_range, rep.holidays_in_range = len(sessions), weekdays - len(sessions)
    if df.empty:
        return
    local_days = df.index.tz_convert(EXCHANGE_TZ).date if timeframe in INTRADAY else df.index.date
    first, last = local_days[0], local_days[-1]
    rep.sessions_before_data = sum(1 for d in sessions if d < first)
    if rep.sessions_before_data:
        rep.warnings.append(f"No data for the first {rep.sessions_before_data} sessions of the range "
                            f"(data starts {first}); those weren't searched.")
    have = pd.Series(1, index=local_days).groupby(level=0).size()
    expected = bars_per_session(timeframe)
    rep.missing_bars = int(sum(max(expected - have.get(d, 0), 0) for d in sessions if first <= d <= last))
    if rep.missing_bars:
        rep.warnings.append(f"{rep.missing_bars} expected bars are missing inside the data (early-close days "
                            "such as the day after Thanksgiving count as missing; the calendar has no half-days).")
