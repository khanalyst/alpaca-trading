"""Additive stop-geometry hypotheses; original mechanism arms stay frozen."""
from __future__ import annotations

from agent.contracts.rule import RULE_SCHEMA_V7, rule_variant_id, validate_rule_spec
from .edge_ledger_store import content_hash
from .mechanism_cohort_v2 import (
    ERRATA_SCHEMA, REVIEW_SOURCE, mechanism_cohort, thesis_deadline_errata,
)

COHORT_ID = "intraday-mechanisms.cost-compatible.v1"
AUTHORED_STOP_FLOOR_BPS = 100.0
# Finding A-12: the frozen risk_policy label names a 25 bps stress scenario.
STRESS_SCENARIO_ERRATUM = {
    "field": "risk_policy",
    "finding": "A-12",
    "statement": (
        "The 25 bps stressed cost scenario in this label is the value of the "
        "paper profile activated on 2026-09-15 "
        "(docs/paper-activation-2026-09-15.json:119). Every shipped "
        "configuration (config.yaml:52, deploy/paper-orb.config.json:79, "
        "deploy/paper-orf.config.json:79) sets stressed_cost_scenario_bps "
        "9.0 with max_stressed_cost_to_risk_ratio 0.30, so the configured "
        "admission floor is 9 / 0.30 = 30 bps, not 25 / 0.30 = 83 bps. The "
        "100 bps authored floor is therefore not required for admission "
        "under the shipped configuration."),
}


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
        # Stale label, frozen: the runtime rung has been 9 bps
        # (risk.stressed_cost_scenario_bps) since commit 2249cc5.  The text
        # is part of the manifest hash pinned by mechanism_refresh_cohort.py,
        # so it is not edited; read the rung from the configuration.
        "risk_policy": "unchanged_25bps_stress_and_0.30_cost_to_risk_veto",
        "comparison_policy": "only_authored_floor_changes_targets_and_hold_horizons_inherited",
        "runtime_config_changed": False,
        "limitations": ["wider_stops_do_not_prove_edge_or_target_attainability",
                         "quote_gaps_and_invalid_tick_targets_remain_refusals",
                         "historical_diagnostics_require_unseen_forward_confirmation"]}
    # Outside the hash: the refresh cohort pins this manifest_hash.
    errata = {
        "schema": ERRATA_SCHEMA,
        "covered_by_manifest_hash": False,
        "source": REVIEW_SOURCE,
        "invalid_arms": thesis_deadline_errata(
            families, corrected_by="intraday-mechanisms.deadline-compatible.v1"),
        "label_corrections": [{**STRESS_SCENARIO_ERRATUM,
                               "recorded_value": manifest["risk_policy"]}],
    }
    return {**manifest, "manifest_hash": content_hash(manifest),
            "errata": errata}


__all__ = ["COHORT_ID", "STRESS_SCENARIO_ERRATUM", "mechanism_cost_cohort"]
