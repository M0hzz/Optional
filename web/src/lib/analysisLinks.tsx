// Links into the historical analysis (kept separate so the dashboard doesn't load the analysis view and its charts).
import type { SubjectType } from "../api/types";
import { Tip } from "../components/ui";

export function analysisHref(type: SubjectType, id?: string | null) {
  return `#/analysis/new/${type}${id ? `/${id}` : ""}`;
}

export function AnalysisLink({ type, id, small = true }: { type: SubjectType; id: string; small?: boolean }) {
  return (
    <Tip text="Open the Historical Signal Explorer with these conditions: when did they match before, and what did price do afterwards?">
      <a className={`btn ghost ${small ? "small" : ""}`} href={analysisHref(type, id)}>Historical analysis</a>
    </Tip>
  );
}

