"""Bounded subprocess output capture for the research scheduler."""

from __future__ import annotations

import io
import json
import math
import threading
import time
from datetime import datetime, timezone
from typing import Mapping


READINESS_SCHEMA = "research-readiness.v1"
READINESS_MAX_HORIZON_SECONDS = 90 * 86400.0
_READINESS_STATES = frozenset({"unknown", "pending", "ready", "blocked"})
_READINESS_FIELDS = frozenset({
    "schema", "state", "reason", "recorded_sessions",
    "heldout_min_sessions", "shadow_min_sessions", "heldout_fraction",
    "shadow_fraction", "required_sessions", "sessions_remaining",
    "progress_age_seconds", "progress_rate_sessions_per_day", "eta_ts",
    "deadline_ts", "updated_ts", "minimums", "fractions",
    "recorded_session_count", "qualification_min_sessions",
    "observed_session_rate_per_day", "qualification_fraction",
    "development_fraction", "offline_required_sessions",
    "shadow_tail_sessions",
})


# Progress is deliberately a small, closed vocabulary.  The research child
# may add phases internally, but only these operational labels are allowed to
# cross the scheduler/dashboard boundary.  Keeping this contract here also
# means malformed child output can never become an unbounded status payload.
RESEARCH_PROGRESS_PHASES = frozenset({
    "backtest", "bootstrap", "startup", "resolve", "record", "recording", "validate",
    "validation", "discover", "discovery", "factory", "shadow",
    "shadow-ingest", "shadow_ingest", "trial", "review", "report",
    "cleanup", "complete", "completed", "preparing", "diagnosing",
    "evaluating", "aggregating", "persisting",
})
RESEARCH_PROGRESS_UNITS = frozenset({
    "accounts", "bars", "batches", "candidates", "cycles", "days",
    "edges", "files", "hypotheses", "items", "opportunities", "records",
    "reports", "reviews", "rows", "steps", "symbols", "tasks", "trades",
    "vehicles",
})
RESEARCH_PROGRESS_VEHICLES = frozenset({"equity", "option", "both"})
_RESEARCH_PROGRESS_FIELDS = frozenset({
    "schema", "phase", "unit", "vehicle", "done", "total", "updated_ts",
})


def _readiness_number(value: object, *, minimum: float = 0.0,
                      maximum: float = READINESS_MAX_HORIZON_SECONDS) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and minimum <= number <= maximum else None


def _readiness_int(value: object, *, maximum: int = 10_000_000) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= maximum and number == value else None


def structured_research_readiness(payload: object) -> dict | None:
    """Validate one bounded additive ``research-readiness.v1`` event."""
    if not isinstance(payload, dict) or payload.get("schema") != READINESS_SCHEMA:
        return None
    if not set(payload).issubset(_READINESS_FIELDS):
        return None
    state = str(payload.get("state") or "unknown").lower()
    if state not in _READINESS_STATES:
        return None
    result: dict[str, object] = {"schema": READINESS_SCHEMA, "state": state,
                                 "reason": str(payload.get("reason") or "")[:240]}
    nested_minimums = payload.get("minimums")
    if isinstance(nested_minimums, Mapping):
        for lane, key in (("heldout", "heldout_min_sessions"),
                          ("shadow", "shadow_min_sessions")):
            item = nested_minimums.get(lane)
            item = item.get("sessions") if isinstance(item, Mapping) else item
            if payload.get(key) is None and _readiness_int(item) is not None:
                result[key] = _readiness_int(item)
    nested_fractions = payload.get("fractions")
    if isinstance(nested_fractions, Mapping):
        for lane, key in (("heldout", "heldout_fraction"),
                          ("shadow", "shadow_fraction")):
            item = nested_fractions.get(lane)
            if payload.get(key) is None and _readiness_number(item, maximum=1.0):
                result[key] = _readiness_number(item, maximum=1.0)
    for key in ("recorded_sessions", "heldout_min_sessions",
                "shadow_min_sessions", "required_sessions",
                "sessions_remaining", "qualification_min_sessions"):
        raw_value = (payload.get("recorded_session_count")
                     if key == "recorded_sessions" and payload.get(key) is None
                     else payload.get(key))
        value = _readiness_int(raw_value)
        if value is not None:
            result[key] = value
    for key in ("offline_required_sessions", "shadow_tail_sessions"):
        value = _readiness_int(payload.get(key))
        if value is not None:
            result[key] = value
    for key in ("heldout_fraction", "shadow_fraction",
                "qualification_fraction", "development_fraction"):
        value = _readiness_number(payload.get(key), maximum=1.0)
        if value is not None and value > 0:
            result[key] = value
    for key in ("progress_age_seconds", "progress_rate_sessions_per_day",
                "observed_session_rate_per_day"):
        value = _readiness_number(payload.get(key))
        if value is not None:
            result[key] = value
    for key in ("eta_ts", "deadline_ts"):
        value = _readiness_number(payload.get(key), maximum=10_000_000_000.0)
        if value is not None:
            result[key] = value
    updated = payload.get("updated_ts")
    if _progress_timestamp(updated) is not None:
        result["updated_ts"] = updated
    return result


def derive_research_readiness(progress: Mapping[str, object] | None = None,
                              readiness: Mapping[str, object] | None = None,
                              *, now: float | None = None,
                              deadline_ts: object = None) -> dict:
    """Derive non-authorizing lane readiness from persisted minima/progress.

    The function intentionally has no audit-specific constants.  When a
    producer does not persist minima, it reads the current protocol floors;
    fractions and recorded counts, when present, are used to derive the total
    sessions required and the bounded ETA.  A missing progress count remains
    ``unknown`` rather than being guessed from a phase's one-step progress.
    """
    progress = progress if isinstance(progress, Mapping) else {}
    base = structured_research_readiness(readiness) if isinstance(readiness, Mapping) else None
    base = dict(base or {})
    try:
        from research.gates import (PROTOCOL_QUALIFICATION_MIN_SESSIONS,
                                    protocol_minimums)
        backtest_min = int(protocol_minimums("backtest").get("sessions", 0))
        shadow_min = int(protocol_minimums("shadow").get("sessions", 0))
        qualification_floor = int(PROTOCOL_QUALIFICATION_MIN_SESSIONS)
    except Exception:  # pragma: no cover - deployment can run without research deps
        backtest_min, shadow_min, qualification_floor = 0, 0, 0
    heldout_min = _readiness_int(base.get("heldout_min_sessions"))
    shadow_floor = _readiness_int(base.get("shadow_min_sessions"))
    qualification_min = _readiness_int(base.get("qualification_min_sessions"))
    heldout_min = backtest_min if heldout_min is None else heldout_min
    shadow_floor = shadow_min if shadow_floor is None else shadow_floor
    shadow_floor = (_readiness_int(base.get("shadow_tail_sessions"))
                    if _readiness_int(base.get("shadow_tail_sessions")) is not None
                    else shadow_floor)
    qualification_min = (qualification_floor if qualification_min is None
                         else qualification_min)
    heldout_fraction = _readiness_number(base.get("heldout_fraction"), maximum=1.0)
    shadow_fraction = _readiness_number(base.get("shadow_fraction"), maximum=1.0)
    offline_required = _readiness_int(base.get("offline_required_sessions"))
    if offline_required is None:
        offline_parts = []
        if heldout_fraction and heldout_fraction > 0:
            offline_parts.append(math.ceil(heldout_min / heldout_fraction))
        qualification_fraction = _readiness_number(
            base.get("qualification_fraction"), maximum=1.0)
        if qualification_fraction and qualification_fraction > 0:
            offline_parts.append(math.ceil(qualification_min / qualification_fraction))
        offline_required = max(offline_parts, default=heldout_min + qualification_min)
    required = _readiness_int(base.get("required_sessions"))
    if required is None:
        required = offline_required + shadow_floor
    recorded = _readiness_int(base.get("recorded_sessions"))
    if recorded is None and str(progress.get("unit") or "") in {"sessions", "days"}:
        recorded = _readiness_int(progress.get("done"))
    remaining = None if recorded is None else max(0, required - recorded)
    timestamp = (base.get("updated_ts") if base.get("updated_ts") is not None
                 else progress.get("updated_ts"))
    stamp = _progress_timestamp(timestamp)
    current = float(time.time() if now is None else now)
    age = None if stamp is None else max(0.0, current - stamp)
    rate = _readiness_number(
        base.get("progress_rate_sessions_per_day",
                 base.get("observed_session_rate_per_day")))
    eta = _readiness_number(base.get("eta_ts"), maximum=10_000_000_000.0)
    if eta is None and remaining is not None and rate and rate > 0:
        eta = current + remaining / rate * 86400.0
    horizon = current + READINESS_MAX_HORIZON_SECONDS
    if eta is not None:
        eta = min(max(current, eta), horizon)
    deadline = _readiness_number(
        base.get("deadline_ts", deadline_ts), maximum=10_000_000_000.0)
    if deadline is not None:
        deadline = min(max(current, deadline), horizon)
    state = str(base.get("state") or "").lower()
    if state not in _READINESS_STATES or state == "unknown":
        state = ("ready" if remaining == 0 and recorded is not None else
                 "pending" if remaining is not None else "unknown")
    reason = str(base.get("reason") or "")[:240]
    if not reason:
        reason = ("recorded sessions satisfy configured lane minimums"
                  if state == "ready" else
                  "recorded sessions below configured lane minimums"
                  if state == "pending" else
                  "recorded session progress is unavailable")
    result = {
        "schema": READINESS_SCHEMA, "state": state, "reason": reason,
        "recorded_sessions": recorded,
        "heldout_min_sessions": heldout_min,
        "qualification_min_sessions": qualification_min,
        "offline_required_sessions": offline_required,
        "shadow_tail_sessions": shadow_floor,
        "shadow_min_sessions": shadow_floor,
        "required_sessions": required,
        "sessions_remaining": remaining,
        "progress_age_seconds": age,
        "progress_rate_sessions_per_day": rate,
        "observed_session_rate_per_day": rate,
        "eta_ts": eta, "deadline_ts": deadline,
    }
    if heldout_fraction is not None:
        result["heldout_fraction"] = heldout_fraction
    if shadow_fraction is not None:
        result["shadow_fraction"] = shadow_fraction
    if base.get("qualification_fraction") is not None:
        result["qualification_fraction"] = base["qualification_fraction"]
    if base.get("development_fraction") is not None:
        result["development_fraction"] = base["development_fraction"]
    if timestamp is not None:
        result["updated_ts"] = timestamp
    return result


class _BoundedCapture:
    """Drain a subprocess stream while retaining only a bounded tail.

    Structured command results are recognized while streaming, so a large
    later report cannot evict the author/reviewer failure that preceded it.
    """

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self.tail = ""
        self.total_chars = 0
        self.structured_failures: list[dict] = []
        self.research_cycles: list[dict] = []
        self.research_progress: dict | None = None
        self.research_readiness: dict | None = None

    def feed(self, text: str) -> None:
        value = str(text)
        self.total_chars += len(value)
        self.tail = (self.tail + value)[-self.limit:]
        # ``_drain`` supplies one line at a time, while tests and embedders
        # may feed a chunk containing several NDJSON records.  Evaluate each
        # complete line without ever retaining a progress history.
        for line in value.splitlines() or [value]:
            candidate = line.strip()
            if not candidate.startswith("{"):
                continue
            try:
                payload = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            progress = structured_research_progress(payload)
            if progress is not None:
                previous = self.research_progress
                if (previous is None or
                        _progress_timestamp(progress["updated_ts"]) >=
                        _progress_timestamp(previous["updated_ts"])):
                    self.research_progress = progress
            readiness = structured_research_readiness(payload)
            if readiness is not None:
                previous = self.research_readiness
                current_ts = _progress_timestamp(
                    readiness.get("updated_ts")) or 0.0
                previous_ts = (_progress_timestamp(previous.get("updated_ts"))
                               if previous else None) or 0.0
                if previous is None or current_ts >= previous_ts:
                    self.research_readiness = readiness
            cycle = structured_research_cycle(payload)
            if cycle is not None and len(self.research_cycles) < 8:
                self.research_cycles.append(cycle)
            reason = structured_failure(payload)
            if reason is not None and len(self.structured_failures) < 32:
                self.structured_failures.append(reason)

    @property
    def truncated(self) -> bool:
        return self.total_chars > len(self.tail)


def structured_failure(payload: object) -> dict | None:
    """Translate author/review JSON results into operational failure truth."""
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status") or "").upper()
    if {"generation", "accepted_count"} <= set(payload):
        if status in {"FAILED", "NOTHING_ACCEPTED"}:
            return {
                "component": "authoring", "status": status,
                "attempt_id": payload.get("attempt_id"),
                "error": payload.get("error"),
            }
        return None
    if "max_reviews" in payload and {
            "reviewed", "retry_pending", "failed"} <= set(payload):
        retry_pending = int(payload.get("retry_pending") or 0)
        failed = int(payload.get("failed") or 0)
        if retry_pending or failed or status in {"RETRY_PENDING", "PARTIAL"}:
            return {
                "component": "review", "status": status,
                "reviewed": int(payload.get("reviewed") or 0),
                "retry_pending": retry_pending, "failed": failed,
            }
    return None


def structured_research_progress(payload: object) -> dict | None:
    """Return a validated compact ``research-progress.v1`` event.

    Progress events are newline-delimited JSON records emitted by the child.
    This parser intentionally rejects extra keys, nested values, booleans in
    numeric fields, unknown labels, and impossible counters.  The returned
    dictionary is a fresh bounded object suitable for durable status files.
    """
    if not isinstance(payload, dict):
        return None
    if set(payload) != _RESEARCH_PROGRESS_FIELDS:
        return None
    if payload.get("schema") != "research-progress.v1":
        return None
    phase = payload.get("phase")
    unit = payload.get("unit")
    vehicle = payload.get("vehicle")
    if (not isinstance(phase, str) or phase not in RESEARCH_PROGRESS_PHASES or
            not isinstance(unit, str) or unit not in RESEARCH_PROGRESS_UNITS or
            not isinstance(vehicle, str) or
            vehicle not in RESEARCH_PROGRESS_VEHICLES):
        return None
    done = payload.get("done")
    total = payload.get("total")
    if (type(done) is not int or type(total) is not int or done < 0 or
            total < 0 or done > total):
        return None
    updated_ts = payload.get("updated_ts")
    if _progress_timestamp(updated_ts) is None:
        return None
    return {
        "schema": "research-progress.v1", "phase": phase, "unit": unit,
        "vehicle": vehicle, "done": done, "total": total,
        "updated_ts": updated_ts,
    }


def structured_research_cycle(payload: object) -> dict | None:
    """Recognize the terminal JSON emitted by ``research-cycle.sh``.

    The scheduler must not infer a green result from a zero child exit alone:
    a valid corpus can complete without an eligible edge, and an empty corpus
    is an explicit no-data outcome.  Keep the payload small and predictable in
    status/history files while preserving its reason and per-vehicle outcomes.
    """
    if not isinstance(payload, dict):
        return None
    schema = str(payload.get("schema") or "")
    component = str(payload.get("component") or "")
    if schema != "research-cycle.v1" and component not in {
            "research-cycle", "research_cycle"}:
        return None
    status = str(payload.get("status") or "").lower()
    if status not in {"completed", "completed_no_edge", "no_data", "failed",
                      "search_exhausted", "llm_provider_failure"}:
        return None
    outcomes = payload.get("outcomes")
    if not isinstance(outcomes, list):
        outcomes = []
    raw_exit = payload.get("exit_code")
    try:
        exit_code = None if raw_exit is None else int(raw_exit)
    except (TypeError, ValueError):
        exit_code = None
    result = {
        "schema": schema or "research-cycle.v1",
        "status": status,
        "reason": str(payload.get("reason") or ""),
        "exit_code": exit_code,
        "outcomes": [str(item) for item in outcomes[:32]],
        "proofs": bool(payload.get("proofs")),
        "no_edge": bool(payload.get("no_edge")),
    }
    # These additive flags are emitted by newer cycle wrappers. Keep parsing
    # older payloads byte-compatible for callers that compare the bounded
    # dictionary exactly.
    for key in ("unevaluable", "search_exhausted", "llm_provider_failure"):
        if key in payload:
            result[key] = bool(payload.get(key))
    return result


def _drain(stream, capture: _BoundedCapture) -> None:
    try:
        for line in iter(stream.readline, ""):
            capture.feed(line)
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001 - process teardown is best effort
            pass


def _start_capture(stream, limit: int) -> tuple[_BoundedCapture, threading.Thread] | None:
    if not isinstance(stream, io.TextIOBase):
        return None
    capture = _BoundedCapture(limit)
    thread = threading.Thread(
        target=_drain, args=(stream, capture), daemon=True,
        name="research-output-drain")
    thread.start()
    return capture, thread


def _capture_detail(stdout: _BoundedCapture | None,
                    stderr: _BoundedCapture | None) -> dict:
    out = stdout or _BoundedCapture(1)
    err = stderr or _BoundedCapture(1)
    progress = _latest_research_progress(
        out.research_progress, err.research_progress)
    readiness = _latest_research_readiness(
        out.research_readiness, err.research_readiness)
    return {
        "stdout_tail": out.tail, "stderr_tail": err.tail,
        "stdout_chars": out.total_chars, "stderr_chars": err.total_chars,
        "stdout_truncated": out.truncated,
        "stderr_truncated": err.truncated,
        "structured_failures": [
            *out.structured_failures, *err.structured_failures],
        "research_cycles": [*out.research_cycles, *err.research_cycles],
        "research_progress": progress,
        "research_readiness": readiness,
    }


def _latest_research_progress(*progresses: dict | None) -> dict | None:
    """Select one latest progress record without retaining event history."""
    latest = None
    for progress in progresses:
        if progress is None:
            continue
        if latest is None or (_progress_timestamp(progress["updated_ts"]) >=
                              _progress_timestamp(latest["updated_ts"])):
            latest = progress
    return latest


def _latest_research_readiness(*readinesses: dict | None) -> dict | None:
    latest = None
    for value in readinesses:
        if value is None:
            continue
        if latest is None or (
                (_progress_timestamp(value.get("updated_ts")) or 0.0) >=
                (_progress_timestamp(latest.get("updated_ts")) or 0.0)):
            latest = value
    return latest


def _progress_timestamp(value: object) -> float | None:
    """Normalize an epoch or UTC ISO timestamp for latest-event ordering."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            normalized = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return normalized if math.isfinite(normalized) and normalized >= 0 else None
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    normalized = parsed.astimezone(timezone.utc).timestamp()
    return normalized if math.isfinite(normalized) and normalized >= 0 else None
