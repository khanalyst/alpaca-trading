"""The reviewer may propose a hypothesis, not only pick a registered one.

`research/review.py` already accepts one `hypothesis_draft` per review and
already refuses a draft with a missing or out-of-bounds field. These tests
cover the remaining property the draft contract has to carry: a proposal must
state a cause and a test, so "try X and see" is refused rather than stored,
and an accepted proposal stays inert and attributed.
"""

import json
import unittest
from copy import deepcopy

from research import review
from tests.helpers import valid_config
from tests.research.test_research_learning_loop import (
    START, FakeReviewer, ResearchLearningFixture)


def substantive_draft() -> dict:
    """The quality bar the registered research/hypotheses/*.yaml files set."""
    return {
        "title": "Volume-confirmed momentum continuation",
        "strategy_id": "momentum",
        "mechanism": (
            "Elevated participation means the impulse was paid for by real "
            "size rather than by a thin book drifting, so the counterparty "
            "is whoever is still positioned against a funded move."),
        "payer": "Late traders crossing the spread after the impulse",
        "falsifier": (
            "Net paired expectancy is no better when the recorded volume "
            "predicate holds than when it does not, at n >= 100."),
        "predicate": {
            "field": "relative_volume_1h",
            "operator": "gte",
            "value": 1.5,
            "direction": "both",
        },
        "horizon_hours": 12,
        "cost_treatment": "taker_round_trip",
        "evidence_needed": (
            "At least 100 paired forward observations with costs and "
            "separate fit and confirmation windows."),
    }


def response_with(draft: dict | None) -> str:
    payload = {
        "explanation": (
            "The deterministic result is inconclusive because the closed "
            "sample and paired windows remain below the stored gates."),
        "limitations": ["Paper costs are modeled rather than exchange fills."],
        "next_selection": None,
        "hypothesis_draft": draft,
    }
    return json.dumps(payload)


class HypothesisProposalContractTests(unittest.TestCase):
    """Parse-level: what the validator refuses to turn into a proposal."""

    def test_a_substantive_proposal_is_accepted(self):
        parsed = review.parse_review_response(
            response_with(substantive_draft()))

        self.assertEqual(parsed["hypothesis_draft"]["strategy_id"], "momentum")
        self.assertEqual(parsed["hypothesis_draft"]["predicate"]["field"],
                         "relative_volume_1h")

    def test_try_it_and_see_is_refused_in_every_causal_field(self):
        cases = {
            "mechanism": (
                "mechanism",
                "Apply a relative volume filter to the entries and see what "
                "happens over the next couple of weeks of trading."),
            "payer": (
                "payer",
                "Not sure who pays yet, we will find out from the data"),
            "falsifier": (
                "falsifier",
                "Run the experiment for a while and see if it works better "
                "than the baseline does over the same window."),
        }
        for name, (field, text) in cases.items():
            with self.subTest(field=name):
                draft = deepcopy(substantive_draft())
                draft[field] = text
                # Long enough to clear every declared bound; refused anyway.
                self.assertGreater(len(text),
                                   review.HYPOTHESIS_SUBSTANTIVE_MIN_CHARS)
                with self.assertRaisesRegex(
                        ValueError, f"{field} proposes trying something"):
                    review.parse_review_response(response_with(draft))

    def test_a_mechanism_or_falsifier_too_thin_to_state_a_cause_is_refused(self):
        for field in ("mechanism", "falsifier"):
            with self.subTest(field=field):
                draft = deepcopy(substantive_draft())
                # Inside the declared 20..1000 bound, under the substance
                # floor research/gates.py::has_mechanism applies.
                draft[field] = "Order flow is the cause."
                self.assertLess(len(draft[field]),
                                review.HYPOTHESIS_SUBSTANTIVE_MIN_CHARS)
                with self.assertRaisesRegex(
                        ValueError,
                        f"{field} must be at least "
                        f"{review.HYPOTHESIS_SUBSTANTIVE_MIN_CHARS} "
                        "characters"):
                    review.parse_review_response(response_with(draft))

    def test_the_declared_bounds_are_still_reported_first(self):
        """The substance rule must not mask the existing field contract."""
        draft = deepcopy(substantive_draft())
        draft["mechanism"] = "too short"

        with self.assertRaisesRegex(ValueError, "mechanism must be 20 to 1000"):
            review.parse_review_response(response_with(draft))

    def test_a_registered_hypothesis_mechanism_clears_the_bar(self):
        """The corpus is the bar; the rule must not reject the corpus."""
        from agent import hypotheses

        for spec in hypotheses.REGISTRY.values():
            with self.subTest(hypothesis=spec.id):
                draft = deepcopy(substantive_draft())
                draft["mechanism"] = spec.mechanism
                draft["falsifier"] = spec.falsifier
                parsed = review.parse_review_response(response_with(draft))
                self.assertEqual(parsed["hypothesis_draft"]["mechanism"],
                                 spec.mechanism.strip())


class HypothesisProposalInertnessTests(ResearchLearningFixture):
    """A proposal cannot promote itself into anything that trades."""

    def _pending_outcome(self, scope):
        assignment, _ = self._assignment(scope=scope, observations=10)
        self._seed(assignment, 10, "worked")
        self._complete(assignment, 10)
        return self.store.experiment_outcome(assignment["assignment_id"])

    def test_a_refused_proposal_is_never_stored_as_a_proposal(self):
        outcome = self._pending_outcome("demo:proposal-refused:feed-v1")
        draft = substantive_draft()
        draft["falsifier"] = (
            "Just run it for a month and see if the numbers come out ahead "
            "of the baseline arm.")

        result = review.process_pending_review(
            self.store, valid_config(),
            reviewer=FakeReviewer(response_with(draft)), now=START + 2001)
        attempts = self.store.experiment_review_attempts(
            outcome["outcome_id"])

        self.assertEqual(result["status"], "RETRY_PENDING")
        self.assertIsNone(self.store.experiment_review(outcome["outcome_id"]))
        self.assertEqual(attempts[-1]["status"], "FAILED")
        self.assertIn("proposes trying something", attempts[-1]["parse_error"])
        # The raw text is kept for audit, but nothing was parsed into a
        # structured proposal, so there is no draft to register.
        self.assertIsNone(attempts[-1]["response"])

    def test_an_accepted_proposal_is_attributed_and_stays_inert(self):
        outcome = self._pending_outcome("demo:proposal-inert:feed-v1")
        variants_before = self.store.variants()
        candidates_before = self.store.edge_candidates_for(
            scope_key=outcome["scope_key"])
        verdict_before = outcome["verdict"]

        result = review.process_pending_review(
            self.store, valid_config(),
            reviewer=FakeReviewer(response_with(substantive_draft())),
            now=START + 2002)
        attempt = self.store.experiment_review_attempts(
            outcome["outcome_id"])[-1]

        self.assertEqual(result["status"], "REVIEWED")
        # Attribution: which model, which prompt version, and when.
        self.assertEqual(attempt["provider"], "openai")
        self.assertEqual(attempt["model_id"], "test-review-model")
        self.assertEqual(attempt["prompt_version"], "test-review-prompt")
        self.assertEqual(attempt["completed_ts"], START + 2002)
        self.assertEqual(attempt["response"]["hypothesis_draft"],
                         result["hypothesis_draft"])
        # Inert: no runnable strategy, no selection, no rotation entry, no
        # tier movement, and the deterministic verdict is untouched.
        self.assertEqual(self.store.variants(), variants_before)
        self.assertIsNone(result["review"]["selection_id"])
        self.assertEqual(
            self.store.research_history_context(
                outcome["scope_key"])["pending_selections"], [])
        self.assertEqual(
            self.store.edge_candidates_for(scope_key=outcome["scope_key"]),
            candidates_before)
        self.assertEqual(
            self.store.experiment_outcome(
                outcome_id=outcome["outcome_id"])["verdict"], verdict_before)

    def test_the_request_contract_states_the_substance_bar(self):
        outcome = self._pending_outcome("demo:proposal-contract:feed-v1")

        contract = review.build_review_request(
            self.store, outcome)["hypothesis_draft_contract"]

        self.assertEqual(contract["substantive_min_chars"],
                         review.HYPOTHESIS_SUBSTANTIVE_MIN_CHARS)
        self.assertTrue(contract["rejects_action_without_cause"])
        self.assertTrue(contract["manual_registration_required"])
        self.assertIn("must state a cause and a test",
                      review.RESEARCH_REVIEW_SYSTEM)


if __name__ == "__main__":
    unittest.main()
