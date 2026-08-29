"""Durable confirmatory false-discovery budget tests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from research.factory_ledger import (
    CONFIRMATORY_SCOPE_VERSION, FDR_METHOD, FDR_GAMMA_METHOD,
    FDR_INITIAL_WEALTH_FRACTION, LEGACY_FDR_METHOD, LEGACY_RAW_FDR_METHOD,
    FactoryLedger, deferred_fdr)
from research.factory_core import initial_hypotheses
from research.factory_ledger import FactoryError


class FactoryFdrTests(unittest.TestCase):
    def test_balanced_allocations_are_previewed_without_being_spent(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = f"{CONFIRMATORY_SCOPE_VERSION}:equity"

            first = ledger.next_fdr_allocation(scope)
            self.assertEqual(first["method"], FDR_METHOD)
            self.assertEqual(first["tests"], 1)
            self.assertAlmostEqual(first["allocated_alpha"], .025)
            self.assertEqual(ledger.fdr_state(scope)["tests"], 0)

            expected = (.025, .05 / 6, .05 / 12, .05 / 20)
            for index, allocation in enumerate(expected, start=1):
                preview = ledger.next_fdr_allocation(scope)
                self.assertEqual(preview["tests"], index)
                self.assertAlmostEqual(preview["allocated_alpha"], allocation)
                recorded = ledger.record_fdr_decision(
                    scope, f"failed-{index}", 1.0)
                self.assertEqual(recorded["tests"], index)
                self.assertAlmostEqual(recorded["allocated_alpha"], allocation)
                self.assertFalse(recorded["decision"])

    def test_discovery_reward_and_duplicate_test_are_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = f"{CONFIRMATORY_SCOPE_VERSION}:equity"
            first = ledger.record_fdr_decision(scope, "proof-a", .001)
            self.assertTrue(first["decision"])

            # LORD++ keeps the base stream at W0*gamma_2.  With the
            # preregistered W0=alpha, the first-discovery reward alpha-W0 is
            # zero; only later discoveries receive a reward stream.
            preview = ledger.next_fdr_allocation(scope)
            self.assertAlmostEqual(preview["allocated_alpha"], .05 / 6)
            duplicate = ledger.record_fdr_decision(scope, "proof-a", .9)
            self.assertEqual(duplicate["tests"], 1)
            self.assertEqual(duplicate["p_value"], .001)
            self.assertTrue(duplicate["decision"])
            self.assertEqual(ledger.fdr_state(scope)["tests"], 1)

    def test_scope_alpha_is_locked_by_the_first_durable_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = f"{CONFIRMATORY_SCOPE_VERSION}:equity"
            first = ledger.record_fdr_decision(scope, "proof-a", 1.0, alpha=.05)
            self.assertAlmostEqual(first["allocated_alpha"], .025)
            with self.assertRaisesRegex(FactoryError, "immutable"):
                ledger.next_fdr_allocation(scope, alpha=1.0)
            with self.assertRaisesRegex(FactoryError, "immutable"):
                ledger.record_fdr_decision(scope, "proof-b", 1.0, alpha=1.0)
            self.assertEqual(ledger.fdr_state(scope)["tests"], 1)

    def test_concurrent_decisions_cannot_spend_the_same_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = f"{CONFIRMATORY_SCOPE_VERSION}:equity"
            barrier = threading.Barrier(2)

            def record(test_id):
                barrier.wait()
                return ledger.record_fdr_decision(scope, test_id, 1.0)

            with ThreadPoolExecutor(max_workers=2) as pool:
                rows = list(pool.map(record, ("proof-a", "proof-b")))
            self.assertEqual(sorted(row["tests"] for row in rows), [1, 2])
            self.assertEqual(
                sorted(round(row["allocated_alpha"], 12) for row in rows),
                sorted((round(.05 / 2, 12), round(.05 / 6, 12))),
            )
            self.assertEqual(ledger.fdr_state(scope)["tests"], 2)

    def test_offline_deferral_is_explicit_and_non_authorizing(self):
        record = deferred_fdr(f"{CONFIRMATORY_SCOPE_VERSION}:equity", "candidate-a")
        self.assertFalse(record["required"])
        self.assertFalse(record["tested"])
        self.assertFalse(record["decision"])
        self.assertEqual(record["status"], "deferred_to_live_shadow")
        self.assertEqual(record["method"], "deferred_confirmatory_raw_p_v5")
        self.assertEqual(record["p_value_kind"], "raw_confirmatory")
        self.assertEqual(record["online_method"], FDR_METHOD)
        self.assertNotIn("p_value", record)
        self.assertNotIn("allocated_alpha", record)

    def test_legacy_scope_remains_auditable_without_being_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = "shadow-confirmation-v2:equity"
            preview = ledger.next_fdr_allocation(scope)
            self.assertEqual(preview["method"], LEGACY_FDR_METHOD)
            self.assertEqual(preview["p_value_kind"], "legacy_q")
            self.assertEqual(ledger.fdr_state(scope)["tests"], 0)

    def test_lord_plus_plus_pins_initial_and_discovery_rewards(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = f"{CONFIRMATORY_SCOPE_VERSION}:equity"

            pre = ledger.next_fdr_allocation(scope)
            self.assertAlmostEqual(pre["allocated_alpha"], .05 / 2)
            self.assertAlmostEqual(pre["initial_wealth"], .05)
            self.assertEqual(pre["initial_wealth_fraction"],
                             FDR_INITIAL_WEALTH_FRACTION)
            self.assertEqual(pre["gamma_method"], FDR_GAMMA_METHOD)
            self.assertAlmostEqual(pre["first_discovery_reward"], 0.0)
            self.assertAlmostEqual(pre["subsequent_discovery_reward"], .05)

            first = ledger.record_fdr_decision(scope, "first", .001)
            self.assertTrue(first["decision"])
            self.assertAlmostEqual(first["allocated_alpha"], .05 / 2)

            # The first-discovery stream is alpha-W0=0, leaving only the
            # base gamma_2 allocation at the next test.
            second = ledger.record_fdr_decision(scope, "between", 1.0)
            self.assertFalse(second["decision"])
            self.assertAlmostEqual(second["allocated_alpha"], .05 / 6)

            # A second discovery unlocks the alpha reward stream.  At test 4
            # it contributes alpha*gamma_1 to the base alpha*gamma_4.
            third = ledger.record_fdr_decision(scope, "second", .001)
            self.assertTrue(third["decision"])
            fourth = ledger.next_fdr_allocation(scope)
            self.assertAlmostEqual(fourth["allocated_alpha"], .05 / 20 + .05 / 2)
            self.assertEqual(fourth["method"], FDR_METHOD)
            self.assertEqual(fourth["method_version"], "v5")
            self.assertEqual(fourth["algorithm"], "LORD++")

    def test_v4_method_identity_is_preserved_and_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = "shadow-confirmation-v4:equity"
            first = ledger.record_fdr_decision(scope, "old", .001)
            self.assertEqual(first["method"], LEGACY_RAW_FDR_METHOD)
            self.assertEqual(first["method_version"], "v3")
            # This is the old balanced raw-p reward, retained for audit only.
            later = ledger.next_fdr_allocation(scope)
            self.assertAlmostEqual(later["allocated_alpha"], .05 / 6 + .025)
            self.assertEqual(ledger.fdr_state(scope)["method"], LEGACY_RAW_FDR_METHOD)

    def test_pre_method_schema_migrates_without_rewriting_legacy_allocations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE factory_fdr (
                        decision_id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL,
                        test_id TEXT NOT NULL,
                        p_value REAL NOT NULL,
                        alpha REAL NOT NULL,
                        allocated_alpha REAL NOT NULL,
                        decision INTEGER NOT NULL,
                        created_at REAL NOT NULL,
                        UNIQUE(scope,test_id)
                    );
                    INSERT INTO factory_fdr VALUES
                        ('legacy-a','global','proof-a',0.001,0.05,0.025,1,1.0);
                """)

            ledger = FactoryLedger(path)
            state = ledger.fdr_state("global")
            self.assertEqual(state["method"], LEGACY_RAW_FDR_METHOD)
            self.assertEqual(state["method_version"], "v3")
            self.assertAlmostEqual(
                state["next_allocated_alpha"], .05 / 6 + .05 / 2)
            with sqlite3.connect(path) as db:
                row = db.execute(
                    "SELECT method,method_version FROM factory_fdr"
                ).fetchone()
            self.assertEqual(row, (LEGACY_RAW_FDR_METHOD, "v3"))

    def test_unknown_versioned_scope_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            with self.assertRaisesRegex(FactoryError, "unsupported"):
                ledger.next_fdr_allocation("shadow-confirmation-v6:equity")

    def test_qualification_claim_survives_restart_and_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            ledger = FactoryLedger(path)
            hypothesis = initial_hypotheses(1)[0]
            ledger.register(hypothesis)
            first = ledger.claim_qualification(
                "cycle-a", hypothesis.hypothesis_id, vehicle="equity",
                variant_id="variant-a", sessions=["2026-01-02", "2026-01-03"])
            self.assertEqual(first["sessions"], ["2026-01-02", "2026-01-03"])
            # A new ledger instance sees the durable claim; it is not an
            # in-memory sealed-window convention that a crash can erase.
            restarted = FactoryLedger(path)
            self.assertEqual(restarted.evidence_sessions("equity"),
                             {"2026-01-02", "2026-01-03"})
            with self.assertRaises(FactoryError):
                restarted.claim_qualification(
                    "cycle-b", hypothesis.hypothesis_id, vehicle="equity",
                    variant_id="variant-b", sessions=["2026-01-03", "2026-01-04"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
