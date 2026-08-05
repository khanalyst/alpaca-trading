"""Focused regression coverage for the explicit reviewed-variant demo path."""

import unittest
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent import deployment
from tests.helpers import valid_config


class _Client:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetch_open_orders(self, *args):
        return list(self.rows)


class _NonListRegularClient:
    def fetch_open_orders(self, *args):
        if args:
            return []
        return {"orders": []}


class _NonListAlgoClient:
    def fetch_open_orders(self, *args):
        if args:
            return {"orders": []}
        return []


class _DemoExchange:
    demo = True

    def __init__(self, positions=None, orders=None, fingerprint=None):
        self.x = _Client(orders)
        self._positions = positions or []
        self._fingerprint = fingerprint

    def verify_trade_permission(self):
        return {"permissions": ["trade"], "account_fingerprint": self._fingerprint}

    def positions(self):
        return list(self._positions)


class DemoPreflightTests(unittest.TestCase):
    def test_happy_path_is_read_only_and_returns_account_receipt_material(self):
        result = deployment.preflight_demo_account(
            _DemoExchange(fingerprint="demo-account"),
            expected_account_fingerprint="demo-account")
        self.assertEqual(result["account_fingerprint"], "demo-account")
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["open_orders"], [])

    def test_account_mismatch_and_nonflat_state_are_rejected(self):
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "fingerprint"):
            deployment.preflight_demo_account(
                _DemoExchange(fingerprint="other"),
                expected_account_fingerprint="demo-account")
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "positions"):
            deployment.preflight_demo_account(
                _DemoExchange(positions=[{"symbol": "BTC/USDT:USDT"}]),
                expected_account_fingerprint="demo-account")
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "orders"):
            deployment.preflight_demo_account(
                _DemoExchange(orders=[{"id": "order-1"}]),
                expected_account_fingerprint="demo-account")

    def test_open_order_proof_is_required_and_malformed_responses_fail_closed(self):
        missing = _DemoExchange(fingerprint="demo-account")
        missing.x = object()
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "API is unavailable"):
            deployment.preflight_demo_account(
                missing, expected_account_fingerprint="demo-account")

        regular = _DemoExchange(fingerprint="demo-account")
        regular.x = _NonListRegularClient()
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "regular open-order response"):
            deployment.preflight_demo_account(
                regular, expected_account_fingerprint="demo-account")

        algo = _DemoExchange(fingerprint="demo-account")
        algo.x = _NonListAlgoClient()
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "open-order response"):
            deployment.preflight_demo_account(
                algo, expected_account_fingerprint="demo-account")

    def test_runtime_state_and_live_exchange_are_rejected(self):
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "demo exchange"):
            deployment.preflight_demo_account(
                MagicMock(demo=False), expected_account_fingerprint="demo-account")
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "runtime state"):
            deployment.preflight_demo_account(
                _DemoExchange(fingerprint="demo-account"),
                expected_account_fingerprint="demo-account",
                runtime_state_snapshot={"active_trades": {"BTC": {}}})

    def test_demo_verifier_is_separate_and_fail_closed_on_mode_or_missing_scope(self):
        live = valid_config()
        live["mode"] = "live"
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "mode=demo"):
            deployment.verify_demo_artifact(
                live, variant_id="candidate", scope_key="demo:test",
                packet_ref="packet", expected_account_fingerprint="account")
        with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                    "variant_id"):
            deployment.verify_demo_artifact(
                valid_config(), scope_key="demo:test", packet_ref="packet",
                expected_account_fingerprint="account", store=MagicMock())

    def test_authorization_receipt_is_append_only_journalled_with_attribution(self):
        authorization = {
            "variant_id": "candidate",
            "scope_key": "demo:test",
            "packet_id": "packet",
            "packet_hash": "p" * 64,
            "artifact_hash": "a" * 64,
            "variant_definition_hash": "v" * 64,
            "candidate_config_hash": "c" * 64,
            "artifact_strategy_config_version": "s" * 16,
            "model_id": "model",
            "model_assumptions_hash": "m" * 64,
            "prompt_hash": "q" * 64,
            "prompt_inputs_hash": "i" * 64,
            "runtime_source_hash": "r" * 64,
            "deployment_config_hash": "d" * 64,
            "reviewed_by": "reviewer",
            "registry_change_ref": "change-1",
        }
        with patch("agent.deployment.state.log_event") as log_event, \
             patch("agent.deployment.state.set_journal_context") as set_context, \
             patch("agent.deployment.state.journal_context", return_value={}):
            receipt = deployment.record_demo_authorization_receipt(
                authorization, account_fingerprint="demo-account",
                preflight={"account_fingerprint": "demo-account"})
        self.assertEqual(receipt["variant_id"], "candidate")
        self.assertEqual(receipt["account_fingerprint"], "demo-account")
        log_event.assert_called_once()
        self.assertIn("demo_candidate_authorization", log_event.call_args.args)
        self.assertIn("reviewer", log_event.call_args.args[1])
        set_context.assert_called_once()

    def test_authorization_receipt_uses_real_state_log_event_signature(self):
        authorization = {
            "variant_id": "candidate",
            "scope_key": "demo:test",
            "packet_id": "packet",
            "packet_hash": "p" * 64,
            "artifact_hash": "a" * 64,
            "variant_definition_hash": "v" * 64,
            "artifact_strategy_config_version": "s" * 16,
            "deployment_config_hash": "d" * 64,
            "reviewed_by": "reviewer",
            "registry_change_ref": "change-1",
        }
        original = {
            "scope": deployment.state.RUNTIME_SCOPE,
            "runtime": deployment.state.RUNTIME,
            "state": deployment.state.STATE_FILE,
            "pid": deployment.state.PID_FILE,
            "db": deployment.state.DB_FILE,
            "lock": deployment.state.STATE_LOCK_FILE,
            "heartbeat": deployment.state.HEARTBEAT_FILE,
            "context": deployment.state.journal_context(),
        }
        with tempfile.TemporaryDirectory() as directory:
            try:
                deployment.state.configure_runtime("demo", base=Path(directory))
                deployment.state.bind_runtime_identity("demo", "receipt-key")
                receipt = deployment.record_demo_authorization_receipt(
                    authorization, account_fingerprint=(
                        deployment.state.journal_context()["account_fingerprint"]),
                    preflight={"account_fingerprint": "bound"})
                with sqlite3.connect(deployment.state.DB_FILE) as conn:
                    row = conn.execute(
                        "SELECT kind, payload, variant_id, runtime_mode, "
                        "account_fingerprint FROM events ORDER BY ts DESC "
                        "LIMIT 1").fetchone()
            finally:
                deployment.state._set_runtime_paths(
                    original["runtime"], original["scope"])
                deployment.state._JOURNAL_CONTEXT.clear()
                deployment.state._JOURNAL_CONTEXT.update(original["context"])
        self.assertEqual(row[0], "demo_candidate_authorization")
        self.assertEqual(row[2], "candidate")
        self.assertEqual(row[3], "demo")
        self.assertEqual(json.loads(row[1])["reviewed_by"], "reviewer")

    def test_real_verifier_requires_current_positive_paper_success(self):
        from tests.research.test_realtime_findings import QualificationAndPacketTests

        fixture = QualificationAndPacketTests(
            "test_t3_packets_are_deterministic_review_gated_and_immutable")
        fixture.setUp()
        try:
            payload = fixture.complete_payload()
            packet_id = fixture.store.create_t3_packet(
                fixture.v.variant_id, payload, reviewed_by="reviewer",
                registry_change_ref="change-123")
            packet = fixture.store.t3_packet_by_id(packet_id)
            cfg = valid_config()
            cfg["research"] = {"findings_store": str(fixture.path)}
            result = deployment.verify_demo_artifact(
                cfg, variant_id=fixture.v.variant_id, scope_key="demo:a",
                packet_ref="t3-packet:" + packet["payload_hash"],
                expected_account_fingerprint="demo-account",
                store=fixture.store)
            self.assertGreaterEqual(result["paper"]["closed_trades"], 100)
            with patch.object(
                    deployment.variants, "apply",
                    return_value={**cfg, "mode": "live"}):
                with self.assertRaisesRegex(
                        deployment.DeploymentAuthorizationError, "mode=demo"):
                    deployment.verify_demo_artifact(
                        cfg, variant_id=fixture.v.variant_id,
                        scope_key="demo:a",
                        packet_ref="t3-packet:" + packet["payload_hash"],
                        expected_account_fingerprint="demo-account",
                        store=fixture.store, catalog=result["catalog"])
            with sqlite3.connect(fixture.path) as conn:
                conn.execute(
                    "UPDATE paper_trades SET r_multiple=-1 "
                    "WHERE scope_key=? AND variant_id=?",
                    ("demo:a", fixture.v.variant_id))
            with self.assertRaisesRegex(
                    deployment.DeploymentAuthorizationError, "not positive"):
                deployment.verify_demo_artifact(
                    cfg, variant_id=fixture.v.variant_id, scope_key="demo:a",
                    packet_ref="t3-packet:" + packet["payload_hash"],
                    expected_account_fingerprint="demo-account",
                    store=fixture.store)
        finally:
            fixture.tearDown()


class CandidateStartupOrderingTests(unittest.TestCase):
    def test_authorization_precedes_clients_and_ordinary_demo_remains_unchanged(self):
        cfg = valid_config()
        events = []
        auth = {
            "runtime_config": cfg,
            "variant_id": "momentum.rr.fixed_2_5",
            "scope_key": "demo:test",
            "expected_account_fingerprint": "demo-account",
            "catalog": {},
            "system_prompt": "candidate prompt",
            "packet_id": "packet",
            "packet_hash": "p" * 64,
            "artifact_hash": "a" * 64,
            "variant_definition_hash": "v" * 64,
            "artifact_strategy_config_version": "c" * 16,
            "candidate_config_hash": "d" * 64,
            "model_id": "model",
            "model_assumptions_hash": "m" * 64,
            "prompt_hash": "q" * 64,
            "prompt_inputs_hash": "i" * 64,
            "runtime_source_hash": "s" * 64,
            "deployment_config_hash": "e" * 64,
            "reviewed_by": "reviewer",
            "registry_change_ref": "change-1",
        }
        exchange = MagicMock(demo=True)
        with patch("agent.engine.deployment.verify_demo_artifact",
                   side_effect=lambda *a, **k: events.append("authorize") or auth), \
             patch("agent.engine.AlertManager",
                   side_effect=lambda *a, **k: events.append("alerts") or MagicMock()), \
             patch("agent.engine.Exchange",
                   side_effect=lambda *a, **k: events.append("exchange") or exchange), \
             patch("agent.engine.deployment.preflight_demo_account",
                   side_effect=lambda *a, **k: events.append("preflight") or {
                       "account_fingerprint": "demo-account"}), \
             patch("agent.engine.deployment.record_demo_authorization_receipt",
                   side_effect=lambda *a, **k: events.append("receipt") or {}), \
             patch("agent.engine.brain.LLM",
                   side_effect=lambda *a, **k: events.append("llm") or MagicMock()), \
             patch("agent.engine.RiskEngine"), \
             patch("agent.engine.Engine._build_shadow", return_value=None), \
             patch("agent.engine.state.configure_runtime"), \
             patch("agent.engine.state.bind_runtime_identity"), \
             patch("agent.engine.state.journal_context", return_value={}), \
             patch("agent.engine.state.set_journal_context"), \
             patch("agent.engine.state.check_journal"), \
             patch("agent.engine.state.new_run_id", return_value="run"), \
             patch("agent.engine.state.stable_fingerprint", return_value="f"), \
             patch("agent.engine.state.code_fingerprint", return_value="c"):
            from agent.engine import Engine
            Engine(cfg, candidate_demo={
                "variant_id": "momentum.rr.fixed_2_5",
                "scope_key": "demo:test",
                "packet_ref": "packet",
                "expected_account_fingerprint": "demo-account",
            })
        self.assertLess(events.index("authorize"), events.index("exchange"))
        self.assertLess(events.index("exchange"), events.index("llm"))
        self.assertLess(events.index("preflight"), events.index("llm"))

    def test_candidate_request_cannot_authorize_live_mode(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        with patch("agent.engine.state.configure_runtime"), \
             patch("agent.engine.state.bind_runtime_identity"), \
             patch("agent.engine.deployment.verify_demo_artifact") as verifier:
            from agent.engine import Engine
            with self.assertRaisesRegex(deployment.DeploymentAuthorizationError,
                                        "live mode"):
                Engine(cfg, candidate_demo={"variant_id": "x"})
        verifier.assert_not_called()

    def test_run_preserves_candidate_attribution_context(self):
        from agent.engine import Engine

        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.run_id = "run-candidate"
        engine.strategy_id = "momentum"
        engine.strategy_version = "phase1-v3"
        engine.prompt_version = "prompt"
        engine.config_version = "config"
        engine.code_version = "code"
        engine.strategy_config_version = "strategy-config"
        engine.demo_authorization = {
            "variant_id": "momentum.rr.fixed_2_5",
            "packet_id": "packet",
            "packet_hash": "p" * 64,
            "artifact_hash": "a" * 64,
            "variant_definition_hash": "v" * 64,
            "artifact_strategy_config_version": "s" * 16,
            "deployment_config_hash": "d" * 64,
        }
        engine.alerts = MagicMock()
        engine.ex = MagicMock()
        calls = []
        states = iter([
            {"state": "RUNNING", "kill_reason": None,
             "flatten_on_kill": False, "operator_pause": False},
            {"state": "KILLED", "kill_reason": "stop",
             "flatten_on_kill": False, "operator_pause": False},
        ])
        with patch("agent.engine.state.acquire_run_lock", return_value=object()), \
             patch("agent.engine.state.load_state", side_effect=lambda: next(states)), \
             patch("agent.engine.state.set_journal_context",
                   side_effect=lambda **kwargs: calls.append(kwargs)), \
             patch("agent.engine.state.log_run"), \
             patch("agent.engine.state.write_heartbeat"), \
             patch("agent.engine.state.release_run_lock"), \
             patch("agent.engine.state.journal_context", return_value={}):
            engine.run()
        run_context = calls[0]
        self.assertEqual(run_context["variant_id"], "momentum.rr.fixed_2_5")
        self.assertEqual(run_context["packet_id"], "packet")
        self.assertEqual(run_context["artifact_hash"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
