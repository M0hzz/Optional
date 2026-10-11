"""Scan engine: run the scans and position checks defined in scans.yaml once.

A pass:
1. For each scan, fetch bars for its watchlist (one batched request per timeframe)
   and test the `stock` conditions on every symbol.
2. Symbols that pass and whose scan has an `options` section get their full chain;
   contracts are filtered by the structured filters (type, dte, delta, ...) and
   the `options.conditions`, best `top` per symbol kept.
3. Each open position is marked from its chain and its `exit_when` / `watch_when`
   conditions decide EXIT, WATCH or HOLD.

`run_pass` returns plain DataFrames; the monitor script handles timing, files and alerts.
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .expr import Evaluator, ExprError, last, names_used, parse, truthy
from .indicators import bar_functions

# Names computed from a symbol's option chain, usable in stock and option conditions.
CHAIN_NAMES = {"atm_iv", "put_call_volume", "put_call_oi", "skew", "option_volume"}
HIT_COLUMNS = ["scan", "symbol", "price", "contract", "expiration", "dte", "strike", "type", "bid", "ask", "mid",
               "iv", "delta", "open_interest", "volume", "spread_pct", "otm_pct", "annualized_yield", "prob_otm",
               "details"]
POSITION_COLUMNS = ["position", "symbol", "status", "reasons", "price", "pnl", "pnl_pct", "value", "dte",
                    "short_strike_otm_pct", "position_delta", "theta_day", "days_to_earnings", "details"]
NO_EARNINGS = 9999  # days_to_earnings for ETFs/indices and unknown dates, so "> 45"-style filters pass
DEFAULT_EXIT = ["pnl_pct >= 50", "pnl_pct <= -200", "dte <= 21"]
DEFAULT_WATCH = ["short_strike_otm_pct < 3", "earnings_before_expiry"]


# ------------------------------------------------------------------ config
def load_scan_config(path: str | Path) -> dict:
    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}
    base = Path(path).resolve().parent
    lists = {}
    for name, val in (cfg.get("watchlists") or {}).items():
        if isinstance(val, str) and val.startswith("file:"):  # one symbol per line, # comments allowed
            text = (base / val[5:].strip()).read_text()
            val = [ln.split("#")[0].strip() for ln in text.splitlines()]
        lists[name] = [str(s).strip().upper() for s in val if str(s).strip()]
    cfg["watchlists"] = lists
    cfg.setdefault("scans", [])
    cfg.setdefault("positions", [])
    problems = validate(cfg)
    if problems:
        raise ExprError("scans.yaml problems:\n  - " + "\n  - ".join(problems))
    return cfg


def validate(cfg: dict) -> list[str]:
    """Catch typos at startup instead of at 9:30."""
    out = []
    for i, scan in enumerate(cfg["scans"]):
        label = scan.get("name") or f"scan #{i + 1}"
        try:
            scan_symbols(scan, cfg["watchlists"])
        except KeyError as exc:
            out.append(f"{label}: unknown watchlist {exc}")
        exprs = list(scan.get("stock") or []) + list((scan.get("options") or {}).get("conditions") or [])
        for e in exprs:
            try:
                parse(str(e))
            except ExprError as exc:
                out.append(f"{label}: {exc}")
        opt = scan.get("options")
        if opt and str(opt.get("type", "both")) not in ("put", "call", "both"):
            out.append(f"{label}: options.type must be put, call or both")
    defaults = cfg.get("position_defaults") or {}
    for p in cfg["positions"]:
        label = p.get("name") or p.get("symbol", "?")
        if not p.get("symbol") or not p.get("legs"):
            out.append(f"position {label}: needs symbol and legs")
        for leg in p.get("legs") or []:
            if leg.get("type") not in ("call", "put", "stock"):
                out.append(f"position {label}: leg type must be call, put or stock")
            elif leg["type"] != "stock" and not all(k in leg for k in ("strike", "expiration")):
                out.append(f"position {label}: option legs need strike and expiration")
            if "qty" not in leg or "price" not in leg:
                out.append(f"position {label}: legs need qty (negative = short) and price (entry)")
        for e in list(p.get("exit_when", defaults.get("exit_when", DEFAULT_EXIT))) + \
                list(p.get("watch_when", defaults.get("watch_when", DEFAULT_WATCH))):
            try:
                parse(str(e))
            except ExprError as exc:
                out.append(f"position {label}: {exc}")
    return out


def scan_symbols(scan: dict, watchlists: dict) -> list[str]:
    syms = [str(s).upper() for s in scan.get("symbols") or []]
    wl = scan.get("watchlist")
    for name in ([wl] if isinstance(wl, str) else wl or []):
        syms += watchlists[name]
    return list(dict.fromkeys(syms))


# ------------------------------------------------------------- evaluation
def chain_stats(chain: pd.DataFrame) -> dict:
    """Symbol-level numbers from a chain: ~30-day ATM IV, put/call ratios, 25-delta skew."""
    if chain is None or chain.empty:
        return {k: np.nan for k in CHAIN_NAMES}
    near = chain[chain["dte"] >= 7]
    near = near if not near.empty else chain
    exp = near.iloc[(near["dte"] - 30).abs().argsort()]["expiration"].iloc[0]
    e = chain[(chain["expiration"] == exp) & chain["iv"].notna() & (chain["iv"] > 0)]
    atm = e.iloc[(e["moneyness"] - 1).abs().argsort()[:2]]["iv"].mean() if not e.empty else np.nan
    puts, calls = chain[chain["type"] == "put"], chain[chain["type"] == "call"]

    def at_delta(df, d):
        df = df[df["abs_delta"].notna()]
        return df.iloc[(df["abs_delta"] - d).abs().argsort()]["iv"].iloc[0] if not df.empty else np.nan

    skew = at_delta(e[e["type"] == "put"], 0.25) - at_delta(e[e["type"] == "call"], 0.25)
    cv, co = calls["volume"].sum(), calls["open_interest"].sum()
    return {"atm_iv": atm, "skew": skew, "option_volume": float(chain["volume"].sum()),
            "put_call_volume": puts["volume"].sum() / cv if cv else np.nan,
            "put_call_oi": puts["open_interest"].sum() / co if co else np.nan}


def call_values(exprs: list[str], ev: Evaluator) -> str:
    """'rsi(14)=35.2, atm_iv=0.31': the latest value of each study and chain number a condition uses."""
    seen, parts = set(), []
    for e in exprs:
        for node in ast.walk(parse(str(e))):
            if isinstance(node, ast.Name) and node.id in CHAIN_NAMES | {"days_to_earnings"} and node.id not in seen:
                seen.add(node.id)
                v = ev.names.get(node.id)
                if node.id == "days_to_earnings" and v == NO_EARNINGS:
                    parts.append("days_to_earnings=none")
                elif isinstance(v, (int, float, np.floating)):
                    parts.append(f"{node.id}={v:,.4g}")
            elif isinstance(node, ast.Call):
                txt = ast.unparse(node)
                if txt in seen or node.func.id in ("crosses_above", "crosses_below", "ago"):
                    continue
                seen.add(txt)
                try:
                    v = last(ev(txt))
                    parts.append(f"{txt}={v:,.4g}" if isinstance(v, (int, float, np.floating)) else f"{txt}={v}")
                except Exception:
                    pass
    return ", ".join(parts)


def _extras(symbol, earnings: dict, today: date) -> dict:
    d = earnings.get(symbol)
    return {"days_to_earnings": (d - today).days if d else NO_EARNINGS}


def _stock_eval(bars: pd.DataFrame, timeframe: str, extras: dict) -> Evaluator:
    names, funcs = bar_functions(bars, timeframe)
    return Evaluator({**names, **extras}, funcs)


def filter_contracts(chain: pd.DataFrame, opt: dict, stock_ev: Evaluator, scalars: dict) -> pd.DataFrame:
    df = chain
    typ = str(opt.get("type", "both"))
    if typ != "both":
        df = df[df["type"] == typ]
    lo, hi = opt.get("dte", [0, 10_000])
    df = df[df["dte"].between(lo, hi)]
    if "delta" in opt:
        dlo, dhi = sorted(abs(float(x)) for x in opt["delta"])
        df = df[df["abs_delta"].between(dlo, dhi)]
    if "otm_pct" in opt:
        olo, ohi = opt["otm_pct"]
        df = df[df["otm_pct"].between(olo, ohi)]
    df = df[df["open_interest"] >= opt.get("min_open_interest", 0)]
    df = df[df["volume"] >= opt.get("min_volume", 0)]
    if "max_spread_pct" in opt:
        df = df[df["spread_pct"] <= opt["max_spread_pct"]]
    if "min_bid" in opt:
        df = df[df["bid"] >= opt["min_bid"]]
    if df.empty:
        return df
    names = {c: df[c].to_numpy() for c in df.columns}
    names["oi"] = names["open_interest"]
    ev = Evaluator({**names, **scalars}, {}, series_eval=stock_ev)
    mask = np.ones(len(df), dtype=bool)
    for cond in opt.get("conditions") or []:
        r = ev(str(cond))
        mask &= np.broadcast_to(np.nan_to_num(np.asarray(r, dtype=float), nan=0.0).astype(bool), mask.shape)
    df = df[mask]
    sort = str(opt.get("sort", "annualized_yield"))
    desc = not sort.endswith(" asc")
    sort = sort.replace(" asc", "").replace(" desc", "").strip()
    if sort in df.columns:
        df = df.sort_values(sort, ascending=not desc)
    return df.head(int(opt.get("top", 3)))


@dataclass
class PassResult:
    hits: pd.DataFrame
    positions: pd.DataFrame
    errors: list[str] = field(default_factory=list)
    started: datetime = field(default_factory=datetime.now)
    seconds: float = 0.0
    symbols: int = 0


def run_scans(cfg: dict, feed, today: date | None = None) -> tuple[pd.DataFrame, list[str], int]:
    today = today or date.today()
    rows, errors, all_syms = [], [], set()
    for scan in cfg["scans"]:
        name = scan.get("name", "scan")
        tf = str(scan.get("timeframe", "1d"))
        syms = scan_symbols(scan, cfg["watchlists"])
        all_syms.update(syms)
        stock = [str(c) for c in scan.get("stock") or []]
        opt = scan.get("options")
        used = set().union(*(names_used(c) for c in stock)) if stock else set()
        opt_used = set().union(*(names_used(str(c)) for c in (opt or {}).get("conditions") or [])) if opt else set()
        needs_chain_early = bool(used & CHAIN_NAMES)
        earnings = feed.earnings(syms) if "days_to_earnings" in used | opt_used else {}
        bars = feed.bars(syms, tf)
        missing = [s for s in syms if s not in bars or bars[s].empty]
        if missing:
            errors.append(f"{name}: no {tf} bars for {', '.join(missing[:10])}" + (" ..." if len(missing) > 10 else ""))
        candidates = [s for s in syms if s in bars and not bars[s].empty]
        chains, chain_err = (feed.chains(candidates) if needs_chain_early else ({}, {}))
        passed = []
        for s in candidates:
            scalars = chain_stats(chains.get(s)) if needs_chain_early else {}
            ev = _stock_eval(bars[s], tf, {**_extras(s, earnings, today), **scalars})
            try:
                if all(truthy(ev(c)) for c in stock):
                    passed.append((s, ev))
            except ExprError as exc:
                errors.append(f"{name}: {exc}")
                break
            except Exception as exc:  # bad data for one symbol shouldn't stop the scan
                errors.append(f"{name} {s}: {type(exc).__name__}: {exc}")
        if not opt:
            for s, ev in passed:
                rows.append({"scan": name, "symbol": s, "price": float(last(ev.names["close"])),
                             "details": call_values(stock, ev)})
            continue
        need = [s for s, _ in passed if s not in chains]
        more, err2 = feed.chains(need) if need else ({}, {})
        chains.update(more)
        for s, msg in {**chain_err, **err2}.items():
            errors.append(f"{name} {s}: chain unavailable ({msg})")
        for s, ev in passed:
            ch = chains.get(s)
            if ch is None or ch.empty:
                continue
            scalars = chain_stats(ch)
            try:
                picks = filter_contracts(ch, opt, ev, scalars)
            except ExprError as exc:
                errors.append(f"{name}: {exc}")
                break
            details = call_values(stock + [str(c) for c in opt.get("conditions") or []], ev)
            for r in picks.to_dict("records"):
                rows.append({"scan": name, "symbol": s, "price": r["underlying_price"], **{
                    k: r[k] for k in HIT_COLUMNS if k in r and k not in ("symbol",)}, "details": details})
    hits = pd.DataFrame(rows, columns=HIT_COLUMNS)
    return hits, errors, len(all_syms)


# --------------------------------------------------------------- positions
def check_position(pos: dict, chain: pd.DataFrame | None, bars: pd.DataFrame, timeframe: str,
                   defaults: dict, earnings_date: date | None, today: date) -> dict:
    name = pos.get("name") or pos["symbol"]
    out = {"position": name, "symbol": pos["symbol"], "status": "ERROR", "reasons": ""}
    price = float(bars["close"].iloc[-1])
    value = entry = delta = theta = 0.0
    dtes, short_otm, problems = [], [], []
    for leg in pos["legs"]:
        qty, paid = float(leg["qty"]), float(leg["price"])
        if leg["type"] == "stock":
            value += qty * price
            entry += qty * paid
            delta += qty
            continue
        exp = pd.to_datetime(leg["expiration"]).date()
        dtes.append((exp - today).days)
        row = None
        if chain is not None and not chain.empty:
            m = chain[(chain["expiration"] == exp) & (chain["type"] == leg["type"])
                      & np.isclose(chain["strike"], float(leg["strike"]))]
            if "root" in leg:  # e.g. SPX vs SPXW on the same expiration and strike
                m = m[m["contract"].str[:-15] == str(leg["root"]).upper()]
            row = m.iloc[0] if not m.empty else None
        if row is None:
            hint = ""
            if chain is not None and not chain.empty:
                exps = sorted(chain["expiration"].unique(), key=lambda e: abs((e - exp).days))[:2]
                same = chain[(chain["expiration"] == exps[0]) & (chain["type"] == leg["type"])]["strike"]
                near = sorted(same.unique(), key=lambda k: abs(k - float(leg["strike"])))[:3]
                hint = (f" (nearest expirations {', '.join(map(str, sorted(exps)))}; "
                        f"strikes near {leg['strike']} on {exps[0]}: {', '.join(f'{k:g}' for k in sorted(near))})")
            problems.append(f"{exp} {leg['strike']} {leg['type']} not in chain{hint}")
            continue
        mark = row["mid"] if pd.notna(row["mid"]) else row["last"]
        value += qty * mark * 100
        entry += qty * paid * 100
        delta += qty * (row["delta"] if pd.notna(row["delta"]) else 0) * 100
        theta += qty * (row["theta"] if pd.notna(row["theta"]) else 0) * 100
        if qty < 0:
            short_otm.append(float(row["otm_pct"]))
    if problems:
        out["reasons"] = "; ".join(problems)
        return out
    pnl = value - entry
    dte = min(dtes) if dtes else NO_EARNINGS
    dte_earn = (earnings_date - today).days if earnings_date else NO_EARNINGS
    vals = {
        "pnl": pnl, "pnl_pct": 100 * pnl / abs(entry) if entry else 0.0, "value": value, "entry": entry,
        "dte": dte, "short_strike_otm_pct": min(short_otm) if short_otm else np.nan,
        "position_delta": delta, "theta_day": theta, "days_to_earnings": dte_earn,
        "earnings_before_expiry": bool(earnings_date) and 0 <= dte_earn <= dte,
    }
    names, funcs = bar_functions(bars, timeframe)
    stock_ev = Evaluator({**names, "days_to_earnings": dte_earn}, funcs)
    ev = Evaluator({**vals, **chain_stats(chain)}, {}, series_eval=stock_ev)
    exit_when = [str(c) for c in pos.get("exit_when", defaults.get("exit_when", DEFAULT_EXIT))]
    watch_when = [str(c) for c in pos.get("watch_when", defaults.get("watch_when", DEFAULT_WATCH))]
    exits = [c for c in exit_when if truthy(ev(c))]
    watches = [c for c in watch_when if truthy(ev(c))]
    status = "EXIT" if exits else "WATCH" if watches else "HOLD"
    out.update({"status": status, "reasons": "; ".join(exits or watches), "price": price,
                **{k: vals[k] for k in ("pnl", "pnl_pct", "value", "dte", "short_strike_otm_pct",
                                        "position_delta", "theta_day", "days_to_earnings")},
                "details": call_values(exit_when + watch_when, stock_ev)})
    return out


def run_positions(cfg: dict, feed, today: date | None = None) -> tuple[pd.DataFrame, list[str]]:
    today = today or date.today()
    positions = cfg.get("positions") or []
    if not positions:
        return pd.DataFrame(columns=POSITION_COLUMNS), []
    defaults = cfg.get("position_defaults") or {}
    syms = list(dict.fromkeys(p["symbol"].upper() for p in positions))
    needs_chain = [s for s in syms if any(p["symbol"].upper() == s and any(l["type"] != "stock" for l in p["legs"])
                                          for p in positions)]
    chains, chain_err = feed.chains(needs_chain) if needs_chain else ({}, {})
    earnings = feed.earnings(syms)
    errors = [f"{s}: chain unavailable ({m})" for s, m in chain_err.items()]
    rows = []
    for p in positions:
        sym, tf = p["symbol"].upper(), str(p.get("timeframe", "1d"))
        bars = feed.bars([sym], tf).get(sym)
        if bars is None or bars.empty:
            rows.append({"position": p.get("name") or sym, "symbol": sym, "status": "ERROR", "reasons": "no price data"})
            continue
        try:
            rows.append(check_position({**p, "symbol": sym}, chains.get(sym), bars, tf, defaults, earnings.get(sym), today))
        except ExprError as exc:
            rows.append({"position": p.get("name") or sym, "symbol": sym, "status": "ERROR", "reasons": str(exc)})
    return pd.DataFrame(rows, columns=POSITION_COLUMNS), errors


def run_pass(cfg: dict, feed, today: date | None = None) -> PassResult:
    t0 = time.time()
    res = PassResult(pd.DataFrame(columns=HIT_COLUMNS), pd.DataFrame(columns=POSITION_COLUMNS))
    try:
        res.hits, errs, res.symbols = run_scans(cfg, feed, today)
        res.errors += errs
    except Exception as exc:
        res.errors.append(f"scans failed: {type(exc).__name__}: {exc}")
    try:
        res.positions, errs = run_positions(cfg, feed, today)
        res.errors += errs
    except Exception as exc:
        res.errors.append(f"position checks failed: {type(exc).__name__}: {exc}")
    res.seconds = time.time() - t0
    return res
