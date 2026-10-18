"""Historical evidence for overlay rules (filled out in phase 4)."""
from __future__ import annotations

import pandas as pd

OPTIONS_LIMITATION = ("Technical signal available. Historical options affordability cannot be verified because "
                      "timestamped option quotes are unavailable.")


def options_history_status(signal_dates, snapshot_dates) -> tuple[str, str]:
    """Historical option affordability can only use a chain stored on the signal's own date; the current chain is
    never applied to past signals."""
    snaps = {pd.Timestamp(d).date() for d in snapshot_dates or []}
    hits = [d for d in signal_dates if pd.Timestamp(d).date() in snaps]
    if not hits:
        return "INSUFFICIENT_DATA", OPTIONS_LIMITATION
    return "INFO", (f"Stored option chains exist for {len(hits)} of {len(signal_dates)} signal dates; only those "
                    "dates can show historical affordability.")
