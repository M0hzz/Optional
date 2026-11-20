"""The whole server: four read-only data routes and the page. No database, no accounts, no orders.

    GET /api/chain/{symbol}          price, expirations (date, days left, how many contracts)
                                     (from Tastytrade, live, when a Tastytrade login is set up; else CBOE)
    GET /api/chain/{symbol}/{date}   the calls and puts of one expiration
    GET /api/bars/{symbol}           daily bars and the levels the simulator uses
    GET /api/quotes?symbols=A,B      light quotes for the home page monitor (no option chain needed)
    GET /api/earnings?symbols=A,B    the next earnings report within two weeks (Nasdaq's calendar)
    GET /api/source                  where option quotes come from, and any problem with the Tastytrade login
    GET /api/version                 changes when a new version is installed: open pages reload themselves

It answers only to localhost and your Tailscale name (OPTLAB_ALLOWED_HOSTS to change), so a web page you
happen to visit can't reach it through your browser.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import market
from .. import tasty as tasty_mod

PKG = Path(__file__).resolve().parents[1]
WEB = PKG / "web"


def fingerprint(files) -> str:
    """A short hash of the files' names, sizes and modification times: it changes when any of them does."""
    h = hashlib.sha1()
    for f in sorted(files):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f.relative_to(PKG)}:{st.st_size}:{st.st_mtime_ns};".encode())
    return h.hexdigest()[:12]


def web_files():
    return [f for f in WEB.rglob("*") if f.is_file() and f.suffix in (".js", ".css", ".html")]
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")


def host_ok(host: str, allowed: list[str]) -> bool:
    h = host.strip().lower()
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else (h.split(":")[0] if h.count(":") == 1 else h)
    return any(h == a or (a.startswith("*.") and h.endswith(a[1:]) and len(h) > len(a) - 1) for a in allowed)


def create_app(fetch_chain=market.fetch_cboe, fetch_bars=market.fetch_bars, fetch_quote=market.fetch_quote,
               fetch_quotes=market.fetch_quotes, fetch_earnings_day=market.fetch_earnings_day, chain_ttl: float = 15, bars_ttl: float = 600, quote_ttl: float = 2,
               allowed_hosts: list[str] | None = None, tasty=None) -> FastAPI:
    """`tasty`: a tasty.Tasty client for live option chains, "auto" to log in with the saved Tastytrade setup if
    there is one (python -m optlab.api does), or None for CBOE's delayed chains."""
    tasty_error = None
    if tasty == "auto":
        try:
            creds = tasty_mod.load_credentials()
            tasty = tasty_mod.Tasty(creds) if creds else None
        except market.MarketError as exc:
            tasty, tasty_error = None, str(exc)
    allowed = allowed_hosts or [h.strip().lower() for h in os.environ.get(
        "OPTLAB_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,*.ts.net").split(",") if h.strip()]
    app = FastAPI(title="optlab sim", docs_url=None, redoc_url=None, openapi_url=None)
    chains, quotes = market.Cache(chain_ttl), market.Cache(quote_ttl)
    live_sums, live_exps = market.Cache(chain_ttl), market.Cache(min(chain_ttl, 5))
    # The server code is fixed for the life of the process (a new version needs a restart); the page's files are
    # served from disk, so their part of the version is read on every request.
    server_version = fingerprint([f for f in PKG.rglob("*.py") if "__pycache__" not in f.parts])
    bars = {tf: market.Cache(min(ttl, bars_ttl)) for tf, (_, _, ttl) in market.TIMEFRAMES.items()}

    @app.middleware("http")
    async def guard(request, call_next):
        if not host_ok(request.headers.get("host", ""), allowed):
            return JSONResponse({"error": "This server doesn't answer to that host name."}, 400)
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                             "X-Frame-Options": "DENY", "Content-Security-Policy": CSP, "Cache-Control": "no-cache"})
        return resp

    @app.exception_handler(market.MarketError)
    async def market_error(request, exc):
        return JSONResponse({"error": str(exc)}, 502)

    @app.exception_handler(Exception)
    async def any_error(request, exc):          # never a stack trace in a response
        return JSONResponse({"error": "Something went wrong on the server."}, 500)

    def chain(symbol: str) -> tuple[dict, bool]:
        sym = market.clean_symbol(symbol)
        return chains.get(sym, lambda: market.parse_chain(fetch_chain(sym)))

    @app.get("/api/source")
    def source():
        return {"options": "tastytrade" if tasty else "cboe", "error": tasty_error}

    @app.get("/api/chain/{symbol}")
    def chain_summary(symbol: str):
        if tasty:
            sym = market.clean_symbol(symbol)
            c, stale = live_sums.get(sym, lambda: tasty_mod.summary(tasty, sym))
            return {**c, "stale": stale}
        c, stale = chain(symbol)
        return {**{k: v for k, v in c.items() if k != "expirations"}, "stale": stale,
                "expirations": [{"date": e["date"], "dte": e["dte"], "count": len(e["calls"]) + len(e["puts"])}
                                for e in c["expirations"]], "source": "cboe"}

    @app.get("/api/chain/{symbol}/{expiration}")
    def chain_expiration(symbol: str, expiration: str):
        if tasty:
            sym = market.clean_symbol(symbol)
            e, stale = live_exps.get(f"{sym}|{expiration}", lambda: tasty_mod.expiration(tasty, sym, expiration))
            return {**e, "stale": stale}
        c, stale = chain(symbol)
        for e in c["expirations"]:
            if e["date"] == expiration:
                return {"symbol": c["symbol"], "price": c["price"], "change_pct": c["change_pct"], "as_of": c["as_of"],
                        "stale": stale, "source": "cboe", **e}
        raise market.MarketError(f"{c['symbol']} has no options expiring on {expiration}.")

    @app.get("/api/bars/{symbol}")
    def bars_route(symbol: str, tf: str = "1d"):
        """Bars on a timeframe (5m, 15m, 1h, 1d, 1w). `levels` always comes from the daily bars: the simulator's
        volatility and average volume are daily figures whatever the chart shows."""
        sym = market.clean_symbol(symbol)
        if tf not in market.TIMEFRAMES:
            raise market.MarketError("Timeframe must be 5m, 15m, 1h, 1d or 1w.")
        daily, _ = bars["1d"].get(sym, lambda: fetch_bars(sym, "1d"))
        b = daily if tf == "1d" else bars[tf].get(sym, lambda: market.weekly(daily) if tf == "1w" else fetch_bars(sym, tf))[0]
        return {"symbol": sym, "tf": tf, "bars": b, "levels": market.levels(daily)}

    # Earnings dates for the next two weeks of weekdays, loaded in the background (the calendar answers one day at a
    # time, a couple of seconds each) and kept for 6 hours.
    earn: dict = {"days": {}, "loading": False}
    earn_lock = threading.Lock()

    def earnings_days(today: date) -> list[date]:
        out, d = [], today
        while len(out) < 10:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def load_earnings(days):
        try:
            for d in days:
                try:
                    got = fetch_earnings_day(d)
                except market.MarketError:
                    continue
                with earn_lock:
                    earn["days"][d] = (time.monotonic(), got)
        finally:
            earn["loading"] = False

    @app.get("/api/earnings")
    def earnings_route(symbols: str = ""):
        """The next earnings report within two weeks for each symbol: {SYM: {date, when: bmo | amc | unknown}}.
        `loading` is true while the calendar is still being read; symbols without a report in the window are left out."""
        today = datetime.now(ZoneInfo("America/New_York")).date()
        days = earnings_days(today)
        with earn_lock:
            stale = [d for d in days if d not in earn["days"] or time.monotonic() - earn["days"][d][0] > 6 * 3600]
            if stale and not earn["loading"]:
                earn["loading"] = True
                threading.Thread(target=load_earnings, args=(stale,), daemon=True).start()
            known = {d: earn["days"][d][1] for d in days if d in earn["days"]}
        want = {market.clean_symbol(s) for s in symbols.split(",") if s.strip()} if symbols.strip() else set()
        out = {}
        for d in sorted(known):
            for sym, when in known[d].items():
                if sym in want and sym not in out:
                    out[sym] = {"date": d.isoformat(), "when": when}
        return {"earnings": out, "loading": earn["loading"], "days": len(known), "through": days[-1].isoformat()}

    @app.get("/api/version")
    def version():
        """The open page asks this every minute and reloads itself when it changes."""
        return {"version": f"{server_version}-{fingerprint(web_files())}"}

    live: dict[str, tuple[float, dict]] = {}
    live_lock = threading.Lock()

    @app.get("/api/quotes")
    def quotes_route(symbols: str = ""):
        """Live quotes: ?symbols=SPY,QQQ,NVDA (up to 40). The open page asks every few seconds, so tickers are
        fetched together in one batch request and reused for `quote_ttl` seconds, however many pages ask. A ticker
        the batch doesn't know is tried on its own once; one bad ticker doesn't fail the rest."""
        want, out = [], {}
        for raw in [x for x in symbols.split(",") if x.strip()][:40]:
            try:
                want.append(market.clean_symbol(raw))
            except market.MarketError as exc:
                out[raw.strip().upper()[:12]] = {"error": str(exc)}
        want = list(dict.fromkeys(want))
        now = time.monotonic()
        with live_lock:
            missing = [s for s in want if s not in live or now - live[s][0] >= quote_ttl]
            if missing:
                try:
                    got = fetch_quotes(missing)
                except market.MarketError:
                    got = {}
                for s in missing:
                    if s in got:
                        live[s] = (now, got[s])
                    else:                               # not in the batch (or the batch failed): the single route
                        try:
                            q, _ = quotes.get(s, lambda s=s: fetch_quote(s))
                            live[s] = (now, q)
                        except market.MarketError as exc:
                            if s not in live:
                                out[s] = {"error": str(exc)}
            for s in want:
                if s in live and s not in out:
                    at, q = live[s]
                    out[s] = {**q, "stale": now - at > max(quote_ttl * 5, 30)}
        return {"quotes": out}

    @app.get("/api/{rest:path}")
    def no_route(rest: str):
        return JSONResponse({"error": "No such route."}, 404)

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    return app
