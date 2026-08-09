"""Small mode-scoped runtime state and append-only operational journal."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping

from . import journal
from .journal import (JournalNotReady, _ClosingConnection, _JOURNAL_REQUIRED_COLUMNS,
                      _JOURNAL_TABLES, _journal_columns,
                      _validate_existing_journal_schema)
from .state_store import (DEFAULT, DAY_STOPPED, KILLED, PAUSED, RUNNING,
                          StateCorruptionError, _atomic_write, _read,
                          _validated)

RUNTIME_BASE = Path(os.getenv("ALPACA_AGENT_RUNTIME_ROOT") or
                    Path(__file__).resolve().parent.parent / "runtime")
RUNTIME_SCOPE = "_unconfigured"
RUNTIME = RUNTIME_BASE / RUNTIME_SCOPE
STATE_FILE = RUNTIME / "state.json"
PID_FILE = RUNTIME / "agent.pid"
HEARTBEAT_FILE = RUNTIME / "heartbeat.json"
STATE_LOCK_FILE = RUNTIME / "state.lock"
EVENTS_FILE = RUNTIME / "events.jsonl"
JOURNAL_FILE = RUNTIME / "journal.db"


class RuntimeIdentityError(RuntimeError):
    """Runtime files are not scoped to the requested mode/account."""


def _set_paths(runtime: Path, scope: str) -> Path:
    global RUNTIME_SCOPE, RUNTIME, STATE_FILE, PID_FILE, HEARTBEAT_FILE
    global STATE_LOCK_FILE, EVENTS_FILE, JOURNAL_FILE
    RUNTIME_SCOPE = scope
    RUNTIME = runtime
    STATE_FILE = runtime / "state.json"
    PID_FILE = runtime / "agent.pid"
    HEARTBEAT_FILE = runtime / "heartbeat.json"
    STATE_LOCK_FILE = runtime / "state.lock"
    EVENTS_FILE = runtime / "events.jsonl"
    JOURNAL_FILE = runtime / "journal.db"
    return runtime


def configure_runtime(mode: str, base: Path | None = None) -> Path:
    if mode not in {"paper", "live", "test", "_unconfigured"}:
        raise ValueError("runtime mode must be paper, live, test, or _unconfigured")
    root = Path(base) if base is not None else RUNTIME_BASE
    return _set_paths(root / mode, mode)


def account_fingerprint(mode: str, api_key: str) -> str:
    if mode not in {"paper", "live"} or not isinstance(api_key, str) or not api_key:
        raise RuntimeIdentityError("paper/live mode and API key are required")
    digest = hashlib.sha256(f"alpaca-agent\0{mode}\0{api_key}".encode()).hexdigest()[:20]
    return f"alpaca-{mode}-{digest}"


def bind_account_identity(fingerprint: str) -> dict:
    """Bind this mode-specific runtime directory to one account identity.

    A state directory must never silently change accounts.  The fingerprint is
    intentionally non-secret and may be displayed by the read-only dashboard.
    """
    mode = "live" if str(fingerprint).startswith("alpaca-live-") else (
        "paper" if str(fingerprint).startswith("alpaca-paper-") else None)
    if mode is None:
        raise RuntimeIdentityError("a paper/live account fingerprint is required")
    if RUNTIME_SCOPE != mode:
        raise RuntimeIdentityError("account fingerprint does not match runtime mode")

    def bind(value: dict) -> dict:
        existing = value.get("account_fingerprint")
        if existing and existing != fingerprint:
            raise RuntimeIdentityError(
                f"runtime state belongs to a different {mode} account")
        value["account_fingerprint"] = fingerprint
        value["runtime_mode"] = mode
        return value

    return update_state(bind)


def load_state() -> dict:
    return _read(STATE_FILE, DEFAULT)


def save_state(value: Mapping[str, Any]) -> dict:
    result = _validated(value)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = STATE_LOCK_FILE.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _atomic_write(STATE_FILE, result)
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()
    return result


def update_state(update: Mapping[str, Any] | Callable[[dict], Mapping[str, Any] | None]) -> dict:
    """Lock one complete state read-modify-write transaction.

    Callers performing safety-sensitive mutations should use this helper so
    another process cannot replace fields between their read and atomic write.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = STATE_LOCK_FILE.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = _read(STATE_FILE, DEFAULT)
        if callable(update):
            working = deepcopy(current)
            changed = update(working)
            candidate = working if changed is None else changed
        else:
            candidate = deepcopy(current)
            candidate.update({key: value for key, value in update.items()
                              if key in DEFAULT})
        result = _validated(candidate)
        _atomic_write(STATE_FILE, result)
        return result
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def initialize_journal() -> Path:
    """Create/migrate the current mode-scoped operational journal."""
    try:
        return journal.initialize_journal(
            JOURNAL_FILE,
            RUNTIME,
            RUNTIME_SCOPE,
            tables=_JOURNAL_TABLES,
            required_columns=_JOURNAL_REQUIRED_COLUMNS,
            connect=sqlite3.connect,
            connection_factory=_ClosingConnection,
        )
    except JournalNotReady:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise JournalNotReady(
            f"{RUNTIME_SCOPE} journal unavailable: {exc}"
        ) from exc


def journal_ready() -> bool:
    """Initialize, then check readiness of the current journal only."""
    try:
        # Keep this facade call explicit: callers may patch/rebind
        # state.initialize_journal and still expect it to run first.
        initialize_journal()
        return journal.journal_ready(
            JOURNAL_FILE,
            RUNTIME_SCOPE,
            runtime=RUNTIME,
            tables=_JOURNAL_TABLES,
            required_columns=_JOURNAL_REQUIRED_COLUMNS,
            connect=sqlite3.connect,
            connection_factory=_ClosingConnection,
        )
    except (OSError, sqlite3.Error, RuntimeError):
        return False


def ensure_ready() -> None:
    """Make state and journal readiness an explicit pre-order invariant."""
    initialize_journal()
    if not STATE_FILE.exists():
        initial = deepcopy(DEFAULT)
        initial["runtime_mode"] = RUNTIME_SCOPE if RUNTIME_SCOPE in {
            "paper", "live", "test"} else None
        save_state(initial)
    else:
        load_state()
    if not journal_ready():
        raise JournalNotReady(f"{RUNTIME_SCOPE} journal schema is incomplete")


def check_journal() -> None:
    """Strict readiness probe for startup callers."""
    ensure_ready()


def _journal_insert(table: str, payload: Mapping[str, Any]) -> None:
    # Keep state validation outside the SQLite adapter's error normalization so
    # StateCorruptionError remains visible to order-bearing callers.
    ensure_ready()
    try:
        journal.insert(
            JOURNAL_FILE,
            table,
            payload,
            RUNTIME_SCOPE,
            runtime=RUNTIME,
            tables=_JOURNAL_TABLES,
            required_columns=_JOURNAL_REQUIRED_COLUMNS,
            connect=sqlite3.connect,
            connection_factory=_ClosingConnection,
        )
    except JournalNotReady:
        raise
    except StateCorruptionError:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise JournalNotReady(f"journal write failed: {exc}") from exc


def commit(value: Mapping[str, Any], transition=None, kill: str | None = None) -> dict:
    def mutate(current: dict) -> dict:
        current.update({k: value[k] for k in DEFAULT if k in value})
        if kill is not None:
            current["state"] = KILLED; current["kill_reason"] = kill
        elif transition and current.get("state") == transition[0]:
            current["state"] = transition[1]
        return current

    result = update_state(mutate)
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
               "pid": os.getpid(), "runtime_mode": RUNTIME_SCOPE if RUNTIME_SCOPE in {"paper", "live", "test"} else None,
               **detail}
    _atomic_write(HEARTBEAT_FILE, payload)
    try: append_operational_history(operational_history_path(HEARTBEAT_FILE), payload)
    except Exception: pass
    return payload


def log_event(kind: str, payload: str, **detail) -> None:
    row = {"ts": time.time(), "kind": str(kind), "payload": str(payload), **detail}
    # SQLite is the authoritative ledger consumed by report/dashboard.  The
    # append-only JSONL mirror remains useful for direct operator inspection,
    # but never let a successful order exist without its durable SQLite event.
    _journal_insert("events", row)
    try:
        _append(EVENTS_FILE, row)
    except Exception:
        pass


def log_order(order=None, request=None, *, action: str = "submit",
              reason: str | None = None, **detail) -> None:
    """Persist an order lifecycle row in the mode-scoped durable journal."""
    source = order or request
    row = {
        "ts": time.time(), "order_id": getattr(source, "id", None),
        "client_order_id": getattr(source, "client_order_id", None),
        "symbol": getattr(source, "symbol", None), "side": getattr(source, "side", None),
        "action": action, "qty": float(getattr(source, "qty", 0) or 0),
        "type": getattr(source, "type", None),
        "time_in_force": getattr(source, "time_in_force", None),
        "status": getattr(order, "status", None) if order is not None else "submitted",
        "filled_qty": float(getattr(order, "filled_qty", 0) or 0) if order is not None else None,
        "filled_avg_price": float(getattr(order, "filled_avg_price", 0) or 0) if order is not None and getattr(order, "filled_avg_price", None) is not None else None,
        "reason": reason, **detail,
    }
    _journal_insert("orders", row)


def log_equity(equity: Any, state_name: str | None = None, **detail) -> None:
    """Persist one account-equity observation for reports and dashboards."""
    try:
        value = float(equity)
    except (TypeError, ValueError):
        value = None
    _journal_insert("equity", {"ts": time.time(), "equity": value,
                                "state": state_name, **detail})


def log_trade(symbol, side, action, qty, price=None, notional=None,
              reason=None, **detail) -> None:
    row = {"ts": time.time(), "symbol": symbol, "side": side,
           "action": action, "qty": float(qty) if qty is not None else None,
           "price": float(price) if price is not None else None,
           "notional": float(notional) if notional is not None else None,
           "reason": reason, **detail}
    _journal_insert("trades", row)
    try:
        _append(EVENTS_FILE, {"ts": row["ts"], "kind": "trade",
                              "payload": json.dumps(row, default=str)})
    except Exception:
        pass


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
    try:
        handle.seek(0)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


if os.getenv("ALPACA_AGENT_RUNTIME_SCOPE"):
    configure_runtime(os.environ["ALPACA_AGENT_RUNTIME_SCOPE"])
