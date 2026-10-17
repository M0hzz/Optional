"""Company disclosures and financial changes via EdgarTools.

Two feeds, both from SEC EDGAR:
- filings     recent 8-K, 10-Q and 10-K filings. 8-K item codes say what happened;
              some (restatements, auditor changes, impairments, executive departures)
              are reasons to avoid selling premium on a name until the dust settles.
- financials  quarterly revenue, net income and EPS plus balance-sheet lines from
              XBRL company facts, compared year over year to flag deteriorating
              businesses before they show up in the price.
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

FILING_COLS = ["symbol", "filing_date", "form", "accession_no", "items", "description", "url"]
FINANCIAL_COLS = ["symbol", "metric", "period_end", "value", "fiscal_period"]

# 8-K item codes -> short description. RED_FLAG_ITEMS are the ones worth acting on.
ITEM_LABELS = {
    "1.01": "Material agreement", "1.02": "Agreement terminated", "1.03": "Bankruptcy",
    "1.05": "Cybersecurity incident", "2.01": "Acquisition or disposal", "2.02": "Results of operations",
    "2.03": "New debt obligation", "2.05": "Restructuring costs", "2.06": "Impairment",
    "3.01": "Delisting notice", "3.02": "Unregistered equity sale", "4.01": "Auditor change",
    "4.02": "Prior financials unreliable", "5.02": "Officer/director change", "5.03": "Bylaw change",
    "5.07": "Shareholder vote", "7.01": "Reg FD disclosure", "8.01": "Other event", "9.01": "Exhibits",
}
RED_FLAG_ITEMS = {"1.03", "1.05", "2.05", "2.06", "3.01", "4.01", "4.02", "5.02"}

# metric -> (XBRL concepts in order of preference, "flow" for quarterly amounts | "stock" for balances).
# The fallbacks after the first concept cover banks, which tag these lines differently.
METRICS = {
    "revenue": (["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "RevenuesNetOfInterestExpense",
                 "SalesRevenueNet"], "flow"),
    "net_income": (["NetIncomeLoss"], "flow"),
    "eps_diluted": (["EarningsPerShareDiluted"], "flow"),
    "cash": (["CashAndCashEquivalentsAtCarryingValue", "CashAndDueFromBanks"], "stock"),
    "long_term_debt": (["LongTermDebtNoncurrent", "LongTermDebt",
                        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"], "stock"),
    "total_assets": (["Assets"], "stock"),
    "total_liabilities": (["Liabilities"], "stock"),
    "equity": (["StockholdersEquity"], "stock"),
}


def describe_items(items: str | None) -> str:
    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    return "; ".join(ITEM_LABELS.get(c, c) for c in codes if c != "9.01")


# --------------------------------------------------------------------- fetch
def fetch_filings(symbol: str, identity: str, lookback_days: int = 365,
                  forms: tuple[str, ...] = ("8-K", "10-Q", "10-K")) -> pl.DataFrame:
    """Recent 8-K / 10-Q / 10-K filings (amendments included) for one company."""
    from edgar import Company, set_identity

    set_identity(identity)
    since = date.today() - timedelta(days=lookback_days)
    wanted = list(forms) + [f + "/A" for f in forms]
    rows = []
    for f in Company(symbol).get_filings(form=wanted):
        if f.filing_date < since:
            break
        items = str(getattr(f, "items", "") or "")
        desc = describe_items(items) if f.form.startswith("8-K") else ""
        rows.append((symbol, f.filing_date, f.form, f.accession_no, items,
                     desc or str(getattr(f, "primary_doc_description", "") or f.form), f.homepage_url))
    return pl.DataFrame(rows, schema=FILING_COLS, orient="row").with_columns(pl.col("filing_date").cast(pl.Date)) \
        if rows else pl.DataFrame(schema={c: pl.Date if c == "filing_date" else pl.Utf8 for c in FILING_COLS})


def fetch_financials(symbol: str, identity: str, years: int = 3) -> pl.DataFrame:
    """Quarterly flows and period-end balances from XBRL company facts."""
    import pandas as pd
    from edgar import Company, set_identity

    set_identity(identity)
    empty = pl.DataFrame(schema={"symbol": pl.Utf8, "metric": pl.Utf8, "period_end": pl.Date,
                                 "value": pl.Float64, "fiscal_period": pl.Utf8})
    entity_facts = Company(symbol).get_facts()
    if entity_facts is None:  # ETFs and funds file no XBRL financials
        return empty
    facts = entity_facts.to_dataframe()
    facts["name"] = facts["concept"].str.split(":").str[-1]
    facts["period_end"] = pd.to_datetime(facts["period_end"]).dt.date
    facts = facts[facts["period_end"] >= date.today() - timedelta(days=365 * years + 120)]
    out = []
    for metric, (concepts, kind) in METRICS.items():
        for concept in concepts:
            sub = facts[facts["name"] == concept].copy()
            if kind == "flow":  # keep three-month periods; 10-Qs also report year-to-date totals
                days = (pd.to_datetime(sub["period_end"]) - pd.to_datetime(sub["period_start"])).dt.days
                sub = sub[days.between(80, 100)]
            if sub.empty:
                continue
            # the same period is restated in later filings; keep the most recent report
            sub = sub.drop_duplicates("period_end", keep="last")
            out.append(pd.DataFrame({"symbol": symbol, "metric": metric, "period_end": sub["period_end"],
                                     "value": sub["numeric_value"].astype(float),
                                     "fiscal_period": sub["fiscal_period"].astype(str)}))
            break
    if not out:
        return empty
    return pl.from_pandas(pd.concat(out, ignore_index=True))


# ------------------------------------------------------------------- signals
def financial_changes(financials: pl.DataFrame) -> pl.DataFrame:
    """Latest value of each metric vs the same period a year earlier (avoids seasonality)."""
    schema = {"symbol": pl.Utf8, "metric": pl.Utf8, "period_end": pl.Date, "value": pl.Float64,
              "year_ago": pl.Float64, "yoy_pct": pl.Float64}
    if financials.is_empty():
        return pl.DataFrame(schema=schema)
    rows = []
    for (sym, metric), g in financials.sort("period_end").group_by(["symbol", "metric"], maintain_order=True):
        last = g.row(-1, named=True)
        target = last["period_end"] - timedelta(days=365)
        prior = g.filter((pl.col("period_end") - target).abs() <= timedelta(days=45))
        year_ago = prior.row(-1, named=True)["value"] if not prior.is_empty() else None
        yoy = ((last["value"] - year_ago) / abs(year_ago) * 100) if year_ago else None
        rows.append((sym, metric, last["period_end"], last["value"], year_ago, yoy))
    return pl.DataFrame(rows, schema=schema, orient="row")


def financial_flags(changes: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol warnings from year-over-year changes."""
    flags: dict[str, list[str]] = {}
    for r in changes.iter_rows(named=True):
        f = flags.setdefault(r["symbol"], [])
        yoy, m = r["yoy_pct"], r["metric"]
        if m == "net_income" and r["year_ago"] is not None and r["year_ago"] > 0 >= r["value"]:
            f.append("net income turned negative")
        elif yoy is None:
            continue
        elif m == "revenue" and yoy <= -10:
            f.append(f"revenue {yoy:+.0f}% YoY")
        elif m == "net_income" and yoy <= -25:
            f.append(f"net income {yoy:+.0f}% YoY")
        elif m == "long_term_debt" and yoy >= 25:
            f.append(f"long-term debt {yoy:+.0f}% YoY")
        elif m == "cash" and yoy <= -30:
            f.append(f"cash {yoy:+.0f}% YoY")
    return pl.DataFrame({"symbol": list(flags), "financial_flags": ["; ".join(v) for v in flags.values()],
                         "n_financial_flags": [len(v) for v in flags.values()]},
                        schema={"symbol": pl.Utf8, "financial_flags": pl.Utf8, "n_financial_flags": pl.Int64})


def filing_flags(filings: pl.DataFrame, as_of: date | None = None, window_days: int = 90) -> pl.DataFrame:
    """Red-flag 8-K items filed in the last `window_days`, per symbol."""
    as_of = as_of or date.today()
    schema = {"symbol": pl.Utf8, "filing_flags": pl.Utf8, "n_filing_flags": pl.Int64}
    if filings.is_empty():
        return pl.DataFrame(schema=schema)
    recent = filings.filter(pl.col("form").str.starts_with("8-K")
                            & (pl.col("filing_date") >= as_of - timedelta(days=window_days)))
    flags: dict[str, list[str]] = {}
    for r in recent.sort("filing_date", descending=True).iter_rows(named=True):
        for code in (r["items"] or "").split(","):
            if code.strip() in RED_FLAG_ITEMS:
                flags.setdefault(r["symbol"], []).append(f"{ITEM_LABELS[code.strip()]} ({r['filing_date']})")
    return pl.DataFrame({"symbol": list(flags), "filing_flags": ["; ".join(v) for v in flags.values()],
                         "n_filing_flags": [len(v) for v in flags.values()]}, schema=schema)


# ----------------------------------------------------------------- synthetic
def synthetic_filings(symbols: list[str], end: date | None = None, seed: int = 11) -> pl.DataFrame:
    """Fake 8-K/10-Q/10-K history so the dashboard has something to show offline."""
    import numpy as np

    rng = np.random.default_rng(seed)
    end = end or date.today()
    common = ["2.02,9.01", "5.07", "7.01,9.01", "8.01", "1.01,9.01"]
    rare = ["5.02", "2.05,9.01", "4.01", "2.06"]
    rows = []
    for s in symbols:
        if s in {"SPY", "QQQ", "IWM"}:
            continue
        for q in range(4):
            d = end - timedelta(days=30 + 91 * q + int(rng.integers(0, 10)))
            rows.append((s, d, "10-K" if q == 3 else "10-Q", f"SYN-{s}-Q{q}", "", "Quarterly report" if q < 3
                         else "Annual report", ""))
        for i in range(int(rng.integers(3, 8))):
            items = str(rng.choice(rare)) if rng.random() < 0.15 else str(rng.choice(common))
            d = end - timedelta(days=int(rng.integers(1, 360)))
            rows.append((s, d, "8-K", f"SYN-{s}-8K{i}", items, describe_items(items), ""))
    return pl.DataFrame(rows, schema=FILING_COLS, orient="row").with_columns(pl.col("filing_date").cast(pl.Date))


def synthetic_financials(symbols: list[str], end: date | None = None, quarters: int = 9, seed: int = 5) -> pl.DataFrame:
    import numpy as np

    rng = np.random.default_rng(seed)
    end = end or date.today()
    rows = []
    for s in symbols:
        if s in {"SPY", "QQQ", "IWM"}:
            continue
        rev0, growth = float(rng.uniform(5e9, 1e11)), float(rng.normal(0.02, 0.04))  # quarterly growth
        margin, shares = float(rng.uniform(0.05, 0.3)), float(rng.uniform(1e9, 1.5e10))
        debt, cash, assets = rev0 * rng.uniform(0.5, 2), rev0 * rng.uniform(0.2, 1), rev0 * rng.uniform(3, 6)
        for q in range(quarters):
            pe = end - timedelta(days=91 * (quarters - 1 - q) + 40)
            fp = f"Q{(pe.month - 1) // 3 + 1}"
            rev = rev0 * (1 + growth) ** q * (1 + 0.06 * np.sin(q * np.pi / 2)) * rng.normal(1, 0.02)
            ni = rev * (margin + rng.normal(0, 0.03))
            debt *= rng.normal(1.01, 0.04)
            cash *= rng.normal(1.0, 0.08)
            assets *= rng.normal(1.01, 0.02)
            liab = assets * 0.6
            for metric, v in [("revenue", rev), ("net_income", ni), ("eps_diluted", ni / shares), ("cash", cash),
                              ("long_term_debt", debt), ("total_assets", assets), ("total_liabilities", liab),
                              ("equity", assets - liab)]:
                rows.append((s, metric, pe, float(v), fp))
    return pl.DataFrame(rows, schema=FINANCIAL_COLS, orient="row").with_columns(pl.col("period_end").cast(pl.Date))
