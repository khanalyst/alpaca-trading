"""Broker-free, incremental real-time shadow evaluation.

The shadow lane deliberately has no authority over the trading runtime.  It
reads the recorder corpus and the EdgeLedger through read-only connections,
evaluates each immutable candidate in an isolated virtual book, and writes
only to its own WAL SQLite database.  A virtual open is an observation of what
the candidate would have requested; it is never treated as a fill and no P&L
is fabricated when a safe exit cannot be reconstructed.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
import hashlib
import io
import json
from dataclasses import replace
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.config import load_config as load_runtime_config
from agent.contracts.ibr import generate_ibr_signal
from agent.contracts.rule import (
    CROSS_SECTIONAL_BENCHMARK, cross_sectional_symbol_eligibility,
    evaluate_rule_signal_trace, generate_rule_signal, rule_behavior_identity,
    rule_variant_id, rule_vehicle_executable, validate_rule_spec,
)
from agent.risk import RiskEngine
from agent.strategy import build_setup_plan
from deploy.recorder import (
    INDEX_NAME as RECORDER_INDEX_NAME,
    PARTITION_SOURCE_SCHEMA as RECORDER_PARTITION_SOURCE_SCHEMA,
    corpus_partitions,
)
from research.costs import ReplayPolicy, replay_policy_for_session
from research.diagnostic_shadow import (
    DIAGNOSTIC_ACTIVATION_SCHEMA, DIAGNOSTIC_CANDIDATE_PREFIX,
    DIAGNOSTIC_COHORT_SCHEMA, build_diagnostic_cohort,
    is_diagnostic_candidate,
)
from research.diagnostic_accounts import (
    ACCOUNT_SCHEMA as DIAGNOSTIC_ACCOUNT_SCHEMA,
    FILL_SCHEMA as DIAGNOSTIC_FILL_SCHEMA,
    ORDER_SCHEMA as DIAGNOSTIC_ORDER_SCHEMA,
    POSITION_SCHEMA as DIAGNOSTIC_POSITION_SCHEMA,
    DiagnosticAccountBook, DiagnosticAccountError,
    content_digest as diagnostic_content_digest,
    new_account_state, validate_account_state, validate_position_state,
)
from research.quote_costs import cost_resolver_setup, reprice_ibr_result
from research.stressed_cost_calibration import (
    load_stress_calibration_artifact, verify_stress_calibration_artifact,
)
from research.edge_discovery_core import _effective_ibr_config, _opportunity_rows
from research.edge_discovery_core import _null_reference_rows, null_control_account
from research.edge_lab import _null_spec
from research.factory_core import simulate_account
from research.ibr import IBRConfig, replay_ibr
from research.market_data import (
    NormalizationError,
    normalize_option_snapshot,
    normalize_quote,
    normalize_underlying_bar,
)


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
SCHEMA = "live-shadow.v1"
DEFAULT_EQUITY = 100_000.0
DEFAULT_MAX_CANDIDATES = 32
DEFAULT_MAX_EVENTS = 20_000
DEFAULT_MAX_DECISIONS = 100_000
DEFAULT_DIAGNOSTIC_SESSION_MAX_EVENTS = 200_000
MAX_DIAGNOSTIC_SESSION_MAX_EVENTS = 1_000_000
# Candidate evaluation is CPU-heavy but deliberately bounded.  SQLite/WAL
# mutation remains parent-owned; workers only inspect the frozen snapshot.
DEFAULT_MAX_WORKERS = 4
MAX_MAX_WORKERS = 32
# Shadow replay metadata must survive the longest supported confirmatory tail
# (and enough time for an operator to diagnose/replay a delayed session).
# Keep this as the single source of truth for the library and operations CLI.
DEFAULT_RETENTION_DAYS = 180
MAX_PENDING_CORPUS_BYTES = 64 * 1024 * 1024
MAX_QUARANTINE_EVENTS = 1024
QUARANTINE_OVERFLOW_KEY = "__quarantine_overflow__"
# Provider mismatches are a fail-closed replay condition.  Keep the persisted
# sample bounded even when a corrupt/foreign corpus contains a large tail.
MAX_PROVIDER_MISMATCHES = 64
# Replay metadata is immutable evidence; these bounded meta projections make
# an incomplete/mismatched middle session visible to operators and require an
# explicit repaired replay before ingestion may advance its boundary.
REPLAY_QUARANTINE_META_KEY = "replay_quarantine"
SESSION_CATALOG_META_KEY = "session_catalog"
MAX_REPLAY_REPAIR_HISTORY = 32
MAX_ACTIVE_REPLAY_QUARANTINE = 1024
REPLAY_QUARANTINE_OVERFLOW_KEY = "__replay_quarantine_overflow__"
SHADOW_MANIFEST_META_PREFIX = "shadow-manifest.v1:"
SHADOW_MANIFEST_LATEST_KEY = "shadow-manifest.v1:latest"
DIAGNOSTIC_COHORT_META_PREFIX = "diagnostic-shadow-cohort.v1:"
DIAGNOSTIC_ACTIVATION_META_PREFIX = "diagnostic-shadow-activation.v1:"
DIAGNOSTIC_PROGRESS_SCHEMA = "diagnostic-shadow-progress.v1"
DIAGNOSTIC_REPLAY_EPOCH_SCHEMA = "shadow-replay-epoch.v1"
CANDIDATE_REPLAY_EPOCH_SCHEMA = "shadow-candidate-replay-epoch.v1"
MAX_DIAGNOSTIC_ROLLUP_SESSIONS = 64
DIAGNOSTIC_REASON_ROLLUP_PREFIX = "reason:"
_REPLAY_CODE_BUNDLE_ROOTS = ("agent", "research")
_REPLAY_CODE_BUNDLE_EXTRAS = ("deploy/recorder.py", "requirements.lock.txt")


def _poll_interval(value: object) -> float:
    """Return the finite CLI/library cadence with its historical one-second floor."""
    try:
        interval = float(value)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if not math.isfinite(interval):
        return 1.0
    return max(1.0, interval)


def _next_shadow_cadence_deadline(previous: float | None, now: float,
                                  interval: float) -> float:
    """Advance to the next future start-anchored poll slot.

    A slow poll skips every elapsed slot instead of issuing an immediate burst.
    """
    interval = _poll_interval(interval)
    now = float(now)
    if not math.isfinite(now):
        raise ValueError("shadow monotonic clock must be finite")
    if previous is None:
        return now + interval
    deadline = float(previous)
    if not math.isfinite(deadline):
        return now + interval
    if deadline > now:
        return deadline
    missed = int((now - deadline) // interval) + 1
    return deadline + missed * interval


class ShadowError(RuntimeError):
    """Base error for a shadow run."""


class InputConflict(ShadowError):
    """The same recorder event key was observed with different content."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _replay_code_hash() -> str:
    """Return a deterministic hash of the bounded replay source bundle."""
    root = Path(__file__).resolve().parents[1]
    files = {
        path.relative_to(root).as_posix()
        for name in _REPLAY_CODE_BUNDLE_ROOTS
        for path in (root / name).rglob("*.py")
        if path.is_file()
    }
    files.update(name for name in _REPLAY_CODE_BUNDLE_EXTRAS
                if (root / name).is_file())
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0")
        path = root / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(f"missing:{name}".encode("utf-8"))
    return digest.hexdigest()


def _replay_epoch_identity(*, replay_code_hash: str,
                           manifest_digest: str,
                           candidate_set_digest: str) -> str:
    """Bind replay metadata to one strict current-code poll manifest."""
    return _digest({
        "schema": DIAGNOSTIC_REPLAY_EPOCH_SCHEMA,
        "replay_code_hash": str(replay_code_hash),
        "manifest_digest": str(manifest_digest),
        "candidate_set_digest": str(candidate_set_digest),
    })


def _manifest_replay_identity(manifest: Mapping[str, Any],
                              manifest_digest: str | None = None
                              ) -> dict[str, str]:
    digest = str(manifest_digest or manifest.get("manifest_digest") or "")
    code_hash = str(manifest.get("replay_code_hash") or "")
    candidate_set_digest = str(manifest.get("candidate_set_digest") or "")
    if not digest or not code_hash or not candidate_set_digest:
        raise ShadowError("replay manifest identity is incomplete")
    return {
        "replay_code_hash": code_hash,
        "manifest_digest": digest,
        "candidate_set_digest": candidate_set_digest,
        "replay_epoch_identity": _replay_epoch_identity(
            replay_code_hash=code_hash, manifest_digest=digest,
            candidate_set_digest=candidate_set_digest),
    }


def _candidate_epoch_identity(*, candidate_id: str,
                              config: Mapping[str, Any],
                              replay_identity: Mapping[str, Any]) -> str:
    """Bind one replay row to its exact candidate and strict code epoch."""
    required = {
        key: replay_identity.get(key) for key in (
            "replay_code_hash", "manifest_digest", "candidate_set_digest",
            "replay_epoch_identity")
    }
    if any(not isinstance(value, str) or not value
           for value in required.values()):
        raise ShadowError("candidate replay epoch identity is incomplete")
    return _digest({
        "schema": CANDIDATE_REPLAY_EPOCH_SCHEMA,
        "candidate_id": str(candidate_id),
        "config_digest": _digest(dict(config)),
        **required,
    })


def _validated_shadow_manifest(
        value: Any, *, requested_digest: str | None = None,
        require_current_code: bool = True) -> dict[str, Any]:
    """Validate one persisted manifest before exposing it to callers.

    Manifest rows are immutable evidence, so a syntactically valid JSON value
    is not sufficient: the body must reproduce its stored content digest.  A
    digest-addressed lookup also verifies that the requested key names that
    same digest.  The latest pointer has no digest-bearing key of its own, but
    still goes through the body/self-digest check.
    """
    if not isinstance(value, Mapping) or value.get("schema") != "shadow-manifest.v1":
        raise ShadowError("shadow manifest metadata is invalid")
    replay_code_hash = value.get("replay_code_hash")
    if (not isinstance(replay_code_hash, str) or len(replay_code_hash) != 64 or
            any(character not in "0123456789abcdef"
                for character in replay_code_hash.lower())):
        raise ShadowError("shadow manifest replay code identity is invalid")
    if require_current_code and replay_code_hash != _replay_code_hash():
        raise ShadowError("shadow manifest replay code identity is invalid")
    stored_digest = value.get("manifest_digest")
    body = dict(value)
    body.pop("manifest_digest", None)
    computed_digest = _digest(body)
    if not isinstance(stored_digest, str) or stored_digest != computed_digest:
        raise ShadowError("shadow manifest metadata digest mismatch")
    if requested_digest is not None and stored_digest != requested_digest:
        raise ShadowError("shadow manifest metadata key mismatch")
    return dict(value)


def _validated_diagnostic_cohort(value: Any, *,
                                 expected_identity: str) -> dict[str, Any]:
    """Validate one immutable diagnostic cohort definition."""
    if not isinstance(value, Mapping) or value.get("schema") != DIAGNOSTIC_COHORT_SCHEMA:
        raise ShadowError("diagnostic shadow cohort metadata is invalid")
    if value.get("cohort_identity") != expected_identity:
        raise ShadowError("diagnostic shadow cohort identity mismatch")
    digest = str(value.get("cohort_digest") or "")
    if expected_identity != f"{DIAGNOSTIC_CANDIDATE_PREFIX}cohort:{digest}":
        raise ShadowError("diagnostic shadow cohort digest mismatch")
    arms = value.get("arms")
    if not isinstance(arms, list) or len(arms) != 24:
        raise ShadowError("diagnostic shadow cohort arm structure is invalid")
    family_roles: dict[str, set[str]] = {}
    for arm in arms:
        if not isinstance(arm, Mapping):
            raise ShadowError("diagnostic shadow cohort arm structure is invalid")
        family_roles.setdefault(str(arm.get("family") or ""), set()).add(
            str(arm.get("role") or ""))
    if (len(family_roles) != 12 or
            any(roles != {"baseline", "variant"}
                for roles in family_roles.values())):
        raise ShadowError("diagnostic shadow cohort family coverage is invalid")
    identities = value.get("candidate_identities")
    expected_candidates = [str(arm.get("candidate_id") or "")
                           for arm in arms if isinstance(arm, Mapping)]
    if (not isinstance(identities, list) or identities != expected_candidates or
            len(set(expected_candidates)) != 24 or
            any(not candidate.startswith(DIAGNOSTIC_CANDIDATE_PREFIX)
                for candidate in expected_candidates)):
        raise ShadowError("diagnostic shadow cohort candidate identities are invalid")
    stored = value.get("record_digest")
    body = dict(value)
    body.pop("record_digest", None)
    if not isinstance(stored, str) or stored != _digest(body):
        raise ShadowError("diagnostic shadow cohort metadata digest mismatch")
    return dict(value)


def _validated_diagnostic_activation(value: Any, *,
                                     cohort_identity: str) -> dict[str, Any]:
    """Validate one immutable forward activation record."""
    if (not isinstance(value, Mapping) or
            value.get("schema") != DIAGNOSTIC_ACTIVATION_SCHEMA or
            value.get("cohort_identity") != cohort_identity):
        raise ShadowError("diagnostic shadow activation metadata is invalid")
    watermark = value.get("activation_event_watermark")
    if not isinstance(watermark, Mapping):
        raise ShadowError("diagnostic shadow activation watermark is invalid")
    inserted_after = _finite(watermark.get("last_inserted_at"))
    if inserted_after is None or inserted_after < 0:
        raise ShadowError("diagnostic shadow activation watermark is invalid")
    activated_at = _timestamp(value.get("activated_at"))
    if activated_at is None:
        raise ShadowError("diagnostic shadow activation timestamp is invalid")
    activation_session = activated_at.astimezone(NEW_YORK).date().isoformat()
    if (value.get("activation_session") != activation_session or
            value.get("warmup_session") != activation_session):
        raise ShadowError("diagnostic shadow activation session is invalid")
    source_offsets = value.get("source_offsets")
    if (not isinstance(source_offsets, Mapping) or any(
            not isinstance(key, str) or isinstance(offset, bool) or
            not isinstance(offset, int) or offset < 0
            for key, offset in source_offsets.items())):
        raise ShadowError("diagnostic shadow activation source offsets are invalid")
    stored = value.get("activation_digest")
    body = dict(value)
    body.pop("activation_digest", None)
    body.pop("activation_identity", None)
    computed = _digest(body)
    expected = f"{DIAGNOSTIC_CANDIDATE_PREFIX}activation:{computed}"
    if (not isinstance(stored, str) or stored != computed or
            value.get("activation_identity") != expected):
        raise ShadowError("diagnostic shadow activation digest mismatch")
    return dict(value)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
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
    return parsed.astimezone(UTC)


def _availability_time(row: Mapping[str, Any]) -> datetime | None:
    """Return when a recorded event was actually usable by the strategy.

    Provider event time, provider as-of time, and local observation time are
    separate point-in-time constraints.  The event is not available until all
    three have occurred.
    """
    timestamp = _timestamp(row.get("timestamp"))
    if timestamp is None:
        return None
    as_of = _timestamp(row.get("as_of") or row.get("timestamp"))
    observed = _timestamp(row.get("observed_at") or row.get("as_of") or
                          row.get("timestamp"))
    if as_of is None or observed is None:
        return None
    return max(timestamp, as_of, observed)


def _row_visible(row: Mapping[str, Any], at: datetime) -> bool:
    available = _availability_time(row)
    return available is not None and available <= at


def _recorded_session_bounds(corpus_path: Path, session: str) -> tuple[datetime, datetime] | None:
    """Read the recorder's Alpaca-calendar close for one session.

    The sidecar is recorder-owned and rewritten atomically.  Missing legacy
    calendar metadata deliberately falls back to the regular close in replay;
    newly recorded early closes use the exact broker calendar boundary.
    """
    index_path = corpus_path.parent / RECORDER_INDEX_NAME
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    calendar = payload.get("session_calendar") if isinstance(payload, Mapping) else None
    value = calendar.get(session) if isinstance(calendar, Mapping) else None
    if not isinstance(value, Mapping):
        return None
    opened = _timestamp(value.get("open"))
    closed = _timestamp(value.get("close"))
    if (opened is None or closed is None or opened >= closed or
            opened.astimezone(NEW_YORK).date().isoformat() != session or
            closed.astimezone(NEW_YORK).date().isoformat() != session):
        return None
    return opened, closed


def _recorded_session_calendar(corpus_path: Path) -> dict[str, tuple[datetime, datetime]]:
    """Return validated, already-closed sessions from the recorder sidecar."""
    index_path = corpus_path.parent / RECORDER_INDEX_NAME
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    calendar = payload.get("session_calendar") if isinstance(payload, Mapping) else None
    if not isinstance(calendar, Mapping):
        return {}
    now = datetime.now(UTC)
    result: dict[str, tuple[datetime, datetime]] = {}
    for session in calendar:
        day = str(session)
        bounds = _recorded_session_bounds(corpus_path, day)
        if bounds is not None and bounds[1] <= now:
            result[day] = bounds
    return result


def _recorded_session_close(corpus_path: Path, session: str) -> datetime | None:
    bounds = _recorded_session_bounds(corpus_path, session)
    return bounds[1] if bounds is not None else None


def _session_close(corpus_path: Path, session: str, *,
                   require_exact_calendar: bool = False) -> tuple[datetime | None, str]:
    """Resolve the exact close, retaining an explicit legacy fallback label."""
    recorded = _recorded_session_close(corpus_path, session)
    if recorded is not None:
        return recorded, "recorder_alpaca_calendar"
    if require_exact_calendar:
        return None, "exact_calendar_metadata_missing"
    try:
        local_day = date.fromisoformat(session)
    except ValueError:
        return None, "invalid_session"
    return (datetime.combine(local_day, dt_time(16, 0), NEW_YORK).astimezone(UTC),
            "regular_close_fallback")


def _event_end(row: Mapping[str, Any]) -> datetime | None:
    as_of = _timestamp(row.get("as_of"))
    if as_of is not None:
        return as_of
    stamp = _timestamp(row.get("timestamp"))
    return stamp + timedelta(minutes=1) if stamp is not None else None


def _session_policy(config: Mapping[str, Any], close_at: datetime | None,
                    *, policy: ReplayPolicy | None = None) -> ReplayPolicy:
    """Apply runtime close-relative entry and force-flat cutoffs to replay."""
    policy = _policy(config) if policy is None else policy
    if close_at is None:
        return policy
    local_close = close_at.astimezone(NEW_YORK)
    # The close-relative helper is shared with factory and IBR replay.  The
    # synthetic open is only used to validate the NY session date here.
    local_open = datetime.combine(local_close.date(), dt_time(9, 30),
                                  tzinfo=NEW_YORK).astimezone(UTC)
    return replay_policy_for_session(
        policy, session_open=local_open, session_close=close_at,
        session_date=local_close.date())


def _option_snapshot_index(values: Sequence[Any]) -> dict[datetime, Any]:
    """Preserve every option snapshot even when contracts share a timestamp."""
    origin = datetime(1970, 1, 1, tzinfo=UTC)
    return {origin + timedelta(microseconds=index): value
            for index, value in enumerate(values)}


def _plain(value: Any) -> Any:
    """Convert normalized dataclasses into finite JSON-safe mappings."""
    if dataclasses.is_dataclass(value):
        return _plain(dataclasses.asdict(value))
    if not isinstance(value, type) and hasattr(value, "__dict__"):
        return _plain(vars(value))
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _iso_time(value: Any) -> str | None:
    parsed = _timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def _number_or_none(value: Any) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, 10)


_CROSS_SECTIONAL_CONTEXT_FIELDS = (
    "benchmark_symbol", "market_context_digest",
    "candidate_behavior_identity", "residual_return", "eligibility",
)


def _canonical_context_value(value: Any) -> Any:
    """Return a deterministic JSON-safe projection of context metadata."""
    try:
        return json.loads(_json(value))
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return str(value)


def _cross_sectional_context(sources: Sequence[Mapping[str, Any]], *,
                            force: bool = False,
                            defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Project relative-rule context, retaining missing values as ``None``.

    The context is omitted for ordinary arms so their legacy signatures remain
    byte-for-byte compatible.  Once a cross-sectional marker or one context
    field is present, all fields are bound; a missing replay field therefore
    fails closed instead of silently matching.
    """
    flattened: list[Mapping[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        flattened.append(source)
        for nested_key in ("context", "market_context", "metadata",
                           "cross_sectional_context"):
            nested = source.get(nested_key)
            if isinstance(nested, Mapping):
                flattened.append(nested)
    present = force or any(
        any(field in source for field in _CROSS_SECTIONAL_CONTEXT_FIELDS)
        for source in flattened)
    if not present:
        return {}
    fallback = defaults if isinstance(defaults, Mapping) else {}
    result: dict[str, Any] = {}
    for field in _CROSS_SECTIONAL_CONTEXT_FIELDS:
        value: Any = None
        found = False
        for source in flattened:
            if field in source:
                value = source[field]
                found = True
                break
        if not found and field in fallback:
            value = fallback[field]
        if field == "benchmark_symbol" and value is not None:
            value = str(value).strip().upper()
        elif field in {"market_context_digest", "candidate_behavior_identity"}:
            value = None if value is None else str(value)
        elif field == "residual_return":
            value = _number_or_none(value)
        elif field == "eligibility" and value is not None:
            value = _canonical_context_value(value)
        result[field] = value
    return result


def _canonical_equity_feed(value: Any) -> str | None:
    feed = str(value or "").strip().lower().replace("-", "_")
    if feed == "delayed":
        feed = "delayed_sip"
    return feed if feed in {"iex", "sip", "delayed_sip"} else None


def _canonical_equity_provider(value: Any) -> str | None:
    """Normalize a row provider using the :class:`ReplayPolicy` identity."""
    if value in (None, ""):
        return None
    provider = str(value).strip().lower()
    return provider or None


def _provider_mismatch_telemetry(
        rows_by_kind: Sequence[tuple[str, Sequence[Mapping]]], *,
        expected_provider: str) -> tuple[list[dict[str, Any]], int]:
    """Return a bounded mismatch sample and the total mismatch count."""
    mismatches: list[dict[str, Any]] = []
    count = 0
    for kind, rows in rows_by_kind:
        for row in rows:
            observed_provider = _canonical_equity_provider(row.get("provider"))
            if observed_provider == expected_provider:
                continue
            count += 1
            if len(mismatches) >= MAX_PROVIDER_MISMATCHES:
                continue
            mismatches.append({
                "kind": kind,
                "symbol": str(row.get("symbol") or ""),
                "timestamp": str(row.get("timestamp") or ""),
                "observed_provider": observed_provider,
            })
    return mismatches, count


def _opportunity_capacity(rows: Sequence[Mapping[str, Any]], *,
                          vehicle: str | None = None,
                          min_trades: int | None = None,
                          min_sessions: int | None = None) -> dict[str, Any]:
    """Summarize the complete symbol/session opportunity denominator.

    Replay/account rows deliberately materialize ``no_trade`` opportunities.
    This diagnostic must therefore operate on the raw rows, before the gate's
    authorizing projection removes refusals.  One symbol/session is one
    opportunity even if a malformed retry supplied duplicate rows; an
    executed row wins over a duplicate refusal for the observed count.
    """
    selected: dict[str, dict[str, Any]] = {}
    refusal_reasons: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if vehicle is not None and str(row.get("vehicle") or vehicle) != str(vehicle):
            continue
        symbol = str(row.get("symbol") or "").upper()
        session = str(row.get("session_date") or "")
        opportunity = str(row.get("opportunity_id") or "")
        # Capacity is explicitly symbol×session.  Prefer that stable pair so
        # a malformed/reused opportunity id cannot collapse two sessions.
        key = (f"{symbol}:{session}" if symbol and session else opportunity)
        if not key:
            # Keep malformed rows visible in the refusal denominator without
            # allowing an unbounded arbitrary payload to become a key.
            key = f"__row_{len(selected)}"
        executed = row.get("no_trade") is not True
        item = selected.get(key)
        if item is None or (executed and not item["executed"]):
            selected[key] = {"executed": executed, "session": session}
        if not executed:
            reason = str(row.get("reject_reason") or "unspecified")[:120]
            refusal_reasons[reason] = refusal_reasons.get(reason, 0) + 1
    opportunity_count = len(selected)
    observed_trades = sum(1 for item in selected.values() if item["executed"])
    observed_sessions = len({item["session"] for item in selected.values()
                             if item["executed"] and item["session"]})
    opportunity_sessions = len({item["session"] for item in selected.values()
                                if item["session"]})
    floor_trades = (None if min_trades is None else max(0, int(min_trades)))
    floor_sessions = (None if min_sessions is None else max(0, int(min_sessions)))
    required_rate = (None if floor_trades is None or opportunity_count <= 0
                     else floor_trades / opportunity_count)
    observed_rate = (observed_trades / opportunity_count
                     if opportunity_count else 0.0)
    capacity_feasible = bool(
        (floor_trades is None or opportunity_count >= floor_trades) and
        (floor_sessions is None or opportunity_sessions >= floor_sessions))
    observed_feasible = bool(
        (floor_trades is None or observed_trades >= floor_trades) and
        (floor_sessions is None or observed_sessions >= floor_sessions))
    if not capacity_feasible:
        status = "structurally_impossible"
        reason = "opportunity capacity cannot satisfy configured floor"
    elif not observed_feasible:
        status = "underpowered_observed"
        reason = "observed executed trades are below configured floor"
    else:
        status = "feasible"
        reason = "opportunity capacity satisfies configured floor"
    return {
        "observed_trades": observed_trades,
        "observed_sessions": observed_sessions,
        "opportunity_count": opportunity_count,
        "max_trade_opportunities": opportunity_count,
        "opportunity_sessions": opportunity_sessions,
        "observed_trade_rate": observed_rate,
        "required_trade_rate": required_rate,
        "required_rate_for_floor": required_rate,
        # ``feasible`` is the end-to-end observed floor result.  Keep the
        # structural capacity result separate so selective low-rate lanes are
        # distinguishable from a corpus that cannot possibly supply enough
        # symbol/session opportunities.
        "feasible": bool(capacity_feasible and observed_feasible),
        "capacity_feasible": capacity_feasible,
        "observed_feasible": observed_feasible,
        "status": status,
        "reason": reason,
        "shortfalls": {
            "trades": max(0, (floor_trades or 0) - observed_trades),
            "opportunities": max(0, (floor_trades or 0) - opportunity_count),
            "sessions": max(0, (floor_sessions or 0) - opportunity_sessions),
        },
        "refusal_reason_counts": dict(sorted(refusal_reasons.items())[:64]),
    }


def _signal_dispositions(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count signal opportunities by actual shadow admission disposition.

    ``decisions`` includes data checks and no-signal observations, so using
    its row count as a signal denominator materially understates admission and
    refusal rates.  A generated signal is the opportunity boundary; an
    ``open_incomplete`` decision is admitted and every other disposition is a
    refusal (including stale/unpriced, risk, and duplicate-book vetoes).
    """
    opportunities = admitted = refused = 0
    refusal_reasons: dict[str, int] = {}
    by_candidate: dict[str, dict[str, int]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        payload: Any = row.get("payload")
        if payload is None:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
        if not isinstance(payload, Mapping):
            continue
        signal = payload.get("signal")
        if not isinstance(signal, Mapping) or not signal:
            continue
        opportunities += 1
        kind = str(row.get("kind") or "")
        if kind == "open_incomplete":
            admitted += 1
            disposition = "admitted"
        else:
            refused += 1
            disposition = "refused"
            reason = str(row.get("reason") or "unspecified")[:120]
            refusal_reasons[reason] = refusal_reasons.get(reason, 0) + 1
        candidate = str(row.get("candidate_id") or "")
        if candidate:
            item = by_candidate.setdefault(candidate, {
                "signal_opportunities": 0, "admitted": 0, "refused": 0,
            })
            item["signal_opportunities"] += 1
            item[disposition] += 1
    return {
        "denominator": "generated_signal",
        "signal_opportunities": opportunities,
        "signal_opportunity_count": opportunities,
        "opportunities": opportunities,
        "admitted": admitted,
        "admitted_count": admitted,
        "refused": refused,
        "refused_count": refused,
        "admitted_rate": (admitted / opportunities if opportunities else 0.0),
        "refused_rate": (refused / opportunities if opportunities else 0.0),
        "refusal_reason_counts": dict(sorted(refusal_reasons.items())[:64]),
        "by_candidate": dict(sorted(by_candidate.items())[-64:]),
    }


def _shadow_signature(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a runtime shadow open into the replay comparison contract."""
    if row.get("kind") != "open_incomplete":
        return None
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    signal = payload.get("signal") if isinstance(payload.get("signal"), Mapping) else {}
    plan = payload.get("setup_plan") if isinstance(payload.get("setup_plan"), Mapping) else {}
    risk_plan = payload.get("risk_plan") if isinstance(payload.get("risk_plan"), Mapping) else {}
    execution_profile = str(plan.get("execution_profile") or
                            risk_plan.get("execution_profile") or "shares").lower()
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), Mapping) else {}
    equity_feed = _canonical_equity_feed(
        payload.get("equity_feed") or plan.get("equity_feed") or
        snapshot.get("equity_feed"))
    # Prefer the top-level immutable binding, then the nested legacy shapes.
    # Presence (rather than truthiness) matters: an explicit blank/invalid
    # provider must remain a mismatch instead of being masked by a fallback.
    provider_value: Any = None
    for source in (payload, plan, risk_plan, snapshot):
        if "equity_provider" in source:
            provider_value = source["equity_provider"]
            break
    equity_provider = _canonical_equity_provider(provider_value)
    signal_ts = _finite(plan.get("signal_ts", signal.get("signal_ts")))
    decision_ts = plan.get("decision_timestamp", signal.get("decision_timestamp"))
    entry_ts = plan.get("entry_timestamp", signal.get("entry_timestamp"))
    if entry_ts is None and decision_ts is not None:
        entry_ts = decision_ts
    if entry_ts is None and signal_ts is not None:
        # Legacy shadow payloads predate explicit causal timestamps.
        entry_ts = (datetime.fromtimestamp(signal_ts, UTC) +
                    timedelta(seconds=60)).isoformat()
    signature = {
        "symbol": str(row.get("symbol") or plan.get("symbol") or signal.get("symbol") or ""),
        "session_date": str(row.get("session_date") or plan.get("session") or signal.get("session") or ""),
        "direction": str(plan.get("direction") or signal.get("direction") or ""),
        "setup_type": str(plan.get("setup_type") or signal.get("setup_type") or ""),
        "signal_ts": _iso_time(datetime.fromtimestamp(signal_ts, UTC).isoformat()) if signal_ts is not None else None,
        "decision_ts": _iso_time(decision_ts),
        "entry_ts": _iso_time(entry_ts),
        "stop_price": _number_or_none(plan.get("stop_price", signal.get("stop_price"))),
        "target_price": _number_or_none(plan.get("target_price", signal.get("target_price"))),
        "stop_distance": _number_or_none(plan.get("stop_distance", signal.get("stop_distance"))),
        "range_high": _number_or_none(plan.get("range_high", signal.get("range_high"))),
        "range_low": _number_or_none(plan.get("range_low", signal.get("range_low"))),
        "target_r": _number_or_none(plan.get("target_r", signal.get("target_r"))),
        "vehicle": ("option" if execution_profile
                     in {"option", "options"} else "equity"),
        "profile": execution_profile,
        "equity_feed": equity_feed,
        "equity_provider": equity_provider,
    }
    family = str(plan.get("family") or signal.get("family") or
                 payload.get("family") or "").lower()
    setup_type = str(signature.get("setup_type") or "").lower()
    context = _cross_sectional_context(
        (plan, risk_plan, signal, snapshot, payload),
        force=(family == "cross_sectional_residual" or
               setup_type.endswith("cross_sectional_residual")))
    signature.update(context)
    return signature


def _replay_signature(row: Mapping[str, Any], *, vehicle: str,
                      strategy_id: str, target_r: float | None = None,
                      setup_type: str | None = None,
                      equity_feed: str | None = None,
                      equity_provider: str | None = None,
                      context_defaults: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Project a factory/IBR replay trade into the same semantic contract."""
    if row.get("no_trade") is True:
        return None
    signal_ts = row.get("signal_timestamp")
    # Legacy rows have no causal decision field; preserve that absence so
    # their stored signatures remain comparable.  New factory/IBR rows carry
    # the explicit decision timestamp.
    decision_ts = row.get("decision_timestamp")
    entry_ts = row.get("entry_timestamp")
    direction = str(row.get("direction") or "")
    stop = _number_or_none(row.get("stop_price"))
    target = _number_or_none(row.get("target_price"))
    distance = _number_or_none(row.get("stop_distance"))
    if distance is None and stop is not None and row.get("entry_reference") is not None:
        reference = _finite(row.get("entry_reference"))
        distance = _number_or_none(abs(reference - stop)) if reference is not None else None
    resolved_target_r = target_r
    if resolved_target_r is None and stop is not None and target is not None and distance:
        resolved_target_r = abs(target - stop) / distance - 1.0
    setup_type = ("ibr_breakout" if strategy_id == "ibr" else
                  str(setup_type or row.get("setup_type") or "rule_signal"))
    signature = {
        "symbol": str(row.get("symbol") or ""),
        "session_date": str(row.get("session_date") or ""),
        "direction": direction,
        "setup_type": setup_type,
        "signal_ts": _iso_time(signal_ts),
        "decision_ts": _iso_time(decision_ts),
        "entry_ts": _iso_time(entry_ts),
        "stop_price": stop,
        "target_price": target,
        "stop_distance": distance,
        "range_high": _number_or_none(row.get("range_high")),
        "range_low": _number_or_none(row.get("range_low")),
        "target_r": _number_or_none(resolved_target_r),
        "vehicle": "option" if vehicle in {"option", "options"} else "equity",
        "profile": "options" if vehicle in {"option", "options"} else "shares",
        "equity_feed": _canonical_equity_feed(equity_feed),
        # Replay trades do not necessarily carry their source event identity;
        # bind the canonical provider supplied by the immutable replay policy.
        # Falling back to a row identity keeps direct legacy callers readable.
        "equity_provider": _canonical_equity_provider(
            equity_provider if equity_provider is not None else
            row.get("equity_provider", row.get("provider"))),
    }
    context = _cross_sectional_context(
        (row,),
        force=str(setup_type or row.get("setup_type") or "").lower().endswith(
            "cross_sectional_residual"),
        defaults=context_defaults)
    signature.update(context)
    return signature


def _signature_diffs(expected: Sequence[Mapping[str, Any]],
                    observed: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic, field-level differences between semantic rows."""
    key_fields = ("symbol", "session_date", "direction", "setup_type")
    compare_fields = ("signal_ts", "decision_ts", "entry_ts", "stop_price", "target_price",
                      "stop_distance", "range_high", "range_low", "target_r",
                      "vehicle", "profile", "equity_feed", "equity_provider",
                      *_CROSS_SECTIONAL_CONTEXT_FIELDS)
    left = sorted((dict(item) for item in expected),
                  key=lambda item: tuple(str(item.get(key) or "") for key in key_fields))
    right = sorted((dict(item) for item in observed),
                   key=lambda item: tuple(str(item.get(key) or "") for key in key_fields))
    differences: list[dict[str, Any]] = []
    for index in range(max(len(left), len(right))):
        before = left[index] if index < len(left) else None
        after = right[index] if index < len(right) else None
        if before is None or after is None:
            differences.append({"index": index, "kind": "missing" if after is None else "extra",
                                "expected": before, "observed": after})
            continue
        for field in key_fields + compare_fields:
            if before.get(field) != after.get(field):
                differences.append({"index": index, "field": field,
                                    "expected": before.get(field),
                                    "observed": after.get(field)})
    return differences


def _row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    # Empty CSV cells are absent from normalized payloads.  Keeping the raw
    # key set out of the contract also makes equivalent CSV exports hash alike.
    return {str(key): value for key, value in row.items()
            if value is not None and str(value) != ""}


def _normalize_row(row: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    raw = _row_payload(row)
    event_type = str(raw.get("event_type") or "").strip().lower()
    provider = str(raw.get("provider") or "recorder")
    feed = str(raw.get("feed") or "recorded")
    if event_type in {"bar", "bar_1m"}:
        event = normalize_underlying_bar(raw, provider=provider, feed=feed)
    elif event_type == "quote":
        event = normalize_quote(raw, provider=provider, feed=feed)
    elif event_type in {"option", "option_snapshot"}:
        event = normalize_option_snapshot(raw, provider=provider, feed=feed)
    else:
        raise NormalizationError(f"unsupported recorder event_type {event_type!r}")
    return raw, event


def _corpus_sources(path: Path) -> list[Path]:
    sources = [path] if path.is_file() and path.stat().st_size else []
    sources.extend(corpus_partitions(path))
    return sources


def _read_corpus_append(source: Path, offset: int) -> tuple[list[dict], int]:
    """Read only complete CSV rows appended after a durable byte offset."""
    size = source.stat().st_size
    if size < offset:
        raise ShadowError(f"shadow corpus source shrank: {source}")
    if size == offset:
        return [], offset
    with source.open("rb") as handle:
        header = handle.readline().decode("utf-8")
        try:
            fieldnames = next(csv.reader([header]))
        except (csv.Error, StopIteration) as exc:
            raise ShadowError(f"shadow corpus source has invalid header: {source}") from exc
        handle.seek(offset)
        payload = handle.read(size - offset)
    if payload and not payload.endswith(b"\n"):
        boundary = payload.rfind(b"\n")
        if boundary < 0:
            return [], offset
        payload = payload[:boundary + 1]
    consumed = offset + len(payload)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShadowError(f"shadow corpus source is not UTF-8: {source}") from exc
    if offset == 0:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fields = set(reader.fieldnames or ())
        required = {"event_key", "event_type", "symbol", "timestamp"}
        if not required.issubset(fields):
            raise ShadowError(f"shadow corpus source has invalid header: {source}")
    else:
        reader = csv.DictReader(io.StringIO(text, newline=""), fieldnames=fieldnames)
    rows = []
    for row in reader:
        if None in row:
            raise ShadowError(f"shadow corpus source has malformed CSV: {source}")
        rows.append(row)
    return rows, consumed


def _recorded_partition_source_mode(source_path: str) -> str | None:
    """Read one recorder partition's crash-safe historical source marker."""
    source = Path(str(source_path or ""))
    if not source.name.startswith("market-") or source.suffix != ".csv":
        return None
    marker = source.with_name(source.name + ".source.json")
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ShadowError(
            f"invalid recorder partition source marker {marker}") from exc
    if (not isinstance(payload, Mapping) or
            payload.get("schema") != RECORDER_PARTITION_SOURCE_SCHEMA or
            payload.get("partition") != source.name or
            payload.get("source_mode") != "historical_backfill"):
        raise ShadowError(f"invalid recorder partition source marker {marker}")
    return "historical_backfill"


def _diagnostic_source_projection(
        event: Mapping[str, Any],
        source_modes: dict[str, str | None]) -> dict[str, Any]:
    """Overlay durable recorder source provenance without rewriting the WAL."""
    projected = dict(event)
    try:
        payload = (json.loads(event.get("event_json"))
                   if isinstance(event.get("event_json"), str)
                   else event.get("event_json"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return projected
    if not isinstance(payload, Mapping):
        return projected
    source_path = str(event.get("source_path") or "")
    if source_path not in source_modes:
        source_modes[source_path] = _recorded_partition_source_mode(source_path)
    marker_mode = source_modes[source_path]
    row_mode = str(payload.get("source_mode") or "").strip().lower()
    if row_mode and row_mode not in {"forward_observed", "historical_backfill"}:
        raise ShadowError(f"unsupported diagnostic source mode {row_mode!r}")
    if marker_mode is not None and row_mode and row_mode != marker_mode:
        raise ShadowError(
            f"diagnostic event source mode conflicts with {source_path}")
    body = dict(payload)
    body["source_mode"] = marker_mode or row_mode or "forward_observed"
    projected["event_json"] = _json(body)
    return projected


def _compact_shadow_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Keep all bars/options and every quote needed at a decision boundary.

    First/last minute quotes retain the open/close path while the exact latest
    visible quote at each recorded bar availability time preserves delayed
    after-close decisions.  This remains bounded without replacing an entry
    boundary quote with a later quote from the same minute.
    """
    retained: list[dict] = []
    quote_rows: dict[str, list[tuple[datetime, datetime, dict]]] = {}
    cutoffs: dict[str, list[datetime]] = {}
    for raw in rows:
        row = dict(raw)
        event_type = str(row.get("event_type") or "").strip().lower()
        if event_type != "quote":
            retained.append(row)
            if event_type in {"bar", "bar_1m"}:
                available = _availability_time(row)
                if available is not None:
                    cutoffs.setdefault(str(row.get("symbol") or ""), []).append(available)
            continue
        stamp = _timestamp(row.get("timestamp"))
        available = _availability_time(row)
        if stamp is None or available is None:
            retained.append(row)  # normal validation emits the hard failure
            continue
        quote_rows.setdefault(str(row.get("symbol") or ""), []).append(
            (available, stamp, row))

    selected: dict[str, dict] = {}
    for symbol, values in quote_rows.items():
        values.sort(key=lambda item: (item[1], str(item[2].get("event_key") or "")))
        by_minute: dict[str, list[tuple[datetime, datetime, dict]]] = {}
        for item in values:
            by_minute.setdefault(item[1].replace(second=0, microsecond=0).isoformat(), []).append(item)
        for minute_values in by_minute.values():
            for item in (minute_values[0], minute_values[-1]):
                row = item[2]
                selected[str(row.get("event_key") or _digest(row))] = row

        available_values = sorted(
            values, key=lambda item: (item[0], item[1],
                                      str(item[2].get("event_key") or "")))
        cursor = 0
        latest: dict | None = None
        for cutoff in sorted(set(cutoffs.get(symbol, ()))):
            while cursor < len(available_values) and available_values[cursor][0] <= cutoff:
                latest = available_values[cursor][2]
                cursor += 1
            if latest is not None:
                selected[str(latest.get("event_key") or _digest(latest))] = latest

    retained.extend(selected.values())
    retained.sort(key=lambda row: (str(row.get("timestamp") or ""),
                                   str(row.get("event_key") or "")))
    return retained


@dataclass(frozen=True)
class ShadowConfig:
    """Bounded paths and resource limits for one shadow process."""

    corpus_path: Path
    edge_db: Path
    shadow_db: Path
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_events: int = DEFAULT_MAX_EVENTS
    max_decisions: int = DEFAULT_MAX_DECISIONS
    diagnostic_session_max_events: int = DEFAULT_DIAGNOSTIC_SESSION_MAX_EVENTS
    max_workers: int = DEFAULT_MAX_WORKERS
    retention_days: int = DEFAULT_RETENTION_DAYS
    equity: float = DEFAULT_EQUITY
    poll_seconds: float = 60.0
    # The fixed family cohort is opt-in at the library boundary.  Deployment
    # CLIs enable it explicitly and must supply the mounted runtime config so
    # signal, setup, risk, execution, and cost policy are identical to runtime.
    diagnostic: bool = False
    runtime_config: Mapping[str, Any] | None = None
    runtime_config_path: str | Path | None = None
    # Shadow-only empirical stress overlay.  These fields are intentionally
    # separate from candidate/runtime config: enabling them cannot mutate the
    # trader's production policy or its persisted configuration.
    stress_calibration_path: str | Path | None = None
    stress_calibration_enabled: bool | None = None
    stress_calibration_artifact: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("max_candidates", "max_events", "max_decisions",
                     "diagnostic_session_max_events", "max_workers",
                     "retention_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) != value or int(value) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if int(self.max_workers) > MAX_MAX_WORKERS:
            raise ValueError(f"max_workers must be <= {MAX_MAX_WORKERS}")
        if (int(self.diagnostic_session_max_events) >
                MAX_DIAGNOSTIC_SESSION_MAX_EVENTS):
            raise ValueError(
                "diagnostic_session_max_events must be <= "
                f"{MAX_DIAGNOSTIC_SESSION_MAX_EVENTS}")
        if _finite(self.equity) is None or float(self.equity) <= 0:
            raise ValueError("equity must be positive and finite")
        object.__setattr__(self, "poll_seconds",
                           _poll_interval(self.poll_seconds))
        if not isinstance(self.diagnostic, bool):
            raise ValueError("diagnostic must be true or false")
        if self.diagnostic and not isinstance(self.runtime_config, Mapping):
            raise ValueError(
                "diagnostic shadow requires the mounted --config mapping")
        if isinstance(self.runtime_config, Mapping):
            # Detach mutable caller state; the cohort identity and every arm
            # are derived from this frozen projection for the process lifetime.
            object.__setattr__(self, "runtime_config",
                               json.loads(_json(dict(self.runtime_config))))
        if self.runtime_config_path not in (None, ""):
            object.__setattr__(self, "runtime_config_path",
                               str(self.runtime_config_path))
        path = self.stress_calibration_path
        if path in (None, ""):
            path = (os.getenv("ALPACA_SHADOW_STRESS_CALIBRATION_PATH") or
                    os.getenv("ALPACA_SHADOW_CALIBRATION_PATH") or None)
        enabled = self.stress_calibration_enabled
        if enabled is None:
            raw = (os.getenv("ALPACA_SHADOW_STRESS_CALIBRATION_ENABLED") or
                   os.getenv("ALPACA_SHADOW_CALIBRATION_ENABLED"))
            if raw is None:
                # A path is inert unless the dedicated operator switch is
                # explicitly true; accidental artifact mounts must not alter
                # shadow policy.
                enabled = False
            else:
                normalized = str(raw).strip().lower()
                if normalized not in {
                        "0", "1", "false", "true", "no", "yes", "off", "on"}:
                    raise ValueError(
                        "shadow stress calibration enabled flag must be boolean")
                enabled = normalized in {"1", "true", "yes", "on"}
        if not isinstance(enabled, bool):
            raise ValueError("stress_calibration_enabled must be true or false")
        if enabled:
            artifact = self.stress_calibration_artifact
            reason = None
            if artifact is None:
                artifact, reason = load_stress_calibration_artifact(path)
            if artifact is None:
                raise ValueError(
                    f"shadow stress calibration artifact unavailable: {reason or 'artifact_missing'}")
            valid, reason = verify_stress_calibration_artifact(
                artifact, expected_provider="alpaca")
            if not valid:
                raise ValueError(
                    f"shadow stress calibration artifact invalid: {reason or 'artifact_invalid'}")
            object.__setattr__(self, "stress_calibration_artifact", dict(artifact))
        object.__setattr__(self, "stress_calibration_path",
                           None if path in (None, "") else str(path))
        object.__setattr__(self, "stress_calibration_enabled", bool(enabled))


class ShadowStore:
    """Own the isolated append-only shadow database."""

    def __init__(self, path: str | Path, *, retention_days: int = DEFAULT_RETENTION_DAYS,
                 readonly: bool = False):
        self.path = Path(path)
        self.readonly = bool(readonly)
        if not self.readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = int(retention_days)
        if not self.readonly:
            self._init()

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            if not self.path.is_file():
                raise ShadowError(f"shadow database is unavailable: {self.path}")
            uri = f"file:{self.path.resolve()}?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        if not self.readonly:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            if not self.readonly:
                db.commit()
        finally:
            db.close()

    def _init(self) -> None:
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cursor (
                    scope TEXT PRIMARY KEY, last_event_key TEXT NOT NULL,
                    last_timestamp TEXT NOT NULL, last_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    event_json TEXT NOT NULL, event_type TEXT NOT NULL,
                    symbol TEXT NOT NULL, timestamp TEXT NOT NULL,
                    as_of TEXT, inserted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY, variant_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL, vehicle TEXT NOT NULL,
                    status TEXT NOT NULL, config_json TEXT NOT NULL,
                    proof_json TEXT NOT NULL, observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    event_key TEXT NOT NULL, session_date TEXT NOT NULL,
                    symbol TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT,
                    payload_json TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(candidate_id, event_key)
                );
                CREATE TABLE IF NOT EXISTS virtual_books (
                    book_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
                    status TEXT NOT NULL, quantity REAL, entry_price REAL,
                    plan_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_diffs (
                    diff_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    session_date TEXT NOT NULL, source_digest TEXT NOT NULL,
                    shadow_digest TEXT NOT NULL, replay_digest TEXT,
                    status TEXT NOT NULL, details_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(candidate_id, session_date)
                );
                CREATE TABLE IF NOT EXISTS shadow_accounts (
                    account_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    session_date TEXT NOT NULL, replay_digest TEXT NOT NULL,
                    vehicle TEXT NOT NULL, starting_cash REAL NOT NULL,
                    ending_cash REAL NOT NULL, realized_pnl REAL NOT NULL,
                    trade_count INTEGER NOT NULL, replay_status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(candidate_id, session_date, replay_digest)
                );
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    trade_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    session_date TEXT NOT NULL, replay_digest TEXT NOT NULL,
                    replay_status TEXT NOT NULL, trade_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS diagnostic_progress (
                    cohort_identity TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    last_inserted_at REAL NOT NULL,
                    last_event_key TEXT NOT NULL,
                    processed_events INTEGER NOT NULL,
                    rollups_json TEXT NOT NULL,
                    pending_sessions_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(cohort_identity,candidate_id)
                );
                CREATE TABLE IF NOT EXISTS diagnostic_accounts (
                    cohort_identity TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    starting_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    equity REAL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL,
                    open_position_count INTEGER NOT NULL,
                    closed_position_count INTEGER NOT NULL,
                    order_count INTEGER NOT NULL,
                    fill_count INTEGER NOT NULL,
                    late_data_gap_count INTEGER NOT NULL,
                    mark_status TEXT NOT NULL,
                    last_event_key TEXT NOT NULL,
                    last_event_at TEXT,
                    state_json TEXT NOT NULL,
                    state_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(cohort_identity,candidate_id)
                );
                CREATE TABLE IF NOT EXISTS diagnostic_positions (
                    position_id TEXT PRIMARY KEY,
                    cohort_identity TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    mark_price REAL,
                    unrealized_pnl REAL,
                    realized_pnl REAL NOT NULL,
                    late_data_gap INTEGER NOT NULL,
                    entry_event_key TEXT NOT NULL,
                    exit_event_key TEXT,
                    state_json TEXT NOT NULL,
                    state_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS diagnostic_orders (
                    order_id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    cohort_identity TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS diagnostic_fills (
                    fill_id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE,
                    cohort_identity TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    fill_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                  BEGIN SELECT RAISE(ABORT, 'shadow events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                  BEGIN SELECT RAISE(ABORT, 'shadow events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS candidates_no_update BEFORE UPDATE ON candidates
                  BEGIN SELECT RAISE(ABORT, 'shadow candidates are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS candidates_no_delete BEFORE DELETE ON candidates
                  BEGIN SELECT RAISE(ABORT, 'shadow candidates are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
                  BEGIN SELECT RAISE(ABORT, 'shadow decisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
                  BEGIN SELECT RAISE(ABORT, 'shadow decisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_accounts_no_update BEFORE UPDATE ON shadow_accounts
                  BEGIN SELECT RAISE(ABORT, 'shadow accounts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_accounts_no_delete BEFORE DELETE ON shadow_accounts
                  BEGIN SELECT RAISE(ABORT, 'shadow accounts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_trades_no_update BEFORE UPDATE ON shadow_trades
                  BEGIN SELECT RAISE(ABORT, 'shadow trades are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_trades_no_delete BEFORE DELETE ON shadow_trades
                  BEGIN SELECT RAISE(ABORT, 'shadow trades are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS diagnostic_orders_no_update
                  BEFORE UPDATE ON diagnostic_orders
                  BEGIN SELECT RAISE(ABORT, 'diagnostic orders are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS diagnostic_orders_no_delete
                  BEFORE DELETE ON diagnostic_orders
                  BEGIN SELECT RAISE(ABORT, 'diagnostic orders are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS diagnostic_fills_no_update
                  BEFORE UPDATE ON diagnostic_fills
                  BEGIN SELECT RAISE(ABORT, 'diagnostic fills are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS diagnostic_fills_no_delete
                  BEFORE DELETE ON diagnostic_fills
                  BEGIN SELECT RAISE(ABORT, 'diagnostic fills are immutable'); END;
                CREATE INDEX IF NOT EXISTS events_timestamp_idx ON events(timestamp);
                CREATE INDEX IF NOT EXISTS events_inserted_at_idx
                  ON events(inserted_at,event_key);
                CREATE INDEX IF NOT EXISTS diagnostic_positions_open_idx
                  ON diagnostic_positions(cohort_identity,candidate_id,status,symbol);
                CREATE INDEX IF NOT EXISTS diagnostic_orders_candidate_idx
                  ON diagnostic_orders(cohort_identity,candidate_id,created_at);
                CREATE INDEX IF NOT EXISTS diagnostic_fills_candidate_idx
                  ON diagnostic_fills(cohort_identity,candidate_id,created_at);
            """)
            event_columns = {
                str(row["name"]) for row in db.execute(
                    "PRAGMA table_info(events)").fetchall()
            }
            for name, declaration in (
                    ("source_path", "TEXT"),
                    ("source_offset_start", "INTEGER"),
                    ("source_offset_end", "INTEGER")):
                if name not in event_columns:
                    db.execute(f"ALTER TABLE events ADD COLUMN {name} {declaration}")

    def ingest_event(self, row: Mapping[str, Any], *, max_events: int,
                     source_path: str | None = None,
                     source_offset_start: int | None = None,
                     source_offset_end: int | None = None) -> tuple[str, bool]:
        event_key = str(row.get("event_key") or "").strip()
        if not event_key:
            raise ShadowError("recorder row has no event_key")
        # The corpus remains a streamed source, but known rows do not need to
        # be normalized again.  We still hash every row before skipping it so
        # a changed payload for an old key is always a hard conflict.
        payload = _row_payload(row)
        digest = _digest(payload)
        with self._connection() as db:
            existing = db.execute("SELECT digest FROM events WHERE event_key=?",
                                  (event_key,)).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    raise InputConflict(f"event_key {event_key} changed content")
                return event_key, False
        payload, event = _normalize_row(row)
        timestamp = _timestamp(payload.get("timestamp"))
        as_of = _timestamp(payload.get("as_of") or payload.get("timestamp"))
        if timestamp is None:
            raise ShadowError(f"event {event_key} has invalid timestamp")
        symbol = str(payload.get("symbol") or getattr(event, "symbol", ""))
        event_type = str(payload.get("event_type") or "").lower()
        with self._connection() as db:
            db.execute("""INSERT INTO events
                (event_key,digest,event_json,event_type,symbol,timestamp,as_of,inserted_at,
                 source_path,source_offset_start,source_offset_end)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (event_key, digest, _json(payload), event_type, symbol,
                 timestamp.isoformat(), as_of.isoformat() if as_of else None,
                 time.time(), (str(source_path) if source_path else None),
                 (int(source_offset_start)
                  if source_offset_start is not None else None),
                 (int(source_offset_end)
                  if source_offset_end is not None else None)))
            cursor = db.execute("SELECT last_timestamp,last_event_key FROM cursor WHERE scope='corpus'").fetchone()
            if cursor is None or (timestamp.isoformat(), event_key) >= (cursor[0], cursor[1]):
                db.execute("""INSERT INTO cursor(scope,last_event_key,last_timestamp,last_digest,updated_at)
                    VALUES('corpus',?,?,?,?)
                    ON CONFLICT(scope) DO UPDATE SET last_event_key=excluded.last_event_key,
                        last_timestamp=excluded.last_timestamp,last_digest=excluded.last_digest,
                        updated_at=excluded.updated_at""",
                    (event_key, timestamp.isoformat(), digest, time.time()))
        return event_key, True

    def event_count(self) -> int:
        with self._connection() as db:
            return int(db.execute("SELECT count(*) FROM events").fetchone()[0])

    def source_offsets(self) -> dict[str, int] | None:
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='corpus_source_offsets'").fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow corpus offsets are invalid") from exc
        if not isinstance(value, dict) or any(
                not isinstance(key, str) or isinstance(offset, bool) or
                not isinstance(offset, int) or offset < 0
                for key, offset in value.items()):
            raise ShadowError("shadow corpus offsets are invalid")
        return value

    def save_source_offsets(self, offsets: Mapping[str, int]) -> None:
        payload = _json({str(key): int(value) for key, value in offsets.items()})
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES('corpus_source_offsets',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (payload,))

    def save_manifest(self, manifest: Mapping[str, Any]) -> str:
        """Persist one immutable content-addressed poll manifest.

        The manifest is written by the parent after ingestion and before any
        worker starts.  Its digest is derived from the manifest body (not
        wall-clock metadata), so retries over the same candidate/event
        snapshot address the same evidence.  A mutable latest pointer is only
        an operational convenience; the digest-keyed copy is the audit source.
        """
        if self.readonly:
            raise ShadowError("cannot update manifest on a read-only WAL")
        body = dict(manifest)
        body.pop("manifest_digest", None)
        body.setdefault("schema", "shadow-manifest.v1")
        body.setdefault("replay_code_hash", _replay_code_hash())
        digest = _digest(body)
        encoded = _json({**body, "manifest_digest": digest})
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO NOTHING""",
                       (f"{SHADOW_MANIFEST_META_PREFIX}{digest}", encoded))
            db.execute("""INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (SHADOW_MANIFEST_LATEST_KEY, encoded))
        return digest

    def manifest(self, digest: str | None = None) -> dict[str, Any] | None:
        """Read a digest-addressed manifest, or the latest poll manifest."""
        requested_digest = None if digest is None else str(digest)
        key = SHADOW_MANIFEST_LATEST_KEY if requested_digest is None else (
            f"{SHADOW_MANIFEST_META_PREFIX}{requested_digest}")
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow manifest metadata is invalid") from exc
        return _validated_shadow_manifest(value, requested_digest=requested_digest)

    def latest_manifest_for_rollover(self) -> dict[str, Any] | None:
        """Read the latest self-valid manifest without requiring current code.

        This reader is operational only: it lets a new code epoch inspect and
        decline reuse of the prior epoch, then append a current-code manifest.
        Digest-addressed ``manifest()`` remains strict for proof consumers.
        """
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key=?",
                (SHADOW_MANIFEST_LATEST_KEY,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow manifest metadata is invalid") from exc
        return _validated_shadow_manifest(value, require_current_code=False)

    def save_diagnostic_cohort(self, cohort: Mapping[str, Any]) -> dict[str, Any]:
        """Preregister one immutable content-addressed diagnostic cohort."""
        if self.readonly:
            raise ShadowError("cannot register diagnostic cohort on a read-only WAL")
        body = dict(cohort)
        body.pop("record_digest", None)
        identity = str(body.get("cohort_identity") or "")
        encoded_value = {**body, "record_digest": _digest(body)}
        key = f"{DIAGNOSTIC_COHORT_META_PREFIX}{identity}"
        with self._connection() as db:
            existing = db.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO meta(key,value) VALUES(?,?)",
                           (key, _json(encoded_value)))
                value = encoded_value
            else:
                try:
                    value = json.loads(existing["value"])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ShadowError(
                        "diagnostic shadow cohort metadata is invalid") from exc
        validated = _validated_diagnostic_cohort(
            value, expected_identity=identity)
        if _json(validated) != _json(encoded_value):
            raise ShadowError("diagnostic shadow cohort identity conflicts")
        return validated

    def diagnostic_cohort(self, cohort_identity: str) -> dict[str, Any] | None:
        key = f"{DIAGNOSTIC_COHORT_META_PREFIX}{str(cohort_identity)}"
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("diagnostic shadow cohort metadata is invalid") from exc
        return _validated_diagnostic_cohort(
            value, expected_identity=str(cohort_identity))

    def save_diagnostic_activation(
            self, *, cohort: Mapping[str, Any],
            activation_event_watermark: Mapping[str, Any],
            source_offsets: Mapping[str, int], forward_event_floor: float) -> dict[str, Any]:
        """Append the sole forward activation for one frozen cohort epoch."""
        if self.readonly:
            raise ShadowError("cannot activate diagnostic cohort on a read-only WAL")
        cohort_identity = str(cohort.get("cohort_identity") or "")
        activated = datetime.now(UTC)
        activation_session = activated.astimezone(NEW_YORK).date().isoformat()
        body = {
            "schema": DIAGNOSTIC_ACTIVATION_SCHEMA,
            "cohort_identity": cohort_identity,
            "code_identity": str(cohort.get("code_identity") or ""),
            "runtime_config_identity": str(
                cohort.get("runtime_config_identity") or ""),
            "policy_config_identity": str(
                cohort.get("policy_config_identity") or ""),
            "activation_event_watermark": dict(activation_event_watermark),
            "source_offsets": {str(key): int(value)
                               for key, value in source_offsets.items()},
            "forward_event_floor": float(forward_event_floor),
            "activation_session": activation_session,
            "warmup_session": activation_session,
            "activated_at": activated.isoformat(),
        }
        digest = _digest(body)
        value = {
            **body,
            "activation_digest": digest,
            "activation_identity": (
                f"{DIAGNOSTIC_CANDIDATE_PREFIX}activation:{digest}"),
        }
        key = f"{DIAGNOSTIC_ACTIVATION_META_PREFIX}{cohort_identity}"
        with self._connection() as db:
            existing = db.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO meta(key,value) VALUES(?,?)",
                           (key, _json(value)))
                stored = value
            else:
                try:
                    stored = json.loads(existing["value"])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ShadowError(
                        "diagnostic shadow activation metadata is invalid") from exc
        validated = _validated_diagnostic_activation(
            stored, cohort_identity=cohort_identity)
        if existing is not None and _json(validated) != _json(value):
            # A cohort has exactly one activation. Restarting reuses it; a
            # different code/config epoch receives a different cohort key.
            return validated
        return validated

    def diagnostic_activation(self, cohort_identity: str) -> dict[str, Any] | None:
        key = f"{DIAGNOSTIC_ACTIVATION_META_PREFIX}{str(cohort_identity)}"
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("diagnostic shadow activation metadata is invalid") from exc
        return _validated_diagnostic_activation(
            value, cohort_identity=str(cohort_identity))

    def diagnostic_progress(
            self, cohort_identity: str, candidate_ids: Sequence[str],
            activation: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Return bounded per-arm cursors and compact diagnostic rollups."""
        watermark = activation.get("activation_event_watermark")
        if not isinstance(watermark, Mapping):
            raise ShadowError("diagnostic shadow activation watermark is invalid")
        initial_at = _finite(watermark.get("last_inserted_at"))
        if initial_at is None:
            raise ShadowError("diagnostic shadow activation watermark is invalid")
        initial_key = str(watermark.get("last_event_key") or "")
        wanted = {str(value) for value in candidate_ids}
        with self._connection() as db:
            rows = db.execute("""SELECT * FROM diagnostic_progress
                WHERE cohort_identity=? ORDER BY candidate_id""",
                (str(cohort_identity),)).fetchall()
        result: dict[str, dict[str, Any]] = {
            candidate_id: {
                "cohort_identity": str(cohort_identity),
                "candidate_id": candidate_id,
                "last_inserted_at": float(initial_at),
                "last_event_key": initial_key,
                "processed_events": 0,
                "rollups": {"cumulative": {}, "sessions": {},
                            "completed_sessions": []},
                "pending_sessions": [],
            }
            for candidate_id in wanted
        }
        for row in rows:
            candidate_id = str(row["candidate_id"])
            if candidate_id not in wanted:
                continue
            try:
                rollups = json.loads(row["rollups_json"])
                pending = json.loads(row["pending_sessions_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ShadowError("diagnostic shadow progress is invalid") from exc
            if (not isinstance(rollups, Mapping) or
                    not isinstance(pending, list) or
                    any(not isinstance(value, str) for value in pending)):
                raise ShadowError("diagnostic shadow progress is invalid")
            result[candidate_id] = {
                "cohort_identity": str(cohort_identity),
                "candidate_id": candidate_id,
                "last_inserted_at": float(row["last_inserted_at"]),
                "last_event_key": str(row["last_event_key"]),
                "processed_events": int(row["processed_events"]),
                "rollups": dict(rollups),
                "pending_sessions": sorted(set(pending)),
            }
        return result

    @staticmethod
    def _diagnostic_account_values(state: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            float(state["starting_cash"]), float(state["cash"]),
            state.get("equity"), float(state["realized_pnl"]),
            state.get("unrealized_pnl"), int(state["open_position_count"]),
            int(state["closed_position_count"]), int(state["order_count"]),
            int(state["fill_count"]), int(state["late_data_gap_count"]),
            str(state["mark_status"]), str(state.get("last_event_key") or ""),
            state.get("last_event_at"), _json(state),
            str(state["state_digest"]), time.time(),
        )

    def seed_diagnostic_accounts(
            self, *, cohort_identity: str, candidate_ids: Sequence[str],
            starting_cash: float) -> None:
        """Add the current cohort's isolated cash books without replaying history."""
        if self.readonly:
            raise ShadowError("cannot seed diagnostic accounts on a read-only WAL")
        with self._connection() as db:
            for candidate_id in sorted({str(value) for value in candidate_ids}):
                row = db.execute("""SELECT state_json,state_digest
                    FROM diagnostic_accounts
                    WHERE cohort_identity=? AND candidate_id=?""",
                    (str(cohort_identity), candidate_id)).fetchone()
                if row is None:
                    state = new_account_state(
                        cohort_identity=str(cohort_identity),
                        candidate_id=candidate_id,
                        starting_cash=float(starting_cash))
                    db.execute("""INSERT INTO diagnostic_accounts
                        (cohort_identity,candidate_id,starting_cash,cash,equity,
                         realized_pnl,unrealized_pnl,open_position_count,
                         closed_position_count,order_count,fill_count,
                         late_data_gap_count,mark_status,last_event_key,
                         last_event_at,state_json,state_digest,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (str(cohort_identity), candidate_id,
                         *self._diagnostic_account_values(state)))
                    continue
                try:
                    decoded = json.loads(row["state_json"])
                    state = validate_account_state(
                        decoded, cohort_identity=str(cohort_identity),
                        candidate_id=candidate_id)
                except (TypeError, ValueError, json.JSONDecodeError,
                        DiagnosticAccountError) as exc:
                    raise ShadowError("diagnostic account state is invalid") from exc
                if str(row["state_digest"]) != str(state["state_digest"]):
                    raise ShadowError("diagnostic account state digest mismatch")
                if abs(float(state["starting_cash"]) - float(starting_cash)) > 1e-9:
                    raise InputConflict(
                        f"diagnostic account {candidate_id} starting cash changed")

    def diagnostic_account_snapshot(
            self, *, cohort_identity: str, candidate_id: str) -> dict[str, Any]:
        """Return one validated account and only its still-open positions."""
        with self._connection() as db:
            account_row = db.execute("""SELECT state_json,state_digest
                FROM diagnostic_accounts
                WHERE cohort_identity=? AND candidate_id=?""",
                (str(cohort_identity), str(candidate_id))).fetchone()
            position_rows = db.execute("""SELECT state_json,state_digest
                FROM diagnostic_positions
                WHERE cohort_identity=? AND candidate_id=? AND status='open'
                ORDER BY position_id""",
                (str(cohort_identity), str(candidate_id))).fetchall()
        if account_row is None:
            raise ShadowError("diagnostic account is unavailable")
        try:
            account = validate_account_state(
                json.loads(account_row["state_json"]),
                cohort_identity=str(cohort_identity),
                candidate_id=str(candidate_id))
            positions = [validate_position_state(
                json.loads(row["state_json"]),
                cohort_identity=str(cohort_identity),
                candidate_id=str(candidate_id)) for row in position_rows]
        except (TypeError, ValueError, json.JSONDecodeError,
                DiagnosticAccountError) as exc:
            raise ShadowError("diagnostic account state is invalid") from exc
        if str(account_row["state_digest"]) != str(account["state_digest"]):
            raise ShadowError("diagnostic account state digest mismatch")
        for row, position in zip(position_rows, positions):
            if str(row["state_digest"]) != str(position["state_digest"]):
                raise ShadowError("diagnostic position state digest mismatch")
        return {"account": account, "positions": positions}

    def diagnostic_account_summary(
            self, *, cohort_identity: str,
            candidate_ids: Sequence[str]) -> dict[str, Any]:
        """Return bounded non-authorizing telemetry for the persistent books."""
        wanted = sorted({str(value) for value in candidate_ids})
        if not wanted:
            return {
                "schema": "diagnostic-forward-accounts-summary.v1",
                "account_count": 0, "priced_account_count": 0,
                "unpriced_account_count": 0, "open_positions": 0,
                "closed_positions": 0, "orders": 0, "modeled_fills": 0,
                "entry_fills": 0, "exit_fills": 0,
                "late_data_gap_positions": 0, "cash": 0.0,
                "equity": 0.0, "priced_equity": 0.0,
                "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "priced_unrealized_pnl": 0.0, "actual_fills": 0,
                "by_candidate": [],
            }
        placeholders = ",".join("?" for _ in wanted)
        params = (str(cohort_identity), *wanted)
        with self._connection() as db:
            rows = db.execute(f"""SELECT * FROM diagnostic_accounts
                WHERE cohort_identity=? AND candidate_id IN ({placeholders})
                ORDER BY candidate_id""", params).fetchall()
            order_count = int(db.execute(f"""SELECT count(*)
                FROM diagnostic_orders WHERE cohort_identity=?
                AND candidate_id IN ({placeholders})""", params).fetchone()[0])
            fill_rows = db.execute(f"""SELECT action,count(*) AS count
                FROM diagnostic_fills WHERE cohort_identity=?
                AND candidate_id IN ({placeholders}) GROUP BY action""",
                params).fetchall()
        by_candidate: list[dict[str, Any]] = []
        cash = realized = 0.0
        equity = unrealized = 0.0
        all_priced = True
        open_positions = closed_positions = late = fills = 0
        for row in rows:
            try:
                state = validate_account_state(
                    json.loads(row["state_json"]),
                    cohort_identity=str(cohort_identity),
                    candidate_id=str(row["candidate_id"]))
            except (TypeError, ValueError, json.JSONDecodeError,
                    DiagnosticAccountError) as exc:
                raise ShadowError("diagnostic account state is invalid") from exc
            if str(row["state_digest"]) != str(state["state_digest"]):
                raise ShadowError("diagnostic account state digest mismatch")
            priced = state.get("equity") is not None
            all_priced = all_priced and priced
            cash += float(state["cash"])
            realized += float(state["realized_pnl"])
            if priced:
                equity += float(state["equity"])
                unrealized += float(state["unrealized_pnl"] or 0.0)
            open_positions += int(state["open_position_count"])
            closed_positions += int(state["closed_position_count"])
            late += int(state["late_data_gap_count"])
            fills += int(state["fill_count"])
            by_candidate.append({
                "candidate_id": state["candidate_id"],
                "cash": round(float(state["cash"]), 8),
                "equity": (round(float(state["equity"]), 8)
                           if priced else None),
                "realized_pnl": round(float(state["realized_pnl"]), 8),
                "unrealized_pnl": (round(float(state["unrealized_pnl"]), 8)
                                   if priced else None),
                "open_positions": int(state["open_position_count"]),
                "closed_positions": int(state["closed_position_count"]),
                "fills": int(state["fill_count"]),
                "late_data_gaps": int(state["late_data_gap_count"]),
                "mark_status": state["mark_status"],
                "last_event_at": state.get("last_event_at"),
            })
        fill_counts = {str(row["action"]): int(row["count"])
                       for row in fill_rows}
        entry_fills = sum(count for action, count in fill_counts.items()
                          if action.startswith("entry_"))
        exit_fills = sum(count for action, count in fill_counts.items()
                         if action.startswith("exit_"))
        priced_count = sum(1 for item in by_candidate
                           if item["equity"] is not None)
        return {
            "schema": "diagnostic-forward-accounts-summary.v1",
            "account_count": len(by_candidate),
            "priced_account_count": priced_count,
            "unpriced_account_count": len(by_candidate) - priced_count,
            "open_positions": open_positions,
            "closed_positions": closed_positions,
            "orders": order_count,
            "modeled_fills": fills,
            "entry_fills": entry_fills,
            "exit_fills": exit_fills,
            "late_data_gap_positions": late,
            "cash": round(cash, 8),
            "equity": round(equity, 8) if all_priced else None,
            "priced_equity": round(equity, 8),
            "realized_pnl": round(realized, 8),
            "unrealized_pnl": round(unrealized, 8) if all_priced else None,
            "priced_unrealized_pnl": round(unrealized, 8),
            "actual_fills": 0,
            "by_candidate": by_candidate[-64:],
        }

    @staticmethod
    def _validated_immutable_diagnostic_record(
            record: Mapping[str, Any], *, schema: str,
            identity_key: str) -> tuple[str, str, dict[str, Any]]:
        if not isinstance(record, Mapping) or record.get("schema") != schema:
            raise ShadowError("diagnostic account record is invalid")
        payload = dict(record)
        identity = str(payload.get(identity_key) or "")
        digest = str(payload.pop("digest", "") or "")
        if (not identity or not digest or
                diagnostic_content_digest(payload) != digest):
            raise ShadowError("diagnostic account record digest mismatch")
        return identity, digest, dict(record)

    def _apply_diagnostic_account_batch(
            self, db: sqlite3.Connection, *, cohort_identity: str,
            candidate_id: str, account_batch: Mapping[str, Any]) -> None:
        if (not isinstance(account_batch, Mapping) or
                account_batch.get("schema") !=
                "diagnostic-forward-account-batch.v1"):
            raise ShadowError("diagnostic account batch is invalid")
        account_update = account_batch.get("account")
        if not isinstance(account_update, Mapping):
            raise ShadowError("diagnostic account update is invalid")
        try:
            account = validate_account_state(
                account_update.get("state"),
                cohort_identity=str(cohort_identity),
                candidate_id=str(candidate_id))
        except DiagnosticAccountError as exc:
            raise ShadowError("diagnostic account update is invalid") from exc
        expected_account = account_update.get("previous_state_digest")
        current = db.execute("""SELECT state_digest FROM diagnostic_accounts
            WHERE cohort_identity=? AND candidate_id=?""",
            (str(cohort_identity), str(candidate_id))).fetchone()
        current_digest = str(current["state_digest"]) if current else None
        if current_digest != str(account["state_digest"]):
            if current_digest != expected_account:
                raise InputConflict(
                    f"diagnostic account {candidate_id} changed content")
            if current is None:
                db.execute("""INSERT INTO diagnostic_accounts
                    (cohort_identity,candidate_id,starting_cash,cash,equity,
                     realized_pnl,unrealized_pnl,open_position_count,
                     closed_position_count,order_count,fill_count,
                     late_data_gap_count,mark_status,last_event_key,last_event_at,
                     state_json,state_digest,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(cohort_identity), str(candidate_id),
                     *self._diagnostic_account_values(account)))
            else:
                db.execute("""UPDATE diagnostic_accounts SET
                    starting_cash=?,cash=?,equity=?,realized_pnl=?,
                    unrealized_pnl=?,open_position_count=?,
                    closed_position_count=?,order_count=?,fill_count=?,
                    late_data_gap_count=?,mark_status=?,last_event_key=?,
                    last_event_at=?,state_json=?,state_digest=?,updated_at=?
                    WHERE cohort_identity=? AND candidate_id=?""",
                    (*self._diagnostic_account_values(account),
                     str(cohort_identity), str(candidate_id)))
        positions = account_batch.get("positions") or ()
        if not isinstance(positions, Sequence) or isinstance(
                positions, (str, bytes, bytearray)):
            raise ShadowError("diagnostic position updates are invalid")
        for update in positions:
            if not isinstance(update, Mapping):
                raise ShadowError("diagnostic position update is invalid")
            try:
                position = validate_position_state(
                    update.get("state"), cohort_identity=str(cohort_identity),
                    candidate_id=str(candidate_id))
            except DiagnosticAccountError as exc:
                raise ShadowError("diagnostic position update is invalid") from exc
            position_id = str(position["position_id"])
            row = db.execute("""SELECT state_digest FROM diagnostic_positions
                WHERE position_id=?""", (position_id,)).fetchone()
            current_digest = str(row["state_digest"]) if row else None
            if current_digest == str(position["state_digest"]):
                continue
            if current_digest != update.get("previous_state_digest"):
                raise InputConflict(
                    f"diagnostic position {position_id} changed content")
            values = (
                str(cohort_identity), str(candidate_id),
                str(position["symbol"]), str(position["status"]),
                str(position["direction"]), float(position["quantity"]),
                float(position["entry_price"]), position.get("mark_price"),
                position.get("unrealized_pnl"),
                float(position.get("realized_pnl") or 0.0),
                int(position.get("late_data_gap") is True),
                str(position["entry_event_key"]),
                position.get("exit_event_key"), _json(position),
                str(position["state_digest"]), time.time())
            if row is None:
                db.execute("""INSERT INTO diagnostic_positions
                    (position_id,cohort_identity,candidate_id,symbol,status,
                     direction,quantity,entry_price,mark_price,unrealized_pnl,
                     realized_pnl,late_data_gap,entry_event_key,exit_event_key,
                     state_json,state_digest,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (position_id, *values))
            else:
                db.execute("""UPDATE diagnostic_positions SET
                    cohort_identity=?,candidate_id=?,symbol=?,status=?,
                    direction=?,quantity=?,entry_price=?,mark_price=?,
                    unrealized_pnl=?,realized_pnl=?,late_data_gap=?,
                    entry_event_key=?,exit_event_key=?,state_json=?,
                    state_digest=?,updated_at=? WHERE position_id=?""",
                    (*values, position_id))
        orders = account_batch.get("orders") or ()
        fills = account_batch.get("fills") or ()
        for record in orders:
            order_id, digest, payload = (
                self._validated_immutable_diagnostic_record(
                    record, schema=DIAGNOSTIC_ORDER_SCHEMA,
                    identity_key="order_id"))
            existing = db.execute("""SELECT digest FROM diagnostic_orders
                WHERE order_id=?""", (order_id,)).fetchone()
            if existing is not None:
                if str(existing["digest"]) != digest:
                    raise InputConflict(
                        f"diagnostic order {order_id} changed content")
                continue
            if (str(payload.get("cohort_identity") or "") !=
                    str(cohort_identity) or
                    str(payload.get("candidate_id") or "") !=
                    str(candidate_id)):
                raise ShadowError("diagnostic order identity conflicts")
            db.execute("""INSERT INTO diagnostic_orders
                (order_id,digest,cohort_identity,candidate_id,position_id,
                 event_key,action,status,order_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (order_id, digest, str(cohort_identity), str(candidate_id),
                 str(payload.get("position_id") or ""),
                 str(payload.get("event_key") or ""),
                 str(payload.get("action") or ""),
                 str(payload.get("status") or ""), _json(payload), time.time()))
        for record in fills:
            fill_id, digest, payload = (
                self._validated_immutable_diagnostic_record(
                    record, schema=DIAGNOSTIC_FILL_SCHEMA,
                    identity_key="fill_id"))
            existing = db.execute("""SELECT digest FROM diagnostic_fills
                WHERE fill_id=?""", (fill_id,)).fetchone()
            if existing is not None:
                if str(existing["digest"]) != digest:
                    raise InputConflict(
                        f"diagnostic fill {fill_id} changed content")
                continue
            order = db.execute("""SELECT candidate_id FROM diagnostic_orders
                WHERE order_id=?""", (str(payload.get("order_id") or ""),)
            ).fetchone()
            if (order is None or str(order["candidate_id"]) !=
                    str(candidate_id)):
                raise ShadowError("diagnostic fill order is unavailable")
            if (str(payload.get("cohort_identity") or "") !=
                    str(cohort_identity) or
                    str(payload.get("candidate_id") or "") !=
                    str(candidate_id)):
                raise ShadowError("diagnostic fill identity conflicts")
            db.execute("""INSERT INTO diagnostic_fills
                (fill_id,digest,order_id,cohort_identity,candidate_id,
                 position_id,event_key,action,fill_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fill_id, digest, str(payload.get("order_id") or ""),
                 str(cohort_identity), str(candidate_id),
                 str(payload.get("position_id") or ""),
                 str(payload.get("event_key") or ""),
                 str(payload.get("action") or ""), _json(payload), time.time()))

    def record_diagnostic_batch(
            self, *, cohort_identity: str, candidate_id: str,
            cursor_inserted_at: float, cursor_event_key: str,
            processed_events: int, rollups: Mapping[str, Mapping[str, int]],
            pending_sessions: Sequence[str], decisions: Sequence[Mapping[str, Any]],
            warmup_session: str | None, max_decisions: int,
            account_batch: Mapping[str, Any] | None = None) -> int:
        """Atomically persist sparse decisions and advance one arm cursor."""
        if self.readonly:
            raise ShadowError("cannot update diagnostic progress on a read-only WAL")
        persistent_kinds = {"open_incomplete", "reject", "unpriced"}
        normalized_rollups = {
            str(day): {str(kind): int(count)
                       for kind, count in counts.items()}
            for day, counts in rollups.items()
        }
        normalized_decisions = sorted(
            (_json(dict(decision)) for decision in decisions))
        batch_identity = _digest({
            "schema": "diagnostic-shadow-batch.v1",
            "cohort_identity": str(cohort_identity),
            "candidate_id": str(candidate_id),
            "cursor": [float(cursor_inserted_at), str(cursor_event_key)],
            "processed_events": int(processed_events),
            "rollups": normalized_rollups,
            "pending_sessions": sorted({str(day) for day in pending_sessions}),
            "decisions": normalized_decisions,
            "warmup_session": str(warmup_session or ""),
            "account_batch": (dict(account_batch)
                              if isinstance(account_batch, Mapping) else None),
        })
        inserted = 0
        with self._connection() as db:
            prior = db.execute("""SELECT * FROM diagnostic_progress
                WHERE cohort_identity=? AND candidate_id=?""",
                (str(cohort_identity), str(candidate_id))).fetchone()
            previous_rollups: dict[str, Any] = {
                "cumulative": {}, "sessions": {}, "completed_sessions": []}
            previous_pending: set[str] = set()
            previous_processed = 0
            previous_cursor = (0.0, "")
            if prior is not None:
                try:
                    decoded_rollups = json.loads(prior["rollups_json"])
                    decoded_pending = json.loads(prior["pending_sessions_json"])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ShadowError("diagnostic shadow progress is invalid") from exc
                if (not isinstance(decoded_rollups, Mapping) or
                        not isinstance(decoded_pending, list)):
                    raise ShadowError("diagnostic shadow progress is invalid")
                previous_rollups = dict(decoded_rollups)
                previous_pending = {str(value) for value in decoded_pending}
                previous_processed = int(prior["processed_events"])
                previous_cursor = (float(prior["last_inserted_at"]),
                                   str(prior["last_event_key"]))
            next_cursor = (float(cursor_inserted_at), str(cursor_event_key))
            if next_cursor < previous_cursor:
                raise ShadowError("diagnostic shadow cursor cannot move backwards")
            if prior is not None and next_cursor == previous_cursor:
                if previous_rollups.get("last_batch_identity") == batch_identity:
                    return 0
                raise InputConflict(
                    "diagnostic shadow equal-cursor batch conflicts with persisted progress")

            cumulative = dict(previous_rollups.get("cumulative") or {})
            sessions = {
                str(day): dict(counts)
                for day, counts in dict(
                    previous_rollups.get("sessions") or {}).items()
                if isinstance(counts, Mapping)
            }
            for day, counts in normalized_rollups.items():
                target = sessions.setdefault(str(day), {})
                for kind, raw_count in counts.items():
                    count = int(raw_count)
                    target[str(kind)] = int(target.get(str(kind), 0)) + count
                    if not str(kind).startswith(
                            DIAGNOSTIC_REASON_ROLLUP_PREFIX):
                        cumulative[str(kind)] = int(
                            cumulative.get(str(kind), 0)) + count
            sessions = {
                day: sessions[day]
                for day in sorted(sessions)[-MAX_DIAGNOSTIC_ROLLUP_SESSIONS:]
            }
            completed_sessions = {
                str(day) for day in
                (previous_rollups.get("completed_sessions") or ()) if str(day)
            }
            previous_pending.update(
                str(day) for day in pending_sessions
                if str(day) and str(day) not in completed_sessions)

            cohort_candidate_ids: list[str] = []
            for candidate_row in db.execute(
                    "SELECT candidate_id,config_json FROM candidates"):
                try:
                    candidate_config = json.loads(candidate_row["config_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    candidate_config = None
                marker = (candidate_config.get("diagnostic_shadow")
                          if isinstance(candidate_config, Mapping) else None)
                if (isinstance(marker, Mapping) and
                        marker.get("diagnostic_only") is True and
                        str(marker.get("cohort_identity") or "") ==
                        str(cohort_identity)):
                    cohort_candidate_ids.append(str(
                        candidate_row["candidate_id"]))
            if str(candidate_id) not in cohort_candidate_ids:
                cohort_candidate_ids.append(str(candidate_id))
            placeholders = ",".join("?" for _ in cohort_candidate_ids)
            diagnostic_count = int(db.execute(
                f"SELECT count(*) FROM decisions WHERE candidate_id IN "
                f"({placeholders})", tuple(cohort_candidate_ids)).fetchone()[0])
            for decision in decisions:
                kind = str(decision.get("kind") or "no_data")
                if kind not in persistent_kinds:
                    continue
                source_event_key = str(decision.get("event_key") or "")
                session_date = str(decision.get("session_date") or "")
                reason = str(decision.get("reason") or "unspecified")
                plan = decision.get("plan")
                payload = decision.get("payload")
                payload = dict(payload) if isinstance(payload, Mapping) else {}
                event_key = source_event_key
                if (kind in {"reject", "unpriced"} and
                        not isinstance(plan, Mapping)):
                    # Rejection and missing-quote traces can repeat for every
                    # bar. Preserve one immutable representative per
                    # arm/session/kind/reason; exact occurrence counts live in
                    # the bounded session rollup. Actionable plans are never
                    # collapsed by this path.
                    trace_identity = _digest({
                        "schema": "diagnostic-shadow-trace.v1",
                        "candidate_id": str(candidate_id),
                        "kind": kind,
                        "reason": reason,
                    })
                    event_key = (
                        f"{DIAGNOSTIC_CANDIDATE_PREFIX}trace:{trace_identity}")
                    marker = payload.get("diagnostic_shadow")
                    marker = (dict(marker) if isinstance(marker, Mapping)
                              else {})
                    marker["trace_representative"] = True
                    marker["representative_event_key"] = source_event_key
                    marker["trace_identity"] = trace_identity
                    marker["trace_group"] = {
                        "scope": "cohort",
                        "kind": kind,
                        "reason": reason,
                    }
                    marker["representative_session_date"] = session_date
                    payload["diagnostic_shadow"] = marker
                decision_id = _digest({"candidate_id": candidate_id,
                                       "event_key": event_key})
                exists = db.execute(
                    "SELECT 1 FROM decisions WHERE candidate_id=? AND event_key=?",
                    (str(candidate_id), event_key)).fetchone()
                if exists is not None:
                    continue
                if diagnostic_count + inserted >= int(max_decisions):
                    raise ShadowError(
                        f"diagnostic shadow decision bound {max_decisions} exceeded")
                db.execute("""INSERT INTO decisions
                    (decision_id,candidate_id,event_key,session_date,symbol,kind,
                     reason,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (decision_id, str(candidate_id), event_key, session_date,
                     str(decision.get("symbol") or ""), kind,
                     decision.get("reason"), _json(payload), time.time()))
                inserted += 1
                if (kind == "open_incomplete" and isinstance(plan, Mapping) and
                        (not warmup_session or session_date > warmup_session)):
                    quantity = _finite(plan.get("contracts", plan.get("shares")))
                    entry = _finite(plan.get("entry_price"))
                    db.execute("""INSERT OR IGNORE INTO virtual_books
                        (book_id,candidate_id,decision_id,symbol,status,quantity,
                         entry_price,plan_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (_digest({"candidate_id": candidate_id,
                                  "decision_id": decision_id}),
                         str(candidate_id), decision_id,
                         str(decision.get("symbol") or ""), "open_incomplete",
                         quantity, entry, _json(plan), time.time()))
            if account_batch is not None:
                self._apply_diagnostic_account_batch(
                    db, cohort_identity=str(cohort_identity),
                    candidate_id=str(candidate_id), account_batch=account_batch)
            encoded_rollups = {
                "cumulative": dict(sorted(cumulative.items())),
                "sessions": sessions,
                "completed_sessions": sorted(completed_sessions)[
                    -MAX_DIAGNOSTIC_ROLLUP_SESSIONS:],
                "last_batch_identity": batch_identity,
            }
            db.execute("""INSERT INTO diagnostic_progress
                (cohort_identity,candidate_id,last_inserted_at,last_event_key,
                 processed_events,rollups_json,pending_sessions_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(cohort_identity,candidate_id) DO UPDATE SET
                  last_inserted_at=excluded.last_inserted_at,
                  last_event_key=excluded.last_event_key,
                  processed_events=excluded.processed_events,
                  rollups_json=excluded.rollups_json,
                  pending_sessions_json=excluded.pending_sessions_json,
                  updated_at=excluded.updated_at""",
                (str(cohort_identity), str(candidate_id), next_cursor[0],
                 next_cursor[1], previous_processed + int(processed_events),
                 _json(encoded_rollups), _json(sorted(previous_pending)),
                 time.time()))
        return inserted

    def complete_diagnostic_replay(self, *, cohort_identity: str,
                                   candidate_id: str,
                                   session_date: str) -> None:
        """Remove one replay obligation after its durable outcome is written."""
        with self._connection() as db:
            row = db.execute("""SELECT pending_sessions_json,rollups_json
                FROM diagnostic_progress WHERE cohort_identity=? AND candidate_id=?""",
                (str(cohort_identity), str(candidate_id))).fetchone()
            if row is None:
                return
            try:
                pending = json.loads(row["pending_sessions_json"])
                rollups = json.loads(row["rollups_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ShadowError("diagnostic shadow progress is invalid") from exc
            if not isinstance(pending, list) or not isinstance(rollups, Mapping):
                raise ShadowError("diagnostic shadow progress is invalid")
            remaining = sorted({str(value) for value in pending
                                if str(value) != str(session_date)})
            encoded_rollups = dict(rollups)
            completed = {str(value) for value in
                         (encoded_rollups.get("completed_sessions") or ())}
            completed.add(str(session_date))
            encoded_rollups["completed_sessions"] = sorted(completed)[
                -MAX_DIAGNOSTIC_ROLLUP_SESSIONS:]
            db.execute("""UPDATE diagnostic_progress
                SET pending_sessions_json=?,rollups_json=?,updated_at=?
                WHERE cohort_identity=? AND candidate_id=?""",
                (_json(remaining), _json(encoded_rollups), time.time(),
                 str(cohort_identity), str(candidate_id)))

    def event_watermark(self) -> dict[str, Any]:
        """Return a scalar immutable-event boundary without loading the WAL."""
        with self._connection() as db:
            count = int(db.execute("SELECT count(*) FROM events").fetchone()[0])
            decision_events = int(db.execute("""SELECT count(*) FROM events
                WHERE event_type IN ('bar','bar_1m')""").fetchone()[0])
            row = db.execute("""SELECT event_key,timestamp,digest,event_json,inserted_at
                FROM events ORDER BY inserted_at DESC,event_key DESC LIMIT 1""").fetchone()
        available = None
        if row is not None:
            try:
                payload = json.loads(row["event_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, Mapping):
                available = _availability_time(payload)
        return {
            "count": count,
            "decision_event_count": decision_events,
            "last_event_key": (str(row["event_key"]) if row is not None else None),
            "last_timestamp": (str(row["timestamp"]) if row is not None else None),
            "last_available_at": (available.isoformat()
                                  if available is not None else None),
            "last_digest": (str(row["digest"]) if row is not None else None),
            "last_inserted_at": (float(row["inserted_at"])
                                 if row is not None else 0.0),
        }

    def forward_event_floor(self) -> float | None:
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='forward_event_floor'").fetchone()
        if row is None:
            return None
        try:
            value = float(row["value"])
        except (TypeError, ValueError) as exc:
            raise ShadowError("shadow forward event floor is invalid") from exc
        if not math.isfinite(value) or value < 0:
            raise ShadowError("shadow forward event floor is invalid")
        return value

    def save_forward_event_floor(self, value: float) -> None:
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES('forward_event_floor',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (str(float(value)),))

    def quarantine_through_session(self) -> str | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='quarantine_through_session'").fetchone()
        return None if row is None else str(row["value"])

    def save_quarantine_through_session(self, value: str) -> None:
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value)
                VALUES('quarantine_through_session',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (value,))

    def quarantine_events(self) -> dict[str, dict[str, Any]]:
        """Return durable malformed-event diagnostics awaiting corrected replay."""
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='quarantine_events'").fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow quarantine metadata is invalid") from exc
        if not isinstance(value, Mapping):
            raise ShadowError("shadow quarantine metadata is invalid")
        result: dict[str, dict[str, Any]] = {}
        for key, detail in value.items():
            if not isinstance(key, str) or not isinstance(detail, Mapping):
                raise ShadowError("shadow quarantine metadata is invalid")
            result[key] = dict(detail)
        return result

    def save_quarantine_events(self, value: Mapping[str, Mapping[str, Any]], *,
                               replace: bool = False) -> None:
        """Persist bounded malformed-event diagnostics as mutable metadata.

        This metadata is not authorizing evidence.  It records why a source
        offset remains uncommitted and is removed only when the exact event
        key successfully normalizes on a later corrected replay.
        """
        prior = {} if replace else self.quarantine_events()
        encoded = {**prior, **{str(key): dict(detail) for key, detail in value.items()}}
        if len(encoded) > MAX_QUARANTINE_EVENTS:
            # Do not silently discard malformed evidence when the diagnostic
            # bound is reached. Retain a deterministic prefix plus an
            # unknown-tail sentinel; its missing session identity forces all
            # replay/gate consumers closed until an operator repairs the
            # source and explicitly clears the quarantine metadata.
            existing_overflow = encoded.get(QUARANTINE_OVERFLOW_KEY, {})
            dropped = int(existing_overflow.get("dropped_events", 0) or 0)
            keys = sorted(key for key in encoded
                          if key != QUARANTINE_OVERFLOW_KEY)
            dropped += max(0, len(keys) - (MAX_QUARANTINE_EVENTS - 1))
            encoded = {key: encoded[key]
                       for key in keys[:MAX_QUARANTINE_EVENTS - 1]}
            encoded[QUARANTINE_OVERFLOW_KEY] = {
                "event_key": QUARANTINE_OVERFLOW_KEY,
                "session_date": None,
                "reason": "quarantine_overflow",
                "dropped_events": dropped,
                "unknown_tail": True,
            }
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES('quarantine_events',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (_json(encoded),))

    def session_catalog(self) -> dict[str, dict[str, Any]]:
        """Return recorder-calendar session provenance seen by ShadowRunner."""
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?",
                             (SESSION_CATALOG_META_KEY,)).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow session catalog metadata is invalid") from exc
        if not isinstance(value, Mapping):
            raise ShadowError("shadow session catalog metadata is invalid")
        result: dict[str, dict[str, Any]] = {}
        for session, detail in value.items():
            if not isinstance(session, str) or not isinstance(detail, Mapping):
                raise ShadowError("shadow session catalog metadata is invalid")
            result[session] = dict(detail)
        return result

    def save_session_catalog(self, value: Mapping[str, Mapping[str, Any]]) -> None:
        """Persist bounded exact-calendar session provenance.

        Only entries sourced from the recorder's Alpaca calendar may be used
        to detect an all-arm gap.  Timestamp-derived weekdays are deliberately
        not promoted to this catalog because holidays and early closes must
        remain an external authority.
        """
        if self.readonly:
            raise ShadowError("cannot update session catalog on a read-only WAL")
        incoming = {str(key): dict(detail) for key, detail in value.items()}
        existing = self.session_catalog()
        for key, detail in incoming.items():
            prior = existing.get(key)
            if prior is not None and _digest(prior) != _digest(detail):
                raise ShadowError(f"session catalog provenance conflicts for {key}")
        # The catalog is monotonic: a correction can add a session, but cannot
        # erase or replace exchange-calendar authority already observed.
        encoded = {**existing, **incoming}
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (SESSION_CATALOG_META_KEY, _json(encoded)))

    def record_session_calendar(self, session_date: str, *, opened: str,
                                closed: str, source: str) -> None:
        if self.readonly:
            raise ShadowError("cannot update session catalog on a read-only WAL")
        catalog = self.session_catalog()
        existing = catalog.get(str(session_date))
        if existing is not None:
            expected = {"session_date": str(session_date), "open": str(opened),
                        "close": str(closed), "source": str(source)}
            if any(str(existing.get(key)) != value for key, value in expected.items()):
                raise ShadowError(f"session catalog provenance conflicts for {session_date}")
            return
        catalog[str(session_date)] = {
            "session_date": str(session_date), "open": str(opened),
            "close": str(closed), "source": str(source),
            "recorded_ts": time.time(),
        }
        self.save_session_catalog(catalog)

    def replay_quarantine(self) -> dict[str, dict[str, Any]]:
        """Return durable replay repair/quarantine state.

        A replay that is incomplete or semantically mismatched is never
        silently treated as a missing row.  The shadow worker records a
        bounded diagnostic entry keyed by candidate/session; a later complete
        parity replay changes that same entry to ``repaired`` and retains the
        prior digests for audit.  This projection is also readable from the
        ingest-only process (which opens the WAL read-only).
        """
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key=?",
                (REPLAY_QUARANTINE_META_KEY,)).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow replay quarantine metadata is invalid") from exc
        if not isinstance(value, Mapping):
            raise ShadowError("shadow replay quarantine metadata is invalid")
        result: dict[str, dict[str, Any]] = {}
        for key, detail in value.items():
            if not isinstance(key, str) or not isinstance(detail, Mapping):
                raise ShadowError("shadow replay quarantine metadata is invalid")
            result[key] = dict(detail)
        return result

    def _save_replay_quarantine(self, value: Mapping[str, Mapping[str, Any]]) -> None:
        # Keep every unresolved entry (they are the repair boundary), while
        # bounding retained repaired history.  If the active set exceeds the
        # explicit safety limit, persist a visible overflow sentinel with a
        # deterministic digest/count; ingestion treats it as a global block.
        active = {
            str(key): dict(detail) for key, detail in value.items()
            if str(key) != REPLAY_QUARANTINE_OVERFLOW_KEY
            and str((detail or {}).get("status") or "") != "repaired"
        }
        repaired = [
            (str(key), dict(detail)) for key, detail in value.items()
            if str(key) != REPLAY_QUARANTINE_OVERFLOW_KEY
            and str((detail or {}).get("status") or "") == "repaired"
        ]
        repaired.sort(key=lambda item: (
            float(item[1].get("repaired_ts", 0.0) or 0.0), item[0]))
        encoded: dict[str, dict[str, Any]] = dict(active)
        encoded.update(dict(repaired[-MAX_REPLAY_REPAIR_HISTORY:]))
        if len(active) > MAX_ACTIVE_REPLAY_QUARANTINE:
            active_keys = sorted(active)
            encoded[REPLAY_QUARANTINE_OVERFLOW_KEY] = {
                "schema": "shadow-replay-repair.v1",
                "candidate_id": None,
                "session_date": None,
                "status": "overflow",
                "reason": "active replay quarantine exceeds safety bound",
                "max_active": MAX_ACTIVE_REPLAY_QUARANTINE,
                "active_count": len(active),
                "active_digest": _digest(active_keys),
                "unknown_tail": True,
                "updated_ts": time.time(),
            }
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (REPLAY_QUARANTINE_META_KEY, _json(encoded)))

    @staticmethod
    def _replay_quarantine_key(candidate_id: str, session_date: str) -> str:
        return f"{candidate_id}:{session_date}"

    def quarantine_replay_session(self, *, candidate_id: str,
                                  session_date: str, reason: str,
                                  status: str, source_digest: str | None = None,
                                  shadow_digest: str | None = None,
                                  replay_digest: str | None = None) -> dict[str, Any]:
        """Record a blocked replay session without deleting any evidence."""
        if self.readonly:
            raise ShadowError("cannot update replay quarantine on a read-only WAL")
        if status not in {"incomplete", "mismatch"}:
            raise ValueError("replay quarantine status must be incomplete or mismatch")
        key = self._replay_quarantine_key(str(candidate_id), str(session_date))
        quarantine = self.replay_quarantine()
        now = time.time()
        previous = quarantine.get(key, {})
        history = list(previous.get("history") or []) if isinstance(previous, Mapping) else []
        history.append({
            "status": status, "reason": str(reason),
            "source_digest": source_digest, "shadow_digest": shadow_digest,
            "replay_digest": replay_digest, "observed_ts": now,
        })
        history = history[-MAX_REPLAY_REPAIR_HISTORY:]
        entry = {
            "schema": "shadow-replay-repair.v1",
            "candidate_id": str(candidate_id),
            "session_date": str(session_date),
            "status": "quarantined",
            "reason": str(reason),
            "source_digest": source_digest,
            "shadow_digest": shadow_digest,
            "replay_digest": replay_digest,
            "first_seen_ts": previous.get("first_seen_ts", now),
            "last_seen_ts": now,
            "repair_count": int(previous.get("repair_count", 0) or 0),
            "history": history,
        }
        quarantine[key] = entry
        self._save_replay_quarantine(quarantine)
        return entry

    def repair_replay_session(self, *, candidate_id: str,
                              session_date: str, source_digest: str,
                              shadow_digest: str, replay_digest: str,
                              reason: str = "complete parity replay") -> dict[str, Any] | None:
        """Persist an explicit repaired/replayed transition for a session."""
        if self.readonly:
            raise ShadowError("cannot update replay quarantine on a read-only WAL")
        key = self._replay_quarantine_key(str(candidate_id), str(session_date))
        quarantine = self.replay_quarantine()
        previous = quarantine.get(key)
        if not isinstance(previous, Mapping):
            return None
        now = time.time()
        history = list(previous.get("history") or [])
        history.append({
            "status": "repaired", "reason": str(reason),
            "source_digest": source_digest, "shadow_digest": shadow_digest,
            "replay_digest": replay_digest, "repaired_ts": now,
        })
        entry = dict(previous)
        entry.update({
            "status": "repaired", "reason": str(reason),
            "source_digest": source_digest, "shadow_digest": shadow_digest,
            "replay_digest": replay_digest,
            "repaired_ts": now,
            "repair_count": int(previous.get("repair_count", 0) or 0) + 1,
            "history": history[-MAX_REPLAY_REPAIR_HISTORY:],
        })
        quarantine[key] = entry
        self._save_replay_quarantine(quarantine)
        return entry

    def upsert_candidate(self, candidate: Mapping[str, Any]) -> None:
        config = candidate.get("config")
        if config is None:
            encoded = candidate.get("config_json")
            if isinstance(encoded, str):
                try:
                    decoded = json.loads(encoded)
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded = None
                if isinstance(decoded, Mapping):
                    config = decoded
        if not isinstance(config, Mapping):
            config = {}
        if is_diagnostic_candidate({**dict(candidate), "config": config}):
            proof = {
                "diagnostic_only": True,
                "authorizing": False,
                "gate_eligible": False,
                "promotion_eligible": False,
            }
        else:
            proof = {key: candidate.get(key) for key in (
                "dataset_hash", "config_hash", "code_hash", "provenance_hash", "status")}
        with self._connection() as db:
            db.execute("""INSERT OR IGNORE INTO candidates
                (candidate_id,variant_id,strategy_id,vehicle,status,config_json,proof_json,observed_at)
                VALUES(?,?,?,?,?,?,?,?)""", (
                    str(candidate["candidate_id"]), str(candidate.get("variant_id") or ""),
                    str(candidate.get("strategy_id") or ""), str(candidate.get("vehicle") or ""),
                    str(candidate.get("status") or ""), _json(config), _json(proof), time.time()))

    def decision(self, *, candidate_id: str, event_key: str, session_date: str,
                 symbol: str, kind: str, reason: str | None, payload: Mapping,
                 max_decisions: int) -> bool:
        decision_id = _digest({"candidate_id": candidate_id, "event_key": event_key})
        diagnostic = self.candidate_is_diagnostic(str(candidate_id))
        with self._connection() as db:
            existing = db.execute("SELECT 1 FROM decisions WHERE candidate_id=? AND event_key=?",
                                  (candidate_id, event_key)).fetchone()
            if existing is not None:
                return False
            if diagnostic:
                count = int(db.execute("""SELECT count(*)
                    FROM decisions d LEFT JOIN candidates c
                      ON c.candidate_id=d.candidate_id
                    WHERE d.candidate_id LIKE ?
                       OR c.proof_json LIKE '%\"diagnostic_only\":true%'
                       OR c.config_json LIKE '%\"diagnostic_only\":true%'""",
                    (f"{DIAGNOSTIC_CANDIDATE_PREFIX}%",)).fetchone()[0])
            else:
                count = int(db.execute("""SELECT count(*)
                    FROM decisions d LEFT JOIN candidates c
                      ON c.candidate_id=d.candidate_id
                    WHERE d.candidate_id NOT LIKE ?
                      AND COALESCE(c.proof_json,'') NOT LIKE '%\"diagnostic_only\":true%'
                      AND COALESCE(c.config_json,'') NOT LIKE '%\"diagnostic_only\":true%'""",
                    (f"{DIAGNOSTIC_CANDIDATE_PREFIX}%",)).fetchone()[0])
            if count >= int(max_decisions):
                lane = "diagnostic shadow" if diagnostic else "shadow"
                raise ShadowError(f"{lane} decision bound {max_decisions} exceeded")
            db.execute("""INSERT INTO decisions
                (decision_id,candidate_id,event_key,session_date,symbol,kind,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (decision_id, candidate_id, event_key,
                    session_date, symbol, kind, reason, _json(payload), time.time()))
        return True

    def virtual_open(self, *, candidate_id: str, decision_id: str, symbol: str,
                     plan: Mapping) -> None:
        quantity = _finite(plan.get("contracts", plan.get("shares")))
        entry = _finite(plan.get("entry_price"))
        with self._connection() as db:
            db.execute("""INSERT OR IGNORE INTO virtual_books
                (book_id,candidate_id,decision_id,symbol,status,quantity,entry_price,plan_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (_digest({"candidate_id": candidate_id,
                "decision_id": decision_id}), candidate_id, decision_id, symbol,
                "open_incomplete", quantity, entry, _json(plan), time.time()))

    def has_open(self, candidate_id: str, symbol: str) -> bool:
        with self._connection() as db:
            return db.execute("""SELECT 1 FROM virtual_books WHERE candidate_id=?
                AND symbol=? AND status='open_incomplete' LIMIT 1""", (candidate_id, symbol)).fetchone() is not None

    def open_books(self, candidate_id: str) -> list[dict]:
        """Return this candidate's still-open virtual books only.

        The shadow lane never shares portfolio state across candidates.  This
        read is intentionally scoped by candidate and leaves the immutable
        book rows untouched; callers decode ``plan_json`` for admission.
        """
        with self._connection() as db:
            rows = db.execute("""SELECT * FROM virtual_books
                WHERE candidate_id=? AND status='open_incomplete'
                ORDER BY created_at, book_id""", (candidate_id,)).fetchall()
            return [dict(row) for row in rows]

    def close_session_books(self, candidate_id: str, session_date: str) -> int:
        """Close incomplete virtual observations once that session is replayed."""
        with self._connection() as db:
            cursor = db.execute("""UPDATE virtual_books SET status='closed_replay'
                WHERE candidate_id=? AND status='open_incomplete' AND decision_id IN
                    (SELECT decision_id FROM decisions WHERE candidate_id=? AND session_date=?)""",
                               (candidate_id, candidate_id, session_date))
            return int(cursor.rowcount)

    def candidates(self) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM candidates ORDER BY candidate_id")]

    @staticmethod
    def _candidate_row_is_diagnostic(candidate_id: str,
                                     config_json: Any = None,
                                     proof_json: Any = None) -> bool:
        if is_diagnostic_candidate(str(candidate_id)):
            return True
        decoded: dict[str, Any] = {}
        for key, encoded in (("config", config_json), ("proof", proof_json)):
            try:
                value = (json.loads(encoded) if isinstance(encoded, str)
                         else encoded)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = None
            decoded[key] = dict(value) if isinstance(value, Mapping) else {}
        if is_diagnostic_candidate({
                "candidate_id": str(candidate_id),
                "config": decoded["config"]}):
            return True
        return decoded["proof"].get("diagnostic_only") is True

    def candidate_is_diagnostic(self, candidate_id: str) -> bool:
        if is_diagnostic_candidate(str(candidate_id)):
            return True
        with self._connection() as db:
            row = db.execute(
                "SELECT config_json,proof_json FROM candidates WHERE candidate_id=?",
                (str(candidate_id),)).fetchone()
        return bool(row is not None and self._candidate_row_is_diagnostic(
            str(candidate_id), row["config_json"], row["proof_json"]))

    def events(self, *, inserted_after: float = 0.0) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE inserted_at>=? ORDER BY timestamp,event_key",
                (float(inserted_after),))]

    def events_after_cursor(self, *, inserted_at: float, event_key: str,
                            max_events: int) -> list[dict]:
        """Read a bounded insertion-ordered diagnostic increment."""
        with self._connection() as db:
            rows = db.execute("""SELECT * FROM events
                WHERE inserted_at>? OR (inserted_at=? AND event_key>?)
                ORDER BY inserted_at,event_key LIMIT ?""",
                (float(inserted_at), float(inserted_at), str(event_key),
                 int(max_events) + 1)).fetchall()
        if len(rows) > int(max_events):
            raise ShadowError(
                f"diagnostic shadow event batch bound {max_events} exceeded")
        return [dict(row) for row in rows]

    def events_for_sessions(self, sessions: Sequence[str], *,
                            max_events: int) -> list[dict]:
        """Read bounded immutable events for replay-correction sessions only."""
        ranges: list[tuple[str, str]] = []
        for raw in sorted({str(value) for value in sessions if str(value).strip()}):
            try:
                day = date.fromisoformat(raw)
            except ValueError:
                continue
            opened = datetime.combine(day, dt_time.min, tzinfo=NEW_YORK).astimezone(UTC)
            ranges.append((opened.isoformat(), (opened + timedelta(days=1)).isoformat()))
        if not ranges:
            return []
        clauses = " OR ".join("(timestamp>=? AND timestamp<?)" for _ in ranges)
        params: list[Any] = [bound for item in ranges for bound in item]
        params.append(int(max_events) + 1)
        with self._connection() as db:
            rows = db.execute(
                f"SELECT * FROM events WHERE {clauses} "
                "ORDER BY timestamp,event_key LIMIT ?", tuple(params)).fetchall()
        if len(rows) > int(max_events):
            raise ShadowError(
                f"shadow replay validation event bound {max_events} exceeded")
        return [dict(row) for row in rows]

    def decisions(self, candidate_id: str | None = None) -> list[dict]:
        with self._connection() as db:
            if candidate_id:
                rows = db.execute("SELECT * FROM decisions WHERE candidate_id=? ORDER BY created_at,decision_id", (candidate_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM decisions ORDER BY created_at,decision_id").fetchall()
            return [dict(row) for row in rows]

    def decision_count(self) -> int:
        """Return a scalar decision count without materializing WAL rows."""
        with self._connection() as db:
            return int(db.execute("SELECT count(*) FROM decisions").fetchone()[0])

    def gate_sessions(self) -> list[tuple[str, str]]:
        """Return candidate/session pairs whose replay evidence currently gates."""
        with self._connection() as db:
            rows = db.execute("""SELECT DISTINCT t.candidate_id,t.session_date,
                       c.config_json,c.proof_json
                FROM shadow_trades t JOIN replay_diffs d
                  ON d.candidate_id=t.candidate_id
                 AND d.session_date=t.session_date
                 AND d.replay_digest=t.replay_digest
                LEFT JOIN candidates c ON c.candidate_id=t.candidate_id
                WHERE d.status='match' AND t.replay_status='match'
                ORDER BY t.session_date,t.candidate_id""").fetchall()
        return [
            (str(row["candidate_id"]), str(row["session_date"]))
            for row in rows
            if not self._candidate_row_is_diagnostic(
                str(row["candidate_id"]), row["config_json"], row["proof_json"])
        ]

    def replay_diff(self, *, candidate_id: str, session_date: str, source_digest: str,
                    shadow_digest: str, replay_digest: str | None, status: str,
                    details: Mapping) -> None:
        with self._connection() as db:
            db.execute("""INSERT INTO replay_diffs
                (diff_id,candidate_id,session_date,source_digest,shadow_digest,replay_digest,status,details_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id,session_date) DO UPDATE SET
                    source_digest=excluded.source_digest,
                    shadow_digest=excluded.shadow_digest,
                    replay_digest=excluded.replay_digest,
                    status=excluded.status,
                    details_json=excluded.details_json,
                    created_at=excluded.created_at""", (_digest({"candidate_id": candidate_id, "session_date": session_date}),
                    candidate_id, session_date, source_digest, shadow_digest, replay_digest,
                    status, _json(details), time.time()))

    def record_replay_evidence(self, *, candidate_id: str, session_date: str,
                               replay_digest: str, vehicle: str,
                               starting_cash: float, ending_cash: float,
                               realized_pnl: float, trades: Sequence[Mapping],
                               replay_status: str) -> None:
        """Persist immutable replay outcomes in the isolated shadow WAL.

        Rows are deliberately not written to EdgeLedger.  ``gate_rows`` below
        joins them to the current replay diff and exposes them only when the
        completed same-session replay has semantic parity.
        """
        values = (float(starting_cash), float(ending_cash), float(realized_pnl))
        if not all(math.isfinite(value) for value in values):
            raise ShadowError("shadow account cash/P&L must be finite")
        ordered: list[dict[str, Any]] = []
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            ordered.append(dict(trade))
        ordered.sort(key=_json)
        trade_count = len([row for row in ordered if row.get("no_trade") is not True])
        account_id = _digest({"candidate_id": candidate_id,
                              "session_date": session_date,
                              "replay_digest": replay_digest})
        with self._connection() as db:
            db.execute("""INSERT OR IGNORE INTO shadow_accounts
                (account_id,candidate_id,session_date,replay_digest,vehicle,
                 starting_cash,ending_cash,realized_pnl,trade_count,replay_status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    account_id, candidate_id, session_date, replay_digest,
                    str(vehicle), values[0], values[1], values[2], trade_count,
                    str(replay_status), time.time()))
            occurrence: dict[str, int] = {}
            for trade in ordered:
                trade_digest = _digest(trade)
                index = occurrence.get(trade_digest, 0)
                occurrence[trade_digest] = index + 1
                trade_id = _digest({"candidate_id": candidate_id,
                                    "session_date": session_date,
                                    "replay_digest": replay_digest,
                                    "trade_digest": trade_digest,
                                    "occurrence": index})
                db.execute("""INSERT OR IGNORE INTO shadow_trades
                    (trade_id,candidate_id,session_date,replay_digest,replay_status,
                     trade_json,created_at)
                    VALUES(?,?,?,?,?,?,?)""", (
                        trade_id, candidate_id, session_date, replay_digest,
                        str(replay_status), _json(trade), time.time()))

    def replay_accounts(self, candidate_id: str | None = None) -> list[dict]:
        with self._connection() as db:
            if candidate_id is None:
                rows = db.execute("""SELECT * FROM shadow_accounts
                    ORDER BY session_date,candidate_id,replay_digest""").fetchall()
            else:
                rows = db.execute("""SELECT * FROM shadow_accounts
                    WHERE candidate_id=? ORDER BY session_date,replay_digest""",
                                  (candidate_id,)).fetchall()
            return [dict(row) for row in rows]

    def replay_metadata(self, candidate_id: str | None = None) -> list[dict]:
        """Return replay diffs joined to their immutable account summaries.

        Ingestion uses this read-only projection as the completion/parity
        boundary.  Keeping the join here prevents callers from accidentally
        treating a diagnostic trade row as authorizing evidence without its
        same-session source and replay digests.
        """
        query = """SELECT d.candidate_id, d.session_date, d.source_digest,
                   d.shadow_digest, d.replay_digest, d.status,
                   d.details_json, a.account_id, a.vehicle, a.starting_cash,
                   a.ending_cash, a.realized_pnl, a.trade_count,
                   a.replay_status, a.created_at AS account_created_at
                   FROM replay_diffs d LEFT JOIN shadow_accounts a
                   ON a.candidate_id=d.candidate_id
                   AND a.session_date=d.session_date
                   AND a.replay_digest=d.replay_digest"""
        params: tuple[Any, ...] = ()
        if candidate_id is not None:
            query += " WHERE d.candidate_id=?"
            params = (str(candidate_id),)
        query += " ORDER BY d.session_date,d.candidate_id"
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["details"] = {}
            result.append(item)
        return result

    def gate_rows(self, candidate_id: str, session_date: str | None = None) -> list[dict]:
        """Return rows eligible for existing gates after replay parity only."""
        if self.candidate_is_diagnostic(str(candidate_id)):
            return []
        params: list[Any] = [candidate_id]
        clause = ""
        if session_date is not None:
            clause = " AND t.session_date=?"
            params.append(session_date)
        with self._connection() as db:
            rows = db.execute(f"""SELECT t.trade_json FROM shadow_trades t
                JOIN replay_diffs d ON d.candidate_id=t.candidate_id
                    AND d.session_date=t.session_date
                    AND d.replay_digest=t.replay_digest
                WHERE t.candidate_id=? AND d.status='match'
                    AND t.replay_status='match'{clause}
                ORDER BY t.session_date,t.trade_id""", tuple(params)).fetchall()
            result: list[dict] = []
            for row in rows:
                try:
                    item = json.loads(row["trade_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(item, Mapping):
                    result.append(dict(item))
            return result

    def prune(self) -> dict[str, Any]:
        """Prune only derived replay metadata and report what was removed.

        Source events, decisions, accounts, and trades are immutable evidence
        and are intentionally never part of retention.  Returning bounded
        counters makes retention visible to the polling heartbeat without
        changing the evidence contract.
        """
        floor = time.time() - max(1, self.retention_days) * 86400
        # Retention is the only intentional deletion.  The append-only rows
        # themselves cannot be updated or deleted by ordinary writes.  Fixed
        # diagnostic cohorts are preregistered forward evidence, so even their
        # derived replay diffs remain outside retention.
        with self._connection() as db:
            before = int(db.execute(
                """SELECT count(*) FROM replay_diffs
                   WHERE created_at < ? AND candidate_id NOT LIKE ?""",
                (floor, f"{DIAGNOSTIC_CANDIDATE_PREFIX}%")).fetchone()[0])
            previous = db.execute(
                "SELECT value FROM meta WHERE key='replay_diff_prune_watermark'").fetchone()
            if before:
                latest_session = db.execute(
                    """SELECT MAX(session_date) FROM replay_diffs
                       WHERE created_at < ? AND candidate_id NOT LIKE ?""",
                    (floor, f"{DIAGNOSTIC_CANDIDATE_PREFIX}%")).fetchone()[0]
                watermark = {
                    "floor_ts": float(floor),
                    "pruned_replay_diffs": before,
                    "latest_pruned_session": (str(latest_session)
                                               if latest_session else None),
                    "updated_ts": time.time(),
                }
                # The watermark is diagnostic metadata, not an authorizing
                # row. Commit it in the same transaction before deleting
                # derived diffs so a crash cannot erase the gap indication.
                db.execute("""INSERT INTO meta(key,value) VALUES('replay_diff_prune_watermark',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                           (_json(watermark),))
            else:
                try:
                    watermark = (json.loads(previous["value"])
                                 if previous is not None else None)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ShadowError("shadow retention watermark is invalid") from exc
                if watermark is not None and not isinstance(watermark, Mapping):
                    raise ShadowError("shadow retention watermark is invalid")
            db.execute(
                """DELETE FROM replay_diffs
                   WHERE created_at < ? AND candidate_id NOT LIKE ?""",
                (floor, f"{DIAGNOSTIC_CANDIDATE_PREFIX}%"))
        return {"retention_days": int(self.retention_days),
                "retention_floor_ts": float(floor),
                "pruned_replay_diffs": before,
                "retention_gap_watermark": watermark}

    def prune_watermark(self) -> dict[str, Any] | None:
        """Return the last non-authorizing replay-diff retention watermark."""
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='replay_diff_prune_watermark'").fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow retention watermark is invalid") from exc
        if not isinstance(value, Mapping):
            raise ShadowError("shadow retention watermark is invalid")
        return dict(value)


def _read_candidates(path: Path, *, max_candidates: int) -> list[dict]:
    """Resolve candidates without constructing EdgeLedger (which migrates DBs)."""
    if not path.exists():
        return []
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        # ``sqlite3.Connection`` implements the transaction context manager,
        # but that manager does not close the connection on exit.  These
        # read-only helpers are called on every shadow poll; explicitly close
        # each handle so repeated polls cannot leak file descriptors.
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            # Diagnostic arms are owned exclusively by the isolated shadow
            # WAL.  Reject any attempted registration in EdgeLedger even when
            # its lifecycle status would otherwise keep it out of this poll.
            for marker_row in db.execute(
                    "SELECT candidate_id,config_json FROM candidates").fetchall():
                try:
                    marker_config = json.loads(marker_row["config_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    marker_config = {}
                if is_diagnostic_candidate({
                        "candidate_id": marker_row["candidate_id"],
                        "config": marker_config}):
                    raise ShadowError(
                        "diagnostic shadow candidates cannot be ingested from EdgeLedger")
            rows = db.execute("""SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                ON c.candidate_id=s.candidate_id
                WHERE (s.status IN ('backtest_passed','shadow','demoted','validated','champion')
                       AND c.strategy_id IN ('ibr','rule'))
                   OR (c.strategy_id='ibr' AND c.variant_id='ibr.baseline')
                ORDER BY CASE WHEN c.strategy_id='ibr' AND c.variant_id='ibr.baseline'
                              THEN 0 ELSE 1 END, c.created_at,c.candidate_id
                LIMIT ?""", (int(max_candidates),)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["config"] = json.loads(item.pop("config_json"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["config"] = {}
                try:
                    item["axes"] = json.loads(item.pop("axes_json"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["axes"] = {}
                if is_diagnostic_candidate(item):
                    raise ShadowError(
                        "diagnostic shadow candidates cannot be ingested from EdgeLedger")
                result.append(item)
            return result
    except sqlite3.OperationalError as exc:
        # A deployment may start before the research cycle has created its
        # ledger.  Treat that as an empty read-only source, never initialize
        # or mutate it from this process.
        if "no such table" in str(exc).lower():
            return []
        raise ShadowError(f"cannot read EdgeLedger read-only: {exc}") from exc
    except sqlite3.Error as exc:
        raise ShadowError(f"cannot read EdgeLedger read-only: {exc}") from exc


def _read_factory_rule_roots(path: Path) -> dict[str, dict[str, Any]]:
    """Read immutable factory root specs without opening a writable ledger.

    The live shadow worker may read the research ledger, but it must never
    initialize or migrate it.  ``factory_hypotheses`` is therefore queried
    directly through SQLite's read-only URI and malformed rows are ignored.
    """
    if not path.is_file():
        return {}
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT hypothesis_id,vehicle,spec_json FROM factory_hypotheses"
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    roots: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            raw = json.loads(row["spec_json"])
            spec = validate_rule_spec(raw)
            roots[str(row["hypothesis_id"])] = {
                "vehicle": str(row["vehicle"]),
                "rule_spec": spec,
                "variant_id": rule_variant_id(spec),
            }
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return roots


def _policy(config: Mapping[str, Any]) -> ReplayPolicy:
    try:
        return replace(ReplayPolicy.from_config(config), strict_market_data=True)
    except Exception:
        session = config.get("session") if isinstance(config, Mapping) else {}
        required = bool(session.get("require_exact_calendar", False)) \
            if isinstance(session, Mapping) else False
        return ReplayPolicy(strict_market_data=True,
                            require_exact_calendar=required)


def _safe_config(candidate: Mapping[str, Any]) -> dict:
    config = candidate.get("config")
    # EdgeLedger's public candidate row stores the immutable configuration as
    # ``config_json``.  The read-only resolver decodes that field before
    # handing rows to the runner, but direct callers (and older integrations)
    # may provide the raw ledger row.  Preserve the same replay path for both
    # shapes without ever mutating the candidate mapping.
    if config is None:
        encoded = candidate.get("config_json")
        if isinstance(encoded, str):
            try:
                decoded = json.loads(encoded)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, Mapping):
                config = decoded
    out = dict(config) if isinstance(config, Mapping) else {}
    strategy = dict(out.get("strategy") or {})
    strategy.setdefault("id", candidate.get("strategy_id") or "ibr")
    strategy.setdefault("version", candidate.get("base_version") or "v1")
    strategy.setdefault("variant_id", candidate.get("variant_id"))
    out["strategy"] = strategy
    return out


class ShadowRunner:
    """Incrementally ingest, evaluate, and replay one broker-free corpus."""

    def __init__(self, config: ShadowConfig):
        self.config = config
        self.store = ShadowStore(config.shadow_db, retention_days=config.retention_days)
        self._factory_roots = _read_factory_rule_roots(config.edge_db)
        # Workers install an in-memory portfolio projection for their arm.
        # Thread-local state keeps the existing ``_evaluate`` call contract
        # (and test seams) while ensuring no worker mutates or observes a
        # sibling candidate's virtual book.
        self._worker_state = threading.local()

    def _prepare_diagnostic_cohort(self) -> tuple[
            dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
        """Preregister the current code/config cohort before corpus observation."""
        if not self.config.diagnostic:
            return None, [], None
        cohort = build_diagnostic_cohort(
            self.config.runtime_config or {}, code_identity=_replay_code_hash())
        cohort = self.store.save_diagnostic_cohort(cohort)
        arms = [dict(arm) for arm in cohort.get("arms", ())
                if isinstance(arm, Mapping)]
        for arm in arms:
            self.store.upsert_candidate(arm)
        activation = self.store.diagnostic_activation(
            str(cohort["cohort_identity"]))
        return cohort, arms, activation

    def _activate_diagnostic_cohort(
            self, cohort: Mapping[str, Any], *, source_offsets: Mapping[str, int],
            forward_event_floor: float) -> dict[str, Any]:
        watermark = self.store.event_watermark()
        return self.store.save_diagnostic_activation(
            cohort=cohort, activation_event_watermark=watermark,
            source_offsets=source_offsets,
            forward_event_floor=float(forward_event_floor))

    @staticmethod
    def _diagnostic_event_provenance(
            event: Mapping[str, Any],
            activation: Mapping[str, Any]) -> tuple[bool, str | None]:
        """Validate insertion, recorder-offset, and observation-time causality."""
        watermark = activation.get("activation_event_watermark")
        floor = (_finite(watermark.get("last_inserted_at"))
                 if isinstance(watermark, Mapping) else None)
        floor_key = (str(watermark.get("last_event_key") or "")
                     if isinstance(watermark, Mapping) else "")
        inserted = _finite(event.get("inserted_at"))
        event_key = str(event.get("event_key") or "")
        if (floor is None or inserted is None or
                (inserted, event_key) <= (floor, floor_key)):
            return False, "preactivation_insertion"
        try:
            payload = (json.loads(event.get("event_json"))
                       if isinstance(event.get("event_json"), str)
                       else event.get("event_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, Mapping):
            return False, "invalid_event_payload"
        source_mode = str(
            payload.get("source_mode") or "forward_observed").strip().lower()
        if source_mode != "forward_observed":
            return False, f"non_forward_source_mode:{source_mode or 'missing'}"
        observed_at = _timestamp(payload.get("observed_at"))
        activated_at = _timestamp(activation.get("activated_at"))
        if (observed_at is None or activated_at is None or
                observed_at < activated_at):
            return False, "preactivation_observed_at"
        source_path = str(event.get("source_path") or "")
        source_start = event.get("source_offset_start")
        source_end = event.get("source_offset_end")
        offsets = activation.get("source_offsets")
        if (not source_path or isinstance(source_start, bool) or
                isinstance(source_end, bool) or
                not isinstance(source_start, int) or
                not isinstance(source_end, int) or source_start < 0 or
                source_end <= source_start or not isinstance(offsets, Mapping)):
            return False, "missing_forward_source_provenance"
        activation_offset = offsets.get(source_path, 0)
        if (isinstance(activation_offset, bool) or
                not isinstance(activation_offset, int) or
                source_start < activation_offset or
                source_end <= activation_offset):
            return False, "preactivation_source_offset"
        return True, None

    @classmethod
    def _diagnostic_event_eligible(cls, event: Mapping[str, Any],
                                   activation: Mapping[str, Any]) -> bool:
        return cls._diagnostic_event_provenance(event, activation)[0]

    @staticmethod
    def _diagnostic_decision_payload(
            arm: Mapping[str, Any], activation: Mapping[str, Any],
            payload: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        session = str(payload.get("session_date") or "")
        warmup_session = str(activation.get("warmup_session") or "")
        result["diagnostic_shadow"] = {
            "schema": "diagnostic-shadow-decision.v1",
            "diagnostic_only": True,
            "authorizing": False,
            "gate_eligible": False,
            "promotion_eligible": False,
            "family": arm.get("family"),
            "role": arm.get("role"),
            "candidate_id": arm.get("candidate_id"),
            "variant_id": arm.get("variant_id"),
            "spec_identity": arm.get("spec_identity"),
            "config_identity": arm.get("config_identity"),
            "code_identity": arm.get("code_identity"),
            "cohort_identity": arm.get("cohort_identity"),
            "activation_identity": activation.get("activation_identity"),
            "activation_event_watermark": activation.get(
                "activation_event_watermark"),
            "activated_at": activation.get("activated_at"),
            "warmup_session": activation.get("warmup_session"),
            "activation_phase": ("warmup" if warmup_session and
                                 session == warmup_session else
                                 "forward_diagnostic"),
        }
        return result

    @staticmethod
    def _stored_event_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            payload = (json.loads(event.get("event_json"))
                       if isinstance(event.get("event_json"), str)
                       else event.get("event_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        return dict(payload) if isinstance(payload, Mapping) else None

    @classmethod
    def _stored_event_session(cls, event: Mapping[str, Any]) -> str | None:
        payload = cls._stored_event_payload(event)
        if payload is None:
            return None
        stamp = _timestamp(payload.get("as_of") or payload.get("timestamp"))
        return (stamp.astimezone(NEW_YORK).date().isoformat()
                if stamp is not None else None)

    @staticmethod
    def _stored_event_cursor(event: Mapping[str, Any]) -> tuple[float, str]:
        inserted = _finite(event.get("inserted_at"))
        if inserted is None:
            raise ShadowError("diagnostic event insertion cursor is invalid")
        return float(inserted), str(event.get("event_key") or "")

    def _diagnostic_snapshot(
            self, cohort: Mapping[str, Any] | None,
            arms: Sequence[Mapping[str, Any]],
            activation: Mapping[str, Any] | None
            ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
        if cohort is None or activation is None or not arms:
            return [], {}, {"count": 0, "events_digest": _digest([])}
        candidate_ids = [str(arm.get("candidate_id") or "") for arm in arms]
        progress = self.store.diagnostic_progress(
            str(cohort.get("cohort_identity") or ""), candidate_ids, activation)
        cursors = [
            (float(item["last_inserted_at"]), str(item["last_event_key"]))
            for item in progress.values()
        ]
        minimum = min(cursors) if cursors else (0.0, "")
        rows = self.store.events_after_cursor(
            inserted_at=minimum[0], event_key=minimum[1],
            max_events=self.config.max_events)
        watermark = {
            "count": len(rows),
            "events_digest": _digest([{
                "event_key": row.get("event_key"),
                "digest": row.get("digest"),
                "inserted_at": row.get("inserted_at"),
                "source_path": row.get("source_path"),
                "source_offset_start": row.get("source_offset_start"),
                "source_offset_end": row.get("source_offset_end"),
            } for row in rows]),
            "minimum_cursor": {"inserted_at": minimum[0],
                               "event_key": minimum[1]},
            "last_cursor": ({
                "inserted_at": self._stored_event_cursor(rows[-1])[0],
                "event_key": self._stored_event_cursor(rows[-1])[1],
            } if rows else None),
        }
        return rows, progress, watermark

    def _diagnostic_session_complete(
            self, arm: Mapping[str, Any], session: str,
            bars: Sequence[Mapping[str, Any]]) -> bool:
        cfg = self._shadow_candidate_config(_safe_config(arm))
        session_cfg = cfg.get("session") if isinstance(
            cfg.get("session"), Mapping) else {}
        close, _source = _session_close(
            self.config.corpus_path, session,
            require_exact_calendar=bool(session_cfg.get(
                "require_exact_calendar", False)))
        return bool(close is not None and any(
            (_event_end(row) or datetime.min.replace(tzinfo=UTC)) >= close
            for row in bars))

    def _run_diagnostic_poll(
            self, cohort: Mapping[str, Any] | None,
            arms: Sequence[Mapping[str, Any]],
            activation: Mapping[str, Any] | None, *,
            snapshot_rows: Sequence[Mapping[str, Any]],
            progress: Mapping[str, Mapping[str, Any]],
            replay_identity: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate sparse forward diagnostics independently of gate progress."""
        if cohort is None or activation is None or not arms:
            return {"this_poll_decisions": 0, "provenance_rejections": 0,
                    "candidate_errors": {}}
        cohort_identity = str(cohort.get("cohort_identity") or "")
        candidate_ids = [str(arm.get("candidate_id") or "") for arm in arms]
        source_modes: dict[str, str | None] = {}
        snapshot_rows = tuple(
            _diagnostic_source_projection(row, source_modes)
            for row in snapshot_rows)
        sessions = {
            session for row in snapshot_rows
            if (session := self._stored_event_session(row)) is not None
        }
        for item in progress.values():
            sessions.update(str(day) for day in
                            (item.get("pending_sessions") or ()) if str(day))

        context_rows: list[dict[str, Any]] = []
        if sessions:
            context_rows = self.store.events_for_sessions(
                sorted(sessions),
                max_events=self.config.diagnostic_session_max_events)
            context_rows = [
                _diagnostic_source_projection(row, source_modes)
                for row in context_rows]
        eligible_context = [
            row for row in context_rows
            if self._diagnostic_event_eligible(row, activation)
        ]
        context_bars, context_quotes, context_options = self._group_event_rows(
            eligible_context)

        def payload_session(row: Mapping[str, Any]) -> str | None:
            payload = self._stored_event_payload(row)
            if payload is None:
                return None
            stamp = _timestamp(payload.get("as_of") or payload.get("timestamp"))
            return (stamp.astimezone(NEW_YORK).date().isoformat()
                    if stamp is not None else None)

        session_inputs: dict[str, tuple[tuple[dict, ...], tuple[dict, ...],
                                             tuple[dict, ...]]] = {}
        for session in sorted(sessions):
            session_inputs[session] = (
                tuple(row for values in context_bars.values() for row in values
                      if (_timestamp(row.get("as_of") or row.get("timestamp")) or
                          datetime.min.replace(tzinfo=UTC)).astimezone(
                              NEW_YORK).date().isoformat() == session),
                tuple(row for values in context_quotes.values() for row in values
                      if (_timestamp(row.get("as_of") or row.get("timestamp")) or
                          datetime.min.replace(tzinfo=UTC)).astimezone(
                              NEW_YORK).date().isoformat() == session),
                tuple(row for values in context_options.values() for row in values
                      if (_timestamp(row.get("as_of") or row.get("timestamp")) or
                          datetime.min.replace(tzinfo=UTC)).astimezone(
                              NEW_YORK).date().isoformat() == session),
            )

        work: dict[str, dict[str, Any]] = {}
        provenance_rejections = 0
        for arm in arms:
            candidate_id = str(arm.get("candidate_id") or "")
            arm_progress = progress[candidate_id]
            cursor = (float(arm_progress.get("last_inserted_at") or 0.0),
                      str(arm_progress.get("last_event_key") or ""))
            arm_rows = [row for row in snapshot_rows
                        if self._stored_event_cursor(row) > cursor]
            rollups: dict[str, dict[str, int]] = {}
            session_events: dict[str, list[dict[str, Any]]] = {}
            rejected = 0
            for row in arm_rows:
                session = payload_session(row) or "unknown"
                ok, reason = self._diagnostic_event_provenance(row, activation)
                if not ok:
                    key = f"provenance_reject:{reason or 'unknown'}"
                    counts = rollups.setdefault(session, {})
                    counts[key] = int(counts.get(key, 0)) + 1
                    rejected += 1
                    continue
                payload = self._stored_event_payload(row)
                if payload is None or session == "unknown":
                    counts = rollups.setdefault(session, {})
                    reject_key = ("provenance_reject:invalid_event_payload"
                                  if payload is None else
                                  "provenance_reject:invalid_session")
                    counts[reject_key] = int(counts.get(reject_key, 0)) + 1
                    rejected += 1
                    continue
                if str(row.get("event_type") or "") not in {
                        "bar", "bar_1m", "quote"}:
                    counts = rollups.setdefault(session, {})
                    counts["context_event"] = int(counts.get(
                        "context_event", 0)) + 1
                    continue
                if str(row.get("event_type") or "") == "quote":
                    counts = rollups.setdefault(session, {})
                    counts["context_event"] = int(counts.get(
                        "context_event", 0)) + 1
                session_events.setdefault(session, []).append(payload)
            for values in session_events.values():
                values.sort(key=lambda row: (
                    (_availability_time(row) or
                     datetime.min.replace(tzinfo=UTC)).isoformat(),
                    str(row.get("event_key") or "")))
            close_sessions = [
                session for session in session_events
                if session != "unknown" and session in session_inputs and
                self._diagnostic_session_complete(
                    arm, session, session_inputs[session][0])
            ]
            work[candidate_id] = {
                "arm": arm, "rows": arm_rows, "rollups": rollups,
                "session_events": session_events,
                "close_sessions": close_sessions, "rejected": rejected,
            }
            provenance_rejections += rejected

        worker_results: dict[str, dict[str, Any]] = {}
        candidate_errors: dict[str, str] = {}
        active = [item for item in work.values() if item["session_events"]]
        if active:
            with ThreadPoolExecutor(max_workers=self.config.max_workers,
                                    thread_name_prefix="diagnostic-shadow") as pool:
                futures = {}
                for item in active:
                    arm = item["arm"]
                    candidate_id = str(arm.get("candidate_id") or "")
                    initial = self.store.diagnostic_account_snapshot(
                        cohort_identity=cohort_identity,
                        candidate_id=candidate_id)
                    selected_inputs = {
                        session: session_inputs[session]
                        for session in item["session_events"]
                    }
                    futures[pool.submit(
                        self._evaluate_diagnostic_arm_snapshot, arm,
                        item["session_events"], selected_inputs,
                        context_bars, context_quotes, context_options,
                        initial, str(activation.get("warmup_session") or ""))
                    ] = candidate_id
                for future in as_completed(futures):
                    candidate_id = futures[future]
                    try:
                        worker_results[candidate_id] = future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        worker_results[candidate_id] = {
                            "candidate_id": candidate_id, "decisions": [],
                            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                        }

        this_poll_decisions = 0
        for candidate_id, item in work.items():
            rows = item["rows"]
            if not rows:
                continue
            result = worker_results.get(candidate_id, {
                "candidate_id": candidate_id, "decisions": [], "error": None})
            if result.get("error"):
                candidate_errors[candidate_id] = str(result["error"])
                continue
            arm = item["arm"]
            tagged: list[dict[str, Any]] = []
            for decision in result.get("decisions") or ():
                normalized = dict(decision)
                payload = normalized.get("payload")
                payload = dict(payload) if isinstance(payload, Mapping) else {}
                payload.setdefault("session_date", normalized.get("session_date"))
                normalized["payload"] = self._diagnostic_decision_payload(
                    arm, activation, payload)
                tagged.append(normalized)
                session_rollup = item["rollups"].setdefault(
                    str(normalized.get("session_date") or "unknown"), {})
                kind = str(normalized.get("kind") or "no_data")
                session_rollup[kind] = int(session_rollup.get(kind, 0)) + 1
                if kind in {"reject", "unpriced"}:
                    reason = str(normalized.get("reason") or "unspecified")
                    reason_key = (
                        f"{DIAGNOSTIC_REASON_ROLLUP_PREFIX}{kind}:{reason}")
                    session_rollup[reason_key] = int(
                        session_rollup.get(reason_key, 0)) + 1
            cursor = self._stored_event_cursor(rows[-1])
            this_poll_decisions += self.store.record_diagnostic_batch(
                cohort_identity=cohort_identity, candidate_id=candidate_id,
                cursor_inserted_at=cursor[0], cursor_event_key=cursor[1],
                processed_events=len(rows), rollups=item["rollups"],
                pending_sessions=item["close_sessions"], decisions=tagged,
                warmup_session=str(activation.get("warmup_session") or ""),
                max_decisions=self.config.max_decisions,
                account_batch=result.get("account_batch"))

        refreshed = self.store.diagnostic_progress(
            cohort_identity, candidate_ids, activation)
        arm_by_id = {str(arm.get("candidate_id") or ""): arm for arm in arms}
        for candidate_id in sorted(candidate_ids):
            arm = arm_by_id[candidate_id]
            for session in refreshed[candidate_id].get("pending_sessions") or ():
                inputs = session_inputs.get(str(session))
                if inputs is None or not self._diagnostic_session_complete(
                        arm, str(session), inputs[0]):
                    continue
                try:
                    complete = self._replay(
                        arm, str(session), inputs[0], inputs[1],
                        self.store.decisions(candidate_id), inputs[2],
                        replay_identity=replay_identity,
                        diagnostic_activation=activation)
                    if complete:
                        self.store.complete_diagnostic_replay(
                            cohort_identity=cohort_identity,
                            candidate_id=candidate_id,
                            session_date=str(session))
                except Exception as exc:
                    candidate_errors[candidate_id] = (
                        f"{type(exc).__name__}: {str(exc)[:240]}")
        return {
            "this_poll_decisions": int(this_poll_decisions),
            "provenance_rejections": int(provenance_rejections),
            "candidate_errors": dict(sorted(candidate_errors.items())),
        }

    def _diagnostic_coverage(
            self, cohort: Mapping[str, Any] | None,
            activation: Mapping[str, Any] | None, *,
            this_poll_decisions: int = 0,
            preactivation_rejections: int = 0,
            poll_duration_seconds: float = 0.0) -> dict[str, Any]:
        """Return bounded, explicitly non-authorizing cohort telemetry."""
        if cohort is None:
            return {
                "enabled": False, "diagnostic": False, "authorizing": False,
                "families_total": 12, "families_covered": 0,
                "families_missing": [], "baseline_count": 0,
                "variant_count": 0, "candidate_identities": [],
                "decision_counts": {"total": 0, "this_poll": 0, "by_kind": {}},
                "rejection_counts": {"reject": 0, "preactivation": 0},
                "quoteable_virtual_opens": 0, "unpriced_virtual_opens": 0,
                "replay_modeled_fills": 0, "actual_fills": 0,
                "forward_accounts": {
                    "schema": "diagnostic-forward-accounts-summary.v1",
                    "account_count": 0, "priced_account_count": 0,
                    "unpriced_account_count": 0, "open_positions": 0,
                    "closed_positions": 0, "orders": 0,
                    "modeled_fills": 0, "entry_fills": 0,
                    "exit_fills": 0, "late_data_gap_positions": 0,
                    "cash": 0.0, "equity": 0.0, "priced_equity": 0.0,
                    "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                    "priced_unrealized_pnl": 0.0,
                    "actual_fills": 0, "by_candidate": [],
                },
                "poll_duration_seconds": round(max(0.0, poll_duration_seconds), 6),
                "source_lag_seconds": None,
            }
        candidate_ids = {str(value) for value in
                         cohort.get("candidate_identities", ())}
        arms = [arm for arm in cohort.get("arms", ()) if isinstance(arm, Mapping)]
        covered = sorted({str(arm.get("family") or "") for arm in arms
                          if str(arm.get("family") or "")})
        expected = [str(value) for value in cohort.get("families", ())]
        decisions = [row for row in self.store.decisions()
                     if str(row.get("candidate_id") or "") in candidate_ids]
        progress = (self.store.diagnostic_progress(
            str(cohort.get("cohort_identity") or ""), sorted(candidate_ids),
            activation) if isinstance(activation, Mapping) else {})
        evaluated_by_kind: dict[str, int] = {}
        reason_counts: dict[str, dict[str, int]] = {
            "reject": {}, "unpriced": {}}
        processed_events = 0
        pending_sessions: set[str] = set()
        completed_sessions: set[str] = set()
        cursors: dict[str, dict[str, Any]] = {}
        for candidate_id, item in progress.items():
            processed_events += int(item.get("processed_events") or 0)
            pending_sessions.update(str(value) for value in
                                    (item.get("pending_sessions") or ()))
            rollups = item.get("rollups")
            if isinstance(rollups, Mapping):
                cumulative = rollups.get("cumulative")
                if isinstance(cumulative, Mapping):
                    for kind, count in cumulative.items():
                        evaluated_by_kind[str(kind)] = int(
                            evaluated_by_kind.get(str(kind), 0)) + int(count)
                sessions = rollups.get("sessions")
                if isinstance(sessions, Mapping):
                    for counts in sessions.values():
                        if not isinstance(counts, Mapping):
                            continue
                        for key, count in counts.items():
                            raw_key = str(key)
                            if not raw_key.startswith(
                                    DIAGNOSTIC_REASON_ROLLUP_PREFIX):
                                continue
                            kind, separator, reason = raw_key.removeprefix(
                                DIAGNOSTIC_REASON_ROLLUP_PREFIX).partition(":")
                            if separator and kind in reason_counts:
                                target = reason_counts[kind]
                                target[reason] = int(
                                    target.get(reason, 0)) + int(count)
                completed_sessions.update(str(value) for value in
                                          (rollups.get("completed_sessions") or ()))
            cursors[candidate_id] = {
                "last_inserted_at": item.get("last_inserted_at"),
                "last_event_key": item.get("last_event_key"),
                "processed_events": item.get("processed_events"),
            }
        family_by_candidate = {
            str(arm.get("candidate_id") or ""): str(arm.get("family") or "")
            for arm in arms}
        observed_candidate_ids = {
            str(row.get("candidate_id") or "") for row in decisions}
        observed_candidate_ids.update(
            candidate_id for candidate_id, item in progress.items()
            if int(item.get("processed_events") or 0) > 0)
        observed_families = sorted({
            family_by_candidate.get(candidate_id, "")
            for candidate_id in observed_candidate_ids
            if family_by_candidate.get(candidate_id, "")})
        by_kind: dict[str, int] = {}
        warmup_decisions = 0
        warmup_session = (str(activation.get("warmup_session"))
                          if isinstance(activation, Mapping) and
                          activation.get("warmup_session") else None)
        for row in decisions:
            kind = str(row.get("kind") or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if warmup_session and str(row.get("session_date") or "") == warmup_session:
                warmup_decisions += 1
        accounts = [row for row in self.store.replay_accounts()
                    if str(row.get("candidate_id") or "") in candidate_ids]
        forward_accounts = self.store.diagnostic_account_summary(
            cohort_identity=str(cohort.get("cohort_identity") or ""),
            candidate_ids=sorted(candidate_ids))
        modeled_fills = sum(int(row.get("trade_count") or 0) for row in accounts)
        warmup_modeled_fills = sum(
            int(row.get("trade_count") or 0) for row in accounts
            if warmup_session and str(row.get("session_date") or "") == warmup_session)
        watermark = self.store.event_watermark()
        latest = (_timestamp(watermark.get("last_available_at")) or
                  _timestamp(watermark.get("last_timestamp")))
        source_lag = (max(0.0, time.time() - latest.timestamp())
                      if latest is not None else None)
        quoteable = sum(
            1 for row in decisions
            if str(row.get("kind") or "") == "open_incomplete" and
            (not warmup_session or
             str(row.get("session_date") or "") > warmup_session))
        warmup_quoteable = sum(
            1 for row in decisions
            if str(row.get("kind") or "") == "open_incomplete" and
            warmup_session and
            str(row.get("session_date") or "") == warmup_session)
        unpriced = int(evaluated_by_kind.get("unpriced", 0))
        evaluated_decisions = sum(
            int(count) for kind, count in evaluated_by_kind.items()
            if kind != "context_event" and
            not str(kind).startswith("provenance_reject:"))
        if activation is None:
            observation_status = "awaiting_forward_activation"
        elif not decisions and evaluated_decisions <= 0:
            observation_status = "no_post_activation_events"
        elif quoteable:
            observation_status = "quoteable_virtual_observations"
        elif unpriced:
            observation_status = "signals_unpriced_no_virtual_fill_claim"
        elif warmup_quoteable:
            observation_status = "warmup_signals_not_evaluated"
        else:
            observation_status = "observed_no_quoteable_virtual_opens"
        activation_identity = (activation.get("activation_identity")
                               if isinstance(activation, Mapping) else None)
        activation_watermark = (activation.get("activation_event_watermark")
                                if isinstance(activation, Mapping) else None)
        preregistered_decision_events = (
            int(activation_watermark.get("decision_event_count") or 0)
            if isinstance(activation_watermark, Mapping) else 0)
        return {
            "schema": "diagnostic-shadow-coverage.v1",
            "enabled": True,
            "diagnostic": True,
            "authorizing": False,
            "gate_eligible": False,
            "promotion_eligible": False,
            "online_fdr": False,
            "families_total": int(cohort.get("families_total") or len(expected)),
            "families_covered": len(covered),
            "families_missing": sorted(set(expected) - set(covered)),
            "families_observed": len(observed_families),
            "families_without_decisions": sorted(
                set(expected) - set(observed_families)),
            "baseline_count": sum(1 for arm in arms if arm.get("role") == "baseline"),
            "variant_count": sum(1 for arm in arms if arm.get("role") == "variant"),
            "cohort_identity": cohort.get("cohort_identity"),
            "code_identity": cohort.get("code_identity"),
            "runtime_config_identity": cohort.get("runtime_config_identity"),
            "policy_config_identity": cohort.get("policy_config_identity"),
            "activation_identity": activation_identity,
            "activation_event_watermark": (
                dict(activation_watermark) if isinstance(
                    activation_watermark, Mapping) else None),
            "activation_status": ("active" if activation_identity else "preregistered"),
            "warmup_session": warmup_session,
            "candidate_identities": sorted(candidate_ids),
            "arms": [{
                "candidate_id": arm.get("candidate_id"),
                "family": arm.get("family"),
                "role": arm.get("role"),
                "variant_id": arm.get("variant_id"),
                "spec_identity": arm.get("spec_identity"),
                "config_identity": arm.get("config_identity"),
                "code_identity": arm.get("code_identity"),
                "cohort_identity": arm.get("cohort_identity"),
            } for arm in sorted(arms, key=lambda item: str(
                item.get("candidate_id") or ""))],
            "decision_counts": {
                "total": len(decisions),
                "this_poll": int(this_poll_decisions),
                "by_kind": dict(sorted(by_kind.items())),
                "warmup": warmup_decisions,
                "evaluated_by_kind": dict(sorted(evaluated_by_kind.items())),
                "compacted_no_trade": int(evaluated_by_kind.get(
                    "no_trade", 0)),
                "compacted_no_data": int(evaluated_by_kind.get(
                    "no_data", 0)),
                "evaluated_total": int(evaluated_decisions),
                "trace_representatives": int(
                    by_kind.get("reject", 0) + by_kind.get("unpriced", 0)),
            },
            "rejection_counts": {
                "reject": int(evaluated_by_kind.get("reject", 0)),
                "unpriced": unpriced,
                "by_reason": dict(sorted(reason_counts["reject"].items())),
                "unpriced_by_reason": dict(sorted(
                    reason_counts["unpriced"].items())),
                "preactivation": preregistered_decision_events * len(candidate_ids),
                "preactivation_this_poll": int(preactivation_rejections),
                "forward_provenance": sum(
                    int(count) for kind, count in evaluated_by_kind.items()
                    if str(kind).startswith("provenance_reject:")),
            },
            "processed_events": int(processed_events),
            "processed_event_cursors": cursors,
            "pending_replay_sessions": sorted(pending_sessions),
            "completed_replay_sessions": sorted(completed_sessions),
            "quoteable_virtual_opens": quoteable,
            "warmup_quoteable_signals": warmup_quoteable,
            "unpriced_virtual_opens": unpriced,
            "replay_modeled_fills": modeled_fills,
            "warmup_replay_modeled_fills": warmup_modeled_fills,
            "forward_accounts": forward_accounts,
            "forward_modeled_fills": int(
                forward_accounts.get("modeled_fills") or 0),
            "actual_fills": 0,
            "actual_fill_claims": False,
            "realized_pnl_authorizing": False,
            "observation_status": observation_status,
            "poll_duration_seconds": round(max(0.0, poll_duration_seconds), 6),
            "source_lag_seconds": (round(source_lag, 6)
                                   if source_lag is not None else None),
            "config_source": self.config.runtime_config_path,
            "incremental_max_events": int(self.config.max_events),
            "session_context_max_events": int(
                self.config.diagnostic_session_max_events),
        }

    def _shadow_policy(self, config: Mapping[str, Any]) -> ReplayPolicy:
        """Resolve a candidate policy with the optional shadow-only overlay."""
        base = _policy(config)
        if not self.config.stress_calibration_enabled:
            return base
        artifact = self.config.stress_calibration_artifact
        valid, reason = verify_stress_calibration_artifact(
            artifact, expected_provider=base.equity_provider,
            expected_feed=base.equity_feed)
        if not valid:
            raise ShadowError(
                f"shadow stress calibration artifact invalid: {reason or 'artifact_invalid'}")
        return replace(
            base,
            stressed_cost_calibration_enabled=True,
            stressed_cost_calibration_path=self.config.stress_calibration_path,
            stressed_cost_calibration_artifact=artifact,
        )

    def _stress_telemetry(self, policy: ReplayPolicy, *,
                          symbol: str | None = None,
                          timestamp: Any = None,
                          vehicle: str = "equity") -> dict[str, Any]:
        scenario, reason = policy.resolve_stress_scenario(
            symbol, timestamp, vehicle=vehicle)
        artifact = policy.stressed_cost_calibration_artifact
        return {
            "enabled": bool(policy.stressed_cost_calibration_enabled),
            "effective_scenario_bps": scenario,
            "activation_reason": reason,
            "source": ("shadow_override" if self.config.stress_calibration_enabled
                        else ("candidate_policy" if policy.stressed_cost_calibration_enabled
                              else "disabled")),
            "path": (self.config.stress_calibration_path
                     if self.config.stress_calibration_enabled else
                     policy.stressed_cost_calibration_path),
            "artifact_content_hash": (
                artifact.get("content_hash") if isinstance(artifact, Mapping)
                else None),
        }

    def _calibration_status(self) -> dict[str, Any]:
        """Return bounded operator-facing status for the shadow overlay."""
        artifact = self.config.stress_calibration_artifact
        scenarios = sorted({float(cell.get("selected_scenario_bps"))
                            for cell in (artifact.get("cells", ())
                                         if isinstance(artifact, Mapping) else ())
                            if isinstance(cell, Mapping) and
                            _finite(cell.get("selected_scenario_bps")) is not None})
        return {
            "enabled": bool(self.config.stress_calibration_enabled),
            "source": ("shadow_override" if self.config.stress_calibration_enabled
                        else "disabled"),
            "path": self.config.stress_calibration_path,
            "artifact_content_hash": (
                artifact.get("content_hash") if isinstance(artifact, Mapping)
                else None),
            "validation": "valid" if self.config.stress_calibration_enabled else "disabled",
            "effective_scenario_bps": scenarios[0] if len(scenarios) == 1 else None,
            "effective_scenarios_bps": scenarios[:16],
        }

    def _shadow_candidate_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Copy candidate config and apply only the shadow stress overlay."""
        if not self.config.stress_calibration_enabled:
            return dict(config)
        result = dict(config)
        risk = dict(config.get("risk") or {}) if isinstance(
            config.get("risk"), Mapping) else {}
        risk["stressed_cost_calibration_enabled"] = True
        risk["stressed_cost_calibration_path"] = self.config.stress_calibration_path
        result["risk"] = risk
        return result

    def _rule_root_control(self, candidate: Mapping[str, Any]) -> dict[str, Any] | None:
        """Build the exact-window root control for a tuned rule candidate.

        Factory hypotheses are the authority for a slot's root rule.  A
        descendant is therefore compared with a synthetic control namespace
        tied to its own candidate id; this keeps the control replay isolated
        from any EdgeLedger lifecycle state and prevents a candidate from
        accidentally selecting its own mutated spec as its baseline.
        """
        if str(candidate.get("strategy_id")) != "rule":
            return None
        axes = candidate.get("axes")
        if axes is None and isinstance(candidate.get("axes_json"), str):
            try:
                axes = json.loads(str(candidate["axes_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                axes = None
        hypothesis_id = axes.get("hypothesis_id") if isinstance(axes, Mapping) else None
        root = self._factory_roots.get(str(hypothesis_id)) if hypothesis_id else None
        if not isinstance(root, Mapping) or root.get("vehicle") != str(candidate.get("vehicle")):
            return None
        root_variant = str(root.get("variant_id") or "")
        if not root_variant or str(candidate.get("variant_id")) == root_variant:
            # The root hypothesis is itself the control target.  Its paired
            # baseline is the randomized-entry null generated by _replay.
            return None
        config = _safe_config(candidate)
        strategy = dict(config.get("strategy") or {})
        strategy.update({"id": "rule", "variant_id": root_variant,
                         "rule_spec": dict(root["rule_spec"])})
        config["strategy"] = strategy
        return {**dict(candidate),
                "candidate_id": f"shadow:baseline:{candidate['candidate_id']}",
                "variant_id": root_variant,
                "config": config,
                "axes": {"hypothesis_id": hypothesis_id, "role": "paired_root_control"}}

    @staticmethod
    def _group_event_rows(event_rows: Sequence[Mapping[str, Any]]) -> tuple[
            dict[str, list[dict]], dict[str, list[dict]], dict[str, list[dict]]]:
        bars: dict[str, list[dict]] = {}
        quotes: dict[str, list[dict]] = {}
        options: dict[str, list[dict]] = {}
        for row in event_rows:
            try:
                payload = json.loads(row["event_json"])
                _, event = _normalize_row(payload)
            except (TypeError, ValueError, json.JSONDecodeError, NormalizationError):
                continue
            plain = payload
            symbol = str(row["symbol"])
            if row["event_type"] in {"bar", "bar_1m"}:
                bars.setdefault(symbol, []).append(plain)
            elif row["event_type"] == "quote":
                quotes.setdefault(symbol, []).append(plain)
            elif row["event_type"] in {"option", "option_snapshot"}:
                underlying = str(plain.get("underlying") or "")
                options.setdefault(underlying, []).append(plain)
        for values in (bars, quotes, options):
            for key in values:
                values[key].sort(key=lambda row: str(row.get("timestamp") or ""))
        return bars, quotes, options

    def _load_events(self) -> tuple[list[dict], dict[str, list[dict]], dict[str, list[dict]], dict[str, list[dict]]]:
        floor = self.store.forward_event_floor() or 0.0
        event_rows = self.store.events(inserted_after=floor)
        bars, quotes, options = self._group_event_rows(event_rows)
        return event_rows, bars, quotes, options

    def _load_events_for_sessions(self, sessions: Sequence[str]) -> tuple[
            list[dict], dict[str, list[dict]], dict[str, list[dict]], dict[str, list[dict]]]:
        event_rows = self.store.events_for_sessions(
            sessions, max_events=self.config.max_events)
        bars, quotes, options = self._group_event_rows(event_rows)
        return event_rows, bars, quotes, options

    @staticmethod
    def _latest_quote(rows: Sequence[Mapping], at: datetime, *,
                      expected_feed: str | None = None,
                      expected_provider: str | None = None) -> Mapping | None:
        valid = []
        for row in rows:
            if (expected_feed is not None and
                    _canonical_equity_feed(row.get("feed")) != expected_feed):
                continue
            if (expected_provider is not None and
                    _canonical_equity_provider(row.get("provider")) !=
                    expected_provider):
                continue
            stamp = _timestamp(row.get("timestamp"))
            if stamp is not None and stamp <= at and _row_visible(row, at):
                bid, ask = _finite(row.get("bid")), _finite(row.get("ask"))
                if bid is not None and ask is not None and bid > 0 and ask >= bid:
                    valid.append((stamp, row))
        return max(valid, key=lambda pair: pair[0])[1] if valid else None

    def _evaluate(self, candidate: Mapping[str, Any], event: Mapping[str, Any],
                  bars: Mapping[str, list], quotes: Mapping[str, list],
                  options: Mapping[str, list]) -> tuple[str, str | None, dict, dict | None]:
        symbol = str(event.get("symbol") or "")
        diagnostic_candidate = is_diagnostic_candidate(candidate)
        cfg = self._shadow_candidate_config(_safe_config(candidate))
        strategy = cfg.get("strategy", {})
        event_at = _availability_time(event)
        if event_at is None:
            return "no_data", "event timestamp unavailable", {}, None
        market_at = _timestamp(event.get("timestamp") or event.get("as_of"))
        if market_at is None:
            return "no_data", "event market timestamp unavailable", {}, None
        session = market_at.astimezone(NEW_YORK).date().isoformat()
        session_cfg = cfg.get("session") if isinstance(cfg.get("session"), Mapping) else {}
        require_exact_calendar = bool(session_cfg.get("require_exact_calendar", False))
        close_at, calendar_source = _session_close(
            self.config.corpus_path, session,
            require_exact_calendar=require_exact_calendar)
        if require_exact_calendar and close_at is None:
            return ("no_data", "exact broker calendar metadata unavailable",
                    {"session_date": session,
                     "calendar_source": calendar_source}, None)
        policy = _session_policy(
            cfg, close_at, policy=self._shadow_policy(cfg))
        expected_equity_feed = policy.equity_feed
        expected_equity_provider = policy.equity_provider
        observed_equity_provider = _canonical_equity_provider(event.get("provider"))
        if observed_equity_provider != expected_equity_provider:
            return ("no_data", "equity provider mismatch",
                    {"session_date": session,
                     "equity_provider": expected_equity_provider,
                     "observed_equity_provider": observed_equity_provider}, None)
        observed_equity_feed = _canonical_equity_feed(event.get("feed"))
        if observed_equity_feed != expected_equity_feed:
            return ("no_data", "equity feed mismatch",
                    {"session_date": session,
                     "equity_feed": expected_equity_feed,
                     "observed_equity_feed": observed_equity_feed}, None)
        observed_source_mode = str(
            event.get("source_mode") or "forward_observed").strip().lower()
        if diagnostic_candidate and observed_source_mode != "forward_observed":
            return ("no_data", "diagnostic source mode is not forward observed",
                    {"session_date": session,
                     "source_mode": observed_source_mode}, None)
        calendar_bounds = _recorded_session_bounds(
            self.config.corpus_path, session)
        if close_at is not None:
            local_day = market_at.astimezone(NEW_YORK).date()
            latest_at = (datetime.combine(local_day, policy.latest_entry_time,
                                          tzinfo=NEW_YORK).astimezone(UTC)
                         if policy.latest_entry_time is not None else close_at)
            if event_at >= close_at or event_at >= latest_at:
                return ("no_trade", "session entry cutoff reached",
                        {"session_date": session,
                         "session_close": close_at.isoformat(),
                         "calendar_source": calendar_source}, None)
        stream = [row for row in bars.get(symbol, [])
                  if _canonical_equity_feed(row.get("feed")) == expected_equity_feed
                  and (_canonical_equity_provider(row.get("provider")) ==
                       expected_equity_provider)
                  and (not diagnostic_candidate or str(
                      row.get("source_mode") or "forward_observed").strip().lower()
                      == "forward_observed")
                  and _row_visible(row, event_at)
                  and (close_at is None or (
                      (calendar_bounds is None or
                       (_timestamp(row.get("timestamp")) or close_at) >= calendar_bounds[0])
                      and (_timestamp(row.get("timestamp")) or close_at) < close_at
                      and (_event_end(row) or close_at) <= close_at))]
        if len(stream) < 2:
            return "no_data", "insufficient bars", {"session_date": session}, None
        strategy_id = str(candidate.get("strategy_id") or strategy.get("id") or "ibr")
        rule_context = None
        rule_spec = None
        if strategy_id == "rule":
            raw_spec = strategy.get("rule_spec") if isinstance(strategy, Mapping) else None
            try:
                rule_spec = validate_rule_spec(raw_spec or {})
            except (TypeError, ValueError) as exc:
                return "reject", "invalid rule specification", {
                    "session_date": session, "error": str(exc)[:240]}, None
            if (rule_spec["family"] == "cross_sectional_residual" and
                    not rule_vehicle_executable(
                        rule_spec, str(candidate.get("vehicle") or "equity"))):
                return ("reject", "cross_sectional_requires_equity_shares",
                        {"session_date": session,
                         "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK}, None)
            if rule_spec["family"] == "cross_sectional_residual":
                # Relative signals may consume only completed SPY bars that
                # were observable at the same decision instant as the subject.
                stream = [row for row in stream
                          if (_event_end(row) or event_at) <= event_at]
                benchmark = tuple(
                    row for row in bars.get(CROSS_SECTIONAL_BENCHMARK, ())
                    if (_canonical_equity_feed(row.get("feed")) ==
                        expected_equity_feed)
                    and (_canonical_equity_provider(row.get("provider")) ==
                         expected_equity_provider)
                    and (not diagnostic_candidate or str(
                        row.get("source_mode") or "forward_observed").strip().lower()
                        == "forward_observed")
                    and _row_visible(row, event_at)
                    and (_event_end(row) or event_at) <= event_at
                    and ((_timestamp(row.get("timestamp")) or market_at)
                         .astimezone(NEW_YORK).date().isoformat() == session)
                )
                rule_context = MappingProxyType({
                    CROSS_SECTIONAL_BENCHMARK: benchmark,
                })
        try:
            if strategy_id == "rule":
                signal = (generate_rule_signal(
                              symbol, stream, config=cfg, now=event_at)
                          if rule_context is None else
                          generate_rule_signal(
                              symbol, stream, config=cfg, now=event_at,
                              bars_by_symbol=rule_context))
            else:
                signal = generate_ibr_signal(
                    symbol, stream, config=cfg, now=event_at)
        except Exception as exc:
            return "reject", f"signal exception: {type(exc).__name__}", {"error": str(exc)[:240]}, None
        base = {"session_date": session, "strategy_id": strategy_id,
                "equity_feed": expected_equity_feed,
                "variant_id": candidate.get("variant_id"), "signal": signal,
                "stress_calibration": self._stress_telemetry(
                    policy, symbol=symbol, timestamp=event_at,
                    vehicle=str(candidate.get("vehicle") or "equity"))}
        if diagnostic_candidate:
            base["equity_provider"] = expected_equity_provider
        if signal is None:
            if (rule_spec is not None and
                    rule_spec["family"] == "cross_sectional_residual"):
                trace = evaluate_rule_signal_trace(
                    stream, rule_spec, bars_by_symbol=rule_context,
                    symbol=symbol)
                stages = trace.get("stages") or []
                trace_reason = (str(stages[-1].get("reason") or "")
                                if stages else "")
                base["signal_trace"] = trace
                if trace_reason.startswith(("benchmark_context_",
                                            "subject_context_")):
                    return "no_data", trace_reason, base, None
            return "no_trade", "no signal", base, None
        # Runtime decisions are made when the completed feature prefix is
        # actually observed.  Persist that causal instant and use it as the
        # entry boundary; replay compares these fields rather than deriving a
        # synthetic next-bar timestamp from the market event alone.
        signal = dict(signal)
        signal["decision_timestamp"] = event_at.isoformat()
        signal["entry_timestamp"] = event_at.isoformat()
        base["signal"] = signal
        quote_rows = quotes.get(symbol, ())
        if diagnostic_candidate:
            quote_rows = tuple(
                row for row in quote_rows
                if str(row.get("source_mode") or "forward_observed")
                .strip().lower() == "forward_observed")
        quote = self._latest_quote(
            quote_rows, event_at,
            expected_feed=expected_equity_feed,
            expected_provider=expected_equity_provider)
        snap: dict[str, Any] = {"price": _finite(event.get("close")) or _finite(event.get("open")),
                                "close": _finite(event.get("close")),
                                "spread_bps": None, "stale": True, "quote_stale": True,
                                "session": session, "signal_ts": signal.get("signal_ts"),
                                "equity_feed": expected_equity_feed}
        if diagnostic_candidate:
            snap["equity_provider"] = expected_equity_provider
        if quote is not None:
            quote_at = _timestamp(quote.get("timestamp"))
            bid, ask = _finite(quote.get("bid")), _finite(quote.get("ask"))
            age = None if quote_at is None else max(0.0, (event_at - quote_at).total_seconds())
            if bid and ask and bid > 0 and ask >= bid:
                snap.update(price=(bid + ask) / 2,
                            spread_bps=(ask - bid) / ((bid + ask) / 2) * 10_000,
                            quote_ts=quote_at.isoformat() if quote_at else None,
                            quote_age_seconds=age,
                            stale=bool(age is None or age > policy.max_market_data_age_seconds),
                            quote_stale=bool(age is None or age > policy.max_market_data_age_seconds))
                if diagnostic_candidate:
                    snap.update(
                        bid=bid, ask=ask, quote_as_of=quote.get("as_of"),
                        quote_observed_at=quote.get("observed_at"),
                        quote_feed=_canonical_equity_feed(quote.get("feed")),
                        quote_provider=_canonical_equity_provider(
                            quote.get("provider")),
                        quote_source_mode=str(
                            quote.get("source_mode") or
                            "forward_observed").strip().lower())
        # The setup primitive consumes the exact signal geometry while the
        # quote fields above carry strict point-in-time freshness metadata.
        snap.update({key: value for key, value in signal.items()
                     if key not in {"symbol", "action"} and value is not None})
        if (diagnostic_candidate and
                str(candidate.get("vehicle") or "equity") == "equity" and
                _finite(snap.get("bid")) is not None and
                _finite(snap.get("ask")) is not None):
            # A diagnostic requested entry is priced from the executable side,
            # never the midpoint.  Both sides remain in the immutable snapshot
            # for later liquidation and spread audits.
            snap["price"] = (float(snap["ask"])
                             if str(signal.get("direction") or "") == "long"
                             else float(snap["bid"]))
            snap["entry_price"] = snap["price"]
        else:
            snap["price"] = snap.get("price") or _finite(signal.get("entry_price"))
            snap["entry_price"] = (_finite(signal.get("entry_price")) or
                                   snap.get("price"))
        if signal.get("range_high") is not None and signal.get("range_low") is not None:
            snap["ibr_range"] = {"high": signal.get("range_high"),
                                  "low": signal.get("range_low"),
                                  "width": signal.get("range_width"),
                                  "complete": True}
        if str(candidate.get("vehicle") or "equity") == "option":
            option_rows = []
            for row in options.get(symbol, ()):
                stamp = _timestamp(row.get("timestamp"))
                if (stamp is not None and stamp <= event_at and
                        _row_visible(row, event_at) and
                        str(row.get("feed") or "").strip().lower() == "opra"):
                    item = dict(row)
                    item.setdefault("quote_ts", stamp.isoformat())
                    age = max(0.0, (event_at - stamp).total_seconds())
                    item.setdefault("quote_age_seconds", age)
                    option_rows.append(item)
            if not option_rows:
                return ("unpriced", "executable OPRA option chain unavailable",
                        base | {"snapshot": snap}, None)
            snap["option_chain"] = option_rows
        if snap["stale"] or snap["quote_stale"]:
            return "unpriced", "stale or unavailable quote", base | {"snapshot": snap}, None
        try:
            plan, why = build_setup_plan(signal, snap, cfg)
        except Exception as exc:
            return "reject", f"setup exception: {type(exc).__name__}", base, None
        if plan is None:
            return "reject", why or "setup rejected", base | {"snapshot": snap}, None
        plan = dict(plan)
        plan["decision_timestamp"] = event_at.isoformat()
        plan["entry_timestamp"] = event_at.isoformat()
        plan["equity_feed"] = expected_equity_feed
        # A candidate's virtual book is isolated and feeds only that
        # candidate's portfolio admission.  Plans are immutable observations;
        # no fills, mark-to-market, or fabricated P&L is introduced here.
        try:
            positions, active_trades, gross_notional = self._portfolio_state(
                str(candidate["candidate_id"]))
        except ShadowError as exc:
            return "reject", f"portfolio state unavailable: {exc}", base, None
        risk = RiskEngine(cfg)
        try:
            equity_overrides = getattr(self._worker_state, "equities", None)
            current_equity = (
                equity_overrides.get(str(candidate["candidate_id"]))
                if isinstance(equity_overrides, Mapping) else
                float(self.config.equity))
            if _finite(current_equity) is None or float(current_equity) <= 0:
                return ("reject", "persistent account equity unavailable",
                        base | {"snapshot": snap}, None)
            risk_plan, why = risk.vet_open(
                plan, float(current_equity), positions, {symbol: snap}, {},
                gross_notional, active_trades=active_trades,
                now=event_at.timestamp())
        except Exception as exc:
            return "reject", f"risk exception: {type(exc).__name__}", base, None
        if risk_plan is None:
            return "reject", why or "risk rejected", base | {"snapshot": snap}, None
        return "open_incomplete", "virtual open; fills and P&L incomplete", base | {
            "snapshot": snap, "setup_plan": plan, "risk_plan": risk_plan,
        }, risk_plan

    def _portfolio_state(self, candidate_id: str) -> tuple[list[dict], dict[str, dict], float]:
        """Build risk admission state from one candidate's open books."""
        overrides = getattr(self._worker_state, "portfolios", None)
        if isinstance(overrides, Mapping) and candidate_id in overrides:
            positions, active_trades, gross_notional = overrides[candidate_id]
            return (list(positions), dict(active_trades), float(gross_notional))
        positions: list[dict] = []
        active_trades: dict[str, dict] = {}
        gross_notional = 0.0
        for row in self.store.open_books(candidate_id):
            try:
                plan = json.loads(row.get("plan_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ShadowError("open book plan is invalid") from exc
            if not isinstance(plan, Mapping):
                raise ShadowError("open book plan is invalid")
            symbol = str(row.get("symbol") or plan.get("symbol") or "")
            if not symbol:
                raise ShadowError("open book symbol is unavailable")
            risk_usd = _finite(plan.get("risk_usd"))
            notional = _finite(plan.get("notional"))
            if risk_usd is None or risk_usd < 0:
                raise ShadowError(f"open book risk is unavailable for {symbol}")
            if notional is None or notional < 0:
                raise ShadowError(f"open book notional is unavailable for {symbol}")
            position = dict(plan)
            position["symbol"] = symbol
            if row.get("quantity") is not None:
                position.setdefault("quantity", row["quantity"])
            if row.get("entry_price") is not None:
                position.setdefault("entry_price", row["entry_price"])
            positions.append(position)
            active_trade = dict(position)
            active_trade["risk_usd"] = risk_usd
            active_trades[symbol] = active_trade
            gross_notional += notional
        return positions, active_trades, gross_notional

    def _replay(self, candidate: Mapping[str, Any], session: str,
                session_bars: Sequence[Mapping], session_quotes: Sequence[Mapping],
                decisions: Sequence[Mapping],
                session_options: Sequence[Mapping] | None = None, *,
                replay_identity: Mapping[str, Any] | None = None,
                diagnostic_activation: Mapping[str, Any] | None = None) -> bool:
        candidate_id = str(candidate["candidate_id"])
        diagnostic = is_diagnostic_candidate(candidate)
        replay_identity = dict(replay_identity or {})
        identity_details = {
            "replay_code_hash": replay_identity.get("replay_code_hash"),
            "manifest_digest": replay_identity.get("manifest_digest"),
            "candidate_set_digest": replay_identity.get("candidate_set_digest"),
            "replay_epoch_identity": replay_identity.get(
                "replay_epoch_identity"),
            "candidate_epoch_identity": (
                candidate.get("cohort_identity") or
                _candidate_epoch_identity(
                    candidate_id=candidate_id,
                    config=_safe_config(candidate),
                    replay_identity=replay_identity)),
        }
        # Option replay is also point-in-time input.  Include it in the source
        # digest so a corrected/stale option snapshot cannot be mistaken for
        # the same replay window merely because the underlying bars/quotes
        # were unchanged.
        cfg = self._shadow_candidate_config(_safe_config(candidate))
        session_cfg = cfg.get("session") if isinstance(cfg.get("session"), Mapping) else {}
        require_exact_calendar = bool(session_cfg.get("require_exact_calendar", False))
        calendar_close, calendar_source = _session_close(
            self.config.corpus_path, session,
            require_exact_calendar=require_exact_calendar)
        if require_exact_calendar and calendar_close is None:
            session_bars = ()
            session_quotes = ()
            session_options = ()
        in_session_bars = [row for row in session_bars
                           if calendar_close is None or (
                               (_timestamp(row.get("timestamp")) or calendar_close) < calendar_close
                               and (_event_end(row) or calendar_close) <= calendar_close)]
        in_session_quotes = [row for row in session_quotes
                             if calendar_close is None or
                             (_timestamp(row.get("timestamp")) or calendar_close) <= calendar_close]
        in_session_options = [row for row in (session_options or ())
                              if calendar_close is None or
                              (_timestamp(row.get("timestamp")) or calendar_close) <= calendar_close]
        replay_policy = _session_policy(
            cfg, calendar_close, policy=self._shadow_policy(cfg))
        expected_equity_feed = replay_policy.equity_feed
        expected_equity_provider = replay_policy.equity_provider
        candidate_vehicle = str(candidate.get("vehicle") or "equity")
        # Resolve the candidate's immutable measured/static economics once for
        # this session.  Every arm below receives the same resolver, while a
        # tampered/foreign/sparse schedule raises and leaves the session
        # incomplete rather than silently switching economics.
        cost_setup = cost_resolver_setup(cfg, vehicle=candidate_vehicle)
        feed_mismatches = [
            {"kind": kind,
             "symbol": str(row.get("symbol") or ""),
             "timestamp": str(row.get("timestamp") or ""),
             "observed_feed": _canonical_equity_feed(row.get("feed"))}
            for kind, rows in (("bar", in_session_bars),
                               ("quote", in_session_quotes))
            for row in rows
            if _canonical_equity_feed(row.get("feed")) != expected_equity_feed
        ]
        provider_mismatches, provider_mismatch_count = _provider_mismatch_telemetry(
            (("bar", in_session_bars), ("quote", in_session_quotes),
             ("option", in_session_options)),
            expected_provider=expected_equity_provider)
        source_digest = _digest({"bars": in_session_bars,
                                 "quotes": in_session_quotes,
                                 "options": in_session_options,
                                 "equity_feed": expected_equity_feed,
                                 "equity_provider": expected_equity_provider,
                                 "session_close": (calendar_close.isoformat()
                                                   if calendar_close else None),
                                 "calendar_source": calendar_source})
        shadow_signatures = []
        for row in decisions:
            if row.get("session_date") != session:
                continue
            signature = _shadow_signature(row)
            if signature is not None:
                shadow_signatures.append(signature)
        shadow_signatures.sort(key=lambda row: _json(row))
        shadow_digest = _digest(shadow_signatures)
        replay_digest = None
        replay_ok = False
        replay_signatures: list[dict[str, Any]] = []
        evidence_rows: list[dict[str, Any]] = []
        null_rows: list[dict[str, Any]] = []
        null_account: dict[str, Any] = {}
        starting_cash = float(self.config.equity)
        ending_cash = starting_cash
        realized_pnl = 0.0
        details: dict[str, Any] = {"complete": False, "trade_count": 0,
                                   "equity_feed": expected_equity_feed,
                                   "equity_provider": expected_equity_provider,
                                   "feed_mismatches": feed_mismatches,
                                   "provider_mismatches": provider_mismatches,
                                   "provider_mismatch_count": provider_mismatch_count,
                                   "session_close": (calendar_close.isoformat()
                                                     if calendar_close else None),
                                   "calendar_source": calendar_source,
                                   "cost_model_provenance": cost_setup.model.provenance,
                                   "measured_quote": (
                                       dict(cost_setup.measured)
                                       if cost_setup.measured is not None else None),
                                   "shadow_signatures": shadow_signatures,
                                   "stress_calibration": self._stress_telemetry(
                                       replay_policy,
                                       vehicle=candidate_vehicle,
                                       timestamp=(calendar_close or
                                                  datetime.now(UTC))),
                                   "replay_signatures": replay_signatures,
                                   **identity_details}
        complete = bool(calendar_close is not None and
                        any((_event_end(row) or datetime.min.replace(tzinfo=UTC)) >=
                            calendar_close for row in in_session_bars))
        if diagnostic and not complete:
            # Diagnostic replay is a close-only modeled outcome. Intraday
            # explanation is retained in sparse decisions/rollups instead of
            # repeatedly replaying an incomplete full session.
            return False
        warmup_session = (
            str(diagnostic_activation.get("warmup_session") or "")
            if isinstance(diagnostic_activation, Mapping) else "")
        if diagnostic and warmup_session and str(session) <= warmup_session:
            details.update({
                "complete": True,
                "evaluated": False,
                "signature_match": False,
                "status_reason": "activation session is explanatory only",
                "diagnostic_only": True,
                "authorizing": False,
                "gate_eligible": False,
                "promotion_eligible": False,
                "cohort_identity": candidate.get("cohort_identity"),
                "activation_identity": diagnostic_activation.get(
                    "activation_identity"),
            })
            self.store.replay_diff(
                candidate_id=candidate_id, session_date=session,
                source_digest=source_digest, shadow_digest=shadow_digest,
                replay_digest=None, status="warmup_not_evaluated",
                details=details)
            return True
        try:
            if provider_mismatches:
                raise ShadowError(
                    "equity provider mismatch: expected "
                    f"{expected_equity_provider}")
            if feed_mismatches:
                raise ShadowError(
                    f"equity feed mismatch: expected {expected_equity_feed}")
            calendar_bounds = _recorded_session_bounds(
                self.config.corpus_path, session)
            if require_exact_calendar and calendar_bounds is None:
                raise ShadowError("exact broker calendar metadata unavailable")
            normalized_bar_rows = []
            for row in in_session_bars:
                enriched = dict(row)
                if calendar_bounds is not None:
                    enriched["session_open"] = calendar_bounds[0].isoformat()
                    enriched["session_close"] = calendar_bounds[1].isoformat()
                normalized_bar_rows.append(enriched)
            normalized_bars = [_normalize_row(row)[1]
                              for row in normalized_bar_rows]
            normalized_quotes = [_normalize_row(row)[1] for row in in_session_quotes]
            normalized_options = [_normalize_row(row)[1]
                                 for row in in_session_options]
            if str(candidate.get("strategy_id")) == "ibr":
                replay_cfg, _ = _effective_ibr_config(
                    cfg, {}, vehicle=candidate_vehicle,
                    close_confirmed=True, policy=replay_policy)
                option_index = _option_snapshot_index(normalized_options)
                result = replay_ibr(normalized_bars, config=replay_cfg,
                                    vehicle=candidate_vehicle,
                                    option_snapshots=option_index,
                                    quotes=normalized_quotes)
                result = reprice_ibr_result(
                    result, resolver=cost_setup.resolver,
                    vehicle=candidate_vehicle)
                trades = [_plain(trade) for trade in result.trades]
                evidence_rows = _opportunity_rows(
                    result, normalized_bars, candidate_vehicle)
                realized_pnl = sum(_finite(trade.get("net_pnl")) or 0.0
                                   for trade in trades)
                ending_cash = starting_cash + realized_pnl
                replay_signatures = [signature for trade in trades
                                     if (signature := _replay_signature(
                                         trade, vehicle=candidate_vehicle,
                                         strategy_id="ibr", target_r=replay_cfg.target_r,
                                         equity_feed=expected_equity_feed,
                                         equity_provider=expected_equity_provider)) is not None]
                details.update(complete=complete, trade_count=len(trades),
                               opportunity_count=len(evidence_rows), trades=trades,
                               replay_signatures=replay_signatures)
                details["opportunity_capacity"] = _opportunity_capacity(
                    evidence_rows, vehicle=candidate_vehicle)
                # Persist the exact-window randomized-entry null alongside the
                # candidate replay.  It is a diagnostic research source, not a
                # runtime shadow decision, and therefore receives its own
                # synthetic WAL candidate id below.
                if diagnostic:
                    details["null_control"] = False
                    details["null_reason"] = "diagnostic_non_authorizing"
                else:
                    try:
                        null_account = null_control_account(
                            normalized_bars, normalized_options, _null_spec(replay_cfg),
                            vehicle=candidate_vehicle,
                            reference_rows=_null_reference_rows(
                                result, normalized_bars,
                                str(candidate.get("vehicle") or "equity"),
                                policy=replay_cfg.policy),
                            account_id=f"shadow:null:{candidate_id}:{session}",
                            starting_cash=float(self.config.equity), costs=cost_setup.model,
                            cost_resolver=cost_setup.resolver,
                            quotes=normalized_quotes, fixed_quantity=replay_cfg.quantity,
                            policy=_policy(cfg))
                        null_rows = list(null_account.get("rows") or [])
                        details["null_control"] = True
                        details["null_trade_count"] = len([
                            row for row in null_rows if row.get("no_trade") is not True])
                    except Exception as exc:
                        details["null_control"] = False
                        details["null_error"] = (
                            f"{type(exc).__name__}: {str(exc)[:240]}")
                replay_ok = True
            else:
                strategy = cfg.get("strategy") if isinstance(cfg.get("strategy"), Mapping) else {}
                spec = strategy.get("rule_spec") if isinstance(strategy, Mapping) else None
                if not isinstance(spec, Mapping):
                    raise ValueError("rule candidate has no validated rule_spec")
                policy = replay_policy
                account = simulate_account(
                    normalized_bars, normalized_options,
                    spec, vehicle=candidate_vehicle,
                    account_id=f"shadow:{candidate_id}:{session}",
                    starting_cash=float(self.config.equity),
                    risk_pct=float((cfg.get("risk") or {}).get("risk_per_trade_pct", .5)),
                    costs=cost_setup.model, cost_resolver=cost_setup.resolver,
                    quotes=normalized_quotes, policy=policy)
                rows = list(account.get("rows") or [])
                evidence_rows = rows
                starting_cash = _finite(account.get("starting_cash")) or starting_cash
                ending_cash = _finite(account.get("ending_equity")) or starting_cash
                realized_pnl = _finite(account.get("realized_pnl"))
                if realized_pnl is None:
                    realized_pnl = ending_cash - starting_cash

                normalized_spec = validate_rule_spec(spec)

                def replay_context_defaults(trade: Mapping[str, Any]) -> dict[str, Any] | None:
                    if normalized_spec["family"] != "cross_sectional_residual":
                        return None
                    defaults: dict[str, Any] = {
                        "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK,
                        "eligibility": cross_sectional_symbol_eligibility(
                            str(trade.get("symbol") or ""), spec=normalized_spec),
                    }
                    context_digest = trade.get("market_context_digest")
                    if context_digest is not None:
                        defaults["candidate_behavior_identity"] = rule_behavior_identity(
                            normalized_spec, market_context_digest=str(context_digest))
                    return defaults

                replay_signatures = [signature for trade in rows
                                     if (signature := _replay_signature(
                                         trade, vehicle=candidate_vehicle,
                                         strategy_id="rule", target_r=float(spec.get("target_r", 2.0)),
                                         setup_type=f"rule_{spec.get('family', 'signal')}",
                                         equity_feed=expected_equity_feed,
                                         equity_provider=expected_equity_provider,
                                         context_defaults=replay_context_defaults(trade))) is not None]
                details.update(complete=complete, trade_count=len(replay_signatures),
                               replay_signatures=replay_signatures, account=account)
                details["opportunity_capacity"] = _opportunity_capacity(
                    rows, vehicle=candidate_vehicle)
                if diagnostic:
                    details["null_control"] = False
                    details["null_reason"] = "diagnostic_non_authorizing"
                else:
                    try:
                        null_account = null_control_account(
                            normalized_bars, normalized_options, spec,
                            vehicle=candidate_vehicle,
                            reference_rows=rows,
                            account_id=f"shadow:null:{candidate_id}:{session}",
                            starting_cash=float(self.config.equity),
                            risk_pct=float((cfg.get("risk") or {}).get(
                                "risk_per_trade_pct", .5)),
                            costs=cost_setup.model,
                            cost_resolver=cost_setup.resolver,
                            quotes=normalized_quotes,
                            policy=policy)
                        null_rows = list(null_account.get("rows") or [])
                        details["null_control"] = True
                        details["null_trade_count"] = len([
                            row for row in null_rows if row.get("no_trade") is not True])
                    except Exception as exc:
                        details["null_control"] = False
                        details["null_error"] = (
                            f"{type(exc).__name__}: {str(exc)[:240]}")
                replay_ok = True
        except Exception as exc:
            details.update(complete=complete, error=f"{type(exc).__name__}: {str(exc)[:240]}")
        differences = _signature_diffs(shadow_signatures, replay_signatures)
        replay_digest = _digest(replay_signatures) if details.get("complete") and replay_ok else None
        details.update(signature_match=not differences,
                       signature_mismatches=differences)
        status = ("incomplete" if not details.get("complete") or not replay_ok or replay_digest is None
                  else ("match" if not differences else "mismatch"))
        persisted_status = f"diagnostic_{status}" if diagnostic else status
        if diagnostic:
            details.update({
                "diagnostic_only": True,
                "authorizing": False,
                "gate_eligible": False,
                "promotion_eligible": False,
                "cohort_identity": candidate.get("cohort_identity"),
            })
        if complete and replay_ok:
            details["account_summary"] = {
                "starting_cash": starting_cash,
                "ending_cash": ending_cash,
                "realized_pnl": realized_pnl,
                "trade_count": len([row for row in evidence_rows
                                     if row.get("no_trade") is not True]),
                "replay_status": persisted_status,
            }
        self.store.replay_diff(candidate_id=candidate_id, session_date=session,
                               source_digest=source_digest, shadow_digest=shadow_digest,
                               replay_digest=replay_digest, status=persisted_status,
                               details=details)
        # Keep a durable repair trail for any session that was incomplete or
        # semantically mismatched.  Ingestion treats a quarantined session as
        # blocked even when later sessions are healthy; only a subsequent
        # complete, parity-matched replay records the explicit ``repaired``
        # transition that permits the chronological tail to advance.
        # A normal forward poll may observe a session before its closing bar;
        # that expected open tail is diagnostic but is not itself a repair
        # incident.  Quarantine only an attempted closed-session replay (or a
        # replay exception), while ingestion still refuses incomplete metadata.
        if diagnostic:
            pass
        elif status == "mismatch" or (
                status == "incomplete" and (complete or not replay_ok)):
            self.store.quarantine_replay_session(
                candidate_id=candidate_id, session_date=session,
                reason=("replay incomplete" if status == "incomplete"
                        else "shadow/replay semantic mismatch"),
                status=status, source_digest=source_digest,
                shadow_digest=shadow_digest, replay_digest=replay_digest)
        else:
            self.store.repair_replay_session(
                candidate_id=candidate_id, session_date=session,
                source_digest=source_digest, shadow_digest=shadow_digest,
                replay_digest=str(replay_digest),
                reason="complete parity replay after quarantine")
        if complete and replay_ok and replay_digest is not None:
            # Persist fills/exits/P&L in the isolated shadow database.  The
            # rows are diagnostic while parity is mismatched; ``gate_rows``
            # exposes them to existing gates only for a current ``match``.
            self.store.record_replay_evidence(
                candidate_id=candidate_id, session_date=session,
                replay_digest=replay_digest,
                vehicle=str(candidate.get("vehicle") or "equity"),
                starting_cash=starting_cash, ending_cash=ending_cash,
                realized_pnl=realized_pnl, trades=evidence_rows,
                replay_status=persisted_status)
            # Randomized-entry nulls are generated from this exact normalized
            # session, so their source digest is identical.  They have no
            # runtime semantic signature to compare; ``match`` here means the
            # deterministic null replay completed, not that a broker decision
            # matched it.
            if not diagnostic:
                null_digest = _digest(null_rows)
                null_candidate_id = f"shadow:null:{candidate_id}"
                self.store.replay_diff(
                    candidate_id=null_candidate_id, session_date=session,
                    source_digest=source_digest, shadow_digest=_digest([]),
                    replay_digest=null_digest, status="match",
                    details={"complete": True, "signature_match": True,
                             "equity_feed": expected_equity_feed,
                             "equity_provider": expected_equity_provider,
                             "null_control": True, "replay_signatures": [],
                             "null_rows_digest": null_digest,
                             **identity_details})
                self.store.record_replay_evidence(
                    candidate_id=null_candidate_id, session_date=session,
                    replay_digest=null_digest,
                    vehicle=str(candidate.get("vehicle") or "equity"),
                    starting_cash=_finite(null_account.get("starting_cash")) or starting_cash,
                    ending_cash=_finite(null_account.get("ending_equity")) or starting_cash,
                    realized_pnl=_finite(null_account.get("realized_pnl")) or 0.0,
                    trades=null_rows, replay_status="match")
        # A closing timestamp alone is not enough: malformed input or a
        # replay exception must remain diagnostic and keep the virtual open
        # blocked rather than silently declaring it settled.
        if complete and replay_ok:
            self.store.close_session_books(candidate_id, session)
        return complete

    @staticmethod
    def _append_worker_open(state: tuple[list[dict], dict[str, dict], float],
                            plan: Mapping[str, Any], symbol: str
                            ) -> tuple[list[dict], dict[str, dict], float]:
        """Advance one legacy worker's private virtual-book projection."""
        positions, active_trades, gross_notional = state
        position = dict(plan)
        position["symbol"] = str(position.get("symbol") or symbol)
        risk_usd = _finite(position.get("risk_usd")) or 0.0
        notional = _finite(position.get("notional")) or 0.0
        active = dict(position)
        active["risk_usd"] = risk_usd
        positions.append(position)
        active_trades[str(symbol)] = active
        return positions, active_trades, gross_notional + notional

    def _evaluate_arm_snapshot(self, arm: Mapping[str, Any],
                               session_events: Mapping[str, Sequence[Mapping]],
                               session_inputs: Mapping[str, tuple[Sequence[Mapping],
                                                                   Sequence[Mapping],
                                                                   Sequence[Mapping]]],
                               bars: Mapping[str, Sequence[Mapping]],
                               quotes: Mapping[str, Sequence[Mapping]],
                               options: Mapping[str, Sequence[Mapping]],
                               initial_state: tuple[list[dict], dict[str, dict], float]
                               ) -> dict[str, Any]:
        """Evaluate one legacy arm without touching the shadow WAL."""
        candidate_id = str(arm["candidate_id"])
        state = (list(initial_state[0]), dict(initial_state[1]),
                 float(initial_state[2]))
        self._worker_state.portfolios = {candidate_id: state}
        decisions: list[dict[str, Any]] = []
        try:
            for session in sorted(session_events):
                for event in session_events[session]:
                    symbol = str(event.get("symbol") or "")
                    event_key = str(event.get("event_key") or "")
                    if any(str(row.get("symbol") or "") == symbol
                           for row in state[0]):
                        kind, reason, payload, plan = (
                            "no_trade", "virtual book has an incomplete open",
                            {"session_date": session,
                             "strategy_id": arm.get("strategy_id"),
                             "variant_id": arm.get("variant_id")}, None)
                    else:
                        kind, reason, payload, plan = self._evaluate(
                            arm, event, bars, quotes, options)
                    decisions.append({
                        "candidate_id": candidate_id,
                        "event_key": event_key,
                        "session_date": session,
                        "symbol": symbol,
                        "kind": kind,
                        "reason": reason,
                        "payload": payload,
                        "plan": plan,
                    })
                    if plan is not None:
                        state = self._append_worker_open(state, plan, symbol)
                        self._worker_state.portfolios[candidate_id] = state
                state = ([], {}, 0.0)
                self._worker_state.portfolios[candidate_id] = state
        except Exception as exc:
            return {"candidate_id": candidate_id, "decisions": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        finally:
            try:
                del self._worker_state.portfolios
            except AttributeError:
                pass
        return {"candidate_id": candidate_id, "decisions": decisions,
                "error": None}

    def _evaluate_diagnostic_arm_snapshot(
            self, arm: Mapping[str, Any],
            session_events: Mapping[str, Sequence[Mapping]],
            session_inputs: Mapping[str, tuple[Sequence[Mapping],
                                                Sequence[Mapping],
                                                Sequence[Mapping]]],
            bars: Mapping[str, Sequence[Mapping]],
            quotes: Mapping[str, Sequence[Mapping]],
            options: Mapping[str, Sequence[Mapping]],
            initial_state: Mapping[str, Any],
            warmup_session: str | None = None) -> dict[str, Any]:
        """Evaluate one immutable arm without touching the shadow WAL.

        Every worker receives the same tuple-backed event snapshot.  The
        private portfolio projection reproduces within-arm admission for
        multiple events while keeping SQLite reads/writes out of the worker.
        """
        candidate_id = str(arm["candidate_id"])
        cfg = self._shadow_candidate_config(_safe_config(arm))
        strategy = cfg.get("strategy") if isinstance(
            cfg.get("strategy"), Mapping) else {}
        rule_spec = strategy.get("rule_spec") if isinstance(
            strategy, Mapping) else None
        if not isinstance(rule_spec, Mapping):
            return {"candidate_id": candidate_id, "decisions": [],
                    "error": "diagnostic rule specification unavailable"}
        book = DiagnosticAccountBook(
            account=initial_state.get("account") or {},
            positions=initial_state.get("positions") or (), config=cfg,
            policy=self._shadow_policy(cfg), rule_spec=rule_spec)
        state = book.risk_state()
        self._worker_state.portfolios = {candidate_id: state}
        self._worker_state.equities = {candidate_id: book.equity}
        decisions: list[dict[str, Any]] = []
        warmup_session = str(warmup_session or "")
        quote_rows = tuple(row for values in quotes.values() for row in values)
        ordered_events: list[tuple[datetime, str, str, Mapping[str, Any]]] = []
        for session, events in session_events.items():
            if session not in session_inputs:
                raise ShadowError(
                    f"diagnostic session context {session} is unavailable")
            for event in events:
                available_at = _availability_time(event)
                if available_at is None:
                    raise ShadowError("diagnostic event availability is invalid")
                ordered_events.append((
                    available_at, str(event.get("event_key") or ""),
                    str(session), event))
        ordered_events.sort(key=lambda item: (item[0], item[1]))
        persisted_at = _timestamp(book.account.get("last_event_at"))
        account_watermark = (
            (persisted_at, str(book.account.get("last_event_key") or ""))
            if persisted_at is not None else None)
        try:
            for available_at, event_key, session, event in ordered_events:
                # The insertion cursor can legitimately encounter a delayed
                # event whose claimed availability precedes already committed
                # account state. Consume it, but never rewind cash or positions.
                if (account_watermark is not None and
                        (available_at, event_key) <= account_watermark):
                    continue
                symbol = str(event.get("symbol") or "")
                event_type = str(event.get("event_type") or "").lower()
                if event_type == "quote":
                    if not warmup_session or session > warmup_session:
                        book.advance_quote_event(event, quote_rows=quote_rows)
                        state = book.risk_state()
                        self._worker_state.portfolios[candidate_id] = state
                        self._worker_state.equities[candidate_id] = book.equity
                    account_at = _timestamp(book.account.get("last_event_at"))
                    if account_at is not None:
                        account_watermark = (
                            account_at,
                            str(book.account.get("last_event_key") or ""))
                    continue
                # Existing positions consume this completed bar before a new
                # signal is considered. A newly modeled entry can therefore
                # never consume its own signal bar as exit data.
                if not warmup_session or session > warmup_session:
                    book.advance_completed_bar(event, quote_rows=quote_rows)
                state = book.risk_state()
                self._worker_state.portfolios[candidate_id] = state
                self._worker_state.equities[candidate_id] = book.equity
                if book.has_open(symbol):
                    kind, reason, payload, plan = (
                        "no_trade", "persistent diagnostic position is open",
                        {"session_date": session,
                         "strategy_id": arm.get("strategy_id"),
                         "variant_id": arm.get("variant_id")}, None)
                else:
                    kind, reason, payload, plan = self._evaluate(
                        arm, event, bars, quotes, options)
                decisions.append({
                    "candidate_id": candidate_id,
                    "event_key": event_key,
                    "session_date": session,
                    "symbol": symbol,
                    "kind": kind,
                    "reason": reason,
                    "payload": payload,
                    "plan": plan,
                })
                if (plan is not None and
                        (not warmup_session or session > warmup_session)):
                    book.open_requested_position(
                        event=event, plan=plan, quote_rows=quote_rows)
                    state = book.risk_state()
                    self._worker_state.portfolios[candidate_id] = state
                    self._worker_state.equities[candidate_id] = book.equity
                account_at = _timestamp(book.account.get("last_event_at"))
                if account_at is not None:
                    account_watermark = (
                        account_at,
                        str(book.account.get("last_event_key") or ""))
        except Exception as exc:
            # The parent records the bounded diagnostic in its poll result and
            # continues sibling arms.  No partial worker output is committed,
            # so a retry can deterministically recompute this candidate.
            return {"candidate_id": candidate_id, "decisions": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        finally:
            for name in ("portfolios", "equities"):
                try:
                    delattr(self._worker_state, name)
                except AttributeError:
                    pass
        return {"candidate_id": candidate_id, "decisions": decisions,
                "account_batch": book.batch(), "error": None}

    def run_once(self) -> dict[str, Any]:
        poll_started = time.monotonic()
        # Validate the mutable operational pointer before writing anything.
        # Older code identities are readable for rollover; malformed or
        # self-digest-mismatched manifests are never silently replaced.
        prior_manifest = self.store.latest_manifest_for_rollover()
        # Cohort registration is intentionally the first durable action.  The
        # activation watermark is written only after this poll's existing
        # corpus bytes have been ingested, so none can become a decision event.
        diagnostic_cohort, diagnostic_arms, diagnostic_activation = (
            self._prepare_diagnostic_cohort())
        # Factory hypotheses can be registered by the research cycle between
        # shadow polls; refresh the read-only root catalog for every pass.
        self._factory_roots = _read_factory_rule_roots(self.config.edge_db)
        candidates = _read_candidates(self.config.edge_db, max_candidates=self.config.max_candidates)
        for candidate in candidates:
            self.store.upsert_candidate(candidate)
        ingested = 0
        conflicts = 0
        invalid_events = 0
        sources = _corpus_sources(self.config.corpus_path)
        offsets = self.store.source_offsets()
        forward_floor = self.store.forward_event_floor()
        if forward_floor is None:
            # feaca71 established source offsets before filtering the legacy
            # event cache. Mark the migration boundary once and keep all later
            # cycles scoped to genuinely forward observations.
            # An empty-candidate poll establishes its activation boundary only
            # after ingestion/quarantine checks below; initializing it to
            # ``time.time()`` here would skip unresolved evidence.
            forward_floor = (time.time() if candidates and
                             self.store.event_count() >= self.config.max_events
                             else 0.0)
            self.store.save_forward_event_floor(forward_floor)
        if offsets is None:
            # A pre-upgrade WAL already at the old total-event ceiling has
            # consumed historical evidence. Baseline at the current committed
            # ends instead of replaying a multi-million-row recorder catch-up.
            offsets = ({str(source.resolve()): source.stat().st_size
                        for source in sources}
                       if self.store.event_count() >= self.config.max_events
                       else {})
            self.store.save_source_offsets(offsets)
        pending: list[dict] = []
        next_offsets = dict(offsets)
        source_consumed: dict[str, int] = {}
        source_for_event: dict[str, tuple[str, int, int]] = {}
        pending_bytes = sum(max(0, source.stat().st_size - offsets.get(
            str(source.resolve()), 0)) for source in sources)
        skipped_recovery_bytes = 0
        if pending_bytes > MAX_PENDING_CORPUS_BYTES:
            # A failed pre-offset consumer may leave hundreds of megabytes of
            # raw quotes pending. Do not materialize that range merely to
            # compact it. Baseline it explicitly and quarantine the current
            # NY session so a partial forward window can never qualify.
            skipped_recovery_bytes = pending_bytes
            next_offsets = {str(source.resolve()): source.stat().st_size
                            for source in sources}
            if candidates:
                forward_floor = time.time()
                self.store.save_forward_event_floor(forward_floor)
            self.store.save_quarantine_through_session(
                datetime.now(UTC).astimezone(NEW_YORK).date().isoformat())
        else:
            for source in sources:
                key = str(source.resolve())
                source_start = int(offsets.get(key, 0))
                rows, consumed = _read_corpus_append(source, source_start)
                pending.extend(rows)
                source_consumed[key] = consumed
                for row in rows:
                    event_key = str(row.get("event_key") or "")
                    if event_key:
                        source_for_event[event_key] = (
                            key, source_start, int(consumed))
        selected = _compact_shadow_rows(pending)
        if len(selected) > self.config.max_events:
            raise ShadowError(
                f"shadow event batch bound {self.config.max_events} exceeded")
        quarantine = self.store.quarantine_events()
        invalid_sources: set[str] = set()
        resolved_quarantine: set[str] = set()
        invalid_sessions: set[str] = {
            str(detail.get("session_date"))
            for detail in quarantine.values()
            if isinstance(detail, Mapping) and detail.get("session_date")
        }
        unknown_quarantine = any(
            not isinstance(detail, Mapping) or not detail.get("session_date")
            for detail in quarantine.values())
        quarantine_overflow = QUARANTINE_OVERFLOW_KEY in quarantine
        for raw in selected:
            event_key = str(raw.get("event_key") or "")
            source_provenance = source_for_event.get(event_key)
            source_key = source_provenance[0] if source_provenance else None
            try:
                _, added = self.store.ingest_event(
                    raw, max_events=self.config.max_events,
                    source_path=(source_provenance[0]
                                 if source_provenance else None),
                    source_offset_start=(source_provenance[1]
                                         if source_provenance else None),
                    source_offset_end=(source_provenance[2]
                                       if source_provenance else None))
            except InputConflict:
                conflicts += 1
                raise
            except NormalizationError:
                # Keep the source offset before this malformed event.  The
                # event is explicitly quarantined and its local session is
                # blocked, so an operator can correct the recorder row and the
                # next poll will retry the exact same bytes.  Advancing over
                # it would permanently skip an unknown/incomplete session.
                invalid_events += 1
                if source_key:
                    invalid_sources.add(source_key)
                stamp = _timestamp(raw.get("as_of") or raw.get("timestamp"))
                session = (stamp.astimezone(NEW_YORK).date().isoformat()
                           if stamp is not None else None)
                if session:
                    invalid_sessions.add(session)
                else:
                    unknown_quarantine = True
                quarantine[event_key or _digest(raw)] = {
                    "event_key": event_key,
                    "source": source_key,
                    "session_date": session,
                    "reason": "normalization_error",
                }
                continue
            if event_key in quarantine:
                resolved_quarantine.add(event_key)
            if added:
                ingested += 1
        # Only commit offsets for sources whose complete forward batch was
        # normalized.  Other sources remain at their previous boundary and
        # are retried after correction; already-ingested rows are idempotent.
        for key, consumed in source_consumed.items():
            if key not in invalid_sources:
                next_offsets[key] = consumed
        for event_key in resolved_quarantine:
            quarantine.pop(event_key, None)
        # ``replace=True`` lets this poll remove an event after the exact
        # corrected bytes normalize; direct operator writes default to a
        # monotonic merge so unrelated quarantine evidence is preserved.
        self.store.save_quarantine_events(quarantine, replace=True)
        self.store.save_source_offsets(next_offsets)
        # Recompute the durable block after resolving corrected rows.  A
        # corrected replay can therefore become eligible in this same poll;
        # it does not require an operator to run an extra no-op cycle.
        invalid_sessions = {
            str(detail.get("session_date"))
            for detail in quarantine.values()
            if isinstance(detail, Mapping) and detail.get("session_date")
        }
        unknown_quarantine = any(
            not isinstance(detail, Mapping) or not detail.get("session_date")
            for detail in quarantine.values())
        quarantine_overflow = QUARANTINE_OVERFLOW_KEY in quarantine

        if diagnostic_cohort is not None and diagnostic_activation is None:
            diagnostic_activation = self._activate_diagnostic_cohort(
                diagnostic_cohort, source_offsets=next_offsets,
                forward_event_floor=float(forward_floor or 0.0))
        if diagnostic_cohort is not None and diagnostic_activation is not None:
            self.store.seed_diagnostic_accounts(
                cohort_identity=str(diagnostic_cohort.get(
                    "cohort_identity") or ""),
                candidate_ids=[str(arm.get("candidate_id") or "")
                               for arm in diagnostic_arms],
                starting_cash=float(self.config.equity))
        diagnostic_rows, diagnostic_progress, diagnostic_event_watermark = (
            self._diagnostic_snapshot(
                diagnostic_cohort, diagnostic_arms, diagnostic_activation))

        # A poll without an eligible candidate still has to finish ingestion,
        # quarantine, and source-offset persistence above.  It must not then
        # materialize the complete immutable event WAL merely to report that
        # there is no work: a recorder can legitimately contain hundreds of
        # thousands of rows before the first hypothesis is registered.  The
        # activation boundary makes this empty poll a strict forward barrier;
        # future candidates cannot consume evidence observed before they became
        # eligible, while every event remains durably queryable in SQLite.
        if not candidates:
            replay_quarantine = self.store.replay_quarantine()
            pending_repairs = [
                dict(detail) for detail in replay_quarantine.values()
                if isinstance(detail, Mapping) and detail.get("status") in {
                    "quarantined", "overflow"}
            ]
            pending_repairs.sort(key=lambda item: (
                str(item.get("session_date") or ""),
                str(item.get("candidate_id") or "")))
            current_floor = float(forward_floor or 0.0)
            blocked_activation = bool(
                invalid_sessions or unknown_quarantine or quarantine_overflow or
                pending_repairs or skipped_recovery_bytes)
            if not blocked_activation and (current_floor <= 0.0 or ingested > 0):
                activation_boundary = time.time()
                forward_floor = max(current_floor, activation_boundary)
                if forward_floor > current_floor:
                    self.store.save_forward_event_floor(forward_floor)
            else:
                # Preserve the prior floor across malformed or unresolved
                # evidence; an empty candidate set is not permission to skip
                # a quarantined session.
                forward_floor = current_floor
            prune = self.store.prune()
            stored_events = self.store.event_count()
            quarantine_through = self.store.quarantine_through_session()
            signal_dispositions = _signal_dispositions([
                row for row in self.store.decisions()
                if not self.store.candidate_is_diagnostic(
                    str(row.get("candidate_id") or ""))
            ])
            calibration_status = self._calibration_status()
            stale_tail = {
                "status": "blocked" if (
                    invalid_sessions or unknown_quarantine or pending_repairs or
                    skipped_recovery_bytes) else "clear",
                "sessions": sorted(invalid_sessions),
                "unknown_events": bool(unknown_quarantine),
                "quarantine_overflow": bool(quarantine_overflow),
                "invalid_events": int(invalid_events),
                "replay_repairs_required": len(pending_repairs),
                "replay_quarantine_sessions": sorted({
                    str(item.get("session_date")) for item in pending_repairs
                    if item.get("session_date")}),
                "replay_quarantine": pending_repairs[-64:],
                "authoritative_catalog_sessions": [],
                "signal_dispositions": signal_dispositions,
                "stress_calibration": calibration_status,
            }
            candidate_watermark = [{
                "candidate_id": str(arm.get("candidate_id") or ""),
                "variant_id": str(arm.get("variant_id") or ""),
                "strategy_id": str(arm.get("strategy_id") or ""),
                "vehicle": str(arm.get("vehicle") or ""),
                "status": str(arm.get("status") or ""),
                "config_digest": _digest(_safe_config(arm)),
                "diagnostic_only": True,
                "family": arm.get("family"),
                "role": arm.get("role"),
                "cohort_identity": arm.get("cohort_identity"),
            } for arm in diagnostic_arms]
            manifest = {
                "schema": "shadow-manifest.v1",
                "replay_code_hash": _replay_code_hash(),
                "replay_scope": "no_candidates",
                "candidate_set": candidate_watermark,
                "candidate_set_digest": _digest(candidate_watermark),
                "source_watermark": {
                    "offsets": {str(key): int(value)
                                for key, value in next_offsets.items()},
                    "forward_event_floor": float(forward_floor),
                },
                "event_watermark": {
                    "count": int(stored_events),
                    "snapshot": "not_loaded",
                    "forward_event_floor": float(forward_floor),
                },
                "session_watermark": [],
                "max_workers": int(self.config.max_workers),
                "diagnostic_shadow": ({
                    "diagnostic_only": True,
                    "authorizing": False,
                    "cohort_identity": diagnostic_cohort.get(
                        "cohort_identity"),
                    "activation_identity": diagnostic_activation.get(
                        "activation_identity") if diagnostic_activation else None,
                    "event_watermark": diagnostic_event_watermark,
                    "candidate_identities": diagnostic_cohort.get(
                        "candidate_identities"),
                } if diagnostic_cohort is not None else {
                    "diagnostic_only": False, "authorizing": False,
                    "enabled": False,
                }),
            }
            manifest_digest = self.store.save_manifest(manifest)
            replay_identity = _manifest_replay_identity(
                manifest, manifest_digest)
            diagnostic_result = self._run_diagnostic_poll(
                diagnostic_cohort, diagnostic_arms, diagnostic_activation,
                snapshot_rows=diagnostic_rows,
                progress=diagnostic_progress,
                replay_identity=replay_identity)
            diagnostic_errors = diagnostic_result.get("candidate_errors") or {}
            decision_count = self.store.decision_count()
            result = {
                "candidates": 0,
                "no_candidates": True,
                "ingested_events": ingested,
                # ``events`` is the bounded poll snapshot; no event rows were
                # loaded.  ``stored_events`` retains the scalar WAL count.
                "events": 0,
                "stored_events": int(stored_events),
                "decisions": int(decision_count),
                "conflicts": conflicts,
                "invalid_events": invalid_events,
                "manifest_digest": manifest_digest,
                "candidate_errors": dict(diagnostic_errors),
                "skipped_recovery_bytes": skipped_recovery_bytes,
                "quarantine_through_session": quarantine_through,
                "forward_event_floor": float(forward_floor),
                "stale_tail": stale_tail,
                "replay_quarantine": pending_repairs[-64:],
                "opportunity_capacity": [],
                "signal_dispositions": signal_dispositions,
                "stress_calibration": calibration_status,
                "authorizing_candidates": 0,
                "diagnostic_candidates": len(diagnostic_arms),
                **prune,
            }
            result["diagnostic_shadow"] = self._diagnostic_coverage(
                diagnostic_cohort, diagnostic_activation,
                this_poll_decisions=int(diagnostic_result.get(
                    "this_poll_decisions") or 0),
                preactivation_rejections=int(diagnostic_result.get(
                    "provenance_rejections") or 0),
                poll_duration_seconds=time.monotonic() - poll_started)
            result["poll_duration_seconds"] = result[
                "diagnostic_shadow"]["poll_duration_seconds"]
            result["source_lag_seconds"] = result[
                "diagnostic_shadow"]["source_lag_seconds"]
            return result
        events, bars, quotes, options = self._load_events()
        # Persist only exact recorder/Alpaca calendar sessions.  This catalog
        # is the continuity authority used by ingestion; event timestamps or
        # weekday heuristics are intentionally insufficient (holidays and
        # early closes must remain represented by the recorder provenance).
        catalog = self.store.session_catalog()
        # Import completed calendar sessions even when a particular session
        # has no normalized events.  This is what makes an all-arm missing
        # middle session visible instead of letting the union of replay rows
        # silently skip it.
        for session, bounds in _recorded_session_calendar(self.config.corpus_path).items():
            catalog.setdefault(session, {
                "session_date": session,
                "open": bounds[0].isoformat(),
                "close": bounds[1].isoformat(),
                "source": "recorder_alpaca_calendar",
                "recorded_ts": time.time(),
            })
        for event in events:
            if event.get("event_type") not in {"bar", "bar_1m"}:
                continue
            stamp = _timestamp(event.get("as_of") or event.get("timestamp"))
            if stamp is None:
                continue
            session = stamp.astimezone(NEW_YORK).date().isoformat()
            bounds = _recorded_session_bounds(self.config.corpus_path, session)
            event_end = _event_end(event)
            if bounds is None or event_end is None or event_end < bounds[1]:
                continue
            catalog.setdefault(session, {
                "session_date": session,
                "open": bounds[0].isoformat(),
                "close": bounds[1].isoformat(),
                "source": "recorder_alpaca_calendar",
                "recorded_ts": time.time(),
            })
        if catalog != self.store.session_catalog():
            self.store.save_session_catalog(catalog)
        # Process one local session at a time.  A completed replay closes that
        # session's virtual books before the next session is evaluated, which
        # is essential when the recorder is catching up multiple sessions in
        # a single invocation.  Replay receives the complete symbol set for a
        # session; its diff row is unique by candidate/session and must not be
        # overwritten once per symbol.
        def row_session(row: Mapping[str, Any]) -> str | None:
            stamp = _timestamp(row.get("as_of") or row.get("timestamp"))
            return (stamp.astimezone(NEW_YORK).date().isoformat()
                    if stamp is not None else None)

        session_events: dict[str, list[dict]] = {}
        quarantine_through = self.store.quarantine_through_session()
        for event in events:
            if event.get("event_type") not in {"bar", "bar_1m"}:
                continue
            session = row_session(event)
            if session is not None and (
                    not unknown_quarantine and session not in invalid_sessions and
                    (quarantine_through is None or session > quarantine_through)):
                session_events.setdefault(session, []).append(event)

        session_inputs: dict[str, tuple[list[dict], list[dict], list[dict]]] = {}
        for session in sorted(session_events):
            session_inputs[session] = (
                [row for values in bars.values() for row in values
                 if row_session(row) == session],
                [row for values in quotes.values() for row in values
                 if row_session(row) == session],
                [row for values in options.values() for row in values
                 if row_session(row) == session],
            )

        # Freeze both arm definitions and event inputs before dispatch.  A
        # research poll that registers a candidate or appends a recorder row
        # while workers run therefore affects only the next poll.
        arms: list[dict[str, Any]] = []
        for candidate in candidates:
            if str(candidate.get("vehicle") or "equity") not in {"equity", "option"}:
                continue
            arms.append(dict(candidate))
            root_control = self._rule_root_control(candidate)
            if root_control is not None:
                arms.append(dict(root_control))
        arms.sort(key=lambda item: str(item.get("candidate_id") or ""))
        for arm in arms:
            self.store.upsert_candidate(arm)

        # A strict forward floor intentionally excludes already-consumed
        # source rows. Revalidate only sessions that currently contribute
        # matched gate evidence, using a timestamp-indexed bounded WAL query;
        # this catches corrected replay semantics without rescanning corpus
        # history or treating a no-op poll as fresh evidence.
        arm_ids = {str(arm.get("candidate_id") or "") for arm in arms}
        gate_pairs = {(candidate_id, session)
                      for candidate_id, session in self.store.gate_sessions()
                      if candidate_id in arm_ids}
        replay_only_sessions = {
            session for _candidate_id, session in gate_pairs
            if session not in session_inputs}
        if replay_only_sessions:
            old_events, old_bars, old_quotes, old_options = (
                self._load_events_for_sessions(sorted(replay_only_sessions)))
            for target, source in ((bars, old_bars), (quotes, old_quotes),
                                  (options, old_options)):
                for symbol, values in source.items():
                    target.setdefault(symbol, []).extend(values)
            for session in sorted(replay_only_sessions):
                session_events[session] = [
                    row for row in old_events
                    if row.get("event_type") in {"bar", "bar_1m"}
                    and row_session(row) == session]
                session_inputs[session] = (
                    [row for values in old_bars.values() for row in values
                     if row_session(row) == session],
                    [row for values in old_quotes.values() for row in values
                     if row_session(row) == session],
                    [row for values in old_options.values() for row in values
                     if row_session(row) == session],
                )

        # Convert all worker inputs to detached JSON values and tuples.  The
        # tuples are never handed to a mutating path, making the poll snapshot
        # explicit even if a provider returns mutable row objects.
        frozen_events = tuple(json.loads(_json(dict(row))) for row in events)
        frozen_bars = {
            str(symbol): tuple(json.loads(_json(dict(row))) for row in values)
            for symbol, values in bars.items()}
        frozen_quotes = {
            str(symbol): tuple(json.loads(_json(dict(row))) for row in values)
            for symbol, values in quotes.items()}
        frozen_options = {
            str(symbol): tuple(json.loads(_json(dict(row))) for row in values)
            for symbol, values in options.items()}
        frozen_session_events: dict[str, tuple[dict, ...]] = {}
        for session, values in session_events.items():
            frozen_session_events[session] = tuple(
                json.loads(_json(dict(row))) for row in values)
        frozen_session_inputs: dict[str, tuple[tuple[dict, ...], tuple[dict, ...], tuple[dict, ...]]] = {}
        for session, (session_bars, session_quotes, session_options) in session_inputs.items():
            frozen_session_inputs[session] = (
                tuple(json.loads(_json(dict(row))) for row in session_bars),
                tuple(json.loads(_json(dict(row))) for row in session_quotes),
                tuple(json.loads(_json(dict(row))) for row in session_options),
            )
        event_watermark = {
            "count": len(frozen_events),
            "events_digest": _digest([
                {key: row.get(key) for key in (
                    "event_key", "digest", "event_type", "symbol",
                    "timestamp", "as_of")}
                for row in frozen_events]),
            "last_event_key": (str(frozen_events[-1].get("event_key") or "")
                                if frozen_events else None),
            "last_timestamp": (str(frozen_events[-1].get("timestamp") or "")
                                if frozen_events else None),
        }
        manifest_arms = [*arms, *(dict(arm) for arm in diagnostic_arms)]
        manifest_arms.sort(key=lambda item: str(item.get("candidate_id") or ""))
        candidate_watermark = [{
            "candidate_id": str(arm.get("candidate_id") or ""),
            "variant_id": str(arm.get("variant_id") or ""),
            "strategy_id": str(arm.get("strategy_id") or ""),
            "vehicle": str(arm.get("vehicle") or ""),
            "status": str(arm.get("status") or ""),
            "config_digest": _digest(_safe_config(arm)),
            "diagnostic_only": is_diagnostic_candidate(arm),
            "family": arm.get("family") if is_diagnostic_candidate(arm) else None,
            "role": arm.get("role") if is_diagnostic_candidate(arm) else None,
            "cohort_identity": (arm.get("cohort_identity")
                                if is_diagnostic_candidate(arm) else None),
        } for arm in manifest_arms]
        manifest = {
            "schema": "shadow-manifest.v1",
            "replay_code_hash": _replay_code_hash(),
            "candidate_set": candidate_watermark,
            "candidate_set_digest": _digest(candidate_watermark),
            "source_watermark": {
                "offsets": {str(key): int(value) for key, value in next_offsets.items()},
                "forward_event_floor": float(forward_floor or 0.0),
            },
            "event_watermark": event_watermark,
            "session_watermark": sorted(str(session) for session in
                                         set(frozen_session_events) |
                                         replay_only_sessions),
            "replay_validation_sessions": sorted(replay_only_sessions),
            "max_workers": int(self.config.max_workers),
            "diagnostic_shadow": ({
                "diagnostic_only": True,
                "authorizing": False,
                "cohort_identity": diagnostic_cohort.get("cohort_identity"),
                "code_identity": diagnostic_cohort.get("code_identity"),
                "runtime_config_identity": diagnostic_cohort.get(
                    "runtime_config_identity"),
                "policy_config_identity": diagnostic_cohort.get(
                    "policy_config_identity"),
                "activation_identity": (diagnostic_activation.get(
                    "activation_identity") if diagnostic_activation else None),
                "activation_event_watermark": (
                    diagnostic_activation.get("activation_event_watermark")
                    if diagnostic_activation else None),
                "event_watermark": diagnostic_event_watermark,
                "candidate_identities": diagnostic_cohort.get(
                    "candidate_identities"),
                "arms": [{key: arm.get(key) for key in (
                    "candidate_id", "family", "role", "variant_id",
                    "spec_identity", "config_identity", "code_identity",
                    "cohort_identity")}
                         for arm in diagnostic_cohort.get("arms", ())
                         if isinstance(arm, Mapping)],
            } if diagnostic_cohort is not None else {
                "diagnostic_only": False, "authorizing": False,
                "enabled": False,
            }),
        }
        # A successful replay advances the immutable event floor below.  On a
        # retry with no newly loaded rows, reuse that final content-addressed
        # snapshot rather than creating a digest that merely says ``count=0``;
        # this keeps retries auditable and idempotent while still allowing a
        # changed candidate/source snapshot to receive a new manifest.
        reuse_manifest = bool(
            not events and not frozen_session_events and
            not replay_only_sessions and not diagnostic_rows and
            isinstance(prior_manifest, Mapping) and
            prior_manifest.get("candidate_set_digest") == manifest["candidate_set_digest"] and
            prior_manifest.get("source_watermark", {}).get("offsets") ==
            manifest["source_watermark"]["offsets"] and
            prior_manifest.get("source_watermark", {}).get("forward_event_floor") ==
            manifest["source_watermark"]["forward_event_floor"] and
            prior_manifest.get("replay_code_hash") == manifest["replay_code_hash"])
        if reuse_manifest:
            manifest = dict(prior_manifest)
            manifest_digest = str(manifest["manifest_digest"])
        else:
            manifest_digest = self.store.save_manifest(manifest)
        replay_identity = _manifest_replay_identity(manifest, manifest_digest)
        diagnostic_result = self._run_diagnostic_poll(
            diagnostic_cohort, diagnostic_arms, diagnostic_activation,
            snapshot_rows=diagnostic_rows, progress=diagnostic_progress,
            replay_identity=replay_identity)

        # Dispatch one immutable session at a time.  The parent commits
        # decisions and performs replay before the next session is submitted,
        # preserving virtual-book blocking when a replay remains incomplete.
        # Workers still run candidate arms concurrently within each barrier.
        arm_by_id = {str(arm["candidate_id"]): arm for arm in arms}
        diagnostic_errors = dict(diagnostic_result.get("candidate_errors") or {})
        candidate_errors: dict[str, str] = dict(diagnostic_errors)
        authorizing_errors: dict[str, str] = {}
        failed_arms: set[str] = set()
        replay_blocked = False
        replay_decisions: dict[str, list[dict[str, Any]]] = {}
        for session in sorted(set(frozen_session_events) | replay_only_sessions):
            is_replay_only = session in replay_only_sessions
            session_inputs_one = {session: frozen_session_inputs[session]}
            session_candidates = ({candidate_id for candidate_id, value in gate_pairs
                                   if value == session}
                                  if is_replay_only else arm_ids)
            eligible_events_by_arm: dict[str, tuple[dict, ...]] = {}
            active_arms: list[dict[str, Any]] = []
            for arm in arms:
                candidate_id = str(arm["candidate_id"])
                if candidate_id not in session_candidates or candidate_id in failed_arms:
                    continue
                eligible_events_by_arm[candidate_id] = tuple(
                    frozen_session_events[session])
                active_arms.append(arm)
            session_success = bool(active_arms)
            initial_states = {
                str(arm["candidate_id"]): self._portfolio_state(
                    str(arm["candidate_id"])) for arm in active_arms}
            worker_results: list[dict[str, Any]] = []
            if active_arms:
                with ThreadPoolExecutor(max_workers=self.config.max_workers,
                                        thread_name_prefix="shadow-eval") as pool:
                    futures = {
                        pool.submit(self._evaluate_arm_snapshot, arm,
                                    {session: eligible_events_by_arm[
                                        str(arm["candidate_id"])]},
                                    session_inputs_one,
                                    frozen_bars, frozen_quotes, frozen_options,
                                    initial_states[str(arm["candidate_id"])]): arm
                        for arm in active_arms}
                    for future in as_completed(futures):
                        arm = futures[future]
                        try:
                            worker_results.append(future.result())
                        except Exception as exc:  # pragma: no cover - defensive
                            worker_results.append({
                                "candidate_id": str(arm["candidate_id"]),
                                "decisions": [],
                                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                            })
            worker_results.sort(key=lambda item: str(item.get("candidate_id") or ""))
            if len(worker_results) != len(active_arms):
                session_success = False

            # Stable candidate/event/session order is the sole write order.
            for result in worker_results:
                candidate_id = str(result.get("candidate_id") or "")
                if result.get("error"):
                    candidate_errors[candidate_id] = str(result["error"])
                    authorizing_errors[candidate_id] = str(result["error"])
                    failed_arms.add(candidate_id)
                    session_success = False
                    continue
                decisions = sorted(result.get("decisions") or [], key=lambda item: (
                    str(item.get("event_key") or ""),
                    str(item.get("session_date") or ""),
                    str(item.get("symbol") or "")))
                for decision in decisions:
                    payload = decision.get("payload") or {}
                    inserted = self.store.decision(
                        candidate_id=candidate_id,
                        event_key=str(decision.get("event_key") or ""),
                        session_date=str(decision.get("session_date") or ""),
                        symbol=str(decision.get("symbol") or ""),
                        kind=str(decision.get("kind") or "no_data"),
                        reason=decision.get("reason"),
                        payload=payload,
                        max_decisions=self.config.max_decisions)
                    if inserted and decision.get("plan") is not None:
                        self.store.virtual_open(
                            candidate_id=candidate_id,
                            decision_id=_digest({
                                "candidate_id": candidate_id,
                                "event_key": str(decision.get("event_key") or "")}),
                            symbol=str(decision.get("symbol") or ""),
                            plan=decision["plan"])
                if is_replay_only:
                    replay_decisions[candidate_id] = decisions

            # Replay is parent-only and runs before the next session barrier.
            session_bars, session_quotes, session_options = frozen_session_inputs[session]
            for result in worker_results:
                candidate_id = str(result.get("candidate_id") or "")
                if result.get("error"):
                    continue
                try:
                    rows = self.store.decisions(candidate_id)
                    if is_replay_only and candidate_id in replay_decisions:
                        current_rows = [{
                            "candidate_id": candidate_id,
                            "event_key": str(item.get("event_key") or ""),
                            "session_date": str(item.get("session_date") or ""),
                            "symbol": str(item.get("symbol") or ""),
                            "kind": str(item.get("kind") or "no_data"),
                            "reason": item.get("reason"),
                            "payload_json": _json(item.get("payload") or {}),
                        } for item in replay_decisions[candidate_id]]
                        rows = [row for row in rows
                                if row.get("session_date") != session]
                        rows.extend(current_rows)
                    complete = self._replay(
                        arm_by_id[candidate_id], session, session_bars,
                        session_quotes, rows, session_options,
                        replay_identity=replay_identity)
                    if complete is not True:
                        session_success = False
                except Exception as exc:
                    message = f"{type(exc).__name__}: {str(exc)[:240]}"
                    candidate_errors[candidate_id] = message
                    authorizing_errors[candidate_id] = message
                    failed_arms.add(candidate_id)
                    session_success = False
            if not session_success:
                replay_blocked = True
        prune = self.store.prune()
        replay_quarantine = self.store.replay_quarantine()
        pending_repairs = [
            dict(detail) for detail in replay_quarantine.values()
            if isinstance(detail, Mapping) and detail.get("status") in {
                "quarantined", "overflow"}
        ]
        pending_repairs.sort(key=lambda item: (
            str(item.get("session_date") or ""),
            str(item.get("candidate_id") or "")))

        # Advance only after every loaded event belongs to a session whose
        # complete replay and durable evidence write succeeded.  ``events``
        # uses an inclusive SQL floor, so nextafter makes the boundary strict:
        # a later candidate cannot repeatedly reload the final completed row.
        # Any unresolved worker/replay/quarantine state leaves the floor in
        # place for a deterministic retry; immutable evidence is never pruned.
        floor_eligible = bool(
            frozen_session_events and not replay_blocked and
            not authorizing_errors and not failed_arms and
            not invalid_sessions and not unknown_quarantine and
            not pending_repairs)
        if floor_eligible:
            loaded_sessions = {row_session(row) for row in events}
            processed_sessions = set(frozen_session_events)
            inserted_at_values = [
                float(row.get("inserted_at")) for row in events
                if row_session(row) in processed_sessions and
                _finite(row.get("inserted_at")) is not None
            ]
            floor_eligible = bool(
                loaded_sessions == processed_sessions and inserted_at_values)
        if floor_eligible:
            completed_floor = math.nextafter(max(inserted_at_values), math.inf)
            current_floor = float(forward_floor or 0.0)
            if completed_floor > current_floor:
                self.store.save_forward_event_floor(completed_floor)
                forward_floor = completed_floor
                manifest["source_watermark"]["forward_event_floor"] = completed_floor
                manifest["event_watermark"]["forward_event_floor"] = completed_floor
                # This second immutable body records the post-replay boundary;
                # the pre-dispatch manifest remains addressable by its digest.
                manifest_digest = self.store.save_manifest(manifest)
        catalog = self.store.session_catalog()
        stale_tail = {
            "status": "blocked" if (
                invalid_sessions or unknown_quarantine or pending_repairs) else "clear",
            "sessions": sorted(invalid_sessions),
            "unknown_events": bool(unknown_quarantine),
            "quarantine_overflow": bool(quarantine_overflow),
            "invalid_events": int(invalid_events),
            "replay_repairs_required": len(pending_repairs),
            "replay_quarantine_sessions": sorted({
                str(item.get("session_date")) for item in pending_repairs
                if item.get("session_date")}),
            "replay_quarantine": pending_repairs[-64:],
            "authoritative_catalog_sessions": sorted(
                str(session) for session, detail in catalog.items()
                if isinstance(detail, Mapping)
                and str(detail.get("source") or "") == "recorder_alpaca_calendar")[-64:],
        }
        diagnostic_candidate_ids = {
            str(row.get("candidate_id") or "")
            for row in self.store.candidates()
            if self.store._candidate_row_is_diagnostic(
                str(row.get("candidate_id") or ""), row.get("config_json"),
                row.get("proof_json"))
        }
        authorizing_decisions = [
            row for row in self.store.decisions()
            if (str(row.get("candidate_id") or "") not in diagnostic_candidate_ids
                and not is_diagnostic_candidate(
                    str(row.get("candidate_id") or "")))]
        signal_dispositions = _signal_dispositions(authorizing_decisions)
        calibration_status = self._calibration_status()
        stale_tail["signal_dispositions"] = signal_dispositions
        stale_tail["stress_calibration"] = calibration_status
        # Surface the latest per-candidate capacity summaries without copying
        # raw account rows into the heartbeat.  Replay details are already
        # bounded at write time; retain only a deterministic candidate/session
        # tail for the operational result.
        capacity: list[dict[str, Any]] = []
        for metadata in self.store.replay_metadata():
            if (str(metadata.get("candidate_id") or "") in diagnostic_candidate_ids or
                    is_diagnostic_candidate(str(metadata.get("candidate_id") or ""))):
                continue
            details = metadata.get("details")
            summary = details.get("opportunity_capacity") if isinstance(details, Mapping) else None
            if not isinstance(summary, Mapping):
                continue
            capacity.append({
                "candidate_id": str(metadata.get("candidate_id") or ""),
                "session_date": str(metadata.get("session_date") or ""),
                "vehicle": str(metadata.get("vehicle") or ""),
                **dict(summary),
            })
        capacity = sorted(capacity, key=lambda item: (
            item["candidate_id"], item["session_date"]))[-64:]
        diagnostic_coverage = self._diagnostic_coverage(
            diagnostic_cohort, diagnostic_activation,
            this_poll_decisions=int(diagnostic_result.get(
                "this_poll_decisions") or 0),
            preactivation_rejections=int(diagnostic_result.get(
                "provenance_rejections") or 0),
            poll_duration_seconds=time.monotonic() - poll_started)
        return {"candidates": len(candidates),
                "authorizing_candidates": len(candidates),
                "diagnostic_candidates": len(diagnostic_arms),
                "ingested_events": ingested,
                "events": len(events), "decisions": self.store.decision_count(),
                "conflicts": conflicts, "invalid_events": invalid_events,
                "manifest_digest": manifest_digest,
                "candidate_errors": dict(sorted(candidate_errors.items())),
                "skipped_recovery_bytes": skipped_recovery_bytes,
                "quarantine_through_session": quarantine_through,
                "forward_event_floor": float(forward_floor or 0.0),
                "stale_tail": stale_tail,
                "replay_quarantine": pending_repairs[-64:],
                "opportunity_capacity": capacity,
                "signal_dispositions": signal_dispositions,
                "stress_calibration": calibration_status,
                "diagnostic_shadow": diagnostic_coverage,
                "poll_duration_seconds": diagnostic_coverage[
                    "poll_duration_seconds"],
                "source_lag_seconds": diagnostic_coverage[
                    "source_lag_seconds"],
                **prune}


def run_shadow_once(config: ShadowConfig) -> dict[str, Any]:
    """Convenience entrypoint used by operations and tests."""
    return ShadowRunner(config).run_once()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("runtime/research/recorded/data.csv"))
    parser.add_argument("--edge-db", type=Path, default=Path("runtime/research/edge_lab.sqlite3"))
    parser.add_argument("--shadow-db", type=Path, default=Path("runtime/research/shadow.sqlite3"))
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "config.yaml")
    parser.add_argument("--diagnostic", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--once", action="store_true", help="ingest and evaluate one cycle")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument(
        "--diagnostic-session-max-events", type=int,
        default=DEFAULT_DIAGNOSTIC_SESSION_MAX_EVENTS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime_config = (load_runtime_config(args.config)
                      if args.diagnostic else None)
    cfg = ShadowConfig(corpus_path=args.corpus, edge_db=args.edge_db,
                       shadow_db=args.shadow_db, poll_seconds=args.interval,
                       max_workers=args.max_workers,
                       diagnostic_session_max_events=(
                           args.diagnostic_session_max_events),
                       diagnostic=args.diagnostic,
                       runtime_config=runtime_config,
                       runtime_config_path=args.config)
    interval = cfg.poll_seconds
    # Anchor before work begins so request duration is part of the configured
    # period instead of being added to it.
    next_tick: float | None = time.monotonic()
    while True:
        try:
            print(json.dumps(run_shadow_once(cfg), sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
            if args.once:
                return 1
            # Failure retry pacing remains completion-relative: wait at least
            # one configured interval, then start a fresh cadence anchor.
            time.sleep(interval)
            next_tick = time.monotonic()
            continue
        if args.once:
            return 0
        now = time.monotonic()
        next_tick = _next_shadow_cadence_deadline(
            next_tick, now, interval)
        time.sleep(max(0.0, next_tick - time.monotonic()))


__all__ = [
    "DEFAULT_DIAGNOSTIC_SESSION_MAX_EVENTS", "DEFAULT_EQUITY",
    "DEFAULT_MAX_WORKERS", "DEFAULT_RETENTION_DAYS", "InputConflict",
    "_opportunity_capacity", "_signal_dispositions", "REPLAY_QUARANTINE_META_KEY",
    "SESSION_CATALOG_META_KEY", "REPLAY_QUARANTINE_OVERFLOW_KEY",
    "ShadowConfig", "ShadowError",
    "ShadowRunner", "ShadowStore", "run_shadow_once", "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
