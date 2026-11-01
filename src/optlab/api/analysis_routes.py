"""/api/v1 historical analysis routes (Phase 6). Descriptive statistics only: nothing here predicts or recommends."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from ..analysis import drafts
from ..analysis import service as an
from ..history.strategy import _known_names
from ..portfolio.models import AnalysisRun
from ..portfolio.service import DomainError
from .deps import Read, Write, quotes_from_app

router = APIRouter()


def data_source(app) -> str:
    """Synthetic quotes mode searches synthetic bars; live mode searches real history (Yahoo + the local
    archive) and never synthetic bars."""
    forced = getattr(app.state, "analysis_source", None)
    if forced:
        return forced
    return "synthetic" if quotes_from_app(app).mode == "synthetic" else "yahoo"


@router.get("/analysis/draft", tags=["analysis"])
def draft(request: Request, s: Read, subject_type: str, subject_id: str | None = None, symbol: str | None = None):
    """A starting point for a historical analysis of a plan, position, alert rule, watched contract or event:
    its symbol, timeframe and the conditions that have price history (the others are listed with the reason)."""
    d = drafts.draft_for(s, subject_type, subject_id, symbol=symbol)
    d["source"] = data_source(request.app)
    return d


@router.get("/analysis/vocabulary", tags=["analysis"])
def vocabulary():
    names, funcs = _known_names()
    return {"names": sorted(names), "functions": sorted(funcs), "timeframes": list(an.SPAN_LIMITS),
            "span_limits_days": an.SPAN_LIMITS, "default_horizons": {tf: drafts.horizons_for(tf) for tf in an.SPAN_LIMITS},
            "horizon_help": "N = N bars after the signal; Nd = N trading sessions later (intraday bars)."}


@router.post("/analysis/runs", status_code=202, tags=["analysis"])
def create(request: Request, body: an.AnalysisIn, s: Write):
    """Queue a run; it executes in the background. Poll GET /analysis/runs/{id}."""
    run = an.create_run(s, body, data_source(request.app))
    s.flush()
    return {"id": run.id, "status": run.status, "strategy_version": run.strategy_version, "source": run.source}


@router.post("/analysis/runs/{run_id}/start", tags=["analysis"])
def start(request: Request, run_id: str, s: Read):
    """Start a queued run (the dashboard calls this right after creating it)."""
    run = s.get(AnalysisRun, run_id)
    if run is None:
        raise DomainError("NOT_FOUND", f"Analysis {run_id} does not exist.", 404)
    if run.status == "queued":
        if getattr(request.app.state, "analysis_sync", False):
            an.run_job(request.app.state.db, run_id, **(getattr(request.app.state, "analysis_overrides", None) or {}))
        else:
            jobs = request.app.state.analysis_jobs
            if jobs is None:
                jobs = request.app.state.analysis_jobs = an.Jobs(request.app.state.db)
            jobs.submit(run_id)
    return {"id": run_id, "status": "running" if run.status == "queued" else run.status}


@router.get("/analysis/runs", tags=["analysis"])
def runs(s: Read, subject_type: str | None = None, subject_id: str | None = None,
         limit: Annotated[int, Query(ge=1, le=200)] = 50):
    q = select(AnalysisRun)
    if subject_type:
        q = q.where(AnalysisRun.subject_type == subject_type)
    if subject_id:
        q = q.where(AnalysisRun.subject_id == subject_id)
    return [an.run_out(r, full=False) for r in s.scalars(q.order_by(AnalysisRun.created_at.desc()).limit(limit))]


@router.get("/analysis/runs/{run_id}", tags=["analysis"])
def get_run(run_id: str, s: Read):
    r = s.get(AnalysisRun, run_id)
    if r is None:
        raise DomainError("NOT_FOUND", f"Analysis {run_id} does not exist.", 404)
    return an.run_out(r)


@router.get("/analysis/runs/{run_id}/matches/{n}/bars", tags=["analysis"])
def match_bars(request: Request, run_id: str, n: int, s: Read):
    """Price bars around one match, with the signal bar and each horizon's end marked (drill-down chart)."""
    r = s.get(AnalysisRun, run_id)
    if r is None:
        raise DomainError("NOT_FOUND", f"Analysis {run_id} does not exist.", 404)
    ov = getattr(request.app.state, "analysis_overrides", None) or {}
    return an.match_window(r, n, bars_override=ov.get("bars_override"), now=ov.get("now"))
