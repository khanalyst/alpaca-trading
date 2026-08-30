#!/usr/bin/env python3
"""Build temporary research views from an append-only market corpus.

The recorder can expose either one legacy corpus file or a directory of
session partitions.  This module deliberately streams the selected source
files: it never creates a merged input and never retains the corpus in
memory.  The normalized row is serialized once and that exact serialization
is used for every applicable output projection.

Recorder releases before the post-fetch observation fix could write otherwise
well-formed rows whose ``as_of`` timestamp is later than ``observed_at``.
Those rows remain in the source corpus but are quarantined from temporary
research views and reported explicitly.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo


BAR_KINDS = {"bar", "bar_1m", "underlying", "underlying_bar"}
QUOTE_KINDS = {"quote", "quote_snapshot", "equity_quote", "underlying_quote"}
OPTION_KINDS = {"option", "option_snapshot", "option_quote"}
_PARTITION_NAME = re.compile(r"market-(\d{4}-\d{2}-\d{2})\.csv\Z")
_NEW_YORK = ZoneInfo("America/New_York")


def apply_partition_source(payload: dict, session_day: str,
                           partition_sources: Mapping[str, object] | None,
                           *, row_number: int | None = None) -> dict:
    """Annotate a temporary view from recorder sidecar source metadata."""
    if not isinstance(partition_sources, Mapping):
        return payload
    source = partition_sources.get(f"market-{session_day}.csv")
    if source is None:
        return payload
    label = f"row {row_number}" if row_number is not None else "row"
    if not isinstance(source, Mapping):
        raise ValueError(f"source metadata for {session_day} is malformed")
    source_mode = str(source.get("source_mode") or "").strip().lower()
    if source_mode != "historical_backfill":
        raise ValueError(f"source metadata for {session_day} is unsupported")
    row_mode = str(payload.get("source_mode") or "").strip().lower()
    if row_mode and row_mode != source_mode:
        raise ValueError(
            f"{label} conflicts with recorder source metadata for {session_day}")
    payload["source_mode"] = source_mode
    return payload


def _clean(value):
    return None if value in (None, "") else value


def _csv_payload(row: Mapping[str, object], *, csv_mode: str = "recorder") -> dict:
    """Convert one CSV row using the recorder or external CSV contract.

    ``external`` intentionally retains every named, non-empty CSV field.  It
    matches the historical shell conversion, including retaining
    ``event_type`` and adding a lower-case ``kind`` projection.  Recorder CSV
    has a stable schema and is projected to the fields consumed by research.
    """
    event = str(row.get("event_type") or "").lower()
    if csv_mode == "external":
        item = {
            key: value for key, value in row.items()
            if key is not None and value not in (None, "")
        }
        kinds = {
            "bar": "bar", "bar_1m": "bar", "quote": "quote",
            "quote_snapshot": "quote", "option": "option_snapshot",
            "option_snapshot": "option_snapshot",
            "option_quote": "option_snapshot",
        }
        item["kind"] = kinds.get(event, event)
        return item

    common = {
        # Preserve provenance exactly as recorded.  A missing provider/feed is
        # an integrity error for research and must remain visible to
        # ``validate-data``; silently selecting Alpaca/IEX would turn a
        # partial external row into apparently executable evidence.
        "provider": _clean(row.get("provider")),
        "feed": _clean(row.get("feed")),
        "source_mode": _clean(row.get("source_mode")),
        "symbol": _clean(row.get("symbol")),
        "timestamp": _clean(row.get("timestamp")),
        "observed_at": _clean(row.get("observed_at") or row.get("timestamp")),
        "as_of": _clean(row.get("as_of") or row.get("timestamp")),
    }
    if event in {"bar", "bar_1m"}:
        return {
            "kind": "bar", **common,
            "open": _clean(row.get("open")),
            "high": _clean(row.get("high")),
            "low": _clean(row.get("low")),
            "close": _clean(row.get("close")),
            "volume": _clean(row.get("volume")),
        }
    if event == "quote":
        return {
            "kind": "quote", **common,
            "bid": _clean(row.get("bid")),
            "ask": _clean(row.get("ask")),
            "bid_size": _clean(row.get("bid_size")),
            "ask_size": _clean(row.get("ask_size")),
        }
    if event in OPTION_KINDS:
        return {
            "kind": "option_snapshot", **common,
            "contract": _clean(row.get("contract")),
            "underlying": _clean(row.get("underlying")),
            "expiration": _clean(row.get("expiration")),
            "strike": _clean(row.get("strike")),
            "right": _clean(row.get("right")),
            "multiplier": _clean(row.get("multiplier")),
            "bid": _clean(row.get("bid")),
            "ask": _clean(row.get("ask")),
            "last": _clean(row.get("last")),
            "bid_size": _clean(row.get("bid_size")),
            "ask_size": _clean(row.get("ask_size")),
            "volume": _clean(row.get("volume")),
            "open_interest": _clean(row.get("open_interest")),
            "underlying_price": _clean(row.get("underlying_price")),
        }
    # Preserve unknown recorder event types so validate-data names them instead
    # of silently dropping evidence from the cycle.
    return {"kind": event, **common}


def _timestamp(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        # Validation owns malformed timestamps. Returning None deliberately
        # keeps the row in the temporary dataset so it remains a hard failure.
        return None


def _legacy_observation_inversion(payload: Mapping[str, object]) -> bool:
    as_of = _timestamp(payload.get("as_of"))
    observed = _timestamp(payload.get("observed_at"))
    return as_of is not None and observed is not None and as_of > observed


def _stat_identity(path: Path) -> tuple[object, ...]:
    """Return the source identity used to detect replacement/append/mutation."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"cannot stat source {path}: {exc}") from exc
    # Device/inode detect replacement; size and mtime_ns detect appends and
    # ordinary in-place edits.  ``getattr`` keeps this friendly to test fakes
    # and non-POSIX stat implementations.
    return (
        getattr(stat, "st_dev", None), getattr(stat, "st_ino", None),
        getattr(stat, "st_size", None), getattr(stat, "st_mtime_ns", None),
    )


def _partition_paths(root: Path, session_window: int) -> list[Path]:
    if session_window < 0:
        raise ValueError("session window must be nonnegative")
    try:
        candidates = []
        for path in root.iterdir():
            if not path.is_file():
                continue
            match = _PARTITION_NAME.fullmatch(path.name)
            if match is None:
                continue
            try:
                date.fromisoformat(match.group(1))
            except ValueError:
                continue
            candidates.append(path)
    except OSError as exc:
        raise ValueError(f"cannot list partition root {root}: {exc}") from exc
    candidates.sort(key=lambda path: path.name)
    if session_window:
        candidates = candidates[-session_window:]
    if not candidates:
        raise ValueError(
            f"no market-YYYY-MM-DD.csv partitions found under {root}")
    return candidates


def _source_paths(source: Path | Sequence[Path] | Iterable[Path] | None,
                  *, partition_root: Path | None,
                  session_window: int) -> list[Path]:
    if partition_root is not None:
        if source is not None:
            raise ValueError("source and partition root are mutually exclusive")
        return _partition_paths(Path(partition_root), session_window)
    if source is None:
        raise ValueError("a source or partition root is required")
    if isinstance(source, (str, Path)):
        return [Path(source)]
    try:
        paths = [Path(item) for item in source]
    except (TypeError, ValueError) as exc:
        raise ValueError("source must be a path or ordered paths") from exc
    if not paths:
        raise ValueError("source must contain at least one path")
    return paths


def _partition_calendar_sidecars(source_paths: Sequence[Path],
                                 recorded_root: Path | None) -> dict | None:
    """Merge exact calendar markers across all selected old partitions.

    Older recorder indexes may only contain the newest partition's metadata.
    Reading one tiny marker per selected partition avoids a corpus-sized
    migration and gives research the same exact early-close boundaries.  A
    malformed marker is a hard error; guessing a regular 16:00 close would
    contaminate replay.
    """
    roots = []
    if recorded_root is not None:
        roots.append(Path(recorded_root) / "sessions")
    roots.extend(path.parent for path in source_paths)
    roots = list(dict.fromkeys(root.resolve() for root in roots
                               if root.is_dir()))
    result: dict[str, dict] = {}
    sidecars: set[Path] = set()
    for source in source_paths:
        if _PARTITION_NAME.fullmatch(source.name) is None:
            continue
        for root in roots:
            candidate = root / f"{source.name}.calendar.json"
            if candidate.is_file():
                sidecars.add(candidate)
    for path in sorted(sidecars):
        root = path.parent
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read exact calendar sidecar {path}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != \
                "recorder-partition-calendar.v1":
            raise ValueError(f"invalid exact calendar sidecar {path}")
        partition = payload.get("partition")
        if not isinstance(partition, str) or path.name != partition + ".calendar.json":
            raise ValueError(f"invalid exact calendar sidecar {path}")
        match = _PARTITION_NAME.fullmatch(partition)
        if match is None:
            raise ValueError(f"invalid exact calendar sidecar {path}")
        # A marker is written before a partition during backfill.  A crash in
        # that narrow window leaves a harmless orphan; do not let an old
        # sidecar supply calendar metadata to a replacement partition.
        if not (root / partition).is_file():
            continue
        day = match.group(1)
        opened = _calendar_timestamp(payload.get("open"))
        closed = _calendar_timestamp(payload.get("close"))
        if (opened is None or closed is None or opened >= closed or
                opened.astimezone(_NEW_YORK).date().isoformat() != day or
                closed.astimezone(_NEW_YORK).date().isoformat() != day or
                payload.get("source") != "alpaca_calendar"):
            raise ValueError(f"invalid exact calendar sidecar {path}")
        value = {"open": opened.isoformat(), "close": closed.isoformat(),
                 "source": "alpaca_calendar"}
        previous = result.get(day)
        if previous is not None and previous != value:
            raise ValueError(f"conflicting exact calendar sidecars for {day}")
        result[day] = value
    return result or None


def _validate_csv_headers(paths: Sequence[Path], *, csv_mode: str,
                          identities: Mapping[Path, tuple[object, ...]]) -> list[str]:
    del csv_mode  # Kept in the helper signature to document the CSV contract.
    expected: list[str] | None = None
    for path in paths:
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                fields = next(reader, None)
        except (OSError, csv.Error) as exc:
            raise ValueError(f"cannot read CSV source {path}: {exc}") from exc
        if fields is None or any(field is None for field in fields):
            raise ValueError(f"CSV source {path} has no valid header")
        current = [str(field) for field in fields]
        if expected is None:
            expected = current
        elif current != expected:
            raise ValueError(f"CSV headers do not match: {path}")
        if _stat_identity(path) != identities[path]:
            raise ValueError(f"source mutated during header validation: {path}")
    return expected or []


def _jsonl_rows(path: Path) -> Iterator[tuple[int, dict]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: expected an object")
            yield number, value


def _csv_rows(path: Path, *, csv_mode: str,
              expected_header: Sequence[str], row_offset: int = 0
              ) -> Iterator[tuple[int, dict]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        # Headers were preflighted for every selected source. DictReader now
        # consumes only this source's rows, without buffering a partition.
        for number, row in enumerate(reader, 2):
            # The legacy external conversion dropped DictReader's overflow
            # bucket. Preserve that behavior while retaining all named fields.
            if None in row:
                row = {key: value for key, value in row.items() if key is not None}
            # Partition rows retain the one-header merged-corpus numbering
            # used by the legacy quarantine report while remaining streamed.
            yield number + row_offset, _csv_payload(row, csv_mode=csv_mode)


def _config_and_sidecars(agent_config: Path | Mapping[str, object] | None,
                         *, recorded_root: Path | None,
                         from_recorder: bool,
                         source_paths: Sequence[Path]) -> tuple[bool, dict | None,
                                                                 dict | None]:
    """Load exact-calendar policy and optional recorder sidecars."""
    if agent_config is None:
        return False, None, None
    try:
        if isinstance(agent_config, Mapping):
            config = dict(agent_config)
        else:
            # When invoked as ``python /repo/deploy/research_dataset.py`` the
            # interpreter puts only ``deploy`` on sys.path.  Resolve the
            # repository root so the same agent.config loader works for the
            # shell entry point and for package imports.
            repo_root = str(Path(__file__).resolve().parents[1])
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            from agent.config import load_config
            config = load_config(str(agent_config))
    except Exception as exc:
        raise ValueError(f"configuration validation failed: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("configuration validation failed: expected an object")
    session = config.get("session")
    required = (bool(session.get("require_exact_calendar", True))
                if isinstance(session, dict) else True)

    calendar = None
    partition_sources = None
    if from_recorder:
        sidecar_root = Path(recorded_root) if recorded_root is not None else None
        if sidecar_root is None and source_paths:
            sidecar_root = source_paths[0].parent
        sidecar = (sidecar_root / ".recorder-index.json"
                   if sidecar_root is not None else None)
        if sidecar is not None and not sidecar.is_file() and source_paths:
            candidate = source_paths[0].parent / ".recorder-index.json"
            if candidate.is_file():
                sidecar = candidate
        if sidecar is not None and sidecar.is_file():
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    value = payload.get("session_calendar")
                    calendar = value if isinstance(value, dict) else None
                    value = payload.get("partition_sources")
                    partition_sources = value if isinstance(value, dict) else None
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # This matches the cycle's fail-closed behavior when exact
                # metadata is required; optional mode can still process rows.
                calendar = None
                partition_sources = None
        marker_calendar = _partition_calendar_sidecars(source_paths, sidecar_root)
        if marker_calendar:
            calendar = {**(calendar or {}), **marker_calendar}
    return required, calendar, partition_sources


def _calendar_timestamp(value) -> datetime | None:
    """Parse calendar metadata exactly as the cycle's inline converter does."""
    if value in (None, ""):
        return None
    try:
        raw = str(value)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _apply_calendar(payload: dict, *, row_number: int,
                    calendar: dict | None,
                    partition_sources: Mapping[str, object] | None,
                    required: bool) -> None:
    stamp = _calendar_timestamp(payload.get("timestamp"))
    if stamp is None:
        if required:
            raise ValueError(
                f"row {row_number} has no timestamp for exact calendar validation")
        return
    day = stamp.astimezone(_NEW_YORK).date().isoformat()
    apply_partition_source(payload, day, partition_sources, row_number=row_number)

    opened = _calendar_timestamp(payload.get("session_open"))
    closed = _calendar_timestamp(payload.get("session_close"))
    if (opened is None) != (closed is None):
        raise ValueError(f"row {row_number} has only one exact session boundary")
    side = calendar.get(day) if isinstance(calendar, dict) else None
    side_open = _calendar_timestamp(side.get("open")) if isinstance(side, dict) else None
    side_close = _calendar_timestamp(side.get("close")) if isinstance(side, dict) else None
    if side is not None and (side_open is None or side_close is None):
        raise ValueError(f"calendar metadata for {day} is malformed")
    if opened is None and side_open is not None:
        opened, closed = side_open, side_close
        payload["session_open"] = opened.isoformat()
        payload["session_close"] = closed.isoformat()
    if opened is None:
        if required:
            raise ValueError(f"exact broker calendar metadata missing for {day}")
        return
    if (opened >= closed or
            opened.astimezone(_NEW_YORK).date().isoformat() != day or
            closed.astimezone(_NEW_YORK).date().isoformat() != day):
        raise ValueError(f"conflicting exact broker calendar metadata for {day}")
    if side_open is not None and (opened != side_open or closed != side_close):
        raise ValueError(f"row {row_number} conflicts with recorder calendar for {day}")
    if not opened <= stamp < closed:
        raise ValueError(f"row {row_number} is outside exact broker session {day}")


def _selected_set(selected_vehicles: str | Iterable[object] | None) -> set[str] | None:
    if selected_vehicles is None:
        return None
    if isinstance(selected_vehicles, str):
        return {item for item in selected_vehicles.split() if item}
    return {str(item) for item in selected_vehicles if str(item)}


def _option_observation_fix(payload: dict, *, row_number: int) -> None:
    feed = str(payload.get("feed") or "").strip().lower()
    if feed != "opra":
        raise ValueError(
            f"option row {row_number} has non-executable feed "
            f"{feed or '[missing]'}; OPRA is required")
    observed = _timestamp(payload.get("observed_at"))
    as_of = _timestamp(payload.get("as_of"))
    if observed is not None and (as_of is None or as_of < observed):
        payload["as_of"] = payload["observed_at"]


def build_views(
        source: Path | Sequence[Path] | Iterable[Path] | None = None, *,
        input_format: str | None = None,
        normalized: Path | None = None,
        bars: Path | None = None,
        quotes: Path | None = None,
        options: Path | None = None,
        replay: Path | None = None,
        partition_root: Path | None = None,
        session_window: int = 0,
        csv_mode: str = "recorder",
        selected_vehicles: str | Iterable[object] | None = None,
        agent_config: Path | Mapping[str, object] | None = None,
        recorded_root: Path | None = None,
        from_recorder: bool = False) -> dict:
    """Stream a source into normalized, vehicle, and replay projections.

    ``source`` remains a single :class:`~pathlib.Path` in the legacy API, but
    an ordered iterable of CSV paths is accepted for partition callers.
    ``partition_root`` selects strict ``market-YYYY-MM-DD.csv`` files in name
    order and, when nonzero, retains only the latest ``session_window``.
    """
    if normalized is None or bars is None or options is None:
        raise ValueError("normalized, bars, and options outputs are required")
    if session_window < 0:
        raise ValueError("session window must be nonnegative")
    if csv_mode not in {"recorder", "external"}:
        raise ValueError("csv mode must be recorder or external")
    paths = _source_paths(source, partition_root=partition_root,
                          session_window=session_window)
    if input_format is None:
        input_format = "csv" if partition_root is not None or any(
            path.suffix.lower() == ".csv" for path in paths) else "jsonl"
    if input_format not in {"csv", "jsonl"}:
        raise ValueError("input format must be csv or jsonl")
    if input_format == "jsonl" and len(paths) != 1:
        raise ValueError("multiple sources require CSV format")

    identities = {path: _stat_identity(path) for path in paths}
    expected_header: list[str] = []
    if input_format == "csv":
        expected_header = _validate_csv_headers(
            paths, csv_mode=csv_mode, identities=identities)
    required_calendar, calendar, partition_sources = _config_and_sidecars(
        agent_config, recorded_root=recorded_root, from_recorder=from_recorder,
        source_paths=paths)
    selected = _selected_set(selected_vehicles)
    excluded_options = 0
    quarantined = Counter()
    kept = 0
    view_counts = {"normalized": 0, "bars": 0, "quotes": 0,
                   "options": 0, "replay": 0}
    first_source_row = None
    last_source_row = None

    def rows_for(path: Path, row_offset: int = 0) -> Iterator[tuple[int, dict]]:
        if input_format == "csv":
            yield from _csv_rows(path, csv_mode=csv_mode,
                                 expected_header=expected_header,
                                 row_offset=row_offset)
        else:
            yield from _jsonl_rows(path)

    with ExitStack() as stack:
        normalized_output = stack.enter_context(
            Path(normalized).open("w", encoding="utf-8"))
        bars_output = stack.enter_context(Path(bars).open("w", encoding="utf-8"))
        quotes_output = (stack.enter_context(Path(quotes).open("w", encoding="utf-8"))
                         if quotes is not None else None)
        options_output = stack.enter_context(
            Path(options).open("w", encoding="utf-8"))
        replay_output = (stack.enter_context(Path(replay).open("w", encoding="utf-8"))
                         if replay is not None else None)

        source_row_offset = 0
        transformed_row = 0
        for path in paths:
            rows_in_source = 0
            for source_row, payload in rows_for(path, source_row_offset):
                rows_in_source += 1
                kind = str(payload.get("kind", "bar")).lower()
                # This must happen before vehicle filtering so quarantined
                # option rows remain represented in the quarantine report.
                if _legacy_observation_inversion(payload):
                    quarantined[kind or "unknown"] += 1
                    if first_source_row is None:
                        first_source_row = source_row
                    last_source_row = source_row
                    continue

                # ``kept_rows`` is the pre-vehicle-filter count from the
                # historical dataset report.  View counts below are final
                # selected-row counts.
                kept += 1
                if (selected is not None and "option" not in selected
                        and kind in OPTION_KINDS):
                    excluded_options += 1
                    continue

                transformed_row += 1
                if agent_config is not None:
                    _apply_calendar(
                        payload, row_number=transformed_row, calendar=calendar,
                        partition_sources=partition_sources,
                        required=required_calendar)
                # The historical direct API accepted diagnostic option rows
                # without feed metadata.  Enforce executable OPRA provenance
                # whenever the new vehicle-aware/production path is active;
                # this keeps that direct compatibility default intact.
                if (kind in OPTION_KINDS
                        and (selected is not None or agent_config is not None)):
                    _option_observation_fix(payload, row_number=transformed_row)

                serialized = json.dumps(payload, sort_keys=True) + "\n"
                normalized_output.write(serialized)
                view_counts["normalized"] += 1
                if kind in BAR_KINDS:
                    bars_output.write(serialized)
                    view_counts["bars"] += 1
                    if replay_output is not None:
                        replay_output.write(serialized)
                        view_counts["replay"] += 1
                elif kind in QUOTE_KINDS:
                    if quotes_output is not None:
                        quotes_output.write(serialized)
                    view_counts["quotes"] += 1
                elif kind in OPTION_KINDS:
                    options_output.write(serialized)
                    view_counts["options"] += 1
                    if replay_output is not None:
                        replay_output.write(serialized)
                        view_counts["replay"] += 1

            source_row_offset += rows_in_source

        # Check every selected path only after the complete stream.  This also
        # catches an early partition replaced/appended while a later partition
        # was being read.
        for path in paths:
            if _stat_identity(path) != identities[path]:
                raise ValueError(f"source mutated while streaming: {path}")

    skipped = sum(quarantined.values())
    result = {
        "schema": "research-cycle-quarantine.v1",
        "status": "quarantined" if skipped else "clean",
        "reason": "as_of_after_observed_at" if skipped else None,
        "rows": skipped,
        "kept_rows": kept,
        "view_counts": view_counts,
        "by_kind": dict(sorted(quarantined.items())),
        "first_source_row": first_source_row,
        "last_source_row": last_source_row,
    }
    if selected is not None:
        result["vehicle_filter"] = {
            "schema": "research-cycle-vehicle-filter.v1",
            "status": "filtered" if excluded_options else "unchanged",
            "selected_vehicles": sorted(selected),
            "excluded_option_rows": excluded_options,
            "source_unchanged": True,
        }
    return result


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?",
                        help="single JSONL/CSV source (omit with --partition-root)")
    parser.add_argument("--partition-root", type=Path,
                        help="directory containing market-YYYY-MM-DD.csv files")
    parser.add_argument("--session-window", type=_nonnegative_int, default=0,
                        help="retain the latest N partitions; 0 means all")
    parser.add_argument("--format", choices=("csv", "jsonl"), default=None)
    parser.add_argument("--csv-mode", choices=("recorder", "external"),
                        default="recorder")
    parser.add_argument("--selected-vehicles", default=None,
                        help="whitespace-separated selected research vehicles")
    parser.add_argument("--agent-config", type=Path, default=None)
    parser.add_argument("--recorded-root", type=Path, default=None)
    parser.add_argument("--from-recorder", action="store_true")
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--quotes", type=Path, default=None)
    parser.add_argument("--options", type=Path, required=True)
    parser.add_argument("--replay", dest="replay", type=Path,
                        help="optional bar+option JSONL view for replay workers")
    args = parser.parse_args()
    if (args.partition_root is None) == (args.source is None):
        parser.error("provide exactly one source or --partition-root")
    if args.partition_root is not None and args.format == "jsonl":
        parser.error("--partition-root requires CSV format")
    try:
        result = build_views(
            args.source, input_format=args.format, normalized=args.normalized,
            bars=args.bars, quotes=args.quotes, options=args.options,
            replay=args.replay, partition_root=args.partition_root,
            session_window=args.session_window, csv_mode=args.csv_mode,
            selected_vehicles=args.selected_vehicles,
            agent_config=args.agent_config, recorded_root=args.recorded_root,
            from_recorder=args.from_recorder)
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"research_dataset: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
