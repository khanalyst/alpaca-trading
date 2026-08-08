import time
import unittest

from agent.risk import RiskEngine, select_option_contract, size_shares


class RiskProfileTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"risk": {"risk_per_trade_pct": 1.0,
                              "max_position_notional_pct": 50,
                              "max_concurrent_positions": 3,
                              "options_min_dte": 7,
                              "options_max_dte": 45,
                              "options_max_spread_pct": 10},
                    "execution": {}}
        self.risk = RiskEngine(self.cfg)

    def test_share_sizing_is_floor_risk_over_stop_and_notional_capped(self):
        result = self.risk.size_shares(equity=10_000, entry_price=100,
                                       stop_distance=2, risk_usd=101,
                                       symbol_data={})
        self.assertEqual(result["shares"], 50)  # 50% notional cap binds
        self.assertEqual(result["notional"], 5_000)

    def test_liquidity_cap_reduces_share_size(self):
        result = size_shares(equity=10_000, entry_price=100,
                             stop_distance=2, risk_usd=100,
                             risk_config={"max_position_notional_pct": 90},
                             symbol_data={"liquidity_cap_shares": 7})
        self.assertEqual(result["shares"], 7)

    def test_option_multiplier_is_returned_and_max_loss_is_debit_times_multiplier(self):
        option = self.risk.size_options(
            equity=10_000, risk_usd=100,
            candidates=[{"type": "call", "dte": 14, "bid": 1.9,
                         "ask": 2.0, "volume": 10, "open_interest": 100,
                         "multiplier": 10}], direction="long")
        self.assertEqual(option["multiplier"], 10)
        self.assertEqual(option["contracts"], 5)
        self.assertEqual(option["max_loss"], 100)

    def test_short_uses_put_and_debit_spread_is_rejected(self):
        put = select_option_contract(
            [{"type": "put", "dte": 21, "bid": 2.0, "ask": 2.1,
              "volume": 1, "open_interest": 2, "multiplier": 50}],
            direction="short", risk_config={})
        self.assertEqual(put["type"], "put")
        with self.assertRaisesRegex(ValueError, "multi-leg"):
            select_option_contract(
                [{"strategy": "debit_spread", "dte": 21, "debit": 1.2,
                  "volume": 1, "open_interest": 2, "multiplier": 25}],
                direction="long", risk_config={})

    def test_rejects_zero_dte_stale_wide_illiquid_and_naked_short(self):
        cases = [
            [{"type": "call", "dte": 0, "ask": 1, "volume": 1,
              "multiplier": 100}],
            [{"type": "call", "dte": 14, "ask": 1, "stale": True,
              "volume": 1, "multiplier": 100}],
            [{"type": "call", "dte": 14, "bid": 1, "ask": 2,
              "volume": 1, "multiplier": 100}],
            [{"type": "call", "dte": 14, "ask": 1, "volume": 0,
              "open_interest": 0, "multiplier": 100}],
            [{"type": "call", "side": "sell", "dte": 14, "ask": 1,
              "volume": 1, "multiplier": 100}],
        ]
        for candidates in cases:
            with self.subTest(candidates=candidates):
                with self.assertRaisesRegex(ValueError, "no eligible"):
                    self.risk.select_option_contract(candidates, direction="long")

    def test_vet_options_keeps_underlying_stop_target(self):
        decision = {"symbol": "SPY", "direction": "long", "entry_price": 101,
                    "stop_price": 99.5, "target_price": 104,
                    "execution_profile": "options", "option_chain": [
                        {"type": "call", "dte": 14, "bid": 1.9,
                         "ask": 2, "volume": 1, "open_interest": 10,
                         "multiplier": 10}], "force_flat": True}
        plan, why = self.risk.vet_open(
            decision, 10_000, [], {"SPY": {"price": 101}}, {}, 0)
        self.assertIsNone(why)
        self.assertEqual(plan["underlying_stop_price"], 99.5)
        self.assertEqual(plan["underlying_target_price"], 104)
        self.assertEqual(plan["contract_multiplier"], 10)


if __name__ == "__main__":
    unittest.main()
