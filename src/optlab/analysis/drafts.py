"""A draft historical analysis for a dashboard object: which symbol, timeframe and conditions to search, and
which of the object's conditions can't be searched (and why). The draft is only a starting point: every
field can be edited before running."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..history.bars import INTRADAY, bars_per_session
from ..history.explorer import condition_kind
from ..monitoring import settings as msettings
from ..monitoring.rules import split_conditions
from ..portfolio.models import AlertRule, Event, Position, TradePlan, WatchedContract
from ..portfolio.service import DomainError
from .translate import translate

SUBJECTS = ("plan", "position", "alert_rule", "contract", "event", "manual")
# how far back a draft looks by default (Yahoo's limits for intraday bars)
DEFAULT_SPAN_DAYS = {"1m": 6, "5m": 58, "15m": 58, "30m": 58, "1h": 700, "1d": 365 * 8, "1wk": 365 * 15}
DEFAULT_HORIZONS = {"1d": ["1", "5", "10", "20"], "1wk": ["1", "4", "12"]}
INTRADAY_HORIZONS = ["1", "3", "6", "12", "1d"]
NEAR_TOLERANCE_PCT = 2.0


def horizons_for(timeframe: str) -> list[str]:
    return DEFAULT_HORIZONS.get(timeframe, INTRADAY_HORIZONS)


def horizon_bars(h: str, timeframe: str) -> int:
    if h.endswith("d"):
        n = int(h[:-1])
        return int(round(n * bars_per_session(timeframe))) if timeframe in INTRADAY else n
    return int(h)


def _conditions(clauses: list[dict]) -> list[dict]:
    out = []
    for i, c in enumerate(clauses, 1):
        kind, _ = condition_kind(c["expression"])
        tol = kind in ("compare", "between")
        out.append({"id": f"c{i}", "expression": c["expression"], "origin": c["origin"], "required": True, "weight": 1.0,
                    "tolerance_type": "percent_distance" if tol else "none", "tolerance": NEAR_TOLERANCE_PCT if tol else 0.0})
    return out


def _base(title: str, symbol: str, timeframe: str, today: date, subject: dict) -> dict:
    tf = timeframe if timeframe in DEFAULT_SPAN_DAYS else "1d"
    hz = horizons_for(tf)
    return {"subject": subject, "name": title[:120], "symbols": [symbol.upper()], "timeframe": tf,
            "start": (today - timedelta(days=DEFAULT_SPAN_DAYS[tf])).isoformat(), "end": today.isoformat(),
            "conditions": [], "excluded": [], "notes": [], "direction": None, "horizons": hz,
            "focus_horizon": hz[min(1, len(hz) - 1)], "dedupe_bars": max(horizon_bars(h, tf) for h in hz),
            "include_near": True, "minimum_score": 80.0, "required_mode": "tolerant"}


def _from_plan(s: Session, pl: TradePlan, today: date, st, subject: dict | None = None) -> dict:
    d = _base(f"Plan: {pl.title}", pl.symbol, st.timeframe, today,
              subject or {"type": "plan", "id": pl.id, "label": pl.title, "link": f"#/plans/{pl.id}"})
    src = [("trigger", c) for c in split_conditions(pl.trigger_conditions)] + \
          [("confirmation", c) for c in split_conditions(pl.confirmation_conditions)]
    inc, exc = translate(src)
    d["conditions"], d["excluded"] = _conditions(inc), exc
    d["direction"] = pl.direction if pl.direction in ("bullish", "bearish") else None
    for c in split_conditions(pl.invalidation_conditions):
        d["excluded"].append({"expression": c, "origin": "invalidation",
                              "reason": "invalidation conditions describe when the setup fails, not the setup itself"})
    if not src:
        d["notes"].append("The plan has no monitored trigger or confirmation conditions: add the conditions that describe "
                          "the setup (for example `close > sma(50) and rsi(14) < 40`).")
    return d


def _from_rule(s: Session, r: AlertRule, today: date, st) -> dict:
    sym = r.symbol
    if r.target_type == "position" and r.position_id:
        p = s.get(Position, r.position_id)
        sym = p.underlying if p else sym
    elif r.target_type == "contract" and r.contract_id:
        c = s.get(WatchedContract, r.contract_id)
        sym = c.underlying if c else sym
    if not sym:
        raise DomainError("NO_SYMBOL", "This alert rule has no symbol to analyse.", 422)
    d = _base(f"Alert rule: {r.name}", sym, r.timeframe or st.timeframe, today,
              {"type": "alert_rule", "id": r.id, "label": r.name, "link": None})
    inc, exc = translate([("alert rule", r.condition)])
    d["conditions"], d["excluded"] = _conditions(inc), exc
    return d


def _from_contract(s: Session, c: WatchedContract, today: date, st) -> dict:
    d = _base(f"Watched contract: {c.occ_symbol}", c.underlying, st.timeframe, today,
              {"type": "contract", "id": c.id, "label": c.occ_symbol, "link": None})
    inc, exc = translate([("entry condition", x) for x in split_conditions(c.alert_conditions)])
    d["conditions"], d["excluded"] = _conditions(inc), exc
    d["direction"] = "bullish" if c.option_right.value == "CALL" else "bearish"
    d["notes"].append(f"Direction assumed from the contract ({c.option_right.value.lower()}): change it if the contract "
                      "would be sold rather than bought.")
    return d


def _from_position(s: Session, p: Position, today: date, st) -> dict:
    from ..monitoring.status import rules_for

    if p.plan_id and (pl := s.get(TradePlan, p.plan_id)) is not None:
        d = _from_plan(s, pl, today, st, {"type": "position", "id": p.id, "label": p.name, "link": f"#/positions/{p.id}"})
        d["name"] = f"Position: {p.name}"
        d["notes"].insert(0, f"Conditions from the plan this position came from ({pl.title}).")
    else:
        rules, tf, _ = rules_for(p, st)
        d = _base(f"Position: {p.name}", p.underlying, tf, today,
                  {"type": "position", "id": p.id, "label": p.name, "link": f"#/positions/{p.id}"})
        src = [("position rule", r) for r in rules.watch_when + rules.action_when + rules.exit_when]
        inc, exc = translate(src)
        d["conditions"], d["excluded"] = _conditions(inc), exc
        d["notes"].insert(0, "This position has no plan: the draft uses its monitoring rules that have price history. "
                             "Describe the entry setup to see how it behaved before.")
    d["entry"] = {"opened_at": p.opened_at.isoformat(), "note": "The entry time is marked in the match list when it falls "
                                                               "on a matching bar."}
    return d


def draft_for(s: Session, subject_type: str, subject_id: str | None, today: date | None = None,
              symbol: str | None = None) -> dict:
    """The draft for one dashboard object (or a blank one for `manual`)."""
    today = today or date.today()
    st = msettings.load(s)
    if subject_type not in SUBJECTS:
        raise DomainError("BAD_SUBJECT", f"subject_type must be one of {', '.join(SUBJECTS)}.", 422)
    if subject_type == "manual":
        return _base("Historical analysis", (symbol or "SPY"), "1d", today, {"type": "manual", "id": None, "label": "manual",
                                                                            "link": None})

    def need(obj, what):
        if obj is None:
            raise DomainError("NOT_FOUND", f"{what} {subject_id} does not exist.", 404)
        return obj

    if subject_type == "plan":
        return _from_plan(s, need(s.get(TradePlan, subject_id), "Plan"), today, st)
    if subject_type == "position":
        return _from_position(s, need(s.get(Position, subject_id), "Position"), today, st)
    if subject_type == "alert_rule":
        return _from_rule(s, need(s.get(AlertRule, subject_id), "Alert rule"), today, st)
    if subject_type == "contract":
        return _from_contract(s, need(s.get(WatchedContract, subject_id), "Watched contract"), today, st)
    from sqlalchemy import select

    ev = s.scalar(select(Event).where(Event.id == subject_id))
    ev = need(ev, "Event")
    src = ev.source_json or {}
    if ev.alert_rule_id and (r := s.get(AlertRule, ev.alert_rule_id)) is not None:
        d = _from_rule(s, r, today, st)
    elif src.get("plan_id") and (pl := s.get(TradePlan, src["plan_id"])) is not None:
        d = _from_plan(s, pl, today, st)
    elif ev.contract_id and (c := s.get(WatchedContract, ev.contract_id)) is not None:
        d = _from_contract(s, c, today, st)
    elif ev.position_id and (p := s.get(Position, ev.position_id)) is not None:
        d = _from_position(s, p, today, st)
    elif ev.symbol:
        d = _base(f"Event: {ev.title}", ev.symbol, st.timeframe, today, {})
        d["notes"].append("This event isn't tied to conditions: add the conditions you want to look for.")
    else:
        raise DomainError("NO_SYMBOL", "This event has no symbol or conditions to analyse.", 422)
    d["subject"] = {"type": "event", "id": ev.id, "label": ev.title, "link": None, "via": d["subject"]}
    d["name"] = f"Event: {ev.title}"[:120]
    return d
