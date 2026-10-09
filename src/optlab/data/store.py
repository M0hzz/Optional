"""Local market-data store: DuckDB on disk, Polars in memory.

Tables
------
prices          symbol, date, open, high, low, close, volume
option_chains   snapshot_date, symbol, expiration, strike, option_type,
                bid, ask, last, volume, open_interest, implied_vol, underlying_price
insider_trades  symbol, filing_date, transaction_date, insider, title, code,
                shares, price, value
fundamentals    symbol, as_of, market_cap, pe_ratio, forward_pe, dividend_yield, beta,
                debt_to_equity, profit_margin, revenue_growth, next_earnings
macro           date, series, value      (treasury_1m ... treasury_10y as decimals, vix)
filings         symbol, filing_date, form, accession_no, items, description, url
financials      symbol, metric, period_end, value, fiscal_period
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
    "fundamentals": """
        CREATE TABLE IF NOT EXISTS fundamentals (
            symbol VARCHAR, as_of DATE, market_cap DOUBLE, pe_ratio DOUBLE, forward_pe DOUBLE,
            dividend_yield DOUBLE, beta DOUBLE, debt_to_equity DOUBLE, profit_margin DOUBLE,
            revenue_growth DOUBLE, next_earnings DATE, PRIMARY KEY (symbol, as_of))""",
    "macro": """
        CREATE TABLE IF NOT EXISTS macro (
            date DATE, series VARCHAR, value DOUBLE, PRIMARY KEY (date, series))""",
    "filings": """
        CREATE TABLE IF NOT EXISTS filings (
            symbol VARCHAR, filing_date DATE, form VARCHAR, accession_no VARCHAR, items VARCHAR,
            description VARCHAR, url VARCHAR, PRIMARY KEY (symbol, accession_no))""",
    "financials": """
        CREATE TABLE IF NOT EXISTS financials (
            symbol VARCHAR, metric VARCHAR, period_end DATE, value DOUBLE, fiscal_period VARCHAR,
            PRIMARY KEY (symbol, metric, period_end))""",
}

KEYS = {
    "prices": ["symbol", "date"],
    "option_chains": ["snapshot_date", "symbol", "expiration", "strike", "option_type"],
    "insider_trades": None,  # append-only, deduplicated on write
    "fundamentals": ["symbol", "as_of"],
    "macro": ["date", "series"],
    "filings": ["symbol", "accession_no"],
    "financials": ["symbol", "metric", "period_end"],
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

    def has_table(self, table: str) -> bool:
        """False for tables added after a database was built; re-run ingest.py to create them."""
        return bool(self.con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]).fetchone()[0])

    def latest_fundamentals(self) -> pl.DataFrame:
        """Newest fundamentals snapshot per symbol."""
        return self.query("SELECT * FROM fundamentals QUALIFY row_number() OVER "
                          "(PARTITION BY symbol ORDER BY as_of DESC) = 1 ORDER BY symbol")

    def macro(self, series: list[str] | None = None) -> pl.DataFrame:
        df = self.query("SELECT * FROM macro ORDER BY date")
        return df.filter(pl.col("series").is_in(series)) if series else df

    def latest_macro(self, series: str) -> float | None:
        row = self.con.execute("SELECT value FROM macro WHERE series = ? AND value IS NOT NULL "
                               "ORDER BY date DESC LIMIT 1", [series]).fetchone()
        return float(row[0]) if row else None

    def filings(self, symbol: str | None = None) -> pl.DataFrame:
        if symbol:
            return self.query("SELECT * FROM filings WHERE symbol = ? ORDER BY filing_date DESC", [symbol])
        return self.query("SELECT * FROM filings ORDER BY filing_date DESC")

    def financials(self, symbol: str | None = None) -> pl.DataFrame:
        if symbol:
            return self.query("SELECT * FROM financials WHERE symbol = ? ORDER BY metric, period_end", [symbol])
        return self.query("SELECT * FROM financials ORDER BY symbol, metric, period_end")

    def symbols(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT DISTINCT symbol FROM prices ORDER BY 1").fetchall()]

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def risk_free_rate(store: Store, cfg: dict) -> float:
    """Latest Treasury yield from the macro table (config market.rate_series), else the config fallback."""
    fallback = cfg["market"]["risk_free_rate"]
    if not store.has_table("macro"):
        return fallback
    live = store.latest_macro(cfg["market"].get("rate_series", "treasury_3m"))
    return live if live is not None else fallback
