import unittest

from agent.risk import RiskEngine
from tests.helpers import valid_config


def snapshot():
    return {
        "_market_context": {"benchmark": "BTC/USDT:USDT",
                            "regime": "trend_up"},
        "BTC/USDT:USDT": {
            "price": 100.0, "spread_pct": 0.05,
            "funding_rate_pct": 0.02,
            "funding_interval_hours": 4,
            "next_funding_minutes": 240,
        },
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

    def test_non_finite_snapshot_price_is_rejected(self):
        malformed = snapshot()
        malformed["BTC/USDT:USDT"]["price"] = float("nan")
        plan, why = self.risk.vet_open(
            decision(), 10_000, [], malformed, {}, 0)
        self.assertIsNone(plan)
        self.assertEqual(why, "invalid price")

    def test_all_in_costs_reduce_size_and_stay_inside_risk_budget(self):
        cfg = valid_config()
        cfg["risk"]["max_position_notional_pct"] = 100
        cfg["risk"]["max_gross_exposure_pct"] = 300
        cfg["risk"]["max_net_direction_pct"] = 300
        risk = RiskEngine(cfg)
        plan, why = risk.vet_open(
            decision(), 10_000, [], snapshot(), {}, 0)
        self.assertIsNone(why)
        self.assertGreater(plan["estimated_loss_pct"], plan["sl_pct"])
        self.assertLess(plan["notional"], 7_500)  # stop-only sizing
        self.assertAlmostEqual(plan["risk_usd"], 150, places=6)
        self.assertEqual(plan["estimated_funding_intervals"], 2)
        self.assertEqual(
            plan["entry_slippage_budget_pct"],
            cfg["execution"]["max_order_book_slippage_pct"],
        )


if __name__ == "__main__":
    unittest.main()
