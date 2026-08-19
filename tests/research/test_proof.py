from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest

from agent.contracts.rule import DEFAULT_RULE_SPEC
import research.proof_payload as proof_payload
from research.proof import (PROOF_SCHEMA, build_proof_payload, payload_hash,
                             canonical_json, render_markdown, write_proof)


class FakeLedger:
    def __init__(self, vehicle="equity"):
        self.record = {
            "candidate_id": "cand/unsafe",
            "variant_id": "rule.momentum.abc123",
            "strategy_id": "rule",
            "vehicle": vehicle,
            "status": "validated",
            "dataset_hash": "d" * 64,
            "config_hash": "c" * 64,
            "code_hash": "k" * 64,
            "provenance_hash": "p" * 64,
            "config_json": '{"strategy":{"rule_spec":' + __import__("json").dumps(DEFAULT_RULE_SPEC) + '}}',
        }

    def candidate(self, candidate_id):
        return dict(self.record, candidate_id=candidate_id)

    def runs(self, candidate_id):
        return [{"run_id": "run-1", "candidate_id": candidate_id,
                 "lane": "shadow", "vehicle": self.record["vehicle"],
                 "dataset_hash": "d" * 64, "config_hash": "c" * 64,
                 "code_hash": "k" * 64, "provenance_hash": "p" * 64,
                 "metrics": {"fit_samples": 12, "heldout_samples": 8,
                             "shadow_samples": 5, "gate": {"passes": True},
                             "confidence": .97, "max_drawdown": .12,
                             "cost": 1.5}}]

    def evidence(self, candidate_id):
        return [{"evidence_id": "ev-1", "kind": "llm-proposal",
                 "evidence_hash": "e" * 64,
                 "payload": {"provider": "openai", "feed": "sip",
                              "schema": "bars.v1", "as_of": "2024-01-02T15:00:00Z"}}]


class ProofTests(unittest.TestCase):
    def test_authorized_proof_refuses_diagnostic_historical_backfill(self):
        class DiagnosticLedger(FakeLedger):
            def runs(self, candidate_id):
                run = super().runs(candidate_id)[0]
                run["metrics"]["gate"]["authorization_projection"] = {
                    "heldout": {"reasons": {
                        "diagnostic_historical_backfill": 1,
                    }},
                }
                return [run]

            def evidence(self, candidate_id):
                return [{
                    "evidence_id": "gate-1", "run_id": "run-1",
                    "kind": "verified_gate", "evidence_hash": "g" * 64,
                    "payload": {
                        "run_id": "run-1",
                        "gate": {"passes": False,
                                 "authorization_projection": {
                                     "heldout": {"reasons": {
                                         "diagnostic_historical_backfill": 1,
                                     }}}},
                    },
                }]

            def trades(self, candidate_id):
                return [{"run_id": "run-1", "trade_id": "trade-1",
                         "evidence_mode":
                             "diagnostic_historical_backfill"}]

        with self.assertRaisesRegex(
                ValueError, "diagnostic historical backfill"):
            build_proof_payload(
                DiagnosticLedger(), "candidate-1",
                {"authorized_run_id": "run-1"},
            )

    def test_payload_helpers_are_reexported_without_wrappers(self):
        self.assertIs(canonical_json, proof_payload.canonical_json)
        self.assertIs(payload_hash, proof_payload.payload_hash)
        self.assertIs(build_proof_payload, proof_payload.build_proof_payload)
        self.assertIs(render_markdown, proof_payload.render_markdown)

    def test_payload_is_canonical_and_has_required_fields(self):
        ledger = FakeLedger()
        context = {"session_timestamp": "2024-01-02T15:00:00Z"}
        one = build_proof_payload(ledger, "candidate-1", context)
        two = build_proof_payload(ledger, "candidate-1", context)
        self.assertEqual(one, two)
        self.assertEqual(one["schema"], PROOF_SCHEMA)
        self.assertEqual(one["vehicle"], "equity")
        self.assertEqual(one["samples"], {"fit": 12, "heldout": 8, "shadow": 5})
        self.assertEqual(one["session"]["timezone"], "America/New_York")
        self.assertEqual(one["session"]["dst_label"], "EST")
        self.assertEqual(one["llm_evidence_hashes"], ["e" * 64])
        self.assertNotIn("created_at", one)
        self.assertEqual(payload_hash(one), payload_hash(two))

    def test_authorized_run_ignores_caller_metric_spec_and_hash_overrides(self):
        class AuthorizedLedger(FakeLedger):
            def runs(self, candidate_id):
                return [{**super().runs(candidate_id)[0],
                         "heldout_end": "2024-01-02"}]

            def evidence(self, candidate_id):
                return [{"evidence_id": "gate-1", "run_id": "run-1",
                         "kind": "verified_gate", "evidence_hash": "g" * 64,
                         "payload": {"run_id": "run-1", "gate": {
                             "passes": False,
                             "counts": {"fit": {"trades": 12},
                                        "heldout": {"trades": 8}},
                             "performance": {"max_drawdown": .12}}}}]

            def trades(self, candidate_id):
                return []

        malicious = dict(DEFAULT_RULE_SPEC)
        malicious["family"] = "opening_drive"
        payload = build_proof_payload(AuthorizedLedger(), "candidate-1", {
            "authorized_run_id": "run-1",
            "metrics": {"confidence": 1.0, "gate": {"passes": True}},
            "samples": {"fit": 999, "heldout": 999, "shadow": 999},
            "rule_spec": malicious,
            "hashes": {"dataset_hash": "x" * 64, "config_hash": "x" * 64,
                       "code_hash": "x" * 64, "provenance_hash": "x" * 64},
            "llm_evidence_hashes": ["x" * 64],
            "statuses": {"forged": "champion"},
        })
        self.assertEqual(payload["gate"]["passes"], False)
        self.assertEqual(payload["samples"], {"fit": 12, "heldout": 8, "shadow": 5})
        self.assertEqual(payload["normalized_rule_spec"], DEFAULT_RULE_SPEC)
        self.assertEqual(payload["dataset_hash"], "d" * 64)
        self.assertNotIn("forged", payload["statuses"])
        self.assertEqual(payload["llm_evidence_hashes"], [])

    def test_equity_and_option_paths_and_no_overwrite(self):
        for vehicle in ("equity", "option"):
            with tempfile.TemporaryDirectory() as directory:
                result = write_proof(FakeLedger(vehicle), "candidate/unsafe",
                                     context={"session_date": "2024-07-01"},
                                     output_root=directory)
                self.assertEqual(result.path.parent.name, vehicle)
                self.assertTrue(result.path.name.startswith("candidate_unsafe-"))
                self.assertIn("Canonical payload", result.path.read_text())
                repeated = write_proof(
                    FakeLedger(vehicle), "candidate/unsafe",
                    context={"session_date": "2024-07-01"},
                    output_root=directory)
                self.assertFalse(repeated.created)
                self.assertEqual(repeated.path, result.path)

    def test_webhook_success_and_failure_do_not_block_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def sender(url, metadata):
                calls.append((url, metadata))
                return {"accepted": True}

            result = write_proof(FakeLedger(), "candidate-1",
                                 output_root=directory,
                                 webhook_url="https://example.test/hook",
                                 webhook_sender=sender)
            self.assertTrue(result.webhook["ok"])
            self.assertEqual(set(calls[0][1]), {
                "candidate_id", "vehicle", "status", "payload_hash", "artifact"})

        with tempfile.TemporaryDirectory() as directory:
            result = write_proof(FakeLedger(), "candidate-2",
                                 output_root=directory,
                                 webhook_url="https://example.test/hook",
                                 webhook_sender=lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
            self.assertFalse(result.webhook["ok"])
            self.assertTrue(result.path.exists())

    def test_mapping_ledger_and_unsafe_markdown_values_are_supported(self):
        candidate = dict(FakeLedger().record)
        candidate["candidate_id"] = "<script>bad```"
        payload = build_proof_payload({"candidate_id": candidate["candidate_id"], **candidate},
                                      candidate["candidate_id"],
                                      {"session_date": "2024-01-02"})
        self.assertEqual(payload["candidate_id"], candidate["candidate_id"])
        with tempfile.TemporaryDirectory() as directory:
            result = write_proof({"candidate_id": candidate["candidate_id"], **candidate},
                                 candidate["candidate_id"],
                                 context={"session_date": "2024-01-02"},
                                 output_root=directory)
            text = result.path.read_text()
            self.assertNotIn("<script>", text)
            self.assertNotIn("```bad", text)

    def test_invalid_session_date_and_slow_webhook_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "invalid proof session date"):
            build_proof_payload(
                FakeLedger(), "candidate-1", {"session_date": "bad-date"})

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            result = write_proof(
                FakeLedger(), "candidate-timeout", output_root=directory,
                webhook_url="https://example.test/hook",
                webhook_sender=lambda *_: time.sleep(.2),
                webhook_timeout_seconds=.01)
        self.assertLess(time.monotonic() - started, .15)
        self.assertFalse(result.webhook["ok"])
        self.assertIn("TimeoutError", result.webhook["error"])

        calls = []
        def internal_type_error(url, metadata):
            calls.append((url, metadata))
            raise TypeError("internal sender failure")
        with tempfile.TemporaryDirectory() as directory:
            result = write_proof(
                FakeLedger(), "candidate-type-error", output_root=directory,
                webhook_url="https://example.test/hook",
                webhook_sender=internal_type_error)
        self.assertEqual(len(calls), 1)
        self.assertFalse(result.webhook["ok"])

        with self.assertRaisesRegex(ValueError, "invalid proof session date"):
            build_proof_payload(FakeLedger(), "candidate-1", {
                "session_date": "2024-01-02T12:00:00+00:00"})


if __name__ == "__main__":
    unittest.main()
