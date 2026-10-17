"""Data-health checks. They run before any rule and can block or downgrade other overlays.

    effect "block"      the dependent features are not calculated (shown as DATA ERROR)
    effect "downgrade"  they are calculated but labelled (STALE DATA, lower data-quality score)
    effect "none"       informational

Nothing here repairs data silently: duplicates and a still-forming bar are removed only
with a check that says so, and nothing missing is filled in.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
SPLIT_RATIOS = (2, 3, 4, 5, 10, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10)


@dataclass
class Check:
    id: str
    name: str
    passed: bool
    detail: str
    symbol: str = ""
    effect: str = "none"              # none | downgrade | block
    affects: list[str] = field(default_factory=list)
    action: str = ""                  # what to do about it

    def as_dict(self) -> dict:
        return asdict(self)


@lru_cache(maxsize=64)
def _sessions(start: date, end: date) -> tuple[date, ...]:
    from ..history.bars import nyse_business_days

    return tuple(nyse_business_days(start, end))


def last_completed_session(now: datetime) -> date:
    """The latest NYSE session whose 16:00 ET close has passed."""
    now_et = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)
    d = now_et.date() if now_et.time() >= dtime(16, 0) else now_et.date() - timedelta(days=1)
    sess = _sessions(d - timedelta(days=10), d)
    return sess[-1] if sess else d


def completed_bars(bars: pd.DataFrame, now: datetime, timeframe: str = "1d") -> tuple[pd.DataFrame, bool]:
    """Drop a daily bar dated today while today's session is still open. Returns (bars, dropped?)."""
    if bars.empty or timeframe != "1d":
        return bars, False
    now_et = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)
    last = pd.Timestamp(bars.index[-1]).date()
    if last >= now_et.date() and now_et.time() < dtime(16, 0):
        return bars.iloc[:-1], True
    return bars, False


def bar_health(symbol: str, bars: pd.DataFrame, now: datetime, thresholds: dict, source: str,
               dropped_forming: bool = False) -> tuple[pd.DataFrame, list[Check]]:
    """Check one symbol's daily bars. Returns (bars with duplicates removed, checks)."""
    out: list[Check] = []
    add = lambda *a, **k: out.append(Check(*a, symbol=symbol, **k))  # noqa: E731
    if bars is None or bars.empty:
        add("bars_present", "Price bars available", False, "No price bars for this symbol.", effect="block",
            affects=["technical rules", "historical evidence", "market overview"],
            action="Run `python scripts/ingest.py` (add the symbol to config.yaml → universe).")
        return bars, out
    add("source", "Data source", True, f"{source}" + (" (illustrative, not real market data)" if source == "synthetic" else ""))
    if not bars.index.is_monotonic_increasing:
        add("order", "Bars in time order", False, "Bars are out of order.", effect="block",
            affects=["technical rules", "historical evidence"], action="Re-run ingest; the store should sort by date.")
        return bars, out
    add("order", "Bars in time order", True, "Timestamps increase.")
    dup = bars.index.duplicated(keep="last")
    if dup.any():
        add("duplicates", "No duplicate bars", False, f"{int(dup.sum())} duplicate timestamp(s); the last copy of each "
            "is used.", effect="downgrade", affects=["technical rules"], action="Re-run ingest to rebuild the table.")
        bars = bars[~dup]
    else:
        add("duplicates", "No duplicate bars", True, "Each timestamp appears once.")
    px = bars[["open", "high", "low", "close"]]
    bad = px.isna().any(axis=1) | (px <= 0).any(axis=1)
    if bad.iloc[-1]:
        add("values", "Valid prices", False, "The latest bar has a missing or non-positive price.", effect="block",
            affects=["technical rules"], action="Re-run ingest for this symbol.")
    elif bad.any():
        add("values", "Valid prices", False, f"{int(bad.sum())} older bar(s) have missing or non-positive prices.",
            effect="downgrade", affects=["historical evidence"], action="Re-run ingest for this symbol.")
    else:
        add("values", "Valid prices", True, "Every bar has positive open, high, low and close.")
    first, last = pd.Timestamp(bars.index[0]).date(), pd.Timestamp(bars.index[-1]).date()
    expected = set(_sessions(first, last))
    have = {pd.Timestamp(t).date() for t in bars.index}
    missing = len(expected - have)
    if missing:
        add("missing", "No missing sessions", False, f"{missing:,} NYSE session(s) between {first} and {last} have no bar "
            f"({missing / max(len(expected), 1):.1%}).", effect="downgrade", affects=["historical evidence"],
            action="Historical confidence is downgraded; re-run ingest to fill the gap.")
    else:
        add("missing", "No missing sessions", True, f"Every NYSE session from {first} to {last} has a bar.")
    extra = len(have - expected)
    if extra:
        add("calendar", "Bars only on trading days", False, f"{extra:,} bar(s) fall on weekends or NYSE holidays "
            "(typical of synthetic data).", effect="none", affects=[], action="Use real data for live decisions.")
    c = bars["close"].to_numpy(float)
    ratio = c[1:] / c[:-1]
    jumps = [i + 1 for i, r in enumerate(ratio) if np.isfinite(r) and any(abs(r * k - 1) < 0.03 for k in SPLIT_RATIOS)
             and abs(r - 1) > 0.35]
    if jumps:
        when = pd.Timestamp(bars.index[jumps[-1]]).date()
        add("split", "No unadjusted splits", False, f"Close jumps by a split-like ratio ({ratio[jumps[-1] - 1]:.2f}×) on "
            f"{when}; history may be unadjusted.", effect="block", affects=["historical evidence", "backtest"],
            action="Historical calculations are blocked until adjusted prices are loaded (re-run ingest).")
    else:
        add("split", "No unadjusted splits", True, "No day-to-day move matches a split ratio.")
    if dropped_forming:
        add("forming", "Completed bars only", True, "Today's bar is still forming and is excluded.")
    sess = _sessions(last, last_completed_session(now)) if last <= last_completed_session(now) else ()
    behind = max(len(sess) - 1, 0)
    lim = int(thresholds["stale_data_sessions"])
    if behind > lim:
        add("fresh", "Data is current", False, f"Latest bar is {last}, {behind} session(s) behind the last completed "
            f"session ({last_completed_session(now)}); the limit is {lim}.", effect="downgrade",
            affects=["technical rules", "market overview", "account fit"],
            action="Run `python scripts/ingest.py` to refresh.")
    else:
        add("fresh", "Data is current", True, f"Latest bar {last}; {behind} session(s) behind (limit {lim}).")
    return bars, out


def chain_health(symbol: str, chain: pd.DataFrame | None, fetched_at: datetime | None, now: datetime, thresholds: dict,
                 source: str, delayed_minutes: int = 0) -> list[Check]:
    out: list[Check] = []
    add = lambda *a, **k: out.append(Check(*a, symbol=symbol, **k))  # noqa: E731
    if chain is None or chain.empty:
        add("chain_present", "Option chain available", False, "No option chain loaded.", effect="block",
            affects=["option liquidity", "account fit", "structures"], action="Load the chain on the Option chain tab.")
        return out
    n = len(chain)
    bid, ask = chain["bid"].astype(float), chain["ask"].astype(float)
    crossed = int(((bid > ask) & (ask > 0)).sum())
    add("crossed", "No crossed markets", crossed == 0, f"{crossed:,} of {n:,} contracts have bid above ask."
        if crossed else "No contract has bid above ask.", effect="downgrade" if crossed else "none",
        affects=["structures"] if crossed else [], action="Those contracts are excluded from structures." if crossed else "")
    zero = int((bid <= 0).sum())
    add("zero_bids", "Bids present", zero < n, f"{zero:,} of {n:,} contracts ({zero / n:.0%}) have no bid.",
        effect="none")
    if "delta" in chain:
        miss = int(chain["delta"].isna().sum())
        add("greeks", "Greeks present", miss == 0, f"{miss:,} contracts have no delta." if miss else "Every contract has Greeks.",
            effect="none")
    iv = chain["iv"] if "iv" in chain else chain.get("implied_vol")
    if iv is not None:
        badiv = int((iv.isna() | (iv <= 0) | (iv > 5)).sum())
        add("iv", "Implied volatility valid", badiv == 0, f"{badiv:,} contracts have a missing or implausible IV "
            "(≤ 0 or > 500%)." if badiv else "Every IV is between 0 and 500%.", effect="none")
    typ = chain["type"] if "type" in chain else chain.get("option_type")
    if typ is not None:
        kinds = set(typ.str.lower().unique())
        add("complete", "Calls and puts present", {"call", "put"} <= kinds, f"Types present: {', '.join(sorted(kinds))}; "
            f"{chain['expiration'].nunique():,} expirations.", effect="none" if {"call", "put"} <= kinds else "downgrade",
            affects=[] if {"call", "put"} <= kinds else ["structures"])
    if fetched_at is not None:
        age = (now - fetched_at).total_seconds()
        lim = float(thresholds["maximum_quote_age_seconds"])
        add("quote_age", "Quotes are fresh", age <= lim, f"Fetched {age:,.0f} s ago (limit {lim:,.0f} s)"
            + (f"; the source itself is delayed about {delayed_minutes} minutes" if delayed_minutes else ""),
            effect="none" if age <= lim else "downgrade", affects=[] if age <= lim else ["account fit", "option liquidity"],
            action="" if age <= lim else "Reload the chain; account-fit results are shown as stale.")
    elif "snapshot_date" in chain:
        snap = pd.Timestamp(chain["snapshot_date"].iloc[0]).date()
        fresh = snap >= last_completed_session(now)
        add("quote_age", "Quotes are fresh", fresh, f"Stored snapshot from {snap}.", effect="none" if fresh else "downgrade",
            affects=[] if fresh else ["account fit", "option liquidity"],
            action="" if fresh else "Use the Option chain tab for current quotes.")
    add("source", "Quote source", True, source + (f", delayed about {delayed_minutes} min" if delayed_minutes else ""))
    return out


BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def intraday_health(symbol: str, bars: pd.DataFrame, timeframe: str, now: datetime, source: str,
                    delay_minutes: int = 15) -> tuple[pd.DataFrame, list[Check]]:
    """Intraday bars (index = bar START, exchange time): drop the bar still forming, then check order,
    duplicates and latency (time since the last completed bar ended, allowing the feed's delay)."""
    out: list[Check] = []
    add = lambda *a, **k: out.append(Check(*a, symbol=symbol, **k))  # noqa: E731
    if bars is None or bars.empty:
        add("bars_present", f"{timeframe} bars available", False, "No intraday bars.", effect="block",
            affects=["technical rules"], action="Check the monitor's data feed.")
        return bars, out
    width = pd.Timedelta(minutes=BAR_MINUTES[timeframe])
    now_et = pd.Timestamp(now).tz_convert(ET).tz_localize(None) if pd.Timestamp(now).tzinfo else pd.Timestamp(now)
    idx = pd.DatetimeIndex(bars.index)
    idx = idx.tz_convert(ET).tz_localize(None) if idx.tz is not None else idx
    done = idx + width <= now_et
    if not done.all():
        add("forming", "Completed bars only", True, f"{int((~done).sum())} bar(s) still forming are excluded.")
    bars = bars[np.asarray(done)]
    idx = idx[np.asarray(done)]
    if not idx.is_monotonic_increasing:
        add("order", "Bars in time order", False, "Bars are out of order.", effect="block", affects=["technical rules"])
        return bars, out
    dup = idx.duplicated(keep="last")
    if dup.any():
        add("duplicates", "No duplicate bars", False, f"{int(dup.sum())} duplicate bar(s); last copy used.",
            effect="downgrade", affects=["technical rules"])
        bars, idx = bars[~dup], idx[~dup]
    if len(idx):
        age = (now_et - (idx[-1] + width)).total_seconds() / 60
        lim = 2 * BAR_MINUTES[timeframe] + delay_minutes
        add("latency", "Bar latency", age <= lim, f"Last completed {timeframe} bar ended {age:,.0f} min ago (limit "
            f"{lim} min, allowing a {delay_minutes}-minute feed delay).", effect="none" if age <= lim else "downgrade",
            affects=[] if age <= lim else ["technical rules"], action="" if age <= lim else "Check the feed or market hours.")
    add("source", "Data source", True, source)
    return bars, out


def summarize(checks: list[Check]) -> dict:
    failed = [c for c in checks if not c.passed]
    return {"passed": sum(c.passed for c in checks), "failed": len(failed),
            "blocking": [c for c in failed if c.effect == "block"],
            "downgrading": [c for c in failed if c.effect == "downgrade"],
            "score": max(0.0, 100.0 - 40 * sum(c.effect == "block" for c in failed)
                         - 10 * sum(c.effect == "downgrade" for c in failed))}


def blocked(checks: list[Check], feature: str) -> list[Check]:
    return [c for c in checks if not c.passed and c.effect == "block" and feature in c.affects]


def downgraded(checks: list[Check], feature: str) -> list[Check]:
    return [c for c in checks if not c.passed and c.effect == "downgrade" and feature in c.affects]
