import json
import unittest
from unittest.mock import Mock, patch

from agent.engine import Engine


def open_decision(symbol, confidence):
    return {
        "action": "open", "symbol": symbol, "direction": "long",
        "confidence": confidence, "size_pct_equity": 0, "leverage": 2,
        "stop_loss_pct": 1.0, "take_profit_pct": 2.0, "reasoning": "",
    }


class SortedOpensTests(unittest.TestCase):
    def test_opens_are_ordered_by_descending_confidence(self):
        opens, conflicted = Engine._sorted_opens([
            open_decision("ETH/USDT:USDT", 0.7),
            open_decision("BTC/USDT:USDT", 0.9),
        ])
        self.assertEqual([d["symbol"] for d in opens],
                         ["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.assertEqual(conflicted, [])

    def test_open_and_close_on_one_symbol_drops_the_open(self):
        close = {"action": "close", "symbol": "ETH/USDT:USDT",
                 "reasoning": "thesis broken"}
        keep = open_decision("BTC/USDT:USDT", 0.7)
        conflict = open_decision("ETH/USDT:USDT", 0.95)
        opens, conflicted = Engine._sorted_opens([close, conflict, keep])
        # The higher-confidence open loses: SYSTEM forbids closing a symbol
        # and re-entering (or reversing) it in the same reply, and the
        # engine enforces that instead of trusting the prompt.
        self.assertEqual(opens, [keep])
        self.assertEqual(conflicted, [conflict])


class PositionMetricTests(unittest.TestCase):
    def test_non_finite_ccxt_notional_uses_contract_fallback(self):
        engine = Engine.__new__(Engine)
        engine.ex = Mock()
        engine.ex.x.market.return_value = {"contractSize": 0.1}
        position = {
            "symbol": "BTC/USDT:USDT", "notional": float("nan"),
            "contracts": 2, "markPrice": 100,
        }
        self.assertEqual(engine._notional(position), 20)

    def test_completely_invalid_position_notional_returns_zero(self):
        engine = Engine.__new__(Engine)
        engine.ex = Mock()
        engine.ex.x.market.return_value = {"contractSize": 0.1}
        position = {
            "symbol": "BTC/USDT:USDT", "notional": float("nan"),
            "contracts": float("nan"), "markPrice": 100,
        }
        self.assertEqual(engine._notional(position), 0)


class LLMAuditEventTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.llm = Mock()

    @patch("agent.engine.state.log_event")
    def test_input_event_contains_exact_provider_request(self, log_event):
        request = {
            "provider": "openai",
            "request": {"model": "gpt-test", "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "snapshot"},
            ]},
        }
        self.engine.llm.audit_request.return_value = request

        self.engine._journal_llm_input({"price": 1}, {"equity": 10}, 1)

        kind, payload = log_event.call_args.args
        self.assertEqual(kind, "llm_input")
        self.assertEqual(json.loads(payload), request)
        self.engine.llm.audit_request.assert_called_once_with(
            {"price": 1}, {"equity": 10}, 1)

    @patch("agent.engine.state.log_event")
    def test_output_event_contains_raw_response_and_attempts(self, log_event):
        result = {
            "provider": "openai", "model": "gpt-test",
            "request_attempts": [{"temperature": 0.2}, {}],
            "response": {"id": "req-1", "raw_text": '{"decisions":[]}',
                         "effective_temperature": None,
                         "parsed_decisions": []},
        }
        self.engine.llm.call_audit.return_value = result

        self.engine._journal_llm_output()

        kind, payload = log_event.call_args.args
        self.assertEqual(kind, "llm_output")
        self.assertEqual(json.loads(payload), result)


if __name__ == "__main__":
    unittest.main()
