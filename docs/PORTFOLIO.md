# Portfolio dashboard: user guide

Open the dashboard at `http://127.0.0.1:8600` (or your Tailscale link). The first time, create an
account, then enter its values (copied from your broker). Everything is informational: optlab never
places orders.

## Adding a position

1. **+ Add position** on the dashboard.
2. Pick a template (put credit spread, iron condor, covered call, ...) or **Custom**, then type the
   underlying symbol.
3. For each leg: stock or option, buy or sell, quantity (whole shares or contracts), and for options
   call/put, strike and expiration. Enter the **fill price per share** (an option premium of $1.25 is
   `1.25`, not `125`) and the leg's total **fees**. Under **More**: fill time, broker order ID, notes.
4. Read the **Analysis preview** (nothing is saved yet):
   - the recognised strategy, or **Custom** when the legs don't exactly match a named strategy;
   - **Entry cost**: net premium paid (debit) or received (credit, shown negative), plus fees;
   - **Maximum loss / gain** in dollars, or **Unlimited**, or **Unknown**: never $0 for "not known";
   - **Breakevens** at expiration; **Buying-power effect** (an estimate, with its basis);
   - Greeks from current quotes, with the quote's source and delay;
   - errors (must be fixed), warnings (expired legs, earnings before expiry, a debit spread entered as a
     credit, ...), and assumptions.
5. If the maximum loss is unlimited or unknown, tick the acknowledgement. Such positions are never
   labelled defined risk.
6. **Record position**. A double click can't create it twice.

A short put is recorded as **uncovered** unless you tick *cash-secured* (cash set aside for assignment).
A short call is **covered** only when the same position holds exactly 100 shares per contract.

## Watched contracts

**+ Watch a contract**: symbol, call/put, strike, expiration, and optionally a target entry price, the
invalidation ("what would make me drop this"), alert conditions (stored now, evaluated from Phase 2)
and notes. Tick *research only* to keep it outside any account. **Get quote** shows bid × ask with its
freshness; hover for spread, volume, open interest, IV, Greeks, intrinsic/extrinsic value and breakeven.

Statuses: Researching · Waiting for confirmation · Entry qualified · Entry condition lost · In position ·
Expired (set automatically for a past expiration) · Archived.

## Position statuses (Phase 1)

| Status | Meaning |
|---|---|
| Open | Recorded and not closed. Not monitored live yet (Phase 2). |
| Closed | Every leg has a closing fill (or expired worthless); realized P&L is final. |
| Archived | Kept read-only. Either archived after closing, or archived as *entered in error* (no P&L). |

HOLD, WATCH, ACTION REQUIRED, EXIT CONDITION MET and STALE DATA arrive with live monitoring in Phase 2.

## Closing, editing, archiving

- **Close…**: a closing price per share for every leg (or *expired worthless*), closing fees, time.
  Realized P&L per leg = side × quantity × multiplier × (close price − fill price), minus opening and
  closing fees. You confirm before it's saved.
- **Edit**: name, notes and legs of an open position. Every change to fills is recorded with the values
  before and after (**History** on the position page). Closed positions need a correction reason, and
  their legs can't be replaced.
- **Archive…** a closed position, or **Entered in error…** for an open one recorded by mistake.
- Edits carry the version you loaded: if the position changed meanwhile you get a conflict, not an
  overwrite. Reload and redo.

## Current estimates and data freshness

Values that depend on market data are fetched only when you ask (**Show estimates**, **Get on-demand
estimate**, **Get quote**) and are labelled:

| Label | Meaning |
|---|---|
| Live | Real-time quote (no real-time provider is configured yet) |
| Delayed | Cboe through OpenBB, about 15 minutes behind during market hours |
| Cached | An older snapshot: the last refresh failed or it's past the 5-minute limit |
| Estimated | Calculated from quotes (estimated value, open P&L, Greeks), not a broker value |
| Synthetic | Offline demo data (`data.source: synthetic`), never market prices |
| Unavailable | No data. Nothing is filled in |

A position's value is shown only when every leg has a usable quote (no partial values). A contract that
isn't in the chain (adjusted, expired, changed symbol) shows *Not in chain* with the nearest strikes or
expirations. Mid prices are used for the estimate; the exit value uses natural prices (sell at the bid,
buy back at the ask).

## Money and rounding

- Money is stored exactly, in units of $0.0001.
- Prices, fees and account values you type may have **at most 4 decimal places**; more is rejected,
  never rounded.
- Values optlab calculates (totals, P&L, provider quotes that arrive as floats) are rounded once, to
  $0.0001, half to even. The screen shows dollars to 2 decimals (half to even) and prices to 2-4.
- Quantities are whole numbers. Fractional shares aren't supported yet.

## CSV import and export

**Import CSV** checks the file first (nothing is saved), then imports in one transaction. *Import
nothing* (default) refuses the whole file if anything is wrong; *Import only the valid positions* skips
invalid positions entirely (a position is never imported with some of its legs missing). Duplicates
(the same leg twice, or a position already stored with the same account, symbol, open date and legs)
are reported and not imported. Limit: 1 MB, 2,000 rows, UTF-8.

One row per leg; rows with the same `position_ref` form one position:

| Column | Required | Example |
|---|---|---|
| `position_ref` | yes | `IC-1` |
| `underlying` | yes | `SPY` |
| `asset_type` | yes | `OPTION` or `STOCK` |
| `side` | yes | `BUY` or `SELL` |
| `quantity` | yes | `1` |
| `option_right` | options | `PUT` |
| `strike` | options | `720` |
| `expiration` | options | `2026-12-18` |
| `fill_price` | no | `7.45` |
| `fees` | no | `0.65` |
| `filled_at` | no | `2026-11-02T14:31:00-05:00` |
| `opened_at` | no (needed for duplicate detection) | `2026-11-02T14:30:00Z` |
| `position_name`, `broker_order_id`, `notes` | no | |
| `cash_secured`, `acknowledge_risk` | no | `true` / `false` |

**Export CSV** writes the same columns (so an export re-imports). Text cells starting with `=`, `+`, `-`,
`@`, tab or carriage return are prefixed with `'` so spreadsheets don't run them as formulas.

## Terminology

The sidebar switches between **Professional** and **Colony** labels (Account ↔ Colony, Buying power ↔
Treasury, Positions ↔ Expeditions, Watched contracts ↔ Scouted units, Capital at risk ↔ Resource
exposure, ...). It changes labels only; in Colony mode the financial term is shown beside each label,
and every tooltip gives the exact financial meaning.

## Supported strategies

Recognised exactly (anything else is **Custom**, still with full payoff maths when it has one
expiration): long/short stock, long call, long put, short call (uncovered), short put (uncovered),
cash-secured put, covered call, protective put, collar, call/put debit spreads, call/put credit spreads,
calendar spread, diagonal spread, iron condor (equal width or broken wing), iron butterfly, long/short
straddle, long/short strangle. Calendars and diagonals report maximum gain and breakevens as unknown
(they depend on implied volatility at the front expiration); the maximum loss of a long calendar or
diagonal is bounded under the stated assumption.
