import unittest
from unittest.mock import Mock

from agent.brain import LLM, parse_decisions


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


class ModelOutputValidationTests(unittest.TestCase):
    def test_non_finite_model_numbers_are_replaced_with_safe_defaults(self):
        decisions = parse_decisions(
            '{"decisions":[{"action":"open",'
            '"symbol":"BTC/USDT:USDT","direction":"long",'
            '"confidence":NaN,"leverage":Infinity,'
            '"stop_loss_pct":2,"take_profit_pct":4}]}'
        )
        self.assertEqual(decisions[0]["confidence"], 0)
        self.assertEqual(decisions[0]["leverage"], 1)


if __name__ == "__main__":
    unittest.main()
