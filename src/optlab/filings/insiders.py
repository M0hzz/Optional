"""SEC insider activity via EdgarTools.

Form 4 filings report insider trades within two business days. Open-market
purchases (transaction code "P") are the meaningful signal: insiders sell for many
reasons, but they buy for one. For an options seller, a cluster of insider buying
is a reason to prefer selling puts (bullish) and to avoid selling calls.
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

TRADE_COLS = ["symbol", "filing_date", "transaction_date", "insider", "title", "code", "shares", "price", "value"]


def _pick(row: dict, *names, default=None):
    for n in names:
        for k, v in row.items():
            if k.lower().replace(" ", "_") == n:
                return v
    return default


def _num(x) -> float:
    try:
        v = float(str(x).replace(",", "").replace("$", ""))
        return v if v == v else 0.0  # NaN -> 0
    except (TypeError, ValueError):
        return 0.0


def fetch_insider_trades(symbol: str, identity: str, lookback_days: int = 180, max_filings: int = 60) -> pl.DataFrame:
    """Download and normalize recent Form 4 transactions for one company."""
    from edgar import Company, set_identity

    set_identity(identity)
    since = date.today() - timedelta(days=lookback_days)
    filings = Company(symbol).get_filings(form="4")
    rows: list[tuple] = []
    for i, filing in enumerate(filings):
        if i >= max_filings or filing.filing_date < since:
            break
        try:
            form4 = filing.obj()
            df = form4.to_dataframe()
        except Exception:  # malformed or amended filings are skipped, not fatal
            continue
        if df is None or df.empty:
            continue
        insider = getattr(form4, "insider_name", None) or ""
        for rec in df.to_dict("records"):
            code = str(_pick(rec, "code", "transaction_code", default="") or "")
            shares = abs(_num(_pick(rec, "shares", "transaction_shares", default=0)))
            price = _num(_pick(rec, "price", "transaction_price", "price_per_share", default=0))
            value = _num(_pick(rec, "value", default=0)) or shares * price
            tdate = _pick(rec, "date", "transaction_date", default=filing.filing_date)
            rows.append((
                symbol, filing.filing_date, tdate,
                str(_pick(rec, "insider", "reporting_owner", default=insider) or insider),
                str(_pick(rec, "position", "title", "officer_title", default="") or ""),
                code, shares, price, value,
            ))
    if not rows:
        return pl.DataFrame(schema={c: pl.Utf8 for c in TRADE_COLS})
    out = pl.DataFrame(rows, schema=TRADE_COLS, orient="row", strict=False)
    return out.with_columns(
        pl.col("filing_date").cast(pl.Date),
        pl.col("transaction_date").cast(pl.Date, strict=False),
    )


def insider_signal(trades: pl.DataFrame, as_of: date | None = None, window_days: int = 90) -> pl.DataFrame:
    """Per-symbol summary: open-market buy/sell dollars, distinct buyers, a -1..+1 score."""
    if trades.is_empty():
        return pl.DataFrame(schema={"symbol": pl.Utf8, "buy_value": pl.Float64, "sell_value": pl.Float64,
                                    "buyers": pl.UInt32, "score": pl.Float64})
    as_of = as_of or date.today()
    recent = trades.filter(pl.col("transaction_date") >= as_of - timedelta(days=window_days))
    agg = recent.group_by("symbol").agg(
        pl.col("value").filter(pl.col("code") == "P").sum().alias("buy_value"),
        pl.col("value").filter(pl.col("code") == "S").sum().alias("sell_value"),
        pl.col("insider").filter(pl.col("code") == "P").n_unique().alias("buyers"),
    )
    # Buying counts ~5x more than selling; several distinct buyers ("cluster buying") counts most.
    return agg.with_columns(
        (((pl.col("buy_value") * 5 - pl.col("sell_value")) / (pl.col("buy_value") * 5 + pl.col("sell_value") + 1))
         * (1 + pl.col("buyers").clip(0, 4) / 4) / 2).clip(-1, 1).alias("score")
    ).sort("score", descending=True)


def synthetic_insider_trades(symbols: list[str], seed: int = 7) -> pl.DataFrame:
    """Fake Form 4 data so the dashboard has something to show offline."""
    import numpy as np

    rng = np.random.default_rng(seed)
    rows = []
    for s in symbols:
        for _ in range(rng.integers(3, 15)):
            d = date.today() - timedelta(days=int(rng.integers(1, 170)))
            code = "P" if rng.random() < 0.25 else "S"
            sh = float(rng.integers(500, 40000))
            px = float(rng.uniform(50, 500))
            rows.append((s, d + timedelta(days=2), d, f"Insider {rng.integers(1, 9)}",
                         rng.choice(["CEO", "CFO", "Director", "COO", "10% Owner"]), code, sh, px, sh * px))
    return pl.DataFrame(rows, schema=TRADE_COLS, orient="row")
