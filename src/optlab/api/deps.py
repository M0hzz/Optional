"""Request dependencies: one database transaction per request, and the shared quote service.

`Write` sessions start with BEGIN IMMEDIATE and commit when the handler returns, or roll back on any
exception (including DomainError). `Read` sessions use a plain BEGIN and never write.
An authentication dependency can be added to both here later without touching routes or the domain.
"""
from __future__ import annotations

from typing import Annotated, Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session


def _write(request: Request) -> Iterator[Session]:
    with request.app.state.db.session(write=True) as s:
        yield s


def _read(request: Request) -> Iterator[Session]:
    with request.app.state.db.session(write=False) as s:
        yield s


Write = Annotated[Session, Depends(_write)]
Read = Annotated[Session, Depends(_read)]


def quotes_from_app(app):
    """The process-wide QuoteService (created on first use from config.yaml's data.source), shared by the
    API and the monitor."""
    if app.state.quotes is None:
        from ..portfolio.quotes import default_service

        app.state.quotes = default_service()
    return app.state.quotes


def quotes_of(request: Request):
    return quotes_from_app(request.app)
