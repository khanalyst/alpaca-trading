"""Durable confirmatory false-discovery budget tests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from research.factory_ledger import (
    CONFIRMATORY_SCOPE_VERSION, CONFIRMATORY_SCOPE_VERSION_V5,
    FDR_METHOD, FDR_METHOD_V5, FDR_GAMMA_METHOD,
    FDR_INITIAL_WEALTH_FRACTION, FDR_INITIAL_WEALTH_FRACTION_V5,
    LEGACY_FDR_METHOD, LEGACY_RAW_FDR_METHOD,
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
            self.assertAlmostEqual(first["allocated_alpha"], .0125)
            self.assertEqual(ledger.fdr_state(scope)["tests"], 0)

            expected = (.0125, .025 / 6, .025 / 12, .025 / 20)
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

            # LORD++ keeps the base stream at W0*gamma_2.  With v6's
            # preregistered W0=alpha/2, the first-discovery reward is alpha/2.
            preview = ledger.next_fdr_allocation(scope)
            self.assertAlmostEqual(preview["allocated_alpha"], .025 / 6 + .025 / 2)
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
            self.assertAlmostEqual(first["allocated_alpha"], .0125)
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
                sorted((round(.025 / 2, 12), round(.025 / 6, 12))),
            )
            self.assertEqual(ledger.fdr_state(scope)["tests"], 2)

    def test_offline_deferral_is_explicit_and_non_authorizing(self):
        record = deferred_fdr(f"{CONFIRMATORY_SCOPE_VERSION}:equity", "candidate-a")
        self.assertFalse(record["required"])
        self.assertFalse(record["tested"])
        self.assertFalse(record["decision"])
        self.assertEqual(record["status"], "deferred_to_live_shadow")
        self.assertEqual(record["method"], "deferred_confirmatory_raw_p_v6")
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
            self.assertAlmostEqual(pre["allocated_alpha"], .025 / 2)
            self.assertAlmostEqual(pre["initial_wealth"], .025)
            self.assertEqual(pre["initial_wealth_fraction"],
                             FDR_INITIAL_WEALTH_FRACTION)
            self.assertEqual(pre["gamma_method"], FDR_GAMMA_METHOD)
            self.assertAlmostEqual(pre["first_discovery_reward"], .025)
            self.assertAlmostEqual(pre["subsequent_discovery_reward"], .05)

            first = ledger.record_fdr_decision(scope, "first", .001)
            self.assertTrue(first["decision"])
            self.assertAlmostEqual(first["allocated_alpha"], .025 / 2)

            # The first-discovery stream contributes (alpha/2)*gamma_1 to
            # the base W0*gamma_2 allocation at the next test.
            second = ledger.record_fdr_decision(scope, "between", 1.0)
            self.assertFalse(second["decision"])
            self.assertAlmostEqual(second["allocated_alpha"], .025 / 6 + .025 / 2)

            # A second discovery unlocks the full-alpha reward stream.  At
            # test 4 both reward streams are visible alongside the base.
            third = ledger.record_fdr_decision(scope, "second", .001)
            self.assertTrue(third["decision"])
            fourth = ledger.next_fdr_allocation(scope)
            self.assertAlmostEqual(fourth["allocated_alpha"], .025 / 20 + .025 / 12 + .05 / 2)
            self.assertEqual(fourth["method"], FDR_METHOD)
            self.assertEqual(fourth["method_version"], "v6")
            self.assertEqual(fourth["algorithm"], "LORD++")

    def test_v5_replay_keeps_w0_alpha_while_v6_starts_w0_alpha_half(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            old_scope = f"{CONFIRMATORY_SCOPE_VERSION_V5}:equity"
            new_scope = f"{CONFIRMATORY_SCOPE_VERSION}:equity"
            old = ledger.record_fdr_decision(old_scope, "old", .001)
            self.assertEqual(old["method"], FDR_METHOD_V5)
            self.assertEqual(old["method_version"], "v5")
            self.assertEqual(old["initial_wealth_fraction"],
                             FDR_INITIAL_WEALTH_FRACTION_V5)
            self.assertAlmostEqual(old["allocated_alpha"], .05 / 2)

            fresh = ledger.next_fdr_allocation(new_scope)
            self.assertEqual(fresh["method"], FDR_METHOD)
            self.assertEqual(fresh["method_version"], "v6")
            self.assertAlmostEqual(fresh["initial_wealth"], .025)
            self.assertAlmostEqual(fresh["allocated_alpha"], .025 / 2)
            self.assertEqual(ledger.fdr_state(old_scope)["tests"], 1)
            self.assertEqual(ledger.fdr_state(new_scope)["tests"], 0)

    def test_pre_v6_rows_with_method_columns_remain_readable_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            ledger = FactoryLedger(path)
            old_scope = f"{CONFIRMATORY_SCOPE_VERSION_V5}:equity"
            ledger.record_fdr_decision(old_scope, "old", .001)
            with sqlite3.connect(path) as db:
                before = db.execute(
                    "SELECT method,method_version,allocated_alpha FROM factory_fdr"
                ).fetchone()
            restarted = FactoryLedger(path)
            self.assertEqual(restarted.fdr_state(old_scope)["method_version"], "v5")
            with sqlite3.connect(path) as db:
                after = db.execute(
                    "SELECT method,method_version,allocated_alpha FROM factory_fdr"
                ).fetchone()
            self.assertEqual(after, before)

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
            # v7 became a registered scope (Javanmard-Montanari gamma); the
            # next unregistered version must still fail closed.
            with self.assertRaisesRegex(FactoryError, "unsupported"):
                ledger.next_fdr_allocation("shadow-confirmation-v8:equity")

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



class JavanmardMontanariScopeTests(unittest.TestCase):
    """Review finding D1-08: a slowly decaying sequence as ledger version v7."""

    @staticmethod
    def _gamma(index):
        import math
        return math.log(max(index, 2)) / (index * math.exp(math.sqrt(math.log(index))))

    def test_the_sequence_is_normalized_to_at_most_one_and_non_increasing(self):
        import math
        from research.factory_ledger import FDR_JM_GAMMA_SERIES_SUM, _jm_gamma
        count = 200_000
        partial = math.fsum(self._gamma(index) for index in range(1, count))
        root = math.sqrt(math.log(count))
        tail = 2.0 * math.exp(-root) * (root ** 3 + 3 * root ** 2 + 6 * root + 6)
        total = partial + tail + self._gamma(count) / 2
        self.assertAlmostEqual(total, 12.645107872871751, places=8)
        self.assertGreaterEqual(FDR_JM_GAMMA_SERIES_SUM, total)
        self.assertLess(FDR_JM_GAMMA_SERIES_SUM - total, 1e-9)
        values = [_jm_gamma(index) for index in range(1, 20_000)]
        self.assertTrue(all(left >= right for left, right in zip(values, values[1:])))
        self.assertAlmostEqual(values[0], math.log(2) / FDR_JM_GAMMA_SERIES_SUM)

    def test_v7_scope_spends_the_new_sequence_and_persists_its_method(self):
        from research.factory_ledger import (
            CONFIRMATORY_SCOPE_VERSION_V7, FDR_JM_GAMMA_SERIES_SUM,
            FDR_METHOD_V7, _jm_gamma)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            ledger = FactoryLedger(path)
            scope = f"{CONFIRMATORY_SCOPE_VERSION_V7}:equity"
            first = ledger.next_fdr_allocation(scope)
            self.assertEqual(first["method"], FDR_METHOD_V7)
            self.assertEqual(first["method_version"], "v7")
            self.assertEqual(first["gamma_series_sum"], FDR_JM_GAMMA_SERIES_SUM)
            self.assertAlmostEqual(first["allocated_alpha"], .025 * _jm_gamma(1))
            for index in range(1, 31):
                recorded = ledger.record_fdr_decision(scope, f"t-{index}", 1.0)
                self.assertAlmostEqual(recorded["allocated_alpha"],
                                       .025 * _jm_gamma(index))
            # A discovery starts the alpha/2 first-discovery reward stream.
            hit = ledger.record_fdr_decision(scope, "hit", 0.0)
            self.assertTrue(hit["decision"])
            after = FactoryLedger(path).next_fdr_allocation(scope)
            self.assertAlmostEqual(after["allocated_alpha"],
                                   .025 * _jm_gamma(32) + .025 * _jm_gamma(1))
            with sqlite3.connect(path) as db:
                methods = {row[0] for row in db.execute(
                    "SELECT method_version FROM factory_fdr WHERE scope=?",
                    (scope,))}
            self.assertEqual(methods, {"v7"})

    def test_v6_ledgers_keep_their_recorded_sequence(self):
        from research.factory_ledger import CONFIRMATORY_SCOPE_VERSION_V7
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            v6 = f"{CONFIRMATORY_SCOPE_VERSION}:equity"
            v7 = f"{CONFIRMATORY_SCOPE_VERSION_V7}:equity"
            for index in range(1, 6):
                ledger.record_fdr_decision(v6, f"a-{index}", 1.0)
                ledger.record_fdr_decision(v7, f"a-{index}", 1.0)
            state = ledger.fdr_state(v6)
            self.assertEqual(state["method"], FDR_METHOD)
            self.assertEqual(
                [round(row["allocated_alpha"], 12) for row in state["decisions"]],
                [round(.025 / (index * (index + 1)), 12) for index in range(1, 6)])
            self.assertAlmostEqual(state["next_allocated_alpha"], .025 / 42)

    def test_v7_is_more_lenient_than_v6_only_after_22_tests(self):
        # The protocol documents this honestly: the slowly decaying sequence
        # gives up early wealth to keep later tests passable.
        from research.factory_ledger import _fdr_gamma, _jm_gamma
        crossover = next(index for index in range(1, 1000)
                         if _jm_gamma(index) > _fdr_gamma(index))
        self.assertEqual(crossover, 23)
        self.assertGreater(_jm_gamma(100) / _fdr_gamma(100), 4.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
