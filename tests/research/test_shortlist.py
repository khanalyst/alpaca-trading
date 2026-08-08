"""The shortlist must never overstate, and must not confuse "measured and
lost" with "never measured".

This is the report a human reads before risking money, so its failure mode is
not a crash but a confident sentence that outruns its evidence. Two things it
must hold: a label can only say what the sample supports, and a candidate
starved of market data is reported as untested rather than as unsupported -
the distinction that made six strategies look falsified for a week.
"""

import unittest

from research import shortlist
from research.replay import ReplayDecision


def trades(returns, start_ts=1_000.0, risk=100.0):
    return [
        {"status": "CLOSED", "result": "target", "valid_for_inference": 1,
         "r_multiple": r, "net_pnl_usd": r * risk,
         "entry_ts": start_ts + index * 3_600.0}
        for index, r in enumerate(returns)
    ]


class LabelsCannotOutrunTheSampleTests(unittest.TestCase):
    def test_no_trades_is_no_evidence(self):
        candidate = shortlist.measure("staged.x", "demo:s", [])
        self.assertEqual(candidate.label, shortlist.NO_EVIDENCE)

    def test_a_starved_candidate_is_untested_not_unsupported(self):
        candidate = shortlist.measure(
            "staged.x", "demo:s", [], starved=400)
        self.assertEqual(candidate.label, shortlist.NO_EVIDENCE)
        joined = " ".join(candidate.reasons)
        self.assertIn("never evaluated", joined)
        self.assertIn("untested rather than unsupported", joined)

    def test_a_declining_candidate_says_it_never_fired(self):
        candidate = shortlist.measure(
            "staged.x", "demo:s", [], declined=400)
        joined = " ".join(candidate.reasons)
        self.assertIn("declined", joined)
        self.assertIn("no trades to judge", joined)

    def test_a_strong_mean_on_a_tiny_sample_is_still_insufficient(self):
        candidate = shortlist.measure("staged.x", "demo:s", trades([2.0] * 5))
        self.assertEqual(candidate.label, shortlist.INSUFFICIENT)
        self.assertGreater(candidate.mean_r, 0)

    def test_a_moderate_sample_reaches_preliminary_only(self):
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades([0.4, 0.6] * 25))
        self.assertEqual(candidate.trades, 50)
        self.assertEqual(candidate.label, shortlist.PRELIMINARY)

    def test_an_adequate_sample_whose_interval_includes_zero_is_inconclusive(self):
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades([1.0, -1.0] * 60))
        self.assertEqual(candidate.label, shortlist.INCONCLUSIVE)
        self.assertLessEqual(candidate.ci_low, 0)

    def test_a_decisively_negative_result_says_so_at_any_size(self):
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades([-0.5] * 40))
        self.assertEqual(candidate.label, shortlist.NEGATIVE)

    def test_supported_needs_sample_interval_and_a_held_out_window(self):
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades([0.5, 0.4, 0.6] * 40))
        self.assertEqual(candidate.label, shortlist.SUPPORTED)
        self.assertGreater(candidate.ci_low, 0)
        self.assertIsNotNone(candidate.confirmation_mean_r)

    def test_a_result_that_dies_out_of_sample_is_not_supported(self):
        # Positive in the fitting window, negative in the held-out one: the
        # exact shape of an edge that only worked where it was found.
        returns = [1.2] * 90 + [-1.0] * 40
        candidate = shortlist.measure("staged.x", "demo:s", trades(returns))
        self.assertGreater(candidate.trades, shortlist.MIN_FOR_SUPPORTED)
        self.assertEqual(candidate.label, shortlist.INCONCLUSIVE)
        self.assertIn("does not persist out of sample",
                      " ".join(candidate.reasons))

    def test_the_split_is_by_time_not_at_random(self):
        returns = [0.1] * 70 + [0.9] * 30
        candidate = shortlist.measure("staged.x", "demo:s", trades(returns))
        self.assertLess(candidate.fit_mean_r, candidate.confirmation_mean_r)

    def test_unfilled_and_invalid_rows_never_count(self):
        rows = trades([0.5] * 10)
        rows.append({"status": "CLOSED", "result": "unfilled",
                     "valid_for_inference": 1, "r_multiple": 9.0,
                     "net_pnl_usd": 900.0, "entry_ts": 99_000.0})
        rows.append({"status": "CLOSED", "result": "target",
                     "valid_for_inference": 0, "r_multiple": 9.0,
                     "net_pnl_usd": 900.0, "entry_ts": 99_001.0})
        rows.append({"status": "OPEN", "result": None,
                     "valid_for_inference": 1, "r_multiple": None,
                     "net_pnl_usd": None, "entry_ts": 99_002.0})
        candidate = shortlist.measure("staged.x", "demo:s", rows)
        self.assertEqual(candidate.trades, 10)
        self.assertAlmostEqual(candidate.mean_r, 0.5, places=3)


class RankingAndReportTests(unittest.TestCase):
    def _population(self):
        return [
            shortlist.measure("staged.good", "demo:s",
                              trades([0.5, 0.4, 0.6] * 40),
                              mechanism="Forced sellers must complete.",
                              payer="the liquidated trader"),
            shortlist.measure("staged.bad", "demo:s", trades([-0.5] * 40)),
            shortlist.measure("staged.thin", "demo:s", trades([1.0] * 5)),
            shortlist.measure("staged.starved", "demo:s", [], starved=200),
        ]

    def test_supported_ranks_above_everything_and_negative_below(self):
        ordered = shortlist.rank(self._population())
        self.assertEqual(ordered[0].variant_id, "staged.good")
        self.assertEqual(ordered[-1].variant_id, "staged.bad")

    def test_the_report_refuses_to_call_anything_proven(self):
        report = shortlist.render(self._population())
        lowered = report.lower()
        for word in ("proven", "guaranteed", "will make", "risk-free"):
            self.assertNotIn(word, lowered)
        self.assertIn("no entry here is a recommendation to trade", lowered)

    def test_the_report_carries_the_mechanism_and_the_payer(self):
        report = shortlist.render(self._population())
        self.assertIn("Forced sellers must complete.", report)
        self.assertIn("the liquidated trader", report)

    def test_the_report_states_starvation_separately_from_failure(self):
        report = shortlist.render(self._population())
        self.assertIn("not evidence against the claim", report)

    def test_an_empty_population_says_so_rather_than_rendering_a_table(self):
        report = shortlist.render([])
        self.assertIn("Nothing has been measured yet.", report)
        self.assertNotIn("| Rank |", report)

    def test_a_baseline_comparison_is_reported_when_available(self):
        candidate = shortlist.measure(
            "staged.good", "demo:s", trades([0.5, 0.4, 0.6] * 40),
            baseline_trades=trades([-0.1] * 100),
            baseline_variant_id="momentum.baseline")
        self.assertIsNotNone(candidate.delta_vs_baseline)
        self.assertGreater(candidate.delta_vs_baseline, 0)
        self.assertIn("beats its baseline", " ".join(candidate.reasons))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class FalseDiscoveryControlTests(unittest.TestCase):
    """Screening many candidates produces winners with no edge at all."""

    def test_a_lone_real_result_survives_correction(self):
        population = [
            shortlist.measure("staged.real", "demo:s",
                              trades([0.5, 0.4, 0.6] * 40)),
            *[shortlist.measure(f"staged.noise{i}", "demo:s",
                                trades([1.0, -1.0] * 60))
              for i in range(10)],
        ]
        shortlist.apply_family_correction(population)
        real = population[0]
        self.assertEqual(real.label, shortlist.SUPPORTED)
        self.assertIsNotNone(real.p_adjusted)
        self.assertLessEqual(real.p_adjusted, shortlist.FDR_ALPHA)
        self.assertIn("survives false-discovery control",
                      " ".join(real.reasons))

    def test_correction_demotes_a_marginal_winner_found_among_many(self):
        # SUPPORTED on its own at p=0.0085. One of forty-one screened
        # together it is not: this is the candidate that would have headed
        # an uncorrected shortlist purely because enough things were tried.
        alternating = [value for _ in range(55) for value in (0.75, -0.45)]
        marginal = shortlist.measure(
            "staged.marginal", "demo:s", trades(alternating))
        self.assertEqual(marginal.label, shortlist.SUPPORTED)
        self.assertLess(marginal.p_value, shortlist.FDR_ALPHA)

        family = [marginal] + [
            shortlist.measure(f"staged.noise{i}", "demo:s",
                              trades([0.9, -0.9] * 55))
            for i in range(40)
        ]
        shortlist.apply_family_correction(family)
        self.assertEqual(marginal.family_size, 41)
        self.assertGreater(marginal.p_adjusted, shortlist.FDR_ALPHA)
        self.assertEqual(marginal.label, shortlist.INCONCLUSIVE)
        joined = " ".join(marginal.reasons)
        self.assertIn("does not survive false-discovery control", joined)
        self.assertIn("41 candidates", joined)

    def test_thin_arms_do_not_inflate_the_family(self):
        # Counting tests that were never run would make the correction look
        # stricter than the search actually was.
        population = [
            shortlist.measure("staged.real", "demo:s",
                              trades([0.5, 0.4, 0.6] * 40)),
            *[shortlist.measure(f"staged.thin{i}", "demo:s", trades([0.5] * 3))
              for i in range(50)],
        ]
        shortlist.apply_family_correction(population)
        self.assertEqual(population[0].family_size, 1)
        self.assertEqual(population[0].label, shortlist.SUPPORTED)

    def test_correction_never_promotes(self):
        negative = shortlist.measure(
            "staged.bad", "demo:s", trades([-0.5] * 120))
        shortlist.apply_family_correction([negative])
        self.assertEqual(negative.label, shortlist.NEGATIVE)


class OpportunityEvidenceTests(unittest.TestCase):
    """The shortlist estimand is per eligible opportunity, not per fill."""

    @staticmethod
    def _trade(index, value, proposal):
        return {
            "status": "CLOSED", "result": "target",
            "valid_for_inference": 1, "r_multiple": value,
            "net_pnl_usd": value, "entry_ts": float(index),
            "proposal_id": proposal,
        }

    @staticmethod
    def _decision(proposal, outcome="PROPOSED", reason=""):
        return {"proposal_id": proposal, "decision_outcome": outcome,
                "reason": reason}

    def _paired_arms(self, *, declines=0, unresolved=0):
        trade_rows = [self._trade(i, 1.0, f"p{i}") for i in range(100)]
        candidate = [self._decision(f"p{i}") for i in range(100)]
        baseline = [self._decision(f"p{i}") for i in range(100)]
        for i in range(declines):
            candidate.append(self._decision(f"d{i}", "VETOED",
                                            "contract declined"))
            baseline.append(self._decision(f"d{i}", "VETOED",
                                           "contract declined"))
        for i in range(unresolved):
            candidate.append(self._decision(f"u{i}"))
        return trade_rows, candidate, baseline

    def test_vetoes_are_zero_returns_in_the_opportunity_mean(self):
        trades_, decisions, baseline = self._paired_arms(declines=100)
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades_, decisions=decisions,
            baseline_trades=trades_, baseline_decisions=baseline,
            baseline_variant_id="momentum.baseline")
        self.assertEqual(candidate.eligible_opportunities, 200)
        self.assertAlmostEqual(candidate.mean_r, 0.5, places=3)
        self.assertEqual(candidate.pair_coverage_pct, 100.0)

    def test_a_rare_fill_rate_cannot_be_supported(self):
        trades_, decisions, baseline = self._paired_arms(declines=10_000)
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades_, decisions=decisions,
            baseline_trades=trades_, baseline_decisions=baseline,
            baseline_variant_id="momentum.baseline")
        self.assertLess(candidate.firing_rate, shortlist.MIN_FIRING_RATE)
        self.assertEqual(candidate.label, shortlist.INCONCLUSIVE)
        self.assertIn("minimum firing rate", " ".join(candidate.reasons))

    def test_a_ledger_candidate_without_a_baseline_cannot_be_supported(self):
        trades_ = [self._trade(i, 1.0, f"p{i}") for i in range(100)]
        decisions = [self._decision(f"p{i}") for i in range(100)]
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades_, decisions=decisions)
        self.assertEqual(candidate.pair_coverage_pct, 0.0)
        self.assertEqual(candidate.label, shortlist.INCONCLUSIVE)

    def test_unresolved_opportunities_reduce_coverage_and_block_support(self):
        trades_, decisions, baseline = self._paired_arms(unresolved=100)
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades_, decisions=decisions,
            baseline_trades=trades_, baseline_decisions=baseline,
            baseline_variant_id="momentum.baseline")
        self.assertEqual(candidate.coverage_pct, 50.0)
        self.assertEqual(candidate.pair_coverage_pct, 50.0)
        self.assertEqual(candidate.label, shortlist.INCONCLUSIVE)

    def test_starved_decisions_are_not_zero_return_opportunities(self):
        trades_, decisions, baseline = self._paired_arms(declines=10)
        decisions.extend(self._decision(f"s{i}", "VETOED", "data missing")
                         for i in range(100))
        baseline.extend(self._decision(f"s{i}", "VETOED", "data missing")
                        for i in range(100))
        candidate = shortlist.measure(
            "staged.x", "demo:s", trades_, decisions=decisions,
            baseline_trades=trades_, baseline_decisions=baseline,
            baseline_variant_id="momentum.baseline")
        self.assertEqual(candidate.starved_decisions, 100)
        self.assertEqual(candidate.eligible_opportunities, 110)
        self.assertAlmostEqual(candidate.mean_r, 100 / 110, places=3)

    def test_replay_and_shortlist_use_the_same_canonical_identity(self):
        replay = ReplayDecision(
            cycle_id="different-cycle", ts=10.0, symbol="BTC/USDT:USDT",
            signal_ts=123, stage="executed", direction="long",
            setup_type="range_breakout", proposal_id="proposal-1")
        row = {"proposal_id": "proposal-1", "symbol": "other-symbol",
               "signal_ts": 999, "direction": "short",
               "setup_type": "other"}
        self.assertEqual(replay.proposal_key(), shortlist._proposal_key(row))

    def test_duplicate_rows_are_excluded_and_block_supported(self):
        trade_rows = [self._trade(i, 1.0, f"p{i}") for i in range(100)]
        trade_rows.extend([
            self._trade(100, 1.0, "duplicate"),
            self._trade(101, 2.0, "duplicate"),
            self._trade(102, 3.0, "duplicate"),
        ])
        decisions = [self._decision(f"p{i}") for i in range(100)]
        decisions.extend([
            self._decision("duplicate"),
            self._decision("duplicate"),
            self._decision("duplicate"),
        ])
        baseline_trades = [self._trade(i, 0.0, f"p{i}") for i in range(100)]
        baseline_decisions = [self._decision(f"p{i}") for i in range(100)]
        candidate = shortlist.measure(
            "staged.x", "demo:s", trade_rows, decisions=decisions,
            baseline_trades=baseline_trades,
            baseline_decisions=baseline_decisions,
            baseline_variant_id="momentum.baseline")

        self.assertEqual(candidate.trades, 100)
        self.assertEqual(candidate.eligible_opportunities, 100)
        self.assertEqual(candidate.duplicate_count, 4)
        self.assertEqual(candidate.pair_coverage_pct, 100.0)
        self.assertNotEqual(candidate.label, shortlist.SUPPORTED)
        self.assertIn("duplicate", " ".join(candidate.reasons).lower())
