"""Helpers for option-chain snapshots."""
from __future__ import annotations

import polars as pl


def with_dte(chain: pl.DataFrame) -> pl.DataFrame:
    return chain.with_columns(
        (pl.col("expiration") - pl.col("snapshot_date")).dt.total_days().alias("dte"),
        ((pl.col("bid") + pl.col("ask")) / 2).alias("mid"),
        (pl.col("strike") / pl.col("underlying_price")).alias("moneyness"),
    )


def atm_iv(chain: pl.DataFrame, target_dte: int = 30) -> float | None:
    """Average call/put IV at the strike nearest spot, in the expiry nearest target_dte."""
    if chain.is_empty():
        return None
    c = with_dte(chain).filter(pl.col("implied_vol").is_not_null() & (pl.col("dte") > 3))
    if c.is_empty():
        return None
    exp = c.with_columns((pl.col("dte") - target_dte).abs().alias("gap")).sort("gap")["expiration"][0]
    c = c.filter(pl.col("expiration") == exp).with_columns((pl.col("moneyness") - 1).abs().alias("m"))
    k = c.sort("m")["strike"][0]
    return float(c.filter(pl.col("strike") == k)["implied_vol"].mean())


def skew_25d(chain: pl.DataFrame, target_dte: int = 30) -> float | None:
    """Rough skew: IV of ~0.90 moneyness put minus IV of ~1.10 moneyness call."""
    c = with_dte(chain)
    if c.is_empty():
        return None
    exp = c.with_columns((pl.col("dte") - target_dte).abs().alias("gap")).sort("gap")["expiration"][0]
    c = c.filter(pl.col("expiration") == exp)
    put = c.filter(pl.col("option_type") == "put").with_columns((pl.col("moneyness") - 0.9).abs().alias("g")).sort("g")
    call = c.filter(pl.col("option_type") == "call").with_columns((pl.col("moneyness") - 1.1).abs().alias("g")).sort("g")
    if put.is_empty() or call.is_empty():
        return None
    return float(put["implied_vol"][0] - call["implied_vol"][0])
