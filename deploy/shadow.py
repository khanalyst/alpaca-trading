#!/usr/bin/env python3
"""Operations entrypoint for the broker-free real-time shadow lane."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.live_shadow import (DEFAULT_DIAGNOSTIC_SESSION_MAX_EVENTS,
                                  DEFAULT_MAX_WORKERS, DEFAULT_RETENTION_DAYS,
                                  ShadowConfig, ShadowRunner,
                                  _next_shadow_cadence_deadline)  # noqa: E402
from agent.config import load_config as load_runtime_config  # noqa: E402


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path,
                   default=Path("runtime/research/recorded/data.csv"))
    p.add_argument("--edge-db", type=Path,
                   default=Path("runtime/research/edge_lab.sqlite3"))
    p.add_argument("--shadow-db", type=Path,
                   default=Path("runtime/research/shadow.sqlite3"))
    p.add_argument("--config", type=Path, default=ROOT / "config.yaml",
                   help="mounted runtime config used unchanged for diagnostic policy")
    p.add_argument("--diagnostic", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="run the fixed 24-arm non-authorizing family cohort")
    p.add_argument("--health-file", type=Path,
                   help="durable polling heartbeat (defaults beside shadow DB)")
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--once", action="store_true",
                   help="run one bounded ingest/evaluation cycle and exit")
    p.add_argument("--max-candidates", type=int, default=32)
    p.add_argument("--max-events", type=int, default=20_000)
    p.add_argument("--diagnostic-session-max-events", type=int,
                   default=DEFAULT_DIAGNOSTIC_SESSION_MAX_EVENTS,
                   help="bounded full-session context available to diagnostics")
    p.add_argument("--max-decisions", type=int, default=100_000)
    p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
                   help="bounded parallel candidate evaluators (default: %(default)s)")
    p.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    return p


def _write_health(path: Path, status: str, **detail) -> dict:
    """Atomically publish one bounded shadow-loop heartbeat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "shadow-health.v1", "status": str(status),
        "updated_ts": time.time(), "pid": os.getpid(), **detail,
    }
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    runtime_config = (load_runtime_config(args.config)
                      if args.diagnostic else None)
    config = ShadowConfig(
        corpus_path=args.corpus, edge_db=args.edge_db, shadow_db=args.shadow_db,
        max_candidates=args.max_candidates, max_events=args.max_events,
        max_decisions=args.max_decisions,
        diagnostic_session_max_events=args.diagnostic_session_max_events,
        max_workers=args.max_workers,
        retention_days=args.retention_days,
        poll_seconds=args.interval,
        diagnostic=args.diagnostic,
        runtime_config=runtime_config,
        runtime_config_path=args.config)
    runner = ShadowRunner(config)
    health_file = args.health_file or args.shadow_db.with_name("shadow-health.json")
    interval = config.poll_seconds
    # Anchor before work so poll duration is included in the configured cadence.
    next_tick: float | None = time.monotonic()
    while True:
        try:
            result = runner.run_once()
            candidate_errors = result.get("candidate_errors") or {}
            safe_result = {key: result.get(key) for key in (
                "candidates", "events", "decisions", "ingested_events",
                "conflicts", "invalid_events", "skipped_recovery_bytes",
                "manifest_digest", "candidate_errors",
                "quarantine_through_session", "pruned_replay_diffs",
                "retention_days", "retention_floor_ts",
                "retention_gap_watermark", "signal_dispositions",
                "stress_calibration", "stale_tail", "diagnostic_shadow",
                "authorizing_candidates", "diagnostic_candidates",
                "poll_duration_seconds", "source_lag_seconds")
                if key in result}
            _write_health(health_file, "degraded" if candidate_errors else "running",
                          last_error=("candidate evaluation failures" if candidate_errors
                                      else None), **safe_result)
            print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            _write_health(health_file, "degraded", last_error=error[:500])
            print(json.dumps({"error": error}), flush=True)
            if args.once:
                return 1
            # Preserve completion-relative retry pacing after a failure, then
            # start a fresh fixed-cadence sequence with the next attempt.
            time.sleep(interval)
            next_tick = time.monotonic()
            continue
        if args.once:
            return 0
        now = time.monotonic()
        next_tick = _next_shadow_cadence_deadline(
            next_tick, now, interval)
        time.sleep(max(0.0, next_tick - time.monotonic()))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
