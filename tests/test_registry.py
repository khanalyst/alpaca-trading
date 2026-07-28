import unittest

from agent import registry
from agent.registry import (LIVE_MIN_TIER, REGISTRY, StrategySpec, TIERS,
                            UnknownStrategy, implemented_ids,
                            live_eligible_ids, spec_for)


def spec(**overrides):
    base = {
        "id": "example",
        "version": "v1",
        "mechanism": "someone pays and cannot stop",
        "falsification": "the effect vanishes under a placebo",
        "tier": "T1_HYPOTHESIS",
        "signal_timeframe": "15m",
        "required_timeframes": ("15m", "1h"),
        "max_hold_hours_ceiling": 24.0,
        "execution_style": "taker",
        "setup_types": ("thing",),
    }
    base.update(overrides)
    return StrategySpec(**base)


class SpecValidationTests(unittest.TestCase):
    """A spec that cannot state its own claim must not construct."""

    def test_mechanism_is_mandatory(self):
        with self.assertRaisesRegex(ValueError, "must state a mechanism"):
            spec(mechanism="   ")

    def test_falsification_is_mandatory(self):
        with self.assertRaisesRegex(ValueError, "must state a falsification"):
            spec(falsification="")

    def test_unknown_tier_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown tier"):
            spec(tier="probably_fine")

    def test_unknown_execution_style_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown execution style"):
            spec(execution_style="hopeful")

    def test_signal_timeframe_must_be_collected(self):
        with self.assertRaisesRegex(
                ValueError, "not in its required timeframes"):
            spec(signal_timeframe="1m")


class TierLadderTests(unittest.TestCase):
    def test_ladder_is_ordered_worst_to_best(self):
        ranks = [TIERS.index(t) for t in TIERS]
        self.assertEqual(ranks, sorted(ranks))

    def test_meets_compares_up_the_ladder(self):
        candidate = spec(tier="T2_CANDIDATE")
        self.assertTrue(candidate.meets("T1_HYPOTHESIS"))
        self.assertTrue(candidate.meets("T2_CANDIDATE"))
        self.assertFalse(candidate.meets("T3_VALIDATED"))


class RegisterContentTests(unittest.TestCase):
    def test_every_registered_strategy_states_its_claim(self):
        for strategy_id, entry in REGISTRY.items():
            with self.subTest(strategy=strategy_id):
                self.assertTrue(entry.mechanism.strip())
                self.assertTrue(entry.falsification.strip())
                self.assertIn(entry.tier, TIERS)

    def test_registry_is_keyed_by_its_own_ids(self):
        for key, entry in REGISTRY.items():
            self.assertEqual(key, entry.id)

    def test_momentum_is_the_rejected_benchmark(self):
        # The audit measured it. Recording that verdict in code is what
        # keeps it from being promoted by accident later.
        momentum = spec_for("momentum")
        self.assertEqual(momentum.tier, "T0_REJECTED")
        self.assertTrue(momentum.implemented)
        self.assertFalse(momentum.meets(LIVE_MIN_TIER))

    def test_every_implemented_strategy_declares_shadow_parameters(self):
        # A strategy shadowed with whatever the live config happens to hold
        # produces forward evidence attributable to nothing.
        for strategy_id in implemented_ids():
            with self.subTest(strategy=strategy_id):
                self.assertTrue(spec_for(strategy_id).contract_params)

    def test_scalp_maker_stays_unimplemented(self):
        # Blocked on recorded order-book data, not on development effort.
        self.assertNotIn("scalp-maker", implemented_ids())

    def test_nothing_is_live_eligible_yet(self):
        # If this ever fails, a strategy has been promoted to T3 and the
        # change should be deliberate and evidenced.
        self.assertEqual(live_eligible_ids(), ())

    def test_unknown_id_names_the_alternatives(self):
        with self.assertRaises(UnknownStrategy) as caught:
            spec_for("nope")
        self.assertIn("momentum", str(caught.exception))

    def test_scalping_is_registered_as_maker_only(self):
        # Round-trip taker cost exceeds a typical 1m move, so a taker scalp
        # is dead arithmetic rather than a tuning problem.
        self.assertEqual(spec_for("scalp-maker").execution_style, "maker")

    def test_multi_day_strategies_declare_longer_ceilings(self):
        self.assertGreater(spec_for("funding-carry").max_hold_hours_ceiling,
                           spec_for("momentum").max_hold_hours_ceiling)


class ContractCoverageTests(unittest.TestCase):
    def test_every_implemented_strategy_has_a_contract(self):
        from agent.contracts import EVIDENCE_BUILDERS
        for strategy_id in implemented_ids():
            with self.subTest(strategy=strategy_id):
                self.assertIn(strategy_id, EVIDENCE_BUILDERS)

    def test_no_contract_exists_without_a_registry_entry(self):
        from agent.contracts import EVIDENCE_BUILDERS
        for strategy_id in EVIDENCE_BUILDERS:
            with self.subTest(strategy=strategy_id):
                self.assertIn(strategy_id, registry.REGISTRY)


if __name__ == "__main__":
    unittest.main()
