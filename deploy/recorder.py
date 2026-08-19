#!/usr/bin/env python3
"""Small Alpaca paper-data recorder used by the optional Compose lane.

The recorder writes normalized, append-only CSV rows for configured US equity
symbols. It has no order methods and never mutates trading state. A failed
authenticated read exits non-zero so the service health check cannot report a
fresh but empty dataset.

Storage is partitioned by New York session date under ``sessions/`` next to the
nominal dataset path, with a durable sidecar index holding the watermark, the
per-symbol last bar and coverage evidence, a time-bounded dedup window and the
option contracts held open for continued sampling. A cycle therefore costs
O(new rows) instead of rescanning the corpus. The index is a cache with a
corruption check: partition sizes are verified on load and a mismatch rebuilds
it from the partitions.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:  # minimal health/recovery shell
    def load_dotenv(path, override=False):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if override or key not in os.environ:
                os.environ[key.strip()] = value.strip().strip('"\'')

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.alpaca_provider import AlpacaProvider  # noqa: E402
from agent.alpaca_sdk import _canonical_feed  # noqa: E402
from agent.alpaca_session import (  # noqa: E402
    AlpacaError,
    normalize_calendar_day,
)
from agent.instruments import (  # noqa: E402
    validate_equity_symbol,
    validate_option_symbol,
)

from deploy.recorder_market import (  # noqa: E402
    FIELDS,
    MAX_OPTION_LIMIT,
    _call_market_data,
    _call_options,
    _call_quotes,
    _event_key,
    _feed,
    _iso,
    _number,
    _option_rank,
    _option_right,
    _option_rows,
    _options_feed,
    _point_in_time,
    _rows,
    _timeframe,
    _timestamp,
    _underlying_price,
    _value,
)


def _validate_dataset_row(row: dict) -> tuple[str, str, datetime]:
    event = str(row.get("event_type") or "").strip().lower()
    timestamp = _timestamp(row.get("timestamp") or row.get("as_of"))
    if timestamp is None:
        raise RuntimeError("recorder dataset row has an invalid timestamp")
    try:
        if event in {"bar", "bar_1m", "quote"}:
            symbol = validate_equity_symbol(row.get("symbol"))
        elif event in {"option", "option_snapshot"}:
            underlying = validate_equity_symbol(row.get("underlying"))
            symbol = validate_option_symbol(
                row.get("contract") or row.get("symbol"),
                underlying=underlying, expiration=row.get("expiration"),
                strike=row.get("strike"))
        else:
            raise ValueError(f"unsupported recorder event_type {event!r}")
    except ValueError as exc:
        raise RuntimeError(f"invalid recorder dataset row: {exc}") from exc
    return event, symbol, timestamp


NEW_YORK = ZoneInfo("America/New_York")
INDEX_NAME = ".recorder-index.json"
INDEX_SCHEMA = "recorder-index.v1"
PARTITION_DIR = "sessions"
PARTITION_SOURCE_SCHEMA = "recorder-partition-source.v1"
# The recorder only ever asks the provider for ``watermark - 1 minute`` onwards,
# so a row it can legally receive is at most one minute older than the
# watermark. The dedup window is fifteen minutes: an order of magnitude of
# headroom over that overlap, and small enough that the index stays tiny. A row
# older than the window cannot be checked against durable keys, so it is a hard
# failure rather than a silent append -- replaying an old key is impossible, not
# merely unlikely.
DEDUP_HORIZON = timedelta(minutes=15)
DEFAULT_FETCH_WINDOW_MINUTES = 15
DEFAULT_BAR_GAP_MINUTES = 5
MAX_ERROR_BACKOFF_SECONDS = 15 * 60
# Calendar metadata is an audit boundary, not a deduplication cache.  Keep the
# name for callers that imported the old constant, but do not prune calendar
# entries by age.
SESSION_CALENDAR_RETENTION_DAYS = 90


def _canonical_data_feed(value: object) -> str:
    try:
        return _canonical_feed(value)
    except ValueError as exc:
        raise RuntimeError(
            f"unsupported recorder equity data feed {value!r}") from exc


def _session_date(value: datetime) -> date:
    return value.astimezone(NEW_YORK).date()


def _corpus_root(output: Path) -> Path:
    return output.parent


def _partition_path(output: Path, day: date) -> Path:
    return _corpus_root(output) / PARTITION_DIR / f"market-{day.isoformat()}.csv"


def _partition_source_path(output: Path, day: date) -> Path:
    """Durable provenance marker written before a backfill partition."""
    partition = _partition_path(output, day)
    return partition.with_name(partition.name + ".source.json")


def _save_partition_source(output: Path, day: date, source_mode: str) -> None:
    """Atomically persist partition provenance independently of the index.

    The recorder index is rebuilt after a crash.  Keeping this tiny marker
    beside the partition prevents that rebuild from silently relabelling a
    historical backfill as forward-observed evidence.
    """
    if source_mode != "historical_backfill":
        raise RuntimeError("unsupported recorder partition source mode")
    path = _partition_source_path(output, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"schema": PARTITION_SOURCE_SCHEMA,
                   "partition": _partition_path(output, day).name,
                   "source_mode": source_mode}, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _partition_sources_from_markers(output: Path) -> dict[str, dict[str, str]]:
    """Load provenance markers for partitions that actually exist."""
    directory = _corpus_root(output) / PARTITION_DIR
    if not directory.is_dir():
        return {}
    existing = {path.name for path in corpus_partitions(output)}
    result: dict[str, dict[str, str]] = {}
    for path in sorted(directory.glob("market-*.csv.source.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid recorder partition source marker {path}") from exc
        partition = payload.get("partition") if isinstance(payload, dict) else None
        source_mode = payload.get("source_mode") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or
                payload.get("schema") != PARTITION_SOURCE_SCHEMA or
                not isinstance(partition, str) or partition not in existing or
                path.name != partition + ".source.json" or
                source_mode != "historical_backfill"):
            raise RuntimeError(f"invalid recorder partition source marker {path}")
        result[partition] = {"source_mode": source_mode}
    return result


def corpus_partitions(output: Path) -> list[Path]:
    """Return the session partitions of a corpus, oldest session first."""
    directory = _corpus_root(output) / PARTITION_DIR
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("market-*.csv") if path.is_file())


def iter_corpus_rows(output: Path):
    """Stream every recorded row: legacy flat file first, then partitions."""
    sources = ([output] if output.exists() and output.stat().st_size else [])
    sources.extend(corpus_partitions(output))
    for source in sources:
        try:
            with source.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fields = set(reader.fieldnames or ())
                required = {"event_key", "event_type", "symbol", "timestamp"}
                if not required.issubset(fields):
                    raise RuntimeError(
                        f"recorder dataset has invalid header; missing {sorted(required - fields)}")
                for row in reader:
                    if None in row:
                        raise RuntimeError("recorder dataset contains a malformed CSV row")
                    yield row
        except (OSError, csv.Error) as exc:
            raise RuntimeError(f"cannot read recorder dataset {source}: {exc}") from exc


def _validated_corpus_rows(output: Path):
    """Yield validated corpus rows without retaining the corpus in memory."""
    for row in iter_corpus_rows(output):
        key = str(row.get("event_key") or "").strip()
        if not key:
            raise RuntimeError("recorder dataset row has no event_key")
        event, symbol, parsed = _validate_dataset_row(row)
        yield row, key, event, symbol, parsed


def _scan_corpus(output: Path) -> dict:
    """Rebuild live state with memory bounded by the deduplication window.

    The live recorder needs the watermark, latest bars, and recent event keys;
    it does not need an in-memory set of every historical event key.  A first
    pass validates the corpus and finds the watermark.  A second pass retains
    only keys inside ``DEDUP_HORIZON``.  Exact historical duplicate detection
    is provided by :func:`audit_corpus`, which is intentionally an explicit
    offline operation.
    """
    watermark: datetime | None = None
    latest_bars: dict[str, str] = {}
    data_feeds: set[str] = set()
    for row, _key, event, symbol, parsed in _validated_corpus_rows(output):
        if watermark is None or parsed > watermark:
            watermark = parsed
        if event in {"bar", "bar_1m", "quote"}:
            feed = str(row.get("feed") or "").strip().lower()
            if feed:
                data_feeds.add(_canonical_data_feed(feed))
        if event in {"bar", "bar_1m"}:
            previous = latest_bars.get(symbol)
            if previous is None or parsed.isoformat() > previous:
                latest_bars[symbol] = parsed.isoformat()

    if len(data_feeds) > 1:
        raise RuntimeError(
            "recorder corpus mixes equity data feeds: " +
            ", ".join(sorted(data_feeds)))

    recent_keys: dict[str, str] = {}
    if watermark is not None:
        floor = watermark - DEDUP_HORIZON
        for _row, key, _event, _symbol, parsed in _validated_corpus_rows(output):
            if parsed < floor:
                continue
            if key in recent_keys:
                raise RuntimeError(f"recorder dataset repeats recent event_key {key}")
            recent_keys[key] = parsed.isoformat()

    index = {"schema": INDEX_SCHEMA,
             "watermark": watermark.isoformat() if watermark else None,
             "latest_bars": latest_bars, "recent_keys": recent_keys,
             "option_pins": {}, "bar_coverage": {},
             "session_calendar": {},
             "partition_sources": _partition_sources_from_markers(output),
             "data_feed": next(iter(data_feeds), None),
             "partitions": _partition_sizes(output)}
    return _prune_index(index)


def audit_corpus(output: Path) -> dict:
    """Validate every row and detect global duplicate keys on disk.

    SQLite provides an exact uniqueness constraint without allocating a Python
    set proportional to the corpus.  This is deliberately separate from live
    recovery: a full historical audit can be expensive, while normal recorder
    startup must remain bounded by the recent deduplication window.
    """
    root = _corpus_root(output)
    root.mkdir(parents=True, exist_ok=True)
    rows = 0
    watermark: datetime | None = None
    with tempfile.TemporaryDirectory(prefix=".recorder-audit-", dir=str(root)) as directory:
        database = Path(directory) / "keys.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            with db:
                db.execute("PRAGMA journal_mode=OFF")
                db.execute("PRAGMA synchronous=OFF")
                db.execute("PRAGMA temp_store=FILE")
                db.execute("CREATE TABLE event_keys (event_key TEXT PRIMARY KEY)")
                for _row, key, _event, _symbol, parsed in _validated_corpus_rows(output):
                    try:
                        db.execute("INSERT INTO event_keys(event_key) VALUES (?)", (key,))
                    except sqlite3.IntegrityError as exc:
                        raise RuntimeError(
                            f"recorder dataset repeats event_key {key}") from exc
                    rows += 1
                    if watermark is None or parsed > watermark:
                        watermark = parsed
                    if rows % 10_000 == 0:
                        db.commit()
                db.commit()
    return {
        "schema": "recorder-audit.v1",
        "status": "ok",
        "rows": rows,
        "watermark": watermark.isoformat() if watermark else None,
        "partitions": _partition_sizes(output),
    }


def _partition_sizes(output: Path) -> dict[str, int]:
    return {path.name: path.stat().st_size for path in corpus_partitions(output)}


def _prune_index(index: dict) -> dict:
    """Drop dedup keys and option pins that fell out of their bounded window."""
    index.setdefault("bar_coverage", {})
    index.setdefault("session_calendar", {})
    index.setdefault("partition_sources", {})
    index.setdefault("data_feed", None)
    watermark = _timestamp(index.get("watermark"))
    if watermark is not None:
        floor = (watermark - DEDUP_HORIZON).isoformat()
        index["recent_keys"] = {key: value for key, value in index["recent_keys"].items()
                                if value >= floor}
    return index


def _load_index(output: Path) -> dict | None:
    """Return the sidecar index when it demonstrably matches the partitions."""
    path = _corpus_root(output) / INDEX_NAME
    if not path.is_file():
        return None
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        return None
    for name in ("latest_bars", "recent_keys", "option_pins", "partitions"):
        if not isinstance(index.get(name), dict):
            return None
    coverage = index.get("bar_coverage")
    if coverage is None:  # backward-compatible additive recorder-index.v1 field
        index["bar_coverage"] = {}
    elif not isinstance(coverage, dict):
        return None
    session_calendar = index.get("session_calendar")
    if session_calendar is None:  # additive recorder-index.v1 field
        index["session_calendar"] = {}
    elif not isinstance(session_calendar, dict):
        return None
    else:
        for day, value in session_calendar.items():
            if not isinstance(day, str) or not isinstance(value, dict):
                return None
            if not all(isinstance(value.get(name), str)
                       for name in ("open", "close")):
                return None
            opened = _timestamp(value.get("open"))
            closed = _timestamp(value.get("close"))
            try:
                parsed_day = date.fromisoformat(day)
            except ValueError:
                return None
            if (opened is None or closed is None or opened >= closed or
                    opened.astimezone(NEW_YORK).date() != parsed_day or
                    closed.astimezone(NEW_YORK).date() != parsed_day):
                return None
    partition_sources = index.get("partition_sources")
    if partition_sources is None:  # additive recorder-index.v1 field
        index["partition_sources"] = {}
    elif not isinstance(partition_sources, dict):
        return None
    else:
        for name, value in partition_sources.items():
            if (not isinstance(name, str) or not isinstance(value, dict) or
                    not isinstance(value.get("source_mode"), str)):
                return None
    durable_sources = _partition_sources_from_markers(output)
    if any(index["partition_sources"].get(name) != value
           for name, value in durable_sources.items()):
        return None
    data_feed = index.get("data_feed")
    if data_feed is not None and not isinstance(data_feed, str):
        return None
    if isinstance(data_feed, str) and data_feed.strip():
        try:
            index["data_feed"] = _canonical_data_feed(data_feed)
        except RuntimeError:
            return None
    else:
        index["data_feed"] = None
    # A cycle appends rows and then rewrites the index; a crash between the two
    # leaves the index short. Byte sizes catch exactly that and force a rebuild.
    if index["partitions"] != _partition_sizes(output):
        return None
    if output.exists() and output.stat().st_size:
        return None
    return _prune_index(index)


def _save_index(output: Path, index: dict) -> None:
    root = _corpus_root(output)
    root.mkdir(parents=True, exist_ok=True)
    index = {**index, "schema": INDEX_SCHEMA, "partitions": _partition_sizes(output)}
    temporary = root / (INDEX_NAME + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(index, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / INDEX_NAME)


def _existing_state(output: Path) -> tuple[set[str], datetime | None, dict[str, datetime]]:
    """Durable state for one cycle: dedup window, watermark, per-symbol bars."""
    index = _load_index(output) or _scan_corpus(output)
    latest_bars = {symbol: _timestamp(value)
                   for symbol, value in index["latest_bars"].items()}
    return (set(index["recent_keys"]), _timestamp(index.get("watermark")),
            {symbol: value for symbol, value in latest_bars.items() if value is not None})


def _existing_keys(output: Path) -> set[str]:
    """Every durable key. Operational inspection only; a cycle never calls it."""
    return {str(row.get("event_key") or "") for row in iter_corpus_rows(output)}


def _corpus_data_feed(output: Path) -> str | None:
    """Stream a pre-coverage corpus once to recover unambiguous feed identity."""
    feeds: set[str] = set()
    for row in iter_corpus_rows(output):
        event = str(row.get("event_type") or "").strip().lower()
        feed = str(row.get("feed") or "").strip()
        if event not in {"bar", "bar_1m", "quote"} or not feed:
            continue
        feeds.add(_canonical_data_feed(feed))
        if len(feeds) > 1:
            raise RuntimeError(
                "recorder corpus mixes equity data feeds: " +
                ", ".join(sorted(feeds)))
    return next(iter(feeds), None)


class CalendarCache:
    """Cache the Alpaca trading calendar so continuity never fetches per cycle.

    The calendar is the authority on holidays and early closes. When it is
    unavailable the recorder keeps the conservative regular-session heuristic
    rather than inventing sessions, and retries at most once per local day.
    """

    def __init__(self, provider, *, span_days: int = 14) -> None:
        self.provider = provider
        self.span_days = int(span_days)
        self.days: dict[date, object] = {}
        self.covered: tuple[date, date] | None = None
        self.available: bool | None = None
        self.fetches = 0
        self._failed_on: date | None = None

    def _refresh(self, day: date) -> None:
        method = getattr(self.provider, "calendar", None)
        if not callable(method):
            self.available = False
            return
        start = day - timedelta(days=self.span_days)
        end = day + timedelta(days=self.span_days)
        try:
            rows = method(start=start, end=end) or []
        except (AlpacaError, TypeError, ValueError, OSError):
            self.available = False
            self._failed_on = day
            return
        self.fetches += 1
        self.days = {}
        malformed = False
        for row in rows:
            try:
                session = normalize_calendar_day(row)
            except (TypeError, ValueError):
                malformed = True
                continue
            self.days[session.date] = session
        self.covered = (start, end)
        self.available = not malformed

    def session(self, day: date):
        """Return the calendar day, or ``None`` when the market is shut."""
        if self.available is False and self._failed_on == day:
            return None
        if self.covered is None or not (self.covered[0] <= day <= self.covered[1]):
            self._refresh(day)
        if self.available is not True:
            return None
        return self.days.get(day)

    def known(self, day: date) -> bool:
        self.session(day)
        return self.available is True


def _record_session_calendar(index: dict, calendar: CalendarCache | None,
                             start: datetime, end: datetime) -> None:
    """Persist exact Alpaca opens/closes for replay completion boundaries."""
    if calendar is None:
        return
    # The requested range is the authority.  Historical calendar boundaries
    # must remain available for reproducible replay well beyond ninety days.
    first = start.astimezone(NEW_YORK).date()
    last = end.astimezone(NEW_YORK).date()
    sessions = index.setdefault("session_calendar", {})
    cursor = first
    while cursor <= last:
        if calendar.known(cursor):
            session = calendar.session(cursor)
            if session is not None:
                sessions[cursor.isoformat()] = {
                    "open": session.open.astimezone(timezone.utc).isoformat(),
                    "close": session.close.astimezone(timezone.utc).isoformat(),
                    "source": "alpaca_calendar",
                }
        cursor += timedelta(days=1)


def _inside_recorded_session(index: dict, row: dict, *,
                              require_exact_calendar: bool = False) -> bool:
    """Drop extended-hours rows when an exact broker session is known."""
    stamp = _timestamp(row.get("timestamp"))
    if stamp is None:
        return False
    session = stamp.astimezone(NEW_YORK).date().isoformat()
    calendar = index.get("session_calendar")
    value = calendar.get(session) if isinstance(calendar, dict) else None
    if not isinstance(value, dict):
        return not require_exact_calendar
    opened = _timestamp(value.get("open"))
    closed = _timestamp(value.get("close"))
    if (opened is None or closed is None or opened >= closed or
            opened.astimezone(NEW_YORK).date().isoformat() != session or
            closed.astimezone(NEW_YORK).date().isoformat() != session):
        return False
    if not opened <= stamp < closed:
        return False
    if str(row.get("event_type") or "") in {"bar", "bar_1m"}:
        as_of = _timestamp(row.get("as_of"))
        return as_of is not None and as_of <= closed
    return True


def _regular_session_gap(previous: datetime, current: datetime,
                         calendar: CalendarCache | None = None,
                         maximum: timedelta = timedelta(
                             minutes=DEFAULT_BAR_GAP_MINUTES)) -> bool:
    """Is the hole between two bars a real gap inside a scheduled session?"""
    before = previous.astimezone(NEW_YORK)
    after = current.astimezone(NEW_YORK)
    if before.date() != after.date():
        return False
    if current - previous <= maximum:
        return False
    if calendar is not None and calendar.known(before.date()):
        session = calendar.session(before.date())
        if session is None:  # scheduled holiday: silence is correct
            return False
        open_minute = session.open.hour * 60 + session.open.minute
        close_minute = session.close.hour * 60 + session.close.minute
    else:
        if before.weekday() >= 5:
            return False
        open_minute = 9 * 60 + 30
        close_minute = 16 * 60
    before_minute = before.hour * 60 + before.minute
    after_minute = after.hour * 60 + after.minute
    return (open_minute <= before_minute <= close_minute and
            open_minute <= after_minute <= close_minute)


def _verify_bar_continuity(rows: list[dict], latest_bars: dict[str, datetime],
                           now: datetime, symbols: list[str],
                           calendar: CalendarCache | None = None, *,
                           feed: str = "unknown", policy: str = "strict",
                           maximum: timedelta = timedelta(
                               minutes=DEFAULT_BAR_GAP_MINUTES)) -> dict[str, dict]:
    """Return bounded coverage evidence and reject gaps only in strict mode."""
    if policy not in {"observe", "strict"}:
        raise ValueError("recorder bar continuity policy must be observe or strict")
    by_symbol: dict[str, list[datetime]] = {symbol: [] for symbol in symbols}
    for row in rows:
        if row.get("event_type") != "bar_1m":
            continue
        symbol = validate_equity_symbol(row.get("symbol"))
        parsed = _timestamp(row.get("timestamp"))
        if parsed is not None:
            by_symbol.setdefault(symbol, []).append(parsed)
    evidence: dict[str, dict] = {}
    first_gap: str | None = None
    for symbol in symbols:
        previous = latest_bars.get(symbol)
        fresh = sorted({
            item for item in by_symbol.get(symbol, ())
            if previous is None or item > previous
        })
        gaps: list[tuple[datetime, datetime]] = []
        latest = previous
        if previous is None:
            latest = fresh[-1] if fresh else None
        elif not fresh:
            if _regular_session_gap(previous, now, calendar, maximum):
                gaps.append((previous, now))
        else:
            for current in fresh:
                if _regular_session_gap(previous, current, calendar, maximum):
                    gaps.append((previous, current))
                previous = current
            latest = previous

        last_gap = gaps[-1] if gaps else None
        window_max = max(
            (int((end - start).total_seconds()) for start, end in gaps),
            default=0)
        status = ("gap_observed" if gaps else
                  "covered" if latest is not None else "unobserved")
        evidence[symbol] = {
            "feed": str(feed),
            "policy": policy,
            "status": status,
            "checked_through": now.isoformat(),
            "last_bar": latest.isoformat() if latest is not None else None,
            "gap_threshold_seconds": int(maximum.total_seconds()),
            "window_bars": len(fresh),
            "window_gap_count": len(gaps),
            "window_max_gap_seconds": window_max,
            "last_gap_start": last_gap[0].isoformat() if last_gap else None,
            "last_gap_end": last_gap[1].isoformat() if last_gap else None,
            "last_gap_seconds": (
                int((last_gap[1] - last_gap[0]).total_seconds())
                if last_gap else None),
        }
        if gaps and first_gap is None:
            first_gap = symbol

    if policy == "strict" and first_gap is not None:
        raise RuntimeError(f"recorder bar continuity gap for {first_gap}")
    return evidence


def _coverage_count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _update_bar_coverage(index: dict, observations: dict[str, dict],
                         observed_at: datetime) -> None:
    """Merge one window into bounded per-symbol coverage counters."""
    coverage = index.setdefault("bar_coverage", {})
    for symbol, observation in observations.items():
        previous = coverage.get(symbol)
        if not isinstance(previous, dict):
            previous = {}
        window_bars = _coverage_count(observation.get("window_bars"))
        window_gaps = _coverage_count(observation.get("window_gap_count"))
        cumulative_gaps = _coverage_count(
            previous.get("gap_observations")) + window_gaps
        merged = {
            "feed": observation.get("feed"),
            "policy": observation.get("policy"),
            "status": ("gap_observed" if cumulative_gaps else
                       observation.get("status")),
            "checked_through": observation.get("checked_through"),
            "observed_at": observed_at.isoformat(),
            "last_bar": observation.get("last_bar"),
            "gap_threshold_seconds": observation.get("gap_threshold_seconds"),
            "windows_observed": _coverage_count(
                previous.get("windows_observed")) + 1,
            "bars_observed": _coverage_count(
                previous.get("bars_observed")) + window_bars,
            "gap_observations": cumulative_gaps,
            "last_window_bars": window_bars,
            "last_window_gap_count": window_gaps,
            "last_window_max_gap_seconds": _coverage_count(
                observation.get("window_max_gap_seconds")),
            "max_gap_seconds": max(
                _coverage_count(previous.get("max_gap_seconds")),
                _coverage_count(observation.get("window_max_gap_seconds"))),
            "last_gap_start": previous.get("last_gap_start"),
            "last_gap_end": previous.get("last_gap_end"),
            "last_gap_seconds": previous.get("last_gap_seconds"),
            "last_gap_observed_at": previous.get("last_gap_observed_at"),
        }
        if window_gaps:
            merged.update({
                "last_gap_start": observation.get("last_gap_start"),
                "last_gap_end": observation.get("last_gap_end"),
                "last_gap_seconds": observation.get("last_gap_seconds"),
                "last_gap_observed_at": observed_at.isoformat(),
            })
        coverage[symbol] = merged


def _migrate_header(output: Path) -> None:
    """Upgrade a pre-dedup CSV before appending rows with ``event_key``."""
    if not output.exists() or output.stat().st_size == 0:
        return
    temporary = output.with_suffix(output.suffix + ".migrate")
    try:
        with output.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if "event_key" in (reader.fieldnames or []):
                return
            required = {"event_type", "symbol", "timestamp"}
            fields = set(reader.fieldnames or ())
            if not required.issubset(fields):
                raise RuntimeError(
                    f"legacy recorder dataset has invalid header; missing {sorted(required - fields)}")
            with temporary.open("w", newline="", encoding="utf-8") as target:
                writer = csv.DictWriter(target, fieldnames=FIELDS)
                writer.writeheader()
                for raw in reader:
                    if None in raw:
                        raise RuntimeError("legacy recorder dataset contains a malformed CSV row")
                    row = {str(key): value for key, value in raw.items() if key is not None}
                    _validate_dataset_row(row)
                    row["event_key"] = _event_key(
                        row.get("event_type", ""), row.get("symbol", ""),
                        row.get("timestamp", ""))
                    writer.writerow({field: row.get(field, "") for field in FIELDS})
                target.flush()
                os.fsync(target.fileno())
    except (OSError, csv.Error) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"cannot migrate recorder dataset {output}: {exc}") from exc
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, output)


def _append_partitions(output: Path, rows: list[dict]) -> None:
    """Append rows to their session partitions, creating headers as needed."""
    by_day: dict[date, list[dict]] = {}
    for row in rows:
        parsed = _timestamp(row.get("timestamp"))
        if parsed is None:
            raise RuntimeError("recorder row has an invalid timestamp")
        by_day.setdefault(_session_date(parsed), []).append(row)
    for day in sorted(by_day):
        path = _partition_path(output, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if fresh:
                writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in FIELDS}
                             for row in by_day[day])
            handle.flush()
            os.fsync(handle.fileno())


def migrate_corpus(output: Path) -> int:
    """Split a legacy single-file corpus into durable session partitions.

    The legacy file is validated in full before anything is written and is kept
    beside the corpus as ``*.migrated`` afterwards, so a first run after upgrade
    either partitions the whole corpus or leaves it untouched.
    """
    _migrate_header(output)
    if not output.exists() or output.stat().st_size == 0:
        return 0
    if corpus_partitions(output):
        raise RuntimeError(
            f"cannot migrate {output}: session partitions already exist")

    # Migration is an explicit offline operation, so validate all historical
    # keys with the disk-backed audit before streaming rows into partitions.
    audit_corpus(output)
    staged: dict[date, tuple[object, csv.DictWriter, Path]] = {}
    count = 0
    try:
        for row, _key, _event, _symbol, parsed in _validated_corpus_rows(output):
            day = _session_date(parsed)
            path = _partition_path(output, day)
            temporary = path.with_suffix(path.suffix + ".migrate")
            if day not in staged:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary.unlink(missing_ok=True)
                handle = temporary.open("w", newline="", encoding="utf-8")
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                staged[day] = (handle, writer, path)
            handle, writer, _path = staged[day]
            writer.writerow({field: row.get(field, "") for field in FIELDS})
            count += 1
        for handle, _writer, _path in staged.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        for _day, (_handle, _writer, path) in staged.items():
            temporary = path.with_suffix(path.suffix + ".migrate")
            os.replace(temporary, path)
    except Exception:
        for handle, _writer, path in staged.values():
            try:
                handle.close()
            except OSError:
                pass
            path.with_suffix(path.suffix + ".migrate").unlink(missing_ok=True)
        raise
    os.replace(output, output.with_name(output.name + ".migrated"))
    _save_index(output, _scan_corpus(output))
    return count


def _fetch_window_minutes() -> int:
    raw = os.getenv("ALPACA_RECORDER_FETCH_WINDOW_MINUTES",
                    str(DEFAULT_FETCH_WINDOW_MINUTES))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "ALPACA_RECORDER_FETCH_WINDOW_MINUTES must be a positive integer"
        ) from exc
    if value <= 0:
        raise RuntimeError(
            "ALPACA_RECORDER_FETCH_WINDOW_MINUTES must be a positive integer")
    return value


def _bar_gap_minutes() -> int:
    raw = os.getenv("ALPACA_RECORDER_BAR_GAP_MINUTES",
                    str(DEFAULT_BAR_GAP_MINUTES))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "ALPACA_RECORDER_BAR_GAP_MINUTES must be a positive integer"
        ) from exc
    if value <= 0:
        raise RuntimeError(
            "ALPACA_RECORDER_BAR_GAP_MINUTES must be a positive integer")
    return value


def _strict_bar_feeds() -> frozenset[str]:
    raw = os.getenv("ALPACA_RECORDER_STRICT_BAR_FEEDS", "").strip().lower()
    if not raw or raw == "none":
        return frozenset()
    feeds = set()
    for value in raw.split(","):
        feed = value.strip().replace("-", "_")
        if not feed:
            raise RuntimeError(
                "ALPACA_RECORDER_STRICT_BAR_FEEDS must contain only "
                "iex, sip, delayed_sip, or *")
        if feed != "*":
            try:
                feed = _canonical_data_feed(feed)
            except RuntimeError as exc:
                raise RuntimeError(
                    "ALPACA_RECORDER_STRICT_BAR_FEEDS must contain only "
                    "iex, sip, delayed_sip, or *") from exc
        feeds.add(feed)
    return frozenset(feeds)


def _bar_gap_policy(feed: str) -> str:
    strict = _strict_bar_feeds()
    return "strict" if "*" in strict or feed in strict else "observe"


def _resolved_feed(provider, feed: str | None, config: dict | None) -> str:
    value = (feed if feed is not None else
             getattr(provider, "data_feed", None) or _feed(config))
    return _canonical_data_feed(value)


def _ingest_chunk(output: Path, index: dict, seen: set[str],
                  watermark: datetime | None,
                  latest_bars: dict[str, datetime], pins: dict,
                  rows: list[dict], symbols: list[str],
                  window_end: datetime, observed_at: datetime,
                  option_hold: timedelta,
                  horizon: datetime | None,
                  calendar: CalendarCache | None, *, feed: str,
                  bar_gap_policy: str,
                  bar_gap_maximum: timedelta):
    """Validate and durably append one bounded provider response."""
    coverage = _verify_bar_continuity(
        rows, latest_bars, window_end, symbols, calendar, feed=feed,
        policy=bar_gap_policy, maximum=bar_gap_maximum)
    _update_bar_coverage(index, coverage, observed_at)
    unique_rows: list[dict] = []
    for row in rows:
        parsed = _timestamp(row.get("timestamp"))
        if parsed is None:
            raise RuntimeError("recorder row has an invalid timestamp")
        if horizon is not None and parsed < horizon:
            # Outside the durable dedup window uniqueness cannot be proven, so
            # the row is refused rather than risking a silent replay.
            raise RuntimeError(
                "recorder received rows older than the dedup window")
        key = str(row.get("event_key") or "").strip()
        if not key:
            raise RuntimeError("recorder row has no event_key")
        if row.get("event_type") == "option_snapshot":
            # Refresh the hold even when the snapshot itself is a duplicate;
            # the option contract remains pinned while the recorder observes it.
            pins[str(row.get("contract") or row.get("symbol"))] = (
                observed_at + option_hold).isoformat()
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
        index["recent_keys"][key] = parsed.isoformat()
        if watermark is None or parsed > watermark:
            watermark = parsed
        if row.get("event_type") in {"bar", "bar_1m"}:
            symbol = str(row.get("symbol") or "")
            if index["latest_bars"].get(symbol, "") < parsed.isoformat():
                index["latest_bars"][symbol] = parsed.isoformat()
            previous = latest_bars.get(symbol)
            if previous is None or parsed > previous:
                latest_bars[symbol] = parsed

    index["option_pins"] = pins
    index["watermark"] = watermark.isoformat() if watermark else None
    index = _prune_index(index)
    if rows:
        if unique_rows:
            _append_partitions(output, unique_rows)
        # Persist even a duplicate-only response: the sidecar write is the
        # recorder's durable liveness signal, while the corpus remains unchanged.
        _save_index(output, index)
    return index, set(index["recent_keys"]), watermark, latest_bars, pins, len(unique_rows)


def record_once(provider: AlpacaProvider, symbols: list[str], output: Path,
                *, feed: str | None = None, config: dict | None = None,
                include_options: bool | None = None, option_limit: int = 5,
                option_hold: timedelta = timedelta(minutes=180),
                calendar: CalendarCache | None = None) -> int:
    symbols = [validate_equity_symbol(symbol) for symbol in symbols]
    if not symbols:
        raise ValueError("at least one US equity symbol is required")
    now = datetime.now(timezone.utc)
    if include_options is None:
        classes = (config or {}).get("universe", {}).get("asset_classes", [])
        include_options = any(str(value).lower() in {"us_option", "option"}
                              for value in classes)
    _corpus_root(output).mkdir(parents=True, exist_ok=True)
    migrate_corpus(output)
    index = _load_index(output) or _scan_corpus(output)
    session_cfg = (config or {}).get("session") if isinstance(config, dict) else {}
    require_exact_calendar = bool(
        session_cfg.get("require_exact_calendar", False)
        if isinstance(session_cfg, dict) else False)
    if require_exact_calendar and calendar is None:
        raise RuntimeError(
            "exact broker calendar metadata is required for production recording")
    resolved_feed = _resolved_feed(provider, feed, config)
    indexed_feed = str(index.get("data_feed") or "").strip().lower()
    if not indexed_feed and (index.get("partitions") or
                             (output.exists() and output.stat().st_size)):
        indexed_feed = _corpus_data_feed(output) or ""
        if indexed_feed:
            index["data_feed"] = indexed_feed
    if indexed_feed and indexed_feed != resolved_feed:
        raise RuntimeError(
            f"recorder data feed changed from {indexed_feed} to {resolved_feed}; "
            "use a separate corpus")
    index["data_feed"] = resolved_feed
    bar_gap_policy = _bar_gap_policy(resolved_feed)
    bar_gap_maximum = timedelta(minutes=_bar_gap_minutes())
    seen = set(index["recent_keys"])
    watermark = _timestamp(index.get("watermark"))
    latest_bars = {symbol: parsed for symbol, parsed in
                   ((key, _timestamp(value))
                    for key, value in index["latest_bars"].items())
                   if parsed is not None}
    if watermark is not None and watermark > now + timedelta(seconds=5):
        raise RuntimeError("recorder dataset watermark is in the future")
    # Resume from durable data rather than a fixed three-minute lookback. The
    # one-minute overlap makes retries safe while the event key removes dupes.
    # A stale watermark is split into bounded requests so a long outage cannot
    # materialize millions of quotes in one provider response.
    start = (watermark - timedelta(minutes=1)
             if watermark is not None else now - timedelta(minutes=3))
    _record_session_calendar(index, calendar, start, now)
    pins = {contract: value for contract, value in index["option_pins"].items()
            if str(value) > now.isoformat()}
    horizon = None if watermark is None else watermark - DEDUP_HORIZON
    window = timedelta(minutes=_fetch_window_minutes())
    cursor = start
    total_rows = 0
    total_unique = 0
    while True:
        window_end = min(cursor + window, now)
        fetched_rows = list(_rows(
            provider, symbols, window_end, feed=resolved_feed, config=config,
            # Option-chain snapshots are observations made now, not historical
            # market data. Sampling them in every stale catch-up window can
            # admit an illiquid contract's old quote timestamp and incorrectly
            # trip the equity dedup horizon. Sample once, in the live window,
            # and let recorder_market discard candidates older than that
            # request's lower bound.
            include_options=bool(include_options and window_end >= now),
            option_limit=option_limit,
            start=cursor, option_pins=frozenset(pins), observed_at=now))
        total_rows += len(fetched_rows)
        rows = []
        for row in fetched_rows:
            inside = _inside_recorded_session(
                index, row, require_exact_calendar=require_exact_calendar)
            if require_exact_calendar and not inside:
                session = _session_date(parsed) if (parsed := _timestamp(
                    row.get("timestamp"))) is not None else "unknown"
                raise RuntimeError(
                    f"exact broker calendar metadata missing or row outside session: {session}")
            if inside:
                rows.append(row)
        index, seen, watermark, latest_bars, pins, unique = _ingest_chunk(
            output, index, seen, watermark, latest_bars, pins, rows, symbols,
            window_end, now, option_hold, horizon, calendar,
            feed=resolved_feed, bar_gap_policy=bar_gap_policy,
            bar_gap_maximum=bar_gap_maximum)
        total_unique += unique
        if window_end >= now:
            break
        cursor = window_end

    if total_rows == 0:
        raise RuntimeError("Alpaca returned no point-in-time bars or quotes")
    return total_unique


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="record Alpaca paper market data")
    p.add_argument("--out", default="runtime/research/recorded")
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--once", action="store_true")
    p.add_argument("--audit", action="store_true",
                   help="validate the full corpus and detect duplicate keys")
    p.add_argument("--config", default="config.yaml")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    env_file = os.getenv("ALPACA_AGENT_SECRETS_FILE")
    if env_file:
        load_dotenv(env_file, override=False)
    output = Path(args.out) / "market.csv"
    if args.audit:
        try:
            print(json.dumps(audit_corpus(output), sort_keys=True))
            return 0
        except Exception as exc:  # explicit audit must report a machine-readable failure
            print(json.dumps({
                "schema": "recorder-audit.v1",
                "status": "failed",
                "error": str(exc),
            }, sort_keys=True), file=sys.stderr, flush=True)
            return 2
    from main import load_cfg
    cfg = load_cfg(args.config)
    symbols = list(cfg.get("universe", {}).get("symbols") or [])
    if not symbols:
        raise SystemExit("config.universe.symbols is empty")
    provider = AlpacaProvider(cfg)
    option_limit = max(1, min(MAX_OPTION_LIMIT,
                              int(os.getenv("ALPACA_RECORDER_OPTION_LIMIT", "10"))))
    option_hold = timedelta(minutes=max(1, int(
        os.getenv("ALPACA_RECORDER_OPTION_HOLD_MINUTES", "180"))))
    include_options = any(str(value).lower() in {"us_option", "option"}
                          for value in cfg.get("universe", {}).get("asset_classes", []))
    calendar = CalendarCache(provider)
    failure_count = 0
    while True:
        try:
            count = record_once(provider, symbols, output, config=cfg,
                                include_options=include_options,
                                option_limit=option_limit,
                                option_hold=option_hold, calendar=calendar)
        except Exception as exc:
            failure_count += 1
            delay = min(
                MAX_ERROR_BACKOFF_SECONDS,
                max(args.interval, 1.0) * (2 ** min(failure_count - 1, 4)),
            )
            payload = {
                "schema": "recorder-error.v1",
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "failure_count": failure_count,
                "retry_seconds": delay,
            }
            print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)
            if args.once:
                return 1
            time.sleep(delay)
            continue
        failure_count = 0
        print(f"recorded {count} Alpaca rows to {output}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
