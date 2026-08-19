"""Evidence from a superseded replay engine cannot promote anything.

A run records what a candidate did *under a particular replay engine*.  When
that engine is corrected — point-in-time option entry pricing, bounded and
session-scoped quote ages, per-signal bar adjacency, runtime portfolio limits,
contiguous walk-forward folds, post-selection qualification, a cumulative
false-discovery budget — every number measured under the old one describes a
market that does not exist.  Re-hashing such a run still verifies; that is the
point of quarantining on the engine rather than on the digest.

Quarantine is not deletion.  The rows stay readable and the lifecycle history
stays intact; the run simply cannot authorize deployment, so re-deriving under
the current engine is the only route back to ``validated``.
"""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from research.edge_lab import EdgeLedger
from research.edge_ledger_proof import run_engine_epoch, run_engine_epoch_current
from research.edge_ledger_store import REPLAY_ENGINE_EPOCH
from tests.research.test_edge_discovery import _persist_gate


def superseded_engine():
    """Advance the engine so already-recorded runs become previous-epoch.

    Persisted runs are immutable by trigger, which is exactly right: evidence
    is never edited to make a point.  Moving the engine forward instead is also
    the real sequence — a replay correction lands, the constant is bumped, and
    every run recorded before it is stale from that moment on.
    """
    return mock.patch("research.edge_ledger_store.REPLAY_ENGINE_EPOCH",
                      REPLAY_ENGINE_EPOCH + 1)


class ReplayEngineEpochTests(unittest.TestCase):
    def test_epoch_four_evidence_is_legacy_readable_but_quarantined(self):
        # Legacy rows are intentionally interpreted, not rewritten.  The
        # epoch-4 payload remains auditable while the current engine (epoch 5)
        # refuses to treat it as authorizing evidence.
        legacy = {"metrics": {"replay_engine_epoch": 4}}
        self.assertEqual(run_engine_epoch(legacy), 4)
        self.assertFalse(run_engine_epoch_current(legacy))

    def test_epoch_five_is_the_current_authorizing_generation(self):
        current = {"metrics": {"replay_engine_epoch": 5}}
        self.assertEqual(REPLAY_ENGINE_EPOCH, 5)
        self.assertEqual(run_engine_epoch(current), 5)
        self.assertTrue(run_engine_epoch_current(current))

    def test_future_epoch_is_quarantined_until_this_verifier_understands_it(self):
        future = {"metrics": {
            "replay_engine_epoch": REPLAY_ENGINE_EPOCH + 1}}
        self.assertEqual(run_engine_epoch(future), REPLAY_ENGINE_EPOCH + 1)
        self.assertFalse(run_engine_epoch_current(future))

    def test_epoch_four_run_can_be_read_then_reproved_under_epoch_five(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="epoch reproof",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]

            # Simulate an immutable run written before the epoch-5 bump.  The
            # run remains in the ledger and readable, but it cannot advance
            # lifecycle state under the current engine.
            with mock.patch("research.edge_ledger.REPLAY_ENGINE_EPOCH", 4):
                stale, _ = _persist_gate(ledger, candidate_id, "backtest")
            self.assertEqual(run_engine_epoch(ledger.run(stale["run_id"])), 4)
            # Offline lifecycle bookkeeping remains readable even for a
            # legacy proof; the live authorization boundary is where epoch
            # quarantine is enforced.
            self.assertEqual(
                ledger.transition(candidate_id, "backtest_passed",
                                  reason="legacy backtest remains readable")["status"],
                "backtest_passed")
            with mock.patch("research.edge_ledger.REPLAY_ENGINE_EPOCH", 4):
                stale_shadow, _ = _persist_gate(ledger, candidate_id, "shadow")
            self.assertEqual(run_engine_epoch(stale_shadow), 4)
            self.assertEqual(
                ledger.transition(candidate_id, "shadow",
                                  reason="legacy shadow remains readable")["status"],
                "shadow")
            with self.assertRaises(ValueError):
                ledger.transition(candidate_id, "validated",
                                  reason="epoch-4 evidence is quarantined")

            # Re-deriving appends a new immutable proof stamped by epoch 5;
            # that latest proof is the one lifecycle/live authorization consumes.
            fresh, _ = _persist_gate(ledger, candidate_id, "shadow")
            self.assertEqual(run_engine_epoch(fresh), REPLAY_ENGINE_EPOCH)
            self.assertTrue(run_engine_epoch_current(fresh))
            self.assertEqual(
                ledger.transition(candidate_id, "validated",
                                  reason="epoch-5 reproof")["status"],
                "validated")

    def test_an_unstamped_run_reports_the_pre_stamp_epoch(self):
        self.assertEqual(run_engine_epoch({"metrics": {}}), 1)
        self.assertFalse(run_engine_epoch_current({"metrics": {}}))

    def test_a_forged_epoch_type_is_not_accepted_as_current(self):
        for value in (True, "2", 2.0, None, -1):
            run = {"metrics": {"replay_engine_epoch": value}}
            self.assertFalse(run_engine_epoch_current(run), value)

    def test_a_run_written_now_carries_the_current_engine_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="epoch stamp",
                config={"strategy": {"target_r": 1.5}})
            _persist_gate(ledger, candidate["candidate_id"], "shadow")
            run = ledger.latest_verified_run(candidate["candidate_id"],
                                             lane="shadow")
            self.assertEqual(run_engine_epoch(run), REPLAY_ENGINE_EPOCH)
            self.assertTrue(run_engine_epoch_current(run))

    def test_a_caller_cannot_claim_an_engine_it_did_not_run_under(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="epoch forge",
                config={"strategy": {"target_r": 1.5}})
            run = ledger.append_run(
                candidate["candidate_id"], lane="backtest", vehicle="equity",
                metrics={"replay_engine_epoch": REPLAY_ENGINE_EPOCH + 99})
            stored = next(item for item in ledger.runs(candidate["candidate_id"])
                          if item["run_id"] == run["run_id"])
            # The stamp is assigned by the ledger, never accepted from metrics.
            self.assertEqual(run_engine_epoch(stored), REPLAY_ENGINE_EPOCH)

    def test_pre_epoch_evidence_cannot_promote_a_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="quarantine",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest")
            _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")

            with superseded_engine(), self.assertRaises(ValueError):
                ledger.transition(candidate_id, "validated",
                                  reason="legacy evidence must not promote")
            # The candidate keeps the state it legitimately reached; only the
            # promotion the stale engine would have authorized is refused.
            self.assertEqual(ledger.candidate(candidate_id)["status"], "shadow")

    def test_quarantined_evidence_is_named_rather_than_silently_ineligible(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="reporting",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest")
            _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")
            ledger.transition(candidate_id, "validated", reason="validated")

            fresh = ledger.eligibility(candidate_id)
            self.assertTrue(fresh["eligible"])
            self.assertFalse(fresh["quarantined"])
            self.assertEqual(fresh["replay_engine_epoch"], REPLAY_ENGINE_EPOCH)

            with superseded_engine():
                stale = ledger.eligibility(candidate_id)
            self.assertFalse(stale["eligible"])
            self.assertTrue(stale["quarantined"])
            self.assertFalse(stale["engine_epoch_current"])
            self.assertEqual(stale["replay_engine_epoch"], REPLAY_ENGINE_EPOCH)

    def test_a_quarantined_candidate_cannot_be_selected_as_champion(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="champion",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest")
            _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")
            ledger.transition(candidate_id, "validated", reason="validated")

            self.assertIsNotNone(ledger.select_champion(vehicle="equity"))
            with superseded_engine():
                self.assertIsNone(ledger.select_champion(vehicle="equity"))


if __name__ == "__main__":
    unittest.main()
