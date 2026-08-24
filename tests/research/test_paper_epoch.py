"""Invariants for the isolated, frozen paper/shadow epoch control plane."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from research.paper_epoch import (
    CohortMember, ConfirmationRestart, EpochStateError,
    EpochValidationError, ExecutionObservation, FrozenEpoch, IdentityError,
    IntegrityViolation, IsolationError, OutcomeConflict, PaperEpochStore,
    PaperRuntimeIdentity, RuntimeAdaptationError, RuntimeStartAttestation,
    SealedLesson, content_digest, identity_fingerprint,
)


class PaperEpochTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-epoch-")
        self.db_path = Path(self.tmp.name) / "paper_epochs.sqlite3"
        self.store = PaperEpochStore(self.db_path)
        self.paper_account = identity_fingerprint(
            "paper-account", "paper-account-public-id")
        self.paper_runtime = identity_fingerprint(
            "paper-runtime", "paper-process-id")
        self.shadow_runtime = identity_fingerprint(
            "shadow-runtime", "shadow-process-id")
        self.primary_identity = PaperRuntimeIdentity(
            self.paper_account, self.paper_runtime)
        self.trader_account = identity_fingerprint(
            "paper-account", "deployed-trader-account")
        self.trader_runtime = identity_fingerprint(
            "paper-runtime", "deployed-trader-runtime")
        self.primary = CohortMember.paper_primary(
            "paper", self.primary_identity)
        self.shadow = CohortMember.shadow_sibling(
            "shadow-a", self.shadow_runtime)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def frozen(label="one", **changes):
        values = {
            "realtime_stream_digest": content_digest(["iex", "realtime"]),
            "data_window_digest": content_digest(["window", label]),
            "config_digest": content_digest(["config", label]),
            "code_digest": content_digest(["code", label]),
            "cost_digest": content_digest(["cost", label]),
            "risk_digest": content_digest(["risk", label]),
            "runtime_llm_adaptation": False,
            "halt_on_operational_failure": True,
        }
        values.update(changes)
        return FrozenEpoch(**values)

    def create(self, label="one", **kwargs):
        return self.store.create_epoch(
            self.frozen(label), self.primary, [self.shadow],
            trader_account_fingerprint=self.trader_account,
            trader_runtime_fingerprint=self.trader_runtime, **kwargs)

    def start(self, epoch):
        manifest = epoch["manifest_digest"]
        return self.store.start_epoch(epoch["epoch_id"], RuntimeStartAttestation(
            self.primary_identity,
            {"paper": self.paper_runtime, "shadow-a": self.shadow_runtime},
            {"paper": manifest, "shadow-a": manifest},
            runtime_llm_adaptation=False,
        ))

    @staticmethod
    def fills(paper_slippage=1.5, shadow_slippage=1.0):
        return [
            ExecutionObservation(
                "paper", "fill", quantity=10, reference_price=100,
                fill_price=100.015, slippage_bps=paper_slippage),
            ExecutionObservation(
                "shadow-a", "fill", quantity=10, reference_price=100,
                fill_price=100.01, slippage_bps=shadow_slippage),
        ]

    def terminal_with_lessons(self, epoch, lessons=("keep the spread guard",)):
        self.start(epoch)
        self.store.complete_epoch(epoch["epoch_id"])
        self.store.seal_lessons(epoch["epoch_id"], list(lessons))

    def restart(self, predecessor, label):
        window = self.frozen(label).data_window_digest
        return ConfirmationRestart(
            predecessor["epoch_id"],
            identity_fingerprint("confirmation-restart", label),
            window, unseen_data_confirmed=True, runtime_restarted=True,
            prior_epoch_data_excluded=True)

    def test_epoch_atomically_freezes_exactly_one_primary_and_a_shadow(self):
        epoch = self.create()
        self.assertEqual(epoch["status"], "frozen")
        self.assertEqual([row["role"] for row in epoch["cohort"]],
                         ["paper_primary", "shadow"])
        self.assertEqual(
            {row["manifest_digest"] for row in epoch["cohort"]},
            {epoch["manifest_digest"]})
        self.assertFalse(epoch["manifest"]["runtime_llm_adaptation"])
        self.assertFalse(epoch["policy"]["paper_success_can_promote"])
        self.assertEqual(self.store.verify_integrity()["epochs"], 1)

    def test_missing_shadow_duplicate_runtime_and_nonpaper_primary_fail_closed(self):
        with self.assertRaisesRegex(EpochValidationError, "shadow"):
            self.store.create_epoch(self.frozen(), self.primary, [])
        duplicate = CohortMember.shadow_sibling(
            "shadow-b", self.shadow_runtime)
        with self.assertRaises(IdentityError):
            self.store.create_epoch(
                self.frozen(), self.primary, [self.shadow, duplicate],
                trader_account_fingerprint=self.trader_account,
                trader_runtime_fingerprint=self.trader_runtime)
        with self.assertRaises(IdentityError):
            PaperRuntimeIdentity(
                identity_fingerprint("live-account", "account"),
                self.paper_runtime)
        with self.assertRaisesRegex(IdentityError, "deployed trader"):
            self.store.create_epoch(
                self.frozen(), self.primary, [self.shadow])
        with self.assertRaisesRegex(IdentityError, "accounts must be separate"):
            self.store.create_epoch(
                self.frozen(), self.primary, [self.shadow],
                trader_account_fingerprint=self.paper_account,
                trader_runtime_fingerprint=self.trader_runtime)

    def test_runtime_llm_adaptation_is_explicitly_disabled_twice(self):
        with self.assertRaises(RuntimeAdaptationError):
            self.frozen(runtime_llm_adaptation=True)
        epoch = self.create()
        with self.assertRaises(RuntimeAdaptationError):
            RuntimeStartAttestation(
                self.primary_identity,
                {"paper": self.paper_runtime,
                 "shadow-a": self.shadow_runtime},
                {"paper": epoch["manifest_digest"],
                 "shadow-a": epoch["manifest_digest"]},
                runtime_llm_adaptation=True)

    def test_start_requires_exact_identity_cohort_and_manifest(self):
        epoch = self.create()
        with self.assertRaises(IdentityError):
            self.store.start_epoch(epoch["epoch_id"], RuntimeStartAttestation(
                self.primary_identity,
                {"paper": self.paper_runtime},
                {"paper": epoch["manifest_digest"]}, False))
        wrong_manifest = content_digest("changed")
        with self.assertRaises(EpochValidationError):
            self.store.start_epoch(epoch["epoch_id"], RuntimeStartAttestation(
                self.primary_identity,
                {"paper": self.paper_runtime,
                 "shadow-a": self.shadow_runtime},
                {"paper": wrong_manifest, "shadow-a": wrong_manifest}, False))
        started = self.start(epoch)
        self.assertEqual(started["status"], "running")
        with self.assertRaises(EpochStateError):
            self.start(epoch)

    def test_database_stores_fingerprints_but_not_identity_sources_or_secrets(self):
        raw_account = "paper-account-public-id"
        raw_runtime = "paper-process-id"
        secret = "super-secret-api-key"
        epoch = self.create()
        self.start(epoch)
        database = self.db_path.read_bytes()
        self.assertNotIn(raw_account.encode(), database)
        self.assertNotIn(raw_runtime.encode(), database)
        self.assertNotIn(secret.encode(), database)
        self.assertIn(self.paper_account.encode(), database)
        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)

    def test_paired_fills_record_slippage_without_alpha_or_promotion(self):
        epoch = self.create()
        self.start(epoch)
        outcome = self.store.record_outcome(
            epoch["epoch_id"], "SPY:2026-08-24:1", "event-1", self.fills())
        self.assertTrue(outcome["operational_parity"])
        self.assertAlmostEqual(
            outcome["shadow_parity"][0]["slippage_delta_bps"], .5)
        self.assertEqual(outcome["alpha_evidence_count"], 0)
        self.assertFalse(outcome["promotion_authority"])
        summary = self.store.operational_summary(epoch["epoch_id"])
        self.assertEqual(summary["paper_fills"], 1)
        self.assertAlmostEqual(summary["mean_paper_slippage_bps"], 1.5)
        self.assertFalse(summary["paper_counts_as_alpha"])
        self.assertEqual(summary["alpha_contribution"], 0.0)
        completed = self.store.complete_epoch(epoch["epoch_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertFalse(completed["policy"]["promotion_authority"])

    def test_rejections_are_paired_and_changed_duplicate_is_a_conflict(self):
        epoch = self.create()
        self.start(epoch)
        observations = [
            ExecutionObservation("paper", "rejection",
                                 rejection_code="risk_limit"),
            ExecutionObservation("shadow-a", "rejection",
                                 rejection_code="risk_limit"),
        ]
        first = self.store.record_outcome(
            epoch["epoch_id"], "op-1", "event-1", observations)
        again = self.store.record_outcome(
            epoch["epoch_id"], "op-1", "event-1", observations)
        self.assertEqual(first["batch_id"], again["batch_id"])
        self.assertTrue(first["operational_parity"])
        changed = [observations[0], ExecutionObservation(
            "shadow-a", "rejection", rejection_code="stale_quote")]
        with self.assertRaises(OutcomeConflict):
            self.store.record_outcome(
                epoch["epoch_id"], "op-1", "event-1", changed)

    def test_incomplete_pair_is_rejected_and_operational_mismatch_stops(self):
        epoch = self.create()
        self.start(epoch)
        with self.assertRaisesRegex(EpochValidationError, "cohort"):
            self.store.record_outcome(
                epoch["epoch_id"], "op-1", "event-1", self.fills()[:1])
        mismatched = [
            ExecutionObservation(
                "paper", "fill", quantity=10, reference_price=100,
                fill_price=100.01, slippage_bps=1),
            ExecutionObservation(
                "shadow-a", "rejection", rejection_code="stale_quote"),
        ]
        outcome = self.store.record_outcome(
            epoch["epoch_id"], "op-2", "event-2", mismatched)
        self.assertFalse(outcome["operational_parity"])
        self.assertTrue(outcome["operational_failure"])
        self.assertEqual(self.store.epoch(epoch["epoch_id"])["status"], "stopped")
        with self.assertRaises(EpochStateError):
            self.store.record_outcome(
                epoch["epoch_id"], "op-3", "event-3", self.fills())

    def test_lessons_are_next_epoch_only_and_need_unseen_restart(self):
        isolated = {
            "trader_account_fingerprint": self.trader_account,
            "trader_runtime_fingerprint": self.trader_runtime,
        }
        first = self.create("one")
        self.terminal_with_lessons(first, [SealedLesson(
            "execution", "keep the spread guard")])
        self.assertEqual(self.store.lessons_for_epoch(first["epoch_id"]), [])

        with self.assertRaisesRegex(EpochStateError, "confirmation"):
            self.store.create_epoch(
                self.frozen("two"), self.primary, [self.shadow],
                predecessor_epoch_id=first["epoch_id"], **isolated)
        with self.assertRaisesRegex(EpochValidationError, "new unseen"):
            same_window = ConfirmationRestart(
                first["epoch_id"],
                identity_fingerprint("confirmation-restart", "same"),
                first["manifest"]["data_window_digest"], True, True, True)
            self.store.create_epoch(
                self.frozen("one"), self.primary, [self.shadow],
                predecessor_epoch_id=first["epoch_id"],
                confirmation=same_window, **isolated)

        second = self.store.create_epoch(
            self.frozen("two"), self.primary, [self.shadow],
            predecessor_epoch_id=first["epoch_id"],
            confirmation=self.restart(first, "two"), **isolated)
        visible = self.store.lessons_for_epoch(second["epoch_id"])
        self.assertEqual([row["statement"] for row in visible],
                         ["keep the spread guard"])

        self.terminal_with_lessons(second, ["new-window lesson"])
        third = self.store.create_epoch(
            self.frozen("three"), self.primary, [self.shadow],
            predecessor_epoch_id=second["epoch_id"],
            confirmation=self.restart(second, "three"), **isolated)
        self.assertEqual(
            [row["statement"] for row in
             self.store.lessons_for_epoch(third["epoch_id"])],
            ["new-window lesson"])

    def test_cohort_and_audit_rows_are_sqlite_immutable(self):
        epoch = self.create()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute(
                    "UPDATE paper_epochs SET config_digest=? WHERE epoch_id=?",
                    (content_digest("tamper"), epoch["epoch_id"]))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "frozen"):
                db.execute("""INSERT INTO paper_cohort
                    (epoch_id,member_id,role,runtime_mode,runtime_fingerprint,
                     account_fingerprint,manifest_digest,created_at,row_digest)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (
                        epoch["epoch_id"], "late-shadow", "shadow", "shadow",
                        identity_fingerprint("shadow-runtime", "late"), None,
                        epoch["manifest_digest"], 1.0, content_digest("row")))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute("DELETE FROM paper_epoch_audit")
        self.assertTrue(self.store.verify_integrity()["ok"])

    def test_verifier_detects_trigger_removal_and_bypassed_row_tamper(self):
        epoch = self.create()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DROP TRIGGER paper_epochs_no_update")
            db.execute(
                "UPDATE paper_epochs SET config_digest=? WHERE epoch_id=?",
                (content_digest("forged"), epoch["epoch_id"]))
            db.commit()
        with self.assertRaises(IntegrityViolation):
            self.store.verify_integrity()

    def test_store_refuses_to_share_a_foreign_sqlite_namespace(self):
        foreign = Path(self.tmp.name) / "edge.sqlite3"
        with closing(sqlite3.connect(foreign)) as db, db:
            db.execute("CREATE TABLE candidates(candidate_id TEXT PRIMARY KEY)")
        with self.assertRaisesRegex(IsolationError, "separate"):
            PaperEpochStore(foreign)

    def test_research_cli_creates_and_verifies_the_isolated_store(self):
        document = Path(self.tmp.name) / "create.json"
        document.write_text(json.dumps({
            "schema": "paper-epoch-create.v1",
            "epoch_id": "paper-epoch-cli",
            "frozen": self.frozen("cli").as_dict(),
            "primary": self.primary.as_dict(),
            "shadows": [self.shadow.as_dict()],
            "trader_account_fingerprint": self.trader_account,
            "trader_runtime_fingerprint": self.trader_runtime,
        }), encoding="utf-8")
        root = Path(__file__).resolve().parents[2]
        created = subprocess.run([
            sys.executable, "research.py", "paper-epoch", "create",
            "--db", str(self.db_path), "--input", str(document),
        ], cwd=root, capture_output=True, text=True, check=False)
        self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
        self.assertEqual(json.loads(created.stdout)["status"], "frozen")
        verified = subprocess.run([
            sys.executable, "research.py", "paper-epoch", "verify",
            "--db", str(self.db_path),
        ], cwd=root, capture_output=True, text=True, check=False)
        self.assertEqual(verified.returncode, 0,
                         verified.stderr + verified.stdout)
        self.assertTrue(json.loads(verified.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
