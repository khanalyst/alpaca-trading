#!/usr/bin/env python3
"""Independent flatten-only watchdog for the option profile's software stop.

Alpaca supports market and limit day orders on options only: no bracket, no
OCO/OTO, and no stop or stop-limit at all.  An option position's protective
stop is therefore the trader's own 60-second poller, and its failure mode is
the trader dying with exposure open.  This process watches the trader's
heartbeat from outside the trader, holds its own broker session, and can only
ever do one thing: cancel resting protective legs and flatten.

It never opens a position.  The interlock against racing a live trader into a
double close is the mode-scoped run lock: a running trader holds it for its
whole life, so a watchdog that cannot take that lock stays inert no matter how
stale the heartbeat looks, and the flatten it eventually performs runs under
the same lock and the same deterministic client-order-id scheme the trader
uses.  Before any close, the watchdog authenticates the broker account and
binds the returned account id to the mode-scoped runtime fingerprint.  A
hung-but-alive trader still owns the lock and is deliberately left alone; no
action beats an unsynchronized second closer.

Nothing local helps when the broker or the network is unreachable.  That
residual is real and is documented rather than papered over.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent import state as agent_state  # noqa: E402
from deploy.health import _fresh, _read_json  # noqa: E402

log = logging.getLogger("watchdog")

DEFAULT_MAX_HEARTBEAT_AGE = 300.0
DEFAULT_INTERVAL = 30.0

INERT_TRADER = "trader_holds_run_lock"
INERT_FRESH = "heartbeat_fresh"
INERT_FLAT = "no_open_positions"
ACT_STALE = "stale_heartbeat_with_open_positions"


class WatchdogError(RuntimeError):
    """The watchdog cannot prove it is safe to act."""


def write_status(path: Path, status: str, **detail) -> dict:
    payload = {"schema": 1, "status": status, "updated_ts": time.time(),
               "pid": os.getpid(), **detail}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
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
    return payload


def decide(heartbeat: Mapping[str, Any] | None, positions: Any, *,
           trader_alive: bool, max_age: float, now: float | None = None) -> dict:
    """Decide whether flattening is both necessary and unambiguously safe.

    The order of the tests is the safety argument: a live trader wins over
    every other signal, a fresh heartbeat wins over exposure, and exposure the
    broker does not report is not exposure this process may act on.  A missing
    or unreadable heartbeat is stale, never fresh.
    """
    if trader_alive:
        return {"act": False, "reason": INERT_TRADER}
    fresh = _fresh((heartbeat or {}).get("updated_ts"), max_age, now)
    if fresh:
        return {"act": False, "reason": INERT_FRESH}
    if not positions:
        return {"act": False, "reason": INERT_FLAT}
    return {"act": True, "reason": ACT_STALE,
            "symbols": sorted({str(getattr(item, "symbol", "")).upper()
                               for item in positions})}


def _engine(cfg: Mapping[str, Any], provider):
    # ``light=True`` builds the flatten/cancel machinery without the run loop,
    # the startup reconciliation, or any entry path.  The watchdog calls
    # exactly one method on it.
    from agent.engine import Engine
    return Engine(dict(cfg), light=True, provider=provider)


def _lock_handle_open(handle: Any) -> bool:
    """Return whether a transferred run-lock descriptor can still be used."""
    if handle is None or bool(getattr(handle, "closed", False)):
        return False
    try:
        handle.fileno()
    except (OSError, ValueError, AttributeError):
        return False
    return True


def _bind_verified_identity(cfg: Mapping[str, Any], provider) -> str:
    """Authenticate and bind the broker account before any destructive call."""
    mode = str(cfg.get("mode", "paper")).lower()
    if mode not in {"paper", "live"}:
        raise WatchdogError("watchdog mode is invalid")
    try:
        account = provider.account()
    except Exception as exc:  # noqa: BLE001
        raise WatchdogError(f"cannot verify broker account identity: {exc}") from exc
    account_id = getattr(account, "id", None)
    if account_id is None and isinstance(account, Mapping):
        account_id = account.get("id")
    account_id = str(account_id or "").strip()
    if not account_id:
        raise WatchdogError("broker account identity is unavailable")
    session = getattr(provider, "session", None)
    api_key = getattr(session, "api_key", None)
    if not api_key:
        broker = cfg.get("broker") if isinstance(cfg.get("broker"), Mapping) else {}
        api_key = broker.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise WatchdogError("authenticated API key is unavailable")
    fingerprint = agent_state.account_fingerprint(
        mode, f"{api_key}\0{account_id}")
    try:
        agent_state.bind_account_identity(fingerprint)
    except Exception as exc:  # noqa: BLE001
        raise WatchdogError(f"runtime account identity verification failed: {exc}") from exc
    return fingerprint


def run_once(cfg: Mapping[str, Any], provider, *, max_age: float,
             now: float | None = None) -> dict:
    mode = str(cfg.get("mode", "paper")).lower()
    agent_state.configure_runtime(mode)
    handle = agent_state.acquire_run_lock()
    try:
        if handle is None:
            # A running trader legitimately owns the mode-scoped lock.  The
            # watchdog remains inert, including when that trader is hung.
            heartbeat = _read_json(agent_state.HEARTBEAT_FILE)
            return {
                "act": False, "reason": INERT_TRADER,
                "heartbeat_status": str(heartbeat.get("status") or "missing"),
                "status": "watching",
            }

        # Keep the lock from this point through every snapshot and the action.
        # Re-read the heartbeat immediately before flattening so a trader that
        # refreshed while the watchdog was obtaining its snapshot wins safely.
        heartbeat = _read_json(agent_state.HEARTBEAT_FILE)
        positions = provider.positions()
        verdict = decide(heartbeat, positions, trader_alive=False,
                         max_age=max_age, now=now)
        verdict["heartbeat_status"] = str(heartbeat.get("status") or "missing")
        if not verdict["act"]:
            verdict["status"] = "watching"
            return verdict

        heartbeat = _read_json(agent_state.HEARTBEAT_FILE)
        positions = provider.positions()
        verdict = decide(heartbeat, positions, trader_alive=False,
                         max_age=max_age, now=now)
        verdict["heartbeat_status"] = str(heartbeat.get("status") or "missing")
        if not verdict["act"]:
            verdict["status"] = "watching"
            return verdict

        # Identity is checked after the final read-only snapshot and before
        # constructing the flatten engine.  Missing identity is bound only
        # from this authenticated account response; an existing mismatch is
        # rejected by bind_account_identity and no order is submitted.
        verdict["account_fingerprint"] = _bind_verified_identity(cfg, provider)
        engine = _engine(cfg, provider)
        # Engine.flatten_all normally acquires a temporary lock.  Transfer the
        # watchdog's already-held descriptor so it cannot release/reacquire
        # around the destructive action and introduce a race.
        engine._lock_handle = handle
        owned_handle = handle
        handle = None
        try:
            verdict["flattened"] = bool(
                engine.flatten_all("watchdog_stale_heartbeat"))
            verdict["residual_risk"] = not verdict["flattened"]
            verdict["status"] = "acted" if verdict["flattened"] else "degraded"
        finally:
            # Let the engine own release when it still has a usable descriptor.
            # A current/future flatten implementation may release the
            # transferred handle itself; clear a closed descriptor before
            # close() so that method cannot attempt a second release.
            if not _lock_handle_open(owned_handle):
                engine._lock_handle = None
            try:
                engine.close()
            finally:
                # A lightweight/injected engine may not release its handle at
                # all.  Fall back only while the descriptor is still open.
                if _lock_handle_open(owned_handle):
                    agent_state.release_run_lock(owned_handle)
        return verdict
    finally:
        if handle is not None:
            agent_state.release_run_lock(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--status-file", default="runtime/health/watchdog.json")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--max-heartbeat-age", type=float,
                        default=DEFAULT_MAX_HEARTBEAT_AGE)
    parser.add_argument("--once", action="store_true")
    return parser


_running = True


def _stop(_signum, _frame) -> None:
    global _running
    _running = False


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval <= 0 or args.max_heartbeat_age <= 0:
        raise SystemExit("--interval and --max-heartbeat-age must be positive")
    env_file = os.getenv("ALPACA_AGENT_SECRETS_FILE")
    if env_file:
        from deploy.recorder import load_dotenv
        load_dotenv(env_file, override=False)
    from main import load_cfg
    from agent.alpaca_provider import AlpacaProvider
    cfg = load_cfg(args.config)
    provider = AlpacaProvider(cfg)
    status_path = Path(args.status_file)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    failures = 0
    while True:
        try:
            verdict = run_once(cfg, provider, max_age=args.max_heartbeat_age)
            failures = 0
            status = str(verdict.get("status") or (
                "watching" if not verdict["act"] else
                "acted" if verdict.get("flattened") is True else "degraded"))
            write_status(status_path, status,
                         **{key: value for key, value in verdict.items()
                            if key not in {"act", "status"}})
        except Exception as exc:                           # noqa: BLE001
            # The watchdog can only act on a state it has proved.  An error
            # here means it has not, so it reports and takes no action.
            failures += 1
            log.warning("watchdog cycle failed: %s", exc)
            write_status(status_path, "failed", reason=str(exc),
                         consecutive_failures=failures)
            if args.once:
                return 1
        if args.once:
            return 1 if verdict.get("act") and verdict.get("flattened") is not True else 0
        if not _running:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
