"""CSV import (preview, then commit) and export of positions.

Schema (one row per leg; rows sharing `position_ref` form one position). Header names are exact:

    position_ref      required  any text grouping the rows of one position (e.g. "IC-1")
    underlying        required  e.g. SPY
    asset_type        required  STOCK or OPTION
    side              required  BUY or SELL
    quantity          required  whole number above zero
    option_right      options   CALL or PUT
    strike            options   decimal, at most 4 places
    expiration        options   YYYY-MM-DD
    fill_price        optional  per-share price, at most 4 decimal places
    fees              optional  total fees for the leg (default 0)
    filled_at         optional  ISO-8601 with a timezone, e.g. 2026-11-02T14:30:00-05:00
    opened_at         optional  ISO-8601 with a timezone (first non-empty value in the group is used)
    position_name     optional
    broker_order_id   optional
    notes             optional
    cash_secured      optional  true/false (short puts)
    acknowledge_risk  optional  true/false (needed for unlimited/unknown maximum loss)

Rules
    * Uploads are limited in size and rows, must be UTF-8, and are parsed in memory: no file paths.
    * Preview validates every row and every position and writes nothing.
    * Commit re-validates, then imports in ONE transaction. mode=all_or_nothing (default) imports nothing
      if anything is invalid; mode=valid_only imports the positions whose rows are all valid. A
      position is never imported with some of its legs missing.
    * Duplicates: the same leg twice within a position, or a position identical (account, underlying,
      opened date and every leg) to one already in the database, are reported and not imported.
    * Export escapes text cells that a spreadsheet would run as a formula (=, +, -, @, tab, CR).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any
from datetime import date

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import service as svc
from .models import Lifecycle, Position
from .money import fmt, from_units
from .schemas import LegIn, PositionIn

MAX_BYTES = 1_000_000
MAX_ROWS = 2_000
REQUIRED = ["position_ref", "underlying", "asset_type", "side", "quantity"]
COLUMNS = ["position_ref", "position_name", "underlying", "opened_at", "asset_type", "option_right", "strike",
           "expiration", "side", "quantity", "fill_price", "fees", "filled_at", "broker_order_id", "notes",
           "cash_secured", "acknowledge_risk"]
LEG_FIELDS = ["asset_type", "option_right", "strike", "expiration", "side", "quantity", "fill_price", "fees",
              "filled_at", "broker_order_id", "notes"]


@dataclass
class RowResult:
    row: int
    position_ref: str
    errors: list[str] = field(default_factory=list)


@dataclass
class GroupResult:
    position_ref: str
    rows: list[int]
    underlying: str | None
    payload: PositionIn | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    strategy: str | None = None
    max_loss: str | None = None
    max_loss_state: str | None = None
    duplicate_of: str | None = None

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass
class ImportReport:
    rows: list[RowResult]
    groups: list[GroupResult]
    file_errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.file_errors and all(g.valid for g in self.groups)


class CsvError(ValueError):
    pass


def _bool(v: str) -> bool:
    v = (v or "").strip().lower()
    if v in ("", "false", "0", "no", "n"):
        return False
    if v in ("true", "1", "yes", "y"):
        return True
    raise ValueError(f"{v!r} is not true/false")


def _parse_rows(content: bytes) -> tuple[list[dict], list[str]]:
    if len(content) > MAX_BYTES:
        raise CsvError(f"The file is larger than {MAX_BYTES // 1000:,} KB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvError("The file must be UTF-8 text (save it as CSV UTF-8).") from None
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED if c not in header]
    unknown = [c for c in header if c not in COLUMNS]
    errors = []
    if missing:
        errors.append(f"Missing required column(s): {', '.join(missing)}.")
    if unknown:
        errors.append(f"Unknown column(s): {', '.join(unknown)} (see the schema in docs/PORTFOLIO.md).")
    rows = []
    for i, raw in enumerate(reader, start=2):              # row 1 is the header
        if i - 1 > MAX_ROWS:
            raise CsvError(f"More than {MAX_ROWS:,} rows.")
        if None in raw:
            errors.append(f"Row {i}: more values than columns.")
            continue
        rows.append({"_row": i, **{k.strip(): (v or "").strip() for k, v in raw.items() if k}})
    if not rows and not errors:
        errors.append("The file has no data rows.")
    return rows, errors


def _leg(r: dict) -> LegIn:
    d: dict[str, Any] = {k: r.get(k) or None for k in LEG_FIELDS}
    d["quantity"] = int(d["quantity"]) if d["quantity"] and d["quantity"].lstrip("-").isdigit() else d["quantity"]
    for k in ("asset_type", "option_right", "side"):
        if d[k]:
            d[k] = d[k].upper()
    if d["fees"] is None:
        d.pop("fees")
    return LegIn.model_validate(d)


def _fingerprint(account_id: str, underlying: str, opened: date | None, legs) -> tuple:
    return (account_id, underlying, opened,
            tuple(sorted((lg["asset_type"], lg["right"], lg["strike_units"], lg["expiration"], lg["quantity"],
                          lg["side"], lg["fill_price_units"]) for lg in legs)))


def _leg_fp(lg: LegIn) -> dict:
    from .money import to_units

    return {"asset_type": lg.asset_type, "right": lg.option_right,
            "strike_units": None if lg.strike is None else to_units(lg.strike),
            "expiration": lg.expiration.isoformat() if lg.expiration else None, "quantity": lg.quantity,
            "side": lg.side, "fill_price_units": None if lg.fill_price is None else to_units(lg.fill_price)}


def analyse(s: Session, content: bytes, account_id: str) -> ImportReport:
    """Validate everything; write nothing."""
    svc.get_account(s, account_id)
    try:
        rows, file_errors = _parse_rows(content)
    except CsvError as exc:
        return ImportReport([], [], [str(exc)])
    row_results: list[RowResult] = []
    groups: dict[str, list[tuple[dict, LegIn | None]]] = {}
    for r in rows:
        rr = RowResult(r["_row"], r.get("position_ref", ""))
        if not rr.position_ref:
            rr.errors.append("position_ref is empty.")
        leg = None
        try:
            leg = _leg(r)
        except ValidationError as exc:
            rr.errors += [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        except ValueError as exc:
            rr.errors.append(str(exc))
        row_results.append(rr)
        groups.setdefault(rr.position_ref, []).append((r, leg if not rr.errors else None))

    existing = {}
    for p in s.scalars(select(Position).options(selectinload(Position.legs))
                       .where(Position.account_id == account_id, Position.lifecycle != Lifecycle.ARCHIVED)):
        legs = svc.leg_snapshot(p)["legs"]
        fp = _fingerprint(account_id, p.underlying, p.opened_at.date(),
                          [{**lg, "right": lg["right"]} for lg in legs])
        existing[fp] = p.id
    by_row = {rr.row: rr for rr in row_results}
    out = []
    for ref, items in groups.items():
        g = GroupResult(ref, [r["_row"] for r, _ in items], items[0][0].get("underlying") or None, None)
        bad = [r["_row"] for r, leg in items if leg is None]
        if bad:
            g.errors.append(f"Row(s) {', '.join(map(str, bad))} are invalid, so this position can't be imported.")
        unders = {r.get("underlying", "").upper() for r, _ in items}
        if len(unders) > 1:
            g.errors.append(f"Rows disagree on the underlying: {', '.join(sorted(unders))}.")
        seen: dict[tuple, int] = {}
        for r, leg in items:
            if leg is None:
                continue
            key = tuple(sorted(_leg_fp(leg).items()))
            if key in seen:
                msg = f"Row {r['_row']} duplicates row {seen[key]} (same leg and fill)."
                g.errors.append(msg)
                by_row[r["_row"]].errors.append(msg)
            seen[key] = r["_row"]
        if not g.errors:
            first = items[0][0]
            try:
                opened = next((r["opened_at"] for r, _ in items if r.get("opened_at")), None)
                payload = PositionIn.model_validate({
                    "account_id": account_id, "underlying": first["underlying"],
                    "name": next((r["position_name"] for r, _ in items if r.get("position_name")), None),
                    "opened_at": opened, "legs": [leg.model_dump() for _, leg in items if leg is not None],
                    "cash_secured": any(_bool(r.get("cash_secured", "")) for r, _ in items),
                    "acknowledge_risk": any(_bool(r.get("acknowledge_risk", "")) for r, _ in items)})
            except (ValidationError, ValueError) as exc:
                errs = exc.errors() if isinstance(exc, ValidationError) else [{"loc": (), "msg": str(exc)}]
                g.errors += [f"{'.'.join(str(x) for x in e['loc']) or 'position'}: {e['msg']}" for e in errs]
                payload = None
            if payload is not None:
                g.payload = payload
                a = svc.analyze_legs(payload.legs, payload.underlying,
                                     svc.context_for(s, account_id, payload.underlying, payload.cash_secured, None))
                g.strategy = a.name
                g.max_loss_state = a.max_loss.state
                g.max_loss = fmt(a.max_loss.value) if a.max_loss.value is not None else None
                g.errors += [e.message for e in a.errors]
                g.warnings += [w.message for w in a.warnings]
                if a.max_loss.state != "bounded" and not payload.acknowledge_risk:
                    g.errors.append(f"Maximum loss is {a.max_loss.state}: set acknowledge_risk=true to import it.")
                fp = _fingerprint(account_id, payload.underlying, (payload.opened_at.date() if payload.opened_at else None),
                                  [_leg_fp(lg) for lg in payload.legs])
                if payload.opened_at and fp in existing:
                    g.duplicate_of = existing[fp]
                    g.errors.append(f"Already in the database as position {existing[fp]}.")
        out.append(g)
    return ImportReport(row_results, out, file_errors)


def commit(s: Session, content: bytes, account_id: str, mode: str = "all_or_nothing") -> tuple[ImportReport, list[Position]]:
    """Import inside the caller's transaction; raises DomainError (and the caller rolls back) on failure."""
    if mode not in ("all_or_nothing", "valid_only"):
        raise svc.DomainError("BAD_MODE", "mode must be all_or_nothing or valid_only", 422)
    rep = analyse(s, content, account_id)
    if rep.file_errors or (mode == "all_or_nothing" and not rep.ok):
        raise svc.DomainError("IMPORT_INVALID", "Nothing was imported: fix the errors and try again "
                                                "(or import only the valid positions).", 422,
                              details={"report": report_dict(rep)})
    created = []
    for g in rep.groups:
        if g.valid and g.payload is not None:
            created.append(svc.create_position(s, g.payload, source="csv"))
    if not created:
        raise svc.DomainError("IMPORT_EMPTY", "No valid positions to import.", 422, details={"report": report_dict(rep)})
    return rep, created


def report_dict(rep: ImportReport) -> dict:
    return {"ok": rep.ok, "file_errors": rep.file_errors,
            "rows": [{"row": r.row, "position_ref": r.position_ref, "errors": r.errors} for r in rep.rows],
            "positions": [{"position_ref": g.position_ref, "rows": g.rows, "underlying": g.underlying,
                           "valid": g.valid, "errors": g.errors, "warnings": g.warnings, "strategy": g.strategy,
                           "max_loss": g.max_loss, "max_loss_state": g.max_loss_state,
                           "duplicate_of": g.duplicate_of, "legs": len(g.rows)} for g in rep.groups]}


# ------------------------------------------------------------------ export
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def _safe(v) -> str:
    """Text a spreadsheet would treat as a formula is prefixed with an apostrophe."""
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(_DANGEROUS) else s


def export(positions: list[Position]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for p in positions:
        for lg in p.legs:
            w.writerow([_safe(p.id), _safe(p.name), _safe(p.underlying), p.opened_at.isoformat(), lg.asset_type.value,
                        lg.option_right.value if lg.option_right else "",
                        fmt(from_units(lg.strike_units)) or "", lg.expiration.isoformat() if lg.expiration else "",
                        lg.side.value, lg.quantity, fmt(from_units(lg.fill_price_units)) or "",
                        fmt(from_units(lg.fees_units)), lg.filled_at.isoformat() if lg.filled_at else "",
                        _safe(lg.broker_order_id), _safe(lg.notes), str(bool(p.metadata_json.get("cash_secured"))).lower(),
                        str(bool(p.risk_acknowledged)).lower()])
    return buf.getvalue()

