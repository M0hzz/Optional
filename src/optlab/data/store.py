"""Local market-data store: DuckDB on disk, Polars in memory.

Tables
------
prices          symbol, date, open, high, low, close, volume
option_chains   snapshot_date, symbol, expiration, strike, option_type,
                bid, ask, last, volume, open_interest, implied_vol, underlying_price
insider_trades  symbol, filing_date, transaction_date, insider, title, code,
                shares, price, value
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl

SCHEMA = {
    "prices": """
        CREATE TABLE IF NOT EXISTS prices (
            symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE, PRIMARY KEY (symbol, date))""",
    "option_chains": """
        CREATE TABLE IF NOT EXISTS option_chains (
            snapshot_date DATE, symbol VARCHAR, expiration DATE, strike DOUBLE,
            option_type VARCHAR, bid DOUBLE, ask DOUBLE, last DOUBLE, volume DOUBLE,
            open_interest DOUBLE, implied_vol DOUBLE, underlying_price DOUBLE,
            PRIMARY KEY (snapshot_date, symbol, expiration, strike, option_type))""",
    "insider_trades": """
        CREATE TABLE IF NOT EXISTS insider_trades (
            symbol VARCHAR, filing_date DATE, transaction_date DATE, insider VARCHAR,
            title VARCHAR, code VARCHAR, shares DOUBLE, price DOUBLE, value DOUBLE)""",
}

KEYS = {
    "prices": ["symbol", "date"],
    "option_chains": ["snapshot_date", "symbol", "expiration", "strike", "option_type"],
    "insider_trades": None,  # append-only, deduplicated on write
}


class Store:
    def __init__(self, db_path: str | Path, read_only: bool = False):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(self.db_path, read_only=read_only)
        if not read_only:
            for ddl in SCHEMA.values():
                self.con.execute(ddl)

    # ------------------------------------------------------------------ writes
    def upsert(self, table: str, df: pl.DataFrame) -> int:
        """Insert rows, replacing any with the same primary key."""
        if df.is_empty():
            return 0
        cols = [c for c in self.columns(table) if c in df.columns]
        df = df.select(cols)
        self.con.register("_incoming", df.to_arrow())
        col_list = ", ".join(cols)
        if KEYS[table]:
            self.con.execute(f"INSERT OR REPLACE INTO {table} ({col_list}) SELECT {col_list} FROM _incoming")
        else:
            self.con.execute(
                f"INSERT INTO {table} ({col_list}) SELECT DISTINCT {col_list} FROM _incoming "
                f"EXCEPT SELECT {col_list} FROM {table}"
            )
        self.con.unregister("_incoming")
        return df.height

    # ------------------------------------------------------------------- reads
    def query(self, sql: str, params: list | None = None) -> pl.DataFrame:
        return self.con.execute(sql, params or []).pl()

    def columns(self, table: str) -> list[str]:
        return [r[0] for r in self.con.execute(f"DESCRIBE {table}").fetchall()]

    def prices(self, symbol: str, start: str | None = None) -> pl.DataFrame:
        sql = "SELECT * FROM prices WHERE symbol = ?"
        params: list = [symbol]
        if start:
            sql += " AND date >= ?"
            params.append(start)
        return self.query(sql + " ORDER BY date", params)

    def close_matrix(self, symbols: list[str] | None = None) -> pl.DataFrame:
        """Wide frame: date x symbol closing prices."""
        df = self.query("SELECT date, symbol, close FROM prices ORDER BY date")
        if symbols:
            df = df.filter(pl.col("symbol").is_in(symbols))
        return df.pivot(on="symbol", index="date", values="close").sort("date")

    def latest_chain(self, symbol: str) -> pl.DataFrame:
        return self.query(
            """SELECT * FROM option_chains WHERE symbol = ? AND snapshot_date =
               (SELECT max(snapshot_date) FROM option_chains WHERE symbol = ?)
               ORDER BY expiration, option_type, strike""",
            [symbol, symbol],
        )

    def insider_trades(self, symbol: str | None = None) -> pl.DataFrame:
        if symbol:
            return self.query("SELECT * FROM insider_trades WHERE symbol = ? ORDER BY transaction_date DESC", [symbol])
        return self.query("SELECT * FROM insider_trades ORDER BY transaction_date DESC")

    def symbols(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT DISTINCT symbol FROM prices ORDER BY 1").fetchall()]

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
