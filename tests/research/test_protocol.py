"""B9.1: the promotion protocol, and its willingness to say no.

The tests that matter here are the ones asserting the rule refuses. A
protocol that promotes when it should is pleasant; a protocol that declines
to promote on 12 trades, on a single parameter setting, or on a variant that
only worked in the first half of the corpus is the reason any of the numbers
are worth reading.
"""

import unittest

from research import protocol, score, stats
from research.replay import ReplayDecision


def decision(r_multiple, ts=0.0, vol_ratio=None):
    d = ReplayDecision(
        cycle_id=f"c{ts}", ts=ts, symbol="BTC/USDT:USDT", signal_ts=int(ts),
        stage="executed", direction="long", setup_type="range_breakout")
    d.outcome = {"r_multiple": r_multiple, "result": "target"}
    if vol_ratio is not None:
        d.enrichment = {"realised_vol_ratio_8_96": vol_ratio}
    return d


def series(values, start=0.0, vol_ratio=None):
    return [decision(v, ts=start + i, vol_ratio=vol_ratio)
            for i, v in enumerate(values)]


def strong(n=120, value=1.0):
    """A clearly positive arm, alternating so the interval is finite."""
    return series([value if i % 4 else value * 0.6 for i in range(n)])


def flat(n=120):
    return series([0.5 if i % 2 else -0.5 for i in range(n)])


class CompareTests(unittest.TestCase):
    """The stub this replaced returned zeros and always said 'not significant'."""

    def test_a_real_difference_is_detected(self):
        better = [1.0, 1.2, 0.9, 1.1] * 30
        worse = [0.0, 0.1, -0.1, 0.05] * 30

        result = score.compare(better, worse)

        self.assertGreater(result["delta_r"], 0.5)
        self.assertTrue(result["significant"])
        self.assertNotEqual(result["ci_low"], 0.0)
        self.assertNotEqual(result["ci_high"], 0.0)

    def test_identical_arms_do_not_differ(self):
        arm = [0.5, -0.5, 1.0, -1.0] * 30

        result = score.compare(arm, list(arm))

        self.assertAlmostEqual(result["delta_r"], 0.0, 9)
        self.assertFalse(result["significant"])

    def test_the_interval_brackets_the_delta(self):
        result = score.compare([1.0, 2.0, 0.5] * 40, [0.0, 0.5, -0.5] * 40)

        self.assertLessEqual(result["ci_low"], result["delta_r"])
        self.assertLessEqual(result["delta_r"], result["ci_high"])

    def test_an_empty_arm_is_insufficient_not_zero(self):
        result = score.compare([], [1.0, 2.0])

        self.assertEqual(result["verdict"], stats.INSUFFICIENT_SAMPLE)
        self.assertEqual(result["n_variant"], 0)

    def test_a_small_sample_is_flagged_even_when_it_differs(self):
        result = score.compare([5.0] * 10, [-5.0] * 10)

        self.assertTrue(result["significant"])
        self.assertEqual(result["verdict"], stats.INSUFFICIENT_SAMPLE)


class SplitTests(unittest.TestCase):
    def test_the_split_is_chronological(self):
        items = series([1.0] * 10)
        fit, confirm = protocol.split_by_time(items)

        self.assertEqual(len(fit), 7)
        self.assertEqual(len(confirm), 3)
        self.assertLess(fit[-1].ts, confirm[0].ts)

    def test_unordered_input_is_sorted_before_splitting(self):
        items = list(reversed(series([1.0] * 10)))
        fit, confirm = protocol.split_by_time(items)

        self.assertLess(fit[-1].ts, confirm[0].ts)

    def test_a_single_observation_does_not_crash(self):
        fit, confirm = protocol.split_by_time(series([1.0]))
        self.assertEqual((len(fit), len(confirm)), (1, 0))

    def test_both_windows_are_always_non_empty_when_possible(self):
        fit, confirm = protocol.split_by_time(series([1.0, 2.0]))
        self.assertTrue(fit)
        self.assertTrue(confirm)


class OutOfSampleTests(unittest.TestCase):
    def test_a_consistent_variant_survives(self):
        result = protocol.out_of_sample(strong())
        self.assertTrue(result["survives"])

    def test_a_variant_that_only_worked_early_does_not_survive(self):
        """Fitted to the first window, contradicted by the second."""
        decisions = series([2.0] * 70) + series([-2.0] * 30, start=100)

        result = protocol.out_of_sample(decisions)

        self.assertFalse(result["survives"])
        self.assertIn("below", result["reason"])

    def test_the_regime_profile_of_both_windows_is_reported(self):
        decisions = (series([1.0] * 70, vol_ratio=0.9)
                     + series([1.0] * 30, start=100, vol_ratio=1.0))

        result = protocol.out_of_sample(decisions)

        self.assertEqual(result["fit_regime"]["median_vol_ratio"], 0.9)
        self.assertEqual(result["confirm_regime"]["median_vol_ratio"], 1.0)

    def test_a_regime_shift_between_windows_is_flagged_incomparable(self):
        """Otherwise the split measures the market, not the variant."""
        decisions = (series([1.0] * 70, vol_ratio=0.5)
                     + series([1.0] * 30, start=100, vol_ratio=2.0))

        result = protocol.out_of_sample(decisions)

        self.assertFalse(result["fit_regime"]["comparable"])

    def test_comparable_windows_are_marked_as_such(self):
        decisions = (series([1.0] * 70, vol_ratio=1.0)
                     + series([1.0] * 30, start=100, vol_ratio=1.1))

        result = protocol.out_of_sample(decisions)

        self.assertTrue(result["fit_regime"]["comparable"])


class RejectionTests(unittest.TestCase):
    def test_structural_invalidity_needs_no_sample(self):
        verdict = protocol.evaluate_axis(
            [], [], structurally_invalid=(
                "structure_target degenerates to fixed_rr in both setups it "
                "was designed for"))

        self.assertEqual(verdict.verdict, protocol.REJECT)
        self.assertEqual(verdict.governing_criterion, "structurally invalid")

    def test_a_whole_axis_below_the_baseline_is_rejected(self):
        baseline = strong(120, value=1.0)
        settings = [(f"v{i}", series([-1.0] * 120, start=i * 1000))
                    for i in range(3)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, protocol.REJECT)
        self.assertIn("whole axis", verdict.governing_criterion)

    def test_one_bad_setting_does_not_reject_the_axis(self):
        """Intention #4: never kill a hypothesis on one parameter value."""
        baseline = flat()
        settings = [("v0", series([-1.0] * 120))]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertNotEqual(verdict.verdict, protocol.REJECT)
        self.assertEqual(verdict.verdict, protocol.CONTINUE)
        self.assertIn("too few settings", verdict.governing_criterion)


class PromotionTests(unittest.TestCase):
    def test_a_small_sample_refuses_to_promote(self):
        baseline = flat(120)
        settings = [(f"v{i}", strong(12)) for i in range(3)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertIn("promotion floor", verdict.governing_criterion)
        self.assertIn("MDE", verdict.detail)

    def test_two_settings_is_not_an_axis(self):
        baseline = flat(120)
        settings = [(f"v{i}", strong(120)) for i in range(2)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, protocol.CONTINUE)
        self.assertIn("too few settings", verdict.governing_criterion)

    def test_a_delta_inside_the_interval_does_not_promote(self):
        baseline = flat(200)
        settings = [(f"v{i}", flat(200)) for i in range(3)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, protocol.CONTINUE)
        self.assertIn("inside the interval", verdict.governing_criterion)

    def test_a_deeper_drawdown_blocks_promotion(self):
        """A better expectancy bought with a deeper hole is not better."""
        baseline = series([0.05] * 200)
        # Same mean, but front-loaded losses produce a much deeper trough.
        rough = series([-3.0] * 20 + [0.4] * 180)
        settings = [(f"v{i}", rough) for i in range(3)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertNotEqual(verdict.verdict, protocol.PROMOTE)

    def test_a_variant_that_decays_does_not_promote(self):
        baseline = flat(200)
        decaying = series([3.0] * 140) + series([0.0] * 60, start=1000)
        settings = [(f"v{i}", decaying) for i in range(3)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, protocol.CONTINUE)

    def test_a_variant_meeting_every_criterion_promotes(self):
        baseline = series([-0.2 if i % 2 else 0.1 for i in range(200)])
        winner = series([1.0 if i % 3 else 0.8 for i in range(200)])
        settings = [(f"v{i}", winner) for i in range(3)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, protocol.PROMOTE)
        self.assertIn("round trips", verdict.detail)

    def test_no_baseline_is_insufficient_not_a_promotion(self):
        verdict = protocol.evaluate_axis(
            [(f"v{i}", strong(200)) for i in range(3)], [])

        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertIn("baseline", verdict.governing_criterion)

    def test_every_verdict_names_its_governing_criterion(self):
        for settings, baseline in (
            ([], flat()),
            ([("v0", strong(12))], flat()),
            ([(f"v{i}", flat(200)) for i in range(3)], flat(200)),
        ):
            verdict = protocol.evaluate_axis(settings, baseline)
            self.assertTrue(verdict.governing_criterion)
            self.assertTrue(verdict.detail)


class FamilyCorrectionTests(unittest.TestCase):
    def test_the_corrected_figure_is_attached_to_every_bucket(self):
        results = {name: score.score_returns([1.0, -0.5] * 60, label=name)
                   for name in ("a", "b", "c")}

        corrected = protocol.correct_family(results)

        for row in corrected.values():
            self.assertIn("p_adjusted", row)
            self.assertIn("significant_corrected", row)

    def test_an_uncorrected_significance_claim_is_not_carried_forward(self):
        results = {"a": score.score_returns([1.0] * 120, label="a")}

        corrected = protocol.correct_family(results)

        self.assertNotIn("significant", corrected["a"])

    def test_a_bucket_whose_interval_crosses_zero_is_never_significant(self):
        results = {"a": score.score_returns([2.0, -2.0] * 60, label="a")}

        corrected = protocol.correct_family(results)

        self.assertFalse(corrected["a"]["significant_corrected"])

    def test_correction_raises_p_as_the_family_grows(self):
        one = protocol.correct_family(
            {"a": score.score_returns([1.0, 0.9] * 60, label="a")})
        many = protocol.correct_family({
            name: score.score_returns([1.0, 0.9] * 60, label=name)
            for name in "abcdefgh"})

        self.assertGreaterEqual(many["a"]["p_adjusted"],
                                one["a"]["p_adjusted"])

    def test_a_small_bucket_stays_insufficient_rather_than_no_effect(self):
        results = {"a": score.score_returns([2.0, -2.0] * 4, label="a")}

        corrected = protocol.correct_family(results)

        self.assertEqual(corrected["a"]["verdict"],
                         stats.INSUFFICIENT_SAMPLE)


if __name__ == "__main__":
    unittest.main()
