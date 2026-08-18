from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deploy.research_dataset import build_views
from research.costs import (SQLiteQuoteIndex, SQLiteQuoteIndexDescriptor,
                             quote_fill)
from research.edge_discovery_core import DiscoveryError
import research.gates as gates
import research.strategy_factory as factory_module
from research.strategy_factory import run_factory
from research.market_data import normalize_quote


BASE = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def _quote(minute: int, bid: float, ask: float, *, as_of_minute: int | None = None):
    timestamp = BASE + timedelta(minutes=minute)
    as_of = BASE + timedelta(minutes=(minute if as_of_minute is None else as_of_minute))
    return normalize_quote({
        "kind": "quote", "provider": "test", "feed": "sip", "symbol": "SPY",
        "timestamp": timestamp.isoformat(), "as_of": as_of.isoformat(),
        "observed_at": as_of.isoformat(), "bid": bid, "ask": ask,
    })


class QuoteIndexDescriptorTests(unittest.TestCase):
    def test_read_only_descriptor_opens_legacy_schema_without_observed_at(self):
        # Version-1 indexes predate local observation metadata.  They remain
        # auditable by treating the old as_of column as observed_at.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute("""
                    CREATE TABLE quotes (
                        symbol_id INTEGER NOT NULL,
                        timestamp REAL NOT NULL,
                        as_of REAL NOT NULL,
                        bid REAL NOT NULL,
                        ask REAL NOT NULL,
                        provider TEXT NOT NULL,
                        feed TEXT NOT NULL,
                        session_day INTEGER NOT NULL,
                        sequence INTEGER NOT NULL,
                        PRIMARY KEY (symbol_id, timestamp, sequence)
                    ) WITHOUT ROWID
                """)
                db.execute(
                    "INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?)",
                    (1, BASE.timestamp(), BASE.timestamp(), 100.0, 100.1,
                     "test", "sip", BASE.date().toordinal(), 0),
                )
                db.commit()
            finally:
                db.close()
            descriptor = SQLiteQuoteIndexDescriptor(
                path=str(path), symbols=(("SPY", 1),), count=1,
                max_session_date=BASE.date().isoformat(), schema_version=1)
            child = SQLiteQuoteIndex.open_read_only(descriptor)
            try:
                self.assertEqual(
                    quote_fill(child, symbol="SPY", at=BASE, side="buy"), 100.1)
                self.assertEqual(child.descriptor().schema_version, 1)
            finally:
                child.close()

    def test_read_only_descriptor_matches_tie_and_rejects_delayed_as_of(self):
        parent = SQLiteQuoteIndex()
        try:
            parent.add(_quote(-1, 98, 99))
            delayed = _quote(0, 100, 101, as_of_minute=5)
            parent.add(delayed)
            descriptor = parent.descriptor()
            child = SQLiteQuoteIndex.open_read_only(descriptor)
            try:
                # The delayed row is not visible yet; the preceding quote is.
                self.assertEqual(
                    quote_fill(child, symbol="SPY", at=BASE + timedelta(minutes=1),
                               side="buy", max_age_seconds=180), 99.0)
            finally:
                child.close()

            # Equal timestamps use insertion sequence as the deterministic tie
            # breaker, and a read-only child does not remove the parent's file.
            parent.add(_quote(0, 102, 103))
            parent.add(_quote(0, 104, 105))
            descriptor = parent.descriptor()
            child = SQLiteQuoteIndex.open_read_only(descriptor)
            try:
                self.assertEqual(
                    quote_fill(child, symbol="SPY", at=BASE, side="buy"), 105.0)
                self.assertEqual(child.count, descriptor.count)
                self.assertEqual(child.max_session_date.isoformat(),
                                 descriptor.max_session_date)
                self.assertTrue(Path(descriptor.path).is_file())
            finally:
                child.close()
        finally:
            parent.close()

    def test_replay_view_excludes_quotes_and_reports_same_pass_counts(self):
        rows = [
            {"kind": "bar", "symbol": "SPY", "timestamp": BASE.isoformat()},
            {"kind": "quote", "symbol": "SPY", "timestamp": BASE.isoformat()},
            {"kind": "option_snapshot", "symbol": "SPY", "timestamp": BASE.isoformat()},
            {"kind": "option_quote", "symbol": "SPY", "timestamp": BASE.isoformat()},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                              encoding="utf-8")
            outputs = {name: root / name for name in ("normalized", "bars", "quotes",
                                                       "options", "replay")}
            result = build_views(
                source, input_format="jsonl", normalized=outputs["normalized"],
                bars=outputs["bars"], quotes=outputs["quotes"],
                options=outputs["options"], replay=outputs["replay"])
            self.assertEqual(result["view_counts"], {
                "normalized": 4, "bars": 1, "quotes": 1,
                "options": 2, "replay": 3,
            })
            replay_rows = [line for line in outputs["replay"].read_text().splitlines()
                           if line.strip()]
            self.assertEqual(len(replay_rows), 3)
            self.assertNotIn('"kind": "quote"', outputs["replay"].read_text())

    def test_malformed_full_quote_fails_before_worker_data_can_be_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full = root / "full.jsonl"
            full.write_text(
                '{"kind":"bar","symbol":"SPY","timestamp":"2026-01-05T14:30:00+00:00",'
                '"as_of":"2026-01-05T14:31:00+00:00","observed_at":"2026-01-05T14:31:00+00:00",'
                '"open":100,"high":101,"low":99,"close":100,"volume":10}\n'
                '{"kind":"quote","symbol":"SPY","timestamp":"not-a-time",'
                '"as_of":"not-a-time","observed_at":"not-a-time","bid":1,"ask":2}\n',
                encoding="utf-8")
            replay = root / "replay.jsonl"
            replay.write_text(full.read_text().splitlines()[0] + "\n", encoding="utf-8")
            # Reach the source-descriptor validation before the compact fixture
            # is rejected for having fewer authorization samples than the
            # immutable production floors.
            with patch.multiple(
                    gates,
                    PROTOCOL_BACKTEST_MIN_TRADES=1,
                    PROTOCOL_BACKTEST_MIN_SESSIONS=1,
                    PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
                    PROTOCOL_SHADOW_MIN_TRADES=1,
                    PROTOCOL_SHADOW_MIN_SESSIONS=1,
                    PROTOCOL_SHADOW_MIN_CLUSTERS=1,
                    PROTOCOL_QUALIFICATION_MIN_TRADES=1,
                    PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
                    PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1), \
                    patch.object(factory_module, "MIN_PROMOTION_CLUSTERS", 1), \
                    self.assertRaises(DiscoveryError):
                run_factory(full, worker_data=replay,
                            db_path=root / "factory.sqlite3", strategies=1,
                            variants_per_strategy=2, workers=1, min_trades=1,
                            min_sessions=1, alpha=1.0)


if __name__ == "__main__":
    unittest.main()
