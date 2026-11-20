"""Live option chains from Tastytrade's API, for account holders. Read-only: the login asks for the `read` scope only,
and nothing here can place, change or cancel an order.

Set up once (see the README, "Live option quotes from Tastytrade"):
  my.tastytrade.com -> Manage -> My Profile -> API -> OAuth Applications: create an application (scope: read),
  then Manage -> Create Grant. Put the client secret and the grant's refresh token in
  ~/.config/optlab/tastytrade.json   {"client_secret": "...", "refresh_token": "..."}   (chmod 600)
or in the environment variables TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN.
Check it with:   python -m optlab.tasty SPY

The chain's strikes and expirations come from GET /option-chains/{symbol} (kept for an hour); prices from
GET /market-data/by-type (real time, 100 symbols a request), only for the strikes near the price on the expiration
being looked at. The quotes carry no implied volatility or delta, so both are worked out here from each contract's
mid price (Black-Scholes, the same model the page uses). Open interest isn't in the quotes either.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

from .market import MarketError, clean_symbol

API = "https://api.tastyworks.com"
USER_AGENT = "optlab/1.0"
INDEXES = {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"}
RATE = 0.04                       # the page's rate (sim.js RATE)
NEAR = 30                         # strikes each side of the price that get quotes
BATCH = 100                       # symbols per market-data request
CRED_FILE = Path(os.environ.get("OPTLAB_TASTYTRADE_FILE", "~/.config/optlab/tastytrade.json")).expanduser()


def load_credentials(path: Path = CRED_FILE, env=os.environ) -> dict | None:
    """{"client_secret", "refresh_token", "client_id"?} from the environment, else the file; None when neither is set."""
    secret, token = env.get("TASTYTRADE_CLIENT_SECRET"), env.get("TASTYTRADE_REFRESH_TOKEN")
    if secret and token:
        return {"client_secret": secret, "refresh_token": token, "client_id": env.get("TASTYTRADE_CLIENT_ID")}
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise MarketError(f"Can't read {path}: it should be JSON with client_secret and refresh_token.") from None
    if not isinstance(data, dict):
        raise MarketError(f"{path} should be a JSON object with client_secret and refresh_token.")
    # forgiving about spelling (client-secret, clientSecret) and stray spaces from copying
    norm = {k.lower().replace("-", "_").replace(" ", "_"): v for k, v in data.items()}
    norm.update({k.replace("clientsecret", "client_secret").replace("refreshtoken", "refresh_token")
                  .replace("clientid", "client_id"): v for k, v in list(norm.items())})
    got = {k: str(norm.get(k) or "").strip() for k in ("client_secret", "refresh_token", "client_id")}
    missing = [k for k in ("client_secret", "refresh_token") if not got[k] or got[k].startswith("PASTE-")]
    if missing:
        keys = ", ".join(f"{k} ({len(str(v).strip())} characters)" for k, v in data.items()) or "nothing"
        raise MarketError(f"{path} is missing {' and '.join(missing)}. It has: {keys}. "
                          'It should look like {"client_secret": "...", "refresh_token": "..."}.')
    return {"client_secret": got["client_secret"], "refresh_token": got["refresh_token"], "client_id": got["client_id"] or None}


def field(item: dict, name: str):
    """A response field whether the API spells it dasherized (prev-close) or camelCase (prevClose)."""
    if name in item:
        return item[name]
    parts = name.split("-")
    return item.get(parts[0] + "".join(p.title() for p in parts[1:]))


def num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, iv, is_call, r=RATE) -> float:
    if T <= 0 or iv <= 0 or S <= 0:
        return max(S - K if is_call else K - S, 0.0)
    sq = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + iv * iv / 2) * T) / sq
    d2 = d1 - sq
    if is_call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price, S, K, T, is_call) -> float | None:
    if not (price and price > 0 and T > 0):
        return None
    lo, hi = 0.005, 6.0
    if price <= bs_price(S, K, T, lo, is_call) or price >= bs_price(S, K, T, hi, is_call):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, mid, is_call) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def bs_delta(S, K, T, iv, is_call, r=RATE) -> float | None:
    if not (iv and T > 0 and S > 0):
        return None
    d1 = (math.log(S / K) + (r + iv * iv / 2) * T) / (iv * math.sqrt(T))
    return _ncdf(d1) if is_call else _ncdf(d1) - 1


class Tasty:
    """One logged-in client: an access token (15 minutes, renewed a minute early) and the requests that use it."""

    def __init__(self, creds: dict, session=None, clock=time.time, sleep=time.sleep):
        import requests

        self.creds, self.http = creds, session or requests.Session()
        self.clock, self.sleep = clock, sleep
        self._token, self._until = None, 0.0
        self._lock = threading.Lock()
        self._instruments: dict[str, tuple[float, list]] = {}

    def _login(self):
        body = {"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"],
                "client_secret": self.creds["client_secret"]}
        if self.creds.get("client_id"):
            body["client_id"] = self.creds["client_id"]
        try:
            r = self.http.post(f"{API}/oauth/token", json=body, timeout=20, headers={"User-Agent": USER_AGENT})
        except Exception:
            raise MarketError("Can't reach Tastytrade to log in. Check the connection.") from None
        if r.status_code != 200:
            raise MarketError(f"Tastytrade refused the login (HTTP {r.status_code}). Check the client secret and refresh "
                              "token (README: Live option quotes from Tastytrade).")
        data = r.json()
        self._token = data.get("access_token")
        if not self._token:
            raise MarketError("Tastytrade's login answer had no access token.")
        self._until = self.clock() + float(data.get("expires_in") or 900) - 60

    def get(self, path: str, params=None) -> dict:
        for attempt in range(4):
            with self._lock:
                if not self._token or self.clock() >= self._until:
                    self._login()
                token = self._token
            try:
                r = self.http.get(f"{API}{path}", params=params, timeout=20,
                                  headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
            except Exception:
                raise MarketError("Can't reach Tastytrade right now.") from None
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and attempt == 0:          # the token ran out early: log in again, once
                with self._lock:
                    self._token = None
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt < 3:
                self.sleep((2 ** attempt) * (0.5 + random.random() / 2))
                continue
            raise MarketError(f"Tastytrade answered HTTP {r.status_code} for {path.split('?')[0]}.")
        raise MarketError("Tastytrade is busy right now. Try again in a moment.")

    # ---------------------------------------------------------------- data
    def instruments(self, sym: str) -> list[dict]:
        """Every active option on `sym`: {occ, symbol, strike, call, date, root}, kept for an hour."""
        hit = self._instruments.get(sym)
        if hit and self.clock() - hit[0] < 3600:
            return hit[1]
        items, page = [], 0
        while True:
            data = self.get(f"/option-chains/{sym}", params={"page-offset": page} if page else None)
            d = data.get("data") or {}
            items += d.get("items") or []
            pg = data.get("pagination") or d.get("pagination") or {}
            total = int(num(field(pg, "total-pages")) or 1)
            page += 1
            if page >= total or page > 20:
                break
        out = {}
        for it in items:
            if field(it, "active") is False:
                continue
            strike, exp, kind = num(field(it, "strike-price")), field(it, "expiration-date"), field(it, "option-type")
            symbol = field(it, "symbol")
            if strike is None or not exp or kind not in ("C", "P") or not symbol:
                continue
            root = field(it, "root-symbol") or ""
            key = (exp, kind, strike)
            # SPX lists AM-settled monthlies and PM-settled SPXW on the same day: keep the PM ones (like CBOE's list)
            if key in out and not root.endswith("W"):
                continue
            out[key] = {"symbol": symbol, "occ": symbol.replace(" ", ""), "strike": strike, "call": kind == "C",
                        "date": exp, "root": root}
        if not out:
            raise MarketError(f"Tastytrade has no options for {sym}. Check the ticker.")
        rows = sorted(out.values(), key=lambda x: (x["date"], x["strike"]))
        self._instruments[sym] = (self.clock(), rows)
        return rows

    def quotes(self, equity=(), index=(), options=()) -> dict[str, dict]:
        """Real-time quotes by symbol, in batches of 100."""
        wanted = [("equity", s) for s in equity] + [("index", s) for s in index] + [("equity-option", s) for s in options]
        out = {}
        for i in range(0, len(wanted), BATCH):
            params = [(f"{kind}[]", s) for kind, s in wanted[i:i + BATCH]]
            data = self.get("/market-data/by-type", params=params)
            for q in (data.get("data") or {}).get("items") or []:
                if field(q, "symbol"):
                    out[field(q, "symbol")] = q
        return out

    def underlying(self, sym: str) -> dict:
        q = self.quotes(**({"index": [sym]} if sym in INDEXES else {"equity": [sym]})).get(sym)
        if not q:
            raise MarketError(f"Tastytrade has no price for {sym}.")
        price = num(field(q, "last")) or num(field(q, "mark")) or num(field(q, "mid"))
        prev = num(field(q, "prev-close"))
        if not price:
            raise MarketError(f"Tastytrade has no price for {sym}.")
        return {"price": price, "prev_close": prev, "change": price - prev if prev else None,
                "change_pct": round(100 * (price / prev - 1), 2) if prev else None}


def summary(t: Tasty, symbol: str, today: date | None = None) -> dict:
    """Like the CBOE summary: the price and every expiration (date, days left, how many contracts)."""
    sym = clean_symbol(symbol)
    today = today or date.today()
    rows = t.instruments(sym)
    exps: dict[str, int] = {}
    for r in rows:
        exps[r["date"]] = exps.get(r["date"], 0) + 1
    u = t.underlying(sym)
    return {"symbol": sym, **u, "iv30": None, "as_of": datetime.now().isoformat(timespec="seconds"), "source": "tastytrade",
            "expirations": [{"date": d, "dte": (date.fromisoformat(d) - today).days, "count": n}
                            for d, n in sorted(exps.items()) if d >= today.isoformat()]}


def expiration(t: Tasty, symbol: str, exp: str, today: date | None = None) -> dict:
    """The calls and puts of one expiration, quoted live: the NEAR strikes each side of the price."""
    sym = clean_symbol(symbol)
    today = today or date.today()
    rows = [r for r in t.instruments(sym) if r["date"] == exp]
    if not rows:
        raise MarketError(f"{sym} has no options expiring on {exp}.")
    u = t.underlying(sym)
    S = u["price"]
    strikes = sorted({r["strike"] for r in rows})
    at = min(range(len(strikes)), key=lambda i: abs(strikes[i] - S))
    keep = set(strikes[max(0, at - NEAR):at + NEAR + 1])
    rows = [r for r in rows if r["strike"] in keep]
    q = t.quotes(options=[r["symbol"] for r in rows])
    dte = (date.fromisoformat(exp) - today).days
    T = max(dte, 0.5) / 365
    calls, puts = [], []
    for r in rows:
        x = q.get(r["symbol"]) or {}
        bid, ask, last = num(field(x, "bid")) or 0.0, num(field(x, "ask")) or 0.0, num(field(x, "last"))
        mid = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else (last if last and last > 0 else None)
        iv = implied_vol(mid, S, r["strike"], T, r["call"]) if mid else None
        (calls if r["call"] else puts).append({
            "occ": r["occ"], "strike": r["strike"], "bid": bid, "ask": ask, "mid": mid, "iv": iv,
            "delta": round(bs_delta(S, r["strike"], T, iv, r["call"]), 4) if iv else None, "theta": None,
            "volume": int(num(field(x, "volume")) or 0), "oi": None})
    return {"symbol": sym, "price": S, "change_pct": u["change_pct"], "as_of": datetime.now().isoformat(timespec="seconds"),
            "source": "tastytrade", "date": exp, "dte": dte, "calls": calls, "puts": puts}


if __name__ == "__main__":                                     # python -m optlab.tasty SPY: check the setup
    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    try:
        creds = load_credentials()
        if not creds:
            sys.exit(f"No Tastytrade login set up: create {CRED_FILE} (see the README).")
        t = Tasty(creds)
        s = summary(t, sym)
        print(f"Logged in. {s['symbol']} {s['price']} ({s['change_pct']}%), {len(s['expirations'])} expirations.")
        e = expiration(t, sym, next(x["date"] for x in s["expirations"] if x["dte"] >= 1))
        near = min(e["calls"], key=lambda r: abs(r["strike"] - e["price"]))
        print(f"{e['date']} {near['strike']} call: bid {near['bid']} ask {near['ask']} iv {near['iv'] and round(near['iv'], 3)} "
              f"delta {near['delta']}  ({len(e['calls'])} calls, {len(e['puts'])} puts quoted)")
    except MarketError as exc:
        sys.exit(str(exc))
