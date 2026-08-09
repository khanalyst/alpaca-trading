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
    ok = fresh and status in {"starting", "running", "paused"} and research_ok
    return {
        "ok": ok,
        "component": "trader",
        "status": status,
        "fresh": fresh,
        "research_available": heartbeat.get("research_available"),
        "research_status": heartbeat.get("research_status"),
    }


def recorder(path: Path, max_age: float, *, now: float | None = None) -> dict:
    files = [item for item in path.rglob("*.csv") if item.is_file()]
    latest = max((item.stat().st_mtime for item in files), default=None)
    fresh = _fresh(latest, max_age, now)
    return {
        "ok": bool(files) and fresh,
        "component": "recorder",
        "status": "recording" if files and fresh else "stale_or_empty",
        "fresh": fresh,
        "series_files": len(files),
        "latest_write_ts": latest,
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
    ok = fresh and not hung and status in {
        "waiting", "running", "completed", "completed_no_edge"} and (
        last_exit in {None, 0})
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
        "next_run_ts": heartbeat.get("next_run_ts"),
        "structured_failures": heartbeat.get("structured_failures") or [],
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
        else:
            result = dashboard(args.url, args.timeout)
    except Exception as exc:                               # noqa: BLE001
        result = {"ok": False, "component": args.component,
                  "status": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
