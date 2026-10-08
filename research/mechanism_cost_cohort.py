"""Additive stop-geometry hypotheses; original mechanism arms stay frozen."""
from __future__ import annotations

from agent.contracts.rule import RULE_SCHEMA_V7, rule_variant_id, validate_rule_spec
from .edge_ledger_store import content_hash
from .mechanism_cohort_v2 import mechanism_cohort

COHORT_ID = "intraday-mechanisms.cost-compatible.v1"
AUTHORED_STOP_FLOOR_BPS = 100.0


def mechanism_cost_cohort() -> dict:
    source = mechanism_cohort()
    families = []
    for family in source["families"]:
        original = family["arms"][0]
        spec = validate_rule_spec({**original["rule_spec"],
            "schema": RULE_SCHEMA_V7,
            "min_stop_distance_bps": AUTHORED_STOP_FLOOR_BPS})
        arm = {"arm": 0, "variant_id": rule_variant_id(spec), "rule_spec": spec,
               "parent_variant_id": original["variant_id"],
               "geometry_delta": {"min_stop_distance_bps": AUTHORED_STOP_FLOOR_BPS},
               "inherited_max_hold_bars": spec["max_hold_bars"]}
        families.append({"name": family["name"], "thesis": family["thesis"],
            "arms": [arm]})
    manifest = {"cohort_id": COHORT_ID, "families": families,
        "source_cohort_id": source["cohort_id"],
        "source_manifest_hash": source["manifest_hash"], "registered_arms": 12,
        "diagnostic_only": True, "authorizing": False,
        "selection_policy": "diagnostic_only_no_automatic_winner_selection",
        "risk_policy": "unchanged_25bps_stress_and_0.30_cost_to_risk_veto",
        "comparison_policy": "only_authored_floor_changes_targets_and_hold_horizons_inherited",
        "runtime_config_changed": False,
        "limitations": ["wider_stops_do_not_prove_edge_or_target_attainability",
                         "quote_gaps_and_invalid_tick_targets_remain_refusals",
                         "historical_diagnostics_require_unseen_forward_confirmation"]}
    return {**manifest, "manifest_hash": content_hash(manifest)}


__all__ = ["COHORT_ID", "mechanism_cost_cohort"]
