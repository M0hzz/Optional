"""Load market data and SEC filings into the local DuckDB store.

OpenBB:     prices, option chains, fundamentals + next earnings date, Treasury yields and VIX
EdgarTools: Form 4 insider trades, 8-K/10-Q/10-K filings, quarterly financials

    python scripts/ingest.py                    # source from config.yaml
    python scripts/ingest.py --source openbb    # real data via OpenBB and EDGAR
    python scripts/ingest.py --symbols SPY AAPL --no-insiders --no-filings
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from optlab.config import load_config  # noqa: E402
from optlab.data import Store, get_source  # noqa: E402
from optlab.filings import (fetch_filings, fetch_financials, fetch_insider_trades, synthetic_filings,  # noqa: E402
                            synthetic_financials, synthetic_insider_trades)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "openbb"])
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--no-chains", action="store_true")
    ap.add_argument("--no-insiders", action="store_true")
    ap.add_argument("--no-fundamentals", action="store_true")
    ap.add_argument("--no-macro", action="store_true")
    ap.add_argument("--no-filings", action="store_true", help="skip 8-K/10-Q/10-K filings and financials")
    args = ap.parse_args()

    cfg = load_config()
    if args.source:
        cfg["data"]["source"] = args.source
    symbols = args.symbols or cfg["universe"]
    src = get_source(cfg)
    real = cfg["data"]["source"] == "openbb"

    with Store(cfg["data"]["db_path"]) as store:
        for sym in symbols:
            try:
                n = store.upsert("prices", src.prices(sym, cfg["data"]["history_start"]))
                msg = f"{sym}: {n} price rows"
                if not args.no_chains:
                    msg += f", {store.upsert('option_chains', src.option_chain(sym))} option quotes"
                print(msg)
            except Exception as exc:
                print(f"{sym}: FAILED - {exc}")

        if not args.no_fundamentals:
            n = 0
            for sym in symbols:
                try:
                    n += store.upsert("fundamentals", src.fundamentals(sym))
                except Exception as exc:
                    print(f"{sym}: fundamentals failed - {exc}")
            print(f"fundamentals: {n} symbols (ETFs have none)")

        if not args.no_macro:
            try:
                print(f"macro (Treasury yields, VIX): {store.upsert('macro', src.macro(cfg['data']['history_start']))} rows")
            except Exception as exc:
                print(f"macro failed - {exc}")

        ident = cfg["edgar"]["identity"]
        edgar_ok = "example.com" not in ident
        if real and not edgar_ok and not (args.no_insiders and args.no_filings):
            print("Skipping EDGAR: set edgar.identity in config.yaml to your name and email.")

        if not args.no_insiders:
            if real:
                for sym in symbols if edgar_ok else []:
                    try:
                        df = fetch_insider_trades(sym, ident, cfg["edgar"]["lookback_days"])
                        print(f"{sym}: {store.upsert('insider_trades', df)} insider transactions")
                    except Exception as exc:
                        print(f"{sym}: EDGAR insiders failed - {exc}")
            else:
                print(f"insider trades (synthetic): {store.upsert('insider_trades', synthetic_insider_trades(symbols))}")

        if not args.no_filings:
            if real:
                for sym in symbols if edgar_ok else []:
                    try:
                        nf = store.upsert("filings", fetch_filings(sym, ident, cfg["edgar"].get("filings_lookback_days", 365)))
                        nq = store.upsert("financials", fetch_financials(sym, ident))
                        print(f"{sym}: {nf} filings, {nq} financial data points")
                    except Exception as exc:
                        print(f"{sym}: EDGAR filings failed - {exc}")
            else:
                print(f"filings (synthetic): {store.upsert('filings', synthetic_filings(symbols))}, "
                      f"financial data points: {store.upsert('financials', synthetic_financials(symbols))}")
    print(f"Done. Database: {cfg['data']['db_path']}")


if __name__ == "__main__":
    main()
