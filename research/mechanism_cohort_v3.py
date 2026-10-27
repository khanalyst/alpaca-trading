"""Diagnostic-only mechanism cohort v3: the v2 mechanisms, corrected.

The v1, v2, cost-compatible, deadline-compatible and refresh cohorts stay
frozen.  Each v3 arm is its ``intraday-mechanisms.v2`` parent moved to the
diagnostic V7 grammar with the opt-in corrections from the October 2026
signal review (docs/review-2026-10-15/reviews/handoff-A_signals.md).  Every
changed field is declared per arm with the finding it answers; everything
else, including v2's 1.5R trailing stop, is inherited unchanged.  No arm can
execute, be promoted or be selected automatically.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent.contracts.rule import (
    DEFAULT_RULE_SPEC, RULE_FAMILIES, RULE_SCHEMA_V7, rule_variant_id,
    rule_vehicle_executable, thesis_deadline_conflict, validate_rule_spec,
)
from .edge_ledger_store import content_hash
from .mechanism_cohort_v2 import mechanism_cohort as mechanism_cohort_v2


COHORT_ID = "intraday-mechanisms.v3"
# The configured admission floor: stressed_cost_scenario_bps 9.0 divided by
# max_stressed_cost_to_risk_ratio 0.30 (config.yaml:52-53).  Volatility-scaled
# stops sit above it wherever the symbol's horizon volatility does.
STOP_FLOOR_BPS = 30.0
# 15:30 New York, which fits every v3 entry window plus its maximum hold.
THESIS_EXIT_BEFORE_MINUTES = 30
RESIDUAL_BETA_SESSIONS = 5
VALUE_TARGETS = {
    "opening_range_fade": "session_vwap",
    "mean_reversion": "rolling_mean",
    "vwap_reversion": "session_vwap",
}

_HORIZON_STOP = ({"stop_mode": "horizon_atr"},
                 "A-02: stop_atr x the symbol's own ATR x sqrt(max_hold_bars)")
_FIRST_CROSS = ({"signal_event": "first_cross"},
                "A-04: signal only on the bar at which the predicate turns true")
_DEADLINE = ({"exit_before_minutes": THESIS_EXIT_BEFORE_MINUTES},
             "A-01: 15:30 thesis deadline instead of 10:00")
_SAME_CLOCK = ({"volume_confirmation_mode": "same_clock"},
               "A-10: volume confirmation against the same clock minute of "
               "five prior sessions")

# family: ordered (fields, reason) corrections applied to the v2 parent.
CORRECTIONS: dict[str, tuple[tuple[dict[str, Any], str], ...]] = {
    "opening_range_breakout": (_FIRST_CROSS, _HORIZON_STOP),
    "opening_range_fade": (
        _DEADLINE, _FIRST_CROSS,
        ({"stop_mode": "structure"},
         "A-05: stop one ATR beyond the failed-breakout extreme")),
    "momentum_continuation": (_FIRST_CROSS, _SAME_CLOCK, _HORIZON_STOP),
    "mean_reversion": (_DEADLINE, _HORIZON_STOP),
    "trend_pullback": (
        ({"confirmation": "volume", "volume_confirmation_mode": "same_clock"},
         "A-14: a same-clock participation filter that the trend predicate "
         "does not already apply"),
        _HORIZON_STOP),
    "volatility_breakout": (_FIRST_CROSS, _HORIZON_STOP),
    "volume_breakout": (
        ({"confirmation": "none"},
         "A-10: drop the current-feed volume confirmation; the family's "
         "same-clock RVOL is the volume test"),
        _FIRST_CROSS, _HORIZON_STOP),
    "vwap_reversion": (_DEADLINE, _HORIZON_STOP),
    "vwap_trend": (_HORIZON_STOP,),
    "range_expansion": (
        ({"range_multiplier": 2.0,
          "volume_multiplier": DEFAULT_RULE_SPEC["volume_multiplier"]},
         "A-15: the 2.0 range multiple moves to range_multiplier; "
         "volume_multiplier returns to its unused grammar default"),
        _HORIZON_STOP),
    "opening_drive": (_FIRST_CROSS, _HORIZON_STOP),
    "cross_sectional_residual": (
        ({"residual_mode": "beta",
          "residual_beta_sessions": RESIDUAL_BETA_SESSIONS},
         "A-09: residual net of a causal beta from the five prior sessions"),
        _HORIZON_STOP),
}


def _arm(parent: dict[str, Any], family: str) -> dict[str, Any]:
    authored = {**deepcopy(parent["rule_spec"]), "schema": RULE_SCHEMA_V7,
                "min_stop_distance_bps": STOP_FLOOR_BPS}
    corrections = []
    for fields, reason in CORRECTIONS[family]:
        authored.update(fields)
        corrections.append({"fields": sorted(fields), "reason": reason})
    if family in VALUE_TARGETS:
        corrections.append({
            "fields": [],
            "reason": "A-03: the value target "
                      f"({VALUE_TARGETS[family]}) is inherited from v2"})
    spec = validate_rule_spec(authored)
    if thesis_deadline_conflict(spec, require_full_window=True) is not None:
        raise ValueError(f"v3 {family} thesis deadline does not fit its window")
    if family in VALUE_TARGETS and spec["target_mode"] != VALUE_TARGETS[family]:
        raise ValueError(f"v3 {family} must target its value anchor")
    if rule_vehicle_executable(spec, "equity"):
        raise ValueError(f"v3 {family} must remain non-executable")
    parent_spec = parent["rule_spec"]
    changed = sorted(key for key in set(parent_spec) | set(spec)
                     if parent_spec.get(key) != spec.get(key))
    return {
        "arm": 0,
        "variant_id": rule_variant_id(spec),
        "rule_spec": spec,
        "parent_variant_id": parent["variant_id"],
        "fields_changed_from_parent": changed,
        "corrections": corrections,
    }


def mechanism_cohort(name: str = COHORT_ID) -> dict[str, Any]:
    if name != COHORT_ID:
        raise ValueError(f"unknown mechanism cohort: {name}")
    parent = mechanism_cohort_v2()
    families = []
    for item in parent["families"]:
        arm = _arm(item["arms"][0], item["name"])
        families.append({
            "name": item["name"],
            "thesis": item["thesis"],
            "arms": [arm],
        })
    if [family["name"] for family in families] != list(RULE_FAMILIES):
        raise ValueError("v3 cohort family order or inventory changed")
    if len({family["arms"][0]["variant_id"] for family in families}) != 12:
        raise ValueError("v3 cohort arms must be distinct")
    manifest = {
        "cohort_id": COHORT_ID,
        "families": families,
        "registered_arms": len(families),
        "parent_cohort_id": parent["cohort_id"],
        "parent_manifest_hash": parent["manifest_hash"],
        "selection_policy": "diagnostic_only_no_automatic_winner_selection",
        "risk_policy": "unchanged_runtime_limits_and_configured_stress_veto",
        "stop_floor_bps": STOP_FLOOR_BPS,
        "stop_floor_basis": (
            "config.yaml stressed_cost_scenario_bps 9.0 / "
            "max_stressed_cost_to_risk_ratio 0.30"),
        "comparison_policy": (
            "each arm is its v2 parent on the V7 grammar with the declared "
            "corrections; every other field is inherited"),
        "diagnostic_only": True,
        "authorizing": False,
        "runtime_config_changed": False,
        "limitations": [
            "V7 is a diagnostic grammar: no arm can generate a live signal "
            "or order",
            "the beta-adjusted residual needs prior-session subject and SPY "
            "bars in bars_by_symbol; research.factory_core passes the "
            "cross-sectional family only the current session's SPY prefix, "
            "so that arm fails closed (residual_beta_unavailable) in factory "
            "replay until the replay context supplies prior sessions",
            "same-clock volume needs five prior same-feed sessions in the "
            "replay context",
            "research.strategy_factory.run_factory dispatches only the v1 "
            "and v2 cohort ids",
            "historical diagnostics require unseen forward confirmation",
        ],
    }
    return {**manifest, "manifest_hash": content_hash(manifest)}


__all__ = ["COHORT_ID", "CORRECTIONS", "STOP_FLOOR_BPS",
           "THESIS_EXIT_BEFORE_MINUTES", "VALUE_TARGETS", "mechanism_cohort"]
