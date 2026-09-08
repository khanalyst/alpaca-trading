#!/usr/bin/env python3
"""Supervise one trader child and transfer recovery only after it has exited.

The supervisor has no entry path. It measures actual child heartbeat progress
with a monotonic deadline, sends signals only to the process group it created,
and waits for exit before invoking the authenticated, exclusive-lock watchdog.
This closes the hung-owner gap without letting two processes close positions
concurrently. The separate watchdog container still handles supervisor death.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import state
from deploy.health import _read_json
from deploy.watchdog import run_once, write_status


def terminate_child(child, *, grace: float = 15.0) -> bool:
    """Reap our child before handing off; never infer death from a stale PID."""
    if child.poll() is not None:
        child.wait()
        return True
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=grace)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=grace)
            return True
        except subprocess.TimeoutExpired:
            return False


def monitor(child, heartbeat: Path, *, max_age: float, interval: float,
            started_wall: float, stop_requested: Callable[[], bool],
            monotonic: Callable[[], float] = time.monotonic,
            wall_time: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep) -> str:
    """Return exit, shutdown, or unresponsive without any process mutation."""
    progress_at = monotonic()
    latest_stamp = started_wall - 1.0
    while child.poll() is None:
        if stop_requested():
            return "shutdown"
        payload = _read_json(heartbeat)
        raw = payload.get("updated_ts")
        stamp = (float(raw) if isinstance(raw, (int, float)) and
                 not isinstance(raw, bool) else float("nan"))
        # Old files, another process's heartbeat, clock jumps into the future,
        # and repeated unchanged writes cannot extend this child's deadline.
        if (payload.get("pid") == child.pid and math.isfinite(stamp) and
                latest_stamp < stamp <= wall_time() + 1.0):
            latest_stamp = stamp
            progress_at = monotonic()
        if monotonic() - progress_at >= max_age:
            return "unresponsive"
        sleep(interval)
    return "exit"


def recover(config_path: str) -> dict:
    from agent.alpaca_provider import AlpacaProvider
    from main import load_cfg
    cfg = load_cfg(config_path)
    return run_once(cfg, AlpacaProvider(cfg), max_age=300,
                    terminated_child=True)


def wait_until_resumed(status_path: Path, *, interval: float,
                       stop_requested: Callable[[], bool],
                       sleep: Callable[[float], None] = time.sleep) -> bool:
    """Park without a trader child or broker I/O while operator pause persists."""
    while not stop_requested():
        paused = state.load_state().get("operator_pause", False)
        if not isinstance(paused, bool):
            raise ValueError("operator pause state must be boolean")
        if not paused:
            return True
        state.write_heartbeat("paused", reason="operator_pause",
                              trader_child_running=False)
        write_status(status_path, "paused", reason="operator_pause",
                     operator_pause=True, trader_child_running=False)
        sleep(interval)
    return False


def supervise(config_path: str, *, max_age: float = 300.0,
              interval: float = 5.0, grace: float = 15.0,
              status_path: Path = Path("runtime/health/trader-supervisor.json")) -> int:
    from main import load_cfg
    cfg = load_cfg(config_path)
    state.configure_runtime(str(cfg.get("mode", "paper")))
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    child = None
    reaped = False
    recovery_attempted = False
    started = time.time()
    try:
        if not wait_until_resumed(status_path, interval=interval,
                                  stop_requested=lambda: stopping):
            write_status(status_path, "stopped", reason="shutdown_while_paused",
                         operator_pause=True, trader_child_running=False)
            return 0
        started = time.time()
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py"), "--config", config_path, "run"],
            cwd=ROOT, start_new_session=True, close_fds=True)
        write_status(status_path, "watching", child_pid=child.pid,
                     max_heartbeat_age=max_age, started_ts=started)
        result = monitor(child, state.HEARTBEAT_FILE, max_age=max_age,
                         interval=interval, started_wall=started,
                         stop_requested=lambda: stopping)
        if result == "exit":
            code = child.wait()
            reaped = True
            if code == 0:
                write_status(status_path, "stopped", child_pid=child.pid, exit_code=code)
                return 0
        else:
            reaped = terminate_child(child, grace=grace)
            if not reaped:
                write_status(status_path, "failed", child_pid=child.pid,
                             reason="child_exit_unconfirmed", residual_risk=True)
                return 1
            # A cooperative normal shutdown already completed the trader's
            # own flatten path; otherwise independently reconcile recovery.
            if result == "shutdown" and child.returncode == 0:
                write_status(status_path, "stopped", child_pid=child.pid, exit_code=0)
                return 0
        recovery_attempted = True
        verdict = recover(config_path)
        ok = verdict.get("flattened") is True or verdict.get("reason") == "no_open_positions"
        write_status(status_path, "recovered" if ok else "failed",
                     child_pid=child.pid, reason=result, recovery=verdict,
                     operator_pause=True, residual_risk=not ok)
        # Never relaunch entries here. A later restart retains operator pause.
        return 1
    except Exception as exc:
        # Even a failed status write/monitor must leave the failed child
        # stopped and entries paused before an external restart can occur.
        # Recovery takes the run lock and persists pause before broker I/O.
        if child is not None and not reaped:
            reaped = terminate_child(child, grace=grace)
        if reaped and not recovery_attempted:
            recovery_attempted = True
            try:
                recover(config_path)
            except Exception:
                pass  # the separate watchdog retains its recovery path
        try:
            write_status(status_path, "failed", reason=str(exc), residual_risk=True)
        except OSError:
            pass
        return 1
    finally:
        # An observability/configuration exception must not orphan a writer
        # that an external restart could race. The watchdog recovers after its
        # stale deadline if recovery itself was unavailable.
        if child is not None and not reaped:
            terminate_child(child, grace=grace)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--max-heartbeat-age", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--terminate-grace", type=float, default=15.0)
    args = parser.parse_args(argv)
    if any(not math.isfinite(x) or x <= 0 for x in
           (args.max_heartbeat_age, args.interval, args.terminate_grace)):
        parser.error("timing values must be finite and positive")
    env_file = os.getenv("ALPACA_AGENT_SECRETS_FILE") or os.getenv("ALPACA_AGENT_SECRET_FILE")
    from dotenv import load_dotenv
    load_dotenv(env_file or ROOT / ".env", override=False)
    return supervise(str(Path(args.config).resolve()), max_age=args.max_heartbeat_age,
                     interval=args.interval, grace=args.terminate_grace)


if __name__ == "__main__":
    raise SystemExit(main())
