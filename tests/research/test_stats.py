import unittest

from research.stats import (
    deterministic_dependence_map,
    effective_breadth_report,
    moving_block_cluster_bootstrap_lower_bound,
)
from research.factory_ledger import dependence_policy_digest


class ResearchStatisticsTests(unittest.TestCase):
    def test_semantic_policy_digest_excludes_freeze_audit_identity(self):
        base = {"schema": "dependence-policy.v1", "version": 1,
                "vehicle": "equity", "target_cycle_id": "random-a",
                "cutoff": 10.0, "source_cycles": ["prior-a", "prior-b"],
                "cluster_map": {"a": "dependence-0001"},
                "evidence": {"minimum_prior_cycles": 2,
                             "minimum_complete_sessions": 20,
                             "correlation_threshold": .8}}
        changed_audit = {**base, "target_cycle_id": "random-b", "cutoff": 99.0}
        self.assertEqual(dependence_policy_digest(base),
                         dependence_policy_digest(changed_audit))
        changed_semantics = {**base, "evidence": {
            **base["evidence"], "correlation_threshold": .9}}
        self.assertNotEqual(dependence_policy_digest(base),
                            dependence_policy_digest(changed_semantics))

    def test_dependence_map_requires_prior_cycles_and_uses_sorted_union_find(self):
        rows = []
        for cycle in ("cycle-b", "cycle-a"):
            for session in range(20):
                value = float(session + 1)
                rows.extend([
                    {"cycle_id": cycle, "family": "zeta", "session": str(session),
                     "delta": value},
                    {"cycle_id": cycle, "family": "alpha", "session": str(session),
                     "delta": 2 * value},
                ])
        report = deterministic_dependence_map(rows)
        self.assertTrue(report["available"])
        self.assertEqual(report["source_cycles"], ["cycle-a", "cycle-b"])
        self.assertEqual(report["cluster_map"], {
            "alpha": "dependence-0001", "zeta": "dependence-0001"})
        self.assertEqual(report["pairwise"][0]["complete_sessions"], 40)

    def test_dependence_map_does_not_use_a_single_current_cycle(self):
        rows = [{"cycle_id": "only", "family": family, "session": str(index),
                 "delta": float(index + 1)}
                for family in ("a", "b") for index in range(100)]
        report = deterministic_dependence_map(rows)
        self.assertFalse(report["available"])
        self.assertEqual(report["reason"], "insufficient_prior_cycles")
        self.assertEqual(report["cluster_map"], {})

    def test_moving_block_bootstrap_is_reproducible_and_records_design(self):
        deltas = [0.2, 0.4, 0.1, 0.7, 0.5, 0.3]
        clusters = ["s1", "s2", "s3", "s4", "s5", "s6"]
        first = moving_block_cluster_bootstrap_lower_bound(
            deltas, clusters, draws=300, block_length=2, seed=17)
        second = moving_block_cluster_bootstrap_lower_bound(
            deltas, clusters, draws=300, block_length=2, seed=17)
        self.assertEqual(first, second)
        self.assertTrue(first["available"])
        self.assertEqual(first["method"], "moving_block_cluster_bootstrap")
        self.assertEqual(first["seed"], 17)
        self.assertEqual(first["draws"], 300)
        self.assertEqual(first["block_length"], 2)
        self.assertEqual(first["clusters"], 6)
        self.assertEqual(first["observations"], 6)

    def test_moving_blocks_widen_bound_for_serial_dependence(self):
        # A long, alternating regime creates a dependence-sensitive mean.  A
        # block bootstrap retains regimes, while iid cluster resampling breaks
        # them up and produces a narrower lower-tail distribution.
        deltas = ([0.0] * 8) + ([1.0] * 8)
        clusters = list(range(len(deltas)))
        iid = moving_block_cluster_bootstrap_lower_bound(
            deltas, clusters, draws=2_000, block_length=1, seed=29)
        serial = moving_block_cluster_bootstrap_lower_bound(
            deltas, clusters, draws=2_000, block_length=8, seed=29)
        self.assertTrue(iid["available"] and serial["available"])
        self.assertLessEqual(serial["lower_bound"], iid["lower_bound"])
        self.assertGreaterEqual(
            serial["replicate_max"] - serial["replicate_min"],
            iid["replicate_max"] - iid["replicate_min"])

    def test_insufficient_clusters_is_unavailable(self):
        report = moving_block_cluster_bootstrap_lower_bound(
            [0.1, 0.2], ["only-session", "only-session"],
            draws=50, block_length=2, seed=5)
        self.assertFalse(report["available"])
        self.assertEqual(report["reason"], "insufficient_clusters")
        self.assertIsNone(report["lower_bound"])
        self.assertEqual(report["clusters"], 1)
        self.assertEqual(report["observations"], 2)

    def test_full_series_block_is_unavailable_not_a_point_confidence_bound(self):
        # With one block covering the entire circular series, every replicate
        # contains each cluster exactly once and the old implementation
        # returned the observed mean as both confidence quantiles.
        for cluster_count in range(2, 6):
            with self.subTest(cluster_count=cluster_count):
                report = moving_block_cluster_bootstrap_lower_bound(
                    [float(index + 1) for index in range(cluster_count)],
                    list(range(cluster_count)), draws=50, block_length=5,
                    seed=5)
                self.assertFalse(report["available"])
                self.assertEqual(report["reason"],
                                 "block_length_ge_cluster_count")
                self.assertIsNone(report["lower_bound"])
                self.assertIsNone(report["upper_bound"])
                self.assertNotIn("replicate_min", report)
                self.assertNotIn("replicate_max", report)
                self.assertEqual(report["block_length"], 5)

    def test_effective_breadth_perfect_correlation_is_one(self):
        rows = []
        for session in range(8):
            value = float(session * 2 + 1)
            rows.extend([
                {"session": session, "symbol": "A", "delta": value},
                {"session": session, "symbol": "B", "delta": 3 * value + 2},
            ])
        report = effective_breadth_report(rows)
        self.assertTrue(report["available"])
        self.assertAlmostEqual(report["effective_breadth"], 1.0)
        self.assertAlmostEqual(report["concentration"], 1.0)

    def test_effective_breadth_orthogonal_symbols_exceeds_one(self):
        rows = []
        values = [(1, 0, 0), (0, 1, 0), (0, 0, 1),
                  (-1, 0, 0), (0, -1, 0), (0, 0, -1)]
        for session, triple in enumerate(values):
            for symbol, value in zip(("A", "B", "C"), triple):
                rows.append((session, symbol, value))
        report = effective_breadth_report(rows)
        self.assertTrue(report["available"])
        self.assertGreater(report["effective_breadth"], 1.0)
        self.assertLessEqual(report["effective_breadth"], 3.0)

    def test_effective_breadth_reports_missing_data_and_sparse_unavailable(self):
        rows = [
            ("s1", "A", 1.0), ("s1", "B", 2.0),
            ("s2", "A", 2.0),
        ]
        report = effective_breadth_report(rows)
        self.assertFalse(report["available"])
        self.assertEqual(report["reason"], "insufficient_complete_sessions")
        self.assertEqual(report["missing_cells"], 1)
        self.assertEqual(report["missing_data"]["missing_cells"], 1)
        self.assertEqual(report["complete_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
