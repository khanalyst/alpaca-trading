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
from .contracts.rule import (BAR_SECONDS, RULE_SCHEMA_V3, RuleSpecError,
                             completed_bar_exit_transition,
                             initialize_exit_state)
from .instruments import validate_instrument

_FILLED_ORDER_STATUSES = {"filled", "partially_filled"}
_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "replaced", "stopped",
    "suspended", "failed", "not_found",
}
# Alpaca may use these terminal aliases on the order endpoint even though the
# normalized lifecycle rows generally use the shorter forms above.
_PROTECTIVE_TERMINAL_STATUSES = _TERMINAL_ORDER_STATUSES | {
    "done", "closed", "done_for_day",
}
_OPTION_PROFILES = frozenset({"option", "options"})


_EDGE_OUTBOX_WARN = 500


def _outbox_entry_id(outcome: Mapping) -> str:
    """Identify a learning event by the opportunity the ledger keys on."""
    proof_run_id = outcome.get("proof_run_id")
    suffix = f"\0{proof_run_id}" if proof_run_id else ""
    return (f"{outcome.get('variant_id')}\0{outcome.get('vehicle')}"
            f"\0{outcome.get('opportunity_id')}{suffix}")


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
            str(trade.get("execution_profile", "")).lower() in _OPTION_PROFILES)


def _option_profile(profile: Any) -> bool:
    """Whether an execution profile uses the single-leg option semantics."""
    return str(profile or "").lower() in _OPTION_PROFILES


def _value(obj: Any, name: str, default=None):
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


class ExecutionLifecycleMixin:
    def _configured_risk_budget(self, plan: Mapping | None,
                                fallback: float | None = None) -> float | None:
        """Read the immutable pre-cap risk budget from a plan/state row.

        ``configured_risk_budget_usd`` is canonical.  ``risk_budget_usd`` is
        retained as an explicit alias because research rows historically use
        the shorter budget name.  Neither field is reconstructed from a fill:
        the notional cap is precisely why that distinction matters.
        """
        source = plan if isinstance(plan, Mapping) else {}
        for name in ("configured_risk_budget_usd", "risk_budget_usd"):
            value = self._number(source.get(name))
            if value is not None:
                return round(value, 12)
        return self._number(fallback)

    def _planned_risk(self, plan: Mapping | None,
                      fallback: float | None = None) -> float | None:
        """Read the full post-cap planned risk for an immutable sizing plan."""
        source = plan if isinstance(plan, Mapping) else {}
        for name in ("planned_risk_usd", "risk_usd", "intended_risk_usd"):
            value = self._number(source.get(name))
            if value is not None:
                return round(value, 12)
        return self._number(fallback)

    @staticmethod
    def _risk_ratio(numerator: float | None,
                    denominator: float | None) -> float | None:
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return round(numerator / denominator, 12)

    def _risk_telemetry(self, plan: Mapping, profile: str,
                        requested_qty: float | None, planned_qty: float | None,
                        multiplier: float, filled_qty: float,
                        fill_price: float | None) -> tuple[float | None, float | None,
                                                          float | None, float | None]:
        """Return intended/delivered risk and delivery telemetry.

        ``intended`` is captured from the immutable sizing plan at the
        requested quantity.  ``delivered`` is recomputed from cumulative
        broker fills, using stop distance for shares and premium paid times
        contract multiplier for options.  Missing legacy plan/price fields
        fall back to quantity-proportional values so old state remains
        observable without changing admission or sizing behavior.
        """
        plan = plan if isinstance(plan, Mapping) else {}
        profile_key = str(profile or "shares").lower()
        requested = self._number(requested_qty)
        planned = self._number(planned_qty) or requested
        multiplier = abs(self._number(multiplier) or 1.0)

        intended = self._number(plan.get("risk_usd"))
        if intended is None:
            intended = self._number(plan.get("planned_risk_usd"))
        if intended is not None and requested and planned and planned > 0:
            # A plan's risk is normally already sized to the request.  Scale
            # only when the durable planned/requested quantities differ.
            intended *= requested / planned
        if intended is None:
            entry = (self._number(plan.get("entry_price")) or
                     self._number(plan.get("entry_reference")) or
                     self._number(plan.get("reference_price")))
            if _option_profile(profile_key):
                option = plan.get("option") if isinstance(plan.get("option"), Mapping) else {}
                premium = (self._number(option.get("debit")) or entry)
                if premium is not None and requested and requested > 0:
                    intended = abs(premium) * requested * multiplier
            else:
                stop = self._number(plan.get("underlying_stop_price"))
                if stop is None:
                    stop = self._number(plan.get("stop_price"))
                if entry is not None and stop is not None and requested and requested > 0:
                    intended = abs(entry - stop) * requested * multiplier

        filled = max(0.0, self._number(filled_qty) or 0.0)
        delivered: float | None = 0.0 if filled <= 0 else None
        price = self._number(fill_price)
        if filled > 0 and price is not None and price == price and abs(price) != float("inf"):
            if _option_profile(profile_key):
                delivered = abs(price) * filled * multiplier
            else:
                stop = self._number(plan.get("underlying_stop_price"))
                if stop is None:
                    stop = self._number(plan.get("stop_price"))
                if stop is not None:
                    delivered = abs(price - stop) * filled * multiplier
        if delivered is None and intended is not None and planned and planned > 0:
            delivered = intended * (filled / planned)
        if delivered is not None:
            delivered = round(delivered, 12)
        if intended is not None:
            intended = round(intended, 12)
        ratio = (delivered / intended if delivered is not None and intended and intended > 0
                 else (0.0 if delivered is not None and intended == 0 else None))
        shortfall = (max(0.0, intended - delivered)
                     if intended is not None and delivered is not None else None)
        if ratio is not None:
            ratio = round(ratio, 12)
        if shortfall is not None:
            shortfall = round(shortfall, 12)
        return intended, delivered, ratio, shortfall

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
        profile = str(risk_plan.get("execution_profile", "shares") or "shares").lower()
        vehicle = "option" if _option_profile(profile) else "equity"
        option = risk_plan.get("option") if isinstance(risk_plan.get("option"), Mapping) else {}
        # These are plan-time values, not reconstructed from any observed
        # fill.  Options are priced in their own premium unit; the underlying
        # entry price is retained separately on the active trade.
        reference = (risk_plan.get("reference_price")
                     or risk_plan.get("entry_reference")
                     or (option.get("debit") if _option_profile(profile) else None)
                     or risk_plan.get("entry_price"))
        planned_qty = self._number(risk_plan.get("planned_qty"))
        if planned_qty is None:
            planned_qty = self._number(request.qty)
        requested_qty = self._number(request.qty)
        market_price = (self._number(risk_plan.get("market_price"))
                        or self._number(risk_plan.get("mid_price")))
        mid_price = self._number(risk_plan.get("mid_price"))
        option_multiplier = (risk_plan.get("option", {}).get("multiplier")
                             if isinstance(risk_plan.get("option"), Mapping)
                             else None)
        multiplier = (self._number(risk_plan.get("contract_multiplier")) or
                      self._number(option_multiplier) or 1.0)
        intended_risk, delivered_risk, risk_ratio, risk_shortfall = \
            self._risk_telemetry(risk_plan, profile, requested_qty, planned_qty,
                                 multiplier, filled_qty, fill_price)
        configured_budget = self._configured_risk_budget(risk_plan)
        # Keep planned risk at the full immutable sizing quantity.  The
        # existing intended value may be scaled to requested/planned quantity
        # for rolling-state compatibility and is tracked separately.
        planned_risk = self._planned_risk(risk_plan, fallback=intended_risk)
        order_state = {
            "order_id": order_id, "symbol": symbol, "status": status,
            "client_order_id": request.client_order_id, "qty": str(request.qty),
            "filled_qty": filled_qty, "filled_avg_price": fill_price,
            "side": request.side, "type": request.type,
            "time_in_force": request.time_in_force,
            "position_intent": request.position_intent,
            "order_class": request.order_class,
            "execution_profile": profile, "vehicle": vehicle,
            "variant_id": risk_plan.get("variant_id"),
            "reference_price": self._number(reference),
            "entry_reference": self._number(risk_plan.get("entry_reference") or reference),
            "exit_reference": self._number(risk_plan.get("exit_reference")),
            "market_price": market_price, "mid_price": mid_price,
            "requested_qty": requested_qty, "planned_qty": planned_qty,
            "risk_usd": delivered_risk,
            "intended_risk_usd": intended_risk,
            "delivered_risk_usd": delivered_risk,
            "risk_delivery_ratio": risk_ratio,
            "risk_shortfall_usd": risk_shortfall,
            "configured_risk_budget_usd": configured_budget,
            "risk_budget_usd": configured_budget,
            "planned_risk_usd": planned_risk,
            "planned_to_configured_risk_ratio": self._risk_ratio(
                planned_risk, configured_budget),
            "delivered_to_configured_risk_ratio": self._risk_ratio(
                delivered_risk, configured_budget),
            "protective_legs": _protective_legs(getattr(order, "legs", ())),
            "risk_plan": _plain(risk_plan), "fill_logged": False,
            "logged_filled_qty": 0.0, "logged_filled_avg_price": None,
            "updated_ts": time.time(),
        }
        def update(current: dict) -> dict:
            current.setdefault("orders", {})[order_id] = order_state
            if filled_qty > 0 and status in _FILLED_ORDER_STATUSES:
                self._activate_filled_trade(
                    current, order_state, filled_qty, fill_price)
            return current
        current = state.update_state(update)
        persisted_order = current.get("orders", {}).get(order_id, order_state)
        state.log_order(order, request, action="submit", run_id=self.run_id,
                        runtime_mode=self.mode, account_fingerprint=current.get("account_fingerprint"),
                        setup_id=risk_plan.get("setup_id"),
                        execution_profile=profile, vehicle=vehicle,
                        variant_id=risk_plan.get("variant_id"),
                        reference_price=self._number(reference),
                        entry_reference=self._number(risk_plan.get("entry_reference") or reference),
                        exit_reference=self._number(risk_plan.get("exit_reference")),
                        market_price=market_price, mid_price=mid_price,
                        requested_qty=requested_qty, planned_qty=planned_qty,
                        cumulative_filled_qty=filled_qty,
                        fill_fraction=(filled_qty / requested_qty
                                       if requested_qty and requested_qty > 0 else None),
                        filled_fraction=(filled_qty / requested_qty
                                         if requested_qty and requested_qty > 0 else None),
                        risk_usd=persisted_order.get("risk_usd"),
                        intended_risk_usd=persisted_order.get("intended_risk_usd"),
                        delivered_risk_usd=persisted_order.get("delivered_risk_usd"),
                        risk_delivery_ratio=persisted_order.get("risk_delivery_ratio"),
                        risk_shortfall_usd=persisted_order.get("risk_shortfall_usd"),
                        configured_risk_budget_usd=persisted_order.get(
                            "configured_risk_budget_usd"),
                        risk_budget_usd=persisted_order.get("risk_budget_usd"),
                        planned_risk_usd=persisted_order.get("planned_risk_usd"),
                        planned_to_configured_risk_ratio=persisted_order.get(
                            "planned_to_configured_risk_ratio"),
                        delivered_to_configured_risk_ratio=persisted_order.get(
                            "delivered_to_configured_risk_ratio"))
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
        profile = str(plan.get("execution_profile", "shares")).lower()
        vehicle = "option" if _option_profile(profile) else "equity"
        underlying = str(plan.get("underlying_symbol") or
                         (symbol if profile == "shares" else "")).upper()
        direction = str(plan.get("direction") or
                        ("long" if order_state.get("side") == "buy" else "short"))
        instrument_entry = fill_price
        if instrument_entry is None:
            instrument_entry = self._number(
                plan.get("option", {}).get("debit") if _option_profile(profile) and
                isinstance(plan.get("option"), Mapping) else plan.get("entry_price"))
        existing = current.get("active_trades", {}).get(symbol, {})
        existing = existing if isinstance(existing, Mapping) else {}
        v3_progressed = (
            str(existing.get("rule_schema") or "") == RULE_SCHEMA_V3 and
            (existing.get("last_completed_bar_epoch") is not None or
             isinstance(existing.get("stop_replacement"), Mapping)))
        # The broker's ``filled_avg_price`` is cumulative.  Keep the last
        # journaled average so an incremental row can be priced at the
        # implied fill rather than at the new cumulative average.  This is
        # important when fills arrive at different prices: summing the rows
        # must still reproduce the actual weighted position economics.
        logged_qty = self._number(order_state.get("logged_filled_qty")) or 0.0
        logged_avg = self._number(order_state.get("logged_filled_avg_price"))
        if logged_avg is None and logged_qty > 0:
            logged_avg = self._number(existing.get("entry_price"))
        if fill_price is None:
            fill_price = self._number(existing.get("entry_price"))
        if fill_price is None:
            fill_price = self._number(
                plan.get("option", {}).get("debit") if _option_profile(profile) and
                isinstance(plan.get("option"), Mapping) else plan.get("entry_price"))
        instrument_entry = fill_price
        if v3_progressed:
            prior_qty = self._number(existing.get("cumulative_filled_qty"))
            prior_entry = self._number(existing.get("entry_price"))
            if (prior_qty is None or prior_entry is None or
                    abs(prior_qty - filled_qty) > 1e-9 or
                    instrument_entry is None or
                    abs(prior_entry - instrument_entry) > 1e-9):
                raise AlpacaError(
                    "v3 entry fill changed after completed-bar exit processing")

        if order_state.get("fill_logged") and "logged_filled_qty" not in order_state:
            # Older state files used a boolean only; avoid duplicating their
            # already-journaled fill during a rolling upgrade.
            logged_qty = filled_qty
        incremental_qty = max(0.0, filled_qty - logged_qty)
        incremental_price = fill_price
        if (incremental_qty > 0 and logged_qty > 0 and logged_avg is not None and
                fill_price is not None):
            # ``filled_avg_price`` is a quantity-weighted cumulative average.
            # Recover the price represented by this increment while tolerating
            # a contradictory broker snapshot by falling back to its reported
            # cumulative price.
            implied = ((fill_price * filled_qty) - (logged_avg * logged_qty)) / incremental_qty
            if implied == implied and abs(implied) != float("inf"):
                incremental_price = implied

        option = plan.get("option") if isinstance(plan.get("option"), Mapping) else {}
        option_multiplier = option.get("multiplier")
        multiplier = (self._number(plan.get("contract_multiplier")) or
                      self._number(option_multiplier))
        multiplier = abs(multiplier) if multiplier is not None and multiplier > 0 else 1.0
        requested_qty = (self._number(order_state.get("requested_qty"))
                         or self._number(order_state.get("qty"))
                         or self._number(plan.get("planned_qty")))
        planned_qty = (self._number(order_state.get("planned_qty"))
                       or requested_qty)
        entry_reference = (self._number(plan.get("entry_reference"))
                           or self._number(plan.get("reference_price"))
                           or (self._number(option.get("debit")) if _option_profile(profile) else None)
                           or self._number(plan.get("entry_price")))
        market_price = (self._number(plan.get("market_price"))
                        or self._number(plan.get("mid_price")))
        mid_price = self._number(plan.get("mid_price"))

        def economics(quantity: float, price: float | None) -> tuple[float | None, float | None]:
            """Return actual notional and opening risk for a filled quantity."""
            if quantity <= 0:
                return 0.0, 0.0
            planned_qty = self._number(order_state.get("qty"))
            ratio = (quantity / planned_qty if planned_qty and planned_qty > 0 else 1.0)
            planned_notional = self._number(plan.get("notional"))
            planned_risk = self._number(plan.get("risk_usd"))
            notional = None
            risk = None
            if price is not None and price == price and abs(price) != float("inf"):
                notional = abs(price) * quantity * multiplier
                if _option_profile(profile):
                    # A long option's defined opening risk is the debit paid.
                    risk = notional
                else:
                    stop = self._number(plan.get("underlying_stop_price",
                                               plan.get("stop_price")))
                    if stop is not None:
                        risk = abs(price - stop) * quantity * multiplier
            if notional is None and planned_notional is not None:
                notional = planned_notional * ratio
            if risk is None and planned_risk is not None:
                risk = planned_risk * ratio
            # Keep JSON/journal values stable when a cumulative average such
            # as 101.2 implies a repeating binary float (11.000000000000014
            # should remain the economically exact 11.0).
            return (round(notional, 12) if notional is not None else None,
                    round(risk, 12) if risk is not None else None)

        cumulative_notional, cumulative_risk = economics(filled_qty, fill_price)
        intended_risk, delivered_risk, risk_ratio, risk_shortfall = \
            self._risk_telemetry(plan, profile, requested_qty, planned_qty,
                                 multiplier, filled_qty, fill_price)
        if delivered_risk is None:
            delivered_risk = cumulative_risk
            if intended_risk is not None and delivered_risk is not None:
                risk_ratio = (delivered_risk / intended_risk
                              if intended_risk > 0 else 0.0)
                risk_shortfall = max(0.0, intended_risk - delivered_risk)
        if delivered_risk is not None:
            delivered_risk = round(delivered_risk, 12)
        if risk_ratio is not None:
            risk_ratio = round(risk_ratio, 12)
        if risk_shortfall is not None:
            risk_shortfall = round(risk_shortfall, 12)
        configured_budget = self._configured_risk_budget(
            plan, fallback=order_state.get("configured_risk_budget_usd"))
        # ``planned_risk_usd`` is the full post-cap sizing risk.  Do not use
        # the requested-quantity-scaled intended value here.
        planned_risk = self._planned_risk(
            plan, fallback=order_state.get("planned_risk_usd", intended_risk))
        planned_to_configured = self._risk_ratio(planned_risk, configured_budget)
        delivered_to_configured = self._risk_ratio(delivered_risk, configured_budget)
        # ``risk_usd`` historically represented the cumulative open-position
        # risk. Keep it as a compatibility alias for delivered risk.
        order_state.update({
            "risk_usd": delivered_risk,
            "intended_risk_usd": intended_risk,
            "delivered_risk_usd": delivered_risk,
            "risk_delivery_ratio": risk_ratio,
            "risk_shortfall_usd": risk_shortfall,
            "configured_risk_budget_usd": configured_budget,
            "risk_budget_usd": configured_budget,
            "planned_risk_usd": planned_risk,
            "planned_to_configured_risk_ratio": planned_to_configured,
            "delivered_to_configured_risk_ratio": delivered_to_configured,
        })
        trade = {
            "symbol": symbol, "underlying_symbol": underlying,
            "execution_profile": profile, "vehicle": vehicle,
            "direction": direction,
            "position_side": "long" if order_state.get("side") == "buy" else "short",
            "qty": str(filled_qty), "entry_price": instrument_entry,
            "underlying_entry_price": plan.get("entry_price"),
            "requested_qty": requested_qty, "planned_qty": planned_qty,
            "cumulative_filled_qty": filled_qty,
            "fill_fraction": (filled_qty / requested_qty
                              if requested_qty and requested_qty > 0 else None),
            "filled_fraction": (filled_qty / requested_qty
                                if requested_qty and requested_qty > 0 else None),
            "reference_price": entry_reference, "entry_reference": entry_reference,
            "exit_reference": self._number(plan.get("exit_reference")),
            "market_price": market_price, "mid_price": mid_price,
            "opened_at": existing.get("opened_at", time.time()),
            "setup_type": plan.get("setup_type", "ibr"),
            "setup_id": plan.get("setup_id"), "order_id": order_state.get("order_id"),
            "status": "open", "stop_price": plan.get("underlying_stop_price", plan.get("stop_price")),
            "target_price": plan.get("underlying_target_price", plan.get("target_price")),
            "force_flat_at": plan.get("force_flat_at"),
            "max_hold_bars": plan.get("max_hold_bars", existing.get("max_hold_bars")),
            "hold_deadline_ts": plan.get("hold_deadline_ts",
                                         existing.get("hold_deadline_ts")),
            "risk_usd": delivered_risk,
            "intended_risk_usd": intended_risk,
            "delivered_risk_usd": delivered_risk,
            "risk_delivery_ratio": risk_ratio,
            "risk_shortfall_usd": risk_shortfall,
            "configured_risk_budget_usd": configured_budget,
            "risk_budget_usd": configured_budget,
            "planned_risk_usd": planned_risk,
            "planned_to_configured_risk_ratio": planned_to_configured,
            "delivered_to_configured_risk_ratio": delivered_to_configured,
            "notional": cumulative_notional, "variant_id": plan.get("variant_id"),
            "candidate_id": plan.get("candidate_id"),
            "proof_run_id": plan.get("proof_run_id"),
            "strategy_id": plan.get("strategy_id", self.cfg.get("strategy", {}).get("id")),
            "strategy_version": plan.get("strategy_version", self.cfg.get("strategy", {}).get("version")),
            "contract_multiplier": multiplier,
        }
        if str(plan.get("rule_schema") or "") == RULE_SCHEMA_V3:
            if v3_progressed:
                bounded_exit = {
                    key: deepcopy(existing[key]) for key in (
                        "direction", "entry_price", "initial_stop_price",
                        "active_stop_price", "target_price", "initial_risk",
                        "breakeven_r", "breakeven_armed_at",
                        "breakeven_armed_epoch", "entry_bar_pending",
                        "last_completed_bar_at", "last_completed_bar_epoch")
                    if key in existing
                }
            else:
                try:
                    bounded_exit = initialize_exit_state(
                        direction, instrument_entry,
                        plan.get("underlying_stop_price", plan.get("stop_price")),
                        plan.get("underlying_target_price", plan.get("target_price")),
                        breakeven_r=plan.get("breakeven_r"))
                except RuleSpecError as exc:
                    raise AlpacaError(f"filled v3 exit state is invalid: {exc}") from exc
            signal_ts = self._number(plan.get("signal_ts"))
            trade.update(bounded_exit)
            trade.update({
                "rule_schema": RULE_SCHEMA_V3,
                "exit_entry_bar_epoch": (
                    signal_ts + BAR_SECONDS if signal_ts is not None else None),
            })
        # The broker-resident bracket legs are the position's real protection.
        # Keep the ids observed at submission; a later reconciliation refreshes
        # their status but must not lose the association.
        legs = (_leg_rows(existing) if v3_progressed else
                (_leg_rows(order_state) or _leg_rows(existing)))
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
                "max_hold_bars", "hold_deadline_ts", "protective_legs",
                "rule_schema", "initial_stop_price", "active_stop_price",
                "breakeven_r", "breakeven_armed_at", "breakeven_armed_epoch",
                "last_completed_bar_at", "last_completed_bar_epoch")
        }
        if incremental_qty > 0:
            incremental_notional, incremental_risk = economics(
                incremental_qty, incremental_price)
            state.log_trade(
                symbol, order_state.get("side"), "open", incremental_qty,
                price=incremental_price, notional=incremental_notional,
                risk_usd=incremental_risk, order_id=order_state.get("order_id"),
                setup_id=plan.get("setup_id"), setup_type=plan.get("setup_type"),
                strategy_id=trade.get("strategy_id"),
                strategy_version=trade.get("strategy_version"),
                variant_id=trade.get("variant_id"), fill_status="filled",
                execution_profile=profile, vehicle=trade.get("vehicle"),
                reference_price=entry_reference, entry_reference=entry_reference,
                exit_reference=trade.get("exit_reference"), market_price=market_price,
                mid_price=mid_price, requested_qty=requested_qty,
                planned_qty=planned_qty, cumulative_filled_qty=filled_qty,
                fill_fraction=(filled_qty / requested_qty
                               if requested_qty and requested_qty > 0 else None),
                filled_fraction=(filled_qty / requested_qty
                                 if requested_qty and requested_qty > 0 else None),
                intended_risk_usd=intended_risk,
                delivered_risk_usd=delivered_risk,
                risk_delivery_ratio=risk_ratio,
                risk_shortfall_usd=risk_shortfall,
                configured_risk_budget_usd=configured_budget,
                risk_budget_usd=configured_budget,
                planned_risk_usd=planned_risk,
                planned_to_configured_risk_ratio=planned_to_configured,
                delivered_to_configured_risk_ratio=delivered_to_configured,
                runtime_mode=self.mode, account_fingerprint=current.get("account_fingerprint"),
                run_id=self.run_id)
            order_state["fill_logged"] = True
            order_state["logged_filled_qty"] = filled_qty
        if filled_qty > 0:
            # Persist the cumulative average even when quantity did not grow;
            # a corrected broker average must be the baseline for the next
            # increment and must not cause a duplicate journal row.
            order_state["logged_filled_avg_price"] = fill_price
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
        submitted underneath it is rejected for insufficient quantity.  The
        cancel endpoint's acceptance response is not proof: a stop/target can
        fill in the race window, so the broker's terminal order snapshot must
        confirm every leg before a close is sent.
        """
        live = [leg for leg in legs if _leg_live(leg)]
        if not live:
            return True
        cancel = getattr(self.provider, "cancel_order", None)
        orders = getattr(self.provider, "orders", None)
        if not callable(orders):
            # Lightweight/test providers may expose only their existing
            # reconciliation snapshot.  It is still a broker boundary and
            # therefore sufficient for terminal-status proof; an absent
            # snapshot remains fail-closed below.
            reconcile = getattr(self.provider, "reconcile", None)
            if callable(reconcile):
                def orders(**_kwargs):
                    snapshot = reconcile()
                    return snapshot.get("orders") if isinstance(snapshot, Mapping) else None
        if not callable(cancel) or not callable(orders):
            self._event("protection_cancel_failed", {
                "symbol": symbol,
                "reason": "provider cannot cancel and confirm one order"})
            return False

        cancel_errors: dict[str, str] = {}
        for leg in live:
            leg_id = str(leg.get("order_id"))
            try:
                cancel(leg_id)
            except Exception as exc:  # noqa: BLE001
                # A cancel race often reports a provider error after the
                # broker has already filled the child.  Continue to the
                # authoritative snapshot before deciding whether it is safe.
                cancel_errors[leg_id] = str(exc)

        by_id: dict[str, Any] = {}
        # The order endpoint can lag the cancel request by one short poll. A
        # bounded retry proves terminal state without turning a close into an
        # unbounded wait. Missing/working rows remain unsafe.
        for poll in range(3):
            try:
                try:
                    snapshot = orders(status="all")
                except TypeError:
                    snapshot = orders()
            except Exception as exc:  # noqa: BLE001
                self._event("protection_cancel_failed", {
                    "symbol": symbol, "reason": f"status confirmation unavailable: {exc}"})
                return False
            if not isinstance(snapshot, (list, tuple)):
                self._event("protection_cancel_failed", {
                    "symbol": symbol, "reason": "status confirmation malformed"})
                return False
            by_id = {
                str(_value(order, "id", "")): order
                for order in snapshot
                if _value(order, "id", None) is not None
            }
            unresolved = False
            for leg in live:
                broker_leg = by_id.get(str(leg.get("order_id")))
                if broker_leg is None:
                    unresolved = True
                    continue
                status_value = _value(broker_leg, "status", "")
                status = str(getattr(status_value, "value", status_value) or "")
                status = status.split(".")[-1].strip().lower()
                filled_qty = self._number(_value(broker_leg, "filled_qty", 0)) or 0.0
                if filled_qty > 0 or status == "filled":
                    # A filled child is the close.  Mark it as such so the
                    # monitor cannot submit a second market close while the
                    # next reconciliation catches its terminal evidence.
                    leg["status"] = "filled"
                    leg["filled_qty"] = filled_qty
                    leg["filled_avg_price"] = self._number(
                        _value(broker_leg, "filled_avg_price", None))
                    leg["cancel_race"] = True
                    self._event("protection_fill_race", {
                        "symbol": symbol, "order_id": str(leg.get("order_id")),
                        "status": status, "filled_qty": filled_qty})
                    unresolved = True
                    continue
                if status not in _PROTECTIVE_TERMINAL_STATUSES:
                    unresolved = True
                    continue
                leg["status"] = status
                if str(leg.get("order_id")) in cancel_errors:
                    # The error is acceptable only because the broker now
                    # proves a terminal non-fill state.
                    self._event("protection_cancel_confirmed_after_error", {
                        "symbol": symbol, "order_id": str(leg.get("order_id")),
                        "status": status, "error": cancel_errors[str(leg.get("order_id"))]})
            if not unresolved:
                break
            if poll < 2:
                time.sleep(0.05)

        cancelled = [str(leg.get("order_id")) for leg in live
                     if str(leg.get("status", "")).lower() in _PROTECTIVE_TERMINAL_STATUSES
                     and str(leg.get("status", "")).lower() not in _FILLED_ORDER_STATUSES]
        unsafe = [str(leg.get("order_id")) for leg in live
                  if str(leg.get("status", "")).lower() not in _PROTECTIVE_TERMINAL_STATUSES]
        filled = [str(leg.get("order_id")) for leg in live
                  if str(leg.get("status", "")).lower() in _FILLED_ORDER_STATUSES]

        def mark(current: dict) -> dict:
            for bucket in ("active_trades", "protection"):
                row = current.get(bucket, {}).get(symbol)
                if not isinstance(row, dict):
                    continue
                for leg in _leg_rows(row):
                    leg_id = str(leg.get("order_id"))
                    if leg_id in cancelled or leg_id in filled:
                        source = next((item for item in live
                                       if str(item.get("order_id")) == leg_id), None)
                        if source is not None:
                            leg.update({key: value for key, value in source.items()
                                        if key in {"status", "filled_qty",
                                                   "filled_avg_price", "cancel_race"}})
                        if leg_id in filled:
                            row["status"] = "closing"
                            row.setdefault("closing_reason", "protection_fill")
                            row.setdefault("closing_order_id", leg_id)
            return current
        try:
            state.update_state(mark)
        except Exception as exc:  # noqa: BLE001
            self._event("protection_cancel_failed", {
                "symbol": symbol, "reason": f"cancel not durable: {exc}"})
            return False
        if filled:
            # A child fill is terminal broker evidence, but not proof that the
            # whole position is flat.  Refuse the parent close and let
            # reconciliation attribute any residual quantity.
            return False
        if unsafe:
            self._event("protection_cancel_failed", {
                "symbol": symbol, "order_ids": unsafe,
                "reason": "broker status is not terminal"})
            return False
        self._event("protection_cancelled", {"symbol": symbol,
                                              "order_ids": cancelled})
        return True

    def _protection_price(self, trade: Mapping, position: Any,
                          now: datetime) -> float | None:
        """Return the underlying price used by both stock and option exits."""
        profile = str(trade.get("execution_profile", "shares")).lower()
        if not _option_profile(profile):
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

    def _completed_exit_bars(self, trade: Mapping, now: datetime,
                             market_rows: Mapping[str, Any] | None) -> list[Any]:
        """Return ordered unseen completed bars for one durable v3 exit."""
        underlying = str(trade.get("underlying_symbol") or
                         trade.get("symbol") or "").upper()
        row = market_rows.get(underlying, {}) if isinstance(market_rows, Mapping) else {}
        bars = row.get("bars", []) if isinstance(row, Mapping) else []
        if not bars:
            start_epoch = (self._number(trade.get("last_completed_bar_epoch")) or
                           self._number(trade.get("exit_entry_bar_epoch")) or
                           self._number(trade.get("opened_at")))
            start = (datetime.fromtimestamp(max(0.0, start_epoch - BAR_SECONDS),
                                            timezone.utc)
                     if start_epoch is not None else now - timedelta(minutes=5))
            try:
                fetched = self.market.stock_bars(
                    [underlying], timeframe="1m", start=start, end=now)
                bars = fetched.get(underlying, []) if isinstance(fetched, Mapping) else []
            except Exception:  # noqa: BLE001
                return []
        prepared: list[tuple[datetime, Any]] = []
        entry_epoch = self._number(trade.get("exit_entry_bar_epoch"))
        last_epoch = self._number(trade.get("last_completed_bar_epoch"))
        for raw in bars or ():
            try:
                bar = self._bar_mapping(raw, underlying)
            except (AttributeError, TypeError, ValueError):
                continue
            stamp = self._timestamp(bar.get("timestamp"))
            if stamp is None:
                continue
            end = stamp + timedelta(seconds=BAR_SECONDS)
            if end > now or (entry_epoch is not None and
                             stamp.timestamp() < entry_epoch - 1e-9):
                continue
            if last_epoch is not None and end.timestamp() <= last_epoch + 1e-9:
                continue
            prepared.append((stamp, bar))
        prepared.sort(key=lambda item: item[0])
        if any(right[0] - left[0] != timedelta(seconds=BAR_SECONDS)
               for left, right in zip(prepared, prepared[1:])):
            return []
        return [bar for _stamp, bar in prepared]

    @staticmethod
    def _replacement_leg(order: Any, stop_price: float) -> dict[str, Any]:
        return {
            "order_id": str(_value(order, "id", "")), "role": "stop",
            "status": str(_value(order, "status", "accepted") or
                          "accepted").lower(),
            "price": float(stop_price),
            "qty": float(_value(order, "qty", 0) or 0),
            "filled_qty": float(_value(order, "filled_qty", 0) or 0),
            "filled_avg_price": (
                float(_value(order, "filled_avg_price"))
                if _value(order, "filled_avg_price", None) is not None else None),
        }

    def _replace_breakeven_stop(self, symbol: str, trade: dict,
                                desired_stop: float) -> tuple[bool, dict]:
        """Persist intent, amend one stop, then durably attach its successor."""
        live_stops = [leg for leg in _leg_rows(trade)
                      if str(leg.get("role")) == "stop" and _leg_live(leg)]
        if len(live_stops) != 1 or not _broker_protected(_leg_rows(trade)):
            self._event("breakeven_stop_replace_failed", {
                "symbol": symbol, "reason": "one complete live bracket is required"})
            return False, trade
        replace_stop = getattr(self.provider, "replace_stop_order", None)
        if not callable(replace_stop):
            self._event("breakeven_stop_replace_failed", {
                "symbol": symbol, "reason": "provider cannot replace a stop order"})
            return False, trade
        old_leg = live_stops[0]
        old_id = str(old_leg.get("order_id"))
        pending = {
            "status": "pending", "old_order_id": old_id,
            "requested_stop_price": float(desired_stop),
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "requested_epoch": time.time(),
        }
        trade["desired_stop_price"] = float(desired_stop)
        trade["stop_replacement"] = dict(pending)

        def mark_pending(current: dict) -> dict:
            for bucket in ("active_trades", "protection"):
                row = current.get(bucket, {}).get(symbol)
                if not isinstance(row, dict):
                    continue
                for key in ("breakeven_armed_at", "breakeven_armed_epoch",
                            "last_completed_bar_at", "last_completed_bar_epoch"):
                    row[key] = trade.get(key)
                row["desired_stop_price"] = float(desired_stop)
                row["stop_replacement"] = dict(pending)
            return current

        state.update_state(mark_pending)
        try:
            replacement = replace_stop(old_id, desired_stop)
        except Exception as exc:  # noqa: BLE001
            self._event("breakeven_stop_replace_failed", {
                "symbol": symbol, "order_id": old_id, "error": str(exc)})
            # A replace can race a stop/target fill.  Reconcile authoritative
            # broker state before any retry or local close is considered.
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001
                pass
            refreshed = state.load_state().get("active_trades", {}).get(symbol, trade)
            return False, dict(refreshed) if isinstance(refreshed, Mapping) else trade
        new_leg = self._replacement_leg(replacement, desired_stop)
        if not new_leg["order_id"]:
            raise AlpacaError("replacement stop has no broker order id")
        confirmed = {
            **pending, "status": "confirmed",
            "new_order_id": new_leg["order_id"],
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "confirmed_epoch": time.time(),
        }

        def attach(current: dict) -> dict:
            for bucket in ("active_trades", "protection"):
                row = current.get(bucket, {}).get(symbol)
                if not isinstance(row, dict):
                    continue
                legs = [dict(item) for item in _leg_rows(row)
                        if str(item.get("order_id")) != old_id]
                legs.append(dict(new_leg))
                row.update({
                    "protective_legs": legs,
                    "active_stop_price": float(desired_stop),
                    "desired_stop_price": float(desired_stop),
                    "stop_replacement": dict(confirmed),
                })
                if new_leg["status"] in _FILLED_ORDER_STATUSES:
                    row.update({"status": "closing", "closing_reason": "stop",
                                "closing_order_id": new_leg["order_id"],
                                "closing_price": new_leg.get("filled_avg_price")})
            return current

        current = state.update_state(attach)
        refreshed = current.get("active_trades", {}).get(symbol, trade)
        self._event("breakeven_stop_replaced", {
            "symbol": symbol, "old_order_id": old_id,
            "new_order_id": new_leg["order_id"], "stop_price": desired_stop})
        return True, dict(refreshed) if isinstance(refreshed, Mapping) else trade

    def _recover_stop_replacement(self, symbol: str, trade: dict,
                                  by_id: Mapping[str, Any]) -> None:
        """Recover a replacement chain after a crash between broker/local writes."""
        pending = trade.get("stop_replacement")
        if not isinstance(pending, Mapping) or str(pending.get("status")) != "pending":
            return
        pending = dict(pending)
        old_id = str(pending.get("old_order_id") or "")
        desired = self._number(pending.get("requested_stop_price"))
        if not old_id or desired is None:
            raise AlpacaError(f"pending stop replacement for {symbol} is malformed")
        old_order = by_id.get(old_id)
        new_id = str(pending.get("new_order_id") or "")
        if not new_id and old_order is not None:
            new_id = str(_value(old_order, "replaced_by", "") or "")
        successor = by_id.get(new_id) if new_id else None
        if successor is None:
            successor = next((order for order in by_id.values()
                              if str(_value(order, "replaces", "") or "") == old_id),
                             None)
            if successor is not None:
                new_id = str(_value(successor, "id", "") or "")
        old_status = str(_value(old_order, "status", "") or "").lower()
        if successor is None:
            if old_order is None:
                raise AlpacaError(
                    f"pending replacement stop {old_id} is unavailable")
            if old_status == "replaced" or new_id:
                raise AlpacaError(
                    f"replacement successor for stop {old_id} is unavailable")
            # A visible predecessor with no replacement chain can be refreshed
            # by the leg lifecycle below.  A still-live predecessor may be
            # retried by the next monitor; a terminal one fails protection
            # closed instead.
            pending["reconcile_misses"] = int(
                pending.get("reconcile_misses", 0) or 0) + 1
            trade["stop_replacement"] = pending
            return
        if str(_value(successor, "type", "") or "").lower() not in {
                "stop", "stop_limit"}:
            raise AlpacaError("replacement successor is not a stop order")
        raw = _value(successor, "raw", {})
        broker_stop = self._number(_value(raw, "stop_price", None))
        if broker_stop is None or abs(broker_stop - desired) > 1e-9:
            raise AlpacaError("replacement successor stop price is contradictory")
        new_leg = self._replacement_leg(successor, desired)
        legs = [dict(item) for item in _leg_rows(trade)
                if str(item.get("order_id")) != old_id]
        legs.append(new_leg)
        pending.update({
            "status": "confirmed", "new_order_id": new_id,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "confirmed_epoch": time.time(), "recovered": True,
        })
        trade.update({
            "protective_legs": legs, "active_stop_price": desired,
            "desired_stop_price": desired, "stop_replacement": pending,
        })
        if new_leg["status"] in _FILLED_ORDER_STATUSES:
            trade.update({"status": "closing", "closing_reason": "stop",
                          "closing_order_id": new_id,
                          "closing_price": new_leg.get("filled_avg_price")})
        self._event("breakeven_stop_recovered", {
            "symbol": symbol, "old_order_id": old_id,
            "new_order_id": new_id, "stop_price": desired})

    def _monitor_positions(self, now: datetime, positions: list[Any], *,
                           market_rows: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
            transition_reason = None
            filled_fraction = (self._number(trade.get("filled_fraction"))
                               if isinstance(trade, Mapping) else None)
            if (isinstance(trade, Mapping) and
                    str(trade.get("rule_schema") or "") == RULE_SCHEMA_V3 and
                    filled_fraction is not None and filled_fraction >= 1.0 - 1e-9):
                trade = dict(trade)
                pending = trade.get("stop_replacement")
                if (isinstance(pending, Mapping) and
                        str(pending.get("status")) == "pending" and protected):
                    desired_stop = self._number(
                        pending.get("requested_stop_price",
                                    trade.get("desired_stop_price")))
                    if desired_stop is None:
                        failed.append({"symbol": symbol,
                                       "reason": "breakeven_stop_invalid"})
                        continue
                    replaced, trade = self._replace_breakeven_stop(
                        symbol, trade, desired_stop)
                    if not replaced:
                        active[symbol] = trade
                        failed.append({
                            "symbol": symbol,
                            "reason": "breakeven_stop_replace_failed"})
                        continue
                    legs = _leg_rows(trade)
                    protected = _broker_protected(legs)
                bars = self._completed_exit_bars(trade, now, market_rows)
                if bars:
                    old_active_stop = self._number(trade.get("active_stop_price"))
                    stop_changed = False
                    try:
                        for bar in bars:
                            transition = completed_bar_exit_transition(trade, bar)
                            trade.update(transition["state"])
                            stop_changed = stop_changed or bool(
                                transition.get("stop_changed"))
                            if transition.get("exit") is not None:
                                transition_reason = str(
                                    transition["exit"].get("reason") or "") or None
                                trade["completed_bar_exit"] = dict(transition["exit"])
                                break
                    except RuleSpecError as exc:
                        failed.append({"symbol": symbol,
                                       "reason": "v3_exit_state_invalid",
                                       "error": str(exc)})
                        self._event("v3_exit_state_invalid", {
                            "symbol": symbol, "error": str(exc)})
                        continue
                    if stop_changed and transition_reason is None and protected:
                        desired_stop = self._number(trade.get("active_stop_price"))
                        if desired_stop is None:
                            failed.append({"symbol": symbol,
                                           "reason": "breakeven_stop_invalid"})
                            continue
                        # The pure state has armed the desired next-bar stop;
                        # broker protection remains at the old stop until the
                        # replacement response or reconciliation proves success.
                        trade["active_stop_price"] = old_active_stop
                        replaced, trade = self._replace_breakeven_stop(
                            symbol, trade, desired_stop)
                        if not replaced:
                            active[symbol] = trade
                            failed.append({
                                "symbol": symbol,
                                "reason": "breakeven_stop_replace_failed"})
                            continue
                        legs = _leg_rows(trade)
                        protected = _broker_protected(legs)
                    active[symbol] = trade
                    changed = True
                    if (trade.get("closing_order_id") or any(
                            str(leg.get("status", "")).lower() == "filled"
                            for leg in legs)):
                        # A replacement response may already be filled.  That
                        # broker leg is the exit; never fall through to the
                        # local protection poller and submit a second close.
                        continue
            price = self._protection_price(trade, position, now) if isinstance(trade, Mapping) else None
            direction = str(trade.get("direction", _value(position, "side", "long"))).lower()
            stop = self._number(trade.get("active_stop_price",
                                          trade.get("stop_price")))
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
            elif transition_reason is not None and not protected:
                reason = transition_reason
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
                protection = dict(runtime.get("protection", {}))
                for symbol, trade in active.items():
                    if (not isinstance(trade, Mapping) or
                            str(trade.get("rule_schema") or "") != RULE_SCHEMA_V3):
                        continue
                    row = dict(protection.get(symbol, {}))
                    for key in ("rule_schema", "initial_stop_price",
                                "active_stop_price", "breakeven_r",
                                "breakeven_armed_at", "breakeven_armed_epoch",
                                "last_completed_bar_at", "last_completed_bar_epoch",
                                "desired_stop_price", "stop_replacement",
                                "protective_legs"):
                        if key in trade:
                            row[key] = deepcopy(trade[key])
                    protection[symbol] = row
                runtime = state.update_state(lambda current: {
                    **current,
                    "active_trades": dict(active),
                    "protection": protection,
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
            if (isinstance(trade, dict) and
                    str(trade.get("rule_schema") or "") == RULE_SCHEMA_V3):
                self._recover_stop_replacement(symbol, trade, by_id)
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
            fill_growth = filled_qty > saved_filled_qty
            saved.update({"status": status, "filled_qty": filled_qty,
                          "filled_avg_price": fill_price, "not_found_count": 0,
                          "updated_ts": time.time()})
            logged_qty = self._number(saved.get("logged_filled_qty")) or 0.0
            logged_avg = self._number(saved.get("logged_filled_avg_price"))
            avg_changed = (fill_price is not None and
                           (logged_avg is None or abs(fill_price - logged_avg) > 1e-12))
            if (filled_qty > 0 and saved.get("risk_plan") and
                    (not saved.get("fill_logged") or filled_qty > logged_qty or avg_changed)):
                # Some broker snapshots expose filled_qty before updating the
                # order status.  Durable quantity growth is enough evidence
                # to protect and journal the incremental fill.
                self._activate_filled_trade(current, saved, filled_qty, fill_price)
            if status != old_status or fill_growth or avg_changed:
                evidence_plan = saved.get("risk_plan") if isinstance(
                    saved.get("risk_plan"), Mapping) else {}
                evidence_profile = str(saved.get(
                    "execution_profile",
                    evidence_plan.get("execution_profile", "shares")) or "shares").lower()
                evidence_vehicle = str(saved.get(
                    "vehicle",
                    "option" if _option_profile(evidence_profile) else "equity"))
                evidence_requested = (self._number(saved.get("requested_qty"))
                                      or self._number(saved.get("qty")))
                evidence_planned = (self._number(saved.get("planned_qty"))
                                    or evidence_requested)
                evidence_configured = self._configured_risk_budget(
                    evidence_plan,
                    fallback=saved.get("configured_risk_budget_usd",
                                       saved.get("risk_budget_usd")))
                evidence_planned_risk = self._planned_risk(
                    evidence_plan,
                    fallback=(saved.get("planned_risk_usd") or
                              saved.get("intended_risk_usd")))
                evidence_delivered_risk = self._number(
                    saved.get("delivered_risk_usd", saved.get("risk_usd")))
                evidence_reference = (self._number(saved.get("reference_price"))
                                      or self._number(saved.get("entry_reference"))
                                      or self._number(evidence_plan.get("entry_price")))
                state.log_order(
                    broker_order, None, action="reconcile", run_id=self.run_id,
                    runtime_mode=self.mode, account_fingerprint=current.get("account_fingerprint"),
                    setup_id=evidence_plan.get("setup_id"),
                    execution_profile=evidence_profile, vehicle=evidence_vehicle,
                    variant_id=saved.get("variant_id") or evidence_plan.get("variant_id"),
                    reference_price=evidence_reference,
                    entry_reference=(self._number(saved.get("entry_reference"))
                                    or evidence_reference),
                    exit_reference=self._number(saved.get("exit_reference")),
                    requested_qty=evidence_requested, planned_qty=evidence_planned,
                    cumulative_filled_qty=filled_qty,
                    fill_fraction=(filled_qty / evidence_requested
                                   if evidence_requested and evidence_requested > 0 else None),
                    filled_fraction=(filled_qty / evidence_requested
                                     if evidence_requested and evidence_requested > 0 else None),
                    risk_usd=saved.get("risk_usd"),
                    intended_risk_usd=saved.get("intended_risk_usd"),
                    delivered_risk_usd=saved.get("delivered_risk_usd"),
                    risk_delivery_ratio=saved.get("risk_delivery_ratio"),
                    risk_shortfall_usd=saved.get("risk_shortfall_usd"),
                    configured_risk_budget_usd=evidence_configured,
                    risk_budget_usd=evidence_configured,
                    planned_risk_usd=evidence_planned_risk,
                    planned_to_configured_risk_ratio=(
                        self._risk_ratio(evidence_planned_risk,
                                         evidence_configured)),
                    delivered_to_configured_risk_ratio=(
                        self._risk_ratio(evidence_delivered_risk,
                                         evidence_configured)))

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
            # Order reconciliation above may have advanced a cumulative fill
            # in this same broker snapshot.  Prefer that fresh activation over
            # the immutable pre-snapshot view; otherwise a position row would
            # clobber its newly-correct risk/notional with stale economics.
            fresh_item = current.get("active_trades", {}).get(symbol, {})
            refreshed_item = previous.get(symbol, {})
            item = dict(fresh_item if isinstance(fresh_item, Mapping)
                        else refreshed_item)
            # ``previous`` is also where this reconciliation refreshed the
            # broker-resident child legs.  A fresh fill activation must win
            # for quantity/economics, while the refreshed leg lifecycle must
            # win for protection and any close it just observed.
            if isinstance(refreshed_item, Mapping):
                for key in ("protective_legs", "closing_order_id",
                            "closing_reason", "closing_price", "rule_schema",
                            "initial_stop_price", "active_stop_price",
                            "breakeven_r", "breakeven_armed_at",
                            "breakeven_armed_epoch", "last_completed_bar_at",
                            "last_completed_bar_epoch", "desired_stop_price",
                            "stop_replacement"):
                    if key in refreshed_item:
                        item[key] = deepcopy(refreshed_item[key])
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
                        "status": "unprotected", "risk_usd": 0.0,
                        "intended_risk_usd": None,
                        "delivered_risk_usd": 0.0,
                        "risk_delivery_ratio": None,
                        "risk_shortfall_usd": None}
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
            # A canceled/rejected entry with a positive fill still owns real
            # exposure.  If the position endpoint transiently omits that
            # residual, do not interpret cancellation of the remainder as a
            # close of the filled quantity.
            entry_order = order_state.get(str(trade.get("order_id") or ""))
            entry_status = (str(entry_order.get("status", "")).lower()
                            if isinstance(entry_order, Mapping) else "")
            entry_filled_qty = (self._number(entry_order.get("filled_qty")) or 0.0
                                if isinstance(entry_order, Mapping) else 0.0)
            if (entry_status in {"canceled", "cancelled", "expired", "rejected"} and
                    entry_filled_qty > 0 and not trade.get("closing_order_id")):
                fresh = current.get("active_trades", {}).get(symbol)
                active[symbol] = dict(fresh if isinstance(fresh, Mapping) else trade)
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
            # The broker's terminal close and durable active-trade removal are
            # separate writes (SQLite and JSON respectively).  A crash in
            # between must replay the close journal as a no-op, not book a
            # second realized P&L row.  Entry/close order ids are stable across
            # process restarts; the fallback includes the original open time
            # for a broker-only position with no local entry order.
            entry_order_id = str(trade.get("order_id") or "")
            close_order_id = str(trade.get("closing_order_id") or "")
            close_trade_id = (
                f"close:{entry_order_id}:{close_order_id}"
                if entry_order_id or close_order_id else
                f"close:{symbol}:{trade.get('opened_at')}"
            )
            # Close evidence is explicit and independent of the entry's
            # incremental notional.  A terminal close snapshot may carry a
            # partial fill; use the broker's cumulative quantity when it is
            # available, otherwise preserve the historical filled-status
            # fallback where brokers omitted it.
            close_order = (closing_order if closing_order is not None else
                           order_state.get(close_order_id))
            close_status = str(
                getattr(close_order, "status", "") if close_order is not None
                else (close_order.get("status", "") if isinstance(close_order, Mapping) else "")
            ).lower()
            close_filled = (self._number(getattr(close_order, "filled_qty", None))
                            if close_order is not None and not isinstance(close_order, Mapping)
                            else self._number(close_order.get("filled_qty"))
                            if isinstance(close_order, Mapping) else None)
            if close_filled is None or close_filled <= 0:
                close_filled = qty if close_status == "filled" else 0.0
            # A locally recorded close can be durable before its broker order
            # row is available (for example, a position snapshot arrives
            # after the close was submitted but before order reconciliation).
            # ``closing_price`` is only populated as close evidence by the
            # monitor when it has no order fill to report; when an order id is
            # present we wait for that order's terminal fill above.  Preserve
            # the historical close inference for this order-less, explicit
            # local close while continuing to reject canceled/rejected closes
            # with no fill evidence.
            if (close_filled <= 0 and close_order is None and
                    exit_price is not None and trade.get("closing_reason")):
                close_filled = qty
            close_filled = min(qty, max(0.0, close_filled))
            if close_filled <= 0:
                # A canceled/rejected close with no fill is not an exit. Keep
                # the durable exposure active so the monitor can retry it,
                # and never create a zero-quantity calibration row.
                active[symbol] = dict(trade)
                continue
            close_reference = (self._number(trade.get("exit_reference"))
                               or self._number(trade.get("current_price")))
            close_notional = (abs(exit_price) * close_filled * multiplier
                              if exit_price is not None and close_filled > 0 else None)
            close_requested = (self._number(close_order.get("qty"))
                               if isinstance(close_order, Mapping) else
                               self._number(getattr(close_order, "qty", None))) or qty
            close_fraction = (close_filled / close_requested
                              if close_requested and close_requested > 0 else None)
            if entry is not None and exit_price is not None:
                realized = (exit_price - entry) * close_filled * multiplier * sign
            state.log_trade(
                symbol, "sell" if str(trade.get("position_side", "long")) == "long" else "buy",
                "close", close_filled, price=exit_price, notional=close_notional,
                reason=trade.get("closing_reason", "broker_reconcile"),
                trade_id=close_trade_id,
                realized_pnl_usd=realized, pnl_pct=pnl_pct,
                close_trigger=trade.get("closing_reason", "broker_reconcile"),
                setup_id=trade.get("setup_id"), setup_type=trade.get("setup_type"),
                strategy_id=trade.get("strategy_id"),
                strategy_version=trade.get("strategy_version"),
                variant_id=trade.get("variant_id"),
                execution_profile=trade.get("execution_profile"),
                vehicle=trade.get("vehicle"), reference_price=close_reference,
                entry_reference=trade.get("entry_reference"),
                exit_reference=close_reference, market_price=trade.get("current_price"),
                mid_price=trade.get("mid_price"), requested_qty=close_requested,
                planned_qty=close_requested, cumulative_filled_qty=close_filled,
                fill_fraction=close_fraction, filled_fraction=close_fraction,
                risk_usd=trade.get("delivered_risk_usd", trade.get("risk_usd")),
                intended_risk_usd=trade.get("intended_risk_usd"),
                delivered_risk_usd=trade.get(
                    "delivered_risk_usd", trade.get("risk_usd")),
                risk_delivery_ratio=trade.get("risk_delivery_ratio"),
                risk_shortfall_usd=trade.get("risk_shortfall_usd"),
                configured_risk_budget_usd=trade.get(
                    "configured_risk_budget_usd", trade.get("risk_budget_usd")),
                risk_budget_usd=trade.get(
                    "configured_risk_budget_usd", trade.get("risk_budget_usd")),
                planned_risk_usd=trade.get(
                    "planned_risk_usd", trade.get("intended_risk_usd")),
                planned_to_configured_risk_ratio=trade.get(
                    "planned_to_configured_risk_ratio"),
                delivered_to_configured_risk_ratio=trade.get(
                    "delivered_to_configured_risk_ratio"),
                runtime_mode=self.mode,
                account_fingerprint=current.get("account_fingerprint"), run_id=self.run_id)
            self._record_edge_outcome(trade, realized, pnl_pct, exit_price)
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
        protection = dict(current.get("protection", {}))
        for symbol, trade in active.items():
            if (not isinstance(trade, Mapping) or
                    str(trade.get("rule_schema") or "") != RULE_SCHEMA_V3):
                continue
            row = dict(protection.get(symbol, {}))
            for key in ("rule_schema", "initial_stop_price", "active_stop_price",
                        "breakeven_r", "breakeven_armed_at",
                        "breakeven_armed_epoch", "last_completed_bar_at",
                        "last_completed_bar_epoch", "desired_stop_price",
                        "stop_replacement", "protective_legs"):
                if key in trade:
                    row[key] = deepcopy(trade[key])
            protection[symbol] = row
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
        vehicle = "option" if _option_profile(trade.get("execution_profile")) else "equity"
        risk_usd = self._number(trade.get("risk_usd"))
        opened = self._number(trade.get("opened_at")) or time.time()
        outcome = {
            "variant_id": variant_id, "vehicle": vehicle,
            "candidate_id": trade.get("candidate_id"),
            "proof_run_id": trade.get("proof_run_id"),
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
