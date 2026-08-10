"""Portfolio allocation across concurrently proved edges."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.allocation import (CORRELATION_THRESHOLD, MIN_OVERLAP_SESSIONS,
                              UNKNOWN_CORRELATION, allocate, correlation,
                              evidence_rank, heldout_sessions)
from agent.edge import resolve_validated_variants
from agent.engine_cycle import EngineCycleMixin
from research.edge_lab import EdgeLedger

SESSIONS = [f"2024-03-{day:02d}" for day in range(1, 13)]


def _gate(*, lcb, delta=None, drawdown=100.0, trades=40, q_value=.001):
    return {"passes": True,
            "statistics": {"q_value": q_value},
            "performance": {"heldout_delta_lcb": lcb,
                            "heldout_delta": delta if delta is not None else lcb + .05,
                            "max_drawdown": drawdown},
            "counts": {"heldout": {"trades": trades}}}


class _Stub(EngineCycleMixin):
    """The cycle's allocation seam without a broker or runtime state."""

    mode = "paper"
    _edge_selection_mode = "all_proved"

    def __init__(self, db_path):
        self._edge_db_path = str(db_path)
        self.events = []

    def _event(self, name, payload):
        self.events.append((name, dict(payload)))


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "edges.sqlite3"
        self.ledger = EdgeLedger(self.db)
        self.proofs = {}

    def _prove(self, variant_id, family, *, lcb, returns=None, drawdown=100.0,
               trades=40):
        record = self.ledger.register_candidate(
            variant_id, strategy_id="rule", vehicle="equity", hypothesis=family,
            config={"strategy": {"rule_spec": {"family": family}}})
        candidate_id = record["candidate_id"]
        rows = [{"session_date": session} for session in SESSIONS]
        run = self.ledger.append_run(candidate_id, lane="shadow",
                                     vehicle="equity", heldout=rows)
        for session, value in zip(SESSIONS, returns or []):
            self.ledger.append_trade(run["run_id"], {
                "session_date": session, "net_pnl": value,
                "r_multiple": value, "vehicle": "equity"})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE candidate_state SET status='validated' WHERE candidate_id=?",
                       (candidate_id,))
        self.proofs[candidate_id] = {
            "lane": "shadow", "run_id": run["run_id"],
            "config_hash": record["config_hash"],
            "heldout_start": SESSIONS[0], "heldout_end": SESSIONS[-1],
            "metrics": {"confidence": .999},
            "verified_gate": _gate(lcb=lcb, drawdown=drawdown, trades=trades)}
        return self.ledger.candidate(candidate_id)

    def _verified(self):
        return patch.object(
            EdgeLedger, "latest_verified_run",
            side_effect=lambda candidate_id, lane=None: self.proofs.get(candidate_id))

    def _allocate(self, records, free_slots):
        with self._verified():
            return allocate(records, free_slots=free_slots, db_path=self.db)

    def _resolved(self):
        with self._verified():
            return resolve_validated_variants(
                {"strategy": {"id": "rule", "execution_mode": "shares",
                              "selection_mode": "all_proved"}}, db_path=self.db)

    # --- ranking -----------------------------------------------------------

    def test_stronger_later_sorting_family_ranks_ahead_of_weaker_earlier_name(self):
        """The exact defect: 'm' before 'v' is not an allocation decision."""
        self._prove("rule.mr.0000000000000001", "mean_reversion", lcb=.02)
        self._prove("rule.vb.0000000000000001", "volume_breakout", lcb=.20)
        self.assertEqual([row["variant_id"] for row in self._resolved()],
                         ["rule.vb.0000000000000001", "rule.mr.0000000000000001"])
        allocated = self._allocate(self._resolved(), 1)
        self.assertEqual([row["variant_id"] for row in allocated["admitted"]],
                         ["rule.vb.0000000000000001"])

    def test_ranking_reuses_the_champion_ordering_and_never_the_point_estimate(self):
        noisy = {"latest_proof": {"verified_gate": _gate(lcb=.01, delta=5.0)}}
        supported = {"latest_proof": {"verified_gate": _gate(lcb=.10, delta=.11)}}
        self.assertGreater(evidence_rank(supported), evidence_rank(noisy))
        shallower = {"latest_proof": {"verified_gate": _gate(lcb=.10, drawdown=10.0)}}
        deeper = {"latest_proof": {"verified_gate": _gate(lcb=.10, drawdown=90.0)}}
        self.assertGreater(evidence_rank(shallower), evidence_rank(deeper))
        self.assertGreater(evidence_rank(noisy),
                           evidence_rank({"latest_proof": {"verified_gate": {}}}))

    def test_edge_proof_score_is_the_single_ranking_notion(self):
        from agent import edge
        self.assertIs(edge._proof_score, evidence_rank)

    # --- correlation -------------------------------------------------------

    def test_correlated_candidates_cannot_both_consume_a_slot(self):
        series = [1.0, -.5, .8, -.2, 1.4, -.9, .3, .6, -.4, 1.1, .2, -.7]
        self._prove("rule.aa.0000000000000001", "alpha", lcb=.20, returns=series)
        self._prove("rule.bb.0000000000000001", "beta", lcb=.10,
                    returns=[value * 2 + .01 for value in series])
        allocated = self._allocate(self._resolved(), 3)
        self.assertEqual([row["variant_id"] for row in allocated["admitted"]],
                         ["rule.aa.0000000000000001"])
        rejected = allocated["rejected"][0]
        self.assertEqual(rejected["variant_id"], "rule.bb.0000000000000001")
        self.assertEqual(rejected["reason"], "correlated with an admitted candidate")
        self.assertEqual(rejected["correlated_with"], "rule.aa.0000000000000001")
        self.assertGreaterEqual(rejected["correlation"], CORRELATION_THRESHOLD)

    def test_uncorrelated_candidates_are_both_admitted(self):
        left = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
        right = [1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, -1.0]
        self.assertLess(abs(correlation(dict(zip(SESSIONS, left)),
                                        dict(zip(SESSIONS, right)))),
                        CORRELATION_THRESHOLD)
        self._prove("rule.aa.0000000000000001", "alpha", lcb=.20, returns=left)
        self._prove("rule.bb.0000000000000001", "beta", lcb=.10, returns=right)
        allocated = self._allocate(self._resolved(), 3)
        self.assertEqual([row["variant_id"] for row in allocated["admitted"]],
                         ["rule.aa.0000000000000001", "rule.bb.0000000000000001"])
        self.assertEqual(allocated["rejected"], [])

    def test_insufficient_overlap_is_treated_as_maximal_correlation(self):
        short = dict(zip(SESSIONS, [1.0, -1.0, 1.0, -1.0]))
        self.assertLess(len(short), MIN_OVERLAP_SESSIONS)
        self.assertEqual(correlation(short, short), UNKNOWN_CORRELATION)
        self.assertEqual(correlation({}, {}), UNKNOWN_CORRELATION)
        flat = dict(zip(SESSIONS, [1.0] * len(SESSIONS)))
        self.assertEqual(correlation(flat, flat), UNKNOWN_CORRELATION)

    def test_candidates_without_persisted_sessions_are_assumed_correlated(self):
        self._prove("rule.aa.0000000000000001", "alpha", lcb=.20)
        self._prove("rule.bb.0000000000000001", "beta", lcb=.10)
        allocated = self._allocate(self._resolved(), 3)
        self.assertEqual([row["variant_id"] for row in allocated["admitted"]],
                         ["rule.aa.0000000000000001"])
        self.assertEqual(allocated["rejected"][0]["reason"],
                         "correlated with an admitted candidate")

    def test_negative_correlation_is_not_traded_as_a_hedge(self):
        series = [1.0, -.5, .8, -.2, 1.4, -.9, .3, .6, -.4, 1.1, .2, -.7]
        inverse = [-value for value in series]
        self.assertEqual(correlation(dict(zip(SESSIONS, series)),
                                     dict(zip(SESSIONS, inverse))), 0.0)

    def test_sessions_are_read_from_the_proving_run_heldout_window(self):
        record = self._prove("rule.aa.0000000000000001", "alpha", lcb=.20,
                             returns=[1.0] * 12)
        record = dict(record)
        with self._verified():
            sessions = heldout_sessions(self.ledger, record)
        self.assertEqual(sorted(sessions), SESSIONS)
        self.assertEqual(set(sessions.values()), {1.0})

    # --- caps and durability ----------------------------------------------

    def test_allocation_never_exceeds_free_concurrent_position_slots(self):
        for index in range(4):
            self._prove(f"rule.f{index}.0000000000000001", f"family{index}",
                        lcb=.20 - index * .01,
                        returns=[float((index + 1) * (step % 3) - step % 2)
                                 for step in range(12)])
        records = self._resolved()
        for free in (0, 1, 2, 5):
            with self.subTest(free_slots=free):
                allocated = self._allocate(records, free)
                self.assertLessEqual(len(allocated["admitted"]), max(0, free))
                self.assertEqual(
                    len(allocated["admitted"]) + len(allocated["rejected"]),
                    len(records))
        exhausted = self._allocate(records, -3)
        self.assertEqual(exhausted["admitted"], [])

    def test_allocation_only_narrows_so_no_downstream_cap_can_be_relaxed(self):
        """Admissions are input identities, never new or widened candidates.

        Open-risk and notional caps are enforced per order in the cycle and are
        untouched by allocation; the allocator can only remove or reorder
        candidates, so any admitted set still faces every check the
        first-come-first-served path applied.
        """
        for index in range(4):
            self._prove(f"rule.f{index}.0000000000000001", f"family{index}",
                        lcb=.20 - index * .01,
                        returns=[float((step * (index + 1)) % 5) for step in range(12)])
        records = self._resolved()
        allocated = self._allocate(records, 4)
        for record in allocated["admitted"]:
            self.assertTrue(any(record is item for item in records))

    def test_every_rejection_carries_a_durable_reason(self):
        for index in range(3):
            self._prove(f"rule.f{index}.0000000000000001", f"family{index}",
                        lcb=.20 - index * .01)
        allocated = self._allocate(self._resolved(), 1)
        self.assertTrue(allocated["rejected"])
        for row in allocated["rejected"]:
            self.assertTrue(str(row.get("reason") or "").strip())
            self.assertTrue(row.get("candidate_id"))
            self.assertTrue(row.get("variant_id"))

    def test_selection_is_deterministic_across_repeated_allocation(self):
        for index in range(4):
            self._prove(f"rule.f{index}.0000000000000001", f"family{index}",
                        lcb=.2, drawdown=10.0 + index,
                        returns=[float(step % (index + 2)) for step in range(12)])
        records = self._resolved()
        first = self._allocate(records, 3)
        for _ in range(3):
            again = self._allocate(list(reversed(records)), 3)
            self.assertEqual([row["variant_id"] for row in again["admitted"]],
                             [row["variant_id"] for row in first["admitted"]])
            self.assertEqual([row["variant_id"] for row in again["rejected"]],
                             [row["variant_id"] for row in first["rejected"]])

    # --- cycle seam --------------------------------------------------------

    def test_cycle_allocation_bounds_admissions_by_open_and_pending_exposure(self):
        left = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
        right = [1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, -1.0]
        self._prove("rule.aa.0000000000000001", "alpha", lcb=.20, returns=left)
        self._prove("rule.bb.0000000000000001", "beta", lcb=.10, returns=right)
        configs = [(record, {"strategy": {}}) for record in self._resolved()]
        engine = _Stub(self.db)
        with self._verified():
            admitted = engine._allocate_edges(configs, free_slots=1)
        self.assertEqual([record["variant_id"] for record, _cfg in admitted],
                         ["rule.aa.0000000000000001"])
        rejects = [payload for name, payload in engine.events
                   if name == "allocation_reject"]
        self.assertEqual(rejects[0]["reason"], "no free concurrent position slot")
        self.assertTrue(any(name == "allocation" for name, _p in engine.events))
        self.assertTrue(all(item in configs for item in admitted))

    def test_specific_selection_and_live_never_reach_the_allocator(self):
        self._prove("rule.aa.0000000000000001", "alpha", lcb=.20)
        self._prove("rule.bb.0000000000000001", "beta", lcb=.10)
        configs = [(record, {"strategy": {}}) for record in self._resolved()]
        for attribute, value in (("_edge_selection_mode", "specific"),
                                 ("mode", "live")):
            with self.subTest(attribute=attribute):
                engine = _Stub(self.db)
                setattr(engine, attribute, value)
                self.assertEqual(engine._allocate_edges(configs, free_slots=1),
                                 configs)
                self.assertEqual(engine.events, [])
        engine = _Stub(self.db)
        self.assertEqual(engine._allocate_edges(configs[:1], free_slots=1),
                         configs[:1])
        unresolved = [(None, {"strategy": {}}), (None, {"strategy": {}})]
        self.assertEqual(engine._allocate_edges(unresolved, free_slots=2),
                         unresolved)

    def test_unreadable_evidence_keeps_only_the_best_ranked_edge(self):
        self._prove("rule.aa.0000000000000001", "alpha", lcb=.20)
        self._prove("rule.bb.0000000000000001", "beta", lcb=.10)
        configs = [(record, {"strategy": {}}) for record in self._resolved()]
        engine = _Stub(self.db)
        with patch("agent.allocation.allocate", side_effect=RuntimeError("ledger gone")):
            admitted = engine._allocate_edges(configs, free_slots=3)
        self.assertEqual([record["variant_id"] for record, _cfg in admitted],
                         ["rule.aa.0000000000000001"])
        self.assertTrue(any(name == "allocation_failed" for name, _p in engine.events))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
