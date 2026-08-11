import json
from pathlib import Path
import unittest

from agent.contracts.rule import (DEFAULT_RULE_SPEC, RULE_SCHEMA_V2,
                                  rule_spec_hash, rule_variant_id)
from research.llm_strategy import (DISCOVERY_SCHEMA, PROPOSAL_SCHEMA,
                                    RuleProposalAdapter, canonical_json,
                                    content_hash)


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


def discovery(spec=None, thesis="Compression resolves directionally at midday."):
    payload = {"schema": DISCOVERY_SCHEMA, "thesis": thesis,
               "rule_spec": dict(spec or {"schema": RULE_SCHEMA_V2,
                                          "family": "volatility_breakout",
                                          "confirmations": ["volume"],
                                          "entry_after_minutes": 60,
                                          "entry_before_minutes": 240,
                                          "max_atr_bps": 90.0})}
    return json.dumps(payload, separators=(",", ":"))


class LLMDiscoveryTests(unittest.TestCase):
    """Discovery seeds new hypotheses under the same output boundary."""

    def test_success_normalizes_the_spec_and_records_hashed_evidence(self):
        calls = []

        def fake(*, system_prompt, request, timeout):
            calls.append((system_prompt, request, timeout))
            return discovery()

        result = RuleProposalAdapter(caller=fake, max_attempts=1).discover(
            vehicle="equity", slot=2,
            context={"tried_families": ["mean_reversion"],
                     "proved_families": ["trend_pullback"]})
        self.assertTrue(result.success)
        self.assertEqual(result.schema, DISCOVERY_SCHEMA)
        self.assertEqual(result.rule_spec["schema"], RULE_SCHEMA_V2)
        self.assertEqual(result.rule_spec["confirmations"], ["volume"])
        self.assertEqual(result.variant_id, rule_variant_id(result.rule_spec))
        self.assertEqual(result.evidence["spec_id"],
                         rule_spec_hash(result.rule_spec))
        self.assertEqual(result.evidence["kind"], "discovery")
        # The brief reaches the provider and the raw response never persists.
        self.assertEqual(calls[0][1]["slot"], 2)
        self.assertIn("proved_families", calls[0][1]["context"])
        self.assertNotIn("raw_response", result.evidence)
        self.assertEqual(result.evidence["raw_response_hash"],
                         content_hash(discovery()))

    def test_discovery_uses_its_own_prompt_and_schema(self):
        seen = {}

        def fake(*, system_prompt, request, timeout):
            seen["prompt"] = system_prompt
            return discovery()

        adapter = RuleProposalAdapter(caller=fake, max_attempts=1)
        adapter.discover(vehicle="equity", slot=0, context={})
        self.assertEqual(seen["prompt"], adapter.discovery_prompt)
        self.assertNotEqual(adapter.discovery_prompt, adapter.system_prompt)
        self.assertIn("thesis", adapter._schema(DISCOVERY_SCHEMA)["required"])
        self.assertNotIn("thesis", adapter._schema()["required"])

    def test_a_proposal_response_is_not_accepted_as_a_discovery(self):
        result = RuleProposalAdapter(
            caller=lambda **_: proposal(), max_attempts=1).discover(
            vehicle="equity", slot=0, context={})
        self.assertFalse(result.success)
        # It is missing ``thesis`` and carries the wrong schema; either is
        # disqualifying, and the field check simply reports first.
        self.assertIn("thesis", result.error or "")
        self.assertEqual(result.schema, DISCOVERY_SCHEMA)

    def test_thesis_must_be_short_plain_text(self):
        for thesis in ("", "x" * 241, "```py\nimport os\n```"):
            with self.subTest(thesis=thesis[:12]):
                result = RuleProposalAdapter(
                    caller=lambda **_: discovery(thesis=thesis),
                    max_attempts=1).discover(vehicle="equity", slot=0, context={})
                self.assertFalse(result.success)

    def test_unsafe_specs_and_contexts_are_refused(self):
        unsafe_spec = RuleProposalAdapter(
            caller=lambda **_: discovery(
                spec={"family": "mean_reversion", "source": "import os"}),
            max_attempts=1).discover(vehicle="equity", slot=0, context={})
        self.assertFalse(unsafe_spec.success)
        for key in ("raw_rows", "api_key", "market_data"):
            with self.subTest(key=key):
                result = RuleProposalAdapter(
                    caller=lambda **_: discovery(), max_attempts=1).discover(
                    vehicle="equity", slot=0, context={key: "unsafe"})
                self.assertFalse(result.success)

    def test_slot_and_vehicle_are_validated_before_any_provider_call(self):
        def exploding(**_):
            raise AssertionError("no provider call should be made")

        for vehicle, slot in (("crypto", 0), ("equity", -1), ("equity", True)):
            with self.subTest(vehicle=vehicle, slot=slot):
                result = RuleProposalAdapter(
                    caller=exploding, max_attempts=1).discover(
                    vehicle=vehicle, slot=slot, context={})
                self.assertFalse(result.success)

    def test_timeout_is_bounded(self):
        def slow(**_):
            import time
            time.sleep(.1)
            return discovery()

        result = RuleProposalAdapter(
            caller=slow, max_attempts=1, timeout_seconds=.01).discover(
            vehicle="equity", slot=0, context={})
        self.assertFalse(result.success)
        self.assertIn("TimeoutError", result.error or "")


if __name__ == "__main__":
    unittest.main()
