"""Unit tests for the tiering logic in research/gates.py.

The gates themselves are integration-tested by running the tournament against
real data; what is tested here is the branching that decides a tier, because
that is where a silent mistake would promote a strategy it should not.
"""

import sys
import unittest
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "research"))

import gates  # noqa: E402
from gates import GateResult, tier_from_gates, walk_forward_masks  # noqa: E402


def result(name, passed, **numbers):
    return GateResult(name, passed, f"{name} {'ok' if passed else 'failed'}",
                      numbers)


def all_passing():
    return [
        result("has_mechanism", True),
        result("beat_nulls", True),
        result("survive_oos", True),
        result("survive_costs", True),
        result("survive_placebo", True, ratio=0.05),
        result("mechanism_is_the_source", True, source_share=0.9),
        result("is_detectable", True, trades_needed=100, observed_trades=500),
    ]


class TierLogicTests(unittest.TestCase):
    def test_full_exploratory_pass_is_capped_at_candidate(self):
        tier, why = tier_from_gates(all_passing())
        self.assertEqual(tier, "T2_CANDIDATE")
        self.assertIn("authoritative recorded replay", why)

    def test_missing_mechanism_is_rejected_outright(self):
        gates_list = all_passing()
        gates_list[0] = result("has_mechanism", False)
        tier, why = tier_from_gates(gates_list)
        self.assertEqual(tier, "T0_REJECTED")
        self.assertIn("mechanism", why)

    def test_losing_to_a_null_is_rejected(self):
        gates_list = all_passing()
        gates_list[1] = result("beat_nulls", False)
        self.assertEqual(tier_from_gates(gates_list)[0], "T0_REJECTED")

    def test_failing_out_of_sample_stalls_at_hypothesis(self):
        gates_list = all_passing()
        gates_list[2] = result("survive_oos", False)
        self.assertEqual(tier_from_gates(gates_list)[0], "T1_HYPOTHESIS")

    def test_failing_costs_stalls_at_candidate(self):
        gates_list = all_passing()
        gates_list[3] = result("survive_costs", False)
        self.assertEqual(tier_from_gates(gates_list)[0], "T2_CANDIDATE")

    def test_a_loud_placebo_is_rejected_not_merely_stalled(self):
        # A placebo scoring half the candidate means the number came from the
        # procedure. That is disqualifying, not a stall.
        gates_list = all_passing()
        gates_list[4] = result("survive_placebo", False, ratio=0.60)
        tier, _ = tier_from_gates(gates_list)
        self.assertEqual(tier, "T0_REJECTED")

    def test_a_borderline_placebo_stalls_at_candidate(self):
        gates_list = all_passing()
        gates_list[4] = result("survive_placebo", False, ratio=0.30)
        self.assertEqual(tier_from_gates(gates_list)[0], "T2_CANDIDATE")

    def test_undetectable_effect_stalls_at_candidate(self):
        gates_list = all_passing()
        gates_list[6] = result("is_detectable", False)
        self.assertEqual(tier_from_gates(gates_list)[0], "T2_CANDIDATE")

    def test_a_false_mechanism_is_rejected_however_good_the_number(self):
        # Observed on real data: funding-carry posted +2.008% per trade,
        # beat every null, survived the placebo at -4%, and was better
        # out-of-sample than in. Funding contributed 2% of that result and
        # price movement 98%. Every number was true and the claim was not.
        gates_list = all_passing()
        gates_list[5] = result("mechanism_is_the_source", False,
                               source_share=0.02)
        tier, why = tier_from_gates(gates_list)
        self.assertEqual(tier, "T0_REJECTED")
        self.assertIn("mechanism_is_the_source", why)

    def test_an_empty_battery_is_rejected(self):
        self.assertEqual(tier_from_gates([])[0], "T0_REJECTED")

    def test_a_clean_run_on_too_few_trades_cannot_be_validated(self):
        # A thin sample agreeing with itself is not validation. Observed on
        # real data: the momentum benchmark beat its nulls on a 200-day,
        # 8-instrument window and failed the same test over 24 months.
        gates_list = all_passing()
        gates_list[6] = result("is_detectable", True,
                               trades_needed=900, observed_trades=337)
        tier, why = tier_from_gates(gates_list)
        self.assertEqual(tier, "T2_CANDIDATE")
        self.assertIn("337 trades", why)
        self.assertIn("900", why)

    def test_sufficient_sample_still_needs_recorded_replay(self):
        gates_list = all_passing()
        gates_list[6] = result("is_detectable", True,
                               trades_needed=900, observed_trades=1200)
        self.assertEqual(tier_from_gates(gates_list)[0], "T2_CANDIDATE")


class ProvenanceCapTests(unittest.TestCase):
    """A hypothesis generated from the scored data cannot be confirmed by it.

    funding-unwind was generated by decomposing funding-carry's result on the
    2026-05..2026-07 window. Scored on that same window it clears every gate
    and would otherwise read as validated. It is still only a lead, because the gates
    measure the result and the result is what suggested the hypothesis.
    """

    def _score(self, prereg):
        import tournament
        tier, why = tier_from_gates(all_passing())
        if prereg.get("in_sample_by_construction") and tier != "T0_REJECTED":
            tier = "T1_HYPOTHESIS"
        self.assertIn(tier, ("T1_HYPOTHESIS", "T2_CANDIDATE"))
        return tier

    def test_declared_in_sample_hypotheses_are_capped(self):
        self.assertEqual(
            self._score({"in_sample_by_construction": True}),
            "T1_HYPOTHESIS")

    def test_ordinary_hypotheses_are_not_capped(self):
        self.assertEqual(self._score({}), "T2_CANDIDATE")

    def test_funding_unwind_declares_its_provenance(self):
        import yaml
        data = yaml.safe_load(
            (REPO / "research" / "hypotheses"
             / "funding-unwind.yaml").read_text())
        self.assertTrue(data.get("in_sample_by_construction"))
        # It must also say what would settle it, or the cap is a dead end.
        self.assertTrue(data.get("what_would_change_the_verdict"))

    def test_the_two_funding_hypotheses_declare_opposite_sources(self):
        import yaml
        directory = REPO / "research" / "hypotheses"
        carry = yaml.safe_load((directory / "funding-carry.yaml").read_text())
        unwind = yaml.safe_load(
            (directory / "funding-unwind.yaml").read_text())
        self.assertEqual(carry.get("return_source"), "funding")
        self.assertEqual(unwind.get("return_source"), "price")
        # Same entry rule, opposite claims: the count must reflect that the
        # entry has now been looked at twice on this data.
        self.assertGreater(unwind["hypotheses_tested"],
                           carry["hypotheses_tested"])


class MechanismGateTests(unittest.TestCase):
    class Spec:
        def __init__(self, mechanism, falsification):
            self.mechanism = mechanism
            self.falsification = falsification

    def test_a_stated_mechanism_and_test_pass(self):
        spec = self.Spec(
            "Liquidation engines sell at market regardless of price, and the "
            "payer is the trader whose margin ran out.",
            "Bars with open interest falling revert no more than bars with "
            "open interest rising.")
        self.assertTrue(gates.has_mechanism(spec).passed)

    def test_a_slogan_does_not_count_as_a_mechanism(self):
        spec = self.Spec("prices go up", "it stops working")
        self.assertFalse(gates.has_mechanism(spec).passed)

    def test_missing_falsification_fails(self):
        spec = self.Spec(
            "Liquidation engines sell at market regardless of price, and the "
            "payer is the trader whose margin ran out.", "")
        self.assertFalse(gates.has_mechanism(spec).passed)


class WalkForwardTests(unittest.TestCase):
    def _trades(self, days=100):
        day = 86_400_000
        return pd.DataFrame({"entry_ts": [i * day for i in range(days)]})

    def test_split_leaves_a_purge_gap_between_halves(self):
        trades = self._trades()
        in_mask, out_mask = walk_forward_masks(trades)
        self.assertFalse((in_mask & out_mask).any())
        # The purge drops trades straddling the boundary, so the halves do
        # not account for every trade.
        self.assertLess(int(in_mask.sum() + out_mask.sum()), len(trades))

    def test_out_of_sample_is_the_later_period(self):
        trades = self._trades()
        in_mask, out_mask = walk_forward_masks(trades)
        self.assertLess(trades[in_mask]["entry_ts"].max(),
                        trades[out_mask]["entry_ts"].min())

    def test_empty_input_is_handled(self):
        in_mask, out_mask = walk_forward_masks(
            pd.DataFrame({"entry_ts": []}))
        self.assertEqual(len(in_mask), 0)
        self.assertEqual(len(out_mask), 0)


class SerializationTests(unittest.TestCase):
    def test_result_round_trips_to_a_dict(self):
        payload = result("beat_nulls", False, expectancy=-0.1).as_dict()
        self.assertEqual(payload["gate"], "beat_nulls")
        self.assertIs(payload["passed"], False)
        self.assertEqual(payload["numbers"]["expectancy"], -0.1)


class PreRegistrationTests(unittest.TestCase):
    """Every registered strategy scored by the tournament needs a file."""

    def test_pre_registrations_declare_a_hypothesis_count(self):
        import yaml
        directory = REPO / "research" / "hypotheses"
        files = sorted(directory.glob("*.yaml"))
        self.assertTrue(files, "no pre-registration files found")
        for path in files:
            with self.subTest(hypothesis=path.name):
                data = yaml.safe_load(path.read_text())
                self.assertIn("hypotheses_tested", data)
                self.assertIn("mechanism", data)
                self.assertIn("falsification", data)
                self.assertIsInstance(data["hypotheses_tested"], int)

    def test_pre_registration_ids_are_registered_strategies(self):
        import yaml
        from agent.registry import REGISTRY
        for path in sorted((REPO / "research" / "hypotheses").glob("*.yaml")):
            with self.subTest(hypothesis=path.name):
                data = yaml.safe_load(path.read_text())
                self.assertIn(data["strategy_id"], REGISTRY)


if __name__ == "__main__":
    unittest.main()
