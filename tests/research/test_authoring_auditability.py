"""Every nondeterministic authoring attempt must be reconstructable."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent.staging import StagingStore
from deploy import dashboard
from research import authoring, provenance
from tests.research.test_authoring import (
    FakeProposer, good_proposal, response)


class AuthoringAuditabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "legacy-staging.db"
        self.store = StagingStore(self.path)
        self.cfg = {"llm": {"provider": "openai", "model": "configured"}}

    def test_success_retains_complete_prompt_response_and_hashes(self):
        raw = response(good_proposal()) + (" " * 25_000)
        result = authoring.author_generation(
            self.store, self.cfg, author=FakeProposer(raw), now=100.0,
            evidence={"source": "fixed", "rows": [1, 2, 3]})

        [attempt] = provenance.authoring_attempts(self.path)
        self.assertEqual(attempt["attempt_id"], result["attempt_id"])
        self.assertEqual(attempt["raw_response"], raw)
        self.assertEqual(attempt["request"]["system"], authoring.AUTHORING_SYSTEM)
        self.assertEqual(attempt["request"]["user"]["evidence"]["rows"],
                         [1, 2, 3])
        self.assertEqual(attempt["provider"], "openai")
        self.assertEqual(attempt["model_id"], "test-authoring-model")
        self.assertEqual(attempt["prompt_version"], authoring.prompt_version())
        self.assertEqual(attempt["parser_status"], "SUCCEEDED")
        self.assertEqual(attempt["validation_status"], "ACCEPTED")
        self.assertEqual(attempt["returned_contract_ids"],
                         ["funding-tail-unwind"])
        self.assertEqual(attempt["accepted_contract_ids"],
                         ["funding-tail-unwind"])
        self.assertEqual(len(attempt["request_hash"]), 64)
        self.assertEqual(len(attempt["context_hash"]), 64)
        self.assertEqual(len(attempt["evidence_hash"]), 64)
        projected = dashboard._authoring(self.path)
        self.assertTrue(projected["available"])
        self.assertEqual(projected["attempts"][0]["attempt_id"],
                         result["attempt_id"])
        self.assertNotIn("raw_response", projected["attempts"][0])
        self.assertNotIn("request", projected["attempts"][0])

    def test_parse_provider_and_validation_failures_are_all_append_only(self):
        authoring.author_generation(
            self.store, self.cfg, author=FakeProposer("not-json"), now=1.0)
        authoring.author_generation(
            self.store, self.cfg,
            author=FakeProposer(error=RuntimeError("provider down")), now=2.0)
        authoring.author_generation(
            self.store, self.cfg,
            author=FakeProposer(response(good_proposal(
                "bad-field", conditions=[
                    {"field": "moon_phase", "op": ">", "value": 1}]))),
            now=3.0)

        attempts = list(reversed(provenance.authoring_attempts(self.path)))
        self.assertEqual([item["status"] for item in attempts],
                         ["FAILED", "FAILED", "FAILED"])
        self.assertEqual(attempts[0]["parser_status"], "FAILED")
        self.assertEqual(attempts[1]["parser_status"], "NOT_RUN")
        self.assertIn("provider down", attempts[1]["error"])
        self.assertEqual(attempts[2]["validation_status"], "REJECTED")
        self.assertEqual(attempts[2]["returned_contract_ids"], ["bad-field"])
        self.assertEqual(attempts[2]["accepted_contract_ids"], [])
        self.assertEqual(len(attempts[2]["rejections"]), 1)

        with closing(sqlite3.connect(self.path)) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE authoring_attempts SET status='FAILED'")

    def test_existing_staging_database_is_migrated_without_touching_contracts(self):
        self.store.register(good_proposal(), generation=0, now=10.0)
        provenance.migrate(self.path)
        self.assertEqual(self.store.contract("funding-tail-unwind").payer,
                         good_proposal()["payer"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("staged_contracts", tables)
        self.assertIn("authoring_attempts", tables)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
