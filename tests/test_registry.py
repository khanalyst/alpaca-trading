import unittest

from agent import registry


class IBRRegistryTests(unittest.TestCase):
    def test_only_ibr_alpha_is_registered(self):
        self.assertEqual(tuple(registry.REGISTRY), ("ibr",))
        self.assertEqual(registry.spec_for("ibr").setup_types,
                         ("ibr_breakout",))

    def test_contract_identity_and_semantics_are_stable(self):
        contract = registry.contract_for_variant("ibr", "ibr.baseline")
        self.assertEqual(contract.id, "ibr")
        self.assertEqual(contract.variant_id, "ibr.baseline")
        self.assertEqual(contract.semantic_hash,
                         registry.contract_for_variant(
                             "ibr", "ibr.baseline").semantic_hash)

    def test_unknown_strategy_is_rejected(self):
        for name in ("other-alpha", "unregistered", "multi-vehicle",
                     "unknown"):
            with self.subTest(name=name):
                with self.assertRaises(registry.UnknownStrategy):
                    registry.spec_for(name)

    def test_variant_must_remain_in_ibr_namespace(self):
        with self.assertRaisesRegex(ValueError, "does not belong"):
            registry.validate_contract_config({"strategy": {
                "id": "ibr", "version": "v1", "variant_id": "momentum.baseline"}})
        with self.assertRaisesRegex(ValueError, "not pre-registered"):
            registry.validate_variant_id("ibr", "ibr.ad_hoc")


if __name__ == "__main__":
    unittest.main()
