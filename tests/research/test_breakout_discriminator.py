"""6.4 as a question rather than a decision.

`range_breakout` and `trend_continuation` overlap: a symbol in a strong
aligned uptrend is almost always high in its 24h range on elevated volume, so
both fire and which label a trade receives depends on which word the model
chose. Attributing performance by `setup_type` then splits one phenomenon
across two rows and draws conclusions from the split.

What they should be separated *by* is genuinely open. The implementation plan
says trend alignment; the competing rationale says the real partition is
volatility regime and trend alignment is a correlated proxy for it.

Baking either one in would filter the population the other one needs, which
is exactly why gate G5 held this batch. Making it a parameter dissolves the
gate: the corpus decides, and until it has decided the default is `none`,
which is the shipped behaviour.
"""

import unittest

from agent import strategy
from agent.config import ConfigError, validate_config
from tests.helpers import valid_config


def snapshot(**overrides) -> dict:
    """A row where breakout and continuation BOTH fire under `none`."""
    base = {
        "price": 100.0, "signal_ts": 1_760_000_000_000,
        "trend_15m": "up", "trend_1h": "up", "trend_4h": "up",
        "mom_1h_pct": 1.2, "mom_15m_pct": 0.6,
        "atr_1h_pct": 0.8, "atr_1h_ratio": 0.8,
        "ema20_1h_dist_pct": 0.2,
        "range_pos_pct": 95.0, "relative_volume_1h": 1.8,
        "fresh_breakout_long": True, "fresh_breakout_short": False,
        "price_stabilized_long": True, "price_stabilized_short": False,
        "funding_rate_pct": 0.005, "funding_interval_hours": 8,
        "funding_percentile_30": 50, "funding_samples_30": 30,
        "perp_index_basis_pct": 0.01, "open_interest_musd": 500.0,
        "swing_high_pct": 2.0, "swing_low_pct": 2.0, "spread_pct": 0.02,
    }
    base.update(overrides)
    return base


def fires(cfg, snap, setup, direction="long") -> bool:
    evidence = strategy.setup_evidence(snap, cfg)
    contract = evidence.get(setup)
    return isinstance(contract, dict) and contract.get(direction) is True


def cfg_with(discriminator, **strategy_overrides):
    cfg = valid_config()
    cfg["strategy"]["breakout_discriminator"] = discriminator
    cfg["strategy"].update(strategy_overrides)
    return cfg


class TheOverlapExistsTests(unittest.TestCase):
    """The defect 6.4 addresses, demonstrated before it is fixed."""

    def test_both_contracts_fire_on_the_same_row_by_default(self):
        cfg = cfg_with("none")
        snap = snapshot()

        self.assertTrue(fires(cfg, snap, "range_breakout"))
        self.assertTrue(fires(cfg, snap, "trend_continuation"))

    def test_none_is_the_shipped_default(self):
        cfg = validate_config(valid_config())
        self.assertEqual(cfg["strategy"]["breakout_discriminator"], "none")


class TrendAlignmentDiscriminatorTests(unittest.TestCase):
    """A breakout is a transition OUT of chop."""

    def test_an_aligned_trend_no_longer_counts_as_a_breakout(self):
        cfg = cfg_with("trend_alignment")
        snap = snapshot()                       # 1h and 4h both up

        self.assertFalse(fires(cfg, snap, "range_breakout"))
        self.assertTrue(fires(cfg, snap, "trend_continuation"))

    def test_a_breakout_out_of_chop_still_fires(self):
        cfg = cfg_with("trend_alignment")
        snap = snapshot(trend_4h="flat")        # no 1h/4h alignment

        self.assertTrue(fires(cfg, snap, "range_breakout"))
        self.assertFalse(fires(cfg, snap, "trend_continuation"))

    def test_the_two_are_mutually_exclusive_across_trend_states(self):
        cfg = cfg_with("trend_alignment")
        for trend_1h, trend_4h in (("up", "up"), ("up", "flat"),
                                   ("flat", "up"), ("flat", "flat")):
            snap = snapshot(trend_1h=trend_1h, trend_4h=trend_4h)
            both = (fires(cfg, snap, "range_breakout")
                    and fires(cfg, snap, "trend_continuation"))
            self.assertFalse(
                both, f"both fired at 1h={trend_1h}, 4h={trend_4h}")

    def test_the_short_side_mirrors(self):
        cfg = cfg_with("trend_alignment")
        aligned = snapshot(
            trend_15m="down", trend_1h="down", trend_4h="down",
            mom_1h_pct=-1.2, mom_15m_pct=-0.6, range_pos_pct=5.0,
            fresh_breakout_long=False, fresh_breakout_short=True)

        self.assertFalse(fires(cfg, aligned, "range_breakout", "short"))
        self.assertTrue(fires(cfg, aligned, "trend_continuation", "short"))


class VolatilityRegimeDiscriminatorTests(unittest.TestCase):
    """The volatility partition is compression versus expansion."""

    def test_a_compressed_base_still_breaks_out(self):
        cfg = cfg_with("volatility_regime")
        snap = snapshot(atr_1h_ratio=0.7)

        self.assertTrue(fires(cfg, snap, "range_breakout"))

    def test_an_already_elevated_regime_does_not(self):
        """An ordinary excursion in a distribution that is already wide."""
        cfg = cfg_with("volatility_regime")
        snap = snapshot(atr_1h_ratio=1.9)

        self.assertFalse(fires(cfg, snap, "range_breakout"))

    def test_the_threshold_is_configurable(self):
        snap = snapshot(atr_1h_ratio=1.4)

        self.assertFalse(fires(
            cfg_with("volatility_regime"), snap, "range_breakout"))
        self.assertTrue(fires(
            cfg_with("volatility_regime",
                     breakout_compression_max_atr_ratio=1.5),
            snap, "range_breakout"))

    def test_unknown_volatility_does_not_read_as_compressed(self):
        """A missing measurement must never satisfy a condition."""
        cfg = cfg_with("volatility_regime")

        self.assertFalse(fires(
            cfg, snapshot(atr_1h_ratio=None), "range_breakout"))

    def test_continuation_is_untouched_by_this_discriminator(self):
        """It separates by gating the breakout, not by moving both."""
        cfg = cfg_with("volatility_regime")

        for ratio in (0.5, 1.0, 2.5):
            self.assertTrue(fires(
                cfg, snapshot(atr_1h_ratio=ratio), "trend_continuation"))


class TheTwoAnswersDifferTests(unittest.TestCase):
    """If they agreed everywhere, there would be nothing to test."""

    def test_a_row_the_two_discriminators_disagree_about(self):
        # Aligned trend, but compressed volatility. trend_alignment refuses
        # the breakout; volatility_regime allows it.
        snap = snapshot(trend_1h="up", trend_4h="up", atr_1h_ratio=0.6)

        self.assertFalse(fires(
            cfg_with("trend_alignment"), snap, "range_breakout"))
        self.assertTrue(fires(
            cfg_with("volatility_regime"), snap, "range_breakout"))

    def test_the_reverse_disagreement_also_exists(self):
        # Chop, but expanded volatility. trend_alignment allows it;
        # volatility_regime refuses it.
        snap = snapshot(trend_4h="flat", atr_1h_ratio=2.0)

        self.assertTrue(fires(
            cfg_with("trend_alignment"), snap, "range_breakout"))
        self.assertFalse(fires(
            cfg_with("volatility_regime"), snap, "range_breakout"))


class ConfigTests(unittest.TestCase):
    def test_an_unknown_discriminator_is_refused(self):
        raw = valid_config()
        raw["strategy"]["breakout_discriminator"] = "vibes"

        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_every_registered_discriminator_validates(self):
        for name in ("none", "trend_alignment", "volatility_regime"):
            raw = valid_config()
            raw["strategy"]["breakout_discriminator"] = name
            self.assertEqual(
                validate_config(raw)["strategy"]["breakout_discriminator"],
                name)

    def test_the_compression_threshold_is_bounded(self):
        raw = valid_config()
        raw["strategy"]["breakout_compression_max_atr_ratio"] = 99
        with self.assertRaises(ConfigError):
            validate_config(raw)


class RegisteredForComparisonTests(unittest.TestCase):
    """Both answers are variants, so the corpus can rank them."""

    def test_both_discriminators_are_registered_variants(self):
        from agent import variants

        registry = variants.load_registry("research/variants.yaml")

        self.assertIn("momentum.discriminator.trend_alignment", registry)
        self.assertIn("momentum.discriminator.volatility_regime", registry)

    def test_each_variant_applies_to_the_real_config(self):
        from agent import variants

        registry = variants.load_registry("research/variants.yaml")
        for name in ("trend_alignment", "volatility_regime"):
            variant = registry[f"momentum.discriminator.{name}"]
            cfg = variants.apply(variant, valid_config())
            self.assertEqual(
                cfg["strategy"]["breakout_discriminator"], name)

    def test_the_sweep_spec_covers_all_three(self):
        from research import sweep

        spec = sweep.load_spec(
            "research/sweeps/breakout_discriminator.yaml")

        self.assertEqual(spec.grid_points(), 3)

    def test_the_sweep_refuses_an_underpowered_corpus(self):
        """Three settings need 300 round trips before any ranking means much."""
        from research import sweep

        spec = sweep.load_spec(
            "research/sweeps/breakout_discriminator.yaml")

        self.assertFalse(sweep.forecast(spec, 38)["should_run"])
        self.assertTrue(sweep.forecast(spec, 900)["should_run"])


if __name__ == "__main__":
    unittest.main()
