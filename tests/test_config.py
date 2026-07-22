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


if __name__ == "__main__":
    unittest.main()
