"""Historical Signal Explorer tab for the dashboard (imported by streamlit_app.py)."""
from __future__ import annotations

import hashlib
import json
from datetime import date

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
import yaml

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
STATUS_COLORS = {"pass": "#d1e7dd", "near": "#fff3cd", "fail": "#f8d7da", "n/a": "#e2e3e5"}


def _key(*parts) -> str:
    return "hse_" + hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:10]


def _overrides(body: dict, k: str) -> dict:
    """Quick controls; their values are written into the strategy before it is validated."""
    rng = body.setdefault("range", {})
    c = st.columns([3, 1, 1, 1, 1])
    syms = c[0].text_input("Symbols (comma-separated)", ", ".join(map(str, body.get("symbols") or [])), key=k + "s")
    body["symbols"] = [s.strip() for s in syms.split(",") if s.strip()]
    tf = str(body.get("timeframe", "1d"))
    body["timeframe"] = c[1].selectbox("Timeframe", TIMEFRAMES, index=TIMEFRAMES.index(tf) if tf in TIMEFRAMES else 5,
                                       key=k + "tf")
    def _d(v, default):
        try:
            return pd.Timestamp(str(v)).date()
        except Exception:
            return default
    start = c[2].date_input("Start", _d(rng.get("start"), date(2026, 1, 1)), key=k + "a")
    end = c[3].date_input("End", _d(rng.get("end"), date.today()), key=k + "b")
    keep_time = lambda v, d: str(v) if v and len(str(v)) > 10 and _d(v, None) == d else d.isoformat()  # noqa: E731
    rng["start"], rng["end"] = keep_time(rng.get("start"), start), keep_time(rng.get("end"), end)
    src = (body.get("data") or {}).get("source", "yahoo")
    body.setdefault("data", {})["source"] = c[4].selectbox("Data", ["yahoo", "archive", "synthetic"],
                                                           index=["yahoo", "archive", "synthetic"].index(src)
                                                           if src in ("yahoo", "archive", "synthetic") else 0,
                                                           key=k + "src")
    m = body.setdefault("matching", {})
    c = st.columns([1, 1, 1, 2])
    rng["market_hours_only"] = c[0].toggle("Regular hours only", bool(rng.get("market_hours_only", True)), key=k + "mh")
    m["include_exact"] = c[1].checkbox("Exact matches", bool(m.get("include_exact", True)), key=k + "ie")
    m["include_near"] = c[2].checkbox("Near matches", bool(m.get("include_near", False)), key=k + "in")
    m["minimum_score"] = c[3].slider("Minimum score for near matches", 0, 100,
                                     int(float(m.get("minimum_score", 75 if m.get("include_near") else 100))), key=k + "ms")
    with st.expander("Advanced settings"):
        c = st.columns(4)
        m["required_mode"] = c[0].selectbox("Required conditions", ["strict", "tolerant"],
                                            index=0 if m.get("required_mode", "strict") == "strict" else 1, key=k + "rm",
                                            help="strict: must pass. tolerant: may be near, not failed.")
        m["minimum_pass_count"] = c[1].number_input("Minimum conditions passing", 0, 50,
                                                    int(m.get("minimum_pass_count", 0)), key=k + "mp")
        o = body.setdefault("output", {})
        o["limit"] = c[2].number_input("Result limit", 1, 1_000_000, int(o.get("limit", 1000)), key=k + "lim")
        body["warmup_bars"] = c[3].number_input("Warm-up bars (0 = automatic)", 0, 100_000,
                                                int(body.get("warmup_bars", 0) or 0), key=k + "wu")
    return body


def _preview(s) -> None:
    rows = []
    for c in s.conditions:
        kind, note = condition_kind(c.expression)
        tol = "none" if c.tolerance_type == "none" else f"{c.tolerance:g}{'%' if c.tolerance_type == 'percent_distance' else ''} ({c.tolerance_type})"
        rows.append({"id": c.id, "condition": c.expression, "required": c.required, "weight": c.weight,
                     "tolerance": tol + (f", recent {c.recent_bars} bars" if c.recent_bars else ""),
                     "scored as": note})
    if s.sequence:
        steps = " → ".join(f"{st_.id} [{st_.expression}]" + (f" within {st_.within_bars}" if st_.within_bars is not None else "")
                           for st_ in s.sequence)
        rows.append({"id": "sequence", "condition": steps, "required": True,
                     "weight": s.sequence_weight or "average", "tolerance": "none",
                     "scored as": "pass/fail only: completes in order"})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    near = (f"near matches: score ≥ {s.minimum_score:g}, ≥ {s.minimum_pass_count} conditions passing, required "
            f"conditions {'must pass' if s.required_mode == 'strict' else 'may be near'}") if s.include_near else "no near matches"
    st.caption(f"{s.name} · {s.timeframe} bars · {', '.join(s.symbols)} · {s.start} to {s.end} ({s.timezone}) · "
               f"{'regular hours' if s.market_hours_only else 'all sessions'} · {s.warmup_bars} warm-up bars · "
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
                     title="bars from the signal (0 = signal bar; shaded = after the signal)" if show_title else None)

    band = alt.Chart(pd.DataFrame({"a": [0.5], "b": [dom[1]]})).mark_rect(opacity=0.08, color="gray").encode(
        x=alt.X("a:Q", scale=alt.Scale(domain=dom, nice=False), title=None), x2="b:Q")
    marker = alt.Chart(pd.DataFrame({"m": [0]})).mark_rule(color="crimson", strokeDash=[4, 3]).encode(
        x=alt.X("m:Q", scale=alt.Scale(domain=dom, nice=False), title=None))
    label = alt.Chart(pd.DataFrame({"t": ["after the signal: not known at the time"], "p": [0.7]})).mark_text(
        align="left", baseline="top", dy=4, fontSize=11, color="gray").encode(
        x=alt.X("p:Q", scale=alt.Scale(domain=dom, nice=False), title=None), y=alt.value(0), text="t:N")
    tip = ["time", alt.Tooltip("open:Q", format=",.2f"), alt.Tooltip("high:Q", format=",.2f"),
           alt.Tooltip("low:Q", format=",.2f"), alt.Tooltip("close:Q", format=",.2f"), alt.Tooltip("volume:Q", format=",.0f")]
    color = alt.Color("dir:N", scale=alt.Scale(domain=["up", "down"], range=["#2e7d32", "#c62828"]), legend=None)
    base = alt.Chart(w)
    wick = base.mark_rule().encode(x=x(), y=alt.Y("low:Q", scale=alt.Scale(zero=False), title="price"), y2="high:Q",
                                   color=color, tooltip=tip)
    body = base.mark_bar(size=max(2, min(10, 500 // max(len(w), 1)))).encode(x=x(), y="open:Q", y2="close:Q",
                                                                              color=color, tooltip=tip)
    layers = [band, wick, body]
    if overlays:
        ov = pd.DataFrame({k: v.iloc[lo:hi].to_numpy() for k, v in overlays.items()})
        ov["bar"] = w["bar"].to_numpy()
        layers.append(alt.Chart(ov.melt("bar", var_name="study", value_name="value")).mark_line(strokeWidth=1.6).encode(
            x=x(), y="value:Q", color=alt.Color("study:N", legend=alt.Legend(orient="top", title=None)),
            tooltip=["study:N", alt.Tooltip("value:Q", format=",.2f")]))
    price = alt.layer(*layers, marker, label).resolve_scale(color="independent").properties(title=title, height=300)
    charts = [price]
    for name, v in lower.items():  # one small panel per study: each keeps its own scale
        lw = pd.DataFrame({"bar": w["bar"].to_numpy(), "value": v.iloc[lo:hi].to_numpy()})
        charts.append(alt.layer(band, alt.Chart(lw).mark_line(color="#1565c0").encode(
            x=x(), y=alt.Y("value:Q", title=name, scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("value:Q", format=",.3g", title=name)]), marker).properties(height=90))
    charts.append(alt.layer(band, base.mark_bar(opacity=0.6).encode(
        x=x(show_title=True), y=alt.Y("volume:Q", title="volume"), color=color, tooltip=tip), marker).properties(height=90))
    return alt.vconcat(*charts).resolve_scale(color="independent")


def _results(res) -> None:
    s, df = res.strategy, res.results
    n_exact = int((df["match_type"] == "exact").sum()) if not df.empty else 0
    m = st.columns(5)
    m[0].metric("Matches shown", f"{len(df):,}")
    m[1].metric("Exact / near", f"{n_exact:,} / {len(df) - n_exact:,}")
    m[2].metric("Bars evaluated", f"{res.bars_evaluated:,}")
    m[3].metric("Not evaluable", f"{res.not_evaluable:,}", help="Warm-up or missing data: never counted as a match")
    m[4].metric("Search time", f"{res.seconds:.1f}s")
    if res.total_matches > len(df):
        st.warning(f"{res.total_matches:,} bars matched; showing the first {len(df):,} by the chosen sort "
                   "(output.limit). Raise the limit in Advanced settings to see them all.")
    reps = pd.DataFrame([{"symbol": k, "source": r.source, "first bar": r.first_bar, "last bar": r.last_bar,
                          "interval": r.timeframe, "timezone": r.timezone, "bars in range": r.bars_in_range,
                          "warm-up": f"{r.warmup_bars_available}/{r.warmup_bars_requested}",
                          "missing bars": r.missing_bars, "holidays": r.holidays_in_range,
                          "adjusted": r.adjusted, "extended hours": r.extended_hours_included}
                         for k, r in res.reports.items()])
    with st.expander("Data used by this search", expanded=bool(sum(len(r.warnings) for r in res.reports.values()))):
        st.dataframe(reps, width="stretch", hide_index=True)
        for k, r in res.reports.items():
            for w in r.warnings:
                st.warning(f"{k}: {w}")
    for e in res.errors:
        st.error(e)
    if df.empty:
        st.info("No bars matched in this range.")
        return

    f = st.columns([2, 1, 2, 2])
    syms = f[0].multiselect("Symbols", sorted(df["symbol"].unique()), key="hse_fs")
    types = f[1].multiselect("Type", ["exact", "near"], key="hse_ft")
    minscore = f[2].slider("Score at least", 0, 100, 0, key="hse_fsc")
    order = f[3].selectbox("Sort", ["time, oldest first", "time, newest first", "score, highest first"], key="hse_ord")
    view = df.copy()
    if syms:
        view = view[view["symbol"].isin(syms)]
    if types:
        view = view[view["match_type"].isin(types)]
    view = view[view["score"] >= minscore]
    d0, d1 = df["timestamp"].min().date(), df["timestamp"].max().date()
    dr = st.slider("Dates", d0, d1, (d0, d1), key="hse_fd") if d0 < d1 else (d0, d1)
    view = view[(view["timestamp"].dt.date >= dr[0]) & (view["timestamp"].dt.date <= dr[1])]
    by, asc = {"time, oldest first": (["timestamp"], True), "time, newest first": (["timestamp"], False),
               "score, highest first": (["score", "timestamp"], [False, True])}[order]
    view = view.sort_values(by, ascending=asc, kind="stable").reset_index(drop=True)
    shown = view.assign(timestamp=view["timestamp"].dt.strftime("%Y-%m-%d %H:%M %Z"))
    status_cols = [e.cond.id for e in next(iter(res.evals.values()))]
    styled = shown.style.map(lambda v: f"background-color: {STATUS_COLORS.get(v, '')}", subset=[
        c for c in status_cols if c in shown]).format(
        {c: "{:,.4g}" for c in shown.columns if c.endswith("_value")} | {"score": "{:.1f}"} |
        {c: "{:.1f}" for c in shown.columns if c.endswith("_score")}, na_rep="—")
    sel = st.dataframe(styled if len(shown) * len(shown.columns) < 200_000 else shown,
        width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row", key="hse_table", height=380)
    st.caption(f"{len(view):,} of {len(df):,} matches after filters. Select a row to inspect it.")
    d = st.columns(3)
    d[0].download_button("Download CSV", to_csv(view).encode(), f"{s.name}_matches.csv", "text/csv")
    payload = {"strategy": s.to_dict(), "version": s.version, "reports": {k: r.as_dict() for k, r in res.reports.items()},
               "results": json.loads(view.assign(timestamp=view["timestamp"].astype(str)).to_json(orient="records"))}
    d[1].download_button("Download JSON", json.dumps(payload, indent=1, default=str).encode(), f"{s.name}_matches.json",
                         "application/json")
    if d[2].button("Save this search"):
        sid = save_search(res, st.session_state.get("hse_res_yaml", s.to_yaml()))
        st.success(f"Saved as {sid}")

    rows = sel.selection.rows if sel and sel.selection else []
    if rows:
        r = view.iloc[rows[0]]
        bars, evals = res.bars[r["symbol"]], res.evals[r["symbol"]]
        i = bars.index.get_loc(r["timestamp"])
        st.markdown(f"**{r['symbol']} · {r['timestamp']:%Y-%m-%d %H:%M %Z} · {r['match_type']} · score {r['score']:.1f}**")
        ex = pd.DataFrame(explain(evals, i))
        st.dataframe(ex[["id", "status", "required", "explanation", "points", "max_points"]].style.map(
            lambda v: f"background-color: {STATUS_COLORS.get(v, '')}", subset=["status"]).format(
            {"points": "{:.1f}", "max_points": "{:.1f}"}),
            width="stretch", hide_index=True)
        b1, b2 = st.columns(2)
        before = b1.slider("Bars before", 5, 300, 60, key="hse_bb")
        after = b2.slider("Bars after", 0, 300, 20, key="hse_ba")
        overlays, lower = chart_studies(s, bars)
        st.altair_chart(_chart(bars, i, before, after, overlays, lower, f"{r['symbol']} around the signal"),
                        width="stretch")
        st.caption("Everything left of the dashed line was known when the signal bar closed. Bars to the right "
                   "happened afterwards and are shown only for retrospective inspection.")

    st.markdown("**Forward behaviour after signal**")
    st.caption("What price did after each match, measured from the signal bar's close. Not a backtest: no entries, "
               "fills, position sizes, costs or option prices are assumed, and past behaviour doesn't predict future results.")
    c = st.columns([3, 1])
    horizons = c[0].multiselect("Horizons (bars, or trading days with d)", sorted(set(s.horizons) | {"1", "3", "5", "10", "1d", "5d"},
                                                                                key=lambda h: (h.endswith("d"), int(h.rstrip("d")))),
                                default=s.horizons, key="hse_h")
    dd = c[1].number_input("Keep signals at least N bars apart", 0, 10_000, s.dedupe_bars, key="hse_dd",
                           help="0 keeps every signal; overlapping windows then count the same move several times")
    if horizons:
        per, summ = forward_behavior(view, res.bars, horizons, s.timeframe, int(dd))
        if summ.empty:
            st.info("No signals to measure.")
        else:
            st.dataframe(summ.style.format({c: "{:.2f}" for c in summ.columns if c.endswith("_pct")}, na_rep="—"),
                         width="stretch", hide_index=True)
            note = f"{summ.attrs.get('deduped', 0)} signals dropped by the spacing rule. " if dd else ""
            st.caption(note + "Incomplete = the horizon runs past the last available bar; left out of the statistics.")
            with st.expander("Per-signal forward behaviour"):
                st.dataframe(per.assign(timestamp=per["timestamp"].astype(str),
                                        end_time=per.get("end_time", pd.Series(dtype=str)).astype(str)),
                             width="stretch", hide_index=True)


def render() -> None:
    st.subheader("Historical Signal Explorer")
    st.caption("Find the past bars where a set of indicator conditions matched exactly or nearly, and see why. "
               "Historical information for research and education; not a recommendation or a trading instruction.")
    lib = list_strategies()
    top = st.columns([2, 2, 1])
    choice = top[0].selectbox("Strategy", list(lib) + ["New strategy (template)"], key="hse_choice")
    yaml_key = _key("yaml", choice)
    if yaml_key not in st.session_state:
        st.session_state[yaml_key] = lib[choice].read_text() if choice in lib else TEMPLATE
    up = top[1].file_uploader("Import a strategy YAML", type=["yaml", "yml"], key="hse_up")
    if up is not None and st.session_state.get("hse_up_seen") != (up.name, up.size):
        st.session_state["hse_up_seen"] = (up.name, up.size)
        st.session_state[yaml_key] = up.getvalue().decode()
        st.rerun()
    text = st.text_area("Strategy (YAML)", key=yaml_key, height=340,
                        help="Conditions use the same language as scans.yaml. See the user guide for every function.")
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
    run = a[0].button("Search", type="primary")
    new_name = a[1].text_input("Name to save as", strategy.name, key=_key("nm", choice), label_visibility="collapsed")
    if a[2].button("Save strategy"):
        try:
            path = save_strategy(strategy.to_yaml(), name=new_name, overwrite=new_name == strategy.name and choice in lib)
            st.success(f"Saved {path.name}")
        except FileExistsError as exc:
            st.error(f"{exc}; pick another name")
    dup_name = a[3].text_input("Duplicate as", f"{strategy.name} copy", key=_key("dup", choice),
                               label_visibility="collapsed")
    if a[4].button("Duplicate") and choice in lib:
        try:
            st.success(f"Created {duplicate_strategy(lib[choice], dup_name).name}")
        except (FileExistsError, StrategyError) as exc:
            st.error(str(exc))
    st.download_button("Download this strategy as YAML", strategy.to_yaml().encode(), f"{strategy.name}.yaml",
                       "text/yaml")

    if run:
        bar = st.progress(0.0, text="Starting...")
        res = run_search(strategy, progress=lambda i, n, sym: bar.progress(i / n, text=f"Searching {sym} ({i + 1}/{n})"))
        bar.progress(1.0, text=f"Done in {res.seconds:.1f}s")
        st.session_state["hse_res"], st.session_state["hse_res_yaml"] = res, strategy.to_yaml()
    if "hse_res" in st.session_state:
        st.divider()
        _results(st.session_state["hse_res"])

    with st.expander("Saved searches"):
        saved = list_searches()
        if saved.empty:
            st.caption("None yet. Run a search and press **Save this search**.")
        else:
            st.dataframe(saved, width="stretch", hide_index=True)
            sid = st.selectbox("Open a saved search", saved["search_id"], key="hse_sid")
            yml, meta, _ = load_search(sid)
            st.caption(f"Version {meta['strategy_version']} · {meta['total_matches']} matches · {meta['bars_evaluated']:,} "
                       f"bars evaluated · source {meta['source']}")
            c = st.columns(2)
            if c[0].button("Re-run it and compare"):
                res = run_search(parse_strategy(yml))
                notes = compare_to_saved(res, meta)
                st.session_state["hse_res"], st.session_state["hse_res_yaml"] = res, yml
                (st.warning("\n\n".join(notes)) if notes else
                 st.success("Same strategy version and identical bars: the results are directly comparable."))
            c[1].download_button("Download its strategy YAML", yml.encode(), f"{sid}.yaml", "text/yaml")
