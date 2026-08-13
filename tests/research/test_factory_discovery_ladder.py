import unittest

from agent.contracts.rule import RULE_FAMILIES, rule_variant_id
from research.factory_core import (
    MAX_DISCOVERY_ATTEMPTS,
    _DISCOVERY_BANDS,
    _DISCOVERY_CONFIRMATIONS,
    _DISCOVERY_SHAPES,
    _DISCOVERY_WINDOWS,
    discovery_spec,
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


if __name__ == "__main__":
    unittest.main()
