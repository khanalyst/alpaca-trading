"""Explicit forward provenance at the authorizing shadow boundary."""
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.live_shadow import (
    ShadowConfig, ShadowRunner, ShadowError, _RecordedSessionCalendarSnapshot,
    _authorizing_projected_payload, _partition_source_identity,
)
from research.costs import ReplayPolicy


class AuthorizingProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'sessions' / 'market-2026-01-02.csv'
        self.source.parent.mkdir()
        self.source.write_text('event_key\n')
        self.runner = ShadowRunner(ShadowConfig(
            self.root / 'recorded.csv', self.root / 'edge.sqlite3',
            self.root / 'shadow.sqlite3'))

    def marker(self, mode):
        self.source.with_name(self.source.name + '.source.json').write_text(json.dumps({
            'schema': 'recorder-partition-source.v1',
            'partition': self.source.name, 'source_mode': mode}))

    def envelope(self, mode=None):
        body = {'event_key': 'bar', 'event_type': 'bar_1m', 'symbol': 'SPY',
                'timestamp': '2026-01-02T14:30:00+00:00',
                'as_of': '2026-01-02T14:31:00+00:00',
                'observed_at': '2026-01-02T14:31:00+00:00',
                'provider': 'alpaca', 'feed': 'iex',
                'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 100}
        if mode is not None:
            body['source_mode'] = mode
        return {**body, 'source_path': str(self.source), 'event_json': json.dumps(body)}

    def test_unlabelled_and_historical_rows_do_not_authorize(self):
        for mode in (None, '', 'historical_backfill', 'unknown'):
            with self.subTest(mode=mode):
                self.assertIsNone(_authorizing_projected_payload(self.envelope(mode), {}))
                result = self.runner._evaluate({}, self.envelope(mode), {}, {}, {})
                self.assertEqual(result[:2], ('no_data', 'authorizing source provenance is not forward observed'))

    def test_explicit_forward_row_is_accepted(self):
        self.assertEqual(_authorizing_projected_payload(
            self.envelope('forward_observed'), {})['source_mode'], 'forward_observed')

    def test_new_partition_declaration_can_supply_explicit_provenance(self):
        self.marker('forward_observed')
        result = _authorizing_projected_payload(self.envelope(), {})
        self.assertEqual(result['source_mode'], 'forward_observed')
        self.assertEqual(result['_recorded_source_path'], str(self.source))

    def test_historical_marker_overrides_forward_row_claim(self):
        self.marker('historical_backfill')
        self.assertIsNone(_authorizing_projected_payload(self.envelope('forward_observed'), {}))

    def test_forward_marker_does_not_override_historical_row(self):
        self.marker('forward_observed')
        self.assertIsNone(_authorizing_projected_payload(self.envelope('historical_backfill'), {}))

    def test_invalid_partition_declaration_fails_closed(self):
        self.marker('unknown')
        with self.assertRaises(ShadowError):
            _authorizing_projected_payload(self.envelope('forward_observed'), {})

    def test_loaders_remove_unproven_and_historical_context(self):
        rows = [self.envelope(), self.envelope('historical_backfill'), self.envelope('forward_observed')]
        with patch.object(self.runner.store, 'events', return_value=rows):
            events, bars, _, _ = self.runner._load_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(len(bars['SPY']), 1)
        self.assertEqual(bars['SPY'][0]['source_mode'], 'forward_observed')

    def test_marker_change_invalidates_event_cache_and_replay_memo(self):
        kwargs = dict(candidates=[], diagnostic_arms=[],
                      calendar_snapshot=_RecordedSessionCalendarSnapshot(sessions=(), bounds=()),
                      source_offsets={}, forward_event_floor=0, event_watermark={},
                      quarantine={}, replay_quarantine={}, replay_state={}, virtual_book_state=[])
        candidate = {'candidate_id': 'test', 'strategy_id': 'ibr', 'vehicle': 'equity',
                     'config': {'strategy': {'id': 'ibr'}, 'risk': {}, 'execution': {}, 'session': {}}}
        def keys():
            return (self.runner._authorizing_event_fingerprint(**kwargs),
                    self.runner._authorizing_replay_memo_key(
                        candidate, '2026-01-02', {}, {}, {}, {}, {}, None, []),
                    _partition_source_identity(self.runner.config.corpus_path))
        before = keys()
        self.marker('historical_backfill')
        after = keys()
        for old, new in zip(before, after):
            self.assertNotEqual(old, new)

    def test_unproven_bars_and_quotes_cannot_price_authorizing_book(self):
        at = datetime.fromisoformat('2026-01-02T14:31:00+00:00')
        policy = ReplayPolicy(equity_feed='iex', equity_provider='alpaca')
        for mode in (None, 'historical_backfill'):
            row = json.loads(self.envelope(mode)['event_json'])
            self.assertIsNone(self.runner._worker_bar_mark(
                [row], symbol='SPY', at=at, policy=policy))
            quote = {**row, 'event_type': 'quote', 'bid': 99, 'ask': 101}
            self.assertIsNone(self.runner._worker_exit_quote(
                [quote], symbol='SPY', at=at, visible_by=at,
                direction='long', policy=policy))

    def test_reclassified_partition_cannot_supply_a_cached_bar_mark(self):
        self.marker('forward_observed')
        row = _authorizing_projected_payload(self.envelope(), {})
        self.marker('historical_backfill')
        at = datetime.fromisoformat('2026-01-02T14:31:00+00:00')
        self.assertIsNone(self.runner._worker_bar_mark(
            [row], symbol='SPY', at=at,
            policy=ReplayPolicy(equity_feed='iex', equity_provider='alpaca')))

    def test_unproven_replay_is_incomplete_without_simulation(self):
        candidate = {'candidate_id': 'test', 'strategy_id': 'ibr', 'vehicle': 'equity',
                     'config': {'strategy': {'id': 'ibr'}, 'risk': {}, 'execution': {}, 'session': {}}}
        row = json.loads(self.envelope()['event_json'])
        row.update(timestamp='2026-01-02T20:59:00+00:00',
                   as_of='2026-01-02T21:00:00+00:00',
                   observed_at='2026-01-02T21:00:00+00:00')
        with patch('research.live_shadow.replay_ibr') as simulate:
            self.runner._replay(candidate, '2026-01-02', [row], [], [],
                                replay_identity={key: 'a' * 64 for key in (
                                    'replay_code_hash', 'manifest_digest',
                                    'candidate_set_digest', 'replay_epoch_identity')})
        simulate.assert_not_called()
        self.assertEqual(self.runner.store.replay_metadata('test')[0]['status'], 'incomplete')
