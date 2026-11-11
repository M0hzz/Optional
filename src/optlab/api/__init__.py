"""The whole server: three read-only data routes and the page. No database, no accounts, no orders.

    GET /api/chain/{symbol}          price, expirations (date, days left, how many contracts)
    GET /api/chain/{symbol}/{date}   the calls and puts of one expiration
    GET /api/bars/{symbol}           daily bars and the levels the simulator uses

It answers only to localhost and your Tailscale name (OPTLAB_ALLOWED_HOSTS to change), so a web page you
happen to visit can't reach it through your browser.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import market

WEB = Path(__file__).resolve().parents[1] / "web"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")


def host_ok(host: str, allowed: list[str]) -> bool:
    h = host.strip().lower()
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else (h.split(":")[0] if h.count(":") == 1 else h)
    return any(h == a or (a.startswith("*.") and h.endswith(a[1:]) and len(h) > len(a) - 1) for a in allowed)


def create_app(fetch_chain=market.fetch_cboe, fetch_bars=market.fetch_bars, chain_ttl: float = 15,
               bars_ttl: float = 600, allowed_hosts: list[str] | None = None) -> FastAPI:
    allowed = allowed_hosts or [h.strip().lower() for h in os.environ.get(
        "OPTLAB_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,*.ts.net").split(",") if h.strip()]
    app = FastAPI(title="optlab sim", docs_url=None, redoc_url=None, openapi_url=None)
    chains, bars = market.Cache(chain_ttl), market.Cache(bars_ttl)

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

    @app.get("/api/chain/{symbol}")
    def chain_summary(symbol: str):
        c, stale = chain(symbol)
        return {**{k: v for k, v in c.items() if k != "expirations"}, "stale": stale,
                "expirations": [{"date": e["date"], "dte": e["dte"], "count": len(e["calls"]) + len(e["puts"])}
                                for e in c["expirations"]]}

    @app.get("/api/chain/{symbol}/{expiration}")
    def chain_expiration(symbol: str, expiration: str):
        c, stale = chain(symbol)
        for e in c["expirations"]:
            if e["date"] == expiration:
                return {"symbol": c["symbol"], "price": c["price"], "change_pct": c["change_pct"], "as_of": c["as_of"],
                        "stale": stale, **e}
        raise market.MarketError(f"{c['symbol']} has no options expiring on {expiration}.")

    @app.get("/api/bars/{symbol}")
    def bars_route(symbol: str):
        sym = market.clean_symbol(symbol)
        b, _ = bars.get(sym, lambda: fetch_bars(sym))
        return {"symbol": sym, "bars": b[-260:], "levels": market.levels(b)}

    @app.get("/api/{rest:path}")
    def no_route(rest: str):
        return JSONResponse({"error": "No such route."}, 404)

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    return app
