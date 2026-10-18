"""The one set of statuses every overlay result uses, with its colour, icon and severity.

Colours are never the only signal: every status also has an icon and a text label, so the
overlay reads the same for people with colour-vision deficiencies. Display labels can be
renamed in overlay.yaml (`labels:`) without changing what a status means.
"""
from __future__ import annotations

from dataclasses import dataclass

# colour families (background, text). Blue = information/watch, green = confirmed rule match,
# amber = near/caution, red = invalidated/limit breach/serious data problem, gray = unavailable,
# purple = unusual volatility or expansion.
BLUE, GREEN, AMBER, RED, GRAY, PURPLE = "blue", "green", "amber", "red", "gray", "purple"
COLORS = {BLUE: ("#dbeafe", "#1e3a8a"), GREEN: ("#d1fae5", "#065f46"), AMBER: ("#fef3c7", "#92400e"),
          RED: ("#fee2e2", "#991b1b"), GRAY: ("#e5e7eb", "#374151"), PURPLE: ("#ede9fe", "#5b21b6")}


@dataclass(frozen=True)
class StatusInfo:
    code: str
    label: str
    color: str
    icon: str
    severity: int      # 0 info .. 3 serious; used for sorting and the severity filter
    meaning: str


_ALL = [
    StatusInfo("CONFIRMED", "Confirmed", GREEN, "✅", 1,
               "Every required condition passes on the latest completed bar and the score, pass-count and "
               "required-condition rules of the confirmation section are met."),
    StatusInfo("NEAR_CONFIRMATION", "Near confirmation", AMBER, "🟡", 1,
               "Not confirmed, but the score reaches the near-match threshold and every miss is inside its "
               "configured tolerance."),
    StatusInfo("WATCH", "Watch", BLUE, "🔵", 0,
               "Some conditions pass; the score reaches the watch threshold but not the near-match one."),
    StatusInfo("NOT_PRESENT", "Not present", GRAY, "▫️", 0,
               "Too few conditions pass to be worth watching. Shown only in details and checklists."),
    StatusInfo("INVALIDATED", "Invalidated", RED, "⛔", 2,
               "The rule was confirmed within the freshness window and a required condition (or an explicit "
               "invalidation condition) has since failed."),
    StatusInfo("STALE", "Stale signal", GRAY, "⌛", 1,
               "Still confirmed, but it first confirmed more than stale_after_bars bars ago."),
    StatusInfo("OVEREXTENDED", "Overextended", PURPLE, "🟣", 1,
               "Still confirmed, but price has moved more than overextended_atr_multiple ATRs since the trigger "
               "bar or from its reference average."),
    StatusInfo("CONFLICTING", "Conflicting", AMBER, "⚖️", 1,
               "Bullish and bearish rules are confirmed on the same symbol and timeframe at once."),
    StatusInfo("LOW_SAMPLE_SIZE", "Low sample size", AMBER, "🔬", 1,
               "Historical evidence rests on fewer events than minimum_historical_samples."),
    StatusInfo("POOR_LIQUIDITY", "Poor liquidity", RED, "💧", 2,
               "The quote fails one or more liquidity checks (spread, open interest, volume, bid, freshness)."),
    StatusInfo("DOES_NOT_FIT_ACCOUNT", "Does not fit account", RED, "🚫", 2,
               "Capital, maximum loss, risk or assignment needs exceed the account profile."),
    StatusInfo("FITS_BP_EXCEEDS_RISK", "Fits buying power but exceeds risk limit", AMBER, "⚠️", 1,
               "Buying power covers it, but a preferred per-idea limit is exceeded."),
    StatusInfo("FITS_ACCOUNT", "Fits configured limits", BLUE, "🧾", 0,
               "Capital, maximum loss and risk are inside the account profile. This says nothing about setup "
               "quality, so it is blue, never green."),
    StatusInfo("INSUFFICIENT_DATA", "Insufficient data", GRAY, "❔", 1,
               "A value needed by the rule is missing or still warming up; nothing is assumed in its place."),
    StatusInfo("STALE_DATA", "Stale data", GRAY, "🕰️", 2,
               "The underlying bars or quotes are older than the freshness limits."),
    StatusInfo("DATA_ERROR", "Data error", RED, "🛑", 3,
               "A data-health check found a problem serious enough to block the calculation."),
    StatusInfo("VOLATILITY_EXPANSION", "Volatility expansion", PURPLE, "🌀", 1,
               "Volatility measures moved from compressed to expanding."),
    StatusInfo("REQUIRES_DATA_VALIDATION", "Requires data validation", AMBER, "🔎", 1,
               "A quote looks inconsistent with its neighbours. It is a data question, not an opportunity."),
    StatusInfo("INFO", "Information", BLUE, "ℹ️", 0, "Context with no rule verdict."),
]
STATUSES = {s.code: s for s in _ALL}
# Words the overlay must never show (checked by a test over every template and label).
BANNED_WORDS = ("guaranteed", "safe", "certain", "best trade", "winning trade", "free money")


def info(code: str) -> StatusInfo:
    return STATUSES[code]


def label(code: str, overrides: dict | None = None) -> str:
    return (overrides or {}).get(code, STATUSES[code].label)


def badge(code: str, overrides: dict | None = None) -> str:
    """Icon + label, e.g. '✅ Confirmed'. Plain text, safe in tables and tooltips."""
    s = STATUSES[code]
    return f"{s.icon} {label(code, overrides)}"


def css(code: str) -> str:
    bg, fg = COLORS[STATUSES[code].color]
    return f"background-color: {bg}; color: {fg}"


def chip_html(code: str, text: str | None = None, overrides: dict | None = None) -> str:
    bg, fg = COLORS[STATUSES[code].color]
    body = (text if text is not None else badge(code, overrides)).replace("$", "&#36;")  # no Markdown maths
    return (f"<span style='background:{bg};color:{fg};border-radius:6px;padding:2px 8px;margin:0 4px 4px 0;"
            f"display:inline-block;font-size:0.85rem;white-space:{'nowrap' if len(body) < 70 else 'normal'}'>{body}</span>")
