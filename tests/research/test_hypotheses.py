"""6.3: what `other` should have been.

`other` failed as an experimentation mechanism for two structural reasons,
and the tests here hold the fixes for both.

It was an unlabelled bucket, so every distinct idea pooled into one row that
described none of them. Each registered hypothesis now carries its own
attribution, so ten experiments produce ten rows.

And it bypassed the evidence contract entirely, so a demo agent traded a
population live would silently refuse. Every hypothesis now has a contract -
permissive is allowed, absent is not.
"""

import unittest

from agent import brain, hypotheses
from tests.helpers import valid_config


class RegisterTests(unittest.TestCase):
    def test_every_hypothesis_states_a_mechanism_and_a_falsifier(self):
        """A hypothesis that cannot state both is a guess."""
        self.assertTrue(hypotheses.REGISTRY)
        for hypothesis_id, spec in hypotheses.REGISTRY.items():
            self.assertGreater(len(spec.mechanism), 40, hypothesis_id)
            self.assertGreater(len(spec.falsifier), 40, hypothesis_id)
            self.assertGreater(len(spec.claim), 20, hypothesis_id)

    def test_every_hypothesis_has_a_contract(self):
        """Permissive is allowed. Absent is what `other` was."""
        for hypothesis_id, spec in hypotheses.REGISTRY.items():
            self.assertTrue(callable(spec.contract), hypothesis_id)

    def test_every_hypothesis_has_a_registered_point_and_settings_axis(self):
        for hypothesis_id, spec in hypotheses.REGISTRY.items():
            with self.subTest(hypothesis=hypothesis_id):
                self.assertTrue(spec.contract_params)
                self.assertGreaterEqual(len(spec.settings), 3)
                self.assertEqual(spec.settings[0]["id"], "registered")

    def test_settings_are_testable_without_mutating_the_registered_point(self):
        snapshot = {"relative_volume_1h": 1.6, "mom_1h_pct": 0.5}
        cfg = valid_config()
        self.assertTrue(hypotheses.evaluate(
            "volume-thrust", snapshot, "long", cfg)[0])
        self.assertFalse(hypotheses.evaluate(
            "volume-thrust", snapshot, "long", cfg,
            {"min_relative_volume": 2.0})[0])
        self.assertTrue(hypotheses.evaluate(
            "volume-thrust", snapshot, "long", cfg)[0])

    def test_ids_are_their_own_keys(self):
        for key, spec in hypotheses.REGISTRY.items():
            self.assertEqual(key, spec.id)

    def test_attribution_is_distinct_per_hypothesis(self):
        seen = {spec.attribution("momentum")[0]
                for spec in hypotheses.REGISTRY.values()}
        self.assertEqual(len(seen), len(hypotheses.REGISTRY))

    def test_attribution_is_scoped_to_the_strategy(self):
        spec = hypotheses.spec_for("volume-thrust")
        strategy_id, _ = spec.attribution("momentum")
        self.assertEqual(strategy_id, "momentum-hyp-volume-thrust")


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = valid_config()

    def test_an_unregistered_id_never_fires(self):
        fires, why = hypotheses.evaluate(
            "something-i-invented", {}, "long", self.cfg)

        self.assertFalse(fires)
        self.assertIn("not registered", why)
        self.assertIn("volume-thrust", why)     # names what is available

    def test_an_empty_id_never_fires(self):
        fires, _ = hypotheses.evaluate("", {}, "long", self.cfg)
        self.assertFalse(fires)

    def test_volume_thrust_requires_participation(self):
        fires, why = hypotheses.evaluate(
            "volume-thrust", {"relative_volume_1h": 1.2, "mom_1h_pct": 0.5},
            "long", self.cfg)

        self.assertFalse(fires)
        self.assertIn("below 1.5", why)

    def test_volume_thrust_requires_the_matching_direction(self):
        snapshot = {"relative_volume_1h": 2.0, "mom_1h_pct": 0.5}

        self.assertTrue(hypotheses.evaluate(
            "volume-thrust", snapshot, "long", self.cfg)[0])
        self.assertFalse(hypotheses.evaluate(
            "volume-thrust", snapshot, "short", self.cfg)[0])

    def test_missing_fields_do_not_fire_and_do_not_raise(self):
        for hypothesis_id in hypotheses.REGISTRY:
            fires, why = hypotheses.evaluate(
                hypothesis_id, {}, "long", self.cfg)
            self.assertFalse(fires, hypothesis_id)
            self.assertTrue(why, hypothesis_id)

    def test_oi_divergence_needs_a_falling_open_interest(self):
        rising = {"mom_1h_pct": 0.8, "oi_change_4h_pct": 2.0}
        falling = {"mom_1h_pct": 0.8, "oi_change_4h_pct": -2.0}

        self.assertFalse(hypotheses.evaluate(
            "oi-divergence", rising, "short", self.cfg)[0])
        self.assertTrue(hypotheses.evaluate(
            "oi-divergence", falling, "short", self.cfg)[0])

    def test_basis_stretch_requires_enough_funding_samples(self):
        thin = {"perp_index_basis_pct": 0.2, "funding_percentile_30": 90,
                "funding_samples_30": 3}

        fires, why = hypotheses.evaluate(
            "basis-stretch", thin, "short", self.cfg)

        self.assertFalse(fires)
        self.assertIn("funding samples", why)


class PromptTests(unittest.TestCase):
    def test_every_registered_hypothesis_appears_in_the_prompt(self):
        system = brain.build_system(valid_config())

        for hypothesis_id in hypotheses.REGISTRY:
            self.assertIn(hypothesis_id, system)

    def test_the_placeholder_is_substituted(self):
        system = brain.build_system(valid_config())

        self.assertNotIn("__HYPOTHESIS_LIST__", system)
        self.assertIn("REGISTERED HYPOTHESES", system)

    def test_the_prompt_forbids_inventing_one(self):
        system = brain.build_system(valid_config())
        self.assertIn("may not invent", system)

    def test_the_prompt_is_still_byte_identical_across_builds(self):
        """Prompt caching depends on it, and so does attribution."""
        self.assertEqual(brain.build_system(valid_config()),
                         brain.build_system(valid_config()))


class ParseTests(unittest.TestCase):
    def test_hypothesis_id_survives_parsing(self):
        decisions = brain.parse_decisions(
            '{"decisions":[{"action":"open","symbol":"BTC/USDT:USDT",'
            '"direction":"long","setup_type":"other",'
            '"hypothesis_id":"volume-thrust","confidence":0.7,'
            '"invalidation_anchor":"atr","exit_policy":"fixed_rr"}]}')

        self.assertEqual(decisions[0]["hypothesis_id"], "volume-thrust")

    def test_an_absent_hypothesis_id_parses_as_empty(self):
        decisions = brain.parse_decisions(
            '{"decisions":[{"action":"open","symbol":"BTC/USDT:USDT",'
            '"direction":"long","setup_type":"trend_continuation",'
            '"confidence":0.7,"invalidation_anchor":"structure",'
            '"exit_policy":"fixed_rr"}]}')

        self.assertEqual(decisions[0]["hypothesis_id"], "")

    def test_an_unregistered_id_reaches_the_contract_rather_than_vanishing(self):
        """It must be rejected with a reason, not silently dropped."""
        decisions = brain.parse_decisions(
            '{"decisions":[{"action":"open","symbol":"BTC/USDT:USDT",'
            '"direction":"long","setup_type":"other",'
            '"hypothesis_id":"MY-IDEA","confidence":0.7,'
            '"invalidation_anchor":"atr","exit_policy":"fixed_rr"}]}')

        self.assertEqual(decisions[0]["hypothesis_id"], "my-idea")


if __name__ == "__main__":
    unittest.main()
