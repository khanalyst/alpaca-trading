"""NYSE session and market-clock policy plus the lazy Alpaca session boundary.

Alpaca's clock endpoint is authoritative while connected; the calendar
endpoint supplies holidays and early closes.  This module also provides a
deterministic local policy for tests and for startup before credentials exist,
along with the authenticated client session and its provider errors.  SDK
clients remain lazy so importing policy/configuration code never constructs
network clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import os
from typing import Any, Iterable
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


class AlpacaError(RuntimeError):
    """Base provider failure."""


class CredentialsError(AlpacaError):
    """Authenticated operation requested without usable credentials."""


class PaperModeError(AlpacaError):
    """The configured endpoint scope failed its explicit safety guard."""


class AlpacaSession:
    """Lazy construction and endpoint guard for alpaca-py clients."""

    def __init__(self, *, api_key: str | None = None, secret_key: str | None = None,
                 paper: bool = True, allow_live: bool = False,
                 trading_client: Any = None, stock_data_client: Any = None,
                 option_data_client: Any = None) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("ALPACA_API_KEY")
        self.secret_key = secret_key if secret_key is not None else os.getenv("ALPACA_SECRET_KEY")
        if not isinstance(paper, bool) or not isinstance(allow_live, bool):
            raise ValueError("paper and allow_live must be booleans")
        try:
            trading_env_guard(paper=paper, allow_live=allow_live)
        except ValueError as exc:
            raise PaperModeError(str(exc)) from exc
        self.paper = paper
        self._trading = trading_client
        self._stock_data = stock_data_client
        self._option_data = option_data_client

    def require_credentials(self) -> None:
        if not self.api_key or not self.secret_key:
            raise CredentialsError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for authenticated actions")

    @classmethod
    def from_env(cls, *, paper: bool = True, allow_live: bool = False, **kwargs):
        return cls(api_key=os.getenv("ALPACA_API_KEY"), secret_key=os.getenv("ALPACA_SECRET_KEY"), paper=paper, allow_live=allow_live, **kwargs)

    @property
    def endpoint(self) -> str:
        return "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"

    @property
    def trading(self):
        if self._trading is None:
            self.require_credentials()
            try:
                from alpaca.trading.client import TradingClient
            except ImportError as exc:
                raise AlpacaError("alpaca-py is not installed; install it for broker actions") from exc
            self._trading = TradingClient(self.api_key, self.secret_key, paper=self.paper)
        return self._trading

    @property
    def stock_data(self):
        if self._stock_data is None:
            self.require_credentials()
            try:
                from alpaca.data.historical import StockHistoricalDataClient
            except ImportError as exc:
                raise AlpacaError("alpaca-py is not installed; install it for market data") from exc
            self._stock_data = StockHistoricalDataClient(self.api_key, self.secret_key)
        return self._stock_data

    @property
    def option_data(self):
        if self._option_data is None:
            self.require_credentials()
            try:
                from alpaca.data.historical import OptionHistoricalDataClient
            except ImportError as exc:
                raise AlpacaError("alpaca-py is not installed; install it for option data") from exc
            self._option_data = OptionHistoricalDataClient(self.api_key, self.secret_key)
        return self._option_data


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
