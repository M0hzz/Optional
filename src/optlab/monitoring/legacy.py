"""Bridge to the existing YAML monitor (scans.yaml + scripts/monitor.py).

Import   `preview` / `commit` copy the `positions:` of scans.yaml into the portfolio database once.
         Each keeps its exit_when / watch_when rules (or the YAML position_defaults it used), its
         timeframe, and a fingerprint of its legs. scans.yaml itself is never modified.
Skip     scripts/monitor.py asks `imported_fingerprints()` which YAML positions were imported and stops
         checking those itself, so the same trade never alerts twice. YAML positions that weren't
         imported keep working exactly as before.

The YAML has no opening date or fees; imported positions open "now" with zero fees and say so in their
notes (edit them afterwards). Prices in YAML are read as decimal text: more than 4 decimal places is an
error, never rounded.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import PROJECT_ROOT
from ..portfolio import db as dbm
from ..portfolio import service as svc
from ..portfolio.models import Position
from ..portfolio.schemas import LegIn, PositionIn
from .rules import validate
from .settings import DEFAULT_EXIT, DEFAULT_WATCH

SCANS_YAML = PROJECT_ROOT / "scans.yaml"


def _dec(x) -> str:
    try:
        return str(Decimal(str(x)).normalize())
    except (InvalidOperation, ValueError):
        return str(x)


def fingerprint(pos: dict) -> str:
    """Stable id of a YAML position: its symbol and legs (order-independent), not its name or rules."""
    legs = sorted(json.dumps({"type": str(lg.get("type", "")).lower(), "strike": _dec(lg.get("strike", "")),
                              "expiration": str(lg.get("expiration", "")), "qty": _dec(lg.get("qty", "")),
                              "price": _dec(lg.get("price", "")), "root": str(lg.get("root", "")).upper()},
                             sort_keys=True) for lg in pos.get("legs") or [])
    raw = json.dumps({"symbol": str(pos.get("symbol", "")).upper(), "legs": legs}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def read_yaml(path: Path | str | None = None) -> tuple[list[dict], dict]:
    with open(path or SCANS_YAML) as fh:
        cfg = yaml.safe_load(fh) or {}
    return list(cfg.get("positions") or []), dict(cfg.get("position_defaults") or {})


@dataclass
class Row:
    index: int
    name: str
    symbol: str
    fingerprint: str
    status: str = "new"                 # new | duplicate | invalid
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    strategy: str | None = None
    max_loss: dict | None = None
    needs_risk_ack: bool = False
    rules: dict = field(default_factory=dict)
    timeframe: str = "1d"
    position_in: PositionIn | None = None

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "position_in"}
        return d


def _money(x, what: str) -> Decimal:
    if x is None:
        raise ValueError(f"{what} is missing")
    d = Decimal(str(x))
    if d != d.quantize(Decimal("0.0001")):
        raise ValueError(f"{what} {x} has more than 4 decimal places")
    return d


def _legs(pos: dict) -> tuple[list[LegIn], list[str]]:
    legs, warns = [], []
    for i, lg in enumerate(pos.get("legs") or []):
        typ = str(lg.get("type", "")).lower()
        qty = Decimal(str(lg.get("qty", 0)))
        if qty == 0 or qty != qty.to_integral_value():
            raise ValueError(f"leg {i + 1}: qty must be a non-zero whole number")
        side = "BUY" if qty > 0 else "SELL"
        price = _money(lg.get("price"), f"leg {i + 1} price")
        if lg.get("root"):
            warns.append(f"leg {i + 1}: root {lg['root']} is kept in the notes; quotes match by expiration, type and strike")
        if typ == "stock":
            legs.append(LegIn(asset_type="STOCK", side=side, quantity=int(abs(qty)), fill_price=price))
        elif typ in ("call", "put"):
            exp = lg.get("expiration")
            exp = exp if isinstance(exp, date) else date.fromisoformat(str(exp))
            legs.append(LegIn(asset_type="OPTION", side=side, quantity=int(abs(qty)), option_right=typ.upper(),
                              strike=_money(lg.get("strike"), f"leg {i + 1} strike"), expiration=exp, multiplier=100,
                              fill_price=price))
        else:
            raise ValueError(f"leg {i + 1}: type must be stock, call or put (got {typ!r})")
    if not legs:
        raise ValueError("no legs")
    return legs, warns


def preview(s: Session, account_id: str, path: Path | str | None = None) -> list[Row]:
    svc.get_account(s, account_id)
    positions, defaults = read_yaml(path)
    existing = {(p.metadata_json or {}).get("yaml_fingerprint") for p in s.scalars(select(Position))} - {None}
    rows = []
    for i, pos in enumerate(positions):
        sym = str(pos.get("symbol", "")).upper()
        r = Row(i, str(pos.get("name") or sym), sym, fingerprint(pos), timeframe=str(pos.get("timeframe", "1d")))
        r.rules = {"exit_when": [str(c) for c in pos.get("exit_when", defaults.get("exit_when", DEFAULT_EXIT))],
                   "action_when": [],
                   "watch_when": [str(c) for c in pos.get("watch_when", defaults.get("watch_when", DEFAULT_WATCH))]}
        for c in r.rules["exit_when"] + r.rules["watch_when"]:
            err = validate(c, "position")
            if err:
                r.errors.append(err)
        try:
            legs, r.warnings = _legs(pos)
            r.position_in = PositionIn(account_id=account_id, underlying=sym, name=r.name[:160], legs=legs,
                                       acknowledge_risk=False,
                                       notes="Imported from scans.yaml. The opening date and fees weren't in the "
                                             "YAML: edit them if you know them."
                                             + ("\n" + "\n".join(r.warnings) if r.warnings else ""))
            a = svc.analyze_legs(legs, sym, svc.context_for(s, account_id, sym, False, None))
            r.strategy = a.name
            r.max_loss = {"state": a.max_loss.state, "value": None if a.max_loss.value is None else str(a.max_loss.value)}
            r.needs_risk_ack = a.max_loss.state != "bounded"
            r.errors += [e.message for e in a.errors]
        except (ValueError, InvalidOperation, TypeError) as exc:
            r.errors.append(str(exc).split("\n")[0][:300])
        if r.errors:
            r.status = "invalid"
        elif r.fingerprint in existing:
            r.status = "duplicate"
        rows.append(r)
    return rows


def commit(s: Session, account_id: str, acknowledge_risk: bool, path: Path | str | None = None) -> tuple[list[Row], list[Position]]:
    """Import every new, valid YAML position in one transaction. Positions with unlimited or unknown
    maximum loss are imported only with acknowledge_risk; otherwise they are reported and skipped."""
    from ..portfolio.quotes import earnings_date

    rows = preview(s, account_id, path)
    created = []
    for r in rows:
        if r.status != "new" or r.position_in is None:
            continue
        if r.needs_risk_ack and not acknowledge_risk:
            r.status = "skipped"
            r.errors.append("maximum loss is unlimited or unknown: tick 'I understand the risk' to import it")
            continue
        data = r.position_in.model_copy(update={"acknowledge_risk": r.needs_risk_ack and acknowledge_risk})
        p = svc.create_position(s, data, earnings_date(r.symbol), source="scans.yaml")
        p.metadata_json = {**(p.metadata_json or {}), "yaml_fingerprint": r.fingerprint, "yaml_name": r.name}
        p.monitor_json = {"rules": r.rules, "timeframe": r.timeframe, "notify": True}
        s.flush()
        r.status = "imported"
        created.append(p)
    return rows, created


# ------------------------------------------------------------------ used by scripts/monitor.py
def imported_fingerprints(url: str | None = None) -> set[str]:
    """Fingerprints of YAML positions already in the portfolio database (any lifecycle), read-only.
    Empty if there is no database yet or it can't be read."""
    path = dbm.sqlite_path(url or dbm.database_url())
    if path is None or not path.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT json_extract(metadata_json, '$.yaml_fingerprint') FROM positions "
                               "WHERE json_extract(metadata_json, '$.yaml_fingerprint') IS NOT NULL").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return set()
    return {r[0] for r in rows}


def split_imported(positions: list[dict[str, Any]], url: str | None = None) -> tuple[list[dict], list[str]]:
    """(positions the YAML monitor should still check, names of those the dashboard monitors now)."""
    done = imported_fingerprints(url)
    if not done:
        return positions, []
    keep, moved = [], []
    for p in positions:
        (moved if fingerprint(p) in done else keep).append(p)
    return keep, [str(p.get("name") or p.get("symbol")) for p in moved]
