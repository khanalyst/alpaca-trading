import json
from pathlib import Path
import unittest

from agent.contracts.rule import DEFAULT_RULE_SPEC, rule_spec_hash, rule_variant_id
from research.llm_strategy import (PROPOSAL_SCHEMA, RuleProposalAdapter,
                                    canonical_json, content_hash)


def proposal(spec=None):
    return json.dumps({"schema": PROPOSAL_SCHEMA,
                       "rule_spec": dict(spec or DEFAULT_RULE_SPEC)},
                      separators=(",", ":"))


class LLMRuleStrategyTests(unittest.TestCase):
    def test_strict_success_hashes_and_content_ids(self):
        calls = []

        def fake(*, system_prompt, request, timeout):
            calls.append((system_prompt, request, timeout))
            return proposal()

        result = RuleProposalAdapter(caller=fake, max_attempts=1).propose(
            vehicle="equity", generation=2,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC,
            diagnosis={"failure_mode": "late entries", "count": 3})
        self.assertTrue(result.success)
        self.assertEqual(result.variant_id, rule_variant_id(DEFAULT_RULE_SPEC))
        self.assertEqual(result.spec_id, rule_spec_hash(DEFAULT_RULE_SPEC))
        self.assertEqual(result.evidence["request_hash"], content_hash(calls[0][1]))
        self.assertEqual(result.evidence["system_prompt_hash"],
                         content_hash(calls[0][0]))
        self.assertEqual(result.evidence["raw_response_hash"],
                         content_hash(proposal()))
        self.assertEqual(set(calls[0][1]), {
            "vehicle", "generation", "prior_validated_rule_spec", "diagnosis"})

    def test_retries_bad_json_then_accepts(self):
        responses = iter(["not json", proposal()])
        result = RuleProposalAdapter(caller=lambda **_: next(responses),
                                     max_attempts=2).propose(
            vehicle="option", generation=0,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
        self.assertTrue(result.ok)
        self.assertEqual(result.evidence["attempts"], 2)

    def test_rejects_fences_unknown_source_and_nonfinite(self):
        bad = [
            "```json\n" + proposal() + "\n```",
            json.dumps({"schema": PROPOSAL_SCHEMA, "rule_spec": DEFAULT_RULE_SPEC,
                        "source": "print(1)"}),
            json.dumps({"schema": PROPOSAL_SCHEMA, "rule_spec": {
                **DEFAULT_RULE_SPEC, "threshold_bps": float("nan")}}),
        ]
        for response in bad:
            result = RuleProposalAdapter(caller=lambda **_: response,
                                         max_attempts=1).propose(
                vehicle="equity", generation=1,
                prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
            self.assertFalse(result.success, response)
            self.assertIn("attempt 1", result.error or "")

    def test_diagnosis_rejects_raw_rows_and_no_provider_key_in_evidence(self):
        result = RuleProposalAdapter(caller=lambda **_: proposal(),
                                     max_attempts=1).propose(
            vehicle="equity", generation=1,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC,
            diagnosis={"rows": [{"close": 1}]})
        self.assertFalse(result.success)
        self.assertNotIn("api_key", result.evidence)
        for key in ("rows ", " api-key ", "source "):
            with self.subTest(key=key):
                result = RuleProposalAdapter(
                    caller=lambda **_: proposal(), max_attempts=1).propose(
                        vehicle="equity", generation=1,
                        prior_validated_rule_spec=DEFAULT_RULE_SPEC,
                        diagnosis={key: "unsafe"})
                self.assertFalse(result.success)

    def test_timeout_is_bounded_and_error_is_plain(self):
        def slow(**_):
            import time
            time.sleep(.1)
            return proposal()

        result = RuleProposalAdapter(caller=slow, max_attempts=1,
                                     timeout_seconds=.01).propose(
            vehicle="equity", generation=1,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
        self.assertFalse(result.success)
        self.assertIn("TimeoutError", result.error or "")


if __name__ == "__main__":
    unittest.main()
