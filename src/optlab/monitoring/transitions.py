"""State machines that decide when a pass's result becomes an event. Pure functions over a state object
(a MonitorState row, or anything with the same attributes), so they are tested without a database.

Position status (`step_status`)
    * The first status a position gets is adopted at once; it produces an event unless it is HOLD.
    * A different status must be seen on `confirm_passes` passes in a row before it replaces the current
      one (one event per change). Escalation to STALE DATA is immediate: a position is never kept on a
      safe-looking status while its data is stale.
    * While the status stays EXIT CONDITION MET or ACTION REQUIRED, a reminder is due every
      `repeat_minutes` (0 = never).

Alert rules and one-off warnings (`step_alert`)
    * A condition fires when it has been true on `confirm_passes` passes in a row and the alert is armed.
    * It re-arms only after the condition has been false again (cleared) and at least `rearm_minutes`
      have passed since it last fired.
    * While it stays true it repeats every `repeat_minutes` (0 = never).
    * Unknown (None) leaves the state as it is.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .status import HOLD, STALE, URGENT


@dataclass
class StatusChange:
    kind: str                     # initial | change | reminder
    from_status: str | None
    to_status: str


def step_status(st, raw: str, now: datetime, confirm_passes: int, repeat_minutes: int) -> StatusChange | None:
    if st.status is None:
        st.status, st.since, st.candidate, st.candidate_count = raw, now, None, 0
        if raw == HOLD:
            return None
        st.last_event_at = now
        return StatusChange("initial", None, raw)
    if raw == st.status:
        st.candidate, st.candidate_count = None, 0
        if (raw in URGENT and repeat_minutes and st.last_event_at is not None
                and now - st.last_event_at >= timedelta(minutes=repeat_minutes)):
            st.last_event_at = now
            return StatusChange("reminder", raw, raw)
        return None
    st.candidate_count = (st.candidate_count or 0) + 1 if st.candidate == raw else 1
    st.candidate = raw
    if raw == STALE or st.candidate_count >= confirm_passes:
        prev = st.status
        st.status, st.since, st.candidate, st.candidate_count, st.last_event_at = raw, now, None, 0, now
        return StatusChange("change", prev, raw)
    return None


def step_alert(st, result: bool | None, now: datetime, confirm_passes: int, rearm_minutes: int,
               repeat_minutes: int) -> str | None:
    """Returns 'fire', 'repeat', 'clear' or None."""
    if result is None:
        return None
    if not result:
        st.candidate_count = 0
        if st.active:
            st.active, st.last_cleared_at = False, now
            return "clear"
        return None
    st.candidate_count = (st.candidate_count or 0) + 1
    if st.active:
        if repeat_minutes and st.last_event_at is not None and now - st.last_event_at >= timedelta(minutes=repeat_minutes):
            st.last_event_at = now
            return "repeat"
        return None
    if st.candidate_count < confirm_passes:
        return None
    if st.last_event_at is not None and rearm_minutes and now - st.last_event_at < timedelta(minutes=rearm_minutes):
        return None                       # cleared, but too soon after the last alert
    st.active, st.last_event_at = True, now
    return "fire"
