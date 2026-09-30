"""Read-only runtime and broker inspection for operator safety checks.

This module intentionally does not construct :class:`agent.engine.Engine`.
It reads the existing mode-scoped state and journal without creating,
migrating, refreshing, activating, or replacing any runtime object.  Broker
inspection is limited to normalized GET-style account, positions, orders, and
clock calls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import stat as statlib
import tempfile
from typing import Any
from urllib.parse import quote, urlparse

from . import state
from .alpaca_provider import AlpacaProvider
from .journal import (_JOURNAL_REQUIRED_COLUMNS, _JOURNAL_TABLES,
                      _journal_columns)
from .paper_trial import (_TERMINAL_ORDER_STATUSES, _order_status,
                          _same_incumbent, build_descriptor,
                          paper_trial_enabled, public_status)
from .state_store import (DAY_STOPPED, KILLED, PAUSED, RUNNING,
                          StateCorruptionError, _validated)


SCHEMA = "runtime-readonly-inspection.v1"

_STATUS_FATAL_BLOCKERS = {
    "runtime_state_missing",
    "runtime_state_unreadable",
    "runtime_journal_missing",
    "runtime_journal_unreadable",
    "runtime_journal_invalid",
    "runtime_journal_sidecars_incomplete",
    "runtime_journal_snapshot_unstable",
    "runtime_mode_mismatch",
    "runtime_state_killed",
    "kill_reason_present",
    "runtime_state_day_stopped",
    "daily_risk_limit_hit",
    "risk_day_malformed",
    "operator_pause_malformed",
    "runtime_mode_invalid",
    "runtime_state_invalid",
    "local_book_malformed",
    "paper_trial_identity_mismatch",
    "paper_trial_descriptor_unavailable",
    "paper_trial_state_missing",
    "persisted_paper_trial_not_configured",
    "broker_authentication_failed",
    "broker_scope_mismatch",
    "broker_account_inactive",
    "broker_account_identity_missing",
    "broker_account_identity_mismatch",
    "broker_clock_invalid",
    "broker_clock_future",
    "broker_clock_stale",
    "broker_positions_invalid",
    "broker_orders_invalid",
}

_REQUIRED_STATE_FIELDS = frozenset({
    "state",
    "runtime_mode",
    "account_fingerprint",
    "active_trades",
    "protection",
    "kill_reason",
    "operator_pause",
    "orders",
    "risk_day",
    "paper_trial",
})

_JOURNAL_MINIMUM_COLUMNS = {
    **_JOURNAL_REQUIRED_COLUMNS,
    "runs": {"run_id", "started_ts"},
}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item)))


def _runtime_paths(mode: str, runtime_base: str | Path | None) -> tuple[Path, Path, Path]:
    root = Path(runtime_base) if runtime_base is not None else Path(state.RUNTIME_BASE)
    runtime = root / mode
    return runtime, runtime / "state.json", runtime / "journal.db"


def _read_existing_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise StateCorruptionError(
            f"runtime state is unreadable: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise StateCorruptionError("runtime state must be a JSON object")
    missing = sorted(_REQUIRED_STATE_FIELDS - set(raw))
    if missing:
        raise StateCorruptionError(
            "runtime state is missing safety fields: " + ", ".join(missing))
    try:
        return _validated(raw)
    except ValueError as exc:
        raise StateCorruptionError(f"runtime state is invalid: {exc}") from exc


def _file_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not statlib.S_ISREG(info.st_mode):
        raise OSError(f"journal component is not a regular file: {path}")
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _journal_source_signatures(path: Path) -> dict[str, tuple[int, ...] | None]:
    return {
        "database": _file_signature(path),
        "wal": _file_signature(Path(f"{path}-wal")),
        "shm": _file_signature(Path(f"{path}-shm")),
    }


def _copy_stable_journal_snapshot(path: Path, destination: Path) -> str | None:
    """Copy a consistent SQLite main/WAL view without opening the source.

    SQLite may create ``-wal``/``-shm`` files even for a ``mode=ro`` open of a
    WAL database.  Inspection therefore opens only this disposable copy.  A
    source that changes during the copy is rejected instead of presenting a
    mixed-time view.
    """
    try:
        before = _journal_source_signatures(path)
        if before["database"] is None:
            return "runtime_journal_missing"
        if before["shm"] is not None and before["wal"] is None:
            return "runtime_journal_sidecars_incomplete"
        shutil.copyfile(path, destination)
        if before["wal"] is not None:
            shutil.copyfile(Path(f"{path}-wal"), Path(f"{destination}-wal"))
        after = _journal_source_signatures(path)
    except FileNotFoundError:
        return "runtime_journal_snapshot_unstable"
    except OSError:
        return "runtime_journal_unreadable"
    if before != after:
        return "runtime_journal_snapshot_unstable"
    return None


def _readonly_journal_check(path: Path, mode: str) -> tuple[bool, str | None]:
    """Validate the existing journal through SQLite's read-only URI mode.

    ``immutable=1`` is deliberately not used: an active SQLite WAL is part of
    the current journal and must not be ignored by an operator inspection.
    """
    if not path.exists():
        return False, "runtime_journal_missing"
    try:
        with tempfile.TemporaryDirectory(
                prefix="runtime-journal-inspection-") as temp_dir:
            snapshot = Path(temp_dir) / path.name
            snapshot_error = _copy_stable_journal_snapshot(path, snapshot)
            if snapshot_error:
                return False, snapshot_error
            encoded = quote(str(snapshot.resolve()), safe="/")
            uri = f"file:{encoded}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
                db.execute("PRAGMA query_only=ON")
                quick = db.execute("PRAGMA quick_check(1)").fetchone()
                if not quick or str(quick[0]).lower() != "ok":
                    return False, "runtime_journal_invalid"
                rows = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                tables = {str(row[0]) for row in rows}
                application_tables = {
                    table for table in tables
                    if not table.startswith("sqlite_")
                }
                if application_tables != set(_JOURNAL_TABLES):
                    return False, "runtime_journal_invalid"
                if any(
                        not set(columns).issubset(
                            _journal_columns(db, table))
                        for table, columns in
                        _JOURNAL_MINIMUM_COLUMNS.items()):
                    return False, "runtime_journal_invalid"
    except sqlite3.DatabaseError:
        return False, "runtime_journal_invalid"
    except (OSError, RuntimeError):
        return False, "runtime_journal_unreadable"
    return True, None


def _sanitize_error(exc: Exception, provider: Any = None) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    session = getattr(provider, "session", None)
    for secret in (getattr(session, "api_key", None),
                   getattr(session, "secret_key", None)):
        if isinstance(secret, str) and secret:
            message = message.replace(secret, "<redacted>")
    message = re.sub(
        r"(?i)\b(api[_-]?key|secret[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=<redacted>",
        message,
    )
    return f"{exc.__class__.__name__}: {message}"[:500]


def _local_snapshot(config: Mapping[str, Any], *,
                    runtime_base: str | Path | None = None) -> dict[str, Any]:
    mode = str(config.get("mode") or "paper").strip().lower()
    result: dict[str, Any] = {
        "mode": mode,
        "paper": mode == "paper",
        "runtime_state_ready": False,
        "journal_ready": False,
        "state_file_present": False,
        "journal_file_present": False,
        "runtime": None,
        "local_flat": False,
        "local_book_valid": False,
        "paper_trial_context": paper_trial_enabled(config),
        "identity_match": None,
        "frozen_trial_identity": None,
        "expected_trial_identity": None,
        "readiness_blockers": [],
    }
    blockers: list[str] = result["readiness_blockers"]
    if mode not in {"paper", "live"}:
        blockers.append("runtime_mode_invalid")
        return result

    runtime_dir, state_path, journal_path = _runtime_paths(mode, runtime_base)
    result["runtime_path"] = str(runtime_dir)
    result["state_file_present"] = state_path.exists()
    result["journal_file_present"] = journal_path.exists()
    try:
        runtime = _read_existing_state(state_path)
    except FileNotFoundError:
        result["state_error"] = "runtime state file does not exist"
        blockers.append("runtime_state_missing")
        runtime = None
    except Exception as exc:  # noqa: BLE001 - preserve local evidence on corruption
        result["state_error"] = _sanitize_error(exc)
        blockers.append("runtime_state_unreadable")
        runtime = None

    journal_ready, journal_error = _readonly_journal_check(journal_path, mode)
    result["journal_ready"] = journal_ready
    if journal_error:
        blockers.append(journal_error)

    if runtime is None:
        result["readiness_blockers"] = _unique(blockers)
        return result

    result["runtime_state_ready"] = True
    runtime_mode = runtime.get("runtime_mode")
    runtime_state = runtime.get("state")
    operator_pause = runtime.get("operator_pause")
    kill_reason = runtime.get("kill_reason")
    risk_day = runtime.get("risk_day")
    active_trades = runtime.get("active_trades")
    protection = runtime.get("protection")
    orders = runtime.get("orders")

    local_book_valid = bool(
        isinstance(active_trades, Mapping) and
        isinstance(protection, Mapping) and
        isinstance(orders, Mapping) and
        all(isinstance(row, Mapping) for row in active_trades.values()) and
        all(isinstance(row, Mapping) for row in protection.values()) and
        all(isinstance(row, Mapping) for row in orders.values()))
    open_local_orders: list[str] = []
    missing_local_status: list[str] = []
    if local_book_valid:
        for key, row in orders.items():
            status_value = _order_status(row)
            if not status_value:
                missing_local_status.append(str(key))
            elif status_value not in _TERMINAL_ORDER_STATUSES:
                open_local_orders.append(str(key))
    local_flat = bool(
        local_book_valid and not active_trades and not protection and
        not open_local_orders and not missing_local_status)
    result["local_book_valid"] = local_book_valid
    result["local_flat"] = local_flat
    result["runtime"] = {
        "state": runtime_state,
        "runtime_mode": runtime_mode,
        "operator_pause": operator_pause,
        "kill_reason": kill_reason,
        "risk_day": deepcopy(risk_day),
        "account_fingerprint": runtime.get("account_fingerprint"),
        "active_trades": len(active_trades) if isinstance(active_trades, Mapping) else None,
        "protection": len(protection) if isinstance(protection, Mapping) else None,
        "orders": len(orders) if isinstance(orders, Mapping) else None,
        "open_local_orders": open_local_orders,
        "missing_local_order_status": missing_local_status,
    }

    if runtime_mode != mode:
        blockers.append("runtime_mode_mismatch")
    if runtime_state == KILLED:
        blockers.append("runtime_state_killed")
    elif runtime_state == DAY_STOPPED:
        blockers.append("runtime_state_day_stopped")
    elif runtime_state not in {PAUSED, RUNNING}:
        blockers.append("runtime_state_invalid")
    if kill_reason not in (None, False, ""):
        blockers.append("kill_reason_present")
    if not isinstance(operator_pause, bool):
        blockers.append("operator_pause_malformed")
    if not isinstance(risk_day, Mapping):
        blockers.append("risk_day_malformed")
    elif "limit_hit" in risk_day:
        if risk_day.get("limit_hit") is True:
            blockers.append("daily_risk_limit_hit")
        elif risk_day.get("limit_hit") is not False:
            blockers.append("risk_day_malformed")
    if not local_book_valid:
        blockers.append("local_book_malformed")
    else:
        if active_trades:
            blockers.append("active_trades_present")
        if protection:
            blockers.append("protection_present")
        if missing_local_status:
            blockers.append("local_order_status_missing")
        if open_local_orders:
            blockers.append("nonterminal_local_orders")

    persisted_trial = runtime.get("paper_trial")
    has_persisted_trial = isinstance(persisted_trial, Mapping) and bool(persisted_trial)
    configured_trial = paper_trial_enabled(config)
    result["paper_trial_context"] = configured_trial or has_persisted_trial
    descriptor = None
    descriptor_error = None
    if configured_trial:
        try:
            descriptor = build_descriptor(config)
        except Exception as exc:  # noqa: BLE001 - descriptor drift must be visible
            descriptor_error = _sanitize_error(exc)
            result["identity_error"] = descriptor_error
            blockers.append("paper_trial_descriptor_unavailable")
    if descriptor is not None:
        result["expected_trial_identity"] = {
            key: deepcopy(descriptor.get(key)) for key in (
                "trial_id", "candidate_id", "variant_id", "incumbent_identity",
                "policy_identity", "spec_identity", "code_identity",
                "cohort_identity", "include_ibr",
            )
        }
    if has_persisted_trial:
        frozen = dict(persisted_trial)
        result["frozen_trial_identity"] = {
            key: deepcopy(frozen.get(key)) for key in (
                "trial_id", "candidate_id", "variant_id", "incumbent_identity",
                "policy_identity", "spec_identity", "code_identity",
                "cohort_identity", "include_ibr", "state", "started_on",
            )
        }
        result["frozen_trial_identity"].update({
            "accepted_sessions": len(frozen.get("accepted_sessions", ())),
            "closed_outcomes": len(frozen.get("outcomes", ())),
        })
        trial_status = public_status(frozen)
        trial_status["observed_entry_eligible"] = (
            trial_status.get("entry_eligible") is True)
        result["paper_trial"] = trial_status
        if not configured_trial:
            result["identity_match"] = False
            blockers.append("persisted_paper_trial_not_configured")
        elif descriptor is not None:
            result["identity_match"] = _same_incumbent(frozen, descriptor)
            if result["identity_match"] is not True:
                blockers.append("paper_trial_identity_mismatch")
    elif configured_trial:
        result["identity_match"] = False
        blockers.append("paper_trial_state_missing")

    result["readiness_blockers"] = _unique(blockers)
    return result


def _valid_endpoint(provider: Any, mode: str) -> bool:
    session = getattr(provider, "session", None)
    endpoint = getattr(provider, "endpoint", None)
    if endpoint is None:
        endpoint = getattr(session, "endpoint", None)
    if endpoint in (None, ""):
        return True  # Injectable provider protocols need not expose a URL.
    parsed = urlparse(str(endpoint))
    host = "paper-api.alpaca.markets" if mode == "paper" else "api.alpaca.markets"
    return bool(
        parsed.scheme.lower() == "https" and parsed.hostname == host and
        parsed.port is None and parsed.username is None and
        parsed.password is None)


def _broker_snapshot(config: Mapping[str, Any], local: Mapping[str, Any], *,
                     provider: Any = None,
                     provider_factory: Callable[[Mapping[str, Any]], Any] = AlpacaProvider,
                     authenticated: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "authenticated": False,
        "credentials_configured": False,
        "broker_flat": False,
        "account": None,
        "clock": None,
        "positions": None,
        "orders": None,
        "open_orders": None,
        "clock_age_seconds": None,
    }
    if not authenticated:
        return result
    try:
        broker = provider if provider is not None else provider_factory(config)
    except Exception as exc:  # noqa: BLE001
        result["auth_error"] = _sanitize_error(exc)
        result["broker_blockers"] = ["broker_authentication_failed"]
        return result

    session = getattr(broker, "session", None)
    api_key = getattr(session, "api_key", None)
    secret_key = getattr(session, "secret_key", None)
    result["credentials_configured"] = bool(api_key and secret_key)
    mode = str(config.get("mode") or "paper").lower()
    provider_paper = getattr(broker, "paper", None)
    broker_blockers: list[str] = []
    if not isinstance(provider_paper, bool) or provider_paper is not (mode == "paper"):
        broker_blockers.append("broker_scope_mismatch")
    if not _valid_endpoint(broker, mode):
        broker_blockers.append("broker_scope_mismatch")
    if broker_blockers:
        result["broker_blockers"] = _unique(broker_blockers)
        return result

    try:
        account = broker.account()
        positions = broker.positions()
        orders = broker.orders(status="open")
        clock = broker.clock()
        if not isinstance(positions, (list, tuple)):
            broker_blockers.append("broker_positions_invalid")
        if not isinstance(orders, (list, tuple)):
            broker_blockers.append("broker_orders_invalid")
        account_id = str(_field(account, "id", "") or "").strip()
        account_status = str(_field(account, "status", "") or "").lower()
        if not account_id:
            broker_blockers.append("broker_account_identity_missing")
        if account_status not in {"active", "account_status.active"}:
            broker_blockers.append("broker_account_inactive")
        timestamp = _field(clock, "timestamp")
        is_open = _field(clock, "is_open")
        if (not isinstance(timestamp, datetime) or timestamp.tzinfo is None or
                timestamp.utcoffset() is None or not isinstance(is_open, bool)):
            broker_blockers.append("broker_clock_invalid")
        else:
            try:
                maximum_clock_age = float(
                    _field(config.get("execution"),
                           "max_market_data_age_seconds", 30) or 30)
            except (TypeError, ValueError, OverflowError):
                maximum_clock_age = float("nan")
            if (not math.isfinite(maximum_clock_age) or
                    not 1 <= maximum_clock_age <= 30):
                broker_blockers.append("broker_clock_invalid")
            else:
                clock_age = (
                    datetime.now(timezone.utc) -
                    timestamp.astimezone(timezone.utc)
                ).total_seconds()
                result["clock_age_seconds"] = clock_age
                if clock_age < 0:
                    broker_blockers.append("broker_clock_future")
                elif clock_age > maximum_clock_age:
                    broker_blockers.append("broker_clock_stale")

        working_orders: list[Any] = []
        missing_order_status = False
        if isinstance(orders, (list, tuple)):
            for order in orders:
                order_state = _order_status(order)
                if not order_state:
                    missing_order_status = True
                elif order_state not in _TERMINAL_ORDER_STATUSES:
                    working_orders.append(order)
        if missing_order_status:
            broker_blockers.append("broker_orders_invalid")

        runtime = local.get("runtime")
        persisted_fingerprint = (
            runtime.get("account_fingerprint")
            if isinstance(runtime, Mapping) else None)
        broker_fingerprint = None
        if account_id and isinstance(api_key, str) and api_key:
            broker_fingerprint = state.account_fingerprint(
                mode, f"{api_key}\0{account_id}")
            if persisted_fingerprint != broker_fingerprint:
                broker_blockers.append("broker_account_identity_mismatch")
        else:
            broker_blockers.append("broker_account_identity_missing")

        result.update({
            "account": account,
            "clock": clock,
            "positions": positions,
            "orders": orders,
            "open_orders": working_orders,
            "account_fingerprint": broker_fingerprint,
            "authenticated": not any(code in broker_blockers for code in {
                "broker_scope_mismatch", "broker_account_inactive",
                "broker_account_identity_missing", "broker_clock_invalid",
                "broker_clock_future", "broker_clock_stale",
                "broker_positions_invalid", "broker_orders_invalid",
            }),
            "broker_flat": bool(
                isinstance(positions, (list, tuple)) and not positions and
                isinstance(orders, (list, tuple)) and not working_orders and
                not missing_order_status),
        })
    except Exception as exc:  # noqa: BLE001 - retain local pause evidence
        result["auth_error"] = _sanitize_error(exc, broker)
        broker_blockers.append("broker_authentication_failed")
    result["broker_blockers"] = _unique(broker_blockers)
    return result


def _paper_selection(config: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    strategy = config.get("strategy") if isinstance(config.get("strategy"), Mapping) else {}
    paper_trial = result.get("paper_trial")
    frozen = result.get("frozen_trial_identity")
    configured = result.get("expected_trial_identity")
    resolved = None
    if isinstance(frozen, Mapping):
        resolved = {
            "candidate_id": frozen.get("candidate_id"),
            "variant_id": frozen.get("variant_id"),
            "incumbent_identity": frozen.get("incumbent_identity"),
            "family": _field(paper_trial, "family"),
            "proof": None,
            "authorizing": False,
            "proof_authority": False,
        }
    identity_blocker = next((item for item in result.get("readiness_blockers", ())
                             if item in {
                                 "paper_trial_identity_mismatch",
                                 "paper_trial_descriptor_unavailable",
                                 "paper_trial_state_missing",
                                 "persisted_paper_trial_not_configured",
                             }), None)
    runtime = result.get("runtime")
    fatal_blocker = next((
        item for item in result.get("readiness_blockers", ())
        if item in _STATUS_FATAL_BLOCKERS
    ), None)
    trial_blocker = None
    if isinstance(paper_trial, Mapping) and paper_trial.get("entry_eligible") is not True:
        trial_blockers = paper_trial.get("blockers")
        trial_blocker = (
            str(trial_blockers[0])
            if isinstance(trial_blockers, list) and trial_blockers else
            "paper_trial_blocked")
    if identity_blocker:
        blocker = identity_blocker
    elif fatal_blocker:
        blocker = fatal_blocker
    elif not isinstance(runtime, Mapping):
        blocker = "runtime_state_unavailable"
    elif (runtime.get("state") == KILLED or
          runtime.get("kill_reason") not in (None, False, "")):
        blocker = "runtime_killed"
    elif (runtime.get("state") == DAY_STOPPED or
          _field(runtime.get("risk_day"), "limit_hit") is True):
        blocker = "daily_risk_limit"
    elif runtime.get("operator_pause") is not False:
        blocker = "operator_paused"
    elif result.get("authenticated") is not True:
        blocker = "authenticated_broker_required"
    else:
        blocker = trial_blocker
    evidence_ready = bool(
        result.get("status_ok") is True and
        result.get("runtime_state_ready") is True and
        result.get("journal_ready") is True and
        result.get("local_book_valid") is True and
        result.get("authenticated") is True)
    armed = bool(
        result.get("paper_trial_context") and evidence_ready and
        blocker is None and
        isinstance(paper_trial, Mapping) and
        paper_trial.get("entry_eligible") is True)
    return {
        "configured_strategy": "rule" if result.get("paper_trial_context") else
            (str(strategy.get("id") or "").strip() or None),
        "selection_mode": "paper_trial" if result.get("paper_trial_context") else
            str(strategy.get("selection_mode") or "specific"),
        "requested_variant": (
            _field(configured, "variant_id") or _field(frozen, "variant_id") or
            str(strategy.get("variant_id") or "").strip() or None),
        "resolved": resolved,
        "blocker_code": blocker,
        "armed": armed,
        "state": "paper_trial" if armed else "blocked",
        **({"paper_trial": paper_trial} if isinstance(paper_trial, Mapping) else {}),
    }


def inspect_runtime(
    config: Mapping[str, Any],
    *,
    authenticated: bool = True,
    deployment: bool = False,
    provider: Any = None,
    provider_factory: Callable[[Mapping[str, Any]], Any] = AlpacaProvider,
    runtime_base: str | Path | None = None,
) -> dict[str, Any]:
    """Return a fail-closed snapshot without changing local or broker state."""
    local = _local_snapshot(config, runtime_base=runtime_base)
    broker = _broker_snapshot(
        config, local, provider=provider, provider_factory=provider_factory,
        authenticated=authenticated)
    result = {
        "schema": SCHEMA,
        "inspection": "paused_deployment" if deployment else "status",
        **local,
        **broker,
        "entry_eligible": False,
    }
    if deployment and isinstance(result.get("paper_trial"), Mapping):
        result["paper_trial"] = dict(result["paper_trial"])
        result["paper_trial"]["entry_eligible"] = False
    blockers = list(result.get("readiness_blockers", ()))
    blockers.extend(result.get("broker_blockers", ()))

    mode = result.get("mode")
    broker_cfg = (config.get("broker")
                  if isinstance(config.get("broker"), Mapping) else {})
    paper_config_valid = bool(
        mode == "paper" and broker_cfg.get("paper") is True and
        broker_cfg.get("allow_live") is False)
    result["paper_config_valid"] = paper_config_valid
    if deployment and not paper_config_valid:
        blockers.append("paper_mode_required")

    runtime = result.get("runtime")
    exact_pause = bool(
        isinstance(runtime, Mapping) and runtime.get("state") == PAUSED and
        runtime.get("operator_pause") is True)
    result["operator_paused"] = exact_pause
    if deployment and not exact_pause:
        blockers.append("explicit_operator_pause_required")
    if deployment and result.get("local_flat") is not True:
        blockers.append("local_flat_required")
    if deployment and result.get("broker_flat") is not True:
        blockers.append("broker_flat_required")
    if deployment and result.get("authenticated") is not True:
        blockers.append("authenticated_broker_required")

    result["readiness_blockers"] = _unique(blockers)
    fatal = set(result["readiness_blockers"]) & _STATUS_FATAL_BLOCKERS
    result["status_ok"] = not fatal
    result["paper_selection"] = _paper_selection(config, result)
    research = config.get("research") if isinstance(config.get("research"), Mapping) else {}
    result["edge_required"] = bool(
        not result.get("paper_trial_context") and
        research.get("enabled") is True and
        research.get("require_validated_variant", True) is True)
    result["edge_ready"] = False
    result["edge_error"] = (
        None if not result["edge_required"] else
        "validated edge is not resolved by read-only deployment inspection")
    result["variant_id"] = None
    result["variant_ids"] = []
    result["edge_vehicle"] = None

    safety_exclusions = {
        "paper_trial_identity_mismatch",
        "paper_trial_descriptor_unavailable",
        "persisted_paper_trial_not_configured",
    }
    safety_blockers = [
        item for item in result["readiness_blockers"]
        if item not in safety_exclusions and item not in {"operator_paused"}
    ]
    result["paused_flat_verified"] = bool(
        deployment and paper_config_valid and exact_pause and
        result.get("runtime_state_ready") is True and
        result.get("journal_ready") is True and
        result.get("local_book_valid") is True and
        result.get("local_flat") is True and
        result.get("authenticated") is True and
        result.get("broker_flat") is True and
        not safety_blockers)
    return result
