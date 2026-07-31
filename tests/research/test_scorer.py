"""B4: one scorer, a funnel that reconciles, and a sweep that can refuse.

The scorer's most important behaviour is not computing expectancy. It is
declining to rank when the sample cannot support a ranking. Four consecutive
INSUFFICIENT_SAMPLE verdicts will feel like failure and will invite relaxing
the rule, and that moment is exactly what the rule is for.
"""

import unittest

from research import score, stats, sweep
from research.replay import ReplayDecision, ReplayResult


def decision(r_multiple, **overrides):
    d = ReplayDecision(
        cycle_id="c1", ts=1.0, symbol="BTC/USDT:USDT", signal_ts=1,
        stage="executed", direction="long", setup_type="range_breakout")
    d.outcome = {"r_multiple": r_multiple, "result": "target"}
    for key, value in overrides.items():
        setattr(d, key, value)
    return d


class OneScorerTests(unittest.TestCase):
    """report.py must use this module, not a second implementation."""

    def test_report_imports_the_shared_matcher(self):
        import report
        self.assertIs(report.match_round_trips, score.match_round_trips)


class ScoreReturnsTests(unittest.TestCase):
    def test_expectancy_and_win_rate(self):
        result = score.score_returns([2.0, -1.0, 2.0, -1.0])

        self.assertAlmostEqual(result["expectancy_r"], 0.5, 6)
        self.assertAlmostEqual(result["win_rate"], 50.0, 6)
        self.assertAlmostEqual(result["total_r"], 2.0, 6)

    def test_profit_factor(self):
        result = score.score_returns([3.0, -1.0])
        self.assertAlmostEqual(result["profit_factor"], 3.0, 6)

    def test_profit_factor_with_no_losses_is_infinite_not_a_crash(self):
        self.assertEqual(score.score_returns([1.0, 2.0])["profit_factor"],
                         float("inf"))

    def test_max_drawdown_is_measured_on_the_cumulative_curve(self):
        # +2, -3, +1 -> peak 2, trough -1, drawdown 3.
        result = score.score_returns([2.0, -3.0, 1.0])
        self.assertAlmostEqual(result["max_drawdown_r"], 3.0, 6)

    def test_a_small_sample_refuses_to_be_ranked(self):
        result = score.score_returns([2.0, -1.0] * 6)     # n = 12

        self.assertEqual(result["verdict"], stats.INSUFFICIENT_SAMPLE)
        self.assertEqual(result["n"], 12)

    def test_a_sufficient_sample_is_scored(self):
        result = score.score_returns([1.0, -1.0] * 60)    # n = 120
        self.assertEqual(result["verdict"], "SCORED")

    def test_an_empty_sample_is_insufficient_not_zero(self):
        result = score.score_returns([])

        self.assertEqual(result["verdict"], stats.INSUFFICIENT_SAMPLE)
        self.assertEqual(result["mde_r"], float("inf"))

    def test_every_result_carries_an_interval(self):
        result = score.score_returns([1.0, -0.5, 2.0, -1.0] * 5)

        self.assertLessEqual(result["ci_low"], result["expectancy_r"])
        self.assertLessEqual(result["expectancy_r"], result["ci_high"])

    def test_non_finite_returns_are_dropped_not_propagated(self):
        result = score.score_returns([1.0, float("nan"), 2.0, None])
        self.assertEqual(result["n"], 2)


class FunnelTests(unittest.TestCase):
    def _result(self, fired, vetoed, executed, reasons=None):
        r = ReplayResult(variant_id="v", mode="deterministic")
        r.funnel = {"fired": fired, "proposed": fired, "vetoed": vetoed,
                    "executed": executed, "veto_reasons": reasons or {}}
        return r

    def test_a_reconciling_funnel_is_flagged_as_such(self):
        funnel = score.funnel_from_replay(self._result(100, 90, 10))
        self.assertTrue(funnel["reconciles"])

    def test_a_funnel_that_does_not_add_up_is_flagged(self):
        """Arithmetic that looks like a measurement is worse than no funnel."""
        funnel = score.funnel_from_replay(self._result(100, 90, 5))

        self.assertFalse(funnel["reconciles"])
        self.assertIn("does not reconcile", score.format_funnel(funnel))

    def test_dominant_veto_is_identified_with_its_share(self):
        funnel = score.funnel_from_replay(self._result(
            100, 90, 10, {"net directional cap": 60,
                          "confidence below floor": 20,
                          "already holding": 10}))

        reason, share = score.dominant_veto(funnel)

        self.assertEqual(reason, "net directional cap")
        self.assertAlmostEqual(share, 66.667, 2)

    def test_gate_g4_threshold_is_readable_from_the_share(self):
        """G4: over 30% promotes BTC-residual analysis to first priority."""
        funnel = score.funnel_from_replay(self._result(
            100, 90, 10, {"net directional cap": 40, "other": 50}))

        _, share = score.dominant_veto(funnel)
        self.assertGreater(share, 30.0)

    def test_no_vetoes_is_not_a_division_by_zero(self):
        reason, share = score.dominant_veto(
            score.funnel_from_replay(self._result(10, 0, 10)))

        self.assertIsNone(reason)
        self.assertEqual(share, 0.0)


class SweepSpecTests(unittest.TestCase):
    def test_a_spec_without_a_hypothesis_is_refused(self):
        import tempfile
        from pathlib import Path
        from agent.config import ConfigError

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.yaml"
            path.write_text("base: momentum.baseline\naxis: {}\n")
            with self.assertRaises(ConfigError):
                sweep.load_spec(path)

    def test_a_condition_axis_must_be_pre_registered(self):
        import tempfile
        from pathlib import Path
        from agent.config import ConfigError

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.yaml"
            path.write_text(
                "hypothesis: buckets chosen after the fact\n"
                "base: momentum.baseline\n"
                "condition_axis:\n"
                "  variable: utc_hour\n"
                "  buckets: [{name: a, min: 0, max: 12}]\n")
            with self.assertRaises(ConfigError):
                sweep.load_spec(path)

    def test_the_shipped_specs_all_load(self):
        from pathlib import Path
        for path in sorted(Path("research/sweeps").glob("*.yaml")):
            spec = sweep.load_spec(path)
            self.assertTrue(spec.hypothesis)

    def test_a_conditioning_spec_has_one_grid_point(self):
        spec = sweep.load_spec("research/sweeps/regime_conditioning.yaml")

        self.assertTrue(spec.is_conditioning())
        self.assertEqual(spec.grid_points(), 1)

    def test_a_parameter_spec_counts_its_grid(self):
        spec = sweep.load_spec("research/sweeps/exit_policy.yaml")

        self.assertFalse(spec.is_conditioning())
        self.assertEqual(spec.grid_points(), 4)


class SweepForecastTests(unittest.TestCase):
    """C4: refuse before running, not five times during."""

    def test_a_parameter_sweep_on_a_small_corpus_refuses(self):
        spec = sweep.load_spec("research/sweeps/exit_policy.yaml")

        outlook = sweep.forecast(spec, total_round_trips=38)

        self.assertFalse(outlook["should_run"])
        self.assertIn("100 are required", outlook["reason"])

    def test_expand_and_grid_points_agree(self):
        """A union would under-report the grid and overstate per-cell n."""
        for name in ("exit_policy", "regime_conditioning"):
            spec = sweep.load_spec(f"research/sweeps/{name}.yaml")
            expected = 1 if spec.is_conditioning() else spec.grid_points()
            self.assertEqual(len(sweep.expand(spec, {})), expected, name)

    def test_the_same_sweep_runs_on_a_large_corpus(self):
        spec = sweep.load_spec("research/sweeps/exit_policy.yaml")
        self.assertTrue(sweep.forecast(spec, 900)["should_run"])

    def test_a_conditioning_sweep_runs_on_any_corpus(self):
        """It reuses trades rather than dividing them."""
        spec = sweep.load_spec("research/sweeps/regime_conditioning.yaml")

        outlook = sweep.forecast(spec, total_round_trips=38)

        self.assertTrue(outlook["should_run"])
        self.assertIn("no sample divided", outlook["reason"])


class SweepExpansionTests(unittest.TestCase):
    def test_a_parameter_axis_generates_one_variant_per_point(self):
        spec = sweep.load_spec("research/sweeps/exit_policy.yaml")

        generated = sweep.expand(spec, {})

        self.assertEqual(len(generated), 4)
        self.assertEqual(
            sorted(v.variant_id for v in generated),
            ["momentum.fixed_reward_risk.1_5",
             "momentum.fixed_reward_risk.2_0",
             "momentum.fixed_reward_risk.2_5",
             "momentum.fixed_reward_risk.3_0"])

    def test_generated_variants_carry_the_sweeps_hypothesis(self):
        spec = sweep.load_spec("research/sweeps/exit_policy.yaml")
        for variant in sweep.expand(spec, {}):
            self.assertEqual(variant.hypothesis, spec.hypothesis)

    def test_generated_variants_apply_to_the_real_config(self):
        from agent import variants as variant_mod
        from tests.helpers import valid_config

        spec = sweep.load_spec("research/sweeps/exit_policy.yaml")
        for variant in sweep.expand(spec, {}):
            cfg = variant_mod.apply(variant, valid_config())
            self.assertIn(cfg["strategy"]["fixed_reward_risk"],
                          [1.5, 2.0, 2.5, 3.0])

    def test_a_conditioning_axis_generates_no_new_variant(self):
        spec = sweep.load_spec("research/sweeps/regime_conditioning.yaml")
        self.assertEqual(len(sweep.expand(spec, {})), 1)


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.axis = sweep.load_spec(
            "research/sweeps/regime_conditioning.yaml").condition_axis

    def test_values_land_in_their_pre_registered_buckets(self):
        self.assertEqual(self.axis.bucket_for(0.5), "compression")
        self.assertEqual(self.axis.bucket_for(1.0), "neutral")
        self.assertEqual(self.axis.bucket_for(2.0), "expansion")

    def test_boundaries_are_half_open_and_do_not_double_count(self):
        self.assertEqual(self.axis.bucket_for(0.85), "neutral")
        self.assertEqual(self.axis.bucket_for(1.20), "expansion")

    def test_a_missing_value_is_excluded_rather_than_guessed(self):
        self.assertIsNone(self.axis.bucket_for(None))

    def test_partition_splits_decisions_by_bucket(self):
        decisions = [decision(1.0), decision(-1.0), decision(2.0)]
        values = {id(decisions[0]): 0.5, id(decisions[1]): 1.0,
                  id(decisions[2]): 3.0}

        buckets = sweep.partition(
            decisions, self.axis, lambda d: values[id(d)])

        self.assertEqual(set(buckets), {"compression", "neutral",
                                        "expansion"})

    def test_every_bucket_is_scored_including_the_null_ones(self):
        """A programme that records only positives records only noise."""
        buckets = {"compression": [decision(1.0)], "expansion": []}

        scored = sweep.score_partition(buckets)

        self.assertEqual(set(scored), {"compression", "expansion"})
        self.assertEqual(scored["expansion"]["n"], 0)
        self.assertEqual(scored["expansion"]["verdict"],
                         stats.INSUFFICIENT_SAMPLE)


if __name__ == "__main__":
    unittest.main()
