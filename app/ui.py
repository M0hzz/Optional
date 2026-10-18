"""Shared presentation helpers for the dashboard: labels, number formats, colours and help.

Every table goes through `show()`, so headers are plain words, numbers carry thousands
separators, fractions show as percentages, dates drop the empty 00:00:00, blanks show as
a dash and colour scales stay light enough to read dark text on.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

GUIDE = "https://claude.ai/code/artifact/3c22e2de-9705-406a-bfde-014f7ce39749"
_W = "https://en.wikipedia.org/wiki/"
# Every link below was checked to load (HTTP 200) when it was added.
LINKS = {
    "guide": ("optlab user guide", GUIDE),
    "iv": ("Implied volatility", _W + "Implied_volatility"),
    "vol": ("Volatility", _W + "Volatility_(finance)"),
    "smile": ("Volatility smile", _W + "Volatility_smile"),
    "greeks": ("The Greeks", _W + "Greeks_(finance)"),
    "bs": ("Black-Scholes model", _W + "Black%E2%80%93Scholes_model"),
    "style": ("American vs European options", _W + "Option_style"),
    "moneyness": ("Moneyness", _W + "Moneyness"),
    "oi": ("Open interest", _W + "Open_interest"),
    "put": ("Put options", _W + "Put_option"),
    "call": ("Call options", _W + "Call_option"),
    "spread": ("Credit spreads", _W + "Credit_spread_(options)"),
    "condor": ("Iron condor", _W + "Iron_condor"),
    "covered": ("Covered call", _W + "Covered_call"),
    "straddle": ("Straddle", _W + "Straddle"),
    "oic": ("Options Industry Council (free options education)", "https://www.optionseducation.org/"),
    "cboe": ("Cboe delayed quotes", "https://www.cboe.com/delayed_quotes/"),
    "backtest": ("Backtesting", _W + "Backtesting"),
    "sharpe": ("Sharpe ratio", _W + "Sharpe_ratio"),
    "drawdown": ("Drawdown", _W + "Drawdown_(economics)"),
    "cvar": ("CVaR (expected shortfall)", _W + "Expected_shortfall"),
    "riskparity": ("Risk parity", _W + "Risk_parity"),
    "hrp": ("Hierarchical risk parity", _W + "Hierarchical_Risk_Parity"),
    "kelly": ("Position sizing (Kelly criterion)", _W + "Kelly_criterion"),
    "form4": ("SEC Form 4", _W + "Form_4"),
    "insider": ("Insider trading", _W + "Insider_trading"),
    "8k": ("Form 8-K", _W + "Form_8-K"),
    "xbrl": ("XBRL financial data", _W + "XBRL"),
    "edgar": ("SEC EDGAR full-text search", "https://www.sec.gov/search-filings"),
    "pe": ("Price-earnings ratio", _W + "Price%E2%80%93earnings_ratio"),
    "beta": ("Beta", _W + "Beta_(finance)"),
    "yield": ("Yield curve", _W + "Yield_curve"),
    "vix": ("VIX", _W + "VIX"),
    "rsi": ("Relative strength index", _W + "Relative_strength_index"),
    "ma": ("Moving averages", _W + "Moving_average"),
    "macd": ("MACD", _W + "MACD"),
    "bb": ("Bollinger Bands", _W + "Bollinger_Bands"),
    "atr": ("Average true range", _W + "Average_true_range"),
    "stoch": ("Stochastic oscillator", _W + "Stochastic_oscillator"),
    "vwap": ("VWAP", _W + "Volume-weighted_average_price"),
    "quantlib": ("QuantLib", "https://www.quantlib.org/"),
    "vectorbt": ("vectorbt", "https://vectorbt.dev/"),
    "skfolio": ("skfolio", "https://skfolio.org/"),
}


def links(*keys: str) -> str:
    return " · ".join(f"[{LINKS[k][0]}]({LINKS[k][1]})" for k in keys)


def intro(summary: str, steps: list[str], learn: list[str] | tuple = ()) -> None:
    """One line under the tab title, plus a collapsible how-to with links for the details."""
    st.caption(summary)
    with st.expander("How to use this tab"):
        st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
        if learn:
            st.markdown("**Learn more:** " + links(*learn))


# ------------------------------------------------------------------ labels
LABELS = {
    "symbol": "Symbol", "score": "Score", "close": "Close", "iv_rv": "IV ÷ RV", "rv10": "RV 10d", "rv20": "RV 20d",
    "rv60": "RV 60d", "rv_rank": "RV rank", "vol_of_vol": "Vol of vol", "ret20": "20-day return",
    "drawdown": "Off 1-year high", "insider": "Insider score", "days_to_earnings": "Days to earnings",
    "n_financial_flags": "Financial flags", "n_filing_flags": "Filing flags", "qlib_fwd_vol_change": "Qlib vol forecast",
    "buy_value": "Bought ($)", "sell_value": "Sold ($)", "buyers": "Buyers", "filing_date": "Filed",
    "transaction_date": "Traded", "insider_name": "Insider", "title": "Title", "code": "Code", "shares": "Shares",
    "price": "Price", "value": "Value", "metric": "Metric", "period_end": "Quarter ending", "year_ago": "Year earlier",
    "yoy_pct": "Change vs a year ago", "financial_flags": "Financial red flags", "filing_flags": "Filing red flags",
    "form": "Form", "accession_no": "Accession no.", "items": "8-K items", "description": "Description", "url": "Link",
    "expiration": "Expiration", "dte": "DTE", "type": "Type", "option_type": "Type", "strike": "Strike", "bid": "Bid",
    "ask": "Ask", "last": "Last", "mid": "Mid", "volume": "Volume", "open_interest": "Open interest", "iv": "IV",
    "implied_vol": "IV", "delta": "Delta", "gamma": "Gamma", "theta": "Theta", "vega": "Vega", "spread_pct": "Spread",
    "otm_pct": "Out of the money", "breakeven": "Breakeven", "annualized_yield": "Annualized yield",
    "contract": "Contract", "qty": "Qty", "underlying_price": "Underlying", "moneyness": "Moneyness",
    "prob_otm": "Chance OTM", "position": "Position", "status": "Status", "reasons": "Reasons", "pnl": "P&L ($)",
    "pnl_pct": "P&L", "short_strike_otm_pct": "Short strike distance", "position_delta": "Delta (shares)",
    "theta_day": "Theta ($/day)", "details": "Details", "scan": "Scan", "time": "Time", "body": "Message",
    "total_return_%": "Total return", "annual_return_%": "Annual return", "annual_vol_%": "Annual volatility",
    "sharpe": "Sharpe ratio", "sortino": "Sortino ratio", "max_drawdown_%": "Max drawdown", "calmar": "Calmar ratio",
    "entry": "Opened", "exit": "Closed", "spot_in": "Price in", "spot_out": "Price out", "strikes": "Strikes",
    "credit": "Credit ($/share)", "capital": "Capital ($/share)", "account": "Account", "reason": "Exit reason",
    "return_pct": "Return", "weight": "Weight", "root": "Root",
}
_WORDS = {"iv": "IV", "dte": "DTE", "oi": "OI", "pnl": "P&L", "atm": "ATM", "yoy": "YoY", "rv": "RV", "otm": "OTM",
          "pct": "%", "id": "ID", "eps": "EPS", "us": "US", "atr": "ATR", "bp": "BP"}


def label(col) -> str:
    col = str(col)
    if col in LABELS:
        return LABELS[col]
    words = [_WORDS.get(w, w) for w in col.replace("-", "_").split("_") if w]
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else col


# ---------------------------------------------------------------- formats
def _date(v) -> str:
    try:
        t = pd.Timestamp(v)
    except (TypeError, ValueError):
        return str(v)
    if t is pd.NaT:
        return "—"
    return t.strftime("%Y-%m-%d") if (t.hour, t.minute, t.second) == (0, 0, 0) else t.strftime("%Y-%m-%d %H:%M")


def _compact(v) -> str:
    """$37.47B, $812.5M, $2.06: big dollar amounts without a wall of digits."""
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= div:
            return f"{'-' if v < 0 else ''}${a / div:,.2f}{suf}"
    return f"{'-' if v < 0 else ''}${a:,.2f}"


def _money(decimals: int):
    def f(v) -> str:
        return f"{'−' if v < 0 else ''}${abs(v):,.{decimals}f}"
    return f


FMT = {
    "money": _money(2), "money0": _money(0), "smoney0": "{:+,.0f}", "int": "{:,.0f}", "num": "{:,.2f}",
    "num1": "{:,.1f}", "num3": "{:,.3f}", "num4": "{:,.4f}", "signed": "{:+,.2f}", "ratio": "{:,.2f}×",
    "pct": "{:.1%}", "pct2": "{:.2%}", "spct": "{:+.1%}", "pts": "{:,.1f}%", "spts": "{:+,.1f}%",
    "date": _date, "text": str, "compact": _compact,
}


def fmt(value, kind: str) -> str:
    try:
        if value is None or pd.isna(value):
            return "—"
    except (TypeError, ValueError):  # list-like values
        pass
    f = FMT[kind]
    return f(value) if callable(f) else f.format(value)


def _auto(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "text"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    if pd.api.types.is_integer_dtype(s):
        return "int"
    if pd.api.types.is_float_dtype(s):
        x = s.dropna()
        return "int" if len(x) and (x == x.round()).all() and x.abs().max() >= 100 else "num"
    if s.dtype == object and len(s.dropna()) and all(hasattr(v, "year") for v in s.dropna().head(20)):
        return "date"
    return "text"


def show(df: pd.DataFrame, formats: dict | None = None, gradient: dict | None = None, help: dict | None = None,
         rename: dict | None = None, height: int | None = None, styler=None, **kwargs):
    """st.dataframe with friendly headers and formatted values.

    formats  {column: kind} for kinds in FMT; unlisted columns are inferred.
    gradient {column: (vmin, vmax)} light red-to-green shading, dark text throughout.
    help     {column: tooltip} shown when hovering the column header.
    styler   optional function(Styler) -> Styler for extra styling (e.g. status colours).
    """
    formats, gradient, help = formats or {}, gradient or {}, help or {}
    data = df.copy()
    if not isinstance(data.index, pd.RangeIndex):  # a meaningful index becomes an ordinary first column
        data = data.reset_index()
    kinds = {c: formats.get(c) or _auto(data[c]) for c in data.columns}
    names = {c: (rename or {}).get(c, label(c)) for c in data.columns}
    for c, k in kinds.items():  # bools read better as words
        if k == "text" and (pd.api.types.is_bool_dtype(data[c]) or (
                len(data[c].dropna()) and data[c].dropna().map(lambda v: isinstance(v, (bool, np.bool_))).all())):
            data[c] = data[c].map({True: "Yes", False: "No"})
    big = data.size > 150_000
    user_config = kwargs.pop("column_config", None) or {}  # caller's settings, keyed by display name
    opts = {"hide_index": True, **({"height": height} if height else {}), **kwargs}
    numeric = {c for c in data.columns if pd.api.types.is_numeric_dtype(data[c]) and not pd.api.types.is_bool_dtype(data[c])}
    gappy = {c for c in data.columns if data[c].isna().any() and names[c] not in user_config}
    config = {names[c]: st.column_config.Column(help=help.get(c), alignment="right" if c in numeric else None)
              for c in data.columns if c in help or c in numeric & gappy}
    # The grid draws empty cells as "None" whatever the Styler says, so columns with gaps are written out as text.
    raw = data.rename(columns=names)
    for c in gappy:
        k = kinds[c]
        data[c] = data[c].map(lambda v, k=k: fmt(v, k) if k in FMT else ("—" if v is None or v != v else v))
    data = data.rename(columns=names)
    if big:  # Styler is too slow for very large tables; let the grid format numbers with separators
        for c, k in kinds.items():
            fmt_ = {"int": "localized", "num": "localized", "num1": "%.1f", "num3": "%.3f", "num4": "%.4f",
                    "money": "dollar", "money0": "dollar", "pct": "percent", "pts": "%.1f%%", "spts": "%+.1f%%",
                    "signed": "%+.2f", "smoney0": "%+.0f"}.get(k)
            if c not in gappy and fmt_:
                config[names[c]] = st.column_config.NumberColumn(format=fmt_, help=help.get(c))
        return st.dataframe(data, column_config={**config, **user_config} or None, **opts)
    gappy = {names[c] for c in gappy}
    sty = data.style.format({names[c]: FMT[k] for c, k in kinds.items()
                             if k in FMT and k != "text" and names[c] not in gappy}, na_rep="—")
    for c, (lo, hi) in gradient.items():
        if c in names:
            n = names[c]
            sty = sty.background_gradient(subset=[n], cmap="RdYlGn", vmin=lo, vmax=hi, low=0.35, high=0.35,
                                          text_color_threshold=0, gmap=raw[n].astype(float).fillna((lo + hi) / 2))
    if styler:
        sty = styler(sty)
    return st.dataframe(sty, column_config={**config, **user_config} or None, **opts)


def status_styler(columns, colors: dict):
    """Background colours for status words (pass/near/fail, HOLD/WATCH/EXIT), matched case-insensitively."""
    cmap = {k.lower(): v for k, v in colors.items()}

    def apply(sty):
        cols = [c for c in columns if c in sty.data.columns]
        return sty.map(lambda v: f"background-color: {cmap[str(v).lower()]}" if str(v).lower() in cmap else "",
                       subset=cols) if cols else sty
    return apply


STATUS_COLORS = {"pass": "#d1e7dd", "near": "#fff3cd", "fail": "#f8d7da", "n/a": "#e9ecef",
                 "hold": "#d1e7dd", "watch": "#fff3cd", "exit": "#f8d7da", "error": "#e9ecef",
                 "exact": "#d1e7dd"}
