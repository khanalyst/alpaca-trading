import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import hypotheses, shadow, variants
from agent.engine import Engine
from research import findings
from tests.helpers import valid_config
from tests.research.test_adaptive_proposals import (
    open_proposal, research_proposal)
from tests.research.test_enrichment_isolation import symbol_snapshot


THREE_DAYS = 3 * 86_400
# The configured floor. agent/config.py refuses anything below the
# 8-cluster arithmetic minimum, so config-derived assignments use this.
TEN_DAYS = 10 * 86_400


def static_variant(variant_id, path, value, strategy_id="momentum"):
    return variants.Variant(
        variant_id=variant_id,
        strategy_id=strategy_id,
        base_version="phase1-v3",
        overrides={path: value},
        hypothesis=f"The declared setting {variant_id} tests one exact axis.",
    )


def store_candidate(variant, order, *, source="static", proposal_id=None):
    axis, value = next(iter(variant.overrides.items()))
    code_identity = {"code": "test-code", "model": strategy_model(variant)}
    config_identity = {"variant": variant.variant_id, "value": value}
    candidate_key = findings._content_hash({
        "variant_id": variant.variant_id,
        "axis": axis,
        "setting": {axis: value},
        "code_identity": code_identity,
        "config_identity": config_identity,
    })
    return {
        "variant_id": variant.variant_id,
        "candidate_key": candidate_key,
        "axis": axis,
        "setting_id": variant.variant_id.rsplit(".", 1)[-1],
        "setting": {axis: value},
        "source": source,
        "proposal_id": proposal_id,
        "priority": 10,
        "order_key": f"{order:08d}:{variant.variant_id}",
        "code_identity": code_identity,
        "config_identity": config_identity,
    }


def strategy_model(variant):
    return f"{variant.strategy_id}.test-model"


class RotationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "findings.db"
        self.store = findings.FindingsStore(self.path)
        self.baseline = variants.baseline("momentum", "phase1-v3")
        self.first = static_variant(
            "momentum.rr.fixed_1_5", "strategy.fixed_reward_risk", 1.5)
        self.second = static_variant(
            "momentum.rr.fixed_2_5", "strategy.fixed_reward_risk", 2.5)
        self.third = static_variant(
            "momentum.stop.atr_1_5", "strategy.min_stop_atr_multiple", 1.5)
        for variant in (self.baseline, self.first, self.second, self.third):
            self.store.register(variant)

    def candidates(self):
        return [
            store_candidate(self.second, 2),
            store_candidate(self.first, 1),
            store_candidate(self.third, 3),
        ]

    def test_duration_and_observation_gates_are_both_required(self):
        assignment = self.store.ensure_experiment_assignment(
            "demo:gates", "momentum", self.baseline.variant_id,
            self.candidates(), minimum_duration_seconds=THREE_DAYS,
            minimum_observations=2, now=100.0)
        assignment = self.store.record_experiment_observations(
            assignment["assignment_id"], [
                {"observation_key": "one"},
                {"observation_key": "two"},
            ], now=200.0)

        too_early = self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=100.0 + THREE_DAYS - 1)
        self.assertEqual(too_early["status"], "OBSERVING")
        self.assertTrue(too_early["observations_satisfied"])
        self.assertFalse(too_early["duration_satisfied"])

        completed = self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=100.0 + THREE_DAYS)
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["observed_count"], 2)
        self.assertIsNotNone(completed["end_ts"])

        second = self.store.ensure_experiment_assignment(
            "demo:gates", "momentum", self.baseline.variant_id,
            self.candidates(), minimum_duration_seconds=THREE_DAYS,
            minimum_observations=2, now=400_000.0)
        time_only = self.store.maybe_complete_experiment_assignment(
            second["assignment_id"], now=400_000.0 + THREE_DAYS + 1)
        self.assertEqual(time_only["status"], "OBSERVING")
        self.assertTrue(time_only["duration_satisfied"])
        self.assertFalse(time_only["observations_satisfied"])

    def test_restart_resumes_same_assignment_and_observation_count(self):
        assignment = self.store.ensure_experiment_assignment(
            "demo:restart", "momentum", self.baseline.variant_id,
            self.candidates(), minimum_duration_seconds=THREE_DAYS,
            minimum_observations=3, now=10.0)
        self.store.record_experiment_observations(
            assignment["assignment_id"], [{"observation_key": "paired-1"}],
            now=20.0)

        reopened = findings.FindingsStore(self.path)
        resumed = reopened.ensure_experiment_assignment(
            "demo:restart", "momentum", self.baseline.variant_id,
            list(reversed(self.candidates())),
            minimum_duration_seconds=1.0, minimum_observations=1, now=30.0)

        self.assertEqual(resumed["assignment_id"], assignment["assignment_id"])
        self.assertEqual(resumed["candidate_variant_id"], self.first.variant_id)
        self.assertEqual(resumed["observed_count"], 1)
        self.assertEqual(resumed["minimum_duration_seconds"], THREE_DAYS)
        self.assertEqual(resumed["minimum_observations"], 3)
        report = reopened.experiment_rotation_report(
            "demo:restart", "momentum", now=30.0)
        self.assertEqual(report["schema"], "strategy_experiment_rotation.v1")
        self.assertEqual(
            report["active"][0]["assignment_id"], assignment["assignment_id"])

    def test_order_is_deterministic_and_terminal_exact_variants_do_not_rerun(self):
        first = self.store.ensure_experiment_assignment(
            "demo:order", "momentum", self.baseline.variant_id,
            self.candidates(), minimum_duration_seconds=0,
            minimum_observations=1, now=1.0)
        self.assertEqual(first["candidate_variant_id"], self.first.variant_id)
        self.store.record_experiment_observations(
            first["assignment_id"], [{"observation_key": "first"}], now=2.0)
        self.store.maybe_complete_experiment_assignment(
            first["assignment_id"], now=2.0)

        second = self.store.ensure_experiment_assignment(
            "demo:order", "momentum", self.baseline.variant_id,
            self.candidates(), minimum_duration_seconds=0,
            minimum_observations=1, now=3.0)
        self.assertEqual(second["candidate_variant_id"], self.second.variant_id)
        self.store.reject_experiment_assignment(
            second["assignment_id"], "declared setting is structurally invalid",
            now=4.0)

        third = self.store.ensure_experiment_assignment(
            "demo:order", "momentum", self.baseline.variant_id,
            self.candidates(), minimum_duration_seconds=0,
            minimum_observations=1, now=5.0)
        self.assertEqual(third["candidate_variant_id"], self.third.variant_id)
        history = self.store.experiment_assignments("demo:order", "momentum")
        self.assertEqual(
            [item["candidate_variant_id"] for item in history],
            [self.first.variant_id, self.second.variant_id,
             self.third.variant_id])

    def test_strategies_have_independent_active_assignments(self):
        other_baseline = variants.baseline("other", "v1")
        other_candidate = static_variant(
            "other.setting.one", "strategy.fixed_reward_risk", 1.5,
            strategy_id="other")
        self.store.register(other_baseline)
        self.store.register(other_candidate)
        momentum = self.store.ensure_experiment_assignment(
            "demo:parallel", "momentum", self.baseline.variant_id,
            self.candidates(), minimum_duration_seconds=THREE_DAYS,
            minimum_observations=2, now=1.0)
        other = self.store.ensure_experiment_assignment(
            "demo:parallel", "other", other_baseline.variant_id,
            [store_candidate(other_candidate, 1)],
            minimum_duration_seconds=THREE_DAYS,
            minimum_observations=2, now=1.0)

        self.assertNotEqual(momentum["assignment_id"], other["assignment_id"])
        self.assertEqual(
            self.store.active_experiment_assignment(
                "demo:parallel", "momentum")["candidate_variant_id"],
            self.first.variant_id)
        self.assertEqual(
            self.store.active_experiment_assignment(
                "demo:parallel", "other")["candidate_variant_id"],
            other_candidate.variant_id)

    def test_assignment_history_and_observations_are_immutable(self):
        assignment = self.store.ensure_experiment_assignment(
            "demo:immutable", "momentum", self.baseline.variant_id,
            self.candidates(), minimum_duration_seconds=THREE_DAYS,
            minimum_observations=2, now=1.0)
        self.store.record_experiment_observations(
            assignment["assignment_id"], [{"observation_key": "paired"}],
            now=2.0)
        with sqlite3.connect(self.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE strategy_experiment_assignments SET axis='changed' "
                    "WHERE assignment_id=?", (assignment["assignment_id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM strategy_experiment_observations "
                    "WHERE assignment_id=?", (assignment["assignment_id"],))


class RotationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "findings.db"
        self.store = findings.FindingsStore(self.path)
        self.cfg = valid_config()
        self.cfg["research"] = {
            "shadow_enabled": True,
            "shadow_variants": [
                "momentum.rr.fixed_2_5", "momentum.stop.atr_1_5"],
            "shadow_budget_ms": 0,
            "experiment_min_duration_days": 10,
            "experiment_min_observations": 1,
        }
        self.baseline = variants.baseline("momentum", "phase1-v3")
        self.first = static_variant(
            "momentum.rr.fixed_2_5", "strategy.fixed_reward_risk", 2.5)
        self.second = static_variant(
            "momentum.stop.atr_1_5", "strategy.min_stop_atr_multiple", 1.5)
        self.registry = {
            item.variant_id: item
            for item in (self.baseline, self.first, self.second)}

    def test_build_runs_only_baseline_and_one_selected_setting(self):
        evaluator = shadow.build(
            self.cfg, dict(self.registry), scope_key="demo:one-setting",
            store=self.store)

        self.assertEqual(
            evaluator.variant_ids,
            [self.baseline.variant_id, self.first.variant_id])
        assignment = self.store.active_experiment_assignment(
            "demo:one-setting", "momentum")
        self.assertEqual(assignment["candidate_variant_id"], self.first.variant_id)
        self.assertEqual(assignment["axis"], "strategy.fixed_reward_risk")
        self.assertNotIn(self.second.variant_id, evaluator.variant_ids)

    def test_missing_policy_keys_use_conservative_defaults(self):
        cfg = valid_config()
        cfg["research"] = {
            "shadow_enabled": True,
            "shadow_variants": [self.first.variant_id],
            "shadow_budget_ms": 0,
        }
        evaluator = shadow.build(
            cfg, dict(self.registry), scope_key="demo:default-policy",
            store=self.store)
        assignment = self.store.active_experiment_assignment(
            "demo:default-policy", "momentum")

        self.assertIsNotNone(evaluator)
        self.assertEqual(assignment["minimum_duration_seconds"], TEN_DAYS)
        self.assertEqual(assignment["minimum_observations"], 100)

    def test_adaptive_proposal_queues_then_observes_for_full_assignment(self):
        evaluator = shadow.build(
            self.cfg, dict(self.registry), scope_key="demo:adaptive-rotation",
            store=self.store)
        engine = Engine.__new__(Engine)
        engine.strategy_id = "momentum"
        engine.strategy_version = "phase1-v3"
        engine.run_id = "adaptive-rotation-run"
        engine.shadow = evaluator
        accepted = engine._record_research_proposals(
            [open_proposal(), research_proposal(1.3)])
        stored = next(item for item in accepted
                      if item["action"] == "research_proposal")
        current = self.store.active_experiment_assignment(
            "demo:adaptive-rotation", "momentum")
        self.store.record_experiment_observations(
            current["assignment_id"], [{"observation_key": "finish-static"}],
            now=current["started_ts"] + 1)
        self.store.maybe_complete_experiment_assignment(
            current["assignment_id"],
            now=current["started_ts"] + TEN_DAYS)

        now = current["started_ts"] + TEN_DAYS + 1
        records = evaluator.evaluate(
            {"BTC/USDT:USDT": symbol_snapshot(relative_volume_1h=1.25)},
            now=now, proposals=accepted, advance_accounts=False)

        active = self.store.active_experiment_assignment(
            "demo:adaptive-rotation", "momentum", now=now)
        proposal = self.store.hypothesis_proposal(stored["proposal_id"])
        self.assertEqual(active["candidate_variant_id"], stored["variant_id"])
        self.assertEqual(active["source"], "adaptive")
        self.assertEqual(proposal["current_status"], "OBSERVING")
        self.assertEqual(
            [event["status"] for event in self.store.proposal_events(
                stored["proposal_id"])], ["PROPOSED", "OBSERVING"])
        self.assertNotEqual(active["status"], "COMPLETED")
        self.assertIn(stored["variant_id"], {
            record.variant_id for record in records})


class RotationMigrationTests(unittest.TestCase):
    def test_schema_9_legacy_proposal_without_exact_variant_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema9-legacy.db"
            with patch.object(findings, "SCHEMA_VERSION", 8):
                self.assertEqual(
                    findings.FindingsStore(path).schema_version(), 8)
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "INSERT INTO hypothesis_proposals "
                    "(proposal_id, strategy_id, hypothesis_id, setting_id, "
                    "value_json, run_id, proposed_ts, observation_until_ts, "
                    "status) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("legacy-without-variant", "momentum", "volume-thrust",
                     "lower_participation", "1.3", "legacy-run", 10.0,
                     20.0, "PROPOSED"))
            with patch.object(findings, "SCHEMA_VERSION", 9):
                schema9 = findings.FindingsStore(path)
                self.assertIsNone(schema9.hypothesis_proposal(
                    "legacy-without-variant")["variant_id"])

            migrated = findings.FindingsStore(path)
            proposal = migrated.hypothesis_proposal(
                "legacy-without-variant")

            self.assertEqual(proposal["current_status"], "REJECTED")
            self.assertEqual(
                migrated.proposal_events("legacy-without-variant")[-1]
                ["detail"]["source"],
                "schema_migration_10")
            self.assertEqual(
                migrated.pending_hypothesis_proposals("momentum"), [])

    def test_schema_9_pending_adaptive_proposal_becomes_durable_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema9.db"
            baseline = variants.baseline("momentum", "phase1-v3")
            exact = variants.adaptive_hypothesis_variant(
                "momentum", "phase1-v3", "volume-thrust",
                "lower_participation", 1.3)
            metadata = hypotheses.numeric_setting_metadata(
                "volume-thrust", "lower_participation")
            with patch.object(findings, "SCHEMA_VERSION", 9):
                schema9 = findings.FindingsStore(path)
                schema9.register(baseline)
                proposal = schema9.propose_numeric_setting(
                    "momentum", "volume-thrust", "lower_participation", 1.3,
                    "schema9-run", minimum=metadata["minimum"],
                    maximum=metadata["maximum"],
                    target_parameter=metadata["target_parameter"],
                    variant=exact,
                    reasoning="Preserve this exact pending schema 9 proposal.",
                    now=10.0)

            migrated = findings.FindingsStore(path)
            self.assertEqual(
                migrated.schema_version(), findings.SCHEMA_VERSION)
            self.assertIn(
                "durable_strategy_experiment_rotation",
                [row["name"] for row in migrated.migration_history()])
            self.assertEqual(
                migrated.migration_history()[-1]["name"],
                "paper_execution_evidence_and_validity")
            self.assertEqual(
                migrated.pending_hypothesis_proposals("momentum")[0][
                    "proposal_id"], proposal["proposal_id"])

            cfg = valid_config()
            cfg["research"] = {
                "shadow_enabled": True,
                "shadow_variants": [baseline.variant_id],
                "shadow_budget_ms": 0,
                "experiment_min_duration_days": 10,
                "experiment_min_observations": 100,
            }
            evaluator = shadow.build(
                cfg, {baseline.variant_id: baseline},
                scope_key="demo:schema9", store=migrated)
            assignment = migrated.active_experiment_assignment(
                "demo:schema9", "momentum")

            self.assertEqual(assignment["candidate_variant_id"], exact.variant_id)
            self.assertEqual(assignment["proposal_id"], proposal["proposal_id"])
            self.assertEqual(assignment["minimum_duration_seconds"], TEN_DAYS)
            self.assertEqual(assignment["minimum_observations"], 100)
            self.assertEqual(
                evaluator.variant_ids, [baseline.variant_id, exact.variant_id])


if __name__ == "__main__":
    unittest.main()
