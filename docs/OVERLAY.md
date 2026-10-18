# The overlay (Mastermind console)

The overlay scans the data the app already has and highlights what is worth looking at on every view:
confirmed technical conditions, near confirmations and exactly what they still need, fresh vs stale vs
overextended signals, invalidations, market and sector alignment, historical sample quality,
option liquidity and data problems, small-account fit, assignment and event exposure, and cross-tab
confluence.

**It uses no AI.** There is no language model, chatbot, machine-learning model, AI API or
black-box detector anywhere in it. Every highlight, score, badge, warning and sentence comes from a
rule in `overlay.yaml` or a formula in this document, filled into a fixed sentence template. The
same data and settings always give the same output (a test checks this).

It is research and education only. It never places or submits orders, never connects to a broker,
never picks a trade to execute, never calls anything guaranteed or safe, and never hides a technical
signal just because no option structure fits the account.

---

## Where it appears

The dashboard has two sections: **Live** (market hours) and **Research** (backtests and analysis).

| Place | What the overlay adds |
|---|---|
| **Console** (above every view) | Status summary chips, the highlights worth looking at (most important first) with a pointer to the view that has the detail, and an evidence view for each one. Open in Live; folded to a one-line summary in Research (click to open). |
| **Live › Watchlist** | Every rule on every symbol: status, score, passes, what's missing, freshness, ATR extension, timeframe alignment, audit record; cross-tab confluence. |
| **Live › Market** | Market overview: indexes, breadth, VIX, volatility regime, sectors, regime checklists. |
| **Live › Option chain** | Per-contract liquidity, quality flags and account fit; IV context; defined-risk structure scanner; liquidity deterioration. |
| **Live › Trade builder** | Defined-risk test, account fit, breakevens vs expected and historical moves, hidden-risk warnings, leg-removal effects, outlook check, model vs quotes, stress scenarios, assignment scenarios; small-account check of the sized trade. |
| **Live › Monitor** | Overlay alert timeline from the live monitor; liquidity and fit columns on option hits. |
| **Research › Backtest** | Diagnostics: sample size, stability, out-of-sample, costs, neighbouring settings, regimes, biases. |
| **Research › Signal explorer** | Historical evidence for any overlay rule; low-sample warnings on explorer results. |
| **Research › Volatility & pricing** | Checks on the stored chain snapshot (age, crossed markets, Greeks). |
| **Research › Overlay setup** | Data health, rules, status guide, settings editor, audit log. |

Switch it all off with **Show overlay** in the sidebar, or per view in Overlay setup → Settings.
The console can be compacted (one line per highlight) and filtered by status and severity.

The spec's *Market overview* is Live › Market, *Watchlist / technical scanner* is Live › Watchlist,
*Strategy builder* and *Simulator* are Live › Trade builder, *Options scanner* is Live › Option chain
(structure scanner) and Live › Monitor (option scan hits), and *Data health / settings* is Research ›
Overlay setup. The project has no trade journal, so the journal overlay (spec section 16) does not apply.

---

## Statuses

Every status has a colour **and** an icon **and** text, so nothing depends on colour alone.

| Status | Icon | Colour | Meaning |
|---|---|---|---|
| Confirmed | ✅ | green | Required conditions pass and the score / pass-count rules of the rule's confirmation section are met on the latest completed bar. |
| Near confirmation | 🟡 | amber | Score ≥ near_match_score and every required condition passes or is within its tolerance. |
| Watch | 🔵 | blue | Score ≥ watch_score. |
| Not present | ▫️ | gray | Below the watch score (shown only in details). |
| Invalidated | ⛔ | red | Was confirmed within stale_after_bars bars and has lost it, or an invalidation condition is true. |
| Stale signal | ⌛ | gray | Still confirmed, but the confirmed run began more than stale_after_bars bars ago. |
| Overextended | 🟣 | purple | Still confirmed, but price is more than overextended_atr_multiple ATRs from the trigger close or the rule's reference average. |
| Conflicting | ⚖️ | amber | Bullish and bearish rules confirmed on the same symbol and timeframe. |
| Low sample size | 🔬 | amber | Fewer historical events than minimum_historical_samples. |
| Poor liquidity | 💧 | red | A liquidity check failed. |
| Does not fit account | 🚫 | red | Capital, maximum loss, risk or assignment exceeds the account profile. |
| Fits buying power but exceeds risk limit | ⚠️ | amber | Buying power covers it; a preferred per-idea limit is exceeded. |
| Fits configured limits | 🧾 | **blue** | Affordable. Blue, never green: affordability says nothing about setup quality. |
| Insufficient data | ❔ | gray | A needed value is missing or warming up. Nothing is substituted. |
| Stale data | 🕰️ | gray | Bars or quotes are older than the freshness limits. |
| Data error | 🛑 | red | A data-health check blocks the calculation. |
| Volatility expansion | 🌀 | purple | Volatility moved from compressed to expanding. |
| Requires data validation | 🔎 | amber | A quote is inconsistent with its neighbours. A data question, never an opportunity. |

Display labels can be renamed (Overlay setup → Settings → Display labels, or `labels:` in overlay.yaml)
without changing what a status means. The words *guaranteed, safe, certain, best trade, winning
trade* and *free money* never appear (a test checks every label and explanation).

---

## Rules

Rules live in `overlay.yaml` (your settings) over `src/optlab/overlay/defaults.yaml` (built-in, don't
edit). Conditions use the same expression language as `scans.yaml` and the Signal explorer, and are
scored by the same code.

```yaml
overlay:
  rules:
    - id: bullish_continuation
      name: Bullish continuation
      direction: bullish            # bullish | bearish | neutral
      group: trend_up               # related rules share a group and count once in signal stacking
      timeframe: 1d                 # 1d | 1wk (dashboard and monitor); 5m | 15m | 30m | 1h (live monitor only)
      tab_scope: [scanner, macro, monitor]
      conditions:
        - {id: above_vwap, expression: "close > rolling_vwap(20)", required: true, weight: 20,
           tolerance: {type: percent_distance, value: 0.5}}
        - {id: volume, expression: "rel_volume(20) >= 1.5", required: false, weight: 25,
           tolerance: {type: absolute, value: 0.2}}
        - {id: relative_strength, expression: "rs(20) > 0", required: false, weight: 20}
      confirmation: {minimum_score: 85, minimum_pass_count: 4, required_conditions_must_pass: true}
      invalidation: ["close < ema(21)"]
      reference_average: ema(21)    # used for the overextension check
```

Quote any expression that contains a comma. Every setting is validated with its exact path, e.g.
`overlay.rules[2].conditions[0].expression: unknown function rsii(); did you mean rsi?`. A broken
overlay.yaml never takes the dashboard down: the built-in defaults are used and the problems listed.

Built-in rules: bullish continuation, bearish rejection, trend pullback, confirmed breakout, failed
breakout, relative-strength divergence, compression (nondirectional until a confirmed range exit),
and compression to expansion.

Overlay-only names in expressions: `bench_close` (benchmark close), `rs_line` (close ÷ benchmark
close), `sector_close`, `rs(n)` (symbol's n-bar % change minus the benchmark's) and `sector_rs(n)`
(the same against the symbol's sector ETF). Benchmark dates the symbol lacks stay empty, so a
condition using them is unavailable rather than guessed. Two indicator functions were added for
everyone (scans, explorer, overlay): `rolling_vwap(n)` and `pct_rank(x, n)`.

### Scoring and states

Per completed bar:

```
score = 100 × Σ(weight × credit) / Σ(weight)
credit: pass 1; near 1 − shortfall ÷ tolerance (at most 0.99); fail 0; unavailable 0
shortfall: a > b → b − a; a < b → a − b (absolute units, or % of b for percent_distance)
```

- **Confirmed**: required conditions pass (or are at least near, with `required_conditions_must_pass:
  false`), score ≥ minimum_score (rule, else thresholds.confirmed_match_score), passes ≥ minimum_pass_count.
- **Near**: not confirmed, score ≥ near_match_score, every required condition passes or is near.
- **Watch**: score ≥ watch_score. **Not present** otherwise.
- **Insufficient data**: a required condition can't be evaluated on the latest bar.

On the latest bar a confirmation is further labelled:

```
bars_since       = bars since the current confirmed run began
move_since_pct   = close ÷ trigger close − 1
atr_move         = (close − trigger close) ÷ ATR(14) at the trigger
ref_distance_atr = (close − reference average) ÷ ATR(14)
STALE         bars_since > stale_after_bars
OVEREXTENDED  |atr_move| or |ref_distance_atr| > overextended_atr_multiple
INVALIDATED   an invalidation condition is true, or confirmed within stale_after_bars bars and no longer
```

Transitions between the previous and latest completed bar: *near → confirmed*, *newly confirmed*,
*confirmation lost*, *newly near*.

**No look-ahead:** every study is causal (rolling windows, EMAs, positive shifts), and only completed
bars are used: a daily bar dated today is dropped until 16:00 New York time; an intraday bar is
dropped until its end time. A test checks that truncating the history never changes earlier verdicts.

**Timeframe alignment** (three separate states): higher = weekly close and 5-week EMA vs the 10-week
SMA; intermediate = daily close and 21-day EMA vs the 50-day SMA; trigger = daily close and 9-day EMA
vs the 21-day EMA. Weekly bars are built from daily bars; an unfinished week is dropped.

---

## Data health

Checks run before any rule. A failed check either **blocks** dependent features (shown as Data
error) or **downgrades** them (shown with a Stale data or Downgraded flag); nothing is repaired
silently.

| Check | Effect when it fails |
|---|---|
| Bars present, in time order, valid latest prices | block technical rules |
| Duplicate bars | downgrade (last copy used, and said so) |
| Missing NYSE sessions | downgrade historical evidence |
| Split-like jump (2×, 3×, ½ …, > 35%) | block historical evidence and backtests until adjusted data is loaded |
| Data older than stale_data_sessions | downgrade rules, market overview and account fit (Stale data) |
| Intraday bar latency > 2 bars + feed delay | downgrade |
| Option chain: crossed markets, zero bids, missing Greeks, invalid IV, both types present | contracts flagged; crossed legs make structures "requires data validation" |
| Quote age > maximum_quote_age_seconds (or stored snapshot older than the last session) | every contract's liquidity becomes poor and account fit is shown as stale |

```
data-quality score = 100 − 40 × blocking failures − 10 × downgrading failures (min 0)
```

---

## Market overview (Live › Market)

From the stored daily bars and the macro table's VIX series:

- Index states for the configured indexes (default SPY, QQQ, IWM, DIA; missing ones are listed,
  not estimated): close vs 20-day VWAP, EMA 9 vs 21, 50- and 200-day averages, trend, gap, relative
  volume, ATR and band-width percentiles.
- Breadth over the non-index symbols: advancers/decliners, advancing/declining volume, % above the
  50-day and 200-day averages, 252-bar new highs/lows. With fewer than breadth_minimum_symbols it
  carries a **low sample size** warning (the default universe has 5 stocks).
- Sector strength (20-day relative strength vs the benchmark, rank, rank change) only when sector ETFs
  are in the database; otherwise "unavailable" with how to enable it.
- No economic calendar is configured, so scheduled releases are not shown.

Regime checklists (present when every available item passes; nearly present with one failure):
**broad bullish alignment**, **broad bearish alignment**, **breadth divergence** (benchmark at a
divergence_bars closing high while breadth makes no new high above its level then, advancing volume
share < 50%), **volatility regime transition** (band-width percentile ≤ compression + 5 five bars ago,
now ≥ expansion, relative volume ≥ 1.3, price at a 20-day high or low) and **sector rotation**.

---

## Live monitor alerts

`python scripts/monitor.py` runs the overlay every pass (daily rules on completed daily bars,
intraday rules on completed intraday bars) and alerts on state changes only:

- one alert per (rule, symbol, timeframe, bar, transition), however many passes see that bar;
- the same (rule, symbol, timeframe, status) won't alert again inside cooldown_minutes;
- **signal stacking**: rules from ≥ 2 different groups confirming on a symbol within stacking_bars
  bars give one alert; rules in the same group count once.

Example: `CONFIRMED at the 2026-10-16 close. Previous score: 75. Current score: 100. rel_volume(20)
moved from 1.43 to 1.57 and now meets the >= 1.5 requirement.`

The full alert layout (STATUS, RULE, SYMBOL AND TIMEFRAME, OBSERVED CONDITIONS, MISSING CONFIRMATION,
MARKET CONTEXT, HISTORICAL EVIDENCE, ACCOUNT FIT, DATA QUALITY, EVENT FLAGS, INVALIDATION) is produced
by `optlab.overlay.alerts.format_alert`; every section is always present.

---

## Small-account calculations (Small Account Mode, on by default)

```
allocatable_capital = min(settled_cash − reserve_cash, total_buying_power − reserve_cash)
risk_limit          = max_account_risk_pct % × total_buying_power
```

Natural prices (buy at the ask, sell at the bid), one contract, multiplier 100:

| Structure | Capital required | Maximum loss |
|---|---|---|
| Long option | ask × 100 | capital |
| Debit spread | (long ask − short bid) × 100 | same |
| Credit spread | max loss | (width − (short bid − long ask)) × 100 |
| Cash-secured put | strike × 100 − premium | capital (stock to zero) |
| Covered call | 100 shares × price − premium (shares excluded if `owned_shares` records them) | share cost − premium |
| Naked short call | — | unlimited → UNDEFINED RISK |

Classification, first failing check wins (all failing checks are listed): INSUFFICIENT DATA →
UNDEFINED RISK → EXCEEDS CAPITAL LIMIT (capital > allocatable) → ASSIGNMENT OBLIGATION EXCEEDS
CAPACITY (short strike × 100 > settled cash, or buying power in a margin account, when
require_assignment_capacity) → EXCEEDS ACCOUNT-RISK LIMIT (max loss > risk_limit) → EXCEEDS MAXIMUM
LOSS (max loss > max_loss_per_idea) → FITS BUYING POWER BUT EXCEEDS PREFERRED RISK (capital >
max_capital_per_idea, or more than max_contracts) → FITS CONFIGURED LIMITS. Limits are inclusive.

With the default $1,000 profile, any spread on a stock above $10 shows *assignment obligation exceeds
capacity*, because an assigned short leg can require buying or delivering 100 shares before the long
leg helps. That is deliberate; switch `require_assignment_capacity` off in Settings if you accept
that risk. Midpoint figures are shown separately for reference and never as expected fills.
Technical signals stay visible whether or not anything fits the account.

---

## Options overlay (Live › Option chain)

Per contract: spread in $ and % of mid, debit per contract, % of account at risk, buying power left,
cash to exercise, liquidity, account fit, quality flags and data validation.

**Usable liquidity** only when every check passes: quote fresh, bid > 0, not crossed,
(ask − bid) ÷ mid ≤ maximum_bid_ask_pct, open interest ≥ minimum_open_interest, volume ≥
minimum_option_volume. **Quality flags:** zero bid, crossed market, wide spread, stale quote, low
volume, low open interest, missing Greeks, invalid IV (≤ 0 or > 500%), high theta (|theta| ÷ mid >
high_theta_pct %), far OTM low delta (|delta| < far_otm_delta), expires within expiration_warning_days,
earnings before expiration. Cheap is never treated as good.

**IV context:** ATM IV vs 20-day realized vol, each contract vs the median IV of the two strikes either
side, each expiration's ATM IV vs the average of its neighbours. IV rank and percentile only with
≥ 60 stored daily IV observations; otherwise the reason is shown.

**Anomalies** ("requires data validation"): a call priced above the next-lower strike's call (or a put
below the next-lower put) by more than a cent, an ask below intrinsic value, a debit wider than its
spread, a credit larger than its width, a crossed leg.

**Structure scanner:** long calls/puts and adjacent-strike verticals (bull call / bear put debit, bull
put / bear call credit) for chosen expirations, with **separate** technical, liquidity, account-fit and
validation columns: never merged into an opportunity score. **Liquidity deterioration** compares
each load with the previous one (spread widened 50% past the limit, bid gone, volume fell below the
minimum, quote went stale).

---

## Trade builder and simulator

```
breakevens       zero crossings of the expiry payoff (0.02% grid, linear interpolation)
expected move    price × IV × √(DTE ÷ 365)
ATR move         ATR(14) × √DTE
historical move  share of past DTE-session windows whose |close-to-close move| reached the breakeven distance
leg removal      each leg dropped in turn; undefined when the rest is net short calls
```

Hidden-risk warnings: undefined loss, illiquid leg (from the stored chain), assignment exposure,
expiry mismatch, early-exercise risk on in-the-money short legs, pin risk (price within pin_risk_pct of
a short strike with ≤ 7 days left), breakeven beyond 2 expected moves, a profitable range narrower than
one ATR, slippage (5% of each premium) above 20% of the maximum profit, one-leg removal producing
undefined risk, and limits exceeded. The optional outlook (direction, price range, holding days,
volatility) is compared with position delta, the profitable share of the range, DTE and vega.

Stress scenarios (model value after the scenario − entry cost, one unit; IV changes are relative and
applied as one shift to every leg): price ±0.5/1/2 ATR now, IV ±10%/±25%, 1/3/7 days pass, worse-than-mid
entry, conservative exit, expiration week (with and without −1 ATR), at expiration, and 25/50/100% of the
maximum loss. Each shows P&L, % of the account, account after, breakeven crossing and Greeks; plus the
theta/gamma path to 7 and 2 days left and assignment scenarios against the account's capacity.

---

## Historical evidence (Research › Signal explorer)

```
events      new CONFIRMED (exact) or new NEAR states, so a long run counts once
return      close[i+h] ÷ close[i] − 1, signed for the rule's direction
favorable   max(high[i+1..i+h]) ÷ close[i] − 1 (bullish; lows for bearish), adverse the opposite,
            plus which came first and the bars to each
stability   events split into thirds of history; stable when all three medians share a sign
similarity  normalized_distance_f = |today_f − then_f| ÷ scale_f
            similarity = 100 × max(0, 1 − Σ(weight_f × distance_f) ÷ Σ weight_f)
sensitivity each numeric comparison threshold moved one step (1 for |value| ≥ 10, else 10%) either way
            and events re-counted; flagged when the count changes by more than parameter_sensitivity_drop
```

Regime subsets: above/below the 200-day average, high/low ATR percentile, benchmark above/below its
50-day (breadth subsets would need more symbols). Weak-outcome descriptors compare how often simple
measurable conditions were present in the bottom quarter of outcomes vs all events: a descriptive
frequency, not a cause. Anything with fewer than minimum_historical_samples events carries a low
sample warning.

Historical option affordability is shown only for signal dates that have a stored chain from that
date. Otherwise: *"Technical signal available. Historical options affordability cannot be verified
because timestamped option quotes are unavailable."* The current chain is never applied to past
signals.

---

## Backtest diagnostics (Research › Backtest)

```
profit factor  Σ winning P&L ÷ |Σ losing P&L|        expectancy  mean P&L per trade
exposure       days in a trade ÷ days in the test     outlier share  P&L of the two best trades ÷ total
```

Checks: sample size; outlier dependence (share > outlier_share); median vs mean (different signs or
> 2× apart); years positive (≥ ⅔); out-of-sample (first 70% vs last 30%: confirmed when the later
period keeps the sign and ≥ 1 − parameter_sensitivity_drop of the return, weakened otherwise, invalid
on a sign flip); doubled slippage and commission (flagged when the return halves or turns negative);
neighbouring settings (DTE ± 7, delta ± 0.05: flagged when Sharpe falls by more than
parameter_sensitivity_drop or below zero); regimes represented; look-ahead; survivorship bias
(today's universe); and option fills, which are always modeled (Black-Scholes), never quoted.

---

## Cross-tab confluence (Live › Watchlist)

Component scores (0-100; "—" when the data can't support them):

```
market        Broad bullish 100, Leaning bullish 75, Mixed 50, Leaning bearish 25, Broad bearish 0
              (mirrored for the bearish view)
sector        100 × (1 − (rank − 1) ÷ (sectors − 1)) by 20-day relative strength
technical     highest score among the symbol's standing rules in that direction
historical    % of past exact matches that worked at 5 bars (only with enough samples)
liquidity     share of usable quotes (only with a loaded chain)
data_quality  the data-quality score above
composite     Σ(weight × score) ÷ Σ(weight) over available components   (optional, off by default)
```

Account fit and event risk are statuses and never enter any score, so affordability can't raise
technical quality, and the composite is always shown next to the liquidity and data components and
withheld when a data check blocks the symbol. Weights are editable in Settings.

Templates (each a checklist; present when every available item passes): **A** bullish continuation,
**B** bearish rejection, **C** compression to expansion, **D** historical-analogue alignment, **E**
affordable and liquid structure (Live › Option chain → inspect a structure), **F** waiting for
confirmation, e.g. *"NEAR CONFIRMATION (Trend pullback): 4 of 6 conditions pass. Confirmation requires
at_support: distance_pct(low, ema(21)) is 1.4; the configured requirement is <= 1."*

---

## Auditing a result

- Open **Evidence** in the console or **Inspect a result** in Live › Watchlist: every condition with its
  observed value, requirement, status, distance, tolerance and points, the formula, freshness numbers,
  data source and timestamp, and the versions.
- **Audit record (JSON)** shows exactly what was calculated.
- Research › Overlay setup → **Audit log** lists every state change from `data/overlay/audit.jsonl` with rule,
  configuration and indicator versions, data source and timestamp, scan time, inputs, outputs, formula and
  reason code. Only state changes are appended. Nothing from config.yaml is written, so credentials
  never reach the log.

Versions: *config* hashes every overlay setting, *rule* hashes the rule, *indicators* hashes the
expression engine, indicator, scoring and rule code. Equal versions plus equal data give equal results.

---

## Performance

Indicators are computed once per symbol and shared by every rule (memoised evaluator); results are
cached by settings, database file time and the clock minute, so the console appears immediately on
later page loads. Measured here: 8 symbols × 8 rules × 2,200 daily bars in about 0.65 s; 50 symbols in
about 3.6 s; confluence with history about 1 s (cached). The console shows the scan time, symbols, bars
and rules, and warns above 400 symbol-rule pairs. Nothing is truncated or downsampled.

## Known limitations

- Market breadth comes from the configured universe (8 symbols by default), so it carries a low-sample
  warning; add symbols to config.yaml → universe and run ingest for real breadth.
- Sector strength and rotation need the sector ETFs (XLK, XLF …) in the database.
- No economic or dividend calendar is loaded: those events are reported as unavailable.
- Daily rules confirm on the daily close; intraday rules run only in the live monitor.
- Premarket and opening-range levels need intraday bars and are not part of the daily rules.
- IV rank needs a stored daily IV history the free data doesn't provide.
- Trade-builder values are modeled; compare with Live › Option chain quotes before relying on them.
- The quick backtest's option fills are modeled, never real bid/ask.
