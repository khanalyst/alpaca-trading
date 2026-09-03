import unittest

from agent.contracts.rule import (RULE_FAMILIES, RULE_SCHEMA_V2, RULE_SCHEMA_V4,
                                   V2_DEFAULT_EXTENSIONS, V3_DEFAULT_EXTENSIONS,
                                   V4_DEFAULT_EXTENSIONS,
                                   rule_semantic_signature, rule_variant_id)
from research.factory_core import (
    MAX_DISCOVERY_ATTEMPTS,
    _DISCOVERY_BANDS,
    _DISCOVERY_BREAKEVEN_FRACTIONS,
    _DISCOVERY_CONFIRMATIONS,
    _DISCOVERY_SHAPES,
    _DISCOVERY_WINDOWS,
    coordinate_mutation_pool, discovery_attempt_limit, discovery_spec, family_template,
    spec_delta, template_hypothesis,
)


class DiscoveryLadderTests(unittest.TestCase):
    def test_cap_covers_one_complete_cartesian_traversal(self):
        self.assertEqual(
            MAX_DISCOVERY_ATTEMPTS,
            len(_DISCOVERY_WINDOWS) * len(_DISCOVERY_CONFIRMATIONS) *
            len(_DISCOVERY_BANDS) * len(_DISCOVERY_SHAPES) *
            len(_DISCOVERY_BREAKEVEN_FRACTIONS),
        )
        self.assertEqual(discovery_attempt_limit("equity"),
                         MAX_DISCOVERY_ATTEMPTS)
        self.assertEqual(
            discovery_attempt_limit("option"),
            MAX_DISCOVERY_ATTEMPTS // len(_DISCOVERY_BREAKEVEN_FRACTIONS))

    def test_every_family_and_ladder_index_is_reachable_and_unique(self):
        ids = [
            rule_variant_id(discovery_spec(index, family=family))
            for family in RULE_FAMILIES
            for index in range(1, MAX_DISCOVERY_ATTEMPTS + 1)
        ]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_equity_root_is_v4_and_exposes_bounded_exit_axes(self):
        conditional = {*V2_DEFAULT_EXTENSIONS, *V3_DEFAULT_EXTENSIONS}
        exit_fields = set(V4_DEFAULT_EXTENSIONS)
        for slot, family in enumerate(RULE_FAMILIES):
            with self.subTest(family=family):
                root = template_hypothesis(slot).rule_spec
                self.assertEqual(root["schema"], RULE_SCHEMA_V4)
                self.assertTrue(conditional | exit_fields <= set(root))
                legacy = {key: value for key, value in root.items()
                          if key not in conditional | exit_fields and key != "schema"}
                self.assertEqual(
                    rule_semantic_signature(root),
                    rule_semantic_signature(legacy),
                )
                pool = coordinate_mutation_pool(root, {})
                changed_axes = {
                    next(iter(spec_delta(root, candidate)))
                    for candidate, _reason in pool[1:]
                    if len(spec_delta(root, candidate)) == 1
                }
                self.assertTrue(conditional | exit_fields <= changed_axes)
                self.assertTrue(any(
                    "stop_atr" in spec_delta(root, candidate)
                    and float(candidate["stop_atr"]) >= 4.0
                    for candidate, _reason in pool[1:]))
                breakevens = {
                    candidate["breakeven_r"]
                    for candidate, _reason in pool[1:]
                    if "breakeven_r" in spec_delta(root, candidate)
                }
                self.assertIn(0.0, breakevens)
                self.assertTrue(all(value < root["target_r"]
                                    for value in breakevens))
                self.assertTrue(any(
                    candidate.get("target_mode") != root["target_mode"]
                    for candidate, _reason in pool[1:]))

    def test_family_exit_candidates_are_early_without_changing_neutral_roots(self):
        trend = template_hypothesis(0).rule_spec
        reversion = template_hypothesis(3).rule_spec
        for root in (trend, reversion):
            self.assertEqual(
                {key: root[key] for key in V4_DEFAULT_EXTENSIONS},
                V4_DEFAULT_EXTENSIONS)
        trend_pool = coordinate_mutation_pool(trend, {})
        reversion_pool = coordinate_mutation_pool(reversion, {})
        self.assertEqual(spec_delta(trend, trend_pool[1][0]), {
            "trailing_stop_r": {"from": None, "to": 1.5},
        })
        self.assertEqual(spec_delta(reversion, reversion_pool[1][0]), {
            "target_mode": {"from": "fixed_r", "to": "rolling_mean"},
        })
        self.assertGreaterEqual(sum(
            candidate["target_mode"] == "fixed_r"
            for candidate, _reason in reversion_pool[:4]), 2)
        trend_holds = [candidate["max_hold_bars"]
                       for candidate, _reason in trend_pool
                       if "max_hold_bars" in spec_delta(trend, candidate)]
        reversion_holds = [candidate["max_hold_bars"]
                           for candidate, _reason in reversion_pool
                           if "max_hold_bars" in spec_delta(reversion, candidate)]
        self.assertIn(240, trend_holds)
        self.assertIn(15, reversion_holds)
        self.assertEqual(
            discovery_attempt_limit("equity"), MAX_DISCOVERY_ATTEMPTS)

    def test_option_roots_and_discovery_remain_executable_v2(self):
        root = template_hypothesis(0, vehicle="option").rule_spec
        discovered = discovery_spec(1, family=root["family"], vehicle="option")
        self.assertEqual(root["schema"], RULE_SCHEMA_V2)
        self.assertEqual(discovered["schema"], RULE_SCHEMA_V2)
        self.assertNotIn("breakeven_r", root)
        self.assertNotIn("breakeven_r", discovered)

    def test_option_discovery_traversal_has_no_breakeven_axis_duplicates(self):
        limit = discovery_attempt_limit("option")
        ids = [rule_variant_id(discovery_spec(
            index, family="mean_reversion", vehicle="option"))
               for index in range(1, limit + 1)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_equity_discovery_reaches_every_bounded_breakeven_fraction(self):
        values = {
            discovery_spec(index, family="mean_reversion")["breakeven_r"] /
            discovery_spec(index, family="mean_reversion")["target_r"]
            for index in range(1, MAX_DISCOVERY_ATTEMPTS + 1)
        }
        self.assertEqual(values, set(_DISCOVERY_BREAKEVEN_FRACTIONS))

    def test_discovery_shapes_are_the_fastest_varying_dimension(self):
        first = discovery_spec(1, family="mean_reversion")
        second = discovery_spec(2, family="mean_reversion")
        self.assertNotEqual(
            (first["side"], first["target_r"], first["stop_atr"],
             first["max_hold_bars"]),
            (second["side"], second["target_r"], second["stop_atr"],
             second["max_hold_bars"]))

    def test_stressed_cost_diagnosis_reaches_the_full_min_atr_ladder(self):
        root = template_hypothesis(0).rule_spec
        pool = coordinate_mutation_pool(
            root, {"primary_failure": "execution_blocked"})
        values = {float(candidate["min_atr_bps"])
                  for candidate, _reason in pool
                  if "min_atr_bps" in spec_delta(root, candidate)}
        self.assertIn(50.0, values)
        self.assertIn(2_000.0, values)


if __name__ == "__main__":
    unittest.main()
