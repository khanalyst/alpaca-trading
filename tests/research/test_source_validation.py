from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.edge_discovery_core import DiscoveryError
from research.edge_lab import discover
from research.source_validation import (
    SourceValidationError,
    source_content_hash,
    validate_source,
)


BASE = datetime(2025, 1, 6, 14, 30, tzinfo=timezone.utc)


def _bar(*, symbol: str = "SPY", source_mode="forward_observed",
         observed_at: datetime = BASE) -> dict:
    return {
        "kind": "bar", "provider": "alpaca", "feed": "iex",
        "source_mode": source_mode, "symbol": symbol,
        "timestamp": BASE.isoformat(), "as_of": BASE.isoformat(),
        "observed_at": observed_at.isoformat(),
        "open": 100.0, "high": 101.0, "low": 99.0,
        "close": 100.0, "volume": 10,
    }


class SourceValidationTests(unittest.TestCase):
    def test_implicit_source_mode_is_non_authorizing_but_diagnostic_telemetry_is_explicit(self):
        for source_mode in (None, " "):
            with self.subTest(source_mode=source_mode):
                row = _bar(source_mode=source_mode)
                with self.assertRaisesRegex(SourceValidationError, "explicit source_mode"):
                    validate_source([row])

        report = validate_source([_bar(source_mode=None)], diagnostic_only=True)
        self.assertFalse(report["authorizing"])
        self.assertEqual(report["implicit_source_mode_rows"], 1)
        # Keep the compatibility bucket for existing diagnostic consumers,
        # while the separate count makes the implicit provenance visible.
        self.assertEqual(report["source_mode_counts"], {
            "forward_observed": 1,
        })

    def test_historical_data_is_diagnostic_only(self):
        row = _bar(source_mode="historical_backfill")
        with self.assertRaisesRegex(SourceValidationError, "diagnostic-only"):
            validate_source([row])
        report = validate_source([row], diagnostic_only=True)
        self.assertFalse(report["authorizing"])
        self.assertEqual(report["source_mode_counts"], {
            "historical_backfill": 1,
        })

    def test_unlabelled_late_observation_cannot_recreate_backfill_bug(self):
        row = _bar(source_mode="")
        row["observed_at"] = (BASE + timedelta(days=30)).isoformat()
        with self.assertRaisesRegex(
                SourceValidationError, "labelled historical_backfill"):
            validate_source(
                [row], now=BASE + timedelta(days=31),
                diagnostic_only=True)

    def test_diagnostic_mode_still_rejects_structural_source_errors(self):
        for row in (
                _bar(source_mode=False),
                {**_bar(source_mode="historical_backfill"), "kind": "garbage"},
                _bar(source_mode="historical_backfill",
                     observed_at=BASE + timedelta(days=1000))):
            with self.subTest(row=row), self.assertRaises(SourceValidationError):
                validate_source(
                    [row], diagnostic_only=True,
                    now=BASE + timedelta(days=1))

    def test_discovery_diagnostic_normalizes_rows_without_opening_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "edge.sqlite3"
            result = discover(
                [_bar(source_mode="historical_backfill")],
                db_path=db_path, diagnostic_only=True)
            self.assertFalse(result["authorizing"])
            self.assertEqual(result["normalized_counts"]["bars"], 1)
            self.assertFalse(db_path.exists())

            with self.assertRaises(DiscoveryError):
                discover(
                    [_bar(symbol="", source_mode="historical_backfill")],
                    db_path=db_path, diagnostic_only=True)
            self.assertFalse(db_path.exists())

    def test_directory_window_hash_matches_the_replay_partition_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _bar(symbol="AAA")
            second = _bar(symbol="BBB")
            (root / "2025-01-06.jsonl").write_text(
                json.dumps(first) + "\n", encoding="utf-8")
            (root / "2025-01-07.jsonl").write_text(
                json.dumps(second) + "\n", encoding="utf-8")
            with patch.dict(os.environ, {
                    "ALPACA_RESEARCH_SESSION_WINDOW": "1"}):
                report = validate_source(root)
            self.assertEqual(report["rows"], 1)
            self.assertEqual(report["content_hash"],
                             source_content_hash([second]))


if __name__ == "__main__":
    unittest.main()
