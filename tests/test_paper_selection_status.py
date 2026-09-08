"""Honest observability for the one auto-selected paper strategy."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import state
from agent.config import DEFAULT_CONFIG
from agent.engine import Engine


class FakePaperProvider:
    paper = True

    class Session:
        api_key = "paper-key"
        secret_key = "paper-secret"

    session = Session()

    def __init__(self):
        self.orders_sent = []

    def submit_order(self, request):
        self.orders_sent.append(request)
        raise AssertionError("paper selection observability submitted an order")

    def clock(self):
        return {"is_open": False}

    def positions(self):
        return []


def _config(db_path: Path) -> dict:
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["research"]["db_path"] = str(db_path)
    return cfg


def _verified_record() -> dict:
    config_hash = "config-hash-volume-breakout"
    return {
        "candidate_id": "candidate-volume-breakout",
        "variant_id": "rule.volume-breakout.proved",
        "strategy_id": "rule",
        "vehicle": "equity",
        "config_hash": config_hash,
        "config": {
            "strategy": {
                "rule_spec": {"family": "volume_breakout"},
            },
        },
        "axes": {"family": "volume_breakout"},
        "latest_proof": {
            "run_id": "shadow-proof-volume-breakout",
            "lane": "shadow",
            "config_hash": config_hash,
            "gate_hash": "verified-gate-hash",
            "verified_gate": {"passes": True},
        },
    }


class PaperSelectionStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="paper-selection-status-")
        self.original_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.directory.name) / "runtime"
        state.configure_runtime("paper")
        state.ensure_ready()
        self.db_path = Path(self.directory.name) / "edge.sqlite3"
        self.addCleanup(self._cleanup_runtime)

    def _cleanup_runtime(self):
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.directory.cleanup()

    def _engine(self, resolver, *, light=True):
        resolver_patch = patch(
            "agent.edge.resolve_validated_variant", side_effect=resolver)
        apply_patch = patch(
            "agent.edge.apply_variant", side_effect=lambda cfg, _record: cfg)
        resolver_patch.start()
        apply_patch.start()
        self.addCleanup(apply_patch.stop)
        self.addCleanup(resolver_patch.stop)
        return Engine(_config(self.db_path), light=light,
                      provider=FakePaperProvider())

    def test_missing_proof_is_armed_and_waiting_when_operator_is_unpaused(self):
        engine = self._engine([None, None])

        selection = engine.check()["paper_selection"]

        self.assertEqual(selection, {
            "configured_strategy": "rule",
            "selection_mode": "specific",
            "requested_variant": "auto",
            "resolved": None,
            "blocker_code": "validated_edge_required",
            "armed": True,
            "state": "waiting_for_proof",
        })
        self.assertFalse(engine._refresh_edge())
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["reason"], "validated_edge_required")
        self.assertEqual(heartbeat["paper_selection"], selection)
        self.assertEqual(engine.provider.orders_sent, [])

    def test_operator_pause_kill_day_risk_and_shutdown_are_not_armed(self):
        engine = self._engine([None])
        cases = (
            ({"operator_pause": True}, None, "operator_paused"),
            ({"state": state.KILLED, "kill_reason": "operator kill"},
             None, "runtime_killed"),
            ({"state": state.DAY_STOPPED,
              "risk_day": {"limit_hit": True}},
             None, "daily_risk_limit"),
            ({}, "shutdown_flatten_incomplete", "shutdown_requested"),
        )
        for runtime_changes, shutdown_reason, blocker in cases:
            with self.subTest(blocker=blocker):
                runtime = deepcopy(state.DEFAULT)
                runtime["runtime_mode"] = "paper"
                runtime.update(runtime_changes)
                state.save_state(runtime)
                engine.shutdown_reason = shutdown_reason

                selection = engine.status()["paper_selection"]

                self.assertFalse(selection["armed"])
                self.assertEqual(selection["state"], "blocked")
                self.assertEqual(selection["blocker_code"], blocker)

    def test_verified_record_exposes_one_resolved_identity_and_proof(self):
        record = _verified_record()
        engine = self._engine([record])

        selection = engine.check()["paper_selection"]

        self.assertTrue(selection["armed"])
        self.assertEqual(selection["state"], "ready")
        self.assertIsNone(selection["blocker_code"])
        self.assertEqual(selection["resolved"], {
            "candidate_id": "candidate-volume-breakout",
            "variant_id": "rule.volume-breakout.proved",
            "family": "volume_breakout",
            "proof": {
                "run_id": "shadow-proof-volume-breakout",
                "gate_hash": "verified-gate-hash",
                "config_hash": "config-hash-volume-breakout",
                "lane": "shadow",
            },
        })

    def test_startup_heartbeat_reuses_verified_selection_payload(self):
        record = _verified_record()
        with patch.object(Engine, "preflight",
                          return_value={"clock": None}), \
                patch.object(Engine, "reconcile", return_value={}), \
                patch.object(Engine, "_enforce_intraday_cleanup",
                             return_value=True):
            engine = self._engine([record], light=False)

        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "starting")
        self.assertEqual(heartbeat["paper_selection"],
                         engine.check()["paper_selection"])

    def test_resolution_failure_never_labels_a_selected_candidate(self):
        record = _verified_record()
        engine = self._engine([record, RuntimeError("ledger unreadable")])
        self.assertIsNotNone(engine.check()["paper_selection"]["resolved"])

        self.assertFalse(engine._refresh_edge())

        selection = engine.check()["paper_selection"]

        self.assertIsNone(selection["resolved"])
        self.assertFalse(selection["armed"])
        self.assertEqual(selection["blocker_code"], "edge_resolution_failed")

    def test_future_proof_resumes_same_process_without_preproof_orders(self):
        record = _verified_record()
        engine = self._engine([None, None, record])
        engine.running = True

        self.assertFalse(engine._refresh_edge())
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        self.assertEqual(engine.provider.orders_sent, [])
        waiting = engine.check()["paper_selection"]
        self.assertTrue(waiting["armed"])
        self.assertEqual(waiting["state"], "waiting_for_proof")

        self.assertTrue(engine._refresh_edge())

        self.assertEqual(state.load_state()["state"], state.RUNNING)
        self.assertEqual(engine.provider.orders_sent, [])
        selected = engine.status()["paper_selection"]
        self.assertTrue(selected["armed"])
        self.assertEqual(selected["state"], "ready")
        self.assertEqual(selected["resolved"]["candidate_id"],
                         record["candidate_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
