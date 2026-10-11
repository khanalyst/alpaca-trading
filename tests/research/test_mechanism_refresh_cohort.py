"""Contract tests for the additive mechanism refresh diagnostic cohort."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from agent.contracts.rule import RULE_FAMILIES, rule_variant_id, validate_rule_spec
from research.mechanism_cost_cohort import mechanism_cost_cohort
from research.mechanism_deadline_cohort import mechanism_deadline_cohort
from research.mechanism_refresh_cohort import (
    COHORT_ID,
    IBR_COMPARISON_VARIANT_IDS,
    MIN_STOP_DISTANCE_BPS,
    mechanism_refresh_cohort,
)
import research.mechanism_refresh_cohort as refresh_module


EXPECTED_DELTAS = {
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


class MechanismRefreshCohortTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_complete(self):
        first = mechanism_refresh_cohort()
        second = mechanism_refresh_cohort()
        self.assertEqual(first, second)
        self.assertEqual(first["cohort_id"], COHORT_ID)
        self.assertEqual(first["registered_arms"], 12)
        self.assertEqual(
            [family["name"] for family in first["families"]], list(RULE_FAMILIES)
        )
        self.assertEqual(
            first["cohort_manifest_content_hash"], first["manifest_hash"]
        )
        self.assertTrue(first["diagnostic_only"])
        self.assertFalse(first["authorizing"])
        self.assertEqual(first["selection_policy"], "none")
        self.assertFalse(first["runtime_config_changed"])
        self.assertTrue(first["retrospective_hypotheses"])
        self.assertTrue(first["forward_confirmation_required"])
        self.assertFalse(first["optimization_sweep"])

    def test_each_child_is_valid_unique_and_changes_only_declared_field(self):
        manifest = mechanism_refresh_cohort()
        ids = set()
        for family in manifest["families"]:
            name = family["name"]
            arm = family["arms"][0]
            field, before, after = EXPECTED_DELTAS[name]
            spec = validate_rule_spec(arm["rule_spec"])
            parent = validate_rule_spec(arm["parent_spec"])
            self.assertEqual(rule_variant_id(spec), arm["variant_id"])
            self.assertNotIn(arm["variant_id"], ids)
            self.assertNotEqual(arm["variant_id"], arm["parent_variant_id"])
            ids.add(arm["variant_id"])
            changed = [
                key for key in sorted(set(parent) | set(spec))
                if parent.get(key) != spec.get(key)
            ]
            self.assertEqual(changed, [field], name)
            self.assertEqual(parent[field], before)
            self.assertEqual(spec[field], after)
            self.assertEqual(
                arm["declared_mechanism_delta"],
                {field: {"before": before, "after": after}},
            )
            self.assertTrue(family["economic_thesis"].strip())
        self.assertEqual(len(ids), 12)

    def test_reversion_parents_are_deadline_variants_and_geometry_is_inherited(self):
        manifest = mechanism_refresh_cohort()
        cost = mechanism_cost_cohort()
        deadline = mechanism_deadline_cohort()
        cost_by_family = {item["name"]: item for item in cost["families"]}
        deadline_by_family = {item["name"]: item for item in deadline["families"]}
        for family in manifest["families"]:
            arm = family["arms"][0]
            parent = arm["parent_spec"]
            child = arm["rule_spec"]
            source = deadline_by_family if family["name"] in {
                "opening_range_fade", "mean_reversion", "vwap_reversion"
            } else cost_by_family
            self.assertEqual(arm["parent_cohort_id"], (
                deadline["cohort_id"] if family["name"] in deadline_by_family
                else cost["cohort_id"]
            ))
            self.assertEqual(arm["parent_variant_id"], source[family["name"]]["arms"][0]["variant_id"])
            self.assertEqual(child["min_stop_distance_bps"], MIN_STOP_DISTANCE_BPS)
            for field in (
                "target_r", "max_hold_bars", "exit_before_minutes", "trailing_stop_r",
            ):
                self.assertEqual(child[field], parent[field], field)
            if family["name"] in deadline_by_family:
                self.assertEqual(parent["exit_before_minutes"], 30)

    def test_parent_snapshots_and_raw_specs_do_not_share_mutable_state(self):
        before_cost = deepcopy(mechanism_cost_cohort())
        before_deadline = deepcopy(mechanism_deadline_cohort())
        first = mechanism_refresh_cohort()
        first["families"][0]["arms"][0]["rule_spec"]["lookback"] = 999
        first["families"][0]["arms"][0]["parent_spec"]["lookback"] = 998
        first["families"][0]["arms"][0]["declared_mechanism_delta"][
            "opening_hold_bars"
        ]["after"] = 998
        second = mechanism_refresh_cohort()
        self.assertEqual(second["families"][0]["arms"][0]["rule_spec"]["lookback"],
                         before_cost["families"][0]["arms"][0]["rule_spec"]["lookback"])
        self.assertEqual(second["families"][0]["arms"][0]["parent_spec"]["lookback"],
                         before_cost["families"][0]["arms"][0]["rule_spec"]["lookback"])
        self.assertEqual(second["families"][0]["arms"][0]["declared_mechanism_delta"][
            "opening_hold_bars"
        ]["after"], 3)
        self.assertEqual(mechanism_cost_cohort(), before_cost)
        self.assertEqual(mechanism_deadline_cohort(), before_deadline)

    def test_ibr_catalog_and_primary_contrast_are_existing_ids(self):
        manifest = mechanism_refresh_cohort()
        self.assertEqual(tuple(manifest["ibr_comparison_variant_ids"]),
                         IBR_COMPARISON_VARIANT_IDS)
        contrast = manifest["ibr_primary_contrast"]
        self.assertEqual(contrast["baseline_variant_id"], "ibr.baseline")
        self.assertEqual(contrast["contrast_variant_id"], "ibr.buffer.10bps")
        self.assertEqual(contrast["before"], 5.0)
        self.assertEqual(contrast["after"], 10.0)
        self.assertIn("opening_drive", manifest["primary_research_priorities"])
        self.assertIn("momentum_continuation", manifest["primary_research_priorities"])
        self.assertIn("cross_sectional_residual", manifest["primary_research_priorities"])
        self.assertIn("opening_range_breakout", manifest["primary_research_priorities"])

    def test_parent_drift_fails_closed(self):
        drifted = deepcopy(mechanism_cost_cohort())
        drifted["families"][0]["arms"][0]["rule_spec"]["lookback"] = 16
        with patch.object(refresh_module, "mechanism_cost_cohort",
                          return_value=drifted):
            with self.assertRaisesRegex(ValueError, "parent identity"):
                mechanism_refresh_cohort()


if __name__ == "__main__":
    unittest.main()
