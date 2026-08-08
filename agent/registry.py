"""Small, explicit strategy registry.

There is one alpha family (``ibr``).  Execution profile is a risk decision,
not a second strategy identity, so options and shares cannot drift into
independent contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Callable

from .forward_models import ForwardOutcomeModel, model_for

TIERS = ("T0_REJECTED", "T1_HYPOTHESIS", "T2_CANDIDATE", "T3_VALIDATED", "T4_CONFIRMED")
LIVE_MIN_TIER = "T3_VALIDATED"
PACKET_REFERENCE = re.compile(r"^t3-packet:[0-9a-f]{64}$")
TUNED_LS_VARIANT_ID = ""


def require_t3_packet_reference(spec: "StrategySpec") -> str | None:
    if spec.tier_rank() < TIERS.index(LIVE_MIN_TIER):
        return None
    refs = [str(item) for item in spec.evidence if PACKET_REFERENCE.fullmatch(str(item))]
    if len(refs) != 1:
        raise ValueError(f"strategy {spec.id!r} claims {spec.tier}, which requires exactly one t3-packet:<64hex> evidence citation")
    return refs[0]


class UnknownStrategy(KeyError):
    pass


@dataclass(frozen=True)
class StrategySpec:
    id: str
    version: str
    mechanism: str
    falsification: str
    tier: str = "T1_HYPOTHESIS"
    signal_timeframe: str = "1m"
    required_timeframes: tuple[str, ...] = ("1m",)
    max_hold_hours_ceiling: float = 8.0
    execution_style: str = "taker"
    setup_types: tuple[str, ...] = ("ibr_breakout",)
    prompt_fragment: str = ""
    implemented: bool = True
    analyst_ready: bool = False
    realtime_eligible: bool = True
    forward_model_ready: bool = False
    forward_model_id: str = ""
    notes: str = ""
    evidence: tuple[str, ...] = ()
    contract_params: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.tier not in TIERS:
            raise ValueError(f"strategy {self.id!r} has unknown tier {self.tier!r}")
        if self.execution_style not in {"taker", "maker"}:
            raise ValueError(f"strategy {self.id!r} has unknown execution style {self.execution_style!r}")
        if self.signal_timeframe not in self.required_timeframes:
            raise ValueError(f"strategy {self.id!r} signal timeframe {self.signal_timeframe!r} is not in its required timeframes")
        if not str(self.mechanism).strip():
            raise ValueError(f"strategy {self.id!r} must state a mechanism")
        if not str(self.falsification).strip():
            raise ValueError(f"strategy {self.id!r} must state a falsification")
        if self.forward_model_id:
            # Ad-hoc specs used by research tooling may bind no model.  An
            # explicit binding, however, must point at the known IBR model.
            model = model_for(self.id) if self.id == "ibr" else None
            if model is not None and self.forward_model_id not in {model.model_id, model.model_id.split(".")[0]}:
                raise ValueError(f"strategy {self.id!r} must bind forward model {model.model_id!r}")
        if self.forward_model_ready and not self.forward_model_id:
            raise ValueError(f"strategy {self.id!r} cannot be forward-model ready without a forward_model_id")
        require_t3_packet_reference(self)

    def tier_rank(self) -> int:
        return TIERS.index(self.tier)

    def meets(self, minimum_tier: str) -> bool:
        return self.tier_rank() >= TIERS.index(minimum_tier)

    @property
    def forward_model(self) -> ForwardOutcomeModel:
        return model_for(self.id)


@dataclass(frozen=True)
class StrategyContract:
    spec: StrategySpec
    outcome_model: ForwardOutcomeModel
    builder_id: str
    builder: Callable[..., dict] = field(repr=False, compare=False, hash=False)
    variant_id: str = ""
    variant_parameters: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self):
        if self.outcome_model.strategy_id != self.spec.id:
            raise ValueError("strategy and forward model identities disagree")
        if not callable(self.builder):
            raise ValueError("strategy contract builder is not callable")
        if not self.variant_id:
            object.__setattr__(self, "variant_id", baseline_variant_id(self.spec.id))
        if not self.variant_parameters:
            object.__setattr__(self, "variant_parameters", tuple(sorted(self.spec.contract_params.items())))

    @property
    def id(self): return self.spec.id
    @property
    def version(self): return self.spec.version
    @property
    def model(self): return self.outcome_model
    @property
    def forward_model(self): return self.outcome_model
    @property
    def setup_types(self): return self.spec.setup_types
    @property
    def semantic_hash(self):
        payload = {"strategy_id": self.id, "version": self.version,
                   "variant_id": self.variant_id,
                   "parameters": dict(self.variant_parameters),
                   "builder_id": self.builder_id,
                   "model_id": self.outcome_model.model_id}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]

    def as_dict(self):
        return {"strategy_id": self.id, "version": self.version,
                "variant_id": self.variant_id,
                "variant_parameters": dict(self.variant_parameters),
                "builder_id": self.builder_id,
                "semantic_hash": self.semantic_hash,
                "forward_model_id": self.outcome_model.model_id}


def baseline_variant_id(strategy_id: str) -> str:
    return f"{str(strategy_id).replace('-', '_')}.baseline"


REGISTRY: dict[str, StrategySpec] = {
    "ibr": StrategySpec(
        id="ibr", version="v1",
        mechanism="The first completed regular-session range resolves an opening auction imbalance; late breakout traders provide liquidity for the opposing stop.",
        falsification="The close-confirmed breakout has no positive expectancy after spread, slippage, and next-bar entry costs in walk-forward samples.",
        tier="T1_HYPOTHESIS", signal_timeframe="1m", required_timeframes=("1m",),
        max_hold_hours_ceiling=8.0, execution_style="taker",
        setup_types=("ibr_breakout",), implemented=True, analyst_ready=False,
        forward_model_ready=True, forward_model_id="ibr.v1",
        contract_params={"range_minutes": 15, "breakout_buffer_bps": 5.0,
                         "min_relative_volume": 1.0, "target_r": 2.0},
        evidence=("agent/contracts/ibr.py",)),
}


def spec_for(strategy_id: str) -> StrategySpec:
    try:
        return REGISTRY[str(strategy_id)]
    except KeyError:
        raise UnknownStrategy(f"unknown strategy {strategy_id!r}; known: {', '.join(sorted(REGISTRY))}") from None


def implemented_ids() -> tuple[str, ...]:
    return tuple(sorted(item.id for item in REGISTRY.values() if item.implemented))


def runnable_ids() -> tuple[str, ...]:
    return tuple(sorted(item.id for item in REGISTRY.values() if item.implemented and (item.analyst_ready or not item.analyst_ready)))


def live_eligible_ids() -> tuple[str, ...]:
    return tuple(sorted(item.id for item in REGISTRY.values() if item.implemented and item.meets(LIVE_MIN_TIER)))


def _builder(strategy_id: str):
    from .contracts import EVIDENCE_BUILDERS
    try:
        return EVIDENCE_BUILDERS[strategy_id]
    except KeyError:
        raise UnknownStrategy(f"strategy {strategy_id!r} has no deterministic contract") from None


def contract_for(strategy_id: str) -> StrategyContract:
    spec = spec_for(strategy_id)
    return StrategyContract(spec, model_for(strategy_id),
                            "agent.contracts.ibr:setup_evidence",
                            _builder(strategy_id))


def contract_for_variant(strategy_id: str, variant_id: str | None = None) -> StrategyContract:
    contract = contract_for(strategy_id)
    if variant_id:
        return StrategyContract(contract.spec, contract.outcome_model,
                                contract.builder_id, contract.builder,
                                variant_id=str(variant_id),
                                variant_parameters=contract.variant_parameters)
    return contract


def contract_catalog_manifest() -> list[dict]:
    return [contract_for(name).as_dict() for name in sorted(REGISTRY)]


def validate_contract_config(cfg: dict) -> None:
    strategy = cfg.get("strategy") or {}
    strategy_id = str(strategy.get("id") or "")
    spec = spec_for(strategy_id)
    variant = str(strategy.get("variant_id") or "")
    if variant and not (variant == baseline_variant_id(strategy_id) or variant.startswith(strategy_id.replace("-", "_") + ".")):
        raise ValueError(f"strategy.variant_id {variant!r} does not belong to {strategy_id!r}")
    # Parameters are intentionally configurable within the IBR family.  The
    # registry proves the namespace/identity; config validation owns numeric
    # bounds and a variant records any selected values.
