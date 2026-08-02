"""Agent state file, PID management and SQLite journal.

The mode-scoped state file (runtime/demo/state.json or runtime/live/state.json)
is the control channel between the CLI and running loop. The CLI writes it;
the loop reads it every cycle.
"""

import fcntl
import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

RUNTIME_BASE = Path(
    os.getenv("OKX_AGENT_RUNTIME_ROOT")
    or Path(__file__).resolve().parent.parent / "runtime"
)
RUNTIME_SCOPE = "_unconfigured"
RUNTIME = RUNTIME_BASE / RUNTIME_SCOPE
STATE_FILE = RUNTIME / "state.json"
PID_FILE = RUNTIME / "agent.pid"
DB_FILE = RUNTIME / "journal.db"
STATE_LOCK_FILE = RUNTIME / "state.lock"
HEARTBEAT_FILE = RUNTIME / "heartbeat.json"

log = logging.getLogger("state")

RUNNING = "RUNNING"          # normal operation: manage positions, open new ones
PAUSED = "PAUSED"            # housekeeping only: no LLM calls, no new entries
DAY_STOPPED = "DAY_STOPPED"  # daily loss limit hit: model may close, cannot open
KILLED = "KILLED"            # terminal: flatten everything and exit
EQUITY_BASIS = "usdt_currency_equity_v1"
# 4: variant_id and strategy_config_version on events and trades (B0).
JOURNAL_SCHEMA_VERSION = 4

_JOURNAL_CONTEXT: dict[str, object] = {}


class RuntimeIdentityError(RuntimeError):
    """Runtime files belong to a different mode or API-key identity."""


def _set_runtime_paths(runtime: Path, scope: str) -> Path:
    global RUNTIME_SCOPE, RUNTIME, STATE_FILE, PID_FILE, DB_FILE
    global STATE_LOCK_FILE, HEARTBEAT_FILE
    RUNTIME_SCOPE = scope
    RUNTIME = runtime
    STATE_FILE = runtime / "state.json"
    PID_FILE = runtime / "agent.pid"
    DB_FILE = runtime / "journal.db"
    STATE_LOCK_FILE = runtime / "state.lock"
    HEARTBEAT_FILE = runtime / "heartbeat.json"
    _JOURNAL_CONTEXT.clear()
    return runtime


def _legacy_runtime_mode(base: Path) -> str | None:
    """Read the most recent recorded mode from the pre-isolation journal."""
    legacy_db = base / "journal.db"
    if not legacy_db.exists():
        return None
    try:
        with sqlite3.connect(legacy_db) as conn:
            row = conn.execute(
                "SELECT mode FROM runs WHERE mode IN ('demo','live') "
                "ORDER BY started_ts DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def _migrate_legacy_runtime(base: Path, target: Path, mode: str) -> None:
    """Copy one provably matching legacy runtime into its mode directory.

    Journals from releases before runtime isolation lived directly under
    ``runtime/``. A matching ``runs.mode`` is required before any copy, so a
    demo journal can never be guessed to be live (or vice versa). The source
    remains as a read-only rollback artifact.
    """
    if ((target / "state.json").exists()
            or (target / "journal.db").exists()
            or _legacy_runtime_mode(base) != mode):
        return
    legacy_state = base / "state.json"
    legacy_db = base / "journal.db"
    if not legacy_state.exists() and not legacy_db.exists():
        return
    legacy_pid = base / "agent.pid"
    try:
        pid = int(legacy_pid.read_text().strip())
    except Exception:
        pid = None
    if pid and pid_alive(pid):
        raise RuntimeIdentityError(
            "the legacy agent loop is still running; stop it before "
            "migrating runtime files")
    target.mkdir(parents=True, exist_ok=True)
    if legacy_state.exists():
        shutil.copy2(legacy_state, target / "state.json")
    if legacy_db.exists():
        source = sqlite3.connect(
            f"file:{legacy_db}?mode=ro", uri=True)
        destination = sqlite3.connect(target / "journal.db")
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    legacy_log = base / "agent.log"
    if legacy_log.exists():
        shutil.copy2(legacy_log, target / "agent.log")


def configure_runtime(mode: str, base: Path | None = None) -> Path:
    """Select an isolated runtime before any state, PID or journal access."""
    if mode not in {"demo", "live", "test", "_unconfigured"}:
        raise ValueError("runtime mode must be demo, live, or test")
    root = Path(base) if base is not None else RUNTIME_BASE
    target = root / mode
    if mode in {"demo", "live"}:
        _migrate_legacy_runtime(root, target, mode)
    return _set_runtime_paths(target, mode)


def account_fingerprint(mode: str, api_key: str) -> str:
    """Return a non-secret stable identity for one key/mode combination."""
    if mode not in {"demo", "live"} or not isinstance(api_key, str) \
            or not api_key:
        raise RuntimeIdentityError(
            "cannot identify runtime without a valid mode and OKX API key")
    digest = hashlib.sha256(
        f"okx-agent\0{mode}\0{api_key}".encode("utf-8")
    ).hexdigest()[:20]
    return f"okx-{mode}-{digest}"

DEFAULT = {
    "state": PAUSED,
    # Bound on first credentialed access. These fields prevent a state file
    # from being reused with another API key or across demo/live.
    "runtime_mode": None,
    "account_fingerprint": None,
    "high_water_mark": None,
    "day": None,
    "day_start_equity": None,
    # None identifies state written before USDT-only equity was introduced.
    # The engine rebases legacy benchmarks once before evaluating breakers.
    "equity_basis": None,
    # A basis name describes the valuation method; a basis ID identifies one
    # continuous equity segment. Reports never bridge returns across segments.
    "equity_basis_id": None,
    "last_ledger_ts": 0,
    # Stable OKX ledger identifiers already applied to equity benchmarks.
    "processed_transfer_ids": {},
    # Unidentified ledger entries are never presented as exactly deduplicated.
    "transfer_reconciliation_required": {},
    "cooldowns": {},   # symbol -> unix ts until which no new entries are allowed
    # Recent execution feedback. The LLM may reason about one smaller retry;
    # repeated depth failures acquire a deterministic temporary backoff.
    "entry_feedback": {},
    # Non-liquidity entry failures use a separate persistent backoff. Keeping
    # these records distinct prevents a generic exchange error from being
    # mistaken for permission to retry at a smaller size.
    "entry_failures": {},
    "opened_at": {},   # symbol -> unix ts the position was opened
    "active_trades": {},  # symbol -> durable entry/fill/risk metadata
    "protection": {},     # symbol -> intended exchange-side SL/TP metadata
    # setup_id -> bounded decision/execution history. This is operational
    # idempotency state, not a self-modifying trading memory.
    "recent_setups": {},
    "kill_reason": None,
    "flatten_on_kill": True,  # False only for explicit kill --keep-positions
    "operator_pause": False,  # True = pause was an explicit CLI command and
                              # must survive crashes and restarts
}


def _default() -> dict:
    return deepcopy(DEFAULT)


def _validate(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("state root is not an object")
    merged = {**_default(), **data}
    if merged.get("state") not in {RUNNING, PAUSED, DAY_STOPPED, KILLED}:
        raise ValueError("invalid state transition value")
    runtime_mode = merged.get("runtime_mode")
    if runtime_mode not in {None, "demo", "live"}:
        raise ValueError("state.runtime_mode is invalid")
    account_id = merged.get("account_fingerprint")
    if account_id is not None and (
            not isinstance(account_id, str)
            or not account_id.startswith("okx-")
            or len(account_id) > 80):
        raise ValueError("state.account_fingerprint is invalid")
    if (runtime_mode is None) != (account_id is None):
        raise ValueError(
            "state runtime mode and account fingerprint must be bound together")
    for key in ("cooldowns", "entry_feedback", "entry_failures", "opened_at",
                "active_trades", "protection", "recent_setups",
                "processed_transfer_ids", "transfer_reconciliation_required"):
        if not isinstance(merged.get(key), dict):
            raise ValueError(f"state.{key} is not an object")
    for key in ("high_water_mark", "day_start_equity"):
        value = merged.get(key)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float))
                                  or not math.isfinite(float(value))
                                  or float(value) < 0):
            raise ValueError(f"state.{key} is invalid")
    ledger = merged.get("last_ledger_ts")
    if (isinstance(ledger, bool) or not isinstance(ledger, (int, float))
            or not math.isfinite(float(ledger)) or float(ledger) < 0):
        raise ValueError("state.last_ledger_ts is invalid")
    for transfer_id, timestamp in merged["processed_transfer_ids"].items():
        if (not isinstance(transfer_id, str) or not transfer_id
                or len(transfer_id) > 200 or isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or not math.isfinite(float(timestamp)) or float(timestamp) < 0):
            raise ValueError("state.processed_transfer_ids contains invalid data")
    for key, record in merged["transfer_reconciliation_required"].items():
        if (not isinstance(key, str) or not key or len(key) > 240
                or not isinstance(record, dict)):
            raise ValueError(
                "state.transfer_reconciliation_required contains invalid data")
    day = merged.get("day")
    if day is not None and (not isinstance(day, str) or len(day) != 10):
        raise ValueError("state.day is invalid")
    if merged.get("equity_basis") not in {None, EQUITY_BASIS}:
        raise ValueError("state.equity_basis is invalid")
    basis_id = merged.get("equity_basis_id")
    if basis_id is not None and (
            not isinstance(basis_id, str) or not basis_id.strip()
            or len(basis_id) > 160):
        raise ValueError("state.equity_basis_id is invalid")
    if basis_id is not None and merged.get("equity_basis") != EQUITY_BASIS:
        raise ValueError(
            "state.equity_basis_id requires the USDT equity basis")
    for block_name in ("cooldowns", "opened_at"):
        for symbol, value in merged[block_name].items():
            if (not isinstance(symbol, str) or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(f"state.{block_name} contains invalid data")
    for symbol, feedback in merged["entry_feedback"].items():
        if (not isinstance(symbol, str) or not symbol
                or not isinstance(feedback, dict)):
            raise ValueError("state.entry_feedback contains invalid data")
        if (not isinstance(feedback.get("reason"), str)
                or not feedback["reason"]
                or feedback.get("direction") not in {"long", "short"}):
            raise ValueError(f"state.entry_feedback.{symbol} is invalid")
        count = feedback.get("consecutive_rejections")
        if (isinstance(count, bool) or not isinstance(count, int)
                or count < 1):
            raise ValueError(
                f"state.entry_feedback.{symbol}.consecutive_rejections "
                "is invalid")
        numeric = (
            "last_rejected_at", "expires_at", "blocked_until",
            "requested_contracts", "available_contracts",
            "requested_notional_usdt", "available_notional_usdt",
            "available_ratio", "max_retry_size_pct_equity",
            "max_slippage_pct",
        )
        for key in numeric:
            value = feedback.get(key)
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(
                    f"state.entry_feedback.{symbol}.{key} is invalid")
        if (float(feedback["requested_contracts"]) <= 0
                or float(feedback["requested_notional_usdt"]) <= 0
                or float(feedback["available_contracts"])
                >= float(feedback["requested_contracts"])
                or float(feedback["available_ratio"]) > 1
                or float(feedback["max_retry_size_pct_equity"]) > 100
                or float(feedback["max_slippage_pct"]) > 5
                or float(feedback["expires_at"])
                < float(feedback["last_rejected_at"])
                or float(feedback["blocked_until"])
                > float(feedback["expires_at"])):
            raise ValueError(f"state.entry_feedback.{symbol} is invalid")
    for symbol, failure in merged["entry_failures"].items():
        if (not isinstance(symbol, str) or not symbol
                or not isinstance(failure, dict)):
            raise ValueError("state.entry_failures contains invalid data")
        required_strings = ("reason", "stage", "error_message", "direction")
        if any(not isinstance(failure.get(key), str)
               or not failure[key] for key in required_strings):
            raise ValueError(f"state.entry_failures.{symbol} is invalid")
        if failure["direction"] not in {"long", "short"}:
            raise ValueError(f"state.entry_failures.{symbol} is invalid")
        if failure.get("classification") not in {"transient", "permanent"}:
            raise ValueError(f"state.entry_failures.{symbol} is invalid")
        error_code = failure.get("error_code")
        if error_code is not None and not isinstance(error_code, str):
            raise ValueError(f"state.entry_failures.{symbol} is invalid")
        count = failure.get("consecutive_failures")
        if (isinstance(count, bool) or not isinstance(count, int)
                or count < 1):
            raise ValueError(
                f"state.entry_failures.{symbol}.consecutive_failures "
                "is invalid")
        for key in ("last_failed_at", "blocked_until", "expires_at"):
            value = failure.get(key)
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(
                    f"state.entry_failures.{symbol}.{key} is invalid")
        if (float(failure["blocked_until"])
                < float(failure["last_failed_at"])
                or float(failure["expires_at"])
                < float(failure["blocked_until"])):
            raise ValueError(f"state.entry_failures.{symbol} is invalid")
    for symbol, trade in merged["active_trades"].items():
        if not isinstance(symbol, str) or not isinstance(trade, dict):
            raise ValueError("state.active_trades contains invalid data")
        if (not isinstance(trade.get("trade_id"), str)
                or not trade["trade_id"]
                or trade.get("direction") not in {"long", "short"}):
            raise ValueError(f"state.active_trades.{symbol} is invalid")
        for key in ("opened_at", "entry_price", "entry_notional", "qty"):
            value = trade.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(
                    f"state.active_trades.{symbol}.{key} is invalid")
        if float(trade["qty"]) <= 0:
            raise ValueError(f"state.active_trades.{symbol}.qty is invalid")
        age_known = trade.get("age_known", True)
        if not isinstance(age_known, bool):
            raise ValueError(
                f"state.active_trades.{symbol}.age_known is invalid")
        if age_known and float(trade["opened_at"]) <= 0:
            raise ValueError(
                f"state.active_trades.{symbol}.opened_at is invalid")
        for key in ("initial_qty", "leverage", "entry_fee_usd",
                    "entry_fee_remaining_usd", "risk_usd"):
            value = trade.get(key)
            if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(
                    f"state.active_trades.{symbol}.{key} is invalid")
        partial_pnl = trade.get("partial_realized_pnl_usd")
        if partial_pnl is not None and (
                isinstance(partial_pnl, bool)
                or not isinstance(partial_pnl, (int, float))
                or not math.isfinite(float(partial_pnl))):
            raise ValueError(
                f"state.active_trades.{symbol}.partial_realized_pnl_usd "
                "is invalid")
        for key in (
                "strategy_id", "strategy_version", "setup_id", "setup_key",
                "setup_type", "run_id", "cycle_id", "exit_policy",
                "invalidation_anchor", "entry_reason"):
            value = trade.get(key)
            if value is not None and (
                    not isinstance(value, str) or not value.strip()
                    or len(value) > 200):
                raise ValueError(
                    f"state.active_trades.{symbol}.{key} is invalid")
        signal_ts = trade.get("signal_ts")
        if signal_ts is not None and (
                isinstance(signal_ts, bool)
                or not isinstance(signal_ts, (int, float))
                or not math.isfinite(float(signal_ts))
                or float(signal_ts) < 0):
            raise ValueError(
                f"state.active_trades.{symbol}.signal_ts is invalid")
        for key in ("stop_loss_pct", "take_profit_pct"):
            value = trade.get(key)
            if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) <= 0):
                raise ValueError(
                    f"state.active_trades.{symbol}.{key} is invalid")
        evidence = trade.get("entry_evidence")
        if evidence is not None:
            if not isinstance(evidence, dict):
                raise ValueError(
                    f"state.active_trades.{symbol}.entry_evidence is invalid")
            try:
                encoded_evidence = json.dumps(
                    evidence, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError):
                raise ValueError(
                    f"state.active_trades.{symbol}.entry_evidence is invalid")
            if len(encoded_evidence) > 6000:
                raise ValueError(
                    f"state.active_trades.{symbol}.entry_evidence is too large")
    for symbol, target in merged["protection"].items():
        if (not isinstance(symbol, str) or not isinstance(target, dict)
                or target.get("side") not in {"long", "short"}):
            raise ValueError(f"state.protection.{symbol} is invalid")
        contracts = target.get("contracts")
        if (isinstance(contracts, bool)
                or not isinstance(contracts, (int, float))
                or not math.isfinite(float(contracts))
                or float(contracts) <= 0):
            raise ValueError(f"state.protection.{symbol}.contracts is invalid")
        for key in ("sl_price", "tp_price"):
            value = target.get(key)
            if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or float(value) <= 0):
                raise ValueError(f"state.protection.{symbol}.{key} is invalid")
    allowed_setup_statuses = {
        "proposed", "risk_rejected", "attempted", "execution_rejected",
        "opened", "closed",
    }
    for setup_id, record in merged["recent_setups"].items():
        if (not isinstance(setup_id, str) or not setup_id
                or len(setup_id) > 200
                or not isinstance(record, dict)):
            raise ValueError("state.recent_setups contains invalid data")
        required_strings = (
            "strategy_id", "strategy_version", "setup_key", "setup_type",
            "symbol", "direction", "status",
        )
        if any(not isinstance(record.get(key), str) or not record[key]
               or len(record[key]) > 200
               for key in required_strings):
            raise ValueError(f"state.recent_setups.{setup_id} is invalid")
        if (record["direction"] not in {"long", "short"}
                or record["status"] not in allowed_setup_statuses):
            raise ValueError(f"state.recent_setups.{setup_id} is invalid")
        for key in (
                "signal_ts", "first_seen_at", "last_seen_at",
                "blocked_until", "expires_at"):
            value = record.get(key)
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0):
                raise ValueError(
                    f"state.recent_setups.{setup_id}.{key} is invalid")
        if (float(record["last_seen_at"]) < float(record["first_seen_at"])
                or float(record["blocked_until"]) > float(record["expires_at"])):
            raise ValueError(f"state.recent_setups.{setup_id} is invalid")
        outcome = record.get("outcome")
        if outcome not in {None, "win", "loss", "flat", "unknown"}:
            raise ValueError(
                f"state.recent_setups.{setup_id}.outcome is invalid")
        for key in ("closed_at", "realized_pnl_usd", "entry_signal_1h_ts"):
            value = record.get(key)
            if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))):
                raise ValueError(
                    f"state.recent_setups.{setup_id}.{key} is invalid")
        evidence_id = record.get("entry_evidence_fingerprint")
        if evidence_id is not None and (
                not isinstance(evidence_id, str)
                or not evidence_id or len(evidence_id) > 80):
            raise ValueError(
                f"state.recent_setups.{setup_id} evidence is invalid")
    if not isinstance(merged.get("operator_pause"), bool):
        raise ValueError("state.operator_pause is not boolean")
    if not isinstance(merged.get("flatten_on_kill"), bool):
        raise ValueError("state.flatten_on_kill is not boolean")
    reason = merged.get("kill_reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("state.kill_reason is invalid")
    return merged


@contextmanager
def _state_lock():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock_file = RUNTIME / STATE_LOCK_FILE.name
    with lock_file.open("a+") as handle:
        os.chmod(lock_file, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_atomic(st: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(
        f"{STATE_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(st, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)
        try:
            directory = os.open(RUNTIME, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # Some filesystems do not support directory fsync; the state file
            # itself has still been flushed before the atomic replacement.
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _ensure_unlocked() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        _write_atomic(_default())


def _load_state_unlocked() -> dict:
    _ensure_unlocked()
    try:
        return _validate(json.loads(STATE_FILE.read_text()))
    except Exception as exc:
        # Never resume trading from guessed defaults. Preserve the bad file,
        # replace it with an explicit KILLED state, and require a human
        # acknowledgement before the process can run again.
        stamp = int(time.time() * 1000)
        backup = STATE_FILE.with_name(
            f"state.corrupt.{stamp}.{uuid.uuid4().hex[:8]}.json")
        try:
            os.replace(STATE_FILE, backup)
        except Exception:
            backup = None
        safe = _default()
        safe["state"] = KILLED
        safe["operator_pause"] = True
        safe["kill_reason"] = (
            "state file was corrupt; preserved at " + str(backup)
            if backup else "state file was corrupt and could not be preserved"
        )
        _write_atomic(safe)
        log.critical("Corrupt state detected (%s); agent forced to KILLED", exc)
        return safe


def load_state() -> dict:
    with _state_lock():
        return _load_state_unlocked()


def save_state(st: dict) -> None:
    with _state_lock():
        _write_atomic(_validate(st))


def set_state(name: str, reason: str | None = None, **extra) -> dict:
    with _state_lock():
        st = _load_state_unlocked()
        st["state"] = name
        if reason is not None:
            st["kill_reason"] = reason
        st.update(extra)
        _write_atomic(_validate(st))
        return st


def write_heartbeat(status: str, **detail) -> dict:
    """Atomically publish a small, non-secret process-health record.

    The heartbeat is deliberately separate from ``state.json``: health
    polling must not contend with the operator control channel or make a
    trading-state transition. Callers must pass only bounded operational
    metadata, never credentials, request headers, or raw provider errors.
    """
    if status not in {
            "starting", "running", "degraded", "pausing", "paused",
            "killed", "stopped"}:
        raise ValueError(f"unsupported heartbeat status {status!r}")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "status": status,
        "updated_ts": time.time(),
        "pid": os.getpid(),
        "runtime_mode": (
            RUNTIME_SCOPE if RUNTIME_SCOPE in {"demo", "live"} else None),
        **detail,
    }
    tmp = HEARTBEAT_FILE.with_name(
        f"{HEARTBEAT_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, HEARTBEAT_FILE)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return payload


def bind_runtime_identity(mode: str, api_key: str) -> str:
    """Bind this mode's files to one hashed key identity, or fail closed."""
    if RUNTIME_SCOPE != mode:
        raise RuntimeIdentityError(
            f"runtime is scoped to {RUNTIME_SCOPE}, not {mode}")
    fingerprint = account_fingerprint(mode, api_key)
    with _state_lock():
        st = _load_state_unlocked()
        existing_mode = st.get("runtime_mode")
        existing_fingerprint = st.get("account_fingerprint")
        if existing_mode is None and existing_fingerprint is None:
            st["runtime_mode"] = mode
            st["account_fingerprint"] = fingerprint
            _write_atomic(_validate(st))
        elif (existing_mode != mode
              or existing_fingerprint != fingerprint):
            raise RuntimeIdentityError(
                f"{RUNTIME} belongs to {existing_mode or 'unknown mode'} / "
                f"{existing_fingerprint or 'unknown account'}; refusing to "
                "reuse it with another OKX key")
    set_journal_context(
        runtime_mode=mode, account_fingerprint=fingerprint)
    return fingerprint


# Keys the trading loop owns. commit() persists these without clobbering a
# state change (pause/kill) the CLI may have written while a cycle was running.
LOOP_KEYS = ("high_water_mark", "day", "day_start_equity", "equity_basis",
             "equity_basis_id",
             "last_ledger_ts", "processed_transfer_ids",
             "transfer_reconciliation_required", "cooldowns", "entry_feedback",
             "entry_failures", "opened_at", "active_trades", "protection",
             "recent_setups")


def commit(st: dict, transition: tuple[str, str] | None = None,
           kill: str | None = None) -> dict:
    """Merge the loop's bookkeeping into the state file.

    The file is reloaded first because the CLI may have set PAUSED or KILLED
    mid-cycle; a plain save_state(st) with the loop's stale copy would erase
    that command. A state change is applied only as a compare-and-set
    `transition=(from, to)` or an unconditional `kill=reason`. `st` is
    updated in place to the merged result so the caller sees CLI changes.
    """
    with _state_lock():
        cur = _load_state_unlocked()
        for k in LOOP_KEYS:
            if k in st:
                cur[k] = st[k]
        if kill is not None:
            cur["state"] = KILLED
            cur["kill_reason"] = kill
            cur["flatten_on_kill"] = True
        elif transition and cur["state"] == transition[0]:
            cur["state"] = transition[1]
        _write_atomic(_validate(cur))
        st.clear()
        st.update(cur)
        return st


# ---------------------------------------------------------------- PID file

def acquire_run_lock():
    """Atomically acquire the single-agent lock and publish this PID."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    handle = PID_FILE.open("a+")
    os.chmod(PID_FILE, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        return handle
    except Exception:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        raise


def release_run_lock(handle) -> None:
    if handle is None:
        return
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
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- journal


class JournalError(RuntimeError):
    """The durable trading audit trail could not be written."""


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection, then release the DB file."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def new_cycle_id() -> str:
    return f"cycle_{uuid.uuid4().hex}"


def new_equity_basis_id() -> str:
    return f"{EQUITY_BASIS}_{uuid.uuid4().hex[:16]}"


def stable_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


# Explicit allow-list of configuration that can change proposal generation,
# market eligibility, evaluation, sizing, execution, or outcome accounting.
# Secrets are environment variables and never appear here; alerts and research
# storage/scheduling settings cannot change an individual arm's decision.
EXPERIMENT_FINGERPRINT_BLOCKS = (
    "llm", "strategy", "universe", "cycle", "risk", "execution",
    "trading_costs",
)
SENSITIVE_CONFIG_KEYS = {
    "api_key", "apikey", "secret", "client_secret", "password",
    "credential", "credentials", "access_token", "refresh_token",
    "webhook_url",
}


def _secret_free_config(value):
    if isinstance(value, dict):
        return {
            key: _secret_free_config(item)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_CONFIG_KEYS
        }
    if isinstance(value, list):
        return [_secret_free_config(item) for item in value]
    return deepcopy(value)


def experiment_fingerprint_material(cfg: dict) -> dict:
    """Return the secret-free executable inputs that define an experiment."""
    return _secret_free_config({
        "mode": cfg.get("mode"),
        **{block: cfg.get(block) for block in EXPERIMENT_FINGERPRINT_BLOCKS},
    })


def experiment_fingerprint(cfg: dict) -> str:
    """Fingerprint every configured input that can change an experiment."""
    return stable_fingerprint(experiment_fingerprint_material(cfg))


def strategy_fingerprint(cfg: dict) -> str:
    """Compatibility name for the executable experiment fingerprint.

    ``stable_fingerprint`` hashes the whole config, which means raising
    ``alerts.timeout_seconds`` or changing the findings-store path forks the
    attribution bucket even though neither can alter an arm's decisions.

    This allow-listed hash covers mode, LLM provider/model/sampling settings,
    universe selection, the full cycle/cadence input, strategy, risk,
    execution, costs and timeframes. Two runs may be pooled only when every
    executable input agrees. API keys and alert credentials are environment
    data and are never included or persisted.

    The whole-config ``config_version`` is still written alongside it. This
    is an addition, not a replacement: existing rows keep their meaning.
    """
    return experiment_fingerprint(cfg)


def code_fingerprint() -> str:
    """Hash trading-runtime sources without depending on git metadata."""
    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    files = sorted((root / "agent").glob("*.py"))
    if (root / "main.py").exists():
        files.append(root / "main.py")
    for path in files:
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def set_journal_context(**context: object) -> dict:
    """Set process-local IDs automatically attached to subsequent records."""
    allowed = {
        "run_id", "cycle_id", "strategy_id", "strategy_version",
        "prompt_version", "config_version", "code_version",
        "equity_basis_id", "runtime_mode", "account_fingerprint",
        # B0: attribution keys off a readable variant name, never off an
        # opaque whole-config hash. Live trading writes variant_id "live".
        "variant_id", "strategy_config_version",
    }
    unknown = set(context) - allowed
    if unknown:
        raise ValueError(
            "unknown journal context field(s): " + ", ".join(sorted(unknown)))
    for key, value in context.items():
        if value is None:
            _JOURNAL_CONTEXT.pop(key, None)
        else:
            text = str(value)
            if not text or len(text) > 200:
                raise ValueError(f"journal context {key} is invalid")
            _JOURNAL_CONTEXT[key] = text
    return dict(_JOURNAL_CONTEXT)


def journal_context() -> dict:
    return dict(_JOURNAL_CONTEXT)


def _ensure_columns(conn: sqlite3.Connection, table: str,
                    columns: dict[str, str]) -> None:
    existing = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    for column, kind in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def _db() -> sqlite3.Connection:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        DB_FILE, timeout=5, factory=_ClosingConnection)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "ts REAL, kind TEXT, payload TEXT, run_id TEXT, cycle_id TEXT, "
        "strategy_id TEXT, strategy_version TEXT, setup_id TEXT, "
        "prompt_version TEXT, config_version TEXT, code_version TEXT, "
        "equity_basis_id TEXT, runtime_mode TEXT, "
        "account_fingerprint TEXT)"
    )
    _ensure_columns(conn, "events", {
        "run_id": "TEXT", "cycle_id": "TEXT", "strategy_id": "TEXT",
        "strategy_version": "TEXT", "setup_id": "TEXT",
        "prompt_version": "TEXT", "config_version": "TEXT",
        "code_version": "TEXT", "equity_basis_id": "TEXT",
        "runtime_mode": "TEXT", "account_fingerprint": "TEXT",
        "variant_id": "TEXT", "strategy_config_version": "TEXT",
    })
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trades ("
        "ts REAL, symbol TEXT, side TEXT, action TEXT, qty REAL, price REAL, "
        "notional REAL, leverage REAL, reason TEXT, confidence REAL, "
        "pnl_pct REAL, trade_id TEXT, order_id TEXT, fee_usd REAL, "
        "funding_usd REAL, realized_pnl_usd REAL, risk_usd REAL, "
        "fill_status TEXT, slippage_usd REAL, adverse_slippage_usd REAL, "
        "funding_status TEXT, pnl_semantics TEXT, strategy_id TEXT, "
        "strategy_version TEXT, setup_id TEXT, setup_key TEXT, setup_type TEXT, "
        "signal_ts REAL, exit_policy TEXT, invalidation_anchor TEXT, "
        "run_id TEXT, cycle_id TEXT, prompt_version TEXT, "
        "config_version TEXT, code_version TEXT, equity_basis_id TEXT, "
        "entry_equity_usd REAL, close_trigger TEXT, close_evidence TEXT, "
        "runtime_mode TEXT, account_fingerprint TEXT)"
    )
    # Migrate journals created by earlier releases in place.
    _ensure_columns(conn, "trades", {
        "confidence": "REAL", "pnl_pct": "REAL", "trade_id": "TEXT",
        "order_id": "TEXT", "fee_usd": "REAL", "funding_usd": "REAL",
        "realized_pnl_usd": "REAL", "risk_usd": "REAL",
        "fill_status": "TEXT", "slippage_usd": "REAL",
        "adverse_slippage_usd": "REAL", "funding_status": "TEXT",
        "pnl_semantics": "TEXT", "strategy_id": "TEXT",
        "strategy_version": "TEXT", "setup_id": "TEXT", "setup_key": "TEXT",
        "setup_type": "TEXT", "signal_ts": "REAL", "exit_policy": "TEXT",
        "invalidation_anchor": "TEXT", "run_id": "TEXT", "cycle_id": "TEXT",
        "prompt_version": "TEXT", "config_version": "TEXT",
        "code_version": "TEXT", "equity_basis_id": "TEXT",
        "entry_equity_usd": "REAL", "close_trigger": "TEXT",
        "close_evidence": "TEXT", "runtime_mode": "TEXT",
        "account_fingerprint": "TEXT", "variant_id": "TEXT",
        "strategy_config_version": "TEXT",
    })
    conn.execute(
        "CREATE TABLE IF NOT EXISTS equity ("
        "ts REAL, equity REAL, state TEXT, basis_id TEXT, run_id TEXT, "
        "cycle_id TEXT, runtime_mode TEXT, account_fingerprint TEXT)"
    )
    _ensure_columns(conn, "equity", {
        "basis_id": "TEXT", "run_id": "TEXT", "cycle_id": "TEXT",
        "runtime_mode": "TEXT", "account_fingerprint": "TEXT",
    })
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        "run_id TEXT PRIMARY KEY, started_ts REAL, mode TEXT, "
        "strategy_id TEXT, strategy_version TEXT, model TEXT, "
        "prompt_version TEXT, config_version TEXT, code_version TEXT, "
        "account_fingerprint TEXT)"
    )
    _ensure_columns(conn, "runs", {
        "account_fingerprint": "TEXT",
    })
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        ("journal_schema_version", str(JOURNAL_SCHEMA_VERSION)),
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_trade_id ON trades(trade_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_strategy "
        "ON trades(strategy_id, strategy_version, ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_equity_basis_ts "
        "ON equity(basis_id, ts)")
    conn.commit()
    os.chmod(DB_FILE, 0o600)
    return conn


def check_journal() -> None:
    """Fail startup unless the SQLite audit trail is writable."""
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?,?,?)",
                (time.time(), "journal_preflight", "writable"),
            )
            conn.rollback()
    except Exception as exc:
        raise JournalError(f"journal preflight failed: {exc}") from exc


def log_run(run_id: str, *, mode: str, strategy_id: str,
            strategy_version: str, model: str, prompt_version: str,
            config_version: str, code_version: str) -> None:
    context = journal_context()
    try:
        with _db() as c:
            c.execute(
                "INSERT OR IGNORE INTO runs ("
                "run_id, started_ts, mode, strategy_id, strategy_version, "
                "model, prompt_version, config_version, code_version, "
                "account_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, time.time(), mode, strategy_id, strategy_version,
                 model, prompt_version, config_version, code_version,
                 context.get("account_fingerprint")),
            )
    except Exception as exc:
        log.critical("run journal write failed: %s", exc)
        raise JournalError(f"run journal write failed: {exc}") from exc


def log_event(kind: str, payload: str, *, setup_id: str | None = None,
              strategy_id: str | None = None,
              strategy_version: str | None = None,
              variant_id: str | None = None) -> None:
    context = journal_context()
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO events ("
                "ts, kind, payload, run_id, cycle_id, strategy_id, "
                "strategy_version, setup_id, prompt_version, config_version, "
                "code_version, equity_basis_id, runtime_mode, "
                "account_fingerprint, variant_id, strategy_config_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(), kind, payload, context.get("run_id"),
                    context.get("cycle_id"),
                    strategy_id or context.get("strategy_id"),
                    strategy_version or context.get("strategy_version"),
                    setup_id, context.get("prompt_version"),
                    context.get("config_version"), context.get("code_version"),
                    context.get("equity_basis_id"),
                    context.get("runtime_mode"),
                    context.get("account_fingerprint"),
                    variant_id or context.get("variant_id"),
                    context.get("strategy_config_version"),
                ),
            )
    except Exception as exc:
        log.critical("event journal write failed: %s", exc)
        raise JournalError(f"event journal write failed: {exc}") from exc


def new_trade_id() -> str:
    return uuid.uuid4().hex


def log_trade(symbol, side, action, qty, price, notional, leverage, reason,
              confidence=None, pnl_pct=None, trade_id=None, order_id=None,
              fee_usd=0.0, funding_usd=0.0, realized_pnl_usd=None,
              risk_usd=None, fill_status=None, slippage_usd=0.0,
              adverse_slippage_usd=0.0, funding_status=None,
              pnl_semantics=None, strategy_id=None, strategy_version=None,
              setup_id=None, setup_key=None, setup_type=None, signal_ts=None,
              exit_policy=None, invalidation_anchor=None, run_id=None,
              cycle_id=None, prompt_version=None, config_version=None,
              code_version=None, equity_basis_id=None,
              entry_equity_usd=None, close_trigger=None,
              close_evidence=None, runtime_mode=None,
              account_fingerprint=None) -> None:
    """Journal an execution event with a durable round-trip identifier."""
    context = journal_context()
    if (pnl_semantics is None and action in {"partial_close", "close"}
            and realized_pnl_usd is not None):
        pnl_semantics = "incremental_v1"
    try:
        with _db() as c:
            columns = (
                "ts", "symbol", "side", "action", "qty", "price",
                "notional", "leverage", "reason", "confidence", "pnl_pct",
                "trade_id", "order_id", "fee_usd", "funding_usd",
                "realized_pnl_usd", "risk_usd", "fill_status",
                "slippage_usd", "adverse_slippage_usd", "funding_status",
                "pnl_semantics", "strategy_id", "strategy_version",
                "setup_id", "setup_key", "setup_type", "signal_ts",
                "exit_policy", "invalidation_anchor", "run_id", "cycle_id",
                "prompt_version", "config_version", "code_version",
                "equity_basis_id", "entry_equity_usd", "close_trigger",
                "close_evidence", "runtime_mode", "account_fingerprint",
                "variant_id", "strategy_config_version",
            )
            values = (
                time.time(), symbol, side, action, qty, price, notional,
                leverage, reason, confidence, pnl_pct, trade_id, order_id,
                fee_usd, funding_usd, realized_pnl_usd, risk_usd,
                fill_status, slippage_usd, adverse_slippage_usd,
                funding_status, pnl_semantics,
                strategy_id or context.get("strategy_id"),
                strategy_version or context.get("strategy_version"),
                setup_id, setup_key, setup_type, signal_ts, exit_policy,
                invalidation_anchor, run_id or context.get("run_id"),
                cycle_id or context.get("cycle_id"),
                prompt_version or context.get("prompt_version"),
                config_version or context.get("config_version"),
                code_version or context.get("code_version"),
                equity_basis_id or context.get("equity_basis_id"),
                entry_equity_usd, close_trigger, close_evidence,
                runtime_mode or context.get("runtime_mode"),
                account_fingerprint or context.get("account_fingerprint"),
                context.get("variant_id"),
                context.get("strategy_config_version"),
            )
            c.execute(
                f"INSERT INTO trades ({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in values)})",
                values,
            )
    except Exception as exc:
        log.critical("trade journal write failed: %s", exc)
        raise JournalError(f"trade journal write failed: {exc}") from exc


def log_equity(equity: float, st: str, basis_id: str | None = None) -> None:
    context = journal_context()
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO equity ("
                "ts, equity, state, basis_id, run_id, cycle_id, "
                "runtime_mode, account_fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), equity, st,
                 basis_id or context.get("equity_basis_id"),
                 context.get("run_id"), context.get("cycle_id"),
                 context.get("runtime_mode"),
                 context.get("account_fingerprint")),
            )
    except Exception as exc:
        log.critical("equity journal write failed: %s", exc)
        raise JournalError(f"equity journal write failed: {exc}") from exc


# Test runners can select an isolated root before importing agent modules.
# Normal application code deliberately starts in _unconfigured and main.py
# selects demo/live only after validating config.
_requested_runtime_scope = os.getenv("OKX_AGENT_RUNTIME_SCOPE")
if _requested_runtime_scope:
    configure_runtime(_requested_runtime_scope)
