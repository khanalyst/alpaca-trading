import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from agent import brain, shadow, variants
from agent.engine import Engine
from research import findings
from tests.helpers import valid_config


THREE_DAYS = 3 * 86_400


def selector_config():
    cfg = valid_config()
    cfg["research"] = {
        "shadow_enabled": True,
        "shadow_variants": ["*"],
        "shadow_budget_ms": 0,
        "shadow_workers": 1,
        "paper_initial_balance_usdt": 10_000,
        "paper_max_failures": 3,
        "paper_min_closed_trades": 1,
        "experiment_min_duration_days": 3,
        "experiment_min_observations": 1,
        "forward_feed_version": 1,
    }
    return cfg


def selector_registry():
    return variants.load_registry(
        Path(__file__).resolve().parents[2] / "research" / "variants.yaml")


def parsed_selection(strategy_id, variant_id=None, reasoning=None, **extra):
    selection = {
        "strategy_id": strategy_id,
        "reasoning": reasoning or (
            "Prioritize this registered setting for the next research window."),
        **extra,
    }
    if variant_id is not None:
        selection["variant_id"] = variant_id
    parsed = brain.parse_decisions(json.dumps({
        "decisions": [], "research_selection": selection,
    }))
    return parsed[-1]


class ResearchSelectorPromptAndParserTests(unittest.TestCase):
    def test_prompt_enumerates_only_single_axis_nonbaseline_identifiers(self):
        system = brain.build_system(selector_config())
        catalog = variants.research_selection_catalog()

        self.assertIn("research_selection", system)
        self.assertIn("cannot switch the live strategy", system)
        self.assertIn("cannot place, close or modify an order", system)
        for strategy_id, settings in catalog.items():
            self.assertIn(f"- {strategy_id}:", system)
            for setting in settings:
                self.assertIn(setting["variant_id"], system)
        self.assertNotIn("momentum.baseline", system)
        self.assertNotIn("flush_fade.prereg.violent_only", system)
        self.assertNotIn("trend_multiday.prereg.stronger_trend", system)

    def test_parser_accepts_strategy_only_and_exact_registered_setting(self):
        strategy_only = parsed_selection("scalp-maker")
        exact = parsed_selection(
            "scalp-maker", "scalp_maker.prereg.tighter_spread")

        self.assertEqual(strategy_only["validation_status"], "ACCEPTED")
        self.assertEqual(strategy_only["variant_id"], "")
        self.assertEqual(exact["validation_status"], "ACCEPTED")
        self.assertEqual(
            exact["variant_id"], "scalp_maker.prereg.tighter_spread")

    def test_parser_rejects_unknown_baseline_multiaxis_and_live_changes(self):
        cases = (
            parsed_selection("not-registered"),
            parsed_selection("momentum", "momentum.baseline"),
            parsed_selection("momentum", "momentum.rr.fixed_3_0"),
            parsed_selection("flush-fade", "flush_fade.prereg.violent_only"),
            parsed_selection("scalp-maker", reasoning="short"),
            parsed_selection(
                "scalp-maker", leverage=5,
                reasoning="Test the tighter spread setting in research."),
            parsed_selection(
                "scalp-maker",
                reasoning="Place a live order after selecting this setting."),
        )

        for selection in cases:
            with self.subTest(reason=selection.get("rejection_reason")):
                self.assertEqual(selection["validation_status"], "REJECTED")
                self.assertTrue(selection["rejection_reason"])

    def test_rejected_selection_does_not_remove_or_mutate_trade_decisions(self):
        raw_open = {
            "action": "open", "symbol": "BTC/USDT:USDT",
            "direction": "long", "setup_type": "trend_continuation",
            "invalidation_anchor": "structure", "exit_policy": "fixed_rr",
            "execution_choice": "normal", "confidence": 0.8,
            "reasoning": "The recorded momentum setup remains aligned.",
        }
        parsed = brain.parse_decisions(json.dumps({
            "decisions": [raw_open],
            "research_selection": {
                "strategy_id": "momentum",
                "variant_id": "momentum.baseline",
                "reasoning": "Try to select the baseline as an experiment.",
            },
        }))

        self.assertEqual(parsed[0]["action"], "open")
        self.assertEqual(parsed[0]["symbol"], raw_open["symbol"])
        self.assertEqual(parsed[-1]["validation_status"], "REJECTED")

    def test_exact_numeric_adaptive_proposal_schema_is_preserved(self):
        parsed = brain.parse_decisions(json.dumps({
            "decisions": [],
            "proposal": {
                "hypothesis_id": "volume-thrust",
                "setting_id": "lower_participation",
                "value": 1.3,
                "reasoning": "Test this exact registered participation point.",
            },
            "research_selection": {
                "strategy_id": "scalp-maker",
                "reasoning": "Resolve its next registered single-axis setting.",
            },
        }))

        self.assertEqual(
            [item["action"] for item in parsed],
            ["research_proposal", "research_selection"])
        self.assertEqual(parsed[0]["value"], 1.3)
        self.assertEqual(parsed[1]["validation_status"], "ACCEPTED")


class ResearchSelectorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "findings.db"
        self.store = findings.FindingsStore(self.path)
        self.cfg = selector_config()
        self.scope = "demo:selector"
        self.coordinator = shadow.build(
            self.cfg, selector_registry(), scope_key=self.scope,
            store=self.store)
        self.attribution = {
            "run_id": "run-selector",
            "cycle_id": "cycle-selector",
            "model_id": "test-model",
            "prompt_version": "prompt-selector",
        }

    def test_strategy_only_resolution_is_exact_attributed_and_reopenable(self):
        active = self.store.active_experiment_assignment(
            self.coordinator.scope_key, "scalp-maker")
        self.assertEqual(
            active["candidate_variant_id"],
            "scalp_maker.prereg.stronger_imbalance")

        stored = self.coordinator.record_research_selection(
            parsed_selection("scalp-maker"), self.attribution)
        reopened = findings.FindingsStore(self.path).research_selection(
            stored["selection_id"])

        self.assertEqual(stored["current_status"], "ACCEPTED")
        self.assertEqual(
            stored["resolved_variant_id"],
            "scalp_maker.prereg.tighter_spread")
        self.assertEqual(reopened["run_id"], "run-selector")
        self.assertEqual(reopened["cycle_id"], "cycle-selector")
        self.assertEqual(reopened["model_id"], "test-model")
        self.assertEqual(reopened["prompt_version"], "prompt-selector")
        self.assertEqual(
            reopened["original_reasoning"],
            parsed_selection("scalp-maker")["reasoning"])

    def test_exact_selection_waits_then_assigns_survives_restart_and_tests(self):
        strategy_id = "momentum"
        selected_variant = "momentum.stop.atr_1_5"
        evaluator = self.coordinator.evaluators[strategy_id]
        current = self.store.active_experiment_assignment(
            self.coordinator.scope_key, strategy_id)
        other_before = self.store.active_experiment_assignment(
            self.coordinator.scope_key, "scalp-maker")["assignment_id"]

        selected = self.coordinator.record_research_selection(
            parsed_selection(strategy_id, selected_variant), self.attribution)
        evaluator._refresh_rotation(current["started_ts"] + 1)
        still_current = self.store.active_experiment_assignment(
            self.coordinator.scope_key, strategy_id)

        self.assertEqual(still_current["assignment_id"], current["assignment_id"])
        self.assertNotEqual(still_current["candidate_variant_id"], selected_variant)
        self.assertEqual(selected["current_status"], "ACCEPTED")
        self.assertEqual(
            self.store.active_experiment_assignment(
                self.coordinator.scope_key, "scalp-maker")["assignment_id"],
            other_before)

        self.store.record_experiment_observations(
            current["assignment_id"], [{"observation_key": "finish-current"}],
            now=current["started_ts"] + 1)
        self.store.maybe_complete_experiment_assignment(
            current["assignment_id"],
            now=current["started_ts"] + THREE_DAYS)
        evaluator._refresh_rotation(current["started_ts"] + THREE_DAYS + 1)
        assigned = self.store.active_experiment_assignment(
            self.coordinator.scope_key, strategy_id)
        linked = self.store.research_selection(selected["selection_id"])

        self.assertEqual(assigned["candidate_variant_id"], selected_variant)
        self.assertEqual(linked["current_status"], "ASSIGNED")
        self.assertEqual(linked["assignment_id"], assigned["assignment_id"])

        restarted = shadow.build(
            self.cfg, selector_registry(), scope_key=self.scope,
            store=findings.FindingsStore(self.path))
        resumed = restarted.store.active_experiment_assignment(
            restarted.scope_key, strategy_id)
        self.assertEqual(resumed["assignment_id"], assigned["assignment_id"])
        self.assertEqual(resumed["candidate_variant_id"], selected_variant)

        restarted.store.record_experiment_observations(
            assigned["assignment_id"], [{"observation_key": "finish-selected"}],
            now=assigned["started_ts"] + 1)
        restarted.store.maybe_complete_experiment_assignment(
            assigned["assignment_id"],
            now=assigned["started_ts"] + THREE_DAYS)
        tested = restarted.store.research_selection(selected["selection_id"])
        self.assertEqual(tested["current_status"], "TESTED")
        self.assertEqual(
            [event["status"] for event in
             restarted.store.research_selection_events(
                 selected["selection_id"])],
            ["ACCEPTED", "ASSIGNED", "TESTED"])

        duplicate = restarted.record_research_selection(
            parsed_selection(strategy_id, selected_variant),
            {**self.attribution, "cycle_id": "cycle-terminal-retry"})
        self.assertEqual(duplicate["current_status"], "ACCEPTED")
        self.assertEqual(
            self.store.research_selection_events(duplicate["selection_id"])[0]
            ["detail"]["retry_of_assignment_id"], assigned["assignment_id"])

    def test_parser_rejection_is_persisted_append_only(self):
        rejected = self.coordinator.record_research_selection(
            parsed_selection("momentum", "momentum.baseline"),
            self.attribution)

        self.assertEqual(rejected["current_status"], "REJECTED")
        self.assertEqual(rejected["requested_variant_id"], "momentum.baseline")
        self.assertIn("baseline", rejected["status_reason"])
        with sqlite3.connect(self.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE research_selections SET run_id='rewritten' "
                    "WHERE selection_id=?", (rejected["selection_id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM research_selection_events "
                    "WHERE selection_id=?", (rejected["selection_id"],))

    def test_rejected_exact_assignment_cannot_be_selected_for_retest(self):
        strategy_id = "ls-ratio-fade"
        selected_variant = "ls_ratio_fade.prereg.lower_short_tail"
        evaluator = self.coordinator.evaluators[strategy_id]
        current = self.store.active_experiment_assignment(
            self.coordinator.scope_key, strategy_id)
        selected = self.coordinator.record_research_selection(
            parsed_selection(strategy_id, selected_variant), self.attribution)
        self.store.record_experiment_observations(
            current["assignment_id"], [{"observation_key": "finish-first"}],
            now=current["started_ts"] + 1)
        self.store.maybe_complete_experiment_assignment(
            current["assignment_id"],
            now=current["started_ts"] + THREE_DAYS)
        evaluator._refresh_rotation(current["started_ts"] + THREE_DAYS + 1)
        assigned = self.store.active_experiment_assignment(
            self.coordinator.scope_key, strategy_id)
        self.assertEqual(assigned["candidate_variant_id"], selected_variant)

        self.store.reject_experiment_assignment(
            assigned["assignment_id"], "synthetic terminal rejection",
            now=assigned["started_ts"] + 1)
        terminal = self.store.research_selection(selected["selection_id"])
        retry = self.coordinator.record_research_selection(
            parsed_selection(strategy_id, selected_variant),
            {**self.attribution, "cycle_id": "cycle-rejected-retry"})

        self.assertEqual(terminal["current_status"], "REJECTED")
        self.assertEqual(retry["current_status"], "REJECTED")
        self.assertIn("silent retest refused", retry["status_reason"])

    def test_engine_strips_selection_before_any_trading_path(self):
        engine = Engine.__new__(Engine)
        engine.cfg = self.cfg
        engine.strategy_id = "momentum"
        engine.strategy_version = "phase1-v3"
        engine.run_id = "run-engine-selector"
        engine.prompt_version = "prompt-engine-selector"
        engine.shadow = self.coordinator
        live_strategy_before = deepcopy(engine.cfg["strategy"])
        open_decision = {
            "action": "open", "symbol": "BTC/USDT:USDT",
            "direction": "long", "setup_type": "trend_continuation",
            "confidence": 0.8, "invalidation_anchor": "structure",
            "exit_policy": "fixed_rr", "execution_choice": "normal",
            "reasoning": "The live momentum setup remains aligned.",
        }

        with patch("agent.engine.state.journal_context", return_value={
                "cycle_id": "cycle-engine-selector"}):
            trading = engine._record_research_proposals([
                open_decision,
                parsed_selection(
                    "ls-ratio-fade",
                    "ls_ratio_fade.prereg.lower_short_tail"),
            ])

        self.assertEqual(trading, [open_decision])
        self.assertEqual(engine.cfg["strategy"], live_strategy_before)
        self.assertEqual(engine.cfg["strategy"]["id"], "momentum")
        stored = self.store.research_selections(
            self.coordinator.scope_key, "ls-ratio-fade")[-1]
        self.assertEqual(stored["current_status"], "ACCEPTED")
        self.assertEqual(stored["model_id"], self.cfg["llm"]["model"])

    def test_coordinator_outage_persists_an_explicit_rejection(self):
        fallback_path = Path(self.tmp.name) / "fallback-findings.db"
        cfg = selector_config()
        cfg["research"]["findings_store"] = str(fallback_path)
        engine = Engine.__new__(Engine)
        engine.cfg = cfg
        engine.strategy_id = "momentum"
        engine.strategy_version = "phase1-v3"
        engine.run_id = "run-no-coordinator"
        engine.prompt_version = "prompt-no-coordinator"
        engine.shadow = None

        with patch("agent.engine.state.journal_context", return_value={
                "cycle_id": "cycle-no-coordinator",
                "runtime_mode": "demo",
                "account_fingerprint": "test-account",
        }):
            trading = engine._record_research_proposals([
                parsed_selection("scalp-maker")])

        stored = findings.FindingsStore(fallback_path).research_selections()
        self.assertEqual(trading, [])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["current_status"], "REJECTED")
        self.assertIn("unavailable", stored[0]["status_reason"])


class ResearchSelectorMigrationTests(unittest.TestCase):
    def test_schema_10_migrates_without_rewriting_rotation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema10.db"
            baseline = variants.baseline("momentum", "phase1-v3")
            candidate = variants.load_registry(
                Path(__file__).resolve().parents[2]
                / "research" / "variants.yaml")["momentum.rr.fixed_1_5"]
            descriptor = {
                "variant_id": candidate.variant_id,
                "candidate_key": "schema10-candidate-key",
                "axis": "strategy.fixed_reward_risk",
                "setting_id": "fixed_1_5",
                "setting": {"strategy.fixed_reward_risk": 1.5},
                "source": "static",
                "priority": 10,
                "order_key": candidate.variant_id,
                "code_identity": {"schema": 10},
                "config_identity": {"schema": 10},
            }
            with patch.object(findings, "SCHEMA_VERSION", 10):
                schema10 = findings.FindingsStore(path)
                self.assertEqual(schema10.schema_version(), 10)
                schema10.register(baseline)
                schema10.register(candidate)
                assignment = schema10.ensure_experiment_assignment(
                    "demo:schema10", "momentum", baseline.variant_id,
                    [descriptor], minimum_duration_seconds=THREE_DAYS,
                    minimum_observations=1, now=10.0)

            migrated = findings.FindingsStore(path)

            self.assertEqual(migrated.schema_version(), findings.SCHEMA_VERSION)
            self.assertEqual(
                migrated.migration_history()[-1]["name"],
                "paper_execution_evidence_and_validity")
            self.assertEqual(migrated.research_selections(), [])
            self.assertEqual(
                migrated.active_experiment_assignment(
                    "demo:schema10", "momentum")["assignment_id"],
                assignment["assignment_id"])


if __name__ == "__main__":
    unittest.main()
