#!/usr/bin/env python3
"""Read-only full-exchange-session operational acceptance evidence.

This module deliberately has no runner, engine, broker, or SQLite imports.  It
samples the recorder and shadow health files, appending one fsynced JSON line
per observation.  The resulting report is an operational completeness check;
it cannot qualify, select, or authorize a strategy.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy import health


SCHEMA = "session-acceptance.v1"
SAMPLE_SCHEMA = "session-acceptance-sample.v1"
REPORT_SCHEMA = "session-acceptance-report.v1"
RECORDER_INDEX_SCHEMA = "recorder-index.v1"
RECORDER_STATUS_SCHEMA = "recorder-status.v1"
SHADOW_HEALTH_SCHEMA = "shadow-health.v1"
DIAGNOSTIC_COVERAGE_SCHEMA = "diagnostic-shadow-coverage.v1"
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_AGE_SECONDS = 30.0
DEFAULT_START_TOLERANCE_SECONDS = 60.0
DEFAULT_END_TOLERANCE_SECONDS = 60.0
DEFAULT_MAX_SAMPLE_GAP_SECONDS = 15.0
DEFAULT_SAMPLE_GAP_TOLERANCE_SECONDS = 5.0
CLOCK_SKEW_TOLERANCE_SECONDS = 5.0
COMPLETED_BAR_INTERVAL_SECONDS = 60.0
MAX_SAMPLES = 200_000
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_SAMPLE_BYTES = 512 * 1024
MAX_SAMPLE_FILE_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_ACCEPTANCE_FILES = 10_000
NEW_YORK = ZoneInfo("America/New_York")


class SessionAcceptanceError(RuntimeError):
    """A corrupt source or durable acceptance evidence error."""


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _epoch(value: object) -> float | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if value in (None, ""):
        return None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        return None
    else:
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


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(value: object) -> str | None:
    if value in (None, "") or isinstance(value, (Mapping, list, tuple)):
        return None
    result = str(value).strip()
    return result or None


def _read_json(path: Path, label: str, *, missing_ok: bool = False,
               max_bytes: int = MAX_METADATA_BYTES) -> dict:
    if path.is_symlink():
        raise SessionAcceptanceError(f"{label}_symlink: {path}")
    if not path.is_file():
        if missing_ok:
            return {}
        raise SessionAcceptanceError(f"{label}_missing: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SessionAcceptanceError(f"{label}_unreadable: {path}: {exc}") from exc
    if size > max_bytes:
        raise SessionAcceptanceError(f"{label}_oversized: {path}")
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise SessionAcceptanceError(f"{label}_oversized: {path}")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SessionAcceptanceError(f"{label}_corrupt: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SessionAcceptanceError(f"{label}_corrupt: expected JSON object: {path}")
    return value


def recorder_index_path(recorder_root: str | Path) -> Path:
    path = Path(recorder_root)
    return path if path.suffix == ".json" else path / ".recorder-index.json"


def _session_date(value: object) -> str:
    raw = str(value)
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise SessionAcceptanceError("session_date_invalid") from exc
    if parsed.isoformat() != raw:
        raise SessionAcceptanceError("session_date_invalid")
    return raw


def session_file(output_dir: str | Path, session_date: str) -> Path:
    """Return the exact-session JSONL path used by the collector."""
    return Path(output_dir) / f"session-{_session_date(session_date)}.jsonl"


def report_file(output_dir: str | Path, session_date: str) -> Path:
    """Return the exact-session atomic report path."""
    return Path(output_dir) / f"session-{_session_date(session_date)}.report.json"


def _freshness_threshold(value: object) -> float:
    parsed = _finite(value)
    if parsed is None or parsed <= 0:
        raise SessionAcceptanceError("freshness_threshold_invalid")
    # Operational acceptance never weakens quote, source, or completed-bar
    # publication freshness merely because the shadow loop polls slowly.
    return min(parsed, DEFAULT_MAX_AGE_SECONDS)


def _completed_bar_publication_lag(
        watermark: object, *, captured: float, opened: float, closed: float,
        tolerance: float) -> tuple[float | None, str | None]:
    """Return lag beyond the next completed-bar deadline.

    Alpaca timestamps one-minute bars at their open, while the recorder stores
    their ``as_of`` completion boundary as ``bar_watermark``.  A watermark is
    therefore current until the following completed bar is due, plus the
    bounded publication tolerance.  Before the first session bar is due, a
    premarket watermark is deliberately not required.  Re-reading an old bar
    cannot refresh this calculation because it depends only on that immutable
    completion boundary.
    """
    first_due = min(closed, opened + COMPLETED_BAR_INTERVAL_SECONDS)
    parsed = _aware_datetime(watermark)
    if watermark not in (None, "") and parsed is None:
        return None, "bar_watermark_invalid"
    completed = parsed.timestamp() if parsed is not None else None
    if completed is not None:
        if completed > captured + CLOCK_SKEW_TOLERANCE_SECONDS:
            return None, "bar_watermark_future"
        if completed > closed + CLOCK_SKEW_TOLERANCE_SECONDS:
            return None, "bar_watermark_after_session"
        if completed >= opened:
            offset = completed - opened
            remainder = offset % COMPLETED_BAR_INTERVAL_SECONDS
            if min(remainder, COMPLETED_BAR_INTERVAL_SECONDS - remainder) > \
                    CLOCK_SKEW_TOLERANCE_SECONDS:
                return None, "bar_watermark_chronology_invalid"
            if completed < first_due - CLOCK_SKEW_TOLERANCE_SECONDS:
                return None, "bar_watermark_chronology_invalid"
    if captured <= first_due + tolerance:
        return 0.0, None
    if completed is None:
        return None, "bar_publication_unknown"
    if completed >= closed - CLOCK_SKEW_TOLERANCE_SECONDS:
        return 0.0, None
    deadline = (first_due if completed < first_due else
                min(closed, completed + COMPLETED_BAR_INTERVAL_SECONDS))
    return max(0.0, captured - deadline), None


def _gap_threshold(interval_seconds: object,
                   tolerance_seconds: object = DEFAULT_SAMPLE_GAP_TOLERANCE_SECONDS) -> float:
    interval = _finite(interval_seconds)
    tolerance = _finite(tolerance_seconds)
    if interval is None or interval <= 0:
        raise SessionAcceptanceError("acceptance_interval_invalid")
    if tolerance is None or tolerance < 0:
        raise SessionAcceptanceError("acceptance_gap_tolerance_invalid")
    return interval + tolerance


def _ensure_file_slot(path: Path) -> None:
    if path.exists():
        return
    try:
        count = 0
        for item in path.parent.iterdir():
            if item.is_file() and item.name.startswith("session-"):
                count += 1
                if count >= MAX_ACCEPTANCE_FILES:
                    raise SessionAcceptanceError(
                        "session_acceptance_file_bound_exceeded")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SessionAcceptanceError(
            f"session_acceptance_directory_unreadable:{path.parent}: {exc}") from exc


def _session(index: Mapping[str, object], session_date: str) -> dict:
    session_date = _session_date(session_date)
    if index.get("schema") != RECORDER_INDEX_SCHEMA:
        raise SessionAcceptanceError("recorder_index_unknown_or_invalid")
    calendar = index.get("session_calendar")
    value = calendar.get(session_date) if isinstance(calendar, Mapping) else None
    if not isinstance(value, Mapping):
        raise SessionAcceptanceError(f"session_calendar_missing:{session_date}")
    opened_at = _aware_datetime(value.get("open"))
    closed_at = _aware_datetime(value.get("close"))
    opened = opened_at.timestamp() if opened_at is not None else None
    closed = closed_at.timestamp() if closed_at is not None else None
    session_day = date.fromisoformat(session_date)
    if (value.get("status") == "closed" or value.get("source") != "alpaca_calendar" or
            opened is None or closed is None or
            closed - opened < COMPLETED_BAR_INTERVAL_SECONDS or
            opened_at.astimezone(NEW_YORK).date() != session_day or
            closed_at.astimezone(NEW_YORK).date() != session_day):
        raise SessionAcceptanceError(f"session_calendar_invalid:{session_date}")
    return {
        "date": str(session_date),
        "open": str(value["open"]),
        "close": str(value["close"]),
        "open_ts": opened,
        "close_ts": closed,
        "source": "alpaca_calendar",
    }


def expected_symbols(index: Mapping[str, object], override: Sequence[str] | None = None) -> list[str]:
    raw = override if override is not None else index.get("configured_symbols")
    if not isinstance(raw, (list, tuple)) or not raw:
        watermarks = index.get("observation_watermarks")
        raw = list(watermarks) if isinstance(watermarks, Mapping) else []
    symbols = sorted({str(item).strip().upper() for item in raw if str(item).strip()})
    if not symbols:
        raise SessionAcceptanceError("expected_symbols_missing")
    return symbols


def _identity(payload: Mapping[str, object], *, deployment: bool = False) -> str | None:
    candidates = ("identity", "deployment_identity", "deployed_identity")
    if deployment:
        provenance = payload.get("provenance")
        if isinstance(provenance, Mapping):
            result = _text(provenance.get("identity"))
            if result:
                return result
            result = _text(provenance.get("deployment_image_digest"))
            if result:
                return result
            result = _text(provenance.get("deployment_commit"))
            if result:
                return result
        candidates = (*candidates, "deployment_commit", "deployment_image_digest")
    for key in candidates:
        result = _text(payload.get(key))
        if result:
            return result
    return None


def _diagnostic(raw_shadow: Mapping[str, object], projected: Mapping[str, object]) -> dict:
    value = raw_shadow.get("diagnostic_shadow")
    if isinstance(value, Mapping):
        return dict(value)
    value = projected.get("diagnostic_shadow")
    return dict(value) if isinstance(value, Mapping) else {}


def _partition_info(root: Path, index: Mapping[str, object], session_date: str) -> dict:
    name = f"market-{session_date}.csv"
    path = root / "sessions" / name
    result: dict[str, object] = {"name": name, "path": str(path), "bytes": None}
    try:
        result["bytes"] = int(path.stat().st_size) if path.is_file() else None
    except OSError as exc:
        result["error"] = f"partition_stat_failed:{type(exc).__name__}"
    partitions = index.get("partitions")
    if isinstance(partitions, Mapping) and name in partitions:
        value = _finite(partitions[name])
        result["index_bytes"] = int(value) if value is not None and value >= 0 else None
    return result


def _cursor_map(diag: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    value = diag.get("processed_event_cursors")
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items() if isinstance(item, Mapping)}
    return {}


def _error_map(raw_shadow: Mapping[str, object], diag: Mapping[str, object]) -> tuple[object, bool]:
    for payload in (raw_shadow, diag):
        for key in ("candidate_errors", "arm_errors", "errors"):
            if key in payload:
                return payload[key], True
    if "candidate_error_count" in raw_shadow:
        return raw_shadow.get("candidate_error_count"), True
    return None, False


def capture_sample(recorder_root: str | Path, shadow_health_path: str | Path,
                   session_date: str, *, now: float | None = None,
                   expected: Sequence[str] | None = None,
                   max_age: float = DEFAULT_MAX_AGE_SECONDS) -> dict:
    """Capture one read-only health sample; malformed JSON raises visibly."""
    freshness = _freshness_threshold(max_age)
    root = Path(recorder_root)
    index = _read_json(recorder_index_path(root), "recorder_index")
    selected = _session(index, str(session_date))
    symbols = expected_symbols(index, expected)
    # Recorder status is optional during startup, but if present it must not
    # be silently swallowed when corrupt.
    status_path = root / ".recorder-status.json"
    raw_recorder_status = _read_json(
        status_path, "recorder_status", missing_ok=True)
    if (status_path.is_file() and
            raw_recorder_status.get("schema") != RECORDER_STATUS_SCHEMA):
        raise SessionAcceptanceError("recorder_status_schema_invalid")
    shadow_path = Path(shadow_health_path)
    raw_shadow = _read_json(shadow_path, "shadow_health", missing_ok=True)
    if shadow_path.is_file() and raw_shadow.get("schema") != SHADOW_HEALTH_SCHEMA:
        raise SessionAcceptanceError("shadow_health_schema_invalid")
    captured = time.time() if now is None else float(now)
    if not math.isfinite(captured):
        raise SessionAcceptanceError("capture_time_invalid")
    recorder_health = health.recorder(root, freshness, now=captured,
                                      configured_symbols=symbols)
    shadow_health = health.shadow(Path(shadow_health_path), freshness, now=captured)
    diag = _diagnostic(raw_shadow, shadow_health)
    errors, errors_present = _error_map(raw_shadow, diag)
    provenance = recorder_health.get("provenance")
    shadow_provenance = shadow_health.get("provenance")
    deployment_recorder = _identity(recorder_health, deployment=True)
    deployment_shadow = _identity(shadow_health, deployment=True)
    partition = _partition_info(root, index, str(session_date))
    raw_observations = recorder_health.get("observation_ages")
    raw_observations = (raw_observations
                        if isinstance(raw_observations, Mapping) else {})
    observations: dict[str, dict] = {}
    for symbol in symbols:
        value = raw_observations.get(symbol)
        observation = dict(value) if isinstance(value, Mapping) else {}
        publication_lag, _publication_error = _completed_bar_publication_lag(
            observation.get("bar_watermark"), captured=captured,
            opened=selected["open_ts"], closed=selected["close_ts"],
            tolerance=freshness)
        observation["bar_publication_deadline_lag_seconds"] = publication_lag
        observations[symbol] = observation
    sample = {
        "schema": SAMPLE_SCHEMA,
        "captured_ts": captured,
        "freshness_threshold_seconds": freshness,
        "session": {key: selected[key] for key in ("date", "open", "close", "source")},
        "expected_symbols": symbols,
        "recorder": {
            "status": recorder_health.get("status"),
            "ok": recorder_health.get("ok"),
            "fresh": recorder_health.get("fresh"),
            "market_session_status": recorder_health.get("market_session_status"),
            "data_feed": recorder_health.get("data_feed"),
            "observation_ages": observations,
            "cadence": recorder_health.get("cadence"),
            "partition": partition,
            "partition_bytes": partition.get("bytes"),
            "provenance": provenance,
        },
        "shadow": {
            "status": shadow_health.get("status"),
            "ok": shadow_health.get("ok"),
            "fresh": shadow_health.get("fresh"),
            "updated_ts": raw_shadow.get("updated_ts"),
            "last_error": raw_shadow.get("last_error", shadow_health.get("last_error")),
            "candidate_errors": errors,
            "candidate_errors_present": errors_present,
            "candidate_error_count": (len(errors) if isinstance(errors, Mapping)
                                       else errors if isinstance(errors, int) else None),
            "poll_duration_seconds": raw_shadow.get(
                "poll_duration_seconds", diag.get("poll_duration_seconds")),
            "diagnostic_shadow": diag,
            "provenance": shadow_provenance,
        },
        "identities": {
            "deployment_recorder": deployment_recorder,
            "deployment_shadow": deployment_shadow,
            "deployment": (deployment_recorder if deployment_recorder == deployment_shadow
                            else None),
            "code": (_text(diag.get("code_identity")) or
                     _text(raw_shadow.get("code_identity"))),
            "cohort": _text(diag.get("cohort_identity")),
            "activation": _text(diag.get("activation_identity")),
        },
        "operational_only": True,
        "authorizing": False,
        "promotion_eligible": False,
    }
    sample["sample_id"] = (
        f"{session_date}:{captured:.6f}:"
        f"{_finite(raw_shadow.get('updated_ts'))!r}")
    sample["warmup_session"] = _text(diag.get("warmup_session"))
    sample["sample_reasons"] = sample_reasons(
        sample, expected=symbols, max_age=freshness)
    sample["sample_status"] = "healthy" if not sample["sample_reasons"] else "failed"
    return sample


def sample_reasons(sample: Mapping[str, object], *, expected: Sequence[str] | None = None,
                   max_age: float | None = None) -> list[str]:
    reasons: list[str] = []
    if sample.get("schema") != SAMPLE_SCHEMA:
        reasons.append("sample_schema_invalid")
    captured = _finite(sample.get("captured_ts"))
    if captured is None:
        reasons.append("sample_timestamp_unknown")
    session = sample.get("session")
    session_day: date | None = None
    opened: float | None = None
    closed: float | None = None
    if not isinstance(session, Mapping) or not _text(session.get("date")):
        reasons.append("sample_session_unknown")
    else:
        raw_day = _text(session.get("date"))
        opened_at = _aware_datetime(session.get("open"))
        closed_at = _aware_datetime(session.get("close"))
        try:
            session_day = date.fromisoformat(str(raw_day or ""))
        except ValueError:
            session_day = None
        opened = opened_at.timestamp() if opened_at is not None else None
        closed = closed_at.timestamp() if closed_at is not None else None
        if (session_day is None or session_day.isoformat() != raw_day or
                opened is None or closed is None or
                closed - opened < COMPLETED_BAR_INTERVAL_SECONDS or
                session.get("source") != "alpaca_calendar"):
            reasons.append("sample_session_chronology_invalid")
        elif (opened_at.astimezone(NEW_YORK).date() != session_day or
              closed_at.astimezone(NEW_YORK).date() != session_day):
            reasons.append("sample_session_chronology_invalid")
    declared_freshness = sample.get("freshness_threshold_seconds", DEFAULT_MAX_AGE_SECONDS)
    try:
        freshness = _freshness_threshold(
            declared_freshness if max_age is None else max_age)
    except SessionAcceptanceError:
        freshness = DEFAULT_MAX_AGE_SECONDS
        reasons.append("freshness_threshold_invalid")
    declared_value = _finite(declared_freshness)
    if (declared_value is None or declared_value <= 0 or
            declared_value > DEFAULT_MAX_AGE_SECONDS):
        reasons.append("freshness_threshold_invalid")
    symbols = [str(x).strip().upper() for x in (expected or sample.get("expected_symbols") or ())
               if str(x).strip()]
    recorded_symbols = sample.get("expected_symbols")
    if expected is not None and (
            not isinstance(recorded_symbols, (list, tuple)) or
            sorted({str(x).strip().upper() for x in recorded_symbols if str(x).strip()}) != sorted(set(symbols))):
        reasons.append("expected_symbols_mismatch")
    recorder = sample.get("recorder")
    if not isinstance(recorder, Mapping):
        reasons.append("recorder_evidence_missing")
        recorder = {}
    status = str(recorder.get("status") or "unknown").lower()
    if status not in {"recording", "recording_market_closed", "running", "healthy", "ready"}:
        reasons.append(f"recorder_status:{status}")
    if recorder.get("fresh") is not True:
        reasons.append("recorder_stale_or_unknown")
    if recorder.get("market_session_status") not in {"open", "closed"}:
        reasons.append("recorder_session_status_unknown")
    observations = recorder.get("observation_ages")
    if not isinstance(observations, Mapping):
        reasons.append("observation_evidence_missing")
        observations = {}
    for symbol in symbols:
        value = observations.get(symbol)
        if not isinstance(value, Mapping):
            reasons.append(f"symbol:{symbol}:observation_missing")
            continue
        quote_age = _finite(value.get("quote_age_seconds"))
        if quote_age is None:
            reasons.append(f"symbol:{symbol}:quote_unknown")
        elif quote_age < 0:
            reasons.append(f"symbol:{symbol}:quote_future")
        elif quote_age > freshness:
            reasons.append(f"symbol:{symbol}:quote_stale")
        # Completed minute bars can naturally be 30-60 seconds old.  Gate on
        # their immutable completion watermark reaching the next publication
        # deadline; a fresh refetch of an old bar must not refresh acceptance.
        bar_age = _finite(value.get("bar_age_seconds"))
        watermark = value.get("bar_watermark")
        watermark_at = _aware_datetime(watermark)
        if bar_age is not None and bar_age < 0:
            reasons.append(f"symbol:{symbol}:bar_future")
        if (watermark_at is not None and captured is not None and
                (bar_age is None or
                 abs(bar_age - (captured - watermark_at.timestamp())) > 1.0)):
            reasons.append(f"symbol:{symbol}:bar_event_age_invalid")
        if captured is not None and opened is not None and closed is not None:
            publication_lag, publication_error = _completed_bar_publication_lag(
                watermark, captured=captured, opened=opened, closed=closed,
                tolerance=freshness)
            declared_lag = _finite(
                value.get("bar_publication_deadline_lag_seconds"))
            if publication_error is not None:
                reasons.append(f"symbol:{symbol}:{publication_error}")
            elif (declared_lag is None or publication_lag is None or
                  abs(declared_lag - publication_lag) > .001):
                reasons.append(f"symbol:{symbol}:bar_publication_evidence_invalid")
            elif publication_lag > freshness:
                reasons.append(f"symbol:{symbol}:bar_publication_stale")
    cadence = recorder.get("cadence")
    if not isinstance(cadence, Mapping):
        reasons.append("cadence_telemetry_missing")
    else:
        interval = _finite(cadence.get("configured_interval_seconds"))
        if interval is None or interval <= 0:
            reasons.append("cadence_telemetry_unknown")
    partition_bytes = recorder.get("partition_bytes")
    if partition_bytes is None:
        partition = recorder.get("partition")
        if isinstance(partition, Mapping):
            partition_bytes = partition.get("bytes")
    if _finite(partition_bytes) is None or float(partition_bytes) < 0:
        reasons.append("partition_bytes_missing")
    shadow = sample.get("shadow")
    if not isinstance(shadow, Mapping):
        reasons.append("shadow_evidence_missing")
        shadow = {}
    if str(shadow.get("status") or "unknown").lower() != "running":
        reasons.append(f"shadow_status:{str(shadow.get('status') or 'unknown').lower()}")
    if shadow.get("ok") is not True:
        reasons.append("shadow_health_not_ok")
    if shadow.get("fresh") is not True:
        reasons.append("shadow_stale_or_unknown")
    if shadow.get("last_error") not in (None, ""):
        reasons.append("shadow_failure")
    if shadow.get("candidate_errors_present") is not True:
        reasons.append("arm_errors_unknown")
    else:
        errors = shadow.get("candidate_errors")
        if isinstance(errors, Mapping) and errors:
            reasons.append("arm_errors_present")
        elif isinstance(errors, list) and errors:
            reasons.append("arm_errors_present")
        elif isinstance(errors, int) and errors != 0:
            reasons.append("arm_errors_present")
    diag = shadow.get("diagnostic_shadow")
    if not isinstance(diag, Mapping):
        reasons.append("diagnostic_coverage_missing")
        diag = {}
    reasons.extend(_diagnostic_reasons(
        diag, max_age=freshness, captured_ts=captured,
        session_open_ts=opened))
    sample_warmup = _text(sample.get("warmup_session"))
    diag_warmup = _text(diag.get("warmup_session"))
    if sample_warmup != diag_warmup:
        reasons.append("warmup_identity_mismatch")
    try:
        warmup_day = date.fromisoformat(str(sample_warmup or ""))
    except (TypeError, ValueError):
        reasons.append("warmup_chronology_invalid")
    else:
        if session_day is None or warmup_day >= session_day:
            reasons.append("warmup_chronology_invalid")
    identities = sample.get("identities")
    if not isinstance(identities, Mapping):
        reasons.append("identities_missing")
    else:
        for key in ("deployment", "code", "cohort", "activation"):
            if not _text(identities.get(key)):
                reasons.append(f"identity_missing:{key}")
        if _text(diag.get("code_identity")) and identities.get("code") != diag.get("code_identity"):
            reasons.append("code_identity_mismatch")
        if _text(diag.get("cohort_identity")) and identities.get("cohort") != diag.get("cohort_identity"):
            reasons.append("cohort_identity_mismatch")
        if (_text(diag.get("activation_identity")) and
                identities.get("activation") != diag.get("activation_identity")):
            reasons.append("activation_identity_mismatch")
        # Pure summaries may receive a single already-paritied deployment
        # identity.  Captures include both source fields, so their omission
        # remains a visible unknown rather than being silently inferred.
        if ("deployment_recorder" in identities or
                "deployment_shadow" in identities):
            if not _text(identities.get("deployment_recorder")) or not _text(
                    identities.get("deployment_shadow")):
                reasons.append("deployment_identity_parity_unknown")
            elif identities.get("deployment_recorder") != identities.get("deployment_shadow"):
                reasons.append("deployment_identity_drift")
    if sample.get("operational_only") is not True or sample.get("authorizing") is not False:
        reasons.append("non_authorizing_contract_invalid")
    return sorted(set(reasons))


def _activation_marker(diag: Mapping[str, object]) -> dict | None:
    value = diag.get("activation_event_watermark")
    if not isinstance(value, Mapping):
        return None
    inserted = _finite(value.get("last_inserted_at"))
    count = _nonnegative_int(value.get("count"))
    decision_count = _nonnegative_int(value.get("decision_event_count"))
    if inserted is None or inserted < 0 or count is None or decision_count is None:
        return None
    return {
        "last_inserted_at": inserted,
        "last_event_key": _text(value.get("last_event_key")) or "",
        "count": count,
        "decision_event_count": decision_count,
    }


def _progress_snapshot(
        diag: Mapping[str, object]) -> dict[str, tuple[float, str, int]] | None:
    cursors = _cursor_map(diag)
    if len(cursors) != 24:
        return None
    result: dict[str, tuple[float, str, int]] = {}
    for candidate_id, cursor in cursors.items():
        inserted = _finite(cursor.get("last_inserted_at"))
        key = _text(cursor.get("last_event_key"))
        processed = _nonnegative_int(cursor.get("processed_events"))
        if inserted is None or inserted < 0 or not key or processed is None:
            return None
        result[candidate_id] = (inserted, key, processed)
    return result


def _diagnostic_reasons(diag: Mapping[str, object], *, max_age: float,
                        captured_ts: float | None,
                        session_open_ts: float | None) -> list[str]:
    reasons: list[str] = []
    if diag.get("schema") != DIAGNOSTIC_COVERAGE_SCHEMA:
        reasons.append("diagnostic_schema_invalid")
    if not (diag.get("enabled") is True and diag.get("diagnostic") is True and
            diag.get("authorizing") is False and diag.get("gate_eligible") is False and
            diag.get("promotion_eligible") is False and diag.get("actual_fills") == 0):
        reasons.append("diagnostic_authority_contract_invalid")
    if diag.get("activation_status") != "active":
        reasons.append("activation_status_invalid")
    source_lag = _finite(diag.get("source_lag_seconds"))
    if source_lag is None:
        reasons.append("shadow_source_lag_unknown")
    elif source_lag < 0:
        reasons.append("shadow_source_lag_future")
    elif source_lag > max_age:
        reasons.append("shadow_source_lag_stale")
    if _text(diag.get("observation_status")) in (
            None, "awaiting_forward_activation", "no_post_activation_events"):
        reasons.append("post_activation_observation_missing")
    activation = _activation_marker(diag)
    if activation is None:
        reasons.append("activation_watermark_invalid")
    elif (captured_ts is not None and
          activation["last_inserted_at"] >
          captured_ts + CLOCK_SKEW_TOLERANCE_SECONDS):
        reasons.append("activation_watermark_future")
    elif (session_open_ts is not None and
          activation["last_inserted_at"] >= session_open_ts):
        reasons.append("activation_not_pre_session")
    arms = diag.get("arms")
    candidates = diag.get("candidate_identities")
    if not isinstance(arms, list) or len(arms) != 24:
        reasons.append("arms_count_invalid")
        return reasons
    if not isinstance(candidates, list) or len(candidates) != 24 or len(set(map(str, candidates))) != 24:
        reasons.append("candidate_identities_invalid")
    families: dict[str, set[str]] = {}
    arm_ids: set[str] = set()
    code_values: set[str] = set()
    cohort_values: set[str] = set()
    for arm in arms:
        if not isinstance(arm, Mapping):
            reasons.append("arm_invalid")
            continue
        cid = _text(arm.get("candidate_id"))
        family = _text(arm.get("family"))
        role = _text(arm.get("role"))
        if not cid or not family or role not in {"baseline", "variant"}:
            reasons.append("arm_identity_invalid")
        if cid:
            if cid in arm_ids:
                reasons.append("arm_identity_duplicate")
            arm_ids.add(cid)
        if family and role:
            families.setdefault(family, set()).add(role)
        if _text(arm.get("code_identity")):
            code_values.add(str(arm["code_identity"]))
        if _text(arm.get("cohort_identity")):
            cohort_values.add(str(arm["cohort_identity"]))
    if len(families) != 12 or any(value != {"baseline", "variant"} for value in families.values()):
        reasons.append("arm_family_coverage_invalid")
    if isinstance(candidates, list) and len(candidates) == 24 and set(map(str, candidates)) != arm_ids:
        reasons.append("candidate_arm_identity_mismatch")
    if len(code_values) != 1 or len(cohort_values) != 1:
        reasons.append("arm_identity_metadata_invalid")
    if len(code_values) == 1 and _text(diag.get("code_identity")) not in code_values:
        reasons.append("arm_code_identity_mismatch")
    if len(cohort_values) == 1 and _text(diag.get("cohort_identity")) not in cohort_values:
        reasons.append("arm_cohort_identity_mismatch")
    cursors = _cursor_map(diag)
    if len(cursors) != 24 or set(cursors) != arm_ids:
        reasons.append("arm_cursors_missing")
    else:
        values: list[tuple[object, object, object]] = []
        processed_total = 0
        for cursor in cursors.values():
            stamp = _finite(cursor.get("last_inserted_at"))
            key = _text(cursor.get("last_event_key"))
            count = _nonnegative_int(cursor.get("processed_events"))
            if stamp is None or stamp < 0 or not key or count is None:
                reasons.append("arm_cursor_invalid")
                continue
            values.append((stamp, key, count))
            processed_total += count
            if count <= 0 or (activation is not None and
                              (stamp, key) <= (
                                  activation["last_inserted_at"],
                                  activation["last_event_key"])):
                reasons.append("arm_post_activation_progress_missing")
            if (captured_ts is not None and
                    stamp > captured_ts + CLOCK_SKEW_TOLERANCE_SECONDS):
                reasons.append("arm_cursor_future")
            elif (captured_ts is not None and
                  captured_ts - stamp > max_age):
                reasons.append("arm_cursor_stale")
        if values and len(set(values)) != 1:
            reasons.append("arm_cursors_mismatch")
        reported_processed = _nonnegative_int(diag.get("processed_events"))
        if reported_processed is None or reported_processed != processed_total:
            reasons.append("diagnostic_processed_events_invalid")
    if diag.get("health_cursor_matches") is False:
        reasons.append("arm_health_cursor_mismatch")
    if diag.get("families_total") != 12 or diag.get("baseline_count") != 12 or diag.get("variant_count") != 12:
        reasons.append("diagnostic_family_counts_invalid")
    return reasons


def _stats(values: Sequence[float]) -> dict:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    def quantile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
        return round(ordered[index], 6)
    return {"count": len(ordered), "p50": quantile(.50), "p95": quantile(.95),
            "max": round(ordered[-1], 6)}


def summarize_session(samples: Sequence[Mapping[str, object]], *, session: Mapping[str, object],
                      expected: Sequence[str], now: float | None = None,
                      start_tolerance: float = DEFAULT_START_TOLERANCE_SECONDS,
                      end_tolerance: float = DEFAULT_END_TOLERANCE_SECONDS,
                      max_sample_gap: float = DEFAULT_MAX_SAMPLE_GAP_SECONDS) -> dict:
    """Deterministically summarize samples; acceptance is strictly fail-closed."""
    expected_list = sorted({str(x).strip().upper() for x in expected if str(x).strip()})
    session_date = _text(session.get("date"))
    opened_at = _aware_datetime(session.get("open"))
    closed_at = _aware_datetime(session.get("close"))
    opened = opened_at.timestamp() if opened_at is not None else None
    closed = closed_at.timestamp() if closed_at is not None else None
    reasons: list[str] = []
    if not expected_list:
        reasons.append("expected_symbols_missing")
    try:
        session_day = date.fromisoformat(str(session_date or ""))
    except ValueError:
        session_day = None
    if (session_day is None or opened is None or closed is None or
            closed - opened < COMPLETED_BAR_INTERVAL_SECONDS or
            session.get("source") != "alpaca_calendar"):
        reasons.append("session_calendar_invalid")
    elif (opened_at.astimezone(NEW_YORK).date() != session_day or
          closed_at.astimezone(NEW_YORK).date() != session_day):
        reasons.append("session_chronology_invalid")
    declared_open = _finite(session.get("open_ts"))
    declared_close = _finite(session.get("close_ts"))
    if ((declared_open is not None and opened is not None and
         abs(declared_open - opened) > .001) or
            (declared_close is not None and closed is not None and
             abs(declared_close - closed) > .001)):
        reasons.append("session_calendar_bounds_mismatch")
    samples_list = [dict(item) for item in samples if isinstance(item, Mapping)]
    if len(samples_list) != len(samples):
        reasons.append("sample_record_invalid")
    if len(samples_list) > MAX_SAMPLES:
        reasons.append("sample_bound_exceeded")
        samples_list = samples_list[:MAX_SAMPLES]
    ordered = sorted(samples_list, key=lambda item: _finite(item.get("captured_ts")) or -math.inf)
    timestamps = [_finite(item.get("captured_ts")) for item in ordered]
    timestamps_valid = [value for value in timestamps if value is not None]
    report_now = time.time() if now is None else float(now)
    if not math.isfinite(report_now):
        reasons.append("report_time_invalid")
        report_now = max(timestamps_valid, default=0.0)
    if closed is not None and report_now < closed:
        reasons.append("session_not_closed")
    if any(value is None for value in timestamps):
        reasons.append("sample_timestamp_unknown")
    if any(value is not None and value > report_now for value in timestamps):
        reasons.append("future_evidence")
    if not ordered:
        reasons.append("samples_missing")
    if opened is not None and closed is not None and timestamps_valid:
        if timestamps_valid[0] > opened + float(start_tolerance):
            reasons.append("coverage_start_missing")
        if timestamps_valid[-1] < closed - float(end_tolerance):
            reasons.append("coverage_end_missing")
        if timestamps_valid[0] < opened - float(start_tolerance):
            reasons.append("sample_before_session_window")
        if timestamps_valid[-1] > closed + float(end_tolerance):
            reasons.append("sample_after_session_window")
        gaps = [right - left for left, right in zip(timestamps_valid, timestamps_valid[1:])]
        if any(gap > float(max_sample_gap) for gap in gaps):
            reasons.append("sample_gap_detected")
    warmups: set[str] = set()
    healthy_count = 0
    symbol_failures: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    identity_values: list[tuple[object, ...]] = []
    duration_values: list[float] = []
    activation_markers: list[dict] = []
    progress_snapshots: list[dict[str, tuple[float, str, int]]] = []
    quote_age_values: list[float] = []
    bar_event_age_values: list[float] = []
    bar_publication_lag_values: list[float] = []
    shadow_source_lag_values: list[float] = []
    for sample in ordered:
        if session_date and isinstance(sample.get("session"), Mapping):
            if sample["session"].get("date") != session_date:
                reasons.append("sample_session_mismatch")
            if (sample["session"].get("open") != session.get("open") or
                    sample["session"].get("close") != session.get("close") or
                    sample["session"].get("source") != session.get("source")):
                reasons.append("sample_calendar_bounds_mismatch")
        local_reasons = sample_reasons(sample, expected=expected_list)
        recorded_reasons = sample.get("sample_reasons")
        if isinstance(recorded_reasons, list):
            local_reasons.extend(
                str(reason)[:200] for reason in recorded_reasons[:64]
                if isinstance(reason, str) and reason)
            local_reasons = sorted(set(local_reasons))
        if not local_reasons:
            healthy_count += 1
        failed_symbols = {
            reason.split(":", 2)[1]
            for reason in local_reasons
            if reason.startswith("symbol:") and ":" in reason[7:]
        }
        for symbol in failed_symbols:
            symbol_failures[symbol] += 1
        for reason in local_reasons:
            reason_counts[reason] += 1
            reasons.append(reason)
        warmup = _text(sample.get("warmup_session"))
        if warmup:
            warmups.add(warmup)
        identities = sample.get("identities")
        if isinstance(identities, Mapping):
            identity_values.append(tuple(identities.get(key) for key in ("deployment", "code", "cohort", "activation")))
        shadow = sample.get("shadow")
        if isinstance(shadow, Mapping):
            value = _finite(shadow.get("poll_duration_seconds"))
            if value is not None and value >= 0:
                duration_values.append(value)
            diag = shadow.get("diagnostic_shadow")
            if isinstance(diag, Mapping):
                marker = _activation_marker(diag)
                progress = _progress_snapshot(diag)
                if marker is not None and progress is not None:
                    activation_markers.append(marker)
                    progress_snapshots.append(progress)
                source_lag = _finite(diag.get("source_lag_seconds"))
                if source_lag is not None:
                    shadow_source_lag_values.append(source_lag)
        recorder = sample.get("recorder")
        observations = (recorder.get("observation_ages")
                        if isinstance(recorder, Mapping) else None)
        if isinstance(observations, Mapping):
            for observation in observations.values():
                if not isinstance(observation, Mapping):
                    continue
                for key, target in (
                        ("quote_age_seconds", quote_age_values),
                        ("bar_age_seconds", bar_event_age_values),
                        ("bar_publication_deadline_lag_seconds",
                         bar_publication_lag_values)):
                    value = _finite(observation.get(key))
                    if value is not None:
                        target.append(value)
    if len(warmups) != 1:
        reasons.append("warmup_session_unknown")
    else:
        try:
            warmup_day = date.fromisoformat(next(iter(warmups)))
        except ValueError:
            reasons.append("warmup_chronology_invalid")
        else:
            if session_day is None or warmup_day >= session_day:
                reasons.append("warmup_chronology_invalid")
            if session_date in warmups:
                reasons.append("warmup_session_excluded")
    if identity_values and len(set(identity_values)) != 1:
        reasons.append("identity_drift")
    if len(identity_values) != len(ordered):
        reasons.append("identity_evidence_missing")
    progress_summary = {
        "arms": 0, "snapshots": len(progress_snapshots),
        "all_arms_progressed": False, "minimum_processed_events": None,
        "minimum_session_delta": None, "activation_watermark": None,
    }
    if len(progress_snapshots) != len(ordered) or not progress_snapshots:
        reasons.append("post_activation_progress_evidence_missing")
    else:
        first = progress_snapshots[0]
        final = progress_snapshots[-1]
        candidate_ids = set(first)
        identities_stable = all(set(snapshot) == candidate_ids
                                for snapshot in progress_snapshots)
        if len(candidate_ids) != 24 or not identities_stable:
            reasons.append("arm_cursor_identity_drift")
        marker_stable = bool(activation_markers) and all(
            marker == activation_markers[0] for marker in activation_markers)
        if not marker_stable:
            reasons.append("activation_watermark_drift")
        elif (opened is None or
              activation_markers[0]["last_inserted_at"] >= opened):
            reasons.append("activation_not_pre_session")
        regression = False
        if identities_stable:
            for previous, current in zip(
                    progress_snapshots, progress_snapshots[1:]):
                for candidate_id in candidate_ids:
                    if (current[candidate_id][2] < previous[candidate_id][2] or
                            current[candidate_id][:2] < previous[candidate_id][:2]):
                        regression = True
                        break
                if regression:
                    break
        if regression:
            reasons.append("arm_cursor_regression")
        deltas = ([final[candidate_id][2] - first[candidate_id][2]
                   for candidate_id in candidate_ids]
                  if identities_stable else [])
        all_progressed = bool(
            len(candidate_ids) == 24 and not regression and deltas and
            all(value > 0 for value in deltas))
        if not all_progressed:
            reasons.append("session_arm_progress_missing")
        final_counts = [value[2] for value in final.values()]
        progress_summary = {
            "arms": len(candidate_ids), "snapshots": len(progress_snapshots),
            "all_arms_progressed": all_progressed,
            "minimum_processed_events": min(final_counts, default=None),
            "minimum_session_delta": min(deltas, default=None),
            "activation_watermark": (activation_markers[0]
                                     if marker_stable else None),
        }
    intervals = [right - left for left, right in zip(timestamps_valid, timestamps_valid[1:])
                 if right >= left]
    recorder_cadence: list[float] = []
    configured_cadence: list[float] = []
    for sample in ordered:
        recorder = sample.get("recorder")
        cadence = recorder.get("cadence") if isinstance(recorder, Mapping) else None
        value = _finite(cadence.get("realized_interval_seconds")) if isinstance(cadence, Mapping) else None
        if value is not None and value >= 0:
            recorder_cadence.append(value)
        configured = _finite(cadence.get("configured_interval_seconds")) if isinstance(cadence, Mapping) else None
        if configured is not None and configured > 0:
            configured_cadence.append(configured)
    if not recorder_cadence:
        reasons.append("cadence_evidence_missing")
    if not duration_values:
        reasons.append("duration_evidence_missing")
    reasons = sorted(set(reasons))
    for reason in reasons:
        reason_counts.setdefault(reason, 1)
    report = {
        "schema": REPORT_SCHEMA,
        "session": {key: session.get(key) for key in ("date", "open", "close", "source")},
        "accepted": not reasons,
        "status": "accepted" if not reasons else "rejected",
        "operational_only": True,
        "authorizing": False,
        "promotion_eligible": False,
        "expected_symbols": expected_list,
        "reasons": reasons,
        "reason_counts": dict(sorted(reason_counts.items())),
        "sample_counts": {"total": len(ordered), "healthy": healthy_count,
                           "failed": len(ordered) - healthy_count,
                           "valid_timestamps": len(timestamps_valid)},
        "symbol_failure_counts": dict(sorted(symbol_failures.items())),
        "coverage": {"first_sample_ts": timestamps_valid[0] if timestamps_valid else None,
                     "last_sample_ts": timestamps_valid[-1] if timestamps_valid else None,
                     "open_ts": opened, "close_ts": closed,
                     "start_tolerance_seconds": float(start_tolerance),
                     "end_tolerance_seconds": float(end_tolerance),
                     "max_sample_gap_seconds": float(max_sample_gap),
                     "closed_at_report": bool(closed is not None and report_now >= closed),
                     "max_observed_gap_seconds": max(intervals, default=None)},
        "duration_seconds": _stats(duration_values),
        "freshness": {
            "strict_threshold_cap_seconds": DEFAULT_MAX_AGE_SECONDS,
            "quote_event_age_seconds": _stats(quote_age_values),
            "bar_event_age_seconds": _stats(bar_event_age_values),
            "bar_publication_deadline_lag_seconds": _stats(
                bar_publication_lag_values),
            "shadow_source_lag_seconds": _stats(shadow_source_lag_values),
        },
        "cadence_seconds": {"sample_intervals": _stats(intervals),
                            "recorder_realized_intervals": _stats(recorder_cadence),
                            "configured_intervals": _stats(configured_cadence)},
        "storage_growth": _storage_growth(ordered),
        "identities": (dict(zip(("deployment", "code", "cohort", "activation"), identity_values[-1]))
                       if identity_values else {key: None for key in ("deployment", "code", "cohort", "activation")}),
        "warmup_sessions": sorted(warmups),
        "arm_progress": _latest_arm_progress(ordered),
        "post_activation_progress": progress_summary,
    }
    return report


def _storage_growth(samples: Sequence[Mapping[str, object]]) -> dict:
    values: list[int] = []
    for sample in samples:
        recorder = sample.get("recorder")
        value = recorder.get("partition_bytes") if isinstance(recorder, Mapping) else None
        if value is None and isinstance(recorder, Mapping):
            partition = recorder.get("partition")
            value = partition.get("bytes") if isinstance(partition, Mapping) else None
        number = _finite(value)
        if number is not None and number >= 0:
            values.append(int(number))
    return {"samples": len(values), "first_bytes": values[0] if values else None,
            "last_bytes": values[-1] if values else None,
            "growth_bytes": values[-1] - values[0] if values else None,
            "max_bytes": max(values, default=None)}


def _latest_arm_progress(samples: Sequence[Mapping[str, object]]) -> dict:
    if not samples:
        return {"arms": 0, "cursors": {}}
    shadow = samples[-1].get("shadow")
    diag = shadow.get("diagnostic_shadow") if isinstance(shadow, Mapping) else None
    cursors = _cursor_map(diag) if isinstance(diag, Mapping) else {}
    return {"arms": len(cursors), "cursors": {key: dict(value) for key, value in sorted(cursors.items())}}


def read_session_samples(output_dir: str | Path, session_date: str) -> list[dict]:
    path = session_file(output_dir, session_date)
    if not path.is_file():
        raise SessionAcceptanceError(f"session_samples_missing:{path}")
    if path.is_symlink():
        raise SessionAcceptanceError(f"session_samples_symlink:{path}")
    try:
        if path.stat().st_size > MAX_SAMPLE_FILE_BYTES:
            raise SessionAcceptanceError("session_samples_size_bound_exceeded")
    except OSError as exc:
        raise SessionAcceptanceError(f"session_samples_read_failed:{path}: {exc}") from exc
    result: list[dict] = []
    total_bytes = 0
    try:
        with path.open("rb") as handle:
            line_number = 0
            while True:
                line = handle.readline(MAX_SAMPLE_BYTES + 1)
                if not line:
                    break
                line_number += 1
                total_bytes += len(line)
                if total_bytes > MAX_SAMPLE_FILE_BYTES:
                    raise SessionAcceptanceError(
                        "session_samples_size_bound_exceeded")
                if not line.strip():
                    continue
                if len(line) > MAX_SAMPLE_BYTES:
                    raise SessionAcceptanceError(
                        f"session_sample_size_bound_exceeded:{path}:{line_number}")
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise SessionAcceptanceError(
                        f"session_samples_corrupt:{path}:{line_number}: {exc}") from exc
                if not isinstance(value, dict):
                    raise SessionAcceptanceError(f"session_samples_corrupt:{path}:{line_number}")
                result.append(value)
                if len(result) > MAX_SAMPLES:
                    raise SessionAcceptanceError("session_samples_bound_exceeded")
    except OSError as exc:
        raise SessionAcceptanceError(f"session_samples_read_failed:{path}: {exc}") from exc
    return result


def _last_sample_id(path: Path) -> str | None:
    """Read only the bounded tail needed for restart-safe duplicate suppression."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SessionAcceptanceError(f"session_samples_read_failed:{path}: {exc}") from exc
    if size <= 0:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, size - MAX_SAMPLE_BYTES))
            tail = handle.read(MAX_SAMPLE_BYTES)
    except OSError as exc:
        raise SessionAcceptanceError(f"session_samples_read_failed:{path}: {exc}") from exc
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SessionAcceptanceError(f"session_samples_corrupt:{path}:tail") from exc
    return _text(value.get("sample_id")) if isinstance(value, Mapping) else None


class SessionSampleWriter:
    """Append one bounded, durable sample at a time."""

    def __init__(self, output_dir: str | Path, session_date: str) -> None:
        self.path = session_file(output_dir, session_date)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise SessionAcceptanceError(f"session_samples_symlink:{self.path}")
        _ensure_file_slot(self.path)
        self._handle = self.path.open("a", encoding="utf-8")

    def write(self, sample: Mapping[str, object]) -> bool:
        try:
            encoded = json.dumps(dict(sample), sort_keys=True, allow_nan=False)
            encoded_bytes = len(encoded.encode("utf-8")) + 1
            if encoded_bytes > MAX_SAMPLE_BYTES:
                raise SessionAcceptanceError("session_sample_size_bound_exceeded")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            if (_text(sample.get("sample_id")) is not None and
                    _last_sample_id(self.path) == _text(sample.get("sample_id"))):
                return False
            if self.path.stat().st_size + encoded_bytes > MAX_SAMPLE_FILE_BYTES:
                raise SessionAcceptanceError("session_samples_size_bound_exceeded")
            self._handle.write(encoded + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            return True
        except (OSError, TypeError, ValueError) as exc:
            raise SessionAcceptanceError(f"session_samples_write_failed:{self.path}: {exc}") from exc
        finally:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError as exc:
            raise SessionAcceptanceError(f"session_samples_close_failed:{self.path}: {exc}") from exc

    def __enter__(self) -> "SessionSampleWriter":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def _atomic_report(path: Path, report: Mapping[str, object]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SessionAcceptanceError(f"session_report_symlink:{path}")
    _ensure_file_slot(path)
    payload = dict(report)
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise SessionAcceptanceError("session_report_size_bound_exceeded")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise SessionAcceptanceError(f"session_report_write_failed:{path}: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return payload


def _rejected_report(session_date: str, reason: str, *, now: float) -> dict:
    return {
        "schema": REPORT_SCHEMA,
        "session": {"date": str(session_date), "open": None, "close": None,
                    "source": None},
        "accepted": False,
        "status": "rejected",
        "operational_only": True,
        "authorizing": False,
        "promotion_eligible": False,
        "expected_symbols": [],
        "reasons": [str(reason)[:240]],
        "reason_counts": {str(reason)[:240]: 1},
        "sample_counts": {"total": 0, "healthy": 0, "failed": 0,
                          "valid_timestamps": 0},
        "identities": {key: None for key in
                       ("deployment", "code", "cohort", "activation")},
        "finalized_ts": float(now),
    }


def _failed_sample(session: Mapping[str, object], expected: Sequence[str], *,
                   captured: float, freshness: float, reason: str) -> dict:
    return {
        "schema": SAMPLE_SCHEMA,
        "sample_id": f"{session.get('date')}:{captured:.6f}:failure:{reason[:80]}",
        "captured_ts": captured,
        "freshness_threshold_seconds": freshness,
        "session": {key: session.get(key)
                    for key in ("date", "open", "close", "source")},
        "expected_symbols": list(expected),
        "recorder": {},
        "shadow": {},
        "identities": {},
        "warmup_session": None,
        "sample_status": "failed",
        "sample_reasons": [f"capture_failed:{reason[:160]}"],
        "operational_only": True,
        "authorizing": False,
        "promotion_eligible": False,
    }


def finalize_session(recorder_root: str | Path, output_dir: str | Path,
                     session_date: str, *, now: float | None = None,
                     expected: Sequence[str] | None = None,
                     start_tolerance: float = DEFAULT_START_TOLERANCE_SECONDS,
                     end_tolerance: float = DEFAULT_END_TOLERANCE_SECONDS,
                     max_sample_gap: float = DEFAULT_MAX_SAMPLE_GAP_SECONDS) -> dict:
    """Atomically persist one fail-closed report after the exact session close."""
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        raise SessionAcceptanceError("report_time_invalid")
    destination = report_file(output_dir, str(session_date))
    if destination.is_file():
        existing = _read_json(
            destination, "session_report", max_bytes=MAX_REPORT_BYTES)
        session = existing.get("session")
        if (existing.get("schema") != REPORT_SCHEMA or
                not isinstance(existing.get("accepted"), bool) or
                not isinstance(session, Mapping) or
                session.get("date") != str(session_date) or
                existing.get("authorizing") is not False):
            raise SessionAcceptanceError(f"session_report_corrupt:{destination}")
        return existing
    try:
        index = _read_json(recorder_index_path(recorder_root), "recorder_index")
        selected = _session(index, str(session_date))
        symbols = expected_symbols(index, expected)
    except SessionAcceptanceError as exc:
        return _atomic_report(
            destination, _rejected_report(str(session_date), str(exc), now=current))
    if current < selected["close_ts"]:
        raise SessionAcceptanceError("session_not_closed")
    try:
        samples = read_session_samples(output_dir, str(session_date))
    except SessionAcceptanceError as exc:
        if str(exc).startswith("session_samples_missing:"):
            samples = []
        else:
            report = summarize_session(
                [], session=selected, expected=symbols, now=current,
                start_tolerance=start_tolerance,
                end_tolerance=end_tolerance,
                max_sample_gap=max_sample_gap)
            reason = str(exc)[:240]
            report["reasons"] = sorted(set([*report["reasons"], reason]))
            report["reason_counts"][reason] = 1
            report["finalized_ts"] = current
            return _atomic_report(destination, report)
    report = summarize_session(
        samples, session=selected, expected=symbols, now=current,
        start_tolerance=start_tolerance, end_tolerance=end_tolerance,
        max_sample_gap=max_sample_gap)
    report["finalized_ts"] = current
    return _atomic_report(destination, report)


def _selected_session_date(index: Mapping[str, object], now: float,
                           output_dir: str | Path) -> str:
    calendar = index.get("session_calendar")
    if not isinstance(calendar, Mapping):
        raise SessionAcceptanceError("session_calendar_missing")
    local_day = datetime.fromtimestamp(now, timezone.utc).astimezone(
        NEW_YORK).date().isoformat()
    candidates = sorted(
        str(day) for day, value in calendar.items()
        if str(day) <= local_day and isinstance(value, Mapping) and
        value.get("status") != "closed")
    if not candidates:
        raise SessionAcceptanceError("selected_session_unknown")
    pending_closed: list[str] = []
    active: list[str] = []
    invalid: list[str] = []
    upcoming: list[str] = []
    for day in candidates:
        if report_file(output_dir, day).is_file():
            continue
        value = calendar.get(day)
        opened = _epoch(value.get("open")) if isinstance(value, Mapping) else None
        closed = _epoch(value.get("close")) if isinstance(value, Mapping) else None
        if opened is None or closed is None or not opened < closed:
            invalid.append(day)
        elif closed <= now:
            pending_closed.append(day)
        elif opened <= now:
            active.append(day)
        else:
            upcoming.append(day)
    if pending_closed:
        return pending_closed[-1]
    if active:
        return active[-1]
    if invalid:
        return invalid[-1]
    if upcoming:
        return upcoming[-1]
    return candidates[-1]


def record_poll(recorder_root: str | Path, shadow_health_path: str | Path,
                output_dir: str | Path, *, session_date: str | None = None,
                now: float | None = None,
                interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                gap_tolerance_seconds: float = DEFAULT_SAMPLE_GAP_TOLERANCE_SECONDS,
                freshness_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> dict:
    """Record one shadow-loop poll or finalize the latest closed session."""
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        raise SessionAcceptanceError("capture_time_invalid")
    max_sample_gap = _gap_threshold(interval_seconds, gap_tolerance_seconds)
    freshness = _freshness_threshold(freshness_seconds)
    selected_date: str | None = None
    try:
        index = _read_json(recorder_index_path(recorder_root), "recorder_index")
        selected_date = (str(session_date) if session_date is not None else
                         _selected_session_date(index, current, output_dir))
        selected = _session(index, selected_date)
    except SessionAcceptanceError as exc:
        if selected_date is None:
            return {"schema": SCHEMA, "status": "unavailable", "accepted": False,
                    "authorizing": False, "reason": str(exc)}
        destination = report_file(output_dir, selected_date)
        if destination.is_file():
            report = _read_json(
                destination, "session_report", max_bytes=MAX_REPORT_BYTES)
            return {"schema": SCHEMA, "status": "finalized",
                    "accepted": report.get("accepted") is True,
                    "authorizing": False, "session_date": selected_date,
                    "report": report}
        report = _atomic_report(
            destination,
            _rejected_report(selected_date, str(exc), now=current))
        return {"schema": SCHEMA, "status": "finalized", "accepted": False,
                "authorizing": False, "session_date": selected_date,
                "report": report}
    destination = report_file(output_dir, selected_date)
    if destination.is_file():
        report = finalize_session(
            recorder_root, output_dir, selected_date, now=current,
            max_sample_gap=max_sample_gap)
        return {"schema": SCHEMA, "status": "finalized",
                "accepted": report.get("accepted") is True,
                "authorizing": False, "session_date": selected_date,
                "report": report}
    if current < selected["open_ts"]:
        return {"schema": SCHEMA, "status": "waiting_for_open", "accepted": False,
                "authorizing": False, "session_date": selected_date}
    recorded = False
    capture_error = None
    try:
        symbols = expected_symbols(index)
    except SessionAcceptanceError:
        symbols = []
    if current <= selected["close_ts"]:
        try:
            sample = capture_sample(
                recorder_root, shadow_health_path, selected_date, now=current,
                max_age=freshness)
            with SessionSampleWriter(output_dir, selected_date) as writer:
                recorded = writer.write(sample)
        except SessionAcceptanceError as exc:
            capture_error = str(exc)
            try:
                with SessionSampleWriter(output_dir, selected_date) as writer:
                    recorded = writer.write(_failed_sample(
                        selected, symbols, captured=current,
                        freshness=freshness, reason=capture_error))
            except SessionAcceptanceError:
                recorded = False
    if current >= selected["close_ts"]:
        report = finalize_session(
            recorder_root, output_dir, selected_date, now=current,
            max_sample_gap=max_sample_gap)
        return {"schema": SCHEMA, "status": "finalized",
                "accepted": report.get("accepted") is True,
                "authorizing": False, "session_date": selected_date,
                "capture_error": capture_error, "report": report}
    return {"schema": SCHEMA,
            "status": ("sample_rejected" if capture_error is not None else
                       "recorded" if recorded else "duplicate"),
            "accepted": False, "authorizing": False,
            "session_date": selected_date, "capture_error": capture_error}


def report_session(recorder_root: str | Path, output_dir: str | Path, session_date: str,
                   *, now: float | None = None, expected: Sequence[str] | None = None,
                   start_tolerance: float = DEFAULT_START_TOLERANCE_SECONDS,
                   end_tolerance: float = DEFAULT_END_TOLERANCE_SECONDS,
                   max_sample_gap: float = DEFAULT_MAX_SAMPLE_GAP_SECONDS) -> dict:
    index = _read_json(recorder_index_path(recorder_root), "recorder_index")
    selected = _session(index, str(session_date))
    symbols = expected_symbols(index, expected)
    samples = read_session_samples(output_dir, str(session_date))
    return summarize_session(samples, session=selected, expected=symbols, now=now,
                             start_tolerance=start_tolerance,
                             end_tolerance=end_tolerance,
                             max_sample_gap=max_sample_gap)


def monitor(recorder_root: str | Path, shadow_health_path: str | Path,
            output_dir: str | Path, *, session_date: str | None = None,
            interval: float = DEFAULT_INTERVAL_SECONDS,
            max_age: float = DEFAULT_MAX_AGE_SECONDS,
            start_tolerance: float = DEFAULT_START_TOLERANCE_SECONDS,
            end_tolerance: float = DEFAULT_END_TOLERANCE_SECONDS,
            max_sample_gap: float = DEFAULT_MAX_SAMPLE_GAP_SECONDS,
            once: bool = False, clock=time.time, sleeper=time.sleep) -> dict:
    if not math.isfinite(float(interval)) or float(interval) <= 0:
        raise SessionAcceptanceError("monitor_interval_invalid")
    index = _read_json(recorder_index_path(recorder_root), "recorder_index")
    if session_date is None:
        calendar = index.get("session_calendar")
        candidates = []
        current = float(clock())
        if isinstance(calendar, Mapping):
            for day, value in calendar.items():
                if isinstance(value, Mapping) and _epoch(value.get("close")) is not None:
                    if _epoch(value.get("open")) <= current:
                        candidates.append(str(day))
        if not candidates:
            raise SessionAcceptanceError("selected_session_unknown")
        session_date = sorted(candidates)[-1]
    selected = _session(index, str(session_date))
    symbols = expected_symbols(index)
    if not once:
        while float(clock()) < selected["open_ts"]:
            remaining = selected["open_ts"] - float(clock())
            sleeper(min(float(interval), max(0.0, remaining)))
    with SessionSampleWriter(output_dir, str(session_date)) as writer:
        while True:
            captured = float(clock())
            sample = capture_sample(recorder_root, shadow_health_path, str(session_date),
                                    now=captured, expected=symbols, max_age=max_age)
            writer.write(sample)
            # Stop at the exchange close.  Sampling after close would turn a
            # healthy last market observation into a stale sample while
            # waiting for the bounded report end tolerance.
            if once or captured >= selected["close_ts"]:
                break
            sleeper(float(interval))
    return summarize_session(read_session_samples(output_dir, str(session_date)),
                             session=selected, expected=symbols, now=float(clock()),
                             start_tolerance=start_tolerance,
                             end_tolerance=end_tolerance,
                             max_sample_gap=max_sample_gap)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--recorder-root", type=Path, default=Path("runtime/research/recorded"))
    common.add_argument("--output-dir", type=Path, default=Path("runtime/research/session-acceptance"))
    common.add_argument("--session-date", required=True)
    common.add_argument("--start-tolerance", type=float, default=DEFAULT_START_TOLERANCE_SECONDS)
    common.add_argument("--end-tolerance", type=float, default=DEFAULT_END_TOLERANCE_SECONDS)
    common.add_argument("--max-sample-gap", type=float, default=DEFAULT_MAX_SAMPLE_GAP_SECONDS)
    sub.add_parser("report", parents=[common])
    monitor_parser = sub.add_parser("monitor", parents=[common])
    monitor_parser.add_argument("--shadow-health", type=Path, required=True)
    monitor_parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    monitor_parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    monitor_parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "report":
            result = report_session(args.recorder_root, args.output_dir, args.session_date,
                                    start_tolerance=args.start_tolerance,
                                    end_tolerance=args.end_tolerance,
                                    max_sample_gap=args.max_sample_gap)
        else:
            result = monitor(args.recorder_root, args.shadow_health, args.output_dir,
                             session_date=args.session_date, interval=args.interval,
                             max_age=args.max_age, start_tolerance=args.start_tolerance,
                             end_tolerance=args.end_tolerance,
                             max_sample_gap=args.max_sample_gap, once=args.once)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result.get("accepted") is True else 2
    except SessionAcceptanceError as exc:
        print(json.dumps({"schema": REPORT_SCHEMA, "accepted": False,
                          "status": "rejected", "operational_only": True,
                          "authorizing": False, "reasons": [str(exc)]}), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
