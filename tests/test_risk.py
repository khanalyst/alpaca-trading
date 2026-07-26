import time
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


def liquidity_feedback(**overrides):
    base = {
        "reason": "insufficient_depth",
        "direction": "long",
        "expires_at": time.time() + 1800,
        "blocked_until": 0,
        "max_retry_size_pct_equity": 5.0,
    }
    base.update(overrides)
    return {"BTC/USDT:USDT": base}


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

    def test_non_finite_gross_exposure_fails_closed(self):
        plan, why = self.risk.vet_open(
            decision(), 10_000, [], snapshot(), {}, float("nan"))
        self.assertIsNone(plan)
        self.assertEqual(why, "gross exposure measurement is invalid")

    def test_non_finite_held_notional_cannot_bypass_net_cap(self):
        held = [{
            "symbol": "ETH/USDT:USDT", "side": "long",
            "notional": float("nan"),
        }]
        plan, why = self.risk.vet_open(
            decision(), 10_000, held, snapshot(), {}, 0)
        self.assertIsNone(plan)
        self.assertEqual(why, "held position notional is invalid")

    def test_take_profit_beyond_bound_is_rejected(self):
        # A short's TP trigger at entry*(1 - 120/100) would be negative and
        # OKX would reject the whole attached-SL/TP entry order.
        plan, why = self.risk.vet_open(
            decision(direction="short", take_profit_pct=120),
            10_000, [], snapshot(), {}, 0)
        self.assertIsNone(plan)
        self.assertEqual(why, "take-profit distance 120.0% out of bounds")

    def test_take_profit_at_bound_is_accepted(self):
        plan, why = self.risk.vet_open(
            decision(take_profit_pct=50), 10_000, [], snapshot(), {}, 0)
        self.assertIsNone(why)
        self.assertEqual(plan["tp_pct"], 50)

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

    def test_liquidity_retry_requires_model_approval(self):
        plan, why = self.risk.vet_open(
            decision(), 10_000, [], snapshot(), {}, 0,
            liquidity_feedback())
        self.assertIsNone(plan)
        self.assertEqual(
            why, "liquidity retry requires retry_smaller approval")

    def test_approved_liquidity_retry_is_sized_by_code(self):
        plan, why = self.risk.vet_open(
            decision(execution_choice="retry_smaller"),
            10_000, [], snapshot(), {}, 0,
            liquidity_feedback())
        self.assertIsNone(why)
        self.assertIsNotNone(plan)
        self.assertLessEqual(plan["margin_pct_equity"], 5)

    def test_retry_choice_without_current_feedback_is_rejected(self):
        plan, why = self.risk.vet_open(
            decision(execution_choice="retry_smaller"),
            10_000, [], snapshot(), {}, 0)
        self.assertIsNone(plan)
        self.assertEqual(
            why, "no current liquidity rejection supports a retry")

    def test_liquidity_backoff_blocks_same_direction(self):
        plan, why = self.risk.vet_open(
            decision(execution_choice="retry_smaller"),
            10_000, [], snapshot(), {}, 0,
            liquidity_feedback(blocked_until=time.time() + 900))
        self.assertIsNone(plan)
        self.assertEqual(why, "symbol in liquidity backoff")

    def test_opposite_direction_is_not_bound_by_stale_side_depth(self):
        plan, why = self.risk.vet_open(
            decision(direction="short"), 10_000, [], snapshot(), {}, 0,
            liquidity_feedback())
        self.assertIsNone(why)
        self.assertIsNotNone(plan)

    def test_execution_failure_backoff_blocks_symbol(self):
        failures = {
            "BTC/USDT:USDT": {
                "blocked_until": time.time() + 900,
                "expires_at": time.time() + 3600,
            },
        }
        plan, why = self.risk.vet_open(
            decision(), 10_000, [], snapshot(), {}, 0, {}, failures)
        self.assertIsNone(plan)
        self.assertEqual(why, "symbol in execution failure backoff")

    def test_model_cannot_change_configured_leverage(self):
        plan, why = self.risk.vet_open(
            decision(leverage=10_000), 10_000, [], snapshot(), {}, 0)
        self.assertIsNone(why)
        self.assertEqual(plan["leverage"], 2)

    def test_account_fee_rate_overrides_configured_fallback(self):
        market = snapshot()
        market["BTC/USDT:USDT"]["taker_fee_pct_per_side"] = 0.08
        plan, why = self.risk.vet_open(
            decision(), 10_000, [], market, {}, 0)
        self.assertIsNone(why)
        self.assertEqual(plan["taker_fee_pct_per_side"], 0.08)

    def test_held_position_without_durable_planned_risk_blocks_new_entries(self):
        market = snapshot()
        market["ETH/USDT:USDT"] = {
            "price": 100, "beta_btc_1h_72": 1,
            "corr_btc_samples": 72,
        }
        held = [{
            "symbol": "ETH/USDT:USDT", "side": "long",
            "notional": 1_000,
        }]
        plan, why = self.risk.vet_open(
            decision(), 10_000, held, market, {}, 1_000,
            active_trades={})
        self.assertIsNone(plan)
        self.assertEqual(
            why, "held position planned risk is unavailable")

    def test_total_planned_open_risk_cap_blocks_another_stop_budget(self):
        market = snapshot()
        market["ETH/USDT:USDT"] = {
            "price": 100, "beta_btc_1h_72": 0.2,
            "corr_btc_samples": 72,
        }
        held = [{
            "symbol": "ETH/USDT:USDT", "side": "short",
            "notional": 1_000,
        }]
        active = {"ETH/USDT:USDT": {"risk_usd": 300}}
        plan, why = self.risk.vet_open(
            decision(), 10_000, held, market, {}, 1_000,
            active_trades=active)
        self.assertIsNone(plan)
        self.assertEqual(why, "total planned open-risk cap reached")

    def test_btc_beta_weighted_cap_uses_measured_candidate_beta(self):
        cfg = valid_config()
        cfg["risk"]["max_btc_beta_exposure_pct"] = 20
        risk = RiskEngine(cfg)
        market = snapshot()
        market["BTC/USDT:USDT"].update({
            "beta_btc_1h_72": 1.0,
            "corr_btc_samples": 72,
        })
        plan, why = risk.vet_open(
            decision(), 10_000, [], market, {}, 0)
        self.assertIsNone(plan)
        self.assertEqual(
            why, "BTC-beta-weighted exposure cap reached")

    def test_experimental_lane_uses_smaller_risk_budget(self):
        plan, why = self.risk.vet_open(
            decision(setup_type="other"),
            10_000, [], snapshot(), {}, 0)
        self.assertIsNone(why)
        self.assertEqual(plan["risk_budget_pct"], 0.5)
        self.assertAlmostEqual(plan["risk_usd"], 50)


if __name__ == "__main__":
    unittest.main()
