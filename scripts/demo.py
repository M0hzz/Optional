"""End-to-end tour in the terminal: rank -> price -> size -> backtest -> allocate.

    python scripts/demo.py [--symbol SPY]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from optlab.backtest import BTParams, buy_and_hold, simulate, stats, sweep  # noqa: E402
from optlab.config import load_config  # noqa: E402
from optlab.data import Store, risk_free_rate  # noqa: E402
from optlab.data.chain import atm_iv  # noqa: E402
from optlab.filings import insider_signal  # noqa: E402
from optlab.pricing.engine import entry_cost, fill_entry_prices, price_position, scenario_grid  # noqa: E402
from optlab.research import build_features, rank_candidates  # noqa: E402
from optlab.risk import RiskLimits, allocate, portfolio_report, size_position  # noqa: E402
from optlab.strategies import STRATEGIES, max_loss  # noqa: E402

pd.set_option("display.width", 160)
pd.set_option("display.max_columns", 20)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    args = ap.parse_args()
    cfg = load_config()
    d = cfg["strategy_defaults"]

    with Store(cfg["data"]["db_path"], read_only=True) as store:
        syms = store.symbols()
        rate = risk_free_rate(store, cfg)
        prices = store.query("SELECT symbol, date, close FROM prices")
        ivs = {s: atm_iv(store.latest_chain(s)) for s in syms}
        ins = insider_signal(store.insider_trades())
        closes = {s: store.prices(s).to_pandas().set_index("date")["close"] for s in syms}

    for s in closes:
        closes[s].index = pd.to_datetime(closes[s].index)

    print(f"Data source: {cfg['data']['source']}   risk-free rate: {rate:.2%}\n\n1) Premium-selling scanner")
    ranked = rank_candidates(build_features(prices), {k: v for k, v in ivs.items() if v},
                             dict(zip(ins["symbol"].to_list(), ins["score"].to_list())))
    print(ranked.to_pandas().round(3).to_string(index=False))

    sym = args.symbol or ranked["symbol"][0]
    spot, iv = float(closes[sym].iloc[-1]), ivs[sym]
    print(f"\n2) Put credit spread on {sym} (spot {spot:.2f}, ATM IV {iv:.1%})")
    legs = fill_entry_prices(STRATEGIES["Put credit spread"](spot, iv, dte=d["dte"], short_delta=d["short_delta"],
                                                             spread_width_pct=d["spread_width_pct"], rate=rate),
                             spot, rate, style="american")
    for l in legs:
        print(f"   {'sell' if l.quantity < 0 else 'buy '} {l.strike:>8.2f} put  @ {l.entry_price:.2f}")
    g = price_position(legs, spot, rate)
    worst = max_loss(legs)
    print(f"   credit ${-entry_cost(legs):,.0f}  max loss ${worst:,.0f}  "
          f"$delta {g['dollar_delta']:+,.0f}  vega {g['vega']:+.0f}  theta {g['theta']:+.1f}/day")
    sg = scenario_grid(legs, spot, rate, days_forward=7)
    print("   P&L in 7 days (rows: IV change, cols: spot move):")
    print(pd.DataFrame(sg["pnl"], index=[f"{v:+.0%}" for v in sg["vol_shifts"]],
                       columns=[f"{m:+.0%}" for m in sg["spot_moves"]]).round(0).to_string())

    sz = size_position(worst, worst, RiskLimits.from_config(cfg))
    print(f"\n3) Sizing: {sz['contracts']} contracts (limited by {sz['limited_by']}), "
          f"total max loss ${sz['total_max_loss']:,.0f}")
    if sz["contracts"] == 0:
        print("   One spread risks more than the per-trade limit: narrow the wings or raise max_risk_per_trade_pct.")

    print(f"\n4) Quick backtests on {sym}")
    base = BTParams(strategy="put_spread", dte=d["dte"], short_delta=d["short_delta"], width_pct=d["spread_width_pct"],
                    profit_take_pct=d["profit_take_pct"], slippage_pct=d["slippage_pct_of_premium"],
                    commission=d["commission_per_contract"], rate=rate)
    series = {s: simulate(closes[sym], BTParams(**{**base.__dict__, "strategy": s}))[0]
              for s in ["put_write", "put_spread", "iron_condor", "covered_call"]}
    series["buy_and_hold"] = buy_and_hold(closes[sym])
    print(stats(pd.DataFrame(series).fillna(0)).round(2).to_string())
    _, grid = sweep(closes[sym], BTParams(**{**base.__dict__, "strategy": "put_write"}))
    print("\n   put-write Sharpe by DTE x delta:")
    print(grid["sharpe"].unstack().round(2).to_string())

    print("\n5) skfolio allocation across symbols x strategies (min CVaR, max 25% each)")
    R = pd.DataFrame({f"{s} {k}": simulate(closes[s], BTParams(strategy=k, rate=rate))[0]
                      for s in syms for k in ("put_write", "iron_condor")}).fillna(0)
    w = allocate(R, "min_cvar", 0.25)
    rep = portfolio_report(R, w)
    print(w[w > 0.001].sort_values(ascending=False).map("{:.1%}".format).to_string())
    print(f"   in-sample: return {rep['annual_return_%']:.1f}%/yr, vol {rep['annual_vol_%']:.1f}%, "
          f"max DD {rep['max_drawdown_%']:.1f}%")


if __name__ == "__main__":
    main()
