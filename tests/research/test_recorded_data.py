"""Event-plane ingestion and strict as-of invariants."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.recorded_data import EventPlane


class RecordedDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recorded = self.root / "recorded"
        (self.recorded / "order_book").mkdir(parents=True)
        self.archive = self.root / "archive"
        self.db = self.root / "market_events.db"

    def write(self, content: str) -> None:
        (self.recorded / "order_book" / "2026-08-07.csv").write_text(
            content, encoding="utf-8")

    def test_idempotent_ingest_preserves_revisions_and_archive_stability(self):
        self.write(
            "ts,inst_id,mid,observed_at,available_at,source,feed,schema\n"
            "100,BTC,100,110,111,okx,book,v1\n"
            "100,BTC,101,120,121,okx,book,v1\n")
        plane = EventPlane(self.db)
        first = plane.ingest(self.recorded, archive_root=self.archive)
        second = plane.ingest(self.recorded, archive_root=self.archive)
        self.assertEqual(first["rows_inserted"], 2)
        self.assertEqual(second["rows_inserted"], 0)
        self.assertIsNone(second["snapshot_id"])
        self.assertEqual(len(plane.query(
            series="order_book", instrument="BTC", decision_time_ms=200,
            cutoff_event_ts_ms=100, feed="book")), 2)
        manifests = list((self.archive / "snapshots").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        blob_files = list((self.archive / "blobs").glob("*.csv"))
        self.assertEqual(len(blob_files), 1)

    def test_legacy_is_unknown_and_malformed_rows_are_quarantined(self):
        self.write(
            "ts,inst_id,mid,observed_at,available_at\n"
            "100,BTC,100,,\n"
            "bad,BTC,101,110,111\n"
            "102,BTC,102,110,111,unexpected\n")
        # The final row has an extra field and should be diagnosed explicitly.
        plane = EventPlane(self.db)
        report = plane.ingest(self.recorded, archive_root=self.archive)
        self.assertEqual(report["rows_valid"], 1)
        self.assertEqual(report["rows_quarantined"], 2)
        with sqlite3.connect(self.db) as conn:
            quality = conn.execute(
                "SELECT quality FROM market_events").fetchone()[0]
            reasons = [row[0] for row in conn.execute(
                "SELECT error FROM quarantine_diagnostics ORDER BY diagnostic_id")]
        self.assertEqual(quality, "legacy_unknown")
        self.assertEqual(len(reasons), 2)
        self.assertFalse(plane.query_asof(
            series="order_book", instrument="BTC", decision_time_ms=200)["events"])

    def test_asof_requires_available_and_causal_times_and_digest_is_deterministic(self):
        self.write(
            "ts,inst_id,mid,observed_at,available_at,source,feed,schema,status\n"
            "100,BTC,100,110,210,okx,book,v1,forecast\n"
            "100,BTC,101,120,121,okx,book,v1,forecast\n"
            "200,BTC,102,120,121,okx,book,v1,realized\n")
        plane = EventPlane(self.db)
        plane.ingest(self.recorded, archive_root=self.archive)
        before_available = plane.as_of(
            series="order_book", instrument="BTC", decision_time_ms=120,
            cutoff_event_ts_ms=100)
        self.assertIsNone(before_available["event"])
        self.assertEqual(before_available["reason"], "missing_data")
        result = plane.as_of(
            series="order_book", instrument="BTC", decision_time_ms=130,
            cutoff_event_ts_ms=100)
        self.assertEqual(result["event"]["payload_json"],
                         json.dumps({"inst_id": "BTC", "mid": "101", "status": "forecast", "ts": "100"},
                                    sort_keys=True, separators=(",", ":")))
        # The future event is excluded by causal event_ts <= cutoff.
        joined = plane.join_asof([
            {"key": "book", "series": "order_book", "instrument": "BTC"},
            {"key": "missing", "series": "other", "instrument": "BTC"},
        ], decision_time_ms=130, cutoff_event_ts_ms=100)
        self.assertEqual(joined["reason"], "missing_data")
        self.assertEqual(joined["event_join_digest"], plane.join_asof([
            {"key": "missing", "series": "other", "instrument": "BTC"},
            {"key": "book", "series": "order_book", "instrument": "BTC"},
        ], decision_time_ms=130, cutoff_event_ts_ms=100)["event_join_digest"])

    def test_contradictory_provenance_is_quarantined_and_future_observation_is_hidden(self):
        self.write(
            "ts,inst_id,mid,observed_at,available_at,source,feed,schema\n"
            "100,BTC,100,110,111,okx,book,v1\n"
            "101,BTC,101,500,300,okx,book,v1\n")
        plane = EventPlane(self.db)
        report = plane.ingest(self.recorded, archive_root=self.archive)
        self.assertEqual(report["rows_valid"], 1)
        self.assertEqual(report["rows_quarantined"], 1)
        # The future-observed / past-available row is quarantined, so only the
        # causal, observed event remains visible.
        result = plane.as_of(
            series="order_book", instrument="BTC", decision_time_ms=400,
            cutoff_event_ts_ms=101)
        self.assertEqual(result["event"]["event_ts_ms"], 100)


if __name__ == "__main__":
    unittest.main()
