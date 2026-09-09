#!/usr/bin/env python3
"""Census recorder session partitions before expanding research inputs.

This is a metadata-only preflight.  It never reads partition rows, creates a
research view, or claims that a session contains usable bars, quotes, trades,
or proof.  The recorder's exact calendar and source markers are used only to
establish a conservative upper bound for the forward-observed sessions that
could be available to a backtest.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
import stat
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.research_dataset import (  # noqa: E402
    _PARTITION_NAME,
    _normalize_calendar_entry,
    _partition_calendar_sidecars,
    _partition_source_sidecars,
    _source_paths,
)


SCHEMA = "research-partition-census.v1"
ACCEPTANCE_REPORT_SCHEMA = "session-acceptance-report.v1"
MAX_METADATA_BYTES = 1024 * 1024
MAX_PARTITIONS = 100_000
MAX_ACCEPTANCE_FRESHNESS_SECONDS = 30.0
CLOCK_SKEW_TOLERANCE_SECONDS = 5.0


class CensusError(ValueError):
    """A recorder source or exact-calendar metadata error."""


def _timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _regular_file(path: Path, *, label: str, max_bytes: int | None = None) -> int:
    if path.is_symlink():
        raise CensusError(f"{label} must not be a symlink: {path}")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CensusError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CensusError(f"{label} must be a regular file: {path}")
    size = int(metadata.st_size)
    if max_bytes is not None and size > max_bytes:
        raise CensusError(
            f"{label} exceeds the {max_bytes}-byte metadata bound: {path}")
    return size


def _read_object(path: Path, *, label: str, required: bool = False) -> dict | None:
    if not path.exists():
        if required:
            raise CensusError(f"{label} is missing: {path}")
        return None
    _regular_file(path, label=label, max_bytes=MAX_METADATA_BYTES)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise CensusError(
                f"{label} exceeds the {MAX_METADATA_BYTES}-byte metadata bound: {path}")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CensusError(f"{label} is malformed: {path}") from exc
    if not isinstance(value, dict):
        raise CensusError(f"{label} must be a JSON object: {path}")
    return value


def _metadata_candidates(paths: Sequence[Path], recorded_root: Path | None,
                         suffix: str) -> list[Path]:
    roots: list[Path] = []
    if recorded_root is not None:
        roots.append(Path(recorded_root) / "sessions")
    roots.extend(path.parent for path in paths)
    roots = list(dict.fromkeys(root.resolve() for root in roots if root.is_dir()))
    result: set[Path] = set()
    for source in paths:
        if _PARTITION_NAME.fullmatch(source.name) is None:
            continue
        for root in roots:
            candidate = root / f"{source.name}{suffix}"
            if candidate.exists():
                result.add(candidate)
    return sorted(result)


def _check_metadata_files(paths: Sequence[Path], recorded_root: Path | None) -> None:
    for suffix, label in ((".calendar.json", "calendar sidecar"),
                          (".source.json", "source sidecar")):
        for path in _metadata_candidates(paths, recorded_root, suffix):
            _regular_file(path, label=label, max_bytes=MAX_METADATA_BYTES)


def _aggregate_path(paths: Sequence[Path], recorded_root: Path | None) -> Path | None:
    candidates: list[Path] = []
    if recorded_root is not None:
        candidates.append(Path(recorded_root) / ".recorder-index.json")
    if paths:
        parent = paths[0].parent
        candidates.append(parent / ".recorder-index.json")
        if parent.name == "sessions":
            candidates.append(parent.parent / ".recorder-index.json")
    for path in dict.fromkeys(candidate.resolve() for candidate in candidates):
        if path.exists():
            return path
    return None


def _aggregate_metadata(paths: Sequence[Path], recorded_root: Path | None,
                        selected_days: set[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    path = _aggregate_path(paths, recorded_root)
    if path is None:
        return {}, {}
    payload = _read_object(path, label="recorder index")
    assert payload is not None
    if payload.get("schema") not in (None, "recorder-index.v1"):
        raise CensusError(f"recorder index has an unsupported schema: {path}")
    raw_calendar = payload.get("session_calendar", {})
    raw_sources = payload.get("partition_sources", {})
    if not isinstance(raw_calendar, dict) or not isinstance(raw_sources, dict):
        raise CensusError(f"recorder index metadata is malformed: {path}")

    calendar: dict[str, dict] = {}
    for day in selected_days:
        if day not in raw_calendar:
            continue
        try:
            calendar[day] = _normalize_calendar_entry(
                day, raw_calendar[day], label=f"recorder index calendar {path}")
        except ValueError as exc:
            raise CensusError(
                f"recorder index calendar metadata is malformed for {day}") from exc

    sources: dict[str, dict] = {}
    for day in selected_days:
        name = f"market-{day}.csv"
        if name not in raw_sources:
            continue
        value = raw_sources[name]
        if (not isinstance(value, Mapping) or
                value.get("source_mode") != "historical_backfill"):
            raise CensusError(
                f"recorder index source metadata is malformed for {name}")
        sources[name] = {"source_mode": "historical_backfill"}
    return calendar, sources


def _calendar_for_partition(day: str, marker: Mapping[str, object] | None,
                            aggregate: Mapping[str, object] | None) -> dict | None:
    if marker is not None and aggregate is not None and dict(marker) != dict(aggregate):
        raise CensusError(f"conflicting calendar metadata for {day}")
    value = marker if marker is not None else aggregate
    return dict(value) if isinstance(value, Mapping) else None


def _source_for_partition(name: str, marker: Mapping[str, object] | None,
                          aggregate: Mapping[str, object] | None) -> str:
    if marker is not None and aggregate is not None and dict(marker) != dict(aggregate):
        raise CensusError(f"conflicting source metadata for {name}")
    value = marker if marker is not None else aggregate
    if value is None:
        return "forward_observed"
    if (not isinstance(value, Mapping) or
            value.get("source_mode") != "historical_backfill"):
        raise CensusError(f"source metadata is malformed for {name}")
    return "historical_backfill"


def _acceptance_for_partition(root: Path, day: str,
                              calendar: Mapping[str, object], *,
                              now: datetime) -> tuple[str, str]:
    path = root / f"session-{day}.report.json"
    if not path.exists():
        return "missing", "accepted full-session report is missing"
    try:
        payload = _read_object(path, label="session acceptance report", required=True)
    except CensusError as exc:
        return "invalid", str(exc)
    assert payload is not None
    session = payload.get("session")
    identities = payload.get("identities")
    sample_counts = payload.get("sample_counts")
    expected_symbols = payload.get("expected_symbols")
    finalized_ts = _number(payload.get("finalized_ts"))
    expected_open = _timestamp(calendar.get("open"))
    expected_close = _timestamp(calendar.get("close"))
    observed_open = (_timestamp(session.get("open"))
                     if isinstance(session, Mapping) else None)
    observed_close = (_timestamp(session.get("close"))
                      if isinstance(session, Mapping) else None)
    contract_valid = bool(
        payload.get("schema") == ACCEPTANCE_REPORT_SCHEMA and
        isinstance(session, Mapping) and session.get("date") == day and
        session.get("source") == "alpaca_calendar" and
        expected_open is not None and observed_open == expected_open and
        expected_close is not None and observed_close == expected_close and
        payload.get("operational_only") is True and
        payload.get("authorizing") is False and
        payload.get("promotion_eligible") is False and
        isinstance(payload.get("accepted"), bool))
    if not contract_valid:
        return "invalid", "session acceptance report contract is invalid"
    if payload.get("accepted") is not True:
        return "rejected", "full-session operational acceptance was rejected"
    if (payload.get("status") != "accepted" or
            payload.get("reasons") not in ([], ()) or
            payload.get("reason_counts") != {}):
        return "invalid", "accepted session report has inconsistent status"
    if (finalized_ts is None or expected_close is None or
            finalized_ts < expected_close.timestamp() or
            finalized_ts > now.timestamp() + CLOCK_SKEW_TOLERANCE_SECONDS):
        return "invalid", "accepted session report finalization is invalid"
    if (not isinstance(sample_counts, Mapping) or
            _nonnegative_int(sample_counts.get("total")) is None or
            sample_counts.get("total", 0) < 2 or
            sample_counts.get("healthy") != sample_counts.get("total") or
            sample_counts.get("failed") != 0 or
            sample_counts.get("valid_timestamps") != sample_counts.get("total")):
        return "invalid", "accepted session report sample counts are invalid"
    if (not isinstance(expected_symbols, list) or not expected_symbols or
            len(expected_symbols) != len(set(expected_symbols)) or
            any(not isinstance(symbol, str) or not symbol.strip() or
                symbol != symbol.strip().upper()
                for symbol in expected_symbols)):
        return "invalid", "accepted session report symbol coverage is invalid"
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping):
        return "invalid", "accepted session report coverage is missing"
    first_sample = _number(coverage.get("first_sample_ts"))
    last_sample = _number(coverage.get("last_sample_ts"))
    coverage_open = _number(coverage.get("open_ts"))
    coverage_close = _number(coverage.get("close_ts"))
    start_tolerance = _number(coverage.get("start_tolerance_seconds"))
    end_tolerance = _number(coverage.get("end_tolerance_seconds"))
    max_gap = _number(coverage.get("max_sample_gap_seconds"))
    observed_gap = _number(coverage.get("max_observed_gap_seconds"))
    expected_open_ts = expected_open.timestamp() if expected_open is not None else None
    expected_close_ts = expected_close.timestamp() if expected_close is not None else None
    if (None in (first_sample, last_sample, coverage_open, coverage_close,
                 start_tolerance, end_tolerance, max_gap, observed_gap,
                 expected_open_ts, expected_close_ts) or
            start_tolerance < 0 or end_tolerance < 0 or max_gap <= 0 or
            abs(coverage_open - expected_open_ts) > .001 or
            abs(coverage_close - expected_close_ts) > .001 or
            first_sample < expected_open_ts - start_tolerance or
            first_sample > expected_open_ts + start_tolerance or
            last_sample < expected_close_ts - end_tolerance or
            last_sample > expected_close_ts + end_tolerance or
            last_sample > finalized_ts + CLOCK_SKEW_TOLERANCE_SECONDS or
            observed_gap < 0 or observed_gap > max_gap or
            coverage.get("closed_at_report") is not True):
        return "invalid", "accepted session report coverage is invalid"
    progress = payload.get("post_activation_progress")
    if (not isinstance(progress, Mapping) or progress.get("arms") != 24 or
            progress.get("snapshots") != sample_counts.get("total") or
            progress.get("all_arms_progressed") is not True or
            _nonnegative_int(progress.get("minimum_processed_events")) in (None, 0) or
            _nonnegative_int(progress.get("minimum_session_delta")) in (None, 0)):
        return "invalid", "accepted session report post-activation progress is invalid"
    activation = progress.get("activation_watermark")
    activation_at = (_number(activation.get("last_inserted_at"))
                     if isinstance(activation, Mapping) else None)
    if (activation_at is None or expected_open_ts is None or
            activation_at >= expected_open_ts or
            _nonnegative_int(activation.get("count")) is None or
            _nonnegative_int(activation.get("decision_event_count")) is None):
        return "invalid", "accepted session report activation chronology is invalid"
    freshness = payload.get("freshness")
    threshold = (_number(freshness.get("strict_threshold_cap_seconds"))
                 if isinstance(freshness, Mapping) else None)
    if (threshold is None or threshold <= 0 or
            threshold > MAX_ACCEPTANCE_FRESHNESS_SECONDS):
        return "invalid", "accepted session report freshness threshold is invalid"
    expected_observations = sample_counts["total"] * len(expected_symbols)
    for key, expected_count in (
            ("quote_event_age_seconds", expected_observations),
            ("bar_publication_deadline_lag_seconds", expected_observations),
            ("shadow_source_lag_seconds", sample_counts["total"])):
        stats = freshness.get(key) if isinstance(freshness, Mapping) else None
        if not isinstance(stats, Mapping):
            return "invalid", f"accepted session report {key} is invalid"
        maximum = _number(stats.get("max"))
        if (_nonnegative_int(stats.get("count")) != expected_count or
                maximum is None or maximum < 0 or maximum > threshold):
            return "invalid", f"accepted session report {key} is invalid"
    bar_event = (freshness.get("bar_event_age_seconds")
                 if isinstance(freshness, Mapping) else None)
    if (not isinstance(bar_event, Mapping) or
            _nonnegative_int(bar_event.get("count")) in (None, 0) or
            bar_event.get("count") > expected_observations or
            _number(bar_event.get("max")) is None or
            _number(bar_event.get("max")) < 0):
        return "invalid", "accepted session report bar event ages are invalid"
    warmups = payload.get("warmup_sessions")
    if not isinstance(warmups, list) or len(warmups) != 1:
        return "invalid", "accepted session report warmup chronology is invalid"
    try:
        warmup_day = date.fromisoformat(warmups[0])
        session_day = date.fromisoformat(day)
    except (TypeError, ValueError):
        return "invalid", "accepted session report warmup chronology is invalid"
    if warmup_day.isoformat() != warmups[0] or warmup_day >= session_day:
        return "invalid", "accepted session report warmup chronology is invalid"
    if (not isinstance(identities, Mapping) or any(
            not isinstance(identities.get(key), str) or
            not str(identities.get(key)).strip()
            for key in ("deployment", "code", "cohort", "activation"))):
        return "invalid", "accepted session report identities are incomplete"
    return "accepted", "accepted full-session operational report matches partition"


def _empty_result(*, source_kind: str, partition_root: Path | None,
                  session_window: int, reason: str,
                  selected: int = 0,
                  acceptance_root: Path | None = None) -> dict:
    return {
        "schema": SCHEMA,
        "status": "skipped",
        "reason": reason,
        "source_kind": source_kind,
        "partition_root": str(partition_root) if partition_root is not None else None,
        "session_window": int(session_window),
        "selected_partition_count": selected,
        "nonempty_partition_count": 0,
        "closed_partition_count": 0,
        "forward_partition_count": 0,
        "historical_partition_count": 0,
        "eligible_forward_partition_count": 0,
        "complete_forward_partition_count": 0,
        "accepted_forward_partition_count": 0,
        "missing_acceptance_report_count": 0,
        "rejected_acceptance_report_count": 0,
        "invalid_acceptance_report_count": 0,
        "acceptance_root": (str(acceptance_root)
                            if acceptance_root is not None else None),
        "acceptance_required": acceptance_root is not None,
        "eligible_historical_partition_count": 0,
        "coverage": {"status": "unknown", "reason": "census was not authoritative"},
        "activation": {"status": "unknown", "reason": "census cannot observe activation"},
        "readiness": {"status": "unknown", "reason": "census cannot establish readiness"},
        "authorizing": False,
        "partitions": [],
    }


def census(source: Path | Sequence[Path] | Iterable[Path] | None = None, *,
           partition_root: Path | None = None, session_window: int = 0,
           recorded_root: Path | None = None, trusted_recorder: bool = False,
           diagnostic_only: bool = False, now: datetime | None = None,
           backtest_minimum_sessions: int = 30,
           acceptance_root: Path | None = None) -> dict:
    """Return a conservative exact-calendar partition census.

    The source selection intentionally delegates to ``research_dataset`` so a
    census and later preprocessing inspect the same partition window.  A
    trusted recorder's unmarked partition is forward observed, matching the
    dataset converter; an explicit historical marker is never relabelled.
    """
    empty_acceptance_partition_root = False
    if acceptance_root is not None and partition_root is not None:
        root = Path(partition_root)
        if root.is_symlink():
            raise CensusError("partition root must not be a symlink")
        if not root.exists():
            empty_acceptance_partition_root = True
        elif not root.is_dir():
            raise CensusError("partition root must be a directory")
        else:
            try:
                empty_acceptance_partition_root = not any(
                    item.is_file() and _PARTITION_NAME.fullmatch(item.name)
                    for item in root.iterdir())
            except OSError as exc:
                raise CensusError(
                    f"cannot list partition root {root}: {exc}") from exc
    try:
        paths = ([] if empty_acceptance_partition_root else
                 _source_paths(source, partition_root=partition_root,
                               session_window=session_window))
    except (TypeError, ValueError, OSError) as exc:
        raise CensusError(str(exc)) from exc
    if len(paths) > MAX_PARTITIONS:
        raise CensusError(
            f"selected partition count exceeds the {MAX_PARTITIONS}-partition bound")
    if any(path.is_symlink() for path in paths):
        raise CensusError("census source partition must not be a symlink")
    for path in paths:
        _regular_file(path, label="source partition")

    if not trusted_recorder or diagnostic_only:
        return _empty_result(
            source_kind="diagnostic" if diagnostic_only else "untrusted",
            partition_root=partition_root, session_window=session_window,
            reason=("diagnostic source is non-authorizing" if diagnostic_only
                    else "source is not a trusted recorder corpus"),
            selected=len(paths), acceptance_root=acceptance_root)
    if partition_root is None:
        raise CensusError("trusted recorder census requires a partition root")
    if backtest_minimum_sessions < 0:
        raise CensusError("backtest minimum sessions must be nonnegative")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise CensusError("census time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    acceptance_path = Path(acceptance_root) if acceptance_root is not None else None
    if acceptance_path is not None:
        if acceptance_path.is_symlink():
            raise CensusError("acceptance root must not be a symlink")
        if acceptance_path.exists() and not acceptance_path.is_dir():
            raise CensusError("acceptance root must be a directory")

    _check_metadata_files(paths, recorded_root)
    marker_calendar = _partition_calendar_sidecars(paths, recorded_root) or {}
    marker_sources = _partition_source_sidecars(paths, recorded_root) or {}
    days = {(_PARTITION_NAME.fullmatch(path.name)).group(1) for path in paths}
    aggregate_calendar, aggregate_sources = _aggregate_metadata(
        paths, recorded_root, days)

    records: list[dict] = []
    counts = {
        "nonempty": 0, "closed": 0, "forward": 0, "historical": 0,
        "eligible_forward": 0, "eligible_historical": 0,
        "accepted_forward": 0, "missing_acceptance": 0,
        "rejected_acceptance": 0, "invalid_acceptance": 0,
    }
    for path in paths:
        match = _PARTITION_NAME.fullmatch(path.name)
        assert match is not None
        day = match.group(1)
        name = path.name
        size = int(path.stat().st_size)
        nonempty = size > 0
        calendar = _calendar_for_partition(
            day, marker_calendar.get(day), aggregate_calendar.get(day))
        if calendar is None:
            raise CensusError(f"exact calendar metadata is missing for {day}")
        source_mode = _source_for_partition(
            name, marker_sources.get(name), aggregate_sources.get(name))
        status = str(calendar.get("status") or "open").strip().lower()
        if status == "closed":
            is_closed = True
            if nonempty:
                raise CensusError(
                    f"nonempty partition {name} conflicts with a closed calendar day")
        else:
            opened = _timestamp(calendar.get("open"))
            closed = _timestamp(calendar.get("close"))
            if opened is None or closed is None or opened >= closed:
                raise CensusError(f"exact calendar metadata is malformed for {day}")
            is_closed = closed <= current

        eligible = bool(nonempty and is_closed)
        if nonempty:
            counts["nonempty"] += 1
        if is_closed:
            counts["closed"] += 1
        if source_mode == "forward_observed":
            counts["forward"] += int(nonempty)
        else:
            counts["historical"] += int(nonempty)
        if eligible and source_mode == "forward_observed":
            counts["eligible_forward"] += 1
        if eligible and source_mode == "historical_backfill":
            counts["eligible_historical"] += 1
        acceptance_status = "not_configured"
        acceptance_reason = "acceptance root was not supplied"
        if acceptance_path is not None and eligible and source_mode == "forward_observed":
            acceptance_status, acceptance_reason = _acceptance_for_partition(
                acceptance_path, day, calendar, now=current)
            if acceptance_status == "accepted":
                counts["accepted_forward"] += 1
            elif acceptance_status == "missing":
                counts["missing_acceptance"] += 1
            elif acceptance_status == "rejected":
                counts["rejected_acceptance"] += 1
            else:
                counts["invalid_acceptance"] += 1
        elif not (eligible and source_mode == "forward_observed"):
            acceptance_status = "not_applicable"
            acceptance_reason = "partition is not a complete forward session"
        records.append({
            "name": name, "session_date": day, "bytes": size,
            "nonempty": nonempty, "calendar": "closed" if is_closed else "open",
            "source_mode": source_mode, "eligible_forward": bool(
                eligible and source_mode == "forward_observed"),
            "eligible_historical": bool(
                eligible and source_mode == "historical_backfill"),
            "acceptance_status": acceptance_status,
            "acceptance_reason": acceptance_reason,
        })

    eligible_forward = counts["eligible_forward"]
    accepted_forward = counts["accepted_forward"]
    readiness_count = (accepted_forward if acceptance_path is not None
                       else eligible_forward)
    required = int(backtest_minimum_sessions)
    structurally_underpowered = readiness_count < required
    if structurally_underpowered and acceptance_path is not None:
        reason = (
            f"{eligible_forward} complete forward partitions exist but only "
            f"{accepted_forward} have accepted full-session reports; "
            f"{required} are required")
    elif structurally_underpowered:
        reason = "forward partition upper bound is below the backtest session minimum"
    elif acceptance_path is not None:
        reason = "accepted full-session evidence meets the research input minimum"
    else:
        reason = "partition upper bound computed; data usability remains unknown"
    result = {
        "schema": SCHEMA,
        "status": "census",
        "reason": reason,
        "source_kind": "trusted_recorder",
        "partition_root": str(Path(partition_root).resolve()),
        "session_window": int(session_window),
        "selected_partition_count": len(paths),
        "nonempty_partition_count": counts["nonempty"],
        "closed_partition_count": counts["closed"],
        "forward_partition_count": counts["forward"],
        "historical_partition_count": counts["historical"],
        "eligible_forward_partition_count": eligible_forward,
        "complete_forward_partition_count": eligible_forward,
        "accepted_forward_partition_count": accepted_forward,
        "missing_acceptance_report_count": counts["missing_acceptance"],
        "rejected_acceptance_report_count": counts["rejected_acceptance"],
        "invalid_acceptance_report_count": counts["invalid_acceptance"],
        "eligible_historical_partition_count": counts["eligible_historical"],
        "acceptance_root": (str(acceptance_path.resolve())
                            if acceptance_path is not None else None),
        "acceptance_required": acceptance_path is not None,
        "backtest_minimum_sessions": required,
        "required_forward_sessions": required,
        "readiness_session_count": readiness_count,
        "readiness_basis": ("accepted_full_sessions"
                            if acceptance_path is not None else
                            "complete_forward_partition_upper_bound"),
        "sessions_remaining": max(0, required - readiness_count),
        "structurally_underpowered": structurally_underpowered,
        "coverage": {"status": "unknown",
                      "reason": "calendar metadata does not prove row coverage"},
        "activation": {"status": "unknown",
                        "reason": "calendar metadata does not prove strategy activation"},
        "readiness": {
            "schema": "research-readiness.v1",
            "state": ("waiting_for_forward_sessions"
                      if structurally_underpowered else
                      "input_ready" if acceptance_path is not None else
                      "partition_upper_bound_sufficient"),
            "reason": reason,
            "complete_forward_partition_count": eligible_forward,
            "accepted_forward_partition_count": accepted_forward,
            "missing_acceptance_report_count": counts["missing_acceptance"],
            "rejected_acceptance_report_count": counts["rejected_acceptance"],
            "invalid_acceptance_report_count": counts["invalid_acceptance"],
            "required_forward_sessions": required,
            "readiness_session_count": readiness_count,
            "recorded_sessions": readiness_count,
            "required_sessions": required,
            "readiness_basis": ("accepted_full_sessions"
                                if acceptance_path is not None else
                                "complete_forward_partition_upper_bound"),
            "sessions_remaining": max(0, required - readiness_count),
            "acceptance_required": acceptance_path is not None,
            "authorizing": False,
        },
        "authorizing": False,
        "accepted": False,
        "partitions": records,
    }
    return result


def _minimum_sessions() -> int:
    try:
        from research.gates import protocol_minimums
        return int(protocol_minimums("backtest")["sessions"])
    except (ImportError, KeyError, TypeError, ValueError):
        return 30


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partition-root", type=Path, required=True)
    parser.add_argument("--session-window", type=int, default=0)
    parser.add_argument("--recorded-root", type=Path)
    parser.add_argument("--acceptance-root", type=Path)
    parser.add_argument("--trusted-recorder", action="store_true")
    parser.add_argument("--diagnostic-only", action="store_true")
    parser.add_argument("--summary-only", action="store_true",
                        help="omit per-partition records from CLI output")
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    now = _timestamp(args.now) if args.now else None
    if args.now and now is None:
        payload = {"schema": SCHEMA, "status": "error",
                   "reason": "--now must be an aware ISO timestamp"}
        print(json.dumps(payload, sort_keys=True))
        return 3
    try:
        payload = census(
            partition_root=args.partition_root,
            session_window=args.session_window,
            recorded_root=args.recorded_root,
            trusted_recorder=args.trusted_recorder,
            diagnostic_only=args.diagnostic_only,
            now=now,
            backtest_minimum_sessions=_minimum_sessions(),
            acceptance_root=args.acceptance_root)
    except CensusError as exc:
        payload = {"schema": SCHEMA, "status": "error", "reason": str(exc)}
        print(json.dumps(payload, sort_keys=True))
        return 3
    if args.summary_only:
        payload["partitions"] = []
        payload["partitions_omitted"] = True
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
