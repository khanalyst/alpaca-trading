import threading
import unittest

from agent import shadow, variants
from tests.helpers import valid_config
from tests.research.test_shadow_isolation import proposal, snapshot, variant


class ShadowParallelTests(unittest.TestCase):
    def test_independent_variants_overlap_without_sharing_account_state(self):
        evaluator = shadow.ShadowEvaluator(
            [variant(), variant("momentum.rr.fixed_3_0",
                                {"strategy.fixed_reward_risk": 3.0})],
            valid_config(), workers=2)
        entered = threading.Barrier(2)
        original = evaluator._evaluate_one
        thread_ids = set()

        def observed(*args, **kwargs):
            thread_ids.add(threading.get_ident())
            entered.wait(timeout=2)
            return original(*args, **kwargs)

        evaluator._evaluate_one = observed
        records = evaluator.evaluate(
            snapshot(), now=1_760_000_000.0, proposals=[proposal()])

        self.assertEqual({record.variant_id for record in records}, {
            "momentum.rr.fixed_2_5", "momentum.rr.fixed_3_0"})
        self.assertGreaterEqual(len(thread_ids), 2)
        states = [evaluator.store.load_paper_portfolio(
            evaluator.scope_key, item, evaluator.initial_balance_usdt, 1_760_000_001)
                  for item in evaluator.variant_ids]
        self.assertEqual(len({version for _, version in states}), 1)
        self.assertGreaterEqual(states[0][1], 1)

    def test_adaptive_cycle_selects_one_setting_per_strategy(self):
        static = next(
            item for item in variants.hypothesis_variants(
                "momentum", "phase1-v3")
            if item.variant_id.endswith(".lower_participation"))
        exact = variants.adaptive_hypothesis_variant(
            "momentum", "phase1-v3", "volume-thrust",
            "lower_participation", 1.3)
        evaluator = shadow.ShadowEvaluator([static, exact], valid_config())

        selected = evaluator._scheduled_variant_ids([{
            "action": "research_proposal",
            "hypothesis_id": "volume-thrust",
            "setting_id": "lower_participation",
            "value": 1.3,
            "variant_id": exact.variant_id,
        }])

        self.assertEqual(selected, [exact.variant_id])

    def test_default_remains_serial_and_compatible(self):
        evaluator = shadow.ShadowEvaluator([variant()], valid_config())
        self.assertEqual(evaluator.workers, 1)
        records = evaluator.evaluate(
            snapshot(), now=1_760_000_000.0, proposals=[proposal()])
        self.assertTrue(records)


if __name__ == "__main__":
    unittest.main()
