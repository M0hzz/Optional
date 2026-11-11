"""Market data, nothing else: an option chain from CBOE's delayed quotes and daily price bars from Yahoo.

Plain Python on purpose (no pandas). Quotes are about 15 minutes delayed. Nothing here places a trade.
"""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import date, datetime, timezone

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=5y&interval=1d"
OCC_RE = re.compile(r"^(?P<root>.+?)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=5d&interval=1d"
YAHOO_INDEX = {"SPX": "^SPX", "NDX": "^NDX", "RUT": "^RUT", "VIX": "^VIX", "XSP": "^XSP", "DJX": "^DJI",
               "COMP": "^IXIC", "IXIC": "^IXIC", "NASDAQ": "^IXIC", "DJI": "^DJI", "DOW": "^DJI"}
_GATE = threading.BoundedSemaphore(4)            # CBOE answers 429 to bursts


class MarketError(Exception):
    """A problem worth showing to the person as it is."""


def clean_symbol(symbol: str) -> str:
    """brk-b, BRK/B -> BRK.B; ^spx -> SPX."""
    s = re.sub(r"[^A-Z0-9.]", "", re.sub(r"[-/ ]", ".", str(symbol).strip().upper()))
    if not s or len(s) > 8:
        raise MarketError("Type a ticker symbol, like SPY or IWM.")
    return s


def _get(url: str, timeout: float = 20):
    import requests

    return requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (optlab)"})


def fetch_cboe(symbol: str, get=_get, retries: int = 4, pause: float = 1.5) -> dict:
    """The delayed-quotes JSON for a stock or ETF (SPY) or an index (SPX is served as _SPX).
    Too-many-requests answers are retried; they never count as "no such ticker"."""
    sym = clean_symbol(symbol)
    status = None
    for name in (sym, f"_{sym}"):
        for attempt in range(retries + 1):
            with _GATE:
                r = get(CBOE_URL.format(name))
            status = r.status_code
            if status == 200:
                return r.json()
            if status != 429 and status < 500:
                break
            if attempt < retries:
                time.sleep(pause * 2 ** attempt)
        else:
            raise MarketError(f"CBOE is busy right now (HTTP {status}). Try again in a moment.")
    raise MarketError(f"No options found for {sym}. Check the ticker.")


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_chain(payload: dict, today: date | None = None) -> dict:
    """{'symbol', 'price', ..., 'expirations': [{'date', 'dte', 'calls': [...], 'puts': [...]}]}.
    Adjusted contracts (a root that isn't the ticker, such as B1SPY) and expired ones are left out."""
    today = today or date.today()
    data = (payload or {}).get("data") or {}
    base = re.sub(r"[^A-Z]", "", str(data.get("symbol") or "").upper())
    price = _num(data.get("current_price"))
    by_exp: dict[date, dict] = {}
    for o in data.get("options") or []:
        m = OCC_RE.match(str(o.get("option") or ""))
        if not m or not re.fullmatch(f"{base}W?", m["root"]):
            continue
        exp = datetime.strptime(m["exp"], "%y%m%d").date()
        dte = (exp - today).days
        if dte < 0:
            continue
        bid, ask = _num(o.get("bid")) or 0.0, _num(o.get("ask")) or 0.0
        last = _num(o.get("last_trade_price")) if o.get("last_trade_time") else None
        mid = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else (last if last and last > 0 else None)
        iv = _num(o.get("iv"))
        row = {"occ": o["option"], "strike": int(m["strike"]) / 1000, "bid": bid, "ask": ask, "mid": mid,
               "iv": iv if iv and iv > 0 else None, "delta": _num(o.get("delta")), "theta": _num(o.get("theta")),
               "volume": int(_num(o.get("volume")) or 0), "oi": int(_num(o.get("open_interest")) or 0)}
        side = by_exp.setdefault(exp, {"date": exp.isoformat(), "dte": dte, "calls": [], "puts": []})
        side["calls" if m["cp"] == "C" else "puts"].append(row)
    if not by_exp or not price:
        raise MarketError("CBOE sent no usable option prices for this ticker.")
    exps = [by_exp[k] for k in sorted(by_exp)]
    for e in exps:
        e["calls"].sort(key=lambda r: r["strike"])
        e["puts"].sort(key=lambda r: r["strike"])
    iv30 = _num(data.get("iv30"))
    return {"symbol": str(data.get("symbol") or "").lstrip("^"), "price": price,
            "prev_close": _num(data.get("prev_day_close")), "change": _num(data.get("price_change")),
            "change_pct": _num(data.get("price_change_percent")), "iv30": iv30 / 100 if iv30 else None,
            "as_of": payload.get("timestamp"), "expirations": exps}


def fetch_bars(symbol: str, get=_get) -> list[dict]:
    """About five years of daily bars from Yahoo: [{'d', 'o', 'h', 'l', 'c', 'v'}], oldest first."""
    sym = clean_symbol(symbol)
    r = get(YAHOO_URL.format(YAHOO_INDEX.get(sym, sym.replace(".", "-"))))
    if r.status_code != 200:
        raise MarketError(f"No price history for {sym} right now (HTTP {r.status_code}).")
    try:
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        out = []
        for i, ts in enumerate(res["timestamp"]):
            o, h, lo, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, lo, c):
                continue
            out.append({"d": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), "o": round(o, 4),
                        "h": round(h, 4), "l": round(lo, 4), "c": round(c, 4), "v": int(v or 0)})
    except (KeyError, IndexError, TypeError, ValueError):
        raise MarketError(f"No price history for {sym} right now.") from None
    if len(out) < 25:
        raise MarketError(f"Not enough price history for {sym}.")
    return out


def fetch_quote(symbol: str, get=_get) -> dict:
    """A light quote for the monitor (no option chain): price, change from the previous close, today's
    volume and the name. Stocks, ETFs and indexes (SPX, NDX, COMP or NASDAQ, DJI, VIX, RUT)."""
    sym = clean_symbol(symbol)
    r = get(YAHOO_QUOTE_URL.format(YAHOO_INDEX.get(sym, sym.replace(".", "-"))))
    if r.status_code != 200:
        raise MarketError(f"No quote for {sym}. Check the ticker.")
    try:
        res = r.json()["chart"]["result"][0]
        meta, closes = res["meta"], res["indicators"]["quote"][0]["close"]
        days = [datetime.fromtimestamp(ts, timezone.utc).date() for ts in res["timestamp"]]
        price = float(meta["regularMarketPrice"])
        today = datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc).date()
        done = [c for d, c in zip(days, closes) if c is not None and d < today]   # closes of completed days
        prev = float(done[-1]) if done else None
    except (KeyError, IndexError, TypeError, ValueError):
        raise MarketError(f"No quote for {sym} right now.") from None
    return {"symbol": sym, "name": meta.get("shortName") or meta.get("longName") or sym, "price": round(price, 4),
            "prev_close": round(prev, 4) if prev else None,
            "change_pct": round(100 * (price / prev - 1), 2) if prev else None,
            "volume": int(meta.get("regularMarketVolume") or 0), "day": today.isoformat()}


def levels(bars: list[dict], price: float | None = None) -> dict:
    """What the simulator needs from the bars, each by a fixed rule:

        resistance   the lowest of the 20-day and 60-day highs that is above the price (None at new highs)
        support      the highest of the 20-day and 60-day lows that is below the price (None at new lows)
        atr          average true range, 14 days, in dollars
        hv           20-day realised volatility, a yearly rate (0.20 = 20%)
        avg_volume   20-day average volume (the latest bar left out)
        rel_volume   the latest bar's volume / avg_volume
    """
    price = price or bars[-1]["c"]
    past = bars[:-1]                              # completed days: today's bar is still forming
    highs = [max(b["h"] for b in past[-n:]) for n in (20, 60)]
    lows = [min(b["l"] for b in past[-n:]) for n in (20, 60)]
    above = [h for h in highs if h > price * 1.001]
    below = [lo for lo in lows if lo < price * 0.999]
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars[-15:-1], bars[-14:])]
    rets = [math.log(b["c"] / p["c"]) for p, b in zip(bars[-21:-1], bars[-20:])]
    mean = sum(rets) / len(rets)
    hv = math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1) * 252)
    vols = [b["v"] for b in past[-20:] if b["v"]]
    avg_v = sum(vols) / len(vols) if vols else None
    return {"resistance": round(min(above), 2) if above else None, "support": round(max(below), 2) if below else None,
            "high20": round(highs[0], 2), "low20": round(lows[0], 2), "atr": round(sum(trs) / len(trs), 4),
            "hv": round(hv, 4), "avg_volume": round(avg_v) if avg_v else None,
            "rel_volume": round(bars[-1]["v"] / avg_v, 2) if avg_v and bars[-1]["v"] else None}


class Cache:
    """One fetch per key every `ttl` seconds, however many browser tabs ask. On a failed refresh the last
    good answer is served (marked stale) rather than nothing."""

    def __init__(self, ttl: float, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._data: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str, make):
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self._data.get(key)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1], False
            try:
                val = make()
            except MarketError:
                if hit:
                    return hit[1], True
                raise
            self._data[key] = (self.clock(), val)
            return val, False
