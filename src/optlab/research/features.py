"""Features for ranking underlyings as premium-selling candidates.

The question every short-premium trader asks: where is implied vol rich relative
to what the stock will actually do? These features describe each underlying's
volatility regime and trend; `rank_candidates` turns them into a simple score.
For an ML ranker trained on the same features, see integrations/qlib.
"""
from __future__ import annotations

import polars as pl


def build_features(prices: pl.DataFrame) -> pl.DataFrame:
    """prices: symbol, date, close (long format). Returns one row per symbol-date."""
    lr = (pl.col("close") / pl.col("close").shift(1)).log()
    df = prices.sort(["symbol", "date"]).with_columns(lr.over("symbol").alias("lr"))
    ann = 252 ** 0.5
    df = df.with_columns(
        (pl.col("lr").rolling_std(10) * ann).over("symbol").alias("rv10"),
        (pl.col("lr").rolling_std(20) * ann).over("symbol").alias("rv20"),
        (pl.col("lr").rolling_std(60) * ann).over("symbol").alias("rv60"),
        (pl.col("close") / pl.col("close").shift(20) - 1).over("symbol").alias("ret20"),
        (pl.col("close") / pl.col("close").shift(60) - 1).over("symbol").alias("ret60"),
        (pl.col("close") / pl.col("close").rolling_max(252) - 1).over("symbol").alias("drawdown"),
    )
    df = df.with_columns(
        (pl.col("rv20") / pl.col("rv60")).alias("rv_ratio"),
        pl.col("rv20").rolling_std(20).over("symbol").alias("vol_of_vol"),
        ((pl.col("rv20") - pl.col("rv20").rolling_min(252)) /
         (pl.col("rv20").rolling_max(252) - pl.col("rv20").rolling_min(252) + 1e-9))
        .over("symbol").alias("rv_rank"),
        # label for supervised models: forward 21-day realized vol minus today's rv20
        (pl.col("lr").rolling_std(21).shift(-21) * ann - pl.col("rv20")).over("symbol").alias("fwd_vol_change"),
    )
    return df.drop("lr")


def rank_candidates(features: pl.DataFrame, chain_iv: dict[str, float] | None = None,
                    insider_scores: dict[str, float] | None = None) -> pl.DataFrame:
    """Score each symbol on its latest row. Higher = better premium-selling candidate.

    Components (z-scored across the universe):
      + vol rich:   current ATM IV / rv20 (if chain IV supplied), else rv_rank
      + calming:    rv10 below rv20 (vol already falling)
      - turbulence: vol-of-vol
      + insiders:   insider buying score (supports selling puts)
    """
    latest = features.group_by("symbol").agg(pl.all().sort_by("date").last())
    if chain_iv:
        latest = latest.with_columns(
            pl.col("symbol").replace_strict(chain_iv, default=None, return_dtype=pl.Float64).alias("atm_iv"))
        latest = latest.with_columns((pl.col("atm_iv") / pl.col("rv20")).alias("iv_rv"))
        rich = "iv_rv"
    else:
        rich = "rv_rank"
    latest = latest.with_columns(
        pl.col("symbol").replace_strict(insider_scores or {}, default=0.0, return_dtype=pl.Float64).alias("insider"),
        (1 - pl.col("rv10") / pl.col("rv20")).alias("calming"),
    )

    def z(c):
        return ((pl.col(c) - pl.col(c).mean()) / (pl.col(c).std() + 1e-9)).fill_null(0.0)

    return latest.with_columns(
        (z(rich) + 0.5 * z("calming") - 0.5 * z("vol_of_vol") + 0.5 * pl.col("insider")).alias("score")
    ).sort("score", descending=True).select(
        ["symbol", "score", "close", rich, "rv10", "rv20", "rv60", "rv_rank", "vol_of_vol", "ret20", "drawdown", "insider"]
        if rich != "rv_rank" else
        ["symbol", "score", "close", "rv10", "rv20", "rv60", "rv_rank", "vol_of_vol", "ret20", "drawdown", "insider"]
    )


def export_for_qlib(features: pl.DataFrame, out_dir: str) -> None:
    """Write one CSV per symbol in the layout Qlib's dump_bin.py expects."""
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for (sym,), g in features.group_by("symbol"):
        g.drop("symbol").rename({"date": "date"}).write_csv(out / f"{sym}.csv")
