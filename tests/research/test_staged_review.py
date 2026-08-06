"""A dead mechanism must say why it died, in terms the next generation can use.

Registered variants get a coded terminal verdict the planner reads back.
Machine-authored mechanisms had nothing equivalent, so a proposer could see
what was staged but not what had happened to it - and a proposer told only
that something failed restates it with a different threshold, which is the
same claim wearing a different number.

The distinction these tests protect hardest is the one the seven-day corpus
made expensive: a mechanism starved of market data is never retired, because
its claim has not been tested at all.
"""

import tempfile
import unittest
from pathlib import Path

from research import shortlist, staged_review


def candidate(**overrides):
    base = {
        "variant_id": "staged.x", "scope_key": "demo:s",
        "trades": 0, "mean_r": 0.0, "ci_low": 0.0, "ci_high": 0.0,
        "label": shortlist.NO_EVIDENCE,
        "starved_decisions": 0, "declined_decisions": 0,
        "fit_mean_r": None, "confirmation_mean_r": None,
    }
    base.update(overrides)
    return shortlist.Candidate(**base)


class VerdictsExplainThemselvesTests(unittest.TestCase):
    def test_a_losing_mechanism_is_retired_and_says_a_nearby_threshold_wont_help(self):
        code, why = staged_review.verdict_for(candidate(
            trades=120, mean_r=-0.4, ci_low=-0.6, ci_high=-0.2,
            label=shortlist.NEGATIVE, declined_decisions=500))
        self.assertEqual(code, staged_review.NEGATIVE_EXPECTANCY)
        self.assertIn(code, staged_review.RETIRING)
        self.assertIn("same claim", why)

    def test_a_result_that_dies_out_of_sample_says_so(self):
        code, why = staged_review.verdict_for(candidate(
            trades=130, mean_r=0.3, ci_low=0.1, ci_high=0.5,
            label=shortlist.INCONCLUSIVE, fit_mean_r=0.8,
            confirmation_mean_r=-0.4, declined_decisions=500))
        self.assertEqual(code, staged_review.DIED_OUT_OF_SAMPLE)
        self.assertIn("fitted to where it was found", why)

    def test_a_mechanism_that_never_fires_is_unreachable_not_wrong(self):
        code, why = staged_review.verdict_for(candidate(
            declined_decisions=staged_review.NEVER_FIRED_DECISION_FLOOR))
        self.assertEqual(code, staged_review.NEVER_FIRED)
        self.assertIn("unreachable rather than wrong", why)
        self.assertIn("a condition the market actually visits", why)

    def test_starvation_outranks_every_other_verdict(self):
        # It would otherwise read NEGATIVE on its handful of trades, and be
        # retired for a result the pipeline caused rather than the market.
        code, why = staged_review.verdict_for(candidate(
            trades=4, mean_r=-0.9, ci_low=-1.0, ci_high=-0.8,
            label=shortlist.NEGATIVE,
            declined_decisions=10, starved_decisions=900))
        self.assertEqual(code, staged_review.STARVED_OF_DATA)
        self.assertNotIn(code, staged_review.RETIRING)
        self.assertIn("pipeline result, not a market one", why)

    def test_a_supported_mechanism_is_kept_and_claims_no_authority(self):
        code, why = staged_review.verdict_for(candidate(
            trades=200, mean_r=0.4, ci_low=0.2, ci_high=0.6,
            label=shortlist.SUPPORTED, confirmation_mean_r=0.3,
            declined_decisions=500))
        self.assertEqual(code, staged_review.SUPPORTED)
        self.assertNotIn(code, staged_review.RETIRING)
        self.assertIn("evidence, not authority", why)

    def test_a_thin_sample_keeps_collecting_rather_than_concluding(self):
        code, _ = staged_review.verdict_for(candidate(
            trades=8, mean_r=0.4, ci_low=-0.2, ci_high=1.0,
            label=shortlist.INSUFFICIENT, declined_decisions=100))
        self.assertEqual(code, staged_review.COLLECTING)
        self.assertNotIn(code, staged_review.RETIRING)


class ReviewRetiresAndFeedsTheNextGenerationTests(unittest.TestCase):
    def setUp(self):
        from agent.staging import StagingStore

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StagingStore(Path(self.tmp.name) / "staging.db")

    def _register(self, contract_id):
        self.store.register({
            "contract_id": contract_id,
            "mechanism": ("Crowded leverage must keep paying to hold and is "
                          "forced out when continuation stalls."),
            "payer": ("the leveraged trader squeezed out of a position they "
                      "could not finance"),
            "falsifier": ("Expectancy in the crowded cell does not exceed "
                          "the unconditional case at n>=100 per cell."),
            "direction": "both",
            "conditions": [{"field": "funding_percentile_30", "op": ">=",
                            "value": 80.0}],
        }, generation=0)

    def test_a_review_with_no_evidence_retires_nothing(self):
        self._register("alpha-claim")

        class EmptyFindings:
            path = ":memory:"

            def paper_scopes(self):
                return []

        result = staged_review.review(self.store, EmptyFindings())
        self.assertEqual(result["retired"], [])
        self.assertEqual(result["verdicts"][0]["code"],
                         staged_review.COLLECTING)
        self.assertEqual(len(self.store.active()), 1)

    def test_retirement_records_the_code_and_the_claim_together(self):
        self._register("alpha-claim")
        self.store.retire(
            "alpha-claim",
            f"{staged_review.NEGATIVE_EXPECTANCY}: fired 120 times for a "
            "mean of -0.400 R")
        rows = self.store.retired()
        self.assertEqual(len(rows), 1)
        self.assertIn(staged_review.NEGATIVE_EXPECTANCY,
                      rows[0]["retired_reason"])
        # The claim travels with the reason, so a proposer can tell that this
        # mechanism was tried rather than only that some id failed.
        self.assertIn("Crowded leverage", rows[0]["mechanism"])

    def test_history_separates_falsified_from_untested(self):
        self._register("was-tested")
        self._register("was-starved")
        self.store.retire(
            "was-tested",
            f"{staged_review.NEGATIVE_EXPECTANCY}: fired and lost")
        self.store.retire(
            "was-starved",
            f"{staged_review.STARVED_OF_DATA}: never evaluated")

        class EmptyFindings:
            path = ":memory:"

            def paper_scopes(self):
                return []

        history = staged_review.authoring_history(self.store, EmptyFindings())
        falsified = [item["contract_id"] for item in history["falsified"]]
        untested = [item["contract_id"] for item in history["inconclusive"]]
        self.assertIn("was-tested", falsified)
        self.assertIn("was-starved", untested)
        self.assertNotIn("was-starved", falsified)

    def test_history_carries_the_mechanism_not_just_the_id(self):
        self._register("alpha-claim")
        self.store.retire("alpha-claim",
                          f"{staged_review.NEGATIVE_EXPECTANCY}: lost")

        class EmptyFindings:
            path = ":memory:"

            def paper_scopes(self):
                return []

        history = staged_review.authoring_history(self.store, EmptyFindings())
        self.assertIn("Crowded leverage",
                      history["falsified"][0]["mechanism"])
        self.assertIn("squeezed out", history["falsified"][0]["payer"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
