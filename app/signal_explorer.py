"""Historical Signal Explorer tab for the dashboard (imported by streamlit_app.py)."""
from __future__ import annotations

import hashlib
import json
from datetime import date

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

import ui
from optlab.history.bars import TIMEFRAMES, bars_per_session, nyse_business_days
from optlab.history.explorer import chart_studies, condition_kind, explain, run_search, to_csv
from optlab.history.forward import forward_behavior
from optlab.history.library import (compare_to_saved, duplicate_strategy, list_searches, list_strategies,
                                    load_search, save_search, save_strategy)
from optlab.history.strategy import StrategyError, load_yaml, parse_strategy

TEMPLATE = """historical_signal_search:
  name: My strategy
  symbols: [SPY]
  timeframe: 1d
  range: {start: 2024-01-01, end: 2026-10-01, timezone: America/New_York, market_hours_only: true}
  conditions:
    - id: trend
      expression: close > sma(50)
      weight: 50
      tolerance: {type: percent_distance, value: 1}
    - id: momentum
      expression: rsi(14) >= 55
      required: false
      weight: 50
      tolerance: {type: absolute, value: 5}
  matching: {minimum_score: 75, include_exact: true, include_near: true}
"""
LARGE_BARS = 400_000
STATUS_COLORS = ui.STATUS_COLORS
SOURCES = {"yahoo": "Yahoo Finance", "archive": "Local archive", "synthetic": "Synthetic (offline)"}
TF_HELP = ("Bar size. 1m-30m and 1h are intraday bars (Yahoo keeps only about 30-60 days of 1m-30m history and "
           "about 2 years of 1h); 1d is one bar per trading day.")


def _horizon(h: str) -> str:
    n = int(str(h).rstrip("d"))
    return f"{n} trading day{'s' * (n != 1)}" if str(h).endswith("d") else f"{n} bar{'s' * (n != 1)}"


def _key(*parts) -> str:
    return "hse_" + hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:10]


def _overrides(body: dict, k: str) -> dict:
    """Quick controls; their values are written into the strategy before it is validated."""
    rng = body.setdefault("range", {})
    c = st.columns([3, 1, 1, 1, 1])
    syms = c[0].text_input("Symbols (comma-separated)", ", ".join(map(str, body.get("symbols") or [])), key=k + "s",
                          help="Tickers to search, e.g. SPY, AAPL, NVDA. Each one is searched separately.")
    body["symbols"] = [s.strip() for s in syms.split(",") if s.strip()]
    tf = str(body.get("timeframe", "1d"))
    body["timeframe"] = c[1].selectbox("Timeframe", TIMEFRAMES, index=TIMEFRAMES.index(tf) if tf in TIMEFRAMES else 5,
                                       key=k + "tf", help=TF_HELP)
    def _d(v, default):
        try:
            return pd.Timestamp(str(v)).date()
        except Exception:
            return default
    start = c[2].date_input("Start", _d(rng.get("start"), date(2026, 1, 1)), key=k + "a", format="YYYY-MM-DD",
                           help="First day to look for matches. Indicator warm-up data before it is loaded automatically.")
    end = c[3].date_input("End", _d(rng.get("end"), date.today()), key=k + "b", format="YYYY-MM-DD",
                         help="Last day to look for matches.")
    keep_time = lambda v, d: str(v) if v and len(str(v)) > 10 and _d(v, None) == d else d.isoformat()  # noqa: E731
    rng["start"], rng["end"] = keep_time(rng.get("start"), start), keep_time(rng.get("end"), end)
    src = (body.get("data") or {}).get("source", "yahoo")
    body.setdefault("data", {})["source"] = c[4].selectbox("Data", ["yahoo", "archive", "synthetic"],
                                                           index=["yahoo", "archive", "synthetic"].index(src)
                                                           if src in ("yahoo", "archive", "synthetic") else 0,
                                                           key=k + "src", format_func=SOURCES.get,
                                                           help="Yahoo Finance: free historical bars. Local archive: "
                                                                "bars saved by earlier searches or the monitor, "
                                                                "no internet needed. Synthetic: made-up test data.")
    m = body.setdefault("matching", {})
    c = st.columns([1, 1, 1, 2])
    rng["market_hours_only"] = c[0].toggle("Regular hours only", bool(rng.get("market_hours_only", True)), key=k + "mh",
                                          help="On: only 9:30-16:00 New York bars. Off: include pre-market and "
                                               "after-hours bars where the source has them.")
    m["include_exact"] = c[1].checkbox("Exact matches", bool(m.get("include_exact", True)), key=k + "ie",
                                       help="Bars where every condition passes.")
    m["include_near"] = c[2].checkbox("Near matches", bool(m.get("include_near", False)), key=k + "in",
                                      help="Also show bars that came close, within each condition's tolerance.")
    m["minimum_score"] = c[3].slider("Minimum score for near matches", 0, 100,
                                     int(float(m.get("minimum_score", 75 if m.get("include_near") else 100))), key=k + "ms",
                                     help="Each condition earns points by its weight; near misses earn part. A near "
                                          "match needs at least this total out of 100.")
    with st.expander("Advanced settings"):
        c = st.columns(4)
        m["required_mode"] = c[0].selectbox("Required conditions", ["strict", "tolerant"], format_func=str.capitalize,
                                            index=0 if m.get("required_mode", "strict") == "strict" else 1, key=k + "rm",
                                            help="Strict: required conditions must fully pass. Tolerant: they may be "
                                                 "near, but never failed.")
        m["minimum_pass_count"] = c[1].number_input("Minimum conditions passing", 0, 50,
                                                    int(m.get("minimum_pass_count", 0)), key=k + "mp",
                                                    help="Near matches must also have at least this many conditions "
                                                         "fully passing. 0 = no extra rule.")
        o = body.setdefault("output", {})
        o["limit"] = c[2].number_input("Result limit", 1, 1_000_000, int(o.get("limit", 1000)), key=k + "lim",
                                       help="Most matches to keep. The total found is always reported.")
        body["warmup_bars"] = c[3].number_input("Warm-up bars (0 = automatic)", 0, 100_000,
                                                int(body.get("warmup_bars", 0) or 0), key=k + "wu",
                                                help="Bars loaded before the start date so indicators like sma(200) "
                                                     "are ready on day one. 0 works it out from the conditions.")
    return body


def _preview(s) -> None:
    rows = []
    for c in s.conditions:
        kind, note = condition_kind(c.expression)
        tol = "None" if c.tolerance_type == "none" else (
            f"{c.tolerance:g}{'%' if c.tolerance_type == 'percent_distance' else ''} ({c.tolerance_type.replace('_', ' ')})")
        rows.append({"id": c.id, "condition": c.expression, "required": c.required, "weight": c.weight,
                     "tolerance": tol + (f", recent {c.recent_bars} bars" if c.recent_bars else ""),
                     "scored as": note[:1].upper() + note[1:]})
    if s.sequence:
        steps = " → ".join(f"{st_.id} [{st_.expression}]" + (f" within {st_.within_bars}" if st_.within_bars is not None else "")
                           for st_ in s.sequence)
        rows.append({"id": "sequence", "condition": steps, "required": True,
                     "weight": s.sequence_weight or "Average", "tolerance": "None",
                     "scored as": "Pass/fail only: completes in order"})
    ui.show(pd.DataFrame(rows).astype({"weight": str}), rename={"id": "ID", "scored as": "Scored as"},
            help={"id": "The condition's name, used as a column in the results.",
                  "required": "Yes = a bar can't match without it.",
                  "weight": "Share of the 100-point score this condition is worth.",
                  "tolerance": "How close counts as 'near' when the condition just misses.",
                  "scored as": "How partial credit is worked out for this kind of condition."},
            width="stretch")
    near = (f"near matches: score ≥ {s.minimum_score:g}, ≥ {s.minimum_pass_count} conditions passing, required "
            f"conditions {'must pass' if s.required_mode == 'strict' else 'may be near'}") if s.include_near else "no near matches"
    st.caption(f"{s.name} · {s.timeframe} bars · {', '.join(s.symbols)} · {s.start} to {s.end} ({s.timezone}) · "
               f"{'regular hours' if s.market_hours_only else 'all sessions'} · {s.warmup_bars:,} warm-up bars · "
               f"{'exact matches' if s.include_exact else 'no exact matches'}, {near} · version {s.version}")
    try:
        sessions = len(nyse_business_days(pd.Timestamp(s.start).date(), min(pd.Timestamp(s.end).date(), date.today())))
        est = int(len(s.symbols) * sessions * bars_per_session(s.timeframe))
        if est > LARGE_BARS:
            st.warning(f"About {est:,} bars to evaluate. That may take a while; nothing is downsampled or skipped.")
    except Exception:
        pass


def _chart(bars, i, before, after, overlays, lower, title) -> alt.Chart:
    lo, hi = max(0, i - before), min(len(bars), i + after + 1)
    w = bars.iloc[lo:hi].copy()
    w["bar"] = np.arange(lo, hi) - i
    w["time"] = w.index.strftime("%Y-%m-%d %H:%M")
    w["dir"] = np.where(w["close"] >= w["open"], "up", "down")
    dom = [w["bar"].min() - 0.5, w["bar"].max() + 0.5]

    def x(show_title=False):
        return alt.X("bar:Q", scale=alt.Scale(domain=dom, nice=False),
                     title="Bars from the signal (0 = signal bar; shaded = after the signal)" if show_title else None)

    band = alt.Chart(pd.DataFrame({"a": [0.5], "b": [dom[1]]})).mark_rect(opacity=0.08, color="gray").encode(
        x=alt.X("a:Q", scale=alt.Scale(domain=dom, nice=False), title=None), x2="b:Q")
    marker = alt.Chart(pd.DataFrame({"m": [0]})).mark_rule(color="crimson", strokeDash=[4, 3]).encode(
        x=alt.X("m:Q", scale=alt.Scale(domain=dom, nice=False), title=None))
    label = alt.Chart(pd.DataFrame({"t": ["After the signal: not known at the time"], "p": [0.7]})).mark_text(
        align="left", baseline="top", dy=4, fontSize=11, color="gray").encode(
        x=alt.X("p:Q", scale=alt.Scale(domain=dom, nice=False), title=None), y=alt.value(0), text="t:N")
    tip = [alt.Tooltip("time:N", title="Time"), alt.Tooltip("open:Q", title="Open", format=",.2f"),
           alt.Tooltip("high:Q", title="High", format=",.2f"), alt.Tooltip("low:Q", title="Low", format=",.2f"),
           alt.Tooltip("close:Q", title="Close", format=",.2f"), alt.Tooltip("volume:Q", title="Volume", format=",.0f")]
    color = alt.Color("dir:N", scale=alt.Scale(domain=["up", "down"], range=["#2e7d32", "#c62828"]), legend=None)
    base = alt.Chart(w)
    wick = base.mark_rule().encode(x=x(), y=alt.Y("low:Q", scale=alt.Scale(zero=False), title="Price ($)", axis=alt.Axis(format=",.2f")), y2="high:Q",
                                   color=color, tooltip=tip)
    body = base.mark_bar(size=max(2, min(10, 500 // max(len(w), 1)))).encode(x=x(), y="open:Q", y2="close:Q",
                                                                              color=color, tooltip=tip)
    layers = [band, wick, body]
    if overlays:
        ov = pd.DataFrame({k: v.iloc[lo:hi].to_numpy() for k, v in overlays.items()})
        ov["bar"] = w["bar"].to_numpy()
        layers.append(alt.Chart(ov.melt("bar", var_name="study", value_name="value")).mark_line(strokeWidth=1.6).encode(
            x=x(), y="value:Q", color=alt.Color("study:N", legend=alt.Legend(orient="top", title=None)),
            tooltip=[alt.Tooltip("study:N", title="Study"), alt.Tooltip("value:Q", title="Value", format=",.2f")]))
    price = alt.layer(*layers, marker, label).resolve_scale(color="independent").properties(title=title, height=300)
    charts = [price]
    for name, v in lower.items():  # one small panel per study: each keeps its own scale
        lw = pd.DataFrame({"bar": w["bar"].to_numpy(), "value": v.iloc[lo:hi].to_numpy()})
        charts.append(alt.layer(band, alt.Chart(lw).mark_line(color="#1565c0").encode(
            x=x(), y=alt.Y("value:Q", title=name, scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("value:Q", format=",.3g", title=name)]), marker).properties(height=90))
    charts.append(alt.layer(band, base.mark_bar(opacity=0.6).encode(
        x=x(show_title=True), y=alt.Y("volume:Q", title="Volume", axis=alt.Axis(format="~s")), color=color, tooltip=tip), marker).properties(height=90))
    return alt.vconcat(*charts).resolve_scale(color="independent")


def _results(res) -> None:
    s, df = res.strategy, res.results
    n_exact = int((df["match_type"] == "exact").sum()) if not df.empty else 0
    m = st.columns(5)
    m[0].metric("Matches shown", f"{len(df):,}", help="Matching bars kept (up to the result limit).")
    m[1].metric("Exact / near", f"{n_exact:,} / {len(df) - n_exact:,}",
                help="Exact = every condition passed. Near = close enough to reach the minimum score.")
    m[2].metric("Bars evaluated", f"{res.bars_evaluated:,}", help="Bars checked across all symbols in the date range.")
    m[3].metric("Not evaluable", f"{res.not_evaluable:,}",
                help="Bars where an indicator had too little history (warm-up) or data was missing. Never counted "
                     "as a match.")
    m[4].metric("Search time", f"{res.seconds:,.1f} s")
    if res.total_matches > len(df):
        st.warning(f"{res.total_matches:,} bars matched; showing the first {len(df):,} by the chosen sort "
                   "(output.limit). Raise the limit in Advanced settings to see them all.")
    reps = pd.DataFrame([{"symbol": k, "source": r.source, "first bar": r.first_bar, "last bar": r.last_bar,
                          "interval": r.timeframe, "timezone": r.timezone, "bars in range": r.bars_in_range,
                          "warm-up": f"{r.warmup_bars_available}/{r.warmup_bars_requested}",
                          "missing bars": r.missing_bars, "holidays": r.holidays_in_range,
                          "adjusted": r.adjusted, "extended hours": r.extended_hours_included}
                         for k, r in res.reports.items()])
    reps["source"] = reps["source"].map(lambda v: SOURCES.get(v, str(v).capitalize()))
    with st.expander("Data used by this search", expanded=bool(sum(len(r.warnings) for r in res.reports.values()))):
        ui.show(reps, formats={"first bar": "date", "last bar": "date", "bars in range": "int", "missing bars": "int",
                               "holidays": "int"},
                help={"warm-up": "Bars available before the start date ÷ bars needed for the indicators.",
                      "missing bars": "Expected trading bars with no data. Conditions can't be checked on them.",
                      "adjusted": "Yes = prices adjusted for splits and dividends.",
                      "extended hours": "Yes = pre-market and after-hours bars were included."},
                width="stretch")
        for k, r in res.reports.items():
            for w in r.warnings:
                st.warning(f"{k}: {w}")
    for e in res.errors:
        st.error(e)
    if df.empty:
        st.info("No bars matched in this range.")
        return

    f = st.columns([2, 1, 2, 2])
    syms = f[0].multiselect("Symbols", sorted(df["symbol"].unique()), key="hse_fs", placeholder="All symbols",
                            help="Show only these symbols. Leave empty for all.")
    types = f[1].multiselect("Type", ["exact", "near"], key="hse_ft", format_func=str.capitalize, placeholder="Both",
                             help="Exact and/or near matches. Leave empty for both.")
    minscore = f[2].slider("Score at least", 0, 100, 0, key="hse_fsc", help="Hide matches scoring below this.")
    order = f[3].selectbox("Sort", ["time, oldest first", "time, newest first", "score, highest first"], key="hse_ord",
                           format_func=str.capitalize, help="Order of the results table.")
    view = df.copy()
    if syms:
        view = view[view["symbol"].isin(syms)]
    if types:
        view = view[view["match_type"].isin(types)]
    view = view[view["score"] >= minscore]
    d0, d1 = df["timestamp"].min().date(), df["timestamp"].max().date()
    dr = st.slider("Dates", d0, d1, (d0, d1), key="hse_fd", format="YYYY-MM-DD",
                   help="Drag either end to narrow the date range.") if d0 < d1 else (d0, d1)
    view = view[(view["timestamp"].dt.date >= dr[0]) & (view["timestamp"].dt.date <= dr[1])]
    by, asc = {"time, oldest first": (["timestamp"], True), "time, newest first": (["timestamp"], False),
               "score, highest first": (["score", "timestamp"], [False, True])}[order]
    view = view.sort_values(by, ascending=asc, kind="stable").reset_index(drop=True)
    status_cols = [e.cond.id for e in next(iter(res.evals.values()))]
    shown = view.assign(timestamp=view["timestamp"].dt.strftime("%Y-%m-%d %H:%M %Z"),
                        match_type=view["match_type"].str.capitalize(),
                        required=view["required"].astype(str).str.capitalize(),
                        **{c: view[c].astype(str).str.capitalize().replace("N/a", "N/A")
                           for c in status_cols if c in view})
    names = {"timestamp": "Time", "match_type": "Match", "passed": "Passed", "near": "Near", "failed": "Failed",
             "required": "Required conditions", "score": "Score"}
    names |= {c: c for c in status_cols} | {f"{c}_value": f"{c} value" for c in status_cols} \
        | {f"{c}_score": f"{c} points" for c in status_cols}
    sel = ui.show(shown, rename=names,
                  formats={"score": "num1", **{f"{c}_value": "num" for c in status_cols},
                           **{f"{c}_score": "num1" for c in status_cols}},
                  help={"timestamp": "Close time of the matching bar, New York time.",
                        "match_type": "Exact = every condition passed. Near = close enough to reach the minimum score.",
                        "score": "Points out of 100, from each condition's weight.",
                        "passed": "Conditions that fully passed.", "near": "Conditions that just missed, within tolerance.",
                        "failed": "Conditions that missed by more than the tolerance.",
                        **{c: f"Pass, Near or Fail for condition '{c}'." for c in status_cols},
                        **{f"{c}_value": f"Value of the left-hand side of '{c}' on this bar." for c in status_cols},
                        **{f"{c}_score": f"Points '{c}' earned." for c in status_cols}},
                  styler=ui.status_styler(status_cols, STATUS_COLORS),
                  width="stretch", on_select="rerun", selection_mode="single-row", key="hse_table", height=380)
    st.caption(f"{len(view):,} of {len(df):,} matches after filters. Click a row to see why it matched and a chart "
               "around it.")
    d = st.columns(3)
    d[0].download_button("Download CSV", to_csv(view).encode(), f"{s.name}_matches.csv", "text/csv",
                         help="Every filtered match with all its columns, for Excel or Google Sheets.")
    payload = {"strategy": s.to_dict(), "version": s.version, "reports": {k: r.as_dict() for k, r in res.reports.items()},
               "results": json.loads(view.assign(timestamp=view["timestamp"].astype(str)).to_json(orient="records"))}
    d[1].download_button("Download JSON", json.dumps(payload, indent=1, default=str).encode(), f"{s.name}_matches.json",
                         "application/json",
                         help="The matches plus the exact strategy and data report, for use in other programs.")
    if d[2].button("Save this search", help="Store the strategy, data report and results so you can reopen or "
                                            "re-run them later under Saved searches."):
        sid = save_search(res, st.session_state.get("hse_res_yaml", s.to_yaml()))
        st.success(f"Saved as {sid}")

    rows = sel.selection.rows if sel and sel.selection else []
    if rows:
        r = view.iloc[rows[0]]
        bars, evals = res.bars[r["symbol"]], res.evals[r["symbol"]]
        i = bars.index.get_loc(r["timestamp"])
        st.markdown(f"**{r['symbol']} · {r['timestamp']:%Y-%m-%d %H:%M %Z} · {r['match_type'].capitalize()} match · "
                    f"score {r['score']:.1f}**")
        ex = pd.DataFrame(explain(evals, i))
        ex["status"] = ex["status"].astype(str).str.capitalize().replace("N/a", "N/A")
        ui.show(ex[["id", "status", "required", "explanation", "points", "max_points"]],
                formats={"points": "num1", "max_points": "num1"}, rename={"id": "Condition", "max_points": "Out of"},
                help={"explanation": "The value on this bar, the rule, and how close it was."},
                styler=ui.status_styler(["Status"], STATUS_COLORS), width="stretch")
        b1, b2 = st.columns(2)
        before = b1.slider("Bars before", 5, 300, 60, key="hse_bb", help="How many bars before the signal to chart.")
        after = b2.slider("Bars after", 0, 300, 20, key="hse_ba",
                          help="How many bars after the signal to chart (shaded: not known at the time).")
        overlays, lower = chart_studies(s, bars)
        st.altair_chart(_chart(bars, i, before, after, overlays, lower, f"{r['symbol']} around the signal"),
                        width="stretch")
        st.caption("Everything left of the dashed line was known when the signal bar closed. Bars to the right "
                   "happened afterwards and are shown only for retrospective inspection.")

    st.markdown("**Forward behaviour after signal**")
    st.caption("What price did after each match, measured from the signal bar's close. Not a backtest: no entries, "
               "fills, position sizes, costs or option prices are assumed, and past behaviour doesn't predict future results.")
    c = st.columns([3, 1])
    horizons = c[0].multiselect("Horizons", sorted(set(s.horizons) | {"1", "3", "5", "10", "1d", "5d"},
                                                   key=lambda h: (h.endswith("d"), int(h.rstrip("d")))),
                                default=s.horizons, key="hse_h", format_func=_horizon,
                                help="How far ahead to measure. Bars use the strategy's timeframe; trading days count "
                                     "whole sessions.")
    dd = c[1].number_input("Keep signals at least N bars apart", 0, 10_000, s.dedupe_bars, key="hse_dd",
                           help="0 keeps every signal; overlapping windows then count the same move several times.")
    if horizons:
        per, summ = forward_behavior(view, res.bars, horizons, s.timeframe, int(dd))
        if summ.empty:
            st.info("No signals to measure.")
        else:
            FWD = {"horizon": "Horizon", "group": "Matches", "signals": "Signals", "incomplete": "Incomplete",
                   "mean_return_pct": "Average return", "median_return_pct": "Median return",
                   "positive_pct": "Went up", "negative_pct": "Went down",
                   "mean_max_favorable_pct": "Average best move", "mean_max_adverse_pct": "Average worst move",
                   "median_realized_vol_pct": "Median volatility"}
            sm = summ.assign(horizon=summ["horizon"].map(_horizon), group=summ["group"].str.capitalize())
            ui.show(sm, rename=FWD,
                    formats={"signals": "int", "incomplete": "int", "mean_return_pct": "spts", "median_return_pct": "spts",
                             "positive_pct": "pts", "negative_pct": "pts", "mean_max_favorable_pct": "spts",
                             "mean_max_adverse_pct": "spts", "median_realized_vol_pct": "pts"},
                    help={"mean_return_pct": "Average close-to-close change from the signal bar to the horizon.",
                          "positive_pct": "Share of signals where the price was higher at the horizon.",
                          "negative_pct": "Share of signals where the price was lower at the horizon.",
                          "mean_max_favorable_pct": "Average highest point reached along the way.",
                          "mean_max_adverse_pct": "Average lowest point reached along the way.",
                          "median_realized_vol_pct": "Annualized volatility of the bars in the window.",
                          "incomplete": "Signals too close to the end of the data to measure."},
                    width="stretch")
            try:
                from optlab.overlay import load_overlay_config

                mins = int(load_overlay_config().thresholds["minimum_historical_samples"])
            except Exception:
                mins = 30
            small = summ[(summ["group"] == "all") & (summ["signals"] < mins)]
            if not small.empty:
                st.warning(f"🔬 Low sample size: {', '.join(f'{_horizon(h)} has {n} signals' for h, n in zip(small['horizon'], small['signals']))}"
                           f" (minimum {mins}, overlay.yaml → thresholds.minimum_historical_samples). Percentages from "
                           "so few signals can swing widely.")
            st.caption("Technical signal available. Historical options affordability cannot be verified because "
                       "timestamped option quotes are unavailable.")
            note = f"{summ.attrs.get('deduped', 0):,} signals dropped by the spacing rule. " if dd else ""
            st.caption(note + "Incomplete = the horizon runs past the last available bar; left out of the statistics.")
            with st.expander("Per-signal forward behaviour"):
                pv = per.assign(timestamp=per["timestamp"].dt.strftime("%Y-%m-%d %H:%M"),
                                match_type=per["match_type"].str.capitalize(), horizon=per["horizon"].map(_horizon))
                if "end_time" in pv:
                    pv["end_time"] = per["end_time"].dt.strftime("%Y-%m-%d %H:%M").fillna("—")
                ui.show(pv, rename={"timestamp": "Signal time", "match_type": "Match", "bars_ahead": "Bars ahead",
                                    "end_time": "Measured at", "forward_return_pct": "Return",
                                    "max_favorable_pct": "Best move", "max_adverse_pct": "Worst move",
                                    "realized_vol_pct": "Volatility"},
                        formats={"bars_ahead": "int", "forward_return_pct": "spts", "max_favorable_pct": "spts",
                                 "max_adverse_pct": "spts", "realized_vol_pct": "pts"},
                        help={"complete": "No = the horizon runs past the available data."}, width="stretch")


def render() -> None:
    st.subheader("Historical Signal Explorer")
    ui.intro("Find the past bars where a set of indicator conditions matched exactly or nearly, and see why. "
             "Historical information for research and education; not a recommendation or a trading instruction.",
             ["Pick a saved **Strategy** or start from the template. A strategy is a list of conditions such as "
              "`close > sma(50)` or `rsi(14) >= 55`, written in YAML in the box below.",
              "Use the quick controls (symbols, timeframe, dates, data source) to change the common settings "
              "without editing YAML.",
              "Turn on **Near matches** to also find bars that came close. Each condition's weight and tolerance "
              "decide how much a near miss scores.",
              "Check **How this strategy will be read**, then press **Search**.",
              "Click any result row for a condition-by-condition explanation and a chart around the bar.",
              "**Forward behaviour** shows what the price did afterwards, for research only.",
              "The user guide lists every indicator function and the full YAML format."],
             ["guide", "rsi", "ma", "macd", "bb", "atr", "stoch", "vwap"])
    lib = list_strategies()
    top = st.columns([2, 2, 1])
    choice = top[0].selectbox("Strategy", list(lib) + ["New strategy (template)"], key="hse_choice",
                              help="Strategies saved in the strategies/ folder, or a fresh template to edit.")
    yaml_key = _key("yaml", choice)
    if yaml_key not in st.session_state:
        st.session_state[yaml_key] = lib[choice].read_text() if choice in lib else TEMPLATE
    up = top[1].file_uploader("Import a strategy YAML", type=["yaml", "yml"], key="hse_up",
                              help="Load a .yaml strategy file from your computer into the editor.")
    if up is not None and st.session_state.get("hse_up_seen") != (up.name, up.size):
        st.session_state["hse_up_seen"] = (up.name, up.size)
        st.session_state[yaml_key] = up.getvalue().decode()
        st.rerun()
    text = st.text_area("Strategy (YAML)", key=yaml_key, height=340,
                        help="Conditions use the same language as scans.yaml. See the user guide for every function. "
                             "Changes apply as soon as you click outside the box.")
    try:
        raw = load_yaml(text) or {}
        body = raw.get("historical_signal_search", raw) if isinstance(raw, dict) else {}
        parse_strategy(text)
    except StrategyError as exc:
        st.error("This strategy needs fixing:\n\n" + "\n".join(f"- {p}" for p in exc.problems))
        return
    k = _key("ov", choice, text)
    body = _overrides(json.loads(json.dumps(body, default=str)), k)
    try:
        strategy = parse_strategy({"historical_signal_search": body})
    except StrategyError as exc:
        st.error("These settings need fixing:\n\n" + "\n".join(f"- {p}" for p in exc.problems))
        return
    st.markdown("**How this strategy will be read**")
    _preview(strategy)

    a = st.columns([1, 2, 1, 2, 1])
    run = a[0].button("Search", type="primary", help="Run the strategy over the chosen symbols and dates.")
    new_name = a[1].text_input("Name to save as", strategy.name, key=_key("nm", choice), label_visibility="collapsed",
                               help="Name for Save strategy.")
    if a[2].button("Save strategy", help="Save the strategy (including the quick-control settings) to the "
                                         "strategies/ folder under the name on the left."):
        try:
            path = save_strategy(strategy.to_yaml(), name=new_name, overwrite=new_name == strategy.name and choice in lib)
            st.success(f"Saved {path.name}")
        except FileExistsError as exc:
            st.error(f"{exc}; pick another name")
    dup_name = a[3].text_input("Duplicate as", f"{strategy.name} copy", key=_key("dup", choice),
                               label_visibility="collapsed", help="Name for the copy made by Duplicate.")
    if a[4].button("Duplicate", help="Copy the selected saved strategy under the name on the left.") and choice in lib:
        try:
            st.success(f"Created {duplicate_strategy(lib[choice], dup_name).name}")
        except (FileExistsError, StrategyError) as exc:
            st.error(str(exc))
    st.download_button("Download this strategy as YAML", strategy.to_yaml().encode(), f"{strategy.name}.yaml",
                       "text/yaml", help="Save the strategy file to your computer to share or back up.")

    if run:
        bar = st.progress(0.0, text="Starting...")
        res = run_search(strategy, progress=lambda i, n, sym: bar.progress(i / n, text=f"Searching {sym} ({i + 1}/{n})"))
        bar.progress(1.0, text=f"Done in {res.seconds:,.1f} s")
        st.session_state["hse_res"], st.session_state["hse_res_yaml"] = res, strategy.to_yaml()
    if "hse_res" in st.session_state:
        st.divider()
        _results(st.session_state["hse_res"])

    with st.expander("Saved searches"):
        saved = list_searches()
        if saved.empty:
            st.caption("None yet. Run a search and press **Save this search**.")
        else:
            ui.show(saved, formats={"created_at": "date", "results": "int"}, rename={"search_id": "Search ID",
                                                                                      "created_at": "Saved",
                                                                                      "strategy_name": "Strategy",
                                                                                      "strategy_version": "Version",
                                                                                      "results": "Matches"},
                    width="stretch")
            sid = st.selectbox("Open a saved search", saved["search_id"], key="hse_sid",
                               help="Pick a saved search to re-run or download.")
            yml, meta, _ = load_search(sid)
            st.caption(f"Version {meta['strategy_version']} · {meta['total_matches']:,} matches · {meta['bars_evaluated']:,} "
                       f"bars evaluated · source: {SOURCES.get(meta['source'], meta['source'])}")
            c = st.columns(2)
            if c[0].button("Re-run it and compare", help="Run it again on today's data and report whether the "
                                                         "strategy or the underlying bars have changed."):
                res = run_search(parse_strategy(yml))
                notes = compare_to_saved(res, meta)
                st.session_state["hse_res"], st.session_state["hse_res_yaml"] = res, yml
                (st.warning("\n\n".join(notes)) if notes else
                 st.success("Same strategy version and identical bars: the results are directly comparable."))
            c[1].download_button("Download its strategy YAML", yml.encode(), f"{sid}.yaml", "text/yaml",
                                 help="The exact strategy used for that saved search.")
