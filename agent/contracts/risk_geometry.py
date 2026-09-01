"""Shared geometry implied by the stressed-cost admission policy.

The stressed-cost veto charges ``scenario_bps`` of entry notional and caps
that charge at ``max_ratio`` of the trade's planned risk.  For a linear equity
position the quantity cancels, so the minimum admissible stop distance is
``scenario_bps / max_ratio`` basis points of the entry price.

Keeping this arithmetic in one small, dependency-free module lets signal
construction, replay, and runtime risk checks agree without importing one
another.  Invalid policy remains fail-closed.
"""

from __future__ import annotations

import math
from numbers import Real
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR


class RiskGeometryError(ValueError):
    """Raised when a stressed-cost geometry input is malformed."""


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise RiskGeometryError(f"{name} must be a finite number")
    resolved = float(value)
    if not math.isfinite(resolved):
        raise RiskGeometryError(f"{name} must be a finite number")
    return resolved


def required_stop_distance_bps(scenario_bps: object,
                               max_cost_to_risk_ratio: object) -> float:
    """Return the minimum stop width implied by one stress policy.

    A zero ratio is an intentional kill switch: no finite stop can satisfy it,
    so the result is positive infinity rather than an arbitrary large number.
    """

    scenario = _finite_number(scenario_bps, "scenario_bps")
    ratio = _finite_number(max_cost_to_risk_ratio,
                           "max_cost_to_risk_ratio")
    if scenario < 0:
        raise RiskGeometryError("scenario_bps cannot be negative")
    if ratio < 0:
        raise RiskGeometryError("max_cost_to_risk_ratio cannot be negative")
    if ratio == 0:
        return math.inf
    return scenario / ratio


def effective_stop_floor_bps(base_floor_bps: object, scenario_bps: object,
                             max_cost_to_risk_ratio: object) -> float:
    """Return the stricter of the grammar floor and stress-implied floor."""

    base = _finite_number(base_floor_bps, "base_floor_bps")
    if base < 0:
        raise RiskGeometryError("base_floor_bps cannot be negative")
    return max(base, required_stop_distance_bps(
        scenario_bps, max_cost_to_risk_ratio))


def effective_stop_distance(entry_price: object, authored_distance: object, *,
                            base_floor_bps: object, scenario_bps: object,
                            max_cost_to_risk_ratio: object,
                            minimum_increment: object | None = None,
                            ) -> tuple[float, float]:
    """Return ``(distance, floor_bps)`` for a policy-compatible equity stop."""

    entry = _finite_number(entry_price, "entry_price")
    authored = _finite_number(authored_distance, "authored_distance")
    if entry <= 0:
        raise RiskGeometryError("entry_price must be positive")
    if authored < 0:
        raise RiskGeometryError("authored_distance cannot be negative")
    floor_bps = effective_stop_floor_bps(
        base_floor_bps, scenario_bps, max_cost_to_risk_ratio)
    if not math.isfinite(floor_bps):
        raise RiskGeometryError("stress policy admits no finite stop distance")
    distance = max(authored, entry * floor_bps / 10_000.0)
    if not math.isfinite(distance) or distance <= 0:
        raise RiskGeometryError(
            "effective stop distance must be finite and positive")
    if minimum_increment is not None:
        increment = _finite_number(minimum_increment, "minimum_increment")
        if increment <= 0:
            raise RiskGeometryError("minimum_increment must be positive")
        # Round the distance *away* from entry.  The broker-facing layer may
        # normalize equity prices to ticks; rounding a binding floor inward
        # would recreate the cost/risk contradiction by a fraction of a cent.
        distance = float(
            (Decimal(str(distance)) / Decimal(str(increment))).to_integral_value(
                rounding=ROUND_CEILING) * Decimal(str(increment)))
        if not math.isfinite(distance) or distance <= 0:
            raise RiskGeometryError(
                "effective stop distance must be finite and positive")
    return distance, floor_bps


def equity_price_increment(price: object) -> float:
    """Return the conservative US-equity price increment used by execution."""

    resolved = _finite_number(price, "price")
    if resolved <= 0:
        raise RiskGeometryError("price must be positive")
    return 0.01 if resolved >= 1.0 else 0.0001


def quantize_equity_price(value: object, *, rounding: str) -> Decimal:
    """Return one positive broker-valid US-equity price."""

    if isinstance(value, bool):
        raise RiskGeometryError("equity order price must be finite and positive")
    try:
        price = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError, InvalidOperation) as exc:
        raise RiskGeometryError(
            "equity order price must be finite and positive") from exc
    if not price.is_finite() or price <= 0:
        raise RiskGeometryError("equity order price must be finite and positive")
    increment = Decimal("0.01") if price >= Decimal("1") else Decimal("0.0001")
    try:
        rounded = price.quantize(increment, rounding=rounding)
    except (ArithmeticError, InvalidOperation, ValueError) as exc:
        raise RiskGeometryError(
            "equity order price cannot be represented at a valid tick") from exc
    if price < Decimal("1") <= rounded:
        rounded = rounded.quantize(Decimal("0.01"), rounding=rounding)
    if not rounded.is_finite() or rounded <= 0:
        raise RiskGeometryError("equity order price must be finite and positive")
    return rounded


def quantize_equity_bracket(entry_price: object, stop_price: object,
                            target_price: object, direction: object,
                            ) -> tuple[float, float, float]:
    """Round a bracket exactly as the broker boundary does.

    Protective stops move away from entry so rounding cannot shrink risk
    geometry; profit targets move toward entry so replay cannot overstate
    attainable reward.
    """

    if isinstance(entry_price, bool):
        raise RiskGeometryError(
            "equity entry price is unavailable for tick rounding")
    try:
        entry = Decimal(str(entry_price))
    except (TypeError, ValueError, ArithmeticError, InvalidOperation) as exc:
        raise RiskGeometryError(
            "equity entry price is unavailable for tick rounding") from exc
    if not entry.is_finite() or entry <= 0:
        raise RiskGeometryError(
            "equity entry price is unavailable for tick rounding")
    side = str(direction or "").lower()
    if side == "long":
        stop = quantize_equity_price(stop_price, rounding=ROUND_FLOOR)
        target = quantize_equity_price(target_price, rounding=ROUND_FLOOR)
        valid = stop < entry < target
    elif side == "short":
        stop = quantize_equity_price(stop_price, rounding=ROUND_CEILING)
        target = quantize_equity_price(target_price, rounding=ROUND_CEILING)
        valid = target < entry < stop
    else:
        raise RiskGeometryError(
            "equity direction is unavailable for tick rounding")
    if not valid:
        raise RiskGeometryError("tick-rounded bracket legs do not straddle entry")
    return float(stop), float(target), float(abs(entry - stop))


__all__ = [
    "RiskGeometryError", "effective_stop_distance", "equity_price_increment",
    "effective_stop_floor_bps", "quantize_equity_bracket",
    "quantize_equity_price", "required_stop_distance_bps",
]
