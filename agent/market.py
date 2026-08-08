"""US equity, ETF and listed-option market data helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from .alpaca_domain import (Asset, Bar, CalendarDay, MarketClock, OptionContract,
                            Quote)
from .alpaca_session import NEW_YORK, SessionPolicy, as_new_york, local_clock, session_for
from .alpaca_provider import AlpacaError


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    quote: Quote | None = None
    bars: tuple[Bar, ...] = ()
    option_chain: Any = None
    observed_at: datetime | None = None

    @property
    def price(self) -> Decimal | None:
        return self.quote.mid if self.quote else (self.bars[-1].close if self.bars else None)


@dataclass
class MarketData:
    provider: Any
    policy: SessionPolicy = field(default_factory=SessionPolicy)
    _calendar: list[CalendarDay] = field(default_factory=list)
    _calendar_loaded: bool = False
    _calendar_error: str | None = None
    _clock_error: str | None = None

    def clock(self) -> MarketClock:
        try:
            result = self.provider.clock()
            self._clock_error = None
            return result
        except Exception as exc:  # noqa: BLE001
            self._clock_error = str(exc)
            raise AlpacaError(f"market clock unavailable: {exc}") from exc

    def refresh_calendar(self, start=None, end=None) -> list[CalendarDay]:
        try:
            rows = self.provider.calendar(start=start, end=end)
        except TypeError:
            try:
                rows = self.provider.calendar()
            except Exception as exc:  # noqa: BLE001
                self._calendar_loaded = False
                self._calendar_error = str(exc)
                raise AlpacaError(f"market calendar unavailable: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            self._calendar_loaded = False
            self._calendar_error = str(exc)
            raise AlpacaError(f"market calendar unavailable: {exc}") from exc
        self._calendar = list(rows)
        self._calendar_loaded = True
        self._calendar_error = None
        return list(self._calendar)

    def session(self, now: datetime | None = None) -> CalendarDay | None:
        current = now or datetime.now(tz=NEW_YORK)
        found = session_for(current, self._calendar)
        if found is not None:
            return found
        # No locally fabricated weekday sessions: calendar loading is an
        # explicit safety prerequisite, including for status-driven entries.
        return None

    def can_enter(self, now: datetime | None = None) -> bool:
        return bool(self._calendar_loaded and self._clock_error is None and
                    self.policy.entry_allowed(now or datetime.now(tz=NEW_YORK), self.session(now)))

    def can_exit(self, now: datetime | None = None) -> bool:
        return bool(self._calendar_loaded and self._clock_error is None and
                    self.policy.exit_allowed(now or datetime.now(tz=NEW_YORK), self.session(now)))

    def should_force_flat(self, now: datetime | None = None) -> bool:
        return bool(self._calendar_loaded and self.policy.should_force_flat(now or datetime.now(tz=NEW_YORK), self.session(now)))

    def assets(self, *, include_options: bool = False) -> list[Asset | OptionContract]:
        rows: list[Asset | OptionContract] = list(self.provider.assets())
        if include_options:
            rows.extend(self.provider.option_contracts())
        return [a for a in rows if a.tradable and a.status.lower() == "active"]

    def stock_bars(self, symbols: Iterable[str], timeframe="1Day", start=None, end=None):
        return self.provider.bars(list(symbols), timeframe=timeframe, start=start, end=end)

    def stock_quotes(self, symbols: Iterable[str], start=None, end=None):
        return self.provider.quotes(list(symbols), start=start, end=end)

    def option_chain(self, underlying_symbol: str, **kwargs):
        return self.provider.option_chain(underlying_symbol, **kwargs)

    def option_snapshots(self, underlying_symbol: str, **kwargs):
        return self.provider.option_snapshots(underlying_symbol, **kwargs)

    def snapshot(self, symbol: str, *, timeframe="1Day", start=None, end=None,
                 include_options=False) -> MarketSnapshot:
        quotes = self.stock_quotes([symbol], start=start, end=end).get(symbol, [])
        bars = self.stock_bars([symbol], timeframe=timeframe, start=start, end=end).get(symbol, [])
        chain = self.option_chain(symbol, start=start, end=end) if include_options else None
        return MarketSnapshot(symbol.upper(), quotes[-1] if quotes else None, tuple(bars), chain, datetime.now(tz=NEW_YORK))


def regular_session(now: datetime | None = None, calendar: Iterable[CalendarDay] = ()) -> bool:
    """Return whether *now* is inside the NYSE regular session."""
    current = as_new_york(now or datetime.now(tz=NEW_YORK))
    session = session_for(current, calendar)
    if session is None and current.weekday() < 5:
        return current.replace(hour=9, minute=30, second=0, microsecond=0) <= current < current.replace(hour=16, minute=0, second=0, microsecond=0)
    return bool(session and session.open <= current < session.close)
