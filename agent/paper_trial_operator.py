"""Audited operator cancellation for an evidence-empty paper incumbent.

This module intentionally has no trading-loop dependency and no order-bearing path.
It only accepts an explicitly paused, account-bound, broker-flat trial and
records a deterministic audit before changing the trial lifecycle.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from agent import state
from agent.alpaca_provider import AlpacaProvider
from agent.paper_trial import (
    PaperTrialError,
    validate_state,
    replacement_local_book_is_flat,
)


AUDIT_SCHEMA = "paper-incumbent-operator-cancellation.v1"
AUDIT_KIND = "paper_trial_operator_cancelled"
MAX_REASON_LENGTH = 500
MAX_BROKER_SNAPSHOT_SECONDS = 30.0


class PaperTrialOperatorError(RuntimeError):
    """The requested paper-trial cancellation cannot be proven safe."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _plain(value: Any) -> Any:
    return json.loads(_json(value))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, bytes)):
        return ""
    return str(value).strip()


def _reason(value: Any) -> str:
    reason = _text(value)
    if not reason or len(reason) > MAX_REASON_LENGTH or any(
            ord(char) < 32 for char in reason):
        raise PaperTrialOperatorError(
            f"cancellation reason must be 1-{MAX_REASON_LENGTH} printable characters")
    return reason


def _require_paper_config(config: Mapping[str, Any]) -> None:
    if not isinstance(config, Mapping):
        raise PaperTrialOperatorError("paper cancellation config is invalid")
    if str(config.get("mode") or "paper").lower() != "paper":
        raise PaperTrialOperatorError("paper cancellation requires mode=paper")
    broker = config.get("broker")
    if not isinstance(broker, Mapping):
        raise PaperTrialOperatorError("paper cancellation broker config is invalid")
    if broker.get("paper") is not True or broker.get("allow_live") is not False:
        raise PaperTrialOperatorError(
            "paper cancellation requires broker.paper=true and allow_live=false")
    if broker.get("endpoint"):
        raise PaperTrialOperatorError(
            "paper cancellation rejects broker endpoint overrides")


def _require_existing_runtime() -> None:
    state_path = Path(state.STATE_FILE)
    journal_path = Path(state.JOURNAL_FILE)
    if state_path.is_symlink():
        raise PaperTrialOperatorError(
            "paper cancellation rejects a symlinked paper state file")
    if journal_path.is_symlink():
        raise PaperTrialOperatorError(
            "paper cancellation rejects a symlinked paper journal")
    if not state_path.is_file():
        raise PaperTrialOperatorError(
            "paper cancellation requires the existing paper state file")
    if not journal_path.is_file():
        raise PaperTrialOperatorError(
            "paper cancellation requires the existing paper journal")


def _journal_uri(*, mode: str) -> str:
    if mode not in {"ro", "rw"}:
        raise PaperTrialOperatorError("paper cancellation journal mode is invalid")
    path = Path(state.JOURNAL_FILE)
    if path.is_symlink():
        raise PaperTrialOperatorError(
            "paper cancellation rejects a symlinked paper journal")
    if not path.is_file():
        raise PaperTrialOperatorError(
            "paper cancellation requires the existing paper journal")
    return f"{path.absolute().as_uri()}?mode={mode}"


def _journal_columns() -> set[str]:
    try:
        with closing(sqlite3.connect(
                _journal_uri(mode="rw"), uri=True, timeout=5)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "events" not in tables:
                raise PaperTrialOperatorError(
                    "paper cancellation requires an existing events journal")
            return {row[1] for row in db.execute("PRAGMA table_info(events)")}
    except PaperTrialOperatorError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise PaperTrialOperatorError(
            "paper cancellation cannot read the existing journal") from exc


def _require_journal_ready() -> None:
    columns = _journal_columns()
    missing = {"kind", "payload", "run_id"} - columns
    if missing:
        raise PaperTrialOperatorError(
            "paper cancellation journal is missing required event columns: " +
            ", ".join(sorted(missing)))


def _audit_rows(audit_id: str) -> list[tuple[str, str, str]]:
    try:
        with closing(sqlite3.connect(
                _journal_uri(mode="ro"), uri=True, timeout=5)) as db:
            return db.execute(
                "SELECT kind, run_id, payload FROM events "
                "WHERE kind=? AND run_id=? LIMIT 2",
                (AUDIT_KIND, audit_id)).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise PaperTrialOperatorError(
            "paper cancellation cannot read the audit journal") from exc


def _account_id(account: Any) -> str:
    value = (account.get("id") if isinstance(account, Mapping)
             else getattr(account, "id", None))
    return _text(value)


def _account_status(account: Any) -> str:
    value = (account.get("status") if isinstance(account, Mapping)
             else getattr(account, "status", None))
    value = getattr(value, "value", value)
    return _text(value).lower()


def _provider_api_key(config: Mapping[str, Any], provider: Any) -> str:
    session = getattr(provider, "session", None)
    value = getattr(session, "api_key", None)
    if not value:
        broker = config.get("broker") if isinstance(
            config.get("broker"), Mapping) else {}
        value = broker.get("api_key")
    if not isinstance(value, str) or not value.strip():
        raise PaperTrialOperatorError(
            "paper cancellation cannot verify the authenticated API key")
    return value


def _is_exact_paper_endpoint(value: Any) -> bool:
    """Accept only the canonical HTTPS Alpaca paper origin."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlparse(value.strip())
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError:
        return False
    return bool(
        scheme == "https" and
        hostname == "paper-api.alpaca.markets" and
        port is None and
        username is None and
        password is None and
        parsed.path in ("", "/") and
        not parsed.params and
        not parsed.query and
        not parsed.fragment)


def _broker_snapshot(config: Mapping[str, Any], provider: Any,
                     current: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    provider_endpoint = getattr(provider, "endpoint", None)
    session_endpoint = getattr(getattr(provider, "session", None),
                               "endpoint", None)
    if provider_endpoint is None and session_endpoint is None:
        raise PaperTrialOperatorError(
            "paper cancellation provider endpoint metadata is unavailable")
    for endpoint in (provider_endpoint, session_endpoint):
        if endpoint is not None and not _is_exact_paper_endpoint(endpoint):
            raise PaperTrialOperatorError(
                "paper cancellation provider is not on the paper endpoint")
    if (getattr(provider, "paper", None) is not True or
            (hasattr(provider, "mode") and
             str(getattr(provider, "mode") or "").lower() != "paper") or
            (getattr(getattr(provider, "session", None), "paper", True)
             is not True)):
        raise PaperTrialOperatorError(
            "paper cancellation provider is not on the paper endpoint")
    snapshot_started = time.monotonic()
    try:
        account = provider.account()
    except Exception as exc:  # noqa: BLE001
        raise PaperTrialOperatorError(
            f"paper cancellation account GET failed: {type(exc).__name__}") from exc
    account_id = _account_id(account)
    if not account_id or _account_status(account) != "active":
        raise PaperTrialOperatorError(
            "paper cancellation requires an active, identified paper account")
    fingerprint = state.account_fingerprint(
        "paper", f"{_provider_api_key(config, provider)}\0{account_id}")
    persisted = current.get("account_fingerprint")
    if not isinstance(persisted, str) or persisted != fingerprint:
        raise PaperTrialOperatorError(
            "paper cancellation account identity differs from persisted state")
    trial = current.get("paper_trial")
    if (isinstance(trial, Mapping) and
            trial.get("activation_confirmed") is True and
            trial.get("activation_account_fingerprint") != fingerprint):
        raise PaperTrialOperatorError(
            "paper cancellation account identity differs from trial activation")
    try:
        positions = provider.positions()
    except Exception as exc:  # noqa: BLE001
        raise PaperTrialOperatorError(
            f"paper cancellation positions GET failed: {type(exc).__name__}") from exc
    if not isinstance(positions, (list, tuple)):
        raise PaperTrialOperatorError(
            "paper cancellation broker positions snapshot is malformed")
    try:
        open_orders = provider.orders(status="open")
    except Exception as exc:  # noqa: BLE001
        raise PaperTrialOperatorError(
            f"paper cancellation open-orders GET failed: {type(exc).__name__}") from exc
    if not isinstance(open_orders, (list, tuple)):
        raise PaperTrialOperatorError(
            "paper cancellation broker open-orders snapshot is malformed")
    observed_at_ts = time.time()
    if time.monotonic() - snapshot_started > MAX_BROKER_SNAPSHOT_SECONDS:
        raise PaperTrialOperatorError(
            "paper cancellation broker snapshot is too old to audit")
    if positions or open_orders:
        raise PaperTrialOperatorError(
            "paper cancellation requires a fresh broker-flat snapshot")
    return fingerprint, {
        "schema": "paper-incumbent-flat-observation.v1",
        "source": "direct_get_account_positions_open_orders",
        "account_active": True,
        "positions_empty": True,
        "open_orders_empty": True,
        "positions_count": 0,
        "open_orders_count": 0,
        "observed_at_ts": observed_at_ts,
    }


def _runtime_prestate(current: Mapping[str, Any], trial: Mapping[str, Any]) -> dict[str, Any]:
    # Keep the full trial/evidence and the runtime safety fields needed to
    # prove DAY_STOPPED/KILLED/kill-reason preservation without raw broker data.
    return {
        "paper_trial": _plain(trial),
        "runtime": {
            key: _plain(current.get(key)) for key in (
                "state", "runtime_mode", "operator_pause", "kill_reason",
                "account_fingerprint", "active_trades", "protection", "orders",
            )
        },
    }


def _audit_material(trial: Mapping[str, Any], prestate: Mapping[str, Any],
                    reason: str, fingerprint: str,
                    flat_observation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": AUDIT_SCHEMA,
        "kind": AUDIT_KIND,
        "trial_id": trial.get("trial_id"),
        "incumbent_identity": trial.get("incumbent_identity"),
        "reason": reason,
        "prestate_digest": _digest(prestate),
        "account_binding": {
            "runtime_mode": "paper",
            "account_fingerprint": fingerprint,
        },
        "flat_observation": _stable_flat_observation(flat_observation),
    }


def _stable_flat_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return only broker-flat facts that are stable across a retry.

    The observation timestamp is retained in the durable payload and state
    record, but intentionally excluded from the deterministic audit material.
    A failed state write can therefore reuse the journal row after a fresh
    GET-only snapshot without producing a second audit identity.
    """
    if not isinstance(value, Mapping):
        raise PaperTrialOperatorError(
            "paper cancellation flat observation is malformed")
    result = {
        key: value.get(key) for key in (
            "schema", "source", "account_active", "positions_empty",
            "open_orders_empty", "positions_count", "open_orders_count",
        )
    }
    if (result["schema"] != "paper-incumbent-flat-observation.v1" or
            result["source"] != "direct_get_account_positions_open_orders" or
            result["account_active"] is not True or
            result["positions_empty"] is not True or
            result["open_orders_empty"] is not True or
            isinstance(result["positions_count"], bool) or
            not isinstance(result["positions_count"], int) or
            isinstance(result["open_orders_count"], bool) or
            not isinstance(result["open_orders_count"], int) or
            result["positions_count"] != 0 or
            result["open_orders_count"] != 0):
        raise PaperTrialOperatorError(
            "paper cancellation flat observation is malformed")
    return result


def _build_audit(trial: Mapping[str, Any], prestate: Mapping[str, Any],
                 reason: str, fingerprint: str,
                 flat_observation: Mapping[str, Any]) -> dict[str, Any]:
    material = _audit_material(trial, prestate, reason, fingerprint,
                               flat_observation)
    audit = {
        **material,
        "audit_id": _digest(material),
        "trial": _plain(trial),
        "prestate": _plain(prestate),
        "flat_observation": _plain(flat_observation),
        "recorded_ts": flat_observation.get("observed_at_ts"),
        "authorizing": False,
        "proof_authority": False,
    }
    return audit


def _decode_audit(row: tuple[str, str, str]) -> dict[str, Any]:
    kind, run_id, raw = row
    if kind != AUDIT_KIND or not run_id:
        raise PaperTrialOperatorError("paper cancellation audit row is malformed")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PaperTrialOperatorError(
            "paper cancellation audit payload is malformed") from exc
    if not isinstance(payload, Mapping):
        raise PaperTrialOperatorError("paper cancellation audit payload is malformed")
    return dict(payload)


def _validate_audit(payload: Mapping[str, Any], expected: Mapping[str, Any],
                    expected_id: str) -> dict[str, Any]:
    for key in ("schema", "kind", "trial_id", "incumbent_identity", "reason",
                "prestate_digest", "account_binding"):
        if payload.get(key) != expected.get(key):
            raise PaperTrialOperatorError(
                "paper cancellation audit does not match the requested prestate")
    try:
        actual_flat = _stable_flat_observation(payload.get("flat_observation"))
        expected_flat = _stable_flat_observation(expected.get("flat_observation"))
    except PaperTrialOperatorError:
        raise
    if actual_flat != expected_flat:
        raise PaperTrialOperatorError(
            "paper cancellation audit does not match the broker-flat observation")
    if payload.get("audit_id") != expected_id:
        raise PaperTrialOperatorError(
            "paper cancellation audit id does not match the requested prestate")
    if payload.get("authorizing") is not False or \
            payload.get("proof_authority") is not False:
        raise PaperTrialOperatorError(
            "paper cancellation audit cannot carry proof authority")
    prestate = payload.get("prestate")
    trial = payload.get("trial")
    if not isinstance(prestate, Mapping) or not isinstance(trial, Mapping):
        raise PaperTrialOperatorError("paper cancellation audit prestate is malformed")
    if _digest(prestate) != payload.get("prestate_digest") or \
            _plain(trial) != _plain(prestate.get("paper_trial")):
        raise PaperTrialOperatorError(
            "paper cancellation audit prestate digest is invalid")
    if trial.get("state") != "running" or trial.get("accepted_sessions") or \
            trial.get("outcomes"):
        raise PaperTrialOperatorError(
            "paper cancellation audit is not evidence-empty running state")
    recorded_ts = payload.get("recorded_ts")
    if (isinstance(recorded_ts, bool) or
            not isinstance(recorded_ts, (int, float)) or
            not math.isfinite(float(recorded_ts)) or float(recorded_ts) <= 0):
        raise PaperTrialOperatorError(
            "paper cancellation audit recorded time is invalid")
    if payload.get("recorded_ts") != payload.get("flat_observation", {}).get(
            "observed_at_ts"):
        raise PaperTrialOperatorError(
            "paper cancellation audit observed time is invalid")
    return dict(payload)


def verify_operator_cancellation(trial_state: Mapping[str, Any],
                                 record: Mapping[str, Any]) -> dict[str, Any]:
    """Verify one canceled trial against exactly one durable operator audit.

    This deliberately does not call :func:`validate_state`; it is the narrow
    journal boundary used by that validator, and keeping it independent avoids
    a validation recursion while still making missing, changed, or duplicated
    audit rows fail closed.
    """
    if not isinstance(trial_state, Mapping) or not isinstance(record, Mapping):
        raise PaperTrialOperatorError(
            "operator-cancelled paper trial audit is malformed")
    audit_id = _text(record.get("audit_id"))
    if len(audit_id) != 64 or any(char not in "0123456789abcdef"
                                  for char in audit_id):
        raise PaperTrialOperatorError(
            "operator-cancelled paper trial audit id is invalid")
    rows = _audit_rows(audit_id)
    if len(rows) != 1:
        raise PaperTrialOperatorError(
            "operator-cancelled paper trial audit is missing or duplicated")
    payload = _decode_audit(rows[0])
    prestate = payload.get("prestate")
    old_trial = prestate.get("paper_trial") if isinstance(prestate, Mapping) else None
    runtime = prestate.get("runtime") if isinstance(prestate, Mapping) else None
    if not isinstance(prestate, Mapping) or not isinstance(old_trial, Mapping) or \
            not isinstance(runtime, Mapping):
        raise PaperTrialOperatorError(
            "paper cancellation audit prestate is malformed")
    if (old_trial.get("schema") != "paper-incumbent-trial.v1" or
            old_trial.get("state") != "running" or
            old_trial.get("accepted_sessions") != [] or
            old_trial.get("outcomes") != [] or
            old_trial.get("authorizing") is not False or
            old_trial.get("proof_authority") is not False):
        raise PaperTrialOperatorError(
            "paper cancellation audit prestate is not evidence-empty")
    if runtime.get("runtime_mode") != "paper":
        raise PaperTrialOperatorError(
            "paper cancellation audit runtime mode is invalid")
    binding = payload.get("account_binding")
    fingerprint = _text(binding.get("account_fingerprint")) \
        if isinstance(binding, Mapping) else ""
    if (not isinstance(binding, Mapping) or binding.get("runtime_mode") != "paper" or
            not fingerprint.startswith("alpaca-paper-") or
            runtime.get("account_fingerprint") != fingerprint or
            (old_trial.get("activation_confirmed") is True and
             old_trial.get("activation_account_fingerprint") != fingerprint)):
        raise PaperTrialOperatorError(
            "paper cancellation audit account binding is invalid")
    flat_observation = payload.get("flat_observation")
    record_flat = record.get("flat_observation")
    _stable_flat_observation(flat_observation)
    _stable_flat_observation(record_flat)
    if (_plain(record_flat) != _plain(flat_observation) or
            record.get("recorded_ts") != payload.get("recorded_ts")):
        raise PaperTrialOperatorError(
            "paper cancellation record does not match its durable audit")
    expected = _audit_material(
        old_trial, prestate, _text(payload.get("reason")), fingerprint,
        flat_observation)
    if payload.get("audit_id") != audit_id or _digest(expected) != audit_id:
        raise PaperTrialOperatorError(
            "paper cancellation audit id does not match its content")
    _validate_audit(payload, expected, audit_id)
    expected_current = _plain(old_trial)
    expected_current["state"] = "operator_cancelled"
    expected_current["operator_cancellation"] = _plain(record)
    if _plain(trial_state) != expected_current:
        raise PaperTrialOperatorError(
            "operator-cancelled paper trial differs from its durable audit")
    for key in ("schema", "reason", "prestate_digest", "account_fingerprint"):
        if key == "schema":
            if record.get(key) != AUDIT_SCHEMA:
                raise PaperTrialOperatorError(
                    "operator-cancelled paper trial cancellation schema is invalid")
        elif record.get(key) != (payload.get(key) if key != "account_fingerprint"
                                 else fingerprint):
            raise PaperTrialOperatorError(
                "operator-cancelled paper trial cancellation record is invalid")
    if record.get("prestate_digest") != _digest(prestate):
        raise PaperTrialOperatorError(
            "operator-cancelled paper trial prestate digest is invalid")
    return payload


def _write_or_reuse_audit(audit: Mapping[str, Any]) -> dict[str, Any]:
    audit_id = str(audit["audit_id"])
    rows = _audit_rows(audit_id)
    if len(rows) > 1:
        raise PaperTrialOperatorError(
            "paper cancellation audit id has duplicate journal rows")
    if rows:
        return _validate_audit(_decode_audit(rows[0]), audit, audit_id)
    try:
        state.log_event(
            AUDIT_KIND, _json(audit), run_id=audit_id,
            runtime_mode="paper",
            account_fingerprint=audit["account_binding"]["account_fingerprint"],
        )
    except Exception as exc:  # noqa: BLE001
        raise PaperTrialOperatorError(
            f"paper cancellation audit journal write failed: "
            f"{type(exc).__name__}") from exc
    rows = _audit_rows(audit_id)
    if len(rows) != 1:
        raise PaperTrialOperatorError(
            "paper cancellation audit was not durably recorded exactly once")
    return _validate_audit(_decode_audit(rows[0]), audit, audit_id)


def _cancellation_record(audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": AUDIT_SCHEMA,
        "audit_id": audit["audit_id"],
        "reason": audit["reason"],
        "prestate_digest": audit["prestate_digest"],
        "account_fingerprint": audit["account_binding"]["account_fingerprint"],
        "flat_observation": _plain(audit["flat_observation"]),
        "recorded_ts": audit.get("recorded_ts"),
    }


def _validate_cancelled_retry(current: Mapping[str, Any], trial: Mapping[str, Any],
                              reason: str, fingerprint: str) -> dict[str, Any]:
    record = trial.get("operator_cancellation")
    if not isinstance(record, Mapping):
        raise PaperTrialOperatorError(
            "already-cancelled paper trial has no cancellation record")
    payload = verify_operator_cancellation(trial, record)
    if (record.get("reason") != reason or
            record.get("account_fingerprint") != fingerprint or
            payload.get("account_binding", {}).get("account_fingerprint") !=
            fingerprint):
        raise PaperTrialOperatorError(
            "already-cancelled paper trial cancellation record is invalid")
    return payload


def cancel_paper_trial(
        config: Mapping[str, Any], *, confirm_trial_id: str,
        confirm_incumbent_identity: str, reason: str,
        provider_factory: Callable[[Mapping[str, Any]], Any] | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Cancel one evidence-empty paused paper trial after durable proof.

    The provider is constructed only after the mode-scoped run lock is held;
    the only broker calls are account, positions, and open-order GETs.
    """
    _require_paper_config(config)
    trial_id = _text(confirm_trial_id)
    incumbent_identity = _text(confirm_incumbent_identity)
    if not trial_id or not incumbent_identity:
        raise PaperTrialOperatorError(
            "exact trial id and incumbent identity confirmations are required")
    cancel_reason = _reason(reason)
    # ``now`` remains an accepted compatibility argument, but the audit time
    # is the broker snapshot's observed time, captured only after all GETs.
    del now
    state.configure_runtime("paper")
    _require_existing_runtime()
    _require_journal_ready()
    lock_handle = state.acquire_run_lock()
    if lock_handle is None:
        raise PaperTrialOperatorError("paper runtime lock is held")
    provider = None
    try:
        try:
            provider = (provider_factory or AlpacaProvider)(config)
        except Exception as exc:  # noqa: BLE001
            raise PaperTrialOperatorError(
                f"paper cancellation provider creation failed: "
                f"{type(exc).__name__}") from exc

        def mutate(current: dict) -> dict:
            if current.get("runtime_mode") != "paper":
                raise PaperTrialOperatorError(
                    "paper cancellation runtime mode is not paper")
            if current.get("operator_pause") is not True:
                raise PaperTrialOperatorError(
                    "paper cancellation requires operator_pause=true")
            try:
                trial = validate_state(current.get("paper_trial"))
            except PaperTrialError as exc:
                raise PaperTrialOperatorError(str(exc)) from exc
            if not trial:
                raise PaperTrialOperatorError("paper trial state is unavailable")
            if trial.get("trial_id") != trial_id:
                raise PaperTrialOperatorError(
                    "confirmed trial id does not match the active incumbent")
            if trial.get("incumbent_identity") != incumbent_identity:
                raise PaperTrialOperatorError(
                    "confirmed incumbent identity does not match the active incumbent")
            if trial.get("state") not in {"running", "operator_cancelled"}:
                raise PaperTrialOperatorError(
                    "paper cancellation requires a running or already-cancelled trial")
            if trial.get("accepted_sessions") or trial.get("outcomes"):
                raise PaperTrialOperatorError(
                    "paper cancellation requires an evidence-empty trial")
            if not replacement_local_book_is_flat(current):
                raise PaperTrialOperatorError(
                    "paper cancellation requires a flat local paper book")
            fingerprint, flat_observation = _broker_snapshot(
                config, provider, current)
            if trial.get("state") == "operator_cancelled":
                _validate_cancelled_retry(
                    current, trial, cancel_reason, fingerprint)
                return current

            prestate = _runtime_prestate(current, trial)
            audit = _build_audit(
                trial, prestate, cancel_reason, fingerprint, flat_observation)
            durable_audit = _write_or_reuse_audit(audit)
            updated_trial = _plain(trial)
            updated_trial["state"] = "operator_cancelled"
            updated_trial["operator_cancellation"] = _cancellation_record(
                durable_audit)
            current["paper_trial"] = updated_trial
            return current

        result = state.update_state(mutate)
        return {
            "schema": AUDIT_SCHEMA,
            "status": "operator_cancelled",
            "trial_id": result["paper_trial"]["trial_id"],
            "incumbent_identity": result["paper_trial"]["incumbent_identity"],
            "audit_id": result["paper_trial"]["operator_cancellation"]["audit_id"],
            "authorizing": False,
            "proof_authority": False,
        }
    finally:
        state.release_run_lock(lock_handle)


__all__ = [
    "AUDIT_KIND", "AUDIT_SCHEMA", "MAX_BROKER_SNAPSHOT_SECONDS",
    "MAX_REASON_LENGTH", "PaperTrialOperatorError", "cancel_paper_trial",
    "verify_operator_cancellation",
]
