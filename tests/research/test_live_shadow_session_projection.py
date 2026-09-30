"""Regression coverage for bounded legacy shadow session projection."""

from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from deploy.recorder_market import _event_key
from research.edge_ledger import EdgeLedger
from research.live_shadow import (
    ShadowConfig,
    ShadowRunner,
    ShadowStore,
    _digest,
)


class LiveShadowSessionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def _candidate(self):
        ledger = EdgeLedger(self.edge)
        row = ledger.register_candidate(
            "ibr.baseline", strategy_id="ibr", vehicle="equity",
            hypothesis="bounded session projection",
            config={
                "strategy": {"id": "ibr", "version": "v1",
                             "variant_id": "ibr.baseline"},
                "risk": {"risk_per_trade_pct": 1},
                "execution": {},
                "session": {},
            })
        with closing(sqlite3.connect(self.edge)) as db, db:
            db.execute(
                "UPDATE candidate_state SET status='backtest_passed' "
                "WHERE candidate_id=?", (row["candidate_id"],))
        return row

    @staticmethod
    def _bar(session: str, *, index: int = 0, symbol: str = "SPY") -> dict:
        stamp = datetime.fromisoformat(
            f"{session}T20:59:00+00:00") + timedelta(seconds=index)
        return {
            "event_key": _event_key("bar_1m", symbol, stamp.isoformat()),
            "event_type": "bar_1m",
            "symbol": symbol,
            "timestamp": stamp.isoformat(),
            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": (stamp + timedelta(minutes=2)).isoformat(),
            "provider": "alpaca",
            "feed": "iex",
            "open": "100",
            "high": "103",
            "low": "99",
            "close": "101",
            "volume": "1000",
            "bid": "",
            "ask": "",
        }

    def _write_sessions(self, sessions):
        fields = [
            "event_key", "event_type", "symbol", "timestamp", "as_of",
            "observed_at", "provider", "feed", "open", "high", "low",
            "close", "volume", "bid", "ask",
        ]
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self._bar(session) for session in sessions)

    @staticmethod
    def _decision(candidate_id: str, session: str, event_key: str) -> dict:
        return {
            "candidate_id": candidate_id,
            "event_key": event_key,
            "session_date": session,
            "symbol": "SPY",
            "kind": "no_data",
            "reason": "projection test",
            "payload": {"session": session, "event_key": event_key},
        }

    @staticmethod
    def _seed_gate(runner: ShadowRunner, candidate: dict, session: str) -> None:
        runner.store.upsert_candidate(candidate)
        replay_digest = f"replay-{session}"
        runner.store.replay_diff(
            candidate_id=candidate["candidate_id"], session_date=session,
            source_digest="source", shadow_digest="shadow",
            replay_digest=replay_digest, status="match",
            details={"complete": True, "signature_match": True})
        runner.store.record_replay_evidence(
            candidate_id=candidate["candidate_id"], session_date=session,
            replay_digest=replay_digest, vehicle="equity",
            starting_cash=100_000, ending_cash=100_000,
            realized_pnl=0.0,
            trades=[{
                "vehicle": "equity", "symbol": "SPY",
                "session_date": session,
                "opportunity_id": f"SPY:{session}", "net_pnl": 0.0,
                "return_value": 0.0, "no_trade": True,
            }],
            replay_status="match")

    def test_single_pass_grouping_matches_filtered_order_and_identity(self):
        tie = "2026-01-02T15:00:00+00:00"
        rows_by_symbol = {
            "ZZZ": [
                {"event_key": "later-session", "as_of":
                 "2026-01-03T15:00:00+00:00"},
                {"event_key": "tie-z", "as_of": tie},
            ],
            "AAA": [
                {"event_key": "tie-a", "as_of": tie},
                {"event_key": "outside", "as_of":
                 "2026-01-04T15:00:00+00:00"},
                {"event_key": "later-a", "as_of":
                 "2026-01-03T16:00:00+00:00"},
            ],
        }
        flattened = [row for values in rows_by_symbol.values()
                     for row in values]
        before = _digest(flattened)
        sessions = ("2026-01-02", "2026-01-03")
        expected = {
            session: [
                row for row in flattened
                if datetime.fromisoformat(row["as_of"])
                .astimezone(timezone(timedelta(hours=-5)))
                .date().isoformat() == session
            ]
            for session in sessions
        }

        actual = ShadowRunner._group_rows_by_session(flattened, sessions)

        self.assertEqual(
            {key: [row["event_key"] for row in values]
             for key, values in actual.items()},
            {key: [row["event_key"] for row in values]
             for key, values in expected.items()})
        self.assertEqual([row["event_key"] for row in actual["2026-01-02"]],
                         ["tie-z", "tie-a"])
        for session in sessions:
            for projected, original in zip(actual[session], expected[session]):
                self.assertIs(projected, original)
        self.assertEqual(_digest(flattened), before)

    def test_session_query_matches_full_history_filter_with_ties(self):
        store = ShadowStore(self.shadow)
        candidate_id = "candidate-a"
        with patch("research.live_shadow.time.time", return_value=1234.5):
            for session, event_key in (
                    ("2026-01-03", "other-session"),
                    ("2026-01-02", "tie-z"),
                    ("2026-01-02", "tie-a")):
                store.decision(
                    candidate_id=candidate_id, event_key=event_key,
                    session_date=session, symbol="SPY", kind="no_data",
                    reason="tie", payload={"event_key": event_key},
                    max_decisions=100)
            store.decision(
                candidate_id="candidate-b", event_key="foreign",
                session_date="2026-01-02", symbol="SPY", kind="no_data",
                reason="foreign", payload={}, max_decisions=100)

        full = store.decisions(candidate_id)
        before = _digest(full)
        expected = [row for row in full
                    if row["session_date"] == "2026-01-02"]
        selected = store.decisions_for_session(
            candidate_id, "2026-01-02")

        self.assertEqual(selected, expected)
        self.assertEqual(
            [row["decision_id"] for row in selected],
            sorted(row["decision_id"] for row in selected))
        self.assertTrue(all(row["candidate_id"] == candidate_id
                            for row in selected))
        self.assertEqual(_digest(full), before)

    def test_forward_replay_reads_only_each_candidate_session(self):
        sessions = ["2026-01-02", "2026-01-03"]
        candidate = self._candidate()
        self._write_sessions(sessions)
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow))
        original_decisions = runner.store.decisions
        original_for_session = runner.store.decisions_for_session
        scoped_calls = []
        replay_rows = {}

        def evaluate(arm, session_events, *_args):
            session = next(iter(session_events))
            event = session_events[session][0]
            return {
                "candidate_id": arm["candidate_id"],
                "decisions": [self._decision(
                    arm["candidate_id"], session, event["event_key"])],
            }

        def reject_candidate_scan(candidate_id=None):
            if candidate_id is not None:
                raise AssertionError("candidate history scan is forbidden")
            return original_decisions()

        def scoped(candidate_id, session):
            scoped_calls.append((candidate_id, session))
            return original_for_session(candidate_id, session)

        def replay(_candidate, session, _bars, _quotes, decisions,
                   _options=None, **_kwargs):
            replay_rows[session] = [dict(row) for row in decisions]
            return True

        with patch.object(runner, "_evaluate_arm_snapshot",
                          side_effect=evaluate), patch.object(
                runner.store, "decisions",
                side_effect=reject_candidate_scan), patch.object(
                runner.store, "decisions_for_session",
                side_effect=scoped), patch.object(
                runner, "_replay", side_effect=replay):
            runner.run_once()

        candidate_id = candidate["candidate_id"]
        full = original_decisions(candidate_id)
        self.assertEqual(scoped_calls, [
            (candidate_id, "2026-01-02"),
            (candidate_id, "2026-01-03"),
        ])
        self.assertEqual(set(replay_rows), set(sessions))
        for session in sessions:
            self.assertEqual(
                replay_rows[session],
                [row for row in full if row["session_date"] == session])

    def test_replay_only_uses_recomputed_rows_without_history_read(self):
        session = "2026-01-02"
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=10,
            replay_session_max_events=100))
        self._seed_gate(runner, candidate, session)
        runner.store.ingest_event(self._bar(session), max_events=10)
        runner.store.decision(
            candidate_id=candidate["candidate_id"], event_key="historical",
            session_date="2025-12-31", symbol="SPY", kind="no_data",
            reason="historical", payload={"immutable": True},
            max_decisions=100)
        runner.store.save_forward_event_floor(time.time() + 3600.0)
        candidate_id = candidate["candidate_id"]
        worker_decisions = [
            self._decision(candidate_id, session, "tie-z"),
            self._decision(candidate_id, session, "tie-a"),
        ]
        before = _digest(worker_decisions)
        original_decisions = runner.store.decisions
        replay_rows = []

        def evaluate(_arm, _events, *_args):
            return {"candidate_id": candidate_id,
                    "decisions": worker_decisions}

        def reject_candidate_scan(candidate_id_arg=None):
            if candidate_id_arg is not None:
                raise AssertionError("candidate history scan is forbidden")
            return original_decisions()

        def replay(_candidate, replay_session, _bars, _quotes, decisions,
                   _options=None, **_kwargs):
            self.assertEqual(replay_session, session)
            replay_rows.extend(dict(row) for row in decisions)
            return True

        with patch.object(runner, "_evaluate_arm_snapshot",
                          side_effect=evaluate), patch.object(
                runner.store, "decisions",
                side_effect=reject_candidate_scan), patch.object(
                runner.store, "decisions_for_session",
                side_effect=AssertionError(
                    "recomputed replay must not reread decisions")), patch.object(
                runner, "_replay", side_effect=replay):
            runner.run_once()

        expected = [{
            "candidate_id": candidate_id,
            "event_key": item["event_key"],
            "session_date": session,
            "symbol": "SPY",
            "kind": "no_data",
            "reason": "projection test",
            "payload_json": json.dumps(
                item["payload"], sort_keys=True, separators=(",", ":")),
        } for item in sorted(worker_decisions,
                             key=lambda item: item["event_key"])]
        prior_projection = [
            row for row in original_decisions(candidate_id)
            if row["session_date"] != session]
        prior_projection.extend(expected)
        prior_visible = [row for row in prior_projection
                         if row["session_date"] == session]

        self.assertEqual(replay_rows, prior_visible)
        self.assertEqual([row["event_key"] for row in replay_rows],
                         ["tie-a", "tie-z"])
        self.assertEqual(_digest(worker_decisions), before)
        self.assertEqual(worker_decisions[0]["payload"]["event_key"], "tie-z")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
