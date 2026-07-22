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
