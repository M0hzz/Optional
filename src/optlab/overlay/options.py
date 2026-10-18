"""Option-chain overlay: per-contract liquidity and quality flags, small-account fit, implied-volatility
context, a defined-risk structure scanner and quote-anomaly checks.

Works on the chain the Option chain tab loads (live Cboe / Yahoo, or synthetic):
columns contract, expiration, dte, strike, type, bid, ask, mid, last, volume, open_interest,
iv, delta, gamma, theta, vega, underlying_price.

Liquidity is USABLE only when every configured check passes: quote fresh, bid > 0, market
not crossed, (ask − bid) / mid ≤ maximum_bid_ask_pct, open interest ≥ minimum_open_interest
and volume ≥ minimum_option_volume. A cheap contract is never marked positively for being cheap.
Anomalies are labelled REQUIRES DATA VALIDATION, never opportunities.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .account import Profile, credit_spread, debit_spread, long_option

FLAG_TEXT = {"zero_bid": "zero bid", "crossed": "crossed market", "wide_spread": "wide spread", "stale": "stale quote",
             "low_volume": "low volume", "low_oi": "low open interest", "no_greeks": "missing Greeks",
             "bad_iv": "invalid IV", "high_theta": "high theta vs premium", "far_otm": "far OTM, low delta",
             "expiring": "expires within days", "earnings": "earnings before expiration"}


def contract_flags(chain: pd.DataFrame, th: dict, p: Profile, quotes_fresh: bool = True,
                   earnings: date | None = None) -> pd.DataFrame:
    """One row per contract with the overlay columns added (vectorised)."""
    df = chain.copy()
    m = p.contract_multiplier
    bid, ask = df["bid"].astype(float), df["ask"].astype(float)
    mid = (bid + ask) / 2
    df["spread_usd"] = ask - bid
    df["spread_pct_mid"] = np.where(mid > 0, 100 * (ask - bid) / mid, np.nan)
    df["cost_per_contract"] = np.where(ask > 0, ask * m, np.nan)
    flags = {
        "zero_bid": bid <= 0,
        "crossed": (bid > ask) & (ask > 0),
        "wide_spread": ~(df["spread_pct_mid"] <= float(th["maximum_bid_ask_pct"])),
        "stale": pd.Series(not quotes_fresh, index=df.index),
        "low_volume": df["volume"].fillna(0) < float(th["minimum_option_volume"]),
        "low_oi": df["open_interest"].fillna(0) < float(th["minimum_open_interest"]),
        "no_greeks": df["delta"].isna() if "delta" in df else pd.Series(True, index=df.index),
        "bad_iv": df["iv"].isna() | (df["iv"] <= 0) | (df["iv"] > 5),
        "high_theta": (df["theta"].abs() / mid.replace(0, np.nan) * 100 > float(th["high_theta_pct"])).fillna(False)
        if "theta" in df else pd.Series(False, index=df.index),
        "far_otm": (df["delta"].abs() < float(th["far_otm_delta"])).fillna(False) if "delta" in df
        else pd.Series(False, index=df.index),
        "expiring": df["dte"] <= int(th["expiration_warning_days"]),
        "earnings": pd.Series([bool(earnings and pd.Timestamp(e).date() >= earnings) for e in df["expiration"]],
                              index=df.index),
    }
    liquidity_keys = ("zero_bid", "crossed", "wide_spread", "stale", "low_volume", "low_oi")
    df["liquidity"] = np.where(np.logical_or.reduce([flags[k].to_numpy() for k in liquidity_keys]), "POOR_LIQUIDITY",
                               "USABLE")
    names = np.array([FLAG_TEXT[k] for k in flags])
    mat = np.column_stack([flags[k].to_numpy(bool) for k in flags])
    df["flags"] = [", ".join(names[row]) for row in mat]
    df["n_flags"] = mat.sum(1)
    for k, v in flags.items():
        df[f"flag_{k}"] = v.to_numpy(bool)
    fits = [long_option(p, a if a > 0 else np.nan, k, t) for a, k, t in zip(ask, df["strike"], df["type"].str.lower())]
    df["account_fit"] = [f.classification for f in fits]
    df["account_status"] = [f.status for f in fits]
    df["account_risk_pct"] = [f.account_risk_pct for f in fits]
    df["bp_remaining"] = [f.buying_power_remaining for f in fits]
    df["exercise_cash"] = np.where(df["type"].str.lower() == "call", df["strike"] * m, np.nan)
    df["anomaly"] = anomalies(df)
    df["data_quality"] = np.where(df["anomaly"] != "", "Requires data validation", np.where(flags["crossed"] | flags["bad_iv"] | flags["no_greeks"], "Check quote",
                                  np.where(flags["stale"], "Stale", "OK")))
    return df


def iv_context(chain: pd.DataFrame, hv20: float | None, iv_history: pd.Series | None = None,
               min_history: int = 60) -> dict:
    """Implied volatility against realised volatility, neighbouring strikes, adjacent expirations and (only with
    enough stored history) its own rank and percentile."""
    ch = chain[(chain["iv"] > 0) & (chain["iv"] < 5)]
    out = {"iv_rank": None, "iv_percentile": None,
           "history_note": f"IV rank/percentile need ≥ {min_history} daily IV observations; "
                           f"{0 if iv_history is None else len(iv_history)} stored, so they are not calculated."}
    if iv_history is not None and len(iv_history.dropna()) >= min_history:
        h = iv_history.dropna()
        cur = float(h.iloc[-1])
        out["iv_rank"] = 100 * (cur - h.min()) / (h.max() - h.min()) if h.max() > h.min() else float("nan")
        out["iv_percentile"] = float(100 * (h < cur).mean())
        out["history_note"] = f"From {len(h)} stored daily observations."
    u = float(chain["underlying_price"].iloc[0])
    term = []
    for exp, g in ch.groupby("expiration"):
        g = g.iloc[(g["strike"] - u).abs().argsort()[:4]]
        term.append({"expiration": exp, "dte": int(g["dte"].iloc[0]), "atm_iv": float(g["iv"].mean())})
    term = pd.DataFrame(term).sort_values("dte") if term else pd.DataFrame(columns=["expiration", "dte", "atm_iv"])
    if len(term) >= 3:
        nb = (term["atm_iv"].shift(1) + term["atm_iv"].shift(-1)) / 2
        term["vs_adjacent_pct"] = 100 * (term["atm_iv"] / nb - 1)
    out["term"] = term
    near = term[term["dte"] >= 7].head(1)
    out["atm_iv"] = float(near["atm_iv"].iloc[0]) if len(near) else float("nan")
    out["hv20"] = hv20
    out["iv_rv"] = out["atm_iv"] / hv20 if hv20 and np.isfinite(out["atm_iv"]) else float("nan")
    return out


def neighbour_iv(df: pd.DataFrame, k: int = 2) -> pd.Series:
    """Contract IV ÷ median IV of the k strikes either side (same expiration and type) − 1, in %."""
    out = pd.Series(np.nan, index=df.index)
    for _, g in df.groupby(["expiration", "type"]):
        g = g.sort_values("strike")
        iv = g["iv"].where((g["iv"] > 0) & (g["iv"] < 5))
        med = iv.rolling(2 * k + 1, center=True, min_periods=3).median()
        out.loc[g.index] = 100 * (iv / med - 1)
    return out


def anomalies(df: pd.DataFrame) -> pd.Series:
    """Per contract, a reason when its quote is inconsistent with its neighbours (needs data validation):
    calls must not get more expensive at higher strikes, puts not cheaper; asks must not sit below intrinsic value."""
    reason = pd.Series("", index=df.index)
    u = float(df["underlying_price"].iloc[0])
    for (exp, typ), g in df.groupby(["expiration", "type"]):
        g = g.sort_values("strike")
        valid = (g["ask"] > 0) & (g["bid"] >= 0) & (g["bid"] <= g["ask"])
        a = g["ask"].where(valid)
        if typ.lower() == "call":
            bad = a > a.shift(1) + 0.01             # higher strike, higher price (beyond a penny)
        else:
            bad = a < a.shift(1) - 0.01             # higher strike, lower price
        intrinsic = (u - g["strike"]).clip(lower=0) if typ.lower() == "call" else (g["strike"] - u).clip(lower=0)
        below = valid & (g["ask"] < intrinsic - 0.05)
        reason.loc[g.index[bad.fillna(False).to_numpy()]] = "price out of order with the neighbouring strike"
        reason.loc[g.index[below.to_numpy()]] = "ask below intrinsic value"
    return reason


def structures(df: pd.DataFrame, p: Profile, th: dict, expirations: list, max_steps: int = 3,
               band_pct: float = 15.0, symbol: str = "") -> pd.DataFrame:
    """Long calls/puts and adjacent-strike verticals (up to `max_steps` strikes wide) for the given expirations,
    priced at natural bid/ask. `df` must come from contract_flags. Mid-price figures are separate columns."""
    u = float(df["underlying_price"].iloc[0])
    rows = []
    sub = df[df["expiration"].isin(expirations) & ((df["strike"] - u).abs() <= u * band_pct / 100)]
    for (exp, typ), g in sub.groupby(["expiration", "type"]):
        g = g.sort_values("strike").reset_index(drop=True)
        typ = typ.lower()
        for i, r in g.iterrows():
            f = long_option(p, r["ask"] if r["ask"] > 0 else np.nan, r["strike"], typ)
            rows.append(_row(symbol, f"Long {typ}", "bullish" if typ == "call" else "bearish", exp, r["dte"],
                             f"{r['strike']:g}", [r], f, r["ask"], r["mid"], 0.0))
            for s in range(1, max_steps + 1):
                if i + s >= len(g):
                    break
                lo, hi = g.iloc[i], g.iloc[i + s]
                width = float(hi["strike"] - lo["strike"])
                if typ == "call":     # bull call debit: long lo / short hi; bear call credit: short lo / long hi
                    d = debit_spread(p, lo["ask"], hi["bid"], width, hi["strike"], "call")
                    rows.append(_row(symbol, "Bull call debit spread", "bullish", exp, lo["dte"],
                                     f"{lo['strike']:g}/{hi['strike']:g}", [lo, hi], d, lo["ask"] - hi["bid"],
                                     lo["mid"] - hi["mid"], width))
                    c = credit_spread(p, lo["bid"], hi["ask"], width, lo["strike"], "call")
                    rows.append(_row(symbol, "Bear call credit spread", "bearish", exp, lo["dte"],
                                     f"{lo['strike']:g}/{hi['strike']:g}", [lo, hi], c, -(lo["bid"] - hi["ask"]),
                                     -(lo["mid"] - hi["mid"]), width))
                else:                 # bear put debit: long hi / short lo; bull put credit: short hi / long lo
                    d = debit_spread(p, hi["ask"], lo["bid"], width, lo["strike"], "put")
                    rows.append(_row(symbol, "Bear put debit spread", "bearish", exp, hi["dte"],
                                     f"{hi['strike']:g}/{lo['strike']:g}", [hi, lo], d, hi["ask"] - lo["bid"],
                                     hi["mid"] - lo["mid"], width))
                    c = credit_spread(p, hi["bid"], lo["ask"], width, hi["strike"], "put")
                    rows.append(_row(symbol, "Bull put credit spread", "bullish", exp, hi["dte"],
                                     f"{hi['strike']:g}/{lo['strike']:g}", [hi, lo], c, -(hi["bid"] - lo["ask"]),
                                     -(hi["mid"] - lo["mid"]), width))
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["expiration", "strategy", "strikes"], kind="stable").reset_index(drop=True)


def _row(symbol, name, direction, exp, dte, strikes, legs, fit, natural, mid, width) -> dict:
    """natural/mid: net per share, + = debit paid, − = credit received."""
    poor = [lg for lg in legs if lg["liquidity"] != "USABLE"]
    validation = ""
    if width and fit.classification != "INSUFFICIENT DATA":
        if natural > width + 1e-9:
            validation = "net debit wider than the spread"
        elif "debit" in name and natural < -1e-9:
            validation = "debit spread quoted as a credit"
        elif "credit" in name and -natural > width + 1e-9:
            validation = "credit larger than the spread width"
    if any(lg.get("anomaly") for lg in legs):
        validation = validation or "a leg's quote is out of line with its neighbours"
    if any(lg["flag_crossed"] for lg in legs):
        validation = validation or "a leg has a crossed market"
    return {"symbol": symbol, "strategy": name, "direction": direction, "expiration": exp, "dte": int(dte),
            "strikes": strikes, "width": width or None, "net_natural": natural, "net_mid": mid,
            "max_profit": fit.max_profit, "max_loss": fit.max_loss, "capital": fit.capital_required,
            "account_risk_pct": fit.account_risk_pct, "bp_remaining": fit.buying_power_remaining,
            "assignment": fit.assignment_obligation, "account_fit": fit.classification, "account_status": fit.status,
            "liquidity": "POOR_LIQUIDITY" if poor else "USABLE",
            "leg_flags": "; ".join(f"{lg['strike']:g}{lg['type'][0].upper()}: {lg['flags']}" for lg in legs if lg["flags"]),
            "min_oi": float(min(lg["open_interest"] for lg in legs)), "min_volume": float(min(lg["volume"] for lg in legs)),
            "max_spread_pct": float(max(lg["spread_pct_mid"] for lg in legs)),
            "delta": _net(legs, "delta", name), "theta": _net(legs, "theta", name),
            "earnings": bool(any(lg["flag_earnings"] for lg in legs)),
            "validation": validation, "reasons": "; ".join(fit.reasons), "notes": " ".join(fit.notes),
            "formula": fit.formula}


def _net(legs, greek: str, name: str) -> float:
    """Per-share net Greek of the structure: long legs +, short legs − (credit spreads list the short leg first)."""
    signs = [1] if len(legs) == 1 else ([-1, 1] if "credit" in name else [1, -1])
    vals = [lg[greek] for lg in legs]
    return float(sum(s * v for s, v in zip(signs, vals))) if all(pd.notna(v) for v in vals) else float("nan")


def deterioration(prev: pd.DataFrame | None, cur: pd.DataFrame, th: dict) -> pd.DataFrame:
    """Contracts that were usable on the previous load and now show a wider spread, missing bid, lower volume
    or a stale quote."""
    if prev is None or prev.empty:
        return pd.DataFrame(columns=["contract", "change"])
    a = prev.set_index("contract")
    b = cur.set_index("contract")
    common = a.index.intersection(b.index)
    a, b = a.loc[common], b.loc[common]
    was = a["liquidity"] == "USABLE"
    msgs = []
    for c in common[was.to_numpy()]:
        x, y = a.loc[c], b.loc[c]
        parts = []
        if y["spread_pct_mid"] > x["spread_pct_mid"] * 1.5 and y["spread_pct_mid"] > float(th["maximum_bid_ask_pct"]):
            parts.append(f"spread widened {x['spread_pct_mid']:.1f}% → {y['spread_pct_mid']:.1f}%")
        if x["bid"] > 0 and y["bid"] <= 0:
            parts.append("bid disappeared")
        if y["volume"] < x["volume"] and y["flag_low_volume"]:
            parts.append(f"volume fell {x['volume']:,.0f} → {y['volume']:,.0f}")
        if y["flag_stale"] and not x["flag_stale"]:
            parts.append("quote went stale")
        if parts:
            msgs.append({"contract": c, "change": "; ".join(parts)})
    return pd.DataFrame(msgs, columns=["contract", "change"])
