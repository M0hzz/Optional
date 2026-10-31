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
  status: { pro: "Status", colony: "Mission status", meaning: "Lifecycle: open, closed or archived. Live monitoring statuses arrive in Phase 2." },
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
