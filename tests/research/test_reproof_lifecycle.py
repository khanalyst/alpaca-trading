from pathlib import Path
import tempfile
import unittest

from research.edge_lab import EdgeLedger
from tests.research.test_edge_discovery import _persist_gate


class ReproofLifecycleTests(unittest.TestCase):
    def test_demoted_candidate_uses_latest_shadow_proof_not_latest_other_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="reproof lane binding",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]

            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")
            ledger.transition(candidate_id, "validated", reason="validated proof")
            ledger.transition(candidate_id, "demoted", reason="paper safety guard")

            # Reproof is authorized by the newer shadow evidence even if an
            # unrelated diagnostic backtest was persisted afterward.
            _persist_gate(ledger, candidate_id, "shadow")
            _persist_gate(ledger, candidate_id, "backtest")
            result = ledger.transition(
                candidate_id, "shadow", reason="strictly later shadow reproof")
            self.assertEqual(result["status"], "shadow")


if __name__ == "__main__":
    unittest.main()
