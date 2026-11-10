"""Recovery of broker stops after a failed flatten close.

Flattening deliberately removes bracket legs before submitting a close.  When
the close is rejected, the position remains exposed and needs a narrow,
standalone equity stop while the operator pause stays in force.  This module
only coordinates already-collected broker snapshots and the provider's
one-shot recovery boundary; it does not own flattening or lifecycle state.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from . import state
from .alpaca_domain import parse_occ_symbol
from .instruments import validate_equity_symbol


_TERMINAL = frozenset({
    "filled", "canceled", "cancelled", "expired", "rejected", "done",
    "closed", "done_for_day", "replaced", "stopped", "suspended", "failed",
    "not_found",
})
_KNOWN_RETRYABLE_CLOSE_TERMINAL = frozenset({
    "canceled", "cancelled", "expired", "rejected", "failed", "not_found",
})
_KNOWN_PROTECTIVE_LIVE = frozenset({"accepted", "new", "held"})
_OPTION_PROFILES = frozenset({"option", "options"})


def _value(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(field, default)
    return getattr(value, field, default)


def _status(value: Any) -> str:
    raw = _value(value, "status", "")
    raw = getattr(raw, "value", raw)
    return str(raw or "").split(".")[-1].strip().lower()


def _rows(snapshot: Any, name: str) -> tuple[list[Any] | None, str | None]:
    """Unwrap a known snapshot without treating an absent one as empty."""
    if snapshot is None:
        return None, f"{name}_snapshot_unknown"
    if isinstance(snapshot, Mapping):
        if snapshot.get("known") is False:
            return None, f"{name}_snapshot_unknown"
        if name in snapshot:
            snapshot = snapshot[name]
        elif "items" in snapshot:
            snapshot = snapshot["items"]
    if snapshot is None:
        return None, f"{name}_snapshot_unknown"
    if isinstance(snapshot, (str, bytes, Mapping)):
        return None, f"{name}_snapshot_malformed"
    try:
        return list(snapshot), None
    except TypeError:
        return None, f"{name}_snapshot_malformed"


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    return number if number.is_finite() else None


def _positive(value: Any) -> Decimal | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _stop_price(order: Any) -> Decimal | None:
    raw = _value(order, "raw", None)
    value = _value(raw, "stop_price", None) if raw is not None else None
    if value is None:
        value = _value(order, "stop_price", None)
    return _positive(value)


def _order_qty(order: Any) -> Decimal | None:
    return _positive(_value(order, "qty", None))


def _live(order: Any) -> bool:
    return _status(order) not in _TERMINAL


def _side_for_position(position: Any) -> str | None:
    side = str(_value(position, "side", "") or "").lower()
    return {"long": "sell", "short": "buy"}.get(side)


def _order_is_exit(order: Any, symbol: str, exit_side: str) -> bool:
    if str(_value(order, "symbol", "")).upper() != symbol or not _live(order):
        return False
    side = str(_value(order, "side", "") or "").lower()
    action = str(_value(order, "action", "") or "").lower()
    return side == exit_side or action in {"close", "flatten"}


def _has_live_exit_leg(order: Any, symbol: str, exit_side: str) -> bool:
    if (str(_value(order, "symbol", "")).upper() != symbol or
            not _live(order)):
        return False
    for leg in _leg_rows(order):
        if (_status(leg) not in _TERMINAL and
                str(_value(leg, "side", "") or "").lower() == exit_side):
            return True
    return False


def _leg_rows(order: Any) -> list[Any]:
    legs = _value(order, "legs", ()) or ()
    if isinstance(legs, Mapping):
        return [legs]
    return [leg for leg in legs if isinstance(leg, Mapping)]


def _matching_stop(order: Any, *, symbol: str, qty: Decimal,
                   stop_price: Decimal, exit_side: str) -> dict[str, Any] | None:
    """Return a live matching stop row, including a bracket child leg."""
    if (str(_value(order, "symbol", "")).upper() != symbol or
            not _live(order)):
        return None
    if _status(order) not in _KNOWN_PROTECTIVE_LIVE:
        return None
    order_type = str(_value(order, "type", "") or "").lower()
    if (str(_value(order, "side", "") or "").lower() == exit_side and
            order_type in {"stop", "stop_limit"}):
        if (_order_qty(order) == qty and _stop_price(order) == stop_price and
                str(_value(order, "time_in_force", "day") or "").lower() == "day"):
            return {
                "order_id": str(_value(order, "id", "") or ""),
                "qty": qty, "price": stop_price,
                "side": exit_side, "nested": False,
            }
    for leg in _leg_rows(order):
        role = str(_value(leg, "role", "") or "").lower()
        leg_type = str(_value(leg, "type", "") or "").lower()
        leg_side = str(_value(leg, "side", "") or "").lower()
        leg_status = _status(leg)
        leg_qty = _positive(_value(leg, "qty", None))
        leg_price = _positive(_value(leg, "stop_price", None))
        if (leg_side == exit_side and leg_status in _KNOWN_PROTECTIVE_LIVE and
                (role == "stop" or leg_type in {"stop", "stop_limit"}) and
                leg_qty == qty and leg_price == stop_price):
            return {
                "order_id": str(_value(leg, "id", "") or
                                 _value(order, "id", "") or ""),
                "qty": qty, "price": stop_price,
                "side": exit_side, "nested": True,
            }
    return None


def _stop_geometry(trade: Mapping[str, Any], position: Any,
                   stop_price: Decimal, side: str, *, active: bool) -> bool:
    """Reject a stop that is malformed or on the wrong side of the entry."""
    entry = _positive(trade.get("entry_price"))
    current = _positive(_value(position, "current_price", None))
    if active:
        # Active stops can trail through the entry price.  When a current
        # broker mark exists it must still be on the protective side of that
        # mark; with no mark, the durable active value is the only trusted
        # geometry available and is retained for the provider's fresh check.
        if current is not None:
            if side == "long" and stop_price >= current:
                return False
            if side == "short" and stop_price <= current:
                return False
        return True
    if entry is not None:
        if side == "long" and stop_price > entry:
            return False
        if side == "short" and stop_price < entry:
            return False
    if current is not None:
        if side == "long" and stop_price >= current:
            return False
        if side == "short" and stop_price <= current:
            return False
    return True


def _lifecycle_token(trade: Mapping[str, Any], symbol: str,
                     runtime: Mapping[str, Any]) -> tuple[str | None, int]:
    lifecycle = trade.get("lifecycle_id") or trade.get("order_id")
    if lifecycle:
        lifecycle = str(lifecycle)
        if not lifecycle.startswith("entry:") and trade.get("order_id") and lifecycle == str(trade.get("order_id")):
            lifecycle = f"entry:{lifecycle}"
    if not lifecycle:
        opened = trade.get("opened_at")
        if opened not in (None, ""):
            lifecycle = f"opened:{opened}"
    if not lifecycle:
        return None, 0
    orders = runtime.get("orders", {}) if isinstance(runtime, Mapping) else {}
    attempts: list[int] = []
    if isinstance(orders, Mapping):
        for row in orders.values():
            if not isinstance(row, Mapping):
                continue
            if (str(row.get("symbol", "")).upper() == symbol and
                    str(row.get("action", "")).lower() in {"close", "flatten"} and
                    str(row.get("lifecycle_id", lifecycle)) == lifecycle):
                try:
                    attempts.append(int(row.get("attempt", 0) or 0))
                except (TypeError, ValueError):
                    continue
    try:
        attempts.append(int(trade.get("closing_attempt", 0) or 0))
    except (TypeError, ValueError):
        pass
    return lifecycle, max(attempts or [0])


def _durable_close_unresolved(runtime: Mapping[str, Any], symbol: str,
                              lifecycle: str, trade: Mapping[str, Any]) -> bool:
    """Fail closed on every durable close intent for this lifecycle.

    A flatten intent is written before protective legs are cancelled and before
    the broker can return an order id.  Therefore ``active_trades`` may not yet
    carry ``closing_order_id``; the durable orders map is the authoritative
    reservation until a known retryable terminal response replaces/removes the
    intent.  Unknown statuses remain unresolved rather than being guessed safe.
    """
    durable = runtime.get("orders", {}) if isinstance(runtime, Mapping) else {}
    if not isinstance(durable, Mapping):
        return True
    matched = False
    for key, row in durable.items():
        if not isinstance(row, Mapping):
            continue
        row_symbol = str(row.get("symbol", "") or "").upper()
        if row_symbol != symbol:
            continue
        action = str(row.get("action", "") or "").lower()
        if action not in {"close", "flatten"}:
            continue
        row_lifecycle = str(row.get("lifecycle_id", "") or "")
        # Missing lifecycle metadata is ambiguous for a same-symbol close and
        # must remain a reservation; explicit different lifecycles are safe to
        # ignore because the active trade binding is the identity boundary.
        if row_lifecycle and row_lifecycle != lifecycle:
            continue
        matched = True
        status = _status(row)
        if status not in _KNOWN_RETRYABLE_CLOSE_TERMINAL:
            return True
    closing_id = trade.get("closing_order_id")
    if closing_id:
        row = durable.get(str(closing_id))
        if not isinstance(row, Mapping):
            return True
        status = _status(row)
        if status not in _KNOWN_RETRYABLE_CLOSE_TERMINAL:
            return True
        matched = True
    # A closing active trade with no durable row is itself an unresolved state
    # transition.  This catches a crash between the state mutations.
    if not matched and str(trade.get("status", "") or "").lower() == "closing":
        return True
    return False


def _diagnose(engine: Any, kind: str, payload: Mapping[str, Any]) -> None:
    event = getattr(engine, "_event", None)
    if callable(event):
        try:
            event(kind, payload)
        except Exception:  # noqa: BLE001
            pass


def _error(bucket: list[dict[str, Any]], symbol: str,
           reason: str, **detail: Any) -> None:
    bucket.append({"symbol": symbol, "reason": reason, **detail})


def _durably_attach_stop(symbol: str, leg: Mapping[str, Any]) -> None:
    """Attach one confirmed stop leg to both monitor-facing state buckets."""
    def attach(current: dict) -> dict:
        current_active = current.setdefault("active_trades", {})
        row = current_active.get(symbol)
        if not isinstance(row, dict):
            raise ValueError("active trade disappeared before stop durability commit")
        expected_lifecycle = str(leg.get("lifecycle_id") or "")
        current_lifecycle = str(row.get("lifecycle_id") or "")
        if not current_lifecycle and row.get("order_id"):
            current_lifecycle = f"entry:{row.get('order_id')}"
        if expected_lifecycle and current_lifecycle != expected_lifecycle:
            raise ValueError("active trade lifecycle changed before stop durability commit")
        order_id = str(leg.get("order_id") or "")
        client_order_id = str(leg.get("client_order_id") or "")
        rows = [dict(item) for item in row.get("protective_legs", [])
                if isinstance(item, Mapping)]
        rows = [item for item in rows
                if str(item.get("order_id")) != order_id and
                str(item.get("client_order_id")) != client_order_id]
        rows.append(dict(leg))
        row["protective_legs"] = rows
        protection = current.setdefault("protection", {})
        protection_row = protection.get(symbol)
        if not isinstance(protection_row, dict):
            protection_row = {
                key: row.get(key) for key in (
                    "underlying_symbol", "stop_price", "target_price",
                    "active_stop_price", "initial_stop_price")
                if row.get(key) is not None
            }
        protection_rows = [dict(item) for item in protection_row.get(
            "protective_legs", []) if isinstance(item, Mapping)]
        protection_rows = [item for item in protection_rows
                           if str(item.get("order_id")) != order_id and
                           str(item.get("client_order_id")) != client_order_id]
        protection_rows.append(dict(leg))
        protection_row["protective_legs"] = protection_rows
        protection[symbol] = protection_row
        return current
    state.update_state(attach)


def restore_flatten_protection(engine: Any, residual_positions: Any,
                               residual_orders: Any) -> dict[str, list[dict[str, Any]]]:
    """Restore safe equity stops from a final, known flatten snapshot.

    The helper makes no provider reads of its own.  The provider method does a
    second fresh position/order check immediately before its one mutation,
    which keeps this coordinator safe when the supplied final snapshot races
    the broker.
    """
    evidence: dict[str, list[dict[str, Any]]] = {
        "restored": [], "errors": [], "skipped": []}
    positions, positions_error = _rows(residual_positions, "positions")
    orders, orders_error = _rows(residual_orders, "orders")
    if positions_error:
        _error(evidence["errors"], "*", positions_error, residual_risk=True)
    if orders_error:
        _error(evidence["errors"], "*", orders_error, residual_risk=True)
    if positions is None or orders is None:
        _diagnose(engine, "flatten_protection_unavailable", evidence)
        return evidence

    try:
        runtime = state.load_state()
    except Exception as exc:  # noqa: BLE001
        _error(evidence["errors"], "*", "durable_runtime_unavailable",
               residual_risk=True, error=str(exc))
        _diagnose(engine, "flatten_protection_unavailable", evidence)
        return evidence
    if not isinstance(runtime, Mapping):
        _error(evidence["errors"], "*", "durable_runtime_malformed",
               residual_risk=True)
        return evidence
    active = runtime.get("active_trades")
    if not isinstance(active, Mapping):
        _error(evidence["errors"], "*", "active_trades_unavailable",
               residual_risk=True)
        return evidence
    provider = getattr(engine, "provider", None)
    restore = getattr(provider, "restore_equity_stop", None)
    if not callable(restore):
        _error(evidence["errors"], "*", "provider_restore_equity_stop_unavailable",
               residual_risk=True)
        return evidence

    for position in positions:
        symbol = str(_value(position, "symbol", "") or "").upper()
        qty = _positive(_value(position, "qty", None))
        if not symbol or qty is None:
            _error(evidence["errors"], symbol or "*", "residual_position_geometry_invalid",
                   residual_risk=True)
            continue
        trade = active.get(symbol)
        if not isinstance(trade, Mapping) or not trade:
            _error(evidence["skipped"], symbol, "trusted_active_trade_missing")
            continue
        lifecycle, flatten_attempt = _lifecycle_token(trade, symbol, runtime)
        if lifecycle is None:
            _error(evidence["skipped"], symbol, "entry_lifecycle_unavailable")
            continue
        if _durable_close_unresolved(runtime, symbol, lifecycle, trade):
            _error(evidence["skipped"], symbol, "working_or_uncertain_close")
            continue
        profile = str(trade.get("execution_profile", "") or "").lower()
        asset_class = str(_value(position, "asset_class", "") or "").lower()
        if profile in _OPTION_PROFILES or asset_class in {"us_option", "option", "options"} or parse_occ_symbol(symbol):
            _error(evidence["skipped"], symbol, "options_use_software_stop")
            continue
        try:
            symbol = validate_equity_symbol(symbol)
        except ValueError as exc:
            _error(evidence["skipped"], symbol, "non_equity_residual", error=str(exc))
            continue
        side = str(_value(position, "side", "") or "").lower()
        exit_side = _side_for_position(position)
        if exit_side is None:
            _error(evidence["errors"], symbol, "residual_position_side_invalid",
                   residual_risk=True)
            continue
        raw_stop = trade.get("active_stop_price")
        active_stop = raw_stop not in (None, "")
        if raw_stop in (None, ""):
            raw_stop = trade.get("stop_price")
        stop_price = _positive(raw_stop)
        if (stop_price is None or
                not _stop_geometry(trade, position, stop_price, side,
                                   active=active_stop)):
            _error(evidence["skipped"], symbol, "stop_geometry_unavailable_or_invalid")
            continue

        existing_stop = None
        conflicting_exit = None
        for order in orders:
            match = _matching_stop(order, symbol=symbol, qty=qty,
                                   stop_price=stop_price, exit_side=exit_side)
            if match is not None:
                existing_stop = match
                continue
            if (_order_is_exit(order, symbol, exit_side) or
                    _has_live_exit_leg(order, symbol, exit_side)):
                conflicting_exit = order
                break
        if conflicting_exit is not None:
            _error(evidence["skipped"], symbol, "working_or_conflicting_exit")
            continue

        # A stop may have reached the broker before a process died while
        # writing local state.  The supplied row is only a candidate: route
        # it through the provider's fresh exact-CID reconciliation so a stale
        # accepted snapshot cannot be attached as current protection.
        if existing_stop is not None:
            # Bracket children do not carry an independently queryable CID.
            # Treat their parent as a working exit rather than guessing that
            # the child remains protected after flatten cancellation.
            if existing_stop.get("nested"):
                _error(evidence["skipped"], symbol,
                       "working_or_conflicting_exit")
                continue
            existing_order = next((order for order in orders
                                   if str(_value(order, "id", "")) ==
                                   existing_stop.get("order_id")), None)
            existing_cid = str(_value(existing_order, "client_order_id", "") or "")
            if not existing_cid:
                _error(evidence["errors"], symbol,
                       "existing_stop_client_id_unavailable",
                       residual_risk=True)
                continue
            client_order_id = existing_cid
            reassociated = True
        else:
            setup_id = f"{lifecycle}:flatten:{flatten_attempt}"
            try:
                client_order_id = engine._client_id(
                    "recoverstop", {"symbol": symbol, "setup_id": setup_id}, attempt=0)
            except Exception as exc:  # noqa: BLE001
                _error(evidence["errors"], symbol, "recovery_client_id_failed",
                       residual_risk=True, error=str(exc))
                continue
            reassociated = False
        try:
            result = restore(symbol, qty=qty, stop_price=stop_price,
                             client_order_id=client_order_id)
        except Exception as exc:  # noqa: BLE001
            _error(evidence["errors"], symbol, "restore_submit_failed",
                   client_order_id=client_order_id, residual_risk=True,
                   error=str(exc))
            continue
        order_id = str(_value(result, "id", "") or client_order_id)
        result_cid = str(_value(result, "client_order_id", "") or "")
        result_status = _status(result)
        result_type = str(_value(result, "type", "") or "").lower()
        result_symbol = str(_value(result, "symbol", "") or "").upper()
        result_qty = _positive(_value(result, "qty", None))
        result_side = str(_value(result, "side", "") or "").lower()
        result_price = _stop_price(result)
        result_tif = str(_value(result, "time_in_force", "") or "").lower()
        result_filled_qty = _number(_value(result, "filled_qty", 0))
        result_filled_avg = _number(_value(result, "filled_avg_price", None))
        if (not result_status or not result_cid or result_symbol != symbol or
                result_qty != qty or result_side != exit_side or
                result_type not in {"stop", "stop_limit"} or
                result_price != stop_price or result_tif != "day"):
            _error(evidence["errors"], symbol, "restore_response_unverified",
                   client_order_id=client_order_id, residual_risk=True)
            continue
        if result_status == "partially_filled":
            if (result_filled_qty is None or result_filled_qty <= 0 or
                    result_filled_qty > qty):
                _error(evidence["errors"], symbol, "restore_response_unverified",
                       client_order_id=client_order_id, residual_risk=True)
                continue
            # A partial stop is broker evidence, but it is not full requested
            # protection.  Persist the actual fill so reconciliation can
            # account for the reduced position and any remaining stop leg.
            partial_leg = {
                "order_id": order_id, "client_order_id": result_cid,
                "role": "stop", "status": result_status,
                "price": float(stop_price), "qty": float(qty),
                "filled_qty": float(result_filled_qty), "side": exit_side,
                "restored_after_flatten": True, "lifecycle_id": lifecycle,
                "flatten_attempt": flatten_attempt,
            }
            if result_filled_avg is not None and result_filled_avg > 0:
                partial_leg["filled_avg_price"] = float(result_filled_avg)
            try:
                _durably_attach_stop(symbol, partial_leg)
            except Exception as exc:  # noqa: BLE001
                _error(evidence["errors"], symbol, "stop_durability_failed",
                       order_id=order_id, client_order_id=client_order_id,
                       residual_risk=True, error=str(exc))
            else:
                _error(evidence["errors"], symbol,
                       "restore_stop_partially_filled",
                       order_id=order_id, client_order_id=client_order_id,
                       filled_qty=float(result_filled_qty), residual_risk=True)
            continue
        if result_status in _TERMINAL:
            if (result_status != "filled" or result_filled_qty is None or
                    result_filled_qty <= 0 or result_filled_qty > qty or
                    result_filled_avg is None or result_filled_avg <= 0):
                _error(evidence["errors"], symbol, "restore_response_unverified",
                       client_order_id=client_order_id, residual_risk=True)
                continue
            # A stop that filled during the submit/response race is broker
            # evidence, not current protection. Persist it for reconciliation
            # while keeping the flatten incomplete/degraded outcome visible.
            filled_leg = {
                "order_id": order_id, "client_order_id": result_cid,
                "role": "stop", "status": result_status,
                "price": float(stop_price), "qty": float(qty),
                "filled_qty": float(result_filled_qty), "side": exit_side,
                "restored_after_flatten": True, "lifecycle_id": lifecycle,
            }
            try:
                _durably_attach_stop(symbol, filled_leg)
            except Exception as exc:  # noqa: BLE001
                _error(evidence["errors"], symbol, "stop_durability_failed",
                       order_id=order_id, client_order_id=client_order_id,
                       residual_risk=True, error=str(exc))
            else:
                _error(evidence["errors"], symbol, "restore_stop_filled_during_submit",
                       order_id=order_id, client_order_id=client_order_id,
                       filled_qty=float(result_filled_qty), residual_risk=True)
            continue
        if result_status not in _KNOWN_PROTECTIVE_LIVE:
            _error(evidence["errors"], symbol, "restore_response_unverified",
                   client_order_id=client_order_id, residual_risk=True)
            continue
        leg = {
            "order_id": order_id, "client_order_id": result_cid,
            "role": "stop", "status": result_status or "accepted",
            "price": float(stop_price), "qty": float(qty),
            "side": exit_side, "restored_after_flatten": True,
            "lifecycle_id": lifecycle, "flatten_attempt": flatten_attempt,
        }
        if reassociated:
            leg["reassociated"] = True

        try:
            _durably_attach_stop(symbol, leg)
        except Exception as exc:  # noqa: BLE001
            _error(evidence["errors"], symbol, "stop_durability_failed",
                   order_id=order_id, client_order_id=client_order_id,
                   residual_risk=True, error=str(exc))
            continue
        item = {**leg, "stop_price": float(stop_price)}
        evidence["restored"].append(item)
        _diagnose(engine, "flatten_protection_reassociated" if reassociated
                  else "flatten_protection_restored", item)
    return evidence


__all__ = ["restore_flatten_protection"]
