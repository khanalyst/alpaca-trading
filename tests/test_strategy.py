import unittest

from agent import strategy
from tests.helpers import valid_config


def snapshot(**overrides):
    base = {
        "price": 100,
        "signal_ts": 1_000,
        "signal_1h_ts": 900,
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
        "mom_15m_pct": 0.2,
        "fresh_breakout_long": False,
        "fresh_breakout_short": False,
        "price_stabilized_long": True,
        "price_stabilized_short": False,
        "funding_rate_pct": 0.0,
        "funding_samples_30": 20,
        "funding_percentile_30": 50,
        "perp_index_basis_pct": 0.0,
        "open_interest_musd": 100,
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

    def test_continuation_requires_one_hour_and_four_hour_alignment(self):
        plan, why = strategy.build_setup_plan(
            decision(),
            snapshot(trend_4h="flat"),
            valid_config(),
        )
        self.assertIsNone(plan)
        self.assertEqual(
            why, "trend_continuation evidence contract is not met")

    def test_range_position_alone_is_not_a_fresh_breakout(self):
        plan, why = strategy.build_setup_plan(
            decision(setup_type="range_breakout"),
            snapshot(range_pos_pct=95, fresh_breakout_long=False),
            valid_config(),
        )
        self.assertIsNone(plan)
        self.assertEqual(why, "range_breakout evidence contract is not met")

        plan, why = strategy.build_setup_plan(
            decision(setup_type="range_breakout"),
            snapshot(range_pos_pct=95, fresh_breakout_long=True),
            valid_config(),
        )
        self.assertIsNone(why)
        self.assertIsNotNone(plan)

    def test_funding_squeeze_requires_crowding_and_price_stabilization(self):
        plan, why = strategy.build_setup_plan(
            decision(
                setup_type="funding_squeeze",
                invalidation_anchor="atr"),
            snapshot(
                funding_rate_pct=-0.05,
                funding_percentile_30=10,
                perp_index_basis_pct=-0.1,
                price_stabilized_long=False),
            valid_config(),
        )
        self.assertIsNone(plan)
        self.assertEqual(why, "funding_squeeze evidence contract is not met")

        plan, why = strategy.build_setup_plan(
            decision(
                setup_type="funding_squeeze",
                invalidation_anchor="atr"),
            snapshot(
                funding_rate_pct=-0.05,
                funding_percentile_30=10,
                perp_index_basis_pct=-0.1,
                price_stabilized_long=True),
            valid_config(),
        )
        self.assertIsNone(why)
        self.assertIsNotNone(plan)

    def test_funding_threshold_is_normalized_to_an_eight_hour_equivalent(self):
        """A 4h contract charges the same rate twice as often as an 8h one.

        Comparing raw per-interval rates would hold the 4h contract to a bar
        twice as strict in economic terms, which is what made the setup
        unreachable on liquid instruments.
        """
        cfg = valid_config()
        threshold = cfg["strategy"]["funding_extreme_pct_per_8h"]
        half = -threshold / 2

        # Half the threshold on a 4h contract is exactly the threshold per 8h.
        evidence = strategy.setup_evidence(
            snapshot(funding_rate_pct=half,
                     funding_interval_hours=4,
                     funding_percentile_30=10,
                     perp_index_basis_pct=-0.1,
                     mom_15m_pct=0.2,
                     price_stabilized_long=True),
            cfg)
        self.assertTrue(evidence["funding_squeeze"]["long"])
        self.assertAlmostEqual(
            evidence["funding_8h_equivalent_pct"], -threshold, places=6)

        # The identical raw rate on an 8h contract is only half as extreme.
        evidence = strategy.setup_evidence(
            snapshot(funding_rate_pct=half,
                     funding_interval_hours=8,
                     funding_percentile_30=10,
                     perp_index_basis_pct=-0.1,
                     mom_15m_pct=0.2,
                     price_stabilized_long=True),
            cfg)
        self.assertFalse(evidence["funding_squeeze"]["long"])

    def test_unknown_funding_interval_is_treated_as_eight_hours(self):
        cfg = valid_config()
        threshold = cfg["strategy"]["funding_extreme_pct_per_8h"]
        for interval in (None, 0, -4):
            evidence = strategy.setup_evidence(
                snapshot(funding_rate_pct=-threshold,
                         funding_interval_hours=interval,
                         funding_percentile_30=10,
                         perp_index_basis_pct=-0.1,
                         mom_15m_pct=0.2,
                         price_stabilized_long=True),
                cfg)
            self.assertTrue(
                evidence["funding_squeeze"]["long"],
                f"interval {interval!r} should fall back to 8h, not scale")

    def test_funding_threshold_is_reachable_on_liquid_instruments(self):
        """Guard the regression this fix was made for.

        OKX clamps funding on liquid perpetuals at about 0.01% per 8h. A
        threshold above that makes funding_squeeze dead code on every major,
        which is what the shipped 0.03 raw value did.
        """
        threshold = valid_config()["strategy"]["funding_extreme_pct_per_8h"]
        self.assertLessEqual(
            threshold, 0.01,
            "threshold exceeds the rate OKX clamps liquid perps at, so the "
            "funding_squeeze setup could never fire on BTC/ETH/XRP/DOGE")

    def test_experimental_setup_is_demo_only(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        plan, why = strategy.build_setup_plan(
            decision(setup_type="other", invalidation_anchor="atr"),
            snapshot(), cfg)
        self.assertIsNone(plan)
        self.assertEqual(
            why, "experimental setups are allowed only in demo mode")

    def test_experimental_setup_has_separate_attribution_identity(self):
        plan, why = strategy.build_setup_plan(
            decision(setup_type="other", invalidation_anchor="atr"),
            snapshot(), valid_config())
        self.assertIsNone(why)
        self.assertEqual(plan["strategy_id"], "momentum-experimental")
        self.assertIn("experimental", plan["strategy_version"])

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

    def test_failed_thesis_reentry_requires_time_new_hour_and_changed_evidence(self):
        cfg = valid_config()
        first, _ = strategy.build_setup_plan(
            decision(), snapshot(), cfg)
        record = strategy.new_setup_record(first, cfg, now=100)
        strategy.mark_setup(
            record, "closed", cfg, now=200,
            realized_pnl_usd=-10)
        records = {first["setup_id"]: record}

        retry, _ = strategy.build_setup_plan(
            decision(
                what_changed_since_last_loss=(
                    "relative volume expanded into a new impulse")),
            snapshot(
                signal_ts=2_000, signal_1h_ts=1_800,
                relative_volume_1h=2.2),
            cfg,
        )
        self.assertIn(
            "more minute",
            strategy.failed_thesis_reentry_reason(
                records, retry, cfg, now=201))
        self.assertIsNone(
            strategy.failed_thesis_reentry_reason(
                records, retry, cfg, now=4_000))

        unchanged, _ = strategy.build_setup_plan(
            decision(
                what_changed_since_last_loss=(
                    "I want to try the same setup again")),
            snapshot(signal_ts=2_000, signal_1h_ts=1_800),
            cfg,
        )
        self.assertIn(
            "changed objective evidence",
            strategy.failed_thesis_reentry_reason(
                records, unchanged, cfg, now=4_000))


if __name__ == "__main__":
    unittest.main()
