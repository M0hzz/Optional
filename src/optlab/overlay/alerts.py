"""State-transition alerts for the live monitor, with deduplication, cooldown and signal stacking.

An alert fires when, on a completed bar, a rule
    becomes CONFIRMED from NEAR (near_to_confirmed) or from anything else (new_confirmed),
    loses its confirmation (invalidated), or becomes NEAR from below (new_near).
Deduplication: one alert per (rule, symbol, timeframe, bar, transition), however many
monitor passes see that same bar. Cooldown: the same (rule, symbol, timeframe, status)
won't alert again within `cooldown_minutes`. Stacking: rules from two or more different
groups confirming on one symbol within `stacking_bars` bars give one stacking alert; rules in
the same group (e.g. an EMA alignment and a crossover of the same EMAs) count once.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import ACTIVE
from .rules import RuleResult
from .status import STATUSES


@dataclass
class Alert:
    key: str
    title: str
    body: str
    status: str
    symbol: str
    rule: str
    bar: str
    urgency: str = "normal"


def _n(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1e4 else f"{v:,.4g}"


def transition_text(r: RuleResult) -> str:
    """Fixed sentence naming the conditions that changed between the previous and the latest bar."""
    when = f"{pd.Timestamp(r.timestamp):%Y-%m-%d %H:%M}" if r.timestamp is not None else "the latest bar"
    if r.timeframe in ("1d", "1wk"):
        when = f"the {pd.Timestamp(r.timestamp):%Y-%m-%d} close"
    head = {"near_to_confirmed": f"CONFIRMED at {when}.", "new_confirmed": f"CONFIRMED at {when}.",
            "invalidated": f"INVALIDATED at {when}.", "new_near": f"NEAR CONFIRMATION at {when}."}.get(r.transition, "")
    score = f" Previous score: {r.prev_score:.0f}. Current score: {r.score:.0f}." if r.prev_score == r.prev_score else ""
    changes = []
    for c in r.conditions:
        prev, now = c.get("prev_status"), c["status"]
        if prev is None or prev == now or (prev != "pass" and now != "pass"):
            continue
        if c.get("kind") in ("boolean", "sequence"):
            changes.append(f"`{c['expression']}` " + ("became true" if now == "pass" else "is no longer true"))
            continue
        lhs = c.get("lhs") or c["id"]
        pv, nv = c.get("prev_observed"), c["observed"]
        vals = (f" from {_n(pv)} to {_n(nv)}" if isinstance(pv, (int, float)) and isinstance(nv, (int, float))
                and np.isfinite(pv) and np.isfinite(nv) else "")
        if now == "pass":
            changes.append(f"{lhs} moved{vals} and now meets the {c['required_value']} requirement")
        else:
            changes.append(f"{lhs} moved{vals} and no longer meets {c['required_value']}")
    return head + score + (" " + "; ".join(changes) + "." if changes else "")


def format_alert(r: RuleResult, market: dict | None = None, history: dict | None = None,
                 account: dict | None = None, events: dict | None = None, rule=None) -> str:
    """The fixed alert layout: every section always present, 'not evaluated' when there is nothing to say."""
    lines = [f"STATUS: {STATUSES[r.status].label}", f"RULE: {r.rule_id} — {r.rule_name}",
             f"SYMBOL AND TIMEFRAME: {r.symbol}, {r.timeframe}, completed bar {r.data_timestamp}", "OBSERVED CONDITIONS:"]
    for c in r.conditions:
        lines.append(f"  - {c['id']}: {c.get('lhs', c['expression'])} = {_n(c['observed'])} | required "
                     f"{c['required_value']} | {c['status']} | weight {c['weight']:g}"
                     if isinstance(c["observed"], (int, float)) and np.isfinite(c["observed"])
                     else f"  - {c['id']}: {c['expression']} | {c['status']} | weight {c['weight']:g}")
    lines.append("MISSING CONFIRMATION: " + ("none" if r.base_status == "CONFIRMED" or not r.missing else "; ".join(r.missing)))
    if market:
        lines.append(f"MARKET CONTEXT: regime {market.get('regime', 'n/a')}; volatility {market.get('volatility_regime', 'n/a')}")
    else:
        lines.append("MARKET CONTEXT: not evaluated for this alert")
    if history:
        lines.append(f"HISTORICAL EVIDENCE: exact matches {history.get('exact', 0)}, near {history.get('near', 0)}; "
                     + history.get("summary", ""))
    else:
        lines.append("HISTORICAL EVIDENCE: not evaluated for this alert (see the Signal explorer)")
    if account:
        lines.append(f"ACCOUNT FIT: {account.get('classification')}; capital {account.get('capital_required')}, "
                     f"max loss {account.get('max_loss')}")
    else:
        lines.append("ACCOUNT FIT: no option structure attached (technical signals stay visible regardless)")
    lines.append(f"DATA QUALITY: source {r.data_source or 'n/a'}, bar {r.data_timestamp}, flags "
                 f"{', '.join(r.data_flags) or 'none'}, unavailable {', '.join(r.unavailable) or 'none'}; values observed")
    lines.append("EVENT FLAGS: " + (", ".join(f"{k} {v}" for k, v in events.items()) if events else "none available"))
    inval = (", ".join(rule.invalidation) if rule is not None and rule.invalidation else
             "any required condition failing on a completed bar")
    lines.append(f"INVALIDATION: {inval}")
    return "\n".join(lines)


class TransitionTracker:
    """Remembers what was alerted (survives restarts) so repeated passes over one bar alert once."""

    def __init__(self, state_path: Path, cooldown_minutes: float = 30, stacking_bars: int = 3):
        self.path, self.cooldown, self.stacking = Path(state_path), float(cooldown_minutes), int(stacking_bars)
        self.state = {"sent": {}, "last": {}}
        if self.path.exists():
            try:
                self.state.update(json.loads(self.path.read_text()))
            except Exception:
                pass

    def alerts(self, results: list[RuleResult], now: datetime) -> list[Alert]:
        out, ts = [], now.timestamp()
        sent, last = self.state["sent"], self.state["last"]
        for r in results:
            if r.rule_id == "conflict" or r.timestamp is None:
                continue
            base = f"{r.rule_id}|{r.symbol}|{r.timeframe}"
            if not r.transition:
                continue
            dedupe = f"{base}|{r.data_timestamp}|{r.transition}"
            if dedupe in sent:
                continue                                # already alerted for this bar
            cool = f"{base}|{r.status}"
            if ts - last.get(cool, -1e18) < self.cooldown * 60:
                sent[dedupe] = ts                       # inside the cooldown: remember, don't repeat
                continue
            sent[dedupe], last[cool] = ts, ts
            title = {"near_to_confirmed": "Confirmed (was near)", "new_confirmed": "Confirmed",
                     "invalidated": "Invalidated", "new_near": "Near confirmation"}[r.transition]
            out.append(Alert(dedupe, f"{title}: {r.symbol} · {r.rule_name}", transition_text(r), r.status, r.symbol,
                             r.rule_id, r.data_timestamp, "critical" if r.transition == "invalidated" else "normal"))
        out += self._stacking(results, sent, ts)
        cutoff = ts - 14 * 86400                        # keep two weeks of memory
        self.state["sent"] = {k: v for k, v in sent.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state))
        return out

    def _stacking(self, results, sent, ts) -> list[Alert]:
        out = []
        by: dict[tuple, dict] = {}
        for r in results:
            if r.status in ACTIVE and r.bars_since is not None and r.bars_since <= self.stacking and r.rule_id != "conflict":
                by.setdefault((r.symbol, r.timeframe), {}).setdefault(r.group, []).append(r)
        for (sym, tf), groups in by.items():
            if len(groups) < 2:
                continue
            bar = max(r.data_timestamp for g in groups.values() for r in g)
            key = f"stack|{sym}|{tf}|{bar}|{'+'.join(sorted(groups))}"
            if key in sent:
                continue
            sent[key] = ts
            parts = [f"{g}: {', '.join(r.rule_name for r in rs)}" for g, rs in sorted(groups.items())]
            out.append(Alert(key, f"Signal stacking: {sym} · {len(groups)} rule groups",
                             f"{len(groups)} independent rule groups confirmed within {self.stacking} bars on {sym} "
                             f"({tf}): " + "; ".join(parts) + ". Rules in the same group count once.",
                             "CONFIRMED", sym, "stacking", bar))
        return out


def write_alerts(out_dir: Path, alerts: list[Alert], now: datetime) -> None:
    if not alerts:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "overlay_alerts.csv"
    pd.DataFrame([{"time": now.isoformat(timespec="seconds"), "status": a.status, "symbol": a.symbol, "rule": a.rule,
                   "bar": a.bar, "title": a.title, "body": a.body.replace("\n", " | ")} for a in alerts]).to_csv(
        log, mode="a", header=not log.exists(), index=False)
