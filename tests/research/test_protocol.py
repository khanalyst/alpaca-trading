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


# Fixture timestamps are indices, spaced onto the clustered bootstrap's
# six-hour grid so one index is one independent market episode. Spacing is not
# cosmetic: packed one second apart, a whole fixture corpus collapses into a
# single cluster, and a clustered bootstrap over one cluster can only draw that
# cluster - so every interval comes back zero-width and every arm looks
# certain. These tests would then assert PROMOTE on the strength of a bug.
CLUSTER_GRID_SECONDS = 21_600


def at_second(r_multiple, second):
    """A decision at an exact wall-clock second, off the episode grid."""
    d = ReplayDecision(
        cycle_id=f"c{second}", ts=float(second), symbol="BTC/USDT:USDT",
        signal_ts=int(second), stage="executed", direction="long",
        setup_type="range_breakout")
    d.outcome = {"r_multiple": r_multiple, "result": "target"}
    return d


def decision(r_multiple, ts=0.0, vol_ratio=None):
    d = at_second(r_multiple, int(float(ts) * CLUSTER_GRID_SECONDS))
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

    def test_every_arm_uses_the_baseline_calendar_cutoff(self):
        baseline = series([0.0] * 10, start=100)
        dense = series([1.0] * 20, start=95)
        sparse = series([1.0] * 4, start=104)

        cutoff = protocol.common_time_cutoff([baseline])
        dense_fit, dense_confirm = protocol.split_at_time(dense, cutoff)
        sparse_fit, sparse_confirm = protocol.split_at_time(sparse, cutoff)

        self.assertEqual(cutoff, 107.0 * CLUSTER_GRID_SECONDS)
        self.assertTrue(all(item.ts < cutoff
                            for item in dense_fit + sparse_fit))
        self.assertTrue(all(item.ts >= cutoff
                            for item in dense_confirm + sparse_confirm))


class PersistedDecisionTests(unittest.TestCase):
    def test_a_persisted_veto_is_a_paired_zero_return_action(self):
        common = {
            "proposal_id": "same-proposal", "cycle_id": "cycle-1",
            "decision_ts": 100.0, "symbol": "BTC/USDT:USDT",
            "signal_ts": 99, "direction": "long",
            "setup_type": "trend_continuation",
        }
        veto = protocol.paper_trade_decisions([{
            **common, "decision_outcome": "VETOED",
            "trade_status": None, "trade_r_multiple": None,
            "trade_result": None,
        }])
        accepted = protocol.paper_trade_decisions([{
            **common, "decision_outcome": "PROPOSED",
            "trade_status": "CLOSED", "trade_r_multiple": 1.25,
            "trade_result": "target",
        }])

        comparison = protocol.paired_arm_comparison(accepted, veto)

        self.assertEqual(veto[0].outcome["r_multiple"], 0.0)
        self.assertEqual(comparison["paired_n"], 1)
        self.assertEqual(comparison["interval"].point, 1.25)


class ClusterRandomizationTests(unittest.TestCase):
    def test_null_or_weak_clustered_deltas_are_not_significant(self):
        baseline = series([0.0] * 12)
        weak = series([0.20, -0.19] * 6)

        test = protocol.paired_arm_comparison(
            weak, baseline)["randomization"]

        self.assertTrue(test["exact"])
        self.assertEqual(
            test["null_assumption"],
            protocol.PAIR_RANDOMIZATION_NULL_ASSUMPTION)
        self.assertGreater(test["p_value"], 0.05)

    def test_a_strong_paired_clustered_effect_can_survive(self):
        baseline = series([0.0] * 8)
        strong_effect = series([1.0] * 8)

        test = protocol.paired_arm_comparison(
            strong_effect, baseline)["randomization"]

        self.assertEqual(test["method"], "exact_enumeration")
        self.assertEqual(test["resamples"], 256)
        self.assertLess(test["p_value"], 0.05)

    def test_monte_carlo_evidence_is_recorded_as_an_estimate(self):
        baseline = series([0.0] * 20)
        strong_effect = series([1.0] * 20)

        test = protocol.paired_arm_comparison(
            strong_effect, baseline)["randomization"]

        self.assertEqual(test["method"], "monte_carlo")
        self.assertFalse(test["exact"])
        self.assertEqual(test["seed"], protocol.PAIR_RANDOMIZATION_SEED)
        self.assertEqual(test["resamples"],
                         protocol.PAIR_RANDOMIZATION_RESAMPLES)


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

    def test_an_under_observed_setting_blocks_whole_axis_rejection(self):
        baseline = strong(120, value=1.0)
        settings = [
            ("v0", series([-1.0] * 120)),
            ("v1", series([-1.0] * 120, start=1000)),
            ("v2", series([-1.0] * 12, start=2000)),
        ]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertIn("rejection floor", verdict.governing_criterion)


class PromotionTests(unittest.TestCase):
    def test_a_small_sample_refuses_to_promote(self):
        baseline = flat(120)
        settings = [(f"v{i}", strong(12)) for i in range(3)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertIn("promotion floor", verdict.governing_criterion)
        self.assertIn("MDE", verdict.detail)

    def test_two_candidates_plus_the_baseline_are_an_axis(self):
        baseline = flat(120)
        settings = [(f"v{i}", strong(120)) for i in range(2)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, protocol.PROMOTE)

    def test_serial_settings_use_their_own_baseline_for_fit_selection(self):
        first_baseline = series([0.0] * 120)
        first_candidate = series([1.0] * 120)
        later_baseline = series([2.0] * 120, start=1_000)
        later_candidate = series([2.2] * 120, start=1_000)

        verdict = protocol.evaluate_axis([
            ("larger-delta", first_candidate, first_baseline),
            ("larger-absolute-return", later_candidate, later_baseline),
        ])

        self.assertEqual(verdict.verdict, protocol.PROMOTE)
        self.assertEqual(verdict.evidence["best"], "larger-delta")

    def test_one_candidate_plus_the_baseline_is_not_an_axis(self):
        verdict = protocol.evaluate_axis([("v0", strong(120))], flat(120))

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

        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertIn("paired proposal coverage", verdict.governing_criterion)

    def test_confirmation_window_cannot_select_its_own_winner(self):
        baseline = flat(200)
        fit_winner = (series([2.0] * 140)
                      + series([-2.0] * 60, start=1000))
        full_sample_winner = (series([0.8] * 140, start=2000)
                              + series([3.0] * 60, start=3000))
        settings = [
            ("fit-winner", fit_winner),
            ("full-winner", full_sample_winner),
            ("control", series([0.7] * 200, start=4000)),
        ]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertEqual(verdict.evidence["best"], "fit-winner")

    def test_a_variant_meeting_every_criterion_promotes(self):
        baseline = series([-0.2 if i % 2 else 0.1 for i in range(200)])
        winner = series([1.0 if i % 3 else 0.8 for i in range(200)])
        settings = [(f"v{i}", winner) for i in range(3)]

        verdict = protocol.evaluate_axis(settings, baseline)

        self.assertEqual(verdict.verdict, protocol.PROMOTE)
        self.assertIn("round trips", verdict.detail)
        self.assertTrue(all(verdict.evidence["criteria"].values()))
        self.assertGreater(verdict.evidence["fit_interval"]["low"], 0)
        self.assertGreater(
            verdict.evidence["confirmation_interval"]["low"], 0)

    def test_sparse_fit_window_cannot_borrow_from_confirmation(self):
        baseline = series([
            -0.2 if index % 2 else 0.1 for index in range(130)])
        winner = [decision(1.0, ts=index)
                  for index in [*range(69), *range(91, 130)]]

        verdict = protocol.evaluate_axis(
            [("winner", winner), ("control", list(winner))], baseline)

        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertIn("fit-window paired", verdict.governing_criterion)
        self.assertEqual(verdict.evidence["fit_paired"]["paired_n"], 69)

    def test_sparse_confirmation_cannot_pass_on_a_large_fit_sample(self):
        baseline = series([
            -0.2 if index % 2 else 0.1 for index in range(130)])
        winner = [decision(1.0, ts=index)
                  for index in range(120)]

        verdict = protocol.evaluate_axis(
            [("winner", winner), ("control", list(winner))], baseline)

        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertIn("confirmation-window paired",
                      verdict.governing_criterion)
        self.assertEqual(
            verdict.evidence["confirmation_paired"]["paired_n"], 29)

    def test_each_window_requires_clean_dependence_aware_pairs(self):
        left = series([1.0] * 70)
        right = series([0.0] * 70)
        duplicate = decision(1.0, ts=0)
        duplicate.cycle_id = left[0].cycle_id
        comparison = protocol.paired_arm_comparison(
            left + [duplicate], right)

        self.assertFalse(protocol.paired_window_adequate(
            comparison, protocol.MIN_PAIRED_FIT_OBSERVATIONS))
        clean = protocol.paired_arm_comparison(left, right)
        clean["bootstrap"]["cluster_seconds"] = 1
        self.assertFalse(protocol.paired_window_adequate(
            clean, protocol.MIN_PAIRED_FIT_OBSERVATIONS))

    def test_one_market_episode_cannot_promote_however_many_trades(self):
        """A collapsed interval is an absence of evidence, not certainty.

        Packed into a single six-hour cluster, the block bootstrap has one
        possible resample, so it returns zero width around a positive point
        estimate - which reads as a delta known exactly and clears any
        lower-bound test. It has to be refused on the cluster count.
        """
        # One second apart, so all 200 land in the same six-hour bucket while
        # still carrying distinct proposal identities that pair cleanly.
        packed = [at_second(1.0 if i % 4 else 0.6, i) for i in range(200)]
        packed_baseline = [at_second(0.5 if i % 2 else -0.5, i)
                           for i in range(200)]
        comparison = protocol.paired_arm_comparison(packed, packed_baseline)
        interval = comparison["interval"]

        # The degeneracy is real: 200 pairs, zero width, lower bound above 0.
        self.assertEqual(comparison["bootstrap"]["clusters"], 1)
        self.assertEqual(interval.n, 200)
        self.assertEqual(interval.low, interval.high)
        self.assertGreater(interval.low, 0)
        self.assertTrue(interval.is_degenerate())

        # And it is refused rather than believed.
        self.assertFalse(protocol.paired_window_adequate(
            comparison, protocol.MIN_ROUND_TRIPS))
        verdict = protocol.evaluate_axis(
            [("packed", packed), ("control", list(packed))], packed_baseline)
        self.assertEqual(verdict.verdict, stats.INSUFFICIENT_SAMPLE)
        self.assertIn("episode", verdict.detail)

    def test_too_few_episodes_is_refused_even_with_a_wide_interval(self):
        left = series([1.0] * 120)
        right = series([0.0] * 120)
        comparison = protocol.paired_arm_comparison(left, right)
        self.assertTrue(protocol.paired_window_adequate(
            comparison, protocol.MIN_ROUND_TRIPS))

        comparison["bootstrap"]["clusters"] = (
            protocol.MIN_BOOTSTRAP_CLUSTERS - 1)

        self.assertFalse(protocol.paired_window_adequate(
            comparison, protocol.MIN_ROUND_TRIPS))

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
        scored = score.score_returns([1.0, 0.9] * 60, label="a")
        scored["p_value"] = 0.01
        one = protocol.correct_family({"a": scored})
        many = protocol.correct_family({
            name: {**score.score_returns([1.0, 0.9] * 60, label=name),
                   "p_value": 0.01}
            for name in "abcdefgh"})

        self.assertGreaterEqual(many["a"]["p_adjusted"],
                                one["a"]["p_adjusted"])

    def test_a_small_bucket_stays_insufficient_rather_than_no_effect(self):
        results = {"a": score.score_returns([2.0, -2.0] * 4, label="a")}

        corrected = protocol.correct_family(results)

        self.assertEqual(corrected["a"]["verdict"],
                         stats.INSUFFICIENT_SAMPLE)

    def test_missing_calibrated_p_value_fails_closed(self):
        scored = score.score_returns([1.0] * 120, label="a")

        corrected = protocol.correct_family({"a": scored})

        self.assertEqual(corrected["a"]["p_uncorrected"], 1.0)
        self.assertFalse(corrected["a"]["significant_corrected"])
        
def promoted(axis_id, low, high, n=120, p_value=0.01):
    """A PROMOTE verdict carrying one calibrated confirmation test."""
    return protocol.Verdict(
        protocol.PROMOTE, "every promotion criterion holds",
        f"{axis_id} cleared every criterion",
        {"best": f"{axis_id}.best",
         "confirmation_interval": {"point": (low + high) / 2, "low": low,
                                   "high": high, "n": n},
         "confirmation_test": {
             "kind": protocol.PAIR_RANDOMIZATION_KIND,
             "null_assumption":
                 protocol.PAIR_RANDOMIZATION_NULL_ASSUMPTION,
             "method": "exact_enumeration", "alternative": "greater",
             "cluster_seconds": protocol.PAIR_CLUSTER_SECONDS,
             "clusters": 8, "paired_n": n, "observed_mean": 0.5,
             "resamples": 256, "seed": None, "exact": True,
             "p_value": p_value,
         }})


class AxisFamilyCorrectionTests(unittest.TestCase):
    """Criterion 10: several axes are several chances to promote something."""

    def test_a_lone_strong_axis_survives(self):
        corrected = protocol.apply_axis_family_correction(
            {"strategy.fixed_reward_risk": promoted("rr", 0.40, 0.60)})

        verdict = corrected["strategy.fixed_reward_risk"]
        self.assertEqual(verdict.verdict, protocol.PROMOTE)
        self.assertTrue(verdict.evidence["family"]["significant"])
        self.assertEqual(verdict.evidence["family"]["family_n"], 1)

    def test_an_unrecognized_null_assumption_fails_closed(self):
        verdict = promoted("a", 0.40, 0.60)
        verdict.evidence["confirmation_test"]["null_assumption"] = (
            "assumption_free")

        corrected = protocol.apply_axis_family_correction({"a": verdict})["a"]

        self.assertEqual(corrected.verdict, protocol.CONTINUE)
        self.assertFalse(corrected.evidence["family"]["calibrated"])
        self.assertEqual(corrected.evidence["family"]["p"], 1.0)

    def test_a_marginal_axis_stops_promoting_once_the_family_grows(self):
        """The same evidence, judged against how many axes were asked."""
        alone = protocol.apply_axis_family_correction(
            {"a": promoted("a", 0.01, 0.30)})
        crowded = protocol.apply_axis_family_correction({
            "a": promoted("a", 0.01, 0.30),
            **{name: protocol.Verdict(
                protocol.CONTINUE, "confirmation delta does not clear zero",
                "", {}) for name in "bcdefgh"},
        })

        self.assertEqual(alone["a"].verdict, protocol.PROMOTE)
        self.assertEqual(crowded["a"].verdict, protocol.CONTINUE)
        self.assertEqual(
            crowded["a"].governing_criterion,
            "family-wise correction across the axes tested")
        self.assertGreater(crowded["a"].evidence["family"]["p_adjusted"],
                           alone["a"].evidence["family"]["p_adjusted"])

    def test_failed_axes_stay_in_the_family(self):
        """An axis that failed still consumed a chance."""
        corrected = protocol.apply_axis_family_correction({
            "a": promoted("a", 0.40, 0.60),
            "b": protocol.Verdict(protocol.CONTINUE, "x", "y", {}),
            "c": protocol.Verdict(stats.INSUFFICIENT_SAMPLE, "x", "y", {}),
        })

        self.assertEqual(corrected["a"].evidence["family"]["family_n"], 3)
        self.assertEqual(corrected["a"].evidence["family"]["axes"],
                         ["a", "b", "c"])

    def test_the_family_record_reaches_every_verdict(self):
        corrected = protocol.apply_axis_family_correction({
            "a": promoted("a", 0.40, 0.60),
            "b": protocol.Verdict(protocol.CONTINUE, "x", "y", {}),
        })

        for axis_id in ("a", "b"):
            with self.subTest(axis=axis_id):
                self.assertIn("family", corrected[axis_id].evidence)

    def test_statistical_significance_does_not_override_other_failed_gates(self):
        corrected = protocol.apply_axis_family_correction(
            {"a": protocol.Verdict(protocol.CONTINUE, "x", "y", {
                "confirmation_test": promoted(
                    "a", 0.4, 0.6).evidence["confirmation_test"]})})

        self.assertEqual(corrected["a"].verdict, protocol.CONTINUE)
        self.assertTrue(corrected["a"].evidence["family"]["significant"])

    def test_correction_reads_the_confirmation_test_not_interval_geometry(self):
        """Criterion 5 is a screen; the held-out window is the test."""
        verdict = promoted("a", 0.40, 0.60, p_value=0.01)
        verdict.evidence["fit_interval"] = {
            "low": 5.0, "high": 6.0, "n": 500}
        wide_confirmation = promoted("a", 0.40, 0.60, p_value=0.50)
        wide_confirmation.evidence["fit_interval"] = verdict.evidence[
            "fit_interval"]

        self.assertTrue(protocol.apply_axis_family_correction(
            {"a": verdict})["a"].evidence["family"]["significant"])
        self.assertFalse(protocol.apply_axis_family_correction(
            {"a": wide_confirmation})["a"].evidence["family"]["significant"])



if __name__ == "__main__":
    unittest.main()
