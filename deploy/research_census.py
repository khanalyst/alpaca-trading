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
import sqlite3
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
MAX_EPOCH_CONTEXT_AGE_SECONDS = 180.0
EPOCH_CONTEXT_SCHEMA = "research-epoch-context.v1"


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


def _identity(value: object) -> str | None:
    """Return one bounded non-empty identity token."""
    if not isinstance(value, str) or not value or len(value) > 256 or value != value.strip():
        return None
    if value.lower() in {"unknown", "unset", "none", "null", "unavailable"}:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    return value


def _symbols(value: object) -> list[str] | None:
    """Normalize an exact configured symbol catalog, rejecting drift."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        symbol = item.strip().upper()
        if not symbol or symbol != item.strip() or symbol in result:
            return None
        result.append(symbol)
    return sorted(result)


def _cohort_layout(value: object) -> dict | None:
    """Validate the shared diagnostic cohort contract."""
    try:
        from research.diagnostic_cohort_contract import validate_cohort_layout
    except ImportError as exc:
        raise CensusError("diagnostic cohort contract is unavailable") from exc
    try:
        validated = validate_cohort_layout(value)
    except (TypeError, ValueError):
        validated = None
    return dict(validated) if isinstance(validated, Mapping) else None


def _context_identities(value: Mapping[str, object]) -> dict[str, str | None]:
    provenance = value.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    return {
        "deployment": _identity(provenance.get("identity")),
        "code": _identity(value.get("code_identity")),
        "cohort": _identity(value.get("cohort_identity")),
        "activation": _identity(value.get("activation_identity")),
    }


def _normalize_context(value: Mapping[str, object] | None) -> dict | None:
    """Normalize the explicit current-epoch context supplied by the caller.

    The accepted shape is the ``research-epoch-context.v1`` envelope with
    ``verified``/``updated_ts``, four ``identities`` (deployment, code, cohort,
    activation), exact ``expected_symbols``, validated ``cohort_contract``,
    and runtime/policy ``config_identities``.  No report-provided aliases or
    opaque epoch fields are inferred here.
    """
    if not isinstance(value, Mapping):
        return None
    raw_identities = value.get("identities")
    identities = (dict(raw_identities)
                  if isinstance(raw_identities, Mapping) else None)
    symbols = _symbols(value.get("expected_symbols"))
    contract = _cohort_layout(value.get("cohort_contract"))
    updated = _number(value.get("updated_ts"))
    raw_configs = value.get("config_identities")
    config_identities = (dict(raw_configs)
                         if isinstance(raw_configs, Mapping) else None)
    return {
        "schema": value.get("schema"),
        "verified": value.get("verified") is True,
        "context_error": _identity(value.get("context_error")),
        "updated_ts": updated,
        "identities": identities,
        "expected_symbols": symbols,
        "cohort_contract": contract,
        "config_identities": config_identities,
        "include_ibr": value.get("include_ibr"),
    }


def _context_reason(value: Mapping[str, object] | None, *, now: datetime) -> str | None:
    """Return a fail-closed reason for a current epoch context."""
    context = _normalize_context(value)
    if context is None:
        return "current epoch context is missing"
    if context.get("context_error"):
        return str(context["context_error"])
    if context.get("verified") is not True:
        return "current epoch context is not verified"
    if context.get("schema") != EPOCH_CONTEXT_SCHEMA:
        return "current epoch context schema is invalid"
    identities = context.get("identities")
    if (not isinstance(identities, Mapping) or any(
            not _identity(identities.get(key))
            for key in ("deployment", "code", "cohort", "activation"))):
        return "current epoch identities are incomplete"
    if context.get("expected_symbols") is None:
        return "current epoch expected symbols are missing"
    if context.get("cohort_contract") is None:
        return "current epoch cohort contract is invalid"
    contract = context["cohort_contract"]
    if (not isinstance(contract, Mapping) or
            contract.get("code_identity") != identities.get("code") or
            contract.get("cohort_identity") != identities.get("cohort")):
        return "current epoch cohort identities are inconsistent"
    include_ibr = context.get("include_ibr")
    if (not isinstance(include_ibr, bool) or
            contract.get("arm_count") != (31 if include_ibr else 24)):
        return "current epoch cohort mode is inconsistent"
    config_identities = context.get("config_identities")
    if (not isinstance(config_identities, Mapping) or any(
            not _identity(config_identities.get(key))
            for key in ("runtime", "policy"))):
        return "current epoch config identities are incomplete"
    updated = context.get("updated_ts")
    if updated is None:
        return "current epoch context freshness is missing"
    now_ts = now.timestamp()
    if updated > now_ts + CLOCK_SKEW_TOLERANCE_SECONDS:
        return "current epoch context timestamp is in the future"
    if now_ts - updated > MAX_EPOCH_CONTEXT_AGE_SECONDS:
        return "current epoch context is stale"
    return None


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


def _read_immutable_activation(path: Path, cohort_identity: str) -> dict:
    """Read one activation row from the shadow WAL without opening writes."""
    if path.is_symlink():
        raise CensusError(f"shadow database must not be a symlink: {path}")
    _regular_file(path, label="shadow database")
    try:
        # ``mode=ro`` is important: census is a read-only preflight and must
        # not create a SQLite journal, repair the WAL, or mutate its metadata.
        uri = f"file:{path.resolve()}?mode=ro"
        database = sqlite3.connect(uri, uri=True)
        try:
            row = database.execute(
                "SELECT value FROM meta WHERE key=?",
                (f"diagnostic-shadow-activation.v1:{cohort_identity}",),
            ).fetchone()
        finally:
            database.close()
    except (OSError, sqlite3.Error) as exc:
        raise CensusError(f"current epoch activation metadata is unavailable: {exc}") from exc
    if row is None:
        raise CensusError("current epoch activation metadata is missing")
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CensusError("current epoch activation metadata is malformed") from exc
    if not isinstance(value, Mapping):
        raise CensusError("current epoch activation metadata is malformed")
    try:
        from research.live_shadow import _validated_diagnostic_activation
        value = _validated_diagnostic_activation(
            value, cohort_identity=cohort_identity)
    except ImportError as exc:
        raise CensusError("current epoch activation validator is unavailable") from exc
    except Exception as exc:
        raise CensusError(f"current epoch activation metadata is invalid: {exc}") from exc
    return dict(value)


def load_current_epoch_context(*, shadow_health: Path,
                               shadow_db: Path,
                               recorded_root: Path,
                               runtime_config_path: Path,
                               now: datetime | None = None,
                               max_age_seconds: float = MAX_EPOCH_CONTEXT_AGE_SECONDS,
                               expected_deployment: str | None = None,
                               include_ibr: bool = False) -> dict:
    """Build a verified context from mounted shadow health and activation.

    The health heartbeat supplies the current code/cohort/activation catalog;
    the immutable activation row supplies the binding that prevents a forged
    or stale heartbeat from selecting a prior epoch.  Recorder symbols are
    read from its index, never from an acceptance report.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise CensusError("current epoch context time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    if (isinstance(max_age_seconds, bool) or
            not isinstance(max_age_seconds, (int, float)) or
            not math.isfinite(float(max_age_seconds)) or max_age_seconds <= 0):
        raise CensusError("current epoch context freshness bound is invalid")
    if not isinstance(include_ibr, bool):
        raise CensusError("current epoch diagnostic IBR mode is invalid")
    health = _read_object(Path(shadow_health), label="shadow health", required=True)
    assert health is not None
    if health.get("schema") != "shadow-health.v1":
        raise CensusError("current epoch shadow health schema is invalid")
    updated = _number(health.get("updated_ts"))
    if updated is None:
        raise CensusError("current epoch shadow health timestamp is invalid")
    now_ts = current.timestamp()
    if updated > now_ts + CLOCK_SKEW_TOLERANCE_SECONDS:
        raise CensusError("current epoch shadow health timestamp is in the future")
    if now_ts - updated > float(max_age_seconds):
        raise CensusError("current epoch shadow health is stale")
    diagnostic = health.get("diagnostic_shadow")
    if not isinstance(diagnostic, Mapping):
        raise CensusError("current epoch shadow health diagnostic catalog is missing")
    contract = _cohort_layout(diagnostic)
    if contract is None:
        raise CensusError("current epoch shadow health cohort catalog is invalid")
    declared_contract = diagnostic.get("cohort_contract")
    if (declared_contract is not None and
            _cohort_layout(declared_contract) != contract):
        raise CensusError("current epoch shadow health cohort contract drifts from arm catalog")
    identities = _context_identities({**dict(diagnostic),
                                      "provenance": health.get("provenance")})
    if any(identities.get(key) is None for key in (
            "deployment", "code", "cohort", "activation")):
        raise CensusError("current epoch shadow health identities are incomplete")
    if _identity(expected_deployment) is None:
        raise CensusError("current deployment identity is unavailable")
    if identities["deployment"] != _identity(expected_deployment):
        raise CensusError("current epoch deployment identity mismatches shadow health")
    try:
        from research.live_shadow import _replay_code_hash
        expected_code = _replay_code_hash()
    except Exception as exc:
        raise CensusError("current replay code identity is unavailable") from exc
    if identities["code"] != expected_code:
        raise CensusError("current epoch code identity mismatches current replay code")
    activation = _read_immutable_activation(
        Path(shadow_db), str(identities["cohort"]))
    if (activation.get("cohort_identity") != identities["cohort"] or
            activation.get("code_identity") != identities["code"] or
            activation.get("activation_identity") != identities["activation"]):
        raise CensusError("current epoch immutable activation identity mismatches shadow health")
    config_identities = {
        "runtime": _identity(activation.get("runtime_config_identity")),
        "policy": _identity(activation.get("policy_config_identity")),
    }
    if any(value is None for value in config_identities.values()):
        raise CensusError("current epoch immutable activation config identities are incomplete")
    try:
        from agent.config import load_config
        from research.diagnostic_shadow import build_diagnostic_cohort
        runtime_config = load_config(Path(runtime_config_path))
        expected_cohort = build_diagnostic_cohort(
            runtime_config, code_identity=expected_code,
            include_ibr=include_ibr)
    except Exception as exc:
        raise CensusError("current runtime config cannot rebuild diagnostic cohort") from exc
    expected_contract = _cohort_layout(expected_cohort)
    if expected_contract is None or expected_contract != contract:
        raise CensusError("current epoch cohort catalog mismatches current runtime config")
    expected_configs = {
        "runtime": _identity(expected_cohort.get("runtime_config_identity")),
        "policy": _identity(expected_cohort.get("policy_config_identity")),
    }
    if expected_configs != config_identities:
        raise CensusError("current epoch config identities mismatch current runtime config")
    recorded = Path(recorded_root)
    index = _read_object(recorded / ".recorder-index.json",
                         label="recorder index", required=True)
    assert index is not None
    expected_symbols = _symbols(index.get("configured_symbols"))
    if expected_symbols is None:
        raise CensusError("current epoch recorder symbol catalog is invalid")
    runtime_universe = runtime_config.get("universe")
    runtime_symbols = (_symbols(runtime_universe.get("symbols"))
                       if isinstance(runtime_universe, Mapping) else None)
    if runtime_symbols is None or runtime_symbols != expected_symbols:
        raise CensusError("current epoch recorder symbols mismatch runtime universe")
    return {
        "schema": EPOCH_CONTEXT_SCHEMA,
        "verified": True,
        "updated_ts": updated,
        "identities": {key: str(value) for key, value in identities.items()},
        "expected_symbols": expected_symbols,
        "cohort_contract": contract,
        "config_identities": config_identities,
        "activation": dict(activation),
        "include_ibr": include_ibr,
        "source": "shadow_health_and_immutable_activation",
    }


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
                              now: datetime,
                              current_context: Mapping[str, object] | None = None) -> tuple[str, str]:
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
    context = current_context
    context_reason = _context_reason(context, now=now)
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
    if context_reason is not None:
        return "invalid", context_reason
    normalized_context = _normalize_context(context)
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
    report_symbols = _symbols(expected_symbols)
    if report_symbols is None:
        return "invalid", "accepted session report symbol coverage is invalid"
    if normalized_context is not None:
        expected_context_symbols = normalized_context.get("expected_symbols")
        if (expected_context_symbols is None or
                report_symbols != expected_context_symbols):
            return "invalid", "accepted session report symbols do not match current epoch"
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
    expected_contract = (normalized_context.get("cohort_contract")
                         if normalized_context is not None else None)
    if (expected_contract is None or
            not isinstance(progress, Mapping) or
            progress.get("arms") != expected_contract.get("arm_count") or
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
    if normalized_context is not None:
        expected_identities = normalized_context.get("identities")
        if (not isinstance(expected_identities, Mapping) or any(
                identities.get(key) != expected_identities.get(key)
                for key in ("deployment", "code", "cohort", "activation"))):
            return "invalid", "accepted session report identities do not match current epoch"
        report_contract = _cohort_layout(payload.get("cohort_contract"))
        if report_contract is None or report_contract != expected_contract:
            return "invalid", "accepted session report cohort contract does not match current epoch"
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
        "epoch_context": {"status": "unknown",
                          "reason": "census was not authoritative"},
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
           acceptance_root: Path | None = None,
           current_context: Mapping[str, object] | None = None) -> dict:
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

    epoch_context = current_context
    epoch_context_reason = (_context_reason(epoch_context, now=current)
                            if acceptance_path is not None else None)

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
                acceptance_path, day, calendar, now=current,
                current_context=epoch_context)
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
        if epoch_context_reason is not None:
            reason = (f"current epoch acceptance context unavailable: "
                      f"{epoch_context_reason}")
        else:
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
        "epoch_context": {
            "status": ("unknown" if acceptance_path is None else
                       "verified" if epoch_context_reason is None else "invalid"),
            "reason": ("acceptance root was not supplied"
                       if acceptance_path is None else
                       "current epoch context is verified"
                       if epoch_context_reason is None else epoch_context_reason),
        },
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
    parser.add_argument("--runtime-config", type=Path,
                        help="mounted runtime config used to rebuild the current cohort")
    parser.add_argument("--acceptance-root", type=Path)
    parser.add_argument("--trusted-recorder", action="store_true")
    parser.add_argument("--diagnostic-only", action="store_true")
    parser.add_argument("--summary-only", action="store_true",
                        help="omit per-partition records from CLI output")
    parser.add_argument("--shadow-health", type=Path,
                        help="mounted shadow health heartbeat for current epoch binding")
    parser.add_argument("--shadow-db", type=Path,
                        help="mounted read-only shadow WAL containing immutable activation")
    parser.add_argument("--context-max-age", type=float,
                        default=MAX_EPOCH_CONTEXT_AGE_SECONDS,
                        help="maximum shadow-health age for current epoch context")
    parser.add_argument("--diagnostic-include-ibr", action="store_true",
                        help="bind the current epoch to the seven registered IBR arms")
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    now = _timestamp(args.now) if args.now else None
    if args.now and now is None:
        payload = {"schema": SCHEMA, "status": "error",
                   "reason": "--now must be an aware ISO timestamp"}
        print(json.dumps(payload, sort_keys=True))
        return 3
    current_context = None
    if args.acceptance_root is not None and (
            args.shadow_health is not None or args.shadow_db is not None):
        if (args.shadow_health is None or args.shadow_db is None or
                args.recorded_root is None or args.runtime_config is None):
            current_context = {
                "schema": EPOCH_CONTEXT_SCHEMA,
                "verified": False,
                "context_error": (
                    "current epoch context requires shadow health, shadow database, "
                    "recorded root, and runtime config"),
            }
        else:
            from deploy.provenance import deployment_provenance
            expected_deployment = deployment_provenance().get("identity")
            try:
                current_context = load_current_epoch_context(
                    shadow_health=args.shadow_health, shadow_db=args.shadow_db,
                    recorded_root=args.recorded_root,
                    runtime_config_path=args.runtime_config, now=now,
                    max_age_seconds=args.context_max_age,
                    expected_deployment=expected_deployment,
                    include_ibr=args.diagnostic_include_ibr)
            except CensusError as exc:
                current_context = {
                    "schema": EPOCH_CONTEXT_SCHEMA,
                    "verified": False,
                    "context_error": str(exc),
                }
    try:
        payload = census(
            partition_root=args.partition_root,
            session_window=args.session_window,
            recorded_root=args.recorded_root,
            trusted_recorder=args.trusted_recorder,
            diagnostic_only=args.diagnostic_only,
            now=now,
            backtest_minimum_sessions=_minimum_sessions(),
            acceptance_root=args.acceptance_root,
            current_context=current_context)
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
