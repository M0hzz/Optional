"""Market overview: index direction, breadth, volatility regime, sectors and the regime checklists.

Everything is computed from the stored daily bars (and the VIX series in the macro table when
present). Anything the data can't support is reported as unavailable, never estimated:
breadth from a handful of symbols carries a low-sample warning, sectors need the sector ETFs
in the database, and no economic calendar is used because no reliable source is configured.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..live.indicators import atr, bollinger, ema, sma
from .rules import trend_state


def _pct_rank_last(s: pd.Series, n: int) -> float:
    w = s.dropna().iloc[-n:]
    return float(100 * (w <= w.iloc[-1]).mean()) if len(w) >= min(n, 20) else float("nan")


def index_state(sym: str, b: pd.DataFrame) -> dict:
    c, h, lo, v = (b[k].astype(float) for k in ("close", "high", "low", "volume"))
    tp = (h + lo + c) / 3
    rvwap = (tp * v).rolling(20).sum() / v.rolling(20).sum()
    e9, e21, s50, s200 = ema(c, 9), ema(c, 21), sma(c, 50), sma(c, 200)
    a = atr(h, lo, c, 14)
    bw = (lambda u, m, l_: 100 * (u - l_) / m)(*bollinger(c, 20, 2.0))
    gap = 100 * (b["open"].iloc[-1] / c.iloc[-2] - 1) if len(b) > 1 else float("nan")
    return {"symbol": sym, "date": b.index[-1], "close": c.iloc[-1], "change_pct": 100 * (c.iloc[-1] / c.iloc[-2] - 1),
            "ret20_pct": 100 * (c.iloc[-1] / c.iloc[-21] - 1) if len(c) > 21 else float("nan"),
            "above_vwap": bool(c.iloc[-1] > rvwap.iloc[-1]) if np.isfinite(rvwap.iloc[-1]) else None,
            "ema_bullish": bool(e9.iloc[-1] > e21.iloc[-1]), "above_sma50": bool(c.iloc[-1] > s50.iloc[-1]),
            "above_sma200": bool(c.iloc[-1] > s200.iloc[-1]) if np.isfinite(s200.iloc[-1]) else None,
            "trend": trend_state(c, e21, s50), "gap_pct": gap,
            "rel_volume": float(v.iloc[-1] / v.iloc[-21:-1].mean()) if len(v) > 21 else float("nan"),
            "atr_pct_rank": _pct_rank_last(a, 252), "bb_width_pct_rank": _pct_rank_last(bw, 252),
            "bb_width_rank_5ago": _pct_rank_last(bw.iloc[:-5], 252) if len(bw) > 30 else float("nan"),
            "high_20": bool(c.iloc[-1] >= c.iloc[-20:].max()), "low_20": bool(c.iloc[-1] <= c.iloc[-20:].min()),
            "prior_high": float(h.iloc[-2]), "prior_low": float(lo.iloc[-2])}


def breadth(frames: dict[str, pd.DataFrame], ma: int, high_bars: int, exclude: set) -> dict:
    """Breadth over every non-index symbol with data (all symbols if there are no others)."""
    syms = [s for s in frames if s not in exclude and len(frames[s]) > ma] or [s for s in frames if len(frames[s]) > ma]
    if not syms:
        return {"n": 0}
    close = pd.DataFrame({s: frames[s]["close"] for s in syms}).sort_index()
    vol = pd.DataFrame({s: frames[s]["volume"] for s in syms}).reindex(close.index)
    common = close.dropna().index
    if len(common) < 2:
        return {"n": 0}
    d = common[-1]
    chg = close.diff().loc[d]
    above = (close > close.rolling(ma).mean())
    pct_above = 100 * above.where(close.notna()).mean(axis=1)
    hi = close >= close.rolling(high_bars, min_periods=min(high_bars, 60)).max()
    lo = close <= close.rolling(high_bars, min_periods=min(high_bars, 60)).min()
    adv_vol = float(vol.loc[d][chg > 0].sum())
    dec_vol = float(vol.loc[d][chg < 0].sum())
    s200 = close.rolling(200).mean()
    return {"n": len(syms), "date": d, "advancers": int((chg > 0).sum()), "decliners": int((chg < 0).sum()),
            "adv_volume": adv_vol, "dec_volume": dec_vol,
            "adv_volume_share": adv_vol / (adv_vol + dec_vol) if adv_vol + dec_vol else float("nan"),
            "pct_above_ma": float(pct_above.loc[d]),
            "pct_above_200": float(100 * (close.loc[d] > s200.loc[d]).mean()) if s200.loc[d].notna().all() else float("nan"),
            "new_highs": int(hi.loc[d].sum()), "new_lows": int(lo.loc[d].sum()), "pct_above_series": pct_above}


def sectors(frames: dict[str, pd.DataFrame], cfg_market: dict, bench: pd.DataFrame | None) -> dict:
    etfs = {k: v for k, v in (cfg_market.get("sector_etfs") or {}).items() if k in frames and len(frames[k]) > 25}
    if not etfs or bench is None:
        missing = ", ".join(list(cfg_market.get("sector_etfs") or {})[:6])
        return {"available": False, "note": f"No sector ETF data ({missing}...) in the database. Add them to config.yaml "
                                            "→ universe and run ingest to enable sector strength and rotation."}
    rows = []
    bc = bench["close"].astype(float)
    for etf, name in etfs.items():
        c = frames[etf]["close"].astype(float).reindex(bc.index)
        rs = 100 * (c / c.shift(20) - 1) - 100 * (bc / bc.shift(20) - 1)
        rows.append({"etf": etf, "sector": name, "rs20": float(rs.iloc[-1]), "rs20_5ago": float(rs.iloc[-6])})
    df = pd.DataFrame(rows)
    df["rank"] = df["rs20"].rank(ascending=False, method="min").astype(int)
    df["rank_5ago"] = df["rs20_5ago"].rank(ascending=False, method="min").astype(int)
    df["rank_change"] = df["rank_5ago"] - df["rank"]
    return {"available": True, "table": df.sort_values("rank").reset_index(drop=True)}


def _item(name: str, ok, value: str, requirement: str) -> dict:
    status = "n/a" if ok is None else ("pass" if ok else "fail")
    return {"item": name, "status": status, "value": value, "requirement": requirement}


def _verdict(items: list[dict], minimum: int = 3) -> str:
    avail = [i for i in items if i["status"] != "n/a"]
    if len(avail) < minimum:
        return "INSUFFICIENT_DATA"
    fails = sum(i["status"] == "fail" for i in avail)
    return "CONFIRMED" if fails == 0 else "NEAR_CONFIRMATION" if fails == 1 else "NOT_PRESENT"


def overview(frames: dict[str, pd.DataFrame], cfg, macro: pd.DataFrame | None = None, confirmed_bullish=None) -> dict:
    """The whole market picture plus regime checklists A-E. `confirmed_bullish` maps symbol -> True when a
    bullish rule is confirmed on it (used for sector participation)."""
    m, th = cfg.market, cfg.thresholds
    idx = {s: index_state(s, frames[s]) for s in m["indexes"] if s in frames and len(frames[s]) > 60}
    missing_idx = [s for s in m["indexes"] if s not in idx]
    br = breadth(frames, int(m["breadth_ma"]), int(m["new_high_bars"]), set(m["indexes"]) | set(m.get("sector_etfs") or {}))
    bench = frames.get(m["benchmark"])
    sec = sectors(frames, m, bench)
    vix = None
    if macro is not None and not macro.empty:
        v = macro[macro["series"] == "vix"].sort_values("date")
        if len(v) > 6:
            vix = {"level": float(v["value"].iloc[-1]), "change_5d": float(v["value"].iloc[-1] - v["value"].iloc[-6]),
                   "pct_rank": _pct_rank_last(v["value"].reset_index(drop=True), 252), "date": v["date"].iloc[-1]}
    small = br.get("n", 0) < int(th["breadth_minimum_symbols"])
    bp = br.get("pct_above_ma", float("nan"))
    ok = lambda x: None if x is None or (isinstance(x, float) and not np.isfinite(x)) else bool(x)  # noqa: E731
    all_idx = lambda key: (all(i[key] for i in idx.values()) if idx and all(i[key] is not None for i in idx.values())  # noqa: E731
                           else None)
    none_idx = lambda key: (not any(i[key] for i in idx.values()) if idx and all(i[key] is not None for i in idx.values())  # noqa: E731
                            else None)
    leaders = None
    if sec["available"]:
        leaders = int((sec["table"]["rs20"] > 0).sum())
    b_state = index_state(m["benchmark"], bench) if bench is not None and len(bench) > 60 else None
    bull = [
        _item("Indexes above 20-day VWAP", all_idx("above_vwap"), ", ".join(f"{s} {'above' if i['above_vwap'] else 'below'}"
              for s, i in idx.items()) or "no index data", "every configured index above"),
        _item("Bullish EMA alignment (9 > 21)", all_idx("ema_bullish"), ", ".join(f"{s} {'yes' if i['ema_bullish'] else 'no'}"
              for s, i in idx.items()) or "—", "every index"),
        _item(f"Breadth: % above {m['breadth_ma']}-day MA", ok(bp >= m["breadth_bullish_pct"]) if np.isfinite(bp) else None,
              f"{bp:.0f}% of {br.get('n', 0)} symbols" if np.isfinite(bp) else "—", f"≥ {m['breadth_bullish_pct']}%"),
        _item("Leading sectors participating", ok(leaders >= 2) if leaders is not None else None,
              f"{leaders} sector ETFs beating {m['benchmark']}" if leaders is not None else "sector data unavailable", "≥ 2"),
        _item("Volume above baseline", ok(b_state["rel_volume"] >= 1.0) if b_state else None,
              f"{m['benchmark']} relative volume {b_state['rel_volume']:.2f}" if b_state else "—", "≥ 1.00"),
        _item("Holding prior-day low", ok(b_state["close"] > b_state["prior_low"]) if b_state else None,
              f"close {b_state['close']:,.2f} vs prior low {b_state['prior_low']:,.2f}" if b_state else "—", "close above"),
    ]
    bear = [
        _item("Indexes below 20-day VWAP", none_idx("above_vwap"), bull[0]["value"], "every index below"),
        _item("Bearish EMA alignment (9 < 21)", none_idx("ema_bullish"), bull[1]["value"], "every index"),
        _item(f"Breadth: % above {m['breadth_ma']}-day MA", ok(bp <= m["breadth_bearish_pct"]) if np.isfinite(bp) else None,
              bull[2]["value"], f"≤ {m['breadth_bearish_pct']}%"),
        _item("Selling volume expanding", ok(br.get("adv_volume_share", np.nan) < 0.5 and b_state["rel_volume"] >= 1.0)
              if b_state and np.isfinite(br.get("adv_volume_share", np.nan)) else None,
              f"advancing volume share {br.get('adv_volume_share', float('nan')):.0%}, relative volume "
              f"{b_state['rel_volume']:.2f}" if b_state else "—", "share < 50% and rel. volume ≥ 1"),
        _item("Failed to hold prior-day high", ok(b_state["close"] < b_state["prior_high"]) if b_state else None,
              f"close {b_state['close']:,.2f} vs prior high {b_state['prior_high']:,.2f}" if b_state else "—", "close below"),
    ]
    div_items, comp_items = [], []
    if b_state is not None and br.get("n"):
        n = int(m["divergence_bars"])
        bc = bench["close"].astype(float)
        idx_high = bool(bc.iloc[-1] >= bc.iloc[-n:].max())
        ps = br["pct_above_series"].dropna()
        # breadth confirms only with a new high of its own that is also higher than n bars ago
        breadth_high = bool(ps.iloc[-1] >= ps.iloc[-n:].max() and ps.iloc[-1] > ps.iloc[-n]) if len(ps) >= n else None
        div_items = [
            _item(f"{m['benchmark']} at a {n}-bar closing high", idx_high, f"close {bc.iloc[-1]:,.2f}, {n}-bar high "
                  f"{bc.iloc[-n:].max():,.2f}", "close = highest"),
            _item("Breadth fails to confirm", None if breadth_high is None else not breadth_high,
                  f"% above MA {ps.iloc[-1]:.0f}; {n}-bar high {ps.iloc[-n:].max():.0f}; {n} bars ago {ps.iloc[-n]:.0f}"
                  if len(ps) >= n else "—", "no new breadth high above its level n bars ago"),
            _item("Advancing volume declining", ok(br["adv_volume_share"] < 0.5) if np.isfinite(br["adv_volume_share"]) else None,
                  f"advancing share {br['adv_volume_share']:.0%}" if np.isfinite(br["adv_volume_share"]) else "—", "< 50%"),
        ]
        comp, exp = float(th["compression_percentile"]), float(th["expansion_percentile"])
        comp_items = [
            _item("Was compressed (Bollinger width, 5 bars ago)", ok(b_state["bb_width_rank_5ago"] <= comp + 5)
                  if np.isfinite(b_state["bb_width_rank_5ago"]) else None,
                  f"{b_state['bb_width_rank_5ago']:.0f}th percentile", f"≤ {comp + 5:g}"),
            _item("Now expanding (Bollinger width)", ok(b_state["bb_width_pct_rank"] >= exp)
                  if np.isfinite(b_state["bb_width_pct_rank"]) else None,
                  f"{b_state['bb_width_pct_rank']:.0f}th percentile", f"≥ {exp:g}"),
            _item("Volume expanding", ok(b_state["rel_volume"] >= 1.3), f"relative volume {b_state['rel_volume']:.2f}", "≥ 1.30"),
            _item("Price leaves its 20-day range", b_state["high_20"] or b_state["low_20"],
                  "at a 20-day high" if b_state["high_20"] else "at a 20-day low" if b_state["low_20"] else "inside range",
                  "20-day high or low"),
        ]
    rot_items = []
    if sec["available"]:
        t = sec["table"]
        best = t.iloc[0]
        movers = t[t["rank_change"] >= 3]
        rot_items = [
            _item("A sector moved up ≥ 3 ranks in 5 bars", not movers.empty,
                  ", ".join(f"{r.etf} {r.rank_5ago}→{r.rank}" for r in movers.itertuples()) or "none", "rank change ≥ 3"),
            _item("Leader beats the benchmark", bool(best["rs20"] > 0), f"{best['etf']} {best['rs20']:+.1f} pts", "> 0"),
        ]
        if confirmed_bullish is not None:
            sym_sec = m.get("symbol_sectors") or {}
            members = [s for s, e in sym_sec.items() if e == best["etf"] and confirmed_bullish.get(s)]
            rot_items.append(_item("Members with confirmed bullish rules", len(members) >= 2,
                                   ", ".join(members) or "none", "≥ 2 symbols"))
    checks = {"broad_bullish": ("Broad bullish alignment", bull), "broad_bearish": ("Broad bearish alignment", bear),
              "breadth_divergence": ("Breadth divergence", div_items),
              "volatility_transition": ("Volatility regime transition", comp_items),
              "sector_rotation": ("Sector rotation", rot_items)}
    verdicts = {k: (name, _verdict(items, 2 if k in ("breadth_divergence", "sector_rotation") else 3), items)
                for k, (name, items) in checks.items()}
    if verdicts["volatility_transition"][1] == "CONFIRMED":
        verdicts["volatility_transition"] = (verdicts["volatility_transition"][0], "VOLATILITY_EXPANSION",
                                             verdicts["volatility_transition"][2])
    bull_v, bear_v = verdicts["broad_bullish"][1], verdicts["broad_bearish"][1]
    regime = ("Broad bullish alignment" if bull_v == "CONFIRMED" else "Broad bearish alignment" if bear_v == "CONFIRMED"
              else "Leaning bullish" if bull_v == "NEAR_CONFIRMATION" else "Leaning bearish" if bear_v == "NEAR_CONFIRMATION"
              else "Mixed")
    conflicts = []
    if idx:
        ups = [s for s, i in idx.items() if i["trend"] == "Up"]
        downs = [s for s, i in idx.items() if i["trend"] == "Down"]
        if ups and downs:
            conflicts.append(f"Indexes disagree: {', '.join(ups)} up, {', '.join(downs)} down.")
    if b_state and np.isfinite(bp):
        if b_state["trend"] == "Up" and bp < m["breadth_bearish_pct"]:
            conflicts.append(f"{m['benchmark']} trends up but only {bp:.0f}% of symbols are above their {m['breadth_ma']}-day MA.")
        if b_state["trend"] == "Down" and bp > m["breadth_bullish_pct"]:
            conflicts.append(f"{m['benchmark']} trends down but {bp:.0f}% of symbols are above their {m['breadth_ma']}-day MA.")
    vol_regime = "Unavailable"
    if b_state:
        r_ = np.nanmean([b_state["atr_pct_rank"], b_state["bb_width_pct_rank"]])
        vol_regime = ("Compressed" if r_ <= th["compression_percentile"] else "Expanded" if r_ >= th["expansion_percentile"]
                      else "Normal") + f" (ATR {b_state['atr_pct_rank']:.0f}th, band width {b_state['bb_width_pct_rank']:.0f}th percentile)"
    return {"indexes": idx, "missing_indexes": missing_idx, "breadth": br, "breadth_low_sample": small, "sectors": sec,
            "vix": vix, "regime": regime, "verdicts": verdicts, "conflicts": conflicts, "volatility_regime": vol_regime,
            "events_note": "No economic-calendar source is configured, so scheduled economic releases are not shown.",
            "date": b_state["date"] if b_state else None}
