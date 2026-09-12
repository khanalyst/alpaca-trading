"""Calibration publication preserves immutable diagnostic evidence."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.config import load_config
from research import cost_rerun as cost_rerun_module
from research.cost_rerun import (
    main as cost_rerun_main,
    publish_calibration_evidence,
    run_cost_calibration,
)
from research.costs import ReplayPolicy
from research.edge_discovery_core import DiscoveryError, _read_discovery_rows
from research.edge_ledger import content_hash
from research.source_validation import SourceValidationError
from research.stressed_cost_calibration import calibrate_stressed_cost
from tests.research.test_stressed_cost_calibration import _schedule


ROOT = Path(__file__).resolve().parents[2]


def _artifact(*, spread: float, identity: str) -> dict:
    return calibrate_stressed_cost(
        _schedule(spread=spread, digest=f"{identity}-fit", session_start=0),
        validation_schedule=_schedule(
            spread=spread + 1.0,
            digest=f"{identity}-validation",
            session_start=10,
        ),
        expected_provider="alpaca",
        expected_feed="iex",
    )


def _quote_rows(*, feed: str = "iex", future: bool = False) -> list[dict]:
    rows = []
    opening = datetime(2025, 1, 6, 14, 30, tzinfo=timezone.utc)
    for day in range(10):
        for minute in range(2):
            stamp = opening + timedelta(days=day, minutes=minute)
            observed = (datetime(2099, 1, 1, tzinfo=timezone.utc)
                        if future else stamp)
            rows.append({
                "kind": "quote",
                "provider": "alpaca",
                "feed": feed,
                "source_mode": "historical_backfill",
                "symbol": "SPY",
                "timestamp": stamp.isoformat(),
                "as_of": observed.isoformat(),
                "observed_at": observed.isoformat(),
                "bid": 99.99,
                "ask": 100.01,
                "bid_size": 1_000.0,
                "ask_size": 1_000.0,
            })
    return rows


class CalibrationPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.latest = self.root / "latest.json"
        self.first = _artifact(spread=4.0, identity="first")
        self.second = _artifact(spread=8.0, identity="second")

    def tearDown(self):
        self.temporary.cleanup()

    @property
    def archive(self) -> Path:
        return self.root / "latest.artifacts"

    def test_same_then_different_publication_retains_content_addressed_history(self):
        first_result = publish_calibration_evidence(self.latest, self.first)
        first_archive = Path(first_result["artifact_path"])
        first_bytes = first_archive.read_bytes()

        repeated = publish_calibration_evidence(self.latest, self.first)
        self.assertEqual(repeated["artifact_path"], str(first_archive))
        self.assertEqual(list(self.archive.glob("*.json")), [first_archive])
        self.assertEqual(first_archive.read_bytes(), first_bytes)

        second_result = publish_calibration_evidence(self.latest, self.second)
        second_archive = Path(second_result["artifact_path"])
        self.assertEqual(
            {path.name for path in self.archive.glob("*.json")},
            {f"{self.first['content_hash']}.json",
             f"{self.second['content_hash']}.json"},
        )
        self.assertEqual(first_archive.read_bytes(), first_bytes)
        self.assertEqual(json.loads(first_archive.read_text()), self.first)
        self.assertEqual(json.loads(second_archive.read_text()), self.second)
        self.assertEqual(json.loads(self.latest.read_text()), self.second)

    def test_tampered_incoming_or_current_artifact_never_replaces_latest(self):
        publish_calibration_evidence(self.latest, self.first)
        original = self.latest.read_bytes()

        incoming = deepcopy(self.second)
        incoming["aggregate_conservative_scenario_bps"] += 1.0
        with self.assertRaisesRegex(ValueError, "valid diagnostic artifact hash"):
            publish_calibration_evidence(self.latest, incoming)
        self.assertEqual(self.latest.read_bytes(), original)
        self.assertFalse((self.archive / f"{self.second['content_hash']}.json").exists())

        current = json.loads(original)
        current["provider"] = "tampered"
        self.latest.write_text(json.dumps(current), encoding="utf-8")
        tampered_latest = self.latest.read_bytes()
        with self.assertRaisesRegex(ValueError, "valid diagnostic artifact hash"):
            publish_calibration_evidence(self.latest, self.second)
        self.assertEqual(self.latest.read_bytes(), tampered_latest)
        self.assertFalse((self.archive / f"{self.second['content_hash']}.json").exists())

    def test_conflicting_archive_fails_closed_without_overwriting_latest(self):
        result = publish_calibration_evidence(self.latest, self.first)
        immutable = Path(result["artifact_path"])
        original_latest = self.latest.read_bytes()
        immutable.write_text('{"conflict":true}\n', encoding="utf-8")
        conflict = immutable.read_bytes()

        with self.assertRaisesRegex(ValueError, "archive conflicts"):
            publish_calibration_evidence(self.latest, self.second)
        self.assertEqual(self.latest.read_bytes(), original_latest)
        self.assertEqual(immutable.read_bytes(), conflict)
        self.assertFalse((self.archive / f"{self.second['content_hash']}.json").exists())

    def test_atomic_replace_failure_preserves_prior_latest_and_cleans_temporary(self):
        publish_calibration_evidence(self.latest, self.first)
        original = self.latest.read_bytes()
        with patch("research.cost_rerun.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                publish_calibration_evidence(self.latest, self.second)
        self.assertEqual(self.latest.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".calibration-*.tmp")), [])

    def test_archive_directory_symlink_is_rejected_without_external_write(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.archive.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            publish_calibration_evidence(self.latest, self.first)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.latest.exists())

    def test_existing_latest_symlink_is_rejected_without_touching_target(self):
        target = self.root / "target.json"
        target.write_text(json.dumps(self.first), encoding="utf-8")
        original = target.read_bytes()
        self.latest.symlink_to(target)

        with self.assertRaisesRegex(ValueError, "latest view must not be a symlink"):
            publish_calibration_evidence(self.latest, self.second)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(self.archive.exists())

    def test_cli_requires_calibration_only_and_distinct_publication_path(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as missing_mode:
                cost_rerun_main([
                    "--corpus", "unused", "--publish-latest", str(self.latest),
                ])
            with self.assertRaises(SystemExit) as same_path:
                cost_rerun_main([
                    "--calibration-only", "--corpus", "unused",
                    "--out", str(self.latest), "--publish-latest", str(self.latest),
                ])
        self.assertEqual(missing_mode.exception.code, 2)
        self.assertEqual(same_path.exception.code, 2)
        self.assertFalse(self.latest.exists())

    def test_cli_rejects_schedule_output_equal_to_publication_path_before_writing(self):
        config = self.root / "config.json"
        config.write_text("{}\n", encoding="utf-8")
        wrapper = {
            "schema": "stressed-cost-calibration-run.v1",
            "diagnostic_only": True,
            "authorizing": False,
            "stress_calibration": self.first,
            "activation": {"ready": True, "reasons": []},
            "cost_schedule": {"schema": "quote-cost-schedule.v1"},
        }
        with patch("research.cost_rerun.run_cost_calibration",
                   return_value=wrapper) as calibration:
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as collision:
                    cost_rerun_main([
                        "--calibration-only", "--corpus", "unused",
                        "--config", str(config),
                        "--schedule-out", str(self.latest),
                        "--publish-latest", str(self.latest),
                    ])
        self.assertEqual(collision.exception.code, 2)
        calibration.assert_not_called()
        self.assertFalse(self.latest.exists())

    def test_cli_rejects_resolved_aliases_for_every_output_pair(self):
        nested = self.root / "nested"
        nested.mkdir()
        direct = self.root / "collision.json"
        alias = nested / ".." / "collision.json"
        pairs = (
            ("--out", str(direct), "--schedule-out", str(alias)),
            ("--out", str(direct), "--publish-latest", str(alias)),
            ("--schedule-out", str(direct), "--publish-latest", str(alias)),
        )
        with patch("research.cost_rerun.run_cost_calibration") as calibration:
            for pair in pairs:
                with self.subTest(pair=pair), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as collision:
                        cost_rerun_main([
                            "--calibration-only", "--corpus", "unused", *pair,
                        ])
                    self.assertEqual(collision.exception.code, 2)
        calibration.assert_not_called()
        self.assertFalse(direct.exists())

    def test_cli_publishes_without_mutating_config_or_relaxing_out_immutability(self):
        config = self.root / "config.json"
        output = self.root / "immutable.json"
        config.write_text(json.dumps({"marker": "unchanged"}), encoding="utf-8")
        config_before = config.read_bytes()
        wrapper = {
            "schema": "stressed-cost-calibration-run.v1",
            "diagnostic_only": True,
            "authorizing": False,
            "stress_calibration": self.first,
            "activation": {"ready": True, "reasons": []},
            "cost_schedule": {"schema": "unused"},
        }
        stdout = io.StringIO()
        with patch("research.cost_rerun.run_cost_calibration", return_value=wrapper):
            with redirect_stdout(stdout):
                self.assertEqual(cost_rerun_main([
                    "--calibration-only", "--corpus", "unused",
                    "--config", str(config), "--out", str(output),
                    "--publish-latest", str(self.latest),
                ]), 0)
            output_before = output.read_bytes()
            latest_before = self.latest.read_bytes()
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    cost_rerun_main([
                        "--calibration-only", "--corpus", "unused",
                        "--config", str(config), "--out", str(output),
                        "--publish-latest", str(self.latest),
                    ])

        self.assertEqual(config.read_bytes(), config_before)
        self.assertEqual(output.read_bytes(), output_before)
        self.assertEqual(self.latest.read_bytes(), latest_before)
        self.assertEqual(json.loads(output.read_text()), self.first)
        self.assertEqual(json.loads(self.latest.read_text()), self.first)
        self.assertIn('"runtime_config_changed": false', stdout.getvalue())


class QuoteOnlyCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config.yaml")

    def test_quote_only_opt_in_calibrates_while_default_reader_rejects_no_bars(self):
        rows = _quote_rows()
        with self.assertRaisesRegex(DiscoveryError, "no underlying bars"):
            _read_discovery_rows(
                rows, require_provenance=True,
                expected_equity_feed="iex", expected_provider="alpaca")

        raw, bars, snapshots, quotes = _read_discovery_rows(
            rows, require_provenance=True, allow_quote_only=True,
            expected_equity_feed="iex", expected_provider="alpaca")
        self.assertEqual((len(raw), len(bars), len(snapshots), len(quotes)),
                         (20, 0, 0, 20))

        report = run_cost_calibration(
            rows, runtime_config=self.config, min_quotes_per_cell=1)
        artifact = report["stress_calibration"]
        body = dict(artifact)
        supplied = body.pop("content_hash")
        self.assertEqual(supplied, content_hash(body))
        self.assertEqual((report["bars"], report["quotes"]), (0, 20))
        self.assertTrue(report["evidence"]["split_valid"])
        self.assertTrue(report["diagnostic_only"])
        self.assertFalse(report["authorizing"])

    def test_wrong_feed_fails_before_measurement(self):
        with patch("research.cost_rerun.measure_quote_costs") as measure:
            with self.assertRaisesRegex(DiscoveryError, "does not match configured"):
                run_cost_calibration(
                    _quote_rows(feed="sip"), runtime_config=self.config,
                    min_quotes_per_cell=1)
        measure.assert_not_called()

    def test_future_source_fails_before_discovery_read(self):
        with patch("research.cost_rerun._read_discovery_rows") as reader:
            with self.assertRaisesRegex(SourceValidationError, "future"):
                run_cost_calibration(
                    _quote_rows(future=True), runtime_config=self.config,
                    min_quotes_per_cell=1)
        reader.assert_not_called()

    def test_measurement_failure_closes_allocated_quote_index(self):
        rows = _quote_rows()
        quote_index = Mock()
        quote_index.close = Mock()
        with patch("research.cost_rerun._read_discovery_rows",
                   return_value=(rows, [], {}, quote_index)), \
                patch("research.cost_rerun._measure_calibration_rows",
                      side_effect=RuntimeError("measurement failed")):
            with self.assertRaisesRegex(RuntimeError, "measurement failed"):
                cost_rerun_module._prepare_cost_calibration(
                    rows, runtime_config=self.config, min_quotes_per_cell=1,
                    allow_quote_only=True)
        quote_index.close.assert_called_once_with()

    def test_successful_calibration_closes_quote_index_after_counting(self):
        quote_index = Mock()
        quote_index.count = 20
        quote_index.close = Mock()
        prepared = (
            ReplayPolicy.from_config(self.config), [], [], quote_index,
            _schedule(digest="fit"), _schedule(digest="validation", session_start=10),
            None, _artifact(spread=4.0, identity="cleanup"),
            {"2026-01-01"}, {"2026-01-10"}, 10, 10, {},
        )
        with patch("research.cost_rerun._prepare_cost_calibration",
                   return_value=prepared), \
                patch("research.cost_rerun.activation_overlay", return_value={}), \
                patch("research.cost_rerun._evidence_manifest", return_value={}):
            report = run_cost_calibration(
                [], runtime_config=self.config, min_quotes_per_cell=1)
        self.assertEqual(report["quotes"], 20)
        quote_index.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
