import unittest

from agent.contracts.rule import (RULE_FAMILIES, RULE_SCHEMA_V2,
                                   V2_DEFAULT_EXTENSIONS,
                                   rule_semantic_signature, rule_variant_id)
from research.factory_core import (
    MAX_DISCOVERY_ATTEMPTS,
    _DISCOVERY_BANDS,
    _DISCOVERY_CONFIRMATIONS,
    _DISCOVERY_SHAPES,
    _DISCOVERY_WINDOWS,
    coordinate_mutation_pool, discovery_spec, family_template,
    spec_delta, template_hypothesis,
)


class DiscoveryLadderTests(unittest.TestCase):
    def test_cap_covers_one_complete_cartesian_traversal(self):
        self.assertEqual(
            MAX_DISCOVERY_ATTEMPTS,
            len(_DISCOVERY_WINDOWS) * len(_DISCOVERY_CONFIRMATIONS) *
            len(_DISCOVERY_BANDS) * len(_DISCOVERY_SHAPES),
        )

    def test_every_family_and_ladder_index_is_reachable_and_unique(self):
        ids = [
            rule_variant_id(discovery_spec(index, family=family))
            for family in RULE_FAMILIES
            for index in range(1, MAX_DISCOVERY_ATTEMPTS + 1)
        ]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_factory_root_is_v2_and_exposes_conditional_axes(self):
        conditional = set(V2_DEFAULT_EXTENSIONS)
        for slot, family in enumerate(RULE_FAMILIES):
            with self.subTest(family=family):
                root = template_hypothesis(slot).rule_spec
                self.assertEqual(root["schema"], RULE_SCHEMA_V2)
                self.assertTrue(conditional <= set(root))
                legacy = {key: value for key, value in root.items()
                          if key not in conditional and key != "schema"}
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
                self.assertTrue(conditional <= changed_axes)
                self.assertTrue(any(
                    "stop_atr" in spec_delta(root, candidate)
                    and float(candidate["stop_atr"]) >= 4.0
                    for candidate, _reason in pool[1:]))


if __name__ == "__main__":
    unittest.main()
