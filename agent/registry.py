"""Single-strategy registry and fail-closed variant authorization.

The append-only edge ledger owns evidence and promotion state.  This module
only defines the IBR semantic contract and the bounded variant namespace used
by both research and paper execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Callable


class UnknownStrategy(KeyError):
    pass


@dataclass(frozen=True)
class StrategySpec:
    id: str
    version: str
    mechanism: str
    falsification: str
    setup_types: tuple[str, ...] = ("ibr_breakout",)
    contract_params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not self.version:
            raise ValueError("strategy id and version are required")
        if not self.mechanism.strip() or not self.falsification.strip():
            raise ValueError("strategy mechanism and falsification are required")


@dataclass(frozen=True)
class StrategyContract:
    spec: StrategySpec
    builder_id: str
    builder: Callable[..., dict] = field(repr=False, compare=False, hash=False)
    variant_id: str = ""
    variant_parameters: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not callable(self.builder):
            raise ValueError("strategy contract builder is not callable")
        if not self.variant_id:
            object.__setattr__(self, "variant_id", baseline_variant_id(self.spec.id))
        if not self.variant_parameters:
            object.__setattr__(
                self, "variant_parameters",
                tuple(sorted(self.spec.contract_params.items())))

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def version(self) -> str:
        return self.spec.version

    @property
    def setup_types(self) -> tuple[str, ...]:
        return self.spec.setup_types

    @property
    def semantic_hash(self) -> str:
        payload = {
            "strategy_id": self.id, "version": self.version,
            "variant_id": self.variant_id,
            "parameters": dict(self.variant_parameters),
            "builder_id": self.builder_id,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]


def baseline_variant_id(strategy_id: str) -> str:
    return f"{str(strategy_id).replace('-', '_')}.baseline"


REGISTRY: dict[str, StrategySpec] = {
    "ibr": StrategySpec(
        id="ibr", version="v1",
        mechanism=(
            "A completed opening range can reveal an auction imbalance whose "
            "close-confirmed break may continue intraday."),
        falsification=(
            "The close-confirmed breakout has no positive paired expectancy "
            "after executable costs in chronological and forward-shadow data."),
        setup_types=("ibr_breakout",),
        contract_params={"range_minutes": 15, "breakout_buffer_bps": 5.0,
                         "min_relative_volume": 1.0, "target_r": 2.0}),
}


def spec_for(strategy_id: str) -> StrategySpec:
    try:
        return REGISTRY[str(strategy_id)]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise UnknownStrategy(
            f"unknown strategy {strategy_id!r}; known: {known}") from None


def _builder(strategy_id: str):
    from .contracts import EVIDENCE_BUILDERS
    try:
        return EVIDENCE_BUILDERS[strategy_id]
    except KeyError:
        raise UnknownStrategy(
            f"strategy {strategy_id!r} has no deterministic contract") from None


def contract_for(strategy_id: str) -> StrategyContract:
    spec = spec_for(strategy_id)
    return StrategyContract(
        spec, "agent.contracts.ibr:setup_evidence", _builder(strategy_id))


def known_variant_ids(strategy_id: str = "ibr") -> tuple[str, ...]:
    ids = {baseline_variant_id(strategy_id)}
    try:
        from .variants import load_registry
        path = Path(__file__).resolve().parents[1] / "research" / "variants.yaml"
        ids.update(item.variant_id for item in load_registry(path).values()
                   if item.strategy_id == strategy_id)
    except Exception:
        # A missing/corrupt registry must never broaden runtime authority.
        pass
    return tuple(sorted(ids))


def validate_variant_id(strategy_id: str, variant_id: str) -> str:
    if not variant_id:
        return baseline_variant_id(strategy_id)
    if variant_id == baseline_variant_id(strategy_id):
        return variant_id
    prefix = strategy_id.replace("-", "_") + "."
    if not variant_id.startswith(prefix):
        raise ValueError(
            f"strategy.variant_id {variant_id!r} does not belong to {strategy_id!r}")
    if variant_id not in known_variant_ids(strategy_id):
        raise ValueError(
            f"variant {variant_id!r} is not pre-registered for {strategy_id!r}")
    return variant_id


def _parameters_for(spec: StrategySpec, variant_id: str) -> tuple[tuple[str, Any], ...]:
    parameters = dict(spec.contract_params)
    if variant_id != baseline_variant_id(spec.id):
        try:
            from .variants import load_registry
            path = Path(__file__).resolve().parents[1] / "research" / "variants.yaml"
            variant = load_registry(path)[variant_id]
            for name, value in variant.overrides.items():
                if name.startswith("strategy."):
                    parameters[name.split(".", 1)[1]] = value
        except Exception as exc:
            raise ValueError(
                f"variant {variant_id!r} has no readable registry record") from exc
    return tuple(sorted(parameters.items()))


def contract_for_variant(strategy_id: str,
                         variant_id: str | None = None) -> StrategyContract:
    contract = contract_for(strategy_id)
    selected = validate_variant_id(
        strategy_id, str(variant_id or baseline_variant_id(strategy_id)))
    return StrategyContract(
        contract.spec, contract.builder_id, contract.builder,
        variant_id=selected,
        variant_parameters=_parameters_for(contract.spec, selected))


def validate_contract_config(cfg: dict) -> None:
    strategy = cfg.get("strategy") or {}
    strategy_id = str(strategy.get("id") or "")
    spec_for(strategy_id)
    validate_variant_id(strategy_id, str(strategy.get("variant_id") or ""))
