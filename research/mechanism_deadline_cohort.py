"""Diagnostic-only deadline-compatible mechanism variants.

The cost-compatible V7 cohort is the frozen parent inventory.  This manifest
adds one timing-only child for each reversion family whose entry window fits a
15:30 New York thesis deadline.  No source epoch, runtime configuration, or
selection policy is changed by this diagnostic cohort.
"""
from __future__ import annotations

from agent.contracts.rule import RULE_SCHEMA_V7, rule_variant_id, validate_rule_spec

from .edge_ledger_store import content_hash
from .mechanism_cost_cohort import mechanism_cost_cohort


COHORT_ID = "intraday-mechanisms.deadline-compatible.v1"
PARENT_FAMILIES = frozenset({
    "opening_range_fade",
    "mean_reversion",
    "vwap_reversion",
})
PARENT_EXIT_BEFORE_MINUTES = 360
DEADLINE_EXIT_BEFORE_MINUTES = 30


def _timing_delta() -> dict[str, dict[str, dict[str, int]]]:
    """Return a fresh timing delta, including its nested mapping."""

    return {
        "exit_before_minutes": {
            "before": PARENT_EXIT_BEFORE_MINUTES,
            "after": DEADLINE_EXIT_BEFORE_MINUTES,
        },
    }


def mechanism_deadline_cohort() -> dict:
    """Return the deterministic, non-authorizing deadline diagnostic cohort."""

    parent = mechanism_cost_cohort()
    families = []
    for parent_family in parent["families"]:
        if parent_family["name"] not in PARENT_FAMILIES:
            continue
        original = parent_family["arms"][0]
        parent_spec = validate_rule_spec(original["rule_spec"])
        if parent_spec["schema"] != RULE_SCHEMA_V7:
            raise ValueError("deadline cohort requires V7 cost-compatible parents")
        if parent_spec["exit_before_minutes"] != PARENT_EXIT_BEFORE_MINUTES:
            raise ValueError("deadline cohort parent deadline is not the frozen 360-minute cutoff")

        spec = validate_rule_spec({
            **parent_spec,
            "exit_before_minutes": DEADLINE_EXIT_BEFORE_MINUTES,
        })
        arm = {
            "arm": 0,
            "variant_id": rule_variant_id(spec),
            "rule_spec": spec,
            "parent_variant_id": original["variant_id"],
            "timing_delta": _timing_delta(),
            "inherited_max_hold_bars": spec["max_hold_bars"],
        }
        families.append({
            "name": parent_family["name"],
            "thesis": parent_family["thesis"],
            "arms": [arm],
        })

    if [family["name"] for family in families] != [
            "opening_range_fade", "mean_reversion", "vwap_reversion"]:
        raise ValueError("deadline cohort family order or inventory changed")

    manifest = {
        "cohort_id": COHORT_ID,
        "families": families,
        "parent_cohort_id": parent["cohort_id"],
        "parent_manifest_hash": parent["manifest_hash"],
        "registered_arms": 3,
        "diagnostic_only": True,
        "authorizing": False,
        "selection_policy": "diagnostic_only_no_automatic_winner_selection",
        # Stale label, frozen: the runtime rung has been 9 bps
        # (risk.stressed_cost_scenario_bps) since commit 2249cc5.  The text
        # is part of the manifest hash pinned by mechanism_refresh_cohort.py,
        # so it is not edited; read the rung from the configuration.
        "risk_policy": "unchanged_25bps_stress_and_0.30_cost_to_risk_veto",
        "comparison_policy": (
            "only_authored_exit_deadline_changes; schema, stop floor, targets, "
            "max holds, and entry windows inherited"),
        "runtime_config_changed": False,
        "limitations": [
            "deadline-compatible timing does not prove edge or target attainability",
            "quote gaps and invalid tick targets remain refusals",
            "historical diagnostics require unseen forward confirmation",
        ],
    }
    return {**manifest, "manifest_hash": content_hash(manifest)}


__all__ = ["COHORT_ID", "mechanism_deadline_cohort"]
