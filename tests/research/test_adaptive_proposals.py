import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import hypotheses, shadow, variants
from agent.engine import Engine
from research import findings
from tests.helpers import valid_config
from tests.research.test_enrichment_isolation import symbol_snapshot


HYPOTHESIS_ID = "volume-thrust"
SETTING_ID = "lower_participation"
TARGET_PARAMETER = "min_relative_volume"


def research_proposal(value=1.3):
    return {
        "action": "research_proposal",
        "hypothesis_id": HYPOTHESIS_ID,
        "setting_id": SETTING_ID,
        "value": value,
        "reasoning": "Test the exact participation threshold at this value.",
    }


def open_proposal():
    return {
        "action": "open",
        "symbol": "BTC/USDT:USDT",
        "direction": "long",
        "setup_type": "other",
        "hypothesis_id": HYPOTHESIS_ID,
        "confidence": 0.9,
        "invalidation_anchor": "structure",
        "exit_policy": "fixed_rr",
    }


class AdaptiveProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "findings.db"
        self.store = findings.FindingsStore(self.path)
        self.cfg = valid_config()

    @staticmethod
    def exact_variant(value, setting_id=SETTING_ID):
        return variants.adaptive_hypothesis_variant(
            "momentum", "phase1-v3", HYPOTHESIS_ID, setting_id, value)

    def persist(self, value, run_id, now, setting_id=SETTING_ID):
        metadata = hypotheses.numeric_setting_metadata(
            HYPOTHESIS_ID, setting_id)
        return self.store.propose_numeric_setting(
            "momentum", HYPOTHESIS_ID, setting_id, value, run_id,
            minimum=metadata["minimum"], maximum=metadata["maximum"],
            target_parameter=metadata["target_parameter"],
            observation_lock_seconds=metadata["observation_seconds"],
            variant=self.exact_variant(value, setting_id),
            reasoning="Test the exact registered numeric value.", now=now)

    def test_value_1_3_is_registered_persisted_and_scheduled_exactly(self):
        static = next(
            item for item in variants.hypothesis_variants(
                "momentum", "phase1-v3")
            if item.variant_id.endswith(f".{SETTING_ID}"))
        self.assertEqual(static.hypothesis_params[TARGET_PARAMETER], 1.2)
        evaluator = shadow.ShadowEvaluator(
            [static], self.cfg, store=self.store, scope_key="demo:adaptive")
        engine = Engine.__new__(Engine)
        engine.strategy_id = "momentum"
        engine.strategy_version = "phase1-v3"
        engine.run_id = "run-exact-1.3"
        engine.shadow = evaluator

        accepted = engine._record_research_proposals(
            [open_proposal(), research_proposal(1.3)])
        stored = next(item for item in accepted
                      if item["action"] == "research_proposal")
        exact_id = stored["variant_id"]

        self.assertEqual(stored["target_parameter"], TARGET_PARAMETER)
        self.assertEqual(stored["minimum"], 1.0)
        self.assertEqual(stored["maximum"], 5.0)
        self.assertEqual(
            self.store.variant(exact_id)["hypothesis_id"], HYPOTHESIS_ID)
        persisted_params = json.loads(
            self.store.variant(exact_id)["hypothesis_params_json"])
        self.assertEqual(persisted_params[TARGET_PARAMETER], 1.3)

        now = stored["proposed_ts"] + 1.0
        records = evaluator.evaluate(
            {"BTC/USDT:USDT": symbol_snapshot(relative_volume_1h=1.25)},
            now=now, proposals=accepted, advance_accounts=False)

        self.assertEqual([record.variant_id for record in records], [exact_id])
        self.assertIn("below 1.3", records[0].reason)
        self.assertNotEqual(records[0].variant_id, static.variant_id)

        reopened = findings.FindingsStore(self.path)
        proposal = reopened.hypothesis_proposal(stored["proposal_id"])
        self.assertEqual(proposal["value"], 1.3)
        self.assertEqual(proposal["run_id"], "run-exact-1.3")
        self.assertEqual(proposal["current_status"], "PROPOSED")
        self.assertGreater(
            proposal["observation_until_ts"], proposal["proposed_ts"])
        self.assertEqual(
            [event["status"] for event in reopened.proposal_events(
                stored["proposal_id"])],
            ["PROPOSED"])
        finding = reopened.findings_for(exact_id)[0]
        finding_payload = json.loads(finding["text"])
        self.assertEqual(
            finding_payload["proposal"]["reasoning"],
            research_proposal()["reasoning"])

    def test_terminal_exact_values_cannot_retry_across_runs(self):
        observed = self.persist(1.3, "run-1", 10.0)
        self.store.append_proposal_event(
            observed["proposal_id"], "OBSERVING", now=11.0)
        self.store.append_proposal_event(
            observed["proposal_id"], "OBSERVED", now=12.0)
        rejected = self.persist(1.4, "run-2", 13.0)
        self.store.append_proposal_event(
            rejected["proposal_id"], "REJECTED",
            {"reason": "no eligible observation"}, now=14.0)

        with self.assertRaisesRegex(ValueError, "duplicate exact"):
            self.persist(1.3, "run-3", 15.0)
        with self.assertRaisesRegex(ValueError, "duplicate exact"):
            self.persist(1.4, "run-4", 16.0)
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.store.append_proposal_event(
                rejected["proposal_id"], "OBSERVING", now=17.0)

    def test_finding_failure_rolls_back_variant_and_proposal_nonfatally(self):
        static = next(
            item for item in variants.hypothesis_variants(
                "momentum", "phase1-v3")
            if item.variant_id.endswith(f".{SETTING_ID}"))
        evaluator = shadow.ShadowEvaluator(
            [static], self.cfg, store=self.store, scope_key="demo:rollback")
        engine = Engine.__new__(Engine)
        engine.strategy_id = "momentum"
        engine.strategy_version = "phase1-v3"
        engine.run_id = "run-rollback"
        engine.shadow = evaluator
        exact_id = self.exact_variant(1.3).variant_id
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TRIGGER fail_adaptive_finding
                BEFORE INSERT ON findings BEGIN
                    SELECT RAISE(ABORT, 'injected finding failure');
                END;
            """)

        accepted = engine._record_research_proposals(
            [open_proposal(), research_proposal(1.3)])

        self.assertEqual(accepted, [open_proposal()])
        self.assertIsNone(self.store.variant(exact_id))
        self.assertEqual(
            self.store.hypothesis_proposals("momentum", HYPOTHESIS_ID), [])


class AdaptiveProposalMigrationTests(unittest.TestCase):
    def test_v8_proposals_gain_metadata_lifecycle_and_exact_value_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v8.db"
            with patch.object(findings, "SCHEMA_VERSION", 8):
                self.assertEqual(findings.FindingsStore(path).schema_version(), 8)
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "INSERT INTO hypothesis_proposals "
                    "(proposal_id, strategy_id, hypothesis_id, setting_id, "
                    "value_json, run_id, proposed_ts, observation_until_ts, "
                    "status) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("legacy-proposal", "momentum", HYPOTHESIS_ID,
                     SETTING_ID, "1.3", "legacy-run", 10.0, 20.0,
                     "REJECTED"))

            migrated = findings.FindingsStore(path)
            self.assertEqual(migrated.schema_version(), findings.SCHEMA_VERSION)
            row = migrated.hypothesis_proposal("legacy-proposal")
            self.assertEqual(row["target_parameter"], TARGET_PARAMETER)
            self.assertEqual(row["minimum"], 1.0)
            self.assertEqual(row["maximum"], 5.0)
            self.assertIn("schema 8", row["reasoning"])
            self.assertEqual(row["current_status"], "REJECTED")
            self.assertEqual(
                [event["status"] for event in migrated.proposal_events(
                    "legacy-proposal")], ["PROPOSED", "REJECTED"])
            self.assertIn(
                "durable_strategy_experiment_rotation",
                [row["name"] for row in migrated.migration_history()])
            self.assertEqual(
                migrated.migration_history()[-1]["name"],
                "paper_execution_evidence_and_validity")
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM hypothesis_proposal_value_locks"
                ).fetchone()[0], 1)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE hypothesis_proposal_events "
                        "SET status='OBSERVED' WHERE proposal_id=?",
                        ("legacy-proposal",))

            metadata = hypotheses.numeric_setting_metadata(
                HYPOTHESIS_ID, SETTING_ID)
            with self.assertRaisesRegex(ValueError, "duplicate exact"):
                migrated.propose_numeric_setting(
                    "momentum", HYPOTHESIS_ID, SETTING_ID, 1.3, "new-run",
                    minimum=metadata["minimum"],
                    maximum=metadata["maximum"],
                    target_parameter=metadata["target_parameter"],
                    observation_lock_seconds=metadata["observation_seconds"],
                    variant=variants.adaptive_hypothesis_variant(
                        "momentum", "phase1-v3", HYPOTHESIS_ID,
                        SETTING_ID, 1.3),
                    reasoning="Do not retry the migrated exact value.", now=30.0)


if __name__ == "__main__":
    unittest.main()
