import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from agent.brain import LLM, SYSTEM, parse_decisions


class LLMPreflightTests(unittest.TestCase):
    def test_anthropic_model_access_is_checked_without_generation(self):
        llm = LLM.__new__(LLM)
        llm.cfg = {"model": "claude-test"}
        llm.provider = "anthropic"
        llm.client = Mock()
        llm.client.models.retrieve.return_value = Mock(id="claude-test")
        self.assertEqual(llm.preflight(), "claude-test")
        llm.client.models.retrieve.assert_called_once_with(
            model_id="claude-test")
        llm.client.messages.create.assert_not_called()

    def test_openai_model_access_is_checked_without_generation(self):
        llm = LLM.__new__(LLM)
        llm.cfg = {"model": "gpt-test"}
        llm.provider = "openai"
        llm.client = Mock()
        llm.client.models.retrieve.return_value = Mock(id="gpt-test")
        self.assertEqual(llm.preflight(), "gpt-test")
        llm.client.models.retrieve.assert_called_once_with(model="gpt-test")
        llm.client.chat.completions.create.assert_not_called()


class OpenAITemperatureFallbackTests(unittest.TestCase):
    @staticmethod
    def _llm():
        llm = LLM.__new__(LLM)
        llm.cfg = {"model": "gpt-test", "temperature": 0.2,
                   "max_tokens": 2000}
        llm.provider = "openai"
        llm._no_temperature = False
        llm.client = Mock()
        return llm

    @staticmethod
    def _response():
        response = Mock(usage=None)
        response.choices = [Mock(message=Mock(content='{"decisions":[]}'))]
        return response

    def test_rejected_temperature_is_remembered_across_calls(self):
        llm = self._llm()
        ok = self._response()
        create = Mock(side_effect=[
            RuntimeError("Unsupported parameter: 'temperature'"), ok, ok])
        llm.client.chat.completions.create = create

        llm._openai("sys", "user")
        llm._openai("sys", "user")

        self.assertTrue(llm._no_temperature)
        # One doomed attempt ever, not one per cycle: 1 rejection + 1 retry
        # for the first call, then a single clean request for the second.
        self.assertEqual(create.call_count, 3)
        self.assertIn("temperature", create.call_args_list[0].kwargs)
        self.assertNotIn("temperature", create.call_args_list[1].kwargs)
        self.assertNotIn("temperature", create.call_args_list[2].kwargs)

    def test_unrelated_errors_propagate_without_a_blind_retry(self):
        llm = self._llm()
        create = Mock(side_effect=RuntimeError("rate limited"))
        llm.client.chat.completions.create = create
        with self.assertRaisesRegex(RuntimeError, "rate limited"):
            llm._openai("sys", "user")
        self.assertEqual(create.call_count, 1)
        self.assertFalse(llm._no_temperature)

    def test_gpt5_family_omits_temperature_before_first_request(self):
        llm = self._llm()
        llm.cfg["model"] = "gpt-5.6-terra"
        llm._no_temperature = LLM._sampling_unsupported(
            llm.cfg["model"])
        params = llm._openai_params("sys", "user")
        self.assertNotIn("temperature", params)
        self.assertIn("prompt_cache_key", params)

    def test_usage_separates_total_input_from_uncached_input(self):
        llm = self._llm()
        response = self._response()
        response.usage = SimpleNamespace(
            prompt_tokens=5_388,
            completion_tokens=457,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3_407),
        )
        llm.client.chat.completions.create.return_value = response

        llm._openai("sys", "user")

        self.assertEqual(
            llm._last_usage_audit["input_tokens_total"], 5_388)
        self.assertEqual(
            llm._last_usage_audit["input_tokens_fresh"], 1_981)
        self.assertEqual(
            llm._last_usage_audit["cache_read_tokens"], 3_407)


class ModelOutputValidationTests(unittest.TestCase):
    def test_non_finite_confidence_is_safe_and_numeric_risk_fields_are_ignored(self):
        decisions = parse_decisions(
            '{"decisions":[{"action":"open",'
            '"symbol":"BTC/USDT:USDT","direction":"long",'
            '"confidence":NaN,"leverage":Infinity,'
            '"stop_loss_pct":2,"take_profit_pct":4,'
            '"setup_type":"trend_continuation",'
            '"invalidation_anchor":"structure",'
            '"exit_policy":"fixed_rr","execution_choice":"normal"}]}'
        )
        self.assertEqual(decisions[0]["confidence"], 0)
        self.assertNotIn("leverage", decisions[0])
        self.assertNotIn("stop_loss_pct", decisions[0])
        self.assertEqual(
            decisions[0]["setup_type"], "trend_continuation")

    def test_close_requires_structured_trigger_and_evidence_change(self):
        missing = parse_decisions(
            '{"decisions":[{"action":"close",'
            '"symbol":"BTC/USDT:USDT","reasoning":"looks weak"}]}')
        self.assertEqual(missing, [])

        decisions = parse_decisions(
            '{"decisions":[{"action":"close",'
            '"symbol":"BTC/USDT:USDT",'
            '"close_trigger":"thesis_invalidated",'
            '"evidence_change":"entry trend was up; current 1h trend is down",'
            '"reasoning":"the recorded thesis has broken"}]}')
        self.assertEqual(
            decisions[0]["close_trigger"], "thesis_invalidated")
        self.assertIn("current 1h trend", decisions[0]["evidence_change"])


class LLMAuditTests(unittest.TestCase):
    @staticmethod
    def _llm():
        llm = LLM.__new__(LLM)
        llm.cfg = {"model": "gpt-test", "temperature": 0.2,
                   "max_tokens": 2000}
        llm.provider = "openai"
        llm._no_temperature = False
        llm._call = Mock(return_value='{"decisions":[]}')
        llm._last_request_attempts = []
        llm._last_response_audit = None
        return llm

    def test_audit_request_contains_exact_prompt_and_provider_parameters(self):
        llm = self._llm()
        audit = llm.audit_request(
            {"BTC/USDT:USDT": {"price": 100}},
            {"equity_usdt": 10_000}, 1)

        self.assertEqual(audit["provider"], "openai")
        request = audit["request"]
        self.assertEqual(request["model"], "gpt-test")
        self.assertEqual(request["temperature"], 0.2)
        self.assertIn("prompt_cache_key", request)
        self.assertEqual(request["messages"][0]["content"], SYSTEM)
        self.assertIn('"equity_usdt":10000',
                      request["messages"][1]["content"])

    def test_call_audit_records_raw_and_parsed_output(self):
        llm = self._llm()
        llm.decide({}, {}, 0)
        audit = llm.call_audit()

        self.assertEqual(audit["request_attempts"], [])
        self.assertEqual(audit["response"]["raw_text"],
                         '{"decisions":[]}')
        self.assertEqual(audit["response"]["parsed_decisions"], [])
        self.assertEqual(audit["response"]["effective_temperature"], 0.2)


if __name__ == "__main__":
    unittest.main()
