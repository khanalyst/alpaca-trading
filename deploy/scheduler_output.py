"""Bounded subprocess output capture for the research scheduler."""

from __future__ import annotations

import io
import json
import threading


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

    def feed(self, text: str) -> None:
        value = str(text)
        self.total_chars += len(value)
        self.tail = (self.tail + value)[-self.limit:]
        candidate = value.strip()
        if not candidate.startswith("{"):
            return
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            return
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
    return {
        "stdout_tail": out.tail, "stderr_tail": err.tail,
        "stdout_chars": out.total_chars, "stderr_chars": err.total_chars,
        "stdout_truncated": out.truncated,
        "stderr_truncated": err.truncated,
        "structured_failures": [
            *out.structured_failures, *err.structured_failures],
        "research_cycles": [*out.research_cycles, *err.research_cycles],
    }
