# region imports
from AlgorithmImports import *
# endregion

"""The Wheel, run inside LEAN with real option quotes.

1. No position: sell a cash-secured put at ~target delta, 25-45 DTE.
2. Put assigned: you now own 100 shares per contract. Sell covered calls.
3. Call assigned: shares are called away. Back to step 1.
Any short option is bought back early at `profit_take` of its premium.

LEAN models what the quick vectorbt screen cannot: real bid/ask quotes and
skew, Interactive Brokers fees, early assignment, dividends and margin.

Parameters (override in config.json or with `lean backtest --parameter`):
    ticker, delta, min_dte, max_dte, profit_take, max_alloc
"""


class WheelAlgorithm(QCAlgorithm):

    def initialize(self) -> None:
        self.set_start_date(2021, 1, 1)
        self.set_end_date(2024, 12, 31)
        self.set_cash(100_000)
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.MARGIN)

        self.ticker = self.get_parameter("ticker", "SPY")
        self.target_delta = float(self.get_parameter("delta", 0.25))
        self.min_dte = int(self.get_parameter("min_dte", 25))
        self.max_dte = int(self.get_parameter("max_dte", 45))
        self.profit_take = float(self.get_parameter("profit_take", 0.5))
        self.max_alloc = float(self.get_parameter("max_alloc", 0.9))   # share of equity to secure puts

        equity = self.add_equity(self.ticker, Resolution.MINUTE)
        equity.set_data_normalization_mode(DataNormalizationMode.RAW)   # required with options
        self.underlying = equity.symbol

        option = self.add_option(self.ticker, Resolution.MINUTE)
        option.set_filter(lambda u: u.include_weeklies().strikes(-30, 30).expiration(self.min_dte, self.max_dte))
        # American-style pricing so contract.greeks.delta is populated
        option.price_model = OptionPriceModels.crank_nicolson_fd()
        self.option_symbol = option.symbol

        self.chain = None
        self.set_warm_up(timedelta(days=5))
        self.schedule.on(self.date_rules.every_day(self.underlying),
                         self.time_rules.after_market_open(self.underlying, 30),
                         self.trade)
        # Unfilled limit orders are cancelled before the close and re-quoted tomorrow
        self.schedule.on(self.date_rules.every_day(self.underlying),
                         self.time_rules.before_market_close(self.underlying, 5),
                         self.transactions.cancel_open_orders)
        self.set_benchmark(self.underlying)

    def on_data(self, data: Slice) -> None:
        chain = data.option_chains.get(self.option_symbol)
        if chain:
            self.chain = chain

    # ------------------------------------------------------------------ logic
    def short_options(self):
        return [h for h in self.portfolio.values()
                if h.invested and h.type == SecurityType.OPTION and h.quantity < 0]

    def trade(self) -> None:
        if self.is_warming_up or self.chain is None:
            return

        # Take profits on open short options
        for h in self.short_options():
            if h.unrealized_profit_percent >= self.profit_take:
                self.liquidate(h.symbol, tag=f"profit take {h.unrealized_profit_percent:.0%}")

        if self.short_options():
            return

        shares = self.portfolio[self.underlying].quantity
        if shares >= 100:
            self.sell(OptionRight.CALL, shares // 100, "covered call")
        else:
            price = self.securities[self.underlying].price
            budget = self.portfolio.total_portfolio_value * self.max_alloc
            contracts = int(budget // (price * 100))
            if contracts > 0:
                self.sell(OptionRight.PUT, contracts, "cash-secured put")

    def sell(self, right, contracts: int, tag: str) -> None:
        candidates = [c for c in self.chain
                      if c.right == right and c.bid_price > 0 and c.greeks is not None and c.greeks.delta != 0]
        if not candidates:
            return
        best = min(candidates, key=lambda c: (abs(abs(c.greeks.delta) - self.target_delta),
                                              abs((c.expiry - self.time).days - 35)))
        if right == OptionRight.PUT:
            # never secure more than we can pay for if assigned
            cash = self.portfolio.cash
            contracts = min(contracts, int(cash // (best.strike * 100)))
        if contracts <= 0:
            return
        # Sell near the mid instead of hitting the bid
        mid = round((best.bid_price + best.ask_price) / 2, 2)
        self.limit_order(best.symbol, -contracts, mid,
                         tag=f"{tag} d={best.greeks.delta:.2f} {best.expiry:%Y-%m-%d} K={best.strike}")

    def on_assignment_order_event(self, assignment_event: OrderEvent) -> None:
        self.log(f"Assigned: {assignment_event}")
