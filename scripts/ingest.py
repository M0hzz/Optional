"""Load prices, option chains and insider trades into the local DuckDB store.

    python scripts/ingest.py                    # source from config.yaml
    python scripts/ingest.py --source openbb    # real data via OpenBB
    python scripts/ingest.py --symbols SPY AAPL --no-insiders
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from optlab.config import load_config  # noqa: E402
from optlab.data import Store, get_source  # noqa: E402
from optlab.filings import fetch_insider_trades, synthetic_insider_trades  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "openbb"])
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--no-chains", action="store_true")
    ap.add_argument("--no-insiders", action="store_true")
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

        if not args.no_insiders:
            if real:
                ident = cfg["edgar"]["identity"]
                if "example.com" in ident:
                    print("Skipping EDGAR: set edgar.identity in config.yaml to your name and email.")
                else:
                    for sym in symbols:
                        try:
                            df = fetch_insider_trades(sym, ident, cfg["edgar"]["lookback_days"])
                            print(f"{sym}: {store.upsert('insider_trades', df)} insider transactions")
                        except Exception as exc:
                            print(f"{sym}: EDGAR failed - {exc}")
            else:
                print(f"insider trades (synthetic): {store.upsert('insider_trades', synthetic_insider_trades(symbols))}")
    print(f"Done. Database: {cfg['data']['db_path']}")


if __name__ == "__main__":
    main()
