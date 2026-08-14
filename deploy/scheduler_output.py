"""Bounded subprocess output capture for the research scheduler."""

from __future__ import annotations

import io
import json
import math
import threading
from datetime import datetime, timezone


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
    if status not in {"completed", "completed_no_edge", "no_data", "failed"}:
        return None
    outcomes = payload.get("outcomes")
    if not isinstance(outcomes, list):
        outcomes = []
    raw_exit = payload.get("exit_code")
    try:
        exit_code = None if raw_exit is None else int(raw_exit)
    except (TypeError, ValueError):
        exit_code = None
    return {
        "schema": schema or "research-cycle.v1",
        "status": status,
        "reason": str(payload.get("reason") or ""),
        "exit_code": exit_code,
        "outcomes": [str(item) for item in outcomes[:32]],
        "proofs": bool(payload.get("proofs")),
        "no_edge": bool(payload.get("no_edge")),
    }


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
    return {
        "stdout_tail": out.tail, "stderr_tail": err.tail,
        "stdout_chars": out.total_chars, "stderr_chars": err.total_chars,
        "stdout_truncated": out.truncated,
        "stderr_truncated": err.truncated,
        "structured_failures": [
            *out.structured_failures, *err.structured_failures],
        "research_cycles": [*out.research_cycles, *err.research_cycles],
        "research_progress": progress,
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
