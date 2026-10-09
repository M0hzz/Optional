"""Backtest the DeltaHedger on underlying bars from the optlab store.

    python integrations/nautilus/run_backtest.py --symbol SPY --dte 45 --end 2024-06-28

Sells one ATM straddle (configurable) at the start of the window, hedges its delta
daily in shares, and reports the hedge P&L alongside the option P&L.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import USD  # noqa: E402
from nautilus_trader.model.data import BarType  # noqa: E402
from nautilus_trader.model.enums import AccountType, OmsType  # noqa: E402
from nautilus_trader.model.identifiers import Venue  # noqa: E402
from nautilus_trader.model.objects import Money  # noqa: E402
from nautilus_trader.model.data import Bar  # noqa: E402
from nautilus_trader.test_kit.providers import TestInstrumentProvider  # noqa: E402

from delta_hedger import DeltaHedger, DeltaHedgerConfig  # noqa: E402
from optlab.config import load_config  # noqa: E402
from optlab.data import Store  # noqa: E402
from optlab.pricing.fast_bs import bs_price  # noqa: E402


def make_bars(df: pd.DataFrame, bar_type: BarType, instrument) -> list[Bar]:
    """Build Nautilus bars directly (BarDataWrangler needs writable arrays,
    which pandas 3 copy-on-write does not hand out)."""
    out = []
    for ts, row in df.iterrows():
        ns = int(ts.value)
        hi = max(row["high"], row["open"], row["close"])
        lo = min(row["low"], row["open"], row["close"])
        out.append(Bar(bar_type, instrument.make_price(row["open"]), instrument.make_price(hi),
                       instrument.make_price(lo), instrument.make_price(row["close"]),
                       instrument.make_qty(max(row["volume"], 1.0)), ns, ns))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--days", type=int, default=None, help="bars to replay (default: until expiry)")
    ap.add_argument("--end", default=None, help="last bar date, e.g. 2024-06-28 (default: latest)")
    ap.add_argument("--dte", type=int, default=60)
    ap.add_argument("--contracts", type=int, default=-5, help="straddles; negative = short")
    ap.add_argument("--vol", type=float, default=0.20)
    args = ap.parse_args()

    cfg = load_config()
    with Store(cfg["data"]["db_path"], read_only=True) as store:
        px = store.prices(args.symbol).to_pandas()
    if args.end:
        px = px[pd.to_datetime(px["date"]) <= pd.Timestamp(args.end)]
    n_bars = args.days or round(args.dte * 252 / 365) + 1
    px = px.tail(n_bars).copy()
    px["timestamp"] = pd.to_datetime(px["date"]).dt.tz_localize("UTC") + pd.Timedelta(hours=20)
    bars_df = px.set_index("timestamp")[["open", "high", "low", "close", "volume"]]

    instrument = TestInstrumentProvider.equity(symbol=args.symbol, venue="XNAS")
    bar_type = BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
    bars = make_bars(bars_df, bar_type, instrument)

    start = bars_df.index[0]
    spot0 = float(bars_df["close"].iloc[0])
    strike = round(spot0)
    expiry = (start + timedelta(days=args.dte)).date().isoformat()
    legs = (("call", float(strike), expiry, args.contracts, args.vol),
            ("put", float(strike), expiry, args.contracts, args.vol))

    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="WARNING")))
    engine.add_venue(Venue("XNAS"), OmsType.NETTING, AccountType.MARGIN,
                     starting_balances=[Money(1_000_000, USD)], default_leverage=Decimal(4))
    engine.add_instrument(instrument)
    engine.add_data(bars)
    strat = DeltaHedger(DeltaHedgerConfig(instrument_id=str(instrument.id), bar_type=str(bar_type),
                                          legs=legs, rate=cfg["market"]["risk_free_rate"]))
    engine.add_strategy(strat)
    engine.run()

    # Option P&L, marked at the same flat vol (realized-vs-implied is what you're trading)
    t_end = max((pd.Timestamp(expiry, tz="UTC") - bars_df.index[-1]).days, 0) / 365
    spot1 = float(bars_df["close"].iloc[-1])
    r = cfg["market"]["risk_free_rate"]
    t0 = args.dte / 365
    v0 = sum(bs_price(spot0, strike, t0, r, 0, args.vol, c) for c in (True, False))
    v1 = sum(bs_price(spot1, strike, t_end, r, 0, args.vol, c) for c in (True, False))
    option_pnl = float(args.contracts * 100 * (v1 - v0))

    fills = engine.trader.generate_order_fills_report()
    # Hedge P&L = cash from all share trades + value of shares still held (fees excluded).
    cash = 0.0
    for _, f in fills.iterrows():
        qty, avg = float(f["filled_qty"]), float(f["avg_px"])
        cash += -qty * avg if str(f["side"]).upper().endswith("BUY") else qty * avg
    pos = float(engine.portfolio.net_position(instrument.id) or 0)
    hedge_pnl = cash + pos * spot1

    print(f"\n{args.symbol}: {abs(args.contracts)} {'short' if args.contracts < 0 else 'long'} "
          f"{strike} straddles, {args.dte} DTE, {len(bars_df)} daily bars")
    print(f"Spot {spot0:.2f} -> {spot1:.2f}   hedge trades: {len(fills)}   shares held at end: {pos:+.0f}")
    print(f"Option P&L (marked at {args.vol:.0%} vol): {option_pnl:>12,.0f}")
    print(f"Hedge P&L (shares, before fees):  {hedge_pnl:>12,.0f}")
    print(f"Net:                              {option_pnl + hedge_pnl:>12,.0f}")
    engine.dispose()


if __name__ == "__main__":
    main()
