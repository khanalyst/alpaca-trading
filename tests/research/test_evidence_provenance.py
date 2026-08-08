import tempfile
import unittest
from pathlib import Path

from research import backup, provenance
from research.evidence_primitives import canonical_opportunity_key, index_rows
from research.gates import AcceptanceFloor, chronological_split, paired_delta


class EvidenceProvenanceTests(unittest.TestCase):
    def test_duplicate_opportunity_is_excluded_from_resolution(self):
        rows = [
            {"symbol": "SPY", "signal_ts": 1, "direction": "long",
             "setup_type": "ibr", "net_pnl": 1},
            {"symbol": "SPY", "signal_ts": 1, "direction": "long",
             "setup_type": "ibr", "net_pnl": 2},
        ]
        indexed = index_rows(rows, value_fn=lambda row: row["net_pnl"])
        self.assertEqual(indexed.duplicate_count, 1)
        self.assertFalse(indexed.resolved)
        self.assertEqual(canonical_opportunity_key(rows[0]),
                         canonical_opportunity_key(rows[1]))

    def test_provenance_hash_is_canonical_and_attempts_are_immutable(self):
        self.assertEqual(provenance.content_hash({"b": 2, "a": 1}),
                         provenance.content_hash({"a": 1, "b": 2}))
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.db"
            provenance.record_authoring_attempt(
                db, generation=1, requested_ts=1, completed_ts=2,
                provider="fixture", model_id="none", prompt_version="v1",
                request={"x": 1}, context={"session": "2024-01-02"},
                evidence={"digest": "abc"}, raw_response=None,
                parser_status="NOT_RUN", validation_status="NOT_RUN",
                error=None, returned_contract_ids=[], accepted_contract_ids=[],
                rejections=[], result={}, status="SUCCEEDED")
            self.assertEqual(len(provenance.authoring_attempts(db)), 1)

    def test_floor_and_pairing_are_vehicle_local(self):
        rows = [{"vehicle": "equity", "session_date": "2024-01-02",
                 "net_pnl": 1, "opportunity_id": "a"}]
        floor = AcceptanceFloor(min_trades=1, min_sessions=1)
        self.assertTrue(floor.check(rows, vehicle="equity")["passes"])
        self.assertFalse(floor.check(rows, vehicle="option")["passes"])
        pair = paired_delta(rows, [{"vehicle": "equity", "opportunity_id": "a",
                                    "net_pnl": .5}], vehicle="equity")
        self.assertEqual(pair["matched"], 1)

    def test_backup_manifest_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            target = Path(tmp) / "backup"
            source.mkdir()
            (source / "evidence.json").write_text('{"ok":true}\n', encoding="utf-8")
            backup.create_backup(source, target)
            report = backup.verify_backup(target)
            self.assertTrue(report["valid"])
            (target / "evidence.json").write_text("tampered\n", encoding="utf-8")
            self.assertFalse(backup.verify_backup(target)["valid"])

