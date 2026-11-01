"""optlab API and dashboard server (FastAPI). Nothing here places orders.

    python -m optlab.api                  # 127.0.0.1:8600 (OPTLAB_API_HOST / OPTLAB_API_PORT)
    uvicorn optlab.api.main:create_app --factory --host 127.0.0.1 --port 8600

Startup: takes the single-instance lock for the database, runs forward Alembic migrations (never
downgrades), then serves /api/v1 and, at /, the built React dashboard (web/dist).

Settings (environment variables, all optional)
    OPTLAB_DB_URL          sqlite:///<project>/data/optlab.sqlite
    OPTLAB_API_HOST        127.0.0.1     (Docker sets 0.0.0.0 inside its private network only)
    OPTLAB_API_PORT        8600
    OPTLAB_CORS_ORIGINS    comma list; default http://127.0.0.1:8600,http://localhost:8600,
                           http://127.0.0.1:5173,http://localhost:5173 (the Vite dev server)
    OPTLAB_RESEARCH_URL    link to the Streamlit Research Lab; default: same host, port 8517 (or :8443 over HTTPS)
    OPTLAB_MAX_UPLOAD_BYTES 1000000
    OPTLAB_MONITOR         on (default) | off: run the portfolio monitor in this process (optlab.monitoring)
    OPTLAB_ALLOWED_HOSTS   Host names this server answers to (DNS-rebinding protection); default
                           localhost,127.0.0.1,::1,*.ts.net
    OPTLAB_MAX_BODY_BYTES  2000000: larger request bodies are refused (413)
    OPTLAB_NEWS            on (default) | off: fetch news and official events in this process (optlab.news)
    OPTLAB_SEC_IDENTITY    "Name email" for SEC EDGAR (overrides edgar.identity in config.yaml)
    FINNHUB_API_KEY        optional; Finnhub stays disabled without it
Monitoring: with OPTLAB_MONITOR=on the server runs monitoring passes on the schedule in a background
thread, holding a per-database monitor lock so passes never run twice (see monitoring/runner.py).
Authentication: none in Phase 1 (single user, localhost / Tailscale only). Routes take their session
through `deps.py`, so an auth dependency can be added there without touching the domain layer.
"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm.exc import StaleDataError

from ..config import PROJECT_ROOT
from ..portfolio import db as dbm
from ..portfolio.money import MoneyPrecisionError
from ..portfolio.service import DomainError

log = logging.getLogger("optlab.api")
WEB_DIST = PROJECT_ROOT / "web" / "dist"
DEFAULT_CORS = "http://127.0.0.1:8600,http://localhost:8600,http://127.0.0.1:5173,http://localhost:5173"


def error_body(code: str, message: str, field_errors=None, details=None, request_id: str | None = None) -> dict:
    return {"error": {"code": code, "message": message, "field_errors": field_errors or [], "details": details or {},
                      "request_id": request_id}}


def _engine(app: FastAPI):
    """The process's monitoring engine, sharing the API's quote service (one chain snapshot per symbol)."""
    if app.state.engine is None:
        from ..monitoring.engine import Engine
        from .deps import quotes_from_app

        app.state.engine = Engine(app.state.db, quotes_from_app(app), **app.state.engine_options)
    return app.state.engine


def run_manual_pass(app: FastAPI):
    """'Run now': in this process if it runs the monitor (or nobody does); refused if another process does."""
    from ..monitoring.runner import MonitorLock

    runner = app.state.runner
    if runner is not None and runner.lock.held:
        return runner.run_now("manual")
    lk = MonitorLock(app.state.db.url)
    if not lk.try_acquire():
        raise DomainError("MONITOR_ELSEWHERE", f"Another process runs the monitor for this database (pid "
                                               f"{lk.holder() or '?'}); run the pass there.", 409)
    try:
        return _engine(app).run_pass("manual")
    finally:
        lk.release()


def create_app(database: dbm.Database | None = None, quotes=None, migrate: bool = True, lock: bool = True,
               web_dist: Path | None = WEB_DIST, monitor: bool | None = None,
               engine_options: dict | None = None, news: bool | None = None,
               allowed_hosts: list[str] | None = None) -> FastAPI:
    db = database or dbm.Database()
    instance = dbm.InstanceLock(db.url) if lock else None
    run_monitor = (os.environ.get("OPTLAB_MONITOR", "on").lower() not in ("off", "0", "false", "no")
                   if monitor is None else monitor)
    run_news = (os.environ.get("OPTLAB_NEWS", "on").lower() not in ("off", "0", "false", "no")
                if news is None else news)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if instance:
            instance.acquire()
        if migrate:
            rev = dbm.upgrade(db.url)
            log.info("database at revision %s", rev)
        if run_monitor:
            from ..monitoring.runner import Runner

            app.state.runner = Runner(db, _engine(app))
            app.state.runner.start()
        if run_news:
            from ..news.runner import NewsRunner

            app.state.news_runner = NewsRunner(db)
            app.state.news_runner.start()
        try:
            yield
        finally:
            if app.state.runner is not None:
                app.state.runner.stop()
            if app.state.news_runner is not None:
                app.state.news_runner.stop()
            if app.state.analysis_jobs is not None:
                app.state.analysis_jobs.shutdown()
            db.dispose()
            if instance:
                instance.release()

    app = FastAPI(title="optlab API", version="1.0.0", lifespan=lifespan,
                  description="Portfolio, positions, watched contracts, monitoring and events. Informational only: no orders are placed.",
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/v1/openapi.json")
    app.state.db = db
    app.state.quotes = quotes
    app.state.engine = None
    app.state.runner = None
    app.state.news_runner = None
    app.state.analysis_jobs = None
    app.state.engine_options = engine_options or {}
    app.state.max_upload = int(os.environ.get("OPTLAB_MAX_UPLOAD_BYTES", 1_000_000))
    app.state.research_url = os.environ.get("OPTLAB_RESEARCH_URL") or None
    origins = [o.strip() for o in os.environ.get("OPTLAB_CORS_ORIGINS", DEFAULT_CORS).split(",") if o.strip()]
    if "*" in origins:
        raise ValueError("OPTLAB_CORS_ORIGINS may not contain '*': list the exact origins.")
    from .security import SecurityMiddleware, allowed_hosts_from_env

    hosts = allowed_hosts if allowed_hosts is not None else allowed_hosts_from_env()
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                       allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID"], allow_credentials=False)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(error_body(exc.code, exc.message, exc.field_errors, exc.details,
                                       request.state.request_id), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(str(x) for x in e["loc"] if x != "body"), "code": e["type"].upper(),
                   "message": e["msg"].removeprefix("Value error, ")} for e in exc.errors()]
        return JSONResponse(error_body("VALIDATION_ERROR", "Some fields are invalid.", fields, None,
                                       request.state.request_id), status_code=422)

    @app.exception_handler(MoneyPrecisionError)
    async def money_error(request: Request, exc: MoneyPrecisionError):
        return JSONResponse(error_body("MONEY_PRECISION", str(exc), None, None, request.state.request_id), 422)

    @app.exception_handler(StaleDataError)
    async def stale(request: Request, exc: StaleDataError):
        return JSONResponse(error_body("VERSION_CONFLICT", "Someone else changed this record at the same time. Reload "
                                                           "and try again.", None, None, request.state.request_id), 409)

    from .security import BodyTooLarge

    @app.exception_handler(BodyTooLarge)
    async def too_large(request: Request, exc: BodyTooLarge):
        return JSONResponse(error_body("BODY_TOO_LARGE", "The request body is too large.", None, None,
                                       request.state.request_id), 413)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error (request %s)", request.state.request_id)
        return JSONResponse(error_body("INTERNAL_ERROR", "Something went wrong on the server. The details are in the "
                                                         "server log.", None, None, request.state.request_id), 500)

    # outermost: refuses foreign hosts / origins and oversized bodies before anything else runs
    app.add_middleware(SecurityMiddleware, allowed_hosts=hosts, cors_origins=origins,
                       max_body=int(os.environ.get("OPTLAB_MAX_BODY_BYTES", 2_000_000)))

    from .monitor_routes import router as monitor_router
    from .plan_routes import router as plan_router
    from .process_routes import router as process_router
    from .news_routes import router as news_router
    from .analysis_routes import router as analysis_router
    from .routes import router

    app.include_router(router, prefix="/api/v1")
    app.include_router(monitor_router, prefix="/api/v1")
    app.include_router(plan_router, prefix="/api/v1")
    app.include_router(process_router, prefix="/api/v1")
    app.include_router(news_router, prefix="/api/v1")
    app.include_router(analysis_router, prefix="/api/v1")

    if web_dist is not None and (web_dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse(error_body("NOT_FOUND", f"No API route /{path}."), 404)
            target = (web_dist / path).resolve()
            if path and target.is_file() and web_dist.resolve() in target.parents:
                return FileResponse(target)
            return FileResponse(web_dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def no_frontend():
            return JSONResponse({"message": "The dashboard isn't built yet: cd web && npm ci && npm run build. "
                                            "The API is at /api/v1 and its documentation at /api/docs."})
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("OPTLAB_API_HOST", "127.0.0.1")
    port = int(os.environ.get("OPTLAB_API_PORT", "8600"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info", proxy_headers=False)
