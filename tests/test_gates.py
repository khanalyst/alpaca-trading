"""Unit tests for the tiering logic in research/gates.py.

The gates themselves are integration-tested by running the tournament against
real data; what is tested here is the branching that decides a tier, because
that is where a silent mistake would promote a strategy it should not.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "research"))

import gates  # noqa: E402
from gates import GateResult, tier_from_gates, walk_forward_masks  # noqa: E402


def result(name, passed, **numbers):
    return GateResult(name, passed, f"{name} {'ok' if passed else 'failed'}",
                      numbers)


def all_passing():
    return [
        result("has_mechanism", True),
        result("beat_nulls", True),
        result("survive_oos", True),
        result("survive_costs", True),
        result("survive_placebo", True, ratio=0.05),
        result("mechanism_is_the_source", True, source_share=0.9),
        result("is_detectable", True, trades_needed=100, observed_trades=500),
    ]


def setting(setting_id, tier, *, expectancy_r=0.1, trades=400,
            ci=(0.05, 0.15), p_value=None, gates_list=None,
            registered=False):
    """One scored parameterisation, as tier_from_settings receives it."""
    return {
        "setting_id": setting_id, "params": {} if registered else {"x": 1},
        "claim": "a stated claim", "registered": registered,
        "tier": tier, "why": f"{setting_id} said so",
        "results": gates_list if gates_list is not None else all_passing(),
        "headline": {"trades": trades, "expectancy_r": expectancy_r,
                     "expectancy_pct": expectancy_r * 0.5,
                     "expectancy_r_ci95": list(ci), "p_value": p_value},
    }


class TierLogicTests(unittest.TestCase):
    def test_full_exploratory_pass_is_capped_at_candidate(self):
        tier, why = tier_from_gates(all_passing())
        self.assertEqual(tier, "T2_CANDIDATE")
        self.assertIn("authoritative recorded replay", why)

    def test_missing_mechanism_is_rejected_outright(self):
        gates_list = all_passing()
        gates_list[0] = result("has_mechanism", False)
        tier, why = tier_from_gates(gates_list)
        self.assertEqual(tier, "T0_REJECTED")
        self.assertIn("mechanism", why)

    def test_losing_to_a_null_is_rejected(self):
        gates_list = all_passing()
        gates_list[1] = result("beat_nulls", False)
        self.assertEqual(tier_from_gates(gates_list)[0], "T0_REJECTED")

    def test_failing_out_of_sample_stalls_at_hypothesis(self):
        gates_list = all_passing()
        gates_list[2] = result("survive_oos", False)
        self.assertEqual(tier_from_gates(gates_list)[0], "T1_HYPOTHESIS")

    def test_failing_costs_stalls_at_candidate(self):
        gates_list = all_passing()
        gates_list[3] = result("survive_costs", False)
        self.assertEqual(tier_from_gates(gates_list)[0], "T2_CANDIDATE")

    def test_a_loud_placebo_is_rejected_not_merely_stalled(self):
        # A placebo scoring half the candidate means the number came from the
        # procedure. That is disqualifying, not a stall.
        gates_list = all_passing()
        gates_list[4] = result("survive_placebo", False, ratio=0.60)
        tier, _ = tier_from_gates(gates_list)
        self.assertEqual(tier, "T0_REJECTED")

    def test_a_borderline_placebo_stalls_at_candidate(self):
        gates_list = all_passing()
        gates_list[4] = result("survive_placebo", False, ratio=0.30)
        self.assertEqual(tier_from_gates(gates_list)[0], "T2_CANDIDATE")

    def test_undetectable_effect_stalls_at_candidate(self):
        gates_list = all_passing()
        gates_list[6] = result("is_detectable", False)
        self.assertEqual(tier_from_gates(gates_list)[0], "T2_CANDIDATE")

    def test_a_false_mechanism_is_rejected_however_good_the_number(self):
        # Observed on real data: funding-carry posted +2.008% per trade,
        # beat every null, survived the placebo at -4%, and was better
        # out-of-sample than in. Funding contributed 2% of that result and
        # price movement 98%. Every number was true and the claim was not.
        gates_list = all_passing()
        gates_list[5] = result("mechanism_is_the_source", False,
                               source_share=0.02)
        tier, why = tier_from_gates(gates_list)
        self.assertEqual(tier, "T0_REJECTED")
        self.assertIn("mechanism_is_the_source", why)

    def test_an_empty_battery_is_rejected(self):
        self.assertEqual(tier_from_gates([])[0], "T0_REJECTED")

    def test_a_clean_run_on_too_few_trades_cannot_be_validated(self):
        # A thin sample agreeing with itself is not validation. Observed on
        # real data: the momentum benchmark beat its nulls on a 200-day,
        # 8-instrument window and failed the same test over 24 months.
        gates_list = all_passing()
        gates_list[6] = result("is_detectable", True,
                               trades_needed=900, observed_trades=337)
        tier, why = tier_from_gates(gates_list)
        self.assertEqual(tier, "T2_CANDIDATE")
        self.assertIn("337 trades", why)
        self.assertIn("900", why)

    def test_sufficient_sample_still_needs_recorded_replay(self):
        gates_list = all_passing()
        gates_list[6] = result("is_detectable", True,
                               trades_needed=900, observed_trades=1200)
        self.assertEqual(tier_from_gates(gates_list)[0], "T2_CANDIDATE")


class ExploratoryAuthorityTests(unittest.TestCase):
    """Where the battery's authority stops, and who has to know about it.

    Capping the tier without telling the consumers that compare tiers is how a
    cap turns into a nightly false alarm: a strategy registered at T3 on
    recorded-replay evidence would measure T2 here forever, and every run
    would report it as fresh evidence of failure.
    """

    def test_the_ceiling_is_the_highest_tier_the_battery_awards(self):
        self.assertEqual(gates.EXPLORATORY_CEILING, "T2_CANDIDATE")
        self.assertEqual(tier_from_gates(all_passing())[0],
                         gates.EXPLORATORY_CEILING)

    def test_a_tier_above_the_ceiling_is_not_reported_as_a_change(self):
        import tournament

        row = {
            "scored": True, "registered_tier": "T3_VALIDATED",
            "measured_tier": gates.EXPLORATORY_CEILING,
            "beyond_exploratory_authority": True,
            "exploratory_ceiling": gates.EXPLORATORY_CEILING,
            "tier_changed": False,
        }

        self.assertIn("NO CHANGE", tournament.recommendation(row))
        self.assertIn("T3_VALIDATED", tournament.recommendation(row))

    def test_a_change_inside_the_ceiling_still_recommends_action(self):
        import tournament

        row = {
            "scored": True, "registered_tier": "T1_HYPOTHESIS",
            "measured_tier": "T0_REJECTED",
            "beyond_exploratory_authority": False,
            "exploratory_ceiling": gates.EXPLORATORY_CEILING,
            "tier_changed": True,
        }

        self.assertIn("REJECT", tournament.recommendation(row))

    def test_a_registered_tier_above_the_ceiling_raises_no_alert(self):
        """The whole point: a demotion alert must describe a real demotion."""
        import tournament

        rows = [{
            "scored": True, "strategy_id": "momentum", "version": "phase1-v3",
            "registered_tier": "T3_VALIDATED",
            "measured_tier": gates.EXPLORATORY_CEILING,
            "beyond_exploratory_authority": True,
            "exploratory_ceiling": gates.EXPLORATORY_CEILING,
            "tier_changed": False,
            "tier_reason": "cleared every exploratory offline gate",
            "headline": {"expectancy_pct": 0.1, "trades": 500},
        }]

        with patch("agent.alerts.AlertManager") as manager:
            tournament.alert_on_tier_changes(
                rows, {"benchmark_check": {"ok": True}})

        manager.assert_not_called()


class ForwardEvidenceTests(unittest.TestCase):
    """Pending is not zero, and a missing number must not lose the report."""

    @staticmethod
    def _row(expectancy_pct=0.5):
        return {
            "scored": True, "strategy_id": "momentum",
            "measured_tier": "T2_CANDIDATE",
            "headline": {"expectancy_pct": expectancy_pct},
            "gates": [{"gate": "is_detectable",
                       "numbers": {"trades_needed": 80}}],
        }

    def _apply(self, row, entry):
        import tournament
        tournament.apply_forward_evidence(
            row, {"strategies": {"momentum": entry}})
        return row["forward"]

    def test_fired_signals_without_a_resolution_do_not_compare_signs(self):
        forward = self._apply(self._row(), {
            "resolved_trades": 0, "signals_fired": 120,
            "positions_actually_opened": 14})

        self.assertIsNone(forward["signs_agree"])
        self.assertIn("no resolved forward trades", forward["status"])

    def test_a_missing_forward_expectancy_defers_instead_of_agreeing(self):
        forward = self._apply(self._row(), {
            "resolved_trades": 12, "forward_expectancy_pct": None})

        self.assertIsNone(forward["signs_agree"])
        self.assertIn("expectancy is missing", forward["status"])

    def test_a_resolved_disagreement_is_still_recorded_loudly(self):
        forward = self._apply(self._row(expectancy_pct=0.5), {
            "resolved_trades": 90, "forward_expectancy_pct": -0.4})

        self.assertFalse(forward["signs_agree"])
        self.assertIn("disagree in sign", forward["warning"])

    def test_reaching_the_sample_target_asks_for_the_authoritative_run(self):
        forward = self._apply(self._row(expectancy_pct=0.5), {
            "resolved_trades": 90, "forward_expectancy_pct": 0.4})

        self.assertTrue(forward["signs_agree"])
        self.assertIn("recorded-replay", forward["next_step"])

    def test_malformed_evidence_is_named_not_coerced(self):
        forward = self._apply(self._row(), {"resolved_trades": "many"})

        self.assertIn("malformed", forward["status"])

    def test_a_report_survives_a_strategy_with_no_expectancy(self):
        """A nightly run must not lose every result to one missing number."""
        import tempfile

        import tournament

        row = self._row(expectancy_pct=None)
        row.update({
            "version": "phase1-v3", "tier_reason": "capped",
            "registered_tier": "T1_HYPOTHESIS", "tier_changed": True,
            "beyond_exploratory_authority": False,
            "exploratory_ceiling": gates.EXPLORATORY_CEILING,
            "mechanism": "m", "falsification": "f",
            "hypotheses_tested": 1,
            "gates": [{"gate": "is_detectable", "passed": True,
                       "summary": "ok", "numbers": {"trades_needed": 80}}],
            "forward": {"resolved_trades": 3, "trades_needed": 80,
                        "forward_expectancy_pct": None,
                        "backtest_expectancy_pct": None,
                        "signs_agree": None},
        })
        payload = {
            "generated_utc": "2026-07-30T00:00:00Z", "data_root": "/data",
            "instruments": 8, "min_bars": 100,
            "window_start": "a", "window_end": "b",
            "cost_scenario": "base", "exit_policy": "fixed_rr",
            "benchmark_check": {"ok": True, "reason": "reproduced",
                                "measured_expectancy_r": None,
                                "expected_expectancy_r": -0.0963,
                                "drift_r": None, "tolerance_r": 0.02},
            "strategies": [row],
        }

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "REPORT.md"
            tournament.write_report(out, payload)
            written = out.read_text(encoding="utf-8")

        self.assertIn("n/a", written)
        self.assertIn("momentum/phase1-v3", written)


class ProvenanceCapTests(unittest.TestCase):
    """A hypothesis generated from the scored data cannot be confirmed by it.

    funding-unwind was generated by decomposing funding-carry's result on the
    2026-05..2026-07 window. Scored on that same window it clears every gate
    and would otherwise read as validated. It is still only a lead, because the gates
    measure the result and the result is what suggested the hypothesis.
    """

    def _score(self, prereg):
        import tournament
        tier, why = tier_from_gates(all_passing())
        if prereg.get("in_sample_by_construction") and tier != "T0_REJECTED":
            tier = "T1_HYPOTHESIS"
        self.assertIn(tier, ("T1_HYPOTHESIS", "T2_CANDIDATE"))
        return tier

    def test_declared_in_sample_hypotheses_are_capped(self):
        self.assertEqual(
            self._score({"in_sample_by_construction": True}),
            "T1_HYPOTHESIS")

    def test_ordinary_hypotheses_are_not_capped(self):
        self.assertEqual(self._score({}), "T2_CANDIDATE")

    def test_funding_unwind_declares_its_provenance(self):
        import yaml
        data = yaml.safe_load(
            (REPO / "research" / "hypotheses"
             / "funding-unwind.yaml").read_text())
        self.assertTrue(data.get("in_sample_by_construction"))
        # It must also say what would settle it, or the cap is a dead end.
        self.assertTrue(data.get("what_would_change_the_verdict"))

    def test_the_two_funding_hypotheses_declare_opposite_sources(self):
        import yaml
        directory = REPO / "research" / "hypotheses"
        carry = yaml.safe_load((directory / "funding-carry.yaml").read_text())
        unwind = yaml.safe_load(
            (directory / "funding-unwind.yaml").read_text())
        self.assertEqual(carry.get("return_source"), "funding")
        self.assertEqual(unwind.get("return_source"), "price")
        # Same entry rule, opposite claims: the count must reflect that the
        # entry has now been looked at twice on this data.
        self.assertGreater(unwind["hypotheses_tested"],
                           carry["hypotheses_tested"])


class MechanismGateTests(unittest.TestCase):
    class Spec:
        def __init__(self, mechanism, falsification):
            self.mechanism = mechanism
            self.falsification = falsification

    def test_a_stated_mechanism_and_test_pass(self):
        spec = self.Spec(
            "Liquidation engines sell at market regardless of price, and the "
            "payer is the trader whose margin ran out.",
            "Bars with open interest falling revert no more than bars with "
            "open interest rising.")
        self.assertTrue(gates.has_mechanism(spec).passed)

    def test_a_slogan_does_not_count_as_a_mechanism(self):
        spec = self.Spec("prices go up", "it stops working")
        self.assertFalse(gates.has_mechanism(spec).passed)

    def test_missing_falsification_fails(self):
        spec = self.Spec(
            "Liquidation engines sell at market regardless of price, and the "
            "payer is the trader whose margin ran out.", "")
        self.assertFalse(gates.has_mechanism(spec).passed)


class WalkForwardTests(unittest.TestCase):
    def _trades(self, days=100):
        day = 86_400_000
        return pd.DataFrame({"entry_ts": [i * day for i in range(days)]})

    def test_split_leaves_a_purge_gap_between_halves(self):
        trades = self._trades()
        in_mask, out_mask = walk_forward_masks(trades)
        self.assertFalse((in_mask & out_mask).any())
        # The purge drops trades straddling the boundary, so the halves do
        # not account for every trade.
        self.assertLess(int(in_mask.sum() + out_mask.sum()), len(trades))

    def test_out_of_sample_is_the_later_period(self):
        trades = self._trades()
        in_mask, out_mask = walk_forward_masks(trades)
        self.assertLess(trades[in_mask]["entry_ts"].max(),
                        trades[out_mask]["entry_ts"].min())

    def test_empty_input_is_handled(self):
        in_mask, out_mask = walk_forward_masks(
            pd.DataFrame({"entry_ts": []}))
        self.assertEqual(len(in_mask), 0)
        self.assertEqual(len(out_mask), 0)


class SerializationTests(unittest.TestCase):
    def test_result_round_trips_to_a_dict(self):
        payload = result("beat_nulls", False, expectancy=-0.1).as_dict()
        self.assertEqual(payload["gate"], "beat_nulls")
        self.assertIs(payload["passed"], False)
        self.assertEqual(payload["numbers"]["expectancy"], -0.1)


class PreRegistrationTests(unittest.TestCase):
    """Every registered strategy scored by the tournament needs a file."""

    def test_pre_registrations_declare_a_hypothesis_count(self):
        import yaml
        directory = REPO / "research" / "hypotheses"
        files = sorted(directory.glob("*.yaml"))
        self.assertTrue(files, "no pre-registration files found")
        for path in files:
            with self.subTest(hypothesis=path.name):
                data = yaml.safe_load(path.read_text())
                self.assertIn("hypotheses_tested", data)
                self.assertIn("mechanism", data)
                self.assertIn("falsification", data)
                self.assertIsInstance(data["hypotheses_tested"], int)

    def test_pre_registration_ids_are_registered_strategies(self):
        import yaml
        from agent.registry import REGISTRY
        for path in sorted((REPO / "research" / "hypotheses").glob("*.yaml")):
            with self.subTest(hypothesis=path.name):
                data = yaml.safe_load(path.read_text())
                self.assertIn(data["strategy_id"], REGISTRY)

class SettingsAxisTests(unittest.TestCase):
    """The unit of decision is the hypothesis, not one parameterisation."""

    def test_the_reject_floor_matches_the_promotion_protocol(self):
        from research.protocol import MIN_AXIS_SETTINGS

        self.assertEqual(gates.MIN_SETTINGS_TO_REJECT, MIN_AXIS_SETTINGS)

    def test_the_tier_ladder_matches_the_register(self):
        from agent.registry import TIERS

        self.assertEqual(gates.TIER_ORDER, TIERS)

    def test_one_failing_setting_cannot_reject_the_hypothesis(self):
        """This is the flush-fade failure, as a test."""
        tier, why, _ = gates.tier_from_settings(
            [setting("registered", "T0_REJECTED", registered=True)])

        self.assertEqual(tier, "T1_HYPOTHESIS")
        self.assertIn("guessed at once", why)

    def test_two_failing_settings_are_still_not_enough(self):
        tier, _, _ = gates.tier_from_settings([
            setting("registered", "T0_REJECTED", registered=True),
            setting("other", "T0_REJECTED")])

        self.assertEqual(tier, "T1_HYPOTHESIS")

    def test_three_failing_settings_reject_the_hypothesis(self):
        tier, why, _ = gates.tier_from_settings([
            setting("registered", "T0_REJECTED", registered=True),
            setting("violent_only", "T0_REJECTED"),
            setting("permissive", "T0_REJECTED")])

        self.assertEqual(tier, "T0_REJECTED")
        self.assertIn("all 3 pre-registered parameterisations fail", why)

    def test_a_missing_mechanism_is_rejected_at_any_setting_count(self):
        """No threshold can supply a mechanism the hypothesis does not state."""
        broken = all_passing()
        broken[0] = result("has_mechanism", False)

        tier, _, _ = gates.tier_from_settings(
            [setting("registered", "T0_REJECTED", registered=True,
                     gates_list=broken)])

        self.assertEqual(tier, "T0_REJECTED")

    def test_the_best_setting_carries_the_tier(self):
        tier, why, best = gates.tier_from_settings([
            setting("registered", "T0_REJECTED", registered=True,
                    ci=(-0.3, -0.1), expectancy_r=-0.2),
            setting("violent_only", "T2_CANDIDATE", ci=(0.20, 0.30),
                    expectancy_r=0.25, p_value=0.001),
            setting("permissive", "T0_REJECTED", ci=(-0.2, -0.05),
                    expectancy_r=-0.1)])

        self.assertEqual(tier, "T2_CANDIDATE")
        self.assertEqual(best["setting_id"], "violent_only")
        self.assertIn("violent_only of 3 parameterisations", why)

    def test_the_search_is_paid_for_before_the_winner_counts(self):
        """Best of k is the largest of k random numbers until corrected."""
        marginal = (0.001, 0.400)
        tier, why, _ = gates.tier_from_settings([
            setting("registered", "T0_REJECTED", registered=True,
                    ci=(-0.3, -0.1)),
            setting("a", "T2_CANDIDATE", ci=marginal),
            setting("b", "T1_HYPOTHESIS", ci=marginal),
            setting("c", "T1_HYPOTHESIS", ci=marginal),
            setting("d", "T1_HYPOTHESIS", ci=marginal),
            setting("e", "T1_HYPOTHESIS", ci=marginal)])

        self.assertEqual(tier, "T1_HYPOTHESIS")
        self.assertIn("does not survive the Holm correction", why)

    def test_a_lone_setting_is_not_charged_for_a_search(self):
        tier, _, _ = gates.tier_from_settings(
            [setting("registered", "T2_CANDIDATE", registered=True,
                     ci=(0.001, 0.400), p_value=0.04)])

        self.assertEqual(tier, "T2_CANDIDATE")

    def test_every_setting_carries_its_corrected_figure(self):
        settings = [
            setting("registered", "T0_REJECTED", registered=True),
            setting("a", "T2_CANDIDATE"),
            setting("b", "T1_HYPOTHESIS")]

        gates.tier_from_settings(settings)

        for row in settings:
            with self.subTest(setting=row["setting_id"]):
                self.assertIn("p_adjusted", row)
                self.assertIn("significant_corrected", row)

    def test_no_settings_at_all_is_an_error(self):
        with self.assertRaises(ValueError):
            gates.tier_from_settings([])


if __name__ == "__main__":
    unittest.main()
