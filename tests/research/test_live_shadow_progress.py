"""Focused authorizing-shadow cursor and budget regressions."""

from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deploy.recorder_market import _event_key
from research.edge_ledger import EdgeLedger
from research.live_shadow import ShadowConfig, ShadowError, ShadowRunner


FIELDS = [
    "event_key", "event_type", "symbol", "timestamp", "as_of",
    "observed_at", "provider", "feed", "open", "high", "low",
    "close", "volume", "bid", "ask", "source_mode",
]


class AuthorizingProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _candidate(self) -> dict:
        candidate = EdgeLedger(self.edge).register_candidate(
            "ibr.baseline", strategy_id="ibr", vehicle="equity",
            hypothesis="progress", config={
                "strategy": {"id": "ibr", "version": "v1",
                             "variant_id": "ibr.baseline"},
                "risk": {"risk_per_trade_pct": 1},
                "execution": {}, "session": {},
            })
        with closing(sqlite3.connect(self.edge)) as db, db:
            db.execute("UPDATE candidate_state SET status='backtest_passed' "
                       "WHERE candidate_id=?", (candidate["candidate_id"],))
        return candidate

    @staticmethod
    def _bar(index: int) -> dict[str, str]:
        stamp = datetime(2026, 1, 2, 15, index, tzinfo=timezone.utc)
        return {
            "event_key": _event_key("bar_1m", "SPY", stamp.isoformat()),
            "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": stamp.isoformat(),
            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": (stamp + timedelta(minutes=1)).isoformat(),
            "provider": "alpaca", "feed": "iex", "source_mode": "forward_observed",
            "open": "100", "high": "101", "low": "99", "close": "100",
            "volume": "1000", "bid": "", "ask": "",
        }

    @staticmethod
    def _bar_at(day: int, hour: int, minute: int, *, close: bool = False
                ) -> dict[str, str]:
        stamp = datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc)
        row = {
            "event_key": _event_key("bar_1m", "SPY", stamp.isoformat()),
            "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": stamp.isoformat(),
            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": (stamp + timedelta(minutes=1)).isoformat(),
            "provider": "alpaca", "feed": "iex", "source_mode": "forward_observed",
            "open": "100", "high": "101", "low": "99", "close": "100",
            "volume": "1000", "bid": "", "ask": "",
        }
        if close:
            row["as_of"] = (stamp + timedelta(minutes=1)).isoformat()
        return row

    def _write(self, rows: list[dict[str, str]]) -> None:
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _no_trade(candidate, event, bars, quotes, options):
        del bars, quotes, options
        return ("no_trade", "progress test", {
            "session_date": str(event["timestamp"])[:10],
            "strategy_id": candidate.get("strategy_id"),
        }, None)

    def test_legacy_cap_baseline_preserves_rows_then_accepts_forward_append(self):
        candidate = self._candidate()
        original = [self._bar(index) for index in range(2)]
        self._write(original)
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=2,
            max_decisions=2, max_workers=1))
        for row in original:
            runner.store.ingest_event(row, max_events=2)
        before = [dict(row) for row in runner.store.events()]

        with patch.object(runner, "_evaluate", side_effect=self._no_trade):
            first = runner.run_once()
        self.assertEqual(first["ingested_events"], 0)
        self.assertEqual(before, runner.store.events())

        appended = self._bar(2)
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writerow(appended)
        with patch.object(runner, "_evaluate", side_effect=self._no_trade):
            second = runner.run_once()
        self.assertEqual(second["ingested_events"], 1)
        self.assertEqual(len(runner.store.decisions(candidate["candidate_id"])), 1)
        self.assertTrue(second["authorizing_progress"]["legacy_overflow"] is False)

    def test_legacy_cap_reconciles_append_before_first_upgraded_poll(self):
        """A no-provenance WAL must not baseline a newly appended row away."""
        candidate = self._candidate()
        original = [self._bar(index) for index in range(2)]
        self._write(original)
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=2,
            max_decisions=2, max_workers=1))
        for row in original:
            runner.store.ingest_event(row, max_events=2)

        appended = self._bar(2)
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writerow(appended)
        # Deliberately make the source mtime older than the WAL rows.  Mtime
        # is not an ingestion boundary and must not decide whether this
        # unknown row is reconciled.
        os.utime(self.corpus, (1, 1))
        with patch.object(runner, "_evaluate", side_effect=self._no_trade), \
                patch("research.live_shadow.MAX_LEGACY_RECONCILIATION_BYTES", 256):
            polls = [runner.run_once() for _ in range(8)]

        self.assertEqual(sum(int(poll["ingested_events"]) for poll in polls), 1)
        self.assertEqual(
            {row["event_key"] for row in runner.store.events()},
            {row["event_key"] for row in [*original, appended]})
        self.assertEqual(len(runner.store.decisions(candidate["candidate_id"])), 1)
        with patch.object(runner, "_evaluate", side_effect=self._no_trade):
            repeat = runner.run_once()
        self.assertEqual(repeat["ingested_events"], 0)

    def test_equal_insertion_timestamps_resume_by_event_key(self):
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, authorizing_event_budget=2))
        runner.store.upsert_candidate(candidate)
        timestamp = "2026-01-02T15:00:00+00:00"
        rows = []
        for key in ("tie-a", "tie-b", "tie-c"):
            payload = {
                "event_key": key, "event_type": "quote", "symbol": "SPY",
                "timestamp": timestamp, "as_of": timestamp,
                "observed_at": timestamp, "provider": "alpaca", "feed": "iex",
                "source_mode": "forward_observed", "bid": 99, "ask": 101,
            }
            rows.append((key, key, json.dumps(payload, sort_keys=True,
                                               separators=(",", ":")), timestamp, 7.0))
        with sqlite3.connect(self.shadow) as db:
            db.executemany(
                "INSERT INTO events(event_key,digest,event_json,event_type,symbol,"
                "timestamp,inserted_at) VALUES(?,?,?,'quote','SPY',?,?)",
                rows)
        runner._authorizing_progress_candidates = (dict(candidate),)
        first, *_ = runner._load_events()
        self.assertEqual([row["event_key"] for row in first], ["tie-a", "tie-b"])
        cursor = runner._authorizing_page_meta["last_cursor"]
        epoch = runner.store.authorizing_epoch_identity(candidate)
        runner.store.save_authorizing_progress(
            candidate_id=candidate["candidate_id"], epoch_identity=epoch,
            cursor_inserted_at=cursor[0], cursor_event_key=cursor[1],
            processed_events=2, deferred_events=1, deferred_sessions=[])
        second, *_ = runner._load_events()
        self.assertEqual([row["event_key"] for row in second], ["tie-c"])

    def test_legacy_decision_cap_allows_only_forward_budgeted_overflow(self):
        candidate = self._candidate()
        original = self._bar(0)
        self._write([original])
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=1,
            max_decisions=1, max_workers=1,
            authorizing_event_budget=1, authorizing_decision_budget=1))
        runner.store.ingest_event(original, max_events=1)
        runner.store.decision(
            candidate_id=candidate["candidate_id"], event_key=original["event_key"],
            session_date="2026-01-02", symbol="SPY", kind="no_trade",
            reason="legacy", payload={}, max_decisions=1)
        # Establish the legacy activation boundary before new source bytes.
        runner.run_once()
        appended = self._bar(1)
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writerow(appended)
        with patch.object(runner, "_evaluate", side_effect=self._no_trade):
            result = runner.run_once()
        self.assertTrue(result["authorizing_progress"]["legacy_overflow"])
        self.assertEqual(result["authorizing_progress"]["this_poll_decisions"], 1)
        self.assertEqual(len(runner.store.decisions(candidate["candidate_id"])), 2)

    def test_decision_budget_retries_remaining_rows_idempotently(self):
        candidate = self._candidate()
        self._write([self._bar(index) for index in range(3)])
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=10,
            max_decisions=10, max_workers=1,
            authorizing_event_budget=10, authorizing_decision_budget=1))
        with patch.object(runner, "_evaluate", side_effect=self._no_trade):
            first = runner.run_once()
            second = runner.run_once()
            third = runner.run_once()
        self.assertEqual(first["authorizing_progress"]["this_poll_decisions"], 1)
        self.assertEqual(second["authorizing_progress"]["this_poll_decisions"], 1)
        self.assertEqual(third["authorizing_progress"]["this_poll_decisions"], 1)
        self.assertTrue(first["authorizing_progress"]["backlog_deferred"])
        self.assertEqual(len(runner.store.decisions(candidate["candidate_id"])), 3)
        self.assertFalse(third["authorizing_progress"]["backlog_deferred"])
        self.assertEqual(len({row["event_key"] for row in
                              runner.store.decisions(candidate["candidate_id"])}), 3)

    def test_authorizing_progress_counts_only_new_cursor_interval(self):
        """Retrying one arm's page cannot double-count its event telemetry."""
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=10))
        runner.store.upsert_candidate(candidate)
        for row in (self._bar(0), self._bar(1)):
            runner.store.ingest_event(row, max_events=10)
        rows = runner.store.events_after_cursor(
            inserted_at=0.0, event_key="", max_events=10)
        self.assertEqual(len(rows), 2)
        epoch = runner.store.authorizing_epoch_identity(candidate)
        first = rows[0]
        second = rows[1]
        runner.store.save_authorizing_progress(
            candidate_id=candidate["candidate_id"], epoch_identity=epoch,
            cursor_inserted_at=first["inserted_at"],
            cursor_event_key=first["event_key"], processed_events=1,
            deferred_events=1, deferred_sessions=[])
        # Simulate a sibling retry with the same durable cursor and page.
        runner.store.save_authorizing_progress(
            candidate_id=candidate["candidate_id"], epoch_identity=epoch,
            cursor_inserted_at=first["inserted_at"],
            cursor_event_key=first["event_key"], processed_events=1,
            deferred_events=1, deferred_sessions=[])
        runner.store.save_authorizing_progress(
            candidate_id=candidate["candidate_id"], epoch_identity=epoch,
            cursor_inserted_at=second["inserted_at"],
            cursor_event_key=second["event_key"], processed_events=1,
            deferred_events=0, deferred_sessions=[])
        progress = runner.store.authorizing_progress([candidate])[candidate["candidate_id"]]
        self.assertEqual(progress["processed_events"], 2)

    def test_partial_session_page_defers_replay_until_session_cursor_is_complete(self):
        candidate = self._candidate()
        rows = [self._bar(0), self._bar(1)]
        closing = self._bar(2)
        closing["timestamp"] = "2026-01-02T20:59:00+00:00"
        closing["as_of"] = "2026-01-02T21:00:00+00:00"
        closing["observed_at"] = "2026-01-02T21:01:00+00:00"
        closing["event_key"] = _event_key(
            "bar_1m", "SPY", closing["timestamp"])
        self._write([*rows, closing])
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=10,
            max_decisions=10, max_workers=1, authorizing_event_budget=1))
        with patch.object(runner, "_evaluate", side_effect=self._no_trade), \
                patch.object(runner, "_replay", wraps=runner._replay) as replay:
            first = runner.run_once()
            self.assertTrue(first["authorizing_progress"]["backlog_deferred"])
            replay.assert_not_called()
            runner.run_once()
            replay.assert_not_called()
            runner.run_once()
        replay.assert_called()

    def test_prior_deferred_session_survives_later_complete_session(self):
        candidate = self._candidate()
        session_a = self._bar_at(2, 15, 0)
        session_b_open = self._bar_at(3, 15, 0)
        session_b_close = self._bar_at(3, 20, 59, close=True)
        self._write([session_a, session_b_open, session_b_close])
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=10,
            max_decisions=10, max_workers=1, authorizing_event_budget=1))
        with patch.object(runner, "_evaluate", side_effect=self._no_trade), \
                patch.object(runner, "_replay", wraps=runner._replay) as replay:
            runner.run_once()
            runner.run_once()
            third = runner.run_once()
        self.assertTrue(replay.called)
        self.assertIn("2026-01-02", third["authorizing_progress"][
            "deferred_sessions"])
        self.assertIn("2026-01-02", third["stale_tail"][
            "authorizing_deferred_sessions"])
        self.assertEqual(third["stale_tail"]["status"], "blocked")
        self.assertEqual(third["forward_event_floor"], 0.0)

    def test_direct_decision_cap_remains_fail_closed_without_migration_flag(self):
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_decisions=1))
        runner.store.upsert_candidate(candidate)
        runner.store.decision(
            candidate_id=candidate["candidate_id"], event_key="old",
            session_date="2026-01-02", symbol="SPY", kind="no_trade",
            reason="old", payload={}, max_decisions=1)
        with self.assertRaisesRegex(ShadowError, "decision bound 1 exceeded"):
            runner.store.decision(
                candidate_id=candidate["candidate_id"], event_key="new",
                session_date="2026-01-02", symbol="SPY", kind="no_trade",
                reason="new", payload={}, max_decisions=1)


if __name__ == "__main__":
    unittest.main()
