#!/usr/bin/env python3
"""Run the existing research script once per UTC day inside Compose."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


_running = True
_child: subprocess.Popen | None = None


def _stop(_signum, _frame) -> None:
    global _running
    _running = False
    if _child is not None and _child.poll() is None:
        try:
            os.killpg(_child.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass


def write_status(path: Path, status: str, **detail) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1, "status": status, "updated_ts": time.time(),
        "pid": os.getpid(), **detail,
    }
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return payload


def next_run(now: datetime, hour: int, minute: int,
             last_run_date: str | None) -> datetime:
    """Return today's missed run or the next scheduled UTC instant."""
    today = now.astimezone(timezone.utc)
    scheduled = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if today >= scheduled and last_run_date != today.date().isoformat():
        return today
    return scheduled if today < scheduled else scheduled + timedelta(days=1)


def _load_last(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def configured_mode(path: Path) -> str:
    """Bind every nightly path to the same mode as the mounted config."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mode = str(raw.get("mode") or "")
    if mode not in {"demo", "live"}:
        raise ValueError("config.mode must be demo or live")
    os.environ["AGENT_MODE"] = mode
    return mode


def run_scheduler(args) -> int:
    global _child
    from main import load_secrets

    mode = configured_mode(Path(args.config))
    load_secrets(mode)
    status_path = Path(args.status_file)
    previous = _load_last(status_path)
    last_date = previous.get("last_run_date")
    last_exit = previous.get("last_exit_code")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while _running:
        now = datetime.now(timezone.utc)
        due = next_run(now, args.hour, args.minute, last_date)
        if due > now:
            write_status(
                status_path, "waiting", next_run_ts=due.timestamp(),
                last_run_date=last_date, last_exit_code=last_exit)
            for _ in range(min(30, max(1, int((due - now).total_seconds())))):
                if not _running:
                    break
                time.sleep(1)
            continue

        run_date = now.date().isoformat()
        write_status(
            status_path, "running", started_ts=time.time(),
            last_run_date=last_date, last_exit_code=last_exit)
        _child = subprocess.Popen(
            [str(Path(args.script).resolve())],
            cwd=Path(args.root).resolve(), env=os.environ.copy(),
            start_new_session=True)
        last_exit = int(_child.wait())
        _child = None
        last_date = run_date
        write_status(
            status_path, "completed" if last_exit == 0 else "failed",
            completed_ts=time.time(), last_run_date=last_date,
            last_exit_code=last_exit)

    write_status(
        status_path, "stopped", last_run_date=last_date,
        last_exit_code=last_exit)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--script", default="research/nightly.sh")
    parser.add_argument("--status-file", default="runtime/health/research.json")
    parser.add_argument("--hour", type=int, default=3)
    parser.add_argument("--minute", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 0 <= args.hour <= 23 or not 0 <= args.minute <= 59:
        raise SystemExit("hour/minute is outside the UTC clock")
    return run_scheduler(args)


if __name__ == "__main__":
    raise SystemExit(main())
