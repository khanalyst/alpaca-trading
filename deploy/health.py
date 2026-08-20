#!/usr/bin/env python3
"""Container health probes with machine-readable failure reasons."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

# ``python deploy/health.py ...`` sets ``sys.path[0]`` to ``deploy/`` rather
# than the repository root.  Add the root explicitly so recovery shells and
# Compose health checks do not depend on PYTHONPATH being preconfigured.
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deploy import load_config
from deploy.scheduler_output import (derive_research_readiness,
                                     structured_research_progress)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fresh(timestamp: object, max_age: float, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else float(now)) - float(timestamp)
    except (TypeError, ValueError):
        return False
    return -5 <= age <= float(max_age)


def trader(path: Path, max_age: float, *, now: float | None = None) -> dict:
    heartbeat = _read_json(path)
    status = str(heartbeat.get("status") or "missing")
    fresh = _fresh(heartbeat.get("updated_ts"), max_age, now)
    research_ok = not (
        heartbeat.get("research_expected") is True
        and (heartbeat.get("research_available") is not True
             or heartbeat.get("research_status") not in {"healthy", "disabled"}))
    reason = str(heartbeat.get("reason") or "").strip()
    # ``paused`` is a safe operator gate unless the payload explicitly
    # describes residual exposure or a failed/degraded operation.  Degraded
    # is never healthy: it is the runtime's assertion that safety could not
    # be proven (most importantly, an incomplete flatten).
    residual_risk = status == "degraded" or status == "failed" or any(
        marker in reason.lower()
        for marker in ("flatten", "residual", "incomplete", "failed", "error", "unavailable")
    )
    edge_gate_pause = (
        status == "paused"
        and reason == "validated_edge_required"
        and not residual_risk
    )
    operator_pause = (
        status in {"paused", "pausing"}
        and not residual_risk
        and not edge_gate_pause
    )
    classification = (
        "degraded_residual_risk" if residual_risk else
        "validated_edge_required" if edge_gate_pause else
        "operator_pause" if operator_pause else
        "healthy"
    )
    alert_kind = (
        "residual_risk" if residual_risk else
        "operator_pause" if operator_pause else
        None
    )
    ok = fresh and status in {"starting", "running", "paused", "pausing"} and research_ok and not residual_risk
    return {
        "ok": ok,
        "component": "trader",
        "status": status,
        "fresh": fresh,
        "research_available": heartbeat.get("research_available"),
        "research_status": heartbeat.get("research_status"),
        "reason": reason or None,
        "classification": classification,
        "pause_class": classification,
        "operator_pause": operator_pause,
        "edge_gate_pause": edge_gate_pause,
        "residual_risk": residual_risk,
        "alert": residual_risk,
        "alert_kind": alert_kind,
    }


def recorder(path: Path, max_age: float, *, now: float | None = None,
             configured_data_feed: str | None = None,
             configured_options_feed: str | None = None) -> dict:
    files = [item for item in path.rglob("*.csv") if item.is_file()]
    latest_csv = max((item.stat().st_mtime for item in files), default=None)
    # A deduplicated recorder cycle may append no corpus rows while still
    # advancing the durable sidecar.  Only the sidecar at the recorder root
    # is authoritative; nested files must not mask a stale recorder.
    index_path = path / ".recorder-index.json"
    index_write = index_path.stat().st_mtime if index_path.is_file() else None
    index = _read_json(index_path)
    attempt = _read_json(path / ".recorder-status.json")
    attempt_failed = attempt.get("status") == "failed"
    raw_coverage = index.get("bar_coverage")
    coverage = ({str(symbol): dict(value)
                 for symbol, value in raw_coverage.items()
                 if isinstance(value, dict)}
                if isinstance(raw_coverage, dict) else {})
    gap_symbols = sorted(
        symbol for symbol, value in coverage.items()
        if value.get("status") == "gap_observed")
    unobserved = sorted(
        symbol for symbol, value in coverage.items()
        if value.get("status") == "unobserved")
    gap_observations = 0
    for value in coverage.values():
        try:
            gap_observations += max(0, int(value.get("gap_observations") or 0))
        except (TypeError, ValueError):
            continue
    coverage_status = (
        "gap_observed" if gap_symbols else
        "unobserved" if unobserved else
        "covered" if coverage else
        "unknown"
    )
    writes = [timestamp for timestamp in (latest_csv, index_write)
              if timestamp is not None]
    latest = max(writes, default=None)
    fresh = _fresh(latest, max_age, now)
    return {
        "ok": bool(files) and fresh and not attempt_failed,
        "component": "recorder",
        "status": (str(attempt.get("failure_kind") or "failed")
                   if attempt_failed else
                   "recording" if files and fresh else "stale_or_empty"),
        "fresh": fresh,
        "series_files": len(files),
        "latest_write_ts": latest,
        "latest_csv_write_ts": latest_csv,
        "index_write_ts": index_write,
        "data_feed": index.get("data_feed"),
        "configured_data_feed": (attempt.get("data_feed") or
                                  configured_data_feed),
        "configured_options_feed": (attempt.get("options_feed") or
                                     configured_options_feed),
        "last_attempt_ts": attempt.get("updated_ts"),
        "last_error": attempt.get("error"),
        "failure_kind": attempt.get("failure_kind"),
        "retryable": attempt.get("retryable"),
        "probe": attempt.get("probe"),
        "coverage_status": coverage_status,
        "bar_gap_symbols": gap_symbols,
        "bar_unobserved_symbols": unobserved,
        "bar_gap_observations": gap_observations,
        "bar_coverage": coverage,
    }


def research(path: Path, max_age: float, *, now: float | None = None) -> dict:
    heartbeat = _read_json(path)
    status = str(heartbeat.get("status") or "missing")
    fresh = _fresh(heartbeat.get("updated_ts"), max_age, now)
    last_exit = heartbeat.get("last_exit_code")
    current = time.time() if now is None else float(now)
    deadline = heartbeat.get("deadline_ts")
    try:
        hung = status == "running" and deadline is not None and current > float(deadline)
    except (TypeError, ValueError):
        hung = status == "running"
    scheduler_operational = status in {"waiting", "running"}
    previous_cycle_degraded = last_exit not in {None, 0}
    waiting_after_no_data = (
        status == "waiting"
        and str(heartbeat.get("cycle_status") or "").lower() == "no_data"
        and last_exit == 2
    )
    # In both ``waiting`` and ``running``, ``last_exit_code`` describes the
    # previous completed cycle rather than the scheduler process now being
    # probed.  Preserve that result in the response, but judge current
    # scheduler liveness on its own fresh heartbeat/deadline.  Terminal states
    # still inherit the completed result.
    ok = fresh and not hung and status in {
        "waiting", "running", "completed", "completed_no_edge"} and (
        scheduler_operational or not previous_cycle_degraded)
    return {
        "ok": ok,
        "component": "research",
        "status": status,
        "fresh": fresh,
        "hung": hung,
        "job_id": heartbeat.get("job_id"),
        "started_ts": heartbeat.get("started_ts"),
        "completed_ts": heartbeat.get("completed_ts"),
        "last_exit_code": last_exit,
        "previous_cycle_degraded": previous_cycle_degraded,
        "waiting_after_no_data": waiting_after_no_data,
        "next_run_ts": heartbeat.get("next_run_ts"),
        "structured_failures": heartbeat.get("structured_failures") or [],
        # Keep the health response bounded even if an operator hand-edits a
        # status file; scheduler-produced values already satisfy this schema.
        "research_progress": structured_research_progress(
            heartbeat.get("research_progress")),
        "research_readiness": derive_research_readiness(
            structured_research_progress(heartbeat.get("research_progress")),
            heartbeat.get("research_readiness"), now=current,
            deadline_ts=heartbeat.get("deadline_ts")),
    }


def shadow(path: Path, max_age: float, *, now: float | None = None) -> dict:
    """Health of the broker-free forward shadow polling loop."""
    heartbeat = _read_json(path)
    status = str(heartbeat.get("status") or "missing")
    fresh = _fresh(heartbeat.get("updated_ts"), max_age, now)
    raw_error = heartbeat.get("last_error")
    last_error = (str(raw_error)[:500] if raw_error not in {None, ""} else None)
    return {
        "ok": fresh and status == "running",
        "component": "shadow",
        "status": status,
        "fresh": fresh,
        "last_error": last_error,
        "candidates": heartbeat.get("candidates"),
        "events": heartbeat.get("events"),
        "decisions": heartbeat.get("decisions"),
        "ingested_events": heartbeat.get("ingested_events"),
        "pruned_replay_diffs": heartbeat.get("pruned_replay_diffs"),
        "retention_days": heartbeat.get("retention_days"),
        "retention_floor_ts": heartbeat.get("retention_floor_ts"),
        "retention_gap_watermark": heartbeat.get("retention_gap_watermark"),
        "stale_tail": heartbeat.get("stale_tail"),
        "quarantine_through_session": heartbeat.get(
            "quarantine_through_session"),
        # Shadow capacity is diagnostic only.  Keep it bounded by forwarding
        # the already-capped summary emitted by the worker/ingester.
        "opportunity_capacity": heartbeat.get("opportunity_capacity") or
        heartbeat.get("capacity"),
    }


def watchdog(path: Path, max_age: float, *, now: float | None = None) -> dict:
    status_payload = _read_json(path)
    status = str(status_payload.get("status") or "missing")
    fresh = _fresh(status_payload.get("updated_ts"), max_age, now)
    flattened = status_payload.get("flattened")
    # An ``acted`` record is healthy only when flatten completion was
    # explicitly confirmed.  Older/malformed records and degraded/failed
    # records remain visible but are residual-risk alerts.
    incomplete = status == "acted" and flattened is not True
    residual_risk = status in {"degraded", "failed"} or incomplete
    alert_kind = "residual_risk" if residual_risk else None
    return {
        "ok": fresh and status in {"watching", "acted"} and not incomplete,
        "component": "watchdog",
        "status": status,
        "fresh": fresh,
        "reason": status_payload.get("reason"),
        "flattened": flattened,
        "classification": "degraded_residual_risk" if residual_risk else "healthy",
        "pause_class": "degraded_residual_risk" if residual_risk else "healthy",
        "residual_risk": residual_risk,
        "alert": residual_risk,
        "alert_kind": alert_kind,
    }


def dashboard(url: str, timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ok = response.status == 200 and payload.get("ok") is True
    except Exception:                                      # noqa: BLE001
        ok = False
    return {"ok": ok, "component": "dashboard",
            "status": "up" if ok else "unreachable"}


def _trader_path(args) -> Path:
    if args.path:
        return Path(args.path)
    raw = load_config(args.config)
    broker = raw.get("broker") if isinstance(raw.get("broker"), dict) else {}
    mode = str(raw.get("mode") or broker.get("mode") or "paper").lower()
    if mode not in {"paper", "live"}:
        raise ValueError("config mode must be paper or live")
    return Path(args.runtime_root) / mode / "heartbeat.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="component", required=True)

    item = sub.add_parser("trader")
    item.add_argument("--path")
    item.add_argument("--config", default="config.yaml")
    item.add_argument("--runtime-root", default="runtime")
    item.add_argument("--max-age", type=float, default=900)

    item = sub.add_parser("recorder")
    item.add_argument("--path", default="runtime/research/recorded")
    item.add_argument("--max-age", type=float, default=900)

    item = sub.add_parser("research")
    item.add_argument("--path", default="runtime/health/research.json")
    item.add_argument("--max-age", type=float, default=180)

    item = sub.add_parser("shadow")
    item.add_argument("--path", default="runtime/research/shadow-health.json")
    item.add_argument("--max-age", type=float, default=180)

    item = sub.add_parser("watchdog")
    item.add_argument("--path", default="runtime/health/watchdog.json")
    item.add_argument("--max-age", type=float, default=180)

    item = sub.add_parser("dashboard")
    item.add_argument("--url", default="http://127.0.0.1:8080/healthz")
    item.add_argument("--timeout", type=float, default=3)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.component == "trader":
            result = trader(_trader_path(args), args.max_age)
        elif args.component == "recorder":
            result = recorder(Path(args.path), args.max_age)
        elif args.component == "research":
            result = research(Path(args.path), args.max_age)
        elif args.component == "shadow":
            result = shadow(Path(args.path), args.max_age)
        elif args.component == "watchdog":
            result = watchdog(Path(args.path), args.max_age)
        else:
            result = dashboard(args.url, args.timeout)
    except Exception as exc:                               # noqa: BLE001
        result = {"ok": False, "component": args.component,
                  "status": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
