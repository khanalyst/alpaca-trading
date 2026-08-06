"""Declarative signal contracts a proposer can author without writing code.

Every registered mechanism so far is a hand-written Python function in
``agent/hypotheses.py``. That is the ceiling on idea generation: only three
exist, all attached to one strategy, and adding a fourth needs a developer.
An analyst can tune their thresholds but cannot state a new reason someone
loses money to us.

A declarative contract closes that gap without handing an author arbitrary
execution. A proposal is data: a mechanism, the payer, a falsifier, and a
list of comparisons over named market fields. This module validates that data
and compiles it into the same ``(snapshot, direction, cfg, params) ->
(fired, reason)`` callable the hand-written contracts already implement, so a
proposed mechanism reaches the shadow lanes through exactly the path a
reviewed one does.

The bounds are the point. A field must be one the forward models already
declare, an operator must be one of four comparisons, and a threshold must be
finite and inside the range observed for that field. Nothing here can read a
file, call a network, or reach the exchange: the compiled contract only ever
reads numbers out of the snapshot it is handed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dataclass_field
from typing import Callable

from . import registry as strategy_registry
from .forward_models import require_complete_contract

# A proposal must state a cause and a way to be wrong. The floor matches
# research.gates.has_mechanism so a claim accepted here cannot be rejected
# later for thinness it could have been caught for at authoring time.
MIN_CLAIM_CHARS = 40
MAX_CONDITIONS = 6
OPERATORS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda observed, threshold: observed > threshold,
    ">=": lambda observed, threshold: observed >= threshold,
    "<": lambda observed, threshold: observed < threshold,
    "<=": lambda observed, threshold: observed <= threshold,
}
DIRECTIONS = ("long", "short", "both")


class ContractProposalError(ValueError):
    """The proposal is not executable as written."""


def _observable_fields() -> dict[str, None]:
    """Fields the validated forward models already declare as required.

    Deriving the allowlist instead of hardcoding it means a proposer can only
    name inputs the pipeline is known to populate, and a field added to a
    contract becomes proposable without editing this module.
    """
    names: dict[str, None] = {}
    for strategy_id in sorted(strategy_registry.REGISTRY):
        try:
            model = require_complete_contract(strategy_id)
        except Exception:  # noqa: BLE001 - an incomplete contract adds nothing
            continue
        for name in model.required_fields:
            # Enrichment is a nested private structure the simulator walks,
            # not a scalar a comparison can be written against.
            if not name.startswith("_enrichment."):
                names[name] = None
    return names


OBSERVABLE_FIELDS = _observable_fields()

# Percentiles and ratios have natural ranges; a threshold outside them can
# only ever fire always or never, which is a proposal that measures nothing.
FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    "funding_percentile_30": (0.0, 100.0),
    "long_short_percentile_30": (0.0, 100.0),
    "range_pos_pct": (0.0, 100.0),
    "trend_1h": (-1.0, 1.0),
    "trend_4h": (-1.0, 1.0),
    "relative_volume_1h": (0.0, 100.0),
    "atr_1h_ratio": (0.0, 100.0),
    "spread_pct": (0.0, 100.0),
}


@dataclass(frozen=True)
class Condition:
    """One comparison against one observed field."""

    field: str
    op: str
    value: float
    # ``None`` applies the comparison to both directions. Naming a direction
    # is how a proposal says "long only when momentum is positive" without
    # needing two separate contracts.
    when_direction: str | None = None

    def describe(self) -> str:
        scope = f" ({self.when_direction} only)" if self.when_direction else ""
        return f"{self.field} {self.op} {self.value:g}{scope}"


@dataclass(frozen=True)
class ProposedContract:
    """A mechanism a proposer authored, validated and ready to compile."""

    contract_id: str
    mechanism: str
    payer: str
    falsifier: str
    direction: str
    conditions: tuple[Condition, ...]
    author: str = "llm"
    notes: str = ""
    _fields: tuple[str, ...] = dataclass_field(default=())

    def describe(self) -> str:
        return "; ".join(condition.describe() for condition in self.conditions)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractProposalError(f"{name} is required")
    text = " ".join(value.split())
    if len(text) < MIN_CLAIM_CHARS:
        raise ContractProposalError(
            f"{name} is {len(text)} characters; a claim under "
            f"{MIN_CLAIM_CHARS} cannot state a cause and a test")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractProposalError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ContractProposalError(f"{name} must be finite")
    return number


def _condition(raw: object, index: int) -> Condition:
    if not isinstance(raw, dict):
        raise ContractProposalError(f"condition {index} must be an object")
    name = raw.get("field")
    if name not in OBSERVABLE_FIELDS:
        raise ContractProposalError(
            f"condition {index} names {name!r}, which no validated contract "
            f"declares. Proposable fields: {', '.join(sorted(OBSERVABLE_FIELDS))}")
    op = raw.get("op")
    if op not in OPERATORS:
        raise ContractProposalError(
            f"condition {index} uses operator {op!r}; "
            f"allowed: {', '.join(sorted(OPERATORS))}")
    value = _number(raw.get("value"), f"condition {index} value")
    low, high = FIELD_BOUNDS.get(str(name), (None, None))
    if low is not None and not (low <= value <= high):
        raise ContractProposalError(
            f"condition {index} threshold {value:g} is outside the observed "
            f"range of {name} ({low:g}..{high:g}), so it fires always or never")
    when = raw.get("when_direction")
    if when is not None and when not in ("long", "short"):
        raise ContractProposalError(
            f"condition {index} when_direction must be long, short, or absent")
    return Condition(str(name), str(op), value, when)


def validate(proposal: object) -> ProposedContract:
    """Turn an authored proposal into a validated contract, or refuse it."""
    if not isinstance(proposal, dict):
        raise ContractProposalError("a contract proposal must be an object")

    contract_id = proposal.get("contract_id")
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ContractProposalError("contract_id is required")
    contract_id = contract_id.strip()
    if not all(part.isalnum() or part in "-_." for part in contract_id):
        raise ContractProposalError(
            "contract_id may contain only alphanumerics, '-', '_' and '.'")

    direction = proposal.get("direction", "both")
    if direction not in DIRECTIONS:
        raise ContractProposalError(
            f"direction must be one of {', '.join(DIRECTIONS)}")

    raw_conditions = proposal.get("conditions")
    if not isinstance(raw_conditions, (list, tuple)) or not raw_conditions:
        raise ContractProposalError("at least one condition is required")
    if len(raw_conditions) > MAX_CONDITIONS:
        raise ContractProposalError(
            f"{len(raw_conditions)} conditions exceeds the {MAX_CONDITIONS} "
            "allowed; a contract that needs more is fitting the sample")
    conditions = tuple(
        _condition(raw, index) for index, raw in enumerate(raw_conditions))

    # A contract whose conditions all name one direction can never fire for
    # the other, so the declared direction must match what it can do.
    scoped = {condition.when_direction for condition in conditions
              if condition.when_direction}
    if direction == "both" and len(scoped) == 1 and len(scoped) == len(
            {c.when_direction for c in conditions}):
        raise ContractProposalError(
            f"direction is 'both' but every condition is {scoped.pop()}-only")
    if direction in ("long", "short") and scoped - {direction}:
        raise ContractProposalError(
            f"direction is {direction} but a condition is scoped to "
            f"{', '.join(sorted(scoped - {direction}))}")

    return ProposedContract(
        contract_id=contract_id,
        mechanism=_text(proposal.get("mechanism"), "mechanism"),
        payer=_text(proposal.get("payer"), "payer"),
        falsifier=_text(proposal.get("falsifier"), "falsifier"),
        direction=direction,
        conditions=conditions,
        author=str(proposal.get("author") or "llm"),
        notes=" ".join(str(proposal.get("notes") or "").split()),
        _fields=tuple(sorted({c.field for c in conditions})),
    )


def compile_contract(contract: ProposedContract) -> Callable:
    """Return the same callable shape a hand-written contract implements.

    The closure reads only the snapshot it is given. ``params`` may override
    any threshold by field name, which is what lets an authored contract be
    swept along one axis exactly like a registered one.
    """

    def evaluate(snapshot: dict, direction: str, cfg: dict,
                 params: dict) -> tuple[bool, str | None]:
        del cfg  # thresholds live on the contract, not in configuration
        if contract.direction != "both" and direction != contract.direction:
            return False, (
                f"{contract.contract_id} is {contract.direction}-only")
        overrides = params if isinstance(params, dict) else {}
        for condition in contract.conditions:
            if (condition.when_direction is not None
                    and condition.when_direction != direction):
                continue
            raw = (snapshot or {}).get(condition.field)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return False, f"{condition.field} unavailable"
            observed = float(raw)
            if not math.isfinite(observed):
                return False, f"{condition.field} unavailable"
            threshold = condition.value
            if condition.field in overrides:
                try:
                    candidate = float(overrides[condition.field])
                except (TypeError, ValueError):
                    return False, f"{condition.field} override is not a number"
                if math.isfinite(candidate):
                    threshold = candidate
            if not OPERATORS[condition.op](observed, threshold):
                return False, (
                    f"{condition.field} {observed:g} fails "
                    f"{condition.op} {threshold:g}")
        return True, None

    evaluate.__name__ = f"proposed_{contract.contract_id.replace('.', '_')}"
    evaluate.__doc__ = contract.mechanism
    return evaluate


def build(proposal: object) -> tuple[ProposedContract, Callable]:
    """Validate and compile in one step."""
    contract = validate(proposal)
    return contract, compile_contract(contract)
