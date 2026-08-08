"""NYSE session and market-clock policy.

Alpaca's clock endpoint is authoritative while connected; the calendar
endpoint supplies holidays and early closes.  This module also provides a
deterministic local policy for tests and for startup before credentials exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import os
from typing import Iterable
from zoneinfo import ZoneInfo

from .alpaca_domain import CalendarDay, MarketClock

NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def as_new_york(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=NEW_YORK)
    return value.astimezone(NEW_YORK)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class SessionPolicy:
    """Trading-window rules independent of broker implementation."""

    timezone: str = "America/New_York"
    entries_regular_session_only: bool = True
    allow_exits_outside_session: bool = True
    force_flat_minutes_before_close: int = 10
    reject_new_entries_minutes_before_close: int = 5

    def __post_init__(self) -> None:
        if self.timezone != "America/New_York":
            raise ValueError("only America/New_York is supported")
        if self.force_flat_minutes_before_close < 0:
            raise ValueError("force_flat_minutes_before_close must be >= 0")
        if self.reject_new_entries_minutes_before_close < 0:
            raise ValueError("reject_new_entries_minutes_before_close must be >= 0")

    def entry_allowed(self, now: datetime, session: CalendarDay | None) -> bool:
        if not self.entries_regular_session_only:
            return True
        if session is None:
            return False
        local = as_new_york(now)
        cutoff = session.close - timedelta(minutes=self.reject_new_entries_minutes_before_close)
        return session.open <= local < cutoff

    def exit_allowed(self, now: datetime, session: CalendarDay | None) -> bool:
        if self.allow_exits_outside_session:
            return True
        return self.entry_allowed(now, session)

    def should_force_flat(self, now: datetime, session: CalendarDay | None) -> bool:
        if session is None:
            return False
        local = as_new_york(now)
        return local >= session.close - timedelta(minutes=self.force_flat_minutes_before_close)


def paper_env_guard() -> None:
    """Reject an explicit live environment declaration.

    The runtime is intentionally paper-only until a separately reviewed live
    implementation exists.  A false value must not be silently overridden by
    a paper config or an injected client.
    """
    raw = os.getenv("ALPACA_PAPER")
    if raw is None:
        return
    normalized = raw.strip().lower()
    if normalized not in {"1", "true", "yes", "on"}:
        raise ValueError("ALPACA_PAPER must be true; live Alpaca endpoints are disabled")


def normalize_calendar_day(value, *, timezone: ZoneInfo = NEW_YORK) -> CalendarDay:
    """Normalize an Alpaca calendar record or mapping to a local day."""
    if isinstance(value, CalendarDay):
        return value
    data = value if isinstance(value, dict) else vars(value)
    raw_date = data.get("date")
    day = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date)[:10])

    def parse(name: str, default: time) -> datetime:
        raw = data.get(name)
        if isinstance(raw, datetime):
            dt = raw
            return dt.astimezone(timezone) if dt.tzinfo else dt.replace(tzinfo=timezone)
        if raw:
            text = str(raw)
            if "T" in text:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return dt.astimezone(timezone) if dt.tzinfo else dt.replace(tzinfo=timezone)
            return datetime.combine(day, time.fromisoformat(text), timezone)
        return datetime.combine(day, default, timezone)

    return CalendarDay(day, parse("open", time(9, 30)), parse("close", time(16)))


def session_for(now: datetime, calendar: Iterable[CalendarDay]) -> CalendarDay | None:
    local = as_new_york(now)
    for item in calendar:
        day = normalize_calendar_day(item)
        if day.date == local.date():
            return day
    return None


def local_clock(now: datetime | None = None, calendar: Iterable[CalendarDay] = ()) -> MarketClock:
    """Create a deterministic clock from local calendar entries.

    An empty calendar still reports regular NYSE weekday hours, useful for
    status output without credentials.  Real deployments should replace it
    with the provider's calendar response before permitting entries.
    """
    current = as_new_york(now or datetime.now(tz=UTC))
    days = [normalize_calendar_day(x) for x in calendar]
    today = session_for(current, days)
    if today is None and current.weekday() < 5:
        today = CalendarDay(current.date(), datetime.combine(current.date(), time(9, 30), NEW_YORK), datetime.combine(current.date(), time(16), NEW_YORK))
    if today and today.open <= current < today.close:
        return MarketClock(current, True, None, today.close)
    for offset in range(0, 15):
        day = current.date() + timedelta(days=offset)
        candidate = next((x for x in days if x.date == day), None)
        if candidate is None and day.weekday() < 5:
            candidate = CalendarDay(day, datetime.combine(day, time(9, 30), NEW_YORK), datetime.combine(day, time(16), NEW_YORK))
        if candidate and current < candidate.open:
            return MarketClock(current, False, candidate.open, candidate.close)
    return MarketClock(current, False)
