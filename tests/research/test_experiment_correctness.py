import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent import shadow, variants
from research import findings
from tests.helpers import valid_config


MODEL_ID = "test.forward.model.v1"


def candidate_descriptor(candidate):
    axis, value = next(iter(candidate.overrides.items()))
    return {
        "variant_id": candidate.variant_id,
        "candidate_key": findings._content_hash({
            "variant_id": candidate.variant_id,
            "axis": axis,
            "setting": {axis: value},
        }),
        "axis": axis,
        "setting_id": candidate.variant_id.rsplit(".", 1)[-1],
        "setting": {axis: value},
        "source": "static",
        "priority": 10,
        "order_key": candidate.variant_id,
        "code_identity": {
            "baseline": {"forward_model_id": MODEL_ID},
            "candidate": {"forward_model_id": MODEL_ID},
        },
        "config_identity": {
            "baseline": {"config": "baseline"},
            "candidate": {"config": "candidate"},
        },
    }


class ExperimentCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "findings.db"
        self.store = findings.FindingsStore(self.path)
        self.baseline = variants.baseline("momentum", "phase1-v3")
        self.candidate = variants.Variant(
            variant_id="momentum.rr.correctness_2_5",
            strategy_id="momentum",
            base_version="phase1-v3",
            overrides={"strategy.fixed_reward_risk": 2.5},
            hypothesis="Test one exact reward/risk setting.",
        )
        self.store.register(self.baseline)
        self.store.register(self.candidate)
        self.descriptor = candidate_descriptor(self.candidate)

    def assignment(self, scope, *, observations=1, now=1.0):
        return self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id,
            [self.descriptor], minimum_duration_seconds=0,
            minimum_observations=observations, now=now)

    def _seed_open_pair(self, assignment, now=2.0):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for lane, variant_id in (
                    ("baseline", assignment["baseline_variant_id"]),
                    ("candidate", assignment["candidate_variant_id"])):
                trade_id = f"{assignment['assignment_id']}:{lane}:trade"
                proposal_id = f"{assignment['assignment_id']}:proposal"
                assumptions = json.dumps({
                    "experiment_provenance": {"lane": lane},
                    "cost_components": {},
                }, sort_keys=True)
                conn.execute("""
                    INSERT INTO paper_trades (
                        trade_id, proposal_id, scope_key, variant_id, cycle_id,
                        symbol, direction, setup_type, signal_ts, model_id,
                        assumptions_json, entry_ts, entry_price, notional,
                        risk_usd, stop_price, take_price, status, failure)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',NULL)
                """, (
                    trade_id, proposal_id, assignment["scope_key"], variant_id,
                    "cycle-open", "BTC/USDT:USDT", "long", "test", now,
                    MODEL_ID, assumptions, now, 100.0, 1_000.0, 10.0,
                    95.0, 110.0,
                ))
                conn.execute("""
                    INSERT INTO paper_decisions (
                        decision_id, proposal_id, scope_key, variant_id,
                        cycle_id, symbol, direction, setup_type, signal_ts,
                        confidence, decision_outcome, reason, paper_trade_id,
                        model_id, assumptions_json, proposal_json, decision_ts)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'PROPOSED',NULL,?,?,?,?,?)
                """, (
                    f"{trade_id}:decision", proposal_id,
                    assignment["scope_key"], variant_id, "cycle-open",
                    "BTC/USDT:USDT", "long", "test", now, 0.8, trade_id,
                    MODEL_ID, assumptions, "{}", now,
                ))
        return [
            f"{assignment['assignment_id']}:{lane}:trade"
            for lane in ("baseline", "candidate")]

    def _complete_below_floor(self, assignment, *, now):
        trade_ids = self._seed_open_pair(assignment, now=now)
        self.store.record_experiment_observations(
            assignment["assignment_id"],
            [{"observation_key": f"paired-{assignment['assignment_id']}"}],
            now=now + 0.1)
        draining = self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=now + 0.2)
        self.assertEqual(draining["status"], "DRAINING")
        for trade_id in trade_ids:
            self.store.close_paper_trade(
                trade_id, exit_ts=now + 0.3, exit_price=101.0,
                result="timeout", net_pnl_usd=10.0, r_multiple=1.0)
        completed = self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=now + 0.4)
        outcome = self.store.experiment_outcome(assignment["assignment_id"])
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertIn(
            "CLOSED_SAMPLE_BELOW_FLOOR",
            {reason["code"] for reason in outcome["payload"]["reasons"]})
        return outcome

    def test_assignment_rebases_both_accounts_at_one_fresh_boundary(self):
        scope = "demo:fresh-boundary"
        for index, variant_id in enumerate(
                (self.baseline.variant_id, self.candidate.variant_id), start=1):
            state, version = self.store.load_paper_portfolio(
                scope, variant_id, initial_balance=10_000.0, now=0.0)
            state.update({
                "cash_usdt": 8_000.0 + index,
                "equity_usdt": 8_000.0 + index,
                "loss_count": index,
                "cooldowns": {"BTC/USDT:USDT": 999.0},
                "seen_proposals": {f"old-{index}": 0.0},
            })
            self.store.save_paper_portfolio(
                scope, variant_id, state, version, now=0.5)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("""
                INSERT INTO paper_decisions (
                    decision_id, proposal_id, scope_key, variant_id, cycle_id,
                    symbol, direction, setup_type, signal_ts, confidence,
                    decision_outcome, reason, paper_trade_id, model_id,
                    assumptions_json, proposal_json, decision_ts)
                VALUES (?,?,?,?,?,?,?,?,?,NULL,'VETOED',?,NULL,?,?,?,?)
            """, (
                "historical-decision", "historical-proposal", scope,
                self.baseline.variant_id, "old-cycle", "BTC/USDT:USDT",
                "long", "test", 0.25, "historical veto", MODEL_ID,
                "{}", "{}", 0.25,
            ))

        assignment = self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id, [self.descriptor],
            minimum_duration_seconds=0, minimum_observations=1,
            initial_balance_usdt=12_345.0, now=10.0)
        baseline = self.store.paper_portfolio_state(
            scope, self.baseline.variant_id)
        candidate = self.store.paper_portfolio_state(
            scope, self.candidate.variant_id)

        for state in (baseline, candidate):
            self.assertEqual(state["cash_usdt"], 12_345.0)
            self.assertEqual(state["equity_usdt"], 12_345.0)
            self.assertEqual(state["positions"], [])
            self.assertEqual(state["cooldowns"], {})
            self.assertEqual(state["seen_proposals"], {})
            self.assertEqual(state["loss_count"], 0)
            self.assertEqual(
                state["experiment_assignment_id"], assignment["assignment_id"])
            self.assertEqual(state["experiment_assignment_started_ts"], 10.0)
        comparable_keys = set(baseline) - {"portfolio_version", "updated_ts"}
        self.assertEqual(
            {key: baseline[key] for key in comparable_keys},
            {key: candidate[key] for key in comparable_keys})
        self.assertEqual(
            self.store.paper_decisions_for(scope, self.baseline.variant_id)[0]
            ["decision_id"], "historical-decision")

    def test_draining_waits_for_late_resolutions_before_immutable_outcome(self):
        assignment = self.assignment("demo:draining")
        trade_ids = self._seed_open_pair(assignment)
        self.store.record_experiment_observations(
            assignment["assignment_id"],
            [{"observation_key": "paired-open"}], now=2.0)

        draining = self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=3.0)

        self.assertEqual(draining["status"], "DRAINING")
        self.assertEqual(draining["unresolved_actions"]["total"], 2)
        self.assertIsNotNone(self.store.active_experiment_assignment(
            assignment["scope_key"], "momentum", now=3.0))
        with self.assertRaisesRegex(ValueError, "draining"):
            self.store.record_experiment_observations(
                assignment["assignment_id"],
                [{"observation_key": "too-late"}], now=3.1)
        self.assertIsNone(self.store.experiment_outcome(
            assignment["assignment_id"]))

        for trade_id in trade_ids:
            self.store.close_paper_trade(
                trade_id, exit_ts=4.0, exit_price=101.0, result="timeout",
                net_pnl_usd=10.0, r_multiple=1.0)
        completed = self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=5.0)
        outcome = self.store.experiment_outcome(assignment["assignment_id"])

        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["unresolved_actions"]["total"], 0)
        self.assertEqual(outcome["payload"]["baseline"]["closes"], 1)
        self.assertEqual(outcome["payload"]["candidate"]["closes"], 1)
        self.assertEqual(outcome["payload"]["candidate"]["unresolved_opens"], 0)
        self.assertEqual(
            [event["status"] for event in
             self.store.experiment_assignment_history(
                 assignment["assignment_id"])],
            ["STARTED", "OBSERVING", "DRAINING", "COMPLETED"])

    def test_explicit_selection_can_retry_latest_inconclusive_attempt(self):
        scope = "demo:retry"
        first = self.assignment(scope)
        self.store.record_experiment_observations(
            first["assignment_id"], [{"observation_key": "no-actions"}],
            now=2.0)
        self.store.maybe_complete_experiment_assignment(
            first["assignment_id"], now=3.0)
        first_outcome = self.store.experiment_outcome(first["assignment_id"])
        self.assertEqual(first_outcome["verdict"], "INCONCLUSIVE")
        self.assertIsNone(self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id, [self.descriptor],
            minimum_duration_seconds=0, minimum_observations=1, now=4.0))

        selection = self.store.record_research_selection(
            {
                "strategy_id": "momentum",
                "variant_id": self.candidate.variant_id,
                "reasoning": "Explicitly retry the inadequate prior sample.",
            }, [self.descriptor], scope_key=scope, run_id="retry-run",
            cycle_id="retry-cycle", model_id="test-model",
            prompt_version="test-prompt", now=5.0)
        prioritized = self.store.prioritized_experiment_candidates(
            scope, "momentum", [self.descriptor], now=5.1)
        second = self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id, prioritized,
            minimum_duration_seconds=0, minimum_observations=1, now=6.0)

        self.assertEqual(selection["current_status"], "ACCEPTED")
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["retry_of_assignment_id"], first["assignment_id"])
        self.assertNotEqual(second["assignment_id"], first["assignment_id"])
        self.assertEqual(
            self.store.experiment_outcome(first["assignment_id"])
            ["payload_hash"], first_outcome["payload_hash"])
        self.assertEqual(
            self.store.research_selection(selection["selection_id"])
            ["current_status"], "ASSIGNED")

    def test_below_floor_outcome_is_automatically_nominated_and_propagated(self):
        scope = "demo:auto-retry"
        first = self.assignment(scope)
        first_outcome = self._complete_below_floor(first, now=2.0)
        other = variants.Variant(
            variant_id="momentum.rr.auto_retry_other",
            strategy_id="momentum",
            base_version="phase1-v3",
            overrides={"strategy.fixed_reward_risk": 1.5},
            hypothesis="Untested static setting used to verify retry priority.",
        )
        self.store.register(other)
        other_descriptor = candidate_descriptor(other)
        automatically_prioritized = self.store.prioritized_experiment_candidates(
            scope, "momentum", [other_descriptor, self.descriptor], now=2.9)
        ordered = sorted(automatically_prioritized, key=lambda item: (
            item["priority"], item["order_key"], item["variant_id"]))
        self.assertEqual(ordered[0]["variant_id"], self.candidate.variant_id)
        self.assertEqual(
            ordered[0]["retry_of_assignment_id"], first["assignment_id"])
        selection = self.store.record_research_selection(
            {
                "strategy_id": "momentum",
                "reasoning": "Continue the latest closed under-floor sample.",
            }, [other_descriptor, self.descriptor],
            scope_key=scope, run_id="auto-run",
            cycle_id="auto-cycle", model_id="test-model",
            prompt_version="test-prompt", now=3.0)
        event = self.store.research_selection_events(
            selection["selection_id"])[-1]
        prioritized = self.store.prioritized_experiment_candidates(
            scope, "momentum", [other_descriptor, self.descriptor], now=3.1)
        second = self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id, prioritized,
            minimum_duration_seconds=0, minimum_observations=1, now=4.0)

        self.assertEqual(first_outcome["verdict"], "INCONCLUSIVE")
        self.assertEqual(selection["resolved_variant_id"],
                         self.candidate.variant_id)
        self.assertEqual(
            event["detail"]["retry_of_assignment_id"],
            first["assignment_id"])
        retry_candidate = next(
            item for item in prioritized
            if item["variant_id"] == self.candidate.variant_id)
        self.assertEqual(
            retry_candidate["retry_of_assignment_id"], first["assignment_id"])
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["retry_of_assignment_id"], first["assignment_id"])

    def test_automatic_retry_caps_at_three_but_explicit_retry_continues(self):
        scope = "demo:auto-retry-cap"
        current = self.assignment(scope)
        for expected_attempt, now in ((1, 2.0), (2, 4.0), (3, 6.0)):
            self.assertEqual(current["attempt"], expected_attempt)
            self._complete_below_floor(current, now=now)
            prioritized = self.store.prioritized_experiment_candidates(
                scope, "momentum", [self.descriptor], now=now + 0.5)
            if expected_attempt < findings.MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS:
                self.assertEqual(
                    prioritized[0]["retry_of_assignment_id"],
                    current["assignment_id"])
                current = self.store.ensure_experiment_assignment(
                    scope, "momentum", self.baseline.variant_id, prioritized,
                    minimum_duration_seconds=0, minimum_observations=1,
                    now=now + 1.0)
            else:
                self.assertNotIn("retry_of_assignment_id", prioritized[0])
                self.assertIsNone(self.store.ensure_experiment_assignment(
                    scope, "momentum", self.baseline.variant_id, prioritized,
                    minimum_duration_seconds=0, minimum_observations=1,
                    now=now + 1.0))
        selection = self.store.record_research_selection(
            {
                "strategy_id": "momentum",
                "variant_id": self.candidate.variant_id,
                "reasoning": "Explicitly continue the latest under-floor sample.",
            }, [self.descriptor], scope_key=scope, run_id="manual-fourth",
            cycle_id="manual-fourth", model_id="test-model",
            prompt_version="test-prompt", now=8.0)
        explicit = self.store.prioritized_experiment_candidates(
            scope, "momentum", [self.descriptor], now=8.1)
        fourth = self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id, explicit,
            minimum_duration_seconds=0, minimum_observations=1, now=9.0)
        self.assertEqual(selection["current_status"], "ACCEPTED")
        self.assertEqual(fourth["attempt"], 4)
        self.assertEqual(
            fourth["retry_of_assignment_id"], current["assignment_id"])

    def test_non_retryable_terminal_outcome_is_excluded(self):
        scope = "demo:no-auto-retry"
        first = self.assignment(scope)
        self.store.reject_experiment_assignment(
            first["assignment_id"], "structurally invalid setting", now=2.0)
        other = variants.Variant(
            variant_id="momentum.rr.correctness_1_5",
            strategy_id="momentum",
            base_version="phase1-v3",
            overrides={"strategy.fixed_reward_risk": 1.5},
            hypothesis="Test a second exact reward/risk setting.",
        )
        self.store.register(other)
        other_descriptor = candidate_descriptor(other)
        prioritized = self.store.prioritized_experiment_candidates(
            scope, "momentum", [self.descriptor, other_descriptor], now=3.0)
        second = self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id, prioritized,
            minimum_duration_seconds=0, minimum_observations=1, now=4.0)

        first_descriptor = next(
            item for item in prioritized
            if item["variant_id"] == self.candidate.variant_id)
        self.assertNotIn("retry_of_assignment_id", first_descriptor)
        self.assertEqual(second["candidate_variant_id"], other.variant_id)
        self.assertEqual(second["attempt"], 1)

    def test_shadow_does_not_evaluate_new_opens_while_draining(self):
        scope = "demo:shadow-draining"
        evaluator = shadow.ShadowEvaluator(
            [], valid_config(), store=self.store, scope_key=scope,
            rotation_baseline=self.baseline,
            rotation_candidates=[{
                "variant": self.candidate,
                "source": "static", "priority": 10,
                "order_key": self.candidate.variant_id,
            }],
            rotation_min_duration_seconds=0,
            rotation_min_observations=1)
        assignment = self.store.active_experiment_assignment(
            scope, "momentum")
        started = assignment["started_ts"]
        self._seed_open_pair(assignment, now=started + 1.0)
        self.store.record_experiment_observations(
            assignment["assignment_id"], [{"observation_key": "paired"}],
            now=started + 1.0)
        draining = self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=started + 2.0)
        evaluator._rotation_assignment = draining
        before = sum(len(self.store.paper_decisions_for(scope, variant_id))
                     for variant_id in evaluator.variant_ids)

        evaluator.evaluate(
            {}, now=started + 3.0, cycle_id="drain-cycle",
            proposals=[{
                "action": "open", "symbol": "BTC/USDT:USDT",
                "direction": "long", "setup_type": "test",
                "signal_ts": 4_000,
            }], advance_accounts=False)

        after = sum(len(self.store.paper_decisions_for(scope, variant_id))
                    for variant_id in evaluator.variant_ids)
        self.assertEqual(before, after)
        self.assertEqual(
            self.store.active_experiment_assignment(
                scope, "momentum", now=started + 3.0)["status"], "DRAINING")


if __name__ == "__main__":
    unittest.main()
