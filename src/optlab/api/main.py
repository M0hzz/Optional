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


def create_app(database: dbm.Database | None = None, quotes=None, migrate: bool = True, lock: bool = True,
               web_dist: Path | None = WEB_DIST) -> FastAPI:
    db = database or dbm.Database()
    instance = dbm.InstanceLock(db.url) if lock else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if instance:
            instance.acquire()
        if migrate:
            rev = dbm.upgrade(db.url)
            log.info("database at revision %s", rev)
        try:
            yield
        finally:
            db.dispose()
            if instance:
                instance.release()

    app = FastAPI(title="optlab API", version="1.0.0", lifespan=lifespan,
                  description="Portfolio, positions and watched contracts. Informational only: no orders are placed.",
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/v1/openapi.json")
    app.state.db = db
    app.state.quotes = quotes
    app.state.max_upload = int(os.environ.get("OPTLAB_MAX_UPLOAD_BYTES", 1_000_000))
    app.state.research_url = os.environ.get("OPTLAB_RESEARCH_URL") or None
    origins = [o.strip() for o in os.environ.get("OPTLAB_CORS_ORIGINS", DEFAULT_CORS).split(",") if o.strip()]
    if "*" in origins:
        raise ValueError("OPTLAB_CORS_ORIGINS may not contain '*': list the exact origins.")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                       allow_headers=["Content-Type", "Idempotency-Key"], allow_credentials=False)

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

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error (request %s)", request.state.request_id)
        return JSONResponse(error_body("INTERNAL_ERROR", "Something went wrong on the server. The details are in the "
                                                         "server log.", None, None, request.state.request_id), 500)

    from .routes import router

    app.include_router(router, prefix="/api/v1")

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
