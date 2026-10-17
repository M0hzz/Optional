"""Dashboard pieces of the deterministic overlay: the console that sits above every tab, the
per-tab panels and the evidence view. Everything shown comes from optlab.overlay: fixed rules,
fixed formulas and fixed sentence templates. No AI is involved anywhere."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import ui
from optlab.overlay.engine import TRANSITION_TEXT, OverlayScan, results_frame
from optlab.overlay.rules import RuleResult
from optlab.overlay.status import STATUSES, badge, chip_html, css

HIGHLIGHT_STATUSES = ["CONFIRMED", "NEAR_CONFIRMATION", "INVALIDATED", "OVEREXTENDED", "STALE", "CONFLICTING",
                      "INSUFFICIENT_DATA", "DATA_ERROR", "WATCH"]
SEVERITY = {0: "Information and up", 1: "Caution and up", 2: "Serious and up", 3: "Blocking only"}


def chips(items: list[tuple[str, str | None]]) -> None:
    st.markdown("".join(chip_html(code, text) for code, text in items), unsafe_allow_html=True)


def sidebar_controls(cfg) -> dict:
    """Overlay switches and filters (session only; persistent settings live on the Overlay tab)."""
    st.markdown("**Overlay**", help="Rules-based highlights across every tab. Fixed formulas and thresholds from "
                                   "overlay.yaml; no AI or machine learning. Change thresholds on the Overlay tab.")
    on = st.toggle("Show overlay", value=cfg.enabled, key="ovl_on",
                   help="Turns the console and every tab's overlay panel on or off for this session.")
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
        return "Overlay tab → Data health"
    return "Scanner tab → Overlay table" + (" · Signal explorer for history" if r.rule_id != "conflict" else "")


def console(sc: OverlayScan, f: dict, extra_chips: list | None = None) -> None:
    """The always-visible strip above the tabs: status summary, the things worth looking at, evidence."""
    cfg = sc.config
    with st.container(border=True):
        top = st.columns([3, 2])
        top[0].markdown("#### 🧭 Mastermind console", help="Deterministic highlights: every item comes from a rule in "
                        "overlay.yaml evaluated on the latest completed bar. No AI, no predictions, no trading.")
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
        box.markdown("".join(lines), unsafe_allow_html=True)
        if hl:
            with st.expander("Evidence for these highlights"):
                pick = st.selectbox("Highlight", range(len(hl)), key="ovl_ev_pick",
                                    format_func=lambda i: f"{hl[i].symbol} · {hl[i].rule_name} · {badge(hl[i].status, cfg.labels)}",
                                    help="Pick a highlight to see every condition, the formula and the versions behind it.")
                evidence(hl[pick], sc)


def evidence(r: RuleResult, sc: OverlayScan | None = None) -> None:
    """Condition checklist plus the audit record of one result."""
    st.markdown(f"**{badge(r.status)}** · {r.symbol} · {r.rule_name} · {r.timeframe} · bar {r.data_timestamp or '—'}")
    st.write(r.explanation)
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
        st.markdown("**Missing confirmation:** " + "; ".join(r.missing))
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
    st.markdown("\n".join(f"- **{k}:** {v}" for k, v in facts.items()))
    with st.popover("Audit record (JSON)", help="Exactly what was calculated: inputs, outputs, versions."):
        st.code(json.dumps(r.as_dict(), indent=1, default=str)[:20000], language="json")


def status_cells(sty, col="Status"):
    rev = {badge(c): c for c in STATUSES}
    return sty.map(lambda v: css(rev[v]) if v in rev else "", subset=[col]) if col in sty.data.columns else sty


def scanner_panel(sc: OverlayScan) -> None:
    """Every rule on every symbol, with status, score, freshness and what's missing."""
    st.markdown("### 🧭 Overlay: rule status by symbol",
                help="Each row is one rule from overlay.yaml evaluated on the symbol's latest completed daily bar.")
    res = sc.for_tab("scanner")
    if not res:
        st.caption("No overlay rules apply to this tab.")
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
            help={"status": "Overlay status. Hover the Overlay tab's status guide for definitions.",
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
        evidence(pool[i], sc)


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
