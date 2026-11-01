"""One monitoring pass over the portfolio database. Checks only; nothing here places orders.

A pass:
    1. reads the open, monitored positions, the active watched contracts and the enabled alert rules
       (short read transaction);
    2. fetches each underlying's option chain once through the shared QuoteService (the same snapshots
       the API serves), plus price bars only for rules that use studies, and earnings dates;
    3. values every position (portfolio.valuation), evaluates its rules and built-in checks, and derives
       its raw status (status.py);
    4. in one write transaction: stores a snapshot per position and contract, advances the confirmation /
       re-arm state (transitions.py), and writes events only when a status changes, an alert fires, or a
       reminder is due;
    5. after commit, sends desktop notifications for the new events that qualify and logs each attempt.
No database lock is held while market data is fetched.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from ..portfolio.ids import new_id
from ..portfolio.models import (AlertRule, ContractSnapshot, ContractStatus, Lifecycle, MonitorRun, MonitorState,
                                Position, PositionSnapshot, TradePlan, WatchedContract, utcnow)
from ..portfolio.money import computed_to_units, fmt, from_units
from ..portfolio.quotes import ChainSnapshot, QuoteService, earnings_date
from ..portfolio.valuation import PositionValuation, value_position
from . import settings as msettings
from .events import Notifier, deliver, desktop_notifier, emit
from .rules import TARGET_NAMES, Context, RuleResult, bar_vocabulary, completed_only, explain, names_used, split_conditions
from .settings import MonitorSettings
from .status import ACTION, EXIT, LABELS, SEVERITY, STALE, PositionCheck, check_position, rules_for
from .transitions import step_alert, step_status

log = logging.getLogger("optlab.monitor")
WATCHED_STATES = (ContractStatus.RESEARCHING, ContractStatus.WAITING_FOR_CONFIRMATION, ContractStatus.ENTRY_QUALIFIED,
                  ContractStatus.ENTRY_CONDITION_LOST)
AUTOMATED = (ContractStatus.WAITING_FOR_CONFIRMATION, ContractStatus.ENTRY_QUALIFIED, ContractStatus.ENTRY_CONDITION_LOST)
CONTRACT_LABEL = {"WAITING_FOR_CONFIRMATION": "Waiting for confirmation", "ENTRY_QUALIFIED": "Entry qualified",
                  "ENTRY_CONDITION_LOST": "Entry condition lost", "EXPIRED": "Expired"}


# ------------------------------------------------------------------ price bars
class BarsSource:
    """Price bars for rules that use studies (rsi(14), sma(50), ...). Live: Yahoo through yfinance (the
    existing LiveFeed); synthetic mode: the offline SyntheticFeed. Created lazily: no bars are fetched
    unless a rule needs them."""

    def __init__(self, mode: str, rate: float = 0.04):
        self.mode, self.rate = mode, rate
        self.feed: Any = None
        self.name = "synthetic" if mode == "synthetic" else "Yahoo (yfinance)"

    def get(self, symbols: list[str], timeframe: str) -> tuple[dict[str, pd.DataFrame], str | None]:
        try:
            if self.feed is None:
                from ..live.feed import LiveFeed, SyntheticFeed

                self.feed = SyntheticFeed(rate=self.rate) if self.mode == "synthetic" else LiveFeed(rate=self.rate)
            return self.feed.bars(symbols, timeframe), None
        except Exception as exc:                    # network, provider, import
            return {}, f"{type(exc).__name__}: {str(exc)[:160]}"


@dataclass
class PassSummary:
    run_id: str
    trigger: str
    started_at: datetime
    seconds: float
    positions: int = 0
    contracts: int = 0
    rules: int = 0
    events: int = 0
    notified: int = 0
    errors: list[str] = field(default_factory=list)
    ok: bool = True


def _unavailable(p: Position, snap: ChainSnapshot, why: str) -> PositionValuation:
    return PositionValuation("ESTIMATED", "UNAVAILABLE", [why], None, None, None, None, snap.underlying_price(), {},
                             [], snap.provider, "UNAVAILABLE", snap.delay_minutes, snap.fetched_at,
                             snap.source_timestamp, snap.age_seconds(), snap.greeks_source, utcnow())


def usd(d: Decimal | None) -> str:
    if d is None:
        return "unavailable"
    return f"{'+' if d > 0 else '−' if d < 0 else ''}${abs(d):,.2f}"


def _units(d: Decimal | None) -> int | None:
    return None if d is None else computed_to_units(d)


def _metrics_json(m: dict) -> dict:
    from .rules import _jsonable

    return _jsonable(m)


def _state(s: Session, kind: str, subject: str, key: str = "") -> MonitorState:
    st = s.get(MonitorState, (kind, subject, key))
    if st is None:
        st = MonitorState(kind=kind, subject_id=subject, key=key, candidate_count=0, active=False, data_json={})
        s.add(st)
    return st


class Engine:
    def __init__(self, db, quotes: QuoteService, bars: BarsSource | None = None,
                 notifier: Notifier | None = desktop_notifier, earnings: Callable[[str], date | None] = earnings_date,
                 clock: Callable[[], datetime] = utcnow):
        self.db, self.quotes, self.notifier, self.earnings, self.clock = db, quotes, notifier, earnings, clock
        self.bars = bars or BarsSource(quotes.mode)
        self.lock = threading.Lock()
        self.running_since: datetime | None = None

    # ------------------------------------------------------------------ public
    def run_pass(self, trigger: str = "schedule") -> PassSummary:
        with self.lock:
            self.running_since = self.clock()
            try:
                return self._run(trigger)
            finally:
                self.running_since = None

    # ------------------------------------------------------------------ pass
    def _run(self, trigger: str) -> PassSummary:
        t0, now = time.monotonic(), self.clock()
        run_id = new_id()
        summ = PassSummary(run_id, trigger, now, 0.0)
        try:
            self._pass(run_id, now, summ)
        except Exception as exc:                     # a pass must never take the server down
            log.exception("monitor pass failed")
            summ.ok = False
            summ.errors.append(f"pass failed: {type(exc).__name__}: {str(exc)[:200]}")
        summ.seconds = time.monotonic() - t0
        self._finish(summ, now)
        return summ

    def _pass(self, run_id: str, now: datetime, summ: PassSummary) -> None:
        with self.db.session(write=False) as s:
            st = msettings.load(s)
            positions = list(s.scalars(select(Position).options(selectinload(Position.legs))
                                       .where(Position.lifecycle == Lifecycle.OPEN, Position.monitoring_enabled.is_(True))
                                       .order_by(Position.opened_at)))
            contracts = list(s.scalars(select(WatchedContract).where(WatchedContract.status.in_(WATCHED_STATES))))
            alert_rules = list(s.scalars(select(AlertRule).where(AlertRule.enabled.is_(True), AlertRule.archived_at.is_(None))))
            plans = list(s.scalars(select(TradePlan).where(
                TradePlan.state.in_(("ARMED", "WAITING", "QUALIFIED")) | (TradePlan.expires_at.is_not(None)
                                                                         & TradePlan.state.in_(("DRAFT",))))))
            from ..portfolio.service import get_preferences

            display_tz = get_preferences(s).get("timezone") or "America/New_York"
            prev = {r.subject_id: (r.data_json or {}).get("metrics") for r in
                    s.scalars(select(MonitorState).where(MonitorState.kind == "position"))}
        today = now.astimezone(ZoneInfo(st.schedule.timezone)).date()
        by_pos = {p.id: p for p in positions}
        by_contract = {c.id: c for c in contracts}
        alert_rules = [r for r in alert_rules if (r.target_type != "position" or r.position_id in by_pos)
                       and (r.target_type != "contract" or r.contract_id in by_contract)]
        symbols = sorted({p.underlying for p in positions} | {c.underlying for c in contracts if c.expiration >= today}
                         | {r.symbol for r in alert_rules if r.target_type == "symbol" and r.symbol}
                         | {pl.symbol for pl in plans if pl.state != "DRAFT"})

        # ---------------------------------------------------------- fetch (no database lock held)
        with ThreadPoolExecutor(8) as ex:
            snaps: dict[str, ChainSnapshot] = dict(zip(symbols, ex.map(self.quotes.chain, symbols)))
            earn = dict(zip(symbols, ex.map(self._earnings, symbols)))
        bars = self._bars_needed(st, positions, contracts, alert_rules, now, plans)
        vals: dict[str, PositionValuation] = {}
        checks: dict[str, PositionCheck] = {}
        for p in positions:
            try:
                v = value_position(p, self.quotes)
                b, berr = bars.get((p.underlying, rules_for(p, st)[1]), (None, None))
                vals[p.id] = v
                checks[p.id] = check_position(p, v, snaps[p.underlying], st, earn.get(p.underlying), today, b, berr,
                                              prev.get(p.id), self.quotes.max_age, now)
            except Exception as exc:              # never skip silently: the position becomes STALE DATA
                log.exception("checking position %s failed", p.id)
                msg = f"{type(exc).__name__}: {str(exc)[:160]}"
                summ.errors.append(f"{p.name}: {msg}")
                if p.id not in vals:
                    vals[p.id] = _unavailable(p, snaps[p.underlying], msg)
                checks[p.id] = PositionCheck(STALE, [f"the monitor couldn't check this position ({msg})"], [], {})
        contract_quotes = {}
        for c in contracts:
            if c.expiration >= today:
                contract_quotes[c.id] = self.quotes.contract(c.underlying, c.expiration, c.option_right.value,
                                                             from_units(c.strike_units), snaps[c.underlying])
        for sym, sn in snaps.items():
            if sn.rows is None:
                summ.errors.append(f"{sym}: option chain unavailable ({sn.error or 'no data'})")

        # ---------------------------------------------------------- write
        new_events: list[int] = []
        with self.db.session() as s:
            for p in positions:
                if p.id not in checks:
                    continue
                cur = s.get(Position, p.id)
                if cur is None or cur.lifecycle != Lifecycle.OPEN or not cur.monitoring_enabled:
                    continue                          # closed or switched off while the pass was fetching
                new_events += self._write_position(s, p, vals[p.id], checks[p.id], snaps[p.underlying], st, now,
                                                   today, run_id, earn.get(p.underlying))
                summ.positions += 1
            ctx_cache: dict[str, Context] = {}
            for c in contracts:
                new_events += self._write_contract(s, c, contract_quotes.get(c.id), snaps.get(c.underlying), st, now,
                                                   today, run_id, bars, earn, ctx_cache)
                summ.contracts += 1
            for r in alert_rules:
                ctx = self._rule_context(r, checks, by_pos, by_contract, contract_quotes, snaps, bars, earn, today,
                                         ctx_cache)
                new_events += self._write_alert(s, r, ctx, st, now, run_id, by_pos, by_contract)
                summ.rules += 1
            # plans (missions) and account risk limits
            from ..planning.monitor import evaluate_plans, risk_limit_events

            plan_ctx = {}
            for pl in plans:
                if pl.state != "DRAFT" and pl.symbol not in plan_ctx:
                    plan_ctx[pl.symbol] = self._symbol_context(pl.symbol, st.timeframe, snaps, bars, earn, today)
            try:
                with s.begin_nested():
                    new_events += evaluate_plans(s, plans, plan_ctx, st, now, run_id)
            except Exception as exc:
                log.exception("plan checks failed")
                summ.errors.append(f"plans: {type(exc).__name__}: {str(exc)[:160]}")
            try:
                with s.begin_nested():
                    new_events += risk_limit_events(s, st, now, run_id, display_tz)
            except Exception as exc:
                log.exception("risk-limit checks failed")
                summ.errors.append(f"risk limits: {type(exc).__name__}: {str(exc)[:160]}")
            self._monitor_state(s, summ, st, now, run_id, new_events)
            self._retention(s, st, now)
            s.add(MonitorRun(id=run_id, started_at=now, finished_at=None, trigger=summ.trigger, mode=self.quotes.mode,
                             provider=self.quotes.provider.name, positions_checked=summ.positions,
                             contracts_checked=summ.contracts, rules_checked=summ.rules,
                             events_emitted=len(new_events), ok=summ.ok, errors_json=summ.errors[:50]))
        summ.events = len(new_events)
        summ.notified = deliver(self.db, new_events, st, self.notifier)

    def _finish(self, summ: PassSummary, now: datetime) -> None:
        try:
            with self.db.session() as s:
                run = s.get(MonitorRun, summ.run_id)
                if run is None:                      # the write phase never ran
                    run = MonitorRun(id=summ.run_id, started_at=now, trigger=summ.trigger, mode=self.quotes.mode,
                                     provider=self.quotes.provider.name, ok=False, errors_json=summ.errors[:50])
                    s.add(run)
                    st = msettings.load(s)
                    self._monitor_state(s, summ, st, now, summ.run_id, [])
                run.finished_at = now + timedelta(seconds=summ.seconds)
                run.seconds = round(summ.seconds, 3)
                run.ok = summ.ok
                run.errors_json = summ.errors[:50]
        except Exception:
            log.exception("could not record the monitor run")

    def _earnings(self, symbol: str) -> date | None:
        try:
            return self.earnings(symbol)
        except Exception:
            return None

    # ------------------------------------------------------------------ bars
    def _bars_needed(self, st: MonitorSettings, positions, contracts, alert_rules, now, plans=()) -> dict:
        bn, bf = bar_vocabulary()
        need: dict[str, set[str]] = {}

        def add(sym: str, tf: str, exprs: list[str], target: str = "position"):
            vocab = (bn | bf) - set(TARGET_NAMES[target])     # `price` is a position value, not a bar
            for e in exprs:
                try:
                    if names_used(e) & vocab:
                        need.setdefault(tf, set()).add(sym)
                        return
                except Exception:
                    continue
        for p in positions:
            rs, tf, _ = rules_for(p, st)
            add(p.underlying, tf, rs.exit_when + rs.action_when + rs.watch_when)
        for c in contracts:
            add(c.underlying, st.timeframe, split_conditions(c.alert_conditions), "contract")
        for r in alert_rules:
            sym = r.symbol or ""
            if r.target_type == "position":
                sym = next((p.underlying for p in positions if p.id == r.position_id), "")
            elif r.target_type == "contract":
                sym = next((c.underlying for c in contracts if c.id == r.contract_id), "")
            if sym:
                add(sym, r.timeframe, [r.condition], r.target_type)
        from ..planning.monitor import plan_expressions

        for pl in plans:
            if pl.state != "DRAFT":
                add(pl.symbol, st.timeframe, sum(plan_expressions(pl), []), "symbol")
        out: dict[tuple[str, str], tuple[pd.DataFrame | None, str | None]] = {}
        market_now = now.astimezone(ZoneInfo("America/New_York"))
        for tf, syms in need.items():
            got, err = self.bars.get(sorted(syms), tf)
            for sym in syms:
                df = got.get(sym)
                if df is None or df.empty:
                    out[(sym, tf)] = (None, err or f"no {tf} bars returned for {sym}")
                else:
                    out[(sym, tf)] = (completed_only(df, tf, market_now) if st.completed_bars_only else df, None)
        return out

    # ------------------------------------------------------------------ positions
    def _write_position(self, s: Session, p: Position, v: PositionValuation, chk: PositionCheck, snap: ChainSnapshot,
                        st: MonitorSettings, now: datetime, today: date, run_id: str, earn: date | None) -> list[int]:
        state = _state(s, "position", p.id)
        change = step_status(state, chk.raw_status, now, st.confirm_passes, st.repeat_urgent_minutes)
        sid = new_id()
        legs = [{"leg_index": lv.leg_index, "occ_symbol": lv.occ_symbol, "status": lv.status, "reason": lv.reason,
                 "mark": fmt(lv.mark), "exit_price": fmt(lv.exit_price), "bid": fmt(lv.bid), "ask": fmt(lv.ask),
                 "iv": lv.iv, "delta": lv.greeks.get("delta"), "quality": lv.quality,
                 "open_interest": lv.quote.open_interest if lv.quote else None,
                 "spread_pct": lv.quote.spread_pct if lv.quote else None} for lv in v.legs]
        g = v.greeks
        s.add(PositionSnapshot(
            id=sid, position_id=p.id, run_id=run_id, taken_at=now, raw_status=chk.raw_status, status=state.status or chk.raw_status,
            reasons_json=chk.reasons, rules_json=[r.as_dict() for r in chk.rules], valuation_status=v.status,
            value_units=_units(v.value), exit_value_units=_units(v.exit_value), open_pnl_units=_units(v.open_pnl),
            open_pnl_pct=None if v.open_pnl_pct is None else round(v.open_pnl_pct, 4),
            underlying_price_units=_units(v.underlying_price), delta=g.get("delta"), gamma=g.get("gamma"),
            theta=g.get("theta"), vega=g.get("vega"), metrics_json=_metrics_json(chk.metrics), legs_json=legs,
            freshness=v.freshness, provider=v.provider, delay_minutes=v.delay_minutes, fetched_at=v.fetched_at,
            source_timestamp=v.source_timestamp, quote_age_seconds=v.quote_age_seconds, keep=change is not None))
        state.checked_at, state.snapshot_id = now, sid
        old = state.data_json or {}
        # reasons of the confirmed status: this pass's when it agrees, else those recorded when it was confirmed
        status_reasons = chk.reasons if chk.raw_status == state.status else (old.get("status_reasons") or chk.reasons)
        state.data_json = {"metrics": _metrics_json(chk.metrics), "raw_status": chk.raw_status,
                           "reasons": chk.reasons, "status_reasons": status_reasons, "freshness": v.freshness}
        notify_off = None if (p.monitor_json or {}).get("notify", True) else "notifications are off for this position"
        out: list[int] = []
        if change is not None:
            to = change.to_status
            if change.kind == "reminder":
                title = f"{LABELS[to]} (still): {p.name}"
            elif change.from_status is None:
                title = f"{LABELS[to]}: {p.name}"
            else:
                title = f"{LABELS[change.from_status]} → {LABELS[to]}: {p.name}"
            pnl = f" Open P&L {usd(v.open_pnl)} (estimated, {v.freshness.lower()} quotes)." if v.open_pnl is not None else ""
            ev = emit(s, "POSITION_STATUS_REMINDER" if change.kind == "reminder" else "POSITION_STATUS_CHANGED",
                      SEVERITY[to], title, f"{LABELS[to]}: " + "; ".join(chk.reasons) + "." + pnl, settings=st,
                      symbol=p.underlying, account_id=p.account_id, position_id=p.id, snapshot_id=sid, run_id=run_id,
                      source={"from": change.from_status, "to": to, "raw": chk.raw_status, "reasons": chk.reasons,
                              "rules": [r.as_dict() for r in chk.rules if r.result is not False],
                              "freshness": v.freshness, "provider": v.provider, "fetched_at": str(v.fetched_at)},
                      dedupe_key=f"pos:{p.id}:{change.kind}:{to}", muted=notify_off, now=now)
            if ev:
                out.append(ev.seq)
        out += self._position_warnings(s, p, v, chk, st, now, today, run_id, earn, notify_off)
        return out

    def _position_warnings(self, s, p: Position, v, chk: PositionCheck, st: MonitorSettings, now, today, run_id,
                           earn: date | None, muted) -> list[int]:
        out: list[int] = []
        exps = sorted({lg.expiration for lg in p.legs if lg.expiration is not None})
        for e in exps:
            d = (e - today).days
            hits = sorted(t for t in st.expiration_warning_days if 0 <= d <= t)
            if not hits:
                continue
            key = f"expiry:{e.isoformat()}:{hits[0]}"
            ws = _state(s, "warning", p.id, key)
            if ws.active:
                continue
            for t in hits:                          # the wider thresholds are covered by this one
                _state(s, "warning", p.id, f"expiry:{e.isoformat()}:{t}").active = True
            ws.last_event_at = now
            when = "today" if d == 0 else f"in {d} day{'s' if d != 1 else ''}"
            ev = emit(s, "EXPIRATION_WARNING", "critical" if d <= 1 else "warning",
                      f"Expiration {when}: {p.name}",
                      f"Option leg(s) of {p.name} expire {when} ({e:%a %Y-%m-%d}). Decide whether to close, roll or "
                      "let them expire, and check assignment risk on short legs.", settings=st, symbol=p.underlying,
                      account_id=p.account_id, position_id=p.id, run_id=run_id, source={"expiration": e.isoformat(),
                                                                                        "days": d, "threshold": hits[0]},
                      dedupe_key=f"expiry:{p.id}:{key}", muted=muted, now=now)
            if ev:
                out.append(ev.seq)
        if earn is not None and chk.metrics.get("earnings_before_expiry"):
            ws = _state(s, "warning", p.id, f"earnings:{earn.isoformat()}")
            if not ws.active:
                ws.active, ws.last_event_at = True, now
                ev = emit(s, "EARNINGS_WARNING", "warning", f"Earnings before expiration: {p.name}",
                          f"{p.underlying} reports earnings on {earn:%a %Y-%m-%d}, on or before the nearest expiration "
                          f"({chk.metrics.get('dte')} days). Implied volatility and the price can move sharply.",
                          settings=st, symbol=p.underlying, account_id=p.account_id, position_id=p.id, run_id=run_id,
                          source={"earnings": earn.isoformat()}, dedupe_key=f"earn:{p.id}:{earn}", muted=muted, now=now)
                if ev:
                    out.append(ev.seq)
        sp = chk.metrics.get("max_spread_pct")
        zero_bid = any("zero bid" in lv.quality for lv in v.legs)
        bad: bool | None = None
        if isinstance(sp, (int, float)) and sp == sp:
            bad = sp > st.liquidity_max_spread_pct or zero_bid
        elif zero_bid:
            bad = True
        ws = _state(s, "warning", p.id, "liquidity")
        act = step_alert(ws, bad, now, 2, st.rearm_minutes, 0)
        if act == "fire":
            why = (f"widest leg spread is {sp:.1f}% of mid (limit {st.liquidity_max_spread_pct:g}%)"
                   if isinstance(sp, (int, float)) and sp == sp and sp > st.liquidity_max_spread_pct else "a leg has a zero bid")
            ev = emit(s, "LIQUIDITY_WARNING", "warning", f"Liquidity deteriorated: {p.name}",
                      f"{p.name}: {why}. Exiting at a fair price may be hard; the estimated value uses mid prices.",
                      settings=st, symbol=p.underlying, account_id=p.account_id, position_id=p.id, run_id=run_id,
                      source={"max_spread_pct": sp, "zero_bid": zero_bid}, dedupe_key=f"liq:{p.id}", muted=muted, now=now)
            if ev:
                out.append(ev.seq)
        return out

    # ------------------------------------------------------------------ contracts
    def _contract_context(self, c: WatchedContract, q, snap: ChainSnapshot | None, bars, earn, today, st) -> Context:
        strike = float(from_units(c.strike_units))
        price = None if q is None or q.underlying_price is None else float(q.underlying_price)
        found = q is not None and q.status == "FOUND"
        why = q.reason if q is not None and q.reason else "no quote for this contract"

        def f(x):
            return None if x is None else float(x)
        names = {
            "bid": f(q.bid) if found else None, "ask": f(q.ask) if found else None, "mid": f(q.mid) if found else None,
            "last": f(q.last) if found else None, "volume": q.volume if found else None,
            "open_interest": q.open_interest if found else None, "oi": q.open_interest if found else None,
            "iv": q.iv if found else None, "delta": q.delta if found else None,
            "abs_delta": abs(q.delta) if found and q.delta is not None else None, "gamma": q.gamma if found else None,
            "theta": q.theta if found else None, "vega": q.vega if found else None,
            "spread": f(q.spread) if found else None, "spread_pct": q.spread_pct if found else None,
            "dte": (c.expiration - today).days, "strike": strike, "price": price, "underlying_price": price,
            "intrinsic": f(q.intrinsic) if found else None, "extrinsic": f(q.extrinsic) if found else None,
            "breakeven": f(q.breakeven) if found else None,
            "otm_pct": None if price is None else 100 * ((strike - price) if c.option_right.value == "CALL" else (price - strike)) / price,
            "target_entry": float("nan") if c.target_entry_units is None else float(from_units(c.target_entry_units)),
            "days_to_earnings": (earn[c.underlying] - today).days if earn.get(c.underlying) else 9999,
        }
        missing = {k: why for k, v in names.items() if v is None}
        if found and q.mid is None:
            missing.update({k: "no usable two-sided quote (" + ", ".join(q.quality) + ")" for k in ("mid", "spread", "spread_pct",
                                                                                                      "extrinsic", "breakeven")})
        b, berr = bars.get((c.underlying, st.timeframe), (None, None))
        return Context("contract", names, missing, b, berr, st.timeframe, snap.rows if snap else None)

    def _write_contract(self, s: Session, c: WatchedContract, cq, snap, st, now, today, run_id, bars, earn,
                        ctx_cache) -> list[int]:
        out: list[int] = []
        cur = s.get(WatchedContract, c.id)
        if cur is None or cur.status not in WATCHED_STATES:
            return out
        if c.expiration < today:
            prev = cur.status.value
            cur.status = ContractStatus.EXPIRED
            ev = emit(s, "CONTRACT_STATUS_CHANGED", "info", f"Expired: {c.occ_symbol}",
                      f"{c.occ_symbol} expired on {c.expiration:%Y-%m-%d}; it is no longer watched.", settings=st,
                      symbol=c.underlying, account_id=c.account_id, contract_id=c.id, run_id=run_id,
                      source={"from": prev, "to": "EXPIRED"}, dedupe_key=f"wc:{c.id}:EXPIRED", now=now)
            return [ev.seq] if ev else []
        q, _ = cq if cq else (None, None)
        ctx = self._contract_context(c, q, snap, bars, earn, today, st)
        ctx_cache[f"contract:{c.id}"] = ctx
        conds = split_conditions(c.alert_conditions)
        results = [ctx.evaluate(x, "condition") for x in conds]
        fresh = snap.freshness(self.quotes.max_age, now) if snap else "UNAVAILABLE"
        keep = False
        if conds and cur.status in AUTOMATED:
            verdict: bool | None = (None if any(r.result is None for r in results) or fresh in ("UNAVAILABLE", "CACHED")
                                    else all(r.result for r in results))
            target = None
            if verdict is True:
                target = ContractStatus.ENTRY_QUALIFIED
            elif verdict is False and cur.status == ContractStatus.ENTRY_QUALIFIED:
                target = ContractStatus.ENTRY_CONDITION_LOST
            state = _state(s, "contract", c.id, "entry")
            if target is None or target == cur.status:
                state.candidate, state.candidate_count = None, 0
            else:
                state.candidate_count = (state.candidate_count or 0) + 1 if state.candidate == target.value else 1
                state.candidate = target.value
                if state.candidate_count >= st.confirm_passes:
                    prev = cur.status.value
                    cur.status = target
                    state.candidate, state.candidate_count, state.last_event_at = None, 0, now
                    keep = True
                    lines = [explain(r) for r in results]
                    if target == ContractStatus.ENTRY_QUALIFIED:
                        msg = f"All {len(results)} condition(s) hold: " + "; ".join(lines) + "."
                    else:
                        msg = "No longer met: " + "; ".join(explain(r) for r in results if r.result is False) + "."
                    ev = emit(s, "CONTRACT_STATUS_CHANGED", "warning",
                              f"{CONTRACT_LABEL[target.value]}: {c.occ_symbol}", msg + " Informational only: no order "
                              "is placed.", settings=st, symbol=c.underlying, account_id=c.account_id, contract_id=c.id,
                              run_id=run_id, source={"from": prev, "to": target.value,
                                                     "conditions": [r.as_dict() for r in results], "freshness": fresh},
                              dedupe_key=f"wc:{c.id}:{target.value}", now=now)
                    if ev:
                        out.append(ev.seq)
        s.add(ContractSnapshot(
            contract_id=c.id, run_id=run_id, taken_at=now, quote_status=q.status if q else "UNAVAILABLE",
            bid_units=_units(q.bid) if q else None, ask_units=_units(q.ask) if q else None,
            mid_units=_units(q.mid) if q else None, last_units=_units(q.last) if q else None,
            underlying_price_units=_units(q.underlying_price) if q else None, iv=q.iv if q else None,
            delta=q.delta if q else None, volume=q.volume if q else None, open_interest=q.open_interest if q else None,
            spread_pct=q.spread_pct if q else None, conditions_json=[r.as_dict() for r in results], freshness=fresh,
            provider=snap.provider if snap else self.quotes.provider.name, fetched_at=snap.fetched_at if snap else None,
            keep=keep))
        cs = _state(s, "contract", c.id, "quote")
        cs.checked_at, cs.status = now, q.status if q else "UNAVAILABLE"
        cs.data_json = {"conditions": [r.as_dict() for r in results], "freshness": fresh,
                        "reason": q.reason if q else (snap.error if snap else None)}
        return out

    # ------------------------------------------------------------------ alert rules
    def _rule_context(self, r: AlertRule, checks, by_pos, by_contract, contract_quotes, snaps, bars, earn, today,
                      cache) -> Context | None:
        if r.target_type == "position":
            chk = checks.get(r.position_id)
            p = by_pos.get(r.position_id)
            if chk is None or p is None:
                return None
            b, berr = bars.get((p.underlying, r.timeframe), (None, None))
            return Context("position", chk.metrics, chk.why_missing, b, berr, r.timeframe, snaps[p.underlying].rows)
        if r.target_type == "contract":
            return cache.get(f"contract:{r.contract_id}")
        return self._symbol_context((r.symbol or "").upper(), r.timeframe, snaps, bars, earn, today)

    @staticmethod
    def _symbol_context(sym: str, timeframe: str, snaps, bars, earn, today) -> Context:
        sn = snaps.get(sym)
        price = sn.underlying_price() if sn else None
        b, berr = bars.get((sym, timeframe), (None, None))
        names = {"price": None if price is None else float(price),
                 "days_to_earnings": (earn[sym] - today).days if earn.get(sym) else 9999}
        why = {"price": f"no option chain for {sym} ({sn.error if sn else 'not fetched'})"} if price is None else {}
        return Context("symbol", names, why, b, berr, timeframe, sn.rows if sn else None)

    def _write_alert(self, s: Session, r: AlertRule, ctx: Context | None, st, now, run_id, by_pos, by_contract) -> list[int]:
        state = _state(s, "alert_rule", r.id)
        res: RuleResult = ctx.evaluate(r.condition, "alert") if ctx else RuleResult("alert", r.condition, None,
                                                                                     reason="target not checked this pass")
        state.checked_at = now
        state.data_json = {"last": res.as_dict()}
        act = step_alert(state, res.result, now, r.confirm_passes, r.rearm_minutes, r.repeat_minutes)
        if act is None:
            return []
        p = by_pos.get(r.position_id) if r.position_id else None
        c = by_contract.get(r.contract_id) if r.contract_id else None
        symbol = p.underlying if p else c.underlying if c else r.symbol
        what = p.name if p else c.occ_symbol if c else r.symbol
        muted = None if r.notify else "notifications are off for this alert"
        if p is not None and not (p.monitor_json or {}).get("notify", True):
            muted = "notifications are off for this position"
        if act == "clear":
            ev = emit(s, "ALERT_CLEARED", "info", f"Cleared: {r.name}", f"{r.condition} no longer holds for {what}.",
                      symbol=symbol, account_id=r.account_id, position_id=r.position_id, contract_id=r.contract_id,
                      alert_rule_id=r.id, run_id=run_id, source={"rule": res.as_dict()}, now=now)
        else:
            ev = emit(s, "ALERT_TRIGGERED" if act == "fire" else "ALERT_REMINDER", r.severity,
                      f"{'Alert' if act == 'fire' else 'Alert (still)'}: {r.name}",
                      f"{what}: {explain(res)}.", settings=st, symbol=symbol, account_id=r.account_id,
                      position_id=r.position_id, contract_id=r.contract_id, alert_rule_id=r.id, run_id=run_id,
                      source={"rule": res.as_dict()}, dedupe_key=f"alert:{r.id}:{act}", muted=muted, now=now)
        return [ev.seq] if ev else []

    # ------------------------------------------------------------------ monitor health and retention
    def _monitor_state(self, s, summ: PassSummary, st, now, run_id, out: list[int]) -> None:
        ms = _state(s, "monitor", "engine", "health")
        failing = not summ.ok
        if failing and not ms.active:
            ms.active, ms.last_event_at = True, now
            ev = emit(s, "MONITOR_ERROR", "warning", "Monitor pass failed",
                      "; ".join(summ.errors[:5]) or "unknown error", settings=st, run_id=run_id,
                      source={"errors": summ.errors[:20]}, dedupe_key="monitor:error", now=now)
            if ev:
                out.append(ev.seq)
        elif not failing and ms.active:
            ms.active, ms.last_cleared_at = False, now
            emit(s, "MONITOR_RECOVERED", "info", "Monitor recovered", "The last pass completed normally.",
                 run_id=run_id, now=now)
        ms.checked_at = now

    def _retention(self, s: Session, st: MonitorSettings, now: datetime) -> None:
        """Once a day: snapshots older than `retention_full_days` are thinned to the last one per position
        per hour. Snapshots marking a status change (`keep`) are never removed."""
        rs = _state(s, "monitor", "engine", "retention")
        if rs.checked_at is not None and rs.checked_at.date() == now.date():
            return
        rs.checked_at = now
        cutoff = now - timedelta(days=st.retention_full_days)
        for model, owner in ((PositionSnapshot, PositionSnapshot.position_id), (ContractSnapshot, ContractSnapshot.contract_id)):
            hour = func.strftime("%Y-%m-%d %H", model.taken_at)
            survivors = select(func.max(model.id)).where(model.taken_at < cutoff).group_by(owner, hour)
            s.execute(delete(model).where(model.taken_at < cutoff, model.keep.is_(False), model.id.not_in(survivors)))


def urgent(status: str | None) -> bool:
    return status in (STALE, EXIT, ACTION)
