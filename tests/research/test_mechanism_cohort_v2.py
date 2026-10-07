"""Frozen inventory and diagnostic execution contract for mechanism v2."""

import unittest

from agent.contracts.rule import RULE_SCHEMA_V6
from research.mechanism_cohort import mechanism_cohort as mechanism_cohort_v1
from research.mechanism_cohort_v2 import COHORT_ID, mechanism_cohort


class MechanismCohortV2Tests(unittest.TestCase):
    def test_v1_identity_and_inventory_are_unchanged(self):
        frozen = mechanism_cohort_v1()
        self.assertEqual(frozen["cohort_id"], "intraday-mechanisms.v1")
        self.assertEqual(frozen["registered_arms"], 12)
        self.assertEqual(
            frozen["manifest_hash"],
            "13c2b58b22cd5f669d26d9f7611c4067304f6390b9e1666bf83821aa0393abe6",
        )

    def test_v2_has_one_diagnostic_arm_per_family(self):
        manifest = mechanism_cohort()
        self.assertEqual(manifest["cohort_id"], COHORT_ID)
        self.assertEqual(manifest["registered_arms"], 12)
        self.assertTrue(manifest["diagnostic_only"])
        self.assertFalse(manifest["authorizing"])
        self.assertEqual(sum(len(item["arms"]) for item in manifest["families"]), 12)
        specs = [item["arms"][0]["rule_spec"] for item in manifest["families"]]
        self.assertEqual({item["family"] for item in specs}, {
            "opening_range_breakout", "opening_range_fade",
            "momentum_continuation", "mean_reversion", "trend_pullback",
            "volatility_breakout", "volume_breakout", "vwap_reversion",
            "vwap_trend", "range_expansion", "opening_drive",
            "cross_sectional_residual",
        })
        for spec in specs:
            self.assertEqual(spec["schema"], RULE_SCHEMA_V6)
            self.assertEqual(spec["atr_period"], 14)
            self.assertEqual(spec["stop_atr"], 1.0)
            self.assertEqual(spec["target_r"], 2.0)
            self.assertEqual(spec["side"], "both")

    def test_causal_fields_and_descriptive_labels_are_preregistered(self):
        manifest = mechanism_cohort()
        by_family = {item["name"]: item for item in manifest["families"]}
        self.assertEqual(by_family["opening_range_breakout"]["arms"][0]
                         ["rule_spec"]["opening_hold_bars"], 2)
        self.assertEqual(by_family["opening_drive"]["arms"][0]["rule_spec"]
                         ["opening_hold_bars"], 3)
        self.assertEqual(by_family["volatility_breakout"]["arms"][0]["rule_spec"]
                         ["compression_atr_multiple"], 3.0)
        volume_spec = by_family["volume_breakout"]["arms"][0]["rule_spec"]
        self.assertEqual(volume_spec["relative_volume_mode"], "same_clock")
        self.assertEqual(volume_spec["relative_volume_sessions"], 5)
        self.assertIn("same-clock RVOL",
                      by_family["volume_breakout"]["thesis"])
        vwap_spec = by_family["vwap_trend"]["arms"][0]["rule_spec"]
        self.assertEqual(vwap_spec["vwap_slope_mode"], "rolling")
        self.assertEqual(vwap_spec["vwap_slope_window"], 5)
        self.assertIn("rolling VWAP slope",
                      by_family["vwap_trend"]["thesis"])
        self.assertIn("not hedged",
                      by_family["cross_sectional_residual"]["thesis"])


if __name__ == "__main__":
    unittest.main()
