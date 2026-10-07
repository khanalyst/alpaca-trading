#!/usr/bin/env python3
"""Build a bounded, diagnostic-only trader research projection.

The recorder corpus is append-only and can contain many more quote events than
the replay needs.  This module keeps every non-quote event (including bar
revisions and gaps), and keeps the quote events needed to reproduce a replay
boundary: the first and last quote in every market minute plus the latest
quote visible at every recorded bar availability cutoff.  Cost schedules are
measured from the unthinned quote stream before those rows are compacted.

The command intentionally has no promotion or broker-facing path.  It writes a
manifest only after all source identities have been checked again.  A failed
run may leave a ``failure.json`` receipt and temporary files, but never a
successful manifest.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import heapq
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Iterable, Iterator, Mapping, Sequence
import csv

# Running ``python deploy/trader_projection.py`` places only ``deploy`` on
# sys.path.  Resolve the repository root before importing package modules.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from deploy.research_dataset import (
    BAR_KINDS, OPTION_KINDS, QUOTE_KINDS, _apply_calendar,
    _config_and_sidecars, _csv_payload, _legacy_observation_inversion,
    _stat_identity, _validate_csv_headers,
)
from research.quote_costs import QuoteCostError, measure_quote_costs
from research.stressed_cost_calibration import calibrate_stressed_cost


SCHEMA = "trader-projection.v1"
MANIFEST_SCHEMA = "trader-projection-registration.v1"
UNAVAILABLE_SCHEDULE_SCHEMA = "quote-cost-schedule-unavailable.v1"
DEFAULT_MAX_RETAINED_ROWS = 1_000_000
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000_000
DEFAULT_DISK_FLOOR_BYTES = 5 * 1024 ** 3
DEFAULT_PARTITION_ROW_LIMIT = 100_000
DEFAULT_CHECK_INTERVAL_ROWS = 2_000
_UTC = timezone.utc


class ProjectionError(RuntimeError):
    """A projection was refused or failed before it could be sealed."""


class ProjectionBudgetError(ProjectionError):
    """A row, output, or disk budget would have been exceeded."""


class ProjectionSourceChangedError(ProjectionError):
    """A source CSV or sidecar changed during the diagnostic run."""


@dataclass(frozen=True)
class ProjectionLimits:
    max_retained_rows: int = DEFAULT_MAX_RETAINED_ROWS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    disk_floor_bytes: int = DEFAULT_DISK_FLOOR_BYTES
    partition_row_limit: int = DEFAULT_PARTITION_ROW_LIMIT
    check_interval_rows: int = DEFAULT_CHECK_INTERVAL_ROWS

    def __post_init__(self) -> None:
        for name in ("max_retained_rows", "max_output_bytes",
                     "disk_floor_bytes", "partition_row_limit",
                     "check_interval_rows"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=_UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(_UTC)


def _availability_time(row: Mapping[str, Any]) -> datetime | None:
    """Match live-shadow's point-in-time availability rule exactly."""
    timestamp = _parse_timestamp(row.get("timestamp"))
    if timestamp is None:
        return None
    as_of = _parse_timestamp(row.get("as_of") or row.get("timestamp"))
    observed = _parse_timestamp(
        row.get("observed_at") or row.get("as_of") or row.get("timestamp"))
    if as_of is None or observed is None:
        return None
    return max(timestamp, as_of, observed)


def _kind(row: Mapping[str, Any]) -> str:
    return str(row.get("kind") or row.get("event_type") or "").strip().lower()


def _is_quote(row: Mapping[str, Any]) -> bool:
    return _kind(row) in QUOTE_KINDS or _kind(row) in {
        "quote", "quote_snapshot", "equity_quote", "underlying_quote"}


def _row_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    # Three independent fields make ordering deterministic even when a legacy
    # export has no event key or contains two rows at the same timestamp.
    parsed = _parse_timestamp(row.get("timestamp"))
    stamp = parsed.isoformat() if parsed is not None else str(row.get("timestamp") or "")
    return (stamp, str(row.get("event_key") or ""),
            str(row.get("kind") or row.get("event_type") or ""))


def _event_id(row: Mapping[str, Any]) -> str:
    value = str(row.get("event_key") or "").strip()
    if value:
        return value
    body = json.dumps(dict(row), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return "digest:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _minute_key(row: Mapping[str, Any]) -> tuple[str, str]:
    symbol = str(row.get("symbol") or "")
    parsed = _parse_timestamp(row.get("timestamp"))
    if parsed is None:
        return symbol, str(row.get("timestamp") or "")
    return symbol, parsed.replace(second=0, microsecond=0).isoformat()


def _overlay_csv_fields(payload: dict[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Retain recorder fields omitted by the normalized compatibility view."""
    # _csv_payload intentionally has a narrow contract.  Projection identity
    # needs event_key, exact calendar fields, and revision/provenance columns,
    # so carry every named non-empty source field through as well.
    for key, value in raw.items():
        if key is not None and value not in (None, ""):
            payload.setdefault(str(key), value)
    if "kind" not in payload:
        payload["kind"] = str(raw.get("event_type") or "").strip().lower()
    return payload


def _normalized_rows(path: Path, *, csv_mode: str,
                     expected_header: Sequence[str], row_offset: int = 0
                     ) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield canonical CSV payloads while retaining omitted identity fields."""
    # _csv_rows is intentionally a tiny iterator wrapper around this exact
    # DictReader/_csv_payload conversion.  Repeating it here would read every
    # source twice and could observe two different append boundaries.  Keep the
    # same overflow/header behavior in one pass while calling _csv_payload.
    del expected_header
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for number, raw in enumerate(reader, 2):
            if None in raw:
                raw = {key: value for key, value in raw.items() if key is not None}
            payload = _csv_payload(raw, csv_mode=csv_mode)
            yield number + row_offset, _overlay_csv_fields(payload, raw)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "stat": {"st_dev": getattr(stat, "st_dev", None),
                 "st_ino": getattr(stat, "st_ino", None),
                 "st_size": getattr(stat, "st_size", None),
                 "st_mtime_ns": getattr(stat, "st_mtime_ns", None)},
        "sha256": _sha256(path),
    }


def _sidecar_paths(paths: Sequence[Path], recorded_root: Path | None) -> list[Path]:
    """Return only stable selected-session metadata inputs.

    Recorder status is a live heartbeat and must never be part of a sealed
    source binding.  Older recorder builds used ``market-DAY.csv.*`` markers;
    newer snapshots may expose ``market-DAY.*``.  Accept both spellings.
    """
    result: set[Path] = set()
    roots = {path.resolve().parent for path in paths}
    if recorded_root is not None:
        roots.add((Path(recorded_root) / "sessions").resolve())
    for path in paths:
        for suffix in (".calendar.json", ".source.json"):
            for base in (path.name, path.stem):
                for root in roots:
                    candidate = root / f"{base}{suffix}"
                    if candidate.is_file():
                        result.add(candidate.resolve())
    if recorded_root is not None:
        # The aggregate index is a fallback for selected calendar/source
        # entries.  Its identity is filtered below, so unrelated watermarks
        # may change while a long projection is running.
        for name in (".recorder-index.json",):
            candidate = Path(recorded_root) / name
            if candidate.is_file():
                result.add(candidate.resolve())
    return sorted(result)


def _selected_index_identity(path: Path, paths: Sequence[Path]) -> dict[str, Any]:
    """Bind only selected calendar/source entries from the mutable index."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"cannot read recorder index {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ProjectionError(f"recorder index {path} is not an object")
    selected_names = {item.name for item in paths}
    selected_days = {
        name.removeprefix("market-").removesuffix(".csv")
        for name in selected_names if name.startswith("market-")
    }
    calendar = payload.get("session_calendar")
    sources = payload.get("partition_sources")
    selected = {
        "session_calendar": ({day: calendar[day] for day in sorted(selected_days)
                              if isinstance(calendar, Mapping) and day in calendar}),
        "partition_sources": ({name: sources[name] for name in sorted(selected_names)
                               if isinstance(sources, Mapping) and name in sources}),
    }
    return {"path": str(path.resolve()), "kind": "selected-index",
            "sha256": _json_hash(selected), "selected": selected}


def _source_identities(paths: Sequence[Path], sidecars: Sequence[Path],
                       selected_paths: Sequence[Path]) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = [_identity(path) for path in paths]
    selected_set = {path.resolve() for path in selected_paths}
    for path in sidecars:
        if path.resolve() in selected_set and path.name == ".recorder-index.json":
            identities.append(_selected_index_identity(path, paths))
        else:
            identities.append(_identity(path))
    return identities


def _marker_overrides(paths: Sequence[Path], recorded_root: Path | None,
                      sidecars: Sequence[Path]) -> tuple[dict | None, dict | None]:
    """Load marker spellings not understood by the legacy dataset helper."""
    calendars: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, str]] = {}
    selected = {path.name for path in paths}
    for path in sidecars:
        if path.name == ".recorder-index.json":
            continue
        if path.name.endswith(".calendar.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ProjectionError(f"cannot read calendar sidecar {path}: {exc}") from exc
            partition = payload.get("partition") if isinstance(payload, Mapping) else None
            if (not isinstance(payload, Mapping) or
                    payload.get("schema") != "recorder-partition-calendar.v1" or
                    not isinstance(partition, str) or partition not in selected):
                raise ProjectionError(f"invalid calendar sidecar {path}")
            day = partition.removeprefix("market-").removesuffix(".csv")
            if payload.get("status") == "closed":
                value = {"status": "closed", "source": "alpaca_calendar"}
            else:
                value = {"open": payload.get("open"), "close": payload.get("close"),
                         "source": payload.get("source")}
            previous = calendars.get(day)
            if previous is not None and previous != value:
                raise ProjectionError(f"conflicting calendar sidecars for {day}")
            calendars[day] = value
        elif path.name.endswith(".source.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ProjectionError(f"cannot read source sidecar {path}: {exc}") from exc
            partition = payload.get("partition") if isinstance(payload, Mapping) else None
            if (not isinstance(payload, Mapping) or
                    payload.get("schema") != "recorder-partition-source.v1" or
                    not isinstance(partition, str) or partition not in selected):
                raise ProjectionError(f"invalid source sidecar {path}")
            sources[partition] = {"source_mode": payload.get("source_mode")}
    return (calendars or None), (sources or None)


def _discover_paths(corpus: Path | str | Sequence[Path | str],
                    selected_sessions: Sequence[str | Path] | None = None
                    ) -> list[Path]:
    if isinstance(corpus, (str, Path)):
        root = Path(corpus)
        if root.is_file():
            candidates = [root]
        else:
            sessions = root / "sessions" if (root / "sessions").is_dir() else root
            candidates = sorted(sessions.glob("market-*.csv"))
    else:
        candidates = [Path(item) for item in corpus]
    candidates = [path for path in candidates if path.is_file()]
    if selected_sessions:
        wanted = {Path(item).name for item in selected_sessions}
        candidates = [path for path in candidates if path.name in wanted or
                      path.stem in wanted]
    if not candidates:
        raise ValueError("corpus has no selected market CSV sessions")
    return sorted(candidates, key=lambda path: path.name)


def _recorded_root(corpus: Path | str | Sequence[Path | str],
                   paths: Sequence[Path]) -> Path | None:
    if not isinstance(corpus, (str, Path)):
        parent = paths[0].resolve().parent
    else:
        root = Path(corpus).resolve()
        parent = root.parent if root.is_file() else root
    if parent.name == "sessions":
        return parent.parent
    return parent


def _runtime_identity(config: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    broker = config.get("broker") if isinstance(config, Mapping) else None
    data = config.get("data") if isinstance(config, Mapping) else None
    provider = ((broker or {}).get("provider") if isinstance(broker, Mapping) else None)
    provider = provider or ((data or {}).get("provider") if isinstance(data, Mapping) else None)
    feed = ((broker or {}).get("data_feed") if isinstance(broker, Mapping) else None)
    feed = feed or ((data or {}).get("feed") if isinstance(data, Mapping) else None)
    return ((str(provider).strip().lower() if provider else None),
            (str(feed).strip().lower() if feed else None))


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _calibration_code_hash() -> str:
    paths = [Path(measure_quote_costs.__code__.co_filename),
             Path(calibrate_stressed_cost.__code__.co_filename)]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.resolve()).encode("utf-8"))
        digest.update(_sha256(path).encode("ascii"))
    return digest.hexdigest()


def _projection_code_hash() -> str:
    """Bind compaction and source-normalization implementation bytes."""
    paths = [Path(__file__).resolve(),
             Path(_config_and_sidecars.__code__.co_filename).resolve()]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode("utf-8"))
        digest.update(_sha256(path).encode("ascii"))
    return digest.hexdigest()


class _Compactor:
    """Bounded per-session quote maps with a carry-forward visible quote."""

    def __init__(self, staging: Path, limits: ProjectionLimits,
                 *, retained_base: int = 0,
                 carry: dict[str, tuple[datetime, dict[str, Any]]] | None = None
                 ) -> None:
        self.staging = staging
        self.limits = limits
        self.retained_base = int(retained_base)
        self.rows_seen = 0
        self.bars = 0
        self.quotes = 0
        self.options_skipped = 0
        self.quarantined = 0
        self.retained_estimate = 0
        self._part_rows = 0
        self._part_path: Path | None = None
        self._part_handle = None
        self._first_last: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._availability: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._cutoffs: dict[str, list[datetime]] = defaultdict(list)
        self._selected_by_partition: list[Path] = []
        self._carry = carry if carry is not None else {}
        self._global_quote_ids: set[str] = set()

    @property
    def selected_quote_count(self) -> int:
        return len(self._global_quote_ids)

    def begin_partition(self, path: Path) -> None:
        self._first_last.clear()
        self._availability.clear()
        self._cutoffs.clear()
        self._part_rows = 0
        self._part_path = self.staging / (path.name + ".jsonl")
        self._part_handle = self._part_path.open("w", encoding="utf-8")

    def _guard(self, extra: int = 1) -> None:
        if (self.retained_base + self.retained_estimate + extra
                > self.limits.max_retained_rows):
            raise ProjectionBudgetError(
                f"retained row budget exceeded before growth: "
                f"{self.retained_base}+{self.retained_estimate}+{extra}"
                f">{self.limits.max_retained_rows}")
        if self._part_rows + extra > self.limits.partition_row_limit:
            # This is a hard bound on simultaneously resident partition rows.
            # A large source partition must be explicitly selected/chunked by
            # the caller rather than silently creating an unbounded map.
            raise ProjectionBudgetError(
                f"partition row budget exceeded before growth: "
                f"{self._part_rows}+{extra}>{self.limits.partition_row_limit}")

    def add(self, row: dict[str, Any]) -> None:
        self.rows_seen += 1
        if self.rows_seen % self.limits.check_interval_rows == 0:
            _check_disk_floor(self.staging, self.limits.disk_floor_bytes)
        if not _is_quote(row):
            self._guard()
            assert self._part_handle is not None
            self._part_handle.write(json.dumps(row, sort_keys=True,
                                               ensure_ascii=False,
                                               separators=(",", ":")) + "\n")
            self._part_rows += 1
            self.retained_estimate += 1
            if _kind(row) in BAR_KINDS or _kind(row) in {"bar", "bar_1m"}:
                self.bars += 1
                available = _availability_time(row)
                if available is not None:
                    self._cutoffs[str(row.get("symbol") or "")].append(available)
            return

        self.quotes += 1
        stamp = _parse_timestamp(row.get("timestamp"))
        available = _availability_time(row)
        if stamp is None or available is None:
            # Keep malformed quotes in the staged stream so downstream
            # validation reports them; they cannot safely be compacted.
            self._guard()
            assert self._part_handle is not None
            self._part_handle.write(json.dumps(row, sort_keys=True,
                                               ensure_ascii=False,
                                               separators=(",", ":")) + "\n")
            self._part_rows += 1
            self.retained_estimate += 1
            return

        minute = _minute_key(row)
        existing = self._first_last.get(minute)
        if existing is None:
            self._guard(2)
            self._first_last[minute] = [row, row]
            self.retained_estimate += 2
        else:
            key = _row_sort_key(row)
            if key < _row_sort_key(existing[0]):
                existing[0] = row
            if key > _row_sort_key(existing[1]):
                existing[1] = row
        avail_key = available.isoformat()
        by_availability = self._availability[str(row.get("symbol") or "")]
        prior = by_availability.get(avail_key)
        if prior is None:
            self._guard()
            by_availability[avail_key] = row
            self.retained_estimate += 1
        elif _row_sort_key(row) > _row_sort_key(prior):
            by_availability[avail_key] = row

    def finish_partition(self) -> None:
        if self._part_handle is None or self._part_path is None:
            return
        selected: dict[str, dict[str, Any]] = {}
        for pair in self._first_last.values():
            for row in pair:
                selected[_event_id(row)] = row
        # Exact live-shadow boundary selection, including the quote visible
        # before the first current-partition quote (carry from prior session).
        for symbol, cutoffs in self._cutoffs.items():
            values = self._availability.get(symbol, {})
            ordered = sorted(values.items(), key=lambda item: item[0])
            cursor = 0
            latest = self._carry.get(symbol)
            for cutoff in sorted(set(cutoffs)):
                if latest is not None and latest[0] > cutoff:
                    latest = None
                while cursor < len(ordered):
                    available = _parse_timestamp(ordered[cursor][0])
                    if available is None or available > cutoff:
                        break
                    latest = (available, ordered[cursor][1])
                    cursor += 1
                if latest is not None:
                    selected[_event_id(latest[1])] = latest[1]
        for symbol, values in self._availability.items():
            if values:
                newest_key = max(values, key=lambda key: (
                    _parse_timestamp(key) or datetime.min.replace(tzinfo=_UTC),
                    _row_sort_key(values[key])))
                newest = values[newest_key]
                parsed = _parse_timestamp(newest_key)
                if parsed is not None:
                    self._carry[symbol] = (parsed, newest)

        for row in selected.values():
            self._part_handle.write(json.dumps(row, sort_keys=True,
                                               ensure_ascii=False,
                                               separators=(",", ":")) + "\n")
            self._global_quote_ids.add(_event_id(row))
        self._part_handle.flush()
        os.fsync(self._part_handle.fileno())
        self._part_handle.close()
        self._part_handle = None
        self._selected_by_partition.append(self._part_path)

    def staged_paths(self) -> list[Path]:
        return list(self._selected_by_partition)


def _check_disk_floor(path: Path, floor: int) -> None:
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        raise ProjectionError(f"cannot inspect free disk space: {exc}") from exc
    if free < floor:
        raise ProjectionBudgetError(
            f"free disk floor reached before growth: {free}<{floor}")


def _split_paths(paths: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    if len(paths) < 2:
        return list(paths), []
    split = max(1, min(len(paths) - 1, int(len(paths) * 0.70)))
    return list(paths[:split]), list(paths[split:])


def _iter_cost_rows(paths: Sequence[Path], *, csv_mode: str,
                    expected_header: Sequence[str], compactor: _Compactor,
                    calendar: dict | None, partition_sources: Mapping[str, Any] | None,
                    required: bool, trusted_recorder: bool,
                    row_offset: int = 0) -> Iterator[dict[str, Any]]:
    transformed = 0
    source_offset = row_offset
    for path in paths:
        rows_in_source = 0
        compactor.begin_partition(path)
        try:
            for source_row, payload in _normalized_rows(
                    path, csv_mode=csv_mode, expected_header=expected_header,
                    row_offset=source_offset):
                rows_in_source += 1
                if _legacy_observation_inversion(payload):
                    compactor.quarantined += 1
                    continue
                if _kind(payload) in OPTION_KINDS:
                    # This projection is the equity trader lane.  Exclude
                    # option snapshots before calendar/OPRA normalization so
                    # option provenance cannot contaminate equity evidence.
                    compactor.options_skipped += 1
                    continue
                transformed += 1
                if required or calendar is not None or partition_sources is not None:
                    if not _apply_calendar(
                            payload, row_number=transformed, calendar=calendar,
                            partition_sources=partition_sources, required=required,
                            trusted_recorder=trusted_recorder):
                        continue
                compactor.add(payload)
                yield payload
        finally:
            compactor.finish_partition()
        source_offset += rows_in_source


def _unavailable_schedule(reason: str, *, provider: str | None,
                          feed: str | None, seen: int = 0) -> dict[str, Any]:
    return {"schema": UNAVAILABLE_SCHEDULE_SCHEMA, "status": "unavailable",
            "reason": reason, "diagnostic_only": True, "authorizing": False,
            "provider": provider, "feed": feed, "quote_rows_seen": int(seen),
            "no_quotes": int(seen) == 0 or "no usable two-sided quotes" in reason}


def _measure_set(paths: Sequence[Path], *, csv_mode: str,
                 expected_header: Sequence[str], calendar: dict | None,
                 partition_sources: Mapping[str, Any] | None, required: bool,
                 trusted_recorder: bool, limits: ProjectionLimits,
                 provider: str | None, feed: str | None,
                 staging: Path, retained_base: int = 0,
                 carry: dict[str, tuple[datetime, dict[str, Any]]] | None = None
                 ) -> tuple[dict[str, Any], _Compactor]:
    compactor = _Compactor(staging, limits, retained_base=retained_base,
                           carry=carry)
    if not paths:
        return _unavailable_schedule("no_validation_sessions", provider=provider,
                                     feed=feed), compactor
    stream = _iter_cost_rows(
        paths, csv_mode=csv_mode, expected_header=expected_header,
        compactor=compactor, calendar=calendar,
        partition_sources=partition_sources, required=required,
        trusted_recorder=trusted_recorder)
    try:
        schedule = measure_quote_costs(stream, feed=feed, provider=provider,
                                       min_quotes_per_cell=500)
    except QuoteCostError as exc:
        message = str(exc)
        # An empty/no-usable quote set is an explicit diagnostic outcome.  A
        # provenance or feed mismatch is a source-integrity failure and must
        # stop the run rather than being hidden as "no quotes".
        if "no usable two-sided quotes" not in message:
            raise ProjectionError(message) from exc
        schedule = _unavailable_schedule(str(exc), provider=provider, feed=feed,
                                          seen=compactor.quotes)
    return schedule, compactor


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2,
                                     ensure_ascii=False, default=str) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _sort_stage(path: Path, runs_dir: Path, *, chunk_rows: int,
                limits: ProjectionLimits) -> list[Path]:
    runs: list[Path] = []
    chunk: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            chunk.append(json.loads(line))
            if len(chunk) >= chunk_rows:
                chunk.sort(key=_row_sort_key)
                run = runs_dir / f"{path.stem}-run-{len(runs):06d}.jsonl"
                with run.open("w", encoding="utf-8") as out:
                    for row in chunk:
                        out.write(json.dumps(row, sort_keys=True,
                                             ensure_ascii=False,
                                             separators=(",", ":")) + "\n")
                runs.append(run)
                chunk = []
                _check_disk_floor(runs_dir, limits.disk_floor_bytes)
    if chunk:
        chunk.sort(key=_row_sort_key)
        run = runs_dir / f"{path.stem}-run-{len(runs):06d}.jsonl"
        with run.open("w", encoding="utf-8") as out:
            for row in chunk:
                out.write(json.dumps(row, sort_keys=True,
                                     ensure_ascii=False,
                                     separators=(",", ":")) + "\n")
        runs.append(run)
    return runs


def _merge_runs(runs: Sequence[Path], output: Path, *, limits: ProjectionLimits) -> int:
    handles = [path.open(encoding="utf-8") for path in runs]
    heap: list[tuple[tuple[str, str, str], int, dict[str, Any]]] = []
    try:
        for index, handle in enumerate(handles):
            line = handle.readline()
            if line:
                row = json.loads(line)
                heapq.heappush(heap, (_row_sort_key(row), index, row))
        written = 0
        output_bytes = 0
        with output.open("w", encoding="utf-8") as target:
            while heap:
                _key, index, row = heapq.heappop(heap)
                encoded = (json.dumps(row, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")) + "\n").encode("utf-8")
                if output_bytes + len(encoded) > limits.max_output_bytes:
                    raise ProjectionBudgetError(
                        "output byte budget exceeded before writing next row")
                target.write(encoded.decode("utf-8"))
                output_bytes += len(encoded)
                written += 1
                line = handles[index].readline()
                if line:
                    next_row = json.loads(line)
                    heapq.heappush(heap, (_row_sort_key(next_row), index, next_row))
                if written % limits.check_interval_rows == 0:
                    _check_disk_floor(output, limits.disk_floor_bytes)
            target.flush()
            os.fsync(target.fileno())
        return written
    finally:
        for handle in handles:
            handle.close()


def project_corpus(
        corpus: Path | str | Sequence[Path | str],
        config: Path | Mapping[str, Any] | None,
        output: Path | str,
        *, selected_sessions: Sequence[str | Path] | None = None,
        csv_mode: str = "recorder",
        limits: ProjectionLimits | None = None) -> dict[str, Any]:
    """Create one bounded diagnostic projection and return its manifest."""
    limits = limits or ProjectionLimits()
    if csv_mode not in {"recorder", "external"}:
        raise ValueError("csv_mode must be recorder or external")
    paths = _discover_paths(corpus, selected_sessions)
    destination = Path(output)
    if destination.suffix.lower() == ".jsonl":
        compact_path = destination
        destination = destination.parent
    else:
        compact_path = destination / "compact.jsonl"
    destination.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="trader-projection-", dir=str(destination)))
    source_before: list[dict[str, Any]] = []
    source_after: list[dict[str, Any]] = []
    try:
        _check_disk_floor(work, limits.disk_floor_bytes)
        identities = {path: _identity(path) for path in paths}
        expected_header = _validate_csv_headers(
            paths, csv_mode=csv_mode, identities={path: _stat_identity(path)
                                                   for path in paths})
        recorded_root = _recorded_root(corpus, paths)
        required, calendar, partition_sources = _config_and_sidecars(
            config, recorded_root=recorded_root, from_recorder=True,
            source_paths=paths)
        sidecars = _sidecar_paths(paths, recorded_root)
        marker_calendar, marker_sources = _marker_overrides(
            paths, recorded_root, sidecars)
        if marker_calendar:
            calendar = {**(calendar or {}), **marker_calendar}
        if marker_sources:
            partition_sources = {**(partition_sources or {}), **marker_sources}
        source_before = _source_identities(paths, sidecars, sidecars)
        runtime_config: Mapping[str, Any] | None
        if isinstance(config, Mapping):
            runtime_config = config
        elif config is None:
            runtime_config = None
        else:
            # _config_and_sidecars has validated/loaded the path.  Load the
            # same normalized mapping for exact provider/feed binding too.
            repo_root = str(Path(__file__).resolve().parents[1])
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            from agent.config import load_config
            runtime_config = load_config(str(config))
        provider, feed = _runtime_identity(runtime_config)
        fit_paths, validation_paths = _split_paths(paths)
        fit_work = work / "fit"
        validation_work = work / "validation"
        fit_work.mkdir()
        validation_work.mkdir()
        fit_schedule, fit_compactor = _measure_set(
            fit_paths, csv_mode=csv_mode, expected_header=expected_header,
            calendar=calendar, partition_sources=partition_sources, required=required,
            trusted_recorder=True, limits=limits, provider=provider, feed=feed,
            staging=fit_work)
        validation_schedule, validation_compactor = _measure_set(
            validation_paths, csv_mode=csv_mode, expected_header=expected_header,
            calendar=calendar, partition_sources=partition_sources, required=required,
            trusted_recorder=True, limits=limits, provider=provider, feed=feed,
            staging=validation_work,
            retained_base=fit_compactor.retained_base + fit_compactor.retained_estimate,
            carry=fit_compactor._carry)
        fit_seen = fit_compactor.quotes
        validation_seen = validation_compactor.quotes
        if fit_schedule.get("schema") == "quote-cost-schedule.v1":
            try:
                calibration = calibrate_stressed_cost(
                    fit_schedule,
                    validation_schedule=(validation_schedule
                                         if validation_schedule.get("schema") ==
                                         "quote-cost-schedule.v1" else None),
                    validation_failure_reason=(None if validation_schedule.get("schema") ==
                                               "quote-cost-schedule.v1" else
                                               validation_schedule.get("reason")),
                    min_quotes_per_cell=500, min_sessions_per_cell=5,
                    expected_provider=provider, expected_feed=feed,
                    fallback_scenario_bps=25.0,
                    max_cost_to_risk_ratio=0.30)
            except Exception as exc:
                calibration = {"schema": "stressed-cost-calibration-unavailable.v1",
                               "status": "unavailable", "reason": str(exc),
                               "diagnostic_only": True, "authorizing": False,
                               "no_quotes": fit_seen + validation_seen == 0}
        else:
            calibration = {"schema": "stressed-cost-calibration-unavailable.v1",
                           "status": "unavailable", "reason": "no_quotes_in_fit",
                           "diagnostic_only": True, "authorizing": False,
                           "no_quotes": fit_seen + validation_seen == 0}

        all_stages = fit_compactor.staged_paths() + validation_compactor.staged_paths()
        runs_dir = work / "runs"
        runs_dir.mkdir()
        runs: list[Path] = []
        for stage in all_stages:
            runs.extend(_sort_stage(stage, runs_dir,
                                    chunk_rows=min(limits.partition_row_limit,
                                                   10_000), limits=limits))
        compact_path.parent.mkdir(parents=True, exist_ok=True)
        compact_rows = _merge_runs(runs, compact_path, limits=limits)
        _check_disk_floor(work, limits.disk_floor_bytes)

        _write_json(destination / "rawcostsfit.json", fit_schedule)
        _write_json(destination / "validation.json", validation_schedule)
        _write_json(destination / "calibration.json", calibration)
        # Keep explicit descriptive aliases for operators accustomed to the
        # cost-rerun artifact names; all are byte-for-byte diagnostic copies.
        _write_json(destination / "stressed-cost-calibration.json", calibration)
        sidecars_after = _sidecar_paths(paths, recorded_root)
        source_after = _source_identities(paths, sidecars_after, sidecars_after)
        if sidecars_after != sidecars or source_before != source_after:
            raise ProjectionSourceChangedError(
                "source CSV or sidecar changed during projection")
        artifact_names = ("rawcostsfit.json", "validation.json", "calibration.json")
        artifact_hashes = {
            name: _sha256(destination / name) for name in artifact_names}
        compact_bytes = compact_path.stat().st_size
        compact_sha256 = _sha256(compact_path)
        source_binding = {"csv": source_after[:len(paths)],
                          "sidecars": source_after[len(paths):],
                          "before": source_before,
                          "after": source_after,
                          "selected_sessions": [path.name for path in paths]}
        source_csv_hashes = {item["path"]: item["sha256"]
                             for item in source_after[:len(paths)]}
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "projection_schema": SCHEMA,
            "status": "complete",
            "diagnostic_only": True,
            "authorizing": False,
            "automatic_winner_selection": False,
            "scope": "compact_retrospective_diagnostic_not_tick_path_brokerproof",
            "compact_jsonl": str(compact_path.resolve()),
            "compact_bytes": compact_bytes,
            "compact_sha256": compact_sha256,
            "source_csv_binding": source_binding,
            "csv_source_binding": source_binding,
            "source_identity_before": source_before,
            "source_identity_after": source_after,
            "source_csv_count": len(paths),
            "source_csv_hashes": source_csv_hashes,
            "data_file_sha256": (_json_hash(source_csv_hashes)
                                  if len(source_csv_hashes) != 1
                                  else next(iter(source_csv_hashes.values()))),
            "source_rows": (fit_compactor.rows_seen + validation_compactor.rows_seen),
            "compact_rows": compact_rows,
            "bar_rows": fit_compactor.bars + validation_compactor.bars,
            "raw_quote_rows": fit_seen + validation_seen,
            "raw_quote_rows_fit": fit_seen,
            "raw_quote_rows_validation": validation_seen,
            "quote_cost_status": ("no_quotes" if fit_seen + validation_seen == 0
                                   else "measured"),
            "raw_quote_counts": {"fit": fit_seen,
                                 "validation": validation_seen,
                                 "total": fit_seen + validation_seen},
            "compact_quote_rows": len(
                fit_compactor._global_quote_ids | validation_compactor._global_quote_ids),
            "fit_sessions": [path.name for path in fit_paths],
            "validation_sessions": [path.name for path in validation_paths],
            "config_hash": _json_hash(runtime_config or {}),
            "projection_code_hash": _projection_code_hash(),
            "calibration_code_hash": _calibration_code_hash(),
            "artifacts": artifact_hashes,
            "limits": {name: getattr(limits, name) for name in (
                "max_retained_rows", "max_output_bytes", "disk_floor_bytes",
                "partition_row_limit", "check_interval_rows")},
            "provider": provider,
            "feed": feed,
            "quarantined_legacy_inversions": (
                fit_compactor.quarantined + validation_compactor.quarantined),
            "option_rows_skipped": (fit_compactor.options_skipped +
                                     validation_compactor.options_skipped),
        }
        manifest["manifest_hash"] = _json_hash(manifest)
        _write_json(destination / "manifest.json", manifest)
        _write_json(destination / "registration.json", manifest)
        return manifest
    except Exception as exc:
        failure = {"schema": "trader-projection-failure.v1", "status": "failed_partial",
                   "diagnostic_only": True, "authorizing": False,
                   "reason": str(exc), "source_csv_count": len(paths)}
        try:
            _write_json(destination / "failure.json", failure)
            (destination / "manifest.json").unlink(missing_ok=True)
            (destination / "registration.json").unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


# Small pure-ish helper for replay tests and callers that already own an
# in-memory row stream.  Production callers should use ``project_corpus`` so
# source binding, calendar normalization, and cost artifacts are sealed too.
def compact_rows(rows: Iterable[Mapping[str, Any]], *,
                 limits: ProjectionLimits | None = None) -> list[dict[str, Any]]:
    limits = limits or ProjectionLimits(disk_floor_bytes=1)
    with tempfile.TemporaryDirectory(prefix="trader-projection-rows-") as root:
        staging = Path(root)
        compactor = _Compactor(staging, limits)
        path = staging / "market-memory.csv"
        compactor.begin_partition(path)
        for raw in rows:
            compactor.add(dict(raw))
        compactor.finish_partition()
        result: list[dict[str, Any]] = []
        for stage in compactor.staged_paths():
            with stage.open(encoding="utf-8") as handle:
                result.extend(json.loads(line) for line in handle if line.strip())
        result.sort(key=_row_sort_key)
        return result


# Compatibility spellings make the selection primitive discoverable without
# coupling production callers to the private staging implementation.
compact_projection_rows = compact_rows
_compact_rows = compact_rows


# Descriptive aliases used by callers that call this operation a build/run.
build_projection = project_corpus
run = project_corpus
project = project_corpus


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True,
                        help="recorder root, sessions directory, or one CSV")
    parser.add_argument("--config", type=Path, required=True,
                        help="runtime config used for exact calendar/provenance")
    parser.add_argument("--output", type=Path, required=True,
                        help="output directory (or compact JSONL path)")
    parser.add_argument("--session", dest="sessions", action="append",
                        help="explicit market-YYYY-MM-DD.csv selection; repeatable")
    parser.add_argument("--csv-mode", choices=("recorder", "external"),
                        default="recorder")
    parser.add_argument("--max-retained-rows", type=int,
                        default=DEFAULT_MAX_RETAINED_ROWS)
    parser.add_argument("--max-output-bytes", type=int,
                        default=DEFAULT_MAX_OUTPUT_BYTES)
    parser.add_argument("--disk-floor-bytes", type=int,
                        default=DEFAULT_DISK_FLOOR_BYTES)
    args = parser.parse_args(argv)
    limits = ProjectionLimits(max_retained_rows=args.max_retained_rows,
                              max_output_bytes=args.max_output_bytes,
                              disk_floor_bytes=args.disk_floor_bytes)
    result = project_corpus(args.corpus, args.config, args.output,
                            selected_sessions=args.sessions,
                            csv_mode=args.csv_mode, limits=limits)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
