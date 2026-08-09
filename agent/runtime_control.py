"""Runtime shutdown, flattening, and process-loop controls."""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Mapping

from . import state
from .alpaca_domain import OrderRequest
from .alpaca_provider import AlpacaError
from .execution_lifecycle import (
    _TERMINAL_ORDER_STATUSES,
    _plain,
    _value,
)

log = logging.getLogger("engine")


class RuntimeControlMixin:
    def flatten_all(self, reason: str = "operator") -> bool:
        temporary = False
        if self._lock_handle is None:
            if not self._acquire_lock():
                self._event("flatten_blocked", {"reason": "runtime_lock_held"})
                return False
            temporary = True
        try:
            return self._flatten_all_impl(reason)
        finally:
            if temporary:
                self._release_lock()

    def _flatten_all_impl(self, reason: str = "operator") -> bool:
        def order_status(order: Any) -> str:
            value = _value(order, "status", "")
            return str(getattr(value, "value", value)).split(".")[-1].lower()

        try:
            self.provider.cancel_all_orders()
        except Exception as exc:  # noqa: BLE001
            self._event("flatten_cancel_error", {"reason": str(exc)})
            return False
        submitted: dict[str, dict[str, Any]] = {}
        retryable = {"canceled", "cancelled", "expired", "rejected",
                     "failed", "not_found"}
        for poll in range(6):
            try:
                positions = self.provider.positions()
            except Exception as exc:  # noqa: BLE001
                self._event("flatten_positions_error", {"reason": str(exc)})
                return False
            try:
                broker_orders = self.provider.orders()
            except Exception as exc:  # noqa: BLE001
                # A missing order snapshot cannot prove cancellation.  Never
                # report a successful flatten while the broker boundary is
                # unavailable.
                self._event("flatten_orders_error", {"reason": str(exc)})
                return False
            working_orders = [
                order for order in (broker_orders or [])
                if order_status(order) not in _TERMINAL_ORDER_STATUSES
            ]
            if not positions and not working_orders:
                try:
                    self.reconcile()
                except Exception as exc:  # noqa: BLE001
                    self._event("flatten_reconcile_error", {"reason": str(exc)})
                    return False
                self._event("flatten_confirmed", {"reason": reason, "poll": poll})
                return True
            for position in positions:
                qty = abs(Decimal(str(_value(position, "qty", 0))))
                if qty <= 0:
                    continue
                symbol = str(_value(position, "symbol", "")).upper()
                prior = submitted.get(symbol)
                if prior is not None:
                    saved = state.load_state().get("orders", {}).get(
                        str(prior.get("order_id")), {})
                    status = str(saved.get("status", prior.get("status", ""))).lower() \
                        if isinstance(saved, Mapping) else str(prior.get("status", "")).lower()
                    # Never duplicate a live or filled close.  Only an
                    # explicitly failed terminal order earns a new client id.
                    if status not in retryable:
                        continue
                    close_attempt = int(prior.get("attempt", 0)) + 1
                else:
                    close_attempt = 0
                close = getattr(self.provider, "close_position", None)
                if callable(close):
                    client_order_id = self._client_id(
                        "flatten", {"symbol": symbol, "setup_id": reason},
                        attempt=close_attempt)
                    try:
                        order = close(symbol, qty=qty, client_order_id=client_order_id,
                                      order_type="market", time_in_force="day")
                    except TypeError:
                        try:
                            order = close(symbol, qty=qty)
                        except TypeError:
                            order = close(symbol)
                else:
                    side = "sell" if str(_value(position, "side", "long")).lower() in {"long", "buy"} else "buy"
                    request = OrderRequest(symbol, qty, side, type="market", time_in_force="day",
                                           client_order_id=self._client_id(
                                               "flatten", {"symbol": symbol,
                                                           "setup_id": reason},
                                               close_attempt))
                    order = self.provider.submit_order(request)
                order_id = str(getattr(order, "id", None) or client_order_id
                               if callable(close) else
                               getattr(order, "id", None) or request.client_order_id)
                status = str(getattr(order, "status", "submitted") or "submitted").lower()
                order_state = {
                    "order_id": order_id, "symbol": symbol,
                    "status": status,
                    "client_order_id": (client_order_id if callable(close)
                                        else request.client_order_id),
                    "qty": str(qty), "action": "flatten", "reason": reason,
                    "attempt": close_attempt, "updated_ts": time.time(),
                }
                try:
                    state.log_order(
                        order, None if callable(close) else request,
                        action="flatten", reason=reason, symbol=symbol,
                        qty=float(qty), runtime_mode=self.mode,
                        run_id=self.run_id)
                    state.update_state(lambda current: {
                        **current,
                        "orders": {**current.get("orders", {}),
                                   order_id: order_state},
                    })
                except Exception as exc:  # noqa: BLE001
                    self._reconciled = False
                    self._preflight_error = (
                        "post-submit flatten durability failure; reconciliation required")
                    try:
                        state.commit({"operator_pause": True},
                                     transition=(state.RUNNING, state.PAUSED))
                    except Exception:  # noqa: BLE001
                        pass
                    raise AlpacaError(f"{self._preflight_error}: {exc}") from exc
                submitted[symbol] = {"order_id": order_id, "status": status,
                                     "attempt": close_attempt}
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001
                self._event("flatten_reconcile_error", {"reason": str(exc)})
                return False
            if poll < 5:
                time.sleep(0.25)
        positions_error = None
        try:
            residual_positions = self.provider.positions()
        except Exception as exc:  # noqa: BLE001
            self._event("flatten_positions_error", {"reason": str(exc)})
            residual_positions = []
            positions_error = str(exc)
        try:
            residual_orders = self.provider.orders()
        except Exception as exc:  # noqa: BLE001
            self._event("flatten_orders_error", {"reason": str(exc)})
            residual_orders = []
            orders_error = str(exc)
        else:
            orders_error = None
        residual_working_orders = [
            order for order in (residual_orders or [])
            if order_status(order) not in _TERMINAL_ORDER_STATUSES
        ]
        evidence = {
            "reason": reason,
            "residual": _plain(residual_positions),
            "residual_positions": _plain(residual_positions),
            "residual_working_orders": _plain(residual_working_orders),
            "residual_orders": _plain(residual_working_orders),
        }
        if positions_error is not None:
            evidence["positions_error"] = positions_error
        if orders_error is not None:
            evidence["orders_error"] = orders_error
        self._event("flatten_incomplete", evidence)
        return (not residual_positions and not residual_working_orders and
                positions_error is None and orders_error is None)

    def request_shutdown(self, reason: str = "shutdown") -> None:
        self.shutdown_reason = reason
        self.running = False
        try:
            state.commit({"operator_pause": True}, transition=(state.RUNNING, state.PAUSED))
            state.write_heartbeat("pausing", run_id=self.run_id, reason=reason)
        except Exception:
            pass

    def close(self) -> None:
        """Release the process lock and leave a truthful terminal heartbeat."""
        self.running = False
        try:
            # Persist the lifecycle transition before announcing a stopped
            # heartbeat.  Terminal states are intentionally absorbing: close
            # must not overwrite an operator kill or daily risk stop, and it
            # must preserve request_shutdown's operator_pause flag.
            try:
                state.commit({}, transition=(state.RUNNING, state.PAUSED))
            except Exception:
                pass
            try:
                state.write_heartbeat("stopped", run_id=self.run_id,
                                      reason=self.shutdown_reason or "closed")
            except Exception:
                pass
        finally:
            # State/heartbeat persistence is best effort, but a held process
            # lock is never allowed to survive close().
            self._release_lock()

    def run(self, *, max_cycles: int | None = None) -> None:
        if not self._acquire_lock(persistent=True):
            raise AlpacaError(f"another {self.mode} runtime process already holds the run lock")
        def start_runtime(runtime: dict) -> dict:
            if runtime.get("state") == state.KILLED:
                raise AlpacaError(runtime.get("kill_reason") or
                                  f"{self.mode} runtime is killed")
            runtime["state"] = state.RUNNING
            return runtime
        try:
            state.update_state(start_runtime)
        except Exception:
            self._release_lock()
            raise
        try:
            ready = self._ensure_order_ready()
        except BaseException:
            # Readiness may fail after start_runtime has durably marked the
            # process RUNNING.  Roll that state back before propagating the
            # original exception; close() also emits the terminal heartbeat
            # and releases the persistent lock.
            self.running = False
            try:
                state.write_heartbeat("degraded", run_id=self.run_id,
                                      reason=self._preflight_error or
                                      "runtime_readiness_failed")
            except Exception:
                pass
            self.close()
            raise
        if not ready:
            reason = self._preflight_error or \
                "runtime readiness and startup reconciliation are required"
            self.running = False
            try:
                state.write_heartbeat("degraded", run_id=self.run_id, reason=reason)
            except Exception:
                pass
            self.close()
            raise AlpacaError(reason)
        self.running = True
        cycles = 0
        run_failure: BaseException | None = None
        interval = float(self.cfg.get("cycle", {}).get("interval_seconds", 60))
        try:
            while self.running and (max_cycles is None or cycles < max_cycles):
                try:
                    self.run_once()
                except AlpacaError:
                    log.exception("Alpaca cycle failed; pausing safely")
                    self.running = False
                    try: state.write_heartbeat("degraded", reason="alpaca_error")
                    except Exception: pass
                    raise
                cycles += 1
                if self.running and (max_cycles is None or cycles < max_cycles):
                    time.sleep(interval)
        except BaseException as exc:
            run_failure = exc
            raise
        finally:
            exit_reason = self.shutdown_reason or "run_exit"
            flatten_failure: AlpacaError | None = None
            flatten_failure_reason: str | None = None
            try:
                complete = self._flatten_all_impl(exit_reason)
                if not complete:
                    state.write_heartbeat(
                        "degraded", run_id=self.run_id,
                        reason="shutdown_flatten_incomplete")
                    flatten_failure = AlpacaError(
                        f"shutdown flatten incomplete; {self.mode} positions may remain")
                    flatten_failure_reason = "shutdown_flatten_incomplete"
            except Exception as exc:  # noqa: BLE001
                self._event("shutdown_flatten_failed", {
                    "reason": exit_reason, "error": str(exc)})
                flatten_failure = AlpacaError(
                    f"shutdown flatten failed; {self.mode} positions may remain: {exc}")
                flatten_failure_reason = "shutdown_flatten_failed"
            if flatten_failure_reason is not None:
                # Residual exposure is an operator-pause condition.  The
                # transition only changes RUNNING, so KILLED/DAY_STOPPED are
                # preserved while a restart is prevented from trading.
                try:
                    state.commit({"operator_pause": True},
                                 transition=(state.RUNNING, state.PAUSED))
                except Exception:
                    pass
            self.close()
            if flatten_failure_reason is not None:
                # close() deliberately emits stopped for normal shutdown, but
                # residual exposure is not a normal terminal condition.  Keep
                # the final heartbeat degraded after the lock is released so
                # operators and watchdogs see the outstanding risk.
                try:
                    state.write_heartbeat("degraded", run_id=self.run_id,
                                          reason=flatten_failure_reason)
                except Exception:
                    pass
            if flatten_failure is not None and run_failure is None:
                raise flatten_failure
