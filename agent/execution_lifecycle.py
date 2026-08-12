"""Durable order/trade execution lifecycle operations."""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping

from . import state
from .alpaca_domain import OrderRequest
from .alpaca_provider import AlpacaError
from .instruments import validate_instrument

_FILLED_ORDER_STATUSES = {"filled", "partially_filled"}
_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "replaced", "stopped",
    "suspended", "failed", "not_found",
}


_EDGE_OUTBOX_WARN = 500


def _outbox_entry_id(outcome: Mapping) -> str:
    """Identify a learning event by the opportunity the ledger keys on."""
    return (f"{outcome.get('variant_id')}\0{outcome.get('vehicle')}"
            f"\0{outcome.get('opportunity_id')}")


def _plain(value):
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return _plain(value.value)
    return value


def _hold_expired(trade: Any, now: datetime) -> bool:
    """Decide the bounded-hold time exit from durable state alone.

    A trade persisted before this field existed, or an IBR trade, has no
    deadline and keeps its historical stop/target/close-only behavior.  A
    present but unusable deadline is treated as expired.
    """
    if not isinstance(trade, Mapping) or "hold_deadline_ts" not in trade:
        return False
    raw = trade.get("hold_deadline_ts")
    if raw is None:
        return False
    if isinstance(raw, bool):
        return True
    try:
        deadline = float(raw)
    except (TypeError, ValueError, OverflowError):
        return True
    if deadline != deadline or abs(deadline) == float("inf"):
        return True
    return now.timestamp() >= deadline


def _protective_legs(legs: Any) -> list[dict]:
    """Reduce normalized broker legs to the durable protection record."""
    rows = []
    for leg in legs or ():
        if not isinstance(leg, Mapping):
            continue
        leg_id = str(leg.get("id") or leg.get("order_id") or "").strip()
        if not leg_id:
            continue
        price = leg.get("stop_price") if leg.get("role") == "stop" else leg.get("limit_price")
        rows.append({"order_id": leg_id,
                     "role": str(leg.get("role") or "target"),
                     "status": str(leg.get("status") or "").lower(),
                     "price": float(price) if price is not None else None})
    return rows


def _leg_rows(trade: Any) -> list[dict]:
    if not isinstance(trade, Mapping):
        return []
    return [leg for leg in (trade.get("protective_legs") or [])
            if isinstance(leg, dict) and leg.get("order_id")]


def _leg_live(leg: Mapping) -> bool:
    return str(leg.get("status", "")).lower() not in _TERMINAL_ORDER_STATUSES


def _broker_protected(legs: list[dict]) -> bool:
    """Only a complete live pair is protection; a half-bracket is not."""
    return {"stop", "target"} <= {str(leg.get("role")) for leg in legs if _leg_live(leg)}


def _option_trade(trade: Any) -> bool:
    """An option position can never carry a broker-resident stop leg.

    Alpaca supports market and limit day orders on options only: no bracket,
    no OCO/OTO, and no stop or stop-limit at all.  A resting take-profit is
    therefore the whole of the broker-side protection, and its absence is not
    the lost-protection condition that a half-dead equity bracket is.
    """
    return (isinstance(trade, Mapping) and
            str(trade.get("execution_profile", "")).lower() == "options")


def _value(obj: Any, name: str, default=None):
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


class ExecutionLifecycleMixin:
    def _client_order_pending(self, client_order_id: str | None) -> bool:
        if not client_order_id:
            return False
        orders = state.load_state().get("orders", {})
        if not isinstance(orders, Mapping):
            return False
        return any(isinstance(item, Mapping) and
                   item.get("client_order_id") == client_order_id and
                   str(item.get("status", "")).lower() not in _TERMINAL_ORDER_STATUSES
                   for item in orders.values())

    def _record_open_order(self, request: OrderRequest, order: Any, risk_plan: Mapping) -> None:
        """Persist submission metadata; only a reported fill becomes a trade."""
        symbol = request.symbol.upper()
        order_id = str(getattr(order, "id", None) or request.client_order_id)
        status = str(getattr(order, "status", "submitted") or "submitted").lower()
        filled_qty = self._number(getattr(order, "filled_qty", None)) or 0.0
        if status == "filled" and filled_qty <= 0:
            filled_qty = float(request.qty)
        fill_price = self._number(getattr(order, "filled_avg_price", None))
        order_state = {
            "order_id": order_id, "symbol": symbol, "status": status,
            "client_order_id": request.client_order_id, "qty": str(request.qty),
            "filled_qty": filled_qty, "filled_avg_price": fill_price,
            "side": request.side, "type": request.type,
            "time_in_force": request.time_in_force,
            "position_intent": request.position_intent,
            "order_class": request.order_class,
            "protective_legs": _protective_legs(getattr(order, "legs", ())),
            "risk_plan": _plain(risk_plan), "fill_logged": False,
            "logged_filled_qty": 0.0,
            "updated_ts": time.time(),
        }
        def update(current: dict) -> dict:
            current.setdefault("orders", {})[order_id] = order_state
            if filled_qty > 0 and status in _FILLED_ORDER_STATUSES:
                self._activate_filled_trade(
                    current, order_state, filled_qty, fill_price)
            return current
        current = state.update_state(update)
        state.log_order(order, request, action="submit", run_id=self.run_id,
                        runtime_mode=self.mode, account_fingerprint=current.get("account_fingerprint"),
                        setup_id=risk_plan.get("setup_id"))
        self._event("order_submitted", {"symbol": request.symbol, "qty": str(request.qty),
                                         "status": status, "filled_qty": filled_qty,
                                         "client_order_id": request.client_order_id,
                                         "risk": risk_plan})
        # The option profile's take-profit can only rest after its entry
        # fills, so it is submitted here, outside the transaction above.
        self._sync_option_take_profit()

    def _activate_filled_trade(self, current: dict, order_state: dict,
                               filled_qty: float, fill_price: float | None) -> dict:
        """Create durable protection and one open-trade row for an actual fill."""
        plan = order_state.get("risk_plan", {})
        plan = plan if isinstance(plan, Mapping) else {}
        symbol = str(order_state.get("symbol", "")).upper()
        profile = str(plan.get("execution_profile", "shares"))
        underlying = str(plan.get("underlying_symbol") or
                         (symbol if profile == "shares" else "")).upper()
        direction = str(plan.get("direction") or
                        ("long" if order_state.get("side") == "buy" else "short"))
        instrument_entry = fill_price
        if instrument_entry is None:
            instrument_entry = self._number(
                plan.get("option", {}).get("debit") if profile == "options" and
                isinstance(plan.get("option"), Mapping) else plan.get("entry_price"))
        existing = current.get("active_trades", {}).get(symbol, {})
        existing = existing if isinstance(existing, Mapping) else {}
        trade = {
            "symbol": symbol, "underlying_symbol": underlying,
            "execution_profile": profile, "direction": direction,
            "position_side": "long" if order_state.get("side") == "buy" else "short",
            "qty": str(filled_qty), "entry_price": instrument_entry,
            "underlying_entry_price": plan.get("entry_price"),
            "opened_at": existing.get("opened_at", time.time()),
            "setup_type": plan.get("setup_type", "ibr"),
            "setup_id": plan.get("setup_id"), "order_id": order_state.get("order_id"),
            "status": "open", "stop_price": plan.get("underlying_stop_price", plan.get("stop_price")),
            "target_price": plan.get("underlying_target_price", plan.get("target_price")),
            "force_flat_at": plan.get("force_flat_at"),
            "max_hold_bars": plan.get("max_hold_bars", existing.get("max_hold_bars")),
            "hold_deadline_ts": plan.get("hold_deadline_ts",
                                         existing.get("hold_deadline_ts")),
            "risk_usd": plan.get("risk_usd"),
            "notional": plan.get("notional"), "variant_id": plan.get("variant_id"),
            "strategy_id": plan.get("strategy_id", self.cfg.get("strategy", {}).get("id")),
            "strategy_version": plan.get("strategy_version", self.cfg.get("strategy", {}).get("version")),
            "contract_multiplier": plan.get("contract_multiplier", 1),
        }
        # The broker-resident bracket legs are the position's real protection.
        # Keep the ids observed at submission; a later reconciliation refreshes
        # their status but must not lose the association.
        legs = _leg_rows(order_state) or _leg_rows(existing)
        if legs:
            trade["protective_legs"] = [dict(leg) for leg in legs]
        # A newly observed fill may precede the broker position endpoint.  An
        # explicit false marker lets reconciliation retain that exposure over
        # repeated empty-position snapshots; legacy trades without this field
        # keep their historical disappearance behavior.
        if existing:
            if "position_confirmed" in existing:
                trade["position_confirmed"] = existing.get("position_confirmed")
        else:
            trade["position_confirmed"] = False
        current.setdefault("active_trades", {})[symbol] = trade
        current.setdefault("protection", {})[symbol] = {
            key: trade.get(key) for key in (
                "underlying_symbol", "stop_price", "target_price", "force_flat_at",
                "max_hold_bars", "hold_deadline_ts", "protective_legs")
        }
        logged_qty = self._number(order_state.get("logged_filled_qty")) or 0.0
        if order_state.get("fill_logged") and "logged_filled_qty" not in order_state:
            # Older state files used a boolean only; avoid duplicating their
            # already-journaled fill during a rolling upgrade.
            logged_qty = filled_qty
        incremental_qty = max(0.0, filled_qty - logged_qty)
        if incremental_qty > 0:
            state.log_trade(
                symbol, order_state.get("side"), "open", incremental_qty,
                price=instrument_entry, notional=plan.get("notional"),
                risk_usd=plan.get("risk_usd"), order_id=order_state.get("order_id"),
                setup_id=plan.get("setup_id"), setup_type=plan.get("setup_type"),
                strategy_id=trade.get("strategy_id"),
                strategy_version=trade.get("strategy_version"),
                variant_id=trade.get("variant_id"), fill_status="filled",
                runtime_mode=self.mode, account_fingerprint=current.get("account_fingerprint"),
                run_id=self.run_id)
            order_state["fill_logged"] = True
            order_state["logged_filled_qty"] = filled_qty
        return trade

    def _option_take_profit_price(self, trade: Mapping) -> float | None:
        """Price the resting sell in the option's own risk unit.

        The plan's stop and target are underlying prices and a premium is not
        a linear function of them, so they cannot be used as a limit price.  A
        long option's risk is its whole debit, so the plan's reward-to-risk
        ratio is applied to that debit: the leg fills only once the position
        has made the validated variant's target multiple of the risk it really
        took.  Rounding up to the cent keeps a resting order from ever exiting
        cheaper than that.
        """
        debit = self._number(trade.get("entry_price"))
        underlying = self._number(trade.get("underlying_entry_price"))
        stop = self._number(trade.get("stop_price"))
        target = self._number(trade.get("target_price"))
        if debit is None or underlying is None or stop is None or target is None:
            return None
        if debit <= 0:
            return None
        distance = abs(underlying - stop)
        reward = abs(target - underlying)
        if distance <= 0 or reward <= 0:
            return None
        price = Decimal(str(debit)) * (Decimal(1) + Decimal(str(reward / distance)))
        return float(price.quantize(Decimal("0.01"), rounding=ROUND_CEILING))

    def _sync_option_take_profit(self) -> None:
        """Rest a broker-side sell_to_close limit against every filled option.

        This is the only protection Alpaca lets an option position keep when
        this process dies; the stop stays with the local poller.  The order is
        submitted after the fill is already durable and never from inside a
        state transaction, because a retried update callback must not be able
        to replay a broker mutation.  The leg is stored in the same
        ``protective_legs`` structure the equity bracket uses, so cancellation,
        the poller backstop, and the filled-leg close path need no new case.
        """
        runtime = state.load_state()
        active = runtime.get("active_trades", {}) if isinstance(runtime, Mapping) else {}
        if not isinstance(active, Mapping):
            return
        for symbol, trade in list(active.items()):
            if not _option_trade(trade) or trade.get("closing_order_id"):
                continue
            if str(trade.get("status", "open")).lower() != "open":
                continue
            qty = self._number(trade.get("qty")) or 0.0
            legs = _leg_rows(trade)
            if qty <= 0 or any(str(leg.get("status", "")).lower() == "filled"
                               for leg in legs):
                continue
            targets = [leg for leg in legs if str(leg.get("role")) == "target"]
            live = [leg for leg in targets if _leg_live(leg)]
            if live and all(self._number(leg.get("qty")) == qty for leg in live):
                continue
            price = self._option_take_profit_price(trade)
            if price is None:
                self._event("option_take_profit_skipped", {
                    "symbol": symbol, "reason": "target premium is underivable"})
                continue
            # A resting leg reserves the contracts it was sized for.  An
            # amended quantity replaces it only once the old one is provably
            # cancelled, never alongside it.
            if live and not self._cancel_protective_legs(str(symbol), live):
                continue
            request = OrderRequest(
                str(symbol), Decimal(str(int(qty))), "sell", type="limit",
                time_in_force="day", limit_price=Decimal(str(price)),
                client_order_id=self._client_id(
                    "tp", {"symbol": symbol, "setup_id": trade.get("setup_id")},
                    len(targets)),
                position_intent="sell_to_close")
            try:
                order = self.provider.submit_order(request)
            except Exception as exc:  # noqa: BLE001
                # Nothing was mutated, so this is not a durability failure: the
                # poller remains the whole protection until the next attempt.
                self._event("option_take_profit_failed", {"symbol": symbol,
                                                           "error": str(exc)})
                continue
            leg = {"order_id": str(getattr(order, "id", None) or
                                   request.client_order_id),
                   "role": "target",
                   "status": str(getattr(order, "status", "accepted") or
                                 "accepted").lower(),
                   "price": float(price), "qty": qty}

            def attach(current: dict, symbol=symbol, leg=leg) -> dict:
                for bucket in ("active_trades", "protection"):
                    row = current.get(bucket, {}).get(symbol)
                    if not isinstance(row, dict):
                        continue
                    rows = [item for item in _leg_rows(row)
                            if str(item.get("order_id")) != leg["order_id"]]
                    rows.append(dict(leg))
                    row["protective_legs"] = rows
                return current

            try:
                state.update_state(attach)
            except Exception as exc:  # noqa: BLE001
                self._reconciled = False
                self._preflight_error = (
                    "post-submit protection durability failure; reconciliation required")
                try:
                    state.commit({"operator_pause": True},
                                 transition=(state.RUNNING, state.PAUSED))
                except Exception:  # noqa: BLE001
                    pass
                raise AlpacaError(f"{self._preflight_error}: {exc}") from exc
            state.log_order(order, request, action="protect", run_id=self.run_id,
                            runtime_mode=self.mode,
                            setup_id=trade.get("setup_id"))
            self._event("option_take_profit_resting", {
                "symbol": symbol, "order_id": leg["order_id"], "qty": qty,
                "limit_price": float(price)})

    def _close_position(self, position: Any, reason: str, *, attempt: int = 0) -> Any:
        symbol = str(_value(position, "symbol", "")).upper()
        qty = abs(Decimal(str(_value(position, "qty", 0))))
        if not symbol or qty <= 0:
            return None
        client_order_id = self._client_id(
            "close", {"symbol": symbol, "setup_id": reason}, attempt)
        close = getattr(self.provider, "close_position", None)
        if callable(close):
            try:
                result = close(symbol, qty=qty, client_order_id=client_order_id,
                               order_type="market", time_in_force="day")
            except TypeError:
                try:
                    result = close(symbol, qty=qty)
                except TypeError:
                    result = close(symbol)
            try:
                state.log_order(result, None, action="close", reason=reason,
                                symbol=symbol, qty=float(qty), runtime_mode=self.mode,
                                run_id=self.run_id,
                                client_order_id=client_order_id)
            except Exception as exc:  # noqa: BLE001
                self._reconciled = False
                self._preflight_error = (
                    "post-submit close durability failure; reconciliation required")
                try:
                    state.commit({"operator_pause": True},
                                 transition=(state.RUNNING, state.PAUSED))
                except Exception:  # noqa: BLE001
                    pass
                raise AlpacaError(f"{self._preflight_error}: {exc}") from exc
            return result
        side = "sell" if str(_value(position, "side", "long")).lower() in {"long", "buy"} else "buy"
        request = OrderRequest(symbol, qty, side, type="market", time_in_force="day",
                               client_order_id=client_order_id)
        result = self.provider.submit_order(request)
        try:
            state.log_order(result, request, action="close", reason=reason,
                            runtime_mode=self.mode, run_id=self.run_id)
        except Exception as exc:  # noqa: BLE001
            self._reconciled = False
            self._preflight_error = (
                "post-submit close durability failure; reconciliation required")
            try:
                state.commit({"operator_pause": True},
                             transition=(state.RUNNING, state.PAUSED))
            except Exception:  # noqa: BLE001
                pass
            raise AlpacaError(f"{self._preflight_error}: {exc}") from exc
        return result

    def _cancel_protective_legs(self, symbol: str, legs: list[dict]) -> bool:
        """Cancel resting bracket legs before any close, or refuse to close.

        A live leg reserves the position quantity at the broker, so a close
        submitted underneath it is rejected for insufficient quantity.  A
        cancel that cannot be proven leaves the close unsafe to send.
        """
        live = [leg for leg in legs if _leg_live(leg)]
        if not live:
            return True
        cancel = getattr(self.provider, "cancel_order", None)
        if not callable(cancel):
            self._event("protection_cancel_failed", {
                "symbol": symbol, "reason": "provider cannot cancel one order"})
            return False
        cancelled = []
        for leg in live:
            leg_id = str(leg.get("order_id"))
            try:
                cancel(leg_id)
            except Exception as exc:  # noqa: BLE001
                self._event("protection_cancel_failed", {
                    "symbol": symbol, "order_id": leg_id, "error": str(exc)})
                return False
            cancelled.append(leg_id)
            leg["status"] = "canceled"
        def mark(current: dict) -> dict:
            for bucket in ("active_trades", "protection"):
                row = current.get(bucket, {}).get(symbol)
                for leg in _leg_rows(row):
                    if str(leg.get("order_id")) in cancelled:
                        leg["status"] = "canceled"
            return current
        try:
            state.update_state(mark)
        except Exception as exc:  # noqa: BLE001
            self._event("protection_cancel_failed", {
                "symbol": symbol, "reason": f"cancel not durable: {exc}"})
            return False
        self._event("protection_cancelled", {"symbol": symbol,
                                              "order_ids": cancelled})
        return True

    def _protection_price(self, trade: Mapping, position: Any,
                          now: datetime) -> float | None:
        """Return the underlying price used by both stock and option exits."""
        profile = str(trade.get("execution_profile", "shares")).lower()
        if profile != "options":
            direct = self._number(_value(position, "current_price",
                                         _value(position, "price", None)))
            if direct is not None and direct > 0:
                return direct
        underlying = str(trade.get("underlying_symbol") or
                         _value(position, "symbol", "")).upper()
        if not underlying:
            return None
        try:
            rows = self.market.stock_quotes(
                [underlying], start=now - timedelta(minutes=2), end=now)
            values = rows.get(underlying, []) if isinstance(rows, Mapping) else []
            quote = self._quote_mapping(values[-1], underlying) if values else {}
        except Exception:  # noqa: BLE001
            return None
        timestamp = self._timestamp(quote.get("timestamp"))
        maximum = float(self.cfg.get("execution", {}).get(
            "max_market_data_age_seconds", 30) or 30)
        if timestamp is None or timestamp > now or (now - timestamp).total_seconds() > maximum:
            return None
        bid = self._number(quote.get("bid", quote.get("bid_price")))
        ask = self._number(quote.get("ask", quote.get("ask_price")))
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return None
        return (bid + ask) / 2.0

    def _monitor_positions(self, now: datetime, positions: list[Any]) -> dict[str, Any]:
        """Evaluate persisted protection on every cycle and close safely."""
        runtime = state.load_state()
        active = runtime.get("active_trades", {}) if isinstance(runtime, Mapping) else {}
        force_flat = self.market.should_force_flat(now)
        closed = []
        failed = []
        changed = False
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            trade = active.get(symbol, {}) if isinstance(active, Mapping) else {}
            if isinstance(trade, Mapping) and trade.get("closing_order_id"):
                saved_order = runtime.get("orders", {}).get(
                    str(trade.get("closing_order_id")), {})
                saved_status = str(saved_order.get("status", "")).lower() \
                    if isinstance(saved_order, Mapping) else ""
                # Do not send another close while the prior one is live.  A
                # rejected/cancelled/not-found/filled terminal order may be
                # retried if the broker still reports residual quantity.
                if saved_status and saved_status not in _TERMINAL_ORDER_STATUSES:
                    continue
            legs = _leg_rows(trade)
            if any(str(leg.get("status", "")).lower() == "filled" for leg in legs):
                # A filled child leg is the close itself; reconciliation books
                # it.  Sending anything here would double-close.
                continue
            protected = _broker_protected(legs)
            price = self._protection_price(trade, position, now) if isinstance(trade, Mapping) else None
            direction = str(trade.get("direction", _value(position, "side", "long"))).lower()
            stop = self._number(trade.get("stop_price"))
            target = self._number(trade.get("target_price"))
            reason = None
            if force_flat:
                reason = "before_close"
            elif not trade or stop is None or target is None:
                reason = "protection_missing"
            elif legs and not protected and not _option_trade(trade):
                # Recorded legs that are no longer live at the broker leave an
                # open position unprotected; close it fail-closed.
                self._event("unprotected_position", {"symbol": symbol,
                                                      "reason": "protective_legs_terminal"})
                reason = "protection_missing"
            elif protected:
                # The broker owns the stop and target exits.  A local price
                # crossing must not race its own resting legs.
                reason = None
            elif price is not None and direction in {"long", "buy"}:
                if stop is not None and price <= stop:
                    reason = "stop"
                elif target is not None and price >= target:
                    reason = "target"
            elif price is not None:
                if stop is not None and price >= stop:
                    reason = "stop"
                elif target is not None and price <= target:
                    reason = "target"
            if reason is None and _hold_expired(trade, now):
                # The validated contract's bounded hold is a time exit; it must
                # fire without a tradable price rather than be reported as a
                # market-data outage.
                reason = "max_hold"
            if reason is None and price is None and not protected:
                # Missing local prices are only an emergency while the local
                # poller is the protection.
                reason = "protection_data_unavailable"
            if reason:
                try:
                    if not self._cancel_protective_legs(symbol, legs):
                        failed.append({"symbol": symbol, "reason": reason,
                                       "error": "protective legs could not be cancelled"})
                        continue
                    prior_attempt = self._number(trade.get("closing_attempt")) \
                        if isinstance(trade, Mapping) else None
                    attempt = int(prior_attempt) + 1 if prior_attempt is not None else 0
                    order = self._close_position(position, reason, attempt=attempt)
                    closed.append({"symbol": symbol, "reason": reason})
                    if isinstance(trade, dict):
                        close_fill = self._number(getattr(order, "filled_avg_price", None))
                        close_order_id = str(getattr(order, "id", None) or
                                             self._client_id("close", {
                                                 "symbol": symbol, "reason": reason}))
                        trade.update({"status": "closing", "closing_reason": reason,
                                      "closing_price": close_fill,
                                      "closing_trigger_price": price,
                                      "closing_order_id": close_order_id,
                                      "closing_attempt": attempt,
                                      "updated_ts": time.time()})
                        active[symbol] = trade
                        runtime.setdefault("orders", {})[close_order_id] = {
                            "order_id": close_order_id, "symbol": symbol,
                            "status": str(getattr(order, "status", "submitted") or
                                          "submitted").lower(),
                            "client_order_id": getattr(order, "client_order_id", None),
                            "qty": str(_value(position, "qty", 0)),
                            "side": "sell" if str(_value(position, "side", "long")).lower()
                            in {"long", "buy"} else "buy",
                            "action": "close", "reason": reason,
                            "attempt": attempt, "closing_attempt": attempt,
                            "updated_ts": time.time(),
                        }
                        changed = True
                except Exception as exc:  # noqa: BLE001
                    failed.append({"symbol": symbol, "reason": reason,
                                   "error": str(exc)})
                    self._event("close_failed", {"symbol": symbol, "reason": reason,
                                                  "error": str(exc)})
        if changed:
            try:
                runtime = state.update_state(lambda current: {
                    **current,
                    "active_trades": dict(active),
                    "orders": {**current.get("orders", {}),
                               **runtime.get("orders", {})},
                })
            except Exception as exc:  # noqa: BLE001
                self._reconciled = False
                self._preflight_error = (
                    "post-submit close state failure; reconciliation required")
                try:
                    state.commit({"operator_pause": True},
                                 transition=(state.RUNNING, state.PAUSED))
                except Exception:  # noqa: BLE001
                    pass
                raise AlpacaError(f"{self._preflight_error}: {exc}") from exc
        return {"closed": closed, "failed": failed, "force_flat": force_flat}

    def reconcile(self):
        if hasattr(self.provider, "reconcile"):
            result = self.provider.reconcile()
        else:
            result = {"positions": self.provider.positions(), "orders": self.provider.orders()}
        positions = result.get("positions", []) if isinstance(result, Mapping) else []
        broker_orders = result.get("orders", []) if isinstance(result, Mapping) else []
        for position in positions:
            validate_instrument(
                _value(position, "symbol", ""),
                _value(position, "asset_class", _value(position, "class", None)))
        for broker_order in broker_orders:
            validate_instrument(
                _value(broker_order, "symbol", ""),
                _value(broker_order, "asset_class", _value(broker_order, "class", None)))
            tif = str(_value(broker_order, "time_in_force", "") or "").lower()
            if not tif:
                raise AlpacaError("broker reconciliation found an order without time_in_force")
            if tif != "day":
                raise AlpacaError("broker reconciliation found a non-day order")
        current = state.load_state()
        # Keep an immutable view of the active trades from the start of this
        # snapshot.  Order fills can activate or update a trade below; using
        # the live mapping here would make those new fills look like trades
        # that disappeared from the broker in this same reconciliation.
        previous = current.get("active_trades", {})
        previous = deepcopy(previous) if isinstance(previous, Mapping) else {}
        order_state = current.get("orders", {})
        order_state = order_state if isinstance(order_state, dict) else {}
        by_id = {str(getattr(order, "id", "")): order for order in broker_orders
                 if getattr(order, "id", None)}
        by_client = {str(getattr(order, "client_order_id", "")): order
                     for order in broker_orders if getattr(order, "client_order_id", None)}
        # Bracket child legs arrive as broker orders with no local ``orders``
        # row, so the loop below never sees them.  They are associated to
        # their parent trade only through the leg ids persisted at activation,
        # and they still pass the instrument/day-TIF assertions above because
        # the broker creates them on the same equity symbol with the parent's
        # day time-in-force.
        for symbol, trade in previous.items():
            legs = _leg_rows(trade)
            if not legs:
                continue
            for leg in legs:
                broker_leg = by_id.get(str(leg.get("order_id")))
                if broker_leg is None:
                    leg["status"] = "not_found"
                    continue
                leg["status"] = str(getattr(broker_leg, "status", "") or "").lower()
                filled_qty = self._number(getattr(broker_leg, "filled_qty", None)) or 0.0
                if leg["status"] != "filled" and filled_qty <= 0:
                    continue
                if trade.get("closing_order_id"):
                    # An exit is already booked against this trade; a leg can
                    # never add a second close for the same position.
                    continue
                leg_id = str(leg.get("order_id"))
                trade.update({
                    "status": "closing", "closing_reason": str(leg.get("role")),
                    "closing_price": self._number(
                        getattr(broker_leg, "filled_avg_price", None)),
                    "closing_order_id": leg_id, "updated_ts": time.time()})
                order_state.setdefault(leg_id, {
                    "order_id": leg_id, "symbol": symbol,
                    "status": leg["status"], "qty": str(getattr(broker_leg, "qty", "")),
                    "side": str(getattr(broker_leg, "side", "")),
                    "action": "close", "reason": str(leg.get("role")),
                    "updated_ts": time.time()})
            if (not trade.get("closing_order_id") and not _broker_protected(legs) and
                    not _option_trade(trade) and
                    str(trade.get("status", "")).lower() == "open"):
                self._event("unprotected_position", {
                    "symbol": symbol, "reason": "protective_legs_terminal"})

        for key, saved in list(order_state.items()):
            if not isinstance(saved, dict):
                continue
            broker_order = by_id.get(str(saved.get("order_id") or key)) or \
                by_client.get(str(saved.get("client_order_id") or ""))
            if broker_order is None:
                if str(saved.get("status", "")).lower() not in _TERMINAL_ORDER_STATUSES:
                    misses = int(saved.get("not_found_count", 0) or 0) + 1
                    saved.update({"not_found_count": misses,
                                  "updated_ts": time.time()})
                    # The order endpoint can lag a successful submission.
                    # Require repeated complete reconciliation misses before
                    # treating the id as terminal and eligible for retry.
                    if misses >= 3:
                        saved["status"] = "not_found"
                continue
            old_status = str(saved.get("status", "")).lower()
            broker_status = str(getattr(broker_order, "status", old_status) or old_status).lower()
            broker_filled_qty = self._number(getattr(broker_order, "filled_qty", None)) or 0.0
            saved_filled_qty = self._number(saved.get("filled_qty")) or 0.0
            # Filled quantity is durable evidence.  A lagging broker snapshot
            # may report a smaller quantity than one already observed, but it
            # must never erase that evidence or duplicate a later increment.
            filled_qty = max(saved_filled_qty, broker_filled_qty)
            fill_price = self._number(getattr(broker_order, "filled_avg_price", None))
            saved_fill_price = self._number(saved.get("filled_avg_price"))
            # Broker order snapshots and position snapshots can settle in a
            # different order.  Never regress durable fill evidence to an
            # older accepted/new view returned by a lagging order endpoint.
            status = broker_status
            if old_status in _TERMINAL_ORDER_STATUSES:
                # Terminal evidence is absorbing across terminal subtypes:
                # rejected/canceled/expired must not be rewritten as filled
                # by a later contradictory order snapshot.
                status = old_status
            elif (old_status == "partially_filled" and
                  broker_status not in _FILLED_ORDER_STATUSES and
                  broker_status not in _TERMINAL_ORDER_STATUSES):
                status = old_status
            if fill_price is None or broker_filled_qty < saved_filled_qty:
                fill_price = saved_fill_price or fill_price
            saved.update({"status": status, "filled_qty": filled_qty,
                          "filled_avg_price": fill_price, "not_found_count": 0,
                          "updated_ts": time.time()})
            logged_qty = self._number(saved.get("logged_filled_qty")) or 0.0
            if (filled_qty > 0 and saved.get("risk_plan") and
                    (not saved.get("fill_logged") or filled_qty > logged_qty)):
                # Some broker snapshots expose filled_qty before updating the
                # order status.  Durable quantity growth is enough evidence
                # to protect and journal the incremental fill.
                self._activate_filled_trade(current, saved, filled_qty, fill_price)
            if status != old_status:
                state.log_order(
                    broker_order, None, action="reconcile", run_id=self.run_id,
                    runtime_mode=self.mode, account_fingerprint=current.get("account_fingerprint"),
                    setup_id=(saved.get("risk_plan") or {}).get("setup_id")
                    if isinstance(saved.get("risk_plan"), Mapping) else None)

        pending_by_symbol: dict[str, list[dict]] = {}
        for saved in order_state.values():
            if not (isinstance(saved, dict) and saved.get("risk_plan") and
                    not saved.get("position_closed")):
                continue
            status = str(saved.get("status", "")).lower()
            filled_qty = self._number(saved.get("filled_qty")) or 0.0
            # A terminal order with no durable fill cannot explain a later
            # broker position.  Keep accepted/working orders eligible because
            # the position endpoint may settle before order status, and keep
            # terminal orders with positive fill evidence eligible for partial
            # exposure attribution.
            if status in _TERMINAL_ORDER_STATUSES and filled_qty <= 0:
                continue
            pending_by_symbol.setdefault(str(saved.get("symbol", "")).upper(), []).append(saved)
        for rows in pending_by_symbol.values():
            rows.sort(key=lambda item: float(item.get("updated_ts", 0) or 0), reverse=True)

        active = {}
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            if not symbol:
                continue
            item = dict(previous.get(symbol, {}))
            pending = next((row for row in pending_by_symbol.get(symbol, [])
                            if not row.get("position_closed")), None)
            if not item and isinstance(pending, dict):
                pending["filled_qty"] = max(
                    self._number(pending.get("filled_qty")) or 0.0,
                    self._number(_value(position, "qty", 0)) or 0.0)
                pending["filled_avg_price"] = self._number(
                    _value(position, "avg_entry_price", None))
                item = self._activate_filled_trade(
                    current, pending, pending["filled_qty"], pending["filled_avg_price"])
            if not item:
                # A broker position without local protection is reconciled as
                # unprotected; the same cycle's monitor closes it fail-closed.
                item = {"symbol": symbol, "underlying_symbol": symbol,
                        "execution_profile": "unknown", "opened_at": time.time(),
                        "status": "unprotected", "risk_usd": 0.0}
                prior_attempts = [
                    self._number(saved.get("attempt", saved.get("closing_attempt")))
                    for saved in order_state.values()
                    if isinstance(saved, Mapping) and
                    str(saved.get("symbol", "")).upper() == symbol and
                    (str(saved.get("action", "")).lower() == "close" or
                     saved.get("position_closed"))
                ]
                prior_attempts = [attempt for attempt in prior_attempts
                                  if attempt is not None]
                if prior_attempts:
                    item["closing_attempt"] = int(max(prior_attempts))
                self._event("unprotected_position", {"symbol": symbol})
            unprotected = (str(item.get("status", "")).lower() == "unprotected" or
                           str(item.get("execution_profile", "")).lower() == "unknown")
            item.update({"symbol": symbol, "qty": str(_value(position, "qty", 0)),
                         "direction": str(_value(position, "side", "long")).lower(),
                         "current_price": str(_value(position, "current_price", "")),
                         "status": ("closing" if item.get("closing_reason") else
                                    "unprotected" if unprotected else "open"),
                         "position_confirmed": True,
                         "updated_ts": time.time()})
            active[symbol] = item
        # Include fill-first trades created earlier in this same reconcile.
        # Their explicit false marker is the only safe basis for retaining an
        # exposure that the position endpoint has not confirmed yet.
        current_active = current.get("active_trades", {})
        if isinstance(current_active, Mapping):
            for symbol, fresh in current_active.items():
                if symbol in active or not isinstance(fresh, Mapping):
                    continue
                if fresh.get("position_confirmed") is False:
                    active[symbol] = dict(fresh)
        for symbol, trade in previous.items():
            if symbol in active or not isinstance(trade, Mapping):
                continue
            if trade.get("position_confirmed") is False:
                # The order endpoint has durable fill evidence, but this
                # trade has never been confirmed by a position snapshot.
                # Do not infer a close from an empty position list yet.
                fresh = current.get("active_trades", {}).get(symbol)
                if isinstance(fresh, Mapping):
                    active[symbol] = dict(fresh)
                continue
            qty = self._number(trade.get("qty")) or 0.0
            entry = self._number(trade.get("entry_price"))
            exit_price = self._number(trade.get("closing_price"))
            closing_order = by_id.get(str(trade.get("closing_order_id") or ""))
            if exit_price is None and closing_order is not None:
                exit_price = self._number(getattr(closing_order, "filled_avg_price", None))
            multiplier = self._number(trade.get("contract_multiplier")) or 1.0
            realized = None
            sign = -1.0 if str(trade.get("position_side", "long")) == "short" else 1.0
            if entry is not None and exit_price is not None:
                realized = (exit_price - entry) * qty * multiplier * sign
            pnl_pct = ((exit_price - entry) / entry * 100.0 * sign
                       if entry and exit_price is not None else None)
            state.log_trade(
                symbol, "sell" if str(trade.get("position_side", "long")) == "long" else "buy",
                "close", qty, price=exit_price, reason=trade.get("closing_reason", "broker_reconcile"),
                realized_pnl_usd=realized, pnl_pct=pnl_pct,
                close_trigger=trade.get("closing_reason", "broker_reconcile"),
                setup_id=trade.get("setup_id"), setup_type=trade.get("setup_type"),
                strategy_id=trade.get("strategy_id"),
                strategy_version=trade.get("strategy_version"),
                variant_id=trade.get("variant_id"), runtime_mode=self.mode,
                account_fingerprint=current.get("account_fingerprint"), run_id=self.run_id)
            self._record_edge_outcome(trade, realized, pnl_pct, exit_price)
            entry_order_id = str(trade.get("order_id") or "")
            entry_order = order_state.get(entry_order_id)
            if isinstance(entry_order, dict):
                closing_attempt = self._number(trade.get("closing_attempt"))
                prior_attempt = self._number(
                    entry_order.get("closing_attempt", entry_order.get("attempt")))
                attempts = [attempt for attempt in (closing_attempt, prior_attempt)
                            if attempt is not None]
                entry_order["position_closed"] = True
                if attempts:
                    entry_order["closing_attempt"] = int(max(attempts))
            current.setdefault("protection", {}).pop(symbol, None)
        reconciled_at = time.time()
        protection = current.get("protection", {})
        # The learning events for the closes booked above are written in this
        # same atomic replacement.  Either the trade is still active and its
        # close is re-derived next cycle, or the close and its outcome are both
        # durable; there is no interleaving that books one without the other.
        current = state.update_state(lambda latest: {
            **latest,
            "active_trades": active,
            "orders": {**latest.get("orders", {}), **order_state},
            "protection": protection,
            "last_reconciliation_ts": reconciled_at,
            "edge_outbox": self._queued_edge_outbox(latest),
        })
        self._runtime_state = current
        self._pending_edge_outcome_rows = []
        self._reconciled = True
        self._drain_edge_outbox()
        self._event("rest_reconcile", {"positions": len(result.get("positions", [])) if isinstance(result, Mapping) else 0})
        # After the atomic replacement above, so a broker submission can never
        # be replayed by a retried state callback and never delays the outbox.
        self._sync_option_take_profit()
        return result

    def _record_edge_outcome(self, trade: Mapping, realized: float | None,
                             pnl_pct: float | None, exit_price: float | None) -> None:
        if self.mode != "paper":
            return
        variant_id = trade.get("variant_id")
        realized_value = self._number(realized)
        if not variant_id or realized_value is None:
            return
        vehicle = "option" if str(trade.get("execution_profile")) == "options" else "equity"
        risk_usd = self._number(trade.get("risk_usd"))
        opened = self._number(trade.get("opened_at")) or time.time()
        outcome = {
            "variant_id": variant_id, "vehicle": vehicle,
            "opportunity_id": trade.get("setup_id") or
                              f"{trade.get('symbol')}:{opened:.6f}",
            "session_date": datetime.fromtimestamp(opened, timezone.utc).date().isoformat(),
            "net_pnl": realized_value, "pnl_pct": self._number(pnl_pct),
            "risk_usd": risk_usd,
            "r_multiple": (realized_value / risk_usd
                           if risk_usd and risk_usd > 0 else None),
            "entry_price": self._number(trade.get("entry_price")),
            "exit_price": self._number(exit_price),
            "reason": trade.get("closing_reason", "broker_reconcile"),
            "paper": True,
        }
        # Queue only.  The outcome becomes durable in the same atomic state
        # replacement that removes the closed trade, and is ingested by
        # ``_drain_edge_outbox`` from that durable record.
        self._pending_edge_outcomes.append(
            {"entry_id": _outbox_entry_id(outcome), "queued_ts": time.time(),
             "attempts": 0, "outcome": outcome})

    @property
    def _pending_edge_outcomes(self) -> list:
        pending = getattr(self, "_pending_edge_outcome_rows", None)
        if pending is None:
            pending = []
            self._pending_edge_outcome_rows = pending
        return pending

    def _queued_edge_outbox(self, latest: Mapping) -> list:
        """Merge this cycle's outcomes into the durable outbox exactly once."""
        existing = latest.get("edge_outbox")
        rows = [dict(row) for row in existing if isinstance(row, Mapping)] \
            if isinstance(existing, list) else []
        known = {str(row.get("entry_id")) for row in rows}
        for row in self._pending_edge_outcomes:
            if str(row.get("entry_id")) not in known:
                rows.append(dict(row))
                known.add(str(row.get("entry_id")))
        if len(rows) > _EDGE_OUTBOX_WARN:
            self._event("edge_outbox_saturated", {"queued": len(rows)})
        return rows

    def _drain_edge_outbox(self) -> int:
        """Ingest durably queued outcomes, retaining whatever is unproven.

        Ingestion is keyed on ``opportunity_id`` in the ledger, so replaying an
        entry after a crash between the ledger write and the outbox removal is
        a no-op rather than a duplicated observation.
        """
        if self.mode != "paper":
            return 0
        queued = (self._runtime_state or {}).get("edge_outbox")
        if not isinstance(queued, list) or not queued:
            return 0
        from .edge import record_paper_outcome
        settled: set[str] = set()
        deferred: dict[str, int] = {}
        drained = 0
        for entry in queued:
            entry_id = str(entry.get("entry_id")) if isinstance(entry, Mapping) else ""
            outcome = entry.get("outcome") if isinstance(entry, Mapping) else None
            if not entry_id or not isinstance(outcome, Mapping):
                # An unreadable entry can never become ingestible; record the
                # loss in the durable journal instead of retrying forever.
                self._event("edge_outcome_rejected", {"reason": "malformed outbox entry"})
                settled.add(entry_id)
                continue
            try:
                record_paper_outcome(dict(outcome),
                                     db_path=self._edge_db_path or None,
                                     config=self.cfg)
            except (ValueError, KeyError) as exc:
                self._event("edge_outcome_rejected",
                            {"error": str(exc), "entry_id": entry_id,
                             "variant_id": outcome.get("variant_id")})
                settled.add(entry_id)
                continue
            except Exception as exc:  # noqa: BLE001
                attempts = self._number(entry.get("attempts")) or 0.0
                deferred[entry_id] = int(attempts) + 1
                self._event("edge_outcome_deferred",
                            {"error": str(exc), "entry_id": entry_id,
                             "attempts": deferred[entry_id],
                             "variant_id": outcome.get("variant_id")})
                continue
            settled.add(entry_id)
            drained += 1
        if not settled and not deferred:
            return 0

        def prune(latest: dict) -> dict:
            rows = latest.get("edge_outbox")
            rows = [dict(row) for row in rows if isinstance(row, Mapping)] \
                if isinstance(rows, list) else []
            kept = []
            for row in rows:
                entry_id = str(row.get("entry_id"))
                if entry_id in settled:
                    continue
                if entry_id in deferred:
                    row["attempts"] = deferred[entry_id]
                kept.append(row)
            latest["edge_outbox"] = kept
            return latest

        self._runtime_state = state.update_state(prune)
        return drained
