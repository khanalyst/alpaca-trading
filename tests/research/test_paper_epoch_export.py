"""Focused checks for the public-safe paper-epoch operational export."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research.paper_epoch import (
    CohortMember,
    ExecutionObservation,
    FrozenEpoch,
    PaperEpochStore,
    PaperRuntimeIdentity,
    RuntimeStartAttestation,
    content_digest,
    identity_fingerprint,
)
from research.paper_epoch_export import (
    PaperEpochExportError,
    write_paper_epoch_export,
)


class PaperEpochExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-epoch-export-")
        root = Path(self.tmp.name)
        self.db = root / "paper.sqlite3"
        self.output = root / "exports"
        self.store = PaperEpochStore(self.db)
        self.account = identity_fingerprint("paper-account", "export-account")
        self.runtime = identity_fingerprint("paper-runtime", "export-runtime")
        self.shadow_runtime = identity_fingerprint("shadow-runtime", "export-shadow")
        self.trader_account = identity_fingerprint("trader-account", "deployed-account")
        self.trader_runtime = identity_fingerprint("trader-runtime", "deployed-runtime")
        self.primary = CohortMember.paper_primary(
            "paper", PaperRuntimeIdentity(self.account, self.runtime))
        self.shadow = CohortMember.shadow_sibling("shadow-a", self.shadow_runtime)
        self.epoch = self.store.create_epoch(
            FrozenEpoch(
                content_digest(["stream"]), content_digest(["window"]),
                content_digest(["config"]), content_digest(["code"]),
                content_digest(["cost"]), content_digest(["risk"])),
            self.primary, [self.shadow],
            epoch_id="paper-export-test",
            trader_account_fingerprint=self.trader_account,
            trader_runtime_fingerprint=self.trader_runtime,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self):
        self.store.start_epoch(self.epoch["epoch_id"], RuntimeStartAttestation(
            PaperRuntimeIdentity(self.account, self.runtime),
            {"paper": self.runtime, "shadow-a": self.shadow_runtime},
            {"paper": self.epoch["manifest_digest"],
             "shadow-a": self.epoch["manifest_digest"]},
            runtime_llm_adaptation=False,
        ))

    def _record(self, opportunity: str):
        return self.store.record_outcome(
            self.epoch["epoch_id"], opportunity, f"stream-{opportunity}", [
                ExecutionObservation("paper", "fill",  quantity=10,
                                     reference_price=100, fill_price=100.01,
                                     slippage_bps=1),
                ExecutionObservation("shadow-a", "fill", quantity=10,
                                     reference_price=100, fill_price=100.01,
                                     slippage_bps=1),
            ])

    def test_export_is_public_safe_canonical_and_idempotent(self):
        self._start()
        self._record("one")
        first = write_paper_epoch_export(self.store, self.epoch["epoch_id"], self.output)
        second = write_paper_epoch_export(self.store, self.epoch["epoch_id"], self.output)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.record_count, 1)
        self.assertEqual(first.path.read_bytes(), second.path.read_bytes())

        rows = [json.loads(line) for line in first.path.read_text().splitlines()]
        self.assertEqual([row["kind"] for row in rows], ["manifest", "summary", "outcome"])
        self.assertEqual(rows[0]["record_count"], 1)
        text = first.path.read_text()
        for secret in (str(self.db), self.account, self.runtime,
                       self.shadow_runtime, self.trader_account,
                       self.trader_runtime):
            self.assertNotIn(secret, text)
        forbidden = {"session_date", "signal_time", "symbol", "side",
                     "stop", "target", "quote", "exit", "r"}
        self.assertTrue(forbidden.isdisjoint(rows[0]))
        self.assertTrue(forbidden.isdisjoint(rows[1]["summary"]))

    def test_new_stored_outcome_changes_content_address(self):
        self._start()
        first = write_paper_epoch_export(self.store, self.epoch["epoch_id"], self.output)
        self._record("one")
        second = write_paper_epoch_export(self.store, self.epoch["epoch_id"], self.output)
        self.assertNotEqual(first.digest, second.digest)
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(second.record_count, 1)

    def test_existing_artifact_with_different_bytes_is_rejected(self):
        first = write_paper_epoch_export(self.store, self.epoch["epoch_id"], self.output)
        first.path.write_bytes(b"tampered\n")
        with self.assertRaises(PaperEpochExportError):
            write_paper_epoch_export(self.store, self.epoch["epoch_id"], self.output)

    def test_export_handles_uri_metacharacters_and_reports_missing_epoch(self):
        special_db = Path(self.tmp.name) / "db?#folder" / "paper.sqlite3"
        special_store = PaperEpochStore(special_db)
        epoch = special_store.create_epoch(
            FrozenEpoch(
                content_digest(["stream-special"]),
                content_digest(["window-special"]),
                content_digest(["config-special"]),
                content_digest(["code-special"]),
                content_digest(["cost-special"]),
                content_digest(["risk-special"])),
            self.primary, [self.shadow],
            epoch_id="paper-export-special-path",
            trader_account_fingerprint=self.trader_account,
            trader_runtime_fingerprint=self.trader_runtime,
        )
        result = write_paper_epoch_export(
            special_db, epoch["epoch_id"], self.output)
        self.assertTrue(result.path.is_file())
        with self.assertRaisesRegex(PaperEpochExportError, "unknown paper epoch"):
            write_paper_epoch_export(special_db, "missing", self.output)

    def test_long_valid_epoch_id_does_not_overflow_filename_limit(self):
        long_id = "e" * 192
        long_store = PaperEpochStore(Path(self.tmp.name) / "long.sqlite3")
        epoch = long_store.create_epoch(
            FrozenEpoch(
                content_digest(["stream-long"]), content_digest(["window-long"]),
                content_digest(["config-long"]), content_digest(["code-long"]),
                content_digest(["cost-long"]), content_digest(["risk-long"])),
            self.primary, [self.shadow], epoch_id=long_id,
            trader_account_fingerprint=self.trader_account,
            trader_runtime_fingerprint=self.trader_runtime,
        )
        result = write_paper_epoch_export(
            long_store, epoch["epoch_id"], self.output)
        self.assertTrue(result.path.is_file())
        self.assertLessEqual(len(result.path.name.encode()), 255)

    def test_webhook_is_optional_and_carries_only_safe_metadata(self):
        seen = []

        def sender(url, metadata):
            seen.append((url, dict(metadata)))
            return {"accepted": True}

        result = write_paper_epoch_export(
            self.store, self.epoch["epoch_id"], self.output,
            webhook_url="https://example.invalid/paper-epoch",
            webhook_sender=sender,
        )
        self.assertTrue(result.webhook and result.webhook["ok"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1]["candidate_id"], self.epoch["epoch_id"])
        self.assertEqual(seen[0][1]["vehicle"], "paper_epoch_operational")
        self.assertNotIn(str(self.db), json.dumps(seen[0][1]))
        self.assertNotIn(self.account, json.dumps(seen[0][1]))

        failed = write_paper_epoch_export(
            self.store, self.epoch["epoch_id"], self.output / "bad-timeout",
            webhook_url="https://example.invalid/paper-epoch",
            webhook_timeout_seconds=None,
        )
        self.assertTrue(failed.path.is_file())
        self.assertFalse(failed.webhook["ok"])

    def test_cli_export_uses_readonly_store_and_returns_structured_result(self):
        repo = Path(__file__).resolve().parents[2]
        completed = subprocess.run([
            sys.executable, "research.py", "paper-epoch", "export",
            "--db", str(self.db), "--epoch", self.epoch["epoch_id"],
            "--output-root", str(self.output),
        ], cwd=repo, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        self.assertTrue(result["created"])
        self.assertEqual(result["record_count"], 0)
        self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
