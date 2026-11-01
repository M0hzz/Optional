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
invalidation ("what would make me drop this"), entry conditions (checked by the monitor every pass, see
below) and notes. Tick *research only* to keep it outside any account. **Get quote** shows bid × ask with its
freshness; hover for spread, volume, open interest, IV, Greeks, intrinsic/extrinsic value and breakeven.

Statuses: Researching · Waiting for confirmation · Entry qualified · Entry condition lost · In position ·
Expired (set automatically for a past expiration) · Archived.

## Position statuses (monitoring)

Every open position with **Monitor this position** ticked (the default) is checked by the monitor on its
schedule. Each pass values the position from the shared option-chain snapshot, evaluates its rules and the
built-in checks, stores a snapshot, and gives it one status. Highest precedence first:

| Status | Meaning |
|---|---|
| ⌛ **Stale data** | The data can't be trusted: quotes unavailable or older than the limit, a leg not in the chain or without a two-sided quote, a rule that can't be evaluated (a value it needs is missing), or no check for longer than *Stale after* (the monitor stopped, it's outside the monitoring hours, or passes fail). Never shown as Hold. |
| ⛔ **Exit condition met** | One of the position's *exit* rules holds. |
| ❗ **Action required** | One of its *action* rules holds, or a built-in check: an option leg expires today or has expired (record how it ended), or a short option is in the money with *Assignment-risk window* days or fewer left. |
| ◔ **Watch** | One of its *watch* rules holds. |
| ● **Hold** | None of the above, on current data. |
| ✓ **Closed** | Not open. The monitor stops at once when you close or archive a position. |
| ○ **Not monitored** | You switched monitoring off for it. Nothing checks it. |

**Every status says why**, with the values the rules used and the previous pass's value when it changed,
for example *Watch: short_strike_otm_pct < 3 (short_strike_otm_pct = 1.8%, was 2.4%)*. Hover a status
badge, or open the position: **Rules checked on the last pass** lists every rule with its result
(holds / no / unknown) and values.

**Confirmation.** A new status must be seen on *Confirm passes* passes in a row (default 3, from
scans.yaml's `alerts.confirm_passes`) before it replaces the current one, so a value sitting on a
threshold doesn't flap. The position page shows a pending change (*Changing to Hold: 1 of 3 passes*).
Stale data is the exception: it applies at once. A position's first status is adopted at once.

### Rules

Rules use the same expression language as `scans.yaml`. Each position follows the **default rules**
(Monitor → Settings; initially your scans.yaml `position_defaults`) unless you give it its own (**Edit
rules** on the position page). Names you can use (the editor lists them, with meanings):

`pnl`, `pnl_pct`, `value`, `entry`, `dte`, `short_strike_otm_pct`, `position_delta`, `position_gamma`,
`theta_day`, `position_vega`, `days_to_earnings`, `earnings_before_expiry`, `price`, `max_spread_pct`,
`min_open_interest`, `loss_pct_of_max`, `short_itm`, the chain values `atm_iv`, `skew`,
`put_call_volume`, `put_call_oi`, `option_volume`, and every price-bar study (`rsi(14)`, `sma(50)`,
`ema(9)`, `close`, ...) on the timeframe you choose (completed bars only, by default).

A rule that uses a value which doesn't apply (the short-strike distance of a position with no short
legs) simply doesn't fire. A rule that uses a value which applies but is missing is *unknown*, and the
position shows Stale data. Rules are checked as you type; an invalid one can't be saved.

## Alerts and events

The **Alerts and events** feed (Command events in Colony mode) lists, newest first: status changes,
reminders, alert rules firing and clearing, expiration, earnings and liquidity warnings, watched-contract
status changes, monitor failures, and changes to your records (recorded, edited, closed, archived,
settings). Each shows its severity (info, warning, critical), time, symbol, explanation, the source data
(rule values) and whether it was notified. Warnings and critical events need **Acknowledge**: that only
records that you saw it. A position's page shows its own feed and a timeline that merges its events with
its edit history.

What creates an event (and nothing else does, so the same condition never floods the feed):

| Event | When |
|---|---|
| Status change | A position's confirmed status changes (one event per change) |
| Reminder | Exit condition met / Action required still holds after *Repeat* minutes (default 15; 0 = never) |
| Expiration | Once per threshold (default 7 days and 1 day before; critical on the last day) |
| Earnings | Once, when an earnings date falls on or before the nearest expiration |
| Liquidity | The widest leg spread passes the limit (default 25% of mid) or a long leg has a zero bid; again only after it has cleared and *Re-arm* minutes have passed |
| Alert | One of your alert rules becomes true (below) |
| Contract | A watched contract qualifies, loses its entry, or expires |
| Monitor | A pass fails (once until it recovers) |

### Alert rules

**Alert rules** (Monitor panel, or a position's page, or a contract's **Alerts**) watch a condition on a
symbol (`price < 640`, `rsi(14) < 30`, `atm_iv > 0.4`), a position (`loss_pct_of_max >= 50`) or a watched
contract (`spread_pct > 15`). One fires when its condition has held for *Confirm passes* passes, then
stays quiet until the condition clears **and** *Re-arm* minutes have passed since it fired; it repeats
every *Repeat* minutes while true only if you set that. Pause, edit or archive them in the list.

### Watched contracts: entry conditions

Write one condition per line, using the contract's quote: `bid`, `ask`, `mid`, `spread_pct`, `iv`,
`delta`, `abs_delta`, `oi`, `volume`, `dte`, `price`, `otm_pct`, `intrinsic`, `extrinsic`, `breakeven`,
`target_entry` (your target price), plus chain values and price-bar studies. Example:

```
ask <= target_entry
oi >= 500
spread_pct < 10
```

While the contract's status is *Waiting for confirmation*, *Entry qualified* or *Entry condition lost*,
the monitor moves it to **Entry qualified** when every condition holds (on *Confirm passes* passes in a
row), and to **Entry condition lost** when they stop holding. That is information only: no order is
placed. *Researching* contracts are quoted and their conditions shown, but their status never changes by
itself. Past their expiration they become *Expired*.

## Notifications

- **Desktop** (Monitor → Settings): `notify-send` on the machine running the API (your Ubuntu desktop,
  under systemd). Not available inside Docker.
- **This browser** (Monitor panel): pop-up notifications while the dashboard is open in that browser.
  Browsers allow them only over HTTPS or on localhost; the Tailscale `https://` link works, also on a
  phone with the page open.
- Only warning and critical events notify (or critical only); info never does. **Quiet hours** hold
  back warnings (critical still comes through unless you untick it). **Muted symbols** and a position's
  **Notify** switch suppress pop-ups for them. Suppressed events still appear in the feed and still need
  acknowledging; the event says why it wasn't notified. Each desktop attempt is logged (sent, failed,
  unavailable).

## Live updates

The dashboard keeps a connection to the server (`/api/v1/stream`, server-sent events): new events and
finished passes refresh the page at once, and warnings show a toast. The sidebar shows *● live*; if the
connection drops it says *polling* and the dashboard refreshes every 30 seconds until it's back, missing
nothing (it resumes from the last event it saw).

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

## Values and data freshness

Open P&L, values and Greeks on the dashboard come from the latest monitoring pass and carry its age.
**Fresh estimates** / **Get an estimate now** / **Get quote** fetch on demand instead. All are labelled:

| Label | Meaning |
|---|---|
| Live | Real-time quote (no real-time provider is configured yet) |
| Delayed | Cboe through OpenBB, about 15 minutes behind during market hours |
| Cached | An older snapshot: the last refresh failed, it's past the 5-minute limit, or the monitor hasn't checked the position within *Stale after* |
| Estimated | Calculated from quotes (estimated value, open P&L, Greeks), not a broker value |
| Synthetic | Offline demo data (`data.source: synthetic`), never market prices |
| Unavailable | No data. Nothing is filled in |

A position's value is shown only when every leg has a usable quote (no partial values). A contract that
isn't in the chain (adjusted, expired, changed symbol) shows *Not in chain* with the nearest strikes or
expirations. Mid prices are used for the estimate; the exit value uses natural prices (sell at the bid,
buy back at the ask).

The account overview adds **Open P&L** (sum of the latest values; it says how many positions lack a
recent one rather than adding a partial total), **Net Greeks** (only when every open position has them)
and **Need attention** (positions in Stale data, Exit, Action or Watch).

**History** on a position's page charts open P&L, P&L %, delta, theta or the underlying from its
snapshots, one point per pass, with status changes marked. Snapshots are kept in full for *Keep every
snapshot* days (default 30), then thinned to one per hour; snapshots where the status changed are always
kept.

## Importing positions from scans.yaml

**Import scans.yaml** (Monitor panel) shows the `positions:` of the project's scans.yaml with their
recognised strategy, maximum loss and rules, and imports the new ones in one go (each once; duplicates are
recognised by their legs). Their `exit_when` / `watch_when` (or the YAML `position_defaults` they used) and
`timeframe` come along as the position's own rules. Positions whose maximum loss is unlimited or unknown
need the *I understand the risk* tick. The YAML has no opening date or fees: they import as now and $0;
edit them if you know them. scans.yaml isn't changed, and the YAML monitor (`optlab-monitor`) skips the
imported positions from then on (it prints which), so nothing alerts twice. You can delete them from
scans.yaml whenever convenient.

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
