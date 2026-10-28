"""Diagnostic-only one-coordinate refresh hypotheses.

The cost-compatible V7 cohort and its deadline-compatible reversion children
are frozen inputs.  This module derives one deterministic child per rule
family, changing exactly one authored mechanism field while inheriting the
parent's execution geometry.  The resulting catalog is for offline,
retrospective diagnostics; it cannot select, authorize, or change the live
paper baseline.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from agent.contracts.rule import (
    RULE_FAMILIES,
    RULE_SCHEMA_V7,
    rule_variant_id,
    validate_rule_spec,
)

from .edge_ledger_store import content_hash
from .mechanism_cost_cohort import mechanism_cost_cohort
from .mechanism_deadline_cohort import mechanism_deadline_cohort


COHORT_ID = "intraday-mechanisms.refresh.v1"
MIN_STOP_DISTANCE_BPS = 100.0

_COST_COHORT_ID = "intraday-mechanisms.cost-compatible.v1"
_DEADLINE_COHORT_ID = "intraday-mechanisms.deadline-compatible.v1"
_COST_MANIFEST_HASH = (
    "f3f7cb105125541d52a5f9184ede43af5f6e0e3a69dff9eb9fc91685b9f0ab00"
)
_DEADLINE_MANIFEST_HASH = (
    "fb4b6667c80661d494d27ea64e639c8a92ba09cb74d190f1625a436e260c242d"
)

# Pin the existing parent identities.  Keeping these values in the refresh
# manifest makes a parent edit an explicit, fail-closed review event instead
# of silently changing the meaning of a child variant.
_EXPECTED_COST_PARENT_IDS = {
    "opening_range_breakout": "rule.opening-range-breakout.2aa6eecce57d9c56",
    "opening_range_fade": "rule.opening-range-fade.8a6efdff14783e2b",
    "momentum_continuation": "rule.momentum-continuation.bdc87e262eb6fb8a",
    "mean_reversion": "rule.mean-reversion.264ff151b4541339",
    "trend_pullback": "rule.trend-pullback.203b7d4417ea9bda",
    "volatility_breakout": "rule.volatility-breakout.0a3e27f02d6c32e2",
    "volume_breakout": "rule.volume-breakout.3f366a2a931587ad",
    "vwap_reversion": "rule.vwap-reversion.6907a109eaf5f882",
    "vwap_trend": "rule.vwap-trend.23428d749bda8288",
    "range_expansion": "rule.range-expansion.4b22b60b28b8c96d",
    "opening_drive": "rule.opening-drive.0d82e82b09466fa9",
    "cross_sectional_residual": "rule.cross-sectional-residual.a4c9876c220419b0",
}
_EXPECTED_DEADLINE_PARENT_IDS = {
    "opening_range_fade": "rule.opening-range-fade.ab1c2b7f9fa16248",
    "mean_reversion": "rule.mean-reversion.78f80534776d6a4b",
    "vwap_reversion": "rule.vwap-reversion.4128a7b49bb1a8d1",
}

# These are the authored one-coordinate changes.  Values are deliberately
# kept separate from the parent manifests so the declaration itself is
# inspectable and can be compared to the normalized specs below.
MECHANISM_DELTAS: dict[str, tuple[str, Any, Any]] = {
    "opening_range_breakout": ("opening_hold_bars", 2, 3),
    "opening_range_fade": ("threshold_bps", 8.0, 12.0),
    "momentum_continuation": ("lookback", 12, 20),
    "mean_reversion": ("zscore", 1.5, 2.0),
    "trend_pullback": ("regime_efficiency_threshold", 0.4, 0.6),
    "volatility_breakout": ("compression_atr_multiple", 3.0, 2.0),
    "volume_breakout": ("volume_multiplier", 1.5, 2.0),
    "vwap_reversion": ("threshold_bps", 25.0, 35.0),
    "vwap_trend": ("vwap_slope_window", 5, 10),
    "range_expansion": ("body_fraction_min", 0.5, 0.65),
    "opening_drive": ("opening_hold_bars", 3, 4),
    "cross_sectional_residual": ("lookback", 15, 30),
}

_DEADLINE_FAMILIES = frozenset({
    "opening_range_fade",
    "mean_reversion",
    "vwap_reversion",
})

_EXPECTED_PARENT_VALUES: dict[str, dict[str, Any]] = {
    "opening_range_breakout": {
        "opening_hold_bars": 2,
        "exit_before_minutes": None,
        "max_hold_bars": 30,
    },
    "opening_range_fade": {
        "threshold_bps": 8.0,
        "exit_before_minutes": 30,
        "max_hold_bars": 30,
    },
    "momentum_continuation": {
        "lookback": 12,
        "exit_before_minutes": None,
        "max_hold_bars": 30,
    },
    "mean_reversion": {
        "zscore": 1.5,
        "exit_before_minutes": 30,
        "max_hold_bars": 30,
    },
    "trend_pullback": {
        "regime_efficiency_threshold": 0.4,
        "exit_before_minutes": None,
        "max_hold_bars": 60,
    },
    "volatility_breakout": {
        "compression_atr_multiple": 3.0,
        "exit_before_minutes": None,
        "max_hold_bars": 30,
    },
    "volume_breakout": {
        "volume_multiplier": 1.5,
        "exit_before_minutes": None,
        "max_hold_bars": 30,
    },
    "vwap_reversion": {
        "threshold_bps": 25.0,
        "exit_before_minutes": 30,
        "max_hold_bars": 15,
    },
    "vwap_trend": {
        "vwap_slope_window": 5,
        "exit_before_minutes": None,
        "max_hold_bars": 30,
    },
    "range_expansion": {
        "body_fraction_min": 0.5,
        "exit_before_minutes": None,
        "max_hold_bars": 30,
    },
    "opening_drive": {
        "opening_hold_bars": 3,
        "exit_before_minutes": None,
        "max_hold_bars": 30,
    },
    "cross_sectional_residual": {
        "lookback": 15,
        "exit_before_minutes": None,
        "max_hold_bars": 30,
    },
}

ECONOMIC_THESES: dict[str, str] = {
    "opening_range_breakout": (
        "Requiring a third close beyond the opening range tests whether ORB "
        "follow-through survives an extra confirmation bar."
    ),
    "opening_range_fade": (
        "A wider failed-break threshold tests whether only more displaced "
        "opening-range excursions revert toward session VWAP."
    ),
    "momentum_continuation": (
        "A longer completed-bar lookback tests whether continuation is a "
        "persistent move rather than a short-lived impulse."
    ),
    "mean_reversion": (
        "A larger z-score entry threshold tests whether stronger dislocations "
        "improve reversion after the unchanged cost floor."
    ),
    "trend_pullback": (
        "A stricter efficiency threshold tests whether pullbacks work better "
        "inside cleaner five-minute trends."
    ),
    "volatility_breakout": (
        "A tighter ATR compression multiple tests whether breakout energy is "
        "more informative after deeper compression."
    ),
    "volume_breakout": (
        "A higher same-clock volume multiple tests whether unusually strong "
        "participation improves breakout follow-through."
    ),
    "vwap_reversion": (
        "A wider VWAP displacement threshold tests whether only larger "
        "dislocations have enough room to mean-revert after costs."
    ),
    "vwap_trend": (
        "A longer rolling VWAP slope window tests whether trend direction "
        "persists beyond the short window."
    ),
    "range_expansion": (
        "A larger candle-body fraction tests whether directional expansion "
        "is more reliable when the body dominates the bar range."
    ),
    "opening_drive": (
        "Requiring a fourth close beyond the drive endpoint tests whether the "
        "opening drive persists after an additional confirmation bar."
    ),
    "cross_sectional_residual": (
        "A longer residual lookback tests whether relative strength persists "
        "across a broader completed-bar context."
    ),
}

IBR_COMPARISON_VARIANT_IDS = (
    "ibr.baseline",
    "ibr.range.30",
    "ibr.range.45",
    "ibr.target.1_5r",
    "ibr.target.3r",
    "ibr.buffer.0bps",
    "ibr.buffer.10bps",
)


def _source_manifest_map() -> dict[str, dict[str, Any]]:
    """Load and verify the two immutable parent manifests."""

    cost = mechanism_cost_cohort()
    deadline = mechanism_deadline_cohort()
    if cost.get("cohort_id") != _COST_COHORT_ID:
        raise ValueError("refresh cohort cost parent cohort changed")
    if cost.get("manifest_hash") != _COST_MANIFEST_HASH:
        raise ValueError("refresh cohort cost parent manifest changed")
    if deadline.get("cohort_id") != _DEADLINE_COHORT_ID:
        raise ValueError("refresh cohort deadline parent cohort changed")
    if deadline.get("manifest_hash") != _DEADLINE_MANIFEST_HASH:
        raise ValueError("refresh cohort deadline parent manifest changed")
    if list(family.get("name") for family in cost.get("families", [])) != list(RULE_FAMILIES):
        raise ValueError("refresh cohort cost parent family order changed")
    if list(family.get("name") for family in deadline.get("families", [])) != [
        "opening_range_fade", "mean_reversion", "vwap_reversion"
    ]:
        raise ValueError("refresh cohort deadline parent family order changed")
    return {"cost": cost, "deadline": deadline}


def _parent_for_family(
    family: str,
    sources: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_name = "deadline" if family in _DEADLINE_FAMILIES else "cost"
    source = sources[source_name]
    parent_family = next(
        (item for item in source["families"] if item.get("name") == family),
        None,
    )
    if not isinstance(parent_family, Mapping):
        raise ValueError(f"refresh cohort missing parent family: {family}")
    arms = parent_family.get("arms")
    if not isinstance(arms, list) or len(arms) != 1 or not isinstance(arms[0], Mapping):
        raise ValueError(f"refresh cohort parent family must have one arm: {family}")
    return dict(source), dict(arms[0])


def _verified_parent_spec(
    family: str,
    source: Mapping[str, Any],
    parent_arm: Mapping[str, Any],
) -> dict[str, Any]:
    expected_id = (
        _EXPECTED_DEADLINE_PARENT_IDS[family]
        if family in _DEADLINE_FAMILIES
        else _EXPECTED_COST_PARENT_IDS[family]
    )
    if parent_arm.get("variant_id") != expected_id:
        raise ValueError(f"refresh cohort parent variant changed: {family}")
    raw_spec = parent_arm.get("rule_spec")
    normalized = validate_rule_spec(raw_spec)
    if normalized != raw_spec:
        raise ValueError(f"refresh cohort parent spec is not normalized: {family}")
    if normalized.get("schema") != RULE_SCHEMA_V7:
        raise ValueError(f"refresh cohort parent schema changed: {family}")
    if rule_variant_id(normalized) != expected_id:
        raise ValueError(f"refresh cohort parent identity does not match spec: {family}")
    if normalized.get("family") != family:
        raise ValueError(f"refresh cohort parent family changed: {family}")
    if normalized.get("min_stop_distance_bps") != MIN_STOP_DISTANCE_BPS:
        raise ValueError(f"refresh cohort parent stop floor changed: {family}")
    for field, expected in _EXPECTED_PARENT_VALUES[family].items():
        if normalized.get(field) != expected:
            raise ValueError(
                f"refresh cohort expected parent value changed: {family}.{field}"
            )
    if family in _DEADLINE_FAMILIES:
        # Reversion parents come from the deadline-compatible cohort, whose
        # 15:30 thesis deadline is 30 minutes before the close.
        if normalized.get("exit_before_minutes") != 30:
            raise ValueError(f"refresh cohort reversion parent deadline changed: {family}")
    elif normalized.get("exit_before_minutes") is not None:
        # Every other cost parent carries the neutral V4 deadline (none).
        raise ValueError(f"refresh cohort non-reversion parent deadline changed: {family}")
    if source.get("cohort_id") not in {_COST_COHORT_ID, _DEADLINE_COHORT_ID}:
        raise ValueError(f"refresh cohort parent source changed: {family}")
    return deepcopy(normalized)


def _build_family(
    family: str,
    source: Mapping[str, Any],
    parent_arm: Mapping[str, Any],
) -> dict[str, Any]:
    parent_spec = _verified_parent_spec(family, source, parent_arm)
    field, before, after = MECHANISM_DELTAS[family]
    if parent_spec.get(field) != before:
        raise ValueError(f"refresh cohort declared before value changed: {family}.{field}")
    child_spec = validate_rule_spec({**deepcopy(parent_spec), field: after})
    child_id = rule_variant_id(child_spec)
    if child_id == parent_arm["variant_id"]:
        raise ValueError(f"refresh cohort child identity did not change: {family}")
    changed = [
        key for key in sorted(set(parent_spec) | set(child_spec))
        if parent_spec.get(key) != child_spec.get(key)
    ]
    if changed != [field]:
        raise ValueError(f"refresh cohort child is not one-field: {family}: {changed}")
    declared_delta = {field: {"before": deepcopy(before), "after": deepcopy(after)}}
    thesis = ECONOMIC_THESES[family]
    return {
        "name": family,
        "thesis": str(source_family_thesis(source, family)),
        "economic_thesis": thesis,
        "parent_cohort_id": source["cohort_id"],
        "parent_manifest_hash": source["manifest_hash"],
        "declared_mechanism_delta": deepcopy(declared_delta),
        "mechanism_delta": deepcopy(declared_delta),
        "arms": [{
            "arm": 0,
            "variant_id": child_id,
            "rule_spec": deepcopy(child_spec),
            "parent_variant_id": parent_arm["variant_id"],
            "parent_cohort_id": source["cohort_id"],
            "parent_manifest_hash": source["manifest_hash"],
            "source_cohort_id": source["cohort_id"],
            "parent_rule_spec": deepcopy(parent_spec),
            "parent_spec": deepcopy(parent_spec),
            "declared_mechanism_delta": deepcopy(declared_delta),
            "mechanism_delta": deepcopy(declared_delta),
            "economic_thesis": thesis,
            "inherited_min_stop_distance_bps": child_spec["min_stop_distance_bps"],
            "inherited_target_r": child_spec["target_r"],
            "inherited_max_hold_bars": child_spec["max_hold_bars"],
            "inherited_exit_before_minutes": child_spec["exit_before_minutes"],
            "inherited_trailing_stop_r": child_spec["trailing_stop_r"],
        }],
    }


def source_family_thesis(source: Mapping[str, Any], family: str) -> str:
    """Return the parent's descriptive thesis, failing closed on bad input."""

    for item in source.get("families", []):
        if item.get("name") == family and isinstance(item.get("thesis"), str):
            return item["thesis"]
    raise ValueError(f"refresh cohort parent thesis missing: {family}")


def mechanism_refresh_cohort(name: str = COHORT_ID) -> dict[str, Any]:
    """Return the deterministic, non-authorizing refresh diagnostic cohort."""

    if name != COHORT_ID:
        raise ValueError(f"unknown mechanism refresh cohort: {name}")
    sources = _source_manifest_map()
    families: list[dict[str, Any]] = []
    for family in RULE_FAMILIES:
        source, parent_arm = _parent_for_family(family, sources)
        families.append(_build_family(family, source, parent_arm))
    if [family["name"] for family in families] != list(RULE_FAMILIES):
        raise ValueError("refresh cohort family order or inventory changed")

    primary_ibr_contrast = {
        "baseline_variant_id": "ibr.baseline",
        "contrast_variant_id": "ibr.buffer.10bps",
        "axis": "strategy.breakout_buffer_bps",
        "before": 5.0,
        "after": 10.0,
        "hypothesis": (
            "Compare the existing 10 bps IBR buffer against the unchanged "
            "registered baseline as the primary IBR contrast."
        ),
    }
    body: dict[str, Any] = {
        "cohort_id": COHORT_ID,
        "families": families,
        "registered_arms": len(families),
        "source_cost_cohort_id": _COST_COHORT_ID,
        "source_deadline_cohort_id": _DEADLINE_COHORT_ID,
        "source_cost_manifest_hash": sources["cost"]["manifest_hash"],
        "source_deadline_manifest_hash": sources["deadline"]["manifest_hash"],
        "source_cost_manifest_content_hash": sources["cost"]["manifest_hash"],
        "source_deadline_manifest_content_hash": sources["deadline"]["manifest_hash"],
        "source_manifest_hashes": {
            "cost_compatible": sources["cost"]["manifest_hash"],
            "deadline_compatible": sources["deadline"]["manifest_hash"],
        },
        "parent_cohort_by_family": {
            family["name"]: family["arms"][0]["parent_cohort_id"]
            for family in families
        },
        "diagnostic_only": True,
        "authorizing": False,
        "selection_policy": "none",
        "selection": "none",
        "runtime_config_changed": False,
        "retrospective_only": True,
        "retrospective_hypotheses": True,
        "forward_confirmation_required": True,
        "forward_confirmation": "required_before_authorization",
        "optimization_sweep": False,
        "no_optimization_sweep": True,
        "primary_research_priorities": [
            "opening_drive",
            "momentum_continuation",
            "cross_sectional_residual",
            "opening_range_breakout",
        ],
        "primary_research_priority": [
            "opening_drive",
            "momentum_continuation",
            "cross_sectional_residual",
            "opening_range_breakout",
        ],
        "ibr_comparison_variant_ids": list(IBR_COMPARISON_VARIANT_IDS),
        "ibr_primary_contrast": deepcopy(primary_ibr_contrast),
        "primary_ibr_contrast": deepcopy(primary_ibr_contrast),
        "risk_policy": "inherited_100bps_floor_and_exit_geometry",
        "risk_geometry": {
            "min_stop_distance_bps": MIN_STOP_DISTANCE_BPS,
            "target_r": "inherited",
            "max_hold_bars": "inherited",
            "exit_before_minutes": "inherited",
            "trailing_stop_r": "inherited",
        },
        "limitations": [
            "all hypotheses are retrospective diagnostics",
            "forward confirmation is required before any authorization",
            "the seven existing IBR comparison ids remain unchanged",
            "no optimization sweep or automatic winner selection is permitted",
        ],
    }
    digest = content_hash(body)
    body["cohort_manifest_content_hash"] = digest
    # Keep the repository-wide manifest spelling as an alias while exposing
    # the more explicit content-hash field required by this cohort contract.
    body["manifest_hash"] = digest
    return body


__all__ = [
    "COHORT_ID",
    "ECONOMIC_THESES",
    "IBR_COMPARISON_VARIANT_IDS",
    "MECHANISM_DELTAS",
    "MIN_STOP_DISTANCE_BPS",
    "mechanism_refresh_cohort",
]
