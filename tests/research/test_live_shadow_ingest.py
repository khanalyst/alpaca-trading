"""End-to-end authorization tests for the research-side shadow consumer."""

from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research import edge_discovery_core, gates
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
        # Ingestion/tamper behavior is the subject of this fixture.  Production
        # promotion still requires 30 clusters and is covered separately; the
        # compact eight-session tail keeps these boundary tests fast.
        self.cluster_floor = patch.object(
            edge_discovery_core, "MIN_PROMOTION_CLUSTERS", 1)
        self.cluster_floor.start()
        self.addCleanup(self.cluster_floor.stop)
        # This module exercises replay/idempotency boundaries with an
        # eight-session tail.  Explicitly lower every immutable protocol
        # constant in this test-only context; production code and the CLI
        # never expose this patch seam.
        self.compact_protocol = patch.multiple(
            gates,
            PROTOCOL_BACKTEST_MIN_TRADES=1,
            PROTOCOL_BACKTEST_MIN_SESSIONS=1,
            PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
            PROTOCOL_SHADOW_MIN_TRADES=1,
            PROTOCOL_SHADOW_MIN_SESSIONS=1,
            PROTOCOL_SHADOW_MIN_CLUSTERS=1,
            PROTOCOL_QUALIFICATION_MIN_TRADES=1,
            PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
            PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1,
        )
        self.compact_protocol.start()
        self.addCleanup(self.compact_protocol.stop)
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
        # Live-shadow ingest now consumes an older selection half and a newer
        # confirmatory half.  Keep this fixture at sixteen sessions so each
        # half has enough exact clusters for the deterministic tests; short
        # value patterns repeat across the complete tail.
        sessions = [f"2024-04-{day:02d}" for day in range(1, 17)]
        for index, session in enumerate(sessions):
            value = values[index % len(values)]
            row = {"vehicle": "equity", "symbol": "SPY",
                   "session_date": session,
                   # Replay lanes use the stable symbol/session opportunity;
                   # candidate, baseline, and null books therefore pair on
                   # the exact same observation.
                   "opportunity_id": f"equity:SPY:{session}",
                   "net_pnl": float(value), "return_value": float(value),
                   "no_trade": False, "entry_price": 100.0,
                   "exit_price": 100.1, "quantity": 1.0,
                   "multiplier": 1.0, "stop_distance": 1.0,
                   "risk_usd": 10.0,
                   "entry_fill_source": "quote",
                   "exit_fill_source": "quote",
                   "entry_feed": "sip", "exit_feed": "sip",
                   "entry_provider": "alpaca", "exit_provider": "alpaca",
                   "entry_quote_age_seconds": 0.0,
                   "exit_quote_age_seconds": 0.0}
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
        session = f"2024-04-{day:02d}"
        row = {"vehicle": "equity", "symbol": "SPY", "session_date": session,
               "opportunity_id": f"equity:SPY:{session}", "net_pnl": float(value),
               "return_value": float(value), "no_trade": False,
               "entry_price": 100.0, "exit_price": 100.1,
               "quantity": 1.0, "multiplier": 1.0,
               "stop_distance": 1.0, "risk_usd": 10.0,
               "entry_fill_source": "quote", "exit_fill_source": "quote",
               "entry_feed": "sip", "exit_feed": "sip",
               "entry_provider": "alpaca", "exit_provider": "alpaca",
               "entry_quote_age_seconds": 0.0,
               "exit_quote_age_seconds": 0.0}
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
        self.assertEqual(result["ingested"], 1, result)
        row = self.ledger.candidate(cid)
        self.assertEqual(row["status"], "validated")
        runs = self.ledger.runs(cid, lane="shadow")
        self.assertEqual(len(runs), 1)
        online = runs[0]["metrics"]["gate"]["verified_gate"]["online_fdr"]
        self.assertTrue(online["required"])
        self.assertTrue(online["tested"])
        self.assertTrue(online["decision"])
        source = runs[0]["metrics"]["shadow_source"]
        self.assertTrue(source["independent_confirmatory"])
        self.assertTrue(source["disjoint_sessions"])
        self.assertEqual(set(source["selection_sessions"]).intersection(
            source["confirmatory_sessions"]), set())
        self.assertEqual(online["p_value_source"], "live_shadow_confirmatory_gate")
        state = FactoryLedger(self.edge_path).fdr_state(
            "shadow-confirmation-v4:equity")
        self.assertEqual(state["tests"], 1)
        self.assertAlmostEqual(state["decisions"][0]["p_value"],
                               online["p_value"])
        evidence = self.ledger.evidence(cid)
        self.assertTrue(any(item["kind"] == "shadow_ingestion" for item in evidence))
        self.assertEqual(len(self.ledger.trades(cid, lane="shadow")), 8)

    def test_same_tail_or_tampered_window_digest_cannot_authorize(self):
        cid, run_id = self._seed_live_run()
        run = self.ledger.run(run_id)
        source = run["metrics"]["shadow_source"]
        # A same-tail record is readable but fails the disjointness boundary.
        source["confirmatory_sessions"] = list(source["selection_sessions"])
        self.assertFalse(self.ledger._live_shadow_authorized(run))

        run = self.ledger.run(run_id)
        source = run["metrics"]["shadow_source"]
        source["selection_session_digest"] = "tampered"
        self.assertFalse(self.ledger._live_shadow_authorized(run))

    def test_tampered_persisted_selection_rows_cannot_authorize(self):
        cid, run_id = self._seed_live_run()
        with sqlite3.connect(self.edge_path) as db:
            row = db.execute(
                "SELECT metrics_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            metrics = json.loads(row[0])
            metrics["shadow_source"]["selection"]["candidate_source"][0]["net_pnl"] = 999.0
            db.execute("DROP TRIGGER runs_no_update")
            db.execute("UPDATE runs SET metrics_json=? WHERE run_id=?", (
                json.dumps(metrics, sort_keys=True, separators=(",", ":")), run_id))
        self.assertFalse(self.ledger.eligibility(cid)["eligible"])

    def test_insufficient_two_window_tail_spends_no_online_allocation(self):
        cid = self.candidate["candidate_id"]
        self._row(cid, 1, 2.0)
        self._row(self.baseline["candidate_id"], 1, 0.0)
        self._row(f"shadow:null:{cid}", 1, -1.0)
        result = ingest_shadow(ShadowIngestConfig(
            self.edge_path, self.shadow_path, min_trades=1, min_sessions=1))
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["candidates"][0]["status"],
                         "underpowered_confirmatory_split")
        self.assertFalse(result["candidates"][0]["online_allocation_spent"])
        self.assertEqual(FactoryLedger(self.edge_path).fdr_state(
            "shadow-confirmation-v4:equity")["tests"], 0)

    def test_v3_lord_state_is_readable_but_v4_starts_a_fresh_sequence(self):
        FactoryLedger(self.edge_path).record_fdr_decision(
            "shadow-confirmation-v3:equity", "legacy-same-tail", .001)
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 16)
        self._rows(self.baseline["candidate_id"], [0.0] * 16)
        self._rows(f"shadow:null:{cid}", [-1.0] * 16)
        result = ingest_shadow(ShadowIngestConfig(
            self.edge_path, self.shadow_path, min_trades=1, min_sessions=1))
        self.assertEqual(result["ingested"], 1, result)
        ledger = FactoryLedger(self.edge_path)
        self.assertEqual(ledger.fdr_state("shadow-confirmation-v3:equity")["tests"], 1)
        self.assertEqual(ledger.fdr_state("shadow-confirmation-v4:equity")["tests"], 1)

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

    def test_crash_retry_reuses_v4_fdr_decision_when_resolution_changes(self):
        cid = self.candidate["candidate_id"]
        self._rows(cid, [2.0] * 8)
        self._rows(self.baseline["candidate_id"], [0.0] * 8)
        self._rows(f"shadow:null:{cid}", [-1.0] * 8)
        config = ShadowIngestConfig(self.edge_path, self.shadow_path,
                                    min_trades=1, min_sessions=1)

        original_record = FactoryLedger.record_fdr_decision
        crashed = {"value": False}

        def record_then_crash(ledger, scope, test_id, p_value, *, alpha=.05):
            result = original_record(ledger, scope, test_id, p_value,
                                     alpha=alpha)
            if str(scope) == "shadow-confirmation-v4:equity" and not crashed["value"]:
                crashed["value"] = True
                raise RuntimeError("simulated crash after FDR commit")
            return result

        with patch("research.live_shadow_ingest._confirmatory_iterations",
                   side_effect=[20_000, 30_000]), \
                patch.object(FactoryLedger, "record_fdr_decision",
                             new=record_then_crash):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                ingest_shadow(config)
            # The second call in this same retry sequence resolves at 30k.
            result = ingest_shadow(config)

        # The first attempt spent the 20k-resolution allocation but did not
        # append its run.  The retry sees the discovery and therefore resolves
        # at 30k; the immutable tail key must still reuse the one v4 row.
        self.assertEqual(result["ingested"], 1, result)
        state = FactoryLedger(self.edge_path).fdr_state(
            "shadow-confirmation-v4:equity")
        self.assertEqual(state["tests"], 1)
        self.assertEqual(len(state["decisions"]), 1)
        run = self.ledger.runs(cid, lane="shadow")[0]
        online = run["metrics"]["gate"]["verified_gate"]["online_fdr"]
        self.assertEqual(online["tests"], 1)
        self.assertEqual(online["test_iterations"], 30_000)
        self.assertTrue(self.ledger.eligibility(cid)["eligible"])

    def test_online_fdr_records_raw_p_not_selected_global_q(self):
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
        self.assertAlmostEqual(online_p, raw_p)
        self.assertEqual(envelope["online_fdr"]["p_value_kind"],
                         "raw_confirmatory")

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
