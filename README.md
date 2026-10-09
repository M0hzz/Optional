# optlab — a personal options-strategy lab

One Python project that wires ten open-source tools into a single workflow for
trading options: **find** where premium is rich, **price** the trade, **size** it,
**test** it, then **execute and hedge** it.

```
             ┌──────────── data ────────────┐
 OpenBB ───▶ │ prices · option chains       │
             │ fundamentals · earnings date │
             │ Treasury yields · VIX        │
 EdgarTools ▶│ insider trades (Form 4)      │──▶ DuckDB file (data/market.duckdb)
             │ 8-K/10-Q/10-K · financials   │
             └──────────────────────────────┘        │  read with Polars
                                                     ▼
   research ── Polars features + scanner ── Qlib (ML ranker, optional)
   pricing ─── QuantLib: fair value, Greeks, IV, scenario grids
   screen ──── vectorbt: fast strategy backtests and DTE × delta sweeps
   validate ── LEAN: realistic Wheel backtest with real quotes, fees, assignment
   risk ────── sizing rules + Greek limits + skfolio allocation
   execute ─── NautilusTrader: delta-hedging strategy (backtest → live)
   view ────── Streamlit dashboard over all of the above
```

Everything runs offline on **synthetic data** out of the box, so you can explore
before connecting real feeds. Switch `data.source` to `openbb` when ready.

> This is research software, not financial advice. Synthetic numbers are
> illustrative only. Paper-trade anything before risking money.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Python 3.10+
pip install -r requirements.txt
python scripts/ingest.py           # builds data/market.duckdb (synthetic by default)
python -m pytest                   # 21 tests
python scripts/demo.py             # terminal tour: scan → price → size → backtest → allocate
streamlit run app/streamlit_app.py # the dashboard
```

### Real data

1. In `config.yaml` set `data.source: openbb` and `edgar.identity: "Your Name you@email.com"`
   (the SEC requires a name and email on every request).
2. `python scripts/ingest.py` — pulls prices, today's option chains, fundamentals and
   next earnings dates for the `universe`, Treasury yields and VIX, plus Form 4 insider
   trades, recent 8-K/10-Q/10-K filings and quarterly financials from EDGAR.
3. Run it daily (cron / Task Scheduler) to build up your own history of chain
   snapshots; free providers only give the current chain.

`yfinance` is the default free provider. Treasury yields come from the Federal Reserve
(no key). The project pins OpenBB 4.x: OpenBB 5 replaced `obb.equity` / `obb.derivatives`
with per-provider namespaces and dropped yfinance, so the fetchers here don't run on it. For better chains, set
`openbb_provider` to `cboe`, `tradier`, `intrinio` or `polygon` and add the
key with `obb.user.credentials`.

---

## What each piece does

| Tool | Where | What it does here |
|---|---|---|
| **OpenBB** | `src/optlab/data/sources.py` | `OpenBBSource` fetches daily prices, option chains, fundamentals (market cap, P/E, dividend yield, beta, margins, growth) with the next earnings date, and economic data: Treasury yields 1M–10Y and the VIX. The 3-month yield becomes the risk-free rate in pricing, and each stock's dividend yield feeds the pricer. `SyntheticSource` produces stand-in data for all of it with stochastic vol, crash jumps and a realistic smile. |
| **EdgarTools** | `src/optlab/filings/insiders.py`, `filings/company.py` | Downloads Form 4 filings, keeps open-market buys (P) and sales (S), scores each stock −1…+1; insider buying favors selling puts over calls. Also pulls recent 8-K/10-Q/10-K filings, flags risky 8-K items (restatement, auditor change, impairment, executive departure, cyber incident…), and reads quarterly revenue, net income, EPS, cash, debt and balance-sheet totals from XBRL to flag year-over-year deterioration. |
| **DuckDB + Polars** | `src/optlab/data/store.py`, `research/features.py` | One local database file with `prices`, `option_chains`, `insider_trades`, `fundamentals`, `macro`, `filings`, `financials`; idempotent upserts. Polars builds volatility features across the universe. |
| **QuantLib** | `src/optlab/pricing/engine.py` | American (finite-difference) and European pricing, full Greeks, implied-vol solver, multi-leg position Greeks, spot × vol scenario grids. `fast_bs.py` is a NumPy BSM for bulk work, checked against QuantLib in tests. |
| **vectorbt** | `src/optlab/backtest/quick.py` | Simulates put-writes, covered calls, put spreads and iron condors on any price history (profit-take, stop, slippage, commissions, T-bill yield on collateral), scores them with vectorbt, sweeps DTE × delta. |
| **LEAN** | `integrations/lean/wheel/` | The Wheel (cash-secured puts → assignment → covered calls) using real option quotes, IB fees, and delta-based contract selection. |
| **skfolio** | `src/optlab/risk/sizing.py` | Allocates across strategy return streams (min CVaR, risk parity on drawdown, HRP, max Sharpe). Alongside: per-trade sizing by max loss and concentration, and book-level delta/vega/concentration limits. |
| **Streamlit** | `app/streamlit_app.py` | Eight tabs: Scanner · Chain & pricer · Strategy lab · Backtest · Risk & sizing · Insiders · Company · Macro. |
| **Qlib** | `integrations/qlib/vol_ranker.py` | Trains LightGBM to predict which underlyings' volatility will fall over the next month (best to sell premium on). Rankings appear in the dashboard scanner. |
| **NautilusTrader** | `integrations/nautilus/` | `DeltaHedger` keeps an options book delta-neutral by trading the underlying. Same class for backtest and live. |

## Configuration (`config.yaml`)

- `universe` — symbols to track.
- `market` — fallback risk-free rate and which macro series (`rate_series`) to price with.
- `edgar` — your SEC identity and how far back to read insider trades and filings.
- `account` — equity and risk limits: max loss per trade (% equity), max capital
  per underlying, max |dollar delta| and vega as % of equity.
- `strategy_defaults` — DTE, short delta, wing width, profit-take, slippage, commission.

---

## The dashboard

- **Scanner** — ranks the universe for premium selling: IV vs realized vol
  (from the latest chain), whether vol is already calming, vol-of-vol, insider
  score, plus Qlib predictions if you've trained the model. Also shows days to
  the next earnings report and counts of filing / financial red flags. Price and
  realized-vol charts with today's ATM IV overlaid.
- **Chain & pricer** — volatility smile per expiry, put-IV surface, raw quotes,
  and a QuantLib calculator (price, Greeks, implied vol from a market price).
- **Strategy lab** — pick a strategy, DTE, delta and wings; see credit, max loss,
  position Greeks, payoff at expiry and halfway, and a spot × IV scenario table.
  Warns when an earnings report falls before expiry.
- **Backtest** — vectorbt run vs buy-and-hold, trade log, win rate, and a
  DTE × delta heatmap of Sharpe / return / drawdown.
- **Risk & sizing** — contracts allowed for a trade, an editable book checked
  against your limits, and a skfolio allocation across symbols × strategies.
- **Insiders** — Form 4 summary and transactions.
- **Company** — fundamentals and next earnings date, quarterly financials vs a year
  earlier, red flags across the universe, and recent filings with links to EDGAR.
- **Macro** — Treasury curve and its history, 10Y−2Y slope, and the VIX.

---

## Integrations that run outside the core

### LEAN (realistic validation)

```bash
pip install lean
lean login                          # free QuantConnect account
lean init                           # once, in an empty folder; pulls the Docker image
cp -r integrations/lean/wheel ./wheel
lean cloud backtest wheel --push    # QuantConnect cloud has options data
# or locally, buying the data:  lean backtest wheel --download-data
```

Override parameters with `--parameter delta 0.30` etc. (see `config.json`).

### Qlib (ML ranker)

Qlib doesn't support Python 3.13 yet; use a 3.10–3.12 environment.

```bash
pip install pyqlib lightgbm
python integrations/qlib/vol_ranker.py export --out ~/.qlib/optlab_csv
python -m qlib.cli.dump_bin dump_all --data_path ~/.qlib/optlab_csv \
    --qlib_dir ~/.qlib/qlib_data/optlab --include_fields open,high,low,close,volume --date_field_name date
python integrations/qlib/vol_ranker.py train --provider ~/.qlib/qlib_data/optlab
```

If your Qlib version has no `qlib.cli.dump_bin`, run `scripts/dump_bin.py` from
the Qlib GitHub repo with the same arguments.

With only eight symbols the model has little to learn from; point it at a broad
universe (Qlib's US dataset, or a few hundred optionable stocks via `ingest.py
--symbols ...`) for meaningful results.

### NautilusTrader (hedging and live execution)

```bash
python integrations/nautilus/run_backtest.py --symbol SPY --dte 45 --end 2024-06-28 --contracts -5
```

Sells (negative `--contracts`) or buys ATM straddles at the start of the window,
hedges delta daily, and reports option vs hedge P&L. Going live means swapping
the `BacktestEngine` for a `TradingNode` with the Interactive Brokers adapter
(`pip install "nautilus_trader[ib]"`), using `SPY.SMART` style instrument IDs.
Paper account first.

---

## Know the limits

- **Quick backtests reprice options from the underlying** (IV = realized vol ×
  premium, flat across strikes). No real skew, spreads, early assignment or
  dividends. Use them to rank ideas, then confirm in LEAN.
- **Spreads are tested at 25% of the account in margin** (`capital_fraction`);
  committing 100% to defined-risk spreads is all-in leverage.
- **skfolio weights in the dashboard are in-sample.** For honest numbers,
  fit on one period and evaluate on the next (skfolio's `WalkForward`).
- **Free option-chain data is a snapshot.** Historical IV needs your own daily
  ingest or a paid provider.
- **Earnings dates come from yfinance**, not OpenBB: OpenBB 4's earnings calendar
  needs a paid FMP key. ETFs have no fundamentals, earnings or financials.
- The EDGAR and OpenBB fetchers follow those libraries' current APIs; if a
  provider renames a column, adjust the mapping in `sources.py` / `insiders.py`.

## Layout

```
config.yaml               settings and risk limits
src/optlab/
  data/        store.py (DuckDB) · sources.py (OpenBB, synthetic) · chain.py
  filings/     insiders.py · company.py (EdgarTools)
  pricing/     engine.py (QuantLib) · fast_bs.py (NumPy BSM)
  strategies/  covered call, CSP, credit spreads, iron condor, straddle
  backtest/    quick.py (vectorbt)
  risk/        sizing.py (limits + skfolio)
  research/    features.py (Polars features, scanner, Qlib export)
app/streamlit_app.py      dashboard
scripts/                  ingest.py · demo.py
integrations/             lean/ · qlib/ · nautilus/
tests/                    pytest suite
```
