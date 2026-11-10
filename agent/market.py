"""US equity, ETF and listed-option market data helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
import math
from typing import Any, Iterable, Mapping

from .alpaca_domain import Asset, Bar, CalendarDay, MarketClock, OptionContract
from .alpaca_session import NEW_YORK, SessionPolicy, as_new_york, session_for
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


def wilder_atr_decimal_series(bars: Iterable[Any],
                              period: int = 14) -> list[Decimal | None]:
    """Compute a close-confirmed Wilder ATR for each OHLCV bar.

    The value at index *i* uses true ranges through index *i* only (and the
    prior close at *i - 1*).  No future bar is consulted, so attaching the
    result to a production bar cannot introduce look-ahead.  Values before a
    complete ``period`` are ``None`` rather than a partial-window estimate.

    This is the Wilder recursion of
    ``agent.contracts.ibr.wilder_atr_series`` (simple-mean seed over the
    first ``period`` true ranges, then ``(ATR * (period - 1) + TR) /
    period``, no session reset), computed in float and returned as
    ``Decimal(str(value))``, which converts back to the identical float.  It
    differs only at the input boundary: it also reads attribute objects such
    as :class:`Bar`, reads only the full ``high``/``low``/``close`` names,
    and keeps every row in place rather than skipping non-mappings.

    Caller: :func:`attach_atr`, used by ``MarketData.stock_bars`` to attach
    ``atr`` at the market boundary.  The IBR contract recomputes the value
    from the bars it is handed and overwrites it on every row where it has
    one (``agent.contracts.ibr._bars_with_atr``); the rule families never
    read it and use their own simple-mean definition
    (``agent.contracts.rule._atr``).
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


# Historical name kept for existing callers; it is the same function object.
atr_series = wilder_atr_decimal_series


def attach_atr(bars: Iterable[Any], period: int = 14) -> list[Any]:
    """Return bars with a no-lookahead ``atr`` field attached.

    The value is :func:`wilder_atr_decimal_series`.
    """
    rows = list(bars or ())
    values = wilder_atr_decimal_series(rows, period=period)
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


def _finite_aware_datetime(value: Any) -> datetime | None:
    """Return an aware, finite datetime or reject untrusted boundary data."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None or not math.isfinite(value.timestamp()):
            return None
    except (OverflowError, OSError, TypeError, ValueError):
        return None
    return value


def _calendar_session_is_valid(session: CalendarDay | None,
                               now: datetime) -> bool:
    """Require a complete current-day broker calendar boundary."""
    current = _finite_aware_datetime(as_new_york(now))
    opening = _finite_aware_datetime(getattr(session, "open", None))
    closing = _finite_aware_datetime(getattr(session, "close", None))
    if current is None or opening is None or closing is None or session is None:
        return False
    local_open = opening.astimezone(NEW_YORK)
    local_close = closing.astimezone(NEW_YORK)
    return bool(getattr(session, "date", None) == current.date() and
                local_open.date() == current.date() and
                local_close.date() == current.date() and
                local_open < local_close)


def _clock_reports_closed_after_session(clock: Any, now: datetime) -> bool:
    """Recognize a broker-confirmed closed session without inventing a close.

    After a session, the clock is closed and both upcoming boundaries move to
    a future New York date.  That is enough evidence to require flattening
    residual exposure, while a premarket clock whose ``next_open`` is today
    remains outside this degraded exit path.
    """
    if isinstance(clock, Mapping):
        timestamp = clock.get("timestamp")
        is_open = clock.get("is_open")
        next_open = clock.get("next_open")
        next_close = clock.get("next_close")
    else:
        timestamp = getattr(clock, "timestamp", None)
        is_open = getattr(clock, "is_open", None)
        next_open = getattr(clock, "next_open", None)
        next_close = getattr(clock, "next_close", None)
    current = _finite_aware_datetime(as_new_york(now))
    broker_timestamp = _finite_aware_datetime(timestamp)
    opening = _finite_aware_datetime(next_open)
    closing = _finite_aware_datetime(next_close)
    if (current is None or broker_timestamp is None or opening is None or
            closing is None or is_open is not False):
        return False
    local_timestamp = broker_timestamp.astimezone(NEW_YORK)
    local_open = opening.astimezone(NEW_YORK)
    local_close = closing.astimezone(NEW_YORK)
    return bool(local_timestamp.date() == current.date() and
                local_open.date() > current.date() and
                local_close.date() >= local_open.date() and
                local_close > local_open)


def _clock_boundary_is_valid(clock: Any, now: datetime) -> datetime | None:
    """Validate a broker clock close usable for degraded exits.

    ``next_close`` is only an exit authority while the broker says the market
    is open and the clock's timestamp and close are on the current New York
    date.  In particular, a next-session close on a holiday/weekend and a
    premarket clock must not be mistaken for today's regular-session close.
    """
    if isinstance(clock, Mapping):
        timestamp = clock.get("timestamp")
        is_open = clock.get("is_open")
        next_open = clock.get("next_open")
        next_close = clock.get("next_close")
    else:
        timestamp = getattr(clock, "timestamp", None)
        is_open = getattr(clock, "is_open", None)
        next_open = getattr(clock, "next_open", None)
        next_close = getattr(clock, "next_close", None)
    current = _finite_aware_datetime(as_new_york(now))
    broker_timestamp = _finite_aware_datetime(timestamp)
    close = _finite_aware_datetime(next_close)
    if (current is None or broker_timestamp is None or close is None or
            is_open is not True):
        return None
    local_timestamp = broker_timestamp.astimezone(NEW_YORK)
    local_close = close.astimezone(NEW_YORK)
    if (local_timestamp.date() != current.date() or
            local_close.date() != current.date() or
            local_close <= local_timestamp):
        return None
    if next_open is not None:
        opening = _finite_aware_datetime(next_open)
        if opening is None or opening.astimezone(NEW_YORK) > local_timestamp:
            return None
    return local_close


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
        """Return whether the broker-authorized exit cutoff has arrived.

        A current-day calendar row remains valid for exits after a refresh
        outage.  A stale row is never reused across a date rollover; when no
        valid row exists, the broker clock may provide a degraded boundary
        while it reports an open market and a same-day ``next_close``.  A
        closed clock whose next open/close are both on a future New York date
        is independently exit-due, because it proves today's session ended.
        No weekday or standard-close guess is made here.
        """
        current = now or datetime.now(tz=NEW_YORK)
        try:
            session = self.session(current)
        except Exception:  # noqa: BLE001
            session = None
        if _calendar_session_is_valid(session, current):
            return self.policy.should_force_flat(current, session)

        # Do not use a cached clock: degraded mode must obtain a fresh broker
        # boundary, otherwise a prior-day next_close could survive another
        # outage and suppress an emergency exit.
        try:
            clock = self.clock()
        except Exception:  # noqa: BLE001
            return False
        if _clock_reports_closed_after_session(clock, current):
            return True
        close = _clock_boundary_is_valid(clock, current)
        if close is None:
            return False
        local = as_new_york(current)
        cutoff = close - timedelta(minutes=self.policy.force_flat_minutes_before_close)
        return local >= cutoff

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
