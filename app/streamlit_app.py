"""optlab dashboard.   Run:  streamlit run app/streamlit_app.py"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))

import altair as alt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import polars as pl  # noqa: E402
import streamlit as st  # noqa: E402

import ui  # noqa: E402
from optlab.backtest import BTParams, buy_and_hold, simulate, stats, sweep  # noqa: E402
from optlab.config import load_config  # noqa: E402
from optlab.data import Store, risk_free_rate  # noqa: E402
from optlab.data.chain import atm_iv, skew_25d, with_dte  # noqa: E402
from optlab.filings import filing_flags, financial_changes, financial_flags, insider_signal  # noqa: E402
from optlab.pricing import OptionSpec, implied_vol, payoff_at_expiry, price, scenario_grid  # noqa: E402
from optlab.pricing.engine import entry_cost, fill_entry_prices, price_position  # noqa: E402
from optlab.research import build_features, rank_candidates  # noqa: E402
from optlab.risk import RiskLimits, allocate, check_limits, portfolio_report, size_position, stress_loss  # noqa: E402
from optlab.strategies import STRATEGIES, max_loss  # noqa: E402
import overlay_ui as ov  # noqa: E402
from optlab.overlay import OverlayConfigError, frames_from_prices, load_overlay_config  # noqa: E402
from optlab.overlay import scan as overlay_run  # noqa: E402
from optlab.overlay.config import USER_PATH as OVERLAY_PATH, defaults as overlay_defaults, validate as overlay_validate  # noqa: E402

st.set_page_config(page_title="optlab", page_icon="📈", layout="wide")
CFG = load_config()
DEF = CFG["strategy_defaults"]

# display names for internal codes
BT_NAMES = {"put_write": "Put write (cash-secured put)", "covered_call": "Covered call",
            "put_spread": "Put credit spread", "iron_condor": "Iron condor", "buy_and_hold": "Buy and hold"}
ALLOC_NAMES = {"min_cvar": "Minimum CVaR", "risk_parity": "Risk parity", "hrp": "Hierarchical risk parity",
               "max_sharpe": "Maximum Sharpe"}
EXIT_NAMES = {"profit_take": "Profit target", "expiry": "Expiry", "stop_loss": "Stop loss", "open": "Still open"}
SERIES_NAMES = {"treasury_1m": "1-month", "treasury_3m": "3-month", "treasury_1y": "1-year", "treasury_2y": "2-year",
                "treasury_10y": "10-year", "vix": "VIX"}
PALETTE = ["#1f77b4", "#e4572e", "#2ca02c", "#9467bd", "#8c564b"]  # distinct colours for multi-line charts
STRAT_HELP = ("Covered call: own 100 shares, sell a call above. Cash-secured put: sell a put, hold cash to buy "
              "the shares. Credit spreads: sell an option and buy a further one as insurance, so the loss is capped. "
              "Iron condor: a put spread plus a call spread; profits if the price stays in a range. "
              "Long straddle: buy a call and a put; profits from a big move either way.")


# ---------------------------------------------------------------- data access
@st.cache_resource
def store() -> Store:
    return Store(CFG["data"]["db_path"], read_only=True)


@st.cache_data(ttl=600)
def load_prices() -> pl.DataFrame:
    return store().query("SELECT symbol, date, open, high, low, close, volume FROM prices ORDER BY symbol, date")


@st.cache_data(ttl=600)
def load_chain(symbol: str) -> pl.DataFrame:
    return store().latest_chain(symbol)


@st.cache_data(ttl=600)
def load_insiders() -> pl.DataFrame:
    return store().insider_trades()


def _table(name: str, reader) -> pl.DataFrame:
    # databases built before a table existed lack it until ingest.py is re-run
    return reader() if store().has_table(name) else pl.DataFrame()


@st.cache_data(ttl=600)
def load_fundamentals() -> pl.DataFrame:
    return _table("fundamentals", store().latest_fundamentals)


@st.cache_data(ttl=600)
def load_macro() -> pl.DataFrame:
    return _table("macro", store().macro)


@st.cache_data(ttl=600)
def load_filings() -> pl.DataFrame:
    return _table("filings", store().filings)


@st.cache_data(ttl=600)
def load_financials() -> pl.DataFrame:
    return _table("financials", store().financials)


def stored_quotes(symbol: str) -> pd.DataFrame | None:
    """The stored chain in the overlay's column names, with liquidity flags (for leg quote checks)."""
    ch = load_chain(symbol)
    if ch.is_empty():
        return None
    from optlab.overlay.account import Profile
    from optlab.overlay.options import contract_flags

    df = with_dte(ch).to_pandas().rename(columns={"option_type": "type", "implied_vol": "iv"})
    for col in ("delta", "theta", "gamma", "vega"):
        if col not in df:
            df[col] = np.nan
    df["mid"] = (df["bid"] + df["ask"]) / 2
    return contract_flags(df, OCFG.thresholds, Profile.from_config(OCFG.account))


def snapshot_dates() -> list:
    if not store().has_table("option_chains"):
        return []
    return store().query("SELECT DISTINCT snapshot_date FROM option_chains")["snapshot_date"].to_list()


def next_earnings(symbol: str):
    f = load_fundamentals()
    if f.is_empty():
        return None
    row = f.filter(pl.col("symbol") == symbol)
    return row["next_earnings"][0] if not row.is_empty() else None


def close_series(symbol: str) -> pd.Series:
    df = load_prices().filter(pl.col("symbol") == symbol).to_pandas()
    return pd.Series(df["close"].values, index=pd.to_datetime(df["date"]), name=symbol)


if not Path(CFG["data"]["db_path"]).exists():
    st.error("No database yet. Run `python scripts/ingest.py` first.")
    st.stop()

symbols = store().symbols()
RATE = risk_free_rate(store(), CFG)
src_label = CFG["data"]["source"]

# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("optlab")
    st.caption(f"Data source: **{src_label.capitalize()}**"
               + (" (illustrative numbers, not real markets)" if src_label == "synthetic" else ""))
    symbol = st.selectbox("Underlying", symbols, index=symbols.index("SPY") if "SPY" in symbols else 0,
                          help="The stock or ETF every view looks at. Live › Option chain can also load any other "
                               "symbol.")
    st.divider()
    equity = st.number_input("Account equity ($)", value=float(CFG["account"]["equity"]), step=5000.0, format="%.0f",
                             help="Your account size. Position sizing and risk limits in Live › Trade builder are "
                                  "measured against it. The default comes from config.yaml → account.equity.")
    st.caption(f"= **{ui.fmt(equity, 'money0')}**")
    limits = RiskLimits(**{**CFG["account"], "equity": equity})
    st.caption("Risk limits come from config.yaml → account.")
    rate_src = CFG["market"].get("rate_series", "treasury_3m") if not load_macro().is_empty() else "config fallback"
    st.caption(f"Risk-free rate: **{RATE:.2%}** ({SERIES_NAMES.get(rate_src, rate_src)} Treasury)"
               if rate_src in SERIES_NAMES else f"Risk-free rate: **{RATE:.2%}** ({rate_src})",
               help="Used in every option price. It comes from the latest Treasury bill yield in the Macro data.")
    st.divider()
    st.markdown(f"📘 [User guide]({ui.GUIDE})", help="Step-by-step guide to every view, with a sample market day.")
    st.caption("Two sections: **Live** for market hours, **Research** for backtests and analysis. Each view has a **How to use this view** box, and ⓘ icons explain every control.")
    st.divider()
    try:
        OCFG = load_overlay_config()
        ocfg_error = None
    except OverlayConfigError as exc:   # a broken overlay.yaml never takes the dashboard down: defaults + a warning
        OCFG, ocfg_error = overlay_validate(overlay_defaults()), exc
    OF = ov.sidebar_controls(OCFG)

spot_series = close_series(symbol)
spot = float(spot_series.iloc[-1])
chain = load_chain(symbol)
iv_atm = atm_iv(chain) or float(np.log(spot_series).diff().tail(20).std() * np.sqrt(252) * 1.1)

earn_date = next_earnings(symbol)


# ------------------------------------------------------------------ overlay console (above every view)
@st.cache_data(ttl=300, show_spinner="Running overlay rules...")
def overlay_scan(cfg_json: str, data_key: str, minute: str):
    """Cached on settings, database file time and the clock minute: data, settings or a new bar rescan."""
    import json

    from optlab.overlay import audit as oaudit

    cfg = overlay_validate(json.loads(cfg_json))
    frames = frames_from_prices(load_prices().to_pandas())
    sc = overlay_run(frames, cfg, pd.Timestamp.now(tz="America/New_York").to_pydatetime(), src_label,
                     macro=load_macro().to_pandas() if not load_macro().is_empty() else None)
    try:
        oaudit.append(sc)          # state changes only; nothing from config.yaml is written
    except OSError:
        pass
    return sc


@st.cache_data(ttl=300, show_spinner="Combining overlay states...")
def overlay_confluence(cfg_json: str, data_key: str, minute: str):
    from optlab.overlay import confluence as cf

    return cf.table(overlay_scan(cfg_json, data_key, minute))


def overlay_now_key() -> tuple[str, str]:
    import json

    db = Path(CFG["data"]["db_path"])
    return json.dumps(OCFG.raw, sort_keys=True, default=str), f"{db.stat().st_mtime:.0f}"


_OKEY = (*overlay_now_key(), pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M"))
OSCAN = overlay_scan(*_OKEY) if OF["on"] else None
OCONF = overlay_confluence(*_OKEY) if OF["on"] else None
if ocfg_error is not None:
    st.error("overlay.yaml has problems, so the built-in overlay defaults are in use:\n\n"
             + "\n".join(f"- {p}" for p in ocfg_error.problems))


def overlay_on(key: str) -> bool:
    return OSCAN is not None and OCFG.tab_on(key)


# ------------------------------------------------------------------ navigation: two sections
# Live: what you use while the market is open. Research: backtests and everything that looks back.
SECTIONS = {
    "live": ("🟢 Live", [("watchlist", "Watchlist"), ("market", "Market"), ("chain", "Option chain"),
                        ("trade", "Trade builder"), ("monitor", "Monitor")]),
    "research": ("🔬 Research", [("backtest", "Backtest"), ("explorer", "Signal explorer"),
                                ("pricing", "Volatility & pricing"), ("company", "Company & insiders"),
                                ("allocation", "Portfolio allocation"), ("overlay", "Overlay setup")]),
}
VIEW_HELP = {
    "watchlist": "Today's ranking of every symbol, the overlay's rule status for each, and cross-tab confluence.",
    "market": "Market regime, breadth, volatility and sectors, plus Treasury rates and the VIX.",
    "chain": "Full live option chain for any symbol, with quote quality, account fit and a defined-risk structure scanner.",
    "trade": "Build a strategy, see its payoff, stress tests and risks, then size it and check it against your book.",
    "monitor": "What the live monitor (scripts/monitor.py) found on its latest pass, and its alerts.",
    "backtest": "Replay a strategy over history, with diagnostics on how far to trust the result.",
    "explorer": "When did a set of conditions match in the past, and what happened next.",
    "pricing": "The stored chain's volatility smile and surface, and a QuantLib option calculator.",
    "company": "Fundamentals, financial changes, red-flag filings and insider trading.",
    "allocation": "How to split capital across strategy return streams (skfolio).",
    "overlay": "The overlay's data checks, rules, status guide, settings and audit log.",
}
VIEW_SECTION = {k: sec for sec, (_, views) in SECTIONS.items() for k, _ in views}


def market_open_now() -> bool:
    from optlab.history.bars import nyse_business_days

    t = pd.Timestamp.now(tz="America/New_York")
    return bool(nyse_business_days(t.date(), t.date())) and pd.Timestamp("09:30").time() <= t.time() < pd.Timestamp("16:00").time()


MARKET_OPEN = market_open_now()
_qp_view = st.query_params.get("view")
_default_sec = VIEW_SECTION.get(_qp_view) or st.query_params.get("section") or ("live" if MARKET_OPEN else "research")
nav = st.columns([2, 5])
SECTION = nav[0].segmented_control(
    "Section", list(SECTIONS), format_func=lambda k: SECTIONS[k][0], key="nav_section",
    default=_default_sec if _default_sec in SECTIONS else "live", label_visibility="collapsed",
    help="Live: use while the market is open (watchlist, market, chains, building and sizing trades, the monitor). "
         "Research: backtests, historical searches, pricing studies, company data, allocation and overlay setup.") or "live"
nav[1].caption(("🟢 Market open (New York)" if MARKET_OPEN else "⚪ Market closed: live quotes show the last session")
               + " · " + VIEW_HELP.get(st.session_state.get(f"nav_view_{SECTION}") or SECTIONS[SECTION][1][0][0], ""))
_views = SECTIONS[SECTION][1]
_default_view = _qp_view if _qp_view in dict(_views) else _views[0][0]
VIEW = st.segmented_control("View", [k for k, _ in _views], format_func=dict(_views).get, key=f"nav_view_{SECTION}",
                            default=_default_view, label_visibility="collapsed") or _views[0][0]
st.query_params.update({"section": SECTION, "view": VIEW})
SHOW = {k: k == VIEW for k in VIEW_SECTION}
if OSCAN is not None:          # the overlay console stays above every view in both sections
    ov.console(OSCAN, OF, ov.market_chips(OSCAN) + [ov.account_chip(OCFG)] + ov.confluence_chips(OCONF),
               expanded=SECTION == "live")
st.divider()

# ------------------------------------------------------------------- scanner
if SHOW["watchlist"]:
    st.subheader("Where is premium rich?")
    ui.intro("Ranks every symbol by how attractive it looks for selling options today. Higher score = better "
             "candidate.",
             ["Read the table top to bottom: the score combines how expensive options are versus how much the stock "
              "actually moves (IV ÷ RV), whether volatility is calming down, how jumpy volatility itself is, and "
              "insider buying.",
              "Hover any column header for what it means.",
              "Check **Days to earnings**: if it is shorter than the expiry you plan to trade, the trade holds "
              "through an earnings gap.",
              "Non-zero **flags** columns mean red-flag filings or worsening financials; see Research › Company & insiders.",
              "The charts below show the symbol picked in the sidebar: its price, and its realized volatility "
              "against today's implied volatility (dashed line)."],
             ["iv", "vol", "guide"])
    if overlay_on("scanner"):
        ov.scanner_panel(OSCAN)
        ov.confluence_panel(OSCAN, OCONF)
        st.divider()
    feats = build_features(load_prices().select(["symbol", "date", "close"]))
    ivs = {s: atm_iv(load_chain(s)) for s in symbols}
    ivs = {k: v for k, v in ivs.items() if v}
    ins = insider_signal(load_insiders())
    ins_scores = dict(zip(ins["symbol"].to_list(), ins["score"].to_list())) if not ins.is_empty() else {}
    ranked = rank_candidates(feats, ivs or None, ins_scores).to_pandas()
    fund = load_fundamentals()
    if not fund.is_empty():
        e = fund.select(["symbol", "next_earnings"]).to_pandas()
        e["days_to_earnings"] = (pd.to_datetime(e["next_earnings"]) - pd.Timestamp.today().normalize()).dt.days
        ranked = ranked.merge(e[["symbol", "days_to_earnings"]], on="symbol", how="left")
    flag_frames = [financial_flags(financial_changes(load_financials())) if not load_financials().is_empty() else None,
                   filing_flags(load_filings()) if not load_filings().is_empty() else None]
    for fl in flag_frames:
        if fl is not None and not fl.is_empty():
            ranked = ranked.merge(fl.select(["symbol", fl.columns[2]]).to_pandas(), on="symbol", how="left")
    qlib_file = ROOT / "data" / "qlib_rankings.csv"
    if qlib_file.exists():
        q = pd.read_csv(qlib_file)
        q.columns = ["symbol", "qlib_fwd_vol_change"]
        q["symbol"] = q["symbol"].str.upper()
        ranked = ranked.merge(q, on="symbol", how="left")
    lim = float(ranked["score"].abs().max() or 1)
    ui.show(ranked,
            formats={"score": "signed", "close": "money", "iv_rv": "ratio", "rv10": "pct", "rv20": "pct", "rv60": "pct",
                     "rv_rank": "pct", "vol_of_vol": "pct", "ret20": "spct", "drawdown": "spct", "insider": "signed",
                     "days_to_earnings": "int", "n_financial_flags": "int", "n_filing_flags": "int",
                     "qlib_fwd_vol_change": "spct"},
            gradient={"score": (-lim, lim)},
            help={"score": "Overall attractiveness for selling options. Above 0 = better than the average symbol.",
                  "close": "Latest closing price.",
                  "iv_rv": "Implied vol (what options price in) ÷ 20-day realized vol (what the stock actually did). "
                           "Above 1 means options are expensive relative to recent moves.",
                  "rv10": "Realized volatility over the last 10 trading days, annualized.",
                  "rv20": "Realized volatility over the last 20 trading days, annualized.",
                  "rv60": "Realized volatility over the last 60 trading days, annualized.",
                  "rv_rank": "Where 20-day realized vol sits in its 1-year range: 0% = calmest, 100% = wildest.",
                  "vol_of_vol": "How much realized vol itself jumps around. Lower is steadier and safer for sellers.",
                  "ret20": "Price change over the last 20 trading days.",
                  "drawdown": "How far the price is below its 1-year high.",
                  "insider": "Insider buying score from −1 (heavy selling) to +1 (heavy buying). See Research › Company & insiders.",
                  "days_to_earnings": "Calendar days until the next earnings report.",
                  "n_financial_flags": "Count of worsening financial metrics (see Research › Company & insiders).",
                  "n_filing_flags": "Count of red-flag 8-K filings in the last 90 days (see Research › Company & insiders).",
                  "qlib_fwd_vol_change": "Qlib model's forecast of the change in volatility over the next month."},
            width="stretch")
    st.caption("Higher score = better candidate for selling options. "
               + ("Qlib model predictions are merged in." if qlib_file.exists()
                  else "Train the Qlib model (integrations/qlib) to add machine-learning predictions here."))

    c1, c2 = st.columns(2)
    hist = feats.filter(pl.col("symbol") == symbol).select(["date", "close", "rv20", "rv60"]).drop_nulls().to_pandas()
    c1.altair_chart(alt.Chart(hist).mark_line().encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("close:Q", title="Price ($)", scale=alt.Scale(zero=False), axis=alt.Axis(format="$,.0f")),
        tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("close:Q", title="Close", format="$,.2f")])
        .properties(title=f"{symbol} price", height=260), width="stretch")
    vol_long = hist.rename(columns={"rv20": "20-day realized", "rv60": "60-day realized"}).melt(
        id_vars="date", value_vars=["20-day realized", "60-day realized"], var_name="Window", value_name="vol")
    vol_chart = alt.Chart(vol_long).mark_line().encode(
        x=alt.X("date:T", title=None), y=alt.Y("vol:Q", title="Volatility (annualized)", axis=alt.Axis(format="%")),
        color=alt.Color("Window:N", legend=alt.Legend(title=None, orient="top"), scale=alt.Scale(range=PALETTE)),
        tooltip=[alt.Tooltip("date:T", title="Date"), "Window:N", alt.Tooltip("vol:Q", title="Volatility", format=".1%")])
    rule = alt.Chart(pd.DataFrame({"iv": [iv_atm]})).mark_rule(strokeDash=[4, 4], color="crimson").encode(y="iv:Q")
    c2.altair_chart((vol_chart + rule).properties(title=f"Realized vol vs today's 30-day ATM IV ({iv_atm:.0%}, dashed)",
                                                   height=260), width="stretch")

# ------------------------------------------------------------ chain & pricer
if SHOW["pricing"]:
    st.subheader("Volatility smile and surface")
    ui.intro("The stored option chain for the sidebar symbol, and a calculator that prices any single option.",
             ["The top row summarises the chain: price, at-the-money implied volatility, and skew (how much more "
              "downside puts cost than upside calls).",
              "Drag **Expiration** to see the volatility smile for that date. Points are individual strikes; hover "
              "one for its quotes.",
              "The heat map shows put IV across every expiry and strike. Brighter = more expensive.",
              "In **QuantLib pricer**, enter a strike, days and volatility to get a fair price and Greeks. Type a "
              "market price to back out the implied volatility it implies.",
              "For every contract with live quotes, use **Live › Option chain** instead."],
             ["iv", "smile", "greeks", "bs", "style", "quantlib"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spot", ui.fmt(spot, "money"), help="Latest closing price of the underlying.")
    c2.metric("30-day ATM IV", f"{iv_atm:.1%}",
              help="Implied volatility of at-the-money options about 30 days out: the market's expected yearly move.")
    sk = skew_25d(chain)
    c3.metric("Skew (90% put − 110% call IV)", f"{sk:.1%}" if sk is not None else "—",
              help="IV of a put 10% below the price minus IV of a call 10% above. Positive = crash protection "
                   "costs more than upside, which is normal for stocks.")
    c4.metric("Chain snapshot", ui.fmt(chain["snapshot_date"][0], "date") if not chain.is_empty() else "None",
              help="Date the stored chain was captured by scripts/ingest.py.")
    if overlay_on("chain_pricer") and not chain.is_empty():
        from optlab.overlay.quality import chain_health

        _hc = chain_health(symbol, chain.to_pandas().rename(columns={"option_type": "type", "implied_vol": "iv"}), None,
                           pd.Timestamp.now(tz="America/New_York").to_pydatetime(), OCFG.thresholds, "stored snapshot")
        _bad = [c for c in _hc if not c.passed and c.effect != "none"]
        ov.chips([("STALE_DATA", f"🕰️ Stored chain is old: {c.detail} {c.action}") if c.id == "quote_age" else
                  ("DATA_ERROR", f"❌ {c.name} failed: {c.detail} {c.action}")
                  for c in _bad] or [("INFO", "🩺 Stored chain passed its data checks")])

    if not chain.is_empty():
        ch = with_dte(chain).to_pandas()
        ch["option_type"] = ch["option_type"].str.capitalize()
        exps = sorted(ch["expiration"].unique())
        exp = st.select_slider("Expiration", options=exps, value=exps[min(3, len(exps) - 1)],
                               format_func=lambda d: ui.fmt(d, "date"),
                               help="Pick an expiry date to draw its volatility smile.")
        sub = ch[(ch["expiration"] == exp) & ch["moneyness"].between(0.75, 1.25)]
        smile = alt.Chart(sub).mark_line(point=True).encode(
            x=alt.X("strike:Q", title="Strike ($)", scale=alt.Scale(zero=False)),
            y=alt.Y("implied_vol:Q", title="Implied volatility", axis=alt.Axis(format="%")),
            color=alt.Color("option_type:N", title=None, legend=alt.Legend(orient="top"),
                            scale=alt.Scale(domain=["Call", "Put"], range=["#1f77b4", "#e4572e"])),
            tooltip=[alt.Tooltip("strike:Q", title="Strike", format="$,.2f"), alt.Tooltip("option_type:N", title="Type"),
                     alt.Tooltip("bid:Q", title="Bid", format="$,.2f"), alt.Tooltip("ask:Q", title="Ask", format="$,.2f"),
                     alt.Tooltip("implied_vol:Q", title="IV", format=".1%"),
                     alt.Tooltip("open_interest:Q", title="Open interest", format=",.0f")])
        st.altair_chart(smile.properties(title=f"Volatility smile, {ui.fmt(exp, 'date')}", height=280), width="stretch")
        surf = ch[ch["moneyness"].between(0.8, 1.2) & (ch["option_type"] == "Put")]
        heat = alt.Chart(surf).mark_rect().encode(
            x=alt.X("strike:O", title="Strike ($)", axis=alt.Axis(labelOverlap=True, format=",.0f")),
            y=alt.Y("dte:O", title="Days to expiry"),
            color=alt.Color("implied_vol:Q", title="IV", scale=alt.Scale(scheme="viridis"), legend=alt.Legend(format="%")),
            tooltip=[alt.Tooltip("strike:Q", title="Strike", format="$,.2f"), alt.Tooltip("dte:Q", title="DTE"),
                     alt.Tooltip("implied_vol:Q", title="IV", format=".1%")])
        st.altair_chart(heat.properties(title="Put IV surface (days to expiry × strike)", height=260), width="stretch")
        with st.expander("Raw quotes for this expiration"):
            ui.show(sub.drop(columns=["snapshot_date", "symbol"]),
                    formats={"strike": "money", "bid": "money", "ask": "money", "last": "money", "implied_vol": "pct",
                             "underlying_price": "money", "moneyness": "num3", "volume": "int", "open_interest": "int",
                             "dte": "int", "expiration": "date"},
                    help={"moneyness": "Strike ÷ price. Below 1 = below the current price.",
                          "implied_vol": "Implied volatility backed out of the option's price.",
                          "open_interest": "Contracts currently open. Higher = easier to trade."},
                    width="stretch")

    st.subheader("QuantLib pricer")
    p1, p2, p3, p4, p5, p6 = st.columns(6)
    k = p1.number_input("Strike", value=float(round(spot)), step=1.0, format="%.2f",
                        help="The price at which the option lets you buy (call) or sell (put) the shares.")
    days = p2.number_input("Days to expiry", value=30, min_value=1, help="Calendar days until the option expires.")
    vol = p3.number_input("Volatility", value=round(iv_atm, 3), step=0.01, format="%.3f",
                          help="Annualized volatility as a decimal: 0.250 = 25%. Defaults to today's 30-day ATM IV.")
    typ = p4.selectbox("Type", ["put", "call"], format_func=str.capitalize,
                       help="Call = right to buy at the strike. Put = right to sell at the strike.")
    style = p5.selectbox("Exercise", ["american", "european"], format_func=str.capitalize,
                         help="American options can be exercised any day (US stocks and ETFs). European only at "
                              "expiry (most index options, e.g. SPX).")
    mkt = p6.number_input("Market price (for IV)", value=0.0, step=0.05, format="%.2f",
                          help="Optional. Enter a quoted option price to solve for the implied volatility it implies.")
    fq = load_fundamentals()
    fq = fq.filter(pl.col("symbol") == symbol) if not fq.is_empty() else fq
    div_q = float(fq["dividend_yield"][0]) if not fq.is_empty() and fq["dividend_yield"][0] is not None \
        else CFG["market"]["dividend_yield"]
    spec = OptionSpec(spot, k, days, vol, typ, RATE, div_q, style)
    st.caption(f"Spot {ui.fmt(spot, 'money')} · rate {RATE:.2%} · dividend yield {div_q:.2%}"
               + (" (from fundamentals)" if not fq.is_empty() else ""))
    g = price(spec)
    m = st.columns(6)
    for col, (name, val, f, tip) in zip(m, [
            ("Fair value", g["price"], "${:,.2f}", "Theoretical price per share. One contract = 100 shares."),
            ("Delta", g["delta"], "{:+.3f}", "Change in option price for a $1 move in the stock. Also a rough "
                                              "probability of finishing in the money."),
            ("Gamma", g["gamma"], "{:.4f}", "Change in delta for a $1 move in the stock."),
            ("Vega (per 1 pt)", g["vega"], "{:.3f}", "Change in option price if implied volatility rises 1 point."),
            ("Theta (per day)", g["theta"], "{:+.3f}", "Change in option price from one day passing."),
            ("Rho (per 1%)", g["rho"], "{:+.3f}", "Change in option price if interest rates rise 1%.")]):
        col.metric(name, f.format(val), help=tip)
    if mkt > 0:
        iv = implied_vol(mkt, spec)
        st.info(f"Implied volatility at \\${mkt:,.2f}: **{iv:.2%}**" if np.isfinite(iv) else "No volatility reproduces that price.")

# -------------------------------------------------------------- strategy lab
if SHOW["trade"]:
    st.subheader("Build a strategy")
    ui.intro("Build a standard options strategy on the sidebar symbol and see what it costs, what it can lose, and "
             "how it behaves if the price or volatility moves.",
             ["Pick a **Strategy**, then set days to expiry (**DTE**), how far out the short option sits (**Short "
              "delta**: 0.30 is closer to the price than 0.10) and the **Wing width** for spreads.",
              "The metric row shows the cash you collect or pay, the worst case at expiry and the position Greeks "
              "in dollars.",
              "The payoff chart shows profit and loss against the stock price: solid line at expiry, dashed line "
              "halfway there.",
              "Move **Scenario horizon** to see the P&L grid a number of days from now. Rows change implied "
              "volatility, columns move the stock."],
             ["spread", "condor", "covered", "straddle", "greeks", "oic"])
    c1, c2, c3, c4 = st.columns(4)
    strat = c1.selectbox("Strategy", list(STRATEGIES), index=2, help=STRAT_HELP)
    dte = c2.slider("DTE", 7, 120, DEF["dte"], help="Days to expiry for the options in the trade.")
    delta = c3.slider("Short delta", 0.05, 0.50, DEF["short_delta"], 0.01,
                      help="Picks the strike of the option you sell. Delta ≈ chance it ends in the money: 0.16 is "
                           "roughly a 1-in-6 chance, further from the price and cheaper.")
    width = c4.slider("Wing width (% of spot)", 1.0, 15.0, DEF["spread_width_pct"], 0.5,
                      help="Distance between the sold and bought strikes in spreads and condors. Wider = more credit "
                           "but a bigger maximum loss.")
    legs = STRATEGIES[strat](spot, iv_atm, dte=dte, short_delta=delta, spread_width_pct=width, rate=RATE)
    if earn_date is not None and (earn_date - pd.Timestamp.today().date()).days <= dte:
        st.warning(f"{symbol} reports earnings on {ui.fmt(earn_date, 'date')}, before this {dte}-day expiry. Expect a "
                   "gap and an IV crush; size for the move or pick an expiry before the report.")
    fill_entry_prices(legs, spot, RATE, style="european")
    cost = entry_cost(legs)
    greeks = price_position(legs, spot, RATE, style="european")
    worst = max_loss(legs)

    m = st.columns(6)
    m[0].metric("Net credit" if cost < 0 else "Net debit", f"${abs(cost):,.0f}",
                help="Cash collected (credit) or paid (debit) to open one unit of the strategy.")
    m[1].metric("Max loss (expiry)", f"${worst:,.0f}" if worst < 1e6 else "Unlimited",
                help="Worst possible loss if held to expiry, checked over a wide range of prices.")
    m[2].metric("Dollar delta", f"{greeks['dollar_delta']:+,.0f}",
                help="The position's exposure as an equivalent dollar amount of stock. Positive = you gain when "
                     "the stock rises.")
    m[3].metric("Gamma (shares per $1)", f"{greeks['gamma']:+,.1f}",
                help="How many shares of delta you gain or lose for each $1 move in the stock.")
    m[4].metric("Vega ($ per vol point)", f"{greeks['vega']:+,.0f}",
                help="Dollar P&L if implied volatility rises 1 point. Negative = you gain when volatility falls.")
    m[5].metric("Theta ($ per day)", f"{greeks['theta']:+,.0f}",
                help="Dollars earned (positive) or lost per day from time passing, all else equal.")
    ui.show(pd.DataFrame([{"type": leg.option_type.capitalize(), "strike": leg.strike, "qty": leg.quantity,
                           "price": leg.entry_price} for leg in legs]),
            formats={"strike": "money", "qty": "int", "price": "money"},
            rename={"qty": "Quantity", "price": "Price per share"},
            help={"qty": "Positive = bought, negative = sold. Stock legs count shares."})

    grid_x = np.linspace(spot * 0.7, spot * 1.3, 241)
    pay = pd.DataFrame({"spot": grid_x, "At expiry": payoff_at_expiry(legs, grid_x)})
    mid = []
    for x in grid_x[::6]:
        mid.append(price_position(legs, x, RATE, style="european", days_forward=dte / 2)["value"] - cost)
    pay_mid = pd.DataFrame({"spot": grid_x[::6], f"At {dte // 2} DTE": mid})
    lines = (alt.Chart(pay.melt("spot", var_name="When", value_name="pnl")).mark_line()
             + alt.Chart(pay_mid.melt("spot", var_name="When", value_name="pnl")).mark_line(strokeDash=[5, 3])
             ).encode(x=alt.X("spot:Q", title="Stock price ($)", scale=alt.Scale(zero=False), axis=alt.Axis(format="$,.0f")),
                      y=alt.Y("pnl:Q", title="P&L ($)", axis=alt.Axis(format="$,.0f")),
                      color=alt.Color("When:N", legend=alt.Legend(title=None, orient="top"),
                                      scale=alt.Scale(domain=["At expiry", f"At {dte // 2} DTE"],
                                                      range=["#1f77b4", "#e4572e"])),
                      tooltip=[alt.Tooltip("spot:Q", title="Stock price", format="$,.2f"), "When:N",
                               alt.Tooltip("pnl:Q", title="P&L", format="$,.0f")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="gray").encode(y="y:Q")
    now = alt.Chart(pd.DataFrame({"x": [spot]})).mark_rule(strokeDash=[2, 2], color="gray").encode(x="x:Q")
    st.altair_chart((lines + zero + now).properties(title="Payoff (dotted vertical line = today's price)", height=320),
                    width="stretch")

    days_fwd = st.slider("Scenario horizon (days from now)", 0, dte, min(7, dte),
                         help="How many days ahead the scenario grid below is priced. 0 = right now.")
    sg = scenario_grid(legs, spot, RATE, days_forward=days_fwd)
    sdf = pd.DataFrame(sg["pnl"], index=[f"IV {v:+.0%}" for v in sg["vol_shifts"]],
                       columns=[f"{m:+.0%}" for m in sg["spot_moves"]])
    st.markdown("**Scenario P&L ($)**: rows change implied volatility, columns move the underlying",
                help="Each cell is the dollar profit or loss if the stock moves by the column's amount and implied "
                     "volatility shifts by the row's amount, after the chosen number of days.")
    glim = float(np.abs(sdf.to_numpy()).max() or 1)
    st.dataframe(sdf.style.format("{:+,.0f}").background_gradient(cmap="RdYlGn", axis=None, vmin=-glim, vmax=glim,
                                                                  low=0.35, high=0.35, text_color_threshold=0),
                 width="stretch")
    if overlay_on("strategy_lab"):
        _px = load_prices().filter(pl.col("symbol") == symbol).to_pandas()
        from optlab.live.indicators import atr as _atr

        _atr_v = float(_atr(_px["high"], _px["low"], _px["close"], 14).iloc[-1]) if len(_px) > 20 else None
        ov.strategy_panel(legs, spot, RATE, dte, iv_atm, OCFG, symbol, spot_series, _atr_v, stored_quotes(symbol), strat)

# ------------------------------------------------------------------ backtest
if SHOW["backtest"]:
    st.subheader("Quick backtest (vectorbt)")
    ui.intro("Replays a strategy over the symbol's price history to see how it would have done versus simply "
             "holding the stock.",
             ["Choose a **Strategy** and its settings. **Profit take %** closes a trade early once it has earned "
              "that share of the credit (0 = always hold to expiry).",
              "**IV / RV premium** sets how expensive options were assumed to be: 1.15 means implied volatility "
              "15% above realized. Real markets usually sit between 1.0 and 1.3.",
              "Pick a **Start** date. The table compares the strategy with buy-and-hold; the chart shows how $1 "
              "would have grown.",
              "Open **Trade log** to see every simulated trade.",
              "Press **Run DTE × delta sweep** to test many expiry and strike choices at once and see which "
              "worked best.",
              "Options here are repriced from history, not real quotes. Treat results as a screen and re-test "
              "winners in LEAN with real data."],
             ["backtest", "sharpe", "drawdown", "vectorbt"])
    c1, c2, c3, c4, c5 = st.columns(5)
    bstrat = c1.selectbox("Strategy", ["put_write", "covered_call", "put_spread", "iron_condor"],
                          format_func=BT_NAMES.get, help=STRAT_HELP)
    bdte = c2.slider("DTE ", 7, 90, DEF["dte"], help="Days to expiry for each new trade.")
    bdelta = c3.slider("Delta ", 0.05, 0.50, DEF["short_delta"], 0.01,
                       help="Delta of the option sold. Lower = further from the price, safer, less premium.")
    pt = c4.slider("Profit take %", 0, 90, int(DEF["profit_take_pct"]), 5,
                   help="Close early once the trade has made this % of the credit received. 0 = hold to expiry.")
    prem = c5.slider("IV / RV premium", 0.8, 1.6, 1.15, 0.05,
                     help="Assumed implied vol as a multiple of realized vol. Above 1 = options were expensive, "
                          "which is what option sellers are paid for.")
    start = st.date_input("Start", value=spot_series.index[0].date(), format="YYYY-MM-DD",
                          min_value=spot_series.index[0].date(), max_value=spot_series.index[-1].date(),
                          help="First day of the backtest.")
    px = spot_series[spot_series.index >= pd.Timestamp(start)]
    base = BTParams(strategy=bstrat, dte=bdte, short_delta=bdelta, profit_take_pct=pt, iv_premium=prem,
                    width_pct=DEF["spread_width_pct"], slippage_pct=DEF["slippage_pct_of_premium"],
                    commission=DEF["commission_per_contract"], rate=RATE)
    rets, log = simulate(px, base)
    bh = buy_and_hold(px)
    comp = pd.concat([rets, bh], axis=1).fillna(0).rename(columns=BT_NAMES)
    BT_HELP = {"total_return_%": "Gain over the whole period.", "annual_return_%": "Average yearly gain, compounded.",
               "annual_vol_%": "How much daily returns swing, annualized. Lower = smoother.",
               "sharpe": "Return per unit of risk. Above 1 is good, above 2 excellent.",
               "sortino": "Like Sharpe, but only counts downside swings as risk.",
               "max_drawdown_%": "Largest fall from a peak to a later low.",
               "calmar": "Annual return ÷ max drawdown. Higher = better reward for the worst loss."}
    ui.show(stats(comp).rename_axis("Series"),
            formats={c: "spts" if c.endswith("%") else "num" for c in BT_HELP}, help=BT_HELP, width="stretch")
    eq = (1 + comp).cumprod().rename_axis("date").reset_index().melt("date", var_name="Series", value_name="growth")
    st.altair_chart(alt.Chart(eq).mark_line().encode(
        x=alt.X("date:T", title=None), y=alt.Y("growth:Q", title="Growth of $1", axis=alt.Axis(format="$,.2f")),
        color=alt.Color("Series:N", legend=alt.Legend(title=None, orient="top", labelLimit=0),
                        scale=alt.Scale(range=PALETTE)),
        tooltip=[alt.Tooltip("date:T", title="Date"), "Series:N", alt.Tooltip("growth:Q", title="Growth of $1", format="$,.2f")]
    ).properties(height=300), width="stretch")
    if not log.empty:
        w = (log["pnl"] > 0).mean()
        st.markdown(f"**{len(log):,} trades** · win rate {w:.0%} · average win "
                    f"{log.loc[log.pnl > 0, 'return_pct'].mean():+.2f}% · average loss "
                    f"{log.loc[log.pnl <= 0, 'return_pct'].mean():+.2f}% · exits: "
                    + ", ".join(f"{EXIT_NAMES.get(k, k).lower()} {v:,}" for k, v in log["reason"].value_counts().items()))
        with st.expander("Trade log"):
            tl = log.assign(strikes=log["strikes"].map(lambda ks: " / ".join(f"{k:,.2f}" for k in ks)),
                            reason=log["reason"].map(lambda r: EXIT_NAMES.get(r, r)))
            ui.show(tl, formats={"entry": "date", "exit": "date", "spot_in": "money", "spot_out": "money", "iv": "pct",
                                 "credit": "money", "pnl": "signed", "capital": "money", "account": "money",
                                 "return_pct": "spts"},
                    rename={"pnl": "P&L ($/share)", "account": "Account ($/share)"},
                    help={"credit": "Premium collected per share (negative = paid).",
                          "capital": "Cash or margin tied up per share.",
                          "account": "Account size per share backing one unit (capital ÷ the fraction allowed).",
                          "return_pct": "P&L ÷ capital.", "iv": "Assumed implied volatility at entry."},
                    width="stretch")

    if overlay_on("backtest"):
        ov.backtest_panel(px, base, OCFG)
    SWEEP_METRICS = ["sharpe", "annual_return_%", "max_drawdown_%", "calmar"]
    if st.button("Run DTE × delta sweep",
                 help="Re-runs the backtest for 5 expiries × 6 deltas (30 runs) and shows the result as a heat map. "
                      "Takes a few seconds."):
        with st.spinner("Sweeping..."):
            st.session_state["sweep"] = sweep(px, base)[1]
    if "sweep" in st.session_state:
        st_grid = st.session_state["sweep"]
        metric = st.radio("Metric", SWEEP_METRICS, horizontal=True, format_func=ui.label,
                          help="Which result to colour the heat map by. Green = better.")
        hm = st_grid[metric].reset_index().rename(columns={metric: "v"})
        title = ui.label(metric)
        st.altair_chart(alt.Chart(hm).mark_rect().encode(
            x=alt.X("delta:O", title="Short delta"), y=alt.Y("dte:O", title="Days to expiry"),
            color=alt.Color("v:Q", title=title, scale=alt.Scale(scheme="redyellowgreen")),
            tooltip=[alt.Tooltip("dte:O", title="DTE"), alt.Tooltip("delta:O", title="Delta"),
                     alt.Tooltip("v:Q", title=title, format=",.2f")]
        ).properties(height=260), width="stretch")
        st.caption("Max drawdown is negative, so green (closest to zero) is best there too. The sweep uses the "
                   "settings above; press the button again after changing them.")

# -------------------------------------------------------------- risk & sizing
if SHOW["trade"]:
    st.divider()
    st.subheader("Size this trade")
    ui.intro("How many contracts you can trade within your risk limits, a check of your whole book, and how to "
             "split capital across strategies.",
             ["**Size this trade**: pick a strategy, DTE and delta. The contract count respects both your max loss "
              "per trade and your max exposure to one underlying (both set in config.yaml → account).",
              "**Book check**: type your open positions into the table (add rows with the + at the bottom). Use "
              "negative quantities for options you sold. Greeks are totalled and checked against your limits.",
              "**Allocate across strategies**: choose a method; the optimizer splits capital across simulated "
              "strategy return streams and shows the combined result."],
             ["kelly", "greeks", "cvar", "riskparity", "hrp", "skfolio"])
    c1, c2, c3 = st.columns(3)
    rs = c1.selectbox("Strategy ", list(STRATEGIES), index=2, help=STRAT_HELP)
    rdte = c2.slider("DTE  ", 7, 120, DEF["dte"], help="Days to expiry.")
    rdelta = c3.slider("Short delta  ", 0.05, 0.50, DEF["short_delta"], 0.01, help="Delta of the option sold.")
    rlegs = STRATEGIES[rs](spot, iv_atm, dte=rdte, short_delta=rdelta, spread_width_pct=DEF["spread_width_pct"], rate=RATE)
    fill_entry_prices(rlegs, spot, RATE, style="european")
    if rs == "Cash-secured put":
        worst, capital = stress_loss(rlegs, spot, -0.25), rlegs[0].strike * 100
        st.caption("Undefined-risk trade: sized on a 25% gap-down stress loss; capital = cash to secure the put.")
    elif rs == "Covered call":
        worst, capital = stress_loss(rlegs, spot, -0.25), spot * 100
        st.caption("Sized on a 25% gap-down stress loss; capital = 100 shares.")
    else:
        worst = max_loss(rlegs)
        capital = worst if worst > 0 else spot * 100 * 0.2
    sz = size_position(worst, capital, limits)
    m = st.columns(4)
    m[0].metric("Contracts", f"{sz['contracts']:,}", help="Largest number of contracts that stays within both limits.")
    m[1].metric("Limited by", {"max loss per trade": "Max loss", "underlying concentration": "Concentration"}
                .get(sz["limited_by"], sz["limited_by"].capitalize()),
                help="Which limit set the contract count: max loss per trade (account.max_risk_per_trade_pct) or "
                     "concentration in one underlying (account.max_underlying_pct).")
    m[2].metric("Total max loss", f"${sz['total_max_loss']:,.0f}", help="Worst-case loss for all contracts together.")
    m[3].metric("Capital used", f"${sz['total_capital']:,.0f}", help="Cash or margin the position ties up.")
    if sz["contracts"] == 0:
        st.info(f"Even one contract (max loss \\${worst:,.0f}) is above your per-trade limit of "
                f"{limits.max_risk_per_trade_pct:g}% of equity (\\${equity * limits.max_risk_per_trade_pct / 100:,.0f}). "
                "Try a narrower wing width, a lower delta, or raise the limit in config.yaml.")
    if overlay_on("risk"):
        ov.risk_panel(OCFG, rlegs, spot, symbol, rs)

    st.subheader("Book check")
    st.caption("Edit your open positions; Greeks are recomputed and checked against limits. Use the + under the "
               "table to add a row, and select a row and press Delete to remove it.")
    default_book = pd.DataFrame([
        {"symbol": symbol, "type": "Put", "strike": round(spot * 0.95), "dte": 30, "qty": -2, "vol": round(iv_atm, 3)},
        {"symbol": symbol, "type": "Put", "strike": round(spot * 0.90), "dte": 30, "qty": 2, "vol": round(iv_atm, 3)},
    ])
    book = st.data_editor(st.session_state.get("book", default_book), num_rows="dynamic", width="stretch",
                          column_config={
                              "symbol": st.column_config.TextColumn("Symbol", help="Underlying ticker."),
                              "type": st.column_config.SelectboxColumn("Type", options=["Put", "Call"],
                                                                       help="Put or Call."),
                              "strike": st.column_config.NumberColumn("Strike", format="$%.2f"),
                              "dte": st.column_config.NumberColumn("DTE", help="Days to expiry.", format="%d"),
                              "qty": st.column_config.NumberColumn("Qty", format="%d",
                                                                   help="Contracts. Negative = sold, positive = bought."),
                              "vol": st.column_config.NumberColumn("Vol", format="%.3f",
                                                                   help="Implied volatility as a decimal (0.250 = 25%).")})
    rows = []
    from optlab.pricing.engine import Leg
    clean = book.dropna()
    for sym_, grp in clean.groupby("symbol"):
        s_spot = float(close_series(sym_).iloc[-1]) if sym_ in symbols else spot
        lg = [Leg(str(r["type"]).lower(), float(r["strike"]), float(r["dte"]), int(r["qty"]), float(r["vol"]))
              for _, r in grp.iterrows()]
        fill_entry_prices(lg, s_spot, RATE, style="european")
        gk = price_position(lg, s_spot, RATE, style="european")
        # capital at risk = worst expiry loss of this symbol's legs (spreads net out)
        rows.append({"symbol": sym_, "dollar_delta": gk["dollar_delta"], "vega": gk["vega"],
                     "theta": gk["theta"], "capital": max_loss(lg)})
    if rows:
        pos = pd.DataFrame(rows)
        chk = check_limits(pos, limits)
        m = st.columns(4)
        m[0].metric("Dollar delta", f"{chk.metrics['dollar_delta']:+,.0f}", f"{chk.metrics['delta_pct']:.0f}% of equity",
                    delta_color="off", help="Net stock-equivalent exposure of the whole book, in dollars.")
        m[1].metric("Vega ($ per vol point)", f"{chk.metrics['vega']:+,.0f}", f"{chk.metrics['vega_pct']:.2f}% of equity",
                    delta_color="off", help="Dollar P&L of the whole book if implied volatility rises 1 point.")
        m[2].metric("Theta ($ per day)", f"{pos['theta'].sum():+,.0f}", help="Dollars the book earns per day from time.")
        m[3].metric("Largest underlying", f"{chk.metrics['largest_underlying']}",
                    f"{chk.metrics['largest_underlying_pct']:.0f}% of equity", delta_color="off",
                    help="The symbol with the most capital at risk, as a share of equity.")
        if chk.ok:
            st.success("Within all limits.")
        for b in chk.breaches:
            st.error(b)

if SHOW["allocation"]:
    st.subheader("Allocate across strategies (skfolio)")
    method = st.selectbox("Method", ["min_cvar", "risk_parity", "hrp", "max_sharpe"], format_func=ALLOC_NAMES.get,
                          help="Minimum CVaR: smallest average loss on the worst 5% of days. Risk parity: each "
                               "strategy contributes equal risk. Hierarchical risk parity: groups similar strategies "
                               "first, then spreads risk. Maximum Sharpe: best return per unit of risk (most prone "
                               "to overfitting).")
    streams = {}
    for s in symbols[:6]:
        for strat_name in ("put_write", "put_spread", "iron_condor"):
            streams[f"{s} {BT_NAMES[strat_name]}"] = simulate(close_series(s), BTParams(strategy=strat_name, rate=RATE))[0]
    R = pd.DataFrame(streams).fillna(0.0)
    R = R[R.index >= R.index[-1] - pd.Timedelta(days=365 * 4)]
    try:
        wts = allocate(R, method=method, max_weight=0.25)
        rep = portfolio_report(R, wts)
        c1, c2 = st.columns([1, 2])
        with c1:
            ui.show(wts[wts > 0.001].sort_values(ascending=False).rename("weight").rename_axis("Strategy").to_frame(),
                    formats={"weight": "pct"}, help={"weight": "Share of capital. Each is capped at 25%."},
                    width="stretch")
        m = c2.columns(4)
        m[0].metric("Annual return", f"{rep['annual_return_%']:+.1f}%", help="Average yearly gain of the blend.")
        m[1].metric("Annual volatility", f"{rep['annual_vol_%']:.1f}%", help="How much the blend swings, annualized.")
        m[2].metric("Max drawdown", f"{rep['max_drawdown_%']:.1f}%", help="Largest peak-to-trough fall.")
        m[3].metric("95% CVaR (daily)", f"{rep['cvar95_daily_%']:.2f}%",
                    help="Average loss on the worst 5% of days.")
        ec = rep["equity_curve"].rename_axis("date").rename("growth").reset_index()
        c2.altair_chart(alt.Chart(ec).mark_line().encode(
            x=alt.X("date:T", title=None), y=alt.Y("growth:Q", title="Growth of $1", scale=alt.Scale(zero=False),
                                                   axis=alt.Axis(format="$,.2f")),
            tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("growth:Q", title="Growth of $1", format="$,.3f")]
        ).properties(height=220), width="stretch")
        st.caption("Weights are fitted and evaluated on the same 4 years, so these stats are in-sample and optimistic.")
    except Exception as exc:
        st.warning(f"Optimizer failed: {exc}")

# ------------------------------------------------------------------- company
if SHOW["company"]:
    st.subheader(f"{symbol}: fundamentals, financials and filings")
    ui.intro("Business health checks before you sell options on a company: valuation, the latest quarter versus "
             "a year earlier, and recent SEC filings that can signal trouble.",
             ["The top row shows valuation and the next earnings date. Selling options across an earnings report "
              "means accepting a possible gap.",
              "**Financial changes** compares the latest quarter with the same quarter a year earlier. Red cells "
              "are getting worse.",
              "**Red flags** lists any symbol with worrying 8-K filings (restatements, auditor changes, "
              "impairments, executive departures) or deteriorating financials.",
              "**Recent filings** links straight to each document on the SEC site."],
             ["pe", "beta", "8k", "xbrl", "edgar"])
    fund = load_fundamentals()
    frow = fund.filter(pl.col("symbol") == symbol) if not fund.is_empty() else fund
    if frow.is_empty():
        st.info("No fundamentals for this symbol (ETFs have none). For stocks, run `python scripts/ingest.py`.")
    else:
        f = frow.row(0, named=True)
        fmt = lambda v, spec: "—" if v is None else spec.format(v)  # noqa: E731
        m = st.columns(5)
        m[0].metric("Market cap", fmt(f["market_cap"] and f["market_cap"] / 1e9, "${:,.0f}B"),
                    help="Share price × shares outstanding, in billions.")
        m[1].metric("P/E (forward)", f"{fmt(f['pe_ratio'], '{:,.1f}')} ({fmt(f['forward_pe'], '{:,.1f}')})",
                    help="Price ÷ last year's earnings per share; in brackets, price ÷ next year's expected earnings.")
        m[2].metric("Dividend yield", fmt(f["dividend_yield"], "{:.2%}"), help="Yearly dividends ÷ share price.")
        m[3].metric("Beta", fmt(f["beta"], "{:.2f}"),
                    help="How much the stock moves with the market: 1 = in line, 1.5 = 50% bigger moves.")
        m[4].metric("Next earnings", ui.fmt(f["next_earnings"], "date"),
                    f"in {(f['next_earnings'] - pd.Timestamp.today().date()).days:,} days" if f["next_earnings"] else None,
                    delta_color="off", help="Expected date of the next quarterly earnings report.")
        st.caption(f"Profit margin {fmt(f['profit_margin'], '{:.1%}')} · revenue growth "
                   f"{fmt(f['revenue_growth'], '{:+.1%}')} · debt/equity {fmt(f['debt_to_equity'], '{:,.0f}')} "
                   f"· as of {ui.fmt(f['as_of'], 'date')} (OpenBB / yfinance)")

    st.markdown("**Financial changes (SEC XBRL, latest quarter vs a year earlier)**")
    fin = load_financials()
    fin_sym = fin.filter(pl.col("symbol") == symbol) if not fin.is_empty() else fin
    if fin_sym.is_empty():
        st.info("No financials. Set edgar.identity in config.yaml and run `python scripts/ingest.py --source openbb`.")
    else:
        ch = financial_changes(fin_sym).to_pandas()
        ch["metric"] = ch["metric"].map(ui.label)
        ui.show(ch.drop(columns="symbol"), formats={"period_end": "date", "value": "compact", "year_ago": "compact", "yoy_pct": "spts"},
                gradient={"yoy_pct": (-50, 50)},
                help={"value": "Latest reported value, in US dollars (per-share items in dollars per share).",
                      "year_ago": "Same quarter one year earlier.",
                      "yoy_pct": "Percentage change. For debt, a rise is a bad sign even though it shows green."},
                width="stretch")
        trend = (fin_sym.filter(pl.col("metric").is_in(["revenue", "net_income"]))
                 .with_columns(pl.col("period_end").cast(pl.Utf8)).to_pandas())
        trend["metric"] = trend["metric"].map(ui.label)
        trend["value"] = trend["value"] / 1e9
        st.altair_chart(alt.Chart(trend).mark_bar().encode(
            x=alt.X("period_end:O", title="Quarter ending", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("value:Q", title="US$ billions", axis=alt.Axis(format="$,.0f")),
            color=alt.Color("metric:N", legend=alt.Legend(title=None, orient="top"), scale=alt.Scale(range=PALETTE)),
            xOffset="metric:N",
            tooltip=[alt.Tooltip("metric:N", title="Metric"), alt.Tooltip("period_end:O", title="Quarter ending"),
                     alt.Tooltip("value:Q", title="US$ billions", format="$,.2f")]
        ).properties(title="Quarterly revenue and net income", height=260), width="stretch")

    fl = financial_flags(financial_changes(fin)) if not fin.is_empty() else pl.DataFrame()
    ff = filing_flags(load_filings()) if not load_filings().is_empty() else pl.DataFrame()
    flagged = [d.filter(pl.col(d.columns[2]) > 0) for d in (fl, ff) if not d.is_empty()]
    flagged = [d for d in flagged if not d.is_empty()]
    st.markdown("**Red flags across the universe**")
    if flagged:
        out = flagged[0].to_pandas()
        for d in flagged[1:]:
            out = out.merge(d.to_pandas(), on="symbol", how="outer")
        counts = [c for c in out.columns if c.startswith("n_")]
        out[counts] = out[counts].fillna(0).astype(int)
        for c in ("financial_flags", "filing_flags"):
            if c in out:
                out[c] = out[c].fillna("").map(lambda t: t[:1].upper() + t[1:] if t else "—")
        ui.show(out.fillna("—"), rename={"n_financial_flags": "No. of financial flags", "n_filing_flags": "No. of filing flags"}, help={"financial_flags": "Financial metrics that worsened versus a year earlier.",
                                       "filing_flags": "Red-flag 8-K filings in the last 90 days, with dates.",
                                       "n_financial_flags": "Count of financial red flags.",
                                       "n_filing_flags": "Count of filing red flags."},
                width="stretch")
        st.caption("Restatements, auditor changes, impairments, executive departures, falling revenue or "
                   "rising debt: reasons to skip selling premium on a name until it settles.")
    else:
        st.success("No red-flag 8-Ks in the last 90 days and no deteriorating financials.")

    st.markdown("**Recent filings**")
    fil = load_filings()
    fil_sym = fil.filter(pl.col("symbol") == symbol) if not fil.is_empty() else fil
    if fil_sym.is_empty():
        st.info("No filings for this symbol.")
    else:
        fp = fil_sym.drop("symbol").to_pandas()
        fp["items"] = fp["items"].fillna("").str.replace(",", ", ")
        if not fp["url"].fillna("").str.strip().astype(bool).any():
            fp = fp.drop(columns="url")  # no links to show (synthetic data)
        ui.show(fp, formats={"filing_date": "date"}, width="stretch",
                help={"items": "8-K item numbers: 1.01 material agreement, 2.02 results, 4.01 auditor change, "
                               "4.02 restatement, 5.02 executive change, 8.01 other events, 9.01 exhibits.",
                      "accession_no": "The SEC's unique ID for the filing."},
                column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open",
                                                                   help="Opens the filing on sec.gov.")})
    if src_label == "synthetic":
        st.caption("Synthetic fundamentals, financials and filings for demonstration.")

# ------------------------------------------------------------------ insiders
if SHOW["company"]:
    st.divider()
    st.subheader("Insider activity (SEC Form 4)")
    ui.intro("Company officers and directors must report their own trades on SEC Form 4. Insiders buying with "
             "their own money is one of the more reliable bullish signals.",
             ["The first table summarises the last 90 days for every symbol. **Score** runs from −1 (heavy "
              "selling) to +1 (heavy buying).",
              "Purchases count 5× more than sales, because insiders sell for many reasons (taxes, diversifying) "
              "but buy for one. Several different buyers boost the score further.",
              "The second table lists every reported trade for the sidebar symbol. Code **P** = open-market "
              "purchase, **S** = sale.",
              "A positive score favours selling puts (you would be happy to own the stock) over calls."],
             ["form4", "insider", "edgar"])
    trades = load_insiders()
    if trades.is_empty():
        st.info("No insider data. Set edgar.identity in config.yaml and run `python scripts/ingest.py --source openbb`.")
    else:
        sig = insider_signal(trades)
        ui.show(sig.to_pandas(), formats={"buy_value": "money0", "sell_value": "money0", "buyers": "int", "score": "signed"},
                gradient={"score": (-1, 1)},
                help={"buy_value": "Dollar value of open-market purchases in the last 90 days.",
                      "sell_value": "Dollar value of open-market sales in the last 90 days.",
                      "buyers": "Number of different insiders who bought.",
                      "score": "−1 = heavy selling, +1 = heavy buying."},
                width="stretch")
        st.markdown(f"**Every reported trade for {symbol}**")
        ui.show(trades.filter(pl.col("symbol") == symbol).to_pandas(),
                formats={"filing_date": "date", "transaction_date": "date", "shares": "int", "price": "money",
                         "value": "money0"},
                rename={"insider": "Insider"},
                help={"filing_date": "Date the Form 4 reached the SEC.", "transaction_date": "Date of the trade.",
                      "code": "P = open-market purchase, S = sale, A = award, M = option exercise, F = tax withholding.",
                      "value": "Shares × price."},
                width="stretch")
        if src_label == "synthetic":
            st.caption("Synthetic insider records for demonstration.")

# --------------------------------------------------------------------- macro
if SHOW["market"]:
    st.subheader("Market overview, rates and volatility")
    ui.intro("Interest rates feed every option price, and the VIX shows how nervous the whole market is.",
             ["The top row shows today's Treasury yields from 1 month to 10 years, plus the VIX.",
              "**Treasury curve today** plots those yields by maturity. When short rates sit above long rates "
              "(an inverted curve) markets often expect a slowdown.",
              "The VIX chart shows market-wide implied volatility. High VIX = richer option premium, but also "
              "bigger moves against you.",
              "Option pricing in this app uses the rate named in the caption below."],
             ["yield", "vix", "iv"])
    if overlay_on("macro"):
        ov.market_panel(OSCAN)
        st.divider()
    mac = load_macro()
    if mac.is_empty():
        st.info("No macro data. Run `python scripts/ingest.py`.")
    else:
        md = mac.to_pandas()
        md["date"] = pd.to_datetime(md["date"])
        latest = md.sort_values("date").groupby("series").tail(1).set_index("series")["value"]
        tenors = [("treasury_1m", "1M"), ("treasury_3m", "3M"), ("treasury_1y", "1Y"), ("treasury_2y", "2Y"),
                  ("treasury_10y", "10Y")]
        m = st.columns(6)
        for col, (k, lab) in zip(m, tenors):
            col.metric(f"{lab} Treasury", f"{latest[k]:.2%}" if k in latest else "—",
                       help=f"Yield on {SERIES_NAMES[k]} US Treasury debt.")
        m[5].metric("VIX", f"{latest['vix']:,.1f}" if "vix" in latest else "—",
                    help="Expected 30-day volatility of the S&P 500, from its options. Below 15 calm, above 25 stressed.")
        if {"treasury_2y", "treasury_10y"} <= set(latest.index):
            slope = latest["treasury_10y"] - latest["treasury_2y"]
            rs_name = CFG["market"].get("rate_series", "treasury_3m")
            st.caption(f"10Y − 2Y slope: {slope * 1e4:+,.0f} basis points" + (" (inverted)" if slope < 0 else "")
                       + f" · option pricing uses the {SERIES_NAMES.get(rs_name, rs_name)} Treasury yield = {RATE:.2%}")
        c1, c2 = st.columns(2)
        curve = pd.DataFrame({"tenor": [lab for k, lab in tenors if k in latest],
                              "yield": [latest[k] for k, _ in tenors if k in latest]})
        c1.altair_chart(alt.Chart(curve).mark_line(point=True).encode(
            x=alt.X("tenor:N", title="Maturity", sort=[lab for _, lab in tenors]),
            y=alt.Y("yield:Q", title="Yield", axis=alt.Axis(format="%"), scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("tenor:N", title="Maturity"), alt.Tooltip("yield:Q", title="Yield", format=".2%")]
        ).properties(title="Treasury curve today", height=280), width="stretch")
        rates = md[md["series"].isin(["treasury_3m", "treasury_2y", "treasury_10y"])].assign(
            series=lambda d: d["series"].map(SERIES_NAMES))
        c2.altair_chart(alt.Chart(rates).mark_line().encode(
            x=alt.X("date:T", title=None), y=alt.Y("value:Q", title="Yield", axis=alt.Axis(format="%")),
            color=alt.Color("series:N", legend=alt.Legend(title=None, orient="top"), sort=["3-month", "2-year", "10-year"],
                            scale=alt.Scale(domain=["3-month", "2-year", "10-year"], range=PALETTE[:3])),
            tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("series:N", title="Maturity"),
                     alt.Tooltip("value:Q", title="Yield", format=".2%")]
        ).properties(title="Treasury yields", height=280), width="stretch")
        vix = md[md["series"] == "vix"]
        st.altair_chart(alt.Chart(vix).mark_line().encode(
            x=alt.X("date:T", title=None), y=alt.Y("value:Q", title="VIX"),
            tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("value:Q", title="VIX", format=",.1f")])
            .properties(title="VIX: market-wide implied volatility. High = rich premium, but bigger moves",
                        height=240), width="stretch")
        if src_label == "synthetic":
            st.caption("Synthetic rates and VIX for demonstration.")


# -------------------------------------------------------------- option chain
@st.cache_resource
def live_feed(kind: str):
    from optlab.live.feed import LiveFeed, SyntheticFeed

    return SyntheticFeed(rate=RATE) if kind == "synthetic" else LiveFeed(rate=RATE, chain_ttl=60)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_chain(ticker: str, source: str) -> tuple[pd.DataFrame, str]:
    feed = live_feed("synthetic" if source == "synthetic" else "live")
    return feed.chain(ticker, None if source == "synthetic" else source), pd.Timestamp.now(tz="America/New_York").isoformat()


CHAIN_HELP = {"bid": "Highest price a buyer is offering. What you get if you sell now.",
              "ask": "Lowest price a seller is asking. What you pay if you buy now.",
              "last": "Price of the most recent trade.", "mid": "Halfway between bid and ask.",
              "volume": "Contracts traded today.", "open_interest": "Contracts currently open. Higher = easier to trade.",
              "iv": "Implied volatility: the yearly move the option's price implies.",
              "delta": "Price change per $1 move in the stock; also a rough chance of ending in the money.",
              "gamma": "Change in delta per $1 move.", "theta": "Price change per day from time passing.",
              "vega": "Price change per 1-point rise in implied volatility.",
              "spread_pct": "Ask − bid as a % of mid. Under 5% is tight; wide spreads cost you on entry and exit.",
              "otm_pct": "How far the strike is from the price, in %. Negative = in the money.",
              "breakeven": "Stock price at expiry where a buyer of this option breaks even.",
              "annualized_yield": "Premium ÷ strike, annualized: the yield from selling it if it expires worthless.",
              "dte": "Days to expiry.", "contract": "OCC contract symbol.", "strike": "Strike price."}

if SHOW["chain"]:
    st.subheader("Full option chain")
    ui.intro("Every listed call and put for any optionable symbol, with quotes and Greeks.",
             ["Type a symbol (stocks, ETFs or indexes like SPX) and pick a **Source**. Cboe is free and 15 minutes "
              "delayed during market hours; outside hours you see the last close.",
              "Narrow it with **Expiration** and **Strikes within ±%**. Smaller views load faster.",
              "**Calls | Puts** puts calls on the left and puts on the right of each strike, like a broker screen; "
              "**List** shows one contract per row with extra columns such as yield and breakeven.",
              "Hover a column header for its meaning. Click a header to sort.",
              "**Download CSV** saves exactly the contracts shown."],
             ["greeks", "iv", "oi", "moneyness", "cboe", "oic"])
    c1, c2, c3 = st.columns([1, 1, 2])
    ticker = c1.text_input("Any optionable symbol", value=symbol,
                           help="Ticker of a stock, ETF or index, e.g. AAPL, SPY, SPX. Press Enter to load.").strip().upper()
    source = c2.selectbox("Source", ["cboe", "yfinance", "synthetic"],
                          format_func={"cboe": "Cboe", "yfinance": "Yahoo Finance", "synthetic": "Synthetic (offline)"}.get,
                          help="Cboe: every expiration and strike with Greeks, 15 minutes delayed, free. "
                               "Yahoo Finance: same contracts, slower, Greeks computed here. Synthetic: offline test data.")
    c3.caption("Standard calls and puts only: weeklies, monthlies, quarterlies and LEAPS. Adjusted contracts "
               "left over from splits or mergers are dropped. Index options use the index name (SPX, NDX, RUT).")
    if ticker:
        try:
            with st.spinner(f"Loading every {ticker} contract from {source}..."):
                ch, fetched = fetch_chain(ticker, source)
        except Exception as exc:
            ch, fetched = pd.DataFrame(), ""
            st.error(f"Couldn't load {ticker} from {source}: {str(exc).strip().splitlines()[-1][:300]}. "
                     "Check the symbol and your internet connection, or pick another source.")
        if not ch.empty:
            from optlab.live.scanner import chain_stats

            stats_ = chain_stats(ch)
            u = float(ch["underlying_price"].iloc[0])
            m = st.columns(6)
            m[0].metric(f"{ticker} price", ui.fmt(u, "money"), help="Underlying price reported with the chain.")
            m[1].metric("Contracts", f"{len(ch):,}", help="Calls and puts across every expiration.")
            m[2].metric("Expirations", f"{ch['expiration'].nunique():,}")
            m[3].metric("~30-day ATM IV", f"{stats_['atm_iv']:.1%}" if stats_["atm_iv"] == stats_["atm_iv"] else "—",
                        help="Implied volatility of at-the-money options about a month out.")
            m[4].metric("Put/call OI", f"{stats_['put_call_oi']:.2f}" if stats_["put_call_oi"] == stats_["put_call_oi"] else "—",
                        help="Open interest in puts ÷ calls. Above 1 = more puts outstanding, often a cautious mood.")
            m[5].metric("Fetched", pd.Timestamp(fetched).strftime("%H:%M:%S") if fetched else "—",
                        help="Time the chain was downloaded (New York time). It refreshes after 60 seconds.")

            f1, f2, f3, f4 = st.columns([2, 2, 1, 1])
            exps = sorted(ch["expiration"].unique())
            labels = ["All expirations"] + [f"{ui.fmt(e, 'date')} ({(e - pd.Timestamp.today().date()).days:,} days)"
                                            for e in exps]
            pick = f1.selectbox("Expiration", labels, help="Show one expiry date, or all of them.")
            band = f2.slider("Strikes within ±% of price (100 = all)", 5, 100, 100, 5,
                             help="Hide strikes far from the current price. 10 = only strikes within 10% of it.")
            layout = f3.radio("Layout", ["Calls | Puts", "List"],
                              help="Calls | Puts: side by side per strike. List: one contract per row, more columns.")
            greeks = f4.checkbox("Greeks", value=True, help="Show delta, gamma, theta and vega columns.")
            view = ch if pick == "All expirations" else ch[ch["expiration"] == exps[labels.index(pick) - 1]]
            if band < 100:
                view = view[(view["strike"] - u).abs() <= u * band / 100]
            view = view.assign(iv=view["iv"] * 100, type=view["type"].str.capitalize())
            cols = ["bid", "ask", "last", "volume", "open_interest", "iv"] + (["delta", "gamma", "theta", "vega"] if greeks else [])
            short = {"open_interest": "OI", "volume": "Volume", "iv": "IV"}
            name = lambda c: short.get(c, ui.label(c))  # noqa: E731
            if layout == "Calls | Puts":
                view = view.assign(root=view["contract"].str[:-15])
                key = ["expiration", "root", "strike"] if view["root"].nunique() > 1 else ["expiration", "strike"]
                calls = view[view["type"] == "Call"].set_index(key)[cols].rename(columns=lambda c: f"Call {name(c)}")
                puts = view[view["type"] == "Put"].set_index(key)[cols[::-1]].rename(columns=lambda c: f"Put {name(c)}")
                table = calls.join(puts, how="outer").reset_index()
                table = table[key[:-1] + list(calls.columns) + ["strike"] + list(puts.columns)]
                base_of = {f"{side} {name(c)}": c for side in ("Call", "Put") for c in cols}
            else:
                table = view[["expiration", "dte", "type", "strike"] + cols + ["mid", "spread_pct", "otm_pct", "breakeven",
                                                                               "annualized_yield", "contract"]]
                base_of = {}
            table = table.assign(expiration=table["expiration"].map(lambda d: ui.fmt(d, "date")))
            table = table.rename(columns={c: ui.label(c) for c in table.columns if c not in base_of})
            base_of.update({ui.label(c): c for c in ["expiration", "root", "dte", "type", "strike", "mid", "spread_pct",
                                                      "otm_pct", "breakeven", "annualized_yield", "contract"] + cols})
            num = {"bid": "dollar", "ask": "dollar", "last": "dollar", "mid": "dollar", "breakeven": "dollar",
                   "strike": "localized", "volume": "localized", "open_interest": "localized", "dte": "%d",
                   "iv": "%.1f%%", "spread_pct": "%.1f%%", "otm_pct": "%+.1f%%", "annualized_yield": "%.1f%%",
                   "delta": "%+.3f", "gamma": "%.4f", "theta": "%+.3f", "vega": "%.3f"}
            py = {"dollar": "${:,.2f}", "localized": "{:,.0f}", "%d": "{:,.0f}"}
            config = {}
            for c in table.columns:
                b = base_of.get(c)
                if b in num and table[c].isna().any():  # the grid prints gaps as "None"; write these out as text
                    f = py.get(num[b], num[b].replace("%%", "%").replace("%", "{:", 1).replace("f", "f}", 1))
                    table[c] = table[c].map(lambda v, f=f: "—" if v != v else f.format(v))
                    config[c] = st.column_config.Column(help=CHAIN_HELP.get(b), alignment="right")
                elif b in num:
                    config[c] = st.column_config.NumberColumn(format=num[b], help=CHAIN_HELP.get(b))
                elif b in CHAIN_HELP:
                    config[c] = st.column_config.Column(help=CHAIN_HELP[b])
            st.dataframe(table, width="stretch", hide_index=True, height=620, column_config=config)
            st.caption(f"{len(view):,} contracts shown · price {ui.fmt(u, 'money')} · strikes below the price are in "
                       "the money for calls, above it for puts · prices and Greeks are per share (× 100 per contract); "
                       "theta per day, vega per 1 volatility point.")
            st.download_button("Download CSV", view.assign(iv=view["iv"] / 100).to_csv(index=False).encode(),
                               f"{ticker}_chain.csv", "text/csv",
                               help="Saves the contracts currently shown, with every column, as a spreadsheet file.")
            if overlay_on("option_chain"):
                st.divider()
                fetched_at = pd.Timestamp(fetched).to_pydatetime() if fetched else None
                hv = None
                if ticker in symbols:
                    _c = close_series(ticker)
                    hv = float(np.log(_c).diff().tail(20).std() * np.sqrt(252))
                ov.chain_panel(ch, view, ticker, source, fetched_at, OSCAN, OCFG,
                               next_earnings(ticker) if ticker in symbols else None, hv)

# -------------------------------------------------------------------- monitor
MON = ROOT / "data" / "monitor"


@st.fragment(run_every="20s")
def monitor_panel():
    import json

    status_file = MON / "status.json"
    if not status_file.exists():
        st.info("The monitor hasn't run yet. In a terminal: `python scripts/monitor.py` (waits for the start time in "
                "scans.yaml), or `python scripts/monitor.py --once` for a single pass now.")
        return
    status = json.loads(status_file.read_text())
    last = pd.Timestamp(status["last_run"]) if status.get("last_run") else None
    age = (pd.Timestamp.now(tz=last.tz) - last).total_seconds() if last is not None else None
    m = st.columns(4)
    m[0].metric("Last pass", last.strftime("%a %H:%M:%S") if last is not None else "—",
                f"{age / 60:,.0f} min ago" if age and age > 120 else None, delta_color="off",
                help="When the monitor last finished scanning. During market hours it should be under a minute or two.")
    m[1].metric("Pass time", f"{status.get('seconds', 0):,.1f} s", help="How long one full pass took.")
    m[2].metric("Symbols", f"{status.get('symbols', 0):,}", help="Symbols checked in the last pass.")
    m[3].metric("Scan hits", f"{status.get('hits', 0):,}", help="Rows that met every condition of a scan.")
    st.caption(f"Schedule: {str(status.get('schedule', '—')).capitalize()}"
               + (f" · waiting until {status['waiting_until']}" if status.get("waiting_until") else "")
               + " · this panel refreshes every 20 seconds")
    for e in status.get("errors", []):
        st.warning(e)

    pos_file, hits_file = MON / "latest_positions.parquet", MON / "latest_hits.parquet"
    st.markdown("**Open positions**", help="Positions listed under `positions:` in scans.yaml, checked each pass "
                                           "against your exit rules.")
    pos = pd.read_parquet(pos_file) if pos_file.exists() else pd.DataFrame()
    if pos.empty:
        st.caption("No positions in scans.yaml. Add them under `positions:` to get Hold / Watch / Exit checks.")
    else:
        pos = pos.assign(status=pos["status"].astype(str).str.capitalize())
        ui.show(pos, formats={"pnl": "smoney0", "pnl_pct": "spts", "value": "money0", "price": "money",
                              "short_strike_otm_pct": "pts", "position_delta": "smoney0", "theta_day": "signed",
                              "dte": "int", "days_to_earnings": "int"},
                rename={"position_delta": "Delta (shares)", "value": "Value ($)"},
                help={"status": "Hold = fine. Watch = getting close to an exit rule. Exit = a rule triggered.",
                      "reasons": "Which rules fired.", "pnl_pct": "Profit or loss as a % of the credit or cost.",
                      "short_strike_otm_pct": "How far the price is from the strike you sold. Small = danger.",
                      "position_delta": "Share-equivalent exposure. Positive gains when the stock rises."},
                styler=ui.status_styler(["Status"], ui.STATUS_COLORS), width="stretch")
    st.markdown("**Scan hits**", help="Symbols and contracts that passed every condition in a scan on the last pass.")
    hits = pd.read_parquet(hits_file) if hits_file.exists() else pd.DataFrame()
    if not hits.empty and overlay_on("monitor"):
        hits = ov.hits_overlay(hits, OCFG)
    if hits.empty:
        st.caption("Nothing passes right now.")
    else:
        for scan_name, g in hits.groupby("scan", sort=False):
            st.markdown(f"*{scan_name}* · {g['symbol'].nunique():,} symbol(s)")
            g = g.drop(columns="scan").dropna(axis=1, how="all")
            if "type" in g:
                g = g.assign(type=g["type"].str.capitalize())
            ui.show(g, formats={"price": "money", "dte": "int", "strike": "money", "bid": "money", "ask": "money",
                                "mid": "money", "iv": "pct", "delta": "num3", "open_interest": "int", "volume": "int",
                                "spread_pct": "pts", "otm_pct": "pts", "annualized_yield": "pts", "prob_otm": "pts",
                                "expiration": "date"},
                    help={**CHAIN_HELP, "prob_otm": "Model chance the option expires worthless (out of the money).",
                          "details": "Indicator values behind the hit."},
                    rename={"quality_flags": "Quality flags", "account_fit": "Account fit"},
                    styler=lambda s_: ov._cell_colors(s_, ["Liquidity", "Account fit"]), width="stretch")
    alerts_file = MON / "alerts.csv"
    if alerts_file.exists():
        with st.expander("Recent alerts"):
            al = pd.read_csv(alerts_file).tail(100).iloc[::-1]
            al["time"] = al["time"].astype(str).str[:16].str.replace("T", " ")
            ui.show(al, formats={"time": "text"}, rename={"title": "Alert"}, width="stretch")


if SHOW["monitor"]:
    st.subheader("Live monitor")
    ui.intro("Shows what `python scripts/monitor.py` found on its latest pass. The monitor runs in a terminal, scans "
             "on the schedule in scans.yaml and sends desktop notifications. Nothing is ever traded.",
             ["Start the monitor in a terminal: `python scripts/monitor.py` waits for the start time; add `--now` to "
              "start immediately, or `--once` for one pass.",
              "**Last pass** should stay recent while it runs. This panel refreshes itself every 20 seconds.",
              "**Open positions** are coloured green (Hold), yellow (Watch) and red (Exit).",
              "**Scan hits** groups results by scan. Option scans list the matching contracts.",
              "Edit scans.yaml to change symbols, indicators, conditions and the schedule. The scans currently "
              "loaded are listed at the bottom of this view."],
             ["rsi", "ma", "macd", "bb", "atr", "guide"])
    monitor_panel()
    if overlay_on("monitor"):
        ov.monitor_overlay_panel(MON)
    with st.expander("Scans defined in scans.yaml"):
        try:
            from optlab.live.scanner import load_scan_config

            scfg = load_scan_config(ROOT / "scans.yaml")
            for sc in scfg["scans"]:
                wl = sc.get("watchlist") or sc.get("symbols")
                wl = ", ".join(wl) if isinstance(wl, (list, tuple)) else wl
                st.markdown(f"**{sc.get('name')}** · {sc.get('timeframe', '1d')} bars · watchlist: {wl}")
                st.code("\n".join(map(str, sc.get("stock") or [])) +
                        (("\noptions: " + str({k: v for k, v in sc["options"].items()})) if sc.get("options") else ""),
                        language="text")
        except Exception as exc:
            st.error(f"scans.yaml: {exc}")

# ------------------------------------------------------ historical signal explorer
if SHOW["explorer"]:
    from signal_explorer import render as render_signal_explorer  # noqa: E402

    if overlay_on("signal_explorer"):
        with st.expander("🧭 Overlay: historical evidence for the overlay's rules", expanded=True):
            ov.history_panel(OSCAN, OCFG, snapshot_dates())

    render_signal_explorer()

# ------------------------------------------------------------------ overlay settings and data health
if SHOW["overlay"]:
    st.subheader("Overlay: data health, rules and settings")
    ui.intro("The overlay highlights conditions across every view using fixed, documented rules. No AI, machine "
             "learning or language model is involved: the same data and settings always give the same result.",
             ["**Data health** lists every check run on the data. Failed checks either block a calculation (shown as "
              "Data error) or downgrade it (shown with a Stale data or similar flag).",
              "**Rules** lists the rules from overlay.yaml with their conditions and confirmation requirements.",
              "**Status guide** explains every status, colour and icon.",
              "**Settings** changes thresholds, the small-account profile, which tabs show an overlay, confluence "
              "weights and status labels. Saving validates everything first and writes overlay.yaml.",
              "**Audit log** lists every state change with the versions, inputs and formula that produced it.",
              "Rules themselves are edited in overlay.yaml (see docs/OVERLAY.md for the format and every formula)."],
             ["guide"])
    if OSCAN is None:
        st.info("The overlay is switched off in the sidebar.")
    else:
        t1, t2, t3, t4, t5 = st.tabs(["Data health", "Rules", "Status guide", "Settings", "Audit log"])
        with t1:
            ov.data_health_panel(OSCAN)
        with t2:
            st.caption(f"Loaded from {'overlay.yaml + ' if OVERLAY_PATH.exists() else ''}built-in defaults · config "
                       f"version {OCFG.version}")
            for r in OCFG.rules:
                with st.expander(f"{'✅' if r.enabled else '⏸️'} {r.name} · {r.direction} · {r.timeframe}"):
                    st.markdown(f"Confirmation: score ≥ {r.minimum_score if r.minimum_score is not None else OCFG.thresholds['confirmed_match_score']:g}, "
                                f"≥ {r.minimum_pass_count} passing, required conditions "
                                f"{'must pass' if r.required_must_pass else 'may be near'}. Invalidation: "
                                f"{', '.join(f'`{e}`' for e in r.invalidation) or 'none'}. Shown in: "
                                f"{', '.join(ov.PANEL_NAMES.get(t, t) for t in r.tab_scope)}.")
                    if r.timeframe not in ("1d", "1wk"):
                        st.caption("Intraday rule: evaluated by the live monitor (python scripts/monitor.py).")
                    ui.show(pd.DataFrame([{"id": c.id, "expression": c.expression, "required": c.required,
                                           "weight": c.weight, "tolerance": "None" if c.tolerance_type == "none"
                                           else f"{c.tolerance:g} ({c.tolerance_type.replace('_', ' ')})"}
                                          for c in r.conditions]), rename={"id": "ID"}, width="stretch")
        with t3:
            from optlab.overlay.status import STATUSES as OST

            ui.show(pd.DataFrame([{"status": f"{s.icon} {OCFG.labels.get(s.code, s.label)}", "colour": s.color.capitalize(),
                                   "meaning": s.meaning} for s in OST.values()]), width="stretch")
        with t4:
            from optlab.overlay.config import save_user_overrides

            ov.settings_panel(OCFG, save_user_overrides, OVERLAY_PATH)
        with t5:
            ov.audit_panel()

