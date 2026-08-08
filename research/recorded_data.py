"""Small, deterministic event-time data plane for recorder CSVs.

This module intentionally uses only the Python standard library.  Recorder
CSV files remain the human-readable source; SQLite is an indexed, immutable
event view with explicit receipt/availability provenance.  Missing local
provenance is represented as ``legacy_unknown`` rather than inferred from a
file timestamp.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
EVENT_PLANE_SCHEMA = "event-plane.v1"
SCHEMA = EVENT_PLANE_SCHEMA
DEFAULT_DB = REPO / "runtime" / "research" / "market_events.db"
DEFAULT_RECORDED = REPO / "runtime" / "research" / "recorded"
DEFAULT_ARCHIVE = REPO / "runtime" / "research" / "recorded_archive"
MAX_QUERY_ROWS = 10_000

_PROVENANCE_FIELDS = {
    "observed_at", "available_at", "source", "feed", "schema",
    "revision_id", "quality",
}


def _canonical(value: object) -> str:
    if isinstance(value, dict):
        value = {str(key): value[key] for key in value}
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str, allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _integer(value: object, field: str, *, required: bool = False) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        # Do not accept decimal strings for timestamp identity.
        text = str(value).strip()
        if text.startswith(("+", "-")):
            digits = text[1:]
        else:
            digits = text
        if not digits.isdigit():
            raise ValueError
        return int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def initialize(path: str | Path = DEFAULT_DB) -> Path:
    """Create the event-plane schema if necessary and return its path."""
    path = Path(path)
    with closing(_connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_plane_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR REPLACE INTO event_plane_meta(key, value)
                VALUES ('schema', 'event-plane.v1');

            CREATE TABLE IF NOT EXISTS ingest_runs (
                ingest_id TEXT PRIMARY KEY,
                started_at_ms INTEGER NOT NULL,
                completed_at_ms INTEGER,
                status TEXT NOT NULL,
                source_root TEXT NOT NULL,
                snapshot_id TEXT,
                rows_valid INTEGER NOT NULL DEFAULT 0,
                rows_quarantined INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS market_events (
                event_id TEXT PRIMARY KEY,
                series TEXT NOT NULL,
                instrument TEXT,
                event_ts_ms INTEGER NOT NULL,
                observed_at_ms INTEGER,
                available_at_ms INTEGER,
                ingested_at_ms INTEGER NOT NULL,
                feed TEXT NOT NULL,
                schema TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_row INTEGER NOT NULL,
                quality TEXT NOT NULL,
                ingest_id TEXT NOT NULL,
                snapshot_id TEXT
            );
            CREATE INDEX IF NOT EXISTS market_events_lookup
                ON market_events(series, instrument, event_ts_ms,
                                 available_at_ms, event_id);
            CREATE INDEX IF NOT EXISTS market_events_asof
                ON market_events(series, instrument, feed, quality,
                                 available_at_ms, event_ts_ms, event_id);

            CREATE TABLE IF NOT EXISTS quarantine_diagnostics (
                diagnostic_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ingest_id TEXT NOT NULL,
                snapshot_id TEXT,
                source_file TEXT NOT NULL,
                source_row INTEGER NOT NULL,
                error TEXT NOT NULL,
                raw_json TEXT,
                diagnosed_at_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS quarantine_lookup
                ON quarantine_diagnostics(ingest_id, source_file, source_row);
            """)
        conn.commit()
    return path


def _stable_bytes(path: Path) -> bytes:
    """Read one CSV only if it did not change while being read."""
    for _ in range(3):
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if ((before.st_size, before.st_mtime_ns, before.st_ino)
                == (after.st_size, after.st_mtime_ns, after.st_ino)):
            return data
    raise RuntimeError(f"source changed repeatedly while reading: {path}")


def _source_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.csv") if p.is_file()
                  and not p.is_symlink())


def _archive_csvs(
        files: list[tuple[Path, bytes]], root: Path,
        archive_root: Path) -> str | None:
    """Archive changed blobs, then write one completed immutable manifest."""
    if not files:
        return None
    blobs = archive_root / "blobs"
    snapshots = archive_root / "snapshots"
    blobs.mkdir(parents=True, exist_ok=True)
    snapshots.mkdir(parents=True, exist_ok=True)

    current = []
    changed = False
    for path, data in files:
        digest = hashlib.sha256(data).hexdigest()
        relative = path.relative_to(root).as_posix()
        blob = blobs / f"{digest}.csv"
        if not blob.exists():
            temporary = blob.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(blob)
            changed = True
        current.append({
            "source_file": relative,
            "sha256": digest,
            "size_bytes": len(data),
            "blob": f"blobs/{blob.name}",
        })

    # Compare to the latest completed manifest. This avoids a growing tree of
    # duplicate manifests when the recorder is polled without changes.
    prior: dict[str, dict] = {}
    for manifest in sorted(snapshots.glob("*/manifest.json"), reverse=True):
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            prior = {item["source_file"]: item for item in value.get("files", [])}
            break
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not changed and all(prior.get(item["source_file"], {}).get("sha256")
                           == item["sha256"] for item in current):
        return None

    content_id = _hash({"schema": EVENT_PLANE_SCHEMA, "files": current})
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    snapshot_id += "-" + content_id[:16]
    destination = snapshots / snapshot_id
    destination.mkdir(parents=False, exist_ok=False)
    manifest = {
        "schema": EVENT_PLANE_SCHEMA,
        "snapshot_id": snapshot_id,
        "created_at_ms": _now_ms(),
        "source_root": str(root),
        "files": current,
    }
    # A manifest is the completion marker. Do not expose a partially written
    # manifest under the final name.
    temporary = destination / ".manifest.tmp"
    temporary.write_text(_canonical(manifest) + "\n", encoding="utf-8")
    temporary.replace(destination / "manifest.json")
    return snapshot_id


def _payload(row: dict[str, str]) -> dict:
    return {str(k): row.get(k, "") for k in sorted(row)
            if k not in _PROVENANCE_FIELDS}


def _normalize_row(
        series: str, row: dict[str, str], source_file: str, source_row: int,
        ingest_id: str, snapshot_id: str | None) -> dict:
    event_ts = _integer(row.get("ts"), "ts")
    if event_ts is None and series == "funding":
        event_ts = _integer(row.get("settlement_time"), "settlement_time")
    if event_ts is None:
        raise ValueError("ts is required")
    observed = _integer(row.get("observed_at"), "observed_at")
    available = _integer(row.get("available_at"), "available_at")
    if row.get("observed_at") not in (None, "") and observed is None:
        raise ValueError("observed_at must be an integer")
    if row.get("available_at") not in (None, "") and available is None:
        raise ValueError("available_at must be an integer")
    if (observed is not None and available is not None
            and available < observed):
        raise ValueError("available_at must be >= observed_at")
    payload = _payload(row)
    payload_hash = _hash(payload)
    instrument = str(row.get("inst_id") or row.get("ccy") or "") or None
    source = str(row.get("source") or "legacy")
    feed = str(row.get("feed") or "legacy")
    schema = str(row.get("schema") or "legacy_unknown")
    status = str(row.get("status") or "legacy_unknown")
    quality = str(row.get("quality") or (
        "observed" if observed is not None and available is not None
        else "legacy_unknown"))
    event_id = _hash({
        "series": series, "instrument": instrument,
        "event_ts_ms": event_ts, "source": source, "feed": feed,
        "schema": schema, "status": status, "payload_hash": payload_hash,
    })
    return {
        "event_id": event_id, "series": series, "instrument": instrument,
        "event_ts_ms": event_ts, "observed_at_ms": observed,
        "available_at_ms": available, "ingested_at_ms": _now_ms(),
        "feed": feed, "schema": schema, "source": source, "status": status,
        "payload_hash": payload_hash, "payload_json": _canonical(payload),
        "source_file": source_file, "source_row": source_row,
        "quality": quality, "ingest_id": ingest_id, "snapshot_id": snapshot_id,
    }


class EventPlane:
    """SQLite-backed event plane and strict as-of query surface."""

    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = initialize(path)

    def ingest(
            self, recorded_root: str | Path = DEFAULT_RECORDED, *,
            archive_root: str | Path | None = DEFAULT_ARCHIVE,
            batch_size: int = 500) -> dict:
        if isinstance(batch_size, bool) or not 1 <= int(batch_size) <= 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        batch_size = int(batch_size)
        root = Path(recorded_root)
        files: list[tuple[Path, bytes]] = []
        errors: list[tuple[Path, Exception]] = []
        for path in _source_files(root):
            try:
                files.append((path, _stable_bytes(path)))
            except Exception as exc:  # retain diagnostic and continue files
                errors.append((path, exc))
        snapshot_id = (_archive_csvs(files, root, Path(archive_root))
                       if archive_root is not None else None)
        ingest_id = uuid.uuid4().hex
        started = _now_ms()
        valid = quarantined = inserted = 0
        with closing(_connect(self.path)) as conn:
            conn.execute(
                "INSERT INTO ingest_runs(ingest_id,started_at_ms,status,source_root,snapshot_id)"
                " VALUES(?,?,?,?,?)",
                (ingest_id, started, "RUNNING", str(root), snapshot_id))
            conn.commit()
            pending = 0

            def quarantine(source: str, row_number: int, error: str,
                           raw: object = None) -> None:
                nonlocal quarantined, pending
                conn.execute(
                    "INSERT INTO quarantine_diagnostics(ingest_id,snapshot_id,source_file,"
                    "source_row,error,raw_json,diagnosed_at_ms) VALUES(?,?,?,?,?,?,?)",
                    (ingest_id, snapshot_id, source, row_number, error,
                     _canonical(raw) if raw is not None else None, _now_ms()))
                quarantined += 1
                pending += 1

            for path, data in files:
                relative = path.relative_to(root).as_posix()
                try:
                    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
                    if not reader.fieldnames or "ts" not in reader.fieldnames:
                        raise ValueError("CSV header must include ts")
                    for number, row in enumerate(reader, 2):
                        if not row or not any(value not in (None, "")
                                              for value in row.values()):
                            quarantine(relative, number, "empty row", row)
                            continue
                        if None in row:
                            quarantine(relative, number,
                                       "unexpected extra CSV fields", row)
                            continue
                        try:
                            event = _normalize_row(
                                path.parent.name, row, relative, number,
                                ingest_id, snapshot_id)
                        except (TypeError, ValueError) as exc:
                            quarantine(relative, number, str(exc), row)
                            continue
                        conn.execute(
                            "INSERT OR IGNORE INTO market_events(" +
                            ",".join(event) + ") VALUES(" +
                            ",".join("?" for _ in event) + ")",
                            tuple(event.values()))
                        valid += 1
                        inserted += max(0, int(conn.execute(
                            "SELECT changes()" ).fetchone()[0]))
                        pending += 1
                        if pending >= batch_size:
                            conn.commit()
                            pending = 0
                except (UnicodeDecodeError, csv.Error, ValueError) as exc:
                    quarantine(relative, 1, str(exc))
            for path, exc in errors:
                quarantine(path.relative_to(root).as_posix(), 0, str(exc))
            if pending:
                conn.commit()
            conn.execute(
                "UPDATE ingest_runs SET completed_at_ms=?,status=?,rows_valid=?,"
                "rows_quarantined=? WHERE ingest_id=?",
                (_now_ms(), "COMPLETED", valid, quarantined, ingest_id))
            conn.commit()
        return {
            "schema": EVENT_PLANE_SCHEMA, "ingest_id": ingest_id,
            "snapshot_id": snapshot_id, "files": len(files),
            "rows_valid": valid, "rows_inserted": inserted,
            "rows_quarantined": quarantined,
            "db_path": str(self.path),
        }

    def query(self, *, series: str, instrument: str | None = None,
              decision_time_ms: int, cutoff_event_ts_ms: int | None = None,
              feed: str | None = None, quality: str = "observed",
              start_event_ts_ms: int | None = None,
              end_event_ts_ms: int | None = None, window_ms: int | None = None,
              limit: int = 1000, status: str | None = None) -> list[dict]:
        decision = _integer(decision_time_ms, "decision_time_ms", required=True)
        cutoff = (_integer(cutoff_event_ts_ms, "cutoff_event_ts_ms")
                   if cutoff_event_ts_ms is not None else decision)
        if window_ms is not None:
            window = _integer(window_ms, "window_ms", required=True)
            if window <= 0:
                raise ValueError("window_ms must be positive")
            start_event_ts_ms = max(start_event_ts_ms or 0, cutoff - window)
        if start_event_ts_ms is not None:
            start_event_ts_ms = _integer(start_event_ts_ms, "start_event_ts_ms")
        if end_event_ts_ms is not None:
            end_event_ts_ms = _integer(end_event_ts_ms, "end_event_ts_ms")
        limit = int(limit)
        if not 1 <= limit <= MAX_QUERY_ROWS:
            raise ValueError(f"limit must be between 1 and {MAX_QUERY_ROWS}")
        clauses = ["series=?", "quality=?", "available_at_ms IS NOT NULL",
                   "available_at_ms<=?", "event_ts_ms<=?"]
        params: list[object] = [series, quality, decision, cutoff]
        if quality == "observed":
            clauses.extend(["observed_at_ms IS NOT NULL", "observed_at_ms<=?"])
            params.append(decision)
        if instrument is not None:
            clauses.append("instrument=?")
            params.append(instrument)
        if feed is not None:
            clauses.append("feed=?")
            params.append(feed)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if start_event_ts_ms is not None:
            clauses.append("event_ts_ms>=?")
            params.append(start_event_ts_ms)
        if end_event_ts_ms is not None:
            clauses.append("event_ts_ms<=?")
            params.append(end_event_ts_ms)
        params.append(limit)
        with closing(_connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT * FROM market_events WHERE " + " AND ".join(clauses) +
                " ORDER BY event_ts_ms DESC, available_at_ms DESC, "
                "observed_at_ms DESC, event_id ASC LIMIT ?", params).fetchall()
        return [dict(row) for row in rows]

    def query_asof(self, **kwargs) -> dict:
        rows = self.query(**kwargs)
        digest = _hash([row["event_id"] for row in rows])
        return {
            "events": rows,
            "reason": None if rows else "missing_data",
            "event_join_digest": digest,
        }

    def as_of(self, **kwargs) -> dict:
        kwargs.pop("limit", None)
        result = self.query_asof(limit=1, **kwargs)
        event = result["events"][0] if result["events"] else None
        return {
            "event": event,
            "reason": result["reason"],
            "event_join_digest": result["event_join_digest"],
        }

    def join_asof(self, requests: list[dict], *, decision_time_ms: int,
                  cutoff_event_ts_ms: int | None = None,
                  window_ms: int | None = None) -> dict:
        if not isinstance(requests, list) or not requests:
            raise ValueError("requests must be a non-empty list")
        selected = []
        for index, request in enumerate(requests):
            if not isinstance(request, dict) or not request.get("series"):
                raise ValueError(f"request {index} must include series")
            result = self.as_of(
                series=request["series"], instrument=request.get("instrument"),
                feed=request.get("feed"), quality=request.get("quality", "observed"),
                status=request.get("status"),
                decision_time_ms=decision_time_ms,
                cutoff_event_ts_ms=cutoff_event_ts_ms,
                start_event_ts_ms=request.get("start_event_ts_ms"),
                end_event_ts_ms=request.get("end_event_ts_ms"),
                window_ms=request.get("window_ms", window_ms))
            selected.append({
                "key": request.get("key", str(index)),
                "series": request["series"],
                "instrument": request.get("instrument"),
                "event": result["event"],
                "reason": result["reason"],
            })
        digest = _hash([{
            "key": item["key"],
            "event_id": (item["event"] or {}).get("event_id"),
            "reason": item["reason"],
        } for item in sorted(selected, key=lambda item: str(item["key"]))])
        return {"events": selected, "event_join_digest": digest,
                "reason": None if all(item["event"] for item in selected)
                else "missing_data"}


def ingest_recorder_csvs(
        recorded_root: str | Path = DEFAULT_RECORDED, *,
        db_path: str | Path = DEFAULT_DB,
        archive_root: str | Path | None = DEFAULT_ARCHIVE,
        batch_size: int = 500) -> dict:
    """Convenience wrapper used by schedulers and tests."""
    return EventPlane(db_path).ingest(
        recorded_root, archive_root=archive_root, batch_size=batch_size)


def strict_asof(db_path: str | Path = DEFAULT_DB, **kwargs) -> dict:
    return EventPlane(db_path).as_of(**kwargs)


def ingest(*args, **kwargs) -> dict:
    """Short alias retained for scheduler integrations."""
    return ingest_recorder_csvs(*args, **kwargs)


def query_asof(db_path: str | Path = DEFAULT_DB, **kwargs) -> dict:
    return EventPlane(db_path).query_asof(**kwargs)


def join_asof(db_path: str | Path = DEFAULT_DB,
              requests: list[dict] | None = None, **kwargs) -> dict:
    return EventPlane(db_path).join_asof(requests or [], **kwargs)


def event_join_digest(events: list[dict | None]) -> str:
    """Return the stable digest used to bind a replay/join to event IDs."""
    return _hash([(event or {}).get("event_id") if event else None
                  for event in events])


__all__ = [
    "EVENT_PLANE_SCHEMA", "SCHEMA", "DEFAULT_DB", "DEFAULT_RECORDED",
    "DEFAULT_ARCHIVE", "EventPlane", "initialize", "ingest_recorder_csvs",
    "ingest", "query_asof", "strict_asof", "join_asof", "event_join_digest",
]
