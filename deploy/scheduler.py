#!/usr/bin/env python3
"""Run the existing research script once per UTC day inside Compose."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent import state as agent_state  # noqa: E402
from deploy import load_config  # noqa: E402


_running = True
_child: subprocess.Popen | None = None
log = logging.getLogger("research.scheduler")
RUNNING_HEARTBEAT_SECONDS = 30.0
DEFAULT_JOB_TIMEOUT_SECONDS = 4 * 60 * 60
DEFAULT_OUTPUT_LIMIT_CHARS = 32_000
TIMEOUT_EXIT_CODE = 124


class ResearchJobTimeout(RuntimeError):
    """The bounded nightly child exceeded its configured wall clock."""


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
    }


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
        "schema": 2, "status": status, "updated_ts": time.time(),
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
    try:
        agent_state.append_operational_history(
            agent_state.operational_history_path(path), payload)
    except Exception as exc:                              # noqa: BLE001
        try:
            log.warning("could not append research status history: %s", exc)
        except Exception:                                 # noqa: BLE001
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
    raw = load_config(path)
    broker = raw.get("broker") if isinstance(raw.get("broker"), dict) else {}
    mode = str(raw.get("mode") or broker.get("mode") or "paper").lower()
    if mode != "paper":
        raise ValueError("config must select the Alpaca paper endpoint")
    os.environ["AGENT_MODE"] = mode
    return mode


def _wait_for_child_with_heartbeats(
        child: subprocess.Popen, status_path: Path, *,
        heartbeat_seconds: float = RUNNING_HEARTBEAT_SECONDS,
        timeout_seconds: float | None = None,
        terminate_grace_seconds: float = 30.0,
        stdout_capture: _BoundedCapture | None = None,
        stderr_capture: _BoundedCapture | None = None,
        **running_detail) -> int:
    """Wait without threads and durably refresh RUNNING while the child lives."""
    interval = float(heartbeat_seconds)
    if interval <= 0:
        raise ValueError("heartbeat_seconds must be positive")
    timeout = None if timeout_seconds is None else float(timeout_seconds)
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    started = time.monotonic()
    while True:
        wait_for = interval
        if timeout is not None:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    child.wait(timeout=max(0.0, float(terminate_grace_seconds)))
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                    child.wait()
                raise ResearchJobTimeout(
                    f"research child exceeded {timeout:g} seconds")
            wait_for = min(interval, remaining)
        try:
            return int(child.wait(timeout=wait_for))
        except subprocess.TimeoutExpired:
            write_status(
                status_path, "running", **running_detail,
                **_capture_detail(stdout_capture, stderr_capture))


def run_scheduler(args) -> int:
    global _child
    status_path = Path(args.status_file)
    try:
        mode = configured_mode(Path(args.config))
    except Exception as exc:                               # noqa: BLE001
        write_status(
            status_path, "failed", last_exit_code=2,
            structured_failures=[{
                "component": "scheduler_startup",
                "status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
            }])
        return 2
    previous = _load_last(status_path)
    last_date = previous.get("last_run_date")
    last_exit = previous.get("last_exit_code")
    last_job_detail = {
        key: previous[key] for key in (
            "job_id", "started_ts", "completed_ts", "run_date",
            "timeout_seconds", "structured_failures", "stdout_chars",
            "stderr_chars", "stdout_truncated", "stderr_truncated")
        if key in previous
    }
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while _running:
        now = datetime.now(timezone.utc)
        due = now if bool(getattr(args, "once", False)) else next_run(
            now, args.hour, args.minute, last_date)
        if due > now:
            write_status(
                status_path, "waiting", next_run_ts=due.timestamp(),
                last_run_date=last_date, last_exit_code=last_exit,
                **last_job_detail)
            for _ in range(min(30, max(1, int((due - now).total_seconds())))):
                if not _running:
                    break
                time.sleep(1)
            continue

        run_date = now.date().isoformat()
        started_ts = time.time()
        job_id = uuid.uuid4().hex
        timeout_seconds = float(getattr(
            args, "timeout_seconds", DEFAULT_JOB_TIMEOUT_SECONDS))
        output_limit = int(getattr(
            args, "output_limit_chars", DEFAULT_OUTPUT_LIMIT_CHARS))
        try:
            _child = subprocess.Popen(
                [str(Path(args.script).resolve())],
                cwd=Path(args.root).resolve(), env=os.environ.copy(),
                start_new_session=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception as exc:                           # noqa: BLE001
            last_exit = 2
            last_date = run_date
            failure = {
                "component": "scheduler_spawn", "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }
            write_status(
                status_path, "failed", job_id=job_id,
                started_ts=started_ts, completed_ts=time.time(),
                run_date=run_date, last_run_date=last_date,
                last_exit_code=last_exit,
                structured_failures=[failure])
            if bool(getattr(args, "once", False)):
                return last_exit
            continue
        stdout_capture = _start_capture(_child.stdout, output_limit)
        stderr_capture = _start_capture(_child.stderr, output_limit)
        running_detail = {
            "job_id": job_id,
            "started_ts": started_ts,
            "run_date": run_date,
            "child_pid": _child.pid,
            "timeout_seconds": timeout_seconds,
            "deadline_ts": started_ts + timeout_seconds,
            "last_run_date": last_date,
            "last_exit_code": last_exit,
        }
        write_status(status_path, "running", **running_detail)
        timed_out = False
        try:
            try:
                last_exit = _wait_for_child_with_heartbeats(
                    _child, status_path,
                    stdout_capture=(stdout_capture[0]
                                    if stdout_capture else None),
                    stderr_capture=(stderr_capture[0]
                                    if stderr_capture else None),
                    **running_detail)
            except ResearchJobTimeout:
                timed_out = True
                last_exit = TIMEOUT_EXIT_CODE
        finally:
            _child = None
        captures = [item for item in (stdout_capture, stderr_capture)
                    if item is not None]
        for _capture, thread in captures:
            thread.join(timeout=5)
        stdout = (stdout_capture[0] if stdout_capture else _BoundedCapture(1))
        stderr = (stderr_capture[0] if stderr_capture else _BoundedCapture(1))
        structured_failures = list(stdout.structured_failures)
        structured_failures.extend(stderr.structured_failures)
        if stdout.tail:
            sys.stdout.write(stdout.tail)
            sys.stdout.flush()
        if stderr.tail:
            sys.stderr.write(stderr.tail)
            sys.stderr.flush()
        if last_exit == 0 and structured_failures:
            # research.py historically returned zero after persisting retry
            # state. The scheduler is the service boundary and must not turn
            # that structured failure into a green job.
            last_exit = 1
        last_date = run_date
        completed_ts = time.time()
        final_status = (
            "timed_out" if timed_out else
            "completed" if last_exit == 0 else "failed")
        write_status(
            status_path, final_status, job_id=job_id,
            started_ts=started_ts, completed_ts=completed_ts,
            run_date=run_date, timeout_seconds=timeout_seconds,
            last_run_date=last_date, last_exit_code=last_exit,
            structured_failures=structured_failures,
            stdout_tail=stdout.tail, stderr_tail=stderr.tail,
            stdout_chars=stdout.total_chars, stderr_chars=stderr.total_chars,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated)
        last_job_detail = {
            "job_id": job_id, "started_ts": started_ts,
            "completed_ts": completed_ts, "run_date": run_date,
            "timeout_seconds": timeout_seconds,
            "structured_failures": structured_failures,
            "stdout_chars": stdout.total_chars,
            "stderr_chars": stderr.total_chars,
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
        }
        if bool(getattr(args, "once", False)):
            return int(last_exit or 0)

    write_status(
        status_path, "stopped", last_run_date=last_date,
        last_exit_code=last_exit, **last_job_detail)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--script", default="deploy/research-cycle.sh")
    parser.add_argument("--status-file", default="runtime/health/research.json")
    parser.add_argument("--hour", type=int, default=3)
    parser.add_argument("--minute", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float,
                        default=DEFAULT_JOB_TIMEOUT_SECONDS)
    parser.add_argument("--output-limit-chars", type=int,
                        default=DEFAULT_OUTPUT_LIMIT_CHARS)
    parser.add_argument("--once", action="store_true",
                        help="run one due job and return its effective status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 0 <= args.hour <= 23 or not 0 <= args.minute <= 59:
        raise SystemExit("hour/minute is outside the UTC clock")
    return run_scheduler(args)


if __name__ == "__main__":
    raise SystemExit(main())
