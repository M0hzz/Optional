"""Alembic environment for the portfolio database (data/optlab.sqlite by default; OPTLAB_DB_URL overrides).

Run from the project root:
    alembic upgrade head          # forward (the API also does this at startup, never backwards)
    alembic downgrade -1          # one step back: manual only, after a backup (see docs/DEPLOY.md)
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from optlab.portfolio.db import database_url, make_engine  # noqa: E402
from optlab.portfolio.models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url") or database_url(), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(config.get_main_option("sqlalchemy.url") or database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
