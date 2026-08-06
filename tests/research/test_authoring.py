"""Generation must survive a bad proposer and must not repeat dead ideas.

The reviewer nominates the next setting from a fixed catalog, so when every
registered mechanism is falsified there is nothing left to nominate. It also
only runs once an assignment terminates, and on the 7-day demo corpus none
ever did, so it had never run at all. Authoring is the separate cadence that
generates ideas; these tests hold the properties that make an unattended one
safe to leave running.
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent.staging import StagingStore
from research import authoring


class FakeProposer:
    provider = "openai"
    model = "test-authoring-model"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def good_proposal(contract_id="funding-tail-unwind", **overrides):
    base = {
        "contract_id": contract_id,
        "mechanism": (
            "Extreme funding marks crowded leverage that must keep paying to "
            "hold, and that crowd is forced out when continuation stalls."),
        "payer": (
            "the leveraged trader squeezed out of a position they could no "
            "longer finance"),
        "falsifier": (
            "Expectancy in the crowded cell does not exceed the unconditional "
            "case by more than the MDE at n>=100 per cell."),
        "direction": "both",
        "conditions": [
            {"field": "funding_percentile_30", "op": ">=", "value": 80.0,
             "when_direction": "short"},
            {"field": "funding_samples_30", "op": ">=", "value": 20},
        ],
    }
    base.update(overrides)
    return base


def response(*proposals, reasoning="because the last generation died"):
    return json.dumps({"proposals": list(proposals), "reasoning": reasoning})


class AuthoringLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StagingStore(Path(self.tmp.name) / "staging.db")
        self.cfg = {"llm": {"provider": "openai", "model": "x"}}

    def test_a_generation_registers_and_advances(self):
        proposer = FakeProposer(response(good_proposal()))
        result = authoring.author_generation(
            self.store, self.cfg, author=proposer, now=1000.0)
        self.assertEqual(result["status"], "AUTHORED")
        self.assertEqual(result["generation"], 0)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(len(self.store.active()), 1)

        again = authoring.author_generation(
            self.store, self.cfg,
            author=FakeProposer(response(good_proposal("basis-crowding"))),
            now=2000.0)
        self.assertEqual(again["generation"], 1)
        self.assertEqual(self.store.generation(), 1)

    def test_one_bad_proposal_does_not_discard_the_generation(self):
        # Most proposals are expected to be wrong; refusing the batch would
        # make one bad field name cost a whole night of generation.
        proposer = FakeProposer(response(
            good_proposal("keeper"),
            good_proposal("bad-field", conditions=[
                {"field": "moon_phase", "op": ">", "value": 1}]),
            good_proposal("thin-claim", mechanism="it works"),
        ))
        result = authoring.author_generation(
            self.store, self.cfg, author=proposer, now=1000.0)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["rejected_count"], 2)
        self.assertEqual([c.contract_id for c in self.store.active()],
                         ["keeper"])
        reasons = " ".join(item["reason"] for item in result["rejected"])
        self.assertIn("moon_phase", reasons)

    def test_a_provider_failure_is_recorded_and_never_fatal(self):
        for failure in (RuntimeError("provider down"),
                        ValueError("rate limited")):
            with self.subTest(repr(failure)):
                result = authoring.author_generation(
                    self.store, self.cfg,
                    author=FakeProposer(error=failure))
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["accepted_count"], 0)
                self.assertIn(str(failure), result["error"])

    def test_a_malformed_response_is_recorded_with_the_raw_text(self):
        for raw in ("not json at all", "[]", '{"nope": 1}', ""):
            with self.subTest(repr(raw)):
                result = authoring.author_generation(
                    self.store, self.cfg, author=FakeProposer(raw))
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(len(self.store.active()), 0)

    def test_a_fenced_json_block_is_accepted(self):
        raw = "```json\n" + response(good_proposal()) + "\n```"
        result = authoring.author_generation(
            self.store, self.cfg, author=FakeProposer(raw), now=1000.0)
        self.assertEqual(result["accepted_count"], 1)

    def test_the_request_shows_what_is_proposable_and_what_already_died(self):
        proposer = FakeProposer(response(good_proposal()))
        history = {
            "falsified": [
                {"contract_id": "carry", "reason": "funding supplied 2%"}],
            "inconclusive": [{"contract_id": "ls", "reason": "n=1"}],
        }
        authoring.author_generation(
            self.store, self.cfg, author=proposer, history=history)
        request = proposer.requests[0]
        self.assertIn("funding_percentile_30", request["observable_fields"])
        self.assertNotIn("moon_phase", request["observable_fields"])
        self.assertEqual(request["falsified"], history["falsified"])
        self.assertEqual(request["inconclusive"], history["inconclusive"])

    def test_the_second_generation_sees_the_first_as_already_staged(self):
        authoring.author_generation(
            self.store, self.cfg,
            author=FakeProposer(response(good_proposal())), now=1000.0)
        proposer = FakeProposer(response(good_proposal("second")))
        authoring.author_generation(
            self.store, self.cfg, author=proposer, now=2000.0)
        staged = proposer.requests[0]["already_staged"]
        self.assertEqual([item["contract_id"] for item in staged],
                         ["funding-tail-unwind"])
        # The mechanism travels with it: a proposer that cannot read the
        # previous claim will restate it with a different threshold.
        self.assertIn("crowded leverage", staged[0]["mechanism"])

    def test_a_duplicate_id_is_rejected_without_touching_the_original(self):
        authoring.author_generation(
            self.store, self.cfg,
            author=FakeProposer(response(good_proposal())), now=1000.0)
        original = self.store.contract("funding-tail-unwind")
        result = authoring.author_generation(
            self.store, self.cfg, author=FakeProposer(response(
                good_proposal(mechanism=(
                    "A completely different claim about who pays, stated at "
                    "sufficient length to clear the substance floor.")))),
            now=2000.0)
        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(self.store.contract("funding-tail-unwind").mechanism,
                         original.mechanism)

    def test_a_generation_is_capped(self):
        many = [good_proposal(f"idea-{i:02d}")
                for i in range(authoring.MAX_PER_GENERATION + 4)]
        result = authoring.author_generation(
            self.store, self.cfg, author=FakeProposer(response(*many)),
            now=1000.0)
        self.assertLessEqual(
            result["accepted_count"] + result["rejected_count"],
            authoring.MAX_PER_GENERATION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
