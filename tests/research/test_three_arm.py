"""B3.5: does the LLM earn its keep - as a selector, as a vetoer, or at all?

The two-arm framing in the original plan cannot answer this. It asks whether
the LLM beats the deterministic null, with two arms, and the most plausible
truth is neither. A language model is poorly suited to picking setups out of
numeric snapshots, which deterministic code does better and more cheaply. It
is much better suited to rejecting setups that satisfy the contract and sit
in an obviously bad context.

That is a veto function, and a two-arm test cannot see it: if the model is a
good vetoer and a poor selector, the pooled LLM arm looks mediocre and the
conclusion drawn is "the LLM does not earn its keep" - the wrong conclusion
from the right data, and an expensive one, because it would remove the part
that was working.

Arm C costs nothing extra. It is a different join over event streams already
recorded.
"""

import unittest

from research import replay, stats
from tests.research.test_replay_determinism import (cycle, model_output,
                                                    open_decision, row)
from tests.helpers import valid_config


class ThreeArmModeTests(unittest.TestCase):
    def test_all_three_modes_are_supported(self):
        self.assertEqual(
            set(replay.MODES),
            {"deterministic", "recorded_llm", "deterministic_vetoed"})

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            replay.Replay(valid_config(), mode="vibes")


class VetoArmTests(unittest.TestCase):
    """Arm C: the contract proposes, the model may only suppress."""

    def test_a_declined_symbol_is_suppressed(self):
        cycles = [cycle(0)]
        outputs = [model_output(0, [])]

        fired = replay.Replay(valid_config(), mode="deterministic").run(
            cycles, [])
        vetoed = replay.Replay(
            valid_config(), mode="deterministic_vetoed").run(cycles, outputs)

        self.assertGreater(fired.funnel["fired"], 0)
        self.assertEqual(vetoed.funnel["fired"], 0,
                         "the model declined, so nothing is proposed")

    def test_a_confirmed_symbol_is_kept(self):
        cycles = [cycle(0)]
        outputs = [model_output(0, [open_decision(direction="long")])]

        result = replay.Replay(
            valid_config(), mode="deterministic_vetoed").run(cycles, outputs)

        self.assertGreater(result.funnel["fired"], 0)

    def test_a_model_proposal_the_contract_did_not_fire_on_is_ignored(self):
        """Arm C may only subtract from the contract, never add to it."""
        cycles = [cycle(0)]
        outputs = [model_output(0, [open_decision(direction="short")])]

        result = replay.Replay(
            valid_config(), mode="deterministic_vetoed").run(cycles, outputs)

        self.assertEqual(result.funnel["fired"], 0)

    def test_arm_c_never_exceeds_arm_a(self):
        """C is a subset of A by construction, on any corpus."""
        cycles = [cycle(i) for i in range(6)]
        outputs = [model_output(i, [open_decision()]) for i in range(6)]

        a = replay.Replay(valid_config(), mode="deterministic").run(
            cycles, [])
        c = replay.Replay(
            valid_config(), mode="deterministic_vetoed").run(cycles, outputs)

        self.assertLessEqual(c.funnel["fired"], a.funnel["fired"])

    def test_the_three_arms_are_distinguishable_on_the_same_corpus(self):
        cycles = [cycle(i) for i in range(4)]
        outputs = [model_output(i, [open_decision()] if i % 2 == 0 else [])
                   for i in range(4)]

        arms = {
            mode: replay.Replay(valid_config(), mode=mode).run(cycles, outputs)
            for mode in replay.MODES
        }

        self.assertGreater(arms["deterministic"].funnel["fired"],
                           arms["deterministic_vetoed"].funnel["fired"])
        self.assertEqual(arms["deterministic_vetoed"].funnel["fired"],
                         arms["recorded_llm"].funnel["fired"])


class BootstrapTests(unittest.TestCase):
    def test_an_interval_is_reproducible(self):
        values = [0.4, -1.0, 2.1, -0.3, 1.5, 0.2, -0.9, 3.0]
        first = stats.bootstrap_mean(values)
        second = stats.bootstrap_mean(values)

        self.assertEqual((first.low, first.point, first.high),
                         (second.low, second.point, second.high))

    def test_the_interval_brackets_the_point_estimate(self):
        values = [0.4, -1.0, 2.1, -0.3, 1.5, 0.2, -0.9, 3.0]
        interval = stats.bootstrap_mean(values)

        self.assertLessEqual(interval.low, interval.point)
        self.assertLessEqual(interval.point, interval.high)
        self.assertEqual(interval.n, 8)

    def test_a_wide_interval_on_a_tiny_sample_includes_zero(self):
        interval = stats.bootstrap_mean([2.0, -2.0, 1.0, -1.0])
        self.assertFalse(interval.excludes_zero())

    def test_an_empty_sample_is_not_an_exception(self):
        self.assertEqual(stats.bootstrap_mean([]).n, 0)

    def test_pairwise_difference_has_an_interval(self):
        better = [1.0, 1.2, 0.9, 1.1] * 8
        worse = [0.1, 0.0, -0.1, 0.2] * 8

        diff = stats.bootstrap_difference(better, worse)

        self.assertGreater(diff.point, 0.5)
        self.assertTrue(diff.excludes_zero())

    def test_two_identical_arms_do_not_differ(self):
        arm = [0.5, -0.5, 1.0, -1.0] * 10
        diff = stats.bootstrap_difference(arm, list(arm))

        self.assertAlmostEqual(diff.point, 0.0, 9)
        self.assertFalse(diff.excludes_zero())


class MinimumDetectableEffectTests(unittest.TestCase):
    def test_mde_shrinks_as_the_sample_grows(self):
        small = stats.minimum_detectable_effect([1.0, -1.0] * 10)
        large = stats.minimum_detectable_effect([1.0, -1.0] * 200)

        self.assertLess(large, small)

    def test_mde_is_infinite_below_two_observations(self):
        self.assertEqual(stats.minimum_detectable_effect([1.0]),
                         float("inf"))

    def test_a_null_result_is_reported_with_its_mde(self):
        """'No difference' and 'no difference detectable' are not the same."""
        values = [0.4, -0.5, 0.2, -0.1] * 10
        mde = stats.minimum_detectable_effect(values)

        self.assertGreater(mde, 0.0)
        self.assertLess(mde, 1.0)


class AchievableSampleTests(unittest.TestCase):
    def test_a_five_point_sweep_on_forty_trades_is_refused(self):
        forecast = stats.achievable_n(total_round_trips=40, grid_points=5)

        self.assertFalse(forecast["sufficient"])
        self.assertEqual(forecast["per_cell"], 8.0)
        self.assertGreater(forecast["shortfall"], 90)

    def test_a_sufficient_grid_is_allowed(self):
        forecast = stats.achievable_n(total_round_trips=600, grid_points=3)

        self.assertTrue(forecast["sufficient"])
        self.assertEqual(forecast["per_cell"], 200.0)

    def test_a_conditioning_axis_reuses_the_whole_sample(self):
        """One point means no division: the argument for conditioning."""
        forecast = stats.achievable_n(total_round_trips=120, grid_points=1)
        self.assertEqual(forecast["per_cell"], 120.0)


class MultipleComparisonTests(unittest.TestCase):
    def test_correction_raises_every_p_value(self):
        raw = {"h1": 0.01, "h2": 0.04, "h3": 0.20}
        corrected = stats.holm_bonferroni(raw)

        for name, value in raw.items():
            self.assertGreaterEqual(corrected[name]["p_adjusted"], value)

    def test_a_marginal_result_stops_being_significant(self):
        """Six hypotheses against a few hundred trades: something will look real."""
        corrected = stats.holm_bonferroni(
            {f"h{i}": 0.04 for i in range(6)})

        self.assertFalse(any(v["significant"] for v in corrected.values()))

    def test_a_strong_result_survives(self):
        corrected = stats.holm_bonferroni(
            {"strong": 0.0001, "a": 0.5, "b": 0.6})

        self.assertTrue(corrected["strong"]["significant"])

    def test_adjusted_values_are_monotonic(self):
        corrected = stats.holm_bonferroni(
            {"a": 0.001, "b": 0.01, "c": 0.02, "d": 0.5})
        ordered = sorted(corrected.values(), key=lambda v: v["p"])

        adjusted = [v["p_adjusted"] for v in ordered]
        self.assertEqual(adjusted, sorted(adjusted))


if __name__ == "__main__":
    unittest.main()
