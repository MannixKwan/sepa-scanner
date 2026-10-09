"""NASDAQ session clock on the official XNAS calendar (holidays and early closes)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import pandas as pd

ET = ZoneInfo("America/New_York")


class Market:
    def __init__(self):
        self.cal = xc.get_calendar("XNAS")

    def session(self, day) -> tuple[datetime, datetime] | None:
        """(open, close) in UTC for an ET calendar date, or None if the market is closed."""
        try:
            if not self.cal.is_session(day):
                return None
            return (self.cal.session_open(day).to_pydatetime(), self.cal.session_close(day).to_pydatetime())
        except Exception:  # outside the calendar's range: plain weekday 9:30-16:00 ET
            if pd.Timestamp(day).weekday() >= 5:
                return None
            o = datetime.combine(pd.Timestamp(day).date(), datetime.min.time(), ET).replace(hour=9, minute=30)
            return o.astimezone(timezone.utc), (o + timedelta(hours=6, minutes=30)).astimezone(timezone.utc)

    def last_completed(self, now: datetime, delay: timedelta) -> str | None:
        """ET date of the most recent session whose close + delay has passed."""
        for back in range(0, 10):
            day = (now.astimezone(ET) - timedelta(days=back)).date()
            s = self.session(day.isoformat())
            if s and s[1] + delay <= now:
                return day.isoformat()
        return None

    def progress(self, now: datetime | None = None) -> tuple[float, float] | None:
        """(minutes since open, session length in minutes) if the market is open now, else None."""
        now = now or datetime.now(timezone.utc)
        s = self.session(now.astimezone(ET).date().isoformat())
        if not s or not (s[0] <= now <= s[1]):
            return None
        return (now - s[0]).total_seconds() / 60, (s[1] - s[0]).total_seconds() / 60
