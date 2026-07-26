import unittest

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

    def test_unimplemented_strategy_id_is_rejected(self):
        cfg = valid_config()
        cfg["strategy"]["id"] = "scalping"
        with self.assertRaisesRegex(ConfigError, "must be 'momentum'"):
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


if __name__ == "__main__":
    unittest.main()
