"""Durable confirmatory false-discovery budget tests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from research.factory_ledger import (
    FDR_METHOD, LEGACY_FDR_METHOD, FactoryLedger, deferred_fdr)
from research.factory_core import initial_hypotheses
from research.factory_ledger import FactoryError


class FactoryFdrTests(unittest.TestCase):
    def test_balanced_allocations_are_previewed_without_being_spent(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = "shadow-confirmation-v4:equity"

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
            scope = "shadow-confirmation-v4:equity"
            first = ledger.record_fdr_decision(scope, "proof-a", .001)
            self.assertTrue(first["decision"])

            # The second base allocation is alpha/6 and the first discovery
            # starts a fresh alpha/2 reward stream.
            preview = ledger.next_fdr_allocation(scope)
            self.assertAlmostEqual(preview["allocated_alpha"], .05 / 6 + .025)
            duplicate = ledger.record_fdr_decision(scope, "proof-a", .9)
            self.assertEqual(duplicate["tests"], 1)
            self.assertEqual(duplicate["p_value"], .001)
            self.assertTrue(duplicate["decision"])
            self.assertEqual(ledger.fdr_state(scope)["tests"], 1)

    def test_scope_alpha_is_locked_by_the_first_durable_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = "shadow-confirmation-v4:equity"
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
            scope = "shadow-confirmation-v4:equity"
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
        record = deferred_fdr("shadow-confirmation-v4:equity", "candidate-a")
        self.assertFalse(record["required"])
        self.assertFalse(record["tested"])
        self.assertFalse(record["decision"])
        self.assertEqual(record["status"], "deferred_to_live_shadow")
        self.assertEqual(record["method"], "deferred_confirmatory_raw_p_v3")
        self.assertEqual(record["p_value_kind"], "raw_confirmatory")
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
