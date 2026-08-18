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
# A process can discover an observability failure before the cycle writer
# emits its normal ``running`` heartbeat.  Keep the degraded reason at the
# mode-scoped state facade so later heartbeat calls cannot erase it.
OBSERVABILITY_DEGRADED_REASON: str | None = None


class RuntimeIdentityError(RuntimeError):
    """Runtime files are not scoped to the requested mode/account."""


def _set_paths(runtime: Path, scope: str) -> Path:
    global RUNTIME_SCOPE, RUNTIME, STATE_FILE, PID_FILE, HEARTBEAT_FILE
    global STATE_LOCK_FILE, EVENTS_FILE, JOURNAL_FILE
    global OBSERVABILITY_DEGRADED_REASON
    runtime = Path(runtime)
    same_scope = RUNTIME_SCOPE == scope
    same_runtime = Path(RUNTIME) == runtime
    RUNTIME_SCOPE = scope
    RUNTIME = runtime
    STATE_FILE = runtime / "state.json"
    PID_FILE = runtime / "agent.pid"
    HEARTBEAT_FILE = runtime / "heartbeat.json"
    STATE_LOCK_FILE = runtime / "state.lock"
    EVENTS_FILE = runtime / "events.jsonl"
    JOURNAL_FILE = runtime / "journal.db"
    # Rebinding the same mode/path is a normal startup/readiness operation;
    # it must not erase a sticky observability fault before the next heartbeat
    # is emitted.  A genuinely different runtime scope or directory starts a
    # fresh observability epoch.
    if not (same_scope and same_runtime):
        OBSERVABILITY_DEGRADED_REASON = None
    return runtime


def mark_observability_degraded(reason: str) -> None:
    """Keep a runtime observability fault sticky across normal heartbeats."""
    global OBSERVABILITY_DEGRADED_REASON
    OBSERVABILITY_DEGRADED_REASON = str(reason or "runtime_observability_degraded")


def clear_observability_degraded() -> None:
    """Clear a recovered observability fault for the current runtime scope."""
    global OBSERVABILITY_DEGRADED_REASON
    OBSERVABILITY_DEGRADED_REASON = None


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
        path = journal.initialize_journal(
            JOURNAL_FILE,
            RUNTIME,
            RUNTIME_SCOPE,
            tables=_JOURNAL_TABLES,
            required_columns=_JOURNAL_REQUIRED_COLUMNS,
            connect=sqlite3.connect,
            connection_factory=_ClosingConnection,
        )
        # A close can be observed more than once when a process crashes after
        # the SQLite journal commit but before the JSON active-trade removal.
        # Give those rows a durable uniqueness boundary.  ``NULL`` remains
        # allowed for legacy/open rows that predate close idempotency.
        with sqlite3.connect(path, timeout=5,
                             factory=_ClosingConnection) as db:
            db.execute("PRAGMA busy_timeout=5000")
            # Serialize this small migration with other initializers.  The
            # base journal adapter is itself idempotent, but this optional
            # column/index used to be added in a second transaction: two
            # startups could both observe the missing column and one would
            # fail on ``duplicate column name``.
            db.execute("BEGIN IMMEDIATE")
            if "trade_id" not in _journal_columns(db, "trades"):
                try:
                    db.execute("ALTER TABLE trades ADD COLUMN trade_id TEXT")
                except sqlite3.OperationalError as exc:
                    # A legacy process may have completed the ALTER between
                    # the probe and statement.  Re-probe and continue only if
                    # the required column is now present; all other migration
                    # failures remain fail-closed.
                    if "duplicate column name" not in str(exc).lower() or \
                            "trade_id" not in _journal_columns(db, "trades"):
                        raise
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS trades_trade_id_unique "
                "ON trades(trade_id) WHERE trade_id IS NOT NULL")
            db.commit()
        return path
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
    if status == "running" and OBSERVABILITY_DEGRADED_REASON:
        status = "degraded"
        detail["reason"] = OBSERVABILITY_DEGRADED_REASON
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
    source_qty = float(getattr(source, "qty", 0) or 0)
    # ``qty`` remains the broker/request compatibility field.  Calibration
    # reads these explicit aliases so an incremental trade row can never be
    # mistaken for the order's planned quantity.
    detail.setdefault("requested_qty", source_qty)
    detail.setdefault("planned_qty", source_qty)
    row = {
        "ts": time.time(), "order_id": getattr(source, "id", None),
        "client_order_id": getattr(source, "client_order_id", None),
        "symbol": getattr(source, "symbol", None), "side": getattr(source, "side", None),
        "action": action, "qty": source_qty,
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
              reason=None, trade_id: str | None = None, **detail) -> None:
    """Append a trade row, suppressing a previously committed close replay.

    ``trade_id`` is deliberately supplied by the close lifecycle, rather than
    generated here: it must be stable across a restart while the broker's
    terminal close order and the JSON active-trade removal settle separately.
    The unique SQLite index is the final race boundary; the read avoids a
    noisy integrity error on the normal replay path.
    """
    if trade_id is None:
        candidate = detail.get("trade_id")
        if candidate is not None:
            trade_id = str(candidate)
    if trade_id is not None:
        trade_id = str(trade_id)
        ensure_ready()
        try:
            with sqlite3.connect(JOURNAL_FILE, timeout=5,
                                 factory=_ClosingConnection) as db:
                db.execute("PRAGMA busy_timeout=5000")
                row = db.execute(
                    "SELECT 1 FROM trades WHERE trade_id=? LIMIT 1",
                    (trade_id,),
                ).fetchone()
            if row is not None:
                return
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            raise JournalNotReady(f"journal trade idempotency check failed: {exc}") from exc
    row = {"ts": time.time(), "symbol": symbol, "side": side,
           "action": action, "qty": float(qty) if qty is not None else None,
           "price": float(price) if price is not None else None,
           "notional": float(notional) if notional is not None else None,
           "reason": reason, "trade_id": trade_id, **detail}
    try:
        _journal_insert("trades", row)
    except JournalNotReady:
        # Two recovery workers can pass the read above concurrently.  The
        # partial unique index turns the loser into a journal error; if the
        # winning row is now visible, treat that race as the intended replay
        # no-op rather than reporting a false durability failure.
        if trade_id is not None:
            try:
                with sqlite3.connect(JOURNAL_FILE, timeout=5,
                                     factory=_ClosingConnection) as db:
                    db.execute("PRAGMA busy_timeout=5000")
                    duplicate = db.execute(
                        "SELECT 1 FROM trades WHERE trade_id=? LIMIT 1",
                        (trade_id,),
                    ).fetchone()
                if duplicate is not None:
                    return
            except (OSError, sqlite3.Error, RuntimeError):
                pass
        raise
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
