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

from research.live_shadow import (DEFAULT_RETENTION_DAYS, ShadowConfig,
                                  ShadowRunner)  # noqa: E402


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path,
                   default=Path("runtime/research/recorded/data.csv"))
    p.add_argument("--edge-db", type=Path,
                   default=Path("runtime/research/edge_lab.sqlite3"))
    p.add_argument("--shadow-db", type=Path,
                   default=Path("runtime/research/shadow.sqlite3"))
    p.add_argument("--health-file", type=Path,
                   help="durable polling heartbeat (defaults beside shadow DB)")
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--once", action="store_true",
                   help="run one bounded ingest/evaluation cycle and exit")
    p.add_argument("--max-candidates", type=int, default=32)
    p.add_argument("--max-events", type=int, default=20_000)
    p.add_argument("--max-decisions", type=int, default=100_000)
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
    config = ShadowConfig(
        corpus_path=args.corpus, edge_db=args.edge_db, shadow_db=args.shadow_db,
        max_candidates=args.max_candidates, max_events=args.max_events,
        max_decisions=args.max_decisions, retention_days=args.retention_days,
        poll_seconds=args.interval)
    runner = ShadowRunner(config)
    health_file = args.health_file or args.shadow_db.with_name("shadow-health.json")
    while True:
        try:
            result = runner.run_once()
            safe_result = {key: result.get(key) for key in (
                "candidates", "events", "decisions", "ingested_events",
                "conflicts", "invalid_events", "skipped_recovery_bytes",
                "quarantine_through_session", "pruned_replay_diffs",
                "retention_days", "retention_floor_ts",
                "retention_gap_watermark", "stale_tail")
                if key in result}
            _write_health(health_file, "running", last_error=None, **safe_result)
            print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            _write_health(health_file, "degraded", last_error=error[:500])
            print(json.dumps({"error": error}), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        import time
        time.sleep(max(1.0, float(args.interval)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
