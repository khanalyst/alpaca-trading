"""Runtime shutdown, flattening, and process-loop controls."""

from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Mapping

from . import state
from .alpaca_domain import OrderRequest
from .alpaca_provider import AlpacaError
from .contracts.rule import canonical_exit_reason
from .execution_lifecycle import (
    _CloseResponseUnconfirmed,
    _TERMINAL_ORDER_STATUSES,
    _bracket_stripped,
    _leg_live,
    _leg_rows,
    _plain,
    _trade_lifecycle_id,
    _validated_close_response,
    _value,
)

log = logging.getLogger("engine")


class _ResumeBlocked(AlpacaError):
    """Authorization failed after the resume command acquired its lock."""

    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        message = f"operator resume blocked: {reason}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def _normalized_status(value: Any) -> str:
    raw = _value(value, "status", "")
    raw = getattr(raw, "value", raw)
    return str(raw or "").split(".")[-1].strip().lower()


def _aligned_cycle_delay(now: float, interval: float) -> float:
    """Return the delay to the next shared wall-clock interval boundary."""
    cadence = float(interval)
    if cadence <= 0:
        return 0.0
    remainder = float(now) % cadence
    if remainder <= 1e-9:
        return cadence
    return cadence - remainder


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

    def _protected_legs_by_symbol(self) -> dict[str, list[dict]]:
        """Durable live protective legs, keyed by the symbol they protect."""
        # An empty fallback is unsafe here: flattening would bulk-cancel every
        # broker protection while believing there are no local brackets.  The
        # caller must fail closed and leave broker state untouched whenever the
        # durable active-trade record cannot be trusted.
        runtime = state.load_state()
        if not isinstance(runtime, Mapping):
            raise ValueError("runtime state is not an object")
        active = runtime.get("active_trades")
        if not isinstance(active, Mapping):
            raise ValueError("active_trades is not an object")
        found = {}
        for symbol, trade in active.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("active trade symbol is invalid")
            if not isinstance(trade, Mapping):
                raise ValueError(f"active trade {symbol!r} is malformed")
            if not trade:
                raise ValueError(f"active trade {symbol!r} is empty")
            if "protective_legs" in trade:
                legs = trade.get("protective_legs")
                if not isinstance(legs, list):
                    raise ValueError(f"active trade {symbol!r} protective_legs is malformed")
                for leg in legs:
                    if not isinstance(leg, Mapping) or not str(
                            leg.get("order_id") or "").strip():
                        raise ValueError(
                            f"active trade {symbol!r} protective leg is malformed")
            live = [leg for leg in _leg_rows(trade) if _leg_live(leg)]
            if live:
                found[str(symbol).upper()] = live
        return found

    @staticmethod
    def _flatten_rows(runtime: Mapping[str, Any], symbol: str) -> list[dict]:
        orders = runtime.get("orders", {}) if isinstance(runtime, Mapping) else {}
        if not isinstance(orders, Mapping):
            return []
        rows = [dict(row) for row in orders.values()
                if isinstance(row, Mapping) and
                str(row.get("symbol", "")).upper() == symbol and
                str(row.get("action", "")).lower() == "flatten"]
        rows.sort(key=lambda row: (
            int(row.get("attempt", 0) or 0),
            float(row.get("updated_ts", 0) or 0),
            str(row.get("order_id", ""))))
        return rows

    def _flatten_lifecycle_id(self, symbol: str, trade: Mapping | None,
                              runtime: Mapping[str, Any]) -> str:
        """Resolve or allocate a durable flatten identity for one position."""
        if isinstance(trade, Mapping) and trade:
            return _trade_lifecycle_id(trade, symbol)
        rows = self._flatten_rows(runtime, symbol)
        for row in reversed(rows):
            lifecycle = row.get("lifecycle_id")
            status = str(row.get("status", "")).lower()
            if lifecycle and status not in _TERMINAL_ORDER_STATUSES:
                return str(lifecycle)
        return f"orphan:{symbol}:{time.time_ns()}"

    def _persist_flatten_order(self, order_state: Mapping[str, Any], *,
                               intent_id: str | None = None) -> dict:
        """Promote a flatten intent/order and bind it to local exposure."""
        row = dict(order_state)
        order_id = str(row.get("order_id") or "")
        symbol = str(row.get("symbol") or "").upper()

        def record(current: dict) -> dict:
            orders = {**current.get("orders", {})}
            if intent_id:
                orders.pop(intent_id, None)
            orders[order_id] = row
            active = {**current.get("active_trades", {})}
            bound = active.get(symbol)
            if isinstance(bound, Mapping):
                bound = dict(bound)
                exit_reason = canonical_exit_reason(row.get("reason"))
                bound.update({
                    "status": "closing",
                    "closing_reason": row.get("reason"),
                    "closing_exit_reason": exit_reason,
                    "canonical_exit_reason": exit_reason,
                    "closing_price": row.get("filled_avg_price"),
                    "closing_order_id": order_id,
                    "closing_attempt": row.get("attempt", 0),
                    "updated_ts": time.time(),
                })
                active[symbol] = bound
            return {**current, "orders": orders, "active_trades": active}

        return state.update_state(record)

    @staticmethod
    def _persist_flatten_intent(intent_state: Mapping[str, Any]) -> dict:
        """Durably reserve a symbol before its protective legs are cancelled."""
        row = dict(intent_state)
        intent_id = str(row.get("order_id") or "")
        symbol = str(row.get("symbol") or "").upper()

        def record(current: dict) -> dict:
            orders = {**current.get("orders", {}), intent_id: row}
            active = {**current.get("active_trades", {})}
            bound = active.get(symbol)
            if isinstance(bound, Mapping):
                bound = dict(bound)
                exit_reason = canonical_exit_reason(row.get("reason"))
                bound.update({
                    "status": "closing",
                    "closing_reason": row.get("reason"),
                    "closing_exit_reason": exit_reason,
                    "canonical_exit_reason": exit_reason,
                    "closing_order_id": intent_id,
                    "closing_attempt": row.get("attempt", 0),
                    "updated_ts": time.time(),
                })
                active[symbol] = bound
            return {**current, "orders": orders, "active_trades": active}

        return state.update_state(record)

    def _promote_flatten_intents(self, broker_orders: list[Any] | None) -> bool:
        """Recover broker orders submitted after a durable intent was written."""
        runtime = state.load_state()
        orders = runtime.get("orders", {}) if isinstance(runtime, Mapping) else {}
        if not isinstance(orders, Mapping):
            return False
        promoted = False
        for intent_id, intent in list(orders.items()):
            if (not isinstance(intent, Mapping) or
                    str(intent.get("action", "")).lower() != "flatten" or
                    str(intent.get("status", "")).lower() != "intent"):
                continue
            client_order_id = str(intent.get("client_order_id") or "")
            if not client_order_id:
                continue
            broker_order = next(
                (order for order in (broker_orders or [])
                 if str(_value(order, "client_order_id", "") or "") == client_order_id),
                None,
            )
            if broker_order is None:
                continue
            try:
                order_id, broker_status = _validated_close_response(broker_order)
            except AlpacaError as exc:
                self._event("flatten_intent_unresolved", {
                    "client_order_id": client_order_id, "reason": str(exc)})
                continue
            row = dict(intent)
            row.update({
                "order_id": order_id,
                "status": broker_status,
                "filled_qty": float(_value(broker_order, "filled_qty", 0) or 0),
                "filled_avg_price": _value(
                    broker_order, "filled_avg_price", None),
                "updated_ts": time.time(),
            })
            self._persist_flatten_order(row, intent_id=str(intent_id))
            promoted = True
        return promoted

    def _recover_flatten_protection(self) -> dict:
        """Restore confirmed equity stops only after fresh recovery reads."""
        from .flatten_protection import restore_flatten_protection

        try:
            positions = self.provider.positions()
        except Exception:
            positions = None
        try:
            orders = self.provider.orders(status="all")
        except Exception:
            orders = None
        try:
            evidence = restore_flatten_protection(self, positions, orders)
        except Exception as exc:
            evidence = {"restored": [], "errors": [{"reason": str(exc)}],
                        "skipped": []}
        self._event("flatten_protection_recovery", evidence)
        return evidence

    def _flatten_all_impl(self, reason: str = "operator") -> bool:
        try:
            complete = self._flatten_positions(reason)
        except Exception:
            self._recover_flatten_protection()
            self._pause_with_residual_risk("flatten_failed")
            raise
        if not complete:
            self._recover_flatten_protection()
            self._pause_with_residual_risk("flatten_incomplete")
        return complete

    def _flatten_positions(self, reason: str = "operator") -> bool:
        def order_status(order: Any) -> str:
            value = _value(order, "status", "")
            return str(getattr(value, "value", value)).split(".")[-1].lower()

        # Cancelling a bracket parent cancels its protective legs, so a bulk
        # cancel run before the flatten completes strips protection from every
        # position at once and keeps it stripped for the whole retry budget.
        # While protected positions exist, each symbol's legs are cancelled
        # immediately before that symbol's close, and the bulk sweep is
        # deferred until the book is flat.
        try:
            protected = self._protected_legs_by_symbol()
        except Exception as exc:  # noqa: BLE001
            self._event("flatten_state_error", {
                "reason": "active_trade_state_unreadable_or_malformed",
                "error": str(exc),
            })
            return False
        swept = False
        if not protected:
            try:
                self.provider.cancel_all_orders()
            except Exception as exc:  # noqa: BLE001
                self._event("flatten_cancel_error", {"reason": str(exc)})
                return False
            swept = True
        submitted: dict[str, dict[str, Any]] = {}
        retryable = {"canceled", "cancelled", "expired", "rejected",
                     "failed", "not_found"}
        # Symbols whose broker legs this flatten cancelled.  A close that then
        # fails or never fills must not leave them without protection.
        unprotected: dict[str, tuple[Mapping[str, Any] | None, Any]] = {}
        try:
            for symbol, trade in (state.load_state().get("active_trades") or {}).items():
                if _bracket_stripped(trade):
                    # An earlier call stripped this bracket and could not
                    # restore a stop; a failed close here must restore it.
                    unprotected[str(symbol).upper()] = (trade, None)
        except Exception:  # noqa: BLE001
            pass
        failed_symbols: set[str] = set()
        ambiguous_close_symbols: set[str] = set()
        # Client ids of every close this call submitted, per symbol.  The
        # end-of-call re-arm looks each up, so it never places a stop over a
        # close that reached the broker or has already filled.
        close_ids: dict[str, list[str]] = {}
        # Every failure below leaves the poll loop through the residual check,
        # never through an early return, so exposures stripped of their legs
        # earlier in this call still get their broker stop restored.  A loop
        # that stopped early never reports a confirmed flatten.
        loop_error = None
        for poll in range(6):
            try:
                positions = self.provider.positions()
            except Exception as exc:  # noqa: BLE001
                self._event("flatten_positions_error", {"reason": str(exc)})
                loop_error = f"positions unavailable: {exc}"
                break
            try:
                broker_orders = self.provider.orders()
            except Exception as exc:  # noqa: BLE001
                # A missing order snapshot cannot prove cancellation.  Never
                # report a successful flatten while the broker boundary is
                # unavailable.
                self._event("flatten_orders_error", {"reason": str(exc)})
                loop_error = f"orders unavailable: {exc}"
                break
            try:
                self._promote_flatten_intents(broker_orders)
            except Exception as exc:  # noqa: BLE001
                self._event("flatten_state_error", {
                    "reason": "flatten_intent_recovery_failed",
                    "error": str(exc),
                })
                return False
            working_orders = [
                order for order in (broker_orders or [])
                if order_status(order) not in _TERMINAL_ORDER_STATUSES
            ]
            if not positions and not swept:
                try:
                    self.provider.cancel_all_orders()
                    broker_orders = self.provider.orders()
                except Exception as exc:  # noqa: BLE001
                    self._event("flatten_cancel_error", {"reason": str(exc)})
                    return False
                swept = True
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
                reconciled = state.load_state()
                residual_active = reconciled.get("active_trades", {}) \
                    if isinstance(reconciled, Mapping) else {}
                residual_protection = reconciled.get("protection", {}) \
                    if isinstance(reconciled, Mapping) else {}
                if residual_active or residual_protection:
                    # No broker positions or working orders is the
                    # authoritative flat boundary.  Reconcile can retain a
                    # local close-intent row when a protective child filled
                    # during cancellation; clear only the local exposure
                    # buckets while retaining all durable order evidence.
                    try:
                        state.update_state(lambda current: {
                            **current, "active_trades": {}, "protection": {}})
                    except Exception as exc:  # noqa: BLE001
                        self._event("flatten_state_error", {
                            "reason": "flat_local_cleanup_failed",
                            "error": str(exc)})
                        return False
                self._event("flatten_confirmed", {"reason": reason, "poll": poll})
                return True
            runtime_now = state.load_state()
            active_now = runtime_now.get("active_trades", {})
            active_now = active_now if isinstance(active_now, Mapping) else {}
            working_ids = {str(_value(order, "id", "") or "")
                           for order in working_orders}
            # Exit fill evidence, terminal orders included.  The position
            # endpoint can lag the order endpoint, so every close below is
            # sized from the exposure's entry fill less its exits' fills.
            every_orders = None
            if any(isinstance(active_now.get(str(_value(item, "symbol", "")).upper()), Mapping)
                   for item in positions):
                try:
                    every_orders = self._all_orders()
                except Exception as exc:  # noqa: BLE001
                    self._event("flatten_orders_error", {"reason": str(exc)})
                    loop_error = f"order history unavailable: {exc}"
                    break
            every_by_id = {str(_value(order, "id", "") or ""): order
                           for order in (every_orders or [])}
            for position in positions:
                qty = abs(Decimal(str(_value(position, "qty", 0))))
                if qty <= 0:
                    continue
                symbol = str(_value(position, "symbol", "")).upper()
                if symbol in failed_symbols:
                    # A submit exception leaves a durable intent.  Re-enter
                    # the bounded poll so the same CID can be retried after
                    # broker lookup; it is never treated as a fresh close.
                    failed_symbols.discard(symbol)
                runtime_snapshot = state.load_state()
                active_snapshot = runtime_snapshot.get("active_trades", {}) \
                    if isinstance(runtime_snapshot, Mapping) else {}
                durable_trade = (active_snapshot.get(symbol)
                                 if isinstance(active_snapshot, Mapping) else None)
                if isinstance(durable_trade, Mapping):
                    pending_monitor_id = str(
                        durable_trade.get("close_intent_id") or "")
                    if pending_monitor_id:
                        saved_orders = runtime_snapshot.get("orders", {})
                        pending_monitor = (
                            saved_orders.get(pending_monitor_id, {})
                            if isinstance(saved_orders, Mapping) else {})
                        if (isinstance(pending_monitor, Mapping) and
                                str(pending_monitor.get("action", "")).lower() == "close" and
                                str(pending_monitor.get("status", "")).lower() == "intent"):
                            # The monitor owns this exact pre-submit close
                            # domain. Never create a competing flatten CID.
                            self._event("flatten_deferred_close_intent", {
                                "symbol": symbol,
                                "client_order_id": pending_monitor.get(
                                    "client_order_id"),
                            })
                            continue
                    existing_id = durable_trade.get("closing_order_id")
                    if existing_id:
                        saved_orders = runtime_snapshot.get("orders", {})
                        existing_close = (saved_orders.get(str(existing_id), {})
                                          if isinstance(saved_orders, Mapping) else {})
                        existing_status = (
                            str(existing_close.get("status", "")).lower()
                            if isinstance(existing_close, Mapping) else "")
                        own_leg_ids = {
                            str(leg.get("order_id"))
                            for leg in _leg_rows(durable_trade)}
                        own_intent = (
                            existing_status == "intent" and
                            str(existing_close.get("action", "")).lower() == "flatten")
                        if (not own_intent and
                                str(existing_id) not in own_leg_ids and
                                (not existing_status or
                                 existing_status not in _TERMINAL_ORDER_STATUSES)):
                            continue
                trade = active_now.get(symbol)
                trade = trade if isinstance(trade, Mapping) else None
                if trade is None and isinstance(durable_trade, Mapping):
                    trade = durable_trade
                lifecycle_id = self._flatten_lifecycle_id(
                    symbol, trade, runtime_snapshot)
                prior = submitted.get(symbol)
                if prior is not None:
                    saved = state.load_state().get("orders", {}).get(
                        str(prior.get("order_id")), {})
                    status = str(saved.get("status", prior.get("status", ""))).lower() \
                        if isinstance(saved, Mapping) else str(prior.get("status", "")).lower()
                    # Never duplicate a live close. A durable pre-submit intent
                    # remains the owner until broker lookup promotes or retries it.
                    if status not in retryable and status != "intent":
                        continue
                    close_attempt = int(prior.get("attempt", 0))
                    if status in retryable and str(
                            prior.get("status", "")).lower() != "intent":
                        close_attempt += 1
                else:
                    working_close = str((trade or {}).get("closing_order_id") or "")
                    own_legs = {str(leg.get("order_id")) for leg in _leg_rows(trade)}
                    if (working_close and working_close in working_ids and
                            working_close not in own_legs):
                        # An exit for this exposure is already working at the
                        # broker.  A second close would either be refused for
                        # reserved quantity or overfill into a reversal.  A
                        # partly filled bracket leg is not such an exit: it is
                        # cancelled below and the residual is closed.
                        continue
                    durable_rows = [row for row in self._flatten_rows(
                        runtime_snapshot, symbol)
                                    if str(row.get("lifecycle_id", "")) == lifecycle_id]
                    prior = durable_rows[-1] if durable_rows else None
                    if prior is not None:
                        prior_status = str(prior.get("status", "")).lower()
                        if prior_status not in retryable and prior_status != "intent":
                            continue
                        close_attempt = (int(prior.get("attempt", 0))
                                         if prior_status == "intent" else
                                         int(prior.get("attempt", 0)) + 1)
                    else:
                        close_attempt = 0
                close = getattr(self.provider, "close_position", None)
                if isinstance(trade, Mapping) and trade.get("order_id"):
                    client_order_id = self._exit_client_id(
                        "flatten", symbol, reason, trade=trade, position=position,
                        attempt=close_attempt)
                else:
                    # Broker-only generations have no entry order identity;
                    # bind their flatten to the durable orphan lifecycle so a
                    # later same-day position cannot inherit the old CID.
                    client_order_id = self._client_id(
                        "flatten", {"symbol": symbol, "setup_id": lifecycle_id},
                        attempt=close_attempt)
                intent_id = f"flatten-intent:{symbol}:{lifecycle_id}:{close_attempt}"
                intent_state = {
                    "order_id": intent_id, "symbol": symbol,
                    "status": "intent", "client_order_id": client_order_id,
                    "qty": str(qty), "action": "flatten", "reason": reason,
                    "entry_order_id": (trade.get("order_id")
                                        if isinstance(trade, Mapping) else None),
                    "lifecycle_id": lifecycle_id, "attempt": close_attempt,
                    "updated_ts": time.time(),
                }
                try:
                    self._persist_flatten_intent(intent_state)
                except Exception as exc:  # noqa: BLE001
                    self._event("flatten_state_error", {
                        "reason": "flatten_intent_durability_failed",
                        "error": str(exc), "symbol": symbol})
                    return False
                legs = list(protected.get(symbol) or [])
                if trade is not None:
                    # An own leg the broker still works is cancelled whatever
                    # its local status says: a cancel that met a fill marks it
                    # filled locally while the broker may still be working it.
                    listed = {str(leg.get("order_id")) for leg in legs}
                    legs += [leg for leg in _leg_rows(trade)
                             if str(leg.get("order_id")) not in listed]
                    for index, leg in enumerate(legs):
                        broker_leg = every_by_id.get(str(leg.get("order_id")))
                        if (broker_leg is not None and
                                order_status(broker_leg) not in _TERMINAL_ORDER_STATUSES):
                            if leg.get("status") != order_status(broker_leg):
                                legs[index] = {**leg, "status": order_status(broker_leg)}
                if trade is not None and every_orders is not None:
                    exits_filled, _ = self._exposure_exit_fills(
                        trade, every_orders, orders_state=runtime_now.get("orders"),
                        close_client_ids=close_ids.get(symbol, ()))
                    size = self._exposure_qty(trade)
                    if size is not None and exits_filled > 1e-9:
                        remaining = size - exits_filled
                        if remaining <= 1e-9 or abs(float(qty) - remaining) > 1e-6:
                            # The exposure's own exits have taken part or all
                            # of it and the position snapshot has not caught
                            # up.  Closing now could reverse the position, so
                            # wait for a snapshot that agrees.
                            self._event("flatten_waiting_for_position", {
                                "symbol": symbol, "snapshot_qty": str(qty),
                                "remaining_qty": max(0.0, remaining)})
                            continue
                if legs:
                    unprotected[symbol] = (trade, position)
                if legs and not self._cancel_protective_legs(symbol, legs):
                    # A resting leg reserves the quantity; never submit a close
                    # that the broker would bounce.  A leg that filled in the
                    # cancel race leaves a residual, which a later poll closes
                    # once the broker's fills and the snapshot agree.  Other
                    # symbols go on.
                    continue
                protected.pop(symbol, None)
                close_ids.setdefault(symbol, []).append(client_order_id)
                request = None
                try:
                    if callable(close):
                        try:
                            order = close(symbol, qty=qty,
                                          client_order_id=client_order_id,
                                          order_type="market", time_in_force="day")
                        except TypeError:
                            try:
                                order = close(symbol, qty=qty)
                            except TypeError:
                                order = close(symbol)
                    else:
                        side = ("sell" if str(_value(position, "side", "long")).lower()
                                in {"long", "buy"} else "buy")
                        request = OrderRequest(symbol, qty, side, type="market",
                                               time_in_force="day",
                                               client_order_id=client_order_id)
                        order = self.provider.submit_order(request)
                    order_id, status = _validated_close_response(order)
                    ambiguous_close_symbols.discard(symbol)
                except Exception as exc:  # noqa: BLE001
                    # An ambiguous failure may still have created the close at
                    # the broker.  Protection is restored below only from a
                    # fresh snapshot proving the position remains and nothing
                    # is working for it.
                    self._event("flatten_close_failed", {
                        "symbol": symbol, "reason": reason,
                        "attempt": close_attempt, "error": str(exc)})
                    submitted[symbol] = {
                        "order_id": intent_id, "status": "intent",
                        "attempt": close_attempt,
                        "lifecycle_id": lifecycle_id,
                    }
                    failed_symbols.add(symbol)
                    if isinstance(exc, _CloseResponseUnconfirmed):
                        ambiguous_close_symbols.add(symbol)
                    continue
                entry_order_id = (str(trade.get("order_id") or "")
                                  if trade is not None else "")
                order_state = {
                    "order_id": order_id, "symbol": symbol,
                    "status": status,
                    "client_order_id": client_order_id,
                    "qty": str(qty), "action": "flatten", "reason": reason,
                    "entry_order_id": entry_order_id or None,
                    "lifecycle_id": lifecycle_id, "intent_id": intent_id,
                    "filled_qty": float(_value(order, "filled_qty", 0) or 0),
                    "filled_avg_price": _value(order, "filled_avg_price", None),
                    "attempt": close_attempt, "closing_attempt": close_attempt,
                    "updated_ts": time.time(),
                }
                if entry_order_id:
                    # Bind the close to its exposure so reconciliation books it
                    # against this trade and never against a later one.
                    order_state["entry_order_id"] = entry_order_id

                def book_close(current: dict, *, symbol=symbol, order_id=order_id,
                               order_state=order_state, entry_order_id=entry_order_id,
                               close_attempt=close_attempt) -> dict:
                    current = {**current,
                               "orders": {**current.get("orders", {}),
                                          order_id: order_state}}
                    active = current.get("active_trades", {})
                    row = active.get(symbol) if isinstance(active, Mapping) else None
                    # A position known only to the broker has no entry order;
                    # the close binds through closing_order_id, as the
                    # monitor's own closes of such rows do.
                    if (isinstance(row, dict) and
                            str(row.get("order_id") or "") == entry_order_id):
                        exit_reason = canonical_exit_reason(reason)
                        row.update({"status": "closing", "closing_reason": reason,
                                    "closing_exit_reason": exit_reason,
                                    "canonical_exit_reason": exit_reason,
                                    "closing_order_id": order_id,
                                    "closing_attempt": close_attempt,
                                    "updated_ts": time.time()})
                    return current

                try:
                    state.log_order(
                        order, request,
                        action="flatten", reason=reason, symbol=symbol,
                        qty=float(qty), runtime_mode=self.mode,
                        client_order_id=client_order_id,
                        entry_order_id=entry_order_id or None,
                        lifecycle_id=lifecycle_id, run_id=self.run_id)
                    self._persist_flatten_order(
                        order_state, intent_id=intent_id)
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
                                     "attempt": close_attempt,
                                     "lifecycle_id": lifecycle_id}
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001
                self._event("flatten_reconcile_error", {"reason": str(exc)})
                loop_error = f"reconciliation failed: {exc}"
                break
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
        if unprotected and positions_error is None and orders_error is None:
            # This call cancelled these exposures' bracket legs, but their
            # closes did not complete and nothing is working for them now.
            # Restore a broker stop rather than leave them unprotected.
            working_symbols = {str(_value(order, "symbol", "")).upper()
                               for order in residual_working_orders}
            for position in residual_positions or []:
                symbol = str(_value(position, "symbol", "")).upper()
                if (symbol in unprotected and symbol not in working_symbols and
                        symbol not in ambiguous_close_symbols):
                    trade, _ = unprotected.pop(symbol)
                    self._rearm_protective_stop(
                        symbol, trade, position, reason=reason,
                        close_client_ids=close_ids.get(symbol, ()))
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
        if loop_error is not None:
            evidence["loop_error"] = loop_error
        self._event("flatten_incomplete", evidence)
        return (not residual_positions and not residual_working_orders and
                positions_error is None and orders_error is None and
                loop_error is None)

    def _resume_pause_invariants(self, runtime: Mapping[str, Any], *,
                                 check_orders: bool = True) -> None:
        """Validate the durable pause gate without changing any state."""
        if not isinstance(runtime, Mapping):
            raise _ResumeBlocked("runtime_state_malformed")
        if runtime.get("state") != state.PAUSED:
            current = runtime.get("state")
            raise _ResumeBlocked(f"runtime_state_{str(current or 'unknown').lower()}")
        if runtime.get("operator_pause") is not True:
            raise _ResumeBlocked("operator_pause_required")
        kill_reason = runtime.get("kill_reason")
        if (kill_reason is not None and kill_reason is not False and
                kill_reason != ""):
            raise _ResumeBlocked("kill_reason_present")
        if runtime.get("runtime_mode") != self.mode:
            raise _ResumeBlocked("runtime_mode_mismatch")
        risk_day = runtime.get("risk_day", {})
        if not isinstance(risk_day, Mapping):
            raise _ResumeBlocked("risk_day_malformed")
        if "limit_hit" in risk_day:
            limit_hit = risk_day.get("limit_hit")
            if limit_hit is True:
                raise _ResumeBlocked("daily_risk_limit_hit")
            if limit_hit is not False:
                raise _ResumeBlocked("risk_day_malformed")
        for name in ("active_trades", "protection"):
            rows = runtime.get(name, {})
            if not isinstance(rows, Mapping):
                raise _ResumeBlocked(f"{name}_malformed")
            if any(not isinstance(row, Mapping) for row in rows.values()):
                raise _ResumeBlocked(f"{name}_malformed")
            if rows:
                raise _ResumeBlocked(f"{name}_present")
        orders = runtime.get("orders", {})
        if not isinstance(orders, Mapping):
            raise _ResumeBlocked("orders_malformed")
        for key, row in orders.items():
            if not isinstance(row, Mapping):
                raise _ResumeBlocked("orders_malformed")
            if check_orders and _normalized_status(row) not in _TERMINAL_ORDER_STATUSES:
                raise _ResumeBlocked("nonterminal_local_orders")

    def _record_resume_block(self, reason: str, detail: str | None = None) -> None:
        payload = {
            "phase": "authorization",
            "reason": reason,
            "mode": self.mode,
            "run_id": self.run_id,
        }
        if detail:
            payload["error"] = detail
        try:
            state.log_event(
                "operator_resume_blocked",
                json.dumps(payload, sort_keys=True, default=str),
                runtime_mode=self.mode,
                run_id=self.run_id,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            state.write_heartbeat("degraded", run_id=self.run_id, reason=reason)
        except Exception:  # noqa: BLE001
            pass

    def resume(self) -> dict[str, Any]:
        """Authorize a paused runtime to be started again, without trading."""
        # A control handle must never steal or overwrite the heartbeat of a
        # trader that is already running, even when that trader owns the OS
        # lock.  Only the successful acquisition path may emit recovery state.
        if self.running:
            raise AlpacaError("operator resume blocked: runtime_already_running")
        if self._lock_handle is not None or self._persistent_lock:
            raise AlpacaError("operator resume blocked: runtime_lock_held")
        if not self._acquire_lock():
            raise AlpacaError("operator resume blocked: runtime_lock_held")

        phase = "journal"
        try:
            try:
                state.check_journal()
                runtime = state.load_state()
                self._resume_pause_invariants(runtime, check_orders=False)

                # A control object is intentionally light, so force a fresh
                # authenticated preflight instead of trusting constructor data.
                phase = "preflight"
                self._preflight = None
                self.preflight()
                runtime = state.load_state()
                self._resume_pause_invariants(runtime, check_orders=False)

                phase = "reconcile"
                snapshot = self.reconcile()
                if not isinstance(snapshot, Mapping):
                    raise _ResumeBlocked("reconcile_snapshot_malformed")
                positions = snapshot.get("positions")
                broker_orders = snapshot.get("orders")
                if not isinstance(positions, (list, tuple)):
                    raise _ResumeBlocked("reconcile_positions_malformed")
                if not isinstance(broker_orders, (list, tuple)):
                    raise _ResumeBlocked("reconcile_orders_malformed")
                if positions:
                    raise _ResumeBlocked("residual_positions")
                for order in broker_orders:
                    status = _normalized_status(order)
                    if not status:
                        raise _ResumeBlocked("broker_order_status_missing")
                    if status not in _TERMINAL_ORDER_STATUSES:
                        raise _ResumeBlocked("nonterminal_broker_orders")

                runtime = state.load_state()
                self._resume_pause_invariants(runtime, check_orders=True)
                fingerprint = str(runtime.get("account_fingerprint") or "").strip()
                if not fingerprint:
                    raise _ResumeBlocked("account_identity_missing")

                # Reconcile owns durable lifecycle updates; this final broker
                # poll is deliberately read-only and catches an external fill
                # or newly working order that settled after reconciliation.
                try:
                    confirm_positions = self.provider.positions()
                    confirm_orders = self.provider.orders()
                except Exception as exc:  # noqa: BLE001
                    raise _ResumeBlocked(
                        "flat_confirmation_failed", str(exc)) from exc
                if not isinstance(confirm_positions, (list, tuple)):
                    raise _ResumeBlocked("flat_confirmation_positions_malformed")
                if not isinstance(confirm_orders, (list, tuple)):
                    raise _ResumeBlocked("flat_confirmation_orders_malformed")
                if confirm_positions:
                    raise _ResumeBlocked("residual_positions")
                for order in confirm_orders:
                    status = _normalized_status(order)
                    if not status:
                        raise _ResumeBlocked("broker_order_status_missing")
                    if status not in _TERMINAL_ORDER_STATUSES:
                        raise _ResumeBlocked("nonterminal_broker_orders")
                counts = {
                    "positions": len(confirm_positions),
                    "broker_orders": len(confirm_orders),
                    "local_orders": len(runtime.get("orders", {})),
                    "active_trades": len(runtime.get("active_trades", {})),
                    "protection": len(runtime.get("protection", {})),
                }
                audit = {
                    "phase": "authorization",
                    "mode": self.mode,
                    "run_id": self.run_id,
                    "account_fingerprint": fingerprint,
                    "counts": counts,
                    **counts,
                }
                try:
                    state.log_event(
                        "operator_resume_authorized",
                        json.dumps(audit, sort_keys=True, default=str),
                        runtime_mode=self.mode,
                        run_id=self.run_id,
                        account_fingerprint=fingerprint,
                        phase="authorization",
                        **counts,
                    )
                except Exception as exc:  # noqa: BLE001
                    raise _ResumeBlocked(
                        "authorization_audit_failed", str(exc)) from exc
                try:
                    state.write_heartbeat(
                        "paused", run_id=self.run_id,
                        reason="operator_resume_ready",
                        runtime_mode=self.mode,
                        account_fingerprint=fingerprint,
                        **counts,
                    )
                except Exception as exc:  # noqa: BLE001
                    raise _ResumeBlocked(
                        "authorization_heartbeat_failed", str(exc)) from exc

                def clear_pause(current: dict) -> dict:
                    self._resume_pause_invariants(current, check_orders=True)
                    if current.get("account_fingerprint") != fingerprint:
                        raise _ResumeBlocked("account_identity_changed")
                    current["operator_pause"] = False
                    return current

                try:
                    updated = state.update_state(clear_pause)
                except _ResumeBlocked:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise _ResumeBlocked(
                        "authorization_state_write_failed", str(exc)) from exc
                self._runtime_state = updated
                self._heartbeat_owner = False
                return {"action": "resume", "resumed": True,
                        "state": state.PAUSED, "operator_pause": False,
                        "mode": self.mode, "run_id": self.run_id,
                        "account_fingerprint": fingerprint, **counts}
            except _ResumeBlocked as exc:
                self._record_resume_block(exc.reason, exc.detail)
                raise
            except Exception as exc:  # noqa: BLE001
                reason = {
                    "journal": "journal_unavailable",
                    "preflight": "preflight_failed",
                    "reconcile": "reconcile_failed",
                }.get(phase, "resume_failed")
                self._record_resume_block(reason, str(exc))
                raise
        finally:
            self._release_lock()

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
        owns_runtime = bool(self.running or self._persistent_lock or
                            getattr(self, "_heartbeat_owner", False))
        self.running = False
        try:
            if owns_runtime:
                # Persist the lifecycle transition before announcing a
                # stopped heartbeat. Terminal states are intentionally
                # absorbing: close must not overwrite an operator kill or
                # daily risk stop, and it must preserve request_shutdown's
                # operator_pause flag.
                try:
                    state.commit({}, transition=(state.RUNNING, state.PAUSED))
                except Exception:
                    pass
                try:
                    if getattr(self, "_observability_degraded", False):
                        state.write_heartbeat(
                            "degraded", run_id=self.run_id,
                            reason="config_audit_unavailable")
                    else:
                        state.write_heartbeat("stopped", run_id=self.run_id,
                                              reason=self.shutdown_reason or "closed")
                except Exception:
                    pass
        finally:
            # State/heartbeat persistence is best effort, but a held process
            # lock is never allowed to survive close().
            self._heartbeat_owner = False
            self._release_lock()

    def _record_config_version(self) -> None:
        """Stamp the configuration this run is operating under.

        The id is content-addressed, so restarting under unchanged settings
        records nothing new and a single changed value produces a new version
        with a diff naming the field.  That is what makes "which settings were
        in force, and who changed what" answerable after the fact.  Secrets are
        redacted before hashing, so the trail records that a key changed and
        never what it changed to.
        """
        try:
            from .governance import record_config_version

            result = record_config_version(
                state.JOURNAL_FILE, self.cfg,
                source=str(os.getenv("ALPACA_AGENT_CONFIG") or "config.yaml"))
            self._observability_degraded = False
            try:
                state.clear_observability_degraded()
            except Exception:  # noqa: BLE001
                pass
            self.config_version_id = str(result.get("config_version_id") or "")
            if result.get("recorded"):
                self._journal_event("config_version_recorded", {
                    "config_version_id": self.config_version_id,
                    "previous_version_id": result.get("previous_version_id"),
                    "changed_paths": [item["path"] for item in result.get("diff", [])],
                })
        except Exception as exc:  # noqa: BLE001
            # The audit trail is a record of work that is happening anyway.
            # Failing to write it must never stop a runtime from starting.
            self._observability_degraded = True
            try:
                state.mark_observability_degraded("config_audit_unavailable")
            except Exception:  # noqa: BLE001
                pass
            detail = f"{type(exc).__name__}: {exc}"
            # Configuration provenance is part of runtime observability.  It
            # is intentionally non-blocking for trading, but a watchdog must
            # not see a healthy heartbeat after the audit write failed.
            try:
                state.write_heartbeat(
                    "degraded", run_id=self.run_id,
                    reason="config_audit_unavailable", error=detail)
            except Exception as heartbeat_exc:  # noqa: BLE001
                log.warning("config audit heartbeat unavailable: %s",
                            heartbeat_exc)
            print(f"config audit unavailable: {detail}")

    def _journal_event(self, kind: str, payload: Mapping) -> None:
        try:
            state.log_event(kind, json.dumps(dict(payload), sort_keys=True,
                                             default=str),
                            run_id=self.run_id, runtime_mode=self.mode)
        except Exception:  # noqa: BLE001
            pass


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
            self.running = False
            self.close()
            raise
        self._record_config_version()
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
                    time.sleep(_aligned_cycle_delay(time.time(), interval))
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
