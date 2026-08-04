"""Regressions for defects the suite could not see.

Each test here corresponds to something that was broken in production or in a
full-length simulation while every existing test passed. They are grouped
because they share a cause: the suite checked that components behaved as
specified and never checked that the specification was self-consistent.
"""

import pathlib
import unittest

import yaml

from agent import market
from agent.config import ConfigError, validate_config
from research import protocol
from research.replay import ReplayDecision
from tests.helpers import valid_config

REPO = pathlib.Path(__file__).resolve().parents[2]


def shipped_config() -> dict:
    """The real config.yaml, not the synthetic unit-test fixture.

    These assertions are about what actually ships, so reading the fixture
    would let the two drift apart - which is the class of bug this module
    exists to catch.
    """
    return yaml.safe_load((REPO / "config.yaml").read_text())


class DurationFloorIsReachableTests(unittest.TestCase):
    """A collection window shorter than its own evidence floor is a null run.

    The confirmation window is the last 30% of the assignment calendar and
    needs MIN_BOOTSTRAP_CLUSTERS distinct six-hour episodes. At the shipped
    3 days that yields 3.6 episodes, so every strategy reported exactly 4
    confirm clusters and returned INCONCLUSIVE no matter how much evidence it
    gathered. The protocol constant and the calendar were never compared.
    """

    def test_configured_duration_can_satisfy_the_cluster_floor(self):
        cfg = shipped_config()
        days = cfg["research"]["experiment_min_duration_days"]
        confirm_seconds = days * 86_400 * 0.30
        clusters = confirm_seconds / protocol.PAIR_CLUSTER_SECONDS

        self.assertGreaterEqual(clusters, protocol.MIN_BOOTSTRAP_CLUSTERS)

    def test_a_duration_below_the_floor_is_refused(self):
        # The previously shipped value. It must now fail closed rather than
        # start and quietly collect evidence that can never conclude.
        cfg = shipped_config()
        cfg["research"]["experiment_min_duration_days"] = 3
        with self.assertRaises(ConfigError) as caught:
            validate_config(cfg)

        self.assertIn("experiment_min_duration_days", str(caught.exception))

    def test_the_shipped_config_validates(self):
        self.assertTrue(validate_config(shipped_config()))


class ConcordantVetoTests(unittest.TestCase):
    """Concordant vetoes stay in the estimate; an all-veto window is inadequate.

    Both halves matter and they pull in opposite directions.

    Keeping them is required for the estimand. A policy runs over every
    opportunity, so a variant that gains +1R on 1% of them is worth +0.01R
    per opportunity. Dropping the zeros reports +1R instead and removes the
    uncertainty those clusters carry, which would let a rarely-active variant
    clear promotion on an inflated effect - and would contradict this
    protocol's own rule that a veto is an explicit paired 0R action.

    Refusing an all-veto window is required for correctness. With nothing
    informative the interval collapses onto exactly [0, 0], which the outcome
    evaluator reads as a nonpositive delta and records as FAILED, booking a
    strategy that never fired as one that was tested and lost.
    """

    @staticmethod
    def _decision(index, *, r_multiple, result):
        decision = ReplayDecision(
            cycle_id=f"cycle-{index}", ts=1_000.0 + index * 21_600,
            symbol="BTC/USDT:USDT", signal_ts=1_000_000 + index,
            stage="executed" if result != "vetoed" else "vetoed",
            direction="long", setup_type="trend_continuation",
            contract_passed=True, proposal_id=f"proposal-{index}")
        decision.outcome = {"r_multiple": r_multiple, "result": result}
        return decision

    def _arms(self, informative, concordant_vetoes):
        left, right = [], []
        for i in range(informative):
            left.append(self._decision(i, r_multiple=1.0, result="target"))
            right.append(self._decision(i, r_multiple=0.0, result="vetoed"))
        for i in range(informative, informative + concordant_vetoes):
            left.append(self._decision(i, r_multiple=0.0, result="vetoed"))
            right.append(self._decision(i, r_multiple=0.0, result="vetoed"))
        return left, right

    def test_concordant_vetoes_are_counted_and_kept(self):
        left, right = self._arms(informative=40, concordant_vetoes=4_000)
        result = protocol.paired_arm_comparison(left, right)

        self.assertEqual(result["paired_n"], 4_040)
        self.assertEqual(result["informative_pairs"], 40)
        self.assertEqual(result["concordant_veto_pairs"], 4_000)

    def test_the_estimand_stays_per_opportunity(self):
        """+1R on 1% of opportunities is worth +0.01R, and must report as it.

        Dropping the zeros would report +1R for this variant - the return
        conditional on it acting - which is not the number a policy decision
        turns on.
        """
        left, right = self._arms(informative=40, concordant_vetoes=3_960)
        result = protocol.paired_arm_comparison(left, right)

        self.assertAlmostEqual(result["interval"].point, 0.01, places=6)

    def test_coverage_counts_every_matched_proposal(self):
        left, right = self._arms(informative=10, concordant_vetoes=90)
        result = protocol.paired_arm_comparison(left, right)

        self.assertEqual(result["pair_coverage_pct"], 100.0)

    def test_an_all_veto_window_is_inadequate_not_negative(self):
        """A strategy that never fired must not look like one that was tested.

        All-concordant pairs yield an interval of exactly [0, 0], which the
        outcome evaluator reads as BASELINE_DELTA_NONPOSITIVE and records as
        FAILED. Zero informative pairs routes it to the adequacy gate
        instead, where it belongs.
        """
        left, right = self._arms(informative=0, concordant_vetoes=500)
        result = protocol.paired_arm_comparison(left, right)

        self.assertEqual(result["informative_pairs"], 0)
        self.assertEqual(result["interval"].low, 0.0)
        self.assertEqual(result["interval"].high, 0.0)
        self.assertFalse(protocol.paired_window_adequate(result, 1))

    def test_a_mixed_window_is_not_refused_by_the_informative_floor(self):
        # The floor targets the all-veto case only; it must not become a
        # second sample requirement on ordinary mixed evidence.
        left, right = self._arms(informative=1, concordant_vetoes=200)
        result = protocol.paired_arm_comparison(left, right)

        self.assertEqual(result["informative_pairs"], 1)
        self.assertTrue(protocol.paired_window_adequate(result, 1))


class FeeDivergenceTests(unittest.TestCase):
    """Configured cost must describe the account actually being traded.

    Live sizing prefers the fetched rate, so a divergence never showed up as
    a trading bug. Every offline backtest, tournament run and tier verdict
    reads the CONFIGURED number, so the shipped 0.05%/side against a real
    0.248%/side made every after-cost figure in research/results/ describe an
    account nobody was trading - silently, with the suite fully green.
    """

    def test_matching_rates_pass(self):
        cfg = valid_config()
        cfg["trading_costs"]["taker_fee_pct_per_side"] = 0.25
        self.assertEqual(market.fee_divergence(cfg, 0.25)["status"], "ok")

    def test_the_measured_understatement_is_caught(self):
        cfg = valid_config()
        cfg["trading_costs"]["taker_fee_pct_per_side"] = 0.05
        report = market.fee_divergence(cfg, 0.248)

        self.assertEqual(report["status"], "diverged")
        self.assertAlmostEqual(report["divergence_pct"], 79.8, places=1)

    def test_an_unavailable_rate_is_not_reported_as_agreement(self):
        cfg = valid_config()
        for value in (None, 0.0, float("nan"), "n/a"):
            with self.subTest(value=value):
                self.assertEqual(
                    market.fee_divergence(cfg, value)["status"], "unavailable")

    def test_the_shipped_config_matches_the_measured_account_rate(self):
        # 0.2481%/side, derived from the demo journal independently of
        # contract size. If the account tier changes, this and the research
        # cost scenario move together or the corpus goes stale again.
        self.assertEqual(
            market.fee_divergence(shipped_config(), 0.2481)["status"], "ok")

    def test_research_cost_scenario_tracks_the_configured_rate(self):
        from research.edge_lab import COST_SCENARIOS, DEFAULT_COST_SCENARIO

        cfg = shipped_config()
        scenario = COST_SCENARIOS[DEFAULT_COST_SCENARIO]
        self.assertEqual(
            scenario.fee_per_side,
            cfg["trading_costs"]["taker_fee_pct_per_side"])


if __name__ == "__main__":
    unittest.main()
