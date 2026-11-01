"""/api/v1 routes. Thin: validation (schemas), one transaction per request (deps), domain logic in
optlab.portfolio.service. List endpoints paginate (page, page_size ≤ 200)."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse

from ..portfolio import csv_import as cimp
from ..portfolio import service as svc
from ..portfolio.money import fmt
from ..portfolio.quotes import earnings_date
from ..portfolio.schemas import (AccountCreate, AccountOut, AccountUpdate, AnalysisOut, AnalyzeIn, ArchiveIn,
                                 ContractIn, ContractOut, ContractUpdate, Page, PositionArchive, PositionClose,
                                 PositionIn, PositionOut, PositionUpdate, PreferencesIn, PreferencesOut, SnapshotCreate,
                                 SnapshotOut)
from ..portfolio.valuation import value_position
from . import serialize as ser
from .deps import Read, Write, quotes_of

router = APIRouter()


def _views(request: Request, s, positions) -> dict:
    from datetime import datetime, timezone

    from ..monitoring import settings as msettings
    from ..monitoring.read import position_views
    from .monitor_routes import monitor_note

    st = msettings.load(s)
    now = datetime.now(timezone.utc)
    return position_views(s, list(positions), st, now, monitor_note(request, st, now))


def _pos(request: Request, s, p) -> PositionOut:
    return ser.position(p, monitor=_views(request, s, [p])[p.id])
PageNo = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=200)]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


# ------------------------------------------------------------------ health and sources
@router.get("/health", tags=["system"])
def health(s: Read):
    from ..portfolio.db import head_revision

    rev = s.connection().exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    return {"status": "ok", "database": {"revision": rev, "head": head_revision(),
                                         "journal_mode": s.connection().exec_driver_sql("PRAGMA journal_mode").scalar()}}


@router.get("/data-sources", tags=["system"])
def data_sources(request: Request):
    q = quotes_of(request)
    return {"quotes": q.status(), "research_lab_url": request.app.state.research_url,
            "notes": ["Option quotes are delayed about 15 minutes (Cboe through OpenBB) unless a real-time provider is "
                      "configured.", "Position values are estimates from those quotes (mid prices), recorded by the "
                      "monitor on every pass and shown with their age.",
                      "Positions imported from scans.yaml are monitored here; the YAML monitor skips them."]}


# ------------------------------------------------------------------ accounts
@router.get("/accounts", response_model=list[AccountOut], tags=["accounts"])
def list_accounts(s: Read, include_archived: bool = False):
    return [ser.account(a, svc.latest_snapshot(s, a.id)) for a in svc.list_accounts(s, include_archived)]


@router.post("/accounts", response_model=AccountOut, status_code=201, tags=["accounts"])
def create_account(body: AccountCreate, s: Write):
    a = svc.create_account(s, body)
    return ser.account(a, None)


@router.get("/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
def get_account(account_id: str, s: Read):
    return ser.account(svc.get_account(s, account_id), svc.latest_snapshot(s, account_id))


@router.patch("/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
def update_account(account_id: str, body: AccountUpdate, s: Write):
    a = svc.update_account(s, account_id, body)
    return ser.account(a, svc.latest_snapshot(s, account_id))


@router.post("/accounts/{account_id}/archive", response_model=AccountOut, tags=["accounts"])
def archive_account(account_id: str, body: ArchiveIn, s: Write):
    return ser.account(svc.archive_account(s, account_id, body.version), svc.latest_snapshot(s, account_id))


@router.delete("/accounts/{account_id}", status_code=204, tags=["accounts"])
def delete_account(account_id: str, s: Write):
    svc.delete_account(s, account_id)
    return Response(status_code=204)


@router.post("/accounts/{account_id}/snapshots", response_model=SnapshotOut, status_code=201, tags=["accounts"])
def add_snapshot(account_id: str, body: SnapshotCreate, s: Write):
    return ser.snapshot(svc.add_snapshot(s, account_id, body))


@router.get("/accounts/{account_id}/overview", tags=["accounts"])
def account_overview(account_id: str, request: Request, s: Read):
    from decimal import Decimal

    o = svc.account_overview(s, account_id)
    o["snapshot"] = ser.snapshot(o["snapshot"])
    items, _ = svc.list_positions(s, account_id, ["OPEN"], page_size=100_000)
    views = _views(request, s, items)
    counts: dict[str, int] = {}
    pnl, pnl_n, greeks, greeks_ok, newest = Decimal(0), 0, {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}, True, None
    for p in items:
        v = views[p.id]
        counts[v["status"]] = counts.get(v["status"], 0) + 1
        sn = v["snapshot"] if v["status"] not in ("NOT_MONITORED",) and not v["stale_overlay"] else None
        if sn and sn["open_pnl"] is not None:
            pnl += Decimal(sn["open_pnl"])
            pnl_n += 1
        for k in greeks:
            g = sn["greeks"].get(k) if sn else None
            if g is None:
                greeks_ok = False
            else:
                greeks[k] += g
        if v["checked_at"] and (newest is None or v["checked_at"] > newest):
            newest = v["checked_at"]
    o["monitoring"] = {"statuses": counts, "attention": sum(n for k, n in counts.items() if k in
                                                           ("STALE_DATA", "EXIT_CONDITION_MET", "ACTION_REQUIRED", "WATCH")),
                       "open_pnl": fmt(pnl) if items else None, "open_pnl_positions": pnl_n,
                       "open_pnl_complete": pnl_n == len(items),
                       "greeks": {k: round(x, 4) for k, x in greeks.items()} if greeks_ok and items else None,
                       "last_checked_at": newest, "label": "ESTIMATED"}
    from datetime import datetime, timezone

    from ..planning import plans as pl
    from ..planning import policy as pol
    from ..planning import riskcheck as rc

    settings, row = pol.load(s, account_id)
    port = rc.portfolio_state(s, account_id, datetime.now(timezone.utc), pl._tz(s))
    eff = pol.effective(settings, port.account_value)
    util = rc.utilization(eff, port, row)
    o["risk"] = {"overall": util.overall, "summary": util.summary, "checks": [c.__dict__ for c in util.checks],
                 "realized_today": fmt(port.realized_today), "realized_week": fmt(port.realized_week),
                 "small_account_active": eff["small_account_active"], "small_account_reason": eff["small_account_reason"],
                 "policy_revision": row.revision if row else None}
    return o


# ------------------------------------------------------------------ positions
@router.post("/positions/analyze", response_model=AnalysisOut, tags=["positions"])
def analyze(body: AnalyzeIn, request: Request, s: Read):
    """Recognise the strategy and calculate cost, maximum gain/loss, breakevens, buying-power effect and,
    when quotes are available, Greeks. Persists nothing."""
    if body.account_id:
        svc.get_account(s, body.account_id)
    earn = earnings_date(body.underlying)
    a = svc.analyze_legs(body.legs, body.underlying, svc.context_for(s, body.account_id, body.underlying,
                                                                     body.cash_secured, earn))
    quotes = None
    if body.include_quotes and not a.errors:
        quotes = _quote_legs(request, body)
    return ser.analysis(a, quotes)


def _quote_legs(request: Request, body: AnalyzeIn) -> dict:
    q = quotes_of(request)
    snap = q.chain(body.underlying)
    now_fresh = snap.freshness(q.max_age)
    legs, greeks, complete = [], {k: 0.0 for k in ("delta", "gamma", "theta", "vega")}, True
    for i, lg in enumerate(body.legs):
        sign = 1 if lg.side == "BUY" else -1
        if lg.asset_type == "STOCK":
            legs.append({"leg": i, "status": "FOUND" if snap.underlying_price() is not None else "UNAVAILABLE",
                         "mid": fmt(snap.underlying_price())})
            greeks["delta"] += sign * lg.quantity
            continue
        cq, _ = q.contract(body.underlying, lg.expiration, lg.option_right, lg.strike, snap)
        n = sign * lg.quantity * (lg.multiplier or 100)
        legs.append({"leg": i, "status": cq.status, "reason": cq.reason, "occ_symbol": cq.occ_symbol,
                     "bid": fmt(cq.bid), "ask": fmt(cq.ask), "mid": fmt(cq.mid), "iv": cq.iv, "delta": cq.delta,
                     "quality": cq.quality, "nearest": cq.nearest})
        for k in greeks:
            v = getattr(cq, k)
            if v is None:
                complete = False
            else:
                greeks[k] += v * n
    return {"freshness": now_fresh, "provider": snap.provider, "delay_minutes": snap.delay_minutes,
            "fetched_at": snap.fetched_at, "quote_age_seconds": snap.age_seconds(), "error": snap.error,
            "underlying_price": fmt(snap.underlying_price()), "greeks": greeks if complete else None,
            "greeks_source": snap.greeks_source, "legs": legs}


@router.get("/positions", response_model=Page[PositionOut], tags=["positions"])
def list_positions(s: Read, request: Request, account_id: str | None = None,
                   lifecycle: Annotated[list[Literal["OPEN", "CLOSED", "ARCHIVED"]] | None, Query()] = None,
                   underlying: str | None = None, strategy: str | None = None, page: PageNo = 1,
                   page_size: PageSize = 50, sort: str = "-opened_at"):
    items, total = svc.list_positions(s, account_id, lifecycle, underlying, strategy, page, page_size, sort)
    views = _views(request, s, items)
    return Page[PositionOut](items=[ser.position(p, monitor=views[p.id]) for p in items], total=total, page=page,
                             page_size=page_size)


@router.post("/positions", response_model=PositionOut, status_code=201, tags=["positions"])
def create_position(body: PositionIn, request: Request, response: Response, s: Write, idempotency_key: IdemKey = None):
    h = svc.request_hash(body.model_dump(mode="json"))
    prev = svc.idempotent_lookup(s, idempotency_key, "positions.create", h)
    if prev:
        response.status_code = prev.status_code
        response.headers["Idempotent-Replay"] = "true"
        return prev.response_json
    from ..planning import plans as pl

    earn = earnings_date(body.underlying)
    rep = None
    if not svc.analyze_legs(body.legs, body.underlying, svc.context_for(s, body.account_id, body.underlying,
                                                                         body.cash_secured, earn)).errors:
        info = None
        try:
            info = pl.leg_quotes(quotes_of(request), body.underlying, body.legs)
        except Exception:                      # quotes are optional for the check: liquidity becomes UNKNOWN
            info = None
        _, rep = pl.check_legs(s, body.account_id, body.underlying, body.legs, body.cash_secured, earn, info, False)
        if rep.overall == "BLOCKED" and not body.acknowledge_rule_violation:
            raise svc.DomainError("RULE_VIOLATION", "This position breaks your risk policy. Record it anyway only if it "
                                                    "already happened at the broker (acknowledge_rule_violation): it "
                                                    "will be flagged for review.", 422, details={"risk": rep.as_dict()})
    p = svc.create_position(s, body, earn)
    if rep is not None:
        pl.record_risk(s, p, rep)
    out = _pos(request, s, p)
    svc.idempotent_store(s, idempotency_key, "positions.create", h, 201, out.model_dump(mode="json"))
    return out


@router.get("/positions/export.csv", response_class=PlainTextResponse, tags=["positions"])
def export_positions(s: Read, account_id: str | None = None,
                     lifecycle: Annotated[list[Literal["OPEN", "CLOSED", "ARCHIVED"]] | None, Query()] = None):
    items, _ = svc.list_positions(s, account_id, lifecycle, page_size=100_000)
    return PlainTextResponse(cimp.export(items), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=optlab-positions.csv"})


@router.get("/positions/{position_id}", response_model=PositionOut, tags=["positions"])
def get_position(position_id: str, request: Request, s: Read):
    return _pos(request, s, svc.get_position(s, position_id))


@router.patch("/positions/{position_id}", response_model=PositionOut, tags=["positions"])
def update_position(position_id: str, body: PositionUpdate, request: Request, s: Write):
    p = svc.get_position(s, position_id)
    return _pos(request, s, svc.update_position(s, position_id, body, earnings_date(p.underlying)))


@router.post("/positions/{position_id}/close", response_model=PositionOut, tags=["positions"])
def close_position(position_id: str, body: PositionClose, request: Request, s: Write):
    return _pos(request, s, svc.close_position(s, position_id, body))


@router.post("/positions/{position_id}/archive", response_model=PositionOut, tags=["positions"])
def archive_position(position_id: str, body: PositionArchive, request: Request, s: Write):
    from ..process import engine as pe
    from ..process.service import tz_of

    p = svc.archive_position(s, position_id, body)
    pe.refresh(s, p.account_id, tz_of(s))          # an entered-in-error archive revokes that position's points, with the reason
    return _pos(request, s, p)


@router.get("/positions/{position_id}/valuation", tags=["positions"])
def position_valuation(position_id: str, request: Request, s: Read):
    """On-demand estimate from the shared quote snapshot (labelled with provider, delay and age)."""
    p = svc.get_position(s, position_id)
    if p.lifecycle.value != "OPEN":
        return {"label": "NOT_APPLICABLE", "status": "UNAVAILABLE", "reasons": [f"The position is {p.lifecycle.value}."]}
    return ser.valuation(value_position(p, quotes_of(request)))


@router.get("/positions/{position_id}/analysis", response_model=AnalysisOut, tags=["positions"])
def position_analysis(position_id: str, s: Read):
    p = svc.get_position(s, position_id)
    from ..portfolio import recognize as rz

    ctx = rz.Context(cash_secured=bool(p.metadata_json.get("cash_secured")), today=date.today(),
                     earnings_date=earnings_date(p.underlying))
    return ser.analysis(rz.analyze(svc.legs_from_rows(p.legs), ctx))


@router.get("/positions/{position_id}/audit", tags=["positions"])
def position_audit(position_id: str, s: Read):
    svc.get_position(s, position_id)
    return [{"id": e.id, "action": e.action, "reason": e.reason, "at": e.at, "before": e.before_json,
             "after": e.after_json} for e in svc.audit_trail(s, "position", position_id)]


# ------------------------------------------------------------------ CSV import
async def _read_upload(request: Request, file: UploadFile) -> bytes:
    limit = request.app.state.max_upload
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise svc.DomainError("UPLOAD_TOO_LARGE", f"The file is larger than {limit // 1000:,} KB.", 413)
    if file.content_type not in ("text/csv", "application/vnd.ms-excel", "text/plain", "application/octet-stream"):
        raise svc.DomainError("UNSUPPORTED_TYPE", f"Upload a .csv file (got {file.content_type}).", 415)
    return data


@router.post("/imports/positions/preview", tags=["imports"])
async def import_preview(request: Request, file: UploadFile, account_id: str, s: Read):
    content = await _read_upload(request, file)
    return cimp.report_dict(cimp.analyse(s, content, account_id))


@router.post("/imports/positions/commit", tags=["imports"])
async def import_commit(request: Request, response: Response, file: UploadFile, account_id: str, s: Write,
                        mode: Literal["all_or_nothing", "valid_only"] = "all_or_nothing",
                        idempotency_key: IdemKey = None):
    content = await _read_upload(request, file)
    h = svc.request_hash({"content": svc.request_hash(content.decode("utf-8", "replace")), "account": account_id,
                          "mode": mode})
    prev = svc.idempotent_lookup(s, idempotency_key, "positions.import", h)
    if prev:
        response.headers["Idempotent-Replay"] = "true"
        return prev.response_json
    rep, created = cimp.commit(s, content, account_id, mode)
    out = {"imported": [ser.position(p).model_dump(mode="json") for p in created], "report": cimp.report_dict(rep)}
    svc.idempotent_store(s, idempotency_key, "positions.import", h, 200, out)
    return out


# ------------------------------------------------------------------ watched contracts
@router.get("/watched-contracts", response_model=Page[ContractOut], tags=["contracts"])
def list_contracts(s: Read, account_id: str | None = None,
                   status: Annotated[list[str] | None, Query()] = None, underlying: str | None = None,
                   include_archived: bool = False, page: PageNo = 1, page_size: PageSize = 50):
    items, total = svc.list_contracts(s, account_id, status, underlying, include_archived, page, page_size)
    return Page[ContractOut](items=[ser.contract(w) for w in items], total=total, page=page, page_size=page_size)


@router.post("/watched-contracts", response_model=ContractOut, status_code=201, tags=["contracts"])
def create_contract(body: ContractIn, s: Write):
    return ser.contract(svc.create_contract(s, body))


@router.get("/watched-contracts/{contract_id}", response_model=ContractOut, tags=["contracts"])
def get_contract(contract_id: str, s: Read):
    return ser.contract(svc.get_contract(s, contract_id))


@router.patch("/watched-contracts/{contract_id}", response_model=ContractOut, tags=["contracts"])
def update_contract(contract_id: str, body: ContractUpdate, s: Write):
    return ser.contract(svc.update_contract(s, contract_id, body))


@router.post("/watched-contracts/{contract_id}/archive", response_model=ContractOut, tags=["contracts"])
def archive_contract(contract_id: str, body: ArchiveIn, s: Write):
    return ser.contract(svc.archive_contract(s, contract_id, body.version))


@router.get("/watched-contracts/{contract_id}/quote", tags=["contracts"])
def contract_quote(contract_id: str, request: Request, s: Read):
    from ..portfolio.money import from_units

    w = svc.get_contract(s, contract_id)
    qs = quotes_of(request)
    cq, snap = qs.contract(w.underlying, w.expiration, w.option_right.value, from_units(w.strike_units))
    fresh = snap.freshness(qs.max_age)
    return {"label": "QUOTE", "freshness": fresh, "provider": snap.provider, "delay_minutes": snap.delay_minutes,
            "fetched_at": snap.fetched_at, "source_timestamp": snap.source_timestamp,
            "quote_age_seconds": None if snap.age_seconds() is None else round(snap.age_seconds(), 1),
            "status": cq.status, "reason": cq.reason, "nearest": cq.nearest, "occ_symbol": cq.occ_symbol,
            "bid": fmt(cq.bid), "ask": fmt(cq.ask), "mid": fmt(cq.mid), "last": fmt(cq.last), "spread": fmt(cq.spread),
            "spread_pct": None if cq.spread_pct is None else round(cq.spread_pct, 2), "volume": cq.volume,
            "open_interest": cq.open_interest, "iv": cq.iv, "delta": cq.delta, "gamma": cq.gamma, "theta": cq.theta,
            "vega": cq.vega, "underlying_price": fmt(cq.underlying_price), "intrinsic": fmt(cq.intrinsic),
            "extrinsic": fmt(cq.extrinsic), "breakeven": fmt(cq.breakeven), "quality": cq.quality,
            "greeks_source": snap.greeks_source}


# ------------------------------------------------------------------ preferences
@router.get("/preferences", response_model=PreferencesOut, tags=["preferences"])
def get_preferences(s: Read):
    return svc.get_preferences(s)


@router.put("/preferences", response_model=PreferencesOut, tags=["preferences"])
def set_preferences(body: PreferencesIn, s: Write):
    return svc.set_preferences(s, body)
