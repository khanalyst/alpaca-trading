"""Immutable historical inputs and strict outward JSON are enforced."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import report
from research import download_okx_history as downloader
from research import tournament


def strict_loads(value: str):
    def reject(token: str):
        raise ValueError(token)

    return json.loads(value, parse_constant=reject)


class SnapshotFixture(unittest.TestCase):
    def snapshot(self, directory: str) -> tuple[Path, Path, dict]:
        root = Path(directory) / "snapshot"
        swap = root / "swap" / "BTC_USDT_USDT.csv"
        swap.parent.mkdir(parents=True)
        pd.DataFrame({
            "timestamp_ms": [1_000, 2_000, 3_000],
            "open": [1.0, 2.0, 3.0],
            "high": [2.0, 3.0, 4.0],
            "low": [0.5, 1.5, 2.5],
            "close": [1.5, 2.5, 3.5],
            "volume": [10.0, 11.0, 12.0],
            "quote_volume": [15.0, 27.5, 42.0],
        }).to_csv(swap, index=False)
        manifest = {
            "schema": downloader.MANIFEST_SCHEMA,
            "files": downloader.snapshot_file_manifest(root),
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, allow_nan=False) + "\n", encoding="utf-8")
        return root, swap, manifest


class DownloaderSnapshotTests(SnapshotFixture):
    def test_manifest_records_exact_hash_rows_and_time_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root, swap, manifest = self.snapshot(directory)

            self.assertEqual(manifest["files"], [{
                "path": "swap/BTC_USDT_USDT.csv",
                "sha256": hashlib.sha256(swap.read_bytes()).hexdigest(),
                "rows": 3,
                "first_timestamp_ms": 1_000,
                "last_timestamp_ms": 3_000,
            }])
            self.assertEqual(
                tournament.validate_data_snapshot(root), manifest)

    def test_dirty_output_is_refused_before_any_market_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "existing-export"
            root.mkdir()
            original = root / "do-not-overwrite.csv"
            original.write_text("old export\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch.object(sys, "argv", [
                    "download_okx_history.py", "--out", str(root)]), \
                    patch.object(downloader, "discover_symbols") as discover, \
                    contextlib.redirect_stderr(stderr):
                status = downloader.main()

            self.assertEqual(status, 2)
            discover.assert_not_called()
            self.assertEqual(original.read_text(encoding="utf-8"),
                             "old export\n")
            self.assertIn("refusing dirty output", stderr.getvalue())

    def test_snapshot_directory_has_one_exclusive_download_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fresh"

            lock = downloader.claim_output(root)

            self.assertTrue(lock.is_file())
            with self.assertRaisesRegex(RuntimeError, "refusing dirty output"):
                downloader.claim_output(root)


class TournamentSnapshotValidationTests(SnapshotFixture):
    def test_extra_missing_hash_row_and_range_mismatches_are_refused(self):
        defects = ("extra", "missing", "sha256", "rows", "range")
        for defect in defects:
            with self.subTest(defect=defect), \
                    tempfile.TemporaryDirectory() as directory:
                root, swap, manifest = self.snapshot(directory)
                if defect == "extra":
                    extra = root / "funding" / "EXTRA.csv"
                    extra.parent.mkdir()
                    extra.write_text(
                        "timestamp_ms,funding_rate\n1000,0.1\n",
                        encoding="utf-8")
                elif defect == "missing":
                    swap.unlink()
                else:
                    field = {
                        "sha256": "sha256", "rows": "rows",
                        "range": "first_timestamp_ms",
                    }[defect]
                    manifest["files"][0][field] = (
                        "0" * 64 if defect == "sha256" else 999)
                    (root / "manifest.json").write_text(
                        json.dumps(manifest, allow_nan=False) + "\n",
                        encoding="utf-8")

                with self.assertRaises(tournament.TournamentFailure) as raised:
                    tournament.validate_data_snapshot(root)

                self.assertEqual(raised.exception.exit_code, 2)
                self.assertIn(
                    "membership mismatch" if defect in {"extra", "missing"}
                    else "mismatch",
                    str(raised.exception))

    def test_legacy_manifest_is_preserved_but_not_scored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "manifest.json"
            original = '{"source":"legacy export","symbols":[]}\n'
            legacy.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(
                    tournament.TournamentFailure, "legacy exports remain"):
                tournament.validate_data_snapshot(root)

            self.assertEqual(legacy.read_text(encoding="utf-8"), original)


class StrictJsonTests(unittest.TestCase):
    def test_tournament_json_converts_every_non_finite_number_to_null(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "leaderboard.json"
            tournament._json_write_once(path, {
                "values": [float("nan"), float("inf"), float("-inf"), 1.25],
                "nested": {"ratio": float("nan")},
            })

            parsed = strict_loads(path.read_text(encoding="utf-8"))

        self.assertEqual(parsed["values"], [None, None, None, 1.25])
        self.assertIsNone(parsed["nested"]["ratio"])

    def test_live_report_json_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "journal.db"
            with sqlite3.connect(db_path):
                pass
            output = io.StringIO()
            with patch.object(report, "json_report", return_value={
                    "mde": float("inf"), "ratio": float("nan")}), \
                    contextlib.redirect_stdout(output):
                status = report.main(db_path, as_json=True)

        self.assertEqual(status, 0)
        self.assertEqual(strict_loads(output.getvalue()), {
            "mde": None, "ratio": None,
        })

    def test_committed_latest_leaderboard_is_strict_json(self):
        path = (Path(__file__).resolve().parents[2] / "research" / "results"
                / "tournament" / "leaderboard.json")
        strict_loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
