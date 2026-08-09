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

from .alpaca_domain import CalendarDay

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
        if self.entries_regular_session_only is not True:
            raise ValueError("entries_regular_session_only must be true")
        if self.allow_exits_outside_session is not True:
            raise ValueError("allow_exits_outside_session must be true")
        if self.force_flat_minutes_before_close < 1:
            raise ValueError("force_flat_minutes_before_close must be >= 1")
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


def trading_env_guard(*, paper: bool, allow_live: bool) -> None:
    """Require an environment declaration consistent with endpoint scope."""
    truthy = {"1", "true", "yes", "on"}
    raw = os.getenv("ALPACA_PAPER")
    if paper:
        if allow_live:
            raise ValueError("paper mode requires allow_live=false")
        if raw is not None and raw.strip().lower() not in truthy:
            raise ValueError("paper mode requires ALPACA_PAPER=true when set")
        return
    if not allow_live:
        raise ValueError("live mode requires allow_live=true")
    if os.getenv("ALPACA_LIVE_ENABLE", "").strip().lower() not in truthy:
        raise ValueError("live mode requires ALPACA_LIVE_ENABLE=true")
    if raw is not None and raw.strip().lower() in truthy:
        raise ValueError("live mode conflicts with ALPACA_PAPER=true")


def paper_env_guard() -> None:
    """Backward-compatible paper-mode environment guard."""
    trading_env_guard(paper=True, allow_live=False)


def normalize_calendar_day(value, *, timezone: ZoneInfo = NEW_YORK) -> CalendarDay:
    """Normalize an Alpaca calendar record or mapping to a local day."""
    if isinstance(value, CalendarDay):
        return value
    data = value if isinstance(value, dict) else vars(value)
    raw_date = data.get("date")
    day = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date)[:10])

    def parse(name: str) -> datetime:
        raw = data.get(name)
        if raw in {None, ""}:
            raise ValueError(f"calendar {name} is required")
        if isinstance(raw, datetime):
            dt = raw
            return dt.astimezone(timezone) if dt.tzinfo else dt.replace(tzinfo=timezone)
        text = str(raw)
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.astimezone(timezone) if dt.tzinfo else dt.replace(tzinfo=timezone)
        return datetime.combine(day, time.fromisoformat(text), timezone)

    return CalendarDay(day, parse("open"), parse("close"))


def session_for(now: datetime, calendar: Iterable[CalendarDay]) -> CalendarDay | None:
    local = as_new_york(now)
    for item in calendar:
        day = normalize_calendar_day(item)
        if day.date == local.date():
            return day
    return None
