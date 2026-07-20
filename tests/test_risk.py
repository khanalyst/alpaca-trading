import unittest

from agent.risk import RiskEngine
from tests.helpers import valid_config


def snapshot():
    return {
        "_market_context": {"benchmark": "BTC/USDT:USDT",
                            "regime": "trend_up"},
        "BTC/USDT:USDT": {"price": 100.0},
    }


def decision(**overrides):
    base = {
        "action": "open", "symbol": "BTC/USDT:USDT", "direction": "long",
        "confidence": 0.8, "stop_loss_pct": 2.0, "take_profit_pct": 4.0,
        "leverage": 2,
    }
    base.update(overrides)
    return base


class VetOpenSymbolTests(unittest.TestCase):
    def setUp(self):
        self.risk = RiskEngine(valid_config())

    def test_market_context_key_is_not_a_tradable_symbol(self):
        plan, why = self.risk.vet_open(
            decision(symbol="_market_context"), 10_000, [], snapshot(), {}, 0)
        self.assertIsNone(plan)
        self.assertEqual(why, "not a tradable symbol")

    def test_non_string_symbol_is_rejected(self):
        plan, why = self.risk.vet_open(
            decision(symbol=None), 10_000, [], snapshot(), {}, 0)
        self.assertIsNone(plan)
        self.assertEqual(why, "not a tradable symbol")

    def test_real_symbol_still_passes_the_new_guard(self):
        plan, why = self.risk.vet_open(
            decision(), 10_000, [], snapshot(), {}, 0)
        self.assertIsNotNone(plan)
        self.assertIsNone(why)


if __name__ == "__main__":
    unittest.main()
