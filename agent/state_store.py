"""Validated atomic JSON state primitives."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

RUNNING = "RUNNING"
PAUSED = "PAUSED"
DAY_STOPPED = "DAY_STOPPED"
KILLED = "KILLED"

DEFAULT = {
    "state": PAUSED,
    "runtime_mode": None,
    "account_fingerprint": None,
    "active_trades": {},
    "protection": {},
    "opened_at": {},
    "kill_reason": None,
    "operator_pause": False,
    "orders": {},
    "risk_day": {},
    # Last emitted IBR session per immutable edge/symbol key.  Keeping only the
    # latest session makes this bounded while preserving the one-signal/session
    # contract across process restarts.
    "signal_sessions": {},
    "last_reconciliation_ts": None,
    "preflight": {},
    # Closed-trade learning events awaiting durable ingestion by the research
    # ledger.  They ride the same atomic replacement as the trade close that
    # produced them, so a crash cannot book a close and forget its outcome.
    "edge_outbox": [],
}


class StateCorruptionError(RuntimeError):
    """The durable runtime state exists but cannot be trusted."""


def _validated(data: Mapping[str, Any]) -> dict:
    # Drop fields from older state formats instead of propagating them into
    # the mode-scoped control file.
    value = deepcopy(DEFAULT)
    value.update({key: data[key] for key in DEFAULT if key in data})
    if value.get("state") not in {RUNNING, PAUSED, DAY_STOPPED, KILLED}:
        raise ValueError("invalid state")
    if value.get("runtime_mode") not in {None, "paper", "live", "test"}:
        raise ValueError("runtime_mode must be paper, live, or test")
    fingerprint = value.get("account_fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str)
                                    or not fingerprint.startswith(("alpaca-paper-", "alpaca-live-"))
                                    or len(fingerprint) > 80):
        raise ValueError("account_fingerprint is invalid")
    if (value.get("runtime_mode") in {"paper", "live"} and fingerprint and
            not fingerprint.startswith(f"alpaca-{value['runtime_mode']}-")):
        raise ValueError("account_fingerprint does not match runtime_mode")
    for key in ("active_trades", "protection", "opened_at",
                "orders", "risk_day", "signal_sessions", "preflight"):
        if not isinstance(value.get(key), dict):
            raise ValueError(f"{key} must be an object")
    sessions = value["signal_sessions"]
    if len(sessions) > 10_000 or any(
            not isinstance(key, str) or not key or len(key) > 240 or
            not isinstance(session, str) or len(session) != 10
            for key, session in sessions.items()):
        raise ValueError("signal_sessions must map bounded edge keys to ISO session dates")
    outbox = value.get("edge_outbox")
    if not isinstance(outbox, list) or any(not isinstance(item, dict) for item in outbox):
        raise ValueError("edge_outbox must be a list of objects")
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
    except FileNotFoundError:
        return deepcopy(fallback)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateCorruptionError(f"runtime state is unreadable: {exc}") from exc
    if not isinstance(value, Mapping):
        raise StateCorruptionError("runtime state must be a JSON object")
    # A state file that predates the durable pause field cannot safely be
    # interpreted as unpaused.  Keep DEFAULT fallback behavior for a missing
    # file, while treating an existing incomplete file as corruption.
    if "operator_pause" not in value:
        raise StateCorruptionError("runtime state is missing operator_pause")
    try:
        return _validated(value)
    except ValueError as exc:
        raise StateCorruptionError(f"runtime state is invalid: {exc}") from exc
