"""Portfolio domain: accounts, positions and their legs, watched contracts.

Transactional data lives in SQLite (data/optlab.sqlite, WAL mode) through SQLAlchemy 2 and Alembic.
Market data and historical analytics stay in DuckDB (data/market.duckdb, data/history.duckdb).
Money is persisted as integers of $0.0001 (see money.py). Nothing here places orders.
"""
