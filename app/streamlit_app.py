"""optlab dashboard.   Run:  streamlit run app/streamlit_app.py"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import altair as alt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import polars as pl  # noqa: E402
import streamlit as st  # noqa: E402

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

st.set_page_config(page_title="optlab", page_icon="📈", layout="wide")
CFG = load_config()
DEF = CFG["strategy_defaults"]


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
    st.caption(f"Data source: **{src_label}**" + (" — illustrative numbers, not real markets" if src_label == "synthetic" else ""))
    symbol = st.selectbox("Underlying", symbols, index=symbols.index("SPY") if "SPY" in symbols else 0)
    st.divider()
    equity = st.number_input("Account equity ($)", value=float(CFG["account"]["equity"]), step=5000.0)
    limits = RiskLimits(**{**CFG["account"], "equity": equity})
    st.caption("Risk limits come from config.yaml → account.")
    rate_src = CFG["market"].get("rate_series", "treasury_3m") if not load_macro().is_empty() else "config fallback"
    st.caption(f"Risk-free rate: **{RATE:.2%}** ({rate_src})")

spot_series = close_series(symbol)
spot = float(spot_series.iloc[-1])
chain = load_chain(symbol)
iv_atm = atm_iv(chain) or float(np.log(spot_series).diff().tail(20).std() * np.sqrt(252) * 1.1)

earn_date = next_earnings(symbol)
tabs = st.tabs(["Scanner", "Chain & pricer", "Strategy lab", "Backtest", "Risk & sizing", "Insiders",
                "Company", "Macro"])

# ------------------------------------------------------------------- scanner
with tabs[0]:
    st.subheader("Where is premium rich?")
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
    whole = [c for c in ranked.columns if c == "days_to_earnings" or c.startswith("n_")]
    st.dataframe(
        ranked.style.format({c: "{:.0f}" if c in whole else "{:.2f}" for c in ranked.select_dtypes("number").columns},
                            na_rep="—")
        .background_gradient(subset=["score"], cmap="RdYlGn"),
        width="stretch", hide_index=True)
    st.caption("score = IV richness vs realized vol + vol already calming − vol-of-vol + insider buying. "
               "Higher = better candidate for selling options. "
               + ("Qlib model predictions are merged in." if qlib_file.exists()
                  else "Train the Qlib model (integrations/qlib) to add ML predictions here.")
               + " days_to_earnings inside your DTE means the trade holds through an earnings gap; "
               "n_*_flags count red-flag 8-Ks and deteriorating financials (see the Company tab).")

    c1, c2 = st.columns(2)
    hist = feats.filter(pl.col("symbol") == symbol).select(["date", "close", "rv20", "rv60"]).drop_nulls().to_pandas()
    c1.altair_chart(alt.Chart(hist).mark_line().encode(x="date:T", y=alt.Y("close:Q", scale=alt.Scale(zero=False)))
                    .properties(title=f"{symbol} price", height=260), width="stretch")
    vol_long = hist.melt(id_vars="date", value_vars=["rv20", "rv60"], var_name="window", value_name="vol")
    vol_chart = alt.Chart(vol_long).mark_line().encode(x="date:T", y=alt.Y("vol:Q", axis=alt.Axis(format="%")),
                                                       color="window:N")
    rule = alt.Chart(pd.DataFrame({"iv": [iv_atm]})).mark_rule(strokeDash=[4, 4], color="crimson").encode(y="iv:Q")
    c2.altair_chart((vol_chart + rule).properties(title=f"Realized vol vs today's 30d ATM IV ({iv_atm:.0%}, dashed)",
                                                   height=260), width="stretch")

# ------------------------------------------------------------ chain & pricer
with tabs[1]:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spot", f"{spot:,.2f}")
    c2.metric("30d ATM IV", f"{iv_atm:.1%}")
    sk = skew_25d(chain)
    c3.metric("Skew (90% put − 110% call IV)", f"{sk:.1%}" if sk is not None else "—")
    c4.metric("Chain snapshot", str(chain["snapshot_date"][0]) if not chain.is_empty() else "none")

    if not chain.is_empty():
        ch = with_dte(chain).to_pandas()
        exps = sorted(ch["expiration"].unique())
        exp = st.select_slider("Expiration", options=exps, value=exps[min(3, len(exps) - 1)])
        sub = ch[(ch["expiration"] == exp) & ch["moneyness"].between(0.75, 1.25)]
        smile = alt.Chart(sub).mark_line(point=True).encode(
            x=alt.X("strike:Q", scale=alt.Scale(zero=False)), y=alt.Y("implied_vol:Q", axis=alt.Axis(format="%")),
            color="option_type:N", tooltip=["strike", "option_type", "bid", "ask", "implied_vol", "open_interest"])
        st.altair_chart(smile.properties(title=f"Volatility smile, {exp}", height=280), width="stretch")
        surf = ch[ch["moneyness"].between(0.8, 1.2) & (ch["option_type"] == "put")]
        heat = alt.Chart(surf).mark_rect().encode(
            x=alt.X("strike:O", axis=alt.Axis(labelOverlap=True)), y="dte:O",
            color=alt.Color("implied_vol:Q", scale=alt.Scale(scheme="viridis"), legend=alt.Legend(format="%")))
        st.altair_chart(heat.properties(title="Put IV surface (DTE × strike)", height=260), width="stretch")
        with st.expander("Raw quotes"):
            st.dataframe(sub.drop(columns=["snapshot_date", "symbol"]), width="stretch", hide_index=True)

    st.subheader("QuantLib pricer")
    p1, p2, p3, p4, p5, p6 = st.columns(6)
    k = p1.number_input("Strike", value=float(round(spot)), step=1.0)
    days = p2.number_input("Days to expiry", value=30, min_value=1)
    vol = p3.number_input("Vol", value=round(iv_atm, 3), step=0.01, format="%.3f")
    typ = p4.selectbox("Type", ["put", "call"])
    style = p5.selectbox("Exercise", ["american", "european"])
    mkt = p6.number_input("Market price (for IV)", value=0.0, step=0.05)
    fq = load_fundamentals()
    fq = fq.filter(pl.col("symbol") == symbol) if not fq.is_empty() else fq
    div_q = float(fq["dividend_yield"][0]) if not fq.is_empty() and fq["dividend_yield"][0] is not None \
        else CFG["market"]["dividend_yield"]
    spec = OptionSpec(spot, k, days, vol, typ, RATE, div_q, style)
    st.caption(f"Rate {RATE:.2%} · dividend yield {div_q:.2%}" + (" (from fundamentals)" if not fq.is_empty() else ""))
    g = price(spec)
    m = st.columns(6)
    for col, (name, val, fmt) in zip(m, [("Fair value", g["price"], "{:.2f}"), ("Delta", g["delta"], "{:+.3f}"),
                                         ("Gamma", g["gamma"], "{:.4f}"), ("Vega /1pt", g["vega"], "{:.3f}"),
                                         ("Theta /day", g["theta"], "{:+.3f}"), ("Rho /1%", g["rho"], "{:+.3f}")]):
        col.metric(name, fmt.format(val))
    if mkt > 0:
        iv = implied_vol(mkt, spec)
        st.info(f"Implied volatility at {mkt:.2f}: **{iv:.2%}**" if np.isfinite(iv) else "No IV reproduces that price.")

# -------------------------------------------------------------- strategy lab
with tabs[2]:
    c1, c2, c3, c4 = st.columns(4)
    strat = c1.selectbox("Strategy", list(STRATEGIES), index=2)
    dte = c2.slider("DTE", 7, 120, DEF["dte"])
    delta = c3.slider("Short delta", 0.05, 0.50, DEF["short_delta"], 0.01)
    width = c4.slider("Wing width (% of spot)", 1.0, 15.0, DEF["spread_width_pct"], 0.5)
    legs = STRATEGIES[strat](spot, iv_atm, dte=dte, short_delta=delta, spread_width_pct=width, rate=RATE)
    if earn_date is not None and (earn_date - pd.Timestamp.today().date()).days <= dte:
        st.warning(f"{symbol} reports earnings on {earn_date}, before this {dte}-day expiry. Expect a gap and "
                   "an IV crush; size for the move or pick an expiry before the report.")
    fill_entry_prices(legs, spot, RATE, style="european")
    cost = entry_cost(legs)
    greeks = price_position(legs, spot, RATE, style="european")
    worst = max_loss(legs)

    m = st.columns(6)
    m[0].metric("Net credit" if cost < 0 else "Net debit", f"${abs(cost):,.0f}")
    m[1].metric("Max loss (expiry)", f"${worst:,.0f}" if worst < 1e6 else "unlimited")
    m[2].metric("$ delta", f"{greeks['dollar_delta']:+,.0f}")
    m[3].metric("Gamma (shares/$)", f"{greeks['gamma']:+.1f}")
    m[4].metric("Vega ($/vol pt)", f"{greeks['vega']:+,.0f}")
    m[5].metric("Theta ($/day)", f"{greeks['theta']:+,.0f}")
    st.dataframe(pd.DataFrame([{"type": l.option_type, "strike": l.strike, "qty": l.quantity,
                                "price": round(l.entry_price, 2)} for l in legs]), hide_index=True)

    grid_x = np.linspace(spot * 0.7, spot * 1.3, 241)
    pay = pd.DataFrame({"spot": grid_x, "P&L at expiry": payoff_at_expiry(legs, grid_x)})
    mid = []
    for x in grid_x[::6]:
        mid.append(price_position(legs, x, RATE, style="european", days_forward=dte / 2)["value"] - cost)
    pay_mid = pd.DataFrame({"spot": grid_x[::6], f"P&L at {dte // 2} DTE": mid})
    lines = (alt.Chart(pay.melt("spot")).mark_line() + alt.Chart(pay_mid.melt("spot")).mark_line(strokeDash=[5, 3])
             ).encode(x=alt.X("spot:Q", scale=alt.Scale(zero=False)), y=alt.Y("value:Q", title="P&L ($)"),
                      color="variable:N")
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="gray").encode(y="y:Q")
    now = alt.Chart(pd.DataFrame({"x": [spot]})).mark_rule(strokeDash=[2, 2], color="gray").encode(x="x:Q")
    st.altair_chart((lines + zero + now).properties(title="Payoff", height=320), width="stretch")

    days_fwd = st.slider("Scenario horizon (days from now)", 0, dte, min(7, dte))
    sg = scenario_grid(legs, spot, RATE, days_forward=days_fwd)
    sdf = pd.DataFrame(sg["pnl"], index=[f"{v:+.0%}" for v in sg["vol_shifts"]],
                       columns=[f"{m:+.0%}" for m in sg["spot_moves"]])
    st.markdown("**Scenario P&L ($)** — rows: change in implied vol, columns: move in the underlying")
    st.dataframe(sdf.style.format("{:,.0f}").background_gradient(cmap="RdYlGn", axis=None), width="stretch")

# ------------------------------------------------------------------ backtest
with tabs[3]:
    st.subheader("Quick backtest (vectorbt)")
    st.caption("Options are repriced from the underlying's history (IV = realized × premium). Use it to screen "
               "ideas; re-test winners in LEAN with real quotes, spreads and assignment.")
    c1, c2, c3, c4, c5 = st.columns(5)
    bstrat = c1.selectbox("Strategy", ["put_write", "covered_call", "put_spread", "iron_condor"])
    bdte = c2.slider("DTE ", 7, 90, DEF["dte"])
    bdelta = c3.slider("Delta ", 0.05, 0.50, DEF["short_delta"], 0.01)
    pt = c4.slider("Profit take %", 0, 90, int(DEF["profit_take_pct"]), 5)
    prem = c5.slider("IV / RV premium", 0.8, 1.6, 1.15, 0.05)
    start = st.date_input("Start", value=spot_series.index[0].date(),
                          min_value=spot_series.index[0].date(), max_value=spot_series.index[-1].date())
    px = spot_series[spot_series.index >= pd.Timestamp(start)]
    base = BTParams(strategy=bstrat, dte=bdte, short_delta=bdelta, profit_take_pct=pt, iv_premium=prem,
                    width_pct=DEF["spread_width_pct"], slippage_pct=DEF["slippage_pct_of_premium"],
                    commission=DEF["commission_per_contract"], rate=RATE)
    rets, log = simulate(px, base)
    bh = buy_and_hold(px)
    comp = pd.concat([rets, bh], axis=1).fillna(0)
    st.dataframe(stats(comp).T.style.format("{:.2f}"), width="stretch")
    eq = (1 + comp).cumprod().rename_axis("index").reset_index().melt("index", var_name="series", value_name="growth of $1")
    st.altair_chart(alt.Chart(eq).mark_line().encode(x=alt.X("index:T", title=None), y="growth of $1:Q",
                                                     color="series:N").properties(height=300), width="stretch")
    if not log.empty:
        w = (log["pnl"] > 0).mean()
        st.markdown(f"**{len(log)} trades** · win rate {w:.0%} · avg win {log.loc[log.pnl > 0, 'return_pct'].mean():.2f}% "
                    f"· avg loss {log.loc[log.pnl <= 0, 'return_pct'].mean():.2f}% · exits: "
                    + ", ".join(f"{k} {v}" for k, v in log["reason"].value_counts().items()))
        with st.expander("Trade log"):
            st.dataframe(log, width="stretch", hide_index=True)

    if st.button("Run DTE × delta sweep"):
        with st.spinner("Sweeping..."):
            _, st_grid = sweep(px, base)
        metric = st.radio("Metric", ["sharpe", "annual_return_%", "max_drawdown_%", "calmar"], horizontal=True)
        hm = st_grid[metric].reset_index()
        st.altair_chart(alt.Chart(hm).mark_rect().encode(
            x="delta:O", y="dte:O", color=alt.Color(f"{metric}:Q", scale=alt.Scale(scheme="redyellowgreen")),
            tooltip=["dte", "delta", alt.Tooltip(f"{metric}:Q", format=".2f")]
        ).properties(height=260), width="stretch")
        st.session_state["sweep"] = st_grid

# -------------------------------------------------------------- risk & sizing
with tabs[4]:
    st.subheader("Size this trade")
    c1, c2, c3 = st.columns(3)
    rs = c1.selectbox("Strategy ", list(STRATEGIES), index=2)
    rdte = c2.slider("DTE  ", 7, 120, DEF["dte"])
    rdelta = c3.slider("Short delta  ", 0.05, 0.50, DEF["short_delta"], 0.01)
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
    m[0].metric("Contracts", sz["contracts"])
    m[1].metric("Limited by", sz["limited_by"])
    m[2].metric("Total max loss", f"${sz['total_max_loss']:,.0f}")
    m[3].metric("Capital used", f"${sz['total_capital']:,.0f}")

    st.subheader("Book check")
    st.caption("Edit your open positions; Greeks are recomputed and checked against limits.")
    default_book = pd.DataFrame([
        {"symbol": symbol, "type": "put", "strike": round(spot * 0.95), "dte": 30, "qty": -2, "vol": round(iv_atm, 3)},
        {"symbol": symbol, "type": "put", "strike": round(spot * 0.90), "dte": 30, "qty": 2, "vol": round(iv_atm, 3)},
    ])
    book = st.data_editor(st.session_state.get("book", default_book), num_rows="dynamic", width="stretch")
    rows = []
    from optlab.pricing.engine import Leg
    clean = book.dropna()
    for sym_, grp in clean.groupby("symbol"):
        s_spot = float(close_series(sym_).iloc[-1]) if sym_ in symbols else spot
        lg = [Leg(r["type"], float(r["strike"]), float(r["dte"]), int(r["qty"]), float(r["vol"]))
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
        m[0].metric("$ delta", f"{chk.metrics['dollar_delta']:+,.0f}", f"{chk.metrics['delta_pct']:.0f}% of equity", delta_color="off")
        m[1].metric("Vega $/pt", f"{chk.metrics['vega']:+,.0f}", f"{chk.metrics['vega_pct']:.2f}% of equity", delta_color="off")
        m[2].metric("Theta $/day", f"{pos['theta'].sum():+,.0f}")
        m[3].metric("Largest underlying", f"{chk.metrics['largest_underlying']}", f"{chk.metrics['largest_underlying_pct']:.0f}%", delta_color="off")
        if chk.ok:
            st.success("Within all limits.")
        for b in chk.breaches:
            st.error(b)

    st.subheader("Allocate across strategies (skfolio)")
    method = st.selectbox("Method", ["min_cvar", "risk_parity", "hrp", "max_sharpe"])
    streams = {}
    for s in symbols[:6]:
        for strat_name in ("put_write", "put_spread", "iron_condor"):
            streams[f"{s} {strat_name}"] = simulate(close_series(s), BTParams(strategy=strat_name, rate=RATE))[0]
    R = pd.DataFrame(streams).fillna(0.0)
    R = R[R.index >= R.index[-1] - pd.Timedelta(days=365 * 4)]
    try:
        wts = allocate(R, method=method, max_weight=0.25)
        rep = portfolio_report(R, wts)
        c1, c2 = st.columns([1, 2])
        c1.dataframe(wts[wts > 0.001].sort_values(ascending=False).map("{:.1%}".format), width="stretch")
        m = c2.columns(4)
        m[0].metric("Annual return", f"{rep['annual_return_%']:.1f}%")
        m[1].metric("Annual vol", f"{rep['annual_vol_%']:.1f}%")
        m[2].metric("Max drawdown", f"{rep['max_drawdown_%']:.1f}%")
        m[3].metric("95% CVaR (daily)", f"{rep['cvar95_daily_%']:.2f}%")
        c2.line_chart(rep["equity_curve"], height=220)
        st.caption("Weights are fitted and evaluated on the same 4 years, so these stats are in-sample and optimistic.")
    except Exception as exc:
        st.warning(f"Optimizer failed: {exc}")

# ------------------------------------------------------------------ insiders
with tabs[5]:
    st.subheader("Insider activity (SEC Form 4)")
    trades = load_insiders()
    if trades.is_empty():
        st.info("No insider data. Set edgar.identity in config.yaml and run `python scripts/ingest.py --source openbb`.")
    else:
        sig = insider_signal(trades)
        st.dataframe(sig.to_pandas().style.format({"buy_value": "${:,.0f}", "sell_value": "${:,.0f}", "score": "{:+.2f}"})
                     .background_gradient(subset=["score"], cmap="RdYlGn", vmin=-1, vmax=1),
                     width="stretch", hide_index=True)
        st.caption("Open-market purchases (code P) weigh 5× sales; several distinct buyers boost the score. "
                   "Positive = insiders buying → favors selling puts over calls.")
        st.dataframe(trades.filter(pl.col("symbol") == symbol).to_pandas(), width="stretch", hide_index=True)
        if src_label == "synthetic":
            st.caption("Synthetic insider records for demonstration.")

# ------------------------------------------------------------------- company
with tabs[6]:
    st.subheader(f"{symbol}: fundamentals, financials and filings")
    fund = load_fundamentals()
    frow = fund.filter(pl.col("symbol") == symbol) if not fund.is_empty() else fund
    if frow.is_empty():
        st.info("No fundamentals for this symbol (ETFs have none). For stocks, run `python scripts/ingest.py`.")
    else:
        f = frow.row(0, named=True)
        fmt = lambda v, spec: "—" if v is None else spec.format(v)  # noqa: E731
        m = st.columns(5)
        m[0].metric("Market cap", fmt(f["market_cap"] and f["market_cap"] / 1e9, "${:,.0f}B"))
        m[1].metric("P/E (fwd)", f"{fmt(f['pe_ratio'], '{:.1f}')} ({fmt(f['forward_pe'], '{:.1f}')})")
        m[2].metric("Dividend yield", fmt(f["dividend_yield"], "{:.2%}"))
        m[3].metric("Beta", fmt(f["beta"], "{:.2f}"))
        m[4].metric("Next earnings", str(f["next_earnings"] or "—"),
                    f"in {(f['next_earnings'] - pd.Timestamp.today().date()).days} days" if f["next_earnings"] else None,
                    delta_color="off")
        st.caption(f"Profit margin {fmt(f['profit_margin'], '{:.1%}')} · revenue growth "
                   f"{fmt(f['revenue_growth'], '{:+.1%}')} · debt/equity {fmt(f['debt_to_equity'], '{:.0f}')} "
                   f"· as of {f['as_of']} (OpenBB / yfinance)")

    st.markdown("**Financial changes (SEC XBRL, latest quarter vs a year earlier)**")
    fin = load_financials()
    fin_sym = fin.filter(pl.col("symbol") == symbol) if not fin.is_empty() else fin
    if fin_sym.is_empty():
        st.info("No financials. Set edgar.identity in config.yaml and run `python scripts/ingest.py --source openbb`.")
    else:
        ch = financial_changes(fin_sym).with_columns(pl.col("period_end").cast(pl.Utf8)).to_pandas()
        st.dataframe(ch.drop(columns="symbol").style.format({"value": "{:,.2f}", "year_ago": "{:,.2f}",
                                                              "yoy_pct": "{:+.1f}%"}, na_rep="—")
                     .background_gradient(subset=["yoy_pct"], cmap="RdYlGn", vmin=-50, vmax=50),
                     width="stretch", hide_index=True)
        trend = (fin_sym.filter(pl.col("metric").is_in(["revenue", "net_income"]))
                 .with_columns(pl.col("period_end").cast(pl.Utf8)).to_pandas())
        st.altair_chart(alt.Chart(trend).mark_bar().encode(
            x=alt.X("period_end:O", title="quarter ending"), y=alt.Y("value:Q", title="USD", axis=alt.Axis(format="~s")),
            color="metric:N", xOffset="metric:N", tooltip=["metric", "period_end", alt.Tooltip("value:Q", format=",.0f")]
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
        st.dataframe(out.fillna(""), width="stretch", hide_index=True)
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
        st.dataframe(fil_sym.drop("symbol").with_columns(pl.col("filing_date").cast(pl.Utf8)).to_pandas(),
                     width="stretch", hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("link", display_text="open")})
    if src_label == "synthetic":
        st.caption("Synthetic fundamentals, financials and filings for demonstration.")

# --------------------------------------------------------------------- macro
with tabs[7]:
    st.subheader("Rates and market volatility")
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
            col.metric(f"{lab} Treasury", f"{latest[k]:.2%}" if k in latest else "—")
        m[5].metric("VIX", f"{latest['vix']:.1f}" if "vix" in latest else "—")
        if {"treasury_2y", "treasury_10y"} <= set(latest.index):
            slope = latest["treasury_10y"] - latest["treasury_2y"]
            st.caption(f"10Y − 2Y slope: {slope * 1e4:+.0f} bp" + (" (inverted)" if slope < 0 else "")
                       + f" · pricing uses {CFG['market'].get('rate_series', 'treasury_3m')} = {RATE:.2%}")
        c1, c2 = st.columns(2)
        curve = pd.DataFrame({"tenor": [lab for k, lab in tenors if k in latest],
                              "yield": [latest[k] for k, _ in tenors if k in latest]})
        c1.altair_chart(alt.Chart(curve).mark_line(point=True).encode(
            x=alt.X("tenor:N", sort=[lab for _, lab in tenors]), y=alt.Y("yield:Q", axis=alt.Axis(format="%"),
                                                                       scale=alt.Scale(zero=False))
        ).properties(title="Treasury curve today", height=280), width="stretch")
        rates = md[md["series"].isin(["treasury_3m", "treasury_2y", "treasury_10y"])]
        c2.altair_chart(alt.Chart(rates).mark_line().encode(
            x=alt.X("date:T", title=None), y=alt.Y("value:Q", title="yield", axis=alt.Axis(format="%")),
            color="series:N").properties(title="Treasury yields", height=280), width="stretch")
        vix = md[md["series"] == "vix"]
        st.altair_chart(alt.Chart(vix).mark_line().encode(x=alt.X("date:T", title=None), y=alt.Y("value:Q", title="VIX"))
                        .properties(title="VIX: market-wide implied volatility. High = rich premium, but bigger moves",
                                    height=240), width="stretch")
        if src_label == "synthetic":
            st.caption("Synthetic rates and VIX for demonstration.")
