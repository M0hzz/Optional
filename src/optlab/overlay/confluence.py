"""Cross-tab confluence: independent overlay states combined by fixed templates. No AI.

Component scores (0-100, or None when the data can't support them; never guessed):
    market        bullish view: Broad bullish 100, Leaning bullish 75, Mixed 50, Leaning bearish 25,
                  Broad bearish 0 (mirrored for the bearish view)
    sector        100 × (1 − (rank − 1) / (sectors − 1)) of the symbol's sector ETF by 20-day
                  relative strength (None without sector ETF data)
    technical     the highest score among the symbol's standing rules in that direction
    historical    share of past exact matches that worked at the horizon (None below the sample minimum)
    liquidity     share of the chain's quotes that pass the liquidity checks (only with a loaded chain)
    data_quality  100 − 40 per blocking and − 10 per downgrading failed data check (min 0)
Account fit and event risk are statuses, not scores, and never enter the composite.

Optional composite (confluence.show_composite):
    composite = Σ(weight_c × score_c) ÷ Σ(weight_c) over the components that have a value.
It is withheld (None) when a data check blocks the symbol, and it is always shown next to the
liquidity and data components so a high technical score can't hide either.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import ACTIVE, OverlayScan
from .history_ev import evidence
from .quality import blocked, summarize

MARKET_SCORE = {"Broad bullish alignment": 100, "Leaning bullish": 75, "Mixed": 50, "Leaning bearish": 25,
                "Broad bearish alignment": 0}
COMPONENTS = ("market", "sector", "technical", "historical", "liquidity", "data_quality")


def _item(name, ok, value, requirement) -> dict:
    return {"item": name, "status": "n/a" if ok is None else ("pass" if ok else "fail"), "value": value,
            "requirement": requirement}


def _verdict(items: list[dict], minimum: int = 3) -> str:
    avail = [i for i in items if i["status"] != "n/a"]
    if len(avail) < minimum:
        return "INSUFFICIENT_DATA"
    fails = sum(i["status"] == "fail" for i in avail)
    return "CONFIRMED" if fails == 0 else "NEAR_CONFIRMATION" if fails == 1 else "NOT_PRESENT"


def symbol_context(sc: OverlayScan, symbol: str, history_horizon: int = 5, with_history: bool = True) -> dict:
    cfg, th = sc.config, sc.config.thresholds
    bars = sc.bars.get(symbol)
    res = {r.rule_id: r for r in sc.results if r.symbol == symbol and r.rule_id != "conflict"}
    checks = [c for c in sc.checks if c.symbol == symbol]
    ctx = {"symbol": symbol, "results": res, "regime": (sc.market or {}).get("regime"), "checks": checks,
           "data": summarize(checks), "blocked": bool(blocked(checks, "technical rules")), "hist": {}}
    if bars is not None and len(bars) > 25:
        c, o, v = bars["close"], bars["open"], bars["volume"]
        tp = (bars["high"] + bars["low"] + c) / 3
        rv = (tp * v).rolling(20).sum() / v.rolling(20).sum()
        bench = sc.bars.get(cfg.market["benchmark"])
        rs = None
        if bench is not None:
            b = bench["close"].reindex(bars.index)
            rs = float(100 * (c.iloc[-1] / c.iloc[-21] - 1) - 100 * (b.iloc[-1] / b.iloc[-21] - 1)) if len(c) > 21 else None
            rs = rs if rs is not None and np.isfinite(rs) else None
        ctx.update({"rs20": rs, "rel_volume": float(v.iloc[-1] / v.iloc[-20:].mean()),
                    "below_vwap": bool(c.iloc[-1] < rv.iloc[-1]), "down_bar": bool(c.iloc[-1] < o.iloc[-1])})
    sec = (sc.market or {}).get("sectors") or {}
    etf = (cfg.market.get("symbol_sectors") or {}).get(symbol)
    ctx["sector"] = None
    if sec.get("available") and etf in set(sec["table"]["etf"]):
        t = sec["table"]
        rank = int(t.loc[t["etf"] == etf, "rank"].iloc[0])
        ctx["sector"] = {"etf": etf, "rank": rank, "n": len(t), "rs20": float(t.loc[t["etf"] == etf, "rs20"].iloc[0])}
    if with_history and bars is not None and not blocked(checks, "historical evidence"):
        rules = {r.id: r for r in cfg.rules}
        for d in ("bullish", "bearish"):
            cand = [r for r in res.values() if r.direction == d and r.states is not None and r.rule_id in rules]
            if cand:
                best = max(cand, key=lambda r: (r.status in ACTIVE, r.score))
                ev = evidence(best, rules[best.rule_id], bars, sc.bars.get(cfg.market["benchmark"]), cfg,
                              history_horizon, with_sensitivity=False)
                row = ev.summary_exact[ev.summary_exact["horizon"] == history_horizon] if not ev.summary_exact.empty else None
                n = int(row["events"].iloc[0]) if row is not None and len(row) else 0
                worked = float(row["positive_pct"].iloc[0]) if n else float("nan")
                top_sim = float(ev.analogues["similarity"].max()) if not ev.analogues.empty else float("nan")
                ctx["hist"][d] = {"rule": best.rule_name, "n": n, "worked": worked, "enough": n >= int(th["minimum_historical_samples"]),
                                  "similarity": top_sim}
    return ctx


def components(ctx: dict, direction: str, liquidity_pct: float | None = None) -> dict:
    m = MARKET_SCORE.get(ctx.get("regime"))
    market = None if m is None else (m if direction == "bullish" else 100 - m)
    sec = ctx.get("sector")
    sector = None
    if sec and sec["n"] > 1:
        s = 100 * (1 - (sec["rank"] - 1) / (sec["n"] - 1))
        sector = s if direction == "bullish" else 100 - s
    tech = [r.score for r in ctx["results"].values() if r.direction == direction and r.status in ACTIVE + ("NEAR_CONFIRMATION",)]
    h = ctx["hist"].get(direction)
    return {"market": market, "sector": sector, "technical": max(tech) if tech else 0.0,
            "historical": h["worked"] if h and h["enough"] else None, "liquidity": liquidity_pct,
            "data_quality": float(ctx["data"]["score"])}


def composite(comp: dict, weights: dict, blocked_: bool) -> tuple[float | None, dict]:
    """Weighted mean of the available components and each one's contribution (points of the total)."""
    if blocked_:
        return None, {}
    avail = {k: v for k, v in comp.items() if v is not None and np.isfinite(v)}
    wsum = sum(float(weights.get(k, 0)) for k in avail)
    if not wsum:
        return None, {}
    contrib = {k: float(weights.get(k, 0)) * v / wsum for k, v in avail.items()}
    return sum(contrib.values()), contrib


def templates(ctx: dict, th: dict) -> dict:
    """Checklists A-D and F. Template E (affordable and liquid structure) needs a loaded chain:
    see structure_template."""
    res = ctx["results"]
    standing = lambda rid: (res[rid].status in ACTIVE) if rid in res else None  # noqa: E731
    fresh_conf = any(r.direction == "bullish" and r.base_status == "CONFIRMED" and (r.bars_since or 0) <= 1
                     for r in res.values())
    regime = ctx.get("regime")
    rs = ctx.get("rs20")
    hb, hs = ctx["hist"].get("bullish"), ctx["hist"].get("bearish")
    data_ok = not ctx["blocked"] and not ctx["data"]["downgrading"]
    sec = ctx.get("sector")
    rv = ctx.get("rel_volume")
    A = [_item("Supportive market regime", None if regime is None else regime in ("Broad bullish alignment", "Leaning bullish"),
               regime or "unavailable", "broad bullish or leaning bullish"),
         _item("Strong sector", None if not sec else sec["rs20"] > 0, f"{sec['etf']} {sec['rs20']:+.1f} pts" if sec
               else "no sector ETF data", "sector beats the benchmark"),
         _item("Positive relative strength", None if rs is None else rs > 0, f"{rs:+.1f} pts vs benchmark" if rs is not None
               else "—", "> 0 over 20 bars"),
         _item("Trend pullback holds support", standing("trend_pullback"), res["trend_pullback"].status.replace("_", " ").lower()
               if "trend_pullback" in res else "rule not configured", "trend_pullback standing"),
         _item("Fresh confirmation (new or near → confirmed)", fresh_conf, "within the last bar" if fresh_conf else "none",
               "a bullish rule confirmed ≤ 1 bar ago"),
         _item("Volume confirms", None if rv is None else rv >= 1.5, f"relative volume {rv:.2f}" if rv else "—", "≥ 1.50"),
         _item("Historical sample passes", None if not hb else hb["enough"] and hb["worked"] >= 50,
               f"{hb['n']} exact matches of {hb['rule']}, {hb['worked']:.0f}% worked" if hb and hb["n"] else "no history",
               f"≥ {th['minimum_historical_samples']} matches and ≥ 50% worked"),
         _item("Data quality passes", data_ok, "no failed checks" if data_ok else "failed checks present", "no blocking or downgrading check"),
         _item("Defined-risk structure fits the account", None, "checked on the Option chain tab", "optional")]
    B = [_item("Weak market", None if regime is None else regime in ("Broad bearish alignment", "Leaning bearish"),
               regime or "unavailable", "broad bearish or leaning bearish"),
         _item("Lost relative strength", None if rs is None else rs < 0, f"{rs:+.1f} pts" if rs is not None else "—", "< 0"),
         _item("Rejection at resistance", standing("bearish_rejection"), res["bearish_rejection"].status.replace("_", " ").lower()
               if "bearish_rejection" in res else "rule not configured", "bearish_rejection standing"),
         _item("Below the 20-day VWAP", ctx.get("below_vwap"), "below" if ctx.get("below_vwap") else "above", "close below"),
         _item("Selling volume expands", None if rv is None else rv >= 1.3 and ctx.get("down_bar"),
               f"relative volume {rv:.2f}, {'down' if ctx.get('down_bar') else 'up'} bar" if rv else "—", "≥ 1.30 on a down bar"),
         _item("Historical evidence passes", None if not hs else hs["enough"] and hs["worked"] >= 50,
               f"{hs['n']} exact matches of {hs['rule']}, {hs['worked']:.0f}% worked" if hs and hs["n"] else "no history",
               f"≥ {th['minimum_historical_samples']} matches and ≥ 50% worked"),
         _item("Data quality passes", data_ok, "no failed checks" if data_ok else "failed checks present", "none failing")]
    comp_rule, exit_rule = res.get("compression"), res.get("compression_exit")
    was_comp = None
    if comp_rule is not None and comp_rule.states is not None:
        was_comp = bool((comp_rule.states[-11:-1] == "CONFIRMED").any())
    C = [_item("Compressed within the last 10 bars", was_comp, "yes" if was_comp else "no" if was_comp is not None else "—",
               "compression rule confirmed"),
         _item("Expansion confirmed", None if exit_rule is None else exit_rule.status in ACTIVE,
               exit_rule.status.replace("_", " ").lower() if exit_rule else "rule not configured", "compression_exit standing"),
         _item("Relative volume expands", None if rv is None else rv >= 1.3, f"{rv:.2f}" if rv else "—", "≥ 1.30"),
         _item("Market confirms a direction", None if regime is None else regime != "Mixed", regime or "—", "not mixed")]
    best = max(res.values(), key=lambda r: r.score, default=None)
    hbest = hb if hb and (not hs or hb["n"] >= hs["n"]) else hs
    D = [_item("Current condition score", None if best is None else best.score >= th["near_match_score"],
               f"{best.rule_name} {best.score:.0f}" if best else "—", f"≥ {th['near_match_score']}"),
         _item("Minimum historical sample", None if not hbest else hbest["enough"],
               f"{hbest['n']} exact matches" if hbest else "—", f"≥ {th['minimum_historical_samples']}"),
         _item("Similarity to a past match", None if not hbest or not np.isfinite(hbest["similarity"]) else hbest["similarity"] >= 70,
               f"best {hbest['similarity']:.0f}" if hbest and np.isfinite(hbest["similarity"]) else "—", "≥ 70"),
         _item("No critical data issue", not ctx["blocked"], "none" if not ctx["blocked"] else "blocked", "no blocking check")]
    near = [r for r in res.values() if r.status == "NEAR_CONFIRMATION"]
    F = []
    for r in sorted(near, key=lambda r: -r.score):
        F.append(f"NEAR CONFIRMATION ({r.rule_name}): {r.passed} of {r.n_conditions} conditions pass. Confirmation requires "
                 + "; ".join(r.missing) + ".")
    return {"bullish_continuation": ("Bullish continuation confluence", _verdict(A), A),
            "bearish_rejection": ("Bearish rejection confluence", _verdict(B), B),
            "compression_expansion": ("Compression to expansion", _verdict(C, 2), C),
            "historical_analogue": ("Historical-analogue alignment", _verdict(D), D),
            "waiting": ("Waiting for confirmation", "NEAR_CONFIRMATION" if F else "NOT_PRESENT", F)}


def structure_template(row: dict, technical_confirmed: bool, quotes_fresh: bool, th: dict) -> tuple[str, list[dict]]:
    """Template E for one scanned structure (a row from options.structures)."""
    items = [_item("Technical condition independently confirmed", technical_confirmed, str(row.get("technical", "—")),
                   "a standing rule in the structure's direction"),
             _item("Defined maximum loss", row.get("max_loss") is not None, f"${row['max_loss']:,.2f}" if row.get("max_loss")
                   else "—", "finite"),
             _item("Capital and risk limits pass", row.get("account_status") == "FITS_ACCOUNT", row.get("account_fit", "—"),
                   "fits configured limits"),
             _item("Bid/ask threshold passes", row.get("max_spread_pct", 999) <= th["maximum_bid_ask_pct"],
                   f"widest leg {row.get('max_spread_pct', float('nan')):.1f}%", f"≤ {th['maximum_bid_ask_pct']}%"),
             _item("Open interest and volume pass", row.get("min_oi", 0) >= th["minimum_open_interest"]
                   and row.get("min_volume", 0) >= th["minimum_option_volume"],
                   f"lowest OI {row.get('min_oi', 0):,.0f}, volume {row.get('min_volume', 0):,.0f}",
                   f"OI ≥ {th['minimum_open_interest']}, volume ≥ {th['minimum_option_volume']}"),
             _item("Quote freshness passes", quotes_fresh, "fresh" if quotes_fresh else "stale", "within the quote-age limit"),
             _item("Assignment warning displayed", True, f"${row.get('assignment', 0):,.0f} obligation shown", "always")]
    return _verdict(items, 4), items


def table(sc: OverlayScan, with_history: bool = True) -> pd.DataFrame:
    """One row per symbol: template verdicts and component scores (and the optional composite)."""
    w = sc.config.raw["confluence"]["weights"]
    rows = []
    for sym in sorted(sc.bars):
        ctx = symbol_context(sc, sym, with_history=with_history)
        t = templates(ctx, sc.config.thresholds)
        row = {"symbol": sym, **{k: v[1] for k, v in t.items()}}
        for d in ("bullish", "bearish"):
            comp = components(ctx, d)
            total, _ = composite(comp, w, ctx["blocked"])
            row[f"{d}_composite"] = total
            for k, v in comp.items():
                row[f"{d}_{k}"] = v
        rows.append(row)
    return pd.DataFrame(rows)
