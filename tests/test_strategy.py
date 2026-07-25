import unittest

from agent import strategy
from tests.helpers import valid_config


def snapshot(**overrides):
    base = {
        "price": 100,
        "signal_ts": 1_000,
        "trend_15m": "up",
        "trend_1h": "up",
        "trend_4h": "up",
        "atr_1h_pct": 1.0,
        "ema20_1h_dist_pct": 0.5,
        "swing_low_pct": 1.2,
        "swing_high_pct": 2.8,
        "range_pos_pct": 70,
        "relative_volume_1h": 1.4,
        "mom_1h_pct": 0.5,
        "funding_rate_pct": 0.0,
    }
    base.update(overrides)
    return base


def decision(**overrides):
    base = {
        "action": "open",
        "symbol": "BTC/USDT:USDT",
        "direction": "long",
        "confidence": 0.8,
        "setup_type": "trend_continuation",
        "invalidation_anchor": "structure",
        "exit_policy": "fixed_rr",
        "execution_choice": "normal",
        "reasoning": "aligned continuation",
        # Legacy/malformed numeric authority must be discarded.
        "leverage": 10,
        "size_pct_equity": 99,
        "stop_loss_pct": 0.2,
        "take_profit_pct": 50,
    }
    base.update(overrides)
    return base


class StrategyContractTests(unittest.TestCase):
    def test_setup_id_is_stable_for_one_completed_signal_candle(self):
        cfg = valid_config()
        first, why = strategy.build_setup_plan(
            decision(), snapshot(), cfg)
        second, _ = strategy.build_setup_plan(
            decision(), snapshot(), cfg)
        next_bar, _ = strategy.build_setup_plan(
            decision(), snapshot(signal_ts=2_000), cfg)

        self.assertIsNone(why)
        self.assertEqual(first["setup_id"], second["setup_id"])
        self.assertNotEqual(first["setup_id"], next_bar["setup_id"])
        self.assertEqual(first["setup_key"], next_bar["setup_key"])

    def test_code_derives_stop_target_size_and_leverage_authority(self):
        cfg = valid_config()
        plan, why = strategy.build_setup_plan(
            decision(), snapshot(), cfg)

        self.assertIsNone(why)
        # structure 1.2% + 0.15 ATR buffer
        self.assertAlmostEqual(plan["stop_loss_pct"], 1.35)
        self.assertAlmostEqual(plan["take_profit_pct"], 2.70)
        self.assertEqual(plan["leverage"], cfg["risk"]["entry_leverage"])
        self.assertEqual(plan["size_pct_equity"], 0.0)

    def test_extreme_ema_extension_is_a_hard_no_chase_boundary(self):
        plan, why = strategy.build_setup_plan(
            decision(),
            snapshot(ema20_1h_dist_pct=3.0, atr_1h_pct=1.0),
            valid_config(),
        )
        self.assertIsNone(plan)
        self.assertIn("hard no-chase limit", why)

    def test_setup_label_must_match_broad_evidence_contract(self):
        plan, why = strategy.build_setup_plan(
            decision(),
            snapshot(trend_15m="flat", trend_1h="flat", trend_4h="down"),
            valid_config(),
        )
        self.assertIsNone(plan)
        self.assertEqual(
            why, "trend_continuation evidence contract is not met")

    def test_experimental_setup_is_demo_only(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        plan, why = strategy.build_setup_plan(
            decision(setup_type="other", invalidation_anchor="atr"),
            snapshot(), cfg)
        self.assertIsNone(plan)
        self.assertEqual(
            why, "experimental setups are allowed only in demo mode")

    def test_semantic_cooldown_blocks_new_candle_without_erasing_history(self):
        cfg = valid_config()
        plan, _ = strategy.build_setup_plan(
            decision(), snapshot(), cfg)
        record = strategy.new_setup_record(plan, cfg, now=100)
        strategy.mark_setup(
            record, "closed", cfg, now=200, apply_cooldown=True)
        records = {plan["setup_id"]: record}

        self.assertIsNotNone(
            strategy.semantic_block(records, plan["setup_key"], now=201))
        self.assertIsNone(
            strategy.semantic_block(records, plan["setup_key"], now=3_000))

    def test_one_symbol_is_evaluated_only_once_per_completed_signal_bar(self):
        cfg = valid_config()
        first, _ = strategy.build_setup_plan(
            decision(), snapshot(), cfg)
        records = {
            first["setup_id"]: strategy.new_setup_record(
                first, cfg, now=100)
        }
        relabelled, _ = strategy.build_setup_plan(
            decision(
                setup_type="other",
                invalidation_anchor="atr",
                direction="short",
            ),
            snapshot(),
            cfg,
        )

        self.assertIsNotNone(
            strategy.evaluated_signal(records, relabelled))
        next_bar, _ = strategy.build_setup_plan(
            decision(setup_type="other", invalidation_anchor="atr"),
            snapshot(signal_ts=2_000),
            cfg,
        )
        self.assertIsNone(strategy.evaluated_signal(records, next_bar))


if __name__ == "__main__":
    unittest.main()
