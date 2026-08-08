"""Small paper-runtime state and append-only operational journal."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
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
JOURNAL_FILE = RUNTIME / "journal.db"

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
    "orders": {},
    "risk_day": {},
    "last_reconciliation_ts": None,
    "preflight": {},
}


class RuntimeIdentityError(RuntimeError):
    """Runtime files are not scoped to the requested paper account."""


class JournalNotReady(RuntimeError):
    """The durable state/journal is unavailable for an order-bearing action."""


class _ClosingConnection(sqlite3.Connection):
    """Close SQLite handles when used as context managers on Python 3.14+."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


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
    if mode not in {"paper", "test", "_unconfigured"}:
        raise ValueError("runtime mode must be paper, test, or _unconfigured")
    root = Path(base) if base is not None else RUNTIME_BASE
    return _set_paths(root / mode, mode)


def account_fingerprint(mode: str, api_key: str) -> str:
    if mode != "paper" or not isinstance(api_key, str) or not api_key:
        raise RuntimeIdentityError("paper mode and API key are required")
    digest = hashlib.sha256(f"alpaca-agent\0paper\0{api_key}".encode()).hexdigest()[:20]
    return f"alpaca-paper-{digest}"


def bind_account_identity(fingerprint: str) -> dict:
    """Bind this runtime directory to one paper account identity.

    A state directory must never silently change accounts.  The fingerprint is
    intentionally non-secret and may be displayed by the read-only dashboard.
    """
    if not isinstance(fingerprint, str) or not fingerprint:
        raise RuntimeIdentityError("a paper-account fingerprint is required")
    value = load_state()
    existing = value.get("account_fingerprint")
    if existing and existing != fingerprint:
        raise RuntimeIdentityError("runtime state belongs to a different paper account")
    value["account_fingerprint"] = fingerprint
    value["runtime_mode"] = "paper"
    return save_state(value)


def bind_runtime_identity(mode: str, api_key: str, account_id: str | None = None) -> str:
    """Bind a paper runtime using provider credentials and optional account id."""
    if mode != "paper":
        raise RuntimeIdentityError("only paper runtime identity is supported")
    material = f"{api_key}\0{account_id}" if account_id else api_key
    fingerprint = account_fingerprint(mode, material)
    bind_account_identity(fingerprint)
    return fingerprint


def _validated(data: Mapping[str, Any]) -> dict:
    # Drop fields from older state formats instead of propagating them into
    # the paper control file.
    value = deepcopy(DEFAULT)
    value.update({key: data[key] for key in DEFAULT if key in data})
    if value.get("state") not in {RUNNING, PAUSED, DAY_STOPPED, KILLED}:
        raise ValueError("invalid state")
    if value.get("runtime_mode") not in {None, "paper", "test"}:
        raise ValueError("runtime_mode must be paper or test")
    fingerprint = value.get("account_fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str)
                                    or not fingerprint.startswith("alpaca-paper-")
                                    or len(fingerprint) > 80):
        raise ValueError("account_fingerprint is invalid")
    for key in ("active_trades", "recent_setups", "protection", "opened_at",
                "orders", "risk_day", "preflight"):
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


_JOURNAL_TABLES = {
    "events": """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL,
        run_id TEXT, cycle_id TEXT, strategy_id TEXT, strategy_version TEXT,
        setup_id TEXT, prompt_version TEXT, config_version TEXT,
        code_version TEXT, equity_basis_id TEXT, runtime_mode TEXT,
        account_fingerprint TEXT, variant_id TEXT,
        strategy_config_version TEXT
    )""",
    "orders": """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        order_id TEXT, client_order_id TEXT, symbol TEXT, side TEXT,
        action TEXT, qty REAL, type TEXT, time_in_force TEXT, status TEXT,
        filled_qty REAL, filled_avg_price REAL, reason TEXT,
        run_id TEXT, cycle_id TEXT, runtime_mode TEXT,
        account_fingerprint TEXT, setup_id TEXT
    )""",
    "trades": """CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        symbol TEXT, side TEXT, action TEXT, qty REAL, price REAL,
        notional REAL, leverage REAL, reason TEXT, confidence REAL,
        pnl_pct REAL, trade_id TEXT, order_id TEXT, fee_usd REAL,
        funding_usd REAL, realized_pnl_usd REAL, risk_usd REAL,
        fill_status TEXT, slippage_usd REAL, adverse_slippage_usd REAL,
        funding_status TEXT, pnl_semantics TEXT, strategy_id TEXT,
        strategy_version TEXT, setup_id TEXT, setup_key TEXT, setup_type TEXT,
        signal_ts REAL, exit_policy TEXT, invalidation_anchor TEXT,
        run_id TEXT, cycle_id TEXT, runtime_mode TEXT,
        account_fingerprint TEXT, variant_id TEXT,
        strategy_config_version TEXT, close_trigger TEXT, close_evidence TEXT,
        entry_equity_usd REAL
    )""",
    "equity": """CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        equity REAL, state TEXT, basis_id TEXT, run_id TEXT, cycle_id TEXT,
        runtime_mode TEXT, account_fingerprint TEXT
    )""",
    "runs": """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, started_ts REAL NOT NULL, mode TEXT,
        strategy_id TEXT, strategy_version TEXT, model TEXT,
        prompt_version TEXT, config_version TEXT, code_version TEXT,
        account_fingerprint TEXT
    )""",
    "schema_meta": """CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""",
}


def initialize_journal() -> Path:
    """Create/migrate the append-only operational journal and state directory."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(JOURNAL_FILE, timeout=5,
                             factory=_ClosingConnection) as db:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            for ddl in _JOURNAL_TABLES.values():
                db.execute(ddl)
            # Older deployed databases predate the orders table and may omit
            # a few optional columns.  Add only columns required by the
            # runtime writer; existing report/dashboard readers remain valid.
            required = {
                "events": {"run_id": "TEXT", "cycle_id": "TEXT", "runtime_mode": "TEXT", "account_fingerprint": "TEXT"},
                "orders": {"reason": "TEXT", "run_id": "TEXT", "cycle_id": "TEXT", "runtime_mode": "TEXT", "account_fingerprint": "TEXT", "setup_id": "TEXT"},
                "trades": {"run_id": "TEXT", "cycle_id": "TEXT", "runtime_mode": "TEXT", "account_fingerprint": "TEXT"},
                "equity": {"run_id": "TEXT", "cycle_id": "TEXT", "runtime_mode": "TEXT", "account_fingerprint": "TEXT"},
            }
            for table, columns in required.items():
                cursor = db.execute(f"PRAGMA table_info({table})")
                present = {row[1] for row in cursor.fetchall()}
                cursor.close()
                for name, typ in columns.items():
                    if name not in present:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
            db.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                       ("runtime_schema", "2"))
            db.commit()
    except (OSError, sqlite3.Error) as exc:
        raise JournalNotReady(f"paper journal unavailable: {exc}") from exc
    return JOURNAL_FILE


def journal_ready() -> bool:
    try:
        initialize_journal()
        with sqlite3.connect(JOURNAL_FILE, timeout=1,
                             factory=_ClosingConnection) as db:
            cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            cursor.close()
        return {"events", "orders", "trades", "equity"}.issubset(tables)
    except (OSError, sqlite3.Error, JournalNotReady):
        return False


def ensure_ready() -> None:
    """Make state and journal readiness an explicit pre-order invariant."""
    initialize_journal()
    if not STATE_FILE.exists():
        initial = deepcopy(DEFAULT)
        initial["runtime_mode"] = "paper" if RUNTIME_SCOPE == "paper" else None
        save_state(initial)
    if not journal_ready():
        raise JournalNotReady("paper journal schema is incomplete")


def check_journal() -> None:
    """Strict readiness probe for startup callers."""
    ensure_ready()


def _journal_insert(table: str, payload: Mapping[str, Any]) -> None:
    ensure_ready()
    try:
        with sqlite3.connect(JOURNAL_FILE, timeout=5,
                             factory=_ClosingConnection) as db:
            db.execute("PRAGMA busy_timeout=5000")
            cursor = db.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in cursor.fetchall()}
            cursor.close()
            row = {str(key): value for key, value in payload.items()
                   if str(key) in columns}
            if not row:
                raise JournalNotReady(f"journal table {table!r} has no writable columns")
            names = list(row)
            placeholders = ",".join("?" for _ in names)
            db.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders})",
                       [row[name] for name in names])
            db.commit()
    except (OSError, sqlite3.Error) as exc:
        raise JournalNotReady(f"journal write failed: {exc}") from exc


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
    # SQLite is the authoritative ledger consumed by report/dashboard.  The
    # append-only JSONL mirror remains useful for direct operator inspection,
    # but never let a successful order exist without its durable SQLite event.
    _journal_insert("events", row)
    try:
        _append(EVENTS_FILE, row)
    except OSError:
        pass


def log_order(order=None, request=None, *, action: str = "submit",
              reason: str | None = None, **detail) -> None:
    """Persist an order lifecycle row in the durable paper journal."""
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
    except OSError:
        pass


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


def read_pid() -> int | None:
    try: return int(PID_FILE.read_text().strip())
    except (OSError, ValueError): return None


def pid_alive(pid: int) -> bool:
    try: os.kill(pid, 0); return True
    except OSError: return False


if os.getenv("ALPACA_AGENT_RUNTIME_SCOPE"):
    configure_runtime(os.environ["ALPACA_AGENT_RUNTIME_SCOPE"])
