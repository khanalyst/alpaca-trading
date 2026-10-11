"""Whole-poll shadow telemetry and cursor transaction regressions."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from deploy import health
from deploy.recorder_market import _event_key
from research.edge_ledger import EdgeLedger
from research.live_shadow import ShadowConfig, ShadowRunner, ShadowStore


FIELDS = [
    "event_key", "event_type", "symbol", "timestamp", "as_of",
    "observed_at", "provider", "feed", "open", "high", "low",
    "close", "volume", "bid", "ask",
]


class ShadowPollMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.corpus = root / "recorded.csv"
        self.edge = root / "edge.sqlite3"
        self.shadow = root / "shadow.sqlite3"
        EdgeLedger(self.edge)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _bar(stamp: datetime, index: int) -> dict[str, str]:
        value = str(100 + index)
        return {
            "event_key": _event_key("bar_1m", "SPY", stamp.isoformat()),
            "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": stamp.isoformat(),
            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": (stamp + timedelta(minutes=2)).isoformat(),
            "provider": "alpaca", "feed": "iex", "open": value,
            "high": str(101 + index), "low": str(99 + index),
            "close": value, "volume": "1000", "bid": "", "ask": "",
        }

    def _write(self, rows: list[dict[str, str]]) -> None:
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _runner(self) -> ShadowRunner:
        return ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=False,
            max_events=100, max_decisions=100))

    def _candidate(self) -> dict:
        candidate = EdgeLedger(self.edge).register_candidate(
            "ibr.baseline", strategy_id="ibr", vehicle="equity",
            hypothesis="ibr.baseline", config={
                "strategy": {"id": "ibr", "version": "v1",
                             "variant_id": "ibr.baseline"},
                "risk": {"risk_per_trade_pct": 1},
                "execution": {}, "session": {},
            })
        with closing(sqlite3.connect(self.edge)) as db, db:
            db.execute(
                "UPDATE candidate_state SET status='backtest_passed' "
                "WHERE candidate_id=?", (candidate["candidate_id"],))
        return candidate

    def test_whole_poll_metrics_are_published_for_empty_poll(self):
        self._write([])
        result = self._runner().run_once()

        metrics = result["poll_phase_metrics"]
        self.assertEqual(set(metrics), {
            "calendar_seconds", "candidate_preparation_seconds",
            "corpus_read_seconds", "ingest_seconds",
            "diagnostic_snapshot_seconds", "diagnostic_seconds",
            "authorizing_snapshot_seconds", "authorizing_evaluation_seconds",
            "coverage_seconds", "finalization_seconds", "total_seconds",
        })
        self.assertGreaterEqual(metrics["total_seconds"], 0.0)
        self.assertTrue(all(value >= 0.0 for value in metrics.values()))

    def test_ingest_batch_advances_corpus_cursor_once(self):
        start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
        rows = [self._bar(start + timedelta(minutes=index), index)
                for index in range(3)]
        self._write(rows)
        calls: list[tuple[str, str]] = []
        original = ShadowStore._update_corpus_cursor

        def update(db, *, timestamp, event_key, digest):
            calls.append((timestamp, event_key))
            return original(db, timestamp=timestamp, event_key=event_key,
                            digest=digest)

        with patch.object(ShadowStore, "_update_corpus_cursor",
                          side_effect=update):
            result = self._runner().run_once()

        self.assertEqual(result["ingested_events"], 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], rows[-1]["event_key"])
        with ShadowStore(self.shadow, readonly=True)._connection() as db:
            cursor = db.execute(
                "SELECT last_timestamp,last_event_key FROM cursor "
                "WHERE scope='corpus'").fetchone()
        self.assertEqual(cursor[1], rows[-1]["event_key"])

    def test_causal_event_prefix_normalizes_timezone_and_sees_late_rows(self):
        store = ShadowStore(self.shadow)
        row = self._bar(
            datetime(2026, 1, 2, 20, 59, tzinfo=timezone.utc), 0)
        store.ingest_event(row, max_events=100)
        utc_identity = store.event_prefix_identity(
            datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc))
        offset_identity = store.event_prefix_identity(
            datetime.fromisoformat("2026-01-03T01:00:00+04:00"))
        self.assertEqual(utc_identity, offset_identity)

        late = self._bar(
            datetime(2026, 1, 2, 20, 58, tzinfo=timezone.utc), 1)
        store.ingest_event(late, max_events=100)
        late_identity = store.event_prefix_identity(
            datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(late_identity["row_count"],
                         utc_identity["row_count"] + 1)
        self.assertNotEqual(late_identity["last_event_key"],
                            utc_identity["last_event_key"])

    def test_repeated_unchanged_authorizing_input_avoids_event_reload(self):
        self._candidate()
        start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
        first = self._bar(start, 0)
        second = self._bar(start + timedelta(minutes=1), 1)
        self._write([first])
        runner = self._runner()
        # Keep this test focused on the event-view cache; authorizing signal
        # behavior is covered by the incremental poll suite.
        def no_trade(candidate, event, bars, quotes, options):
            del bars, quotes, options
            return ("no_trade", "cache test", {
                "session_date": str(event["timestamp"])[:10],
                "strategy_id": candidate.get("strategy_id"),
            }, None)

        with patch.object(runner, "_evaluate", side_effect=no_trade):
            runner.run_once()  # Establishes the forward floor.
            runner.run_once()  # Rebuilds the now-empty post-floor view.
            with patch.object(runner, "_load_events",
                              wraps=runner._load_events) as load_events:
                cached = runner.run_once()
            load_events.assert_not_called()
            self.assertTrue(cached["authorizing_event_cache_hit"])

            with self.corpus.open("a", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=FIELDS).writerow(second)
            with patch.object(runner, "_load_events",
                              wraps=runner._load_events) as load_events:
                changed = runner.run_once()
            load_events.assert_called_once()
            self.assertFalse(changed["authorizing_event_cache_hit"])

    def test_cache_replay_projection_ignores_unrelated_history(self):
        candidate = self._candidate()
        start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
        self._write([self._bar(start, 0)])
        runner = self._runner()

        def no_trade(candidate, event, bars, quotes, options):
            del bars, quotes, options
            return ("no_trade", "cache history test", {
                "session_date": str(event["timestamp"])[:10],
                "strategy_id": candidate.get("strategy_id"),
            }, None)

        with patch.object(runner, "_evaluate", side_effect=no_trade):
            runner.run_once()
            runner.run_once()
            for index in range(64):
                runner.store.replay_diff(
                    candidate_id=f"historical-{index}",
                    session_date=f"2024-01-{(index % 28) + 1:02d}",
                    source_digest=f"source-{index}",
                    shadow_digest=f"shadow-{index}",
                    replay_digest=f"replay-{index}",
                    status="match",
                    details={"opportunity_capacity": {"index": index}},
                )
            with patch.object(runner.store, "replay_metadata",
                              side_effect=AssertionError(
                                  "cache must not scan replay metadata")):
                with patch.object(
                        runner.store, "replay_diff_state_projection",
                        wraps=runner.store.replay_diff_state_projection) as projection:
                    result = runner.run_once()
                    projection.assert_called_once()

            self.assertTrue(result["authorizing_event_cache_hit"])
            projected = runner.store.replay_diff_state_projection(
                [candidate["candidate_id"]])
            self.assertTrue(all(
                row["candidate_id"] == candidate["candidate_id"]
                for row in projected
            ))
            self.assertTrue(all(
                row["candidate_id"] == candidate["candidate_id"]
                for row in runner.store.replay_metadata_tail(
                    [candidate["candidate_id"]])
            ))

    def test_closed_replay_memo_skips_historical_evaluation_and_retry_churn(self):
        candidate = self._candidate()
        closing = self._bar(
            datetime(2026, 1, 2, 20, 59, tzinfo=timezone.utc), 0)
        self._write([closing])
        runner = self._runner()
        store = runner.store
        store.upsert_candidate(candidate)
        store.ingest_event(closing, max_events=100)
        store.save_source_offsets({str(self.corpus.resolve()):
                                   self.corpus.stat().st_size})
        store.save_forward_event_floor(time.time() + 3600)
        store.decision(
            candidate_id=candidate["candidate_id"],
            event_key=closing["event_key"], session_date="2026-01-02",
            symbol="SPY", kind="no_trade", reason="seed",
            payload={"session_date": "2026-01-02"}, max_decisions=100)
        replay_digest = "stable-replay"
        store.replay_diff(
            candidate_id=candidate["candidate_id"], session_date="2026-01-02",
            source_digest="stable-source", shadow_digest="stable-shadow",
            replay_digest=replay_digest, status="match",
            details={"complete": True, "signature_match": True})
        store.record_replay_evidence(
            candidate_id=candidate["candidate_id"], session_date="2026-01-02",
            replay_digest=replay_digest, vehicle="equity",
            starting_cash=100_000, ending_cash=100_000, realized_pnl=0,
            trades=[{"symbol": "SPY", "session_date": "2026-01-02"}],
            replay_status="match")

        def no_trade(candidate, event, bars, quotes, options):
            del bars, quotes, options
            return ("no_trade", "memo test", {
                "session_date": str(event["timestamp"])[:10],
            }, None)

        with patch.object(runner, "_evaluate", side_effect=no_trade) as evaluate, \
                patch.object(runner, "_replay", return_value=True) as replay:
            first = runner.run_once()
        self.assertEqual(evaluate.call_count, 1)
        replay.assert_called_once()

        # The replay row's retry timestamp changes, but its semantic content
        # stays the same and must not invalidate the memo.
        store.replay_diff(
            candidate_id=candidate["candidate_id"], session_date="2026-01-02",
            source_digest="stable-source", shadow_digest="stable-shadow",
            replay_digest=replay_digest, status="match",
            details={"complete": True, "signature_match": True})
        with patch.object(runner, "_evaluate",
                          side_effect=AssertionError("historical eval repeated")) as evaluate, \
                patch.object(runner, "_replay",
                             side_effect=AssertionError("historical replay repeated")) as replay, \
                patch.object(runner, "_load_events_for_sessions",
                             side_effect=AssertionError(
                                 "historical rows should stay unloaded")) as load_events, \
                patch.object(runner.store, "replay_session_identities",
                             wraps=runner.store.replay_session_identities) as identities:
            second = runner.run_once()
        evaluate.assert_not_called()
        replay.assert_not_called()
        load_events.assert_not_called()
        identities.assert_called_once()
        self.assertEqual(set(identities.call_args.args[0]), {
            (candidate["candidate_id"], "2026-01-02")})
        self.assertTrue(runner._authorizing_replay_memo)
        self.assertEqual(first["candidate_errors"], second["candidate_errors"])

    def test_replay_memo_is_causal_for_new_day_and_late_prior_observation(self):
        candidate = self._candidate()
        closing = self._bar(
            datetime(2026, 1, 2, 20, 59, tzinfo=timezone.utc), 0)
        self._write([closing])
        runner = self._runner()
        store = runner.store
        store.upsert_candidate(candidate)
        store.ingest_event(closing, max_events=100)
        store.save_source_offsets({str(self.corpus.resolve()):
                                   self.corpus.stat().st_size})
        store.save_forward_event_floor(time.time() + 3600)
        store.decision(
            candidate_id=candidate["candidate_id"],
            event_key=closing["event_key"], session_date="2026-01-02",
            symbol="SPY", kind="no_trade", reason="seed",
            payload={"session_date": "2026-01-02"}, max_decisions=100)
        replay_digest = "causal-replay"
        store.replay_diff(
            candidate_id=candidate["candidate_id"], session_date="2026-01-02",
            source_digest="causal-source", shadow_digest="causal-shadow",
            replay_digest=replay_digest, status="match",
            details={"complete": True, "signature_match": True})
        store.record_replay_evidence(
            candidate_id=candidate["candidate_id"], session_date="2026-01-02",
            replay_digest=replay_digest, vehicle="equity",
            starting_cash=100_000, ending_cash=100_000, realized_pnl=0,
            trades=[{"symbol": "SPY", "session_date": "2026-01-02"}],
            replay_status="match")

        def no_trade(candidate, event, bars, quotes, options):
            del bars, quotes, options
            return ("no_trade", "causal memo test", {
                "session_date": str(event["timestamp"])[:10],
            }, None)

        with patch.object(runner, "_evaluate", side_effect=no_trade), \
                patch.object(runner, "_replay", return_value=True):
            runner.run_once()

        new_day = self._bar(
            datetime(2026, 1, 3, 15, 0, tzinfo=timezone.utc), 1)
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writerow(new_day)

        def only_new_day(candidate, event, bars, quotes, options):
            if str(event["event_key"]) == str(closing["event_key"]):
                raise AssertionError("closed prior day was reevaluated")
            return no_trade(candidate, event, bars, quotes, options)

        with patch.object(runner, "_evaluate", side_effect=only_new_day) as evaluate, \
                patch.object(runner, "_replay",
                             side_effect=AssertionError("old replay repeated")):
            runner.run_once()
        self.assertTrue(all(
            str(call.args[1]["event_key"]) == str(new_day["event_key"])
            for call in evaluate.call_args_list
        ))

        late_prior = self._bar(
            datetime(2026, 1, 2, 20, 58, tzinfo=timezone.utc), 2)
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writerow(late_prior)
        with patch.object(runner, "_evaluate", side_effect=no_trade) as evaluate, \
                patch.object(runner, "_replay", return_value=True) as replay, \
                patch.object(runner, "_load_events_for_sessions",
                             wraps=runner._load_events_for_sessions) as load_events:
            runner.run_once()
        self.assertIn(late_prior["event_key"], {
            str(call.args[1]["event_key"]) for call in evaluate.call_args_list})
        replay.assert_called()
        load_events.assert_called()

    def test_health_allowlists_whole_poll_metrics_without_readiness_effect(self):
        path = Path(self.tmp.name) / "shadow-health.json"
        path.write_text(json.dumps({
            "status": "running", "updated_ts": 100.0,
            "poll_phase_metrics": {
                "corpus_read_seconds": 1.25,
                "total_seconds": 2.5,
                "unknown": "discard",
                "ingest_seconds": -1,
            },
        }), encoding="utf-8")
        result = health.shadow(path, 180.0, now=100.0)

        self.assertEqual(result["poll_phase_metrics"], {
            "corpus_read_seconds": 1.25,
            "total_seconds": 2.5,
        })
        self.assertEqual(result["coverage_status"],
                         "diagnostic_metadata_missing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
