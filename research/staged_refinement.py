"""Bounded deterministic configuration neighborhoods for staged mechanisms.

This module plans or registers a handful of preregistered scalar neighbours.
It never searches combinations and never edits strategy code or the registry.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from agent.contract_dsl import FIELD_BOUNDS, ProposedContract, validate
from agent.staged_identity import (configuration_identity, mechanism_identity,
                                   proposal_mapping)
from agent.staging import StagingError, StagingStore

REFINABLE_FAILURES = frozenset({
    "NEGATIVE_EXPECTANCY", "DIED_OUT_OF_SAMPLE", "NEVER_FIRED",
})


@dataclass(frozen=True)
class RefinementPolicy:
    max_attempts: int = 2
    max_variants_per_attempt: int = 2
    max_configurations_per_mechanism: int = 5
    relative_step: float = 0.10

    def __post_init__(self):
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be non-negative")
        if not 1 <= self.max_variants_per_attempt <= 2:
            raise ValueError("max_variants_per_attempt must be 1 or 2")
        if self.max_configurations_per_mechanism < 1:
            raise ValueError("max_configurations_per_mechanism must be positive")
        if not 0.0 < self.relative_step <= 0.5:
            raise ValueError("relative_step must be in (0, 0.5]")


DEFAULT_REFINEMENT_POLICY = RefinementPolicy()


def _short_id(base: str, proposal: dict, label: str) -> str:
    raw = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:7]
    room = 64 - len(label) - len(suffix) - 2
    return f"{base[:room]}.{label}{suffix}"


def _threshold(condition: dict) -> tuple[str, float] | None:
    op = condition.get("op")
    value = condition.get("value")
    if op not in {">", ">=", "<", "<="}:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return str(op), float(value)


def _step(condition: dict, value: float, relative_step: float) -> float:
    field = condition.get("field")
    bounds = FIELD_BOUNDS.get(str(field))
    if bounds:
        return max((bounds[1] - bounds[0]) * relative_step, 1e-9)
    return max(abs(value) * relative_step, 0.01)


def _bounded_value(condition: dict, value: float) -> float:
    bounds = FIELD_BOUNDS.get(str(condition.get("field")))
    if bounds:
        return min(bounds[1], max(bounds[0], value))
    return value


def configuration_neighborhood(
        proposal: ProposedContract | dict, *, max_variants: int = 2,
        relative_step: float = 0.10,
        failure_code: str | None = None) -> list[dict]:
    """Return at most two one-parameter neighbours; never a Cartesian grid."""
    if not 1 <= int(max_variants) <= 2:
        raise ValueError("max_variants must be 1 or 2")
    if not 0.0 < float(relative_step) <= 0.5:
        raise ValueError("relative_step must be in (0, 0.5]")
    contract = proposal if isinstance(proposal, ProposedContract) else validate(proposal)
    base = proposal_mapping(contract)
    selected = None
    for index, condition in enumerate(base["conditions"]):
        threshold = _threshold(condition)
        if threshold is not None:
            selected = index, threshold
            break
    if selected is None:
        return []

    index, (op, value) = selected
    step = _step(base["conditions"][index], value, relative_step)
    if failure_code == "NEVER_FIRED":
        # Move only toward a more reachable cell; the next attempt may move
        # once more, but it cannot fan out into threshold mining.
        signs = (-1,) if op in {">", ">="} else (1,)
    else:
        signs = (-1, 1)

    variants = []
    seen = {configuration_identity(contract)}
    for ordinal, sign in enumerate(signs, start=1):
        candidate = copy.deepcopy(base)
        changed = _bounded_value(
            candidate["conditions"][index], value + sign * step)
        if changed == value:
            continue
        candidate["conditions"][index]["value"] = changed
        candidate["contract_id"] = _short_id(
            contract.contract_id, candidate, f"n{ordinal}")
        validated = validate(candidate)
        identity = configuration_identity(validated)
        if identity in seen:
            continue
        seen.add(identity)
        variants.append(proposal_mapping(validated))
        if len(variants) >= max_variants:
            break
    return variants


def stage_initial_neighborhood(
        store: StagingStore, proposal: object, *, generation: int,
        policy: RefinementPolicy | None = None,
        now: float | None = None) -> dict:
    """Register one novel root and its small deterministic neighbourhood."""
    policy = policy or DEFAULT_REFINEMENT_POLICY
    contract = validate(proposal)
    variant_budget = min(
        policy.max_variants_per_attempt,
        policy.max_configurations_per_mechanism - 1)
    variants = (configuration_neighborhood(
        contract, max_variants=variant_budget,
        relative_step=policy.relative_step)
        if variant_budget else [])
    store.ensure_capacity(1 + len(variants))
    root = store.register_bounded(
        proposal_mapping(contract), generation=generation, now=now)
    mechanism_id_value = mechanism_identity(root)
    registered = [root.contract_id]
    for variant in variants:
        child = store.register_variant(
            variant, mechanism_id=mechanism_id_value,
            parent_contract_id=root.contract_id, generation=generation,
            refinement_attempt=0, now=now)
        registered.append(child.contract_id)
    return {
        "schema": "staged_neighborhood.v1",
        "mechanism_id": mechanism_id_value,
        "root_contract_id": root.contract_id,
        "registered_contract_ids": registered,
        "configuration_count": len(registered),
    }


def plan_refinement(
        store: StagingStore, contract_id: str, failure_code: str, *,
        policy: RefinementPolicy | None = None) -> dict:
    """Plan the next bounded attempt without mutating the staging store."""
    policy = policy or DEFAULT_REFINEMENT_POLICY
    failure_code = str(failure_code)
    if failure_code not in REFINABLE_FAILURES:
        return {
            "status": "NOT_REFINABLE", "contract_id": str(contract_id),
            "failure_code": failure_code, "proposals": [],
        }
    record = store.record(contract_id)
    if record is None:
        raise StagingError(f"contract_id {contract_id!r} is unknown")
    reason = str(record.get("retired_reason") or "")
    if not reason:
        raise StagingError(
            "refinement requires a persisted coded failure on a retired "
            "configuration")
    if reason.split(":", 1)[0] != failure_code:
        raise StagingError("failure_code does not match the persisted retirement")
    next_attempt = int(record["refinement_attempt"]) + 1
    family = store.mechanism_records(record["mechanism_id"])
    remaining = policy.max_configurations_per_mechanism - len(family)
    if next_attempt > policy.max_attempts or remaining <= 0:
        return {
            "status": "BUDGET_EXHAUSTED", "contract_id": str(contract_id),
            "mechanism_id": record["mechanism_id"],
            "attempt": next_attempt, "proposals": [],
            "configuration_count": len(family),
        }
    proposals = configuration_neighborhood(
        record["proposal"],
        max_variants=min(policy.max_variants_per_attempt, remaining),
        relative_step=min(0.5, policy.relative_step * (next_attempt + 1)),
        failure_code=failure_code)
    existing = {str(item["configuration_id"]) for item in family}
    proposals = [item for item in proposals
                 if configuration_identity(item) not in existing][:remaining]
    return {
        "status": "PLANNED" if proposals else "NO_TUNABLE_PARAMETER",
        "contract_id": str(contract_id),
        "mechanism_id": record["mechanism_id"],
        "attempt": next_attempt,
        "failure_code": failure_code,
        "proposals": proposals,
        "configuration_count": len(family),
    }


def register_refinement(
        store: StagingStore, plan: dict, *, generation: int | None = None,
        now: float | None = None) -> dict:
    """Register an already planned refinement, respecting store backpressure."""
    proposals = list(plan.get("proposals") or [])
    if plan.get("status") != "PLANNED" or not proposals:
        return {**plan, "registered_contract_ids": []}
    store.ensure_capacity(len(proposals))
    generation = store.generation() + 1 if generation is None else int(generation)
    registered = []
    for proposal in proposals:
        contract = store.register_variant(
            proposal, mechanism_id=str(plan["mechanism_id"]),
            parent_contract_id=str(plan["contract_id"]), generation=generation,
            refinement_attempt=int(plan["attempt"]), now=now)
        registered.append(contract.contract_id)
    return {
        **plan, "status": "REGISTERED",
        "generation": generation,
        "registered_contract_ids": registered,
    }
