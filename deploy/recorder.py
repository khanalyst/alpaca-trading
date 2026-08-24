#!/usr/bin/env python3
"""Small Alpaca paper-data recorder used by the optional Compose lane.

The recorder writes normalized, append-only CSV rows for configured US equity
symbols. It has no order methods and never mutates trading state. A failed
authenticated read exits non-zero so the service health check cannot report a
fresh but empty dataset.

Storage is partitioned by New York session date under ``sessions/`` next to the
nominal dataset path. A compact JSON sidecar holds the watermark, per-symbol
last bar, coverage evidence, and option contracts held open for continued
sampling; an exact SQLite sidecar holds the time-bounded dedup window without
materializing high-rate quote keys in memory. A cycle therefore costs O(new
rows) instead of rescanning the corpus. Both sidecars are caches bound to the
partition sizes and watermark; any mismatch rebuilds them from the partitions.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import mmap
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing, contextmanager
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
RECENT_KEY_INDEX_NAME = ".recorder-recent-keys.sqlite3"
RECENT_KEY_INDEX_SCHEMA = "recorder-recent-keys.v1"
CORPUS_LOCK_NAME = ".recorder.lock"
STATUS_NAME = ".recorder-status.json"
STATUS_SCHEMA = "recorder-status.v1"
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
MAX_INLINE_INDEX_BYTES = 16 * 1024 * 1024
RECENT_KEY_BATCH_SIZE = 10_000
DEFAULT_FETCH_WINDOW_MINUTES = 1
DEFAULT_BAR_GAP_MINUTES = 5
MAX_ERROR_BACKOFF_SECONDS = 15 * 60
# Calendar metadata is an audit boundary, not a deduplication cache.  Keep the
# name for callers that imported the old constant, but do not prune calendar
# entries by age.
SESSION_CALENDAR_RETENTION_DAYS = 90


def _save_status(output: Path, payload: dict) -> dict:
    """Atomically persist the latest recorder attempt without touching data."""
    root = _corpus_root(output)
    root.mkdir(parents=True, exist_ok=True)
    value = {
        "schema": STATUS_SCHEMA,
        "updated_ts": time.time(),
        **payload,
    }
    temporary = root / (STATUS_NAME + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / STATUS_NAME)
    return value


def _market_data_failure(exc: Exception, *, data_feed: str,
                         options_feed: str) -> tuple[str, bool]:
    """Classify permanent entitlement failures separately from outages."""
    message = str(exc).strip().lower()
    entitlement_markers = (
        "subscription does not permit",
        "not entitled",
        "insufficient subscription",
        "insufficient permission",
        "forbidden",
    )
    if any(marker in message for marker in entitlement_markers):
        selected = options_feed if any(
            marker in message for marker in ("option", "opra")) else data_feed
        return f"{selected}_entitlement_required", False
    return "market_data_request_failed", True


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
    _fsync_directory(path.parent)


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
                not isinstance(partition, str) or
                path.name != partition + ".source.json" or
                source_mode != "historical_backfill"):
            raise RuntimeError(f"invalid recorder partition source marker {path}")
        # Marker-before-partition ordering prevents a backfilled CSV from ever
        # looking forward-observed. A crash in that narrow gap can leave a
        # valid orphan marker; ignore it until the resumable backfill writes the
        # referenced partition.
        if partition not in existing:
            continue
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

    partitions = _partition_sizes(output)
    fingerprints = _partition_fingerprints(output)
    recent_key_index = _rebuild_recent_key_index(
        output, watermark=watermark, partitions=partitions,
        fingerprints=fingerprints)

    index = {"schema": INDEX_SCHEMA,
             "watermark": watermark.isoformat() if watermark else None,
             "latest_bars": latest_bars,
             "recent_key_index": recent_key_index,
             "option_pins": {}, "bar_coverage": {},
             "session_calendar": {},
             "partition_sources": _partition_sources_from_markers(output),
             "data_feed": next(iter(data_feeds), None),
             "partitions": partitions,
             "partition_fingerprints": fingerprints}
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


def _partition_fingerprints(output: Path) -> dict[str, dict[str, int]]:
    """Cheap mutation evidence for caches; the explicit audit hashes contents."""
    result = {}
    for path in corpus_partitions(output):
        stat = path.stat()
        result[path.name] = {
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
    return result


def _fsync_directory(path: Path) -> None:
    """Make an atomic replacement durable across a host power loss."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def corpus_write_lock(output: Path):
    """Serialize every CSV/SQLite/JSON corpus mutation across processes."""
    root = _corpus_root(output)
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / CORPUS_LOCK_NAME).open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _recent_key_index_path(output: Path) -> Path:
    return _corpus_root(output) / RECENT_KEY_INDEX_NAME


def _recent_key_signature(watermark: object,
                          partitions: dict[str, int],
                          fingerprints: dict[str, dict[str, int]]) -> str:
    payload = json.dumps({
        "watermark": str(watermark) if watermark not in (None, "") else None,
        "partitions": partitions,
        "partition_fingerprints": fingerprints,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recent_key_timestamp(value: object) -> str:
    """Return one UTC representation so SQLite text ordering is chronological."""
    parsed = _timestamp(value)
    if parsed is None:
        raise RuntimeError("recorder recent-key index has an invalid timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


class RecentKeyIndex:
    """Disk-backed membership for the recorder's overlap deduplication window.

    A time horizon is not a memory bound: a high-volume IEX minute can contain
    tens of thousands of quote updates.  Keeping those hashes in JSON caused a
    110 MiB sidecar to expand beyond the recorder's 768 MiB cgroup while being
    decoded.  SQLite keeps membership exact without materializing the window.
    """

    def __init__(self, path: Path, *, create: bool = False,
                 read_only: bool = False) -> None:
        self.path = path
        if read_only:
            uri = f"file:{path.resolve()}?mode=ro"
            self.db = sqlite3.connect(uri, uri=True)
        elif create:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
        else:
            if not path.is_file():
                raise RuntimeError("recorder recent-key index is missing")
            uri = f"file:{path.resolve()}?mode=rw"
            self.db = sqlite3.connect(uri, uri=True)
        if not read_only:
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA temp_store=FILE")
        if create:
            with self.db:
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS recent_keys ("
                    "event_key TEXT PRIMARY KEY, event_ts TEXT NOT NULL) "
                    "WITHOUT ROWID")
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS recent_keys_event_ts "
                    "ON recent_keys(event_ts)")
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS metadata ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID")

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def contains(self, event_key: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM recent_keys WHERE event_key=? LIMIT 1",
            (event_key,)).fetchone() is not None

    def add_many(self, entries) -> None:
        values = [(str(key), _recent_key_timestamp(stamp))
                  for key, stamp in entries]
        if not values:
            return
        try:
            with self.db:
                self.db.executemany(
                    "INSERT INTO recent_keys(event_key,event_ts) VALUES (?,?)",
                    values)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                "recorder recent-key index repeats an event_key") from exc

    def prune(self, floor: datetime | None) -> None:
        if floor is None:
            return
        with self.db:
            self.db.execute(
                "DELETE FROM recent_keys WHERE event_ts < ?",
                (_recent_key_timestamp(floor),))

    def count(self) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM recent_keys").fetchone()
        return int(row[0]) if row is not None else 0

    def metadata(self) -> dict[str, str]:
        try:
            return {str(key): str(value) for key, value in self.db.execute(
                "SELECT key,value FROM metadata")}
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("recorder recent-key index is invalid") from exc

    def bind(self, *, signature: str) -> dict:
        count = self.count()
        values = {
            "schema": RECENT_KEY_INDEX_SCHEMA,
            "corpus_signature": signature,
            "count": str(count),
        }
        with self.db:
            self.db.executemany(
                "INSERT INTO metadata(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                sorted(values.items()))
        return {
            "schema": RECENT_KEY_INDEX_SCHEMA,
            "name": RECENT_KEY_INDEX_NAME,
            "count": count,
            "corpus_signature": signature,
        }


def _remove_sqlite_artifacts(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-journal"),
                      Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)


def _build_recent_key_index(output: Path, entries, *, watermark: object,
                            partitions: dict[str, int],
                            fingerprints: dict[str, dict[str, int]]) -> dict:
    """Atomically replace the disk-backed overlap index from streamed keys."""
    target = _recent_key_index_path(output)
    temporary = target.with_name(target.name + ".tmp")
    _remove_sqlite_artifacts(temporary)
    signature = _recent_key_signature(watermark, partitions, fingerprints)
    try:
        with RecentKeyIndex(temporary, create=True) as store:
            batch = []
            for key, stamp in entries:
                batch.append((key, stamp))
                if len(batch) >= RECENT_KEY_BATCH_SIZE:
                    store.add_many(batch)
                    batch.clear()
            store.add_many(batch)
            metadata = store.bind(signature=signature)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        return metadata
    except Exception:
        _remove_sqlite_artifacts(temporary)
        raise


def _rebuild_recent_key_index(output: Path, *, watermark: datetime | None,
                              partitions: dict[str, int],
                              fingerprints: dict[str, dict[str, int]]) -> dict:
    floor = watermark - DEDUP_HORIZON if watermark is not None else None

    def entries():
        for _row, key, _event, _symbol, parsed in _validated_corpus_rows(output):
            if floor is None or parsed >= floor:
                yield key, parsed.isoformat()

    return _build_recent_key_index(
        output, entries(), watermark=watermark.isoformat() if watermark else None,
        partitions=partitions, fingerprints=fingerprints)


def _recent_key_index_matches(output: Path, metadata: object, *,
                              watermark: object,
                              partitions: dict[str, int],
                              fingerprints: dict[str, dict[str, int]]) -> bool:
    if not isinstance(metadata, dict):
        return False
    signature = _recent_key_signature(watermark, partitions, fingerprints)
    if (metadata.get("schema") != RECENT_KEY_INDEX_SCHEMA or
            metadata.get("name") != RECENT_KEY_INDEX_NAME or
            metadata.get("corpus_signature") != signature or
            not isinstance(metadata.get("count"), int) or
            metadata.get("count") < 0):
        return False
    try:
        with RecentKeyIndex(
                _recent_key_index_path(output), read_only=True) as store:
            durable = store.metadata()
            durable_count = store.count()
    except (OSError, RuntimeError, sqlite3.DatabaseError):
        return False
    return (
        durable.get("schema") == RECENT_KEY_INDEX_SCHEMA and
        durable.get("corpus_signature") == signature and
        durable.get("count") == str(metadata.get("count")) and
        durable_count == metadata.get("count")
    )


def _prune_index(index: dict) -> dict:
    """Normalize bounded metadata; legacy inline keys are pruned for migration."""
    index.setdefault("bar_coverage", {})
    index.setdefault("session_calendar", {})
    index.setdefault("partition_sources", {})
    index.setdefault("data_feed", None)
    watermark = _timestamp(index.get("watermark"))
    recent_keys = index.get("recent_keys")
    if watermark is not None and isinstance(recent_keys, dict):
        floor = (watermark - DEDUP_HORIZON).isoformat()
        index["recent_keys"] = {key: value for key, value in recent_keys.items()
                                if value >= floor}
    return index


def _stream_index_metadata(path: Path) -> dict:
    """Read compact top-level fields while skipping a huge legacy key object."""
    whitespace = frozenset(b" \t\r\n")

    def skip_space(view, position: int) -> int:
        while position < len(view) and view[position] in whitespace:
            position += 1
        return position

    def string_end(view, position: int) -> int:
        if position >= len(view) or view[position] != ord('"'):
            raise ValueError("expected a JSON string")
        escaped = False
        position += 1
        while position < len(view):
            value = view[position]
            if escaped:
                escaped = False
            elif value == ord("\\"):
                escaped = True
            elif value == ord('"'):
                return position + 1
            position += 1
        raise ValueError("unterminated JSON string")

    def value_end(view, position: int) -> int:
        if position >= len(view):
            raise ValueError("missing JSON value")
        opening = view[position]
        if opening == ord('"'):
            return string_end(view, position)
        if opening in (ord("{"), ord("[")):
            stack = [ord("}") if opening == ord("{") else ord("]")]
            position += 1
            while position < len(view) and stack:
                value = view[position]
                if value == ord('"'):
                    position = string_end(view, position)
                    continue
                if value == ord("{"):
                    stack.append(ord("}"))
                elif value == ord("["):
                    stack.append(ord("]"))
                elif value in (ord("}"), ord("]")):
                    if value != stack[-1]:
                        raise ValueError("mismatched JSON container")
                    stack.pop()
                position += 1
            if stack:
                raise ValueError("unterminated JSON container")
            return position
        end = position
        while end < len(view) and view[end] not in (ord(","), ord("}")):
            end += 1
        while end > position and view[end - 1] in whitespace:
            end -= 1
        if end == position:
            raise ValueError("empty JSON value")
        return end

    result: dict = {}
    seen: set[str] = set()
    with path.open("rb") as handle, mmap.mmap(
            handle.fileno(), 0, access=mmap.ACCESS_READ) as view:
        position = skip_space(view, 0)
        if position >= len(view) or view[position] != ord("{"):
            raise ValueError("recorder index is not a JSON object")
        position = skip_space(view, position + 1)
        if position < len(view) and view[position] == ord("}"):
            position = skip_space(view, position + 1)
            if position != len(view):
                raise ValueError("recorder index has trailing data")
            return result
        while True:
            key_start = position
            key_end = string_end(view, key_start)
            if key_end - key_start > 4_096:
                raise ValueError("recorder index key is too large")
            key = json.loads(bytes(view[key_start:key_end]).decode("utf-8"))
            if not isinstance(key, str) or key in seen:
                raise ValueError("recorder index has an invalid duplicate key")
            seen.add(key)
            position = skip_space(view, key_end)
            if position >= len(view) or view[position] != ord(":"):
                raise ValueError("recorder index key has no value")
            value_start = skip_space(view, position + 1)
            end = value_end(view, value_start)
            if key != "recent_keys":
                if end - value_start > MAX_INLINE_INDEX_BYTES:
                    raise ValueError(f"recorder index metadata {key!r} is too large")
                result[key] = json.loads(
                    bytes(view[value_start:end]).decode("utf-8"))
            position = skip_space(view, end)
            if position >= len(view):
                raise ValueError("unterminated recorder index")
            if view[position] == ord("}"):
                position = skip_space(view, position + 1)
                if position != len(view):
                    raise ValueError("recorder index has trailing data")
                break
            if view[position] != ord(","):
                raise ValueError("recorder index has an invalid separator")
            position = skip_space(view, position + 1)
    return result


def _validate_index(index: object, output: Path, *, require_recent: bool,
                    require_partition_match: bool = True) -> dict | None:
    """Validate compact metadata and, for normal loads, its dedup sidecar."""
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        return None
    index = dict(index)
    for name in ("latest_bars", "option_pins", "partitions"):
        if not isinstance(index.get(name), dict):
            return None
    raw_watermark = index.get("watermark")
    if raw_watermark is not None and _timestamp(raw_watermark) is None:
        return None
    inline_keys = index.get("recent_keys")
    recent_metadata = index.get("recent_key_index")
    if inline_keys is not None:
        if not isinstance(inline_keys, dict):
            return None
        for key, value in inline_keys.items():
            if (not isinstance(key, str) or not key or
                    not isinstance(value, str) or _timestamp(value) is None):
                return None
    elif require_recent and not isinstance(recent_metadata, dict):
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
    if (require_partition_match and
            any(index["partition_sources"].get(name) != value
                for name, value in durable_sources.items())):
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
    partitions = _partition_sizes(output)
    if require_partition_match and index["partitions"] != partitions:
        return None
    fingerprints = _partition_fingerprints(output)
    indexed_fingerprints = index.get("partition_fingerprints")
    if indexed_fingerprints is not None:
        if not isinstance(indexed_fingerprints, dict):
            return None
        if require_partition_match and indexed_fingerprints != fingerprints:
            return None
    elif require_recent and inline_keys is None:
        return None
    if require_partition_match and output.exists() and output.stat().st_size:
        return None
    index = _prune_index(index)
    if require_recent and "recent_keys" not in index and not _recent_key_index_matches(
            output, index.get("recent_key_index"),
            watermark=index.get("watermark"), partitions=partitions,
            fingerprints=fingerprints):
        return None
    return index


def _load_index(output: Path) -> dict | None:
    """Return the sidecar index when it demonstrably matches the partitions."""
    path = _corpus_root(output) / INDEX_NAME
    if not path.is_file():
        return None
    try:
        # Legacy v1 files embedded every recent key.  Refuse to decode an
        # oversized cache: recovery streams the corpus into SQLite instead of
        # briefly allocating several times the JSON byte size.
        if path.stat().st_size > MAX_INLINE_INDEX_BYTES:
            return None
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _validate_index(index, output, require_recent=True)


def _load_preserved_index_metadata(output: Path) -> dict | None:
    """Recover non-key evidence even when a cache mismatch forces a rescan."""
    path = _corpus_root(output) / INDEX_NAME
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_INLINE_INDEX_BYTES:
            index = _stream_index_metadata(path)
        else:
            index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _validate_index(
        index, output, require_recent=False, require_partition_match=False)


def _save_index(output: Path, index: dict,
                recent_store: RecentKeyIndex | None = None) -> None:
    root = _corpus_root(output)
    root.mkdir(parents=True, exist_ok=True)
    index = _prune_index(dict(index))
    partitions = _partition_sizes(output)
    fingerprints = _partition_fingerprints(output)
    index.update({
        "schema": INDEX_SCHEMA,
        "partitions": partitions,
        "partition_fingerprints": fingerprints,
    })
    inline_keys = index.pop("recent_keys", None)
    signature = _recent_key_signature(
        index.get("watermark"), partitions, fingerprints)
    if isinstance(inline_keys, dict):
        metadata = _build_recent_key_index(
            output, inline_keys.items(), watermark=index.get("watermark"),
            partitions=partitions, fingerprints=fingerprints)
    else:
        owned_store = None
        try:
            if recent_store is None:
                path = _recent_key_index_path(output)
                if path.is_file():
                    owned_store = RecentKeyIndex(path)
                    recent_store = owned_store
                else:
                    metadata = _rebuild_recent_key_index(
                        output, watermark=_timestamp(index.get("watermark")),
                        partitions=partitions, fingerprints=fingerprints)
            if recent_store is not None:
                watermark = _timestamp(index.get("watermark"))
                recent_store.prune(
                    watermark - DEDUP_HORIZON if watermark is not None else None)
                metadata = recent_store.bind(signature=signature)
        finally:
            if owned_store is not None:
                owned_store.close()
    index["recent_key_index"] = metadata
    temporary = root / (INDEX_NAME + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(index, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / INDEX_NAME)
    _fsync_directory(root)


def _prepare_index(output: Path) -> dict:
    """Load or rebuild both caches while preserving irreplaceable metadata."""
    index = _load_index(output)
    if index is None:
        preserved = _load_preserved_index_metadata(output)
        index = _scan_corpus(output)
        if preserved is not None:
            # Partition provenance is reconstructed from marker files and must
            # never be replaced by the stale aggregate sidecar being salvaged.
            for name in ("bar_coverage", "session_calendar", "option_pins"):
                index[name] = dict(preserved.get(name) or {})
        _save_index(output, index)
        loaded = _load_index(output)
        if loaded is None:
            raise RuntimeError("recorder index recovery failed")
        return loaded
    if "recent_keys" in index:  # bounded legacy v1 cache
        _save_index(output, index)
        loaded = _load_index(output)
        if loaded is None:
            raise RuntimeError("recorder recent-key index migration failed")
        return loaded
    return index


def _existing_state(output: Path) -> tuple[RecentKeyIndex, datetime | None,
                                           dict[str, datetime]]:
    """Open disk-backed dedup state; the caller must close the first value."""
    index = _prepare_index(output)
    latest_bars = {symbol: _timestamp(value)
                   for symbol, value in index["latest_bars"].items()}
    return (RecentKeyIndex(_recent_key_index_path(output)),
            _timestamp(index.get("watermark")),
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


def _exact_recorded_session_bounds(
        index: dict, row: dict) -> tuple[datetime, datetime] | None:
    """Return valid persisted broker boundaries for the row's local day.

    A row can be outside those boundaries without the calendar being absent.
    Keeping that distinction explicit lets production recording discard known
    extended-hours observations while still failing closed when exact session
    metadata is genuinely unavailable.
    """
    stamp = _timestamp(row.get("timestamp"))
    if stamp is None:
        return None
    session = stamp.astimezone(NEW_YORK).date().isoformat()
    calendar = index.get("session_calendar")
    value = calendar.get(session) if isinstance(calendar, dict) else None
    if (not isinstance(value, dict) or
            value.get("source") != "alpaca_calendar"):
        return None
    opened = _timestamp(value.get("open"))
    closed = _timestamp(value.get("close"))
    if (opened is None or closed is None or opened >= closed or
            opened.astimezone(NEW_YORK).date().isoformat() != session or
            closed.astimezone(NEW_YORK).date().isoformat() != session):
        return None
    return opened, closed


def _has_exact_recorded_session(index: dict, row: dict) -> bool:
    return _exact_recorded_session_bounds(index, row) is not None


def _recorded_session_rows(index: dict, fetched_rows: list[dict], *,
                           require_exact_calendar: bool) -> list[dict]:
    """Keep regular-session rows and reject only absent exact provenance."""
    rows: list[dict] = []
    for row in fetched_rows:
        if _inside_recorded_session(
                index, row, require_exact_calendar=require_exact_calendar):
            rows.append(row)
            continue
        if require_exact_calendar:
            parsed = _timestamp(row.get("timestamp"))
            session = (_session_date(parsed) if parsed is not None else "unknown")
            bounds = _exact_recorded_session_bounds(index, row)
            if parsed is None or bounds is None:
                raise RuntimeError(
                    f"exact broker calendar metadata missing or invalid: {session}")
            opened, closed = bounds
            if opened <= parsed < closed:
                raise RuntimeError(
                    f"recorder row is invalid within exact broker session: {session}")
    return rows


def _next_exact_session_window(cursor: datetime, end: datetime,
                               maximum: timedelta,
                               calendar: CalendarCache) -> tuple[datetime, datetime] | None:
    """Return the next bounded RTH request, skipping nights and closed days."""
    probe = cursor
    while probe < end:
        day = probe.astimezone(NEW_YORK).date()
        if not calendar.known(day):
            raise RuntimeError(
                f"exact broker calendar metadata unavailable: {day.isoformat()}")
        session = calendar.session(day)
        if session is None:
            probe = datetime.combine(
                day + timedelta(days=1), datetime.min.time(),
                tzinfo=NEW_YORK).astimezone(timezone.utc)
            continue
        opened = session.open.astimezone(timezone.utc)
        closed = session.close.astimezone(timezone.utc)
        if probe < opened:
            probe = opened
        if probe >= closed:
            probe = datetime.combine(
                day + timedelta(days=1), datetime.min.time(),
                tzinfo=NEW_YORK).astimezone(timezone.utc)
            continue
        window_end = min(probe + maximum, closed, end)
        if window_end > probe:
            return probe, window_end
        break
    return None


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
        if fresh:
            _fsync_directory(path.parent)


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


def _ingest_chunk(output: Path, index: dict, recent_store: RecentKeyIndex,
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
    unique_keys: set[str] = set()
    recent_entries: list[tuple[str, str]] = []
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
        if key in unique_keys or recent_store.contains(key):
            continue
        unique_keys.add(key)
        unique_rows.append(row)
        recent_entries.append((key, parsed.isoformat()))
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
            # The corpus append happens first.  If the process dies before the
            # SQLite and JSON commits, partition sizes no longer match the
            # sidecars and recovery rebuilds them from the authoritative CSV.
            recent_store.add_many(recent_entries)
        # Persist even a duplicate-only response: the sidecar write is the
        # recorder's durable liveness signal, while the corpus remains unchanged.
        _save_index(output, index, recent_store=recent_store)
    return index, watermark, latest_bars, pins, len(unique_rows)


def record_once(provider: AlpacaProvider, symbols: list[str], output: Path,
                *, feed: str | None = None, config: dict | None = None,
                include_options: bool | None = None, option_limit: int = 5,
                option_hold: timedelta = timedelta(minutes=180),
                calendar: CalendarCache | None = None) -> int:
    with corpus_write_lock(output):
        return _record_once_locked(
            provider, symbols, output, feed=feed, config=config,
            include_options=include_options, option_limit=option_limit,
            option_hold=option_hold, calendar=calendar)


def _record_once_locked(provider: AlpacaProvider, symbols: list[str], output: Path,
                        *, feed: str | None = None,
                        config: dict | None = None,
                        include_options: bool | None = None,
                        option_limit: int = 5,
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
    index = _prepare_index(output)
    recent_store = RecentKeyIndex(_recent_key_index_path(output))
    try:
        return _record_once_with_index(
            provider, symbols, output, index, recent_store, now=now, feed=feed,
            config=config, include_options=include_options,
            option_limit=option_limit, option_hold=option_hold,
            calendar=calendar)
    finally:
        recent_store.close()


def _record_once_with_index(provider: AlpacaProvider, symbols: list[str],
                            output: Path, index: dict,
                            recent_store: RecentKeyIndex, *, now: datetime,
                            feed: str | None, config: dict | None,
                            include_options: bool, option_limit: int,
                            option_hold: timedelta,
                            calendar: CalendarCache | None) -> int:
    """Run one recorder cycle with an already validated disk-backed index."""
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
        if require_exact_calendar:
            request_window = _next_exact_session_window(
                cursor, now, window, calendar)
            if request_window is None:
                break
            cursor, window_end = request_window
        else:
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
        rows = _recorded_session_rows(
            index, fetched_rows,
            require_exact_calendar=require_exact_calendar)
        index, watermark, latest_bars, pins, unique = _ingest_chunk(
            output, index, recent_store, watermark, latest_bars, pins, rows, symbols,
            window_end, now, option_hold, horizon, calendar,
            feed=resolved_feed, bar_gap_policy=bar_gap_policy,
            bar_gap_maximum=bar_gap_maximum)
        total_unique += unique
        horizon = None if watermark is None else watermark - DEDUP_HORIZON
        if window_end >= now:
            break
        cursor = window_end

    if total_rows == 0:
        raise RuntimeError("Alpaca returned no point-in-time bars or quotes")
    return total_unique


def probe_market_data(provider: AlpacaProvider, symbols: list[str], *,
                      config: dict | None = None,
                      include_options: bool | None = None,
                      now: datetime | None = None) -> dict:
    """Prove configured recent-feed access without mutating the corpus."""
    symbols = [validate_equity_symbol(symbol) for symbol in symbols]
    if not symbols:
        raise ValueError("at least one US equity symbol is required")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("market-data probe time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    if include_options is None:
        classes = (config or {}).get("universe", {}).get("asset_classes", [])
        include_options = any(
            str(value).lower() in {"us_option", "option"}
            for value in classes)
    data_feed = _resolved_feed(provider, None, config)
    options_feed = _options_feed(config) if include_options else None
    rows = list(_rows(
        provider, symbols, current, feed=data_feed, config=config,
        include_options=bool(include_options), option_limit=1,
        start=current - timedelta(minutes=3), observed_at=current))
    event_counts: dict[str, int] = {}
    for row in rows:
        event = str(row.get("event_type") or "unknown")
        event_counts[event] = event_counts.get(event, 0) + 1
    return {
        "status": "probe_ok",
        "probe": True,
        "data_feed": data_feed,
        "options_feed": options_feed,
        "symbols": symbols,
        "rows": len(rows),
        "event_counts": event_counts,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="record Alpaca paper market data")
    p.add_argument("--out", default="runtime/research/recorded")
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--once", action="store_true")
    p.add_argument(
        "--probe", action="store_true",
        help=("verify recent configured market-data access (IEX equities; "
              "OPRA only for an explicitly enabled option lane) without "
              "writing corpus rows"))
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
    data_feed = _resolved_feed(provider, None, cfg)
    options_feed = _options_feed(cfg) if include_options else None
    if args.probe:
        try:
            payload = probe_market_data(
                provider, symbols, config=cfg, include_options=include_options)
        except Exception as exc:
            failure_kind, retryable = _market_data_failure(
                exc, data_feed=data_feed,
                options_feed=options_feed or "opra")
            payload = _save_status(output, {
                "status": "failed",
                "probe": True,
                "data_feed": data_feed,
                "options_feed": options_feed,
                "failure_kind": failure_kind,
                "retryable": retryable,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            print(json.dumps(payload, sort_keys=True), file=sys.stderr,
                  flush=True)
            return 2
        payload = _save_status(output, payload)
        print(json.dumps(payload, sort_keys=True), flush=True)
        return 0
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
            failure_kind, retryable = _market_data_failure(
                exc, data_feed=data_feed,
                options_feed=options_feed or "opra")
            payload = {
                "schema": "recorder-error.v1",
                "status": "failed",
                "probe": False,
                "data_feed": data_feed,
                "options_feed": options_feed,
                "failure_kind": failure_kind,
                "retryable": retryable,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "failure_count": failure_count,
                "retry_seconds": delay,
            }
            _save_status(output, payload)
            print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)
            if args.once:
                return 1
            time.sleep(delay)
            continue
        failure_count = 0
        _save_status(output, {
            "status": "recording",
            "probe": False,
            "data_feed": data_feed,
            "options_feed": options_feed,
            "rows": count,
            "failure_kind": None,
            "retryable": None,
            "error_type": None,
            "error": None,
        })
        print(f"recorded {count} Alpaca rows to {output}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
