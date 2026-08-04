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


class ClosedTradeFloorIsReachableTests(unittest.TestCase):
    """A verdict must be reachable by collection, not only computable.

    The suite already proved a WORKED verdict at 100 closed trades, but it
    did so by writing 100 closed trades straight into SQLite. Nothing asked
    whether the collection path could ever produce them, and it could not: at
    three concurrent slots and a 48h horizon a ten-day assignment yields
    roughly fifteen. Six of seven strategies were short by up to 47x while
    every test passed, because the one number that mattered was supplied by
    the fixture rather than earned by the system.
    """

    def achievable_closes(self, spec, cfg) -> float:
        """Closed trades one arm can produce across its whole attempt budget.

        A strategy holds at most `max_concurrent_positions` at once and each
        position occupies a slot for its full horizon, so throughput is
        bounded by slots divided by holding time regardless of how often it
        signals.
        """
        from agent.forward_models import model_for
        from research.findings import MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS

        horizon_days = model_for(spec.id).horizon_hours / 24.0
        slots = int(cfg["risk"]["max_concurrent_positions"])
        days = (float(cfg["research"]["experiment_min_duration_days"])
                * MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS)
        return slots / horizon_days * days

    def test_every_realtime_strategy_can_reach_the_closed_trade_floor(self):
        from agent import registry

        cfg = shipped_config()
        short = {}
        for spec in registry.REGISTRY.values():
            if not spec.realtime_eligible:
                continue
            closes = self.achievable_closes(spec, cfg)
            if closes < protocol.MIN_ROUND_TRIPS:
                short[spec.id] = round(closes, 1)

        self.assertEqual(
            short, {},
            "strategies held in the real-time loop that cannot reach "
            f"{protocol.MIN_ROUND_TRIPS} closed trades within the attempt "
            f"budget: {short}. Either the horizon is too long for real-time "
            "testing (set realtime_eligible=False and research it offline) "
            "or the attempt budget is too small.")

    def test_the_offline_strategies_are_offline_for_this_reason(self):
        """Not preference: they are short by an order of magnitude.

        If one of them ever becomes reachable this test fails, which is the
        signal to put it back in the loop rather than leave it excluded out
        of habit.
        """
        from agent import registry

        cfg = shipped_config()
        offline = [spec for spec in registry.REGISTRY.values()
                   if not spec.realtime_eligible]
        self.assertTrue(offline, "no strategy is marked offline-only")
        for spec in offline:
            with self.subTest(strategy=spec.id):
                self.assertLess(
                    self.achievable_closes(spec, cfg),
                    protocol.MIN_ROUND_TRIPS,
                    f"{spec.id} can now reach the floor in real time; "
                    "reconsider realtime_eligible")

    def test_the_attempt_budget_is_what_makes_the_floor_reachable(self):
        """One attempt is not enough, which is why attempts pool.

        Pinning this stops the budget being quietly lowered back to a value
        that reads as a retry allowance but silently makes every verdict
        unreachable again.
        """
        from agent import registry
        from research.findings import MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS

        cfg = shipped_config()
        single = dict(cfg)
        slowest = max(
            (spec for spec in registry.REGISTRY.values()
             if spec.realtime_eligible),
            key=lambda spec: self.achievable_closes(spec, cfg) * -1)

        per_attempt = (self.achievable_closes(slowest, single)
                       / MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS)
        self.assertLess(per_attempt, protocol.MIN_ROUND_TRIPS)
        self.assertGreaterEqual(
            per_attempt * MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS,
            protocol.MIN_ROUND_TRIPS)


class ObservationFloorIsExercisedTests(unittest.TestCase):
    """The shipped observation gate must not be a fixture-only number.

    Most rotation fixtures pass minimum_observations=1 so an assignment
    completes on the first paired observation. That is reasonable for testing
    ordering and lifecycle, but it means the gate that actually ships was
    never the gate under test, and a floor nothing exercises is a floor that
    can drift.
    """

    def test_the_shipped_observation_floor_matches_the_trade_floor(self):
        research = shipped_config()["research"]

        self.assertEqual(
            int(research["experiment_min_observations"]),
            protocol.MIN_ROUND_TRIPS,
            "the paired-observation floor and the closed-trade floor answer "
            "the same question and must not drift apart")
        self.assertEqual(
            int(research["paper_min_closed_trades"]),
            protocol.MIN_ROUND_TRIPS)

    def test_the_observation_gate_actually_blocks_at_the_shipped_value(self):
        """At the shipped floor, one observation must not complete anything.

        The fixtures set this to 1, so without this test nothing anywhere
        demonstrates that the real value is load-bearing.
        """
        import tempfile
        from pathlib import Path

        from agent import variants
        from research.findings import FindingsStore

        with tempfile.TemporaryDirectory() as tmp:
            store = FindingsStore(Path(tmp) / "findings.db")
            baseline = variants.baseline("momentum", "phase1-v3")
            store.register(baseline)
            registry = variants.load_registry(REPO / "research" / "variants.yaml")
            candidate = registry["momentum.rr.fixed_2_5"]
            store.register(candidate)
            floor = int(shipped_config()["research"][
                "experiment_min_observations"])
            assignment = store.ensure_experiment_assignment(
                "demo:observation-floor", "momentum", baseline.variant_id,
                [{
                    "variant_id": candidate.variant_id,
                    "candidate_key": "observation-floor-candidate",
                    "axis": next(iter(candidate.overrides)),
                    "setting_id": "fixed_2_5",
                    "setting": dict(candidate.overrides),
                    "source": "static", "priority": 10,
                    "order_key": candidate.variant_id,
                    "code_identity": {"baseline": {}, "candidate": {}},
                    "config_identity": {"baseline": {}, "candidate": {}},
                }],
                minimum_duration_seconds=0, minimum_observations=floor,
                now=0.0)

            store.record_experiment_observations(
                assignment["assignment_id"],
                [{"observation_key": "only-one"}], now=1.0)
            state = store.maybe_complete_experiment_assignment(
                assignment["assignment_id"], now=2.0)

        self.assertNotEqual(
            state["status"], "COMPLETED",
            f"one observation completed an assignment needing {floor}")


class SignalCadenceCanDeliverTheFloorTests(unittest.TestCase):
    """The bar that produces observations must produce enough of them.

    Duration and observations are two independent gates and an assignment
    needs both. Nothing compared them, so a signal timeframe long enough to
    starve the observation floor inside the duration window would have
    produced the same silent INCONCLUSIVE the duration bug produced - and the
    fixtures could not have caught it, because they set the floor to 1.
    """

    BARS_PER_DAY = {
        "1m": 1440, "5m": 288, "15m": 96, "30m": 48,
        "1h": 24, "4h": 6, "1d": 1,
    }

    def test_every_realtime_strategy_can_observe_enough_inside_the_window(self):
        from agent import registry
        from research.findings import MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS

        cfg = shipped_config()
        floor = int(cfg["research"]["experiment_min_observations"])
        days = (float(cfg["research"]["experiment_min_duration_days"])
                * MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS)
        starved = {}
        for spec in registry.REGISTRY.values():
            if not spec.realtime_eligible:
                continue
            per_day = self.BARS_PER_DAY.get(spec.signal_timeframe)
            self.assertIsNotNone(
                per_day, f"unmapped signal timeframe {spec.signal_timeframe}")
            # One evaluation per bar per symbol; a single symbol is the
            # conservative floor, so the universe only ever helps.
            if per_day * days < floor:
                starved[spec.id] = per_day * days

        self.assertEqual(
            starved, {},
            "strategies whose signal cadence cannot deliver "
            f"{floor} observations inside the assignment window: {starved}")

    def test_the_binding_gate_is_closed_trades_not_observations(self):
        """Which gate bites is a fact worth pinning.

        Observations are cheap - one per bar - while closed trades are bounded
        by concurrency and holding time. If observations ever became the
        binding constraint the diagnosis for an INCONCLUSIVE run would change
        completely, so the relationship should not flip silently.
        """
        from agent import registry
        from agent.forward_models import model_for
        from research.findings import MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS

        cfg = shipped_config()
        days = (float(cfg["research"]["experiment_min_duration_days"])
                * MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS)
        slots = int(cfg["risk"]["max_concurrent_positions"])
        for spec in registry.REGISTRY.values():
            if not spec.realtime_eligible:
                continue
            with self.subTest(strategy=spec.id):
                observations = self.BARS_PER_DAY[spec.signal_timeframe] * days
                closes = slots / (model_for(spec.id).horizon_hours / 24.0) * days
                self.assertGreater(observations, closes)


class TierClaimsAreTraceableTests(unittest.TestCase):
    """A tier claim must be checkable against the artifact it cites.

    registry.py states the numbers that keep momentum away from live capital
    and names the report they come from. Nothing joined the two, so the prose
    and the evidence could drift apart in either direction - and the prose is
    what a reader, or a reviewing model, actually acts on.
    """

    # The figures momentum's T0_REJECTED verdict rests on.
    MOMENTUM_CLAIMS = ("45.6", "47.3", "0.096", "115,929")

    def test_momentum_tier_numbers_appear_in_the_cited_report(self):
        from agent import registry

        spec = registry.spec_for("momentum")
        self.assertEqual(spec.tier, "T0_REJECTED")
        cited = [path for path in spec.evidence if path.endswith(".md")]
        self.assertTrue(cited, "momentum's tier cites no readable artifact")

        corpus = "\n".join(
            (REPO / path).read_text(encoding="utf-8") for path in cited)
        prose = (REPO / "agent" / "registry.py").read_text(encoding="utf-8")
        for claim in self.MOMENTUM_CLAIMS:
            with self.subTest(claim=claim):
                self.assertIn(
                    claim, prose,
                    f"{claim} is no longer claimed in the registry; update "
                    "this test if the verdict's basis genuinely changed")
                self.assertIn(
                    claim, corpus,
                    f"the registry claims {claim} but no cited artifact "
                    f"contains it: {cited}")

    def test_every_cited_evidence_path_exists(self):
        from agent import registry

        missing = []
        for spec in registry.REGISTRY.values():
            for path in spec.evidence:
                if not (REPO / path.split("#")[0]).exists():
                    missing.append(f"{spec.id} -> {path}")

        self.assertEqual(
            missing, [],
            f"tier evidence citing files that do not exist: {missing}")


class CommittedReportsDeclareTheirCostBasisTests(unittest.TestCase):
    """A report built at one fee must not be read as though it used another.

    The configured taker fee was corrected from 0.05%/side to the measured
    0.248%/side, and nothing propagated that to the committed reports. Every
    after-cost table under research/results/ still describes an account nobody
    trades, and those tables are what the tier verdicts cite. Nothing compared
    the two numbers, so nothing complained - the same failure mode as the
    original fee bug, one layer up.

    The reports are deliberately NOT restated: `base` is preserved so they stay
    reproducible, and the dataset behind them is not committed. What is
    enforced here is that a reader cannot reach the numbers without meeting
    the correction first.
    """

    REPORTS = (
        "research/results/edge-audit-2024-2026/REPORT.md",
        "research/results/edge-discovery-method/REPORT.md",
    )

    def test_reports_quoting_the_old_fee_carry_a_correction(self):
        for name in self.REPORTS:
            with self.subTest(report=name):
                path = REPO / name
                self.assertTrue(path.exists(), f"{name} is missing")
                text = path.read_text(encoding="utf-8")
                if "0.05% per side" not in text and "0.05%/side" not in text:
                    continue          # nothing stale left to warn about
                self.assertIn(
                    "Cost basis correction", text,
                    f"{name} quotes the superseded 0.05%/side basis with no "
                    "correction; a reader will take its after-cost "
                    "conclusions at face value")

    def test_the_correction_names_the_rate_actually_paid(self):
        from research.edge_lab import COST_SCENARIOS, DEFAULT_COST_SCENARIO

        measured = COST_SCENARIOS[DEFAULT_COST_SCENARIO].fee_per_side
        for name in self.REPORTS:
            with self.subTest(report=name):
                text = (REPO / name).read_text(encoding="utf-8")
                if "Cost basis correction" not in text:
                    continue
                self.assertIn(
                    "0.248", text,
                    f"{name}'s correction does not state the measured rate")
                self.assertGreater(measured, 0.2)

    def test_the_historical_scenario_is_preserved_not_edited(self):
        """`base` must keep meaning what the committed reports meant by it.

        Correcting it in place would rewrite every published number without
        rewriting the documents that quote them - which is worse than the
        drift being fixed, because it would be invisible.
        """
        from research.edge_lab import COST_SCENARIOS

        self.assertEqual(COST_SCENARIOS["base"].fee_per_side, 0.05)
        self.assertNotEqual(
            COST_SCENARIOS["base"].fee_per_side,
            COST_SCENARIOS["account_taker"].fee_per_side)
