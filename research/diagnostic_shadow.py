"""Frozen non-authorizing real-time shadow cohort construction.

The cohort in this module is deliberately independent of EdgeLedger state.
Every code/config epoch receives the same canonical family templates and one
predeclared one-factor comparison per family.  Observed outcomes never feed
back into these definitions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from agent.contracts.rule import RULE_FAMILIES, rule_variant_id, validate_rule_spec
from agent.registry import validate_contract_config
from agent.variants import apply as apply_variant, load_registry
from research.factory_core import template_hypothesis


DIAGNOSTIC_COHORT_SCHEMA = "diagnostic-shadow-cohort.v1"
DIAGNOSTIC_ACTIVATION_SCHEMA = "diagnostic-shadow-activation.v1"
DIAGNOSTIC_CANDIDATE_PREFIX = "shadow:diagnostic:"
IBR_BROKER_ONLY_LIMITS = (
    "shortability", "buying_power", "pending_orders", "actual_fills",
)

# These deltas are fixed design choices, not adaptations to observed returns.
# Each changes one executable entry coordinate from its canonical family root;
# risk, execution, cost, and sizing policy remain inherited unchanged from the
# mounted runtime configuration.
_ONE_FACTOR_VARIANTS: dict[str, tuple[str, Any]] = {
    "opening_range_breakout": ("threshold_bps", 8.0),
    "opening_range_fade": ("threshold_bps", 12.0),
    "momentum_continuation": ("threshold_bps", 24.0),
    "mean_reversion": ("zscore", 1.75),
    "trend_pullback": ("threshold_bps", 20.0),
    "volatility_breakout": ("compression_bps", 65.0),
    "volume_breakout": ("volume_multiplier", 1.75),
    "vwap_reversion": ("threshold_bps", 35.0),
    "vwap_trend": ("threshold_bps", 12.0),
    "range_expansion": ("volume_multiplier", 2.5),
    "opening_drive": ("threshold_bps", 40.0),
    "cross_sectional_residual": ("threshold_bps", 16.0),
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def content_identity(value: Any) -> str:
    """Return the full SHA-256 identity for one JSON-safe value."""
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _plain_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach caller-owned configuration into an immutable JSON projection."""
    return json.loads(_json(dict(value)))


def _policy_config(runtime_config: Mapping[str, Any]) -> dict[str, Any]:
    """Project the mounted execution policy without persisting credentials.

    The full resolved mapping is still content-addressed by
    ``runtime_config_identity``.  Arms retain only fields consulted by signal,
    setup, risk, replay, and cost code; broker credentials and unrelated LLM,
    notification, and scheduler settings never enter the shadow database.
    """
    projected = {
        key: runtime_config[key]
        for key in (
            "mode", "broker", "data", "session", "universe", "strategy",
            "risk", "execution", "costs", "research",
        )
        if key in runtime_config
    }
    broker = projected.get("broker")
    if isinstance(broker, Mapping):
        projected["broker"] = {
            key: value for key, value in broker.items()
            if key not in {"api_key", "secret_key"}
        }
    research = projected.get("research")
    if isinstance(research, Mapping):
        projected["research"] = {
            key: research[key]
            for key in (
                "enabled", "require_validated_variant",
                "backtest_bar_fallback",
            )
            if key in research
        }
    return _plain_mapping(projected)


def _arm_config(policy: Mapping[str, Any], spec: Mapping[str, Any], *,
                family: str, role: str, code_identity: str,
                runtime_config_identity: str, policy_config_identity: str,
                cohort_identity: str) -> dict[str, Any]:
    config = _plain_mapping(policy)
    prior_strategy = config.get("strategy")
    strategy = dict(prior_strategy) if isinstance(prior_strategy, Mapping) else {}
    strategy.update({
        "id": "rule",
        "version": "v1",
        "variant_id": rule_variant_id(spec),
        "execution_mode": "shares",
        "rule_spec": dict(spec),
    })
    config["strategy"] = strategy
    config["diagnostic_shadow"] = {
        "schema": DIAGNOSTIC_COHORT_SCHEMA,
        "diagnostic_only": True,
        "authorizing": False,
        "gate_eligible": False,
        "promotion_eligible": False,
        "online_fdr": False,
        "family": family,
        "role": role,
        "spec_identity": content_identity(spec),
        "runtime_config_identity": runtime_config_identity,
        "policy_config_identity": policy_config_identity,
        "code_identity": code_identity,
        "cohort_identity": cohort_identity,
    }
    return config


def _ibr_logical_arms(runtime: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Resolve the existing equity IBR registry through its runtime adapter."""
    base = _plain_mapping(runtime)
    strategy = dict(base.get("strategy") or {})
    strategy.update({"id": "ibr", "version": "v1",
                     "execution_mode": "shares"})
    strategy.pop("rule_spec", None)
    base["strategy"] = strategy
    registry = load_registry(Path(__file__).with_name("variants.yaml"))
    variants = [
        variant for variant in registry.values()
        if variant.strategy_id == "ibr" and "equity" in variant.vehicles
    ]
    if len(variants) != 7:
        raise RuntimeError(
            "diagnostic IBR cohort requires the seven registered equity variants")
    baseline = next(
        variant for variant in variants
        if variant.variant_id == "ibr.baseline")
    # Resolve contract defaults through the registry adapter before applying
    # dotted overrides.  This supports mounted rule configurations that omit
    # optional IBR fields without inventing a second set of defaults here.
    variant_base = apply_variant(baseline, base)
    arms: list[dict[str, Any]] = []
    for variant in variants:
        applied = apply_variant(variant, variant_base)
        policy = _policy_config(applied)
        configured_strategy = policy.get("strategy")
        if not isinstance(configured_strategy, Mapping):
            raise RuntimeError("diagnostic IBR strategy configuration is unavailable")
        overrides = list(variant.overrides.items())
        role = "baseline" if variant.variant_id == "ibr.baseline" else "variant"
        arms.append({
            "strategy_id": "ibr",
            "family": "ibr",
            "role": role,
            "base_version": variant.base_version,
            "variant_id": variant.variant_id,
            "variant_axis": None if not overrides else overrides[0][0],
            "variant_value": None if not overrides else overrides[0][1],
            "hypothesis": variant.hypothesis,
            "config": policy,
            "spec_identity": content_identity({
                "strategy_id": "ibr",
                "base_version": variant.base_version,
                "variant_id": variant.variant_id,
                "strategy": dict(configured_strategy),
            }),
        })
    return arms


def _ibr_arm_config(policy: Mapping[str, Any], *, family: str, role: str,
                    code_identity: str, runtime_config_identity: str,
                    policy_config_identity: str, cohort_identity: str,
                    spec_identity: str) -> dict[str, Any]:
    config = _plain_mapping(policy)
    config["diagnostic_shadow"] = {
        "schema": DIAGNOSTIC_COHORT_SCHEMA,
        "diagnostic_only": True,
        "authorizing": False,
        "gate_eligible": False,
        "promotion_eligible": False,
        "online_fdr": False,
        "family": family,
        "role": role,
        "spec_identity": spec_identity,
        "runtime_config_identity": runtime_config_identity,
        "policy_config_identity": policy_config_identity,
        "code_identity": code_identity,
        "cohort_identity": cohort_identity,
        "runtime_parity": {
            "shared_signal_setup_risk": True,
            "broker_only_equivalence": "unsupported",
            "unsupported_observations": list(IBR_BROKER_ONLY_LIMITS),
        },
    }
    return config


def _logical_arms() -> list[dict[str, Any]]:
    arms: list[dict[str, Any]] = []
    for slot, family in enumerate(RULE_FAMILIES):
        baseline = validate_rule_spec(
            template_hypothesis(slot, vehicle="equity").rule_spec)
        field, value = _ONE_FACTOR_VARIANTS[family]
        variant = validate_rule_spec({**baseline, field: value})
        changed = [key for key in sorted(set(baseline) | set(variant))
                   if baseline.get(key) != variant.get(key)]
        if changed != [field]:  # import-time catalog changes must fail closed
            raise RuntimeError(
                f"diagnostic variant for {family} is not one-factor: {changed}")
        for role, spec in (("baseline", baseline), ("variant", variant)):
            arms.append({
                "family": family,
                "slot": slot,
                "role": role,
                "variant_axis": None if role == "baseline" else field,
                "variant_value": None if role == "baseline" else value,
                "variant_id": rule_variant_id(spec),
                "rule_spec": dict(spec),
                "spec_identity": content_identity(spec),
            })
    return arms


def build_diagnostic_cohort(runtime_config: Mapping[str, Any], *,
                            code_identity: str,
                            include_ibr: bool = False) -> dict[str, Any]:
    """Build the fixed rule cohort and optionally its registered IBR arms."""
    if not isinstance(runtime_config, Mapping):
        raise TypeError("diagnostic shadow requires a mounted runtime config mapping")
    if not isinstance(include_ibr, bool):
        raise TypeError("include_ibr must be true or false")
    code_identity = str(code_identity).strip()
    if not code_identity:
        raise ValueError("diagnostic shadow code identity is required")
    runtime = _plain_mapping(runtime_config)
    policy = _policy_config(runtime)
    runtime_config_identity = content_identity(runtime)
    policy_config_identity = content_identity(policy)
    logical_arms = _logical_arms()
    if include_ibr:
        logical_arms.extend(_ibr_logical_arms(runtime))
    families = list(RULE_FAMILIES)
    if include_ibr:
        families.append("ibr")
    cohort_body = {
        "schema": DIAGNOSTIC_COHORT_SCHEMA,
        "diagnostic_only": True,
        "authorizing": False,
        "gate_eligible": False,
        "promotion_eligible": False,
        "online_fdr": False,
        "code_identity": code_identity,
        "runtime_config_identity": runtime_config_identity,
        "policy_config_identity": policy_config_identity,
        "families": families,
        "families_total": len(families),
        "baseline_count": sum(
            1 for arm in logical_arms if arm.get("role") == "baseline"),
        "variant_count": sum(
            1 for arm in logical_arms if arm.get("role") == "variant"),
        "registered_arms": len(logical_arms),
        "selection_policy": "fixed_preregistered_no_online_selection",
        "risk_cost_policy": "unchanged_mounted_runtime_policy",
        "logical_arms": logical_arms,
    }
    if include_ibr:
        cohort_body["include_ibr"] = True
    cohort_digest = content_identity(cohort_body)
    cohort_identity = f"{DIAGNOSTIC_CANDIDATE_PREFIX}cohort:{cohort_digest}"
    arms: list[dict[str, Any]] = []
    for logical in logical_arms:
        strategy_id = str(logical.get("strategy_id") or "rule")
        if strategy_id == "rule":
            config = _arm_config(
                policy, logical["rule_spec"], family=str(logical["family"]),
                role=str(logical["role"]), code_identity=code_identity,
                runtime_config_identity=runtime_config_identity,
                policy_config_identity=policy_config_identity,
                cohort_identity=cohort_identity)
        else:
            config = _ibr_arm_config(
                logical["config"], family=str(logical["family"]),
                role=str(logical["role"]), code_identity=code_identity,
                runtime_config_identity=runtime_config_identity,
                policy_config_identity=policy_config_identity,
                cohort_identity=cohort_identity,
                spec_identity=str(logical["spec_identity"]))
        config_identity = content_identity(config)
        config["diagnostic_shadow"]["config_identity"] = config_identity
        validate_contract_config(config)
        arm_identity = {
            "schema": "diagnostic-shadow-arm.v1",
            "cohort_identity": cohort_identity,
            "family": logical["family"],
            "role": logical["role"],
            "variant_id": logical["variant_id"],
            "spec_identity": logical["spec_identity"],
            # The identity is computed before embedding itself in the marker,
            # avoiding a recursive hash while making the value durable in the
            # candidate's persisted config_json.
            "config_identity": config_identity,
            "code_identity": code_identity,
        }
        candidate_id = f"{DIAGNOSTIC_CANDIDATE_PREFIX}{content_identity(arm_identity)}"
        arm = {
            "candidate_id": candidate_id,
            "strategy_id": strategy_id,
            "vehicle": "equity",
            "status": "diagnostic",
            "base_version": str(logical.get("base_version") or "v1"),
            "variant_id": logical["variant_id"],
            "family": logical["family"],
            "role": logical["role"],
            "variant_axis": logical["variant_axis"],
            "variant_value": logical["variant_value"],
            "spec_identity": logical["spec_identity"],
            "config_identity": arm_identity["config_identity"],
            "code_identity": code_identity,
            "cohort_identity": cohort_identity,
            "diagnostic_only": True,
            "authorizing": False,
            "config": config,
            "axes": {
                "diagnostic_shadow": True,
                "family": logical["family"],
                "role": logical["role"],
                "variant_axis": logical["variant_axis"],
                "cohort_identity": cohort_identity,
            },
        }
        if strategy_id == "rule":
            arm["rule_spec"] = logical["rule_spec"]
        else:
            arm["hypothesis"] = logical["hypothesis"]
            arm["runtime_parity"] = config["diagnostic_shadow"][
                "runtime_parity"]
        arms.append(arm)
    arms.sort(key=lambda arm: (
        str(arm["family"]), str(arm["role"]), str(arm["variant_id"])))
    result = dict(cohort_body)
    result.pop("logical_arms", None)
    result.update({
        "cohort_digest": cohort_digest,
        "cohort_identity": cohort_identity,
        "arms": arms,
        "candidate_identities": [str(arm["candidate_id"]) for arm in arms],
    })
    return result


def is_diagnostic_candidate(candidate: Mapping[str, Any] | str) -> bool:
    """Return whether a candidate belongs to the non-authorizing namespace."""
    if isinstance(candidate, str):
        return candidate.startswith(DIAGNOSTIC_CANDIDATE_PREFIX)
    candidate_id = str(candidate.get("candidate_id") or "")
    if candidate_id.startswith(DIAGNOSTIC_CANDIDATE_PREFIX):
        return True
    config = candidate.get("config")
    marker = config.get("diagnostic_shadow") if isinstance(config, Mapping) else None
    return isinstance(marker, Mapping) and marker.get("diagnostic_only") is True


__all__ = [
    "DIAGNOSTIC_ACTIVATION_SCHEMA", "DIAGNOSTIC_CANDIDATE_PREFIX",
    "DIAGNOSTIC_COHORT_SCHEMA", "build_diagnostic_cohort", "content_identity",
    "is_diagnostic_candidate",
]
