"""Historical Signal Explorer from the command line.

    python scripts/explore.py strategies/momentum_breakout.yaml
    python scripts/explore.py strategies/momentum_breakout.yaml --source synthetic --csv out.csv
    python scripts/explore.py my.yaml --symbols AAPL MSFT --start 2026-08-01 --end 2026-10-01

Lists the bars where the strategy's conditions matched exactly or nearly, with the data
each search actually had. Historical information only; it recommends nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from optlab.history.explorer import run_search, to_csv  # noqa: E402
from optlab.history.strategy import StrategyError, parse_strategy  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("strategy", help="strategy YAML file")
    ap.add_argument("--source", choices=["yahoo", "archive", "synthetic"])
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--csv", help="write results to this CSV file")
    ap.add_argument("--json", help="write results and data reports to this JSON file")
    args = ap.parse_args()

    text = Path(args.strategy).read_text()
    try:
        from optlab.history.strategy import load_yaml

        raw = load_yaml(text) or {}
        body = raw.get("historical_signal_search", raw)
        if args.source:
            body.setdefault("data", {})["source"] = args.source
        if args.symbols:
            body["symbols"] = args.symbols
        if args.start:
            body.setdefault("range", {})["start"] = args.start
        if args.end:
            body.setdefault("range", {})["end"] = args.end
        if args.limit:
            body.setdefault("output", {})["limit"] = args.limit
        strategy = parse_strategy({"historical_signal_search": body})
    except StrategyError as exc:
        sys.exit(str(exc))

    print(f"{strategy.name} · {strategy.timeframe} · {', '.join(strategy.symbols)} · {strategy.start} to "
          f"{strategy.end} ({strategy.timezone}) · source {strategy.source} · version {strategy.version}")
    res = run_search(strategy, progress=lambda i, n, s: print(f"  [{i + 1}/{n}] {s} ...", flush=True))
    for sym, r in res.reports.items():
        print(f"  {sym}: {r.bars_in_range} bars in range ({r.first_bar} .. {r.last_bar}), warm-up "
              f"{r.warmup_bars_available}/{r.warmup_bars_requested}, missing {r.missing_bars}, holidays "
              f"{r.holidays_in_range}, adjusted {r.adjusted}, extended hours {r.extended_hours_included}")
        for w in r.warnings:
            print(f"    ! {w}")
    for e in res.errors:
        print(f"  ! {e}")
    df = res.results
    n_exact = int((df["match_type"] == "exact").sum()) if not df.empty else 0
    print(f"\n{len(df)} matches ({n_exact} exact, {len(df) - n_exact} near) from {res.bars_evaluated:,} evaluated bars "
          f"({res.not_evaluable:,} not evaluable during warm-up) in {res.seconds:.1f}s")
    if not df.empty:
        show = df.copy()
        show["timestamp"] = show["timestamp"].dt.strftime("%Y-%m-%d %H:%M %Z")
        with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_colwidth", 60):
            print(show.head(25).to_string(index=False))
        if len(df) > 25:
            print(f"... {len(df) - 25} more (use --csv to save them all)")
    if args.csv:
        Path(args.csv).write_text(to_csv(df))
        print(f"Saved {args.csv}")
    if args.json:
        out = {"strategy": strategy.to_dict(), "version": strategy.version,
               "reports": {s: r.as_dict() for s, r in res.reports.items()},
               "results": json.loads(df.assign(timestamp=df["timestamp"].astype(str)).to_json(orient="records"))
               if not df.empty else []}
        Path(args.json).write_text(json.dumps(out, indent=1, default=str))
        print(f"Saved {args.json}")


if __name__ == "__main__":
    main()
