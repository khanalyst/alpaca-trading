"""Focused regressions for qualification and executable research fidelity."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from agent.forward_models import BY_STRATEGY
from agent.registry import spec_for
from research import gates, shortlist, tournament
from research.edge_lab import (COST_SCENARIOS, FundingCarryContract, simulate,
                               summarize)


def paired_rows(candidate_returns, baseline_returns):
    candidate_trades = []
    baseline_trades = []
    candidate_decisions = []
    baseline_decisions = []
    for index, (candidate, baseline) in enumerate(
            zip(candidate_returns, baseline_returns)):
        proposal_id = f"paired-{index}"
        # One observation per six-hour episode makes the number of genuine
        # inference units explicit in the fixture.
        ts = float((index + 1) * shortlist.PAIR_CLUSTER_SECONDS)
        common = {
            "status": "CLOSED", "result": "target",
            "valid_for_inference": 1, "proposal_id": proposal_id,
            "entry_ts": ts,
        }
        candidate_trades.append({**common, "r_multiple": candidate,
                                 "net_pnl_usd": candidate})
        baseline_trades.append({**common, "r_multiple": baseline,
                                "net_pnl_usd": baseline})
        decision = {"proposal_id": proposal_id,
                    "decision_outcome": "PROPOSED", "decision_ts": ts}
        candidate_decisions.append(dict(decision))
        baseline_decisions.append(dict(decision))
    return (candidate_trades, baseline_trades,
            candidate_decisions, baseline_decisions)


class HeldOutPairedQualificationTests(unittest.TestCase):
    def measure(self, candidate_returns, baseline_returns):
        candidate, baseline, candidate_decisions, baseline_decisions = (
            paired_rows(candidate_returns, baseline_returns))
        return shortlist.measure(
            "staged.regression", "demo:staged", candidate,
            source="staged", decisions=candidate_decisions,
            baseline_trades=baseline,
            baseline_decisions=baseline_decisions,
            baseline_variant_id="staged.regression.baseline")

    def test_fit_winner_that_loses_to_baseline_in_confirmation_is_not_supported(self):
        measured = self.measure(
            [0.5] * 70 + [0.1] * 30,
            [0.0] * 70 + [0.5] * 30)

        self.assertGreater(measured.delta_vs_baseline, 0)
        self.assertAlmostEqual(
            measured.confirmation_delta_vs_baseline, -0.4, places=6)
        self.assertEqual(measured.label, shortlist.INCONCLUSIVE)
        self.assertLess(measured.confirmation_delta_ci_high, 0)
        self.assertIn(
            "held-out paired candidate-minus-baseline interval",
            " ".join(measured.reasons))

    def test_positive_paired_confirmation_can_support(self):
        measured = self.measure([0.5] * 100, [0.0] * 100)

        self.assertEqual(measured.confirmation_pairs, 30)
        self.assertEqual(measured.confirmation_clusters, 30)
        self.assertLessEqual(measured.confirmation_p_value,
                             shortlist.FDR_ALPHA)
        self.assertEqual(measured.p_value, measured.confirmation_p_value)
        self.assertEqual(measured.label, shortlist.SUPPORTED)

    def test_staged_trade_only_evidence_fails_closed(self):
        trades = [{
            "status": "CLOSED", "result": "target",
            "valid_for_inference": 1, "r_multiple": 0.5,
            "net_pnl_usd": 0.5, "entry_ts": float(index),
        } for index in range(120)]

        measured = shortlist.measure(
            "staged.trade_only", "demo:staged", trades, source="staged")

        self.assertEqual(measured.label, shortlist.INCONCLUSIVE)
        self.assertIn("requires matched candidate/baseline decisions",
                      " ".join(measured.reasons))


class TournamentInferenceAndEconomicsTests(unittest.TestCase):
    @staticmethod
    def trades_for_scenario(_frames, _membership, _contract, costs,
                            **_kwargs):
        expectancy = {
            "frictionless": 0.8,
            "optimistic": 0.4,
            "base": 0.2,
            "account_taker": -0.1,
            "realistic_alt": -0.2,
            "stress": -0.4,
            "execution_frictionless_same_funding": 0.8,
        }[costs.name]
        return pd.DataFrame({"net_pct": [expectancy]})

    def test_cost_survival_uses_the_headline_fee_and_slippage_scenario(self):
        with patch.object(gates, "build_trades",
                          side_effect=self.trades_for_scenario), \
                patch.object(gates, "non_overlapping", side_effect=lambda x: x):
            result = gates.survive_costs(
                {}, {}, object(), cost_name="account_taker")

        self.assertFalse(result.passed)
        self.assertEqual(result.numbers["headline_cost_scenario"],
                         "account_taker")
        self.assertAlmostEqual(
            result.numbers["headline_round_trip_cost_pct"], 0.6)
        self.assertEqual(
            result.numbers["expectancy_pct_by_scenario"]["account_taker"],
            -0.1)

    def test_clustered_headline_p_value_is_wired_for_family_correction(self):
        timestamps = pd.to_datetime(
            [f"2025-{month:02d}-15" for month in range(1, 9)], utc=True)
        returns = [0.4, 0.6] * 4
        trades = pd.DataFrame({
            "net_pct": returns,
            "r_multiple": returns,
            "entry_ts": [stamp.timestamp() * 1_000 for stamp in timestamps],
            "bars_held": [4] * 8,
            "exit_reason": ["take_profit"] * 8,
        })

        headline = summarize(trades)
        corrected = gates.tier_from_settings([{
            "setting_id": "registered", "tier": "T2_CANDIDATE",
            "why": "all gates passed", "results": [],
            "headline": headline,
        }, {
            "setting_id": "control", "tier": "T1_HYPOTHESIS",
            "why": "control", "results": [],
            "headline": {**headline, "p_value": 1.0},
        }])

        self.assertEqual(headline["significance_test"]["clusters"], 8)
        self.assertLess(headline["p_value"], 0.05)
        self.assertLess(corrected[2]["p_adjusted"], 0.05)


class FundingExitFidelityTests(unittest.TestCase):
    def test_carry_simulation_exits_on_funding_normalisation_without_a_target(self):
        frame = SimpleNamespace(
            symbol="BTC/USDT:USDT",
            df=pd.DataFrame({
                "open": [100.0] * 5, "high": [100.0] * 5,
                "low": [100.0] * 5, "close": [100.0] * 5,
                "funding_percentile_30": [90.0, 90.0, 80.0, 40.0, 40.0],
            }),
            ts=np.arange(5, dtype=np.int64) * 900_000,
            cum_funding=np.zeros(5), funding_covered_from=None,
            funding_interval_h=8.0, funding_median_pct=0.0,
        )

        trades = simulate(
            frame, np.array([0]), "short", np.array([10.0]),
            np.array([np.nan]), COST_SCENARIOS["frictionless"], 3,
            funding_exit_percentile=50.0)

        self.assertEqual(trades.iloc[0]["exit_reason"],
                         "funding_normalised")
        self.assertEqual(int(trades.iloc[0]["exit_idx"]), 3)
        self.assertTrue(np.isnan(trades.iloc[0]["take_price"]))

    def test_funding_tournaments_use_the_authoritative_forward_exit(self):
        for strategy_id in ("funding-carry", "funding-unwind"):
            observed = []

            def run_all(*_args, **kwargs):
                observed.append(kwargs["exit_policy"])
                return []

            def headline(*_args, **_kwargs):
                return {"trades": 0}

            def choose(settings):
                return "T1_HYPOTHESIS", "fixture", settings[0]

            with self.subTest(strategy=strategy_id), \
                    patch.object(tournament.gate_mod, "run_all",
                                 side_effect=run_all), \
                    patch.object(tournament.gate_mod, "tier_from_gates",
                                 return_value=("T1_HYPOTHESIS", "fixture")), \
                    patch.object(tournament.gate_mod, "headline",
                                 side_effect=headline), \
                    patch.object(tournament.gate_mod, "tier_from_settings",
                                 side_effect=choose):
                row = tournament.score_strategy(
                    spec_for(strategy_id), {}, {}, None,
                    "account_taker", "fixed_rr")

            expected = BY_STRATEGY[strategy_id].exit_policy
            self.assertTrue(observed)
            self.assertEqual(set(observed), {expected})
            self.assertEqual(row["exit_policy"], expected)


if __name__ == "__main__":
    unittest.main()
