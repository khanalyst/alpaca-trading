"""End-to-end authorization tests for the research-side shadow consumer."""

from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research.edge_ledger import EdgeLedger
from research.live_shadow import ShadowStore
from research.factory_ledger import FactoryLedger
from research.live_shadow_ingest import (
    MAX_CONFIRMATORY_ITERATIONS, ShadowIngestConfig, _confirmatory_iterations,
    ingest_shadow,
)

from tests.research.test_edge_discovery import _persist_gate


class LiveShadowIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.edge_path = root / "edge.sqlite3"
        self.shadow_path = root / "shadow.sqlite3"
        self.ledger = EdgeLedger(self.edge_path)
        self.baseline = self.ledger.register_candidate(
            "ibr.baseline", strategy_id="ibr", vehicle="equity",
            hypothesis="baseline", config={"strategy": {"id": "ibr"}})
        self.candidate = self.ledger.register_candidate(
            "ibr.range.30", strategy_id="ibr", vehicle="equity",
            hypothesis="candidate", config={"strategy": {"id": "ibr"}})
        _persist_gate(self.ledger, self.candidate["candidate_id"], "backtest")
        self.ledger.transition(self.candidate["candidate_id"], "backtest_passed",
                               reason="backtest proof")
        self.store = ShadowStore(self.shadow_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, cid, values, *, status="match"):
        sessions = [f"2024-02-{day:02d}" for day in range(1, 9)]
        for session, value in zip(sessions, values):
            row = {"vehicle": "equity", "symbol": "SPY",
                   "session_date": session,
                   # Replay lanes use the stable symbol/session opportunity;
                   # candidate, baseline, and null books therefore pair on
                   # the exact same observation.
                   "opportunity_id": f"equity:SPY:{session}",
                   # A real shadow trade carries the risk unit it planned, and
                   # the gate refuses an equity result that cannot state one.
                   # 100 bps of stop against ~9 bps of round trip is an
                   # ordinary, comfortably adequate ratio.
                   "entry_price": 100.0, "stop_price": 99.0,
                   "net_pnl": float(value), "return_value": float(value)}
            digest = f"source:{cid}:{session}"
            replay = f"replay:{cid}:{session}"
            self.store.replay_diff(
                candidate_id=cid, session_date=session,
                source_digest=digest, shadow_digest=f"shadow:{cid}:{session}",
                replay_digest=replay, status=status,
                details={"complete": status == "match",
                         "signature_match": status == "match"})
            self.store.record_replay_evidence(
                candidate_id=cid, session_date=session,
                replay_digest=replay, vehicle="equity",
                starting_cash=100_000, ending_cash=100_000 + float(value),
                realized_pnl=float(value), trades=[row],
                        replay_status=status)

    def _row(self, cid, day, value, *, status="match"):
        session = f"2024-02-{day:02d}"
        row = {"vehicle": "equity", "symbol": "SPY", "session_date": session,
               "opportunity_id": f"equity:SPY:{session}",
               "entry_price": 100.0, "stop_price": 99.0,
               "net_pnl": float(value), "return_value": float(value)}
        replay = f"replay:{cid}:{session}"
        self.store.replay_diff(
            candidate_id=cid, session_date=session,
            source_digest=f"source:{cid}:{session}",
            shadow_digest=f"shadow:{cid}:{session}", replay_digest=replay,
            status=status, details={"complete": status == "match",
                                    "signature_match": status == "match"})
        self.store.record_replay_evidence(
            candidate_id=cid, session_date=session, replay_digest=replay,
            vehicle="equity", starting_cash=100_000,
            ending_cash=100_000 + float(value), realized_pnl=float(value),
            trades=[row], replay_status=status)

    def test_matched_tail_appends_shadow_proof_and_transitions(self):
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 8)
        self._rows(self.baseline["candidate_id"], [0.0] * 8)
        self._rows(f"shadow:null:{cid}", [-1.0] * 8)
        result = ingest_shadow(ShadowIngestConfig(
            self.edge_path, self.shadow_path, min_trades=1, min_sessions=1))
        self.assertEqual(result["ingested"], 1)
        row = self.ledger.candidate(cid)
        self.assertEqual(row["status"], "validated")
        runs = self.ledger.runs(cid, lane="shadow")
        self.assertEqual(len(runs), 1)
        online = runs[0]["metrics"]["gate"]["verified_gate"]["online_fdr"]
        self.assertTrue(online["required"])
        self.assertTrue(online["tested"])
        self.assertTrue(online["decision"])
        state = FactoryLedger(self.edge_path).fdr_state(
            "shadow-confirmation-v2:equity")
        self.assertEqual(state["tests"], 1)
        self.assertAlmostEqual(state["decisions"][0]["p_value"],
                               online["p_value"])
        evidence = self.ledger.evidence(cid)
        self.assertTrue(any(item["kind"] == "shadow_ingestion" for item in evidence))
        self.assertEqual(len(self.ledger.trades(cid, lane="shadow")), 8)

    def test_mismatch_and_incomplete_tails_do_not_write(self):
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 7 + [2.0], status="mismatch")
        result = ingest_shadow(ShadowIngestConfig(
            self.edge_path, self.shadow_path, min_trades=1, min_sessions=1))
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(self.ledger.runs(cid, lane="shadow"), [])
        self.assertEqual(self.ledger.candidate(cid)["status"], "backtest_passed")

    def test_repeated_ingestion_is_idempotent(self):
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 8)
        self._rows(self.baseline["candidate_id"], [0.0] * 8)
        self._rows(f"shadow:null:{cid}", [-1.0] * 8)
        config = ShadowIngestConfig(self.edge_path, self.shadow_path,
                                    min_trades=1, min_sessions=1)
        self.assertEqual(ingest_shadow(config)["ingested"], 1)
        self.assertEqual(ingest_shadow(config)["ingested"], 0)
        self.assertEqual(len(self.ledger.runs(cid, lane="shadow")), 1)

    def test_online_fdr_records_global_q_not_selected_raw_p(self):
        cid = self.candidate["candidate_id"]
        sibling = self.ledger.register_candidate(
            "ibr.range.45", strategy_id="ibr", vehicle="equity",
            hypothesis="diagnostic sibling",
            config={"strategy": {"id": "ibr"}})
        sibling_id = sibling["candidate_id"]
        _persist_gate(self.ledger, sibling_id, "backtest")
        self.ledger.transition(sibling_id, "backtest_passed",
                               reason="backtest proof")

        self._rows(cid, [2.0] * 8)
        self._rows(sibling_id, [2.0, -2.0] * 4)
        self._rows(self.baseline["candidate_id"], [0.0] * 8)
        self._rows(f"shadow:null:{cid}", [-1.0] * 8)
        self._rows(f"shadow:null:{sibling_id}", [-1.0] * 8)

        result = ingest_shadow(ShadowIngestConfig(
            self.edge_path, self.shadow_path, min_trades=1, min_sessions=1))
        self.assertEqual(result["ingested"], 1)
        run = self.ledger.runs(cid, lane="shadow")[0]
        envelope = run["metrics"]["gate"]["verified_gate"]
        raw_p = envelope["statistics"]["p_value"]
        global_q = envelope["statistics"]["q_value"]
        online_p = envelope["online_fdr"]["p_value"]
        self.assertGreater(global_q, raw_p)
        self.assertAlmostEqual(online_p, global_q)

    def test_only_strictly_newer_sessions_advance_the_boundary(self):
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 8)
        self._rows(self.baseline["candidate_id"], [0.0] * 8)
        self._rows(f"shadow:null:{cid}", [-1.0] * 8)
        config = ShadowIngestConfig(self.edge_path, self.shadow_path,
                                    min_trades=1, min_sessions=1)
        self.assertEqual(ingest_shadow(config)["ingested"], 1)
        # A late-arriving old session is diagnostic only and cannot reopen the
        # consumed window or create a duplicate proof run.
        for control, value in ((cid, 2.0), (self.baseline["candidate_id"], 0.0),
                               (f"shadow:null:{cid}", -1.0)):
            self._row(control, 1, value)
        self.assertEqual(ingest_shadow(config)["ingested"], 0)
        self.assertEqual(len(self.ledger.runs(cid, lane="shadow")), 1)

    def test_legacy_validated_candidate_recovers_without_transition(self):
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 8)
        self._rows(self.baseline["candidate_id"], [0.0] * 8)
        self._rows(f"shadow:null:{cid}", [-1.0] * 8)
        # Simulate an existing offline/legacy promotion.  It has a valid
        # historical proof but no parity marker and is therefore ineligible.
        with sqlite3.connect(self.edge_path) as db:
            db.execute("UPDATE candidate_state SET status='validated' WHERE candidate_id=?", (cid,))
        self.assertFalse(self.ledger.eligibility(cid)["eligible"])
        result = ingest_shadow(ShadowIngestConfig(
            self.edge_path, self.shadow_path, min_trades=1, min_sessions=1))
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(self.ledger.candidate(cid)["status"], "validated")
        self.assertEqual(result["candidates"][0].get("transitions"), [])
        self.assertTrue(self.ledger.eligibility(cid)["eligible"])

    def test_live_authorization_fails_closed_on_source_replay_and_gate_tamper(self):
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 8)
        self._rows(self.baseline["candidate_id"], [0.0] * 8)
        self._rows(f"shadow:null:{cid}", [-1.0] * 8)
        result = ingest_shadow(ShadowIngestConfig(
            self.edge_path, self.shadow_path, min_trades=1, min_sessions=1))
        self.assertEqual(result["ingested"], 1)
        run_id = self.ledger.runs(cid, lane="shadow")[0]["run_id"]
        # Each mutation is on its own fresh database copy so the proof remains
        # independently diagnostic and no mutation can accidentally mask the
        # next check.
        with sqlite3.connect(self.edge_path) as db:
            row = db.execute("SELECT metrics_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            metrics = json.loads(row[0])
            metrics["shadow_source"]["baseline"]["rows_digest"] = "tampered"
            db.execute("DROP TRIGGER runs_no_update")
            db.execute("UPDATE runs SET metrics_json=? WHERE run_id=?",
                       (json.dumps(metrics, sort_keys=True, separators=(",", ":")), run_id))
        self.assertFalse(self.ledger.eligibility(cid)["eligible"])

    def _seed_live_run(self):
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 8)
        self._rows(self.baseline["candidate_id"], [0.0] * 8)
        self._rows(f"shadow:null:{cid}", [-1.0] * 8)
        result = ingest_shadow(ShadowIngestConfig(
            self.edge_path, self.shadow_path, min_trades=1, min_sessions=1))
        self.assertEqual(result["ingested"], 1)
        return cid, self.ledger.runs(cid, lane="shadow")[0]["run_id"]

    def test_live_authorization_fails_closed_on_replay_digest_tamper(self):
        cid, run_id = self._seed_live_run()
        with sqlite3.connect(self.edge_path) as db:
            row = db.execute("SELECT metrics_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            metrics = json.loads(row[0])
            metrics["replay_digests"][0] = "tampered"
            db.execute("DROP TRIGGER runs_no_update")
            db.execute("UPDATE runs SET metrics_json=? WHERE run_id=?",
                       (json.dumps(metrics, sort_keys=True, separators=(",", ":")), run_id))
        self.assertFalse(self.ledger.eligibility(cid)["eligible"])

    def test_live_authorization_fails_closed_on_gate_digest_tamper(self):
        cid, run_id = self._seed_live_run()
        with sqlite3.connect(self.edge_path) as db:
            row = db.execute("""SELECT evidence_id,payload_json FROM evidence
                WHERE run_id=? AND kind='shadow_ingestion'""", (run_id,)).fetchone()
            payload = json.loads(row[1])
            payload["gate_hash"] = "tampered"
            db.execute("DROP TRIGGER evidence_no_update")
            db.execute("UPDATE evidence SET payload_json=? WHERE evidence_id=?",
                       (json.dumps(payload, sort_keys=True, separators=(",", ":")), row[0]))
        self.assertFalse(self.ledger.eligibility(cid)["eligible"])

    def test_live_authorization_fails_closed_on_config_digest_tamper(self):
        cid, run_id = self._seed_live_run()
        with sqlite3.connect(self.edge_path) as db:
            db.execute("DROP TRIGGER runs_no_update")
            db.execute("UPDATE runs SET config_hash=? WHERE run_id=?", ("tampered", run_id))
        self.assertFalse(self.ledger.eligibility(cid)["eligible"])

    def test_manual_offline_promotion_is_rejected_without_live_marker(self):
        cid = self.candidate["candidate_id"]
        _persist_gate(self.ledger, cid, "shadow")
        with sqlite3.connect(self.edge_path) as db:
            db.execute("UPDATE candidate_state SET status='shadow' WHERE candidate_id=?", (cid,))
            db.execute("DROP TRIGGER evidence_no_delete")
            db.execute("DELETE FROM evidence WHERE candidate_id=? AND kind='shadow_ingestion'", (cid,))
        with self.assertRaisesRegex(ValueError, "parity-matched live-shadow"):
            self.ledger.transition(cid, "validated", reason="manual promotion")

    def test_batch_bh_does_not_select_preflight_failed_low_p_candidate(self):
        candidate_ids = ["low", "marginal", "ready"]
        ingestor = object.__new__(type("FakeIngestor", (), {}))
        # Use a real ingestor so the tested selection path is not duplicated,
        # while replacing only its durable I/O seam.
        from research.live_shadow_ingest import ShadowIngestor
        ingestor = object.__new__(ShadowIngestor)
        ingestor.config = ShadowIngestConfig(self.edge_path, self.shadow_path)
        ingestor.store = None
        ingestor.ledger = self.ledger
        calls = []
        prepared = {
            "low": {"candidate_id": "low", "status": "prepared", "raw_p": .001,
                    "family": "rule:x", "preflight_ready": False},
            "marginal": {"candidate_id": "marginal", "status": "prepared", "raw_p": .06,
                         "family": "rule:x", "preflight_ready": True},
            "ready": {"candidate_id": "ready", "status": "prepared", "raw_p": .001,
                      "family": "rule:x", "preflight_ready": True},
        }
        def fake_one(candidate_id, *, dry=False, correction=None,
                     test_iterations=20_000):
            calls.append((candidate_id, dry, correction or {}, test_iterations))
            if dry:
                return dict(prepared[candidate_id])
            return {"candidate_id": candidate_id, "ingested": bool((correction or {}).get("selected"))}
        ingestor._candidate_ids = lambda: candidate_ids
        ingestor._one = fake_one
        result = ingestor.ingest()
        selected = [item for cid, dry, item, iterations in calls
                    if not dry and item.get("selected")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["global"]["significant"], True)
        self.assertAlmostEqual(selected[0]["global"]["p_adjusted"], .0015)
        self.assertGreater(selected[0]["global"]["p_adjusted"],
                           prepared["ready"]["raw_p"])
        self.assertEqual(result["ingested"], 1)
        by_id = {row["candidate_id"]: row for row in result["candidates"]}
        self.assertFalse(by_id["marginal"]["ingested"])
        self.assertTrue(by_id["ready"]["ingested"])

    def test_confirmatory_resolution_scales_with_batch_and_fails_bounded(self):
        self.assertEqual(_confirmatory_iterations(.025, 1), 20_000)
        self.assertEqual(_confirmatory_iterations(.0001, 3), 30_000)
        self.assertGreater(
            _confirmatory_iterations(1e-8, 3), MAX_CONFIRMATORY_ITERATIONS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
