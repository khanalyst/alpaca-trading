"""Small paper-runtime state and append-only operational journal."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

RUNTIME_BASE = Path(os.getenv("ALPACA_AGENT_RUNTIME_ROOT") or
                    Path(__file__).resolve().parent.parent / "runtime")
RUNTIME_SCOPE = "_unconfigured"
RUNTIME = RUNTIME_BASE / RUNTIME_SCOPE
STATE_FILE = RUNTIME / "state.json"
PID_FILE = RUNTIME / "agent.pid"
HEARTBEAT_FILE = RUNTIME / "heartbeat.json"
STATE_LOCK_FILE = RUNTIME / "state.lock"
EVENTS_FILE = RUNTIME / "events.jsonl"

RUNNING = "RUNNING"
PAUSED = "PAUSED"
DAY_STOPPED = "DAY_STOPPED"
KILLED = "KILLED"

DEFAULT = {
    "state": PAUSED,
    "runtime_mode": None,
    "account_fingerprint": None,
    "active_trades": {},
    "recent_setups": {},
    "protection": {},
    "opened_at": {},
    "kill_reason": None,
    "operator_pause": False,
}


class RuntimeIdentityError(RuntimeError):
    """Runtime files are not scoped to the requested paper account."""


def _set_paths(runtime: Path, scope: str) -> Path:
    global RUNTIME_SCOPE, RUNTIME, STATE_FILE, PID_FILE, HEARTBEAT_FILE
    global STATE_LOCK_FILE, EVENTS_FILE
    RUNTIME_SCOPE = scope
    RUNTIME = runtime
    STATE_FILE = runtime / "state.json"
    PID_FILE = runtime / "agent.pid"
    HEARTBEAT_FILE = runtime / "heartbeat.json"
    STATE_LOCK_FILE = runtime / "state.lock"
    EVENTS_FILE = runtime / "events.jsonl"
    return runtime


def configure_runtime(mode: str, base: Path | None = None) -> Path:
    if mode not in {"paper", "test", "_unconfigured"}:
        raise ValueError("runtime mode must be paper, test, or _unconfigured")
    root = Path(base) if base is not None else RUNTIME_BASE
    return _set_paths(root / mode, mode)


def account_fingerprint(mode: str, api_key: str) -> str:
    if mode != "paper" or not isinstance(api_key, str) or not api_key:
        raise RuntimeIdentityError("paper mode and API key are required")
    digest = hashlib.sha256(f"alpaca-agent\0paper\0{api_key}".encode()).hexdigest()[:20]
    return f"alpaca-paper-{digest}"


def _validated(data: Mapping[str, Any]) -> dict:
    # Drop fields from older state formats instead of propagating them into
    # the paper control file.
    value = deepcopy(DEFAULT)
    value.update({key: data[key] for key in DEFAULT if key in data})
    if value.get("state") not in {RUNNING, PAUSED, DAY_STOPPED, KILLED}:
        raise ValueError("invalid state")
    if value.get("runtime_mode") not in {None, "paper"}:
        raise ValueError("runtime_mode must be paper")
    for key in ("active_trades", "recent_setups", "protection", "opened_at"):
        if not isinstance(value.get(key), dict):
            raise ValueError(f"{key} must be an object")
    return value


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, allow_nan=False, default=str)
            handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass


def _read(path: Path, fallback: Mapping[str, Any]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return _validated(value) if isinstance(value, Mapping) else deepcopy(fallback)
    except (OSError, ValueError, json.JSONDecodeError):
        return deepcopy(fallback)


def load_state() -> dict:
    return _read(STATE_FILE, DEFAULT)


def save_state(value: Mapping[str, Any]) -> dict:
    result = _validated(value)
    _atomic_write(STATE_FILE, result)
    return result


def set_state(name: str, reason: str | None = None, **extra) -> dict:
    value = load_state(); value["state"] = name
    if reason is not None: value["kill_reason"] = reason
    value.update(extra)
    return save_state(value)


def commit(value: Mapping[str, Any], transition=None, kill: str | None = None) -> dict:
    current = load_state(); current.update({k: value[k] for k in DEFAULT if k in value})
    if kill is not None:
        current["state"] = KILLED; current["kill_reason"] = kill
    elif transition and current.get("state") == transition[0]:
        current["state"] = transition[1]
    result = save_state(current)
    if isinstance(value, dict):
        value.clear(); value.update(result)
    return result


def operational_history_path(latest_path: str | Path) -> Path:
    path = Path(latest_path)
    stem = path.stem if path.suffix.lower() == ".json" else path.name
    return path.with_name(f"{stem}.history.jsonl")


def _append(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(payload), sort_keys=True, allow_nan=False,
                         default=str) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line); os.fsync(fd)
    finally:
        os.close(fd)


def append_operational_history(path: str | Path, payload: dict) -> None:
    _append(Path(path), payload)


def write_heartbeat(status: str, **detail) -> dict:
    if status not in {"starting", "running", "degraded", "pausing", "paused", "killed", "stopped"}:
        raise ValueError(f"unsupported heartbeat status {status!r}")
    payload = {"schema": 1, "status": status, "updated_ts": time.time(),
               "pid": os.getpid(), "runtime_mode": RUNTIME_SCOPE if RUNTIME_SCOPE == "paper" else None,
               **detail}
    _atomic_write(HEARTBEAT_FILE, payload)
    try: append_operational_history(operational_history_path(HEARTBEAT_FILE), payload)
    except OSError: pass
    return payload


def log_event(kind: str, payload: str, **detail) -> None:
    row = {"ts": time.time(), "kind": str(kind), "payload": str(payload), **detail}
    _append(EVENTS_FILE, row)


def log_trade(symbol, side, action, qty, price=None, notional=None,
              reason=None, **detail) -> None:
    log_event("trade", json.dumps({"symbol": symbol, "side": side,
                                    "action": action, "qty": qty,
                                    "price": price, "notional": notional,
                                    "reason": reason,
                                    **detail}, default=str))


def stable_fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()[:16]


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def new_cycle_id() -> str:
    return f"cycle_{uuid.uuid4().hex}"


def acquire_run_lock():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    handle = PID_FILE.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close(); return None
    handle.seek(0); handle.truncate(); handle.write(str(os.getpid())); handle.flush()
    return handle


def release_run_lock(handle) -> None:
    if handle is None: return
    try: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally: handle.close()


def read_pid() -> int | None:
    try: return int(PID_FILE.read_text().strip())
    except (OSError, ValueError): return None


def pid_alive(pid: int) -> bool:
    try: os.kill(pid, 0); return True
    except OSError: return False


if os.getenv("ALPACA_AGENT_RUNTIME_SCOPE"):
    configure_runtime(os.environ["ALPACA_AGENT_RUNTIME_SCOPE"])
