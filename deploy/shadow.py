#!/usr/bin/env python3
"""Operations entrypoint for the broker-free real-time shadow lane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.live_shadow import ShadowConfig, ShadowRunner  # noqa: E402


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path,
                   default=Path("runtime/research/recorded/data.csv"))
    p.add_argument("--edge-db", type=Path,
                   default=Path("runtime/research/edge_lab.sqlite3"))
    p.add_argument("--shadow-db", type=Path,
                   default=Path("runtime/research/shadow.sqlite3"))
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--once", action="store_true",
                   help="run one bounded ingest/evaluation cycle and exit")
    p.add_argument("--max-candidates", type=int, default=32)
    p.add_argument("--max-events", type=int, default=20_000)
    p.add_argument("--max-decisions", type=int, default=100_000)
    p.add_argument("--retention-days", type=int, default=14)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = ShadowConfig(
        corpus_path=args.corpus, edge_db=args.edge_db, shadow_db=args.shadow_db,
        max_candidates=args.max_candidates, max_events=args.max_events,
        max_decisions=args.max_decisions, retention_days=args.retention_days,
        poll_seconds=args.interval)
    runner = ShadowRunner(config)
    while True:
        try:
            result = runner.run_once()
            print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        import time
        time.sleep(max(1.0, float(args.interval)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
