#!/usr/bin/env python3
"""Container health probes with machine-readable failure reasons."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

# ``python deploy/health.py ...`` sets ``sys.path[0]`` to ``deploy/`` rather
# than the repository root.  Add the root explicitly so recovery shells and
# Compose health checks do not depend on PYTHONPATH being preconfigured.
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deploy import load_config
from deploy.provenance import deployment_parity, deployment_provenance
from deploy.scheduler_output import (derive_research_readiness,
                                     structured_research_preflight,
                                     structured_research_progress)

MAX_RECORDER_INDEX_BYTES = 16 * 1024 * 1024
AUTHORIZING_MARKET_DATA_MAX_AGE_SECONDS = 30.0
NEW_YORK = ZoneInfo("America/New_York")


def _with_provenance(result: dict, payload: dict | None = None) -> dict:
    """Attach one bounded deployment identity to every health projection."""
    value = payload.get("provenance") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        value = deployment_provenance()
    result["provenance"] = value
    # Keep the explicit name available to API consumers that do not know the
    # shorter historical key yet.
    result["deployment_provenance"] = value
    return result


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


def _timestamp_epoch(value: object) -> float | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if value in (None, ""):
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _market_session_status(index: dict, now: float) -> str:
    current = datetime.fromtimestamp(float(now), timezone.utc)
    local = current.astimezone(NEW_YORK)
    calendar = index.get("session_calendar")
    record = (calendar.get(local.date().isoformat())
              if isinstance(calendar, dict) else None)
    if isinstance(record, dict):
        if (record.get("status") == "closed" and
                record.get("source") == "alpaca_calendar"):
            return "closed"
        opened = _timestamp_epoch(record.get("open"))
        closed = _timestamp_epoch(record.get("close"))
        if opened is not None and closed is not None and opened < closed:
            return "open" if opened <= now < closed else "closed"
    if local.weekday() >= 5:
        return "closed"
    return "unknown"


def _market_data_readiness(
        index: dict, *, symbols: list[str], selected_feed: str,
        configured_feed: str | None, now: float,
        index_migration_pending: bool) -> dict:
    raw = index.get("observation_watermarks")
    watermarks = raw if isinstance(raw, dict) else {}
    required = symbols or sorted(str(symbol).strip().upper()
                                 for symbol in watermarks if str(symbol).strip())
    observations: dict[str, dict] = {}
    missing_quotes: list[str] = []
    missing_bars: list[str] = []
    quote_ages: list[float] = []
    bar_ages: list[float] = []
    for symbol in required:
        record = watermarks.get(symbol)
        record = record if isinstance(record, dict) else {}
        quote_epoch = _timestamp_epoch(record.get("quote"))
        bar_epoch = _timestamp_epoch(record.get("bar"))
        quote_age = None if quote_epoch is None else float(now) - quote_epoch
        bar_age = None if bar_epoch is None else float(now) - bar_epoch
        if quote_age is None:
            missing_quotes.append(symbol)
        else:
            quote_ages.append(quote_age)
        if bar_age is None:
            missing_bars.append(symbol)
        else:
            bar_ages.append(bar_age)
        observations[symbol] = {
            "quote_watermark": record.get("quote"),
            "bar_watermark": record.get("bar"),
            "quote_age_seconds": quote_age,
            "bar_age_seconds": bar_age,
            "quote_fresh": (quote_age is not None and
                            -5.0 <= quote_age <=
                            AUTHORIZING_MARKET_DATA_MAX_AGE_SECONDS),
            "bar_fresh": (bar_age is not None and
                          -5.0 <= bar_age <=
                          AUTHORIZING_MARKET_DATA_MAX_AGE_SECONDS),
        }

    configured = str(configured_feed or "").strip().lower().replace("-", "_")
    selected = str(selected_feed or "").strip().lower().replace("-", "_")
    session_status = _market_session_status(index, now)
    stale_quotes = sorted(
        symbol for symbol, value in observations.items()
        if value["quote_age_seconds"] is not None and
        not value["quote_fresh"])
    stale_bars = sorted(
        symbol for symbol, value in observations.items()
        if value["bar_age_seconds"] is not None and
        not value["bar_fresh"])
    if session_status == "closed":
        status, reason = "market_closed", "market_closed"
    elif index_migration_pending:
        status, reason = "unknown", "recorder_index_migration_pending"
    elif not required or not watermarks:
        status, reason = "unknown", "observation_watermarks_missing"
    elif not selected:
        status, reason = "unknown", "recorded_feed_unknown"
    elif configured and selected != configured:
        status, reason = "feed_mismatch", "configured_feed_mismatch"
    elif missing_quotes:
        status = "unknown"
        reason = "quote_watermarks_missing:" + ",".join(sorted(missing_quotes))
    elif stale_quotes:
        status = "stale"
        reason = "quote_observations_stale:" + ",".join(stale_quotes)
    elif missing_bars:
        status = "unknown"
        reason = "bar_watermarks_missing:" + ",".join(sorted(missing_bars))
    elif stale_bars:
        status = "stale"
        reason = "bar_observations_stale:" + ",".join(stale_bars)
    else:
        status, reason = "ready", "fresh_exact_feed_quotes_and_bars"
    return {
        "market_data_ready": status == "ready",
        "market_data_fresh": status == "ready",
        "market_data_freshness_status": status,
        "market_data_reason": reason,
        "market_session_status": session_status,
        "authorization_max_age_seconds":
            AUTHORIZING_MARKET_DATA_MAX_AGE_SECONDS,
        "observation_ages": observations,
        "aggregate_observation_ages": {
            "quote_age_seconds": max(quote_ages) if quote_ages else None,
            "bar_age_seconds": max(bar_ages) if bar_ages else None,
        },
    }


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
    return _with_provenance({
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
    }, heartbeat)


def recorder(path: Path, max_age: float, *, now: float | None = None,
             configured_data_feed: str | None = None,
             configured_options_feed: str | None = None,
             configured_symbols: list[str] | tuple[str, ...] | None = None,
             strict_bar_feeds: str | None = None) -> dict:
    files = [item for item in path.rglob("*.csv") if item.is_file()]
    latest_csv = max((item.stat().st_mtime for item in files), default=None)
    # A deduplicated recorder cycle may append no corpus rows while still
    # advancing the durable sidecar.  Only the sidecar at the recorder root
    # is authoritative; nested files must not mask a stale recorder.
    index_path = path / ".recorder-index.json"
    index_write = index_path.stat().st_mtime if index_path.is_file() else None
    # A legacy recorder index can contain more than a million recent quote keys.
    # Health only needs compact metadata and must not compete with migration for
    # the recorder's cgroup, so defer decoding until the recorder rewrites it.
    index_oversized = bool(
        index_path.is_file() and
        index_path.stat().st_size > MAX_RECORDER_INDEX_BYTES)
    index = {} if index_oversized else _read_json(index_path)
    attempt = _read_json(path / ".recorder-status.json")
    try:
        attempt_ts = float(attempt.get("updated_ts"))
    except (TypeError, ValueError):
        attempt_ts = None
    # A stale retry failure must not mask later durable catch-up progress.  The
    # recorder writes its compact index after each successful chunk; a failure
    # in the current attempt is written afterwards and therefore remains newer.
    failure_superseded = bool(
        attempt.get("status") == "failed" and
        attempt_ts is not None and index_write is not None and
        index_write > attempt_ts)
    attempt_failed = (
        attempt.get("status") == "failed" and not failure_superseded)
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
    current = time.time() if now is None else float(now)
    activity = max([timestamp for timestamp in (latest, attempt_ts)
                    if timestamp is not None], default=None)
    fresh = _fresh(activity, max_age, current)
    configured = [str(symbol).strip().upper() for symbol in
                  (configured_symbols or index.get("configured_symbols") or ())
                  if str(symbol).strip()]
    configured = sorted(set(configured))
    required_unobserved = [symbol for symbol in configured
                           if symbol not in coverage or
                           coverage[symbol].get("status") == "unobserved" or
                           not coverage[symbol].get("last_bar")]
    strict_raw = (strict_bar_feeds if strict_bar_feeds is not None else
                  os.getenv("ALPACA_RECORDER_STRICT_BAR_FEEDS", ""))
    strict = {item.strip().lower().replace("-", "_")
              for item in str(strict_raw).split(",") if item.strip()}
    selected_feed = str(index.get("data_feed") or
                        attempt.get("data_feed") or "").strip().lower()
    market_readiness = _market_data_readiness(
        index, symbols=configured, selected_feed=selected_feed,
        configured_feed=configured_data_feed, now=current,
        index_migration_pending=index_oversized)
    closed_no_data_failure = bool(
        attempt_failed and
        market_readiness["market_session_status"] == "closed" and
        attempt.get("failure_kind") == "market_data_request_failed" and
        "no point-in-time bars or quotes" in str(attempt.get("error") or ""))
    blocking_attempt_failure = attempt_failed and not closed_no_data_failure
    strict_coverage = ("*" in strict or selected_feed in strict or
                       any(value.get("policy") == "strict"
                           for value in coverage.values()))
    coverage_failures = sorted(set(gap_symbols + required_unobserved)) \
        if strict_coverage else []
    coverage_reason = None
    if coverage_failures:
        details = []
        if gap_symbols:
            details.append("gap_observed=" + ",".join(gap_symbols))
        if required_unobserved:
            details.append("unobserved=" + ",".join(required_unobserved))
        coverage_reason = "strict_bar_coverage_failed: " + "; ".join(details)
    service_liveness_ok = fresh and (bool(files) or closed_no_data_failure)
    result = {
        "ok": (service_liveness_ok and not blocking_attempt_failure and
               not coverage_failures),
        "component": "recorder",
        "status": ("recording_market_closed"
                   if closed_no_data_failure else
                   str(attempt.get("failure_kind") or "failed")
                   if blocking_attempt_failure else
                   "recording" if files and fresh else "stale_or_empty"),
        "fresh": fresh,
        "service_liveness": {
            "ok": service_liveness_ok,
            "status": "alive" if service_liveness_ok else "stale_or_empty",
            "latest_activity_ts": activity,
        },
        "series_files": len(files),
        "latest_write_ts": latest,
        "latest_csv_write_ts": latest_csv,
        "index_write_ts": index_write,
        "index_migration_pending": index_oversized,
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
        "configured_symbols": configured,
        "strict_bar_policy": strict_coverage,
        "bar_coverage_failures": coverage_failures,
        "coverage_reason": coverage_reason,
        **market_readiness,
    }
    if coverage_reason:
        result["reason"] = coverage_reason
        result["status"] = "degraded_bar_coverage"
    return _with_provenance(result, attempt)


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
    cycle = heartbeat.get("research_cycle")
    preflight = structured_research_preflight(
        heartbeat.get("research_preflight"))
    if preflight is None and isinstance(cycle, dict):
        preflight = structured_research_preflight(cycle.get("preflight"))
    scheduler_liveness_ok = fresh and not hung and status in {
        "waiting", "running", "completed", "completed_no_edge", "failed",
        "no_data", "unevaluable", "search_exhausted",
        "llm_provider_failure"}
    terminal_status = str(cycle.get("status") if isinstance(cycle, dict)
                          else heartbeat.get("cycle_status") or status).lower()
    explicit_evidence = (cycle.get("evidence_available")
                         if isinstance(cycle, dict) else
                         heartbeat.get("evidence_available"))
    if isinstance(explicit_evidence, bool):
        evidence_available = explicit_evidence
    elif terminal_status in {"failed", "no_data", "unevaluable",
                             "search_exhausted", "llm_provider_failure"}:
        evidence_available = False
    elif isinstance(cycle, dict):
        # Completed/no-edge is a valid negative observation; a completed cycle
        # with no proof, no-edge, and no outcomes is not evidence at all.
        evidence_available = bool(
            cycle.get("proofs") or cycle.get("no_edge") or
            cycle.get("outcomes") or terminal_status == "completed_no_edge")
    else:
        evidence_available = terminal_status in {"completed", "completed_no_edge"}
    readiness = derive_research_readiness(
        structured_research_progress(heartbeat.get("research_progress")),
        heartbeat.get("research_readiness"), now=current,
        deadline_ts=heartbeat.get("deadline_ts"))
    readiness_state = str(readiness.get("state") or "unknown")
    readiness_ok = readiness_state in {"pending", "ready"}
    cycle_ok = not previous_cycle_degraded and evidence_available
    # The Compose/container probe answers whether the scheduler service is
    # alive.  Research evidence and readiness are separate non-authorizing
    # diagnostics: a fresh waiting scheduler before its first cycle is healthy
    # as a process, while failed/no-data/unevaluable cycles remain degraded in
    # their own fields without declaring the scheduler dead.
    ok = scheduler_liveness_ok
    research_evidence_status = "available" if evidence_available else "unavailable"
    research_readiness_status = "ready" if readiness_ok else readiness_state
    research_status = ("healthy" if cycle_ok and readiness_ok else "degraded")
    result = {
        "ok": ok,
        "component": "research",
        "status": status,
        "health_status": "healthy" if ok else "degraded",
        "research_status": research_status,
        "fresh": fresh,
        "hung": hung,
        "job_id": heartbeat.get("job_id"),
        "started_ts": heartbeat.get("started_ts"),
        "completed_ts": heartbeat.get("completed_ts"),
        "last_exit_code": last_exit,
        "previous_cycle_degraded": previous_cycle_degraded,
        "previous_cycle_failed": previous_cycle_degraded,
        "scheduler_operational": scheduler_operational,
        "scheduler_liveness": {
            "ok": scheduler_liveness_ok,
            "status": status,
            "fresh": fresh,
            "hung": hung,
        },
        "cycle_status": terminal_status,
        "cycle_ok": cycle_ok,
        "evidence_available": evidence_available,
        "research_evidence_status": research_evidence_status,
        "readiness_ok": readiness_ok,
        "research_readiness_status": research_readiness_status,
        "waiting_after_no_data": waiting_after_no_data,
        "next_run_ts": heartbeat.get("next_run_ts"),
        "structured_failures": heartbeat.get("structured_failures") or [],
        # A transient provider outage is explicitly non-authorizing evidence:
        # deterministic research may continue, but operators must see that
        # the model lane was degraded in the terminal cycle and history.
        "research_preflight": preflight,
        "provider_preflight_status": (preflight.get("status")
                                       if preflight else None),
        "provider_preflight_degraded": bool(
            preflight and preflight.get("status") == "degraded"),
        # Keep the health response bounded even if an operator hand-edits a
        # status file; scheduler-produced values already satisfy this schema.
        "research_progress": structured_research_progress(
            heartbeat.get("research_progress")),
        "research_readiness": readiness,
    }
    if not scheduler_liveness_ok:
        result["reason"] = "research scheduler is stale or hung"
    elif status == "waiting" and cycle is None and last_exit is None:
        result["reason"] = "research scheduler waiting for first cycle"
    elif previous_cycle_degraded:
        result["reason"] = "previous research cycle failed"
    elif not evidence_available:
        result["reason"] = "latest research cycle produced no usable evidence"
    elif not readiness_ok:
        result["reason"] = f"research readiness is {readiness_state}"
    return _with_provenance(result, heartbeat)


def shadow(path: Path, max_age: float, *, now: float | None = None) -> dict:
    """Health of the broker-free forward shadow polling loop."""
    heartbeat = _read_json(path)
    status = str(heartbeat.get("status") or "missing")
    fresh = _fresh(heartbeat.get("updated_ts"), max_age, now)
    raw_error = heartbeat.get("last_error")
    last_error = (str(raw_error)[:500] if raw_error not in {None, ""} else None)
    return _with_provenance({
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
    }, heartbeat)


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
    return _with_provenance({
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
    }, status_payload)


def dashboard(url: str, timeout: float = 3.0) -> dict:
    payload = {}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ok = response.status == 200 and payload.get("ok") is True
    except Exception:                                      # noqa: BLE001
        ok = False
    return _with_provenance({"ok": ok, "component": "dashboard",
                             "status": "up" if ok else "unreachable"},
                            payload if isinstance(payload, dict) else None)


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
    item.add_argument("--config", default=None)

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

    item = sub.add_parser("parity")
    item.add_argument("paths", nargs="+",
                      help="health/status JSON files from services to compare")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.component == "trader":
            result = trader(_trader_path(args), args.max_age)
        elif args.component == "recorder":
            config_symbols = None
            config_feed = config_options = None
            if args.config:
                raw = load_config(args.config)
                universe = raw.get("universe") if isinstance(raw, dict) else {}
                broker = raw.get("broker") if isinstance(raw, dict) else {}
                config_symbols = (universe or {}).get("symbols") or []
                config_feed = (broker or {}).get("data_feed")
                config_options = (broker or {}).get("options_feed")
            result = recorder(
                Path(args.path), args.max_age,
                configured_symbols=config_symbols,
                configured_data_feed=config_feed,
                configured_options_feed=config_options)
        elif args.component == "research":
            result = research(Path(args.path), args.max_age)
        elif args.component == "shadow":
            result = shadow(Path(args.path), args.max_age)
        elif args.component == "watchdog":
            result = watchdog(Path(args.path), args.max_age)
        elif args.component == "dashboard":
            result = dashboard(args.url, args.timeout)
        else:
            records = []
            for raw_path in args.paths:
                payload = _read_json(Path(raw_path))
                payload.setdefault("component", Path(raw_path).stem)
                records.append(payload)
            result = deployment_parity(records)
    except Exception as exc:                               # noqa: BLE001
        result = {"ok": False, "component": args.component,
                  "status": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
