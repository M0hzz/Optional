"""Deterministic event classification (rules-v1). Every result names the rule and the words that matched.

Structured sources come first: an SEC filing is classified by its form and 8-K item codes; a Federal Reserve
release by its feed category; BLS and BEA releases are macro releases. Other items are matched against the
keyword rules below: a match in the headline counts more than one in the summary. Confidence:
structured 0.95; keyword 0.5 + 0.25 per headline match + 0.1 per summary match (at most 0.9).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

VERSION = "classify-rules/v1"

CATEGORIES = {
    "earnings": "Earnings", "guidance": "Guidance", "sec_filing": "SEC filing", "offering_dilution": "Offering / dilution",
    "buyback": "Buyback", "dividend": "Dividend", "merger_acquisition": "Merger / acquisition",
    "analyst_action": "Analyst action", "product": "Product announcement", "regulatory": "Regulatory action",
    "lawsuit": "Lawsuit", "management_change": "Management change", "macro_release": "Macro release",
    "fed_event": "Federal Reserve event", "industry": "Industry event", "other": "Other",
}
# categories that raise awareness for an open position (they can move the price or implied volatility)
RISK = {"earnings", "guidance", "offering_dilution", "merger_acquisition", "regulatory", "lawsuit", "management_change",
        "fed_event", "macro_release"}
WHY = {
    "earnings": "Earnings results often move the price and reset implied volatility.",
    "guidance": "A change in the company's outlook can re-price the stock and its options.",
    "sec_filing": "An official filing with the SEC; the items say what it reports.",
    "offering_dilution": "New shares or convertible debt can dilute existing holders and often weigh on the price.",
    "buyback": "A share repurchase changes the share count and can support the price.",
    "dividend": "Dividend changes affect early-assignment risk on short calls and the stock's carry.",
    "merger_acquisition": "Deals can gap the price and change volatility sharply (and, for a target, cap it).",
    "analyst_action": "Rating and target changes can move the price for a session or two.",
    "product": "Product news can change expectations for the company.",
    "regulatory": "Regulatory decisions and investigations can move the price suddenly.",
    "lawsuit": "Litigation adds uncertainty and can lead to large settlements.",
    "management_change": "Leadership changes add uncertainty, especially unplanned departures.",
    "macro_release": "Macro data moves rates and the whole market, including index options.",
    "fed_event": "Federal Reserve decisions and communication move rates and the whole market.",
    "industry": "Sector news can move every company in it.",
    "other": "General news about the company.",
}

# 8-K items → category (the others stay 'sec_filing')
ITEM_CATEGORY = {"2.02": "earnings", "7.01": "sec_filing", "1.01": "sec_filing", "2.01": "merger_acquisition",
                 "1.03": "regulatory", "3.01": "regulatory", "3.02": "offering_dilution", "2.03": "offering_dilution",
                 "5.02": "management_change", "4.01": "management_change", "4.02": "regulatory", "2.06": "sec_filing",
                 "2.05": "sec_filing", "1.05": "regulatory", "8.01": "sec_filing"}
FORM_CATEGORY = {"S-1": "offering_dilution", "S-3": "offering_dilution", "424B": "offering_dilution",
                 "SC 13D": "merger_acquisition", "SC TO": "merger_acquisition", "DEFM14A": "merger_acquisition",
                 "10-Q": "earnings", "10-K": "earnings"}
FED_CATEGORY = {"monetary policy": ("fed_event", "high"), "speeches": ("fed_event", "low"), "testimony": ("fed_event", "medium")}

_R = [
    ("offering_dilution", r"\b(public offering|secondary offering|stock offering|share offering|offering of (common )?(stock|shares)"
                          r"|at[- ]the[- ]market|convertible (senior )?notes|priced (an? )?offering|dilution|dilutive)\b"),
    ("buyback", r"\b(buybacks?|share repurchases?|repurchase (program|plan)|repurchases? \$?[\d.,]+ ?(billion|million))\b"),
    ("dividend", r"\b(dividends?|special distribution)\b"),
    ("guidance", r"\b(guidance|outlook|forecasts?|raises? (its )?(full[- ]year|annual|fy)|cuts? (its )?(full[- ]year|annual|fy)"
                 r"|reaffirms?|withdraws? (its )?(guidance|forecast))\b"),
    ("earnings", r"\b(earnings|quarterly results|(first|second|third|fourth)[- ]quarter results|q[1-4] (results|earnings)"
                 r"|eps|beats? estimates|misses? estimates|revenue (rose|fell|grew|declined))\b"),
    ("merger_acquisition", r"\b(acquires?|acquisition|merger|merge with|takeover|buyout|to be acquired|tender offer|deal to (buy|acquire))\b"),
    ("analyst_action", r"\b(upgrades?|downgrades?|price target|initiates? coverage|reiterates? (buy|sell|hold|outperform)|rating)\b"),
    ("lawsuit", r"\b(lawsuits?|sues|sued|class action|litigation|settles?|settlement|verdict)\b"),
    ("regulatory", r"\b(fda|sec charges|antitrust|regulators?|probe|investigation|recall|ftc|doj|subpoena|fine[ds]?|sanctions?"
                   r"|approval|approves?|clearance|delist(ing|ed)?)\b"),
    ("management_change", r"\b(ceo|cfo|coo|chief executive|chief financial|president|chairman|resigns?|steps down|retires?"
                          r"|appoints?|names .{0,40}(chief|president|ceo|cfo)|departure)\b"),
    ("fed_event", r"\b(fomc|federal reserve|fed (chair|governor)|rate decision|interest rates?|monetary policy)\b"),
    ("macro_release", r"\b(cpi|consumer price index|payrolls|unemployment rate|jobless claims|gdp|gross domestic product|pce"
                      r"|inflation|producer price|retail sales|job openings)\b"),
    ("product", r"\b(launch(es|ed)?|unveils?|introduces?|new (product|model|chip|drug|service)|rolls? out|release[sd]? (a|its) new)\b"),
    ("industry", r"\b(sector|industry|industry-wide|peers)\b"),
]
RULES = [(c, re.compile(p, re.I)) for c, p in _R]


@dataclass
class Classification:
    category: str
    confidence: float
    rule: str
    matched: dict[str, list[str]] = field(default_factory=dict)       # category -> matched words
    secondary: list[str] = field(default_factory=list)
    importance: str = "medium"

    def as_dict(self) -> dict:
        return {"version": VERSION, "category": self.category, "label": CATEGORIES[self.category],
                "confidence": self.confidence, "rule": self.rule, "matched": self.matched, "secondary": self.secondary,
                "importance": self.importance, "why_it_may_matter": WHY[self.category]}


def classify_text(title: str, summary: str | None) -> Classification:
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for cat, rx in RULES:
        t = [m.group(0) for m in rx.finditer(title or "")]
        b = [m.group(0) for m in rx.finditer(summary or "")]
        if t or b:
            scores[cat] = 0.25 * len(set(x.lower() for x in t)) + 0.1 * len(set(x.lower() for x in b))
            matched[cat] = sorted({x.lower() for x in t + b})
    if not scores:
        return Classification("other", 0.3, "no keyword rule matched", {})
    ranked = sorted(scores, key=lambda c: (-scores[c], [r[0] for r in RULES].index(c)))
    best = ranked[0]
    conf = round(min(0.9, 0.5 + scores[best]), 2)
    return Classification(best, conf, f"keyword rule '{best}'", matched, ranked[1:4],
                          "high" if best in ("offering_dilution", "merger_acquisition", "earnings", "guidance") else "medium")


def classify_filing(form: str, items: list[str]) -> Classification:
    for code in items:
        if code in ITEM_CATEGORY and ITEM_CATEGORY[code] != "sec_filing":
            cat = ITEM_CATEGORY[code]
            return Classification(cat, 0.95, f"SEC form {form}, item {code}", {cat: [f"item {code}"]},
                                  ["sec_filing"], "high" if code in ("1.03", "2.02", "3.01", "4.02", "5.02", "2.01") else "medium")
    base = form.split("/")[0]
    for prefix, cat in FORM_CATEGORY.items():
        if base.startswith(prefix):
            return Classification(cat, 0.95, f"SEC form {form}", {cat: [f"form {form}"]}, ["sec_filing"],
                                  "high" if cat == "offering_dilution" else "medium")
    return Classification("sec_filing", 0.95, f"SEC form {form}" + (f", items {', '.join(items)}" if items else ""),
                          {"sec_filing": [f"form {form}"]}, [], "low" if not items else "medium")


def classify_fed(categories: list[str], title: str) -> Classification:
    for c in categories:
        if c.lower() in FED_CATEGORY:
            cat, imp = FED_CATEGORY[c.lower()]
            if cat == "fed_event" and re.search(r"\bfomc statement|minutes\b", title, re.I):
                imp = "high"
            return Classification(cat, 0.95, f"Federal Reserve feed category '{c}'", {cat: [c]}, [], imp)
    return Classification("fed_event", 0.8, "Federal Reserve press release", {"fed_event": categories[:2]}, [], "low")


def classify_macro(source_name: str, title: str) -> Classification:
    imp = "high" if re.search(r"\b(cpi|consumer price|employment situation|payrolls|gdp|gross domestic|pce|personal income)\b",
                              title, re.I) else "medium"
    return Classification("macro_release", 0.95, f"{source_name} release", {"macro_release": [source_name]}, [], imp)
