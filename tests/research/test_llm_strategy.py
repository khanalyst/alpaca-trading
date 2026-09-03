import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent.contracts.rule import (DEFAULT_RULE_SPEC, RULE_SCHEMA_V2,
                                  RULE_SCHEMA_V3, RULE_SCHEMA_V4,
                                  rule_spec_hash, rule_variant_id,
                                  validate_rule_spec)
from research.llm_strategy import (DISCOVERY_SCHEMA, PROPOSAL_SCHEMA,
                                    SYSTEM_PROMPT, TUNING_SCHEMA, RuleProposalAdapter,
                                    RESEARCH_SAMPLING_TEMPERATURE,
                                    _parse_response, _safe_text,
                                    canonical_json, content_hash)


UNSUPPORTED_PROVIDER_SCHEMA_KEYS = frozenset({
    "oneOf", "const", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "multipleOf", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "format", "minProperties",
    "maxProperties", "contains", "minContains", "maxContains",
    "patternProperties", "propertyNames", "unevaluatedProperties",
    "unevaluatedItems", "allOf", "not", "if", "then", "else", "$ref",
    "$defs", "definitions",
})


def proposal(spec=None):
    return json.dumps({"schema": PROPOSAL_SCHEMA,
                       "rule_spec": dict(spec or DEFAULT_RULE_SPEC)},
                      separators=(",", ":"))


class LLMRuleStrategyTests(unittest.TestCase):
    def test_prompts_describe_vehicle_schema_boundaries(self):
        adapter = RuleProposalAdapter()
        for prompt in (SYSTEM_PROMPT, adapter.discovery_prompt):
            self.assertIn("rule-strategy.v3", prompt)
            self.assertIn("rule-strategy.v4", prompt)
            self.assertIn("breakeven_r", prompt)
            self.assertIn("options", prompt)

        self.assertIn("rule-strategy.v3", adapter.discovery_prompt)
        self.assertIn("options remain on executable v1/v2", adapter.discovery_prompt)

    def test_oversized_proposal_diagnosis_is_compacted_with_provenance(self):
        seen = []

        def caller(*, system_prompt, request, timeout):
            seen.append(request)
            return proposal()

        diagnosis = {
            "primary_failure": "negative_expectancy",
            "provenance_hash": "a" * 64,
            "telemetry": ["aggregate-%04d-%s" % (index, "x" * 300)
                          for index in range(100)],
        }
        result = RuleProposalAdapter(caller=caller, max_attempts=1).propose(
            vehicle="equity", generation=0,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC,
            diagnosis=diagnosis)
        self.assertTrue(result.success, result.error)
        compact = seen[0]["diagnosis"]
        self.assertLessEqual(len(canonical_json(compact).encode("utf-8")),
                             8192)
        metadata = compact["_compaction"]
        self.assertEqual(metadata["original_bytes"],
                         len(canonical_json(diagnosis).encode("utf-8")))
        self.assertEqual(metadata["original_hash"], content_hash(diagnosis))
        self.assertTrue(result.evidence["diagnosis_compacted"])
        self.assertEqual(result.evidence["diagnosis_original_hash"],
                         metadata["original_hash"])

    def test_oversized_discovery_context_is_compacted_deterministically(self):
        seen = []

        def caller(*, system_prompt, request, timeout):
            seen.append(request)
            return discovery()

        context = {
            "tried_families": ["mean_reversion"],
            "telemetry": ["context-%04d-%s" % (index, "y" * 300)
                          for index in range(100)],
        }
        adapter = RuleProposalAdapter(caller=caller, max_attempts=1)
        first = adapter.discover(vehicle="equity", slot=0, context=context)
        second = adapter.discover(vehicle="equity", slot=0, context=context)
        self.assertTrue(first.success, first.error)
        self.assertTrue(second.success, second.error)
        self.assertEqual(seen[0]["context"], seen[1]["context"])
        compact = seen[0]["context"]
        self.assertLessEqual(len(canonical_json(compact).encode("utf-8")),
                             8192)
        metadata = compact["_compaction"]
        self.assertEqual(metadata["original_hash"], content_hash(context))
        self.assertTrue(first.evidence["context_compacted"])
        self.assertEqual(first.evidence["context_original_bytes"],
                         metadata["original_bytes"])

    def test_provider_schema_is_recursive_strict_subset(self):
        schema = RuleProposalAdapter._schema()

        def walk(value):
            if isinstance(value, dict):
                self.assertTrue(
                    UNSUPPORTED_PROVIDER_SCHEMA_KEYS.isdisjoint(value),
                    f"unsupported provider keywords: "
                    f"{UNSUPPORTED_PROVIDER_SCHEMA_KEYS.intersection(value)}")
                if value.get("type") == "object" or "properties" in value:
                    self.assertEqual(set(value.get("properties", {})),
                                     set(value.get("required", ())))
                    self.assertFalse(value.get("additionalProperties", True))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(schema)
        rule = schema["properties"]["rule_spec"]
        self.assertIn("anyOf", rule)
        lookback = rule["anyOf"][0]["properties"]["lookback"]
        self.assertEqual(lookback["type"], "integer")
        self.assertNotIn("minimum", lookback)
        self.assertNotIn("maximum", lookback)

        tuning = RuleProposalAdapter._schema(TUNING_SCHEMA)
        builds_on = tuning["properties"]["variants"]["items"]["properties"]["builds_on"]
        self.assertEqual({branch["type"] for branch in builds_on["anyOf"]},
                         {"string", "null"})
        self.assertIn("builds_on", tuning["properties"]["variants"]["items"]["required"])

    def test_v4_optional_grammar_fields_are_required_nullable_provider_values(self):
        schema = RuleProposalAdapter._schema(PROPOSAL_SCHEMA, vehicle="equity")
        v4 = next(branch for branch in schema["properties"]["rule_spec"]["anyOf"]
                  if branch["properties"]["schema"]["enum"] == [RULE_SCHEMA_V4])
        self.assertEqual(set(v4["properties"]), set(v4["required"]))
        for field in ("target_mode", "target_lookback", "trailing_stop_r",
                      "exit_before_minutes"):
            self.assertTrue(any(option.get("type") == "null"
                                for option in v4["properties"][field]["anyOf"]))

    def test_vehicle_provider_schema_excludes_v3_and_v4_for_options(self):
        equity = RuleProposalAdapter._schema(DISCOVERY_SCHEMA, vehicle="equity")
        option = RuleProposalAdapter._schema(DISCOVERY_SCHEMA, vehicle="option")
        equity_schemas = {
            branch["properties"]["schema"]["enum"][0]
            for branch in equity["properties"]["rule_spec"]["anyOf"]
        }
        option_schemas = {
            branch["properties"]["schema"]["enum"][0]
            for branch in option["properties"]["rule_spec"]["anyOf"]
        }
        self.assertEqual(equity_schemas, {"rule-strategy.v1", "rule-strategy.v2",
                                          RULE_SCHEMA_V3, RULE_SCHEMA_V4})
        self.assertEqual(option_schemas, {"rule-strategy.v1", "rule-strategy.v2"})

    def test_evidence_hashes_the_vehicle_specific_rule_grammar(self):
        adapter = RuleProposalAdapter()
        equity = adapter._base_evidence(
            kind="proposal", schema_name=PROPOSAL_SCHEMA, vehicle="equity")
        option = adapter._base_evidence(
            kind="proposal", schema_name=PROPOSAL_SCHEMA, vehicle="option")

        self.assertEqual(
            equity["grammar_schema_hash"],
            content_hash(adapter._grammar_schema("equity")))
        self.assertEqual(
            option["grammar_schema_hash"],
            content_hash(adapter._grammar_schema("option")))
        self.assertNotEqual(equity["grammar_schema_hash"],
                            option["grammar_schema_hash"])

        attempt = adapter._attempt_evidence(
            attempt=1, schema_name=PROPOSAL_SCHEMA,
            prompt_hash="prompt", request_hash="request", vehicle="option")
        self.assertEqual(attempt["grammar_schema_hash"],
                         option["grammar_schema_hash"])

    def test_openai_responses_seam_receives_sanitized_schema(self):
        seen = {}

        class Response:
            output_text = proposal()

        class Responses:
            def create(self, **kwargs):
                seen.update(kwargs)
                return Response()

        class Client:
            responses = Responses()

        result = RuleProposalAdapter(client=Client(), model="test-model",
                                     max_attempts=1).propose(
            vehicle="equity", generation=0,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
        self.assertTrue(result.success)
        self.assertEqual(seen["temperature"], RESEARCH_SAMPLING_TEMPERATURE)
        provider_schema = seen["text"]["format"]["schema"]

        def walk(value):
            if isinstance(value, dict):
                self.assertTrue(UNSUPPORTED_PROVIDER_SCHEMA_KEYS.isdisjoint(value))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(provider_schema)
        threshold = (provider_schema["properties"]["rule_spec"]["anyOf"][0]
                     ["properties"]["threshold_bps"])
        self.assertEqual(threshold["type"], "number")
        self.assertNotIn("minimum", threshold)
        self.assertNotIn("maximum", threshold)

    def test_deployment_alias_is_used_for_provider_call_and_recorded_separately(self):
        seen = {}

        class Response:
            output_text = proposal()

        class Responses:
            def create(self, **kwargs):
                seen.update(kwargs)
                return Response()

        class Client:
            responses = Responses()

        adapter = RuleProposalAdapter(client=Client(), model="gpt-catalog",
                                      deployment="azure-prod", max_attempts=1)
        result = adapter.propose(
            vehicle="equity", generation=0,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
        self.assertTrue(result.success, result.error)
        self.assertEqual(seen["model"], "azure-prod")
        self.assertEqual(result.evidence["model"], "gpt-catalog")
        self.assertEqual(result.evidence["deployment"], "azure-prod")

    def test_gpt5_deployment_omits_unsupported_temperature(self):
        seen = {}

        class Response:
            output_text = proposal()

        class Responses:
            def create(self, **kwargs):
                seen.update(kwargs)
                return Response()

        class Client:
            responses = Responses()

        adapter = RuleProposalAdapter(
            client=Client(), model="gpt-5.6-terra",
            deployment="gpt-5.6-terra", max_attempts=1)
        result = adapter.preflight()
        self.assertEqual(result.status, "ready", result.reason)
        self.assertNotIn("temperature", seen)
        self.assertEqual(seen["max_output_tokens"], 1200)
        self.assertIn("rule_spec", json.dumps(seen["text"]["format"]["schema"]))

        expected = content_hash({
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "deployment": "gpt-5.6-terra",
            "temperature": None,
            "max_attempts": adapter.max_attempts,
            "timeout_seconds": adapter.timeout_seconds,
            "max_response_bytes": adapter.max_response_bytes,
            "max_total_calls": adapter.max_total_calls,
        })
        self.assertEqual(result.evidence["config_hash"], expected)

    def test_sampling_and_deployment_are_part_of_evidence_config(self):
        adapter = RuleProposalAdapter(
            model="gpt-catalog", deployment="azure-prod",
            caller=lambda **_: proposal(), max_attempts=1)
        expected = content_hash({
            "provider": "openai",
            "model": "gpt-catalog",
            "deployment": "azure-prod",
            "temperature": RESEARCH_SAMPLING_TEMPERATURE,
            "max_attempts": adapter.max_attempts,
            "timeout_seconds": adapter.timeout_seconds,
            "max_response_bytes": adapter.max_response_bytes,
            "max_total_calls": adapter.max_total_calls,
        })
        result = adapter.propose(
            vehicle="equity", generation=0,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.evidence["config_hash"], expected)

    def test_azure_endpoint_without_deployment_is_fatal_before_call(self):
        calls = []
        adapter = RuleProposalAdapter(
            caller=lambda **_: calls.append(True), model="gpt-catalog",
            max_attempts=1)
        with patch.dict("os.environ", {
                "OPENAI_BASE_URL": "https://resource.openai.azure.com/openai/v1"}):
            outcome = adapter.preflight()
        self.assertEqual(outcome.status, "fatal")
        self.assertIn("deployment", outcome.reason or "")
        self.assertEqual(calls, [])

    def test_internal_validation_retains_removed_provider_bounds(self):
        with self.assertRaises(ValueError):
            validate_rule_spec({**DEFAULT_RULE_SPEC, "threshold_bps": 500.1})

        too_many_confirmations = {
            **DEFAULT_RULE_SPEC,
            "schema": RULE_SCHEMA_V2,
            "confirmations": ["trend", "volume", "volatility", "trend"],
        }
        with self.assertRaises(ValueError):
            validate_rule_spec(too_many_confirmations)

        with self.assertRaises(ValueError):
            _safe_text("x" * 241, label="reason", limit=240)
        with self.assertRaises(ValueError):
            _parse_response(proposal(), max_bytes=16)

    def test_v3_tuning_allows_nullable_breakeven_activation_but_not_options(self):
        root = validate_rule_spec({"schema": RULE_SCHEMA_V3,
                                   "family": "momentum_continuation"})
        tuned = {**root, "breakeven_r": 0.5}
        reply = json.dumps({"schema": TUNING_SCHEMA, "variants": [{
            "rule_spec": tuned,
            "reason": "Activated breakeven_r to protect gains after the diagnosed loss.",
        }]})
        adapter = RuleProposalAdapter(caller=lambda **_: reply, max_attempts=1)
        equity = adapter.tune("equity", 0, root, {"primary_failure": "negative_expectancy"},
                              count=1)
        self.assertTrue(equity.success, equity.error)
        self.assertEqual(equity.variants[0]["rule_spec"]["breakeven_r"], 0.5)
        option = adapter.tune("option", 0, root, {"primary_failure": "negative_expectancy"},
                              count=1)
        self.assertFalse(option.success)
        self.assertIn("not executable for options", option.error or "")

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
        self.assertEqual(len(result.evidence["attempt_evidence"]), 2)
        for attempt in result.evidence["attempt_evidence"]:
            self.assertRegex(attempt["config_hash"], r"^[0-9a-f]{64}$")
            self.assertRegex(attempt["response_schema_hash"], r"^[0-9a-f]{64}$")

    def test_total_call_budget_is_shared_across_requests(self):
        calls = []
        adapter = RuleProposalAdapter(
            caller=lambda **_: (calls.append(True) or proposal()),
            max_attempts=1, max_total_calls=1)
        first = adapter.propose(
            vehicle="equity", generation=1,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
        second = adapter.propose(
            vehicle="equity", generation=2,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.evidence["calls_remaining"], 0)

    def test_auth_failure_opens_circuit_without_retries(self):
        calls = []

        class AuthenticationError(Exception):
            pass

        def fail(**_):
            calls.append(True)
            raise AuthenticationError("invalid api key")

        adapter = RuleProposalAdapter(caller=fail, max_attempts=3)
        result = adapter.propose(
            vehicle="equity", generation=1,
            prior_validated_rule_spec=DEFAULT_RULE_SPEC, diagnosis={})
        self.assertFalse(result.success)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.evidence["auth_circuit_open"])

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

    def test_equity_discovery_accepts_v3_breakeven_and_option_discovery_rejects_it(self):
        v3 = validate_rule_spec({"schema": RULE_SCHEMA_V3,
                                 "family": "mean_reversion",
                                 "breakeven_r": 0.5})
        adapter = RuleProposalAdapter(
            caller=lambda **_: discovery(spec=v3), max_attempts=1)
        equity = adapter.discover(vehicle="equity", slot=0, context={})
        self.assertTrue(equity.success, equity.error)
        self.assertEqual(equity.rule_spec["schema"], RULE_SCHEMA_V3)
        option = adapter.discover(vehicle="option", slot=0, context={})
        self.assertFalse(option.success)
        self.assertIn("not executable for options", option.error or "")

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
