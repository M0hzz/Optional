// Professional / Colony terminology. Labels only: the financial meaning is always kept in the tooltip
// (and Colony mode always shows the professional term next to the colony one).
import { createContext, useContext } from "react";

export type Mode = "professional" | "colony";

export interface Term { pro: string; colony: string; meaning: string }

export const TERMS = {
  account: { pro: "Account", colony: "Colony", meaning: "Your brokerage account as recorded in optlab." },
  accounts: { pro: "Accounts", colony: "Colonies", meaning: "Brokerage accounts recorded in optlab." },
  buyingPower: { pro: "Buying power", colony: "Treasury", meaning: "Money available to open new positions, as you last recorded it." },
  cash: { pro: "Cash", colony: "Reserves", meaning: "Cash balance, as you last recorded it." },
  totalValue: { pro: "Account value", colony: "Colony value", meaning: "Total account value (cash plus positions), as you last recorded it." },
  position: { pro: "Position", colony: "Expedition", meaning: "One trade: one or more legs (stock or option contracts) opened together." },
  positions: { pro: "Positions", colony: "Expeditions", meaning: "Your trades, each with its legs, risk and lifecycle." },
  openPositions: { pro: "Open positions", colony: "Active expeditions", meaning: "Positions not yet closed." },
  watched: { pro: "Watched contracts", colony: "Scouted units", meaning: "Option contracts you are researching but haven't bought or sold." },
  capitalAtRisk: { pro: "Capital at risk", colony: "Resource exposure", meaning: "Sum of the maximum loss of open positions whose maximum loss is bounded." },
  maxLoss: { pro: "Maximum loss", colony: "Maximum loss", meaning: "The most the position can lose if held to expiration, from the recorded fills and opening fees." },
  riskControls: { pro: "Risk", colony: "Colony defense", meaning: "Risk limits and checks." },
  status: { pro: "Status", colony: "Mission health", meaning: "Monitoring status from the latest check: Stale data, Exit condition met, Action required, Watch or Hold (in that order of precedence), each with the reasons." },
  events: { pro: "Alerts and events", colony: "Command events", meaning: "Status changes, alerts, warnings and changes to your records, newest first. Warnings and critical events need acknowledging." },
  monitor: { pro: "Monitor", colony: "Expedition control", meaning: "The position monitor: checks open positions, watched contracts and alert rules on a schedule using delayed quotes. It never places orders." },
  openPnl: { pro: "Open P&L", colony: "Expedition yield", meaning: "Estimated profit or loss of open positions: value at mid prices minus entry cost and opening fees, from the latest monitoring pass." },
  netGreeks: { pro: "Net Greeks", colony: "Net Greeks", meaning: "Sum over open positions: delta in shares, gamma per $1, theta in $ per day, vega in $ per vol point." },
  attention: { pro: "Need attention", colony: "Expeditions needing orders", meaning: "Open positions whose status is Stale data, Exit condition met, Action required or Watch." },
  plan: { pro: "Trade plan", colony: "Mission", meaning: "A plan written before entering: thesis, trigger, confirmation, invalidation, planned risk and size. The monitor watches it; converting it records the position and compares what you did with what you planned." },
  plans: { pro: "Trade plans", colony: "Missions", meaning: "Plans written before entering a position, with their state from Draft to Converted, Skipped or Expired." },
  risk: { pro: "Risk", colony: "Colony defense", meaning: "Your risk policy and how the account stands against it: PASS, WARNING, BLOCKED (breaks a rule) or UNKNOWN (can't be checked yet). It never places or stops orders." },
  alertRules: { pro: "Alert rules", colony: "Tripwires", meaning: "Your own conditions on a position, a watched contract or a symbol. Each fires once, re-arms after it clears, and can repeat." },
  research: { pro: "Research Lab", colony: "Research Lab", meaning: "The original optlab analysis app (Streamlit): backtests, history, pricing, company data." },
  dashboard: { pro: "Dashboard", colony: "Command center", meaning: "Overview of your account, positions and watched contracts." },
} satisfies Record<string, Term>;

export type TermKey = keyof typeof TERMS;

export const TermsContext = createContext<Mode>("professional");

export function useTerms() {
  const mode = useContext(TermsContext);
  return {
    mode,
    t: (k: TermKey) => (mode === "colony" ? TERMS[k].colony : TERMS[k].pro),
    meaning: (k: TermKey) => {
      const x = TERMS[k];
      return mode === "colony" && x.colony !== x.pro ? `${x.pro}: ${x.meaning}` : x.meaning;
    },
    secondary: (k: TermKey) => (mode === "colony" && TERMS[k].colony !== TERMS[k].pro ? TERMS[k].pro : null),
  };
}
