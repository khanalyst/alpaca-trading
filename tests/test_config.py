import unittest
from unittest.mock import patch

from agent.config import ConfigError, validate_config
from tests.helpers import valid_config


class ConfigValidationTests(unittest.TestCase):
    def test_valid_configuration_is_defensively_copied(self):
        original = valid_config()
        result = validate_config(original)
        self.assertEqual(result["mode"], "demo")
        result["risk"]["max_leverage"] = 9
        self.assertEqual(original["risk"]["max_leverage"], 3)

    def test_unknown_mode_fails_instead_of_becoming_live(self):
        cfg = valid_config()
        cfg["mode"] = "production"
        with self.assertRaisesRegex(ConfigError, "exactly 'demo' or 'live'"):
            validate_config(cfg)

    def test_full_book_may_not_sit_on_top_of_the_margin_guard(self):
        # 3 positions x 40% notional / 2x leverage = 60% initial margin, which
        # is exactly max_margin_usage_pct. Any unrealized loss then trips the
        # guard and force-closes the largest position for configuration
        # reasons rather than strategy ones.
        cfg = valid_config()
        cfg["risk"]["max_position_notional_pct"] = 40
        with self.assertRaisesRegex(ConfigError, "no safe headroom"):
            validate_config(cfg)

    def test_shipped_full_book_margin_keeps_headroom(self):
        cfg = validate_config(valid_config())
        risk = cfg["risk"]
        full_book = (risk["max_concurrent_positions"]
                     * risk["max_position_notional_pct"]
                     / risk["entry_leverage"])
        self.assertLessEqual(full_book, risk["max_margin_usage_pct"] * 0.8)

    def test_margin_headroom_can_be_bought_with_fewer_positions(self):
        # The same 40% notional is safe with a smaller book, so the rule
        # constrains the product and not one parameter in isolation.
        cfg = valid_config()
        cfg["risk"]["max_position_notional_pct"] = 40
        cfg["risk"]["max_concurrent_positions"] = 2
        self.assertEqual(
            validate_config(cfg)["risk"]["max_concurrent_positions"], 2)

    def test_margin_headroom_can_be_bought_with_a_higher_guard(self):
        cfg = valid_config()
        cfg["risk"]["max_position_notional_pct"] = 40
        cfg["risk"]["max_margin_usage_pct"] = 75
        self.assertEqual(
            validate_config(cfg)["risk"]["max_margin_usage_pct"], 75)

    def test_multi_day_holds_are_not_reachable(self):
        # A day-trading system may run a position into a second session but
        # not for days: weekend gaps, accumulated funding and overnight news
        # are a different risk profile than the one this strategy was tested
        # against.
        cfg = valid_config()
        cfg["risk"]["max_hold_hours"] = 72
        with self.assertRaisesRegex(ConfigError, "between 0.25 and 48"):
            validate_config(cfg)

    def test_forty_eight_hour_hold_is_allowed(self):
        cfg = valid_config()
        cfg["risk"]["max_hold_hours"] = 48
        self.assertEqual(validate_config(cfg)["risk"]["max_hold_hours"], 48)

    def test_renamed_funding_threshold_rejects_the_stale_key(self):
        # The threshold changed meaning from a raw per-interval rate to an
        # 8h-equivalent one. A stale key must fail loudly rather than be
        # silently reinterpreted as a much stricter bar.
        cfg = valid_config()
        del cfg["strategy"]["funding_extreme_pct_per_8h"]
        cfg["strategy"]["funding_extreme_pct"] = 0.03
        with self.assertRaisesRegex(ConfigError, "unknown field"):
            validate_config(cfg)

    def test_unsafe_leverage_is_rejected(self):
        cfg = valid_config()
        cfg["risk"]["max_leverage"] = 11
        with self.assertRaisesRegex(ConfigError, "between 1 and 10"):
            validate_config(cfg)

    def test_fractional_leverage_is_rejected_before_exchange_rounding(self):
        cfg = valid_config()
        cfg["risk"]["max_leverage"] = 2.9
        with self.assertRaisesRegex(ConfigError, "must be an integer"):
            validate_config(cfg)

    def test_missing_new_schema_block_is_rejected(self):
        cfg = valid_config()
        del cfg["trading_costs"]
        with self.assertRaisesRegex(ConfigError, "trading_costs must be"):
            validate_config(cfg)

    def test_unknown_field_is_rejected_as_likely_typo(self):
        cfg = valid_config()
        cfg["risk"]["max_leverge"] = 4
        with self.assertRaisesRegex(ConfigError, "unknown field.*max_leverge"):
            validate_config(cfg)

    def test_liquidity_depth_buffer_cannot_exceed_visible_depth(self):
        cfg = valid_config()
        cfg["execution"]["liquidity_depth_buffer_pct"] = 101
        with self.assertRaisesRegex(ConfigError, "between 10 and 100"):
            validate_config(cfg)

    def test_liquidity_retry_count_must_be_an_integer(self):
        cfg = valid_config()
        cfg["execution"]["liquidity_retries_before_backoff"] = 1.5
        with self.assertRaisesRegex(ConfigError, "must be an integer"):
            validate_config(cfg)

    def test_missing_paper_maker_ttl_defaults_to_full_candle_window(self):
        cfg = valid_config()
        del cfg["execution"]["paper_maker_order_ttl_seconds"]

        self.assertEqual(
            validate_config(cfg)["execution"]
            ["paper_maker_order_ttl_seconds"],
            120.0)

    def test_paper_maker_ttl_cannot_be_below_full_candle_window(self):
        cfg = valid_config()
        cfg["execution"]["paper_maker_order_ttl_seconds"] = 119

        with self.assertRaisesRegex(ConfigError, "between 120 and 300"):
            validate_config(cfg)

    def test_history_requirement_cannot_exceed_snapshot_candles(self):
        cfg = valid_config()
        cfg["universe"]["min_history_candles"] = 121
        with self.assertRaisesRegex(
                ConfigError, "cannot exceed cycle.candles"):
            validate_config(cfg)

    def test_maintenance_margin_ratio_must_stay_above_liquidation(self):
        cfg = valid_config()
        cfg["risk"]["min_maintenance_margin_ratio"] = 1.0
        with self.assertRaisesRegex(ConfigError, "between 1.01 and 100"):
            validate_config(cfg)

    def test_entry_failure_backoff_bounds_are_consistent(self):
        cfg = valid_config()
        cfg["execution"]["entry_failure_backoff_minutes"] = 30
        cfg["execution"]["entry_failure_backoff_max_minutes"] = 15
        with self.assertRaisesRegex(ConfigError, "cannot be below"):
            validate_config(cfg)

    def test_unregistered_strategy_id_is_rejected(self):
        cfg = valid_config()
        cfg["strategy"]["id"] = "does-not-exist"
        with self.assertRaisesRegex(ConfigError, "is not registered"):
            validate_config(cfg)

    def test_registered_but_unimplemented_strategy_is_rejected(self):
        # Registered for research is not the same as runnable. Every entry
        # in the register carries a mechanism and a tier; only some carry a
        # live contract.
        cfg = valid_config()
        cfg["strategy"]["id"] = "scalp-maker"
        cfg["strategy"]["version"] = "v1"
        with self.assertRaisesRegex(
                ConfigError, "no live contract implementation"):
            validate_config(cfg)

    def test_shadow_contract_without_analyst_schema_is_not_runnable(self):
        cfg = valid_config()
        cfg["strategy"]["id"] = "flush-fade"
        cfg["strategy"]["version"] = "v1"
        with self.assertRaisesRegex(ConfigError, "analyst prompt/schema"):
            validate_config(cfg)

    def test_strategy_version_must_match_the_registered_spec(self):
        cfg = valid_config()
        cfg["strategy"]["version"] = "phase1-v99-unregistered"
        with self.assertRaisesRegex(
                ConfigError, "needs its own registry entry"):
            validate_config(cfg)

    def test_deterministic_entry_leverage_cannot_exceed_cap(self):
        cfg = valid_config()
        cfg["risk"]["entry_leverage"] = 4
        with self.assertRaisesRegex(ConfigError, "cannot exceed"):
            validate_config(cfg)

    def test_strategy_signal_timeframe_must_be_available(self):
        cfg = valid_config()
        cfg["strategy"]["signal_timeframe"] = "5m"
        with self.assertRaisesRegex(ConfigError, "must be exactly '15m'"):
            validate_config(cfg)

    def test_experimental_risk_budget_cannot_exceed_primary_budget(self):
        cfg = valid_config()
        cfg["risk"]["experimental_risk_per_trade_pct"] = 2
        with self.assertRaisesRegex(ConfigError, "cannot exceed"):
            validate_config(cfg)

    def test_total_open_risk_cannot_exceed_daily_loss_stop(self):
        cfg = valid_config()
        cfg["risk"]["max_total_open_risk_pct"] = 6
        with self.assertRaisesRegex(
                ConfigError, "cannot exceed daily_loss_limit_pct"):
            validate_config(cfg)

    def test_same_direction_cap_cannot_exceed_concurrent_cap(self):
        cfg = valid_config()
        cfg["risk"]["max_same_direction_positions"] = 4
        with self.assertRaisesRegex(
                ConfigError, "cannot exceed max_concurrent_positions"):
            validate_config(cfg)

    def test_minimum_hold_cannot_outlast_the_max_hold_timer(self):
        # A close floor above the force-close ceiling would mean no model
        # close is ever permitted and every position exits on the clock.
        cfg = valid_config()
        cfg["risk"]["max_hold_hours"] = 1
        cfg["strategy"]["min_hold_minutes"] = 90
        with self.assertRaisesRegex(
                ConfigError, "min_hold_minutes must be below"):
            validate_config(cfg)

    def test_minimum_hold_below_the_timer_is_accepted(self):
        cfg = valid_config()
        cfg["risk"]["max_hold_hours"] = 2
        cfg["strategy"]["min_hold_minutes"] = 90
        self.assertEqual(
            validate_config(cfg)["strategy"]["min_hold_minutes"], 90)


class LiveTierGateTests(unittest.TestCase):
    """A measured-negative strategy must not reach live capital.

    The register records that momentum/phase1-v2 is T0_REJECTED. Demo may
    still run it - paper trading is a legitimate operations rehearsal - but
    switching mode to live has to fail, loudly and with the reason.
    """

    def test_rejected_strategy_cannot_run_live(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        with self.assertRaisesRegex(
                ConfigError, "tier T0_REJECTED and mode is live"):
            validate_config(cfg)

    def test_the_refusal_states_the_evidence(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        with self.assertRaises(ConfigError) as caught:
            validate_config(cfg)
        self.assertIn("45.6-47.3%", str(caught.exception))

    def test_same_strategy_is_allowed_on_demo(self):
        cfg = valid_config()
        cfg["mode"] = "demo"
        self.assertEqual(validate_config(cfg)["mode"], "demo")


class PerStrategyBoundsTests(unittest.TestCase):
    """Timeframe and holding period come from the spec, not from constants."""

    def test_hold_ceiling_comes_from_the_registered_spec(self):
        cfg = valid_config()
        cfg["risk"]["max_hold_hours"] = 72  # above momentum's 48h ceiling
        with self.assertRaisesRegex(
                ConfigError, "max_hold_hours must be between"):
            validate_config(cfg)

    def test_signal_timeframe_must_match_the_spec(self):
        cfg = valid_config()
        cfg["cycle"]["timeframes"] = ["5m", "15m", "1h", "4h"]
        cfg["strategy"]["signal_timeframe"] = "5m"
        with self.assertRaisesRegex(
                ConfigError, "must be exactly '15m' for strategy.id"):
            validate_config(cfg)

    def test_missing_required_timeframe_is_named(self):
        cfg = valid_config()
        cfg["cycle"]["timeframes"] = ["15m", "1h"]
        with self.assertRaisesRegex(ConfigError, "missing: 4h"):
            validate_config(cfg)


class MakerFirstGuardTests(unittest.TestCase):
    """The B7.5 flag is demo-scoped until the gate that judges it passes.

    Enabling maker-first on demo is what lets gate B7.5 collect its 20
    attempts; leaving it off means READY forever and the largest measured
    cost lever stays unmeasured. But a demo config that carries the flag is
    one `mode:` edit away from putting an unvalidated entry path in front of
    real capital, and nothing else in the config stops that.
    """

    def test_demo_may_enable_maker_first(self):
        cfg = valid_config()
        cfg["execution"]["maker_first_enabled"] = True
        self.assertTrue(
            validate_config(cfg)["execution"]["maker_first_enabled"])

    def test_the_tier_gate_still_speaks_first_in_live(self):
        """No registered strategy meets T3, so live stops before this rule.

        The maker-first guard is defence for the day one does, not a
        substitute for the tier gate. If this ever starts reporting B7.5
        instead, the ordering has regressed and the more important refusal
        has been masked.
        """
        cfg = valid_config()
        cfg["mode"] = "live"
        cfg["execution"]["maker_first_enabled"] = True
        with self.assertRaisesRegex(ConfigError, "requires T3_VALIDATED"):
            validate_config(cfg)

    def test_live_refuses_maker_first_once_the_tier_gate_is_satisfied(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        cfg["execution"]["maker_first_enabled"] = True
        # Stand in for the future in which a strategy has cleared the tier
        # bar, which is the only world where this guard is reachable.
        with patch("agent.config.LIVE_MIN_TIER", "T0_REJECTED"):
            with self.assertRaisesRegex(ConfigError, "B7.5"):
                validate_config(cfg)

    def test_live_is_allowed_when_maker_first_is_off(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        cfg["execution"]["maker_first_enabled"] = False
        with patch("agent.config.LIVE_MIN_TIER", "T0_REJECTED"):
            self.assertEqual(validate_config(cfg)["mode"], "live")

    def test_the_shipped_config_enables_it_so_the_gate_can_move(self):
        import yaml
        from pathlib import Path
        shipped = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.yaml").read_text())
        self.assertEqual(shipped["mode"], "demo")
        self.assertTrue(shipped["execution"]["maker_first_enabled"])


if __name__ == "__main__":
    unittest.main()
