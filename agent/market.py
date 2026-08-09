"""US equity, ETF and listed-option market data helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
import math
from typing import Any, Iterable, Mapping

from .alpaca_domain import (Asset, Bar, CalendarDay, MarketClock, OptionContract,
                            Quote)
from .alpaca_session import NEW_YORK, SessionPolicy, session_for
from .alpaca_provider import AlpacaError


def _bar_number(value: Any, key: str) -> float | None:
    if isinstance(value, Mapping):
        value = value.get(key)
    else:
        value = getattr(value, key, None)
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def atr_series(bars: Iterable[Any], period: int = 14) -> list[Decimal | None]:
    """Compute a close-confirmed Wilder ATR for each OHLCV bar.

    The value at index *i* uses true ranges through index *i* only (and the
    prior close at *i - 1*).  No future bar is consulted, so attaching the
    result to a production bar cannot introduce look-ahead.  Values before a
    complete ``period`` are ``None`` rather than a partial-window estimate.
    """
    period = int(period)
    if period <= 0:
        raise ValueError("ATR period must be positive")
    rows = list(bars or ())
    output: list[Decimal | None] = [None] * len(rows)
    true_ranges: list[float] = []
    previous_close: float | None = None
    atr: float | None = None
    for index, row in enumerate(rows):
        high = _bar_number(row, "high"); low = _bar_number(row, "low")
        close = _bar_number(row, "close")
        if high is None or low is None or close is None or high < low:
            previous_close = close
            true_ranges.append(float("nan"))
            continue
        tr = high - low if previous_close is None else max(
            high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(tr)
        if index + 1 >= period and all(math.isfinite(value) for value in true_ranges[index + 1 - period:index + 1]):
            if atr is None:
                atr = sum(true_ranges[index + 1 - period:index + 1]) / period
            else:
                atr = ((atr * (period - 1)) + tr) / period
            output[index] = Decimal(str(atr))
        previous_close = close
    return output


def attach_atr(bars: Iterable[Any], period: int = 14) -> list[Any]:
    """Return bars with a no-lookahead ``atr`` field attached."""
    rows = list(bars or ())
    values = atr_series(rows, period=period)
    output = []
    for row, atr in zip(rows, values):
        if isinstance(row, Bar):
            output.append(replace(row, atr=atr))
        elif isinstance(row, Mapping):
            item = dict(row)
            if atr is not None:
                item["atr"] = float(atr)
                item["atr_period"] = int(period)
            output.append(item)
        else:
            output.append(row)
    return output


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
    atr_period: int = 14
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
        rows = self.provider.bars(list(symbols), timeframe=timeframe, start=start, end=end)
        if not isinstance(rows, Mapping):
            return rows
        # ATR is attached at the market boundary so both live and replay
        # callers receive identical close-confirmed evidence.
        return {str(symbol).upper(): attach_atr(values, period=self.atr_period)
                for symbol, values in rows.items()}

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
