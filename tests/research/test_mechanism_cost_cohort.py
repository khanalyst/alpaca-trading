import unittest

from agent.contracts.rule import RULE_SCHEMA_V7, rule_vehicle_executable
from research.mechanism_cohort_v2 import mechanism_cohort
from research.mechanism_cost_cohort import mechanism_cost_cohort


class CostCompatibleCohortTests(unittest.TestCase):
    def test_floor_is_the_only_experimental_change_to_each_frozen_parent(self):
        frozen = mechanism_cohort()
        self.assertEqual(frozen["manifest_hash"],
            "41cb0233046d280c32e38f9af0af846debb4015baa52f4940a2822eb6ae59748")
        new = mechanism_cost_cohort()
        self.assertEqual(new["registered_arms"], 12)
        self.assertTrue(new["diagnostic_only"])
        self.assertFalse(new["authorizing"])
        self.assertFalse(new["runtime_config_changed"])
        ids = set()
        for old_family, new_family in zip(frozen["families"], new["families"]):
            old, arm = old_family["arms"][0], new_family["arms"][0]
            spec = dict(arm["rule_spec"])
            self.assertEqual(spec.pop("min_stop_distance_bps"), 100.0)
            self.assertEqual(spec.pop("schema"), RULE_SCHEMA_V7)
            inherited = dict(old["rule_spec"])
            inherited.pop("schema")
            self.assertEqual(spec, inherited)
            self.assertEqual(arm["parent_variant_id"], old["variant_id"])
            self.assertEqual(arm["inherited_max_hold_bars"], spec["max_hold_bars"])
            self.assertNotEqual(arm["variant_id"], old["variant_id"])
            self.assertFalse(rule_vehicle_executable(arm["rule_spec"], "equity"))
            ids.add(arm["variant_id"])
        self.assertEqual(len(ids), 12)


if __name__ == "__main__":
    unittest.main()
