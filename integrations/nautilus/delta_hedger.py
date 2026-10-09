"""NautilusTrader: keep an options book delta-neutral by trading the underlying.

This is where research meets execution. The option legs come from optlab (the
same Leg objects the dashboard and backtests use); NautilusTrader watches the
underlying, recomputes the book's delta on every bar, and trades shares whenever
the hedge drifts outside a band. The same Strategy class runs unchanged in a
backtest (run_backtest.py) and live against a broker adapter such as Interactive
Brokers (see README "Going live").
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from nautilus_trader.config import StrategyConfig  # noqa: E402
from nautilus_trader.model.data import Bar, BarType  # noqa: E402
from nautilus_trader.model.enums import OrderSide, TimeInForce  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402
from nautilus_trader.trading.strategy import Strategy  # noqa: E402

from optlab.pricing.fast_bs import bs_delta  # noqa: E402

MULTIPLIER = 100


class DeltaHedgerConfig(StrategyConfig, frozen=True):
    instrument_id: str                      # underlying, e.g. "SPY.XNAS" or "SPY.SMART" (IB)
    bar_type: str                           # e.g. "SPY.XNAS-1-DAY-LAST-EXTERNAL"
    # Each leg: (option_type, strike, expiry ISO date, contracts (+long/-short), implied vol)
    legs: tuple[tuple[str, float, str, int, float], ...] = ()
    rate: float = 0.04
    hedge_band_shares: int = 50             # re-hedge when |target - current| exceeds this
    hedge_band_pct: float = 0.0             # ...or this % of the book's gross delta, whichever is larger


class DeltaHedger(Strategy):
    def __init__(self, config: DeltaHedgerConfig) -> None:
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.legs = [(t, k, datetime.fromisoformat(e).replace(tzinfo=timezone.utc), q, v)
                     for t, k, e, q, v in config.legs]
        self.hedges = 0

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def option_delta(self, spot: float, now: datetime) -> float:
        """Book delta in shares (options only)."""
        total = 0.0
        for typ, k, exp, q, vol in self.legs:
            t = max((exp - now).total_seconds() / (365 * 86400), 0.0)
            if t <= 0:
                continue  # expired: exercise/assignment is handled by the broker, not the hedger
            total += q * MULTIPLIER * float(bs_delta(spot, k, t, self.config.rate, 0.0, vol, typ == "call"))
        return total

    def on_bar(self, bar: Bar) -> None:
        spot = float(bar.close)
        now = datetime.fromtimestamp(bar.ts_event / 1e9, tz=timezone.utc)
        opt_delta = self.option_delta(spot, now)
        target = -round(opt_delta)                         # shares that neutralize the options
        pos = self.portfolio.net_position(self.instrument_id)
        current = int(pos) if pos is not None else 0
        diff = target - current
        band = max(self.config.hedge_band_shares, abs(opt_delta) * self.config.hedge_band_pct / 100)
        if abs(diff) < band:
            return
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY if diff > 0 else OrderSide.SELL,
            quantity=self.instrument.make_qty(Decimal(abs(diff))),
            time_in_force=TimeInForce.DAY,
        )
        self.submit_order(order)
        self.hedges += 1
        self.log.info(f"spot={spot:.2f} option_delta={opt_delta:+.0f} hedge {current:+d} -> {target:+d}")

    def on_stop(self) -> None:
        self.log.info(f"Hedger stopped after {self.hedges} hedge trades")
