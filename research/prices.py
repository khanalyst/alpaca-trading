"""A 1-minute OHLCV cache: the only network access in the research layer.

Outcome resolution needs price data fine enough to order two events inside a
bar - "did the stop or the target come first?" Coarse bars make that
ambiguous, and the ambiguity is not neutral: resolving ties in the target's
favour manufactures edge out of nothing.

The cache is keyed by ``(symbol, day)`` and immutable. Re-fetching a key that
already exists is a no-op, not a refresh. That matters for a reason that has
nothing to do with speed: exchanges revise recent candles, so a "refresh"
would silently mix revised data into a corpus whose whole claim is that it
reflects what was knowable at the time.

This module never touches ``runtime/``. It reads no journal and writes no
state; the only thing it owns is ``research/cache/prices.db``.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_CACHE = Path(__file__).resolve().parent / "cache" / "prices.db"
MINUTE_MS = 60_000
DAY_MS = 86_400_000


@dataclass(frozen=True)
class Bar:
    ts: int          # epoch ms, the bar's OPEN time
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def close_ts(self) -> int:
        return self.ts + MINUTE_MS


def day_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(
        int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bars ("
        "symbol TEXT, day TEXT, ts INTEGER, open REAL, high REAL, "
        "low REAL, close REAL, volume REAL, "
        "PRIMARY KEY (symbol, ts))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fetched ("
        "symbol TEXT, day TEXT, fetched_ts REAL, bars INTEGER, "
        "PRIMARY KEY (symbol, day))")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS bars_symbol_ts ON bars (symbol, ts)")
    return conn


class PriceCache:
    """Fetch once, serve offline forever after."""

    def __init__(self, path: str | Path = DEFAULT_CACHE, fetcher=None) -> None:
        self.path = Path(path)
        self.fetcher = fetcher
        self.network_calls = 0

    def bars(self, symbol: str, start_ms: int, end_ms: int) -> list[Bar]:
        """Bars with ``start_ms <= ts < end_ms``, ascending, from cache only."""
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM bars "
                "WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts ASC",
                (symbol, int(start_ms), int(end_ms))).fetchall()
        return [Bar(int(r[0]), *(float(v) for v in r[1:])) for r in rows]

    def has_day(self, symbol: str, day: str) -> bool:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT 1 FROM fetched WHERE symbol=? AND day=?",
                (symbol, day)).fetchone()
        return row is not None

    def days_covering(self, start_ms: int, end_ms: int) -> list[str]:
        out, cursor = [], int(start_ms) - (int(start_ms) % DAY_MS)
        while cursor <= int(end_ms):
            out.append(day_of(cursor))
            cursor += DAY_MS
        return out

    def store(self, symbol: str, day: str, bars: list) -> int:
        """Insert bars for one day. Existing rows win; the cache is immutable.

        ``INSERT OR IGNORE`` rather than ``OR REPLACE``, deliberately. An
        exchange that revised a candle must not be able to rewrite history
        that a previous analysis already used.
        """
        rows = []
        for bar in bars:
            ts, o, h, low, c, v = (
                bar[:6] if isinstance(bar, (list, tuple))
                else (bar.ts, bar.open, bar.high, bar.low, bar.close,
                      bar.volume))
            rows.append((symbol, day, int(ts), float(o), float(h),
                         float(low), float(c), float(v)))
        with _connect(self.path) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO bars "
                "(symbol, day, ts, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.execute(
                "INSERT OR REPLACE INTO fetched "
                "(symbol, day, fetched_ts, bars) VALUES (?,?,?,?)",
                (symbol, day, time.time(), len(rows)))
        return len(rows)

    def ensure(self, symbol: str, start_ms: int, end_ms: int) -> int:
        """Fetch any day in the range not already cached. Returns days fetched.

        Idempotent: a second call over the same range performs zero network
        calls, which the test suite asserts with a counting fetcher.
        """
        fetched = 0
        for day in self.days_covering(start_ms, end_ms):
            if self.has_day(symbol, day):
                continue
            if self.fetcher is None:
                raise RuntimeError(
                    f"{symbol} {day} is not cached and no fetcher was given. "
                    "Research runs offline by default; pass a fetcher "
                    "explicitly to allow network access.")
            day_start = int(datetime.strptime(day, "%Y-%m-%d")
                            .replace(tzinfo=timezone.utc).timestamp() * 1000)
            bars = self.fetcher(symbol, day_start, day_start + DAY_MS)
            self.network_calls += 1
            self.store(symbol, day, bars or [])
            fetched += 1
        return fetched


def ccxt_fetcher(exchange, rate_limit_seconds: float = 0.2):
    """A fetcher backed by ccxt, paginating a day of 1m candles.

    Kept out of ``PriceCache`` so the cache has no exchange dependency and
    every test can run without one.
    """
    def fetch(symbol: str, start_ms: int, end_ms: int) -> list:
        out, since = [], int(start_ms)
        while since < int(end_ms):
            batch = exchange.fetch_ohlcv(symbol, "1m", since=since, limit=1000)
            if not batch:
                break
            out.extend(row for row in batch if int(row[0]) < int(end_ms))
            advanced = int(batch[-1][0]) + MINUTE_MS
            if advanced <= since:
                break
            since = advanced
            time.sleep(rate_limit_seconds)
        return out
    return fetch
