"""Learning-epoch quarantine at the factory lesson ledger boundary."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace

from agent.contracts.rule import rule_variant_id
from research.factory_core import initial_hypotheses, replacement_hypothesis
from research.factory_ledger import FactoryError, FactoryLedger, learning_epoch_fingerprint
from research.llm_strategy import PROPOSAL_SCHEMA, TUNING_SCHEMA, ProposalResult
from research.strategy_factory import (
    _discovery_context, _llm_replacement, _sanitize_fit_selection,
    _slot_families, _tuned_variants)


class FactoryLessonEpochTests(unittest.TestCase):
    def _ledger(self, directory: str) -> tuple[FactoryLedger, str, str]:
        ledger = FactoryLedger(Path(directory) / "factory.sqlite3")
        hypothesis = initial_hypotheses(1)[0]
        ledger.register(hypothesis)
        return ledger, hypothesis.hypothesis_id, hypothesis.family

    @staticmethod
    def _epoch(suffix: str) -> str:
        return learning_epoch_fingerprint(
            economics={"fee_bps": suffix},
            geometry={"stop_floor": suffix},
            feed={"provider": "test", "name": suffix},
            grammar={"schema": "rule-grammar.v4", "name": suffix},
        )

    def _record_and_grade(self, ledger: FactoryLedger, hypothesis_id: str,
                          family: str, variant_id: str, *, epoch=None,
                          passed: bool = True) -> str:
        lesson_id = ledger.record_lesson(
            hypothesis_id, vehicle="equity", family=family,
            variant_id=variant_id, kind="tuning", source="llm",
            reason="epoch-bound proposal", changed={"threshold_bps": {
                "from": 5, "to": 6}}, learning_epoch=epoch)
        self.assertEqual(
            ledger.grade_lesson(
                hypothesis_id, variant_id, kind="tuning",
                outcome={"passed": passed}, learning_epoch=epoch), lesson_id)
        return lesson_id

    def test_legacy_rows_are_auditable_but_not_active(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, hypothesis_id, family = self._ledger(directory)
            legacy = self._record_and_grade(
                ledger, hypothesis_id, family, "legacy", epoch=None, passed=False)
            current = self._epoch("current")

            self.assertEqual(ledger.lessons(
                vehicle="equity", graded_only=True), [])
            audit = ledger.lessons(
                vehicle="equity", graded_only=True, include_quarantined=True)
            self.assertEqual([row["lesson_id"] for row in audit], [legacy])
            self.assertTrue(audit[0]["quarantined"])
            self.assertEqual(ledger.lessons(
                vehicle="equity", graded_only=True,
                learning_epoch=current), [])

    @staticmethod
    def _downgrade_to_legacy_schema(path: Path) -> None:
        """Recreate the pre-epoch append-only tables for migration coverage."""
        db = sqlite3.connect(path)
        db.execute("PRAGMA foreign_keys=OFF")
        for trigger in (
                "factory_lessons_no_update", "factory_lessons_no_delete",
                "factory_lesson_outcomes_no_update",
                "factory_lesson_outcomes_no_delete",
                "factory_accounts_no_update", "factory_accounts_no_delete",
                "factory_variant_closures_no_update",
                "factory_variant_closures_no_delete"):
            db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for index in (
                "factory_lessons_family", "factory_lessons_parent",
                "factory_lessons_legacy_key", "factory_lessons_epoch_key",
                "factory_variant_closures_vehicle",
                "factory_variant_closures_legacy_key",
                "factory_variant_closures_epoch_key"):
            db.execute(f"DROP INDEX IF EXISTS {index}")
        db.execute("ALTER TABLE factory_lesson_outcomes RENAME TO old_outcomes")
        db.execute("ALTER TABLE factory_lessons RENAME TO old_lessons")
        db.execute("ALTER TABLE factory_accounts RENAME TO old_accounts")
        db.execute("ALTER TABLE factory_variant_closures RENAME TO old_closures")
        db.execute("""CREATE TABLE factory_lessons (
            lesson_id TEXT PRIMARY KEY,
            hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
            vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
            family TEXT NOT NULL, variant_id TEXT NOT NULL, kind TEXT NOT NULL,
            source TEXT NOT NULL, reason TEXT NOT NULL,
            changed_json TEXT NOT NULL, diagnosis_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL, created_at REAL NOT NULL,
            parent_lesson_id TEXT REFERENCES factory_lessons(lesson_id),
            UNIQUE(hypothesis_id,variant_id,kind))""")
        db.execute("""CREATE TABLE factory_lesson_outcomes (
            outcome_id TEXT PRIMARY KEY,
            lesson_id TEXT NOT NULL REFERENCES factory_lessons(lesson_id),
            passed INTEGER NOT NULL, underpowered INTEGER NOT NULL,
            classification TEXT NOT NULL, fit_delta REAL, heldout_delta REAL,
            heldout_net_pnl REAL, q_value REAL, failed_checks_json TEXT NOT NULL,
            gate_hash TEXT, created_at REAL NOT NULL, UNIQUE(lesson_id))""")
        db.execute("""CREATE TABLE factory_variant_closures (
            closure_id TEXT PRIMARY KEY,
            hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
            vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
            variant_id TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('scientific','budget','recenter')),
            reason TEXT NOT NULL, attempts INTEGER NOT NULL,
            evidence_json TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(hypothesis_id,variant_id))""")
        db.execute("""CREATE TABLE factory_accounts (
            account_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
            variant_id TEXT NOT NULL, vehicle TEXT NOT NULL,
            starting_cash REAL NOT NULL, ending_equity REAL NOT NULL,
            realized_pnl REAL NOT NULL, max_drawdown REAL NOT NULL,
            trades INTEGER NOT NULL, worker_pid INTEGER NOT NULL,
            result_json TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(cycle_id,variant_id,vehicle))""")
        db.execute("""INSERT INTO factory_lessons
            SELECT lesson_id,hypothesis_id,vehicle,family,variant_id,kind,source,
                   reason,changed_json,diagnosis_json,evidence_json,created_at,
                   parent_lesson_id FROM old_lessons""")
        db.execute("""INSERT INTO factory_lesson_outcomes
            SELECT outcome_id,lesson_id,passed,underpowered,classification,fit_delta,
                   heldout_delta,heldout_net_pnl,q_value,failed_checks_json,gate_hash,
                   created_at FROM old_outcomes""")
        db.execute("""INSERT INTO factory_variant_closures
            SELECT closure_id,hypothesis_id,vehicle,variant_id,mode,reason,attempts,
                   evidence_json,created_at FROM old_closures""")
        db.execute("""INSERT INTO factory_accounts
            SELECT account_id,cycle_id,hypothesis_id,variant_id,vehicle,
                   starting_cash,ending_equity,realized_pnl,max_drawdown,trades,
                   worker_pid,result_json,created_at FROM old_accounts""")
        db.execute("DROP TABLE old_outcomes")
        db.execute("DROP TABLE old_lessons")
        db.execute("DROP TABLE old_closures")
        db.execute("DROP TABLE old_accounts")
        db.commit()
        db.close()

    def test_pre_epoch_database_migrates_without_reinterpreting_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.sqlite3"
            ledger, hypothesis_id, family = self._ledger(directory)
            legacy = self._record_and_grade(
                ledger, hypothesis_id, family, "legacy", epoch=None)
            ledger.close_variant(
                hypothesis_id, "legacy", vehicle="equity", mode="budget",
                reason="legacy closure")
            ledger.add_account(
                "legacy-account", hypothesis_id,
                {"variant_id": "legacy", "vehicle": "equity", "worker_pid": 1,
                 "account": {"account_id": "legacy-account", "starting_cash": 100.0,
                             "ending_equity": 99.0, "realized_pnl": -1.0,
                             "max_drawdown": 1.0, "trades": 1}})
            self._downgrade_to_legacy_schema(path)

            migrated = FactoryLedger(path)
            audit = migrated.lessons(
                vehicle="equity", graded_only=True, include_quarantined=True)
            self.assertEqual([row["lesson_id"] for row in audit], [legacy])
            self.assertIsNone(audit[0]["learning_epoch"])
            self.assertEqual(migrated.closed_variant_ids(
                vehicle="equity", learning_epoch=self._epoch("current")), set())
            with closing(sqlite3.connect(path)) as connection:
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(factory_lessons)")}
                account_columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(factory_accounts)")}
            self.assertIn("learning_epoch", columns)
            self.assertIn("learning_epoch", account_columns)
            self.assertEqual(migrated.account_attempts(
                hypothesis_id, "legacy", learning_epoch=self._epoch("current")), 0)

    def test_exact_epoch_is_required_for_active_grading(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, hypothesis_id, family = self._ledger(directory)
            epoch_a = self._epoch("a")
            epoch_b = self._epoch("b")
            lesson_a = self._record_and_grade(
                ledger, hypothesis_id, family, "same-variant",
                epoch=epoch_a, passed=True)
            lesson_b = self._record_and_grade(
                ledger, hypothesis_id, family, "same-variant",
                epoch=epoch_b, passed=False)

            self.assertNotEqual(lesson_a, lesson_b)
            self.assertEqual([row["lesson_id"] for row in ledger.lessons(
                vehicle="equity", graded_only=True,
                learning_epoch=epoch_a)], [lesson_a])
            self.assertEqual([row["lesson_id"] for row in ledger.lessons(
                vehicle="equity", graded_only=True,
                learning_epoch=epoch_b)], [lesson_b])
            self.assertEqual(ledger.lessons(
                vehicle="equity", graded_only=True,
                learning_epoch=self._epoch("other")), [])
            audit = ledger.lessons(
                vehicle="equity", graded_only=True,
                learning_epoch=epoch_a, include_quarantined=True)
            by_id = {row["lesson_id"]: row for row in audit}
            self.assertEqual(set(by_id), {lesson_a, lesson_b})
            self.assertTrue(by_id[lesson_b]["quarantined"])
            self.assertTrue(by_id[lesson_a]["active_for_epoch"])

    def test_legacy_closures_do_not_poison_a_new_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, hypothesis_id, family = self._ledger(directory)
            legacy = ledger.close_variant(
                hypothesis_id, "same-variant", vehicle="equity", mode="budget",
                reason="legacy closure")
            epoch = self._epoch("current")
            self.assertIsNone(legacy["learning_epoch"])
            self.assertEqual(ledger.closed_variant_ids(vehicle="equity"), set())
            self.assertEqual(ledger.closed_variant_ids(
                vehicle="equity", learning_epoch=epoch), set())
            current = ledger.close_variant(
                hypothesis_id, "same-variant", vehicle="equity", mode="budget",
                reason="current closure", learning_epoch=epoch)
            self.assertEqual(current["learning_epoch"], epoch)
            self.assertEqual(ledger.closed_variant_ids(
                vehicle="equity", learning_epoch=epoch), {"same-variant"})
            self.assertEqual(len(ledger.variant_closures(vehicle="equity")), 2)
            closure_audit = ledger.variant_closures(
                vehicle="equity", learning_epoch=epoch,
                include_quarantined=True)
            self.assertEqual(len(closure_audit), 2)
            by_epoch = {row["learning_epoch"]: row for row in closure_audit}
            self.assertTrue(by_epoch[None]["quarantined"])
            self.assertFalse(by_epoch[epoch]["quarantined"])

    def test_attempt_budgets_exclude_legacy_and_mismatched_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, hypothesis_id, family = self._ledger(directory)
            variant_id = "same-variant"
            current = self._epoch("current")
            other = self._epoch("other")
            gate = {"passes": False, "sample_adequate": True,
                    "heldout_sample_adequate": True}
            for cycle, epoch in (("legacy", None), ("other", other),
                                 ("current", current)):
                ledger.add_account(
                    cycle, hypothesis_id,
                    {"variant_id": variant_id, "vehicle": "equity",
                     "worker_pid": 1, "gate": gate,
                     "account": {"account_id": cycle,
                                 "starting_cash": 100.0,
                                 "ending_equity": 99.0,
                                 "realized_pnl": -1.0,
                                 "max_drawdown": 1.0, "trades": 10}},
                    learning_epoch=epoch)
            self.assertEqual(ledger.account_attempts(hypothesis_id, variant_id), 3)
            self.assertEqual(ledger.variant_attempts(
                hypothesis_id, variant_id, learning_epoch=current), 1)
            self.assertEqual(ledger.account_attempts(
                hypothesis_id, variant_id, learning_epoch=current), 1)
            closure = ledger.close_variant(
                hypothesis_id, variant_id, vehicle="equity", mode="budget",
                reason="current budget", attempts=1, learning_epoch=current)
            self.assertEqual(closure["account_attempts_total"], 1)

    def test_retirement_accounts_must_match_epoch_for_every_mode(self):
        current = self._epoch("current")
        other = self._epoch("other")
        for mode in ("scientific", "budget", "recenter"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                ledger = FactoryLedger(Path(directory) / "factory.sqlite3")
                hypothesis = initial_hypotheses(1)[0]
                ledger.register(hypothesis)
                child = replacement_hypothesis(
                    asdict(hypothesis), {"primary_failure": "negative_expectancy"},
                    max_generations=3)
                self.assertIsNotNone(child)
                ledger.register(child)

                # Both rows belong to the requested cycle, but neither is in
                # the active epoch: one is from another campaign and one is a
                # pre-epoch legacy row.  They must not satisfy the intended
                # account count in any retirement mode.
                for variant_id, epoch in (("other-epoch", other),
                                          ("legacy", None)):
                    ledger.add_account(
                        "retirement-cycle", hypothesis.hypothesis_id,
                        {"variant_id": variant_id, "vehicle": "equity",
                         "worker_pid": 1,
                         "account": {"account_id": variant_id,
                                     "starting_cash": 100.0,
                                     "ending_equity": 99.0,
                                     "realized_pnl": -1.0,
                                     "max_drawdown": 1.0, "trades": 1}},
                        learning_epoch=epoch)

                with self.assertRaisesRegex(
                        FactoryError, "requires every intended variant account"):
                    ledger.retire_hypothesis(
                        hypothesis.hypothesis_id, cycle_id="retirement-cycle",
                        expected_variants=2, reason="epoch quarantine regression",
                        mode=mode, learning_epoch=current)
                with self.assertRaisesRegex(
                        FactoryError, "requires every intended variant account"):
                    ledger.retire_hypothesis(
                        hypothesis.hypothesis_id, cycle_id="retirement-cycle",
                        expected_variants=2, reason="legacy epoch quarantine regression",
                        mode=mode)

    def test_retirement_binds_explicit_successor_instead_of_latest_child(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "factory.sqlite3")
            hypothesis = initial_hypotheses(1)[0]
            ledger.register(hypothesis)
            old_spec = {**hypothesis.rule_spec,
                        "threshold_bps": hypothesis.rule_spec["threshold_bps"] + 1}
            latest_spec = {**hypothesis.rule_spec,
                           "threshold_bps": hypothesis.rule_spec["threshold_bps"] + 2}
            old_child = replace(
                hypothesis, hypothesis_id="child-old", generation=1,
                parent_hypothesis_id=hypothesis.hypothesis_id,
                rule_spec=old_spec)
            latest_child = replace(
                hypothesis, hypothesis_id="child-latest", generation=2,
                parent_hypothesis_id=hypothesis.hypothesis_id,
                rule_spec=latest_spec)
            ledger.register(old_child)
            ledger.register(latest_child)

            current = self._epoch("successor-binding")
            latest_variant = rule_variant_id(latest_spec)
            ledger.add_account(
                "successor-cycle", hypothesis.hypothesis_id,
                {"variant_id": latest_variant, "vehicle": "equity",
                 "worker_pid": 1,
                 "account": {"account_id": "successor-account",
                             "starting_cash": 100.0,
                             "ending_equity": 99.0,
                             "realized_pnl": -1.0,
                             "max_drawdown": 1.0, "trades": 1}},
                learning_epoch=current)
            payload = {
                # The older child is the intended successor.  The fit fields
                # deliberately name the newer child's variant: a latest-child
                # lookup would accept this and retire against the wrong child.
                "replacement_hypothesis_id": old_child.hypothesis_id,
                "from_variant_id": latest_variant,
                "to_variant_id": latest_variant,
                "fit_score_source": "fit_test.mean_delta",
                "fit_score": 1.0,
            }
            with self.assertRaisesRegex(
                    FactoryError, "does not match successor spec"):
                ledger.retire_hypothesis(
                    hypothesis.hypothesis_id, cycle_id="successor-cycle",
                    expected_variants=1, reason="successor binding regression",
                    payload=payload, mode="recenter", learning_epoch=current)

            with self.assertRaisesRegex(
                    FactoryError, "requires explicit replacement_hypothesis_id"):
                ledger.retire_hypothesis(
                    hypothesis.hypothesis_id, cycle_id="successor-cycle",
                    expected_variants=1, reason="ambiguous successor regression",
                    mode="recenter", learning_epoch=current)

    def test_slot_families_are_epoch_scoped_for_active_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, hypothesis_id, family = self._ledger(directory)
            legacy = self._record_and_grade(
                ledger, hypothesis_id, family, "legacy", epoch=None)
            current = self._epoch("current")
            self.assertEqual(_slot_families(
                ledger, "equity", 0, learning_epoch=current), set())
            self.assertEqual(ledger.slot_families("equity", 0), {family})
            self._record_and_grade(
                ledger, hypothesis_id, family, "current", epoch=current)
            self.assertEqual(_slot_families(
                ledger, "equity", 0, learning_epoch=current), {family})

    def test_old_citation_and_budget_callers_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, hypothesis_id, family = self._ledger(directory)
            legacy = self._record_and_grade(
                ledger, hypothesis_id, family, "legacy", epoch=None)
            self.assertIsNone(ledger.resolve_lesson_ref(legacy[:8]))
            self.assertEqual(ledger.novel_tuning_values(
                hypothesis_id=hypothesis_id, vehicle="equity", family=family),
                set())
            self.assertEqual(ledger.failed_variant_ids(vehicle="equity"), set())

    def test_epoch_scoped_prompt_explains_quarantine_without_stale_details(self):
        context = _discovery_context(
            slot=0, reason="test", previous={}, tried_families=set(),
            proved_families=(), learning_epoch="current")
        projected = _sanitize_fit_selection(
            context, context="discovery_context")
        notice = projected["learning_epoch_notice"]
        self.assertIn("audit-only", notice)
        self.assertIn("non-citable", notice)
        self.assertIn("absence is not evidence", notice)

    def test_epoch_notice_reaches_tuning_and_replacement_adapters(self):
        class CaptureAdapter:
            def __init__(self):
                self.tuning_diagnosis = None
                self.replacement_diagnosis = None

            def tune(self, **kwargs):
                self.tuning_diagnosis = kwargs["diagnosis"]
                return ProposalResult(False, schema=TUNING_SCHEMA,
                                      error="test adapter")

            def propose(self, **kwargs):
                self.replacement_diagnosis = kwargs["diagnosis"]
                return ProposalResult(False, schema=PROPOSAL_SCHEMA,
                                      error="test adapter")

        adapter = CaptureAdapter()
        hypothesis = initial_hypotheses(1)[0]
        diagnostic = {"primary_failure": "negative_expectancy",
                      "trades": 40, "net_pnl": -1.0}
        _tuned_variants(
            asdict(hypothesis), diagnostic, count=2, vehicle="equity",
            llm_enabled=True, config={"model": "test"}, adapter=adapter,
            learning_epoch="current")
        _llm_replacement(
            asdict(hypothesis), diagnostic, config={"model": "test"},
            max_generations=3, not_before=None, existing_variant_ids=set(),
            adapter=adapter, learning_epoch="current")
        self.assertIn("audit-only", adapter.tuning_diagnosis[
            "learning_epoch_notice"])
        self.assertIn("non-citable", adapter.replacement_diagnosis[
            "learning_epoch_notice"])


if __name__ == "__main__":
    unittest.main()
