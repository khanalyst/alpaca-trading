"""Stable candidate assumptions shared by research and runtime.

The replay lane (backtest versus shadow) is evidence provenance, not an alpha
assumption.  Candidate and run rows therefore hash the same validated runtime
assumptions object while lane-specific policy remains in the provenance body.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


_ASSUMPTION_KEYS = (
    "broker", "data", "session", "universe", "strategy", "risk",
    "execution", "costs", "llm",
)
_RUNTIME_CONFIG_KEYS = {
    "mode", "broker", "data", "session", "universe", "strategy", "risk",
    "execution", "costs", "llm", "research", "cycle",
}
# Paper/live is a deployment mode, not a strategy assumption.  Keeping it out
# lets one proved edge move through paper qualification into a separately
# guarded live process while still binding the complete feed entitlement.
_BROKER_KEYS = ("data_feed", "options_feed")
_STRATEGY_SELECTION_KEYS = {"selection_mode", "pinned"}


def candidate_assumptions(
    config: Mapping[str, Any] | None,
    *,
    vehicle: str,
    strategy_id: str,
    variant_id: str,
    rule_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the canonical, deployment-relevant candidate configuration.

    ``config`` is validated through the runtime contract so omitted defaults
    cannot create a second identity.  Mode, research scheduling, credentials,
    and selection/pinning controls are deliberately excluded: they select an
    already-proved edge or describe how evidence was collected, but do not
    change the executable strategy assumptions.  The candidate's strategy
    parameters, costs, risk, session, universe, and complete feed identity do
    remain part of the immutable hash.
    """
    if vehicle not in {"equity", "option"}:
        raise ValueError("vehicle must be equity or option")
    if not str(strategy_id).strip() or not str(variant_id).strip():
        raise ValueError("strategy_id and variant_id are required")

    # Import lazily to keep this small identity module usable by both research
    # and the runtime edge resolver without introducing an import cycle.
    from agent.config import validate_config

    # ``_effective_ibr_config`` carries a small replay-only helper block;
    # discard it before handing the object to the strict runtime validator.
    source = {key: deepcopy(value) for key, value in dict(config or {}).items()
              if key in _RUNTIME_CONFIG_KEYS}
    # Validation of the assumptions object must not depend on process mode or
    # the live-enable environment variable.  Feed/session/risk/cost fields are
    # retained below; paper is only the neutral validation envelope.
    source["mode"] = "paper"
    broker_source = dict(source.get("broker") or {})
    broker_source.update({"paper": True, "allow_live": False})
    source["broker"] = broker_source
    costs_source = source.get("costs")
    if isinstance(costs_source, Mapping):
        # CostModel.as_dict() also exposes rejection-cap diagnostics.  Those
        # are represented by the validated execution block, not the runtime
        # costs schema, so keep only the economic schedule here.
        cost_keys = ("spread_bps", "slippage_bps", "fee_bps",
                     "option_fee_per_contract_side", "provenance")
        source["costs"] = {
            key: deepcopy(costs_source[key])
            for key in cost_keys if key in costs_source
        }
        # Measured quote economics are part of the immutable candidate
        # assumptions.  The normalized block contains the schedule (or its
        # verified hash/path reference), percentile/depth policy, source
        # identity, and strict coverage policy.  Omitting it would let a
        # later shadow/runtime replay silently use different costs while the
        # candidate hash still matched.
        if (isinstance(costs_source.get("measured_quote"), Mapping) and
                costs_source["measured_quote"].get("enabled") is True):
            source["costs"]["measured_quote"] = deepcopy(
                costs_source["measured_quote"])
        # An explicitly configured vehicle schedule is part of the immutable
        # proof assumptions.  It is intentionally absent from this projection
        # for legacy flat configs, preserving their historical hashes exactly.
        if "vehicles" in costs_source:
            source["costs"]["vehicles"] = deepcopy(costs_source["vehicles"])
    strategy = dict(source.get("strategy") or {})
    strategy["id"] = str(strategy_id)
    strategy["variant_id"] = str(variant_id)
    strategy.setdefault("version", "v1")
    if strategy_id == "rule":
        if not isinstance(rule_spec, Mapping):
            raise ValueError("rule candidate assumptions require rule_spec")
        strategy["rule_spec"] = deepcopy(dict(rule_spec))
        strategy["execution_mode"] = (
            "options" if vehicle == "option" else "shares")
    elif vehicle == "option":
        strategy["execution_mode"] = "options"
    else:
        strategy["execution_mode"] = "shares"
    # Selection is a runtime orchestration choice and must not fork the edge
    # identity as it moves from a specific proof to all-proved/pinned paper.
    for key in _STRATEGY_SELECTION_KEYS:
        strategy.pop(key, None)
    source["strategy"] = strategy

    # IBR replay has three execution parameters that intentionally are not
    # exposed as live rule-strategy knobs.  Keep them in a namespaced part of
    # the immutable assumptions rather than silently dropping them or feeding
    # unknown fields to the runtime validator.
    strategy_replay = {
        "range_stop": bool(strategy.get("range_stop", True)),
        "stop_pct": float(strategy.get("stop_pct", .003)),
        "target_pct": float(strategy.get("target_pct", .006)),
    }
    for key in strategy_replay:
        strategy.pop(key, None)

    # Validate the complete runtime shape.  A research config may omit a
    # universe for a compact unit test; the runtime defaults still produce the
    # same explicit feed/session/risk/cost assumptions as a deployed config.
    validated = validate_config(source)
    assumptions: dict[str, Any] = {
        "schema": "candidate-assumptions.v1",
        "vehicle": str(vehicle),
    }
    for key in _ASSUMPTION_KEYS:
        value = validated.get(key)
        if key == "broker" and isinstance(value, Mapping):
            # Never hash credentials, endpoints, or the paper/live mode.  The
            # entitled feed identity is the executable broker assumption.
            value = {name: value.get(name) for name in _BROKER_KEYS}
        if value is not None:
            assumptions[key] = deepcopy(value)
    assumptions["strategy_replay"] = strategy_replay
    return assumptions


__all__ = ["candidate_assumptions"]
