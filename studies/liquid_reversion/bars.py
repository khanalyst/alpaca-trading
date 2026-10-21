"""Bars, New York regular sessions and the two bar loaders.

A bar's ``ts`` is the aware UTC instant at which the bar starts. A bar is
usable only once ``ts + interval`` has passed; the simulator enforces that.
Both loaders treat their input as untrusted data: files are parsed with
``csv`` or ``json`` and validated, and nothing in them is ever executed.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
TIMEFRAME_SECONDS = {"1Min": 60, "5Min": 300}
PUBLIC_INTERVAL_SECONDS = {"1m": 60, "5m": 300}
ALPACA_CSV_COLUMNS = ("symbol", "timestamp", "open", "high", "low", "close",
                      "volume", "trade_count", "vwap")
SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9.]{0,9}")
_MONTH_FILE = re.compile(
    r"(?P<symbol>[A-Z][A-Z0-9.]{0,9})_(?P<timeframe>1Min|5Min)_"
    r"(?P<month>\d{4}-\d{2})(?P<partial>\.partial)?\.csv\.gz")


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar. ``ts`` is the aware UTC start; the bar is usable from ``end``."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval_seconds: int

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() != timedelta(0):
            raise ValueError(f"{self.symbol}: a bar start must be an aware UTC datetime")
        if (isinstance(self.interval_seconds, bool) or not isinstance(self.interval_seconds, int)
                or self.interval_seconds <= 0 or self.interval_seconds % 60):
            raise ValueError(f"{self.symbol}: interval must be a positive whole number of minutes")
        prices = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(price) and price > 0 for price in prices):
            raise ValueError(f"{self.symbol} {self.ts.isoformat()}: prices must be finite and positive")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError(f"{self.symbol} {self.ts.isoformat()}: inconsistent high and low")
        if not (math.isfinite(self.volume) and self.volume >= 0):
            raise ValueError(f"{self.symbol} {self.ts.isoformat()}: volume must be finite and non-negative")

    @property
    def end(self) -> datetime:
        """The instant the bar is complete, and so the earliest it may be used."""
        return self.ts + timedelta(seconds=self.interval_seconds)


@lru_cache(maxsize=65536)
def ny_instant(day: date, clock: time) -> datetime:
    """The UTC instant of a New York wall-clock time on ``day``."""
    return datetime.combine(day, clock, tzinfo=NEW_YORK).astimezone(timezone.utc)


def session_date(ts: datetime) -> date:
    """The New York calendar date of an instant."""
    return ts.astimezone(NEW_YORK).date()


def session_bars(bars: Iterable[Bar], *,
                 early_closes: Mapping[date, time] | None = None) -> dict[str, dict[date, list[Bar]]]:
    """Group regular-hours bars by symbol and New York session date.

    A bar is kept when 09:30 <= start < 16:00 New York, or before an earlier
    regular close listed in ``early_closes``. Sessions come back in date order
    with bars in time order. Duplicate bars, mixed intervals and bars that are
    not aligned to their interval from 09:30 are errors.
    """
    grouped: dict[str, dict[date, list[Bar]]] = {}
    intervals: dict[str, int] = {}
    for bar in bars:
        local = bar.ts.astimezone(NEW_YORK)
        day = local.date()
        close = early_closes.get(day, REGULAR_CLOSE) if early_closes else REGULAR_CLOSE
        if not REGULAR_OPEN <= local.time() < close:
            continue
        offset = local.hour * 60 + local.minute - (REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute)
        if local.second or local.microsecond or offset % (bar.interval_seconds // 60):
            raise ValueError(f"{bar.symbol} bar at {local.isoformat()} is not aligned to its interval")
        if intervals.setdefault(bar.symbol, bar.interval_seconds) != bar.interval_seconds:
            raise ValueError(f"{bar.symbol} mixes bar intervals")
        grouped.setdefault(bar.symbol, {}).setdefault(day, []).append(bar)
    out: dict[str, dict[date, list[Bar]]] = {}
    for symbol in sorted(grouped):
        days = grouped[symbol]
        for rows in days.values():
            rows.sort(key=_start)
            for previous, current in zip(rows, rows[1:]):
                if previous.ts == current.ts:
                    raise ValueError(f"duplicate {symbol} bar at {current.ts.isoformat()}")
        out[symbol] = {day: days[day] for day in sorted(days)}
    return out


def _start(bar: Bar) -> datetime:
    return bar.ts


def parse_utc(value: object) -> datetime:
    """Parse an ISO 8601 instant that carries an offset and return it in UTC."""
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"not a timestamp: {value!r:.60}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without an offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"not a number: {value!r:.60}")
    return float(value)


def _symbol(value: object) -> str:
    if not isinstance(value, str) or not SYMBOL_PATTERN.fullmatch(value):
        raise ValueError(f"not a ticker symbol: {value!r:.60}")
    return value


def load_alpaca_csv(path: str | Path, *, interval_seconds: int = 60) -> list[Bar]:
    """Load one gzip CSV written by the fetcher (all hours; filter with ``session_bars``)."""
    out: list[Bar] = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None or tuple(header) != ALPACA_CSV_COLUMNS:
            raise ValueError(f"{Path(path).name}: unexpected header {header!r:.200}")
        for line, row in enumerate(reader, start=2):
            if len(row) != len(ALPACA_CSV_COLUMNS):
                raise ValueError(f"{Path(path).name} line {line}: expected {len(ALPACA_CSV_COLUMNS)} fields")
            try:
                out.append(Bar(_symbol(row[0]), parse_utc(row[1]), float(row[2]), float(row[3]),
                               float(row[4]), float(row[5]), float(row[6]), interval_seconds))
            except ValueError as error:
                raise ValueError(f"{Path(path).name} line {line}: {error}") from None
    return out


def load_public_chart_json(path: str | Path, *, symbols: Iterable[str] | None = None) -> list[Bar]:
    """Load the review's public consolidated-tape bar file.

    The layout is ``{"interval": "1m" | "5m", "bars": {symbol: [row, ...]}}``
    with each row carrying an ISO ``timestamp`` (bar start, New York offset)
    and ``open``, ``high``, ``low``, ``close`` and ``volume``. Other keys and
    the precomputed ``session`` and ``minute`` fields are ignored: both are
    derived again from the timestamp.
    """
    with open(path, "rb") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("public bar file: top level must be an object")
    interval = PUBLIC_INTERVAL_SECONDS.get(data.get("interval"))  # type: ignore[arg-type]
    if interval is None:
        raise ValueError(f"public bar file: unsupported interval {data.get('interval')!r:.20}")
    raw = data.get("bars")
    if not isinstance(raw, dict):
        raise ValueError("public bar file: 'bars' must map symbols to lists")
    wanted = None if symbols is None else set(symbols)
    out: list[Bar] = []
    for name, rows in raw.items():
        symbol = _symbol(name)
        if wanted is not None and symbol not in wanted:
            continue
        if not isinstance(rows, list):
            raise ValueError(f"public bar file: rows for {symbol} must be a list")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"public bar file: {symbol} row {index} is not an object")
            if row.get("symbol", symbol) != symbol:
                raise ValueError(f"public bar file: {symbol} row {index} names another symbol")
            try:
                out.append(Bar(symbol, parse_utc(row.get("timestamp")), _number(row.get("open")),
                               _number(row.get("high")), _number(row.get("low")),
                               _number(row.get("close")), _number(row.get("volume")), interval))
            except ValueError as error:
                raise ValueError(f"public bar file: {symbol} row {index}: {error}") from None
    return out


def month_file_name(symbol: str, timeframe: str, month: str, *, partial: bool = False) -> str:
    """File name for one symbol-month; a partial month is never treated as final."""
    if timeframe not in TIMEFRAME_SECONDS or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("unsupported timeframe or month label")
    return f"{_symbol(symbol)}_{timeframe}_{month}{'.partial' if partial else ''}.csv.gz"


def alpaca_month_files(directory: str | Path, timeframe: str,
                       symbols: Iterable[str] | None = None) -> dict[str, list[Path]]:
    """Month files per symbol in month order; a final file wins over a partial one."""
    wanted = None if symbols is None else set(symbols)
    chosen: dict[tuple[str, str], tuple[bool, Path]] = {}
    for path in sorted(Path(directory).iterdir()):
        match = _MONTH_FILE.fullmatch(path.name)
        if match is None or match["timeframe"] != timeframe or not path.is_file():
            continue
        if wanted is not None and match["symbol"] not in wanted:
            continue
        key = (match["symbol"], match["month"])
        partial = match["partial"] is not None
        if key not in chosen or (chosen[key][0] and not partial):
            chosen[key] = (partial, path)
    out: dict[str, list[Path]] = {}
    for symbol, month in sorted(chosen):
        out.setdefault(symbol, []).append(chosen[(symbol, month)][1])
    return out


def iter_alpaca_sessions(paths: Sequence[Path], *, interval_seconds: int,
                         early_closes: Mapping[date, time] | None = None
                         ) -> Iterator[tuple[date, list[Bar]]]:
    """Stream one symbol's regular sessions in date order, one month file at a time."""
    symbol: str | None = None
    last: date | None = None
    for path in paths:
        grouped = session_bars(load_alpaca_csv(path, interval_seconds=interval_seconds),
                               early_closes=early_closes)
        for name, days in grouped.items():
            if symbol is not None and name != symbol:
                raise ValueError(f"{path.name}: mixes {symbol} and {name}")
            symbol = name
            for day, rows in days.items():
                if last is not None and day <= last:
                    raise ValueError(f"{path.name}: session {day} is out of order or repeated")
                last = day
                yield day, rows


def load_calendar(path: str | Path) -> tuple[dict[date, time], frozenset[date]]:
    """Early closes (before 16:00) and every trading day, from the fetcher's ``calendar.json``.

    The trading days let the runner prove the calendar covers the data: a
    session missing from it could be an unrecognised early close.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("calendar file: expected a list of trading days")
    early: dict[date, time] = {}
    days: set[date] = set()
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("calendar file: every entry must be an object")
        day = date.fromisoformat(str(entry.get("date")))
        close = time.fromisoformat(str(entry.get("close")))
        days.add(day)
        if close < REGULAR_CLOSE:
            early[day] = close
    return early, frozenset(days)


def load_early_closes(path: str | Path) -> dict[date, time]:
    """Regular closes before 16:00, read from a ``calendar.json`` written by the fetcher."""
    return load_calendar(path)[0]
