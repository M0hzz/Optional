"""When the monitor runs: the configured weekly window, minus US equity market holidays, ending early on
early-close days. Times are wall-clock in the schedule's time zone (zoneinfo handles daylight saving).

The holiday list covers NYSE full closures and 1 p.m. early closes published for 2026 and 2027. For later
years, or if the exchange changes a date, the schedule simply runs on that day (prices then stop moving
and positions show their last data); `skip_market_holidays: false` turns the list off.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .settings import DAYS, ScheduleSettings

HOLIDAYS = {
    date(2026, 1, 1): "New Year's Day", date(2026, 1, 19): "Martin Luther King Jr. Day",
    date(2026, 2, 16): "Washington's Birthday", date(2026, 4, 3): "Good Friday", date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth", date(2026, 7, 3): "Independence Day (observed)", date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day", date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1): "New Year's Day", date(2027, 1, 18): "Martin Luther King Jr. Day",
    date(2027, 2, 15): "Washington's Birthday", date(2027, 3, 26): "Good Friday", date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth (observed)", date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day", date(2027, 11, 25): "Thanksgiving Day", date(2027, 12, 24): "Christmas (observed)",
}
EARLY_CLOSES = {date(2026, 11, 27): time(13, 0), date(2026, 12, 24): time(13, 0), date(2027, 11, 26): time(13, 0)}
MARKET_TZ = ZoneInfo("America/New_York")


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class Window:
    def __init__(self, sch: ScheduleSettings):
        self.sch, self.tz = sch, ZoneInfo(sch.timezone)
        self.days = {DAYS.index(d) for d in sch.days}
        self.start, self.end = _t(sch.start), _t(sch.end)

    def closure(self, d: date) -> str | None:
        """Why the monitor doesn't run on day `d`, or None if it does."""
        if d.weekday() not in self.days:
            return "not a scheduled day"
        if self.sch.skip_market_holidays and d in HOLIDAYS:
            return f"market holiday ({HOLIDAYS[d]})"
        return None

    def end_on(self, d: date) -> time:
        """Scheduled end, cut to the market's early close (converted to the schedule's zone)."""
        if self.sch.skip_market_holidays and d in EARLY_CLOSES:
            close = datetime.combine(d, EARLY_CLOSES[d], MARKET_TZ).astimezone(self.tz).time()
            return min(self.end, close)
        return self.end

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        d = local.date()
        return self.closure(d) is None and self.start <= local.time() < self.end_on(d)

    def next_open(self, now: datetime) -> datetime | None:
        """`now` if inside the window, else the next start (None if nothing in the next 3 weeks)."""
        if self.is_open(now):
            return now
        local = now.astimezone(self.tz)
        for i in range(22):
            d = local.date() + timedelta(days=i)
            start = datetime.combine(d, self.start, self.tz)
            if self.closure(d) is None and start > local and self.start < self.end_on(d):
                return start
        return None

    def next_tick(self, now: datetime) -> datetime:
        """Next clock-aligned pass time (every 60 s -> the top of each minute)."""
        every = self.sch.every_seconds
        epoch = now.timestamp()
        return datetime.fromtimestamp(epoch - epoch % every + every, now.tzinfo)

    def describe(self) -> str:
        return (f"{self.sch.start}–{self.sch.end} {self.sch.timezone}, {'/'.join(self.sch.days)}, every "
                f"{self.sch.every_seconds} s" + (", market holidays skipped" if self.sch.skip_market_holidays else ""))

    def why_closed(self, now: datetime) -> str | None:
        if self.is_open(now):
            return None
        local = now.astimezone(self.tz)
        c = self.closure(local.date())
        if c:
            return f"Today is {c}."
        if local.time() < self.start:
            return f"Before the monitoring window ({self.sch.start})."
        return f"After the monitoring window ({self.end_on(local.date()):%H:%M})."
