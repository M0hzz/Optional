"""Dashboard pieces of the deterministic overlay: the console that sits above every view, the
per-view panels and the evidence view. Everything shown comes from optlab.overlay: fixed rules,
fixed formulas and fixed sentence templates. No AI is involved anywhere."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import streamlit as st

import ui
from optlab.overlay.engine import TRANSITION_TEXT, OverlayScan, results_frame
from optlab.overlay.rules import RuleResult
from optlab.overlay.status import STATUSES, badge, chip_html, css

# overlay panel keys (overlay.yaml → tabs) and where each panel now lives
PANEL_NAMES = {"scanner": "Live › Watchlist", "macro": "Live › Market", "option_chain": "Live › Option chain",
               "strategy_lab": "Live › Trade builder (strategy)", "risk": "Live › Trade builder (sizing)",
               "monitor": "Live › Monitor", "backtest": "Research › Backtest", "signal_explorer": "Research › Signal explorer",
               "chain_pricer": "Research › Volatility & pricing", "company": "Research › Company & insiders (company)",
               "insiders": "Research › Company & insiders (insiders)"}
HIGHLIGHT_STATUSES = ["CONFIRMED", "NEAR_CONFIRMATION", "INVALIDATED", "OVEREXTENDED", "STALE", "CONFLICTING",
                      "INSUFFICIENT_DATA", "DATA_ERROR", "WATCH"]
SEVERITY = {0: "Information and up", 1: "Caution and up", 2: "Serious and up", 3: "Blocking only"}


def md(text: str) -> str:
    """Escape $ so amounts like $700 ... $100 aren't rendered as Markdown maths."""
    return str(text).replace("$", "\\$")


def chips(items: list[tuple[str, str | None]]) -> None:
    st.markdown("".join(chip_html(code, text) for code, text in items), unsafe_allow_html=True)


def sidebar_controls(cfg) -> dict:
    """Overlay switches and filters (session only; persistent settings live in Research › Overlay setup)."""
    st.markdown("**Overlay**", help="Rules-based highlights across every view. Fixed formulas and thresholds from "
                                   "overlay.yaml; no AI or machine learning. Change thresholds in Research › Overlay setup.")
    on = st.toggle("Show overlay", value=cfg.enabled, key="ovl_on",
                   help="Turns the console and every view's overlay panel on or off for this session.")
    compact = st.toggle("Compact console", value=bool(cfg.raw.get("compact")), key="ovl_compact",
                        help="One line per highlight instead of a line plus its explanation.")
    statuses = st.multiselect("Show statuses", HIGHLIGHT_STATUSES, default=HIGHLIGHT_STATUSES[:-1], key="ovl_status",
                              format_func=lambda c: badge(c, cfg.labels),
                              help="Which statuses appear in the console's highlight list.")
    sev = st.select_slider("Minimum severity", options=list(SEVERITY), value=0, format_func=SEVERITY.get,
                           key="ovl_sev", help="Hide lower-severity highlights. Serious = invalidations, account "
                                               "and liquidity violations, data problems.")
    return {"on": on, "compact": compact, "statuses": statuses, "min_severity": sev}


def pointer(r: RuleResult) -> str:
    if r.status in ("DATA_ERROR",) or "DATA_ERROR" in r.data_flags:
        return "Research › Overlay setup → Data health"
    return "Live › Watchlist" + (" · Research › Signal explorer for history" if r.rule_id != "conflict" else "")


def console_summary(sc: OverlayScan) -> str:
    """One line for the console's folded header: the counts that matter most."""
    c = sc.counts()
    parts = [f"{STATUSES[k].icon} {c[k]} {sc.config.labels.get(k, STATUSES[k].label).lower()}"
             for k in ("CONFIRMED", "NEAR_CONFIRMATION", "INVALIDATED", "CONFLICTING", "DATA_ERROR") if c.get(k)]
    regime = (sc.market or {}).get("regime")
    return " · ".join(parts + ([f"market {regime.lower()}"] if regime else [])) or "nothing to flag"


def console(sc: OverlayScan, f: dict, extra_chips: list | None = None, expanded: bool = True) -> None:
    """The strip above every view: status summary, the things worth looking at, evidence. It folds to one line
    (it never disappears); open by default in Live, folded in Research."""
    cfg = sc.config
    with st.expander(f"🧭 Mastermind console — {console_summary(sc)}", expanded=expanded):
        top = st.columns([3, 2])
        top[0].caption("Deterministic highlights: every item comes from a rule in overlay.yaml evaluated on the "
                       "latest completed bar. No AI, no predictions, no trading.")
        top[1].caption(f"Scanned {sc.now:%H:%M:%S} in {sc.seconds:,.2f} s · {sc.n_symbols:,} symbols · "
                       f"{sc.n_bars:,} bars · {sc.n_rules:,} rules · config {sc.versions.get('config', '')}")
        fails = sc.failed_checks()
        blocks = [c for c in fails if c.effect == "block"]
        stale = sorted({c.symbol for c in fails if c.id == "fresh"})
        items = []
        if blocks:
            items.append(("DATA_ERROR", f"🛑 Data errors: {len(blocks)}"))
        if stale:
            items.append(("STALE_DATA", f"🕰️ Stale data: {len(stale)} symbol(s)"))
        if not blocks and not stale:
            items.append(("INFO", "🩺 Data checks passed" if not fails else f"🩺 {len(fails)} data warning(s)"))
        counts = sc.counts()
        for code in ("CONFIRMED", "NEAR_CONFIRMATION", "INVALIDATED", "OVEREXTENDED", "STALE", "CONFLICTING",
                     "INSUFFICIENT_DATA"):
            if counts.get(code):
                items.append((code, f"{STATUSES[code].icon} {ui.fmt(counts[code], 'int')} {cfg.labels.get(code, STATUSES[code].label).lower()}"))
        if sc.n_symbols * max(sc.n_rules, 1) > 400:
            items.append(("FITS_BP_EXCEEDS_RISK", f"⚠️ Large scan: {sc.n_symbols:,} symbols × {sc.n_rules:,} rules"))
        items += extra_chips or []
        chips(items)
        hl = sc.highlights(f["statuses"], min_severity=f["min_severity"], limit=int(cfg.raw.get("console_items", 8)))
        if not hl:
            st.caption("Nothing matches the console filters right now.")
        lines = []
        for r in hl:
            ch = chip_html(r.status, None, cfg.labels)
            move = f" <b>· {TRANSITION_TEXT[r.transition]}</b>" if r.transition else ""
            flags = "".join(chip_html(x, None, cfg.labels) for x in r.data_flags if x in STATUSES)
            body = "" if f["compact"] else f"<br><span style='opacity:0.8'>{r.explanation}</span>"
            lines.append(f"<div style='margin:2px 0 8px 0'>{ch}{flags}<b>{r.symbol}</b> · {r.rule_name}{move} "
                         f"<span style='opacity:0.65'>→ {pointer(r)}</span>{body}</div>")
        box = st.container(height=None if f["compact"] or len(lines) <= 3 else 330, border=False)
        box.markdown("".join(lines).replace("$", "&#36;"), unsafe_allow_html=True)
        if hl:
            with st.popover("Evidence for these highlights", use_container_width=True):
                pick = st.selectbox("Highlight", range(len(hl)), key="ovl_ev_pick",
                                    format_func=lambda i: f"{hl[i].symbol} · {hl[i].rule_name} · {badge(hl[i].status, cfg.labels)}",
                                    help="Pick a highlight to see every condition, the formula and the versions behind it.")
                evidence(hl[pick], sc, "console")


def evidence(r: RuleResult, sc: OverlayScan | None = None, where: str = "panel") -> None:
    """Condition checklist plus the audit record of one result."""
    st.markdown(f"**{badge(r.status)}** · {r.symbol} · {r.rule_name} · {r.timeframe} · bar {r.data_timestamp or '—'}")
    st.markdown(md(r.explanation))
    if r.conditions:
        rows = pd.DataFrame([{"condition": c["id"], "expression": c["expression"], "required": c["required"],
                              "status": c["status"].upper() if c["status"] == "n/a" else c["status"].capitalize(),
                              "observed": c["observed"], "requirement": c["required_value"],
                              "distance": c["shortfall"], "tolerance": c["tolerance"], "points": c["points"],
                              "max_points": c["max_points"], "still_needed": c["needed"] or "—"} for c in r.conditions])
        ui.show(rows, formats={"observed": "num", "distance": "num", "points": "num1", "max_points": "num1"},
                rename={"max_points": "Out of", "still_needed": "Still needed"},
                help={"distance": "Shortfall from passing, in the tolerance's units (% for percent-distance tolerances).",
                      "points": "Score points earned: 100 × weight × credit ÷ total weight."},
                styler=ui.status_styler(["Status"], ui.STATUS_COLORS), width="stretch")
    if r.missing and r.status != "CONFIRMED":
        st.markdown(md("**Missing confirmation:** " + "; ".join(r.missing)))
    facts = {"Score": f"{r.score:.1f} (previous bar {r.prev_score:.1f})" if r.prev_score == r.prev_score else f"{r.score:.1f}",
             "Confirmation needs": f"score ≥ {r.minimum_score:g}, ≥ {r.minimum_pass_count} passing, required conditions pass",
             "Bars since trigger": "—" if r.bars_since is None else f"{r.bars_since}",
             "Move since trigger": "—" if r.move_since_pct is None else f"{r.move_since_pct:+.2f}%"
             + ("" if r.atr_move is None else f" ({r.atr_move:+.2f} ATR)"),
             "Distance from reference average": "—" if r.ref_distance_atr is None else f"{r.ref_distance_atr:+.2f} ATR",
             "Data": f"{r.data_source or '—'} · latest bar {r.data_timestamp or '—'}"
             + (f" · {', '.join(r.data_flags)}" if r.data_flags else ""),
             "Formula": r.formula, "Reason code": r.reason_code,
             "Versions": ", ".join(f"{k} {v}" for k, v in r.versions.items())}
    if sc is not None and r.symbol in sc.alignment:
        a = sc.alignment[r.symbol]
        facts["Timeframes"] = f"higher {a['higher']} · intermediate {a['intermediate']} · trigger {a['lower']} → {a['agreement']}"
    st.markdown(md("\n".join(f"- **{k}:** {v}" for k, v in facts.items())))
    if st.toggle("Show the audit record (JSON)", key=f"ovl_aud_{where}",
                 help="Exactly what was calculated: inputs, outputs, versions."):
        st.code(json.dumps(r.as_dict(), indent=1, default=str, ensure_ascii=False)[:20000], language="json")


def status_cells(sty, col="Status"):
    rev = {badge(c): c for c in STATUSES}
    return sty.map(lambda v: css(rev[v]) if v in rev else "", subset=[col]) if col in sty.data.columns else sty


def scanner_panel(sc: OverlayScan) -> None:
    """Every rule on every symbol, with status, score, freshness and what's missing."""
    st.markdown("### 🧭 Overlay: rule status by symbol",
                help="Each row is one rule from overlay.yaml evaluated on the symbol's latest completed daily bar.")
    res = sc.for_tab("scanner")
    if not res:
        st.caption("No overlay rules apply to this view.")
        return
    df = results_frame(res)
    c = st.columns([2, 2, 2])
    rules = c[0].multiselect("Rules", sorted(df["rule"].unique()), key="ovl_sc_rules", placeholder="All rules",
                             help="Show only these rules.")
    syms = c[1].multiselect("Symbols", sorted(df["symbol"].unique()), key="ovl_sc_syms", placeholder="All symbols",
                            help="Show only these symbols.")
    hide = c[2].toggle("Hide 'not present'", value=True, key="ovl_sc_hide",
                       help="Hide rows where too few conditions pass to matter.")
    if rules:
        df = df[df["rule"].isin(rules)]
    if syms:
        df = df[df["symbol"].isin(syms)]
    if hide:
        df = df[df["status"] != "NOT_PRESENT"]
    df = df.assign(status=df["status"].map(badge), direction=df["direction"].str.capitalize())
    order = {badge(k): v for k, v in {"CONFIRMED": 0, "INVALIDATED": 1, "NEAR_CONFIRMATION": 2, "CONFLICTING": 3,
                                      "OVEREXTENDED": 4, "STALE": 5, "DATA_ERROR": 6, "INSUFFICIENT_DATA": 7,
                                      "WATCH": 8, "NOT_PRESENT": 9}.items()}
    df = df.sort_values(["status", "score"], key=lambda s: s.map(order) if s.name == "status" else -s).reset_index(drop=True)
    ui.show(df.drop(columns=["rule_id", "timeframe", "prev_score", "explanation"]),
            formats={"score": "num1", "passed": "int", "near": "int", "failed": "int", "unavailable": "int",
                     "conditions": "int", "bars_since": "int", "move_since_pct": "spts", "atr_move": "signed",
                     "ref_distance_atr": "signed", "bar": "text"},
            rename={"bars_since": "Bars since trigger", "move_since_pct": "Move since trigger", "atr_move": "ATR move",
                    "ref_distance_atr": "ATR from average", "change": "Change on latest bar", "data": "Data flags",
                    "bar": "Latest bar"},
            help={"status": "Overlay status. Research › Overlay setup → Status guide has the definitions.",
                  "score": "100 × Σ(weight × credit) ÷ Σ(weight).", "unavailable": "Conditions with no value (warm-up "
                  "or missing data). They score 0; nothing is substituted.",
                  "bars_since": "Bars since the current confirmed run began.",
                  "atr_move": "(close − trigger close) ÷ ATR(14) at the trigger.",
                  "ref_distance_atr": "(close − reference average) ÷ ATR(14).",
                  "change": "State change between the previous and the latest completed bar."},
            styler=status_cells, width="stretch", height=380)
    st.caption(f"{len(df):,} rows. Click a row in the evidence picker below for the full checklist.")
    with st.expander("Timeframe alignment", expanded=False):
        al = pd.DataFrame([{"symbol": s, **a} for s, a in sorted(sc.alignment.items())])
        if not al.empty:
            ui.show(al, rename={"higher": "Higher (weekly)", "intermediate": "Intermediate (daily structure)",
                                "lower": "Trigger (daily)", "agreement": "Agreement"},
                    help={"higher": "Weekly close and 5-week EMA vs the 10-week SMA.",
                          "intermediate": "Daily close and 21-day EMA vs the 50-day SMA.",
                          "lower": "Daily close and 9-day EMA vs the 21-day EMA."}, width="stretch")
    pool = [r for r in res if r.status != "NOT_PRESENT"] or res
    i = st.selectbox("Inspect a result", range(len(pool)), key="ovl_sc_pick",
                     format_func=lambda i: f"{pool[i].symbol} · {pool[i].rule_name} · {badge(pool[i].status)}",
                     help="Full checklist and audit record for one rule on one symbol.")
    with st.container(border=True):
        evidence(pool[i], sc, "watchlist")


def data_health_panel(sc: OverlayScan) -> None:
    checks = pd.DataFrame([c.as_dict() for c in sc.checks])
    if checks.empty:
        st.caption("No data checks ran.")
        return
    failed = checks[~checks["passed"]]
    m = st.columns(4)
    m[0].metric("Checks passed", f"{int(checks['passed'].sum()):,}")
    m[1].metric("Checks failed", f"{len(failed):,}")
    m[2].metric("Blocking", f"{int((failed['effect'] == 'block').sum()):,}", help="Failed checks that stop dependent "
                "calculations (shown as Data error).")
    m[3].metric("Downgrading", f"{int((failed['effect'] == 'downgrade').sum()):,}", help="Failed checks that leave "
                "results visible but labelled (e.g. Stale data).")
    show = st.toggle("Show passed checks too", value=False, key="ovl_dh_all")
    view = checks if show else failed
    view = view.assign(passed=view["passed"].map({True: "✅ Passed", False: "❌ Failed"}),
                       effect=view["effect"].map({"block": "🛑 Blocks", "downgrade": "⚠️ Downgrades", "none": "ℹ️ None"}),
                       affects=view["affects"].map(lambda a: ", ".join(a) if a else "—"))
    ui.show(view[["symbol", "name", "passed", "detail", "effect", "affects", "action"]],
            rename={"name": "Check", "passed": "Result", "effect": "Effect", "affects": "Affected features",
                    "action": "Corrective action"}, width="stretch")


# ------------------------------------------------------------------ phase 2: market and monitor
VERDICT_TEXT = {"CONFIRMED": "Present", "NEAR_CONFIRMATION": "Nearly present (one item missing)",
                "NOT_PRESENT": "Not present", "INSUFFICIENT_DATA": "Insufficient data",
                "VOLATILITY_EXPANSION": "Present: volatility expanding"}


def market_chips(sc: OverlayScan) -> list:
    m = sc.market or {}
    if not m or m.get("error"):
        return [("DATA_ERROR", "🧭 Market overview unavailable")] if m.get("error") else []
    out = [("INFO", f"🧭 Market: {m['regime']}")]
    vr = m.get("volatility_regime", "")
    out.append(("VOLATILITY_EXPANSION" if vr.startswith("Expanded") else "INFO", f"🌀 Volatility: {vr.split(' (')[0]}"))
    if m.get("breadth_low_sample"):
        out.append(("LOW_SAMPLE_SIZE", f"🔬 Breadth from {m['breadth'].get('n', 0)} symbols"))
    return out


def market_panel(sc: OverlayScan) -> None:
    m = sc.market or {}
    st.markdown("### 🧭 Overlay: market overview", help="Computed from the stored daily bars and the VIX series. "
                "Fixed checklists; nothing is estimated when data is missing.")
    if not m or m.get("error"):
        st.warning(f"Market overview unavailable: {m.get('error', 'no data')}")
        return
    br, vix = m["breadth"], m["vix"]
    c = st.columns(4)
    c[0].metric("Market regime", m["regime"], help="Broad bullish/bearish alignment when every available item of that "
                "checklist passes; leaning when one item fails; otherwise mixed.")
    c[1].metric("Volatility regime", m["volatility_regime"].split(" (")[0], help=m["volatility_regime"])
    c[2].metric("Breadth", f"{br.get('pct_above_ma', float('nan')):.0f}% above MA" if br.get("n") else "—",
                f"{br.get('advancers', 0)} up / {br.get('decliners', 0)} down" if br.get("n") else None, delta_color="off",
                help=f"Share of {br.get('n', 0)} non-index symbols above their {sc.config.market['breadth_ma']}-day "
                     "moving average on the latest common date.")
    c[3].metric("VIX", f"{vix['level']:.1f}" if vix else "—", f"{vix['change_5d']:+.1f} over 5 days" if vix else None,
                delta_color="off", help="From the macro table; 1-year percentile "
                f"{vix['pct_rank']:.0f}." if vix else "No VIX series in the macro table.")
    if m["breadth_low_sample"]:
        chips([("LOW_SAMPLE_SIZE", f"🔬 Low sample size: breadth uses {br.get('n', 0)} symbols (minimum "
                f"{sc.config.thresholds['breadth_minimum_symbols']}). Add symbols to config.yaml → universe.")])
    if m["missing_indexes"]:
        st.caption(f"Not in the database, so left out (not estimated): {', '.join(m['missing_indexes'])}.")
    if m["indexes"]:
        rows = pd.DataFrame(m["indexes"].values()).drop(columns=["date", "prior_high", "prior_low", "bb_width_rank_5ago"])
        ui.show(rows, formats={"close": "money", "change_pct": "spts", "ret20_pct": "spts", "gap_pct": "spts",
                               "rel_volume": "num", "atr_pct_rank": "int", "bb_width_pct_rank": "int"},
                rename={"change_pct": "Day change", "ret20_pct": "20-day return", "above_vwap": "Above 20-day VWAP",
                        "ema_bullish": "EMA 9 > 21", "above_sma50": "Above 50-day", "above_sma200": "Above 200-day",
                        "gap_pct": "Gap at open", "rel_volume": "Relative volume", "atr_pct_rank": "ATR percentile",
                        "bb_width_pct_rank": "Band-width percentile", "high_20": "20-day high", "low_20": "20-day low"},
                help={"trend": "Up: close and 21-day EMA above the 50-day SMA; Down: both below; else Mixed.",
                      "atr_pct_rank": "Where ATR(14) sits among the last 252 values (0 = lowest)."}, width="stretch")
    st.markdown("**Regime checklists**")
    for key, (name, verdict, items) in m["verdicts"].items():
        with st.expander(f"{STATUSES[verdict].icon} {name}: {VERDICT_TEXT.get(verdict, verdict)}"):
            if not items:
                st.caption(m["sectors"].get("note", "Not enough data for this checklist."))
                continue
            df = pd.DataFrame(items).assign(status=lambda d: d["status"].map({"pass": "Pass", "fail": "Fail", "n/a": "N/A"}))
            ui.show(df, rename={"item": "Condition", "value": "Observed"}, width="stretch",
                    styler=ui.status_styler(["Status"], ui.STATUS_COLORS))
    if m["sectors"]["available"]:
        st.markdown("**Sector strength vs the benchmark (20-day)**")
        ui.show(m["sectors"]["table"], formats={"rs20": "spts", "rs20_5ago": "spts", "rank": "int", "rank_5ago": "int",
                                                "rank_change": "int"},
                rename={"etf": "ETF", "rs20": "Relative strength", "rs20_5ago": "5 bars ago", "rank_5ago": "Rank 5 bars ago",
                        "rank_change": "Rank change"}, width="stretch")
    else:
        st.caption(m["sectors"]["note"])
    for t in m["conflicts"]:
        chips([("CONFLICTING", f"⚖️ {t}")])
    st.caption(f"{m['events_note']} Data through {ui.fmt(m['date'], 'date')}.")


def monitor_overlay_panel(mon_dir) -> None:
    latest, log = mon_dir / "overlay_latest.json", mon_dir / "overlay_alerts.csv"
    st.markdown("**Overlay alerts (state changes on completed bars)**",
                help="Written by scripts/monitor.py each pass: one alert per rule, symbol, bar and change, with a "
                     "cooldown so the same state doesn't repeat. Signal stacking groups related rules once.")
    if latest.exists():
        d = json.loads(latest.read_text())
        st.caption(f"Last overlay pass {d['time'][:19].replace('T', ' ')} · {d['seconds']:,.2f} s · {d['symbols']:,} symbols"
                   f" · {d['rules']:,} rules · {d['bars']:,} bars"
                   + (f" · skipped: {'; '.join(n for n, _ in d['skipped'])}" if d.get("skipped") else ""))
    if not log.exists():
        st.caption("No overlay alerts yet. They appear when a rule is newly confirmed, goes from near to confirmed, "
                   "becomes near, or loses its confirmation.")
        return
    al = pd.read_csv(log).tail(200).iloc[::-1]
    al["time"] = al["time"].astype(str).str[:16].str.replace("T", " ")
    al["status"] = al["status"].map(lambda c: badge(c) if c in STATUSES else c)
    ui.show(al[["time", "status", "symbol", "bar", "title", "body"]], rename={"bar": "Bar", "title": "Alert",
                                                                            "body": "Details"},
            styler=status_cells, width="stretch", height=320)


# ------------------------------------------------------------------ phase 3: options and account
from optlab.overlay.account import Profile, legs_fit  # noqa: E402
from optlab.overlay.options import contract_flags, deterioration, iv_context, neighbour_iv, structures  # noqa: E402
from optlab.overlay.quality import chain_health  # noqa: E402

FIT_STATUS = {"FITS CONFIGURED LIMITS": "FITS_ACCOUNT", "FITS BUYING POWER BUT EXCEEDS PREFERRED RISK": "FITS_BP_EXCEEDS_RISK",
              "INSUFFICIENT DATA": "INSUFFICIENT_DATA"}


def fit_badge(cls: str) -> str:
    code = FIT_STATUS.get(cls, "DOES_NOT_FIT_ACCOUNT")
    return f"{STATUSES[code].icon} {cls.capitalize()}"


def liq_badge(v: str) -> str:
    return "💧 Poor liquidity" if v == "POOR_LIQUIDITY" else "✔️ Usable"


def _cell_colors(sty, cols):
    def color(v):
        s = str(v)
        if s.startswith("💧") or s.startswith("🚫") or s.startswith("🛑"):
            return css("POOR_LIQUIDITY")
        if s.startswith("⚠️") or s.startswith("🔎"):
            return css("FITS_BP_EXCEEDS_RISK")
        if s.startswith("🧾") or s.startswith("✔️"):
            return css("FITS_ACCOUNT")
        if s.startswith("❔"):
            return css("INSUFFICIENT_DATA")
        return ""
    return sty.map(color, subset=[c for c in cols if c in sty.data.columns])


def account_chip(cfg) -> tuple:
    p = Profile.from_config(cfg.account)
    mode = "Small account" if cfg.account.get("small_account_mode", True) else "Account"
    return ("FITS_ACCOUNT", f"🧾 {mode}: ${p.allocatable:,.0f} allocatable · max loss ${p.max_loss_per_idea:,.0f}/idea")


def technical_status(sc: OverlayScan | None, symbol: str) -> dict:
    """Best standing rule status per direction for a symbol, from the daily overlay scan."""
    out = {"bullish": "No rule standing", "bearish": "No rule standing", "available": False}
    if sc is None:
        return out
    res = [r for r in sc.results if r.symbol == symbol and r.rule_id != "conflict"]
    if not res:
        out["bullish"] = out["bearish"] = f"No daily bars for {symbol} in the database"
        return out
    out["available"] = True
    order = ["CONFIRMED", "OVEREXTENDED", "STALE", "NEAR_CONFIRMATION", "INVALIDATED"]
    for d in ("bullish", "bearish"):
        cand = [r for r in res if r.direction == d and r.status in order]
        if cand:
            r = min(cand, key=lambda r: order.index(r.status))
            out[d] = f"{badge(r.status)} ({r.rule_name})"
    return out


def chain_panel(ch: pd.DataFrame, view: pd.DataFrame, ticker: str, source: str, fetched_at, sc, cfg, earnings=None,
                hv20=None) -> None:
    """Overlay for Live › Option chain: quote quality, account fit, IV context and the structure scanner."""
    th = cfg.thresholds
    p = Profile.from_config(cfg.account)
    now = pd.Timestamp.now(tz="America/New_York").to_pydatetime()
    delay = 15 if source == "cboe" else 0
    checks = chain_health(ticker, ch, fetched_at, now, th, source, delay)
    fresh = next((c.passed for c in checks if c.id == "quote_age"), True)
    df = contract_flags(ch, th, p, fresh, earnings)
    df["iv_vs_neighbours"] = neighbour_iv(df)
    st.markdown("### 🧭 Overlay: contract quality and account fit",
                help="Every contract checked against fixed liquidity thresholds and the account profile in overlay.yaml. "
                     "Affordability and quality are separate: a cheap contract is never marked good for being cheap.")
    usable = int((df["liquidity"] == "USABLE").sum())
    fits = int((df["account_status"] == "FITS_ACCOUNT").sum())
    items = [("FITS_ACCOUNT" if usable else "POOR_LIQUIDITY", f"✔️ {usable:,} usable quotes"),
             ("POOR_LIQUIDITY", f"💧 {len(df) - usable:,} poor liquidity"),
             ("FITS_ACCOUNT", f"🧾 {fits:,} long options fit the account"),
             ("REQUIRES_DATA_VALIDATION", f"🔎 {int((df['anomaly'] != '').sum()):,} quotes need validation")]
    if delay:
        items.append(("INFO", f"ℹ️ Quotes delayed about {delay} min"))
    for c in checks:
        if not c.passed and c.effect != "none":
            items.append(("STALE_DATA", f"🕰️ Stale quotes: {c.detail}") if c.id == "quote_age"
                         else ("DATA_ERROR", f"❌ {c.name} failed: {c.detail}"))
    chips(items)
    key = f"ovl_prev_{ticker}_{source}"
    prev = st.session_state.get(key)
    det = deterioration(prev, df, th)
    st.session_state[key] = df[["contract", "liquidity", "spread_pct_mid", "bid", "volume", "flag_low_volume", "flag_stale"]]
    if not det.empty:
        chips([("POOR_LIQUIDITY", f"💧 Liquidity deteriorated since the last load: {len(det)} contract(s)")])
        ui.show(det, width="stretch")
    sel = df[df["contract"].isin(view["contract"])] if "contract" in view else df
    t1, t2, t3 = st.tabs(["Contracts", "Implied volatility", "Defined-risk structures"])
    with t1:
        only = st.toggle("Only contracts that fit the account", value=False, key="ovl_ch_fit",
                         help="Fit is about cost and risk only; check the liquidity and flags columns too.")
        tv = sel[sel["account_status"] == "FITS_ACCOUNT"] if only else sel
        out = tv.assign(liquidity=tv["liquidity"].map(liq_badge), account_fit=tv["account_fit"].map(fit_badge),
                        type=tv["type"].str.capitalize(), iv=tv["iv"] * 100 if tv["iv"].max() < 10 else tv["iv"],
                        validation=tv["anomaly"].map(lambda a: f"🔎 {a}" if a else "—"))
        cols = ["contract", "type", "expiration", "dte", "strike", "bid", "ask", "spread_usd", "spread_pct_mid", "volume",
                "open_interest", "iv", "iv_vs_neighbours", "delta", "cost_per_contract", "account_risk_pct", "bp_remaining",
                "liquidity", "account_fit", "flags", "validation", "exercise_cash"]
        ui.show(out[cols], formats={"bid": "money", "ask": "money", "spread_usd": "money", "spread_pct_mid": "pts",
                                    "volume": "int", "open_interest": "int", "iv": "pts", "iv_vs_neighbours": "spts",
                                    "delta": "signed", "cost_per_contract": "money", "account_risk_pct": "pts",
                                    "bp_remaining": "money", "strike": "money", "dte": "int", "exercise_cash": "money0",
                                    "expiration": "date"},
                rename={"spread_usd": "Spread ($)", "spread_pct_mid": "Spread % of mid", "iv_vs_neighbours": "IV vs neighbours",
                        "cost_per_contract": "Debit per contract", "account_risk_pct": "% of account at risk",
                        "bp_remaining": "Buying power left", "account_fit": "Account fit", "flags": "Quality flags",
                        "validation": "Data validation", "exercise_cash": "Cash to exercise (calls)"},
                help={"spread_pct_mid": f"(ask − bid) ÷ mid. Above {th['maximum_bid_ask_pct']}% is flagged.",
                      "iv_vs_neighbours": "IV ÷ median IV of the two strikes either side − 1.",
                      "cost_per_contract": "Ask × 100: the maximum loss of a long option.",
                      "account_risk_pct": "Debit ÷ total buying power.",
                      "bp_remaining": "Allocatable capital (buying power − reserve) minus the debit.",
                      "liquidity": "Usable only if fresh, bid > 0, not crossed, spread, open interest and volume all pass.",
                      "account_fit": "Small-account classification for buying one contract."},
                styler=lambda s: _cell_colors(s, ["Liquidity", "Account fit", "Data validation"]), width="stretch",
                height=420)
        st.caption(md(f"{len(tv):,} contracts · natural prices (you pay the ask) · limits: allocatable "
                      f"${p.allocatable:,.0f}, max loss ${p.max_loss_per_idea:,.0f} per idea, account risk "
                      f"{p.max_account_risk_pct:g}% (${p.risk_limit:,.0f})."))
    with t2:
        ivc = iv_context(df, hv20)
        c = st.columns(3)
        c[0].metric("~30-day ATM IV", f"{ivc['atm_iv']:.1%}" if np.isfinite(ivc["atm_iv"]) else "—")
        c[1].metric("20-day realized vol", f"{hv20:.1%}" if hv20 else "—",
                    help="From the stored daily bars; unavailable when the symbol isn't in the database.")
        c[2].metric("IV ÷ realized", f"{ivc['iv_rv']:.2f}×" if np.isfinite(ivc["iv_rv"]) else "—",
                    help="Above 1: options price more movement than the stock has shown recently.")
        st.caption(ivc["history_note"])
        if not ivc["term"].empty:
            ui.show(ivc["term"], formats={"atm_iv": "pct", "vs_adjacent_pct": "spts", "dte": "int", "expiration": "date"},
                    rename={"atm_iv": "ATM IV", "vs_adjacent_pct": "vs adjacent expirations"},
                    help={"vs_adjacent_pct": "This expiration's ATM IV vs the average of the expirations either side."},
                    width="stretch")
    with t3:
        structures_panel(df, ticker, sc, cfg, p)


def structures_panel(df: pd.DataFrame, ticker: str, sc, cfg, p: Profile) -> None:
    th = cfg.thresholds
    exps = sorted(df["expiration"].unique())
    default = [e for e in exps if 7 <= int(df.loc[df["expiration"] == e, "dte"].iloc[0]) <= 60][:2] or exps[:1]
    c = st.columns([3, 1, 1, 2])
    pick = c[0].multiselect("Expirations", exps, default=default, key="ovl_st_exp", format_func=lambda d: ui.fmt(d, "date"),
                            help="Structures are built for these expirations only.")
    steps = c[1].slider("Max width (strikes)", 1, 5, 2, key="ovl_st_w", help="Verticals up to this many strikes wide.")
    band = c[2].slider("Strikes within ±%", 2, 30, 10, key="ovl_st_band", help="Only strikes this close to the price.")
    direction = c[3].radio("Direction", ["All", "Bullish", "Bearish"], horizontal=True, key="ovl_st_dir")
    only = st.toggle("Only structures that fit the account", value=False, key="ovl_st_fit",
                     help="Technical signals stay visible elsewhere even when nothing fits.")
    if not pick:
        st.caption("Pick at least one expiration.")
        return
    s = structures(df, p, th, pick, steps, band, ticker)
    if s.empty:
        st.caption("No structures in this range.")
        return
    tech = technical_status(sc, ticker)
    s["technical"] = s["direction"].map(lambda d: tech.get(d, "—"))
    if direction != "All":
        s = s[s["direction"] == direction.lower()]
    if only:
        s = s[s["account_status"] == "FITS_ACCOUNT"]
    st.caption("Status columns are kept separate on purpose: technical, liquidity, account fit and data validation are "
               "never merged into one score. Every figure uses natural prices; the mid column is shown for reference "
               "and is not an expected fill.")
    out = s.assign(liquidity=s["liquidity"].map(liq_badge), account_fit=s["account_fit"].map(fit_badge),
                   validation=s["validation"].map(lambda v: f"🔎 Requires data validation: {v}" if v else "—"),
                   earnings=s["earnings"].map({True: "⚠️ Before expiry", False: "—"}),
                   direction=s["direction"].str.capitalize())
    cols = ["strategy", "direction", "expiration", "dte", "strikes", "width", "net_natural", "net_mid", "max_profit",
            "max_loss", "capital", "account_risk_pct", "assignment", "technical", "liquidity", "account_fit",
            "validation", "earnings", "min_oi", "max_spread_pct", "delta", "theta"]
    ui.show(out[cols], formats={"net_natural": "signed", "net_mid": "signed", "max_profit": "money", "max_loss": "money",
                                "capital": "money", "account_risk_pct": "pts", "assignment": "money0", "width": "money",
                                "min_oi": "int", "max_spread_pct": "pts", "delta": "signed", "theta": "signed",
                                "dte": "int", "expiration": "date"},
            rename={"capital": "Capital required", "max_profit": "Max profit", "max_loss": "Max loss",
                    "net_natural": "Net (natural, + debit)", "net_mid": "Net at mid (reference)",
                    "account_risk_pct": "% of account", "assignment": "Assignment obligation",
                    "technical": "Technical (underlying)", "account_fit": "Account fit", "validation": "Data validation",
                    "earnings": "Earnings", "min_oi": "Lowest OI", "max_spread_pct": "Widest leg spread"},
            help={"net_natural": "Per share: debit = long ask − short bid; credit (negative) = short bid − long ask.",
                  "max_loss": "Debit × 100, or (width − credit) × 100.",
                  "assignment": "Short strike × 100: cash or shares an assignment could require before the long leg helps.",
                  "technical": "Best standing overlay rule on the underlying's daily bars, for this direction."},
            styler=lambda st_: _cell_colors(st_, ["Liquidity", "Account fit", "Data validation", "Earnings"]),
            width="stretch", height=420)
    st.caption(f"{len(s):,} structures · {int((s['account_status'] == 'FITS_ACCOUNT').sum()):,} fit the account profile.")
    i = st.selectbox("Inspect a structure", range(len(s)), key="ovl_st_pick",
                     format_func=lambda i: f"{s.iloc[i]['strategy']} {s.iloc[i]['strikes']} · {ui.fmt(s.iloc[i]['expiration'], 'date')}")
    r = s.iloc[i]
    tech_ok = "Confirmed" in str(r["technical"]) or "Overextended" in str(r["technical"]) or "Stale" in str(r["technical"])
    fresh = not bool(df["flag_stale"].any())
    e_verdict, e_items = cf.structure_template(r.to_dict(), tech_ok, fresh, th)
    with st.container(border=True):
        st.markdown(f"**{r['strategy']} {r['strikes']}**, expiring {ui.fmt(r['expiration'], 'date')} ({r['dte']} days)")
        st.markdown(md(f"- **Formula:** {r['formula']}\n- **Account fit:** {fit_badge(r['account_fit'])}"
                    + (f" — {r['reasons']}" if r["reasons"] else "") + f"\n- **Liquidity:** {liq_badge(r['liquidity'])}"
                    + (f" — {r['leg_flags']}" if r["leg_flags"] else "") + f"\n- **Technical:** {r['technical']}"
                    + (f"\n- **Assignment and legs:** {r['notes']}" if r["notes"] else "")
                    + ("\n- **Events:** earnings fall before this expiration." if r["earnings"] else "")
                    + "\n- **Dividends:** no dividend calendar is loaded, so ex-dividend assignment risk isn't checked."))
        st.markdown(f"**Affordable-and-liquid confluence (template E):** {badge(e_verdict)}")
        ui.show(pd.DataFrame(e_items).assign(status=lambda x: x["status"].map({"pass": "Pass", "fail": "Fail", "n/a": "N/A"})),
                rename={"item": "Condition", "value": "Observed"}, styler=ui.status_styler(["Status"], ui.STATUS_COLORS),
                width="stretch")



def risk_panel(cfg, legs, spot: float, symbol: str, name: str) -> None:
    p = Profile.from_config(cfg.account)
    st.markdown("### 🧭 Overlay: small-account check",
                help="Account profile from overlay.yaml (Small Account Mode by default), separate from the account equity "
                     "above. Change it in Research › Overlay setup.")
    c = st.columns(5)
    c[0].metric("Buying power", f"${p.total_buying_power:,.0f}")
    c[1].metric("Reserve", f"${p.reserve_cash:,.0f}")
    c[2].metric("Allocatable", f"${p.allocatable:,.0f}", help="min(settled cash − reserve, buying power − reserve)")
    c[3].metric("Max loss per idea", f"${p.max_loss_per_idea:,.0f}")
    c[4].metric("Account-risk limit", f"{p.max_account_risk_pct:g}% (${p.risk_limit:,.0f})")
    f = legs_fit(p, legs, spot, symbol, name)
    chips([(f.status, f"{STATUSES[f.status].icon} {f.classification.capitalize()}")])
    st.markdown(md(f"One unit of **{name}** on {symbol}, at modeled (Black-Scholes) prices, not quotes: capital "
                f"{ui.fmt(f.capital_required, 'money')}, maximum loss {ui.fmt(f.max_loss, 'money')}, assignment obligation "
                f"{ui.fmt(f.assignment_obligation, 'money0')}." + (f" Reasons: {'; '.join(f.reasons)}." if f.reasons else "")))
    if f.notes:
        st.caption(md(" ".join(f.notes)))


def hits_overlay(hits: pd.DataFrame, cfg) -> pd.DataFrame:
    """Liquidity and account-fit columns for option rows of the monitor's scan hits."""
    if hits.empty or "contract" not in hits or hits["contract"].isna().all():
        return hits
    h = hits[hits["contract"].notna()].copy()
    h["underlying_price"] = h["price"]
    for col in ("theta", "gamma", "vega", "last"):
        if col not in h:
            h[col] = np.nan
    h["expiration"] = pd.to_datetime(h["expiration"]).dt.date
    f = contract_flags(h.assign(type=h["type"].str.lower()), cfg.thresholds, Profile.from_config(cfg.account))
    out = hits.copy()
    out.loc[f.index, "liquidity"] = f["liquidity"].map(liq_badge)
    out.loc[f.index, "account_fit"] = f["account_fit"].map(fit_badge)
    out.loc[f.index, "quality_flags"] = f["flags"].str.replace("high theta vs premium, ", "", regex=False)
    return out


# ------------------------------------------------------------------ phase 4: strategy, simulator, backtest, history
from optlab.overlay.backtest_diag import diagnostics  # noqa: E402
from optlab.overlay.history_ev import evidence as rule_evidence, options_history_status  # noqa: E402
from optlab.overlay.structure import analyze, assignment_scenario, greek_path, stress  # noqa: E402

RISK_CODE = {"UNDEFINED": "DOES_NOT_FIT_ACCOUNT", "LEG_REMOVAL": "INVALIDATED", "EXPIRY_MISMATCH": "CONFLICTING",
             "EXERCISE": "FITS_BP_EXCEEDS_RISK", "PIN": "FITS_BP_EXCEEDS_RISK", "ASSIGNMENT": "DOES_NOT_FIT_ACCOUNT",
             "ACCOUNT": "DOES_NOT_FIT_ACCOUNT", "BREAKEVEN": "FITS_BP_EXCEEDS_RISK", "NARROW": "FITS_BP_EXCEEDS_RISK",
             "SLIPPAGE": "FITS_BP_EXCEEDS_RISK", "ILLIQUID": "POOR_LIQUIDITY"}


def _status_items(items: list[dict]) -> None:
    df = pd.DataFrame(items)
    df["status"] = df["status"].map(badge)
    ui.show(df[["status", "check", "value", "detail"]], rename={"check": "Check", "value": "Result"},
            styler=status_cells, width="stretch")


def strategy_panel(legs, spot, rate, dte, iv, cfg, symbol, close, atr_val, quotes, name) -> None:
    th = cfg.thresholds
    p = Profile.from_config(cfg.account)
    st.markdown("### 🧭 Overlay: structure checks and stress tests",
                help="Fixed checks on the legs above: defined risk, account fit, breakevens, hidden risks and stress "
                     "scenarios. Values are modeled (Black-Scholes), not quotes. A good-looking payoff is not a likely outcome.")
    with st.expander("Your outlook (optional): checked against the structure", expanded=False):
        c = st.columns(4)
        d = c[0].selectbox("Direction", ["Not set", "Bullish", "Bearish", "Neutral"], key="ovl_o_dir")
        use_rng = c[1].checkbox("Check an expected price range", value=False, key="ovl_o_use")
        lo, hi = c[1].slider("Expected price range", float(round(spot * 0.8)), float(round(spot * 1.2)),
                             (float(round(spot * 0.95)), float(round(spot * 1.05))), key="ovl_o_rng",
                             help="Where you think the price will be at expiry.")
        hold = c[2].number_input("Holding days", 0, 365, 0, key="ovl_o_hold", help="0 = not set.")
        vo = c[3].selectbox("Volatility outlook", ["Not set", "Rising", "Falling", "Flat"], key="ovl_o_vol")
    outlook = {"direction": d if d != "Not set" else None, "range": (lo, hi) if use_rng else (None, None),
               "holding_days": hold or None,
               "volatility": vo if vo != "Not set" else None}
    a = analyze(legs, spot, rate, dte, iv, p, symbol, close, atr_val, th, outlook, quotes, name)
    f = a["fit"]
    m = st.columns(5)
    m[0].metric("Defined risk", "Yes" if a["defined"] else "No (unlimited)", help="Maximum loss is finite and calculable.")
    m[1].metric("Maximum loss", ui.fmt(a["max_loss"], "money0") if a["max_loss"] is not None else "Unlimited")
    m[2].metric("Maximum profit", ui.fmt(a["max_profit"], "money0") if a["max_profit"] is not None else "Unlimited")
    m[3].metric("Breakevens", ", ".join(f"{b:,.2f}" for b in a["breakevens"]["breakeven"]) if not a["breakevens"].empty else "—")
    m[4].metric("Spread width", ui.fmt(a["width"], "money") if a["width"] else "—")
    chips([(f.status, f"{STATUSES[f.status].icon} Account: {f.classification.capitalize()}")]
          + [(RISK_CODE.get(k, "INFO"), f"{STATUSES[RISK_CODE.get(k, 'INFO')].icon} {t}") for k, t in a["risks"]])
    if f.reasons:
        st.markdown(md("**Account limits:** " + "; ".join(f.reasons) + "."))
    st.caption(md(f"Capital {ui.fmt(f.capital_required, 'money')} · max loss {ui.fmt(f.max_loss, 'money')} "
                  f"({ui.fmt(f.account_risk_pct, 'pts') if f.account_risk_pct is not None else '—'} of the account) · "
                  f"buying power left {ui.fmt(f.buying_power_remaining, 'money')} · assignment obligation "
                  f"{ui.fmt(f.assignment_obligation, 'money0')}. {f.formula}."))
    t1, t2, t3, t4 = st.tabs(["Breakevens and outlook", "Legs and quotes", "Stress scenarios", "Assignment and expiry"])
    with t1:
        if not a["breakevens"].empty:
            ui.show(a["breakevens"], formats={"breakeven": "money", "distance_pct": "spts", "vs_expected_move": "ratio",
                                              "atr_multiples": "ratio", "hist_reach_pct": "pts"},
                    rename={"distance_pct": "Distance from price", "vs_expected_move": "× expected move",
                            "atr_multiples": "× ATR move", "hist_reach_pct": f"Past {dte}-day moves this large"},
                    help={"vs_expected_move": f"Expected move = price × IV × √(DTE/365) = {a['expected_move']:,.2f}.",
                          "atr_multiples": "ATR(14) × √DTE." if a["atr_move"] else "No ATR available.",
                          "hist_reach_pct": "Share of past windows of this length (daily closes) whose absolute move "
                                            "reached the breakeven distance. A historical frequency, not a forecast."},
                    width="stretch")
        if not a["forecast"].empty:
            fc = a["forecast"].assign(status=lambda x: x["status"].str.capitalize())
            ui.show(fc, styler=ui.status_styler(["Status"], ui.STATUS_COLORS), width="stretch")
        else:
            st.caption("Set an outlook above to compare it with the structure.")
    with t2:
        ui.show(a["legs"], formats={"strike": "money", "dte": "int", "model_price": "money", "quantity": "int"},
                rename={"model_price": "Model price"}, width="stretch")
        if not a["removal"].empty:
            st.markdown("**Effect of removing each leg**")
            ui.show(a["removal"], formats={"max_loss_after": "money0"},
                    rename={"max_loss_after": "Maximum loss after", "undefined_after": "Undefined risk after"}, width="stretch")
        if not a["quotes"].empty:
            st.markdown("**Model vs the stored chain's nearest quotes**")
            q = a["quotes"].assign(liquidity=a["quotes"]["liquidity"].map(liq_badge))
            ui.show(q, formats={"bid": "money", "ask": "money", "model": "money", "model_vs_mid_pct": "spts"},
                    rename={"model_vs_mid_pct": "Model vs mid"}, styler=lambda s: _cell_colors(s, ["Liquidity"]),
                    width="stretch")
        else:
            st.caption("No stored chain for this symbol, so leg quote quality can't be checked here (use Live › Option chain).")
    with t3:
        s = stress(legs, spot, rate, dte, iv, atr_val, p, a["max_loss"], a["breakevens"]["breakeven"].tolist()
                   if not a["breakevens"].empty else [])
        lim = float(abs(s["pnl"]).max() or 1)
        ui.show(s, formats={"price": "money", "days_passed": "int", "pnl": "smoney0", "pct_of_account": "spts",
                            "account_after": "money0", "delta": "num1", "gamma": "num3", "theta": "signed"},
                rename={"pnl": "P&L ($)", "pct_of_account": "% of account", "account_after": "Account after",
                        "crosses_breakeven": "Crosses a breakeven"},
                gradient={"pnl": (-lim, lim)},
                help={"pnl": "Model value after the scenario minus the entry cost, one unit.",
                      "pct_of_account": "P&L ÷ total buying power of the account profile."}, width="stretch")
        gp = greek_path(legs, spot, rate, dte)
        st.caption("Theta per day " + " → ".join(f"{v['theta']:+,.0f} with {k} days left" for k, v in gp.items())
                   + " · gamma " + " → ".join(f"{v['gamma']:+,.2f}" for v in gp.values())
                   + ". Rising theta and gamma near expiry mean faster decay and bigger swings.")
    with t4:
        lines = assignment_scenario(legs, spot, p)
        st.markdown(md("\n".join(f"- {x}" for x in lines)) if lines else "No short legs: nothing can be assigned.")
        st.caption("Assignment can happen before expiry on American-style options; don't assume the broker will close or "
                   "exercise the protective leg for you. No dividend calendar is loaded, so ex-dividend call assignment "
                   "risk isn't checked.")


@st.cache_data(show_spinner="Running backtest diagnostics...", ttl=600)
def _diag(px: pd.Series, params_dict: dict, th_json: str) -> dict:
    from optlab.backtest import BTParams

    return diagnostics(px, BTParams(**params_dict), json.loads(th_json))


def backtest_panel(px, params, cfg) -> None:
    st.markdown("### 🧭 Overlay: backtest diagnostics",
                help="Fixed checks on whether the result above is supported: sample size, stability, out-of-sample, "
                     "costs, neighbouring settings and known biases.")
    d = _diag(px, dict(params.__dict__), json.dumps(cfg.thresholds))
    _status_items(d["items"])
    c1, c2 = st.columns(2)
    if not d["is_oos"].empty:
        c1.markdown("**In-sample vs out-of-sample**")
        with c1:
            ui.show(d["is_oos"][["period", "total_return_%", "annual_return_%", "sharpe", "max_drawdown_%"]],
                    formats={"total_return_%": "spts", "annual_return_%": "spts", "sharpe": "num", "max_drawdown_%": "spts"},
                    width="stretch")
    if not d["robust"].empty:
        c2.markdown("**Neighbouring settings**")
        with c2:
            ui.show(d["robust"], formats={"sharpe": "num", "annual_return_%": "spts", "max_drawdown_%": "spts"},
                    width="stretch")
    with st.expander("By year and by regime"):
        ui.show(d["years"], formats={"count": "int", "sum": "signed", "year": "text"},
                rename={"count": "Trades", "sum": "P&L ($/share)"}, width="stretch")
        ui.show(d["regimes"], formats={"count": "int", "mean": "spts", "median": "spts"},
                rename={"count": "Trades", "mean": "Mean return", "median": "Median return"}, width="stretch")


def history_panel(sc: OverlayScan, cfg, snapshot_dates=()) -> None:
    """Historical evidence for any overlay rule on any symbol, from the daily bars already scanned."""
    res = [r for r in sc.results if r.rule_id != "conflict" and r.states is not None]
    if not res:
        st.caption("No overlay results to analyse.")
        return
    rules = {r.id: r for r in cfg.rules}
    hl = [r for r in sc.highlights() if r.states is not None]
    default = hl[0] if hl else res[0]
    c = st.columns([2, 1, 1])
    rid = c[0].selectbox("Overlay rule", sorted({r.rule_id for r in res}), key="ovl_h_rule",
                         index=sorted({r.rule_id for r in res}).index(default.rule_id),
                         format_func=lambda k: rules[k].name if k in rules else k)
    syms = sorted({r.symbol for r in res if r.rule_id == rid})
    sym = c[1].selectbox("Symbol", syms, key="ovl_h_sym", index=syms.index(default.symbol) if default.symbol in syms else 0)
    hz = c[2].selectbox("Horizon (bars)", [1, 3, 5, 10, 20], index=2, key="ovl_h_hz")
    r = next(x for x in res if x.rule_id == rid and x.symbol == sym)
    from optlab.overlay.quality import blocked

    blk = blocked([ch for ch in sc.checks if ch.symbol == sym], "historical evidence")
    ev = rule_evidence(r, rules[rid], sc.bars[sym], sc.bars.get(cfg.market["benchmark"]), cfg, hz,
                  blocked_reason=f"Blocked: {blk[0].detail} {blk[0].action}" if blk else None)
    chips([(r.status, f"Today: {badge(r.status)} (score {r.score:.0f})"), (ev.status, f"{STATUSES[ev.status].icon} "
           f"{ev.exact:,} past exact and {ev.near:,} near matches")])
    for n in ev.notes:
        chips([("LOW_SAMPLE_SIZE" if "Low sample" in n else "DATA_ERROR", n)])
    if ev.status == "DATA_ERROR":
        return
    st.caption(f"Events are new confirmations (exact) and new near confirmations; returns are signed for the rule's "
               f"direction ({rules[rid].direction}). Descriptive statistics of past bars, not predictions.")
    for title, sm in (("Exact matches", ev.summary_exact), ("Near matches", ev.summary_near)):
        if sm.empty:
            continue
        st.markdown(f"**{title}: what price did afterwards**")
        view = sm.assign(stable=sm["stable"].map({True: "Yes", False: "No"}),
                         low_sample=sm["low_sample"].map({True: "🔬 Low sample", False: "—"}))
        ui.show(view, formats={"events": "int", "incomplete": "int", "positive_pct": "pts", "median_pct": "spts",
                               "mean_pct": "spts", "favorable_pct": "spts", "adverse_pct": "spts",
                               "favorable_first_pct": "pts", "bars_to_favorable": "num1", "bars_to_adverse": "num1",
                               "period_medians": "text"},
                rename={"horizon": "Horizon (bars)", "positive_pct": "Worked", "median_pct": "Median",
                        "mean_pct": "Mean", "favorable_pct": "Avg favorable excursion", "adverse_pct": "Avg adverse excursion",
                        "favorable_first_pct": "Favorable came first", "bars_to_favorable": "Bars to favorable (median)",
                        "bars_to_adverse": "Bars to adverse (median)", "period_medians": "Medians by third of history",
                        "stable": "Stable", "low_sample": "Sample"},
                help={"positive_pct": "Share of events whose return (in the rule's direction) was positive.",
                      "stable": "All three time-period medians have the same sign."},
                styler=lambda s: _cell_colors(s, ["Sample"]), width="stretch")
    t1, t2, t3, t4 = st.tabs(["Regimes", "Nearest analogues", "Weak-outcome descriptors", "Threshold sensitivity"])
    with t1:
        if ev.regimes.empty:
            st.caption("No exact matches to split.")
        else:
            ui.show(ev.regimes.assign(low_sample=ev.regimes["low_sample"].map({True: "🔬 Low sample", False: "—"})),
                    formats={"events": "int", "positive_pct": "pts", "median_pct": "spts"},
                    rename={"positive_pct": "Worked", "median_pct": f"Median {hz}-bar return", "low_sample": "Sample"},
                    styler=lambda s: _cell_colors(s, ["Sample"]), width="stretch")
    with t2:
        feats = cfg.raw["similarity"]["features"]
        st.caption("similarity = 100 × max(0, 1 − Σ(weight × |today − then| ÷ scale) ÷ Σ weight). Scales and weights: "
                   + ", ".join(f"{k} (scale {v['scale']}, weight {v.get('weight', 1)})" for k, v in feats.items()) + ".")
        if not ev.analogues.empty:
            ui.show(ev.analogues, formats={"similarity": "num1", f"return_{hz}_pct": "spts", "date": "date"},
                    rename={f"return_{hz}_pct": f"{hz}-bar return after"}, width="stretch")
        if not ev.current_vs_history.empty:
            st.markdown("**Today's values vs past exact matches**")
            ui.show(ev.current_vs_history, formats={"current": "num", "exact_median": "num", "percentile_among_exact": "num1"},
                    rename={"exact_median": "Median at exact matches", "percentile_among_exact": "Percentile among them"},
                    width="stretch")
    with t3:
        if ev.clusters.empty:
            st.caption("Needs at least 8 exact matches.")
        else:
            st.caption("How often each measurable descriptor was present at the weakest quarter of outcomes vs all "
                       "exact matches. A frequency comparison, not a cause.")
            ui.show(ev.clusters, formats={"in_weak_outcomes_pct": "pts", "in_all_events_pct": "pts", "difference_pts": "spts"},
                    rename={"in_weak_outcomes_pct": "In weak outcomes", "in_all_events_pct": "In all events",
                            "difference_pts": "Difference (points)"}, width="stretch")
    with t4:
        if ev.sensitivity.empty:
            st.caption("This rule has no numeric comparison thresholds to vary.")
        else:
            ui.show(ev.sensitivity, formats={"threshold": "num", "tested": "num", "events": "int", "base_events": "int",
                                             "change_pct": "spts"},
                    rename={"tested": "Tested value", "base_events": "Events at the configured value",
                            "change_pct": "Change in events", "sensitive": "Sensitive"}, width="stretch")
    st.info(options_history_status(list(ev.per_event["time"]) if not ev.per_event.empty else [], snapshot_dates)[1])


# ------------------------------------------------------------------ phase 5: confluence, audit, settings
from optlab.overlay import audit as oaudit  # noqa: E402
from optlab.overlay import confluence as cf  # noqa: E402
from optlab.overlay.config import THRESHOLD_LIMITS  # noqa: E402

TEMPLATE_NAMES = {"bullish_continuation": "Bullish continuation", "bearish_rejection": "Bearish rejection",
                  "compression_expansion": "Compression → expansion", "historical_analogue": "Historical analogue",
                  "waiting": "Waiting for confirmation"}


def confluence_chips(table: pd.DataFrame) -> list:
    if table is None or table.empty:
        return []
    out = []
    for key in ("bullish_continuation", "bearish_rejection", "compression_expansion", "historical_analogue"):
        syms = table.loc[table[key] == "CONFIRMED", "symbol"].tolist()
        if syms:
            out.append(("CONFIRMED", f"🧩 {TEMPLATE_NAMES[key]}: {', '.join(syms)}"))
    return out


def confluence_panel(sc: OverlayScan, table: pd.DataFrame) -> None:
    cfg = sc.config
    st.markdown("### 🧩 Overlay: confluence",
                help="Independent states (market, sector, technical rules, history, data quality) combined by fixed "
                     "templates. Account fit and event risk stay separate statuses and never raise a score.")
    if table.empty:
        st.caption("No symbols to combine.")
        return
    view = table[["symbol"] + list(TEMPLATE_NAMES)].copy()
    for k in TEMPLATE_NAMES:
        view[k] = view[k].map(badge)
    rev = {badge(c): c for c in STATUSES}
    ui.show(view, rename=TEMPLATE_NAMES, width="stretch",
            styler=lambda s: s.map(lambda v: css(rev[v]) if v in rev else "", subset=list(TEMPLATE_NAMES.values())))
    c = st.columns([1, 1, 2])
    sym = c[0].selectbox("Symbol", table["symbol"].tolist(), key="ovl_cf_sym")
    d = c[1].radio("Direction", ["Bullish", "Bearish"], horizontal=True, key="ovl_cf_dir").lower()
    ctx = cf.symbol_context(sc, sym)
    comp = cf.components(ctx, d)
    total, contrib = cf.composite(comp, cfg.raw["confluence"]["weights"], ctx["blocked"])
    rows = pd.DataFrame([{"component": k.replace("_", " ").capitalize(), "score": v,
                          "weight": cfg.raw["confluence"]["weights"].get(k, 0),
                          "contribution": contrib.get(k)} for k, v in comp.items()])
    with c[2]:
        ui.show(rows, formats={"score": "num1", "weight": "num1", "contribution": "num1"},
                help={"score": "0-100; '—' when the data can't support it (never guessed).",
                      "contribution": "weight × score ÷ Σ weights of the available components."}, width="stretch")
        if cfg.raw["confluence"].get("show_composite"):
            st.metric("Composite (optional)", "—" if total is None else f"{total:.0f}",
                      help="Σ(weight × score) ÷ Σ(weight) over available components. Withheld when a data check blocks "
                           "the symbol. Edit the weights in Research › Overlay setup → Settings.")
        else:
            st.caption("The composite score is off (Research › Overlay setup → Settings → show composite). Components are always shown.")
    t = cf.templates(ctx, cfg.thresholds)
    for key, (name, verdict, items) in t.items():
        with st.expander(f"{STATUSES[verdict].icon} {name}: {VERDICT_TEXT.get(verdict, verdict)}", expanded=key == "waiting" and bool(items)):
            if key == "waiting":
                st.markdown(md("\n".join(f"- {x}" for x in items)) if items else "No near confirmations right now.")
                continue
            df = pd.DataFrame(items).assign(status=lambda x: x["status"].map({"pass": "Pass", "fail": "Fail", "n/a": "N/A"}))
            ui.show(df, rename={"item": "Condition", "value": "Observed"}, styler=ui.status_styler(["Status"], ui.STATUS_COLORS),
                    width="stretch")


def audit_panel() -> None:
    df = oaudit.read()
    st.caption(f"Append-only log of state changes at {oaudit.AUDIT_PATH.relative_to(oaudit.AUDIT_PATH.parents[2])}. "
               "Each record has the rule, configuration and indicator versions, data source and timestamp, inputs, "
               "outputs, formula and reason code. No credentials are written.")
    if df.empty:
        st.info("No audit records yet: they are written when the overlay scans.")
        return
    c = st.columns(3)
    syms = c[0].multiselect("Symbols", sorted(df["symbol"].unique()), key="ovl_au_s", placeholder="All")
    rules = c[1].multiselect("Rules", sorted(df["rule_name"].unique()), key="ovl_au_r", placeholder="All")
    stt = c[2].multiselect("Statuses", sorted(df["status"].unique()), key="ovl_au_st", placeholder="All",
                           format_func=lambda x: badge(x) if x in STATUSES else x)
    v = df
    if syms:
        v = v[v["symbol"].isin(syms)]
    if rules:
        v = v[v["rule_name"].isin(rules)]
    if stt:
        v = v[v["status"].isin(stt)]
    v = v.iloc[::-1].reset_index(drop=True)
    show = v.assign(status=v["status"].map(lambda x: badge(x) if x in STATUSES else x),
                    config=v["versions"].map(lambda d: d.get("config")), rule_version=v["versions"].map(lambda d: d.get("rule")))
    ui.show(show[["scan_time", "symbol", "rule_name", "bar", "status", "score", "reason_code", "config", "rule_version"]],
            formats={"score": "num1", "scan_time": "text", "bar": "text"}, styler=status_cells, width="stretch", height=320)
    if len(v):
        i = st.number_input("Record to inspect (row number above, 0 = newest)", 0, len(v) - 1, 0, key="ovl_au_i")
        rec = {k: (None if isinstance(x, float) and x != x else x) for k, x in v.iloc[int(i)].to_dict().items()}
        st.code(json.dumps(rec, indent=1, default=str, ensure_ascii=False), language="json")


def settings_panel(cfg, save_fn, path) -> None:
    """Edit thresholds, the account profile, labels, tab switches and confluence weights. Saved to overlay.yaml
    only after validation."""
    th, ap, raw = cfg.thresholds, cfg.account, cfg.raw
    with st.form("ovl_settings"):
        st.markdown("**General**")
        g = st.columns(4)
        enabled = g[0].checkbox("Overlay enabled", raw["enabled"])
        compact = g[1].checkbox("Compact console", raw["compact"])
        completed = g[2].checkbox("Completed bars only", raw["completed_bars_only"])
        items = g[3].number_input("Console highlights", 1, 50, int(raw["console_items"]))
        st.markdown("**Thresholds**")
        new_th = {}
        keys = list(th)
        cols = st.columns(4)
        for n, k in enumerate(keys):
            v = th[k]
            whole_ = THRESHOLD_LIMITS[k][2] or float(v).is_integer() and float(v) >= 5
            new_th[k] = cols[n % 4].number_input(ui.label(k), value=float(v), step=1.0 if float(v) >= 5 else 0.05,
                                                 format="%.0f" if whole_ else "%.2f", key=f"ovl_set_{k}")
        st.markdown("**Account profile (Small Account Mode)**")
        cols = st.columns(4)
        new_ap = {}
        nums = ["total_buying_power", "settled_cash", "reserve_cash", "max_capital_per_idea", "max_loss_per_idea",
                "max_account_risk_pct", "max_contracts", "contract_multiplier"]
        for n, k in enumerate(nums):
            new_ap[k] = cols[n % 4].number_input(ui.label(k), value=float(ap[k]), step=1.0, format="%.0f",
                                                 key=f"ovl_ap_{k}")
        flags = ["small_account_mode", "defined_risk_only", "naked_options_allowed", "unlimited_risk_allowed",
                 "require_assignment_capacity"]
        cols = st.columns(5)
        for n, k in enumerate(flags):
            new_ap[k] = cols[n].checkbox(k.replace("_", " ").capitalize(), bool(ap[k]), key=f"ovl_apf_{k}")
        new_ap["account_type"] = st.radio("Account type", ["cash", "margin"], index=0 if ap["account_type"] == "cash" else 1,
                                          horizontal=True, format_func=str.capitalize)
        st.markdown("**Views with an overlay panel**")
        cols = st.columns(4)
        tabs = {k: cols[n % 4].checkbox(PANEL_NAMES.get(k, k), bool(v), key=f"ovl_tab_{k}")
                for n, (k, v) in enumerate(raw["tabs"].items())}
        st.markdown("**Confluence**")
        cols = st.columns(7)
        show_comp = cols[0].checkbox("Show composite", bool(raw["confluence"].get("show_composite")))
        weights = {k: cols[n + 1].number_input(k.replace("_", " ").capitalize(), value=float(v), step=1.0, key=f"ovl_w_{k}")
                   for n, (k, v) in enumerate(raw["confluence"]["weights"].items())}
        st.markdown("**Display labels** (rename a status without changing its meaning; blank = default)")
        cols = st.columns(4)
        labels = {}
        for n, code in enumerate(STATUSES):
            val = cols[n % 4].text_input(STATUSES[code].label, value=cfg.labels.get(code, ""), key=f"ovl_lbl_{code}")
            if val.strip():
                labels[code] = val.strip()
        save = st.form_submit_button("Save to overlay.yaml", type="primary")
    if save:
        whole = {k for k, (_, _, w) in THRESHOLD_LIMITS.items() if w}
        new_th = {k: int(v) if k in whole else v for k, v in new_th.items()}
        new_ap["max_contracts"], new_ap["contract_multiplier"] = int(new_ap["max_contracts"]), int(new_ap["contract_multiplier"])
        change = {"enabled": enabled, "compact": compact, "completed_bars_only": completed, "console_items": int(items),
                  "thresholds": new_th, "account_profile": new_ap, "tabs": tabs, "labels": labels,
                  "confluence": {"show_composite": show_comp, "weights": weights}}
        try:
            p = save_fn(change)
            st.success(f"Saved to {p.name}. The overlay rescans with the new settings.")
            st.cache_data.clear()
            st.rerun()
        except Exception as exc:
            problems = getattr(exc, "problems", [str(exc)])
            st.error("Not saved; fix these first:\n\n" + "\n".join(f"- {x}" for x in problems))
    if path.exists():
        with st.expander("Reset to the built-in defaults"):
            sure = st.checkbox("I want to delete my overlay.yaml settings", key="ovl_reset_ok")
            if st.button("Delete overlay.yaml", disabled=not sure, key="ovl_reset"):
                path.unlink()
                st.cache_data.clear()
                st.rerun()
