"""Recorder declares only newly observed partition contents as forward."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy import recorder
from tests.test_deploy import _QuoteChunkFake


class RecorderForwardDeclarationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'market.csv'
        self.now = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
        self.day = self.now.date()
        for context in (patch.object(recorder.shutil, 'disk_usage', return_value=SimpleNamespace(free=10 * 1024**3)),
                        patch.dict('os.environ', {'ALPACA_RECORDER_CAPTURE_POLICY': 'forward_only',
                                                 'ALPACA_RECORDER_FETCH_WINDOW_MINUTES': '1'})):
            context.start()
            self.addCleanup(context.stop)

    def record(self, now=None):
        clock = now or self.now
        class ReceivedAt(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock
        with patch('deploy.recorder_market.datetime', ReceivedAt):
            return recorder.record_once(_QuoteChunkFake(), ['SPY'], self.path, now=clock)

    def test_new_live_partition_is_declared_before_append_and_survives_rebuild(self):
        original = recorder._append_partitions
        def append(*args, **kwargs):
            marker = recorder._partition_source_path(self.path, self.day)
            self.assertEqual(json.loads(marker.read_text())['source_mode'], 'forward_observed')
            return original(*args, **kwargs)
        with patch.object(recorder, '_append_partitions', side_effect=append):
            self.assertGreater(self.record(), 0)
        self.assertEqual(recorder._scan_corpus(self.path)['partition_sources'], {
            recorder._partition_path(self.path, self.day).name: {'source_mode': 'forward_observed'}})

    def test_existing_unmarked_nonempty_partition_is_not_relabelled(self):
        self.record()
        recorder._partition_source_path(self.path, self.day).unlink()
        self.assertGreater(self.record(self.now + timedelta(minutes=1)), 0)
        self.assertFalse(recorder._partition_source_path(self.path, self.day).exists())

    def test_historical_marker_cannot_be_downgraded(self):
        recorder._save_partition_source(self.path, self.day, 'historical_backfill')
        recorder._save_partition_source(self.path, self.day, 'forward_observed')
        self.assertEqual(json.loads(recorder._partition_source_path(self.path, self.day).read_text())['source_mode'], 'historical_backfill')
        self.record()
        self.assertEqual(recorder._scan_corpus(self.path)['partition_sources'][
            recorder._partition_path(self.path, self.day).name]['source_mode'], 'historical_backfill')

    def test_backfill_overrides_previous_forward_declaration(self):
        recorder._save_partition_source(self.path, self.day, 'forward_observed')
        recorder._save_partition_source(self.path, self.day, 'historical_backfill')
        self.assertEqual(json.loads(recorder._partition_source_path(self.path, self.day).read_text())['source_mode'], 'historical_backfill')

    def test_marker_before_csv_crash_retains_declared_source_in_index(self):
        recorder._save_partition_source(self.path, self.day, 'forward_observed')
        self.record()
        self.assertEqual(recorder._prepare_index(self.path)['partition_sources'][
            recorder._partition_path(self.path, self.day).name]['source_mode'], 'forward_observed')
