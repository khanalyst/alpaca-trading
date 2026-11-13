"""Regular-session one-minute matrices and daily bars for the session-edge study.

A :class:`Session` holds one New York regular session as five parallel tuples
of ``MINUTES`` (390) values, indexed by minutes from 09:30. A minute with no
bar carries the last observed close for diagnostics; leading missing minutes
remain NaN. The observation mask, rather than an imputed price, determines
whether a signal or execution can be evaluated. Sessions with fewer
than ``MIN_OBSERVED`` real bars are dropped, which removes half days and
broken feeds from both the study and the forward runner.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from studies.liquid_reversion.bars import (NEW_YORK, Bar, alpaca_month_files, iter_alpaca_sessions,
                                           load_calendar)

MINUTES = 390
OPEN_MINUTE_OF_DAY = 9 * 60 + 30
MIN_OBSERVED = 280
MAX_SESSION_GAP_DAYS = 5

MinuteRow = tuple[int, float, float, float, float, float]


@dataclass(frozen=True, slots=True)
class Session:
    """One regular session; index ``m`` is the bar that starts ``m`` minutes after 09:30."""

    day: date
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]
    volume: tuple[float, ...]
    observed: int
    observed_mask: tuple[bool, ...] = ()

    def has_observed(self, start: int, stop: int | None = None) -> bool:
        """Whether every minute in ``[start, stop)`` has a real bar.

        With no ``stop``, check one minute. Legacy constructors may omit the
        mask: only an explicitly complete session then proves availability.
        """
        end = start + 1 if stop is None else stop
        if not 0 <= start < end <= MINUTES:
            return False
        if not self.observed_mask:
            return self.observed == MINUTES
        return len(self.observed_mask) == MINUTES and all(self.observed_mask[start:end])


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One daily bar; prices are adjusted for dividends and splits."""

    day: date
    open: float
    high: float
    low: float
    close: float


def require_complete_sessions(sessions: Sequence[Session], *, label: str) -> None:
    """Fail closed before research can condition returns on future missing bars.

    Low-level simulators expose diagnostic behavior on partial prefixes. A
    historical statistical screen must not silently turn a missing future
    exit/path into a zero-return session or erase an existing position.
    """
    previous: date | None = None
    for session in sessions:
        if previous is not None and session.day <= previous:
            raise ValueError(f"{label}: sessions must be unique and chronological")
        previous = session.day
        if not session.has_observed(0, MINUTES):
            raise ValueError(f"{label}: incomplete session {session.day}; exact research replay needs observed bars")
        if not all(math.isfinite(value) and value > 0
                   for column in (session.open, session.high, session.low, session.close)
                   for value in column):
            raise ValueError(f"{label}: invalid observed prices on {session.day}")


def load_exchange_calendar(path: str | Path) -> tuple[date, ...]:
    """Read an explicitly supplied exchange schedule, never infer it from bars."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("exchange calendar must be a nonempty list of session dates")
    days = [date.fromisoformat(str(row.get("date")) if isinstance(row, Mapping) else str(row))
            for row in payload]
    if days != sorted(set(days)):
        raise ValueError("exchange calendar must be unique and chronological")
    return tuple(days)


def build_session(day: date, rows: Iterable[MinuteRow], *, min_observed: int = MIN_OBSERVED) -> Session | None:
    """Assemble one session from ``(minute, open, high, low, close, volume)`` rows.

    Rows outside ``0 <= minute < MINUTES`` are ignored and the first row wins
    for a repeated minute. Returns ``None`` when fewer than ``min_observed``
    minutes carry a valid bar.
    """
    seen: dict[int, MinuteRow] = {}
    for row in rows:
        minute = int(row[0])
        if not 0 <= minute < MINUTES or minute in seen:
            continue
        prices = row[1:5]
        if not all(math.isfinite(price) and price > 0 for price in prices):
            continue
        seen[minute] = row
    if len(seen) < max(1, min_observed):
        return None
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[float] = []
    observed_mask: list[bool] = []
    last = math.nan
    for minute in range(MINUTES):
        row = seen.get(minute)
        observed_mask.append(row is not None)
        if row is None:
            opens.append(last)
            highs.append(last)
            lows.append(last)
            closes.append(last)
            volumes.append(0.0)
            continue
        _, o, h, l, c, v = row
        opens.append(o)
        highs.append(max(h, o, c))
        lows.append(min(l, o, c))
        closes.append(c)
        volumes.append(v if math.isfinite(v) and v > 0 else 0.0)
        last = c
    return Session(day, tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(volumes),
                   len(seen), tuple(observed_mask))


def previous_close(sessions: Sequence[Session], index: int, *,
                   max_gap_days: int = MAX_SESSION_GAP_DAYS) -> float | None:
    """The prior session's last close, or ``None`` when that session is missing.

    A gap of more than ``max_gap_days`` calendar days means sessions were
    dropped in between, so the stored close is not yesterday's close.
    """
    if index <= 0:
        return None
    previous, current = sessions[index - 1], sessions[index]
    if (current.day - previous.day).days > max_gap_days:
        return None
    if not previous.has_observed(MINUTES - 1):
        return None
    return previous.close[-1]


def sessions_from_bars(grouped: Iterable[tuple[date, Sequence[Bar]]], *,
                       min_observed: int = MIN_OBSERVED) -> list[Session]:
    """Sessions from date-ordered one-minute :class:`Bar` rows (the Alpaca month files)."""
    out: list[Session] = []
    for day, bars in grouped:
        rows: list[MinuteRow] = []
        for bar in bars:
            if bar.interval_seconds != 60:
                raise ValueError("the session study needs one-minute bars")
            local = bar.ts.astimezone(NEW_YORK)
            rows.append((local.hour * 60 + local.minute - OPEN_MINUTE_OF_DAY,
                         bar.open, bar.high, bar.low, bar.close, bar.volume))
        session = build_session(day, rows, min_observed=min_observed)
        if session is not None:
            out.append(session)
    return out


def load_alpaca_sessions(directory: str | Path, symbol: str, *, calendar: str | Path | None = None,
                         min_observed: int = MIN_OBSERVED) -> list[Session]:
    """One symbol's sessions from the month files written by ``liquid_reversion.fetch_sip``."""
    files = alpaca_month_files(directory, "1Min", [symbol]).get(symbol, [])
    if not files:
        raise FileNotFoundError(f"no 1Min month files for {symbol} in {directory}")
    early = load_calendar(calendar)[0] if calendar else None
    return sessions_from_bars(iter_alpaca_sessions(files, interval_seconds=60, early_closes=early),
                              min_observed=min_observed)


def _archive_rows(text: io.TextIOBase) -> Iterator[tuple[date, MinuteRow]]:
    for line in text:
        parts = line.strip().split(";")
        if len(parts) < 5:
            continue
        stamp = parts[0]
        try:
            day = date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
            minute = int(stamp[9:11]) * 60 + int(stamp[11:13]) - OPEN_MINUTE_OF_DAY
            o, h, l, c = (float(value) for value in parts[1:5])
        except (ValueError, IndexError):
            continue
        if 0 <= minute < MINUTES:
            yield day, (minute, o, h, l, c, 0.0)


def load_index_minute_archives(directory: str | Path, pair: str, *,
                               min_observed: int = MIN_OBSERVED) -> list[Session]:
    """Sessions from public index one-minute archives (``<pair>_<YYYY>.zip`` or ``<pair>_<YYYYMM>.zip``).

    Each archive holds one ``;``-separated text file of
    ``YYYYMMDD HHMMSS;open;high;low;close;volume`` rows. The source labels its
    stamps "EST", but the opening and closing volatility bursts sit at 09:30
    and 15:59 in both winter and summer, so they are New York wall-clock times
    and are used as such. These are index CFD quotes with no traded volume:
    good for price rules on SPY or QQQ, diagnostic only, never an execution feed.
    """
    paths = sorted(Path(directory).glob(f"{pair}_*.zip"))
    if not paths:
        raise FileNotFoundError(f"no {pair}_*.zip archives in {directory}")
    by_day: dict[date, list[MinuteRow]] = {}
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"{path.name}: expected exactly one csv member")
            with archive.open(names[0]) as raw:
                for day, row in _archive_rows(io.TextIOWrapper(raw, encoding="ascii", errors="replace")):
                    by_day.setdefault(day, []).append(row)
    out: list[Session] = []
    for day in sorted(by_day):
        if day.weekday() >= 5:
            continue
        session = build_session(day, by_day[day], min_observed=min_observed)
        if session is not None:
            out.append(session)
    return out


def load_daily_chart(path: str | Path) -> list[DailyBar]:
    """Adjusted daily bars from a file written by ``fetch_public`` (``.json`` or ``.json.gz``).

    The file is ``{"t": [epoch seconds], "o": [...], "h": [...], "l": [...],
    "c": [...], "adj": [...]}``. Every price is scaled by ``adj / c`` so that
    returns across ex-dividend dates and splits are total returns.
    """
    source = Path(path)
    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, Mapping):
        raise ValueError(f"{source.name}: expected an object")
    stamps = data.get("t") or []
    columns = [data.get(key) or [] for key in ("o", "h", "l", "c")]
    adjusted = data.get("adj") or columns[3]
    out: list[DailyBar] = []
    seen: set[date] = set()
    for index, stamp in enumerate(stamps):
        try:
            o, h, l, c = (columns[k][index] for k in range(4))
            adj = adjusted[index]
        except IndexError:
            break
        if None in (o, h, l, c, adj) or not c or c <= 0 or o <= 0 or h < l:
            continue
        day = datetime.fromtimestamp(int(stamp), NEW_YORK).date()
        if day in seen:
            continue
        seen.add(day)
        factor = float(adj) / float(c)
        out.append(DailyBar(day, o * factor, h * factor, l * factor, c * factor))
    out.sort(key=lambda bar: bar.day)
    return out


def load_daily_closes(path: str | Path) -> dict[date, float]:
    """Unadjusted daily closes of a context series (an index such as the VIX), keyed by New York date.

    Reads the same file format as :func:`load_daily_chart`. An index has no
    dividends or splits, so nothing is adjusted.
    """
    source = Path(path)
    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, Mapping):
        raise ValueError(f"{source.name}: expected an object")
    out: dict[date, float] = {}
    for stamp, close in zip(data.get("t") or [], data.get("c") or []):
        if close is None or not close > 0:
            continue
        out[datetime.fromtimestamp(int(stamp), NEW_YORK).date()] = float(close)
    return out


def minute_of(clock: time) -> int:
    """Minutes from the 09:30 open to a New York wall-clock time."""
    return clock.hour * 60 + clock.minute - OPEN_MINUTE_OF_DAY


def clock_of(minute: int) -> time:
    """The New York wall-clock time ``minute`` minutes after the 09:30 open."""
    total = OPEN_MINUTE_OF_DAY + int(minute)
    return time(total // 60, total % 60)
