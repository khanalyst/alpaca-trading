"""Recorder failures remain readable, fail-closed status observations."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import recorder, session_acceptance
from tests.test_session_acceptance import DATE, OPEN, _write_index


class RecorderStatusContractTests(unittest.TestCase):
    def test_failure_event_cannot_replace_status_envelope(self):
        event = {"schema": "recorder-error.v1", "status": "failed",
                 "failure_kind": "market_data_request_failed", "retryable": True,
                 "error_type": "RuntimeError", "error": "test request timeout"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = recorder._save_status(root / "market.csv", event)
            persisted = json.loads((root / recorder.STATUS_NAME).read_text())
        self.assertEqual(saved["schema"], recorder.STATUS_SCHEMA)
        self.assertEqual(persisted, saved)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["failure_kind"], event["failure_kind"])
        self.assertEqual(saved["error"], event["error"])
        self.assertIs(saved["retryable"], True)
        self.assertEqual(event["schema"], "recorder-error.v1")

    def test_capture_reports_failed_recorder_not_malformed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_index(root)
            recorder._save_status(root / "market.csv", {
                "schema": "recorder-error.v1", "status": "failed",
                "error": "test request timeout", "retryable": True})
            with patch.object(session_acceptance.health, "recorder", return_value={
                    "status": "failed", "fresh": True, "ok": False,
                    "market_session_status": "open"}), \
                    patch.object(session_acceptance.health, "shadow", return_value={}):
                sample = session_acceptance.capture_sample(
                    root, root / "shadow-health.json", DATE, now=OPEN + 10.0)
        self.assertEqual(sample["sample_status"], "failed")
        self.assertIn("recorder_status:failed", sample["sample_reasons"])
        self.assertIs(sample["authorizing"], False)

    def test_service_error_log_keeps_event_schema_but_status_is_readable(self):
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"ALPACA_AGENT_SECRETS_FILE": ""}), \
                patch.object(recorder, "AlpacaProvider", return_value=object()), \
                patch.object(recorder, "record_once", side_effect=RuntimeError(
                    "test request timeout")), \
                redirect_stderr(errors), redirect_stdout(io.StringIO()):
            code = recorder.main(["--config", "config.yaml", "--out", directory,
                                  "--once", "--interval", "30"])
            status = json.loads((Path(directory) / recorder.STATUS_NAME).read_text())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(errors.getvalue())["schema"], "recorder-error.v1")
        self.assertEqual(status["schema"], recorder.STATUS_SCHEMA)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "test request timeout")


if __name__ == "__main__":
    unittest.main()
