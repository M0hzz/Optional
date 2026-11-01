"""Phase 2 integration tests: monitoring passes against a migrated SQLite database with a fake delayed
provider and a fixed clock. Snapshots, exactly-one-event status changes, outages, notifications, alert
rules, watched contracts, warnings, closing, retention, locking and migration 0001 -> 0002."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from conftest import EXP, opt
from sqlalchemy import func, select

from optlab.monitoring import settings as msettings
from optlab.monitoring.engine import Engine
from optlab.monitoring.runner import MonitorLock
from optlab.monitoring.settings import MonitorSettings, RuleSet
from optlab.portfolio import service as svc
from optlab.portfolio.models import (AlertRule, ContractSnapshot, Event, MonitorRun, MonitorState, NotificationDelivery,
                                     PositionSnapshot, WatchedContract)
from optlab.portfolio.schemas import AccountCreate, ContractIn, LegIn, PositionClose, PositionIn

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def tick(self, minutes=1):
        self.t += timedelta(minutes=minutes)


class FakeBars:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def get(self, symbols, timeframe):
        self.calls += 1
        idx = pd.date_range("2026-08-01", periods=60, freq="D")
        df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": np.linspace(120, 100, 60), "volume": 1.0},
                          index=idx)
        return {s: df for s in symbols}, None


@pytest.fixture
def env(database, provider, quotes):
    quotes.ttl = 0                            # every pass refetches, so outages show at once
    clock, sent = Clock(), []
    quotes.wall = clock

    def notifier(title, body, urgency):
        sent.append((title, urgency))
        return True
    eng = Engine(database, quotes, bars=FakeBars(), notifier=notifier, earnings=lambda s: None, clock=clock)
    with database.session() as s:
        a = svc.create_account(s, AccountCreate(name="Main"))
        msettings.save(s, MonitorSettings(default_rules=RuleSet(exit_when=["pnl_pct >= 50"],
                                                                watch_when=["short_strike_otm_pct < 3"])))
    return {"db": database, "eng": eng, "clock": clock, "sent": sent, "provider": provider, "account": a.id,
            "quotes": quotes}


def spread(env, short=95, long=90, credit_short="2.00", credit_long="1.00", rules=None, exp=EXP):
    with env["db"].session() as s:
        p = svc.create_position(s, PositionIn(account_id=env["account"], underlying="XYZ",
                                              legs=[LegIn(**opt("SELL", "PUT", short, credit_short, exp=exp)),
                                                    LegIn(**opt("BUY", "PUT", long, credit_long, exp=exp))]))
        if rules is not None:
            p.monitor_json = {"rules": rules}
        return p.id


def events(env, type_=None):
    with env["db"].session(write=False) as s:
        q = select(Event).order_by(Event.seq)
        if type_:
            q = q.where(Event.type == type_)
        return list(s.scalars(q))


def run(env, n=1, minutes=1):
    out = []
    for _ in range(n):
        out.append(env["eng"].run_pass())
        env["clock"].tick(minutes)
    return out


def set_settings(env, **kw):
    with env["db"].session() as s:
        st = msettings.load(s).model_copy(update=kw)
        msettings.save(s, st)


# ------------------------------------------------------------------ snapshots and status events
def test_every_pass_stores_a_snapshot_and_hold_emits_nothing(env):
    pid = spread(env)
    r = run(env, 3)
    assert all(x.ok and x.positions == 1 for x in r)
    with env["db"].session(write=False) as s:
        snaps = list(s.scalars(select(PositionSnapshot).where(PositionSnapshot.position_id == pid)))
        assert len(snaps) == 3 and {x.status for x in snaps} == {"HOLD"}
        sn = snaps[-1]
        assert sn.open_pnl_units == 310000 and sn.freshness == "DELAYED" and sn.provider == "cboe (OpenBB)"
        assert sn.metrics_json["short_strike_otm_pct"] == 5.0 and sn.legs_json[0]["occ_symbol"].startswith("XYZ")
        assert s.scalar(select(func.count()).select_from(MonitorRun)) == 3
    assert [e.type for e in events(env) if e.type.startswith("POSITION_STATUS")] == []


def test_a_status_change_produces_exactly_one_event_after_confirmation(env):
    spread(env)
    run(env, 1)
    env["provider"].chain.loc[:, "underlying_price"] = 97.5          # short strike 95 is now 2.6% away -> WATCH
    run(env, 6)
    ch = events(env, "POSITION_STATUS_CHANGED")
    assert len(ch) == 1
    e = ch[0]
    assert e.title.startswith("HOLD → WATCH") and e.severity == "warning" and e.requires_ack
    assert "short_strike_otm_pct < 3 (short_strike_otm_pct = 2.6%" in e.message
    assert e.notify_state == "sent" and env["sent"] == [(e.title, "normal")]
    with env["db"].session(write=False) as s:
        assert s.scalar(select(func.count()).select_from(NotificationDelivery)) == 1
        kept = s.scalars(select(PositionSnapshot).where(PositionSnapshot.keep.is_(True))).all()
        assert len(kept) == 1 and kept[0].id == e.snapshot_id


def test_provider_outage_is_stale_at_once_and_recovery_is_confirmed(env):
    spread(env)
    run(env, 2)
    env["provider"].fail = True
    run(env, 1)
    ch = events(env, "POSITION_STATUS_CHANGED")
    assert len(ch) == 1 and "STALE DATA" in ch[0].title and "simulated outage" in ch[0].message
    env["provider"].fail = False
    run(env, 2)
    assert len(events(env, "POSITION_STATUS_CHANGED")) == 1        # 2 good passes: not yet confirmed
    run(env, 1)
    ch = events(env, "POSITION_STATUS_CHANGED")
    assert len(ch) == 2 and ch[1].title.startswith("STALE DATA → HOLD") and ch[1].severity == "info"


def test_no_chain_at_all_is_stale_never_hold(env):
    with env["db"].session() as s:
        svc.create_position(s, PositionIn(account_id=env["account"], underlying="NOOPT",
                                          legs=[LegIn(**opt("BUY", "CALL", 100, "1.00"))]))
    r = run(env, 1)[0]
    assert any("NOOPT" in e for e in r.errors)
    ev = events(env, "POSITION_STATUS_CHANGED")
    assert len(ev) == 1 and "STALE DATA" in ev[0].title and "no options listed" in ev[0].message


def test_exit_reminders_repeat_and_are_critical(env):
    spread(env, rules={"exit_when": ["pnl_pct >= 20"], "action_when": [], "watch_when": []})
    run(env, 1)
    assert events(env, "POSITION_STATUS_CHANGED")[0].severity == "critical"
    run(env, 20, minutes=1)
    rem = events(env, "POSITION_STATUS_REMINDER")
    assert len(rem) == 1 and "(still)" in rem[0].title                # repeat_urgent_minutes = 15


def test_desktop_failures_and_desktop_off_are_recorded(env):
    env["eng"].notifier = lambda t, b, u: False
    spread(env, rules={"exit_when": ["pnl_pct >= 20"], "action_when": [], "watch_when": []})
    run(env, 1)
    assert events(env, "POSITION_STATUS_CHANGED")[0].notify_state == "unavailable"
    set_settings(env, notifications=msettings.NotificationSettings(desktop=False))
    spread(env, short=100, long=95, rules={"exit_when": ["pnl_pct >= -500"], "action_when": [], "watch_when": []})
    run(env, 1)
    assert events(env, "POSITION_STATUS_CHANGED")[-1].notify_state == "browser"


def test_notifications_off_for_a_position_suppresses_but_still_records(env):
    pid = spread(env, rules={"exit_when": ["pnl_pct >= 20"], "action_when": [], "watch_when": []})
    with env["db"].session() as s:
        p = svc.get_position(s, pid)
        p.monitor_json = {**p.monitor_json, "notify": False}
    run(env, 1)
    e = events(env, "POSITION_STATUS_CHANGED")[0]
    assert e.notify_state == "suppressed" and "off for this position" in e.notify_detail and env["sent"] == []


def test_rules_using_bars_are_evaluated_from_the_bar_source(env):
    spread(env, rules={"exit_when": [], "action_when": [], "watch_when": ["rsi(14) < 30"]})
    run(env, 1)
    e = events(env, "POSITION_STATUS_CHANGED")[0]
    assert "WATCH" in e.title and "rsi(14)" in e.message and env["eng"].bars.calls == 1


def test_closing_stops_monitoring_and_marks_closed(env):
    pid = spread(env)
    run(env, 1)
    with env["db"].session() as s:
        p = svc.get_position(s, pid)
        svc.close_position(s, pid, PositionClose(version=p.version, legs=[
            {"leg_id": lg.id, "close_price": "0.5"} for lg in p.legs]))
    r = run(env, 1)[0]
    assert r.positions == 0
    with env["db"].session(write=False) as s:
        assert s.get(MonitorState, ("position", pid, "")).status == "CLOSED"
    assert events(env, "POSITION_CLOSED")[0].message.startswith("Realized P&L $100.00")


# ------------------------------------------------------------------ warnings
def test_expiration_warning_once_per_threshold(env):
    soon = date(2026, 11, 7)                                   # 5 days after T0
    env["provider"].chain = env["provider"].chain.assign(expiration=soon)
    spread(env, exp=soon, rules={"exit_when": [], "action_when": [], "watch_when": []})
    run(env, 5)
    w = events(env, "EXPIRATION_WARNING")
    assert len(w) == 1 and "in 5 days" in w[0].title and w[0].severity == "warning"
    env["clock"].t = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
    run(env, 3)
    w = events(env, "EXPIRATION_WARNING")
    assert len(w) == 2 and "in 1 day" in w[1].title and w[1].severity == "critical"


def test_liquidity_warning_fires_once_and_rearms_after_clearing(env):
    spread(env)
    run(env, 1)
    env["provider"].chain = env["provider"].chain.assign(ask=lambda d: d["bid"] + 2.0)    # spreads blow out
    run(env, 4)
    assert len(events(env, "LIQUIDITY_WARNING")) == 1


# ------------------------------------------------------------------ alert rules
def test_symbol_price_alert_fires_clears_and_rearms(env):
    with env["db"].session() as s:
        s.add(AlertRule(name="XYZ below 99", target_type="symbol", symbol="XYZ", condition="price < 99",
                        rearm_minutes=10, metadata_json={}))
    run(env, 1)
    assert events(env, "ALERT_TRIGGERED") == []
    env["provider"].chain.loc[:, "underlying_price"] = 98.0
    run(env, 3)
    fired = events(env, "ALERT_TRIGGERED")
    assert len(fired) == 1 and "price = 98" in fired[0].message
    env["provider"].chain.loc[:, "underlying_price"] = 100.0
    run(env, 1)
    assert len(events(env, "ALERT_CLEARED")) == 1
    env["provider"].chain.loc[:, "underlying_price"] = 98.0
    run(env, 2)                                                  # back below, but within 10 min of the alert
    assert len(events(env, "ALERT_TRIGGERED")) == 1
    run(env, 1, minutes=10)
    run(env, 1)
    assert len(events(env, "ALERT_TRIGGERED")) == 2


def test_position_alert_rule_uses_position_values(env):
    pid = spread(env)
    with env["db"].session() as s:
        s.add(AlertRule(name="Half the credit", target_type="position", position_id=pid, condition="pnl_pct >= 30",
                        severity="critical", metadata_json={}))
    run(env, 2)
    a = events(env, "ALERT_TRIGGERED")
    assert len(a) == 1 and a[0].severity == "critical" and a[0].position_id == pid


# ------------------------------------------------------------------ watched contracts
def test_contract_conditions_qualify_and_lose_entry_with_confirmation(env):
    with env["db"].session() as s:
        wc = svc.create_contract(s, ContractIn(account_id=env["account"], underlying="XYZ", option_right="PUT",
                                               strike="95", expiration=EXP, status="WAITING_FOR_CONFIRMATION",
                                               target_entry="2.60", alert_conditions="ask <= target_entry\noi >= 500"))
        cid = wc.id
    run(env, 3)
    with env["db"].session(write=False) as s:
        assert s.get(WatchedContract, cid).status.value == "ENTRY_QUALIFIED"
        assert s.scalar(select(func.count()).select_from(ContractSnapshot)) == 3
    q = events(env, "CONTRACT_STATUS_CHANGED")
    assert len(q) == 1 and "Entry qualified" in q[0].title and "no order is placed" in q[0].message
    env["provider"].chain = env["provider"].chain.assign(ask=lambda d: d["ask"] + 1)
    run(env, 3)
    with env["db"].session(write=False) as s:
        assert s.get(WatchedContract, cid).status.value == "ENTRY_CONDITION_LOST"


def test_researching_contracts_are_quoted_but_never_change_status(env):
    with env["db"].session() as s:
        cid = svc.create_contract(s, ContractIn(underlying="XYZ", option_right="CALL", strike="100", expiration=EXP,
                                                alert_conditions="bid > 0")).id
    run(env, 4)
    with env["db"].session(write=False) as s:
        assert s.get(WatchedContract, cid).status.value == "RESEARCHING"


def test_expired_contract_is_marked_expired(env):
    with env["db"].session() as s:
        cid = svc.create_contract(s, ContractIn(underlying="XYZ", option_right="CALL", strike="100",
                                                expiration=date(2026, 11, 3))).id
    env["clock"].t = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
    run(env, 1)
    with env["db"].session(write=False) as s:
        assert s.get(WatchedContract, cid).status.value == "EXPIRED"


def test_bad_contract_conditions_are_rejected(env):
    with env["db"].session() as s, pytest.raises(svc.DomainError) as ei:
        svc.create_contract(s, ContractIn(underlying="XYZ", option_right="CALL", strike="100", expiration=EXP,
                                          alert_conditions="pnl_pct > 5"))
    assert ei.value.code == "BAD_CONDITION"


# ------------------------------------------------------------------ pass failures, retention, locking
def test_a_failing_pass_is_recorded_and_emits_one_monitor_error(env, monkeypatch):
    spread(env)
    monkeypatch.setattr(env["eng"], "_bars_needed", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    r = run(env, 3)
    assert not r[0].ok and "boom" in r[0].errors[0]
    assert len(events(env, "MONITOR_ERROR")) == 1
    monkeypatch.undo()
    run(env, 1)
    assert len(events(env, "MONITOR_RECOVERED")) == 1
    with env["db"].session(write=False) as s:
        assert [x.ok for x in s.scalars(select(MonitorRun).order_by(MonitorRun.started_at))] == [False] * 3 + [True]


def test_retention_thins_old_snapshots_but_keeps_status_changes(env):
    pid = spread(env, rules={"exit_when": ["pnl_pct >= 20"], "action_when": [], "watch_when": []})
    run(env, 30, minutes=2)                                      # one hour of snapshots
    env["clock"].t += timedelta(days=40)
    with env["db"].session() as s:
        s.get(MonitorState, ("monitor", "engine", "retention")).checked_at = T0
    env["provider"].fail = True
    run(env, 1)
    with env["db"].session(write=False) as s:
        old = s.scalars(select(PositionSnapshot).where(PositionSnapshot.position_id == pid,
                                                       PositionSnapshot.taken_at < T0 + timedelta(days=1))).all()
        hours = {x.taken_at.strftime("%H") for x in old}
        kept = [x for x in old if x.keep]                         # the status change and its reminders
        assert kept and len(old) <= len(hours) + len(kept)       # everything else: one per hour
        assert len(old) < 30


def test_only_one_monitor_lock_per_database(database):
    a, b = MonitorLock(database.url), MonitorLock(database.url)
    assert a.try_acquire() and not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


# ------------------------------------------------------------------ migration with existing data
def test_migration_0002_enables_open_positions_and_rolls_back(db_url):
    from alembic import command

    from optlab.portfolio import db as dbm

    dbm.upgrade(db_url, "0001")
    path = dbm.sqlite_path(db_url)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO accounts VALUES ('a', 'A', NULL, 'USD', 1, NULL, NULL, 1, '2026-11-01', '2026-11-01', "
                "'manual', NULL, '{}')")
    cols = ("id, account_id, name, underlying, strategy, strategy_exact, direction, lifecycle, opened_at, closed_at, "
            "fees_units, max_loss_state, max_gain_state, defined_risk, risk_acknowledged, monitoring_enabled, version, "
            "created_at, updated_at, source, metadata_json")
    for pid, life, closed in (("p1", "OPEN", None), ("p2", "CLOSED", "2026-11-01")):
        con.execute(f"INSERT INTO positions ({cols}) VALUES (?, 'a', 'n', 'XYZ', 'CUSTOM', 0, 'unknown', ?, '2026-10-01', "
                    "?, 0, 'unknown', 'unknown', 0, 0, 0, 1, '2026-10-01', '2026-10-01', 'manual', '{}')",
                    (pid, life, closed))
    con.commit()
    con.close()
    assert dbm.upgrade(db_url) == "0002"
    con = sqlite3.connect(path)
    assert dict(con.execute("SELECT id, monitoring_enabled FROM positions")) == {"p1": 1, "p2": 0}
    assert con.execute("SELECT monitor_json FROM positions WHERE id='p1'").fetchone()[0] == "{}"
    con.close()
    command.downgrade(dbm.alembic_config(db_url), "0001")
    con = sqlite3.connect(path)
    assert dict(con.execute("SELECT id, monitoring_enabled FROM positions")) == {"p1": 0, "p2": 0}
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE positions SET monitoring_enabled = 1")
    con.close()


def test_runner_runs_passes_on_the_schedule_and_stops(env):
    import time as _time

    from optlab.monitoring.runner import Runner

    spread(env)
    start = _time.monotonic()
    fast = lambda: T0 + timedelta(seconds=(_time.monotonic() - start) * 120)     # noqa: E731  2 minutes per second
    env["eng"].clock = fast
    r = Runner(env["db"], env["eng"], clock=fast)
    r.max_sleep = 0.05
    assert r.start()
    other = Runner(env["db"], env["eng"])
    assert not other.start() and other.state == "elsewhere"
    deadline = _time.monotonic() + 10
    while _time.monotonic() < deadline:
        with env["db"].session(write=False) as s:
            if (s.scalar(select(func.count()).select_from(MonitorRun)) or 0) >= 2:
                break
        _time.sleep(0.1)
    r.stop()
    with env["db"].session(write=False) as s:
        assert (s.scalar(select(func.count()).select_from(MonitorRun)) or 0) >= 2
    assert r.status()["state"] == "stopped" and not r.lock.held
