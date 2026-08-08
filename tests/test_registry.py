import unittest

from agent import registry
from agent.forward_models import MODELS, require_complete_contract


class IBRRegistryTests(unittest.TestCase):
    def test_only_ibr_alpha_is_registered(self):
        self.assertEqual(tuple(registry.REGISTRY), ("ibr",))
        self.assertEqual(registry.spec_for("ibr").setup_types,
                         ("ibr_breakout",))

    def test_contract_identity_and_forward_model_match(self):
        contract = registry.contract_for_variant("ibr", "ibr.baseline")
        self.assertEqual(contract.id, "ibr")
        self.assertEqual(contract.outcome_model.strategy_id, "ibr")
        self.assertEqual(require_complete_contract("ibr"), MODELS["ibr"])

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


if __name__ == "__main__":
    unittest.main()
