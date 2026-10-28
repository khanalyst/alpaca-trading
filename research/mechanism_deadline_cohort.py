"""Diagnostic-only deadline-compatible mechanism variants.

The cost-compatible V7 cohort is the frozen parent inventory.  This manifest
adds one timing-only child for each reversion family whose entry window fits a
15:30 New York thesis deadline.  No source epoch, runtime configuration, or
selection policy is changed by this diagnostic cohort.
"""
from __future__ import annotations

from agent.contracts.rule import RULE_SCHEMA_V7, rule_variant_id, validate_rule_spec

from .edge_ledger_store import content_hash
from .mechanism_cohort_v2 import ERRATA_SCHEMA, REVIEW_SOURCE
from .mechanism_cost_cohort import STRESS_SCENARIO_ERRATUM, mechanism_cost_cohort


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
    # Outside the hash: the refresh cohort pins this manifest_hash.
    errata = {
        "schema": ERRATA_SCHEMA,
        "covered_by_manifest_hash": False,
        "source": REVIEW_SOURCE,
        "corrects": [{
            "finding": "A-01",
            "statement": (
                "exit_before_minutes counts minutes before the 16:00 close. "
                "The parents' 360 is a 10:00 New York thesis deadline, which "
                "made the VWAP-reversion parent unable to enter and left the "
                "fade and mean-reversion parents a few minutes; this cohort's "
                "30 is a 15:30 deadline that fits every entry window and "
                "maximum hold."),
        }],
        "label_corrections": [{**STRESS_SCENARIO_ERRATUM,
                               "recorded_value": manifest["risk_policy"]}],
    }
    return {**manifest, "manifest_hash": content_hash(manifest),
            "errata": errata}


__all__ = ["COHORT_ID", "mechanism_deadline_cohort"]
